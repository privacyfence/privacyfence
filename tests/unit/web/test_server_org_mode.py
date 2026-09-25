"""Tests for web/server.py's org-mode wiring: build_app(org=...) and
WebServer(org=...) -- see test_org_mcp_e2e.py for the full DCR/authorize/
token/tool-call flow driven over real HTTP; this file covers what server.py
itself is responsible for: which routes exist (and, just as importantly,
which don't), TLS/trusted-proxies plumbing, and base_url/allowed_hosts.
"""
from __future__ import annotations

from starlette.testclient import TestClient

from privacyfence import org_identity as oi
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.principal import Principal
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
        # /settings and /settings/privacy are real routes -- a
        # small, purpose-built surface (web/routes_settings.py's build_org_routes),
        # not the local-mode dispatcher above. An unauthenticated request is
        # redirected to /login, same as /approvals.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/settings")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/settings"

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
        assert r.headers["location"] == "/login"

    def test_downloads_route_is_absent_in_local_mode(self, tmp_path):
        app = build_app(WebApprovalUI(), allowed_hosts=frozenset({"testserver"}))
        client = TestClient(app, base_url="http://testserver")
        assert client.get("/downloads/abc").status_code == 404


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
        assert {"0.0.0.0", "127.0.0.1", "::1"} <= server.allowed_hosts

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
