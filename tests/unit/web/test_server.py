"""Tests for web/server.py -- the Host allowlist and security-header
middleware, the WebServer lifecycle and the audience separation of ``/mcp``.
Covers the Host allowlist against DNS rebinding, CSP/X-Frame-Options,
Cache-Control -- the last one is per-route, tested in test_routes_approvals.py
instead.
"""
from __future__ import annotations

import sys

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from privacyfence import org_identity as oi
from privacyfence.principal import LOCAL_PRINCIPAL_ID, Principal, current_principal, principal_scope
from privacyfence.web import org_session
from privacyfence.web.csp import build_csp
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.org_session import OrgSessionStore
from privacyfence.web.server import (
    DEFAULT_PORT,
    OrgAuth,
    WebServer,
    _parse_host_header,
    _PrincipalScopeMiddleware,
    _SecurityHeadersMiddleware,
    build_app,
)
from privacyfence.web_approval_ui import WebApprovalUI

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com")


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _org_auth(tmp_path, monkeypatch) -> OrgAuth:
    monkeypatch.setattr("privacyfence.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"))
    provider = OrgOAuthProvider(_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    return OrgAuth(provider=provider, sessions=OrgSessionStore(), idp=_idp(), issuer_url=ISSUER)


def _signed_in(client: TestClient, org: OrgAuth) -> str:
    """Creates a real organization session directly and sets it as the cookie, for tests that only
    care about what happens after sign-in."""
    session_id = org.sessions.create(ALICE)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


def _client(tmp_path, monkeypatch, *, allowed_hosts=frozenset({"pf.example.com"}), signed_in=True, **kwargs):
    org = _org_auth(tmp_path, monkeypatch)
    app = build_app(WebApprovalUI(), org=org, allowed_hosts=allowed_hosts, **kwargs)
    client = TestClient(app, base_url=ISSUER, follow_redirects=False)
    if signed_in:
        _signed_in(client, org)
    return client, org


class TestHostAllowlist:
    def _client(self, tmp_path, monkeypatch, allowed_hosts=frozenset({"pf.example.com"})):
        return _client(tmp_path, monkeypatch, allowed_hosts=allowed_hosts)[0]

    def test_allowed_host_passes_through(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        assert r.status_code == 200

    def test_disallowed_host_is_rejected_before_reaching_any_route(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals", headers={"Host": "evil.example.com"})
        assert r.status_code == 400

    def test_port_suffix_on_the_host_header_is_ignored_for_matching(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals", headers={"Host": "pf.example.com:9999"})
        assert r.status_code == 200

    def test_ipv4_host_with_port_matches_the_bare_address(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch, allowed_hosts=frozenset({"127.0.0.1"}))
        r = client.get("/approvals", headers={"Host": "127.0.0.1:9999"})
        assert r.status_code == 200

    def test_ipv6_literal_host_matches_after_stripping_brackets(self, tmp_path, monkeypatch):
        # A naive split(":", 1)[0] returns "[" for this, which could never
        # be in any real allowlist.
        client = self._client(tmp_path, monkeypatch, allowed_hosts=frozenset({"::1"}))
        r = client.get("/approvals", headers={"Host": "[::1]"})
        assert r.status_code == 200

    def test_ipv6_literal_host_with_port_matches_after_stripping_brackets(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch, allowed_hosts=frozenset({"::1"}))
        r = client.get("/approvals", headers={"Host": "[::1]:9999"})
        assert r.status_code == 200

    def test_malformed_host_header_is_rejected(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals", headers={"Host": "[::1"})
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

    def test_build_app_wires_a_custom_principal_resolver_through_over_real_http(self, tmp_path, monkeypatch):
        # Exercises build_app()'s own principal_resolver parameter (not the
        # middleware in isolation, which the two tests above already cover)
        # against a plain, non-streaming route -- what's being checked is
        # only that build_app actually threads the parameter through to
        # _PrincipalScopeMiddleware, i.e. the resolver gets consulted at all.
        seen = []

        def resolver(request):
            seen.append(1)
            return Principal(id="alice")

        client, _org = _client(tmp_path, monkeypatch, principal_resolver=resolver)

        r = client.get("/approvals")

        assert r.status_code == 200
        assert seen  # the custom resolver was actually consulted


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

    @staticmethod
    def _directives(csp: str) -> dict[str, str]:
        return dict(part.strip().split(" ", 1) for part in csp.split(";") if part.strip())

    def test_local_mode_allows_no_form_submission(self):
        """ADR 0082: local mode renders no native form, so it keeps form-action 'none'."""
        directives = self._directives(build_csp("n"))
        assert directives["form-action"] == "'none'"
        assert directives["base-uri"] == "'none'"
        assert directives["frame-ancestors"] == "'none'"

    def test_org_mode_allows_form_posts_back_to_the_app_only(self):
        """ADR 0082: org /connect's native forms post to this origin, and every redirect they
        follow stays on it, so 'self' and nothing wider -- not the IdP, not a wildcard."""
        directives = self._directives(build_csp("n", app_origin="https://org.example.com"))
        assert directives["form-action"] == "'self'"
        assert directives["base-uri"] == "'none'"
        assert directives["frame-ancestors"] == "'none'"


# --------------------------------------------------------------------------- #
# WebServer owns a ControlChannelServer alongside the ASGI app
# in local mode -- test_control_channel.py covers the channel's own protocol
# and socket handling; this is just the wiring (built in __init__, started/
# stopped alongside the rest of WebServer's own lifecycle).
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# No sign-in link is ever written to a discovery file: handoff/ is
# group-shared with the logged-in user by design, so such a file would be a
# session for the taking by anything running as that user.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# The mcp_url discovery file mcpb/shim reads to find /mcp without any
# config the user has to edit. Only written/cleared
# when this WebServer actually has an mcp_dispatcher (i.e. web.mcp.enabled);
# a server started for the approval UI alone must not claim /mcp exists.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Audience separation (ADR 0061): the MCP bearer token and the approval
# surface's session cookie/CSRF token are different secrets, checked in
# different middleware, and neither is ever accepted on the other's routes.
# One test in this class is there to fail loudly if the middleware is ever
# reordered.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# /settings and /api/state/stream folded into the same combined app,
# sharing the approval surface's own session -- see build_app()'s own docstring for why
# this is the deliberate contrast with MCP's separate audience.
# --------------------------------------------------------------------------- #



def test_a_tokens_rules_come_from_its_own_rows(tmp_path, monkeypatch):
    """Each of a token's rules is drawn from the token's own row (ADR 0236), named like the
    Auto-accept page names a row; widening the person's row afterwards does not change it, and
    there is no "missing" or "limited_to" key any more."""
    from privacyfence import auto_accept, paths, scoped_tokens
    from privacyfence.policy import catalogue as policy_catalogue
    from privacyfence.policy.registry import Verb
    from privacyfence.web.server import scoped_token_page_data

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept: {}\n", encoding="utf-8")
    alice = Principal(id="alice", email="alice@example.com")
    store = scoped_tokens.ScopedTokenStore(lambda: [alice])
    scoped_tokens.set_store(store)
    policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)

    def page():
        return scoped_token_page_data(alice, policy=policy, connector_names={"drive"})

    with principal_scope(alice):
        auto_accept.init_config_path(str(config_path))
        auto_accept.add_policy_v2_rules(policy_catalogue.rules_for_catalogue_entry("drive.folder", ["F"], [Verb.READ]))
        (row,) = auto_accept.get_policy_rows()
        store.create(alice, label="sync", days=30, rules=[{"kind": "copy", "rule_id": row.id}], tools=[], policy=policy)
        before = page()

        auto_accept.add_policy_v2_rules(
            policy_catalogue.rules_for_catalogue_entry("drive.folder", ["F"], [Verb.DOWNLOAD])
        )
        after = page()

    (entry,) = before["tokens"][0]["rules"]
    assert entry == before["rules"][0]  # named the way the Auto-accept page names the row
    assert entry["id"] == row.id and entry["title"] and entry["sentence"]
    assert not {"missing", "limited_to"} & set(entry)
    assert "drive_download_file" in after["rules"][0]["tools"]
    assert after["tokens"][0]["rules"] == [entry]
    assert "drive_download_file" not in entry["tools"]


def _page_data(tmp_path, monkeypatch, connector_names):
    from privacyfence import auto_accept, paths, scoped_tokens
    from privacyfence.web.server import scoped_token_page_data

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept: {}\n", encoding="utf-8")
    alice = Principal(id="alice", email="alice@example.com")
    scoped_tokens.set_store(scoped_tokens.ScopedTokenStore(lambda: [alice]))
    policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)
    with principal_scope(alice):
        auto_accept.init_config_path(str(config_path))
        return scoped_token_page_data(alice, policy=policy, connector_names=connector_names)


def test_scope_groups_list_only_enabled_connectors(tmp_path, monkeypatch):
    data = _page_data(tmp_path, monkeypatch, {"drive"})

    ids = {g["id"] for g in data["scope_groups"]}
    assert "drive.folder" in ids
    assert not any(i.startswith("gmail.") for i in ids)
    assert all(g["connector"] == "drive" for g in data["scope_groups"])


def test_scope_groups_say_which_tools_each_verb_covers(tmp_path, monkeypatch):
    data = _page_data(tmp_path, monkeypatch, {"drive"})

    (folder,) = [g for g in data["scope_groups"] if g["id"] == "drive.folder"]
    assert "drive_get_file_content" in folder["verb_tools"]["read"]
    assert set(folder["verb_tools"]) == set(folder["verbs"])


def test_tools_are_grouped_under_the_longest_connector_name(tmp_path, monkeypatch):
    data = _page_data(tmp_path, monkeypatch, {"apps_script"})

    (group,) = data["tools"]
    assert group["connector"] == "apps_script"
    assert group["tools"] and all(t.startswith("apps_script_") for t in group["tools"])
    assert all(t.startswith("apps_script_") for t in data["auto_tools"])


def test_scoped_token_page_data_carries_each_tokens_activity(tmp_path, monkeypatch):
    from privacyfence import auto_accept, paths, scoped_tokens
    from privacyfence.web.server import scoped_token_page_data

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept: {}\n", encoding="utf-8")
    alice = Principal(id="alice", email="alice@example.com")
    store = scoped_tokens.ScopedTokenStore(lambda: [alice])
    scoped_tokens.set_store(store)
    policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)
    with principal_scope(alice):
        auto_accept.init_config_path(str(config_path))
    idle, _ = store.create(alice, label="idle", days=30, rules=[], tools=["drive_list_files"], policy=policy)
    busy, _ = store.create(alice, label="busy", days=30, rules=[], tools=["drive_list_files"], policy=policy)

    def page():
        with principal_scope(alice):
            data = scoped_token_page_data(alice, policy=policy, connector_names={"drive"})
        return {t["label"]: t for t in data["tokens"]}

    before = page()["idle"]
    assert (before["last_used_at"], before["last_denied_at"], before["last_denied_tool"]) == (None, None, "")

    store.note_use(busy, now=1000.0)
    store.note_denial(busy, "drive_download_file", now=2000.0)
    tokens = page()
    assert (tokens["busy"]["last_used_at"], tokens["busy"]["last_denied_at"], tokens["busy"]["last_denied_tool"]) == (
        1000.0, 2000.0, "drive_download_file",
    )
    assert (tokens["idle"]["last_used_at"], tokens["idle"]["last_denied_at"], tokens["idle"]["last_denied_tool"]) == (
        None, None, "",
    )


class TestSecurityHeaders:
    def _client(self, tmp_path, monkeypatch):
        return _client(tmp_path, monkeypatch)[0]

    def test_content_security_policy_is_present_and_locked_down(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "form-action 'self'" in csp  # /connect's native forms post back to this origin (ADR 0082)

    def test_frame_options_deny(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        assert r.headers.get("x-frame-options") == "DENY"

    def test_content_type_options_nosniff(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        assert r.headers.get("x-content-type-options") == "nosniff"

    def test_headers_are_present_even_on_an_error_response(self, tmp_path, monkeypatch):
        client, _org = _client(tmp_path, monkeypatch, signed_in=False)
        r = client.get("/approvals", headers={"Host": "evil.example.com"})  # rejected by the allowlist
        assert r.status_code == 400
        assert r.headers.get("x-frame-options") == "DENY"

    def test_permissions_policy_denies_unused_powerful_features(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        policy = r.headers.get("permissions-policy", "")
        assert "camera=()" in policy
        assert "microphone=()" in policy
        assert "geolocation=()" in policy

    def test_permissions_policy_leaves_webauthn_at_its_self_default(self, tmp_path, monkeypatch):
        # web/routes_security.py's step-up flow needs these from this same
        # origin -- see _PERMISSIONS_POLICY's own comment.
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        policy = r.headers.get("permissions-policy", "")
        assert "publickey-credentials-get=(self)" in policy
        assert "publickey-credentials-create=(self)" in policy

    def test_cross_origin_opener_policy_is_same_origin(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        assert r.headers.get("cross-origin-opener-policy") == "same-origin"

    def test_strict_transport_security_is_sent(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        assert r.headers.get("strict-transport-security") == "max-age=31536000; includeSubDomains"


class TestCacheControlOnSensitivePages:
    """A sweep across every page that carries session- or approval-specific content, rather than
    trusting that each route author remembered Cache-Control: no-store on their own -- a future
    new page that forgets it fails here instead of shipping silently cacheable."""

    def test_the_approvals_page_is_no_store(self, tmp_path, monkeypatch):
        client, _org = _client(tmp_path, monkeypatch)
        r = client.get("/approvals")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_the_settings_page_is_no_store(self, tmp_path, monkeypatch):
        client, _org = _client(tmp_path, monkeypatch)
        r = client.get("/settings")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_the_sign_in_redirect_is_no_store(self, tmp_path, monkeypatch):
        client, _org = _client(tmp_path, monkeypatch, signed_in=False)
        r = client.get("/approvals")
        assert r.status_code == 302
        assert r.headers.get("cache-control") == "no-store"


class TestCspNonce:
    """A fresh CSP nonce per response, matching the one in the document
    (ADR 0063)."""

    def _client(self, tmp_path, monkeypatch):
        return _client(tmp_path, monkeypatch)[0]

    def test_script_src_and_style_src_elem_carry_a_nonce_not_unsafe_inline(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        directives = dict(part.strip().split(" ", 1) for part in csp.split(";") if part.strip())
        assert "'nonce-" in directives["script-src"]
        assert "unsafe-inline" not in directives["script-src"]
        assert "'nonce-" in directives["style-src-elem"]
        assert "unsafe-inline" not in directives["style-src-elem"]
        # style-src-attr keeps 'unsafe-inline' deliberately -- see
        # web/csp.py's own module docstring for why.
        assert directives["style-src-attr"] == "'unsafe-inline'"

    def test_object_src_and_frame_src_allow_data_uris(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        directives = dict(part.strip().split(" ", 1) for part in csp.split(";") if part.strip())
        assert directives["object-src"] == "data:"
        assert directives["frame-src"] == "data:"

    def test_nonce_differs_across_separate_requests(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        first = client.get("/approvals").headers["content-security-policy"]
        second = client.get("/approvals").headers["content-security-policy"]
        assert first != second

    def test_body_style_and_script_tags_carry_the_response_own_nonce(self, tmp_path, monkeypatch):
        r = self._client(tmp_path, monkeypatch).get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        nonce = next(
            part.split("'nonce-", 1)[1].rstrip("'") for part in csp.split(";") if part.strip().startswith("script-src")
        )
        assert f'<style nonce="{nonce}">' in r.text
        assert f'<script nonce="{nonce}">' in r.text


class TestWebServerConstruction:
    def test_binds_to_localhost_by_default(self, tmp_path, monkeypatch):
        server = WebServer(WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), port=0)
        assert server.host == "localhost"

    def test_uses_the_default_port_constant(self, tmp_path, monkeypatch):
        server = WebServer(WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch))
        assert server.port == DEFAULT_PORT

    def test_base_url_is_the_issuer_url_not_the_bind_address(self, tmp_path, monkeypatch):
        server = WebServer(WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), host="0.0.0.0", port=1234)
        assert server.base_url == ISSUER

    def test_mcp_url_is_none_without_an_mcp_dispatcher(self, tmp_path, monkeypatch):
        server = WebServer(WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch))
        assert server.mcp_url is None

    def test_mcp_url_is_set_when_an_mcp_dispatcher_is_given(self, tmp_path, monkeypatch):
        server = WebServer(
            WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), mcp_dispatcher=McpDispatcher(lambda: {}),
        )
        assert server.mcp_url == f"{ISSUER}/mcp"

    def test_the_event_loop_is_never_exposed(self, tmp_path, monkeypatch):
        server = WebServer(WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch))
        assert server.wait_until_ready() is None

    def test_a_trusted_proxy_list_wraps_the_app(self, tmp_path, monkeypatch):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        server = WebServer(
            WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), trusted_proxies=("10.0.0.1",),
        )
        assert isinstance(server._server.config.app, ProxyHeadersMiddleware)


def test_org_tokens_load_after_a_restart_on_an_unseparated_install(tmp_path, monkeypatch):
    """An org server is not privilege-separated; its people's scoped tokens,
    under data_dir()/users/<id>/authority, must still load at startup
    (ADR 0165)."""
    from privacyfence import paths, scoped_tokens

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    alice = Principal(id="alice", email="alice@example.com")
    previous_run = scoped_tokens.ScopedTokenStore(lambda: [alice])
    token, secret = previous_run.create(
        alice, label="sales-sync", days=90, rules=[], tools=["gmail_list_messages"],
        policy=scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365),
    )
    assert (tmp_path / "users" / "alice" / "authority" / scoped_tokens.STORE_FILE_NAME).is_file()
    scoped_tokens.set_store(None)

    WebServer(
        WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), host="localhost", port=0,
        mcp_dispatcher=McpDispatcher(lambda: {}),
    )

    found = scoped_tokens.get_store().find(secret)
    assert found is not None and found.id == token.id and found.principal_id == "alice"


# The mcp_url discovery file is only written/cleared when this WebServer actually has an
# mcp_dispatcher; a server started for the approval UI alone must not claim /mcp exists.
class TestMcpUrlFile:
    def _server(self, tmp_path, monkeypatch, *, with_mcp: bool):
        kwargs = {"mcp_dispatcher": McpDispatcher(lambda: {})} if with_mcp else {}
        return WebServer(
            WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), host="localhost", port=0, **kwargs,
        )

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


class TestStop:
    @staticmethod
    def _free_port() -> int:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    def _wait_until_connectable(host: str, port: int, timeout: float = 5.0) -> None:
        import socket
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError(f"{host}:{port} never became connectable")

    def test_stop_ends_the_server_thread(self, tmp_path, monkeypatch):
        server = WebServer(WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), host="localhost", port=self._free_port())
        server.start()
        self._wait_until_connectable("localhost", server.port)
        assert not server.stopped
        server.stop()
        assert server.stopped

    def test_a_slow_stop_is_logged_with_the_open_connection_count(self, tmp_path, monkeypatch, caplog):
        import socket
        import time

        from privacyfence.web import server as server_module

        monkeypatch.setattr(server_module, "SHUTDOWN_GRACE_SECONDS", 0)
        server = WebServer(WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), host="localhost", port=self._free_port())
        server.start()
        self._wait_until_connectable("localhost", server.port)
        with socket.create_connection(("localhost", server.port)):
            deadline = time.monotonic() + 5
            while not server._server.server_state.connections and time.monotonic() < deadline:
                time.sleep(0.02)
            with caplog.at_level("WARNING", logger="privacyfence.web.server"):
                server.stop()

            assert "The web server took" in caplog.text
            assert "connection(s) were open when it was asked to" in caplog.text

    def test_a_server_never_started_counts_as_stopped(self, tmp_path, monkeypatch):
        assert WebServer(WebApprovalUI(), org=_org_auth(tmp_path, monkeypatch), host="localhost", port=0).stopped


