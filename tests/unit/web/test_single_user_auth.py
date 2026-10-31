"""Tests for web/single_user_auth.py: single-user mode's sign-in with a setup code from the log,
then a passkey."""
from __future__ import annotations

import logging
import re
import threading
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import grist_provisioning, paths, peek_provisioning, webauthn_stepup as wa
from privacyfence.audit_log import get_audit_logger, init_audit_logger
from privacyfence.principal import principal_scope
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, single_user_auth as sua

ISSUER = "http://localhost:8765"

# Copy of gristlabs/grist-core app/common/gutil.ts EMAIL_RE (JS \w is ASCII-only, hence re.ASCII).
GRIST_EMAIL_RE = re.compile(r"^\w[\w%+/='-]*(\.[\w%+/='-]+)*@([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$", re.ASCII)


class TestTheAddress:
    def test_single_user_email_passes_grist_email_check(self):
        assert sua.SINGLE_USER_PRINCIPAL.email == "admin@privacyfence.localhost"
        assert GRIST_EMAIL_RE.match(sua.SINGLE_USER_PRINCIPAL.email)
        assert not GRIST_EMAIL_RE.match("admin@localhost")
HTTPS_ISSUER = "https://pf.example.com"
ADMIN = sua.SINGLE_USER_PRINCIPAL
CODE_LINE = re.compile(r"Setup code: ([0-9A-F]{4}-[0-9A-F]{4})\. Open (\S+)/login to set up your passkey\.")


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    init_audit_logger(str(tmp_path / "audit"))
    return tmp_path


class FakePushStore:
    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    def replace_session(self, old, new, principal_id):
        if self.fail:
            raise OSError("disk full")
        self.calls.append(("replace", old, new, principal_id))

    def remove_session(self, tag):
        self.calls.append(("remove", tag))


class World:
    """The single-user routes on their own, with the code read back out of the log."""

    def __init__(self, caplog, *, issuer=ISSUER, rp_id="localhost", push_store=None):
        self.sessions = org_session.OrgSessionStore()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=sua.__name__):
            routes = sua.build_routes(
                sessions=self.sessions, issuer_url=issuer,
                step_up=StepUpConfig(enabled=True, require_passkey=True, rp_id=rp_id, rp_name="PrivacyFence"),
                default_next_path="/connect", push_store=push_store,
            )
        matches = [CODE_LINE.search(r.getMessage()) for r in caplog.records]
        found = [m for m in matches if m]
        self.code_log_lines = len(found)
        self.code = found[0].group(1) if found else None
        self.logged_origin = found[0].group(2) if found else None
        self.client = TestClient(Starlette(routes=routes), base_url=issuer, follow_redirects=False)
        self.bodies: list[str] = []

    def get(self, *args, **kwargs):
        r = self.client.get(*args, **kwargs)
        self._keep(r)
        return r

    def post(self, *args, **kwargs):
        r = self.client.post(*args, **kwargs)
        self._keep(r)
        return r

    def _keep(self, r):
        self.bodies.append(r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items()))

    def binding(self, next_path: str | None = None) -> str:
        r = self.get("/login", params={"next": next_path} if next_path else None)
        assert r.status_code == 200
        return re.search(r'data-binding="([^"]+)"', r.text).group(1)

    def setup(self, code: str, *, next_path: str = "/connect", binding: str | None = None, headers=None):
        binding = binding if binding is not None else self.binding(next_path)
        return self.post(
            "/login/setup", data={"code": code, "next": next_path, "binding": binding}, headers=headers,
        )


def _enrol():
    wa.add_credential(ADMIN, wa.WebAuthnCredential(
        credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
    ))


def _audit_entries() -> list[tuple[str, str]]:
    with principal_scope(ADMIN):
        return [(e.decision, e.summary) for e in get_audit_logger().recent_entries(200)]


def _session_principal(world: World, response):
    session_id = response.cookies.get(org_session.SESSION_COOKIE)
    return world.sessions.get(session_id) if session_id else None


