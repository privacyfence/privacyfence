"""Org mode's web push: telling a signed-in principal that an approval is waiting, on a phone
whose browser tab is long suspended (notification tier 2, ADR 0081).

Tiers 0 and 1 (web_shell.py's title badge and ``showNotification`` while a tab is open) never
leave the machine (ADR 0064). This tier does: the notification travels from this server to the
browser vendor's push service (Apple, Google, Mozilla or Microsoft) and from there to the device.
So what it carries is fixed here, in one place, by construction:

- **The payload is the minimal level and nothing else.** ``minimal_payload()`` is the only
  function that builds one, and it takes a count, not an approval: a title and "N approval(s)
  pending". No tool name, connector, summary, requester or approval id can reach it, because
  nothing that has them is passed in. ``PushNotifier.on_new_approval`` reads the one field it
  needs off the approval (``principal_id``) and drops the rest. The payload is also encrypted end
  to end to the browser (RFC 8291) and padded to a fixed size, so the push service learns that a
  notification was sent and when, not what it says or how many approvals it counts.
- **Where it may go is an allowlist.** A subscription's endpoint URL comes from a signed-in
  user's browser, so it is attacker-influenced input to a server-side HTTP request.
  ``parse_subscription`` accepts only ``https`` on the default port, with no userinfo, to a host
  under one of ``PUSH_SERVICE_HOST_SUFFIXES``, and every request is sent with redirects off, so a
  subscription cannot point this server at an internal host (SSRF).
- **Nothing about the approval is logged.** A failed delivery logs the push service's host and
  the HTTP status, never the endpoint path (it identifies a device), the payload or the approval.

The cryptography is the repo's existing ``cryptography`` package, not a web-push library: VAPID
(RFC 8292) is one ES256 JWT, and the ``aes128gcm`` content encoding (RFC 8188, as RFC 8291 uses
it) is one ECDH, two HKDFs and one AES-GCM record. ADR 0081 says why that beat a new dependency.
The HTTP call follows the repo's convention (org_identity.py): ``requests`` with a fixed timeout
and default TLS verification against the certifi bundle.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .approvals import PendingApproval, PendingApprovalRegistry
from .paths import safe_principal_id
from .secure_files import atomic_write_bytes, atomic_write_json, secure_mkdir

logger = logging.getLogger(__name__)

# Stored in org_dir(), beside the OAuth client and refresh stores: the server's own secrets, owned
# by the service account, 0600. Never in the org config bundle, which is distributed.
VAPID_KEY_FILE_NAME = "web_push_vapid_key.pem"
# One file per principal, under users/<principal>/.
SUBSCRIPTIONS_FILE_NAME = "push_subscriptions.json"

# The push services the browsers that can install this app actually use. An endpoint whose host
# is not one of these, or a subdomain of one, is refused at subscribe time.
PUSH_SERVICE_HOST_SUFFIXES = (
    "push.apple.com",             # Safari, and every browser on iOS (web.push.apple.com)
    "fcm.googleapis.com",         # Chrome, Android Chrome, most Chromium browsers
    "push.services.mozilla.com",  # Firefox (updates.push.services.mozilla.com)
    "notify.windows.com",         # Edge (*.notify.windows.com)
)

# A person may use a few devices; this bounds what one principal can make the server store and
# send to, whatever their browser posts.
MAX_SUBSCRIPTIONS_PER_PRINCIPAL = 10
_MAX_ENDPOINT_LENGTH = 2048
_HOSTNAME = re.compile(r"^[a-z0-9.-]+$")

# The same rate tier 1 uses (web_shell.py's maybeNotify): at most one notification per principal
# every five seconds, however many approvals arrive in that time.
RATE_LIMIT_SECONDS = 5.0
_HTTP_TIMEOUT_SECONDS = 10
# How long the push service keeps an undelivered message: the default pending-approval lifetime
# (approvals.DEFAULT_PENDING_TTL_SECONDS is 15 minutes). A later notice about an approval that has
# expired would be noise.
DEFAULT_TTL_SECONDS = 900
# RFC 8030 Topic: the push service keeps only the newest undelivered message per topic, so a
# phone that was offline gets one notice, not a backlog.
_TOPIC = "pf-approvals"
_VAPID_TOKEN_LIFETIME_SECONDS = 12 * 3600
_RECORD_SIZE = 4096
# Every payload is padded to this many bytes before encryption, so every push this server sends is
# the same size on the wire: "1 approval pending" and "12 approvals pending" are indistinguishable
# to the push service, which learns that a notification was sent and when, and nothing else.
PADDED_PLAINTEXT_BYTES = 128


class InvalidSubscription(ValueError):
    """A subscription the server refuses to store. The message is safe to show the user."""


# ---------------------------------------------------------------------------------------------- #
# Encoding helpers
# ---------------------------------------------------------------------------------------------- #

def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_\-]*={0,2}", text):
        raise ValueError("not base64url")
    stripped = text.rstrip("=")
    return base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))


def _public_point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ---------------------------------------------------------------------------------------------- #
# VAPID (RFC 8292)
# ---------------------------------------------------------------------------------------------- #

def load_or_create_vapid_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """The server's VAPID key pair, generated on first start and kept at ``path`` (0600). Its
    public half is the ``applicationServerKey`` every browser subscription is bound to, so it must
    stay stable: replacing it invalidates every subscription."""
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError(f"{path} is not a P-256 private key; move it aside to generate a new one")
        return key
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    atomic_write_bytes(path, pem)
    logger.info("Generated this server's web push (VAPID) key at %s", path)
    return key


def application_server_key(key: ec.EllipticCurvePrivateKey) -> str:
    """The public key a browser's ``pushManager.subscribe`` takes, base64url."""
    return b64url_encode(_public_point(key.public_key()))


