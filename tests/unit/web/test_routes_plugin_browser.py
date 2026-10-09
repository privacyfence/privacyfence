"""web/routes_plugin_browser.py, mounted the way the daemon mounts it: through build_app."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from privacyfence.plugins.page_index import PageIndex
from privacyfence.plugins.protocol import PageEntry
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


class FakeHost:
    def __init__(self):
        self.calls: list[tuple] = []
        self.indexes = {
            "alpha": PageIndex("alpha", "Alpha", (PageEntry(path="/", title="Alpha home"),)),
            "beta": PageIndex("beta", "Beta", (PageEntry(path="/x", title="Beta x"),)),
        }

    def page_links(self):
        return [("Alpha", "/plugin-pages/alpha"), ("Beta", "/plugin-pages/beta")]

    async def list_pages(self, name, principal):
        self.calls.append(("list_pages", name, principal.id))
        if name not in self.indexes:
            raise LookupError(name)
        return self.indexes[name]

    async def list_all_pages(self, principal):
        self.calls.append(("list_all_pages", principal.id))
        return list(self.indexes.values())

    async def web_request(self, name, path, query, principal):
        self.calls.append(("web_request", name))
        raise AssertionError("the browser never fetches a plugin page")


def _client(host, *, provenance: str | None = PROVENANCE_HUMAN, principal_id: str = LOCAL_PRINCIPAL_ID,
            bearer: str | None = None):
    sessions = LocalSessionStore()
    app = build_app(WebApprovalUI(), sessions=sessions, plugin_host=host)
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    if provenance is not None:
        client.cookies.set(SESSION_COOKIE, sessions.create(provenance=provenance, principal_id=principal_id))
    if bearer is not None:
        client.headers["Authorization"] = f"Bearer {bearer}"
    return client


def test_owner_session_sees_every_plugin():
    host = FakeHost()
    r = _client(host).get("/plugin-pages")
    assert r.status_code == 200
    assert "Alpha home" in r.text and "Beta x" in r.text
    assert host.calls == [("list_all_pages", LOCAL_PRINCIPAL_ID)]


def test_owner_session_sees_one_plugin():
    host = FakeHost()
    r = _client(host).get("/plugin-pages/alpha")
    assert r.status_code == 200
    assert "Alpha home" in r.text and "Beta x" not in r.text
    assert "All plugin pages" in r.text
    assert host.calls == [("list_pages", "alpha", LOCAL_PRINCIPAL_ID)]


@pytest.mark.parametrize("path", ["/plugin-pages", "/plugin-pages/alpha"])
@pytest.mark.parametrize("kind", ["no_session", "bearer", "non_owner", "unattested"])
def test_everyone_but_the_owner_session_gets_404_and_the_host_is_not_called(path, kind):
    host = FakeHost()
    client = {
        "no_session": lambda: _client(host, provenance=None),
        "bearer": lambda: _client(host, provenance=None, bearer="mcp-token"),
        "non_owner": lambda: _client(host, principal_id="someone-else"),
        "unattested": lambda: _client(host, provenance=PROVENANCE_UNATTESTED),
    }[kind]()
    r = client.get(path)
    assert r.status_code == 404
    assert r.text == "Not Found"
    assert host.calls == []


@pytest.mark.parametrize("name", ["Bad_Name", "a%20b", "UPPER"])
def test_invalid_name_is_404(name):
    host = FakeHost()
    assert _client(host).get(f"/plugin-pages/{name}").status_code == 404


def test_unknown_plugin_is_404():
    host = FakeHost()
    r = _client(host).get("/plugin-pages/gamma")
    assert r.status_code == 404
    assert r.text == "Not Found"


def test_nonce_csp_without_sandbox_and_no_store():
    r = _client(FakeHost()).get("/plugin-pages")
    csp = r.headers["content-security-policy"]
    assert "sandbox" not in csp
    assert "nonce-" in csp
    assert r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers
    nonce = csp.split("nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in r.text


def test_plugins_menu_is_in_the_page():
    r = _client(FakeHost()).get("/plugin-pages")
    assert 'href="/plugin-pages/beta"' in r.text