# --------------------------------------------------------------------------- #
# Audience separation (ADR 0061): the MCP bearer token and the approval
# surface's session cookie/CSRF token are different secrets, checked in
# different middleware, and neither is ever accepted on the other's routes.
# --------------------------------------------------------------------------- #

class TestAudienceSeparation:
    INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}

    def _app(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(
            WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}),
            allowed_hosts=frozenset({"pf.example.com"}),
        )
        return app, org

    @staticmethod
    def _scoped_secret(tmp_path, monkeypatch, principal) -> str:
        from privacyfence import paths, scoped_tokens
        from privacyfence.web import mcp_auth

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        store = scoped_tokens.ScopedTokenStore(mcp_auth.scoped_token_principals)
        store.load()
        scoped_tokens.set_store(store)
        _, secret = store.create(
            principal, label="sales-sync", days=90, rules=[],
            tools=["gmail_list_messages"], policy=scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365),
        )
        return secret

    def test_mcp_rejects_the_approval_surfaces_own_session_id_as_bearer_auth(self, tmp_path, monkeypatch):
        app, org = self._app(tmp_path, monkeypatch)
        session_id = org.sessions.create(ALICE)
        client = TestClient(app, base_url=ISSUER)
        resp = client.post("/mcp", json=self.INITIALIZE, headers={"Authorization": f"Bearer {session_id}"})
        assert resp.status_code == 401

    def test_capability_routes_need_no_bearer_token_at_all(self, tmp_path, monkeypatch):
        # ADR 0028: /mcp-files/slots/<token> and /mcp-files/fetch/<token> are mounted alongside
        # /mcp but deliberately outside the bearer-auth stack -- a malformed/unknown token is a
        # 404 (the route exists, the token just isn't live), never a 401.
        app, _org = self._app(tmp_path, monkeypatch)
        client = TestClient(app, base_url=ISSUER)
        assert client.put("/mcp-files/slots/not-a-real-slot", content=b"data").status_code == 404
        assert client.get("/mcp-files/fetch/not-a-real-token").status_code == 404

    def test_a_scoped_token_is_refused_on_settings_and_approvals(self, tmp_path, monkeypatch):
        # ADR 0164: a scoped token is an /mcp credential only.
        secret = self._scoped_secret(tmp_path, monkeypatch, ALICE)
        app, org = self._app(tmp_path, monkeypatch)
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        bearer = {"Authorization": f"Bearer {secret}"}

        assert client.get("/settings", headers=bearer).status_code in (302, 401, 403)
        assert client.get("/api/approvals/some-id/preview", headers=bearer).status_code in (302, 401, 403)
        # Nor does it pass as the CSRF value next to a real session cookie.
        _signed_in(client, org)
        decide = client.post(
            "/api/approvals/some-id/decide", json={"result": "deny", "csrf": secret}, headers=bearer,
        )
        assert decide.status_code in (401, 403)

    def test_a_scoped_token_is_accepted_on_mcp(self, tmp_path, monkeypatch):
        from privacyfence import scoped_tokens

        secret = self._scoped_secret(tmp_path, monkeypatch, ALICE)
        app, _org = self._app(tmp_path, monkeypatch)
        with TestClient(app, base_url=ISSUER) as client:
            accepted = client.post("/mcp", json=self.INITIALIZE, headers={"Authorization": f"Bearer {secret}"})
            unknown = client.post(
                "/mcp", json=self.INITIALIZE, headers={"Authorization": f"Bearer {scoped_tokens.mint_secret()}"},
            )
        assert accepted.status_code != 401
        assert unknown.status_code == 401

    def test_approvals_decide_rejects_a_bearer_token_as_csrf(self, tmp_path, monkeypatch):
        app, org = self._app(tmp_path, monkeypatch)
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        # A valid *session cookie* but a bearer token presented as the CSRF value -- the
        # double-submit check must still fail, since these are different secrets.
        _signed_in(client, org)
        resp = client.post(
            "/api/approvals/some-id/decide",
            json={"result": "deny", "csrf": "mcp-token-0123456789"},
            headers={"Authorization": "Bearer mcp-token-0123456789"},
        )
        assert resp.status_code in (401, 403)


