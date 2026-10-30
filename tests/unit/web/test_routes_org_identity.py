"""Tests for web/routes_org_identity.py: /login, /oauth/idp/login-callback,
/logout -- the org-mode browser sign-in flow."""
from __future__ import annotations

import urllib.parse as _up

from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import org_identity as oi
from privacyfence.org_mode import AuthzPolicyConfig
from privacyfence.principal import Principal
from privacyfence.web import org_session, routes_org_identity as roi

BASE_URL = "https://pf.example.com"


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _app(sessions: org_session.OrgSessionStore | None = None, *, policy: AuthzPolicyConfig | None = None):
    sessions = sessions or org_session.OrgSessionStore()
    routes = roi.build_routes(idp=_idp(), sessions=sessions, base_url=BASE_URL, policy=policy)
    app = Starlette(routes=routes)
    return app, sessions


def _client(app) -> TestClient:
    return TestClient(app, base_url=BASE_URL, follow_redirects=False)


class TestLogin:
    def test_redirects_to_the_idps_authorization_endpoint(self):
        app, _sessions = _app()
        r = _client(app).get("/login")
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://idp.example.com/authorize?")

    def test_authorization_url_carries_the_fixed_login_callback_redirect_uri(self):
        import urllib.parse as up

        app, _sessions = _app()
        r = _client(app).get("/login")
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        assert qs["redirect_uri"] == f"{BASE_URL}{roi.LOGIN_CALLBACK_PATH}"

    def test_no_store_cache_control(self):
        app, _sessions = _app()
        r = _client(app).get("/login")
        assert r.headers.get("cache-control") == "no-store"

    def test_login_sets_the_binding_cookie(self):
        import hashlib

        app, _sessions = _app()
        r = _client(app).get("/login")
        state = dict(_up.parse_qsl(_up.urlparse(r.headers["location"]).query))["state"]
        set_cookie = r.headers["set-cookie"]
        assert set_cookie.startswith(f"{roi.LOGIN_BINDING_COOKIE}={hashlib.sha256(state.encode()).hexdigest()}")
        lowered = set_cookie.lower()
        assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered
        assert f"path={roi.LOGIN_CALLBACK_PATH}".lower() in lowered
        assert f"max-age={roi._LOGIN_ATTEMPT_TTL_SECONDS}" in lowered

    def test_open_redirect_via_next_is_rejected(self, monkeypatch):
        app, sessions = _app()

        def fake_exchange(idp, *, code, redirect_uri, code_verifier):
            return {"id_token": "irrelevant"}

        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", fake_exchange)
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        client = _client(app)
        r = client.get("/login?next=https://evil.example.com/steal")
        # Extract state from the redirect URL and drive the callback.
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.headers["location"] == roi.DEFAULT_NEXT_PATH  # not the attacker's URL

    def test_scheme_relative_next_is_rejected(self, monkeypatch):
        app, sessions = _app()
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token", lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        client = _client(app)
        r = client.get("/login?next=//evil.example.com/steal")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.headers["location"] == roi.DEFAULT_NEXT_PATH

    def test_same_origin_next_is_honored(self, monkeypatch):
        app, sessions = _app()
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token", lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        client = _client(app)
        r = client.get("/login?next=/settings")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.headers["location"] == "/settings"


