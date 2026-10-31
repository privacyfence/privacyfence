"""Tests for web/server.py's org-mode wiring: build_app(org=...) and
WebServer(org=...) -- see test_org_mcp_e2e.py for the full DCR/authorize/
token/tool-call flow driven over real HTTP; this file covers what server.py
itself is responsible for: which routes exist (and, just as importantly,
which don't), TLS/trusted-proxies plumbing, and base_url/allowed_hosts.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from privacyfence import oidc_provider
from privacyfence import org_identity as oi
from privacyfence import relay_client
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.principal import Principal
from privacyfence.web import routes_org_identity
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.org_session import SESSION_COOKIE, OrgSessionStore
from privacyfence.web.server import OrgAuth, WebServer, build_app
from privacyfence.web_approval_ui import WebApprovalUI

ISSUER = "https://pf.example.com"


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _org_auth(tmp_path, monkeypatch) -> OrgAuth:
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"))
    provider = OrgOAuthProvider(_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    return OrgAuth(provider=provider, sessions=OrgSessionStore(), idp=_idp(), issuer_url=ISSUER)


class TestBuildAppOrgMode:
    def test_mcp_route_is_mounted(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(
            WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}),
            allowed_hosts=frozenset({"pf.example.com"}),
        )
        client = TestClient(app, base_url=ISSUER)
        # No bearer token -- expect a 401, not a 404: proves the route exists.
        r = client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401

    def test_capability_routes_are_mounted_with_no_auth_required(self, tmp_path, monkeypatch):
        # Org mode mounts mount_capability_routes() alone (ADR 0028; never
        # mount_file_bridge's bearer-authenticated pair -- see that
        # function's own docstring), so both a malformed slot and a malformed token come back 404
        # (route exists, token isn't live), never 401.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(
            WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}),
            allowed_hosts=frozenset({"pf.example.com"}),
        )
        client = TestClient(app, base_url=ISSUER)
        assert client.put("/mcp-files/slots/not-a-real-slot", content=b"data").status_code == 404
        assert client.get("/mcp-files/fetch/not-a-real-token").status_code == 404

    def test_oauth_authorization_server_metadata_is_served(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        r = client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        assert r.json()["issuer"].rstrip("/") == ISSUER

    def test_login_route_is_mounted(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/login")
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://idp.example.com/authorize?")

    def test_lifespan_starts_and_stops_cleanly_with_an_mcp_dispatcher(self, tmp_path, monkeypatch):
        # Exercises build_app()'s own lifespan wiring for org mode (the
        # Starlette `lifespan` callback that enters mcp_lifespan via
        # _combined_lifespan) -- TestClient only runs ASGI lifespan
        # startup/shutdown when used as a context manager.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(
            WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}),
            allowed_hosts=frozenset({"pf.example.com"}),
        )
        with TestClient(app, base_url=ISSUER) as client:
            r = client.get("/.well-known/oauth-authorization-server")
            assert r.status_code == 200

    def test_settings_dispatcher_surface_is_not_mounted(self, tmp_path, monkeypatch):
        # /settings' ~30-action local-mode dispatcher surface stays out of
        # org mode (see server.py's module docstring) -- unlike /approvals,
        # which org mode mounts as its own principal-aware route set (below).
        # Org mode mounts its own POST /api/settings/{action}
        # (routes_settings.py's own build_org_routes, restricted to
        # _ORG_ALLOWED_ACTIONS -- see that module's own docstring and
        # ADR 0032), the same path local mode's ~30-action dispatcher
        # answers, so this path existing at all doesn't prove local's
        # surface isn't mounted. quit_app is one of the ~24 local-only
        # actions _ORG_ALLOWED_ACTIONS never includes -- POSTing it 404s
        # (an unauthenticated request 401s before the action name is even
        # checked, so this signs in first); /settings itself is a real
        # route (see the read-only-surface test below), so this only
        # checks the local-mode dispatcher's own unrestricted action
        # surface stays unreachable.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        session_id = org.sessions.create(Principal(id="alice", email="alice@example.com"))
        client.cookies.set(SESSION_COOKIE, session_id)
        r = client.post("/api/settings/quit_app", json={"csrf": session_id})
        assert r.status_code == 404
        assert client.get("/api/state/stream").status_code == 404

    def test_readonly_settings_surface_is_mounted(self, tmp_path, monkeypatch):
        # /settings is a real route -- a small, purpose-built surface
        # (web/routes_settings.py's build_org_routes), not the local-mode
        # dispatcher above. An unauthenticated request is redirected to
        # /login, same as /approvals. /settings/privacy only forwards to the
        # Admin page, where the install-wide privacy policy lives.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/settings")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/settings"
        client.cookies.set(SESSION_COOKIE, org.sessions.create(Principal(id="alice", email="alice@example.com")))
        r = client.get("/settings/privacy")
        assert r.status_code == 302
        assert r.headers["location"] == "/admin"
        # The install-wide privacy actions are Admin actions; Settings no longer answers them.
        assert client.post(
            "/api/settings/toggle_pii_detection", json={"csrf": client.cookies.get(SESSION_COOKIE)},
        ).status_code == 404

    def test_local_mode_approval_surface_is_not_what_gets_mounted(self, tmp_path, monkeypatch):
        # /approvals exists, but it's web/routes_approvals.py's
        # build_routes() principal-aware route set, not that module's
        # create_app() shared-secret one -- an unauthenticated request is redirected to
        # /login, never served the (local-mode-only) card content directly.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/approvals")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals"


class TestConnectSurfaceOrgMode:
    """/connect and the /oauth/start|callback/{service} routes are
    mounted only once a real ConnectorRegistry is supplied on OrgAuth --
    see that class's own docstring. A hand-built OrgAuth without one
    (every test above this class) gets the route set without them."""

    def test_connect_is_not_mounted_without_a_connector_registry(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        assert client.get("/connect").status_code == 404
        assert client.get("/oauth/start/slack").status_code == 404

    def test_connect_is_mounted_with_a_connector_registry(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        registry = ConnectorRegistry(factory=lambda principal: [])
        org_with_registry = org.__class__(
            provider=org.provider, sessions=org.sessions, idp=org.idp, issuer_url=org.issuer_url,
            connector_registry=registry, org_config={},
        )
        app = build_app(WebApprovalUI(), org=org_with_registry, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/connect")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/connect"

    def test_login_with_no_next_lands_on_connect_once_mounted(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        registry = ConnectorRegistry(factory=lambda principal: [])
        org_with_registry = org.__class__(
            provider=org.provider, sessions=org.sessions, idp=org.idp, issuer_url=org.issuer_url,
            connector_registry=registry, org_config={},
        )
        app = build_app(WebApprovalUI(), org=org_with_registry, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/login")
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://idp.example.com/authorize?")
        # Can't drive the whole IdP round trip from here without duplicating
        # test_routes_org_identity.py's own fixtures -- that file's
        # TestLogin already proves default_next_path reaches the callback's
        # redirect; this just proves server.py actually wires "/connect" in
        # as that default once a connector registry is present (see
        # test_routes_org_identity.py::TestLogin for the equivalent
        # no-registry case, which keeps the "/approvals" default).


class TestRelayWellKnown:
    def _client(self, tmp_path, monkeypatch) -> TestClient:
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        return TestClient(app, base_url=ISSUER, follow_redirects=False)

    def test_serves_the_hash_of_the_secret_without_a_session_once_a_service_uses_the_relay(self, tmp_path, monkeypatch):
        monkeypatch.setattr(relay_client, "status", lambda: {"last_ok_at": None, "last_error": "", "needed": True})
        r = self._client(tmp_path, monkeypatch).get("/.well-known/privacyfence-relay")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert r.headers["cache-control"] == "no-store"
        assert r.text == hashlib.sha256(relay_client.relay_secret().encode()).hexdigest()
        assert relay_client.relay_secret() not in r.text

    def test_is_not_found_while_no_service_uses_the_relay(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/.well-known/privacyfence-relay")
        assert r.status_code == 404
        assert r.headers["cache-control"] == "no-store"
        assert not (tmp_path / "relay_token.txt").exists()


class TestApprovalsAndSecuritySurfaceOrgMode:
    """/approvals and /security are mounted unconditionally (unlike
    /connect, they need nothing from OrgAuth.connector_registry) -- see
    web/server.py's own module docstring."""

    def test_approvals_is_mounted_with_no_connector_registry(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/approvals")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals"

    def test_security_is_mounted_since_the_issuer_hostname_becomes_the_rp_id(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/security")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/security"

    def test_step_up_config_section_reaches_the_decide_endpoint(self, tmp_path, monkeypatch):
        # A thin end-to-end wiring check -- web/routes_approvals.py's
        # own test file covers the step-up protocol itself in depth; this
        # only proves server.py actually threads org_config.json's
        # "step_up" section through StepUpConfig.from_org_config into the
        # mounted routes, rather than a hardcoded default.
        from privacyfence.principal import Principal as _P

        org = _org_auth(tmp_path, monkeypatch)
        org = org.__class__(
            provider=org.provider, sessions=org.sessions, idp=org.idp, issuer_url=org.issuer_url,
            org_config={"step_up": {"enabled": True, "scope": "writes"}},
        )
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        session_id = org.sessions.create(_P(id="alice"))
        client.cookies.set("pf_org_session", session_id)
        r = client.post("/api/approvals/does-not-exist/decide", json={"result": "deny", "csrf": session_id})
        # Unknown approval -> answer() rejects it (409), never a 404/500 --
        # proves the route (and therefore the StepUpConfig it was built
        # with) is really live, not just present.
        assert r.status_code == 409


class TestSecurityHeadersOrgMode:
    """Strict-Transport-Security is org mode's own addition to the
    header set web/test_server.py's TestSecurityHeaders already covers for local
    mode -- CSP/X-Frame-Options/Permissions-Policy/Cross-Origin-Opener-
    Policy come from the same shared _SecurityHeadersMiddleware either way,
    so they're not re-asserted here."""

    def test_strict_transport_security_is_present(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        r = client.get("/.well-known/oauth-authorization-server")
        assert r.headers.get("strict-transport-security") == "max-age=31536000; includeSubDomains"

    def test_strict_transport_security_is_present_even_on_an_error_response(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/approvals")  # unauthenticated -> 302 to /login
        assert r.status_code == 302
        assert "strict-transport-security" in r.headers


class TestCrossOriginOpenerPolicyOrgMode:
    """The OAuth paths a client's sign-in popup passes through (including the consent page,
    ADR 0177) send ``unsafe-none``, so the popup keeps its ``window.opener`` (ADR 0108);
    every other response keeps ``same-origin``."""

    def _client(self, tmp_path, monkeypatch) -> TestClient:
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        return TestClient(app, base_url=ISSUER, follow_redirects=False)

    def test_authorize_does_not_isolate_the_popup(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/authorize", params={"client_id": "unknown"})
        assert r.headers.get("cross-origin-opener-policy") == "unsafe-none"

    def test_idp_callback_does_not_isolate_the_popup(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/oauth/idp/callback", params={"state": "x", "code": "y"})
        assert r.status_code == 400
        assert r.headers.get("cross-origin-opener-policy") == "unsafe-none"

    def test_consent_pages_keep_the_popup_opener(self, tmp_path, monkeypatch):
        """The consent page renders a document, but it sits in the sign-in chain (ADR 0177)."""
        client = self._client(tmp_path, monkeypatch)
        got = client.get("/oauth/consent/x")
        posted = client.post("/oauth/consent/x", data={"csrf": "c", "decision": "allow"})
        assert got.status_code == 400
        assert got.headers.get("cross-origin-opener-policy") == "unsafe-none"
        assert posted.headers.get("cross-origin-opener-policy") == "unsafe-none"

    def test_every_other_response_is_still_same_origin(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        for path in ("/login", "/approvals", "/token", "/.well-known/oauth-authorization-server"):
            r = client.get(path)
            assert r.headers.get("cross-origin-opener-policy") == "same-origin", path


class TestCacheControlOnSensitivePagesOrgMode:
    """The org-mode counterpart of web/test_server.py's own
    TestCacheControlOnSensitivePages -- every principal-aware page org mode
    mounts, authenticated and not."""

    def _registry_org(self, org):
        registry = ConnectorRegistry(factory=lambda principal: [])
        return org.__class__(
            provider=org.provider, sessions=org.sessions, idp=org.idp, issuer_url=org.issuer_url,
            connector_registry=registry, org_config={},
        )

    def test_approvals_page_is_no_store(self, tmp_path, monkeypatch):
        from privacyfence.principal import Principal

        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        session_id = org.sessions.create(Principal(id="alice"))
        client.cookies.set("pf_org_session", session_id)
        r = client.get("/approvals")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_approvals_page_redirect_is_no_store_when_signed_out(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/approvals")
        assert r.status_code == 302
        assert r.headers.get("cache-control") == "no-store"

    def test_security_page_is_no_store(self, tmp_path, monkeypatch):
        from privacyfence.principal import Principal

        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        session_id = org.sessions.create(Principal(id="alice"))
        client.cookies.set("pf_org_session", session_id)
        r = client.get("/security")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_connect_page_is_no_store(self, tmp_path, monkeypatch):
        from privacyfence.principal import Principal

        org = _org_auth(tmp_path, monkeypatch)
        org_with_registry = self._registry_org(org)
        app = build_app(WebApprovalUI(), org=org_with_registry, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        session_id = org_with_registry.sessions.create(Principal(id="alice"))
        client.cookies.set("pf_org_session", session_id)
        r = client.get("/connect")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_downloads_redirect_is_no_store_when_signed_out(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/downloads/abc")
        assert r.status_code == 302
        assert r.headers.get("cache-control") == "no-store"


class TestDownloadsSurfaceOrgMode:
    """/downloads/{token} is mounted unconditionally in org mode
    (like /approvals/security --
    needs nothing from OrgAuth.connector_registry), and not mounted at all
    in local mode."""

    def test_downloads_route_is_mounted_with_no_connector_registry(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        # No session cookie -- proves the route exists (302 to /login, not
        # 404) without needing a real staged token.
        r = client.get("/downloads/abc")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=%2Fdownloads%2Fabc"

    def test_downloads_route_is_absent_in_local_mode(self, tmp_path):
        app = build_app(WebApprovalUI(), allowed_hosts=frozenset({"testserver"}))
        client = TestClient(app, base_url="http://testserver")
        assert client.get("/downloads/abc").status_code == 404


_OIDC_SECRET = "s" * 40
_OIDC_CB = "https://peek.example.com/cb"
_OIDC_VERIFIER = "v" * 50
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _oidc_server(tmp_path, monkeypatch):
    """The whole org app, as WebServer builds it (with its real Host allowlist), with an OIDC
    provider for one client, an MCP dispatcher, and one signed-in browser session."""
    key = oidc_provider.load_or_create_signing_key(tmp_path / "oidc_signing_key.pem")
    oidc = oidc_provider.OidcProvider(
        issuer=f"{ISSUER}/oidc", key=key,
        clients={"peek": oidc_provider.OidcClient("peek", (_OIDC_CB,), _OIDC_SECRET)},
    )
    org = dataclasses.replace(_org_auth(tmp_path, monkeypatch), oidc=oidc)
    server = WebServer(
        WebApprovalUI(), host="0.0.0.0", port=443, org=org, mcp_dispatcher=McpDispatcher(lambda: {}),
    )
    session_id = org.sessions.create(Principal(id="u1", email="ann@example.com"), email_verified=True)
    return org, server._server.config.app, session_id


def _oidc_code(client: TestClient, session_id: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(_OIDC_VERIFIER.encode()).digest()).rstrip(b"=").decode()
    client.cookies.set(SESSION_COOKIE, session_id)
    response = client.get("/oidc/authorize", params={
        "client_id": "peek", "redirect_uri": _OIDC_CB, "response_type": "code", "scope": "openid email",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    assert response.status_code == 302, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


def _oidc_token(client: TestClient, code: str):
    return client.post(
        "/oidc/token", auth=("peek", _OIDC_SECRET),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": _OIDC_CB,
              "code_verifier": _OIDC_VERIFIER},
    )


class TestOidcProviderInTheOrgApp:
    def test_oidc_token_with_issuer_host_and_no_origin_reaches_route(self, tmp_path, monkeypatch):
        # Peek and Grist call /oidc/token server-side through the proxy: the issuer's Host and no
        # Origin header. Neither the Host allowlist nor any Origin check may stop that.
        _, app, session_id = _oidc_server(tmp_path, monkeypatch)
        browser = TestClient(app, base_url=ISSUER, follow_redirects=False)
        code = _oidc_code(browser, session_id)
        # The app's own server makes these calls: no browser session cookie, no Origin.
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        response = _oidc_token(client, code)
        assert not client.cookies
        assert "cookie" not in {k.lower() for k in response.request.headers}
        assert "origin" not in {k.lower() for k in response.request.headers}
        assert response.request.headers["host"] == "pf.example.com"
        assert response.status_code == 200, response.text
        access_token = response.json()["access_token"]
        info = client.get("/oidc/userinfo", headers={"Authorization": f"Bearer {access_token}"})
        assert info.status_code == 200
        assert info.json()["sub"] == "u1"

    def test_oidc_access_token_refused_at_mcp(self, tmp_path, monkeypatch):
        _, app, session_id = _oidc_server(tmp_path, monkeypatch)
        with TestClient(app, base_url=ISSUER, follow_redirects=False) as client:
            access_token = _oidc_token(client, _oidc_code(client, session_id)).json()["access_token"]
            response = client.post(
                "/mcp", json={}, headers={**_MCP_HEADERS, "Authorization": f"Bearer {access_token}"},
            )
        assert response.status_code == 401

    def test_mcp_token_refused_at_oidc_userinfo(self, tmp_path, monkeypatch):
        org, app, _ = _oidc_server(tmp_path, monkeypatch)
        mcp_token = org.provider._mint_tokens(
            client_id="claude", scopes=[], resource=None, principal=Principal(id="u1", email="ann@example.com"),
        ).access_token
        with TestClient(app, base_url=ISSUER, follow_redirects=False) as client:
            # The MCP token is live: /mcp lets it past authentication.
            mcp = client.post("/mcp", json={}, headers={**_MCP_HEADERS, "Authorization": f"Bearer {mcp_token}"})
            assert mcp.status_code != 401
            response = client.get("/oidc/userinfo", headers={"Authorization": f"Bearer {mcp_token}"})
        assert response.status_code == 401
        assert response.json() == {"error": "invalid_token"}


class TestWebServerOrgMode:
    def test_base_url_is_the_configured_issuer_url_not_the_bind_address(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), host="0.0.0.0", port=443, org=org)
        assert server.base_url == ISSUER

    def test_mcp_token_and_control_channel_are_none_in_org_mode(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}))
        assert server.mcp_token is None
        assert server.control_channel is None

    def test_mcp_url_still_reflects_base_url_in_org_mode(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}))
        assert server.mcp_url == f"{ISSUER}/mcp"

    def test_state_stream_is_not_built_in_org_mode(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org)
        assert server.state_stream is None

    def test_allowed_hosts_is_exposed_for_reporting(self, tmp_path, monkeypatch):
        # daemon_main.py's org-mode startup line logs this set, so an admin
        # whose reverse proxy forwards some other hostname can see which
        # names the daemon actually accepts instead of only ever getting a
        # bare "Invalid Host header" per request.
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), host="0.0.0.0", port=443, org=org)
        assert "pf.example.com" in server.allowed_hosts
        assert {"127.0.0.1", "::1"} <= server.allowed_hosts

    @pytest.mark.parametrize("bind_host", ["0.0.0.0", "::"])
    def test_a_wildcard_bind_address_is_not_an_allowed_host(self, tmp_path, monkeypatch, bind_host):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), host=bind_host, port=443, org=org)
        assert bind_host not in server.allowed_hosts

    def test_a_request_addressed_to_the_wildcard_bind_address_is_refused(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), host="0.0.0.0", port=443, org=org)
        client = TestClient(server._server.config.app, base_url=ISSUER)
        r = client.get("/.well-known/oauth-authorization-server", headers={"Host": "0.0.0.0"})
        assert r.status_code == 400

    def test_issuer_hostname_is_added_to_the_allowed_hosts(self, tmp_path, monkeypatch):
        # Bound to 0.0.0.0 (a real org deployment's own bind host), which
        # alone wouldn't satisfy the Host-header allowlist for a request
        # actually addressed to the issuer's own hostname -- server.py has
        # to add that hostname itself.
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), host="0.0.0.0", port=443, org=org)
        client = TestClient(server._server.config.app, base_url=ISSUER)

        r = client.get("/.well-known/oauth-authorization-server")

        assert r.status_code == 200  # not 400 "Invalid Host header"

    def test_ssl_certfile_and_keyfile_are_accepted(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        # Must not raise -- uvicorn.Config only validates cert files exist
        # when the server actually starts, not at construction time.
        WebServer(
            WebApprovalUI(), org=org, ssl_certfile="/does/not/exist/cert.pem",
            ssl_keyfile="/does/not/exist/key.pem",
        )

    def test_trusted_proxies_wraps_the_app_in_proxy_headers_middleware(self, tmp_path, monkeypatch):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org, trusted_proxies=("10.0.0.5",))
        assert isinstance(server._server.config.app, ProxyHeadersMiddleware)

    def test_no_trusted_proxies_by_default(self, tmp_path, monkeypatch):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org)
        assert not isinstance(server._server.config.app, ProxyHeadersMiddleware)

    def test_uvicorn_does_not_add_its_own_proxy_headers_middleware(self, tmp_path, monkeypatch):
        # uvicorn.Config defaults to proxy_headers=True with 127.0.0.1/::1
        # (or $FORWARDED_ALLOW_IPS) trusted, which would honour forwarded
        # headers without any trusted_proxies configured.
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org)
        assert server._server.config.proxy_headers is False

    async def test_forwarded_proto_from_loopback_is_ignored_without_trusted_proxies(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        config = WebServer(WebApprovalUI(), org=org)._server.config
        seen: dict[str, object] = {}

        async def probe(scope, receive, send):
            seen["scheme"] = scope["scheme"]
            seen["client"] = scope["client"]

        async def noop(*_args):  # pragma: no cover - the probe never calls it
            return None

        # Swap in a probe as the app, so config.load() wraps it in exactly
        # the middleware uvicorn itself would add at serve time.
        config.app = probe
        config.load()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/", "raw_path": b"/",
            "query_string": b"", "root_path": "",
            "headers": [(b"x-forwarded-proto", b"https"), (b"x-forwarded-for", b"203.0.113.9")],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765),
        }
        await config.loaded_app(scope, noop, noop)
        assert seen == {"scheme": "http", "client": ("127.0.0.1", 50000)}