def test_a_row_that_compiles_to_no_rule_is_titled_by_its_scope_and_has_no_value_to_toggle():
    from privacyfence.policy import scope_rows
    from privacyfence.web.server import _scoped_rule_entry

    row = scope_rows.ScopeRow(
        id="r-empty", scope="drive.folder", value=["f"], verbs=frozenset(), operations=frozenset(),
    )
    entry = _scoped_rule_entry(row, "f", [])

    assert entry["title"] == "drive.folder"
    assert entry["title_parts"] is None
    assert entry["value_items"] == []


def test_scoped_rule_title_says_except_for_an_exclusion_scope():
    from privacyfence.policy import scope_rows
    from privacyfence.policy.registry import Verb
    from privacyfence.web.server import _scoped_rule_entry

    row = scope_rows.ScopeRow(
        id="r-mc", scope="slack.my_conversations", value=["C1"],
        verbs=frozenset({Verb.READ, Verb.SEARCH}), operations=frozenset({"slack.read_messages"}),
    )
    assert "my conversations except C1" in _scoped_rule_entry(row, "C1", ["C1"])["title"]
    no_value = scope_rows.ScopeRow(
        id="r-mc", scope="slack.my_conversations", value=None,
        verbs=frozenset({Verb.READ}), operations=frozenset({"slack.read_messages"}),
    )
    title = _scoped_rule_entry(no_value, "", [])["title"]
    assert "my conversations" in title and "except" not in title


