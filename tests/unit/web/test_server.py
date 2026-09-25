"""Tests for web/server.py -- the Host allowlist and security-header
middleware, and the bootstrap-code flow that authenticates a browser.
Covers the Host allowlist against DNS rebinding, CSP/X-Frame-Options,
Cache-Control -- the last one is per-route, tested in test_routes_approvals.py
instead.
"""
from __future__ import annotations

import sys

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from privacyfence.principal import LOCAL_PRINCIPAL_ID, Principal, current_principal
from privacyfence.web.csp import build_csp
from privacyfence.web.server import (
    DEFAULT_PORT,
    WebServer,
    _local_principal_resolver,
    _parse_host_header,
    _PrincipalScopeMiddleware,
    _SecurityHeadersMiddleware,
    build_app,
)
from privacyfence.web.session_auth import (
    PROVENANCE_HUMAN,
    PROVENANCE_UNATTESTED,
    SESSION_COOKIE,
    BootstrapStore,
    LocalSessionStore,
)
from privacyfence.web_approval_ui import WebApprovalUI


def _signed_in(client: TestClient, sessions: LocalSessionStore) -> str:
    """Mirrors test_routes_org_approvals.py's own ``_signed_in`` for
    OrgSessionStore -- creates a real session directly (bypassing the HTTP
    bootstrap exchange, which TestBootstrapFlow below exercises on its
    own) and sets it as the cookie, for tests that only care about what
    happens *after* authentication."""
    session_id = sessions.create()
    client.cookies.set(SESSION_COOKIE, session_id)
    return session_id


class TestHostAllowlist:
    def _client(self, allowed_hosts=frozenset({"localhost"})):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions, allowed_hosts=allowed_hosts)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        return client

    def test_allowed_host_passes_through(self):
        r = self._client().get("/approvals")
        assert r.status_code == 200

    def test_disallowed_host_is_rejected_before_reaching_any_route(self):
        r = self._client().get("/approvals", headers={"Host": "evil.example.com"})
        assert r.status_code == 400

    def test_port_suffix_on_the_host_header_is_ignored_for_matching(self):
        r = self._client().get("/approvals", headers={"Host": "localhost:9999"})
        assert r.status_code == 200

    def test_ipv4_host_with_port_matches_the_bare_address(self):
        client = self._client(allowed_hosts=frozenset({"127.0.0.1"}))
        r = client.get("/approvals", headers={"Host": "127.0.0.1:9999"})
        assert r.status_code == 200

    def test_ipv6_literal_host_matches_after_stripping_brackets(self):
        # A naive split(":", 1)[0] returns "[" for this, which could never
        # be in any real allowlist.
        client = self._client(allowed_hosts=frozenset({"::1"}))
        r = client.get("/approvals", headers={"Host": "[::1]"})
        assert r.status_code == 200

    def test_ipv6_literal_host_with_port_matches_after_stripping_brackets(self):
        client = self._client(allowed_hosts=frozenset({"::1"}))
        r = client.get("/approvals", headers={"Host": "[::1]:9999"})
        assert r.status_code == 200

    def test_malformed_host_header_is_rejected(self):
        r = self._client().get("/approvals", headers={"Host": "[::1"})
        assert r.status_code == 400


class TestParseHostHeader:
    """Direct coverage of the RFC-3986-aware parser that replaced the manual
    ``split(":", 1)[0]`` with -- TestHostAllowlist above covers it wired
    into the real middleware, this covers every branch of the parser
    itself."""

    def test_bare_hostname(self):
        assert _parse_host_header("localhost") == "localhost"

    def test_hostname_is_lowercased(self):
        assert _parse_host_header("LocalHost") == "localhost"

    def test_ipv4_without_port(self):
        assert _parse_host_header("127.0.0.1") == "127.0.0.1"

    def test_ipv4_with_port(self):
        assert _parse_host_header("127.0.0.1:8080") == "127.0.0.1"

    def test_ipv6_without_port(self):
        assert _parse_host_header("[::1]") == "::1"

    def test_ipv6_with_port(self):
        assert _parse_host_header("[::1]:8443") == "::1"

    def test_empty_header_is_rejected(self):
        assert _parse_host_header("") is None

    def test_unparsable_port_is_rejected(self):
        assert _parse_host_header("[::1]:not-a-port") is None

    def test_userinfo_is_rejected(self):
        # A Host header never carries "user@host" -- accepting it would
        # let an attacker-controlled prefix ride along to a hostname that
        # happens to be allowed.
        assert _parse_host_header("attacker@localhost") is None

    def test_path_smuggled_after_the_host_is_rejected(self):
        assert _parse_host_header("localhost/evil") is None

    def test_query_smuggled_after_the_host_is_rejected(self):
        assert _parse_host_header("localhost?x=1") is None

    def test_fragment_smuggled_after_the_host_is_rejected(self):
        assert _parse_host_header("localhost#frag") is None


class TestPrincipalScopeMiddleware:
    """Entered once per HTTP request, in exactly one place, for the
    browser surface -- proves
    the ASGI wiring actually scopes a real request (and only that request),
    not just that principal_scope() itself works (that's test_principal.py's
    job)."""

    async def _whoami_app(self, scope, receive, send) -> None:
        response = JSONResponse({"principal_id": current_principal().id})
        await response(scope, receive, send)

    def test_default_resolver_scopes_the_request_to_local_principal(self):
        app = _PrincipalScopeMiddleware(self._whoami_app, lambda request: current_principal())
        # current_principal() outside any request is LOCAL_PRINCIPAL -- the
        # resolver above just round-trips whatever's ambient, so this proves
        # the middleware calls it and enters the scope, not merely that it
        # exists.
        client = TestClient(app)
        r = client.get("/")
        assert r.json() == {"principal_id": LOCAL_PRINCIPAL_ID}

    def test_a_custom_resolver_is_honored_and_scopes_only_that_request(self):
        app = _PrincipalScopeMiddleware(self._whoami_app, lambda request: Principal(id="alice"))
        client = TestClient(app)

        r = client.get("/")

        assert r.json() == {"principal_id": "alice"}
        # And the scope doesn't leak past the request that opened it.
        assert current_principal().id == LOCAL_PRINCIPAL_ID

    def test_build_app_wires_a_custom_principal_resolver_through_over_real_http(self):
        # Exercises build_app()'s own principal_resolver parameter (not the
        # middleware in isolation, which the two tests above already cover)
        # against a plain, non-streaming route -- what's being checked is
        # only that build_app actually threads the parameter through to
        # _PrincipalScopeMiddleware, i.e. the resolver gets consulted at all.
        seen = []

        def resolver(request):
            seen.append(1)
            return Principal(id="alice")

        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions, principal_resolver=resolver)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)

        r = client.get("/approvals")

        assert r.status_code == 200
        assert seen  # the custom resolver was actually consulted