SINGLE_USER_ISSUER = "http://localhost:8765"


def _single_user_org(tmp_path, monkeypatch) -> OrgAuth:
    """A single-user server as daemon_main builds one: no IdP, and the admin config a fresh
    single-user install starts with (step-up on, a passkey required, PII reads included)."""
    from privacyfence import admin_config, oauth_apps, paths
    from privacyfence.audit_log import init_audit_logger

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    init_audit_logger(str(tmp_path / "audit"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"))
    admin = admin_config.import_from_bundle({}, mode="org", single_user=True)
    org_config = oauth_apps.effective_org_config({}, admin, mode="org", single_user=True)
    provider = OrgOAuthProvider(None, idp_callback_url="", single_user=True)
    return OrgAuth(
        provider=provider, sessions=OrgSessionStore(), idp=None, issuer_url=SINGLE_USER_ISSUER,
        org_config=org_config, bundle={}, startup_admin=admin,
    )


def _single_user_app(org: OrgAuth, caplog, web_ui=None):
    """The app, and the setup code it logged."""
    import logging
    import re

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="privacyfence.web.single_user_auth"):
        app = build_app(web_ui or WebApprovalUI(), org=org, allowed_hosts=frozenset({"localhost"}))
    code = re.search(r"Setup code: ([0-9A-F]{4}-[0-9A-F]{4})\.", caplog.text).group(1)
    return app, code


def _sign_in_with_the_setup_code(client: TestClient, code: str) -> str:
    import re

    page = client.get("/login")
    binding = re.search(r'data-binding="([^"]+)"', page.text).group(1)
    r = client.post("/login/setup", data={"code": code, "next": "/approvals", "binding": binding})
    assert r.status_code == 303, r.text
    return r.cookies[SESSION_COOKIE]


class TestSingleUserMode:
    def test_login_is_the_single_user_sign_in_page(self, tmp_path, monkeypatch, caplog):
        app, _code = _single_user_app(_single_user_org(tmp_path, monkeypatch), caplog)
        client = TestClient(app, base_url=SINGLE_USER_ISSUER, follow_redirects=False)
        r = client.get("/login")
        assert r.status_code == 200
        assert "Setup code" in r.text

    def test_there_is_no_idp_callback_and_there_is_a_continue_route(self, tmp_path, monkeypatch, caplog):
        app, _code = _single_user_app(_single_user_org(tmp_path, monkeypatch), caplog)
        client = TestClient(app, base_url=SINGLE_USER_ISSUER, follow_redirects=False)
        assert client.get("/oauth/idp/callback", params={"state": "s", "code": "c"}).status_code == 404
        assert client.get("/oauth/idp/login-callback", params={"state": "s", "code": "c"}).status_code == 404
        r = client.get("/oauth/single-user/continue", params={"state": "s"})
        assert r.status_code == 302 and r.headers["location"].startswith("/login?next=")

    def test_the_setup_code_session_reaches_security_with_localhost_as_the_relying_party(
        self, tmp_path, monkeypatch, caplog,
    ):
        app, code = _single_user_app(_single_user_org(tmp_path, monkeypatch), caplog)
        client = TestClient(app, base_url=SINGLE_USER_ISSUER, follow_redirects=False)
        session_id = _sign_in_with_the_setup_code(client, code)
        page = client.get("/security", params={"next": "/approvals"})
        assert page.status_code == 200
        assert "admin@privacyfence.localhost" in page.text
        options = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        assert options.status_code == 200
        assert options.json()["options"]["rp"]["id"] == "localhost"

    def test_an_idp_and_a_single_user_provider_must_agree(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        mismatched = OrgAuth(provider=org.provider, sessions=org.sessions, idp=None, issuer_url=ISSUER)
        with pytest.raises(ValueError, match="single-user"):
            build_app(WebApprovalUI(), org=mismatched, allowed_hosts=frozenset({"pf.example.com"}))


class TestSingleUserSessionAloneCannotApproveOrChangeAdmin:
    """Anyone who can read the log gets a session; the passkey is what a sensitive step needs."""

    @staticmethod
    def _approval(web_ui, *, gate_kind: str, pii_detected: bool = False):
        from privacyfence.principal import principal_scope
        from privacyfence.web.single_user_auth import SINGLE_USER_PRINCIPAL

        with principal_scope(SINGLE_USER_PRINCIPAL):
            approval, _ = web_ui.deferred_registry.register_or_coalesce(
                dedupe_key=f"k-{gate_kind}-{pii_detected}", connector="gmail", tool="gmail_send",
                gate_kind=gate_kind, request_id="r1", summary="a message", tool_name="Send",
                pii_detected=pii_detected,
            )
        web_ui.deferred_registry.set_html(approval.id, "<!doctype html><html><head></head><body>CARD</body></html>")
        return approval

    def _world(self, tmp_path, monkeypatch, caplog):
        from privacyfence.approvals import PendingApprovalRegistry

        web_ui = WebApprovalUI(registry=PendingApprovalRegistry())
        app, code = _single_user_app(_single_user_org(tmp_path, monkeypatch), caplog, web_ui=web_ui)
        client = TestClient(app, base_url=SINGLE_USER_ISSUER, follow_redirects=False)
        return client, _sign_in_with_the_setup_code(client, code), web_ui

    @pytest.mark.parametrize("gate_kind,pii", [("popup", False), ("review", True)])
    def test_an_approval_without_a_passkey_is_refused(self, tmp_path, monkeypatch, caplog, gate_kind, pii):
        client, session_id, web_ui = self._world(tmp_path, monkeypatch, caplog)
        approval = self._approval(web_ui, gate_kind=gate_kind, pii_detected=pii)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert (r.status_code, r.json()["error"]) == (403, "passkey_enrollment_required")
        assert web_ui.deferred_registry.get(approval.id) is not None

    @pytest.mark.parametrize("gate_kind,pii", [("popup", False), ("review", True)])
    def test_an_approval_without_an_assertion_gets_the_step_up_challenge(
        self, tmp_path, monkeypatch, caplog, gate_kind, pii,
    ):
        from privacyfence import webauthn_stepup as wa
        from privacyfence.web.single_user_auth import SINGLE_USER_PRINCIPAL

        client, session_id, web_ui = self._world(tmp_path, monkeypatch, caplog)
        wa.add_credential(SINGLE_USER_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = self._approval(web_ui, gate_kind=gate_kind, pii_detected=pii)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428
        assert "webauthn_options" in r.json()
        assert web_ui.deferred_registry.get(approval.id) is not None

    def test_a_sensitive_admin_action_without_a_passkey_is_refused(self, tmp_path, monkeypatch, caplog):
        client, session_id, _web_ui = self._world(tmp_path, monkeypatch, caplog)
        r = client.post("/api/admin/set_web_push", json={"enabled": False, "csrf": session_id})
        assert (r.status_code, r.json()) == (
            403, {"error": "Add a passkey on /security first.", "enroll_url": "/security"},
        )

    def test_a_sensitive_admin_action_without_an_assertion_gets_the_challenge(self, tmp_path, monkeypatch, caplog):
        from privacyfence import webauthn_stepup as wa
        from privacyfence.web.single_user_auth import SINGLE_USER_PRINCIPAL

        client, session_id, _web_ui = self._world(tmp_path, monkeypatch, caplog)
        wa.add_credential(SINGLE_USER_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        r = client.post("/api/admin/set_web_push", json={"enabled": False, "csrf": session_id})
        assert r.status_code == 428
        assert "webauthn_options" in r.json()


class TestBrowserSignInPolicy:
    def test_the_login_routes_get_the_policy_bound_to_the_idp(self, tmp_path, monkeypatch):
        seen = {}
        real = routes_org_identity.build_routes

        def spy(**kwargs):
            seen["policy"] = kwargs["policy"]
            return real(**kwargs)

        monkeypatch.setattr(routes_org_identity, "build_routes", spy)
        org = _org_auth(tmp_path, monkeypatch)
        builtin = dataclasses.replace(org.idp, builtin=True, admin_emails=("alice@acme.com",))
        org = OrgAuth(provider=org.provider, sessions=org.sessions, idp=builtin, issuer_url=ISSUER,
                      org_config={"authz": {"allowed_domains": ["acme.org"]}})
        build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        policy = seen["policy"]
        assert isinstance(policy, oi.IdpAuthzPolicy)
        assert policy.builtin is True and policy.admin_emails == ("alice@acme.com",)
        assert policy.allowed_domains == ("acme.org",) and policy.issuer == "https://idp.example.com"
