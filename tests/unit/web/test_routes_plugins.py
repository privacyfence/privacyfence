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


EMBEDDED_HEADERS = {
    **SANDBOX_HEADERS,
    "content-security-policy": pages.CSP_EMBEDDED,
    "x-frame-options": "SAMEORIGIN",
}


def _assert_sandbox_headers(response) -> None:
    for name, value in SANDBOX_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


def _assert_embedded_headers(response) -> None:
    for name, value in EMBEDDED_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


class ApprovalHost(FakeHost):
    """A host with one pending approval card per ``(plugin, approval_id)`` that frames ``path``,
    answering ``approval_embed_allowed`` the way the approval service does."""

    def __init__(self, cards: dict[tuple[str, str], str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.cards = dict(cards or {})
        self.embed_calls: list[tuple] = []

    async def approval_embed_allowed(self, name, approval_id, path):
        self.embed_calls.append((name, approval_id, path))
        return self.cards.get((name, approval_id)) == path


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


class TestApprovalEmbed:
    """``pf_approval``: only the page a pending card of that plugin frames may be framed, and only
    by this origin; the query reaches the plugin unchanged either way."""

    def test_pending_card_page_gets_the_embedded_headers(self):
        host = ApprovalHost({("today", "a1"): "/approval"})
        r = _client(host).get("/plugins/today/approval?pf_approval=a1&subject=x")
        assert r.status_code == 200
        _assert_embedded_headers(r)
        assert host.embed_calls == [("today", "a1", "/approval")]
        assert host.calls == [("today", "/approval", {"pf_approval": "a1", "subject": "x"}, LOCAL_PRINCIPAL_ID)]

    def test_head_gets_the_embedded_headers_too(self):
        host = ApprovalHost({("today", "a1"): "/approval"})
        r = _client(host).head("/plugins/today/approval?pf_approval=a1")
        assert r.status_code == 200
        _assert_embedded_headers(r)

    def test_the_path_is_normalized_before_the_check(self):
        host = ApprovalHost({("today", "a1"): "/an approval"})
        r = _client(host).get("/plugins/today/an%20approval?pf_approval=a1")
        _assert_embedded_headers(r)
        assert host.embed_calls == [("today", "a1", "/an approval")]

    @pytest.mark.parametrize("url", [
        "/plugins/today/approval?pf_approval=other",  # not a pending card (finalized or unknown)
        "/plugins/today/other?pf_approval=a1",  # another page of the same plugin
        "/plugins/today/approval/?pf_approval=a1",  # not exactly the framed path
        "/plugins/today/approval",  # no pf_approval at all
        "/plugins/today/approval?pf_approval=",  # an empty one
    ])
    def test_anything_else_keeps_the_sandbox_headers(self, url):
        host = ApprovalHost({("today", "a1"): "/approval"})
        r = _client(host).get(url)
        assert r.status_code == 200
        _assert_sandbox_headers(r)
        assert host.calls[0][2] == pages.parse_query(url.partition("?")[2])

    def test_another_plugins_card_does_not_count(self):
        host = ApprovalHost({("other", "a1"): "/approval"}, running=("today", "other"))
        r = _client(host).get("/plugins/today/approval?pf_approval=a1")
        _assert_sandbox_headers(r)
        assert host.embed_calls == [("today", "a1", "/approval")]

    def test_a_card_that_was_finalized_stops_counting(self):
        host = ApprovalHost({("today", "a1"): "/approval"})
        client = _client(host)
        _assert_embedded_headers(client.get("/plugins/today/approval?pf_approval=a1"))
        del host.cards[("today", "a1")]
        _assert_sandbox_headers(client.get("/plugins/today/approval?pf_approval=a1"))

    def test_a_host_without_the_method_is_not_allowed(self):
        host = FakeHost()
        r = _client(host).get("/plugins/today/approval?pf_approval=a1")
        assert r.status_code == 200
        _assert_sandbox_headers(r)
        assert host.calls == [("today", "/approval", {"pf_approval": "a1"}, LOCAL_PRINCIPAL_ID)]

    def test_a_rejected_path_is_never_checked(self):
        host = ApprovalHost({("today", "a1"): "/approval"})
        r = _client(host).get("/plugins/today/%2e%2e?pf_approval=a1")
        assert r.status_code == 400
        _assert_sandbox_headers(r)
        assert host.embed_calls == []

    def test_no_owner_session_is_never_checked(self):
        host = ApprovalHost({("today", "a1"): "/approval"})
        r = _client(host, provenance=None).get("/plugins/today/approval?pf_approval=a1")
        assert r.status_code == 404
        _assert_sandbox_headers(r)
        assert host.embed_calls == []

    def test_the_plugin_cannot_send_its_own_framing_headers(self):
        host = ApprovalHost({("today", "a1"): "/approval"}, result={
            "status": 200, "body": "x",
            "headers": {"content-type": "text/plain", "content-security-policy": "frame-ancestors *",
                        "x-frame-options": "ALLOWALL"},
        })
        _assert_embedded_headers(_client(host).get("/plugins/today/approval?pf_approval=a1"))


class NewTabsHost(ApprovalHost):
    """A host that answers ``page_new_tabs`` for the plugins in ``new_tabs``."""

    def __init__(self, new_tabs=(), **kwargs):
        super().__init__(**kwargs)
        self.new_tabs = set(new_tabs)
        self.new_tabs_calls: list[str] = []

    def page_new_tabs(self, name):
        self.new_tabs_calls.append(name)
        return name in self.new_tabs


class TestNewTabs:
    """A plugin whose manifest sets ``page_new_tabs`` gets ``CSP_NEW_TABS``; the framed approval
    page never does."""

    def test_a_plugin_with_the_key_gets_the_new_tabs_csp(self):
        host = NewTabsHost({"demo"}, running=("demo",))
        r = _client(host).get("/plugins/demo/")
        assert r.status_code == 200
        assert r.headers.get_list("content-security-policy") == [pages.CSP_NEW_TABS]
        assert r.headers.get_list("x-frame-options") == ["DENY"]
        assert host.new_tabs_calls == ["demo"]

    def test_a_plugin_without_the_key_keeps_the_sandbox(self):
        host = NewTabsHost(running=("demo",))
        r = _client(host).get("/plugins/demo/")
        _assert_sandbox_headers(r)
        assert host.new_tabs_calls == ["demo"]

    def test_a_host_without_the_method_keeps_the_sandbox(self):
        _assert_sandbox_headers(_client(FakeHost(running=("demo",))).get("/plugins/demo/"))

    def test_the_embed_wins(self):
        host = NewTabsHost({"demo"}, cards={("demo", "a1"): "/approval"}, running=("demo",))
        _assert_embedded_headers(_client(host).get("/plugins/demo/approval?pf_approval=a1"))

    def test_a_non_owner_is_refused_before_the_check(self):
        host = NewTabsHost({"demo"}, running=("demo",))
        r = _client(host, provenance=None).get("/plugins/demo/")
        assert r.status_code == 404
        _assert_sandbox_headers(r)
        assert host.new_tabs_calls == []

    def test_the_root_redirect_never_sets_it(self):
        host = NewTabsHost({"demo"}, running=("demo",))
        r = _client(host).get("/plugins/demo")
        assert r.status_code == 307
        assert r.headers.get_list("content-security-policy") == [pages.CSP]
        assert host.new_tabs_calls == []


class TestNotMountedWithoutHost:
    def test_no_plugin_host_no_route(self):
        sessions = LocalSessionStore()
        client = TestClient(build_app(WebApprovalUI(), sessions=sessions), base_url="http://localhost")
        client.cookies.set(SESSION_COOKIE, sessions.create(provenance=PROVENANCE_HUMAN))
        assert client.get("/plugins/today/").status_code == 404