class TestTheSetupCode:
    def test_it_is_logged_once_with_the_sign_in_address(self, caplog):
        world = World(caplog)
        assert world.code_log_lines == 1
        assert re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", world.code)
        assert world.logged_origin == ISSUER

    def test_it_never_appears_in_a_response_or_an_audit_summary(self, caplog):
        world = World(caplog)
        world.setup("0000-0000" if world.code != "0000-0000" else "1111-1111")
        world.post("/login/setup", data={"code": world.code, "next": "/connect", "binding": "wrong"})
        r = world.setup(world.code)
        assert r.status_code == 303
        world.get("/login")
        world.post("/logout")
        world.get("/signed-out")
        assert all(world.code not in body for body in world.bodies)
        assert all(world.code not in summary for _, summary in _audit_entries())

    def test_none_is_made_when_a_passkey_is_already_enrolled(self, caplog):
        _enrol()
        world = World(caplog)
        assert world.code is None
        r = world.get("/login")
        assert "Sign in with your passkey" in r.text
        assert 'name="code"' not in r.text

    @pytest.mark.parametrize("transform", [str.lower, lambda c: c.replace("-", ""), lambda c: f"  {c.lower()}\n"])
    def test_lowercase_dashless_and_padded_forms_are_accepted(self, caplog, transform):
        world = World(caplog)
        r = world.setup(transform(world.code))
        assert r.status_code == 303
        assert _session_principal(world, r) == ADMIN

    def test_a_non_ascii_value_is_a_wrong_attempt_not_an_error(self, caplog):
        world = World(caplog)
        r = world.setup("ＡＢＣＤ-１２３４")
        assert r.status_code == 400
        assert sua.WRONG_CODE_MESSAGE in r.text
        r = world.setup("ß" * 8)
        assert r.status_code == 400

    def test_a_missing_code_field_is_a_wrong_attempt(self, caplog):
        world = World(caplog)
        r = world.post("/login/setup", data={"binding": world.binding()})
        assert r.status_code == 400
        assert sua.WRONG_CODE_MESSAGE in r.text

    def test_it_works_only_once(self, caplog):
        world = World(caplog)
        assert world.setup(world.code).status_code == 303
        world.client.cookies.clear()
        again = world.setup(world.code)
        assert again.status_code == 400
        assert sua.USED_UP_MESSAGE in again.text
        assert again.cookies.get(org_session.SESSION_COOKIE) is None

    def test_the_fifth_wrong_code_uses_it_up_and_then_even_the_right_one_is_refused(self, caplog):
        world = World(caplog)
        wrong = "0000-0000" if world.code != "0000-0000" else "1111-1111"
        for _ in range(4):
            r = world.setup(wrong)
            assert r.status_code == 400
            assert sua.WRONG_CODE_MESSAGE in r.text
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=sua.__name__):
            fifth = world.setup(wrong)
        assert sua.USED_UP_MESSAGE in fifth.text
        assert "Setup code used up after 5 wrong attempts; restart PrivacyFence for a new one." in caplog.text
        right = world.setup(world.code)
        assert right.status_code == 400
        assert sua.USED_UP_MESSAGE in right.text
        assert right.cookies.get(org_session.SESSION_COOKIE) is None
        page = world.get("/login")
        assert 'name="code"' not in page.text
        assert sua.USED_UP_MESSAGE in page.text

    def test_an_enrolled_then_wiped_passkey_does_not_bring_the_old_code_back(self, caplog):
        world = World(caplog)
        _enrol()
        world.get("/login")  # any request notices the passkey
        wa.remove_credential(ADMIN, "Y3JlZC0x")  # the recovery code's wipe
        r = world.setup(world.code)
        assert r.status_code == 400
        assert sua.USED_UP_MESSAGE in r.text
        assert r.cookies.get(org_session.SESSION_COOKIE) is None

    def test_after_enrollment_the_form_is_gone_and_a_setup_post_is_refused(self, caplog):
        world = World(caplog)
        _enrol()
        page = world.get("/login")
        assert 'name="code"' not in page.text
        assert "Sign in with your passkey" in page.text
        r = world.setup(world.code)
        assert r.status_code == 403
        assert sua.PASSKEY_ENROLLED_MESSAGE in r.text
        assert r.cookies.get(org_session.SESSION_COOKIE) is None