class TestLocalPrincipalResolver:
    """ADR 0008: real local-mode traffic's own resolver -- a session
    resolves to whichever principal minted it; anything else (no cookie,
    an unknown one) is LOCAL_PRINCIPAL, byte-identical to every session
    before this ADR."""

    def test_no_cookie_is_local_principal(self):
        sessions = LocalSessionStore()
        resolver = _local_principal_resolver(sessions)
        client = TestClient(_PrincipalScopeMiddleware(
            lambda scope, receive, send: JSONResponse({"id": current_principal().id})(scope, receive, send),
            resolver,
        ))

        r = client.get("/")

        assert r.json() == {"id": LOCAL_PRINCIPAL_ID}

    def test_a_session_minted_for_another_principal_resolves_to_it(self):
        sessions = LocalSessionStore()
        session_id = sessions.create(principal_id="os-1002")
        resolver = _local_principal_resolver(sessions)
        client = TestClient(_PrincipalScopeMiddleware(
            lambda scope, receive, send: JSONResponse({"id": current_principal().id})(scope, receive, send),
            resolver,
        ))
        client.cookies.set(SESSION_COOKIE, session_id)

        r = client.get("/")

        assert r.json() == {"id": "os-1002"}

    @staticmethod
    def _request(session_id: str):
        from starlette.requests import Request

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        headers = [(b"cookie", f"{SESSION_COOKIE}={session_id}".encode())]
        scope = {
            "type": "http", "headers": headers, "method": "GET", "path": "/",
            "scheme": "http", "server": ("localhost", 8765),
        }
        return Request(scope, receive=receive)

    def test_a_session_explicitly_minted_for_local_stays_the_local_principal_object(self):
        from privacyfence.principal import LOCAL_PRINCIPAL

        sessions = LocalSessionStore()
        session_id = sessions.create(principal_id=LOCAL_PRINCIPAL_ID)
        resolver = _local_principal_resolver(sessions)

        assert resolver(self._request(session_id)) is LOCAL_PRINCIPAL

    def test_an_unknown_session_id_falls_back_to_local(self):
        sessions = LocalSessionStore()
        resolver = _local_principal_resolver(sessions)

        assert resolver(self._request("not-a-real-session")).id == LOCAL_PRINCIPAL_ID


class TestSettingsIsOwnerOnly:
    """ADR 0008's own scoping decision: /settings still administers the
    one owner's SettingsController/connector set -- a non-owner principal
    gets a 404, the same "don't confirm existence" shape every other
    cross-principal lookup in this codebase already uses."""

    def _app(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions, controller=controller)
        return TestClient(app, base_url="http://localhost"), sessions

    def test_the_owner_reaches_settings(self, tmp_path, monkeypatch):
        client, sessions = self._app(tmp_path, monkeypatch)
        _signed_in(client, sessions)

        r = client.get("/settings")

        assert r.status_code == 200

    def test_a_non_owner_principal_gets_a_plain_404(self, tmp_path, monkeypatch):
        client, sessions = self._app(tmp_path, monkeypatch)
        session_id = sessions.create(principal_id="os-1002")
        client.cookies.set(SESSION_COOKIE, session_id)

        r = client.get("/settings")

        assert r.status_code == 404

    def test_a_non_owner_principal_cannot_reach_settings_actions_either(self, tmp_path, monkeypatch):
        client, sessions = self._app(tmp_path, monkeypatch)
        session_id = sessions.create(principal_id="os-1002")
        client.cookies.set(SESSION_COOKIE, session_id)

        r = client.post("/api/settings/quit_app", json={"csrf": session_id})

        assert r.status_code == 404


class TestSecurityHeaders:
    def _client(self):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        return client

    def test_content_security_policy_is_present_and_locked_down(self):
        r = self._client().get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_frame_options_deny(self):
        r = self._client().get("/approvals")
        assert r.headers.get("x-frame-options") == "DENY"

    def test_content_type_options_nosniff(self):
        r = self._client().get("/approvals")
        assert r.headers.get("x-content-type-options") == "nosniff"

    def test_headers_are_present_even_on_an_error_response(self):
        app = build_app(WebApprovalUI())
        r = TestClient(app, base_url="http://localhost").get("/approvals")  # unauthenticated -> 401
        assert r.status_code == 401
        assert r.headers.get("x-frame-options") == "DENY"

    def test_permissions_policy_denies_unused_powerful_features(self):
        r = self._client().get("/approvals")
        policy = r.headers.get("permissions-policy", "")
        assert "camera=()" in policy
        assert "microphone=()" in policy
        assert "geolocation=()" in policy

    def test_permissions_policy_leaves_webauthn_at_its_self_default(self):
        # web/routes_security.py's step-up flow needs these from this same
        # origin -- see _PERMISSIONS_POLICY's own comment.
        r = self._client().get("/approvals")
        policy = r.headers.get("permissions-policy", "")
        assert "publickey-credentials-get=(self)" in policy
        assert "publickey-credentials-create=(self)" in policy

    def test_cross_origin_opener_policy_is_same_origin(self):
        r = self._client().get("/approvals")
        assert r.headers.get("cross-origin-opener-policy") == "same-origin"

    def test_no_strict_transport_security_in_local_mode(self):
        # Local mode is plain http://localhost by design (module docstring)
        # -- sending HSTS there would be at best inert, at worst harmful
        # (see _SecurityHeadersMiddleware's own docstring).
        r = self._client().get("/approvals")
        assert "strict-transport-security" not in r.headers


class TestCacheControlOnSensitivePages:
    """A sweep across every local-mode page that carries session-
    or approval-specific content, rather than trusting that each route author remembered
    Cache-Control: no-store on their own -- a future new page that forgets
    it fails here instead of shipping silently cacheable."""

    def test_the_approvals_page_is_no_store(self):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_the_settings_page_is_no_store(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions, controller=controller)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        r = client.get("/settings")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_the_unauthorized_landing_page_is_no_store(self):
        # Regression test: this page names a live bearer-secret command
        # (session_auth.unauthorized_html), so it must never be cached.
        app = build_app(WebApprovalUI())
        r = TestClient(app, base_url="http://localhost").get("/approvals")
        assert r.status_code == 401
        assert r.headers.get("cache-control") == "no-store"


class TestCspNonce:
    """A fresh CSP nonce per response, matching the one in the document."""

    def _client(self):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        return client

    def test_script_src_and_style_src_elem_carry_a_nonce_not_unsafe_inline(self):
        r = self._client().get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        directives = dict(part.strip().split(" ", 1) for part in csp.split(";") if part.strip())
        assert "'nonce-" in directives["script-src"]
        assert "unsafe-inline" not in directives["script-src"]
        assert "'nonce-" in directives["style-src-elem"]
        assert "unsafe-inline" not in directives["style-src-elem"]
        # style-src-attr keeps 'unsafe-inline' deliberately -- see
        # web/csp.py's own module docstring for why.
        assert directives["style-src-attr"] == "'unsafe-inline'"

    def test_object_src_and_frame_src_allow_data_uris(self):
        r = self._client().get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        directives = dict(part.strip().split(" ", 1) for part in csp.split(";") if part.strip())
        assert directives["object-src"] == "data:"
        assert directives["frame-src"] == "data:"

    def test_nonce_differs_across_separate_requests(self):
        client = self._client()
        first = client.get("/approvals").headers["content-security-policy"]
        second = client.get("/approvals").headers["content-security-policy"]
        assert first != second

    def test_body_style_and_script_tags_carry_the_response_own_nonce(self):
        r = self._client().get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        nonce = next(
            part.split("'nonce-", 1)[1].rstrip("'") for part in csp.split(";") if part.strip().startswith("script-src")
        )
        assert f'<style nonce="{nonce}">' in r.text
        assert f'<script nonce="{nonce}">' in r.text


