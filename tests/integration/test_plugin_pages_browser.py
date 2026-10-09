"""A plugin page served by the real web server, loaded in a real headless Chromium (ADR 0124).

The unit tests check the headers as strings. This checks what the sandbox CSP does in a browser:
the page's script runs, but in an opaque origin, so it can neither read the app's cookies nor call
the app's API with the owner's session. A control on the app's own page shows that the cookie is
readable and the same fetch goes through there, so the failures come from the sandbox.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.website_site import chromium_launch_kwargs  # noqa: E402

from privacyfence import paths as paths_module  # noqa: E402
from privacyfence.plugins import pages as plugin_pages  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web.session_auth import PROVENANCE_HUMAN  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser, pytest.mark.timeout(60)]

PROBE_SCRIPT = """
const out = {origin: String(self.origin)};
try { out.cookie = document.cookie; } catch (e) { out.cookie = null; out.cookie_error = e.name; }
async function probe(url) {
  try { const r = await fetch(url, {credentials: 'include'}); return 'status ' + r.status; }
  catch (e) { return 'failed: ' + e.name; }
}
Promise.all([probe('/api/settings/state'), probe(location.href)]).then(([api, self_]) => {
  out.fetch_api = api;
  out.fetch_self = self_;
  document.getElementById('out').textContent = JSON.stringify(out);
});
"""

PAGE = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Probe</title></head>
<body><pre id="out">pending</pre><script>{PROBE_SCRIPT}</script></body></html>"""


LINK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Links</title></head>
<body><a id="two" href="/plugins/demo/two">two</a></body></html>"""

_PREVIEW_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "plugin_page_preview.py"
_spec = importlib.util.spec_from_file_location("plugin_page_preview", _PREVIEW_SCRIPT)
assert _spec is not None and _spec.loader is not None
_preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_preview)
CHECK_PAGE = _preview.CHECK_PAGE

JIRA_URL = "https://jira.example.test/browse/PF-1"
BLANK_PAGE = f'<!doctype html><html><body><a id="go" href="{JIRA_URL}" target="_blank">go</a></body></html>'
SAME_PAGE = f'<!doctype html><html><body><a id="go" href="{JIRA_URL}">go</a></body></html>'
OWN_PAGE = '<!doctype html><html><body><a id="go" href="/plugins/demo/two" target="_blank">go</a></body></html>'
FONT_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>@font-face{font-family:X;src:url(https://fonts.example.test/x.woff2)} p{font-family:X}</style>
<script>
var v = [];
document.addEventListener("securitypolicyviolation", function (e) { v.push({directive: e.violatedDirective}); });
</script></head>
<body><p>text</p><pre id="out">pending</pre>
<script>
(async function () {
  await document.fonts.load("16px X").catch(function () { return null; });
  await new Promise(function (r) { setTimeout(r, 0); });
  document.getElementById("out").textContent = JSON.stringify(v);
})();
</script></body></html>"""

JIRA_BODY = (
    '<p id="jira">jira</p><script>document.title = JSON.stringify('
    "{opener: window.opener === null, referrer: document.referrer})</script>"
)

SECOND_PAGE = '<!doctype html><html><body><p id="second">second page</p></body></html>'


class _PageHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.new_tabs: set[str] = set()

    def page_new_tabs(self, name):
        return name in self.new_tabs

    async def web_request(self, name, path, query, principal):
        self.calls.append((name, path))
        body = {
            "/links": LINK_PAGE,
            "/two": SECOND_PAGE,
            "/check": CHECK_PAGE,
            "/blank": BLANK_PAGE,
            "/same": SAME_PAGE,
            "/own": OWN_PAGE,
            "/font": FONT_PAGE,
        }.get(path, PAGE)
        return {"status": 200, "headers": {"content-type": "text/html; charset=utf-8"}, "body": body}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_connectable(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"{host}:{port} never became connectable")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(**chromium_launch_kwargs())
        except PlaywrightError as exc:
            pytest.skip(
                f"Chromium not available for Playwright ({exc}) -- run `playwright install chromium`, "
                "or set PRIVACYFENCE_TEST_CHROMIUM to a Chromium binary already on this machine"
            )
            return
        yield b
        b.close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    home = tmp_path / f"pf-home-{uuid.uuid4().hex[:8]}"
    (home / ".privacyfence").mkdir(parents=True)
    monkeypatch.setattr(paths_module, "data_dir", lambda: home / ".privacyfence")
    host = _PageHost()
    port = _free_port()
    srv = WebServer(WebApprovalUI(), host="localhost", port=port, plugin_host=host)
    srv.start()
    try:
        _wait_until_connectable("localhost", port)
        yield srv, host
    finally:
        srv.stop()


def _probe_result(page) -> dict:
    page.wait_for_function("document.getElementById('out').textContent !== 'pending'", timeout=10_000)
    return json.loads(page.locator("#out").inner_text())


class TestSandboxedPage:
    def test_page_script_cannot_read_cookies_or_call_the_api(self, browser, server):
        srv, host = server
        context = browser.new_context()
        try:
            page = context.new_page()
            # A human session, the way the companion opens the app.
            page.goto(f"{srv.base_url}/approvals?bootstrap={srv.bootstrap.mint(provenance=PROVENANCE_HUMAN)}")
            page.wait_for_load_state("load")
            # A cookie scripts could read, were they allowed to (the session cookie is HttpOnly).
            context.add_cookies([{"name": "probe", "value": "visible", "url": srv.base_url}])

            # Control: on the app's own page the cookie is readable and the fetch reaches the app.
            control = page.evaluate(
                "async () => ({cookie: document.cookie,"
                " api: await fetch('/api/settings/state').then(r => 'status ' + r.status, e => 'failed')})"
            )
            assert "probe=visible" in control["cookie"]
            assert control["api"].startswith("status ")

            response = page.goto(f"{srv.base_url}/plugins/demo/")
            assert response is not None and response.status == 200
            assert "sandbox allow-scripts" in response.headers["content-security-policy"]
            out = _probe_result(page)

            assert host.calls == [("demo", "/")]
            assert out["origin"] == "null"  # the script ran, in an opaque origin
            assert out["cookie"] in (None, "")
            assert out["fetch_api"].startswith("failed")
            assert out["fetch_self"].startswith("failed")
            assert host.calls == [("demo", "/")]  # the page's own fetch never reached the plugin
        finally:
            context.close()


