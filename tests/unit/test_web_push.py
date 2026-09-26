"""Tests for web_push.py -- org mode's web push (ADR 0081): the RFC 8291 encryption and RFC 8292
VAPID token the repo implements on ``cryptography``, the subscription endpoint allowlist (the SSRF
boundary), the per-principal subscription store, and ``PushNotifier``, above all that what it
sends is the minimal payload and nothing about the approval.

The route side (auth, CSRF, the org-wide switch) is tests/unit/web/test_routes_push.py.
"""
from __future__ import annotations

import base64
import json
import logging
import stat
import struct
import sys

import pytest
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from privacyfence import web_push
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.principal import Principal, principal_scope
from privacyfence.web_push import (
    InvalidSubscription,
    PushNotifier,
    PushSubscription,
    PushSubscriptionStore,
    b64url_decode,
    b64url_encode,
    encrypt_payload,
    minimal_payload,
    parse_subscription,
    validate_endpoint,
)

FCM = "https://fcm.googleapis.com/fcm/send/abc123"
APPLE = "https://web.push.apple.com/QGuQyavXutnMH6Ig2uYOJDwA"

# RFC 8291 Appendix A.
RFC_PLAINTEXT = b"When I grow up, I want to be a watermelon"
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_MESSAGE = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tlz"
    "AC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)

# What an approval carries that must never reach a push service.
SECRETS = ("gmail_send_message", "Quarterly salaries.xlsx", "ceo@acme.example", "claude-desktop", "sk-secret")


def _private(b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64url_decode(b64), "big"), ec.SECP256R1())


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return b64url_decode(web_push.application_server_key(key))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """The browser's side of RFC 8291, written out independently of web_push.encrypt_payload."""
    salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
    assert rs == 4096 and len(ciphertext) <= rs
    ua_public = _point(ua_private)
    shared = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    padded = AESGCM(cek).decrypt(nonce, ciphertext, None).rstrip(b"\x00")  # RFC 8188 padding
    assert padded.endswith(b"\x02")
    return padded[:-1]


class Browser:
    """A subscribing browser: its own key pair and auth secret, and the subscription JSON it posts."""

    def __init__(self, endpoint: str = FCM) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"\x07" * 16
        self.endpoint = endpoint

    def subscription_json(self) -> dict:
        return {"endpoint": self.endpoint, "keys": {"p256dh": b64url_encode(_point(self.private)), "auth": b64url_encode(self.auth)}}

    def subscription(self) -> PushSubscription:
        return parse_subscription(self.subscription_json())


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class RecordingPost:
    def __init__(self, status: int | dict = 201) -> None:
        self.calls: list[dict] = []
        self.status = status

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        status = self.status.get(url, 201) if isinstance(self.status, dict) else self.status
        return FakeResponse(status)


class InlineExecutor:
    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, fn, *args):
        self.submitted += 1
        fn(*args)

    def shutdown(self, **kwargs):
        pass


def _registry() -> PendingApprovalRegistry:
    return PendingApprovalRegistry(hold_window=1.0, pending_ttl=60.0, ledger_ttl=1.0, max_pending=20)


def _register(registry: PendingApprovalRegistry, principal_id: str, *, tool: str = "gmail_send_message", n: int = 0):
    with principal_scope(Principal(id=principal_id)):
        approval, created = registry.register_or_coalesce(
            dedupe_key=f"k{n}", connector="gmail", tool=tool, gate_kind="popup", request_id=f"r{n}",
            summary="Quarterly salaries.xlsx to ceo@acme.example", tool_name=tool,
            preview={"to": "ceo@acme.example", "token": "sk-secret"}, claude_reason="sk-secret",
        )
    assert created
    return approval


@pytest.fixture
def store(tmp_path) -> PushSubscriptionStore:
    return PushSubscriptionStore(lambda: tmp_path / "users")