class TestSecurityHeadersMiddlewareReplacesNotExtends:
    """Blindly appending the middleware's fixed header set would emit *two*
    headers of the same name if the wrapped app already set one of them --
    this proves it overrides instead."""

    async def _app(self, scope, receive, send):
        response = JSONResponse({"ok": True}, headers={
            "X-Frame-Options": "ALLOWALL",
            "Content-Security-Policy": "default-src *",
        })
        await response(scope, receive, send)

    def test_conflicting_headers_from_the_app_are_overridden_not_duplicated(self):
        app = _SecurityHeadersMiddleware(self._app)
        client = TestClient(app, base_url="http://localhost")
        r = client.get("/")
        assert r.headers.get_list("x-frame-options") == ["DENY"]
        csp_values = r.headers.get_list("content-security-policy")
        assert len(csp_values) == 1
        assert csp_values[0] != "default-src *"
        assert "default-src 'none'" in csp_values[0]


class TestBuildCsp:
    def test_same_nonce_appears_in_every_nonce_source(self):
        csp = build_csp("the-nonce")
        assert csp.count("'nonce-the-nonce'") == 2  # script-src, style-src-elem

    def test_no_bare_style_src_directive(self):
        # A bare 'style-src' would silently win over style-src-elem/attr in
        # browsers that don't support the split subdirectives, re-opening
        # exactly the hole this policy means to close -- see web/csp.py's
        # own module docstring.
        csp = build_csp("n")
        directive_names = [part.strip().split(" ", 1)[0] for part in csp.split(";") if part.strip()]
        assert "style-src" not in directive_names
        assert "style-src-elem" in directive_names
        assert "style-src-attr" in directive_names


class TestWebServerConstruction:
    def test_binds_to_localhost_by_default(self):
        server = WebServer(WebApprovalUI(), port=0)
        assert server.host == "localhost"

    def test_uses_the_default_port_constant(self):
        server = WebServer(WebApprovalUI())
        assert server.port == DEFAULT_PORT

    def test_base_url_reflects_host_and_port(self):
        server = WebServer(WebApprovalUI(), host="localhost", port=1234)
        assert server.base_url == "http://localhost:1234"

    def test_mcp_url_is_none_without_an_mcp_dispatcher(self):
        server = WebServer(WebApprovalUI())
        assert server.mcp_url is None
        assert server.mcp_token is None

    def test_mcp_url_is_set_when_an_mcp_dispatcher_is_given(self):
        from privacyfence.web.mcp_dispatch import McpDispatcher

        server = WebServer(
            WebApprovalUI(), host="localhost", port=1234,
            mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token="mcp-tok",
        )
        assert server.mcp_url == "http://localhost:1234/mcp"

    def test_build_app_requires_a_token_or_a_verifier_for_an_mcp_dispatcher(self):
        from privacyfence.web.mcp_dispatch import McpDispatcher

        with pytest.raises(ValueError, match="mcp_token or mcp_verifier"):
            build_app(WebApprovalUI(), mcp_dispatcher=McpDispatcher(lambda: {}))


# --------------------------------------------------------------------------- #
# WebServer owns a ControlChannelServer alongside the ASGI app
# in local mode -- test_control_channel.py covers the channel's own protocol
# and socket handling; this is just the wiring (built in __init__, started/
# stopped alongside the rest of WebServer's own lifecycle).
# --------------------------------------------------------------------------- #

