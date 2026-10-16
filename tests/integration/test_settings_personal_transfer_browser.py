"""Settings > Auto-accept > "Move your settings" in a real browser (web/personal_settings_shim.py).

The shim is JavaScript inside a Python string, and what it draws, the CSP it runs under and the file
download only exist in a live page, so this drives a real ``WebServer`` (local mode) with a real
Chromium: choose a file, read the preview dialog, cancel or confirm, export. Hostile file content must
come out as text. Skipped when Playwright or a Chromium build is missing, as test_browser_smoke.py does
(CI runs it with PRIVACYFENCE_REQUIRE_BROWSER=1)."""
from __future__ import annotations

import json
import pathlib
import uuid
from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright (test-only) not installed")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from privacyfence import paths as paths_module  # noqa: E402
from privacyfence import personal_settings  # noqa: E402
from privacyfence.policy import scope_rows  # noqa: E402
from privacyfence.settings_controller import SettingsController  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402
from tests.integration.test_browser_smoke import (  # noqa: E402
    _free_port,
    _sign_in_local,
    _wait_until_connectable,
    _watch_csp_violations,
)
from tests.website_site import chromium_launch_kwargs  # noqa: E402

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

HOSTILE_VALUE = "<img src=x id=pfxss onerror=\"window.__pfXss=1\">"
NOW = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)


def _row(scope: str, value: str, *, operations=None) -> scope_rows.ScopeRow:
    verbs = scope_rows.scope_verbs(scope)
    return scope_rows.ScopeRow(
        id=scope_rows.row_id_for(scope, [value]), scope=scope, value=[value], verbs=frozenset(verbs),
        operations=frozenset(operations) if operations is not None else scope_rows.implied_operations(scope, verbs),
    )


PRESENT = _row("drive.folder", "FOLDER_ALREADY_HERE")
ADDED = _row("gmail.sender_domain", "partner.example")
SKIPPED = _row("drive.folder", HOSTILE_VALUE, operations={"nonexistent.tool"})


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
def context(browser):
    ctx = browser.new_context(ignore_https_errors=True, accept_downloads=True)
    yield ctx
    ctx.close()