class TestLoginCallback:
    def _drive_login(self, client, monkeypatch, *, claims: dict, next_path: str | None = None):
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "opaque"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {**claims, "nonce": nonce},
        )
        login_url = "/login" + (f"?next={next_path}" if next_path else "")
        r = client.get(login_url)
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        return client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc123&state={qs['state']}")

    def test_happy_path_sets_a_session_cookie_and_redirects_to_next(self, monkeypatch):
        app, sessions = _app()
        client = _client(app)
        cb = self._drive_login(client, monkeypatch, claims={"sub": "alice", "email": "alice@example.com"})

        assert cb.status_code == 302
        assert cb.headers["location"] == roi.DEFAULT_NEXT_PATH
        assert org_session.SESSION_COOKIE in cb.cookies
        session_id = cb.cookies[org_session.SESSION_COOKIE]
        assert sessions.get(session_id).id == "alice"

    def test_login_callback_calls_notify_sign_in_with_email_verified(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            roi.peek_provisioning, "notify_sign_in",
            lambda principal, *, email_verified: calls.append((principal.id, email_verified)),
        )
        app, _sessions = _app()

        self._drive_login(
            _client(app), monkeypatch,
            claims={"sub": "alice", "email": "alice@example.com", "email_verified": True},
        )
        self._drive_login(
            _client(app), monkeypatch, claims={"sub": "bob", "email": "bob@example.com"},
        )

        assert calls == [("alice", True), ("bob", False)]

    def test_failed_login_callback_does_not_call_notify_sign_in(self, monkeypatch):
        calls = []
        monkeypatch.setattr(roi.peek_provisioning, "notify_sign_in", lambda *a, **kw: calls.append(a))
        app, _sessions = _app(policy=AuthzPolicyConfig(allowed_domains=("acme.com",)))

        cb = self._drive_login(
            _client(app), monkeypatch, claims={"sub": "mallory", "email": "mallory@evil.example.com"},
        )

        assert cb.status_code == 400
        assert calls == []

    def test_the_session_cookie_it_mints_is_usable_on_the_page_it_redirects_to(self, monkeypatch):
        # The regression a real browser found and no test here could: this
        # callback is reached by a top-level navigation the IdP initiated,
        # and it redirects to a page that reads the session. A SameSite=
        # Strict cookie is withheld on that landing, so the page saw no
        # session, bounced back to /login, and the IdP -- already consented
        # -- sent the browser straight back: an infinite redirect loop for
        # a session that was valid the whole time.
        #
        # httpx's TestClient enforces no SameSite semantics at all, so this
        # asserts the cookie *attribute* rather than trying to simulate the
        # browser behavior it can't: Lax is what makes the landing work.
        app, sessions = _app()
        client = _client(app)
        cb = self._drive_login(client, monkeypatch, claims={"sub": "alice", "email": "alice@example.com"})

        set_cookie = cb.headers.get("set-cookie", "").lower()
        assert "samesite=lax" in set_cookie
        assert "samesite=strict" not in set_cookie
        # ... and the redirect really does go to a session-reading page,
        # which is what makes the attribute above load-bearing rather than
        # cosmetic.
        assert cb.headers["location"] == roi.DEFAULT_NEXT_PATH
        assert sessions.get(cb.cookies[org_session.SESSION_COOKIE]) is not None

    def _login(self, client, monkeypatch, *, sub: str = "alice"):
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "opaque"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token", lambda idp, token, *, nonce: {"sub": sub, "nonce": nonce},
        )
        r = client.get("/login")
        return dict(_up.parse_qsl(_up.urlparse(r.headers["location"]).query))["state"], r

    def test_login_callback_without_the_starting_browser_cookie_is_rejected(self, monkeypatch):
        app, sessions = _app()
        state, _login = self._login(_client(app), monkeypatch)
        # Another person's browser: it has the callback URL but never visited /login.
        cb = _client(app).get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}")
        assert cb.status_code == 400
        assert org_session.SESSION_COOKIE not in cb.cookies
        assert "set-cookie" not in cb.headers
        assert sessions.session_count == 0

    def test_login_callback_with_another_attempts_cookie_is_rejected(self, monkeypatch):
        app, sessions = _app()
        victim_state, _ = self._login(_client(app), monkeypatch)
        attacker = _client(app)
        _attacker_state, attacker_login = self._login(attacker, monkeypatch)
        attacker_cookie = attacker_login.cookies[roi.LOGIN_BINDING_COOKIE]

        cb = _client(app).get(
            f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={victim_state}",
            headers={"Cookie": f"{roi.LOGIN_BINDING_COOKIE}={attacker_cookie}"},
        )

        assert cb.status_code == 400
        assert org_session.SESSION_COOKIE not in cb.cookies
        assert sessions.session_count == 0

    def test_a_rejected_callback_still_burns_the_state(self, monkeypatch):
        app, sessions = _app()
        client = _client(app)
        state, login = self._login(client, monkeypatch)
        cookie = login.cookies[roi.LOGIN_BINDING_COOKIE]

        wrong = _client(app).get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}")
        right = _client(app).get(
            f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}",
            headers={"Cookie": f"{roi.LOGIN_BINDING_COOKIE}={cookie}"},
        )

        assert wrong.status_code == 400
        assert right.status_code == 400
        assert sessions.session_count == 0

    def test_the_session_cookie_is_first_and_the_binding_cookie_is_cleared(self, monkeypatch):
        app, _sessions = _app()
        client = _client(app)
        state, _login = self._login(client, monkeypatch)
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}")
        headers = cb.headers.get_list("set-cookie")
        assert headers[0].startswith(f"{org_session.SESSION_COOKIE}=")
        assert any(h.startswith(f"{roi.LOGIN_BINDING_COOKIE}=") and "max-age=0" in h.lower() for h in headers[1:])

    def test_login_callback_after_ttl_is_rejected(self, monkeypatch):
        import time

        app, sessions = _app()
        state, login = self._login(_client(app), monkeypatch)
        cookie = login.cookies[roi.LOGIN_BINDING_COOKIE]
        now = time.time()
        monkeypatch.setattr(roi.time, "time", lambda: now + roi._LOGIN_ATTEMPT_TTL_SECONDS + 1)

        cb = _client(app).get(
            f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}",
            headers={"Cookie": f"{roi.LOGIN_BINDING_COOKIE}={cookie}"},
        )

        assert cb.status_code == 400
        assert sessions.session_count == 0

    def test_idp_error_param_fails_cleanly(self):
        app, _sessions = _app()
        client = _client(app)
        r = client.get(f"{roi.LOGIN_CALLBACK_PATH}?error=access_denied&state=whatever")
        assert r.status_code == 400

    def test_missing_state_fails_cleanly(self):
        app, _sessions = _app()
        r = _client(app).get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc")
        assert r.status_code == 400

    def test_unknown_state_fails_cleanly(self):
        app, _sessions = _app()
        r = _client(app).get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state=never-issued")
        assert r.status_code == 400

    def test_state_is_single_use(self, monkeypatch):
        app, sessions = _app()
        client = _client(app)
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "opaque"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token", lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        r = client.get("/login")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        state = qs["state"]

        first = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}")
        second = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}")

        assert first.status_code == 302
        assert second.status_code == 400  # the state was already consumed

    def test_idp_exchange_failure_fails_cleanly_not_500(self, monkeypatch):
        app, _sessions = _app()
        client = _client(app)

        def failing_exchange(*a, **kw):
            raise RuntimeError("IdP unreachable")

        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", failing_exchange)
        r = client.get("/login")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.status_code == 400

    def test_idp_token_response_missing_id_token_fails_cleanly_not_500(self, monkeypatch):
        app, _sessions = _app()
        client = _client(app)
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"access_token": "x"})
        r = client.get("/login")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.status_code == 400

    def test_authz_policy_denial_fails_cleanly_and_mints_no_session(self, monkeypatch):
        # An IdP-authenticated principal PrivacyFence's own sign-in policy
        # (allowed email domains, required groups) rejects gets the same generic failure as any other sign-in
        # failure -- no session cookie, no 500.
        policy = AuthzPolicyConfig(allowed_domains=("acme.com",))
        app, sessions = _app(policy=policy)
        cb = self._drive_login(
            client=_client(app), monkeypatch=monkeypatch,
            claims={"sub": "mallory", "email": "mallory@evil.example.com"},
        )
        assert cb.status_code == 400
        assert org_session.SESSION_COOKIE not in cb.cookies
        assert sessions.session_count == 0

    @staticmethod
    def _record_to_thread(monkeypatch):
        """Record every asyncio.to_thread call, still running it for real."""
        import asyncio

        real = asyncio.to_thread
        calls: list[tuple] = []

        async def _recorder(func, /, *args, **kwargs):
            calls.append((func, args, kwargs))
            return await real(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _recorder)
        return calls

    def test_login_callback_calls_grist_notify_sign_in_with_email_verified(self, monkeypatch):
        notified: list[tuple] = []
        monkeypatch.setattr(
            roi.grist_provisioning, "notify_sign_in",
            lambda principal, *, email_verified: notified.append((principal.id, email_verified)),
        )
        calls = self._record_to_thread(monkeypatch)
        app, _sessions = _app()
        cb = self._drive_login(
            _client(app), monkeypatch,
            claims={"sub": "alice", "email": "alice@example.com", "email_verified": True},
        )

        assert cb.status_code == 302
        # Off the event loop, as a file write: through asyncio.to_thread.
        grist_calls = [c for c in calls if c[0] is roi.grist_provisioning.notify_sign_in]
        assert len(grist_calls) == 1
        assert grist_calls[0][1][0].id == "alice"
        assert grist_calls[0][2] == {"email_verified": True}
        assert notified == [("alice", True)]

    def test_failed_login_callback_does_not_call_grist_notify_sign_in(self, monkeypatch):
        notified: list[str] = []
        monkeypatch.setattr(
            roi.grist_provisioning, "notify_sign_in",
            lambda principal, *, email_verified: notified.append(principal.id),
        )
        policy = AuthzPolicyConfig(allowed_domains=("acme.com",))
        app, _sessions = _app(policy=policy)
        cb = self._drive_login(
            _client(app), monkeypatch, claims={"sub": "mallory", "email": "mallory@evil.example.com"},
        )

        assert cb.status_code == 400
        assert notified == []

    def test_authz_policy_admits_a_matching_principal(self, monkeypatch):
        policy = AuthzPolicyConfig(allowed_domains=("acme.com",))
        app, sessions = _app(policy=policy)
        cb = self._drive_login(
            client=_client(app), monkeypatch=monkeypatch,
            claims={"sub": "alice", "email": "alice@acme.com", "email_verified": True},
        )
        assert cb.status_code == 302
        assert org_session.SESSION_COOKIE in cb.cookies