def test_scoped_token_page_data_lists_upstreams_with_the_audiences_the_person_holds(tmp_path, monkeypatch):
    from privacyfence import auto_accept, paths, scoped_tokens, upstream_mcp_runtime
    from privacyfence.web.server import scoped_token_page_data

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    (tmp_path / "settings.yaml").write_text("auto_accept: {}\n", encoding="utf-8")
    alice = Principal(id="alice", email="alice@example.com", email_verified=True)
    store = scoped_tokens.ScopedTokenStore(lambda: [alice])
    scoped_tokens.set_store(store)
    policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)
    monkeypatch.setitem(auto_accept.TOOL_TO_GATE, "kh_search", "auto")
    monkeypatch.setattr(upstream_mcp_runtime, "configured", lambda: True)
    monkeypatch.setattr(
        upstream_mcp_runtime, "status",
        lambda: [{"id": "kh", "label": "Knowledge hub", "tools_exposed": 1, "state": "ready", "missing_tools": []}],
    )
    seen = []

    def held(principal, email_verified):
        seen.append((principal.id, email_verified))
        return {"kh": frozenset({"hr", "general"})}

    monkeypatch.setattr(upstream_mcp_runtime, "held_audiences", held)
    store.create(
        alice, label="kh", days=30, rules=[], tools=["kh_search"], policy=policy,
        upstream_audiences={"kh": ["hr"]}, held_audiences={"kh": frozenset({"hr"})},
    )
    with principal_scope(alice):
        auto_accept.init_config_path(str(tmp_path / "settings.yaml"))
        data = scoped_token_page_data(alice, policy=policy, connector_names={"drive", "kh"})

    assert data["upstreams"] == [
        {"id": "kh", "label": "Knowledge hub", "tools": ["kh_search"], "audiences": ["general", "hr"],
         "gated_tools": []},
    ]
    assert seen == [("alice", True)]
    assert data["tokens"][0]["upstream_audiences"] == {"kh": ["hr"]}