@pytest.fixture
def page(context):
    pg = context.new_page()
    pg.pf_console_log: list[str] = []  # type: ignore[attr-defined]
    pg.on("console", lambda msg: pg.pf_console_log.append(f"[console:{msg.type}] {msg.text}"))
    pg.on("pageerror", lambda exc: pg.pf_console_log.append(f"[pageerror] {exc}"))
    yield pg
    pg.close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A local-mode daemon whose Auto-accept already holds ``PRESENT``."""
    home = tmp_path / f"pf-home-{uuid.uuid4().hex[:8]}"
    (home / ".privacyfence").mkdir(parents=True)
    monkeypatch.setattr(paths_module, "data_dir", lambda: home / ".privacyfence")
    config_path = home / ".privacyfence" / "settings.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    controller = SettingsController(str(config_path), connectors=[], connector_host=None)
    assert controller.import_policy_rules(scope_rows.compile_rows([PRESENT]))
    port = _free_port()
    web = WebServer(WebApprovalUI(), host="localhost", port=port, controller=controller)
    web.start()
    try:
        _wait_until_connectable("localhost", port)
        yield web
    finally:
        web.stop()


def _write_export(tmp_path, rows) -> str:
    document = personal_settings.build_export(rows, None, mode="local", version="6.3.0", now=NOW)
    path = tmp_path / "incoming.json"
    path.write_bytes(personal_settings.export_bytes(document))
    return str(path)


def _open_auto_accept(page, server):
    _sign_in_local(page, server)
    page.goto(f"{server.base_url}/settings")
    page.wait_for_selector(".pf-navitem")
    page.locator(".pf-navitem", has_text="Auto-accept").click()
    page.wait_for_selector("[data-pf-personal-import]")


def _choose_file(page, path: str) -> None:
    with page.expect_file_chooser() as chooser:
        page.locator("[data-pf-personal-import]").click()
    chooser.value.set_files(path)
    page.wait_for_selector('[role="dialog"][aria-modal="true"]')


def _rule_sentences(page) -> str:
    return page.locator("#app").inner_text()


class TestImportDialog:
    def test_lists_the_groups_and_confirm_applies_the_new_rule(self, page, context, server, tmp_path):
        violations = _watch_csp_violations(context, page)
        _open_auto_accept(page, server)
        assert "partner.example" not in _rule_sentences(page)
        _choose_file(page, _three_row_file(tmp_path))

        dialog = page.locator('[role="dialog"]')
        assert dialog.get_attribute("aria-label") == "Import your settings"
        groups = {g.get_attribute("data-pf-personal-group"): g for g in dialog.locator("[data-pf-personal-group]").all()}
        assert set(groups) == {"add", "present", "skip"}
        assert "partner.example" in groups["add"].inner_text()
        assert "FOLDER_ALREADY_HERE" in groups["present"].inner_text()
        assert "Needs a newer PrivacyFence (unknown tool)." in groups["skip"].inner_text()
        confirm = page.locator("[data-pf-personal-confirm]")
        assert confirm.inner_text() == "Import 1 rule(s)"
        assert page.evaluate("() => document.activeElement.getAttribute('aria-label')") == "Cancel import"

        with page.expect_navigation():
            confirm.click()
        page.wait_for_selector(".pf-navitem")
        page.locator(".pf-navitem", has_text="Auto-accept").click()
        page.wait_for_function("() => document.getElementById('app').innerText.includes('partner.example')")
        assert page.locator('[role="dialog"]').count() == 0
        assert violations == []

    def test_cancel_and_escape_change_nothing(self, page, context, server, tmp_path):
        violations = _watch_csp_violations(context, page)
        _open_auto_accept(page, server)
        before = server.controller.policy_rows()

        _choose_file(page, _three_row_file(tmp_path))
        page.get_by_role("button", name="Cancel import").click()
        assert page.locator('[role="dialog"]').count() == 0
        assert page.evaluate("() => document.activeElement.getAttribute('data-pf-personal-import')") == "1"

        _choose_file(page, _three_row_file(tmp_path))
        page.keyboard.press("Escape")
        assert page.locator('[role="dialog"]').count() == 0

        assert server.controller.policy_rows() == before
        assert "partner.example" not in _rule_sentences(page)
        assert violations == []

    def test_hostile_file_content_renders_as_text(self, page, context, server, tmp_path):
        violations = _watch_csp_violations(context, page)
        _open_auto_accept(page, server)
        _choose_file(page, _three_row_file(tmp_path))

        skipped = page.locator('[data-pf-personal-group="skip"]')
        assert "<img src=x" in skipped.inner_text()
        assert page.locator("#pfxss").count() == 0
        assert page.evaluate("() => window.__pfXss === undefined")
        assert violations == []

    def test_a_file_that_is_not_an_export_alerts_and_opens_nothing(self, page, context, server, tmp_path):
        violations = _watch_csp_violations(context, page)
        _open_auto_accept(page, server)
        bogus = tmp_path / "bogus.json"
        bogus.write_text("{}", encoding="utf-8")
        messages: list[str] = []
        page.on("dialog", lambda d: (messages.append(d.message), d.accept()))

        with page.expect_file_chooser() as chooser:
            page.locator("[data-pf-personal-import]").click()
        chooser.value.set_files(str(bogus))
        page.wait_for_timeout(500)

        assert messages == ["This is not a PrivacyFence settings export."]
        assert page.locator('[role="dialog"]').count() == 0
        assert violations == []


class TestExport:
    def test_export_downloads_a_parsable_file_of_the_current_rules(self, page, context, server):
        violations = _watch_csp_violations(context, page)
        _open_auto_accept(page, server)

        with page.expect_download() as download:
            page.locator("[data-pf-personal-export]").click()
        assert download.value.suggested_filename == personal_settings.FILE_NAME
        raw = pathlib.Path(download.value.path()).read_bytes()
        document = personal_settings.parse_document(raw)
        assert [r.id for r in document.rows] == [PRESENT.id]
        source_mode = json.loads(raw.decode("utf-8"))["source"]["mode"]
        assert source_mode == "local"
        assert page.locator("#app").count() == 1  # the page stayed where it was
        assert violations == []


def _three_row_file(tmp_path) -> str:
    """One row to add, one the install already has, one it must skip. ``build_export`` leaves a
    row it cannot judge alone, so the skipped (unknown-tool) row is written as it would arrive."""
    return _write_export(tmp_path, [ADDED, PRESENT, SKIPPED])
