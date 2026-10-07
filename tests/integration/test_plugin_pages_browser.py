"""A plugin page served by the real web server, loaded in a real headless Chromium (ADR 0124).

The unit tests check the headers as strings. This checks what the sandbox CSP does in a browser:
the page's script runs, but in an opaque origin, so it can neither read the app's cookies nor call
the app's API with the owner's session. A control on the app's own page shows that the cookie is
readable and the same fetch goes through there, so the failures come from the sandbox.
"""
from __future__ import annotations

import json
import socket
import time
import uuid

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.website_site import chromium_launch_kwargs  # noqa: E402

from privacyfence import paths as paths_module  # noqa: E402
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


class _PageHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def web_request(self, name, path, query, principal):
        self.calls.append((name, path))
        return {"status": 200, "headers": {"content-type": "text/html; charset=utf-8"}, "body": PAGE}


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
