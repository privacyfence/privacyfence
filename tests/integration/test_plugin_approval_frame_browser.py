"""A plugin approval card that frames the plugin's own page, served by the real web server and
loaded in a real headless Chromium (ADR 0127).

The unit tests check the two framing headers as strings. This checks what they do in a browser,
with the real approval service deciding which page may be framed: the card's frame loads its page,
the page runs in an opaque origin, and nothing reports a CSP violation. The same plugin URL framed
by a same-origin page that is not its pending card stays blocked by the page's own
``frame-ancestors 'none'``: a control frame on that page loads, so the parent allowed the frame and
the block comes from the plugin response.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.integration.test_browser_smoke import _watch_csp_violations  # noqa: E402
from tests.integration.test_plugin_pages_browser import _free_port, _wait_until_connectable  # noqa: E402
from tests.website_site import chromium_launch_kwargs  # noqa: E402

from privacyfence import paths as paths_module  # noqa: E402
from privacyfence.plugins.approvals import ApprovalService, ApprovalStore  # noqa: E402
from privacyfence.plugins.manifest import Manifest  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web.session_auth import PROVENANCE_HUMAN  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser, pytest.mark.timeout(60)]

PLUGIN = "echo"
DIGEST = "sha256:" + "c" * 64

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Echo approval</title></head>
<body><pre id="out">pending</pre><script>
document.getElementById('out').textContent = JSON.stringify({origin: String(self.origin), query: location.search});
</script></body></html>"""

_OUT_READY = "() => { const o = document.getElementById('out'); return !!o && o.textContent !== 'pending'; }"


def _manifest() -> Manifest:
    return Manifest(
        name=PLUGIN, display_name="Echo", version="1.0.0", protocol="1.1.0", command=("run",),
        source_operations=frozenset(), max_gate_floor="review", pages=True, service_credentials=False,
    )


class _ApprovalPageHost:
    """The two ``PluginHost`` methods the page route uses, with the real approval service behind
    ``approval_embed_allowed``."""

    def __init__(self, service: ApprovalService) -> None:
        self._service = service
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def web_request(self, name, path, query, principal):
        self.calls.append((name, path, dict(query)))
        return {"status": 200, "headers": {"content-type": "text/html; charset=utf-8"}, "body": PAGE}

    async def approval_embed_allowed(self, name, approval_id, path):
        return self._service.embed_allowed(name, approval_id, path)


class _Harness:
    def __init__(self, tmp_path) -> None:
        self.web_ui = WebApprovalUI()
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.service = ApprovalService(
            store=ApprovalStore(tmp_path / "plugin-approvals.json"),
            registry_provider=lambda: self.web_ui.deferred_registry,
            unattended_active=lambda: False,
            executor=self.executor,
            audit=lambda plugin, kind, status: None,
        )
        self.service.poll_seconds = 0.01
        self.host = _ApprovalPageHost(self.service)
        # The service schedules its finalizers on the running loop, so it gets a loop of its own.
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._loop_thread.start()

    def request(self, *, subject_id: str, page: str) -> str:
        params = {
            "principal": "local", "kind": "template", "subject_id": subject_id, "digest": DIGEST,
            "title": f"Approve {subject_id}", "preview": [{"type": "text", "text": "A template"}],
            "page": page, "require_step_up": False,
        }
        result = asyncio.run_coroutine_threadsafe(
            self.service.request(PLUGIN, "Echo", _manifest(), params, introspecting=False), self.loop,
        ).result(timeout=5)
        assert result["status"] == "pending"
        return result["approval_id"]

    def close(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self.service.close(), self.loop).result(timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._loop_thread.join(timeout=5)
            self.executor.shutdown(wait=True)
            self.loop.close()


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
    harness = _Harness(tmp_path)
    port = _free_port()
    srv = WebServer(harness.web_ui, host="localhost", port=port, plugin_host=harness.host)
    srv.start()
    try:
        _wait_until_connectable("localhost", port)
        yield srv, harness
    finally:
        srv.stop()
        harness.close()


def _signed_in_page(browser, srv):
    context = browser.new_context()
    page = context.new_page()
    violations = _watch_csp_violations(context, page)
    page.goto(f"{srv.base_url}/approvals?bootstrap={srv.bootstrap.mint(provenance=PROVENANCE_HUMAN)}")
    page.wait_for_load_state("load")
    return context, page, violations


def _frame_out(frame) -> dict:
    frame.wait_for_function(_OUT_READY, timeout=10_000)
    return json.loads(frame.evaluate("document.getElementById('out').textContent"))


def _add_frame(page, frame_id: str, src: str):
    """Frame ``src`` from the open card, the way any same-origin page could."""
    page.evaluate(
        """([id, src]) => new Promise(resolve => {
            const f = document.createElement('iframe');
            f.id = id;
            f.setAttribute('sandbox', 'allow-scripts');
            f.addEventListener('load', () => resolve());
            f.src = src;
            document.body.appendChild(f);
        })""",
        [frame_id, src],
    )
    frame = page.locator(f"#{frame_id}").element_handle().content_frame()
    assert frame is not None
    return frame


class TestCardFrame:
    def test_the_card_frames_its_page_in_an_opaque_origin(self, browser, server):
        srv, harness = server
        approval_id = harness.request(subject_id="report.html", page="/approval")
        context, page, violations = _signed_in_page(browser, srv)
        try:
            response = page.goto(f"{srv.base_url}/approvals/{approval_id}")
            assert response is not None and response.status == 200
            assert "frame-src data: 'self';" in response.headers["content-security-policy"]

            frame = page.locator("iframe.pf-plugin-frame").element_handle().content_frame()
            assert frame is not None
            out = _frame_out(frame)

            assert out == {"origin": "null", "query": f"?pf_approval={approval_id}"}
            assert harness.host.calls == [(PLUGIN, "/approval", {"pf_approval": approval_id})]
            assert violations == []
        finally:
            context.close()


class TestUnmatchedFrameStaysBlocked:
    def test_the_same_page_framed_without_its_pending_card_is_blocked(self, browser, server):
        srv, harness = server
        decided = harness.request(subject_id="report.html", page="/approval")
        assert harness.web_ui.deferred_registry.finalize(decided, "deny")
        # A pending card of the same plugin, framing a different page.
        other = harness.request(subject_id="other.html", page="/other")
        context, page, violations = _signed_in_page(browser, srv)
        try:
            response = page.goto(f"{srv.base_url}/approvals/{other}")
            assert response is not None and response.status == 200

            # Control: this card's own frame loads, so the card lets same-origin frames in.
            own = page.locator("iframe.pf-plugin-frame").element_handle().content_frame()
            assert own is not None
            assert _frame_out(own)["origin"] == "null"
            assert violations == []

            urls = [
                f"/plugins/{PLUGIN}/approval?pf_approval={decided}",  # its card was decided
                f"/plugins/{PLUGIN}/approval?pf_approval={other}",  # a pending card, another page
                f"/plugins/{PLUGIN}/approval",  # no card at all
            ]
            for index, url in enumerate(urls):
                frame = _add_frame(page, f"probe{index}", url)
                # The plugin answered, but the browser refused to show its answer in a frame.
                assert frame.evaluate("document.getElementById('out')") is None, url

            assert [call[1] for call in harness.host.calls].count("/approval") == len(urls)
            assert violations, "expected frame-ancestors violations"
            assert all("frame-ancestors" in v or "ancestor" in v for v in violations), violations
        finally:
            context.close()