@pytest.fixture
def vapid_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _notifier(store, vapid_key, registry, post, clock=lambda: 0.0) -> PushNotifier:
    notifier = PushNotifier(
        store=store, vapid_key=vapid_key, subject="https://pf.example.com", registry=registry, post=post, clock=clock,
    )
    notifier._executor = InlineExecutor()  # deliver on the calling thread, so the test sees it
    return notifier


class TestEncryption:
    def test_reproduces_rfc_8291_appendix_a(self):
        body = encrypt_payload(
            RFC_PLAINTEXT, ua_public=b64url_decode(RFC_UA_PUBLIC), auth_secret=b64url_decode(RFC_AUTH),
            salt=b64url_decode(RFC_SALT), as_private=_private(RFC_AS_PRIVATE),
        )
        assert b64url_encode(body) == RFC_MESSAGE

    def test_the_browser_can_decrypt_a_fresh_message(self):
        browser = Browser()
        body = encrypt_payload(b"hello", ua_public=_point(browser.private), auth_secret=browser.auth)
        assert decrypt(body, browser.private, browser.auth) == b"hello"

    def test_every_message_has_a_fresh_salt_and_key(self):
        browser = Browser()
        a = encrypt_payload(b"x", ua_public=_point(browser.private), auth_secret=browser.auth)
        b = encrypt_payload(b"x", ua_public=_point(browser.private), auth_secret=browser.auth)
        assert a[:16] != b[:16] and a[21:86] != b[21:86]

    def test_rfc_vector_decrypts_with_the_decrypt_helper_too(self):
        # Guards the helper the payload tests below rely on.
        assert decrypt(b64url_decode(RFC_MESSAGE), _private(RFC_UA_PRIVATE), b64url_decode(RFC_AUTH)) == RFC_PLAINTEXT