class TestWebServerControlChannel:
    def test_local_mode_builds_a_control_channel(self):
        server = WebServer(WebApprovalUI(), port=0)
        assert server.control_channel is not None
        assert server.control_channel.address is None  # not started yet

    def test_local_mode_wires_a_status_callback(self, monkeypatch):
        # daemon_status.py's "the
        # control channel answered" source is this callback, wired to
        # local_status_payload() -- checked here rather than only through a
        # real round trip, since TestWebServerControlChannel's other tests
        # already prove the channel itself binds and tears down correctly.
        import json

        server = WebServer(WebApprovalUI(), port=0)

        payload = json.loads(server.control_channel._status())

        assert set(payload) == {"version", "pid", "started_at", "mode", "separated", "connectors"}
        assert payload["mode"] == "local"

    def test_mint_mcp_token_is_wired_only_when_mcp_is_enabled(self):
        server = WebServer(WebApprovalUI(), port=0)  # no mcp_dispatcher

        assert server.mcp_verifier is None
        assert server.control_channel._mint_mcp_token is None

    def test_mint_mcp_token_is_wired_when_mcp_is_enabled(self):
        from privacyfence.web.mcp_dispatch import McpDispatcher

        server = WebServer(
            WebApprovalUI(), port=0, mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token="mcp-tok",
        )

        assert server.mcp_verifier is not None
        assert server.control_channel._mint_mcp_token == server._mint_mcp_token

    async def test_mint_for_the_owner_returns_the_same_token_already_registered(self, tmp_path, monkeypatch):
        # No explicit mcp_token -- server.mcp_token is load_or_create_mcp_token()'s
        # own real (disk-backed) value here, exactly like every real caller,
        # so MINT MCP for the owner reads back the identical value rather
        # than minting a second one alongside it. Sandboxed to tmp_path:
        # this is the one real (unmocked) load_or_create_mcp_token() call in
        # this class, and paths.data_dir() defaults to this checkout's own
        # root in a dev install, which must never be written to by a test.
        from privacyfence import paths
        from privacyfence.web.mcp_dispatch import McpDispatcher

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = WebServer(WebApprovalUI(), port=0, mcp_dispatcher=McpDispatcher(lambda: {}))

        minted = server._mint_mcp_token(False)

        assert minted == server.mcp_token
        result = await server.mcp_verifier.verify_token(minted)
        assert result is not None and result.subject == LOCAL_PRINCIPAL_ID

    async def test_mint_for_a_different_principal_registers_a_distinct_token(self, monkeypatch):
        from privacyfence.principal import Principal, principal_scope
        from privacyfence.web.mcp_dispatch import McpDispatcher

        server = WebServer(
            WebApprovalUI(), port=0, mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token="mcp-tok",
        )
        monkeypatch.setattr(
            "privacyfence.web.server.mcp_auth._mcp_token_path",
            lambda principal: __import__("pathlib").Path(f"/tmp/nonexistent-{principal.id}"),
        )
        monkeypatch.setattr(
            "privacyfence.web.server.mcp_auth._write_new_token",
            lambda principal, path: f"generated-for-{principal.id}",
        )

        with principal_scope(Principal(id="os-1002")):
            minted = server._mint_mcp_token(False)

        assert minted == "generated-for-os-1002"
        assert minted != "mcp-tok"
        result = await server.mcp_verifier.verify_token(minted)
        assert result is not None and result.subject == "os-1002"

    def test_org_mode_builds_no_control_channel(self, tmp_path, monkeypatch):
        from privacyfence import org_identity as oi
        from privacyfence import paths
        from privacyfence.web.oauth_provider import OrgOAuthProvider
        from privacyfence.web.org_session import OrgSessionStore
        from privacyfence.web.server import OrgAuth

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"),
        )
        monkeypatch.setattr(
            "privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"),
        )
        idp = oi.IdpConfig(
            issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
            authorization_endpoint="https://idp.example.com/authorize",
            token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
        )
        issuer_url = "https://org.example.com"
        provider = OrgOAuthProvider(idp, idp_callback_url=f"{issuer_url}/oauth/idp/callback")
        org = OrgAuth(provider=provider, sessions=OrgSessionStore(), idp=idp, issuer_url=issuer_url)

        server = WebServer(WebApprovalUI(), host="localhost", port=0, org=org)

        assert server.control_channel is None

    def _channel_exists(self, address: str) -> bool:
        # Named pipes aren't filesystem objects the way a POSIX socket path
        # is -- Path.exists() on Windows ends up doing a GetFileAttributes-
        # style probe against the pipe that can itself raise WinError 231
        # ("all pipe instances are busy") rather than cleanly returning
        # False, since it's effectively a connection attempt racing the
        # accept loop's own. tests.control_channel_client's
        # windows_pipe_exists() (open-then-close, OSError caught) is the
        # liveness check that's actually safe to make on that platform.
        if sys.platform == "win32":
            from tests.control_channel_client import windows_pipe_exists

            return windows_pipe_exists(address)
        from pathlib import Path

        return Path(address).exists()

    def test_start_binds_the_channel_and_stop_tears_it_down(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = WebServer(WebApprovalUI(), port=0)
        try:
            server.start()
            assert server.control_channel.address is not None
            assert self._channel_exists(server.control_channel.address)
        finally:
            server.stop()
        assert not self._channel_exists(server.control_channel.address)

    def test_a_code_minted_through_the_channel_authenticates_the_real_server(self, tmp_path, monkeypatch):
        import socket

        import httpx

        from privacyfence import paths
        from tests.control_channel_client import mint_bootstrap_code

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        # uvicorn.Config binds a fixed port at WebServer construction time,
        # so ``port=0`` (let the OS pick) isn't observable afterwards the
        # way it is for a bare socket -- pick a real free port up front
        # instead, the same way every other real-socket test in this module
        # already has to.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        server = WebServer(WebApprovalUI(), host="127.0.0.1", port=free_port)
        server.start()
        try:
            code = mint_bootstrap_code(tmp_path)

            resp = httpx.get(
                f"http://127.0.0.1:{free_port}/approvals", params={"bootstrap": code}, follow_redirects=False,
            )
            assert "pf_session" in resp.headers.get("set-cookie", "")
        finally:
            server.stop()


class TestHumanSessionWiring:
    """The human-session gate, at the one place that decides
    whether the gate is on: privilege separation. It is what ADR 0003 makes
    mandatory for every packaged install (decision 6), and what guarantees
    the companion an attested session is minted through exists at all
    (decisions 3-5) -- on an unseparated build-from-source install neither
    holds, and an agent that can rewrite the credential store directly (ADR
    0002 decision 6) gains nothing from a session check anyway.

    The decide route's own behavior is web/routes_approvals.py's to test
    (TestHumanSessionRequiredToApprove there); what is checked here is that
    ``build_app`` actually turns it on, which nothing else would notice.
    """

    def _decide_status(self, monkeypatch, *, separated: bool) -> int:
        from privacyfence import privilege_separation

        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: separated)
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions)
        client = TestClient(app, base_url="http://localhost")
        session_id = sessions.create(provenance=PROVENANCE_UNATTESTED)
        client.cookies.set(SESSION_COOKIE, session_id)
        return client.post(
            "/api/approvals/no-such-approval/decide",
            json={"action": "resolve", "result": "accept", "csrf": session_id},
        ).status_code

    def test_a_separated_install_refuses_an_unattested_approval(self, monkeypatch):
        assert self._decide_status(monkeypatch, separated=True) == 403

    def test_an_unseparated_install_is_unchanged(self, monkeypatch):
        # 409, not 200: the id does not exist, which is the answer this
        # route has always given past the gate -- the point is that it got
        # past the gate at all.
        assert self._decide_status(monkeypatch, separated=False) == 409


class TestBootstrapFlow:
    def _app(self):
        sessions = LocalSessionStore()
        bootstrap = BootstrapStore()
        app = build_app(WebApprovalUI(), sessions=sessions, bootstrap=bootstrap)
        return app, sessions, bootstrap

    def test_valid_code_mints_a_session_and_redirects_with_no_query_string(self):
        app, _sessions, bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)
        code = bootstrap.mint()

        r = client.get(f"/approvals?bootstrap={code}")

        assert r.status_code == 303
        assert r.headers["location"] == "/approvals"
        assert "pf_session" in r.headers.get("set-cookie", "")

    def test_the_session_inherits_the_code_s_own_provenance(self):
        """The mint is the only moment
        anything knew how this credential came to exist (web/session_auth.py's
        ``PROVENANCE_*``), so the exchange carries it across rather than
        deciding it here."""
        app, sessions, bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)

        for provenance in (PROVENANCE_HUMAN, PROVENANCE_UNATTESTED):
            client.cookies.clear()
            r = client.get(f"/approvals?bootstrap={bootstrap.mint(provenance=provenance)}")
            session_id = client.cookies.get("pf_session")
            assert r.status_code == 303
            assert sessions.provenance(session_id) == provenance

    def test_a_code_with_no_provenance_of_its_own_mints_an_unattested_session(self):
        app, sessions, bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)

        client.get(f"/approvals?bootstrap={bootstrap.mint()}")

        assert sessions.provenance(client.cookies.get("pf_session")) == PROVENANCE_UNATTESTED

    def test_code_is_single_use(self):
        app, _sessions, bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)
        code = bootstrap.mint()
        client.get(f"/approvals?bootstrap={code}")
        client.cookies.clear()

        r = client.get(f"/approvals?bootstrap={code}")

        assert "pf_session" not in r.headers.get("set-cookie", "")

    def test_unknown_code_mints_no_session(self):
        app, _sessions, _bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)

        r = client.get("/approvals?bootstrap=not-a-real-code")

        assert r.status_code == 303
        assert r.headers["location"] == "/approvals"
        assert "pf_session" not in r.headers.get("set-cookie", "")
        follow = client.get(r.headers["location"])
        assert follow.status_code == 401

    def test_following_the_redirect_reaches_an_authenticated_page(self):
        app, _sessions, bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=True)
        code = bootstrap.mint()

        r = client.get(f"/approvals?bootstrap={code}")

        assert r.status_code == 200
        assert "Nothing is waiting" in r.text

    def test_the_old_token_query_param_no_longer_authenticates(self):
        # No route honors a ?token=<persistent secret>, only
        # ?bootstrap=<one-time code>.
        app, _sessions, _bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost")

        r = client.get("/approvals?token=some-old-style-token")

        assert r.status_code == 401

    def test_api_bootstrap_no_longer_exists_as_an_http_route(self):
        # Minting a fresh code on demand is not on this loopback-HTTP
        # surface at all but on web/control_channel.py's
        # ControlChannelServer -- there is no HTTP route to authenticate
        # at all, correct or incorrect Bearer header alike.
        app, _sessions, _bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost")

        r = client.post("/api/bootstrap")

        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# No sign-in link is ever written to a discovery file: handoff/ is
