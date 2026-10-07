"""web/routes_plugins.py, mounted the way the daemon mounts it: through build_app, behind the
owner-only wrapper, the human-session check and the security-header middleware."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from privacyfence.plugins import pages
from privacyfence.plugins.protocol import RpcError
from privacyfence.principal import LOCAL_PRINCIPAL_ID
from privacyfence.web.server import build_app
from privacyfence.web.session_auth import (
    PROVENANCE_HUMAN,
    PROVENANCE_UNATTESTED,
    SESSION_COOKIE,
    LocalSessionStore,
)
from privacyfence.web_approval_ui import WebApprovalUI

pytestmark = pytest.mark.unit

SANDBOX_HEADERS = {
    "content-security-policy": pages.CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "private, no-store",
    "x-frame-options": "DENY",
}


class FakeHost:
    def __init__(self, *, result: dict | None = None, error: Exception | None = None, running=("today",)):
        self.result = result or {
            "status": 200, "headers": {"content-type": "text/html; charset=utf-8", "set-cookie": "a=b"},
            "body": "<p>Today</p>", "body_encoding": "utf8",
        }
        self.error = error
        self.running = set(running)
        self.calls: list[tuple] = []

    async def web_request(self, name, path, query, principal):
        self.calls.append((name, path, query, principal.id))
        if name not in self.running:
            raise LookupError(f"Plugin {name} is not serving pages.")
        if self.error is not None:
            raise self.error
        return self.result


def _client(host: FakeHost, *, provenance: str | None = PROVENANCE_HUMAN, principal_id: str = LOCAL_PRINCIPAL_ID):
    sessions = LocalSessionStore()
    app = build_app(WebApprovalUI(), sessions=sessions, plugin_host=host)
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    if provenance is not None:
        client.cookies.set(SESSION_COOKIE, sessions.create(provenance=provenance, principal_id=principal_id))
    return client


def _assert_sandbox_headers(response) -> None:
    for name, value in SANDBOX_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


class TestRoutes:
    def test_get_served(self):
        host = FakeHost()
        r = _client(host).get("/plugins/today/notes/a%20b?x=1&x=2&y=")
        assert r.status_code == 200
        assert r.text == "<p>Today</p>"
        assert r.headers["content-type"] == "text/html; charset=utf-8"
        assert "set-cookie" not in r.headers
        assert host.calls == [("today", "/notes/a b", {"x": "2", "y": ""}, LOCAL_PRINCIPAL_ID)]
        _assert_sandbox_headers(r)

    def test_root_path_is_slash(self):
        host = FakeHost()
        assert _client(host).get("/plugins/today/").status_code == 200
        assert host.calls[0][1] == "/"

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "PROPFIND"])
    def test_post_returns_405_without_calling_plugin(self, method):
        host = FakeHost()
        r = _client(host).request(method, "/plugins/today/")
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD"
        assert host.calls == []
        _assert_sandbox_headers(r)

    def test_post_to_the_bare_name_is_405_too(self):
        host = FakeHost()
        r = _client(host).post("/plugins/today")
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD"
        assert host.calls == []

    def test_bearer_not_served(self):
        host = FakeHost()
        client = _client(host, provenance=None)
        r = client.get("/plugins/today/", headers={"Authorization": "Bearer some-mcp-token"})
        assert r.status_code == 404
        assert r.text == "Not Found"
        assert host.calls == []

    def test_no_session_not_served(self):
        host = FakeHost()
        r = _client(host, provenance=None).get("/plugins/today/")
        assert r.status_code == 404
        assert r.text == "Not Found"
        assert host.calls == []

    def test_unknown_session_not_served(self):
        host = FakeHost()
        client = _client(host, provenance=None)
        client.cookies.set(SESSION_COOKIE, "not-a-session")
        assert client.get("/plugins/today/").status_code == 404
        assert host.calls == []

    def test_unattested_session_not_served(self):
        host = FakeHost()
        r = _client(host, provenance=PROVENANCE_UNATTESTED).get("/plugins/today/")
        assert r.status_code == 404
        assert host.calls == []

    def test_non_owner_principal_not_served(self):
        host = FakeHost()
        r = _client(host, principal_id="os-1002").get("/plugins/today/")
        assert r.status_code == 404
        assert host.calls == []

    def test_head_served(self):
        host = FakeHost()
        r = _client(host).head("/plugins/today/")
        assert r.status_code == 200
        assert r.content == b""
        assert r.headers["content-type"] == "text/html; charset=utf-8"
        assert host.calls == [("today", "/", {}, LOCAL_PRINCIPAL_ID)]
        _assert_sandbox_headers(r)

    def test_not_running_404(self):
        host = FakeHost(running=())
        r = _client(host).get("/plugins/today/")
        assert r.status_code == 404
        _assert_sandbox_headers(r)

    def test_invalid_name_404_without_calling_plugin(self):
        host = FakeHost()
        assert _client(host).get("/plugins/Not_A_Name/").status_code == 404
        assert host.calls == []

    def test_encoded_name_404_without_calling_plugin(self):
        host = FakeHost()
        assert _client(host).get("/plugins/%74oday/").status_code == 404
        assert host.calls == []

    @pytest.mark.parametrize("path", ["/plugins/today/%2e%2e/x", "/plugins/today/a%5cb", "/plugins/today//x",
                                      "/plugins/today/a%00b", "/plugins/today/a/%2E%2E"])
    def test_traversal_400(self, path):
        host = FakeHost()
        r = _client(host).get(path)
        assert r.status_code == 400
        assert host.calls == []
        _assert_sandbox_headers(r)

    def test_redirect(self):
        host = FakeHost()
        r = _client(host).get("/plugins/today")
        assert r.status_code in (307, 308)
        assert r.headers["location"] == "/plugins/today/"
        assert host.calls == []
        _assert_sandbox_headers(r)

    def test_redirect_needs_the_owner_session(self):
        r = _client(FakeHost(), provenance=None).get("/plugins/today")
        assert r.status_code == 404

    def test_redirect_for_an_invalid_name_is_404(self):
        r = _client(FakeHost()).get("/plugins/Not_A_Name")
        assert r.status_code == 404
        assert "location" not in r.headers

    def test_plugin_error_is_502(self):
        host = FakeHost(error=RpcError("timeout", "web.request timed out"))
        r = _client(host).get("/plugins/today/")
        assert r.status_code == 502
        assert r.text == "The plugin did not answer."

    @pytest.mark.parametrize(("kwargs", "path", "status"), [
        ({"provenance": None}, "/plugins/today/", 404),
        ({}, "/plugins/today/%2e%2e", 400),
        ({}, "/plugins/other/", 404),
        ({}, "/plugins/today/boom", 502),
    ])
    def test_headers_present_on_error(self, kwargs, path, status):
        host = FakeHost(error=RpcError("internal_error", "x")) if path.endswith("boom") else FakeHost()
        r = _client(host, **kwargs).get(path)
        assert r.status_code == status
        _assert_sandbox_headers(r)

    def test_plugin_csp_cannot_replace_the_sandbox(self):
        host = FakeHost(result={
            "status": 200, "body": "x",
            "headers": {"content-type": "text/plain", "content-security-policy": "default-src *",
                        "cache-control": "public, max-age=600", "x-frame-options": "ALLOWALL"},
        })
        r = _client(host).get("/plugins/today/")
        _assert_sandbox_headers(r)


class TestNotMountedWithoutHost:
    def test_no_plugin_host_no_route(self):
        sessions = LocalSessionStore()
        client = TestClient(build_app(WebApprovalUI(), sessions=sessions), base_url="http://localhost")
        client.cookies.set(SESSION_COOKIE, sessions.create(provenance=PROVENANCE_HUMAN))
        assert client.get("/plugins/today/").status_code == 404