def test_scoped_token_page_data_has_no_upstreams_key_when_none_is_configured(tmp_path, monkeypatch):
    from privacyfence import auto_accept, paths, scoped_tokens
    from privacyfence.web.server import scoped_token_page_data

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    (tmp_path / "settings.yaml").write_text("auto_accept: {}\n", encoding="utf-8")
    alice = Principal(id="alice", email="alice@example.com")
    scoped_tokens.set_store(scoped_tokens.ScopedTokenStore(lambda: [alice]))
    policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)
    with principal_scope(alice):
        auto_accept.init_config_path(str(tmp_path / "settings.yaml"))
        data = scoped_token_page_data(alice, policy=policy, connector_names={"drive"})

    assert "upstreams" not in data


def test_scoped_token_page_data_carries_the_runtime_credential_of_a_delivered_token(tmp_path, monkeypatch):
    from privacyfence import auto_accept, paths, runtime_credentials, scoped_tokens
    from privacyfence.web.server import scoped_token_page_data

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept: {}\n", encoding="utf-8")
    alice = Principal(id="alice", email="alice@example.com")
    store = scoped_tokens.ScopedTokenStore(lambda: [alice])
    scoped_tokens.set_store(store)
    records = runtime_credentials.RuntimeCredentialStore(tmp_path / "records.json")
    runtime_credentials.set_store(records)
    policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)
    with principal_scope(alice):
        auto_accept.init_config_path(str(config_path))
    sent, _ = store.create(alice, label="sent", days=30, rules=[], tools=["drive_list_files"], policy=policy)
    plain, _ = store.create(alice, label="plain", days=30, rules=[], tools=["drive_list_files"], policy=policy)
    pending, _ = store.create(alice, label="pending", days=30, rules=[], tools=["drive_list_files"], policy=policy)

    def record(token, state, credential_id):
        return runtime_credentials.RuntimeCredential(
            principal_id="alice", token_id=token.id, proposal_id="stp-1", runtime="n8n",
            endpoint=("https", "n8n.example.com", 443), project_id="", credential_id=credential_id,
            credential_name=f"PrivacyFence {token.label}", workflow_id="wf1", mcp_url="", state=state,
            created_at=1.0, boot_id="b",
        )

    records.add(record(sent, runtime_credentials.DELIVERED, "0123456789abcdef"))
    records.add(record(pending, runtime_credentials.CREATING, "fedcba9876543210"))

    with principal_scope(alice):
        data = scoped_token_page_data(alice, policy=policy, connector_names={"drive"})
    tokens = {t["label"]: t for t in data["tokens"]}
    assert tokens["sent"]["runtime_credential"] == {
        "runtime": "n8n", "credential_name": "PrivacyFence sent", "workflow_id": "wf1",
    }
    assert tokens["plain"]["runtime_credential"] is None
    assert tokens["pending"]["runtime_credential"] is None