def vapid_authorization(key: ec.EllipticCurvePrivateKey, endpoint: str, *, subject: str, now: float) -> str:
    """The ``Authorization: vapid t=..., k=...`` header value for one request to ``endpoint``: an
    ES256 JWT whose audience is the push service's origin. ``subject`` is this server's own https
    URL (RFC 8292 section 2.1), so a push service operator can tell whose traffic it is."""
    parts = urlsplit(endpoint)
    header = {"typ": "JWT", "alg": "ES256"}
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(now) + _VAPID_TOKEN_LIFETIME_SECONDS, "sub": subject}
    signing_input = (
        b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    )
    der = key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    token = signing_input + "." + b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={application_server_key(key)}"


# ---------------------------------------------------------------------------------------------- #
# Payload encryption (RFC 8291, aes128gcm content encoding from RFC 8188)
# ---------------------------------------------------------------------------------------------- #

def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt_payload(
    plaintext: bytes, *, ua_public: bytes, auth_secret: bytes, pad_to: int = 0,
    salt: bytes | None = None, as_private: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """Encrypt ``plaintext`` for the browser holding ``ua_public``/``auth_secret`` (the
    subscription's ``p256dh`` and ``auth`` keys). Returns the request body: the RFC 8188 header
    (salt, record size, the sender's ephemeral public key) followed by one AES-128-GCM record.
    ``pad_to`` pads the record with zero bytes (RFC 8188 section 2) to that many bytes of
    plaintext plus delimiter, so messages of different lengths encrypt to the same size.
    ``salt``/``as_private`` exist only so a test can reproduce RFC 8291's Appendix A vector; every
    real call leaves them to be freshly random."""
    salt = salt if salt is not None else os.urandom(16)
    as_private = as_private if as_private is not None else ec.generate_private_key(ec.SECP256R1())
    as_public = _public_point(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, ecdh_secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    # One record, so its padding delimiter is 0x02 (last record), then zero bytes up to pad_to.
    record = plaintext + b"\x02"
    record += b"\x00" * max(0, pad_to - len(record))
    ciphertext = AESGCM(cek).encrypt(nonce, record, None)
    return salt + struct.pack("!IB", _RECORD_SIZE, len(as_public)) + as_public + ciphertext


def minimal_payload(count: int) -> bytes:
    """The only payload a push ever carries: ``notifications_detail``'s ``minimal`` level, the
    same bare count tier 1 shows at that level (web_shell.py's countBody). It takes a number, not
    an approval, so no approval field can reach it."""
    body = "1 approval pending" if count == 1 else f"{int(count)} approvals pending"
    return json.dumps({"title": "PrivacyFence", "body": body}, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------------------------- #
# Subscriptions
# ---------------------------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PushSubscription:
    """``session`` is ``session_tag()`` of the sign-in session that last posted this
    subscription, so signing out can find this browser's subscriptions and only those. Empty on
    a subscription stored before sessions were recorded; such a subscription is never matched."""

    endpoint: str
    p256dh: str
    auth: str
    created_at: float = 0.0
    session: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint, "p256dh": self.p256dh, "auth": self.auth,
            "created_at": self.created_at, "session": self.session,
        }


def session_tag(session_id: str) -> str:
    """What a stored subscription records about the session that posted it: a SHA-256 of the
    session id, never the id itself, which is a live bearer credential and the page's CSRF token.
    The id is 256 random bits, so an unsalted hash cannot be reversed by guessing."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def validate_endpoint(endpoint: Any) -> str:
    """``endpoint`` if the server may send to it, else ``InvalidSubscription``. See this module's
    docstring: https, the default port, no userinfo, and a host under a known push service."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > _MAX_ENDPOINT_LENGTH:
        raise InvalidSubscription("The subscription has no usable endpoint.")
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError as exc:
        raise InvalidSubscription("The subscription endpoint is not a valid URL.") from exc
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        raise InvalidSubscription("The subscription endpoint must be a plain https URL.")
    if port not in (None, 443) or not _HOSTNAME.match(host):
        raise InvalidSubscription("The subscription endpoint must be a plain https URL.")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in PUSH_SERVICE_HOST_SUFFIXES):
        raise InvalidSubscription("This browser's push service is not one PrivacyFence sends to.")
    return endpoint


def parse_subscription(data: Any, *, now: float | None = None) -> PushSubscription:
    """A browser ``PushSubscription.toJSON()`` (``{"endpoint", "keys": {"p256dh", "auth"}}``),
    validated: the endpoint by ``validate_endpoint``, ``p256dh`` as a real P-256 point and
    ``auth`` as 16 bytes."""
    if not isinstance(data, dict):
        raise InvalidSubscription("The subscription is not a JSON object.")
    endpoint = validate_endpoint(data.get("endpoint"))
    keys = data.get("keys")
    if not isinstance(keys, dict):
        raise InvalidSubscription("The subscription has no keys.")
    try:
        p256dh = b64url_decode(keys.get("p256dh", ""))
        auth = b64url_decode(keys.get("auth", ""))
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    except (ValueError, TypeError) as exc:
        raise InvalidSubscription("The subscription's keys are not valid.") from exc
    if len(p256dh) != 65 or len(auth) != 16:
        raise InvalidSubscription("The subscription's keys are not valid.")
    return PushSubscription(
        endpoint=endpoint, p256dh=b64url_encode(p256dh), auth=b64url_encode(auth),
        created_at=time.time() if now is None else now,
    )


def _default_users_dir() -> Path:
    from .paths import data_dir

    return data_dir() / "users"


class PushSubscriptionStore:
    """Per-principal subscriptions, one file each at ``users/<principal>/push_subscriptions.json``
    (0600). ``users_dir`` defaults to the real ``data_dir()/users`` and is read lazily, so a test
    can point it at a temporary directory.

    An endpoint belongs to one principal at a time. Two people signing in to the same browser
    profile get the same endpoint for the same server key, and the later subscription takes it
    over, so the earlier person's notices stop going to a browser someone else now uses.

    Each subscription also records the session that posted it (``PushSubscription.session``), and
    ``remove_session`` drops every subscription a session posted: the browser that signs out stops
    receiving pushes, the same person's other devices do not. ``replace_session`` does the same
    for a sign-in that replaces an earlier session in one browser, except that the same person's
    own subscriptions move to the new session instead."""

    def __init__(self, users_dir: Callable[[], Path] = _default_users_dir) -> None:
        self._users_dir = users_dir
        self._lock = threading.Lock()

    def _file(self, principal_id: str) -> Path:
        if not principal_id or safe_principal_id(principal_id) != principal_id:
            raise ValueError("Unsafe principal id for push subscription storage")
        return self._users_dir() / principal_id / SUBSCRIPTIONS_FILE_NAME

    def _read(self, path: Path) -> list[PushSubscription]:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [
                PushSubscription(
                    endpoint=str(item["endpoint"]), p256dh=str(item["p256dh"]), auth=str(item["auth"]),
                    created_at=float(item.get("created_at", 0.0)), session=str(item.get("session", "")),
                )
                for item in raw.get("subscriptions", [])
            ]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # A damaged file means "no subscriptions": the next subscribe rewrites it. Failing
            # open here only ever means fewer notifications, never a wider audience.
            logger.warning("Could not read %s (%s) -- treating it as empty", path, type(exc).__name__)
            return []

    def _write(self, path: Path, subscriptions: list[PushSubscription]) -> None:
        secure_mkdir(path.parent)
        atomic_write_json(path, {"subscriptions": [s.to_json() for s in subscriptions]}, indent=2)

    def list(self, principal_id: str) -> list[PushSubscription]:
        with self._lock:
            return self._read(self._file(principal_id))

    def add(self, principal_id: str, subscription: PushSubscription) -> None:
        path = self._file(principal_id)
        with self._lock:
            self._remove_from_others_locked(principal_id, subscription.endpoint)
            current = [s for s in self._read(path) if s.endpoint != subscription.endpoint]
            current.append(subscription)
            current.sort(key=lambda s: s.created_at)
            self._write(path, current[-MAX_SUBSCRIPTIONS_PER_PRINCIPAL:])

    def remove(self, principal_id: str, endpoint: str) -> bool:
        path = self._file(principal_id)
        with self._lock:
            current = self._read(path)
            kept = [s for s in current if s.endpoint != endpoint]
            if len(kept) == len(current):
                return False
            self._write(path, kept)
            return True

    def remove_session(self, tag: str) -> int:
        """Remove every subscription posted by the session whose ``session_tag`` is ``tag``,
        whichever principal holds it. Returns how many were removed. Every principal's file is
        checked, not only the signed-in one's, because a session that has already expired no
        longer says whose it was."""
        if not tag:
            return 0
        with self._lock:
            return self._remove_matching_locked(lambda entry, s: s.session == tag)

    def replace_session(self, old_tag: str, new_tag: str, principal_id: str) -> None:
        """A new sign-in (``new_tag``) replaced session ``old_tag`` in the same browser. The
        subscriptions ``old_tag`` posted move to ``new_tag`` if they are ``principal_id``'s own,
        so signing out later still finds them, and are removed if they are anyone else's, so the
        next person to sign in on a browser does not inherit the last one's notices."""
        if not old_tag:
            return
        with self._lock:
            self._remove_matching_locked(lambda entry, s: entry != principal_id and s.session == old_tag)
            path = self._file(principal_id)
            current = self._read(path)
            if any(s.session == old_tag for s in current):
                self._write(path, [replace(s, session=new_tag) if s.session == old_tag else s for s in current])

    def _remove_from_others_locked(self, principal_id: str, endpoint: str) -> None:
        self._remove_matching_locked(lambda entry, s: entry != principal_id and s.endpoint == endpoint)

    def _remove_matching_locked(self, matches: Callable[[str, PushSubscription], bool]) -> int:
        users = self._users_dir()
        if not users.is_dir():
            return 0
        removed = 0
        for entry in users.iterdir():
            if safe_principal_id(entry.name) != entry.name:
                continue
            path = entry / SUBSCRIPTIONS_FILE_NAME
            if not path.exists():
                continue
            current = self._read(path)
            kept = [s for s in current if not matches(entry.name, s)]
            if len(kept) != len(current):
                self._write(path, kept)
                removed += len(current) - len(kept)
        return removed


# ---------------------------------------------------------------------------------------------- #
# Sending
# ---------------------------------------------------------------------------------------------- #

PostFunction = Callable[..., Any]


class PushNotifier:
    """Sends one minimal push to each of a principal's subscriptions when an approval is created
    for them. Registered with ``PendingApprovalRegistry.add_created_listener`` by org mode's boot
    path (daemon_main.py), never by local mode's.

    ``on_new_approval`` runs on the thread that registered the approval (a gated tool call), so it
    only applies the rate limit and hands delivery to a single background worker. ``deliver`` does
    the network work and is also what tests call directly."""

    def __init__(
        self, *, store: PushSubscriptionStore, vapid_key: ec.EllipticCurvePrivateKey, subject: str,
        registry: PendingApprovalRegistry, ttl_seconds: int = DEFAULT_TTL_SECONDS,
        post: PostFunction | None = None, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._key = vapid_key
        self._subject = subject
        self._registry = registry
        self._ttl = int(ttl_seconds)
        self._post = post if post is not None else requests.post
        self._clock = clock
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-push")

    @property
    def public_key(self) -> str:
        return application_server_key(self._key)

    def on_new_approval(self, approval: PendingApproval) -> None:
        # The principal is the only field read off the approval. Everything else on it (tool,
        # connector, summary, preview, requester) stays on this server.
        principal_id = approval.principal_id
        if not self._claim_slot(principal_id):
            return
        self._executor.submit(self._deliver_logged, principal_id)

    def _claim_slot(self, principal_id: str) -> bool:
        if not principal_id:
            return False
        now = self._clock()
        with self._lock:
            last = self._last_sent.get(principal_id)
            if last is not None and now - last < RATE_LIMIT_SECONDS:
                return False
            self._last_sent[principal_id] = now
            return True

    def _deliver_logged(self, principal_id: str) -> None:
        try:
            self.deliver(principal_id)
        except Exception:
            logger.exception("Web push delivery failed")

    def _pending_count(self, principal_id: str) -> int:
        return sum(1 for a in self._registry.list_pending(principal_id) if a.kind == "card")

    def deliver(self, principal_id: str) -> int:
        """Push the principal's current pending count to each of their subscriptions. Returns how
        many the push services accepted. A 404 or 410 means the subscription is gone for good (RFC
        8030 section 7.3), so it is dropped."""
        count = self._pending_count(principal_id)
        if count == 0:
            return 0
        subscriptions = self._store.list(principal_id)
        if not subscriptions:
            return 0
        payload = minimal_payload(count)
        accepted = 0
        for subscription in subscriptions:
            host = urlsplit(subscription.endpoint).hostname or "?"
            try:
                # Re-checked at send time too: a file written by an older version, or by hand,
                # never gets a request it would have been refused at subscribe time.
                validate_endpoint(subscription.endpoint)
                body = encrypt_payload(
                    payload, ua_public=b64url_decode(subscription.p256dh),
                    auth_secret=b64url_decode(subscription.auth), pad_to=PADDED_PLAINTEXT_BYTES,
                )
            except ValueError:
                logger.warning("Dropping an unusable web push subscription for %s", host)
                self._store.remove(principal_id, subscription.endpoint)
                continue
            headers = {
                "Authorization": vapid_authorization(
                    self._key, subscription.endpoint, subject=self._subject, now=time.time(),
                ),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(self._ttl),
                "Urgency": "high",
                "Topic": _TOPIC,
            }
            try:
                response = self._post(
                    subscription.endpoint, data=body, headers=headers,
                    timeout=_HTTP_TIMEOUT_SECONDS, allow_redirects=False,
                )
            except requests.RequestException as exc:
                logger.warning("Web push to %s failed: %s", host, type(exc).__name__)
                continue
            status = getattr(response, "status_code", 0)
            if status in (404, 410):
                logger.info("A web push subscription at %s has expired; removing it", host)
                self._store.remove(principal_id, subscription.endpoint)
            elif 200 <= status < 300:
                accepted += 1
            else:
                logger.warning("Web push to %s was refused: HTTP %s", host, status)
        return accepted

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "InvalidSubscription",
    "MAX_SUBSCRIPTIONS_PER_PRINCIPAL",
    "PADDED_PLAINTEXT_BYTES",
    "PUSH_SERVICE_HOST_SUFFIXES",
    "PushNotifier",
    "PushSubscription",
    "PushSubscriptionStore",
    "RATE_LIMIT_SECONDS",
    "SUBSCRIPTIONS_FILE_NAME",
    "VAPID_KEY_FILE_NAME",
    "application_server_key",
    "encrypt_payload",
    "load_or_create_vapid_key",
    "minimal_payload",
    "parse_subscription",
    "session_tag",
    "validate_endpoint",
    "vapid_authorization",
]
