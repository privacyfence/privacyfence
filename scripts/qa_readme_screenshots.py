#!/usr/bin/env python3
"""Generates the screenshots top-level README.md shows, from the real embedded web UI in a real
browser (Playwright/Chromium), seeded with synthetic demo data only:

- ``settings-connectors.png`` and ``settings-auto-accept-rules.png``: two pages of the local
  settings UI (``/settings``).
- ``gmail-read-thread.png`` and ``sheets-write.png``: two approval cards, opened from the
  approvals list (``/approvals`` -> Review) the way a person reaches them. Each one is produced by
  calling the real connector tool (``gmail_get_thread``, ``drive_sheets_write_range``) against a
  fake Google client, so the card is whatever the real gate builds for that call -- PII scan and
  highlighting, the "AI will receive" checklist, the stated reason and the calling AI system
  included. Nothing is hand-edited or mocked up.

Requires ``playwright``, which comes with the ``[test]`` extra (``pip install -e '.[test]'``),
and a Chromium binary for it (``playwright install chromium``); see qa_web_smoke.py's own
docstring for where to point --chromium-path if you'd rather use one already on disk.

    .venv/bin/pip install -e '.[test]'           # once; brings playwright
    .venv/bin/playwright install chromium        # once; the browser it drives
    .venv/bin/python scripts/qa_readme_screenshots.py                  # all four
    .venv/bin/python scripts/qa_readme_screenshots.py --only approvals # just the two cards

Regenerate only when the settings page's or the approval card's visual design changes
meaningfully -- not for every settings_controller.py/card_builder.py change.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "images" / "screenshots"

# Representative, fake-but-plausible state -- enough for the two screenshots
# to show a populated, real-looking page rather than every field empty.
# Seeded in the auto_accept: schema (policy.store).
_SETTINGS_YAML = """\
connectors: {}
pii_detection:
  enabled: true
auto_accept:
  version: 2
  rules:
    - id: r-gmail-sender
      predicate: i_am_sender
      value: null
      operations: [gmail.read_message]
      conditions: []
    - id: r-gmail-domain
      predicate: trusted_sender_domain
      value: ["example.com", "partner.example.org"]
      operations: [gmail.read_message]
      conditions: []
    - id: r-gmail-label
      predicate: label_match
      value: ["Newsletters"]
      operations: [gmail.archive_message]
      conditions: []
    - id: r-drive-folder
      predicate: approved_folder
      value: ["1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"]
      operations: [drive.read_file_contents, drive.download_file]
      conditions: []
