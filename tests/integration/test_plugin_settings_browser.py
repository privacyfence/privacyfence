"""The Settings Plugins section and its enable dialog in a real headless Chromium, on the same
phone-layout rules every other app surface is held to (ADR 0078).

The host is a stand-in with two rows, one running and one waiting for review, whose actions record
what they were asked and return at once, like the real one: nothing here starts a plugin.
"""
from __future__ import annotations

import concurrent.futures

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)

from privacyfence.settings_controller import SettingsController  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

from .test_browser_smoke import (  # noqa: E402,F401
    _assert_phone_layout,
    _free_port,
    _phone_cases,
    _phone_screenshot,
    _sign_in_local,
    _wait_until_connectable,
    browser,
    pf_home,
    phone_page,
)

pytestmark = [pytest.mark.integration, pytest.mark.browser, pytest.mark.timeout(120)]

_REVIEW = {
    "name": "beta",
    "display_name": "Beta Reports",
    "version": "2.1.0",
    "executable_sha256": "e" * 64,
    "manifest_sha256": "f" * 64,
    "max_gate_floor": "auto",
    "source_operations": ["calendar.list_events", "gmail.search_messages"],
    "pages": True,
    "service_credentials": False,
    "outputs": True,
    "output_types": ["application/json", "text/csv"],
    "tools": [
        {"name": "beta_summary", "gate": "auto", "read_only": True, "destructive": False,
         "description": "A summary of the week."},
        {"name": "beta_send_report", "gate": "review", "read_only": False, "destructive": False,
         "description": "Mails the report to the people you pick."},
        {"name": "beta_wipe_cache", "gate": "popup", "read_only": False, "destructive": True,
         "description": "Deletes the cached reports."},
    ],
}

_ROWS = [
    {"name": "alpha", "display_name": "Alpha Notes", "version": "1.0.0", "state": "running", "reason": "",
     "enabled": True, "pages": True, "page_url": "/plugins/alpha/", "tools_note": "", "review": None,
     "last_error": None,
     "approvals": [
         {"approval_id": f"ap{i}", "kind": "report", "subject_id": f"weekly-report-{i}-with-a-long-subject-name",
          "digest": "sha256:" + "ab" * 32, "decided_at": "2026-10-01T09:00:00Z",
          "revoked_at": "2026-10-02T09:00:00Z" if i == 2 else None}
         for i in range(3)
     ]},
    {"name": "beta", "display_name": "Beta Reports", "version": "2.1.0", "state": "discovered", "reason": "",
     "enabled": False, "pages": True, "page_url": "", "tools_note": "", "review": _REVIEW,
     "last_error": "The plugin changed since you reviewed it; review it again."},
]


class _TwoRowHost:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def set_rows_changed_listener(self, fn) -> None:
        pass

    def on_connectors_changed(self, rows) -> None:
        pass

    def rows(self) -> list[dict]:
        return [dict(r) for r in _ROWS]

    def submit(self, coro):
        self.submitted.append(coro.cr_code.co_name)
        coro.close()
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result(None)
        return future

    async def rescan(self): ...
    async def inspect(self, name): ...
    async def enable(self, name, *, executable_sha256, manifest_sha256): ...
    async def disable(self, name): ...
    async def purge(self, name): ...
    async def revoke_approval(self, name, approval_id): ...


@pytest.fixture
def plugin_server(pf_home):  # noqa: F811
    config_path = pf_home / ".privacyfence" / "settings.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    host = _TwoRowHost()
    controller = SettingsController(str(config_path), connectors=[], connector_host=None, plugin_host=host)
    port = _free_port()
    server = WebServer(WebApprovalUI(), host="localhost", port=port, controller=controller)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield server, host
    finally:
        server.stop()


def _open_plugins(page, server) -> None:
    _sign_in_local(page, server)
    page.goto(f"{server.base_url}/settings")
    page.wait_for_selector(".pf-navitem")
    page.locator('.pf-navitem[data-nav="plugins"]').evaluate("(el) => el.click()")
    page.wait_for_selector(".pf-plugin-row")


class TestPhoneLayout:
    @pytest.mark.parametrize(("case", "width"), _phone_cases(["settings-plugins"]))
    def test_plugins_section(self, phone_page, plugin_server, case, width):  # noqa: F811
        server, _host = plugin_server
        _open_plugins(phone_page, server)

        assert phone_page.locator(".pf-plugin-row").count() == 2
        assert phone_page.get_by_text("Beta Reports").first.is_visible()
        assert phone_page.locator('.pf-plugin-row a[href="/plugins/alpha/"]').inner_text() == "Open page"
        # The same page is in the top navigation's Plugins menu (inline and in the narrow Menu),
        # and Beta Reports, which has no page, is not.
        assert phone_page.locator('[data-pf-plugins] a[href="/plugins/alpha/"]').count() == 2
        assert phone_page.locator("[data-pf-plugins] a").count() == 2
        assert not phone_page.locator("[data-pf-plugins]").first.get_attribute("hidden")
        assert phone_page.get_by_text("The plugin changed since you reviewed it").is_visible()
        _phone_screenshot(phone_page, f"{case}-{width}")
        _assert_phone_layout(phone_page, width, main=".pf-page")

    @pytest.mark.parametrize(("case", "width"), _phone_cases(["settings-plugins-approvals"]))
    def test_a_row_with_three_approvals(self, phone_page, plugin_server, case, width):  # noqa: F811
        server, host = plugin_server
        _open_plugins(phone_page, server)

        phone_page.locator(".pf-plugin-approvals summary").evaluate("(el) => el.click()")
        assert phone_page.locator(".pf-plugin-approvals li").count() == 3
        assert phone_page.get_by_role("button", name="Revoke approval of weekly-report-0-with-a-long-subject-name").is_visible()
        assert phone_page.get_by_text("Revoked 2026-10-02T09:00:00Z").is_visible()
        phone_page.get_by_role("button", name="Revoke approval of weekly-report-0-with-a-long-subject-name").evaluate("(el) => el.click()")
        phone_page.wait_for_function("() => true")
        assert host.submitted == ["revoke_approval"]
        _phone_screenshot(phone_page, f"{case}-{width}")
        _assert_phone_layout(phone_page, width, main=".pf-page")

    @pytest.mark.parametrize(("case", "width"), _phone_cases(["settings-plugins-enable-dialog"]))
    def test_enable_dialog(self, phone_page, plugin_server, case, width):  # noqa: F811
        server, host = plugin_server
        _open_plugins(phone_page, server)

        phone_page.get_by_role("button", name="Review and enable Beta Reports").evaluate("(el) => el.click()")
        phone_page.wait_for_selector(".pf-plugin-modal")

        dialog = phone_page.locator(".pf-plugin-modal")
        assert host.submitted == ["inspect"]
        text = dialog.inner_text()
        for expected in (
            "Enable Beta Reports 2.1.0?", "beta_summary", "Runs without asking", "beta_send_report", "Review",
            "beta_wipe_cache", "Popup", "Destructive", "Read-only", "Writes",
            "calendar.list_events, gmail.search_messages", "Serves its own pages: yes",
            "Some tools run without asking", "Publishes output files: JSON, CSV",
            "start a new conversation in your AI client",
        ):
            assert expected in text, expected
        _phone_screenshot(phone_page, f"{case}-{width}")
        _assert_phone_layout(phone_page, width, main=".pf-page")

        dialog.get_by_role("button", name="Cancel").evaluate("(el) => el.click()")
        assert phone_page.locator(".pf-plugin-modal").count() == 0
