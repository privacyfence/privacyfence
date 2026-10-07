"""A plugin confirmation card built from hostile blocks, loaded in a real headless Chromium.

A plugin's preview reaches the human as typed blocks that PrivacyFence renders itself (ADR 0122).
The unit tests check the escaped markup as strings; this checks what a browser actually builds from
it: no element the renderer did not write, no script run, no bidi control left in the text.

The card is made the way the daemon makes one: ``ConfirmationService.request`` against a real
approvals registry, and the HTML it stores on the card is what the browser loads.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.website_site import chromium_launch_kwargs  # noqa: E402

from privacyfence.approvals import PendingApprovalRegistry  # noqa: E402
from privacyfence.plugins.confirm import ConfirmationService  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser, pytest.mark.timeout(60)]

SCRIPT = "<script>window.pwned=1</script>"
IMG = "<img src=x onerror=\"window.pwned=1\">"
SVG = "<svg onload=\"window.pwned=1\"></svg>"
BIDI = "‮evil‬ ⁦x⁩ ‏؜"

HOSTILE_PREVIEW = [
    {"type": "heading", "text": f"{BIDI}{SCRIPT}"},
    {"type": "fields", "items": [{"label": IMG, "value": SVG}, {"label": BIDI, "value": SCRIPT}]},
    {"type": "table", "columns": [{"key": "a", "label": SCRIPT}, {"key": "b", "label": IMG}],
     "rows": [{"a": IMG, "b": SVG}, {"a": BIDI}]},
    {"type": "text", "text": f"</div></pre>{SCRIPT}{IMG}"},
    {"type": "code", "text": f"</code></pre>{SCRIPT}", "language": "html"},
    {"type": "diff", "format": "unified", "text": f"@@ {SCRIPT} @@\n+{IMG}\n-{SVG}\n {BIDI}"},
]

# Every tag the confirmation dialog and the block renderer write. Anything else in the document
# came from plugin text that escaped its escaping.
EXPECTED_TAGS = {
    "html", "head", "meta", "style", "body", "script",
    "div", "h2", "p", "span", "pre", "code", "table", "thead", "tbody", "tr", "th", "td",
}


def _hostile_card_html() -> str:
    """Run the service on its own thread and loop: the sync Playwright API holds this one."""
    out: dict[str, str] = {}

    def run() -> None:
        registry = PendingApprovalRegistry()
        with ThreadPoolExecutor(max_workers=1) as executor:
            service = ConfirmationService(
                registry_provider=lambda: registry, unattended_active=lambda: False,
                executor=executor, audit=lambda *_: None,
            )
            result = asyncio.run(service.request("today", f"Today {IMG}", {
                "principal": "local", "kind": "publish_note", "title": f"{BIDI}{SCRIPT}",
                "preview": HOSTILE_PREVIEW,
            }, introspecting=False))
            card = registry.get(result["approval_id"])
            out["html"] = card.html
            card.event.set()  # let the finalizer finish so the executor can shut down

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=30)
    return out["html"]


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


class TestHostileBlocks:
    def test_no_injected_element_and_no_script_ran(self, browser):
        html = _hostile_card_html()
        page = browser.new_page()
        try:
            page.set_content(html)
            page.wait_for_timeout(300)  # time for an onerror or onload to fire, were there one
            assert page.evaluate("typeof window.pwned") == "undefined"

            tags = set(page.evaluate("[...document.querySelectorAll('*')].map(e => e.localName)"))
            assert tags <= EXPECTED_TAGS, tags - EXPECTED_TAGS
            assert page.evaluate("document.querySelectorAll('script').length") == 1
            assert page.evaluate("document.querySelectorAll('img, svg, iframe, object, embed').length") == 0
            handlers = page.evaluate(
                "[...document.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.name))"
                ".filter(n => n.startsWith('on'))"
            )
            assert handlers == []

            # The hostile text is on the card, as text.
            text = page.evaluate("document.body.innerText")
            assert "<script>window.pwned=1</script>" in text
            assert "onerror" in text
            for control in "‮‬⁦⁩‏؜":
                assert control not in text
            assert page.locator("h2").inner_text().startswith("Today <img")

            # The renderer's own structure is all there.
            assert page.locator("pre.pf-code > code").count() == 1
            assert page.locator("pre.pf-diff span.pf-diff-hunk").count() == 1
            assert page.locator("pre.pf-diff span.pf-diff-add").count() == 1
            assert page.locator("pre.pf-diff span.pf-diff-del").count() == 1
            assert page.locator("table.pf-table td").count() == 4
            assert page.locator('[data-pf-action="confirm"]').inner_text() == "Approve"
            assert page.locator('[data-pf-action="cancel"]').inner_text() == "Deny"
        finally:
            page.close()