class TestLinksBetweenPages:
    def test_a_link_to_another_page_of_the_plugin_does_not_carry_the_session(self, browser, server):
        srv, host = server
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto(f"{srv.base_url}/approvals?bootstrap={srv.bootstrap.mint(provenance=PROVENANCE_HUMAN)}")
            page.wait_for_load_state("load")

            # Opened directly, the owner reaches the first page.
            response = page.goto(f"{srv.base_url}/plugins/demo/links")
            assert response is not None and response.status == 200
            assert host.calls == [("demo", "/links")]

            # A navigation from the opaque-origin page is cross-site: no cookie, so the owner-only 404.
            with page.expect_navigation() as navigation:
                page.locator("#two").click()
            assert navigation.value.status == 404
            assert page.url == f"{srv.base_url}/plugins/demo/two"
            assert host.calls == [("demo", "/links")]  # the plugin was never asked for the second page

            # The same URL typed in directly carries the session and reaches the plugin.
            response = page.goto(f"{srv.base_url}/plugins/demo/two")
            assert response is not None and response.status == 200
            assert host.calls == [("demo", "/links"), ("demo", "/two")]
        finally:
            context.close()


def _signed_in_context(browser, srv):
    """A context with a human session and the external site stubbed, so nothing leaves the machine."""
    context = browser.new_context()
    context.route(
        "https://jira.example.test/**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=JIRA_BODY),
    )
    page = context.new_page()
    page.goto(f"{srv.base_url}/approvals?bootstrap={srv.bootstrap.mint(provenance=PROVENANCE_HUMAN)}")
    page.wait_for_load_state("load")
    return context, page


class TestCspAllowsSelfContainedPages:
    def test_inline_script_style_data_images_and_fonts(self, browser, server):
        srv, host = server
        context, page = _signed_in_context(browser, srv)
        try:
            response = page.goto(f"{srv.base_url}/plugins/demo/check")
            assert response is not None and response.status == 200
            out = _probe_result(page)
            assert out["script"] is True
            assert out["css"] == "rgb(0, 128, 0)"
            assert out["attr"] == "rgb(0, 0, 255)"
            assert out["json"] is True
            assert out["img"] == 1
            blocked = [
                v
                for v in out["violations"]
                if v["violatedDirective"].startswith(("script-src", "style-src", "img-src", "font-src", "default-src"))
            ]
            assert blocked == []
        finally:
            context.close()

    def test_a_blocked_font_host_is_reported(self, browser, server):
        srv, host = server
        context, page = _signed_in_context(browser, srv)
        try:
            page.goto(f"{srv.base_url}/plugins/demo/font")
            violations = _probe_result(page)
            assert len(violations) == 1
            assert violations[0]["directive"].startswith("font-src")
        finally:
            context.close()


class TestNewTabs:
    def test_same_tab_link_to_an_external_site_loads(self, browser, server):
        srv, host = server
        context, page = _signed_in_context(browser, srv)
        try:
            page.goto(f"{srv.base_url}/plugins/demo/same")
            with page.expect_navigation():
                page.locator("#go").click()
            assert page.locator("#jira").is_visible()
        finally:
            context.close()

    def test_blank_link_does_nothing_without_page_new_tabs(self, browser, server):
        srv, host = server
        context, page = _signed_in_context(browser, srv)
        try:
            response = page.goto(f"{srv.base_url}/plugins/demo/blank")
            assert response is not None
            assert response.headers["content-security-policy"] == plugin_pages.CSP
            page.locator("#go").click()
            page.wait_for_timeout(1000)
            assert len(context.pages) == 1
        finally:
            context.close()

    def test_blank_link_opens_an_unconnected_tab_with_page_new_tabs(self, browser, server):
        srv, host = server
        host.new_tabs = {"demo"}
        context, page = _signed_in_context(browser, srv)
        try:
            response = page.goto(f"{srv.base_url}/plugins/demo/blank")
            assert response is not None
            assert response.headers["content-security-policy"] == plugin_pages.CSP_NEW_TABS
            with context.expect_page(timeout=5000) as info:
                page.locator("#go").click()
            popup = info.value
            popup.wait_for_selector("#jira")
            assert json.loads(popup.title()) == {"opener": True, "referrer": ""}
            assert page.locator("#go").is_visible()  # the opener tab was not navigated
        finally:
            context.close()

    def test_new_tab_into_privacyfence_carries_no_session(self, browser, server):
        srv, host = server
        host.new_tabs = {"demo"}
        context, page = _signed_in_context(browser, srv)
        try:
            page.goto(f"{srv.base_url}/plugins/demo/own")
            statuses: list[int] = []
            context.on(
                "response",
                lambda r: statuses.append(r.status) if r.url == f"{srv.base_url}/plugins/demo/two" else None,
            )
            with context.expect_page(timeout=5000) as info:
                page.locator("#go").click()
            popup = info.value
            popup.wait_for_load_state("load")
            assert popup.url == f"{srv.base_url}/plugins/demo/two"
            assert statuses == [404]
            assert ("demo", "/two") not in host.calls
        finally:
            context.close()
