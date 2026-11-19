"""web/routes_upstream_oauth.py: the Connect flow of hosted upstream MCP servers (ADR 0329, ADR 0178)."""
from __future__ import annotations

import json
import logging
import stat
import urllib.parse as up
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import paths, upstream_mcp_runtime as runtime
from privacyfence import upstream_oauth_discovery as disc
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.principal import Principal
from privacyfence.upstream_mcp_config import UpstreamOAuthSettings, UpstreamServer
from privacyfence.upstream_oauth_tokens import OAuthClient, UpstreamOAuthError, load_token, token_path
from privacyfence.upstream_tools import ToolStore
from privacyfence.web import org_session
from privacyfence.web import routes_upstream_oauth as ruo

pytestmark = pytest.mark.unit

ISSUER = "https://pf.example.com"
MCP = "https://mcp.canva.com/mcp"
AS = "https://mcp.canva.com"
CALLBACK = f"{ISSUER}/upstream-mcp/canva/callback"
SENTINELS = ("ACCESS-SENTINEL", "REFRESH-SENTINEL", "SECRET-SENTINEL", "CODE-SENTINEL")


def _upstream(upstream_id="canva", label="Canva", url=MCP, **oauth: Any) -> UpstreamServer:
    oauth.setdefault("client_id", "cid")
    return UpstreamServer(
        id=upstream_id, label=label, url=url, oauth=UpstreamOAuthSettings(scopes=("design:read",), **oauth),
    )


class Servers:
    """The upstream and its authorization server, served by a MockTransport."""

    def __init__(self) -> None:
        self.iss_supported = True
        self.token_status = 200
        self.requests: list[httpx2.Request] = []

    def transport(self) -> httpx2.MockTransport:
        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            url = str(request.url)
            if request.method == "POST" and url == MCP:
                return httpx2.Response(401, json={}, headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{AS}/.well-known/oauth-protected-resource/mcp"',
                })
            if url == f"{AS}/.well-known/oauth-protected-resource/mcp":
                return httpx2.Response(200, json={"resource": MCP, "authorization_servers": [AS]})
            if url == f"{AS}/.well-known/oauth-authorization-server":
                return httpx2.Response(200, json={
                    "issuer": AS, "authorization_endpoint": f"{AS}/authorize", "token_endpoint": f"{AS}/token",
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "authorization_response_iss_parameter_supported": self.iss_supported,
                })
            if request.method == "POST" and url == f"{AS}/token":
                if self.token_status != 200:
                    return httpx2.Response(self.token_status, json={"error": "SECRET-SENTINEL"})
                return httpx2.Response(200, json={
                    "access_token": "ACCESS-SENTINEL", "token_type": "Bearer", "refresh_token": "REFRESH-SENTINEL",
                    "expires_in": 3600, "scope": "design:read",
                })
            return httpx2.Response(404, json={})

        return httpx2.MockTransport(handler)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(disc, "_resolve", resolve)
    yield
    runtime.reset()


class Registry(ConnectorRegistry):
    def __init__(self) -> None:
        super().__init__(factory=lambda principal: [])
        self.evicted: list[str] = []

    def evict(self, principal_id: str) -> None:
        self.evicted.append(principal_id)
        super().evict(principal_id)


class Env:
    def __init__(self, tmp_path, servers: Servers, *upstreams: UpstreamServer) -> None:
        runtime.configure(
            upstreams=upstreams or (_upstream(),), tool_store=ToolStore(tmp_path / "org" / "upstream_tools"),
            registrations_dir=tmp_path / "org" / "upstream_oauth", transport=servers.transport(),
        )
        self.servers = servers
        self.sessions = org_session.OrgSessionStore()
        self.registry = Registry()
        routes = ruo.build_routes(
            sessions=self.sessions, connector_registry=self.registry, issuer_url=ISSUER + "/",
            transport=servers.transport(),
        )
        self.client = TestClient(Starlette(routes=routes), base_url=ISSUER, follow_redirects=False)

    def sign_in(self, principal_id: str = "alice") -> tuple[dict[str, str], Principal]:
        principal = Principal(id=principal_id, email=f"{principal_id}@example.com")
        return {org_session.SESSION_COOKIE: self.sessions.create(principal)}, principal

    def start(self, cookies, upstream_id="canva") -> str:
        response = self.client.get(f"/upstream-mcp/{upstream_id}/connect", cookies=cookies)
        assert response.status_code == 302, response.text
        return dict(up.parse_qsl(up.urlparse(response.headers["location"]).query))["state"]

    def callback(self, cookies, query: dict[str, str], upstream_id="canva"):
        return self.client.get(
            f"/upstream-mcp/{upstream_id}/callback?{up.urlencode(query)}", cookies=cookies,
        )


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path, Servers())