class TestConcurrentUse:
    def test_two_racing_correct_checks_give_exactly_one_success(self):
        def submit(setup, barrier, results):
            barrier.wait()
            results.append(setup.check("ABCD-1234"))

        for _ in range(50):
            setup = sua.SetupCode("ABCD-1234")
            barrier = threading.Barrier(2)
            results: list[str] = []
            threads = [threading.Thread(target=submit, args=(setup, barrier, results)) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert sorted(results) == [sua.SetupCode.ACCEPTED, sua.SetupCode.USED_UP]

    def test_two_racing_correct_submissions_give_exactly_one_session(self, caplog):
        world = World(caplog)
        clients = [TestClient(world.client.app, base_url=ISSUER, follow_redirects=False) for _ in range(2)]
        bindings = []
        for client in clients:
            r = client.get("/login")
            bindings.append(re.search(r'data-binding="([^"]+)"', r.text).group(1))
        barrier = threading.Barrier(2)
        statuses = []

        def submit(client, binding):
            barrier.wait()
            r = client.post("/login/setup", data={"code": world.code, "next": "/connect", "binding": binding})
            statuses.append(r.status_code)

        threads = [threading.Thread(target=submit, args=pair) for pair in zip(clients, bindings, strict=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(statuses) == [303, 400]
        assert world.sessions.session_count == 1


class TestSetupSignIn:
    def test_the_right_code_gives_a_session_and_goes_to_security(self, caplog):
        world = World(caplog)
        r = world.setup(world.code, next_path="/connect")
        assert r.status_code == 303
        assert r.headers["location"] == "/security?next=/connect"
        assert r.headers["cache-control"] == "no-store"
        assert _session_principal(world, r) == ADMIN

    def test_the_session_is_email_verified(self, caplog):
        world = World(caplog)
        r = world.setup(world.code, next_path="/connect")
        session_id = r.cookies.get(org_session.SESSION_COOKIE)
        assert world.sessions.get_session(session_id).email_verified is True

    def test_next_is_carried_through_to_security(self, caplog):
        world = World(caplog)
        r = world.setup(world.code, next_path="/oauth/single-user/continue?state=abc")
        assert r.headers["location"] == "/security?next=/oauth/single-user/continue%3Fstate%3Dabc"

    def test_a_missing_next_falls_back_to_the_default(self, caplog):
        world = World(caplog)
        r = world.post("/login/setup", data={"code": world.code, "binding": world.binding()})
        assert r.headers["location"] == "/security?next=/connect"

    @pytest.mark.parametrize("next_path", ["//evil.example", "https://evil.example/", "/\\evil.example", "/\t/evil.example"])
    def test_an_unsafe_next_is_refused(self, caplog, next_path):
        world = World(caplog)
        page = world.get("/login", params={"next": next_path})
        assert 'data-next="/connect"' in page.text
        r = world.setup(world.code, next_path=next_path)
        assert r.headers["location"] == "/security?next=/connect"

    def test_each_sign_in_mints_a_fresh_session_id(self, caplog):
        world = World(caplog)
        world.client.cookies.set(org_session.SESSION_COOKIE, "fixed-by-someone-else")
        r = world.setup(world.code)
        session_id = r.cookies.get(org_session.SESSION_COOKIE)
        assert session_id and session_id != "fixed-by-someone-else"
        assert world.sessions.get("fixed-by-someone-else") is None

    def test_the_binding_cookie_is_removed_after_sign_in(self, caplog):
        world = World(caplog)
        r = world.setup(world.code)
        cookies = r.headers.get_list("set-cookie")
        assert any(c.startswith(f"{sua.SETUP_BINDING_COOKIE}=") and "Max-Age=0" in c for c in cookies)

    def test_a_signed_in_browser_goes_straight_to_next(self, caplog):
        world = World(caplog)
        world.setup(world.code)
        r = world.get("/login", params={"next": "/oauth/single-user/continue?state=s"})
        assert r.status_code == 302
        assert r.headers["location"] == "/oauth/single-user/continue?state=s"

    def test_setup_code_sign_in_notifies_provisioners(self, caplog, monkeypatch):
        calls = []
        monkeypatch.setattr(
            peek_provisioning, "notify_sign_in", lambda p, *, email_verified: calls.append(("peek", p, email_verified)),
        )
        monkeypatch.setattr(
            grist_provisioning, "notify_sign_in", lambda p, *, email_verified: calls.append(("grist", p, email_verified)),
        )
        world = World(caplog)
        assert world.setup(world.code).status_code == 303
        assert calls == [("peek", sua.SINGLE_USER_PRINCIPAL, True), ("grist", sua.SINGLE_USER_PRINCIPAL, True)]


class TestBinding:
    def test_the_binding_cookie_is_strict_httponly_and_scoped_to_login(self, caplog):
        world = World(caplog)
        r = world.get("/login")
        cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith(sua.SETUP_BINDING_COOKIE))
        lowered = cookie.lower()
        assert "httponly" in lowered and "samesite=strict" in lowered
        assert "path=/login" in lowered and "max-age=600" in lowered
        value = cookie.split(";", 1)[0].split("=", 1)[1]
        assert f'value="{value}"' in r.text

    def test_a_missing_binding_cookie_is_refused(self, caplog):
        world = World(caplog)
        binding = world.binding()
        world.client.cookies.clear()
        r = world.setup(world.code, binding=binding)
        assert r.status_code == 403
        assert r.cookies.get(org_session.SESSION_COOKIE) is None
        assert world.setup(world.code).status_code == 303  # the refusal did not spend the code

    def test_a_mismatched_binding_is_refused(self, caplog):
        world = World(caplog)
        world.binding()
        r = world.setup(world.code, binding="something-else")
        assert r.status_code == 403
        assert r.cookies.get(org_session.SESSION_COOKIE) is None

    def test_a_non_ascii_binding_is_refused_without_an_error(self, caplog):
        world = World(caplog)
        world.binding()
        assert world.setup(world.code, binding="é").status_code == 403

    def test_a_foreign_origin_is_refused(self, caplog):
        world = World(caplog)
        r = world.setup(world.code, headers={"Origin": "http://localhost:3000"})
        assert r.status_code == 403
        assert r.cookies.get(org_session.SESSION_COOKIE) is None


class TestCookies:
    def test_cookies_are_not_secure_for_an_http_issuer(self, caplog):
        world = World(caplog)
        r = world.setup(world.code)
        cookies = r.headers.get_list("set-cookie") + world.client.get("/login").headers.get_list("set-cookie")
        assert cookies
        assert all("secure" not in c.lower() for c in cookies)

    def test_cookies_are_secure_for_an_https_issuer(self, caplog):
        world = World(caplog, issuer=HTTPS_ISSUER, rp_id="pf.example.com")
        page = world.get("/login")
        r = world.setup(world.code)
        cookies = page.headers.get_list("set-cookie") + r.headers.get_list("set-cookie")
        session = next(c for c in cookies if c.startswith(f"{org_session.SESSION_COOKIE}="))
        binding = next(c for c in page.headers.get_list("set-cookie") if c.startswith(sua.SETUP_BINDING_COOKIE))
        assert "Secure" in session and "Secure" in binding


def _passkey_options(world: World, binding: str, **extra):
    return world.post("/login/passkey/options", json={"binding": binding, **extra})


def _signed_out(world: World) -> None:
    world.client.cookies.delete(org_session.SESSION_COOKIE)


def _passkey_sign_in(world: World, *, next_path="/approvals"):
    _signed_out(world)
    binding = world.binding(next_path)
    assert _passkey_options(world, binding, next=next_path).status_code == 200
    with patch.object(sua.webauthn_stepup, "verify_assertion", return_value=None):
        return world.post("/login/passkey/verify", json={"binding": binding, "credential": {"id": "Y3JlZC0x"}})


class TestPasskeySignIn:
    def test_passkey_sign_in_notifies_provisioners(self, caplog, monkeypatch):
        _enrol()
        world = World(caplog)
        calls = []
        monkeypatch.setattr(
            peek_provisioning, "notify_sign_in", lambda p, *, email_verified: calls.append(("peek", p, email_verified)),
        )
        monkeypatch.setattr(
            grist_provisioning, "notify_sign_in", lambda p, *, email_verified: calls.append(("grist", p, email_verified)),
        )
        assert _passkey_sign_in(world).status_code == 200
        assert calls == [("peek", sua.SINGLE_USER_PRINCIPAL, True), ("grist", sua.SINGLE_USER_PRINCIPAL, True)]

    def test_options_carry_the_enrolled_relying_party(self, caplog):
        _enrol()
        world = World(caplog)
        r = _passkey_options(world, world.binding())
        assert r.status_code == 200
        assert r.json()["options"]["rpId"] == "localhost"
        assert r.json()["options"]["allowCredentials"][0]["id"] == "Y3JlZC0x"

    def test_a_good_assertion_gives_a_session_and_the_next_path(self, caplog):
        _enrol()
        world = World(caplog)
        seen = {}

        def verify(principal, credential, *, expected_challenge, rp_id, origin):
            seen.update(principal=principal, rp_id=rp_id, origin=origin, challenge=expected_challenge)

        binding = world.binding("/oauth/single-user/continue?state=s")
        options = _passkey_options(world, binding, next="/oauth/single-user/continue?state=s").json()["options"]
        with patch.object(sua.webauthn_stepup, "verify_assertion", side_effect=verify):
            r = world.post("/login/passkey/verify", json={"binding": binding, "credential": {"id": "Y3JlZC0x"}})
        assert r.status_code == 200
        assert r.json() == {"redirect": "/oauth/single-user/continue?state=s"}
        assert _session_principal(world, r) == ADMIN
        assert seen["principal"] == ADMIN and seen["rp_id"] == "localhost" and seen["origin"] == ISSUER
        assert wa.bytes_to_base64url(seen["challenge"]) == options["challenge"]

    def test_a_missing_or_unsafe_next_falls_back_to_the_default(self, caplog):
        _enrol()
        world = World(caplog)
        for extra in ({}, {"next": "//evil.example"}, {"next": 5}):
            _signed_out(world)
            binding = world.binding()
            assert _passkey_options(world, binding, **extra).status_code == 200
            with patch.object(sua.webauthn_stepup, "verify_assertion", return_value=None):
                r = world.post("/login/passkey/verify", json={"binding": binding, "credential": {"id": "x"}})
            assert r.json() == {"redirect": "/connect"}

    def test_a_failed_assertion_is_refused(self, caplog):
        _enrol()
        world = World(caplog)
        binding = world.binding()
        _passkey_options(world, binding)
        with patch.object(sua.webauthn_stepup, "verify_assertion", side_effect=wa.WebAuthnError("bad signature")):
            r = world.post("/login/passkey/verify", json={"binding": binding, "credential": {"id": "Y3JlZC0x"}})
        assert r.status_code == 401
        assert r.cookies.get(org_session.SESSION_COOKIE) is None

    def test_a_missing_credential_is_refused(self, caplog):
        _enrol()
        world = World(caplog)
        binding = world.binding()
        _passkey_options(world, binding)
        r = world.post("/login/passkey/verify", json={"binding": binding, "credential": "nope"})
        assert r.status_code == 401

    def test_a_challenge_is_single_use(self, caplog):
        _enrol()
        world = World(caplog)
        binding = world.binding()
        _passkey_options(world, binding)
        with patch.object(sua.webauthn_stepup, "verify_assertion", return_value=None):
            first = world.post("/login/passkey/verify", json={"binding": binding, "credential": {"id": "x"}})
            world.client.cookies.set(sua.SETUP_BINDING_COOKIE, binding, path="/login")
            second = world.post("/login/passkey/verify", json={"binding": binding, "credential": {"id": "x"}})
        assert first.status_code == 200
        assert second.status_code == 400

    def test_verify_without_options_is_refused(self, caplog):
        _enrol()
        world = World(caplog)
        r = world.post("/login/passkey/verify", json={"binding": world.binding(), "credential": {"id": "x"}})
        assert r.status_code == 400

    def test_a_challenge_issued_to_another_browser_is_not_usable(self, caplog):
        _enrol()
        world = World(caplog)
        other = TestClient(world.client.app, base_url=ISSUER, follow_redirects=False)
        other_binding = re.search(r'data-binding="([^"]+)"', other.get("/login").text).group(1)
        other.post("/login/passkey/options", json={"binding": other_binding})
        r = world.post("/login/passkey/verify", json={"binding": other_binding, "credential": {"id": "x"}})
        assert r.status_code == 403

    @pytest.mark.parametrize("path", ["/login/passkey/options", "/login/passkey/verify"])
    def test_a_missing_or_mismatched_binding_is_refused(self, caplog, path):
        _enrol()
        world = World(caplog)
        world.binding()
        assert world.post(path, json={"binding": "something-else", "credential": {}}).status_code == 403
        world.client.cookies.clear()
        assert world.post(path, json={"credential": {}}).status_code == 403

    @pytest.mark.parametrize("path", ["/login/passkey/options", "/login/passkey/verify"])
    def test_a_foreign_origin_is_refused(self, caplog, path):
        _enrol()
        world = World(caplog)
        binding = world.binding()
        r = world.post(path, json={"binding": binding}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403

    @pytest.mark.parametrize("path", ["/login/passkey/options", "/login/passkey/verify"])
    def test_a_body_that_is_not_a_json_object_is_refused(self, caplog, path):
        world = World(caplog)
        world.binding()
        assert world.post(path, content=b"{", headers={"Content-Type": "application/json"}).status_code == 400
        assert world.post(path, json=["binding"]).status_code == 400

    def test_options_without_an_enrolled_passkey_are_refused(self, caplog):
        world = World(caplog)
        r = _passkey_options(world, world.binding())
        assert (r.status_code, r.json()) == (400, {"error": "No passkey is set up yet."})

    def test_options_without_a_relying_party_are_refused(self, caplog):
        _enrol()
        world = World(caplog, rp_id="")
        r = _passkey_options(world, world.binding())
        assert r.status_code == 400

    def test_the_page_offers_the_passkey_button_and_its_script(self, caplog):
        _enrol()
        world = World(caplog)
        r = world.get("/login", params={"next": "/approvals"})
        assert 'id="pf-passkey-signin"' in r.text
        assert 'data-next="/approvals"' in r.text
        assert "function pfWebauthnGet" in r.text
        assert r.headers["cache-control"] == "no-store"


class TestPasskeyChallengeStore:
    def test_an_expired_challenge_is_gone_and_swept(self, monkeypatch):
        store = sua._PasskeyChallengeStore(ttl=10)
        now = [1000.0]
        monkeypatch.setattr(sua.time, "time", lambda: now[0])
        store.put("a", challenge=b"c", next_path="/")
        now[0] += 11
        assert store.pop("a") is None
        store.put("b", challenge=b"c", next_path="/")
        now[0] += 11
        store.put("c", challenge=b"c", next_path="/")
        assert "b" not in store._pending

    def test_the_oldest_challenge_makes_room_at_the_cap(self, monkeypatch):
        store = sua._PasskeyChallengeStore()
        now = [1000.0]
        monkeypatch.setattr(sua.time, "time", lambda: now[0])
        for i in range(sua._MAX_PENDING_PASSKEY_CHALLENGES):
            now[0] += 1
            store.put(f"k{i}", challenge=b"c", next_path="/")
        store.put("k5", challenge=b"again", next_path="/")  # an existing key evicts nothing
        assert len(store._pending) == sua._MAX_PENDING_PASSKEY_CHALLENGES
        store.put("new", challenge=b"c", next_path="/")
        assert len(store._pending) == sua._MAX_PENDING_PASSKEY_CHALLENGES
        assert "k0" not in store._pending and store.pop("new") is not None


class TestLogout:
    def test_it_ends_the_session_and_goes_to_signed_out(self, caplog):
        world = World(caplog)
        r = world.setup(world.code)
        session_id = r.cookies.get(org_session.SESSION_COOKIE)
        out = world.post("/logout")
        assert out.status_code == 303
        assert out.headers["location"] == "/signed-out"
        assert world.sessions.get(session_id) is None
        assert any(c.startswith(f"{org_session.SESSION_COOKIE}=") for c in out.headers.get_list("set-cookie"))

    def test_without_a_session_it_still_goes_to_signed_out(self, caplog):
        world = World(caplog)
        assert world.post("/logout").headers["location"] == "/signed-out"

    def test_logout_is_post_only(self, caplog):
        world = World(caplog)
        assert world.get("/logout").status_code == 405

    def test_the_signed_out_page(self, caplog):
        world = World(caplog)
        r = world.get("/signed-out")
        assert r.status_code == 200
        assert "You're signed out" in r.text
        assert 'href="/login"' in r.text


class TestBrowserPush:
    def test_a_sign_in_moves_the_previous_sessions_subscriptions_and_a_sign_out_removes_them(self, caplog):
        push = FakePushStore()
        world = World(caplog, push_store=push)
        world.client.cookies.set(org_session.SESSION_COOKIE, "old-session")
        r = world.setup(world.code)
        new_id = r.cookies.get(org_session.SESSION_COOKIE)
        assert push.calls[0][0] == "replace" and push.calls[0][3] == ADMIN.id
        world.client.cookies.set(org_session.SESSION_COOKIE, new_id)
        world.post("/logout")
        assert push.calls[-1][0] == "remove"

    def test_a_first_sign_in_has_nothing_to_move(self, caplog):
        push = FakePushStore()
        world = World(caplog, push_store=push)
        world.setup(world.code)
        assert push.calls == []

    def test_a_push_store_failure_does_not_stop_the_sign_in(self, caplog):
        world = World(caplog, push_store=FakePushStore(fail=True))
        world.client.cookies.set(org_session.SESSION_COOKIE, "old-session")
        r = world.setup(world.code)
        assert r.status_code == 303
        assert "Could not update this browser's push subscriptions: OSError" in caplog.text


class TestAudit:
    def test_each_decision_is_recorded(self, caplog):
        world = World(caplog)
        wrong = "0000-0000" if world.code != "0000-0000" else "1111-1111"
        world.setup(wrong)
        world.setup(world.code)
        _enrol()
        _passkey_sign_in(world)
        _signed_out(world)
        binding = world.binding()
        _passkey_options(world, binding)
        with patch.object(sua.webauthn_stepup, "verify_assertion", side_effect=wa.WebAuthnError("bad")):
            world.post("/login/passkey/verify", json={"binding": binding, "credential": {"id": "x"}})
        decisions = [d for d, _ in _audit_entries()]
        for decision in (
            "single_user_setup_refused", "single_user_setup_signin",
            "single_user_passkey_signin", "single_user_passkey_refused",
        ):
            assert decision in decisions

    def test_every_refusal_reason_is_audited(self, caplog):
        world = World(caplog)
        world.setup(world.code, headers={"Origin": "http://evil.example"})
        world.setup(world.code, binding="wrong")
        _enrol()
        world.setup(world.code)
        binding = world.binding()
        world.post("/login/passkey/verify", json={"binding": binding}, headers={"Origin": "http://evil.example"})
        world.post("/login/passkey/verify", json={"binding": "wrong"})
        world.post("/login/passkey/verify", json={"binding": binding})
        summaries = [s for d, s in _audit_entries() if d.endswith("_refused")]
        assert len(summaries) == 6
        assert any("another site" in s for s in summaries)
        assert any("already enrolled" in s for s in summaries)
        assert any("expired or was already used" in s for s in summaries)

    def test_an_audit_failure_does_not_block_the_sign_in(self, caplog, monkeypatch):
        world = World(caplog)

        class Broken:
            def record(self, entry):
                raise OSError("disk full")

        monkeypatch.setattr(sua, "get_audit_logger", lambda: Broken())
        r = world.setup(world.code)
        assert r.status_code == 303
        assert "Audit log write failed for single_user_setup_signin" in caplog.text


class TestNormalize:
    @pytest.mark.parametrize("typed,expected", [
        ("abcd-1234", "ABCD-1234"), ("ABCD1234", "ABCD-1234"), (" abcd1234 ", "ABCD-1234"),
        ("ABCD-12345", None), ("ABC-1234", None), ("GHIJ-1234", None), ("", None), ("ABCD-123\n", None),
    ])
    def test_shapes(self, typed, expected):
        assert sua.normalize_setup_code(typed) == expected

    def test_a_generated_code_has_the_documented_shape(self):
        assert re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", sua.SetupCode.generate())