class TestLogout:
    def test_clears_the_session_cookie_and_the_server_side_session(self):
        app, sessions = _app()
        alice_session = sessions.create(Principal(id="alice"))
        client = _client(app)
        client.cookies.set(org_session.SESSION_COOKIE, alice_session)

        r = client.post("/logout")

        assert r.status_code == 303
        assert sessions.get(alice_session) is None

    def test_get_is_not_allowed(self):
        app, _sessions = _app()
        r = _client(app).get("/logout")
        assert r.status_code == 405

    def test_without_a_session_cookie_still_lands_on_the_signed_out_page(self):
        app, _sessions = _app()
        r = _client(app).post("/logout")
        assert r.status_code == 303 and r.headers["location"] == "/signed-out"

    def test_a_push_store_that_cannot_be_written_does_not_stop_sign_out(self, caplog):
        """ADR 0081: sign-out removes the browser's push subscriptions, but a disk error doing so
        must not leave the person signed in."""
        class _BrokenStore:
            def remove_session(self, tag):
                raise OSError("disk full")

        sessions = org_session.OrgSessionStore()
        app = Starlette(routes=roi.build_routes(
            idp=_idp(), sessions=sessions, base_url=BASE_URL, push_store=_BrokenStore(),
        ))
        alice_session = sessions.create(Principal(id="alice"))
        client = _client(app)
        client.cookies.set(org_session.SESSION_COOKIE, alice_session)

        r = client.post("/logout")

        assert r.status_code == 303 and r.headers["location"] == "/signed-out"
        assert sessions.get(alice_session) is None
        assert "Could not update this browser's push subscriptions: OSError" in caplog.text