class TestScopedTokenPageData:
    """The New token form's gated upstream tools and the token list's approvals (ADR 0300)."""

    @staticmethod
    def _setup(tmp_path, monkeypatch, *, is_admin):
        from privacyfence import auto_accept, paths, scoped_tokens, upstream_mcp_registry
        from privacyfence import upstream_mcp_runtime
        from privacyfence.policy import scopes as policy_scopes
        from privacyfence.upstream_mcp_config import parse_upstreams

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        (tmp_path / "settings.yaml").write_text("auto_accept: {}\n", encoding="utf-8")
        upstream_mcp_registry.register(parse_upstreams({
            "version": 1,
            "upstreams": [{
                "id": "kh", "label": "Knowledge hub", "url": "https://kh.example/mcp", "audience": "kh-aud",
                "tools": {
                    "propose_change": {"gate": "popup", "verb": "update", "effect": "Proposes a change."},
                    "search": {"gate": "review", "verb": "search"},
                    "list_topics": {"gate": "auto", "verb": "read"},
                },
            }],
        }, source="test"))
        alice = Principal(id="alice", email="alice@example.com", email_verified=True, is_admin=is_admin)
        store = scoped_tokens.ScopedTokenStore(lambda: [alice])
        scoped_tokens.set_store(store)
        policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)
        monkeypatch.setattr(upstream_mcp_runtime, "configured", lambda: True)
        monkeypatch.setattr(upstream_mcp_runtime, "status", lambda: [
            {"id": "kh", "label": "Knowledge hub", "tools_exposed": 3, "state": "ready", "missing_tools": []},
        ])
        monkeypatch.setattr(upstream_mcp_runtime, "held_audiences", lambda p, v: {"kh": frozenset({"hr"})})
        return alice, store, policy, policy_scopes, auto_accept

    @staticmethod
    def _page(alice, policy, auto_accept, tmp_path):
        from privacyfence.web.server import scoped_token_page_data

        with principal_scope(alice):
            auto_accept.init_config_path(str(tmp_path / "settings.yaml"))
            return scoped_token_page_data(alice, policy=policy, connector_names={"drive", "kh"})

    def test_an_administrator_page_lists_gated_tools_with_effect_and_digest(self, tmp_path, monkeypatch):
        alice, _store, policy, scopes, auto_accept = self._setup(tmp_path, monkeypatch, is_admin=True)
        data = self._page(alice, policy, auto_accept, tmp_path)
        (upstream,) = data["upstreams"]
        assert upstream["gated_tools"] == [
            {"name": "kh_propose_change", "gate": "popup", "verb": "update", "effect": "Proposes a change.",
             "digest": scopes.UPSTREAM_DECLARATIONS["kh.propose_change"]},
            {"name": "kh_search", "gate": "review", "verb": "search",
             "effect": "The result goes to the token without a person seeing it. Personal data in it still blocks the call.",
             "digest": scopes.UPSTREAM_DECLARATIONS["kh.search"]},
        ]

    def test_a_non_administrator_page_lists_no_gated_tool(self, tmp_path, monkeypatch):
        alice, _store, policy, _scopes, auto_accept = self._setup(tmp_path, monkeypatch, is_admin=False)
        data = self._page(alice, policy, auto_accept, tmp_path)
        assert data["upstreams"][0]["gated_tools"] == []

    def test_gated_tools_are_not_in_tools_or_auto_tools(self, tmp_path, monkeypatch):
        alice, _store, policy, _scopes, auto_accept = self._setup(tmp_path, monkeypatch, is_admin=True)
        data = self._page(alice, policy, auto_accept, tmp_path)
        listed = {name for group in data["tools"] for name in group["tools"]}
        assert "kh_list_topics" in listed and "kh_list_topics" in data["auto_tools"]
        assert not {"kh_propose_change", "kh_search"} & (listed | set(data["auto_tools"]))
        assert data["upstreams"][0]["tools"] == ["kh_list_topics"]

    def test_a_tool_whose_operation_belongs_to_another_upstream_is_not_offered(self, tmp_path, monkeypatch):
        alice, _store, policy, scopes, auto_accept = self._setup(tmp_path, monkeypatch, is_admin=True)
        # Named with this upstream's prefix, but declared for another operation: never offered for kh.
        monkeypatch.setitem(auto_accept.TOOL_TO_GATE, "kh_shadow", "popup")
        monkeypatch.setitem(auto_accept.TOOL_TO_OPERATION, "kh_shadow", "wiki.shadow")
        monkeypatch.setitem(scopes.UPSTREAM_DECLARATIONS, "wiki.shadow", "d")
        data = self._page(alice, policy, auto_accept, tmp_path)
        assert [t["name"] for t in data["upstreams"][0]["gated_tools"]] == ["kh_propose_change", "kh_search"]

    def test_an_approval_of_a_tool_no_longer_declared_lists_nothing(self, tmp_path, monkeypatch):
        from privacyfence import upstream_mcp_registry

        alice, store, policy, scopes, auto_accept = self._setup(tmp_path, monkeypatch, is_admin=True)
        store.create(
            alice, label="kh", days=30, rules=[], tools=["kh_propose_change"], policy=policy, is_admin=True,
            upstream_approvals=[
                {"tool": "kh_propose_change", "digest": scopes.UPSTREAM_DECLARATIONS["kh.propose_change"]},
            ],
        )
        upstream_mcp_registry.unregister_all()
        (token,) = self._page(alice, policy, auto_accept, tmp_path)["tokens"]
        assert token["rules"] == []
        assert token["approved_upstream_tools"] == []

    def test_approval_rows_are_left_out_of_a_tokens_rules(self, tmp_path, monkeypatch):
        alice, store, policy, scopes, auto_accept = self._setup(tmp_path, monkeypatch, is_admin=True)
        store.create(
            alice, label="kh", days=30, rules=[], tools=["kh_propose_change", "kh_search"], policy=policy,
            is_admin=True,
            upstream_approvals=[
                {"tool": "kh_propose_change", "digest": scopes.UPSTREAM_DECLARATIONS["kh.propose_change"]},
                {"tool": "kh_search", "digest": scopes.UPSTREAM_DECLARATIONS["kh.search"]},
            ],
        )
        (token,) = self._page(alice, policy, auto_accept, tmp_path)["tokens"]
        assert token["rules"] == []
        assert token["approved_upstream_tools"] == [
            {"name": "kh_propose_change", "gate": "popup", "effect": "Proposes a change."},
            {"name": "kh_search", "gate": "review",
             "effect": "The result goes to the token without a person seeing it. Personal data in it still blocks the call."},
        ]