# group-shared with the logged-in user by design, so such a file would be a
# session for the taking by anything running as that user.
# --------------------------------------------------------------------------- #

class TestNoBootstrapUrlFiles:
    def _server(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return WebServer(WebApprovalUI(), host="localhost", port=0)

    def test_nothing_mints_a_link_into_the_handoff_directory(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch)
        server.start()
        try:
            # ``web_base_url`` is the companion's own discovery file and
            # carries no credential -- just "http://127.0.0.1:<port>".
            written = sorted(
                p.name for p in tmp_path.iterdir()
                if p.name.endswith("_url") and p.name != "web_base_url"
            )
        finally:
            server.stop()

        assert written == []
        # Belt and braces: no file anywhere under the data directory carries
        # a live code, whatever it is called.
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert "bootstrap=" not in path.read_text(encoding="utf-8", errors="replace")

    def test_files_it_does_not_write_are_left_alone(self, tmp_path, monkeypatch):
        """ADR 0041: the daemon only manages the current layout's files, so
        a file with an unrelated name in the handoff directory survives a
        start/stop cycle untouched."""
        server = self._server(tmp_path, monkeypatch)
        stray = tmp_path / "approvals_url"
        stray.write_text("not the daemon's", encoding="utf-8")

        server.start()
        server.stop()

        assert stray.read_text(encoding="utf-8") == "not the daemon's"

    def test_the_minting_method_is_gone_from_the_server_itself(self, tmp_path, monkeypatch):
        """Minting is the control channel's business now, and who may ask
        for an *attested* code is decided there (web/control_channel.py).
        Asserted so that reintroducing a convenience method here -- which is
        what fed both the discovery files and the retired MCP tool -- fails
        rather than passes quietly."""
        server = self._server(tmp_path, monkeypatch)
        assert not hasattr(server, "mint_bootstrap_url")


# --------------------------------------------------------------------------- #
# The mcp_url discovery file mcpb/shim reads to find /mcp without any
# config the user has to edit. Only written/cleared
# when this WebServer actually has an mcp_dispatcher (i.e. web.mcp.enabled);
# a server started for the approval UI alone must not claim /mcp exists.
# --------------------------------------------------------------------------- #

class TestMcpUrlFile:
    def _server(self, tmp_path, monkeypatch, *, with_mcp: bool):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        kwargs = {}
        if with_mcp:
            from privacyfence.web.mcp_dispatch import McpDispatcher

            kwargs = {"mcp_dispatcher": McpDispatcher(lambda: {}), "mcp_token": "mcp-tok"}
        return WebServer(WebApprovalUI(), host="localhost", port=0, **kwargs)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_start_writes_the_file_when_mcp_is_enabled(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, with_mcp=True)
        try:
            server.start()
            mcp_url_file = tmp_path / "mcp_url"
            assert mcp_url_file.exists()
            assert mcp_url_file.read_text(encoding="utf-8") == server.mcp_url
            assert oct(mcp_url_file.stat().st_mode)[-3:] == "600"
        finally:
            server.stop()

    def test_stop_clears_the_file(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, with_mcp=True)
        server.start()
        server.stop()
        assert not (tmp_path / "mcp_url").exists()

    def test_no_file_at_all_when_mcp_is_not_enabled(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, with_mcp=False)
        try:
            server.start()
            assert not (tmp_path / "mcp_url").exists()
        finally:
            server.stop()


# --------------------------------------------------------------------------- #
# Audience separation: the MCP bearer token and the approval
# surface's session cookie/CSRF token are different secrets, checked in
# different middleware, and neither is ever accepted on the other's routes.
# One test in this class is there to fail loudly if the middleware is ever
# reordered.
# --------------------------------------------------------------------------- #

class TestAudienceSeparation:
    MCP_TOKEN = "mcp-token-0123456789"

    def _app(self):
        from privacyfence.web.mcp_dispatch import McpDispatcher

        self.sessions = LocalSessionStore()
        return build_app(
            WebApprovalUI(), sessions=self.sessions,
            mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token=self.MCP_TOKEN,
        )

    def test_mcp_rejects_the_approval_surfaces_own_session_id_as_bearer_auth(self):
        app = self._app()
        session_id = self.sessions.create()
        client = TestClient(app, base_url="http://localhost")
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                            headers={"Authorization": f"Bearer {session_id}"})
        assert resp.status_code == 401

    def test_mcp_accepts_only_its_own_token(self):
        # Unlike the 401 checks above (rejected in auth middleware, before
        # ever reaching the session manager), a real "initialize" needs the
        # session manager's task group running -- hence `with`, which is
        # what makes TestClient run the app's ASGI lifespan.
        with TestClient(self._app(), base_url="http://localhost") as client:
            resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                headers={"Authorization": f"Bearer {self.MCP_TOKEN}"})
        assert resp.status_code != 401

    def test_capability_routes_need_no_bearer_token_at_all(self):
        # ADR 0028: /mcp-files/slots/<token> and /mcp-files/fetch/<token>
        # are mounted alongside /mcp and /mcp-files/uploads|downloads, but
        # deliberately outside the bearer-auth stack -- a malformed/unknown
        # token is a 404 (the route exists, the token just isn't live),
        # never a 401 (which would mean the route demanded a credential
        # this caller has no way to present).
        client = TestClient(self._app(), base_url="http://localhost")
        put_resp = client.put("/mcp-files/slots/not-a-real-slot", content=b"data")
        assert put_resp.status_code == 404
        get_resp = client.get("/mcp-files/fetch/not-a-real-token")
        assert get_resp.status_code == 404
        # The bearer-authenticated siblings, by contrast, refuse outright
        # with no credential at all.
        assert client.put("/mcp-files/uploads/not-a-real-slot", content=b"data").status_code == 401
        assert client.get("/mcp-files/downloads/not-a-real-token").status_code == 401

    def test_approvals_decide_rejects_the_mcp_token_as_csrf(self):
        client = TestClient(self._app(), base_url="http://localhost")
        # A valid *session cookie* (a real local-mode session, not the raw
        # secret) but the *MCP* token presented as the CSRF value -- the
        # double-submit check must still fail, since these are different
        # secrets.
        _signed_in(client, self.sessions)
        resp = client.post(
            "/api/approvals/some-id/decide",
            json={"result": "deny", "csrf": self.MCP_TOKEN},
            headers={"Authorization": f"Bearer {self.MCP_TOKEN}"},
        )
        assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# /settings and /api/state/stream folded into the same combined app,