class TestSignedOutPage:
    """Where /logout lands (ADR 0082): a page on this origin, not /login's bounce to the IdP."""

    def test_served_without_a_session_and_links_to_sign_in(self):
        app, _sessions = _app()
        r = _client(app).get("/signed-out")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        assert "You're signed out" in r.text
        assert 'href="/login"' in r.text

    def test_sign_out_lands_on_it(self):
        app, sessions = _app()
        client = TestClient(app, base_url=BASE_URL)
        client.cookies.set(org_session.SESSION_COOKIE, sessions.create(Principal(id="alice")))
        r = client.post("/logout")
        assert r.status_code == 200 and r.url.path == "/signed-out"


class TestSafeNextPath:
    """Unit-level coverage of the open-redirect defense -- see
    TestLogin.test_open_redirect_via_next_is_rejected/test_scheme_relative_
    next_is_rejected for the same thing driven through the real /login ->
    callback round trip."""

    def test_none_and_empty_default(self):
        assert roi._safe_next_path(None) == roi.DEFAULT_NEXT_PATH
        assert roi._safe_next_path("") == roi.DEFAULT_NEXT_PATH

    def test_not_starting_with_a_slash_defaults(self):
        assert roi._safe_next_path("evil.example.com") == roi.DEFAULT_NEXT_PATH
        assert roi._safe_next_path("https://evil.example.com") == roi.DEFAULT_NEXT_PATH

    def test_scheme_relative_defaults(self):
        assert roi._safe_next_path("//evil.example.com/steal") == roi.DEFAULT_NEXT_PATH

    def test_leading_backslash_defaults(self):
        # Browsers normalize a leading "\" to "/" before navigating, so
        # "/\evil.example.com" is a protocol-relative redirect to a
        # real browser even though it reads as an ordinary same-origin
        # path to plain Python string/URL handling.
        assert roi._safe_next_path("/\\evil.example.com") == roi.DEFAULT_NEXT_PATH
        assert roi._safe_next_path("\\\\evil.example.com") == roi.DEFAULT_NEXT_PATH

    def test_mixed_slash_and_backslash_defaults(self):
        assert roi._safe_next_path("/\\/evil.example.com") == roi.DEFAULT_NEXT_PATH

    def test_ordinary_same_origin_path_is_honored(self):
        assert roi._safe_next_path("/settings") == "/settings"
        assert roi._safe_next_path("/approvals/abc123") == "/approvals/abc123"