"""

# Connectors shown as "connected" -- SettingsController marks a connector
# authed purely by name membership in this list, no real client needed.
_FAKE_CONNECTED = ["gmail", "drive", "slack", "calendar"]


def _sign_in_url(server, path: str) -> str:
    """A one-time sign-in link for this in-process server.

    ``WebServer`` does not mint sign-in links itself (web/server.py): minting
    belongs to the control channel, and an
    *attested* mint needs a companion process to call back to -- which this
    script has no reason to start, so it mints from the store directly, the
    same way the daemon's own bootstrap middleware consumes from it.
    """
    from privacyfence.web.session_auth import PROVENANCE_HUMAN

    return f"{server.base_url}{path}?bootstrap={server.bootstrap.mint(provenance=PROVENANCE_HUMAN)}"


def _build_server(tmp_dir: Path, port: int):
    from privacyfence import daemon_main, settings_controller as sc
    from privacyfence.web.server import WebServer
    from privacyfence.web_approval_ui import WebApprovalUI

    sc.data_dir = lambda: tmp_dir
    sc.org_dir = lambda: tmp_dir
    daemon_main.load_org_config = lambda: {}

    config_path = tmp_dir / "settings.yaml"
    config_path.write_text(_SETTINGS_YAML, encoding="utf-8")
    connector_host = SimpleNamespace(set_connectors=lambda c: None)
    controller = sc.SettingsController(
        str(config_path), connectors=list(_FAKE_CONNECTED), connector_host=connector_host,
    )

    web_ui = WebApprovalUI()
    server = WebServer(web_ui, host="127.0.0.1", port=port, controller=controller)
    server.start()
    return server, web_ui


# --------------------------------------------------------------------------- #
# Approval-card demo data. Synthetic throughout: example.com/example.org
# addresses, invented names, and the IBAN from the IBAN standard's own
# worked example -- never a real account, person, or message.
# --------------------------------------------------------------------------- #

_DEMO_MY_EMAIL = "sam.lee@example.com"
_DEMO_AGENT = ("claude-ai", "1.0")  # clientInfo name/version -> "Claude" in agent_identity's registry

_DEMO_THREAD_REASON = "Checking what Jordan still needs from HR before their start date, as you asked."
_DEMO_SHEET_REASON = "Filling in the Q4 budget figures you gave me in the chat."

_DEMO_THREAD_MESSAGES = [
    {
        "sender": "Jordan Rivera <jordan.rivera@example.org>",
        "recipients": [f"Sam Lee <{_DEMO_MY_EMAIL}>"],
        "date": "Mon, 14 Sep 2026 09:12",
        "body_text": (
            "Hi Sam,\n\n"
            "Thanks for the offer letter -- I've signed it and attached it here. For the payroll "
            "form: my date of birth is 3 April 1994, and my home address is 12 Harbour Lane, "
            "Exampleton.\n\n"
            "Salary should be paid to IBAN GB82WEST12345698765432.\n\n"
            "Best,\nJordan"
        ),
    },
    {
        "sender": f"Sam Lee <{_DEMO_MY_EMAIL}>",
        "recipients": ["Jordan Rivera <jordan.rivera@example.org>"],
        "date": "Mon, 14 Sep 2026 11:40",
        "body_text": (
            "Thanks Jordan, all received. I'll pass the payroll details on to finance and send "
            "your laptop pickup time on Thursday.\n\nSam"
        ),
    },
]

_DEMO_SHEET_VALUES = [
    ["Cost centre", "Q4 budget", "Owner", "Note"],
    ["Marketing", "€48,000", "A. Novak", "Includes trade fair"],
    ["Engineering", "€126,500", "R. Okafor", "Two contractor seats"],
    ["Customer success", "€32,750", "M. Duarte", ""],
    ["Total", "=SUM(B2:B4)", "", ""],
]


def _demo_gmail_connector():
    from privacyfence.connectors.gmail import GmailConnector
    from privacyfence.gmail_client import GmailMessage, GmailThread

    subject = "Re: Welcome aboard -- payroll form"
    messages = [
        GmailMessage(id=f"m{i}", thread_id="t-demo", subject=subject, **m)
        for i, m in enumerate(_DEMO_THREAD_MESSAGES, 1)
    ]
    client = SimpleNamespace(get_thread=lambda thread_id: GmailThread(id=thread_id, subject=subject, messages=messages))
    connector = GmailConnector(client)
    connector.my_email = _DEMO_MY_EMAIL
    return connector


def _demo_drive_connector():
    from privacyfence.connectors.drive import DriveConnector
    from privacyfence.drive_client import DriveFile

    sheet = DriveFile(
        id="demo-sheet", name="Q4 budget plan", mime_type="application/vnd.google-apps.spreadsheet",
        size=0, owners=[_DEMO_MY_EMAIL],
    )
    client = SimpleNamespace(
        get_file_metadata=lambda file_id: sheet,
        write_sheet_values=lambda *a, **k: {"updatedCells": 20},
    )
    connector = DriveConnector(client)
    connector.my_email = _DEMO_MY_EMAIL
    return connector


class _GatedCallRunner:
    """Runs real connector tool calls on a private event loop in a background thread, attributed
    to the demo AI system with its stated reason -- the same two scopes the MCP dispatcher enters
    around every tool call. Each call parks on its approval until the script denies it at the
    end."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def submit(self, connector, tool: str, args: dict, reason: str):
        from privacyfence.agent_identity import AgentSource, agent_scope, identify
        from privacyfence.gate import reason_scope

        agent = identify(*_DEMO_AGENT, AgentSource.CLIENT_INFO)

        async def call():
            with agent_scope(agent), reason_scope(reason):
                return await connector.call(tool, args)

        return asyncio.run_coroutine_threadsafe(call(), self._loop)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def _wait_for_pending(web_ui, tool: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for approval in web_ui.deferred_registry.list_pending():
            if approval.tool == tool and approval.html:
                return approval
        time.sleep(0.05)
    raise RuntimeError(f"{tool}: no approval card registered within {timeout}s")


def _capture_approval_card(page, server, approval, out_name: str, *, height: int) -> None:
    """Open ``approval`` from the approvals list's own Review control and capture the card.

    ``height`` is the viewport height: tall enough that the card's left column does not scroll,
    short enough that the image has no large empty band."""
    page.set_viewport_size({"width": 1000, "height": height})
    page.goto(_sign_in_url(server, "/approvals"))
    page.wait_for_selector(f'[data-approval-id="{approval.id}"]')
    with page.expect_navigation():
        page.locator(f'[data-approval-id="{approval.id}"] .pf-btn-review').click()
    page.wait_for_selector("[data-pf-action='deny']")
    page.wait_for_timeout(300)
    path = OUT_DIR / out_name
    page.screenshot(path=str(path), full_page=True)
    print(f"wrote {path}")


def _capture_approvals(page, server, web_ui, tmp_dir: Path) -> None:
    from privacyfence.approval_ui import init_approval_ui
    from privacyfence.audit_log import init_audit_logger
    from privacyfence.pii_detector import init_pii_detection

    init_approval_ui(web_ui)
    init_audit_logger(str(tmp_dir / "audit"))
    init_pii_detection(True)

    runner = _GatedCallRunner()
    calls = []
    try:
        calls.append(runner.submit(
            _demo_gmail_connector(), "gmail_get_thread", {"thread_id": "t-demo"}, _DEMO_THREAD_REASON,
        ))
        thread_card = _wait_for_pending(web_ui, "gmail_get_thread")
        calls.append(runner.submit(
            _demo_drive_connector(), "drive_sheets_write_range",
            {"spreadsheet_id": "demo-sheet", "range_a1": "Budget!A1:D5", "values": json.dumps(_DEMO_SHEET_VALUES)},
            _DEMO_SHEET_REASON,
        ))
        sheet_card = _wait_for_pending(web_ui, "drive_sheets_write_range")

        _capture_approval_card(page, server, thread_card, "gmail-read-thread.png", height=1010)
        _capture_approval_card(page, server, sheet_card, "sheets-write.png", height=780)
    finally:
        # Deny both, then let each tool call finish (a denied call raises) before stopping the loop.
        for approval in web_ui.deferred_registry.list_pending():
            web_ui.resolve(approval.id, "deny")
        for call in calls:
            try:
                call.result(timeout=10)
            except Exception:  # noqa: BLE001 - a denied call is expected to raise
                pass
        runner.stop()


def _capture_settings(page, server) -> None:
    page.goto(_sign_in_url(server, "/settings"))
    page.wait_for_selector("#app")
    page.click("[data-nav='connectors']")
    page.wait_for_timeout(200)
    path = OUT_DIR / "settings-connectors.png"
    page.locator("#app").screenshot(path=str(path))
    print(f"wrote {path}")

    page.click("[data-nav='auto_accept']")
    page.wait_for_timeout(200)
    path = OUT_DIR / "settings-auto-accept-rules.png"
    page.locator("#app").screenshot(path=str(path))
    print(f"wrote {path}")


def _run(chromium_path: str | None, only: str) -> None:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pf-qa-readme-shots-"))
    port = 18701
    try:
        server, web_ui = _build_server(tmp_dir, port)
        time.sleep(0.3)

        launch_kwargs = {"args": ["--no-sandbox"]}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            if only in ("all", "settings"):
                page = browser.new_page(viewport={"width": 1000, "height": 720}, color_scheme="light")
                _capture_settings(page, server)
            if only in ("all", "approvals"):
                # 2x for a sharp README image; each capture sets its own viewport height.
                page = browser.new_page(
                    viewport={"width": 1000, "height": 900}, device_scale_factor=2, color_scheme="light",
                )
                _capture_approvals(page, server, web_ui, tmp_dir)
            browser.close()

        server.stop()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--chromium-path", default=None,
        help="Path to a Chromium/Chrome binary, if Playwright's own bundled browser isn't installed.",
    )
    parser.add_argument(
        "--only", choices=("all", "settings", "approvals"), default="all",
        help="Which screenshots to regenerate (default: all four).",
    )
    args = parser.parse_args()

    try:
        _run(args.chromium_path, args.only)
    except ImportError as exc:
        print(f"qa_readme_screenshots.py: {exc} -- `pip install -e '.[test]'` first.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