# sharing the approval surface's own session -- see build_app()'s own docstring for why
# this is the deliberate contrast with MCP's separate audience.
# --------------------------------------------------------------------------- #

def _controller(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from privacyfence import daemon_main, resource_names, settings_controller as sc, update_checker

    monkeypatch.setattr(resource_names, "_cache_file", lambda: tmp_path / "resource_name_cache.json")
    monkeypatch.setattr(update_checker, "_cache_file", lambda: tmp_path / "update_check_cache.json")
    monkeypatch.setattr(sc, "check_for_update", lambda **kw: None)
    monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})
    org_dir_path = tmp_path / "org"
    org_dir_path.mkdir()
    monkeypatch.setattr(sc, "org_dir", lambda: org_dir_path)
    data_dir_path = tmp_path / "data"
    data_dir_path.mkdir()
    monkeypatch.setattr(sc, "data_dir", lambda: data_dir_path)
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept: {}\nconnectors: {}\n", encoding="utf-8")
    connector_host = SimpleNamespace(set_connectors=lambda conns: None)
    return sc.SettingsController(str(config_path), connectors=[], connector_host=connector_host)


class TestSettingsFoldedIntoTheCombinedApp:
    def test_settings_page_shares_the_approval_surfaces_session(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions, controller=controller)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)

        r = client.get("/settings")

        assert r.status_code == 200
        assert "PrivacyFence — Settings" in r.text

    def test_no_controller_means_no_settings_route(self):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), sessions=sessions)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)

        r = client.get("/settings")

        assert r.status_code == 404

    # No 200-success request test against a real GET here -- the stream's
    # generator never terminates (same reasoning web/test_routes_approvals.
    # py's own TestApprovalsStream documents: TestClient's synchronous,
    # fully-buffering client.get() can't drive an endless SSE response
    # without hanging). Only the auth boundary -- which short-circuits
    # before the generator is ever entered -- is exercised here; the real
    # streaming behavior needs a real browser to observe.

    def test_state_stream_requires_auth(self):
        from privacyfence.web.state_stream import StateStream

        web_ui = WebApprovalUI()
        stream = StateStream(settings_snapshot=lambda: None, list_pending=web_ui.deferred_registry.list_pending)
        app = build_app(web_ui, state_stream=stream)
        client = TestClient(app, base_url="http://localhost")

        r = client.get("/api/state/stream")

        assert r.status_code == 401

    def test_web_server_provisions_the_state_stream_automatically(self):
        # Unlike build_app() (tested directly above), WebServer always
        # builds and wires a StateStream -- see its own __init__.
        server = WebServer(WebApprovalUI(), port=0)
        assert server.state_stream is not None


class TestStateStreamTouchesItsOwnSession:
    """``_state_stream_route`` must not authenticate only once, at connect
    time, and never touch the session again for the life of the
    connection -- a tab watching it past the idle timeout would be evicted
    anyway. Driving the route's generator directly (same reasoning
    TestSettingsFoldedIntoTheCombinedApp gives above for not going through
    a real GET) proves the ``touch`` callback threaded into
    ``stream.subscribe`` is bound to *this* request's own cookie, not a
    shared or wrong session id."""

    @staticmethod
    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    def _request(self, session_id: str):
        from starlette.requests import Request

        headers = [(b"cookie", f"{SESSION_COOKIE}={session_id}".encode())]
        scope = {
            "type": "http", "headers": headers, "method": "GET", "path": "/api/state/stream",
            "scheme": "http", "server": ("localhost", 8765),
        }
        return Request(scope, receive=self._receive)

    async def test_destroying_the_sessions_store_entry_ends_this_stream(self):
        from privacyfence.web.server import _state_stream_route
        from privacyfence.web.state_stream import StateStream

        sessions = LocalSessionStore()
        session_id = sessions.create()
        web_ui = WebApprovalUI()
        stream = StateStream(settings_snapshot=lambda: None, list_pending=web_ui.deferred_registry.list_pending)
        route = _state_stream_route(stream, sessions=sessions)

        response = await route.endpoint(self._request(session_id))
        assert response.status_code == 200

        agen = response.body_iterator
        await agen.__anext__()  # the initial full-state flush

        sessions.destroy(session_id)
        with pytest.raises(StopAsyncIteration):
            # The loop's own touch() call, on the very next tick, finds the
            # session gone and breaks -- exactly as a client disconnect
            # would, rather than streaming on to an evicted session.
            await agen.__anext__()

    async def test_a_different_sessions_eviction_leaves_this_stream_running(self):
        from privacyfence.web.server import _state_stream_route
        from privacyfence.web.state_stream import StateStream

        sessions = LocalSessionStore()
        session_id = sessions.create()
        other_id = sessions.create()
        web_ui = WebApprovalUI()
        stream = StateStream(settings_snapshot=lambda: None, list_pending=web_ui.deferred_registry.list_pending)
        route = _state_stream_route(stream, sessions=sessions)

        response = await route.endpoint(self._request(session_id))
        agen = response.body_iterator
        await agen.__anext__()

        sessions.destroy(other_id)
        stream.push_settings({"a": 1})
        # This connection's own session is still live, so its next tick's
        # touch() succeeds and the push comes through rather than the
        # generator breaking.
        chunk = await agen.__anext__()
        assert "settings" in chunk
        assert '{"a": 1}' in chunk


class TestWebServerWiresTheStateStream:
    def test_controller_is_registered_as_a_change_listener(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), port=0, controller=controller)

        assert server.state_stream is not None
        assert server.state_stream.push_settings in controller._change_listeners

    def test_controller_mutation_reaches_the_stream(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), port=0, controller=controller)
        pushed = []
        monkeypatch.setattr(server.state_stream, "_broadcast", lambda event, data: pushed.append((event, data)))

        # _push_snapshot -- not every mutating method, which already
        # returns its own snapshot directly to its own caller -- is what
        # notifies out-of-band listeners (a rule reloaded from the IPC
        # thread, an async op finishing on its own background thread; see
        # settings_controller.py's own docstring on _push_snapshot/
        # on_change). This proves the wiring reaches state_stream, the
        # same path any of those real triggers goes through.
        controller._push_snapshot()

        assert pushed and pushed[0][0] == "settings"

    def test_main_dispatcher_is_registered_for_headless_call_on_main(self, tmp_path, monkeypatch):
        from privacyfence import settings_controller as sc
        from privacyfence.web import state_stream as ss

        controller = _controller(tmp_path, monkeypatch)
        WebServer(WebApprovalUI(), port=0, controller=controller)

        recorded = []
        monkeypatch.setattr(ss, "_loop", None)  # no real loop running in this test
        sc.call_on_main(lambda x: recorded.append(x), "hi")
        assert recorded == ["hi"]  # falls back to running inline with no loop captured yet