def _saved(principal: Principal):
    return load_token(token_path(principal, "canva"))


# ---------------------------------------------------------------------------- #
# The pending store
# ---------------------------------------------------------------------------- #


def _pending(n: int = 0) -> ruo.PendingUpstreamAuth:
    return ruo.PendingUpstreamAuth(
        principal_id=f"p{n}", upstream_id="canva", code_verifier="v", redirect_uri=CALLBACK, issuer=AS,
        client=OAuthClient(client_id="cid"), token_endpoint=f"{AS}/token", resource=MCP, iss_required=False,
    )


class TestPendingStore:
    def test_a_state_is_single_use(self):
        store = ruo.PendingUpstreamAuthStore()
        store.put("s", _pending())
        assert store.pop("s") == _pending()
        assert store.pop("s") is None

    def test_an_unknown_state_is_none(self):
        assert ruo.PendingUpstreamAuthStore().pop("nope") is None

    def test_a_state_expires_after_the_ttl(self):
        now = [1000.0]
        store = ruo.PendingUpstreamAuthStore(ttl_seconds=600, clock=lambda: now[0])
        store.put("fresh", _pending())
        store.put("old", _pending())
        now[0] += 600
        assert store.pop("fresh") == _pending()
        now[0] += 1
        assert store.pop("old") is None

    def test_the_oldest_entry_is_dropped_beyond_max_entries(self):
        store = ruo.PendingUpstreamAuthStore(max_entries=2)
        for n in range(3):
            store.put(f"s{n}", _pending(n))
        assert store.pop("s0") is None
        assert store.pop("s1") == _pending(1)
        assert store.pop("s2") == _pending(2)

    def test_putting_a_state_again_keeps_one_entry(self):
        store = ruo.PendingUpstreamAuthStore(max_entries=2)
        store.put("a", _pending(1))
        store.put("a", _pending(2))
        store.put("b", _pending(3))
        assert store.pop("a") == _pending(2)


# ---------------------------------------------------------------------------- #
# Connect
# ---------------------------------------------------------------------------- #