class TestLoginAttemptStorePruning:
    def test_stale_attempts_are_pruned_on_the_next_create(self, monkeypatch):
        store = roi._LoginAttemptStore()
        fake_now = [1000.0]
        monkeypatch.setattr(roi.time, "time", lambda: fake_now[0])
        old_state, _attempt, _challenge = store.create(next_path="/x", redirect_uri="https://pf.example.com/cb")

        fake_now[0] += roi._LOGIN_ATTEMPT_TTL_SECONDS + 1
        store.create(next_path="/y", redirect_uri="https://pf.example.com/cb")  # triggers _prune()

        assert store.pop(old_state) is None


# --------------------------------------------------------------------------- #
# Built-in Google sign-in through the relay
# --------------------------------------------------------------------------- #

class _TokenResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> dict:
        return self._body


class FakeGoogle:
    """Google's side of the built-in sign-in, which every server using it shares: one client, and a
    code that only exchanges with the PKCE verifier, redirect_uri and client of the authorization
    request it was issued for, and an ID token carrying that request's nonce. The real
    ``exchange_code_for_tokens`` and ``verify_id_token`` run against it."""

    ISSUER = "https://accounts.google.com"
    CLIENT_ID = "builtin-signin.apps.googleusercontent.com"

    def __init__(self, monkeypatch) -> None:
        import types

        from cryptography.hazmat.primitives.asymmetric import rsa

        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = self._key.public_key()
        self.codes: dict[str, dict] = {}
        self.exchanges: list[dict] = []
        monkeypatch.setattr(oi.requests, "post", self._token_endpoint)
        monkeypatch.setattr(
            oi.PyJWKClient, "get_signing_key_from_jwt", lambda _self, token: types.SimpleNamespace(key=public_key),
        )

    def idp(self, **overrides) -> oi.IdpConfig:
        values = dict(
            issuer=self.ISSUER, client_id=self.CLIENT_ID, client_secret="builtin-secret",
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
            jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
            builtin=True, relay=True, admin_emails=("alice@acme.com",),
        )
        values.update(overrides)
        return oi.IdpConfig(**values)

    def sign_in(self, authorization_url: str, *, email: str = "alice@acme.com", hd: str = "acme.com",
                nonce: str | None = None, sub: str = "alice-sub") -> dict[str, str]:
        """The person signs in at Google; returns the query Google redirects with (through the
        relay, unchanged). ``nonce`` overrides the one in the request, as a token minted for another
        attempt would carry."""
        import secrets as _secrets

        query = dict(_up.parse_qsl(_up.urlsplit(authorization_url).query))
        code = _secrets.token_urlsafe(16)
        self.codes[code] = {
            "challenge": query["code_challenge"], "redirect_uri": query["redirect_uri"],
            "client_id": query["client_id"], "nonce": nonce if nonce is not None else query["nonce"],
            "claims": {"sub": sub, "email": email, "email_verified": True, "hd": hd},
        }
        return {"code": code, "state": query["state"], "hd": hd}

    def _token_endpoint(self, url, data, timeout):
        import base64
        import hashlib
        import time as _time

        import jwt

        self.exchanges.append(dict(data))
        entry = self.codes.pop(data["code"], None)  # single use, like Google's
        verifier = data.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if (
            entry is None or challenge != entry["challenge"] or data.get("redirect_uri") != entry["redirect_uri"]
            or data.get("client_id") != entry["client_id"]
        ):
            return _TokenResponse(400, {"error": "invalid_grant"})
        now = int(_time.time())
        claims = {
            **entry["claims"], "iss": self.ISSUER, "aud": self.CLIENT_ID, "iat": now, "exp": now + 300,
            "nonce": entry["nonce"],
        }
        return _TokenResponse(200, {"id_token": jwt.encode(claims, self._key, algorithm="RS256")})