class TestConfirmFirstPasskeyEnrollment:
    """the enrollment gate: local mode's own
    ``confirm_first_enrollment``, which web/routes_security.py calls when an
    enrollment has no already-enrolled credential to be gated on asserting
    with. Two behaviors live here and nowhere else -- that the ask goes to the
    companion, and that the one bypass is the developer escape hatch ADR 0003
    decision 7 already names, on a non-packaged build only.
    """

    def _server_module(self):
        from privacyfence.web import server as srv

        return srv

    def test_it_asks_the_companion(self, monkeypatch):
        from privacyfence import paths, privilege_separation

        srv = self._server_module()
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        monkeypatch.delenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, raising=False)
        monkeypatch.setattr(srv, "request_enrollment_confirmation", lambda: (True, ""))

        assert srv.confirm_first_passkey_enrollment() == (True, "")

    def test_a_refusal_from_the_companion_is_passed_through(self, monkeypatch):
        from privacyfence import paths, privilege_separation

        srv = self._server_module()
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.delenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, raising=False)
        monkeypatch.setattr(srv, "request_enrollment_confirmation", lambda: (False, "start the companion"))

        assert srv.confirm_first_passkey_enrollment() == (False, "start the companion")

    def test_a_refusal_on_a_source_checkout_also_names_the_developer_paths(self, monkeypatch):
        # A checkout autostarts no companion, so "start the companion" is not
        # the whole answer there -- and a packaged install must never be told
        # about a variable it does not honor (the test above).
        from privacyfence import paths, privilege_separation

        srv = self._server_module()
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        monkeypatch.delenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, raising=False)
        monkeypatch.setattr(srv, "request_enrollment_confirmation", lambda: (False, "no companion"))

        _confirmed, reason = srv.confirm_first_passkey_enrollment()

        assert reason.startswith("no companion")
        assert "privacyfence-companion --serve" in reason
        assert privilege_separation.DEV_ALLOW_UNSEPARATED_ENV in reason

    def test_the_dev_escape_hatch_skips_the_companion_on_a_non_packaged_build(self, monkeypatch):
        from privacyfence import paths, privilege_separation

        srv = self._server_module()
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        asked = []
        monkeypatch.setattr(
            srv, "request_enrollment_confirmation", lambda: asked.append(1) or (False, "no"),
        )

        assert srv.confirm_first_passkey_enrollment() == (True, "")
        assert asked == []

    def test_the_dev_escape_hatch_is_ignored_on_a_packaged_build(self, monkeypatch):
        # The whole reason is_bundled() is checked first: a packaged install
        # always has a companion autostarted for it (ADR 0003 decisions 3-5),
        # so honoring an environment variable there would hand a real shipped
        # product a way around its own gate.
        from privacyfence import paths, privilege_separation

        srv = self._server_module()
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setenv(privilege_separation.DEV_ALLOW_UNSEPARATED_ENV, "1")
        monkeypatch.setattr(srv, "request_enrollment_confirmation", lambda: (False, "companion said no"))

        assert srv.confirm_first_passkey_enrollment() == (False, "companion said no")


class TestLocalModeWiresTheFirstEnrollmentGate:
    def test_build_app_passes_the_companion_confirmation_to_security_routes(self, tmp_path, monkeypatch):
        """The wiring itself, since nothing else would notice it going
        missing: ``/security``'s own tests construct their routes directly
        (tests/unit/web/test_routes_security.py's ``_local_app``), so this is
        the only place that checks local mode's real ``build_app`` hands the
        gate over."""
        from privacyfence import paths
        from privacyfence.step_up_config import StepUpConfig
        from privacyfence.web import routes_security, server as srv

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        passed = {}
        real_build_routes = routes_security.build_routes

        def _spy(**kwargs):
            passed.update(kwargs)
            return real_build_routes(**kwargs)

        monkeypatch.setattr(routes_security, "build_routes", _spy)
        srv.build_app(
            WebApprovalUI(), sessions=LocalSessionStore(),
            step_up=StepUpConfig(rp_id="localhost", rp_name="PrivacyFence"),
        )

        assert passed["confirm_first_enrollment"] is srv.confirm_first_passkey_enrollment

    @pytest.mark.parametrize("separated", [True, False], ids=["separated", "unseparated"])
    def test_build_app_gates_recovery_on_a_human_session_only_when_separated(
        self, tmp_path, monkeypatch, separated,
    ):
        """The recovery-code route asks the human-session question on exactly
        the installs the approvals and settings routes do -- a
        privilege-separated one -- and not on an unseparated one, which has
        no companion to mint a human session at all. Driven through the real
        ``build_app`` so the wiring, not just the route, is what is tested."""
        from starlette.testclient import TestClient

        from privacyfence import paths, privilege_separation
        from privacyfence.step_up_config import StepUpConfig
        from privacyfence.web import server as srv
        from privacyfence.web.session_auth import PROVENANCE_HUMAN, SESSION_COOKIE

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: separated)
        sessions = LocalSessionStore()
        app = srv.build_app(
            WebApprovalUI(), sessions=sessions,
            step_up=StepUpConfig(rp_id="localhost", rp_name="PrivacyFence"),
            step_up_issuer_url="http://localhost",
        )
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)

        unattested = sessions.create()
        client.cookies.set(SESSION_COOKIE, unattested)
        r = client.post("/security/recover", json={"csrf": unattested, "code": "0000-0000-0000-0000"})
        if separated:
            assert r.status_code == 403
            assert r.json()["error"] == "human_session_required"
        else:
            # Past the gate: refused only because the code is wrong.
            assert r.status_code == 401

        human = sessions.create(provenance=PROVENANCE_HUMAN)
        client.cookies.set(SESSION_COOKIE, human)
        r = client.post("/security/recover", json={"csrf": human, "code": "0000-0000-0000-0000"})
        assert r.status_code == 401


class TestLocalEnrollmentState:
    """The first-enrollment offer's daemon-side answer: the one question the companion
    cannot answer for itself, because on a separated install the credential
    store is unreadable to the logged-in user."""

    def _server_module(self):
        from privacyfence.web import server as srv

        return srv

    def test_pending_when_a_passkey_is_required_and_none_is_enrolled(self, monkeypatch):
        from privacyfence.step_up_config import StepUpConfig

        srv = self._server_module()
        monkeypatch.setattr(srv.webauthn_stepup, "has_credentials", lambda principal: False)

        state = srv.local_enrollment_state(StepUpConfig(enabled=True, require_passkey=True))

        assert state == "pending"

    def test_ok_once_something_is_enrolled(self, monkeypatch):
        from privacyfence.step_up_config import StepUpConfig

        srv = self._server_module()
        monkeypatch.setattr(srv.webauthn_stepup, "has_credentials", lambda principal: True)

        assert srv.local_enrollment_state(StepUpConfig(enabled=True, require_passkey=True)) == "ok"

    def test_an_install_not_using_step_up_is_not_waiting_for_anything(self, monkeypatch):
        # "Requires" is enabled *and* require_passkey together, the same
        # pairing every other consumer treats as in force. An install with
        # step-up off is not waiting for an enrollment, and the companion
        # must not nag about one.
        from privacyfence.step_up_config import StepUpConfig

        srv = self._server_module()

        def _unexpected(principal):
            raise AssertionError("read the credential store for an install with step-up off")

        monkeypatch.setattr(srv.webauthn_stepup, "has_credentials", _unexpected)

        assert srv.local_enrollment_state(None) == "ok"
        assert srv.local_enrollment_state(StepUpConfig()) == "ok"
        assert srv.local_enrollment_state(StepUpConfig(enabled=True)) == "ok"
        assert srv.local_enrollment_state(StepUpConfig(require_passkey=True)) == "ok"