class TestConnect:
    def test_without_a_session_it_redirects_to_login(self, env):
        r = env.client.get("/upstream-mcp/canva/connect")
        assert (r.status_code, r.headers["location"]) == (302, "/login?next=/connect")

    def test_an_unknown_id_is_404(self, env):
        cookies, _ = env.sign_in()
        r = env.client.get("/upstream-mcp/nope/connect", cookies=cookies)
        assert (r.status_code, r.text) == (404, "Not found")

    def test_it_redirects_to_the_authorization_endpoint(self, env):
        cookies, _ = env.sign_in()
        r = env.client.get("/upstream-mcp/canva/connect", cookies=cookies)
        assert r.status_code == 302
        assert r.headers["cache-control"] == "no-store"
        location = up.urlparse(r.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == f"{AS}/authorize"
        query = dict(up.parse_qsl(location.query))
        assert query["response_type"] == "code"
        assert query["client_id"] == "cid"
        assert query["redirect_uri"] == CALLBACK
        assert query["code_challenge_method"] == "S256"
        assert len(query["code_challenge"]) >= 43
        assert query["scope"] == "design:read"
        assert query["resource"] == MCP
        assert len(query["state"]) >= 32

    def test_a_discovery_failure_redirects_to_the_error_flash(self, env, caplog):
        bad = _upstream(url="https://mcp.canva.com/other")
        env2 = Env(paths.data_dir(), Servers(), bad)
        cookies, _ = env2.sign_in()
        with caplog.at_level(logging.WARNING):
            r = env2.client.get("/upstream-mcp/canva/connect", cookies=cookies)
        assert (r.status_code, r.headers["location"]) == (302, "/connect?upstream_error=canva")
        assert r.headers["cache-control"] == "no-store"
        assert "Upstream canva connect failed: invalid_metadata" in _ours(caplog)


# ---------------------------------------------------------------------------- #
# Callback
# ---------------------------------------------------------------------------- #

ERROR = "/connect?upstream_error=canva"


def _ours(caplog) -> str:
    """What PrivacyFence logged (the test client's own request log carries the callback URL)."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("privacyfence"))


def _no_secrets(caplog, *extra: str) -> None:
    for needle in (*SENTINELS, *extra):
        assert needle not in _ours(caplog)


class TestCallbackSuccess:
    def test_the_token_is_saved_and_the_registry_evicted(self, env, caplog):
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        with caplog.at_level(logging.DEBUG):
            r = env.callback(cookies, {"code": "CODE-SENTINEL", "state": state, "iss": AS})
        assert (r.status_code, r.headers["location"]) == (302, "/connect?upstream_connected=canva")
        assert r.headers["cache-control"] == "no-store"
        path = token_path(alice, "canva")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        data = json.loads(path.read_text())
        assert data["access_token"] == "ACCESS-SENTINEL"
        assert data["refresh_token"] == "REFRESH-SENTINEL"
        assert data["issuer"] == AS
        assert data["client_id"] == "cid"
        assert data["token_endpoint"] == f"{AS}/token"
        assert data["resource"] == MCP
        assert data["scope"] == "design:read"
        assert env.registry.evicted == ["alice"]
        _no_secrets(caplog, state)

    def test_the_exchange_sends_the_verifier_and_the_redirect_uri(self, env):
        cookies, _ = env.sign_in()
        state = env.start(cookies)
        env.callback(cookies, {"code": "CODE-SENTINEL", "state": state, "iss": AS})
        exchange = [r for r in env.servers.requests if str(r.url) == f"{AS}/token"]
        assert exchange
        form = dict(up.parse_qsl(exchange[-1].content.decode()))
        assert form["grant_type"] == "authorization_code"
        assert form["redirect_uri"] == CALLBACK
        assert form["code"] == "CODE-SENTINEL"
        assert form["code_verifier"]

    def test_a_server_without_iss_support_may_omit_iss(self, tmp_path):
        servers = Servers()
        servers.iss_supported = False
        env = Env(tmp_path, servers)
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        r = env.callback(cookies, {"code": "c", "state": state})
        assert r.headers["location"] == "/connect?upstream_connected=canva"
        assert _saved(alice) is not None


class TestCallbackRefusals:
    def test_a_missing_state_is_400(self, env):
        cookies, alice = env.sign_in()
        r = env.callback(cookies, {"code": "c"})
        assert (r.status_code, r.text) == (400, "Invalid or expired sign-in attempt")
        assert _saved(alice) is None

    def test_an_unknown_state_is_400(self, env):
        cookies, _ = env.sign_in()
        r = env.callback(cookies, {"code": "c", "state": "nope"})
        assert r.status_code == 400

    def test_another_principals_session_saves_nothing(self, env, caplog):
        cookies, alice = env.sign_in("alice")
        bob_cookies, bob = env.sign_in("bob")
        state = env.start(cookies)
        with caplog.at_level(logging.DEBUG):
            r = env.callback(bob_cookies, {"code": "CODE-SENTINEL", "state": state, "iss": AS})
        assert (r.status_code, r.headers["location"]) == (302, ERROR)
        assert _saved(alice) is None and _saved(bob) is None
        assert env.registry.evicted == []
        _no_secrets(caplog, state)

    def test_no_session_saves_nothing(self, env, caplog):
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        with caplog.at_level(logging.DEBUG):
            r = env.callback({}, {"code": "CODE-SENTINEL", "state": state, "iss": AS})
        assert (r.status_code, r.headers["location"]) == (302, ERROR)
        assert _saved(alice) is None
        _no_secrets(caplog, state)

    def test_an_expired_state_is_400(self, tmp_path, monkeypatch):
        now = [1000.0]
        store_class = ruo.PendingUpstreamAuthStore
        monkeypatch.setattr(ruo, "PendingUpstreamAuthStore", lambda: store_class(clock=lambda: now[0]))
        env = Env(tmp_path, Servers())
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        now[0] += 601
        r = env.callback(cookies, {"code": "c", "state": state, "iss": AS})
        assert r.status_code == 400
        assert _saved(alice) is None

    def test_a_reused_state_is_400(self, env):
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        assert env.callback(cookies, {"code": "c", "state": state, "iss": AS}).status_code == 302
        r = env.callback(cookies, {"code": "c", "state": state, "iss": AS})
        assert r.status_code == 400

    def test_a_state_of_another_upstream_is_400(self, tmp_path):
        env = Env(tmp_path, Servers(), _upstream(), _upstream("other", "Other", MCP))
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        r = env.callback(cookies, {"code": "c", "state": state, "iss": AS}, upstream_id="other")
        assert r.status_code == 400
        assert _saved(alice) is None

    def test_a_provider_error_redirects_and_logs_only_its_code(self, env, caplog):
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        with caplog.at_level(logging.DEBUG):
            r = env.callback(cookies, {
                "error": "access_denied<script>" + "x" * 80, "error_description": "SECRET-SENTINEL",
                "state": state, "iss": AS,
            })
        assert (r.status_code, r.headers["location"]) == (302, ERROR)
        assert _saved(alice) is None
        assert "access_deniedscript" in _ours(caplog)
        assert "<script>" not in _ours(caplog)
        assert "x" * 41 not in _ours(caplog)
        _no_secrets(caplog, state)

    def test_a_missing_code_redirects(self, env, caplog):
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        with caplog.at_level(logging.DEBUG):
            r = env.callback(cookies, {"state": state, "iss": AS})
        assert (r.status_code, r.headers["location"]) == (302, ERROR)
        assert _saved(alice) is None
        _no_secrets(caplog, state)

    def test_a_mismatching_iss_redirects(self, env, caplog):
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        with caplog.at_level(logging.DEBUG):
            r = env.callback(cookies, {"code": "CODE-SENTINEL", "state": state, "iss": "https://evil.example.com"})
        assert (r.status_code, r.headers["location"]) == (302, ERROR)
        assert _saved(alice) is None
        assert not [r for r in env.servers.requests if str(r.url) == f"{AS}/token"]
        _no_secrets(caplog, state, "evil.example.com")

    def test_a_missing_iss_redirects_when_the_server_supports_it(self, env, caplog):
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        with caplog.at_level(logging.DEBUG):
            r = env.callback(cookies, {"code": "CODE-SENTINEL", "state": state})
        assert (r.status_code, r.headers["location"]) == (302, ERROR)
        assert _saved(alice) is None
        _no_secrets(caplog, state)

    @pytest.mark.parametrize("status", [400, 401, 500])
    def test_a_failing_exchange_saves_nothing(self, env, caplog, status):
        env.servers.token_status = status
        cookies, alice = env.sign_in()
        state = env.start(cookies)
        with caplog.at_level(logging.DEBUG):
            r = env.callback(cookies, {"code": "CODE-SENTINEL", "state": state, "iss": AS})
        assert (r.status_code, r.headers["location"]) == (302, ERROR)
        assert _saved(alice) is None
        assert env.registry.evicted == []
        assert "code exchange failed" in _ours(caplog)
        _no_secrets(caplog, state)


# ---------------------------------------------------------------------------- #
# The Connect-page rows
# ---------------------------------------------------------------------------- #


class TestConnectSection:
    def test_nothing_when_no_upstream_is_configured(self):
        assert ruo.connect_section_html(Principal(id="alice"), "canva", "canva") == ("", "")

    def test_connect_for_a_person_with_no_token(self, env):
        flash, rows = ruo.connect_section_html(Principal(id="alice"), "", "")
        assert flash == ""
        assert 'class="pf-service-chip">Canva<' in rows
        assert "Not connected" in rows
        assert 'href="/upstream-mcp/canva/connect">Connect</a>' in rows

    def test_reconnect_with_the_badge_for_a_person_with_a_token(self, env):
        cookies, alice = env.sign_in()
        env.callback(cookies, {"code": "c", "state": env.start(cookies), "iss": AS})
        _, rows = ruo.connect_section_html(alice, "", "")
        assert "Connected</span>" in rows and "Not connected" not in rows
        assert 'href="/upstream-mcp/canva/connect">Reconnect</a>' in rows

    def test_a_label_is_escaped(self, tmp_path):
        Env(tmp_path, Servers(), _upstream(label="<b>x</b>"))
        flash, rows = ruo.connect_section_html(Principal(id="alice"), "canva", "canva")
        assert "<b>x</b>" not in rows + flash
        assert "&lt;b&gt;x&lt;/b&gt;" in rows
        assert "&lt;b&gt;x&lt;/b&gt; connected." in flash
        assert "Could not connect &lt;b&gt;x&lt;/b&gt; -- sign-in was declined, or the server could not be reached." in flash

    def test_flash_texts(self, env):
        flash, _ = ruo.connect_section_html(Principal(id="alice"), "canva", "")
        assert flash == '<div class="card card-success" role="status">Canva connected.</div>'
        flash, _ = ruo.connect_section_html(Principal(id="alice"), "", "canva")
        assert 'role="alert">Could not connect Canva -- sign-in was declined, or the server could not be reached.' in flash

    def test_an_undeclared_flash_value_is_ignored(self, env):
        assert ruo.connect_section_html(Principal(id="alice"), "nope", "nope")[0] == ""


def test_the_error_kind_of_a_failed_connect_is_not_echoed(env, monkeypatch, caplog):
    async def boom(*a, **k):
        raise UpstreamOAuthError("registration_failed")

    monkeypatch.setattr(ruo, "discover", boom)
    cookies, _ = env.sign_in()
    with caplog.at_level(logging.DEBUG):
        r = env.client.get("/upstream-mcp/canva/connect", cookies=cookies)
    assert r.headers["location"] == ERROR
    assert "registration_failed" in _ours(caplog)