def prove_relay(*domains: str) -> None:
    """As if this server had registered with the relay, which found these sign-in domains in DNS."""
    from privacyfence import relay_client

    relay_client._persist_last_ok(1.0, list(domains))
    relay_client.reset_for_tests()


class TestLoginThroughTheRelay:
    RELAY_URI = "https://oauth.privacyfence.eu/callback/idp-login"

    def _app(self, google: FakeGoogle, **idp_overrides):
        from privacyfence.org_identity import IdpAuthzPolicy

        idp = google.idp(**idp_overrides)
        sessions = org_session.OrgSessionStore()
        routes = roi.build_routes(
            idp=idp, sessions=sessions, base_url=BASE_URL, policy=IdpAuthzPolicy.bind(AuthzPolicyConfig(), idp),
        )
        return Starlette(routes=routes), sessions

    def _callback(self, client, query: dict[str, str]):
        # What the relay forwards: the same query, on this server's login callback.
        return client.get(f"{roi.LOGIN_CALLBACK_PATH}?{_up.urlencode(query)}")

    def test_the_starting_up_page_before_registration_and_google_after(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        app, _sessions = self._app(google)
        client = _client(app)
        r = client.get("/login")
        assert r.status_code == 503 and "location" not in r.headers
        assert roi.STARTING_UP_TEXT in r.text
        assert r.text.count("Sign-in is starting up: this server is registering with the PrivacyFence relay. "
                            "Try again in a minute.") == 1
        assert roi.LOGIN_BINDING_COOKIE not in r.headers.get("set-cookie", "")
        assert r.headers["cache-control"] == "no-store"
        prove_relay("acme.com")
        r = client.get("/login")
        assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/")

    def test_the_waiting_for_dns_page_while_no_domain_is_proven(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        app, _sessions = self._app(google)
        prove_relay()
        r = _client(app).get("/login")
        assert r.status_code == 503 and "location" not in r.headers
        assert ("Sign-in is waiting for DNS: the administrator must publish the record shown in the server log."
                in r.text)

    def test_the_relay_redirect_uri_for_authorize_and_exchange_and_a_host_state(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        app, sessions = self._app(google)
        prove_relay("acme.com")
        client = _client(app)
        url = client.get("/login").headers["location"]
        query = dict(_up.parse_qsl(_up.urlsplit(url).query))
        assert query["redirect_uri"] == self.RELAY_URI
        host, _, nonce = query["state"].partition("~")
        assert host == "pf.example.com" and len(nonce) == 43
        cb = self._callback(client, google.sign_in(url))
        assert cb.status_code == 302 and cb.headers["location"] == roi.DEFAULT_NEXT_PATH
        assert google.exchanges[0]["redirect_uri"] == self.RELAY_URI
        principal = sessions.get(cb.cookies[org_session.SESSION_COOKIE])
        assert principal.is_admin is True

    def test_a_code_from_another_attempt_fails_the_exchange(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        app, sessions = self._app(google)
        prove_relay("acme.com")
        client = _client(app)
        first = client.get("/login").headers["location"]
        second = client.get("/login").headers["location"]  # this browser's binding is now the second
        code_for_first = google.sign_in(first)["code"]
        state_second = dict(_up.parse_qsl(_up.urlsplit(second).query))["state"]
        cb = self._callback(client, {"code": code_for_first, "state": state_second, "hd": "acme.com"})
        assert cb.status_code == 400 and org_session.SESSION_COOKIE not in cb.cookies
        # The server sent the second attempt's own verifier, which does not match the first's challenge.
        assert len(google.exchanges) == 1

    def test_an_id_token_with_another_attempts_nonce_is_refused(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        app, _sessions = self._app(google)
        prove_relay("acme.com")
        client = _client(app)
        other = client.get("/login").headers["location"]
        mine = client.get("/login").headers["location"]
        other_nonce = dict(_up.parse_qsl(_up.urlsplit(other).query))["nonce"]
        cb = self._callback(client, google.sign_in(mine, nonce=other_nonce))
        assert cb.status_code == 400 and org_session.SESSION_COOKIE not in cb.cookies

    def test_a_replayed_unknown_or_foreign_state_is_refused(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        app, _sessions = self._app(google)
        prove_relay("acme.com")
        client = _client(app)
        url = client.get("/login").headers["location"]
        query = google.sign_in(url)
        assert self._callback(client, query).status_code == 302
        assert self._callback(client, query).status_code == 400  # replayed
        url = client.get("/login").headers["location"]
        query = google.sign_in(url)
        _host, _, nonce = query["state"].partition("~")
        for state in (f"other.example.com~{nonce}", f"pf.example.com~{'x' * 43}", nonce):
            assert self._callback(client, {**query, "state": state}).status_code == 400
        assert len(google.exchanges) == 1

    def test_an_admin_of_a_domain_not_proven_in_dns_is_refused(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        app, _sessions = self._app(google)
        prove_relay("acme.org")
        client = _client(app)
        cb = self._callback(client, google.sign_in(client.get("/login").headers["location"]))
        assert cb.status_code == 400 and org_session.SESSION_COOKIE not in cb.cookies

    def test_a_cancelled_sign_in_shows_the_servers_own_page(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        app, _sessions = self._app(google)
        prove_relay("acme.com")
        client = _client(app)
        state = dict(_up.parse_qsl(_up.urlsplit(client.get("/login").headers["location"]).query))["state"]
        cb = self._callback(client, {"error": "access_denied", "state": state})
        assert cb.status_code == 400 and google.exchanges == []


class TestLoginWithoutTheRelay:
    def test_an_own_idp_keeps_todays_redirect_uri_byte_for_byte_and_ignores_the_relay(self, monkeypatch):
        google = FakeGoogle(monkeypatch)
        idp = google.idp(builtin=False, relay=False, admin_emails=())
        app = Starlette(routes=roi.build_routes(idp=idp, sessions=org_session.OrgSessionStore(), base_url=BASE_URL))
        client = _client(app)
        r = client.get("/login")  # never registered with the relay: no starting-up page
        assert r.status_code == 302
        query = dict(_up.parse_qsl(_up.urlsplit(r.headers["location"]).query))
        assert query["redirect_uri"] == "https://pf.example.com/oauth/idp/login-callback"
        assert "~" not in query["state"]
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?{_up.urlencode(google.sign_in(r.headers['location']))}")
        assert cb.status_code == 302
        assert google.exchanges[0]["redirect_uri"] == "https://pf.example.com/oauth/idp/login-callback"