class TestLocalStatusPayload:
    """The daemon's own answer to the
    companion's ``STATUS`` command."""

    def _server_module(self):
        from privacyfence.web import server as srv

        return srv

    def test_reports_version_pid_mode_and_separation(self, monkeypatch):
        import json

        srv = self._server_module()
        monkeypatch.setattr(srv, "__version__", "4.2.0-test")
        monkeypatch.setattr(srv.os, "getpid", lambda: 4242)
        monkeypatch.setattr(srv.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(srv.routes_connect, "_is_connected", lambda principal, service: False)

        payload = json.loads(srv.local_status_payload("2026-09-23T00:00:00+00:00"))

        assert payload["version"] == "4.2.0-test"
        assert payload["pid"] == 4242
        assert payload["mode"] == "local"
        assert payload["separated"] is True
        assert payload["started_at"] == "2026-09-23T00:00:00+00:00"

    def test_connectors_reflect_is_connected(self, monkeypatch):
        import json

        srv = self._server_module()
        monkeypatch.setattr(srv.privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(
            srv.routes_connect, "_is_connected",
            lambda principal, service: service == "gmail",
        )

        payload = json.loads(srv.local_status_payload("2026-09-23T00:00:00+00:00"))

        assert payload["connectors"]["gmail"] == "ok"
        assert payload["connectors"]["drive"] == "needs_auth"
        assert set(payload["connectors"]) == set(srv.routes_connect.SERVICE_LABELS)

    def test_the_payload_is_one_compact_json_line(self, monkeypatch):
        # It travels over the same one-line-per-message control channel
        # every other command here does -- a stray newline in the payload
        # would truncate the reply.
        srv = self._server_module()
        monkeypatch.setattr(srv.routes_connect, "_is_connected", lambda principal, service: False)

        payload = srv.local_status_payload("2026-09-23T00:00:00+00:00")

        assert "\n" not in payload
        assert " " not in payload


class TestRecoveryCodeDelivery:
    """A credential-store reset token is never a value a process holding a
    ``pf_session`` can read out of an HTTP response."""

    def _server_module(self):
        from privacyfence.web import server as srv

        return srv

    def test_present_recovery_code_hands_it_to_the_companion(self, monkeypatch):
        srv = self._server_module()
        sent = []
        monkeypatch.setattr(srv, "send_recovery_code", lambda code: sent.append(code) or (True, ""))

        assert srv.present_recovery_code("A1B2-C3D4-E5F6-1789") == (True, "")
        assert sent == ["A1B2-C3D4-E5F6-1789"]

    def test_a_reissue_confirms_then_shows_then_stores(self, monkeypatch, tmp_path):
        from privacyfence import paths, webauthn_stepup
        from privacyfence.principal import LOCAL_PRINCIPAL

        srv = self._server_module()
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        order = []
        monkeypatch.setattr(
            srv, "request_recovery_confirmation", lambda: order.append("confirm") or (True, ""),
        )
        shown = []
        monkeypatch.setattr(
            srv, "send_recovery_code",
            lambda code: (order.append("show"), shown.append(code), (True, ""))[-1],
        )

        assert srv.reissue_local_recovery_code() == (True, "")
        assert order == ["confirm", "show"]
        assert webauthn_stepup.has_recovery_code(LOCAL_PRINCIPAL)
        # And it is the code that was shown, not some other one.
        assert webauthn_stepup.consume_recovery_code(LOCAL_PRINCIPAL, shown[0])

    def test_a_denied_confirmation_stores_nothing(self, monkeypatch, tmp_path):
        from privacyfence import paths, webauthn_stepup
        from privacyfence.principal import LOCAL_PRINCIPAL

        srv = self._server_module()
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(srv, "request_recovery_confirmation", lambda: (False, "denied"))

        def _unexpected(code):
            raise AssertionError("showed a code after the confirmation was denied")

        monkeypatch.setattr(srv, "send_recovery_code", _unexpected)

        assert srv.reissue_local_recovery_code() == (False, "denied")
        assert not webauthn_stepup.has_recovery_code(LOCAL_PRINCIPAL)

    def test_a_code_nobody_could_be_shown_is_never_stored(self, monkeypatch, tmp_path):
        # The whole reason minting and storing are two calls: a stored code
        # nobody has would make has_recovery_code() true forever, and no
        # later enrollment would ever issue another.
        from privacyfence import paths, webauthn_stepup
        from privacyfence.principal import LOCAL_PRINCIPAL

        srv = self._server_module()
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(srv, "request_recovery_confirmation", lambda: (True, ""))
        monkeypatch.setattr(srv, "send_recovery_code", lambda code: (False, "no display"))

        assert srv.reissue_local_recovery_code() == (False, "no display")
        assert not webauthn_stepup.has_recovery_code(LOCAL_PRINCIPAL)

    def test_build_app_wires_the_companion_delivery_on_a_packaged_build(self, tmp_path, monkeypatch):
        from privacyfence import paths
        from privacyfence.step_up_config import StepUpConfig
        from privacyfence.web import routes_security, server as srv

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        passed = {}
        real_build_routes = routes_security.build_routes

        def _spy(**kwargs):
            passed.update(kwargs)
            return real_build_routes(**kwargs)

        monkeypatch.setattr(routes_security, "build_routes", _spy)
        srv.build_app(
            WebApprovalUI(), sessions=LocalSessionStore(),
            step_up=StepUpConfig(rp_id="localhost", rp_name="PrivacyFence"),
        )

        assert passed["deliver_recovery_code"] is srv.present_recovery_code

    def test_a_source_checkout_keeps_the_pre_1_3_behavior(self, tmp_path, monkeypatch):
        # Nothing autostarts a companion for a checkout (ADR 0003 decisions
        # 3-5 are the packaged installers' half), so routing the code through
        # one there would mean a dev could never get a recovery code at all.
        from privacyfence import paths
        from privacyfence.step_up_config import StepUpConfig
        from privacyfence.web import routes_security, server as srv

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        passed = {}
        real_build_routes = routes_security.build_routes

        def _spy(**kwargs):
            passed.update(kwargs)
            return real_build_routes(**kwargs)

        monkeypatch.setattr(routes_security, "build_routes", _spy)
        srv.build_app(
            WebApprovalUI(), sessions=LocalSessionStore(),
            step_up=StepUpConfig(rp_id="localhost", rp_name="PrivacyFence"),
        )

        assert passed["deliver_recovery_code"] is None