class TestVapid:
    def test_key_is_generated_once_and_owner_only(self, tmp_path):
        path = tmp_path / "org" / web_push.VAPID_KEY_FILE_NAME
        first = web_push.load_or_create_vapid_key(path)
        again = web_push.load_or_create_vapid_key(path)
        assert web_push.application_server_key(first) == web_push.application_server_key(again)
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_non_p256_key_is_refused(self, tmp_path):
        from cryptography.hazmat.primitives import serialization

        path = tmp_path / "key.pem"
        other = ec.generate_private_key(ec.SECP384R1())
        path.write_bytes(other.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        with pytest.raises(ValueError, match="P-256"):
            web_push.load_or_create_vapid_key(path)

    def test_application_server_key_is_an_uncompressed_point(self, vapid_key):
        raw = b64url_decode(web_push.application_server_key(vapid_key))
        assert len(raw) == 65 and raw[0] == 4

    def test_authorization_is_a_valid_es256_jwt_for_the_push_service_origin(self, vapid_key):
        header = web_push.vapid_authorization(vapid_key, FCM, subject="https://pf.example.com", now=1000)
        assert header.startswith("vapid t=")
        token, k = header[len("vapid t="):].split(", k=")
        assert k == web_push.application_server_key(vapid_key)
        signing_input, sig = token.rsplit(".", 1)
        head, claims = (json.loads(b64url_decode(part)) for part in signing_input.split("."))
        assert head == {"typ": "JWT", "alg": "ES256"}
        assert claims == {"aud": "https://fcm.googleapis.com", "exp": 1000 + 12 * 3600, "sub": "https://pf.example.com"}
        raw = b64url_decode(sig)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        vapid_key.public_key().verify(der, signing_input.encode(), ec.ECDSA(hashes.SHA256()))


class TestEndpointAllowlist:
    @pytest.mark.parametrize("endpoint", [
        FCM, APPLE,
        "https://updates.push.services.mozilla.com/wpush/v2/gAAAA",
        "https://wns2-db5p.notify.windows.com/w/?token=BQYAAA",
        "https://fcm.googleapis.com:443/wp/abc",
    ])
    def test_the_browser_push_services_are_accepted(self, endpoint):
        assert validate_endpoint(endpoint) == endpoint

    @pytest.mark.parametrize("endpoint", [
        "http://fcm.googleapis.com/fcm/send/abc",           # not https
        "https://fcm.googleapis.com:8443/fcm/send/abc",     # another port
        "https://user:pw@fcm.googleapis.com/fcm/send/abc",  # userinfo
        "https://127.0.0.1/push",                           # loopback
        "https://169.254.169.254/latest/meta-data",         # cloud metadata
        "https://[::1]/push",
        "https://localhost/push",
        "https://intranet.acme.local/push",
        "https://fcm.googleapis.com.evil.example/x",        # suffix lookalike
        "https://evilfcm.googleapis.com/x",                 # not a subdomain
        "https://push.apple.com@evil.example/x",
        "file:///etc/passwd",
        "",
        None,
        "https://fcm.googleapis.com/" + "a" * 3000,
    ])
    def test_anything_else_is_refused(self, endpoint):
        with pytest.raises(InvalidSubscription):
            validate_endpoint(endpoint)

    @pytest.mark.parametrize("keys", [
        None,
        {"p256dh": "", "auth": ""},
        {"p256dh": b64url_encode(b"\x04" + b"\x00" * 64), "auth": b64url_encode(b"x" * 16)},  # not on the curve
        {"p256dh": "not base64!", "auth": b64url_encode(b"x" * 16)},
    ])
    def test_bad_keys_are_refused(self, keys):
        with pytest.raises(InvalidSubscription):
            parse_subscription({"endpoint": FCM, "keys": keys})

    def test_short_auth_is_refused(self):
        data = Browser().subscription_json()
        data["keys"]["auth"] = b64url_encode(b"x" * 8)
        with pytest.raises(InvalidSubscription):
            parse_subscription(data)


class TestSubscriptionStore:
    def test_add_list_remove_per_principal(self, store, tmp_path):
        alice, bob = Browser(FCM), Browser(APPLE)
        store.add("alice", alice.subscription())
        store.add("bob", bob.subscription())
        assert [s.endpoint for s in store.list("alice")] == [FCM]
        assert [s.endpoint for s in store.list("bob")] == [APPLE]
        path = tmp_path / "users" / "alice" / web_push.SUBSCRIPTIONS_FILE_NAME
        assert path.exists()
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert store.remove("alice", FCM) is True
        assert store.remove("alice", FCM) is False
        assert store.list("alice") == [] and len(store.list("bob")) == 1

    def test_the_same_endpoint_is_stored_once(self, store):
        browser = Browser()
        store.add("alice", browser.subscription())
        store.add("alice", browser.subscription())
        assert len(store.list("alice")) == 1

    def test_a_later_principal_takes_over_a_shared_browser(self, store):
        browser = Browser()
        store.add("alice", browser.subscription())
        store.add("bob", browser.subscription())
        assert store.list("alice") == []
        assert [s.endpoint for s in store.list("bob")] == [FCM]

    def test_capped_per_principal_keeping_the_newest(self, store):
        for i in range(web_push.MAX_SUBSCRIPTIONS_PER_PRINCIPAL + 3):
            sub = parse_subscription(Browser(f"{FCM}/{i}").subscription_json(), now=float(i))
            store.add("alice", sub)
        endpoints = [s.endpoint for s in store.list("alice")]
        assert len(endpoints) == web_push.MAX_SUBSCRIPTIONS_PER_PRINCIPAL
        assert endpoints[-1] == f"{FCM}/{web_push.MAX_SUBSCRIPTIONS_PER_PRINCIPAL + 2}"

    @pytest.mark.parametrize("principal_id", ["", "..", "../bob", "a/b"])
    def test_unsafe_principal_ids_are_refused(self, store, principal_id):
        with pytest.raises(ValueError):
            store.list(principal_id)

    def test_a_damaged_file_reads_as_empty(self, store, tmp_path):
        path = tmp_path / "users" / "alice" / web_push.SUBSCRIPTIONS_FILE_NAME
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert store.list("alice") == []


class TestPayloadIsMinimal:
    def test_minimal_payload_is_a_title_and_a_count(self):
        assert json.loads(minimal_payload(1)) == {"title": "PrivacyFence", "body": "1 approval pending"}
        assert json.loads(minimal_payload(3)) == {"title": "PrivacyFence", "body": "3 approvals pending"}

    def test_what_reaches_the_push_service_decrypts_to_the_minimal_payload_and_nothing_else(
        self, store, vapid_key, caplog,
    ):
        registry = _registry()
        browser = Browser()
        store.add("alice", browser.subscription())
        post = RecordingPost()
        registry.add_created_listener(_notifier(store, vapid_key, registry, post).on_new_approval)
        with caplog.at_level(logging.DEBUG):
            _register(registry, "alice")
        assert len(post.calls) == 1
        call = post.calls[0]
        assert call["url"] == FCM
        plaintext = decrypt(call["data"], browser.private, browser.auth)
        assert plaintext == minimal_payload(1)
        assert set(json.loads(plaintext)) == {"title", "body"}
        # Nothing about the approval anywhere in what leaves the server, or in the log.
        sent = call["data"] + json.dumps(call["headers"]).encode() + plaintext
        logged = caplog.text
        for secret in SECRETS + (registry.list_pending("alice")[0].id,):
            assert secret.encode() not in sent
            assert secret not in logged

    def test_the_count_is_the_principals_own(self, store, vapid_key):
        registry = _registry()
        browser = Browser()
        store.add("alice", browser.subscription())
        post = RecordingPost()
        notifier = _notifier(store, vapid_key, registry, post)
        _register(registry, "alice", n=1)
        _register(registry, "alice", n=2)
        _register(registry, "bob", n=3)
        notifier.deliver("alice")
        assert decrypt(post.calls[-1]["data"], browser.private, browser.auth) == minimal_payload(2)

    def test_every_push_is_the_same_size_whatever_the_count(self, store, vapid_key):
        # The push service sees ciphertext length. Padding makes "1" and "12" indistinguishable.
        registry = _registry()
        store.add("alice", Browser().subscription())
        post = RecordingPost()
        notifier = _notifier(store, vapid_key, registry, post)
        _register(registry, "alice", n=0)
        notifier.deliver("alice")
        for n in range(1, 12):
            _register(registry, "alice", n=n)
        notifier.deliver("alice")
        sizes = {len(call["data"]) for call in post.calls}
        assert len(post.calls) == 2 and len(sizes) == 1
        assert len(minimal_payload(999)) + 1 <= web_push.PADDED_PLAINTEXT_BYTES

    def test_headers_and_transport(self, store, vapid_key):
        registry = _registry()
        store.add("alice", Browser().subscription())
        post = RecordingPost()
        _register(registry, "alice")
        _notifier(store, vapid_key, registry, post).deliver("alice")
        call = post.calls[0]
        assert call["allow_redirects"] is False  # a push service cannot bounce us to an internal host
        assert call["timeout"] == web_push._HTTP_TIMEOUT_SECONDS
        assert call["headers"]["Content-Encoding"] == "aes128gcm"
        assert call["headers"]["TTL"] == "900"
        assert call["headers"]["Topic"] == "pf-approvals"
        assert call["headers"]["Authorization"].startswith("vapid t=")


class TestDelivery:
    def test_other_principals_devices_get_nothing(self, store, vapid_key):
        registry = _registry()
        store.add("bob", Browser().subscription())
        post = RecordingPost()
        registry.add_created_listener(_notifier(store, vapid_key, registry, post).on_new_approval)
        _register(registry, "alice")
        assert post.calls == []

    @pytest.mark.parametrize("status", [404, 410])
    def test_a_gone_subscription_is_dropped(self, store, vapid_key, status):
        registry = _registry()
        gone, kept = Browser(FCM), Browser(APPLE)
        store.add("alice", gone.subscription())
        store.add("alice", kept.subscription())
        post = RecordingPost({FCM: status, APPLE: 201})
        _register(registry, "alice")
        assert _notifier(store, vapid_key, registry, post).deliver("alice") == 1
        assert [s.endpoint for s in store.list("alice")] == [APPLE]

    @pytest.mark.parametrize("status", [400, 403, 413, 429, 500])
    def test_other_failures_keep_the_subscription(self, store, vapid_key, status):
        registry = _registry()
        store.add("alice", Browser().subscription())
        _register(registry, "alice")
        assert _notifier(store, vapid_key, registry, RecordingPost(status)).deliver("alice") == 0
        assert len(store.list("alice")) == 1

    def test_a_network_error_is_logged_without_the_endpoint_path(self, store, vapid_key, caplog):
        registry = _registry()
        store.add("alice", Browser().subscription())
        _register(registry, "alice")

        def failing_post(url, **kwargs):
            raise requests.ConnectionError(f"cannot reach {url}")

        with caplog.at_level(logging.WARNING):
            assert _notifier(store, vapid_key, registry, failing_post).deliver("alice") == 0
        assert "fcm.googleapis.com" in caplog.text and "abc123" not in caplog.text
        assert len(store.list("alice")) == 1

    def test_a_stored_endpoint_off_the_allowlist_is_never_requested(self, store, vapid_key, tmp_path):
        registry = _registry()
        path = tmp_path / "users" / "alice" / web_push.SUBSCRIPTIONS_FILE_NAME
        path.parent.mkdir(parents=True)
        good = Browser().subscription()
        path.write_text(json.dumps({"subscriptions": [
            {"endpoint": "https://169.254.169.254/x", "p256dh": good.p256dh, "auth": good.auth},
        ]}))
        post = RecordingPost()
        _register(registry, "alice")
        _notifier(store, vapid_key, registry, post).deliver("alice")
        assert post.calls == [] and store.list("alice") == []

    def test_nothing_is_sent_when_nothing_is_pending(self, store, vapid_key):
        registry = _registry()
        store.add("alice", Browser().subscription())
        post = RecordingPost()
        assert _notifier(store, vapid_key, registry, post).deliver("alice") == 0
        assert post.calls == []


class TestRateLimit:
    def test_one_push_per_principal_per_window_like_tier_1(self, store, vapid_key):
        registry = _registry()
        store.add("alice", Browser(FCM).subscription())
        store.add("bob", Browser(APPLE).subscription())
        now = [100.0]
        post = RecordingPost()
        notifier = _notifier(store, vapid_key, registry, post, clock=lambda: now[0])
        registry.add_created_listener(notifier.on_new_approval)
        _register(registry, "alice", n=1)
        now[0] += web_push.RATE_LIMIT_SECONDS - 0.1
        _register(registry, "alice", n=2)
        _register(registry, "bob", n=3)  # a different principal has its own window
        assert [c["url"] for c in post.calls] == [FCM, APPLE]
        now[0] += 0.2
        _register(registry, "alice", n=4)
        assert [c["url"] for c in post.calls] == [FCM, APPLE, FCM]


class TestRegistryListener:
    def test_called_once_per_new_card_only(self):
        registry = _registry()
        seen: list[str] = []
        registry.add_created_listener(lambda approval: seen.append(approval.principal_id))
        with principal_scope(Principal(id="alice")):
            registry.register_or_coalesce(dedupe_key="k", connector="c", tool="t", gate_kind="review", request_id="r")
            registry.register_or_coalesce(dedupe_key="k", connector="c", tool="t", gate_kind="review", request_id="r")
            registry.register_confirm()
        assert seen == ["alice"]

    def test_a_failing_listener_does_not_break_registration(self):
        registry = _registry()

        def boom(_approval):
            raise RuntimeError("listener bug")

        registry.add_created_listener(boom)
        with principal_scope(Principal(id="alice")):
            approval, created = registry.register_or_coalesce(
                dedupe_key="k", connector="c", tool="t", gate_kind="review", request_id="r",
            )
        assert created and registry.get(approval.id) is approval


def test_b64url_round_trip():
    for raw in (b"", b"\x00", b"\xff" * 17):
        assert b64url_decode(b64url_encode(raw)) == raw
    assert b64url_decode(base64.urlsafe_b64encode(b"ab").decode()) == b"ab"  # padded input too
