"""The ``today`` example plugin against a recorded Calendar fixture, one class per framework part."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest
import today_plugin

from privacyfence_plugin_sdk import PROTOCOL_VERSION
from privacyfence_plugin_sdk.testing import PluginTestHost, SourceFixtureMissing

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "plugins" / "today" / "calendar.list_events.json"
MANIFEST = Path(__file__).resolve().parents[3] / "examples" / "plugins" / "today" / "privacyfence-plugin.yaml"

GATES = {
    "status": "auto",
    "refresh": "auto",
    "list_events": "review",
    "add_note": "popup",
    "clear_notes": "popup",
    "publish": "popup",
}


def make_host(crash_tool: bool = False) -> PluginTestHost:
    return PluginTestHost(today_plugin.build_plugin(crash_tool=crash_tool), max_gate_floor="auto")


def load_calendar(host: PluginTestHost) -> None:
    host.source.load(json.loads(FIXTURE.read_text(encoding="utf-8")))


def principal_dir(host: PluginTestHost) -> Path:
    return host.data_dir.parent / "principals" / "local"


async def until(predicate, tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


class TestInitializeAndScopeType:
    async def test_the_tools_and_their_gates_are_declared(self):
        async with make_host() as host:
            tools = {t["name"]: t for t in host.tools}
        assert {name: t["gate"] for name, t in tools.items()} == GATES
        assert {name for name, t in tools.items() if t["read_only"]} == {"status", "list_events"}
        assert {name for name, t in tools.items() if t["destructive"]} == {"clear_notes"}
        assert tools["list_events"]["scopes"] == ["calendar"]

    async def test_the_calendar_scope_type_is_declared(self):
        async with make_host() as host:
            host.rules.allow_scope("calendar", ["primary"])  # refused for a scope type nobody declared

    async def test_the_plugin_speaks_protocol_one(self):
        assert PROTOCOL_VERSION.startswith("1.")
        assert today_plugin.self_test() == f"today ok protocol {PROTOCOL_VERSION}"

    async def test_the_manifest_matches_the_plugin(self):
        manifest = MANIFEST.read_text(encoding="utf-8")
        assert "name: today" in manifest and 'protocol: "1"' in manifest and "max_gate_floor: auto" in manifest
        assert f"version: {today_plugin.VERSION}" in manifest


class TestSourceCall:
    async def test_it_reads_todays_events_on_the_primary_calendar(self):
        async with make_host() as host:
            load_calendar(host)
            outcome = await host.call_tool("refresh", {})
            (call,) = host.source.calls
        assert outcome.error is None
        assert call.operation == "calendar.list_events"
        assert call.params["calendar_id"] == "primary"
        start, end = (datetime.fromisoformat(call.params[k]) for k in ("time_min", "time_max"))
        assert start.time() == time(0) and end.time() == time(0)
        assert end.date() == start.date() + timedelta(days=1)

    async def test_another_calendar_can_be_picked(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {"calendar_id": "team@example.com"})
            assert host.source.calls[0].params["calendar_id"] == "team@example.com"

    async def test_the_audit_names_the_read_and_holds_no_content(self):
        async with make_host() as host:
            load_calendar(host)
            outcome = await host.call_tool("refresh", {})
        reads = [e for e in outcome.audit if e["decision"] == "plugin_source"]
        assert [e["tool"] for e in reads] == ["calendar.list_events"]
        assert "Planning" not in json.dumps(outcome.audit)

    async def test_a_read_with_no_fixture_is_reported(self):
        async with make_host() as host:
            with pytest.raises(SourceFixtureMissing):
                await host.call_tool("refresh", {})

    async def test_a_failing_connector_does_not_release_anything(self):
        async with make_host() as host:
            host.source.fail("calendar.list_events", "connector_unavailable", reason="signed_out")
            outcome = await host.call_tool("list_events", {})
        assert outcome.error is not None and outcome.released is None


class TestStorage:
    async def test_the_day_is_cached_in_the_principal_directory(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            saved = json.loads((principal_dir(host) / "day.json").read_text(encoding="utf-8"))
            assert [e["title"] for e in saved["events"]] == ["Planning", "Team lunch"]
            assert saved["stale"] is False

    async def test_notes_are_kept_in_the_principal_directory(self):
        async with make_host() as host:
            await host.call_tool("add_note", {"event_id": "EXAMPLE-1", "text": "bring slides"})
            saved = json.loads((principal_dir(host) / "notes.json").read_text(encoding="utf-8"))
            assert saved["notes"] == [{"event_id": "EXAMPLE-1", "text": "bring slides"}]

    async def test_the_counter_is_install_wide_and_written_on_a_flush(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            await host.call_tool("refresh", {})
            assert not (host.data_dir / "counter.json").exists()
            await host.emit("plugin.disabling", {"reason": "user"})
            assert json.loads((host.data_dir / "counter.json").read_text(encoding="utf-8")) == {"fetches": 2}


class TestStatusTool:
    async def test_it_reports_nothing_fetched_before_a_refresh(self):
        async with make_host() as host:
            outcome = await host.call_tool("status", {})
        assert not outcome.card_shown and outcome.approval["via"] == "auto"
        assert outcome.released["blocks"][0]["items"] == [{"label": "Fetched", "value": "no"}]

    async def test_it_reports_a_count_and_no_event_content(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            outcome = await host.call_tool("status", {})
        labels = {i["label"]: i["value"] for i in outcome.released["blocks"][0]["items"]}
        assert labels["Events"] == "2" and labels["Stale"] == "no"
        assert "Planning" not in json.dumps(outcome.released)


class TestRefreshTool:
    async def test_it_runs_without_a_card_and_stores_the_day(self):
        async with make_host() as host:
            load_calendar(host)
            outcome = await host.call_tool("refresh", {})
        assert outcome.gate == "auto" and not outcome.card_shown
        assert outcome.result["events"] == 2

    async def test_it_clears_a_stale_flag(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            await host.emit(
                "connector.state_changed", {"connector": "calendar", "state": "signed_out", "principal": "local"}
            )
            await host.call_tool("refresh", {})
            saved = json.loads((principal_dir(host) / "day.json").read_text(encoding="utf-8"))
        assert saved["stale"] is False


class TestListEventsTool:
    async def test_a_review_card_shows_the_table_and_the_ai_gets_exactly_it(self):
        async with make_host() as host:
            load_calendar(host)
            outcome = await host.call_tool("list_events", {})
        assert outcome.card_shown
        table = outcome.card.payload[0]
        assert table["type"] == "table"
        assert [r["title"] for r in table["rows"]] == ["Planning", "Team lunch"]
        assert table["rows"][0]["time"] == "09:00-09:30"
        assert table["rows"][0]["attendees"] == "Jane Example, John Sample"
        assert outcome.released == {"blocks": outcome.card.payload}
        assert outcome.card.scopes == {"calendar": ["primary"]}

    async def test_a_rule_for_the_primary_calendar_skips_the_card_but_not_other_calendars(self):
        async with make_host() as host:
            load_calendar(host)
            host.rules.allow_scope("calendar", ["primary"])
            primary = await host.call_tool("list_events", {})
            other = await host.call_tool("list_events", {"calendar_id": "team@example.com"})
        assert not primary.card_shown and primary.approval["via"] == "rule"
        assert other.card_shown

    async def test_a_denied_card_releases_nothing(self):
        async with make_host() as host:
            load_calendar(host)
            outcome = await host.call_tool("list_events", {}, decide="deny")
        assert outcome.released is None and outcome.error["code"] == "denied"


class TestAddNoteTool:
    async def test_a_popup_card_previews_the_note_and_the_call_is_single_use(self):
        async with make_host() as host:
            args = {"event_id": "EXAMPLE-1", "text": "bring slides"}
            first = await host.call_tool("add_note", args)
            second = await host.call_tool("add_note", args)
        assert first.card_shown and second.card_shown
        assert first.card.preview[0]["items"] == [{"label": "Event", "value": "EXAMPLE-1"}]
        assert first.card.preview[1]["text"] == "bring slides"
        assert first.result == {"notes": 1} and second.result == {"notes": 2}
        assert first.approval["approval_id"] != second.approval["approval_id"]

    async def test_a_denied_note_is_not_stored(self):
        async with make_host() as host:
            await host.call_tool("add_note", {"event_id": "EXAMPLE-1", "text": "x"}, decide="deny")
            assert not (principal_dir(host) / "notes.json").exists()

    async def test_the_calendar_is_never_written(self):
        async with make_host() as host:
            await host.call_tool("add_note", {"event_id": "EXAMPLE-1", "text": "x"})
            assert host.source.calls == []


class TestClearNotesTool:
    async def test_it_is_destructive_on_the_popup_gate_and_deletes_the_notes(self):
        async with make_host() as host:
            await host.call_tool("add_note", {"event_id": "EXAMPLE-1", "text": "x"})
            outcome = await host.call_tool("clear_notes", {})
            assert outcome.card_shown and outcome.result == {"cleared": True}
            assert not (principal_dir(host) / "notes.json").exists()
            tool = next(t for t in host.tools if t["name"] == "clear_notes")
        assert tool["destructive"] and tool["gate"] == "popup"

    async def test_a_rule_never_skips_the_card(self):
        async with make_host() as host:
            host.rules.allow_scope("calendar", ["primary"])
            outcome = await host.call_tool("clear_notes", {})
        assert outcome.card_shown


class TestPublishConfirmation:
    async def refreshed(self, host: PluginTestHost) -> None:
        load_calendar(host)
        await host.call_tool("refresh", {})
        await host.call_tool("add_note", {"event_id": "EXAMPLE-1", "text": "bring slides"})

    async def test_it_opens_a_confirmation_with_a_heading_and_a_diff_and_returns_its_id(self):
        async with make_host() as host:
            await self.refreshed(host)
            outcome = await host.call_tool("publish", {})
            (card,) = host.confirmations
        assert outcome.released == {"approval_id": card.approval_id}
        assert card.kind == "today_publish" and card.require_step_up is True
        assert [b["type"] for b in card.preview] == ["heading", "diff"]
        assert "+09:00-09:30 Planning" in card.preview[1]["text"]
        assert "+note on EXAMPLE-1: bring slides" in card.preview[1]["text"]

    async def test_an_approved_confirmation_publishes_the_day_on_the_page(self):
        async with make_host() as host:
            await self.refreshed(host)
            outcome = await host.call_tool("publish", {})
            assert "Nothing is published yet" in (await host.get("/")).text
            await host.decide_confirmation(outcome.released["approval_id"], "approve")
            await until(
                lambda: principal_dir(host).joinpath("day.json").read_text(encoding="utf-8").count('"published": {')
            )
            page = (await host.get("/")).text
        assert "Team lunch" in page and "bring slides" in page and "Nothing is published yet" not in page

    @pytest.mark.parametrize("decision", ["deny", "expire"])
    async def test_a_refused_confirmation_publishes_nothing(self, decision):
        async with make_host() as host:
            await self.refreshed(host)
            outcome = await host.call_tool("publish", {})
            await host.decide_confirmation(outcome.released["approval_id"], decision)
            await asyncio.sleep(0.05)
            page = (await host.get("/")).text
        assert "Nothing is published yet" in page

    async def test_without_a_fetched_day_there_is_nothing_to_confirm(self):
        async with make_host() as host:
            outcome = await host.call_tool("publish", {})
            assert host.confirmations == []
        assert outcome.result["published"] is False

    async def test_a_second_publish_diffs_against_the_first(self):
        async with make_host() as host:
            await self.refreshed(host)
            first = await host.call_tool("publish", {})
            await host.decide_confirmation(first.released["approval_id"], "approve")
            await until(lambda: 'published": {' in principal_dir(host).joinpath("day.json").read_text(encoding="utf-8"))
            await host.call_tool("add_note", {"event_id": "EXAMPLE-2", "text": "book a table"})
            await host.call_tool("publish", {})
            diff = host.confirmations[1].preview[1]["text"]
        assert "+note on EXAMPLE-2: book a table" in diff
        assert "+09:00-09:30 Planning" not in diff


class TestPage:
    async def test_it_is_served_with_the_sandbox_headers_and_is_self_contained(self):
        async with make_host() as host:
            page = await host.get("/")
        assert page.status == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "sandbox allow-scripts" in page.headers["content-security-policy"]
        assert not re.search(r"""(?:src|href)=["']/""", page.text)  # no subresource from the daemon
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", page.text)  # named or rgb() colours only

    async def test_it_shows_the_raw_manifest_in_a_code_block(self):
        async with make_host() as host:
            page = (await host.get("/")).text
        assert re.search(r"<pre><code[^>]*>name: today", page)
        assert "max_gate_floor: auto" in page

    async def test_it_carries_the_cookie_check_script(self):
        async with make_host() as host:
            page = (await host.get("/")).text
        assert "<script>" in page and "document.cookie" in page and 'id="cookie-check"' in page

    async def test_it_shows_notes_and_escapes_them(self):
        async with make_host() as host:
            await host.call_tool("add_note", {"event_id": "EXAMPLE-1", "text": "<b>bold</b>"})
            page = (await host.get("/")).text
        assert "&lt;b&gt;bold&lt;/b&gt;" in page and "<b>bold</b>" not in page

    async def test_other_methods_and_paths_do_not_reach_it(self):
        async with make_host() as host:
            assert (await host.request("POST", "/")).status == 405
            assert (await host.get("/nowhere")).status == 404

    async def test_a_missing_manifest_is_said_so_on_the_page(self, monkeypatch, tmp_path):
        monkeypatch.setattr(today_plugin, "_home", lambda: tmp_path)
        async with make_host() as host:
            page = (await host.get("/")).text
        assert "The manifest is not next to the executable." in page


class TestEvents:
    async def test_a_calendar_state_change_marks_the_day_stale_and_the_page_says_so(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            await host.emit(
                "connector.state_changed", {"connector": "calendar", "state": "signed_out", "principal": "local"}
            )
            status = await host.call_tool("status", {})
            page = (await host.get("/")).text
        assert {"label": "Stale", "value": "yes"} in status.released["blocks"][0]["items"]
        assert 'id="stale"' in page

    async def test_another_connector_changing_leaves_the_day_alone(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            await host.emit(
                "connector.state_changed", {"connector": "drive", "state": "signed_out", "principal": "local"}
            )
            assert 'id="stale"' not in (await host.get("/")).text

    async def test_a_state_change_before_any_fetch_is_harmless(self):
        async with make_host() as host:
            await host.emit(
                "connector.state_changed", {"connector": "calendar", "state": "signed_in", "principal": "local"}
            )
            assert not (principal_dir(host) / "day.json").exists()

    async def test_a_purge_deletes_its_files_and_acknowledges(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            await host.call_tool("add_note", {"event_id": "EXAMPLE-1", "text": "x"})
            await host.emit("plugin.disabling", {"reason": "user"})
            assert (host.data_dir / "counter.json").exists()
            assert await host.purge("all") is True
            assert not (principal_dir(host) / "day.json").exists()
            assert not (principal_dir(host) / "notes.json").exists()
            assert not (host.data_dir / "counter.json").exists()

    async def test_a_principal_purge_keeps_the_install_wide_counter(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            await host.emit("plugin.disabling", {"reason": "user"})
            assert await host.purge("principal", principal="local") is True
            assert not (principal_dir(host) / "day.json").exists()
            assert (host.data_dir / "counter.json").exists()

    async def test_an_install_purge_keeps_the_principal_files(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            await host.emit("plugin.disabling", {"reason": "user"})
            assert await host.purge("install") is True
            assert (principal_dir(host) / "day.json").exists()
            assert not (host.data_dir / "counter.json").exists()

    async def test_disabling_and_shutdown_each_flush_the_counter(self):
        async with make_host() as host:
            load_calendar(host)
            await host.call_tool("refresh", {})
            await host.emit("plugin.disabling", {"reason": "crash_limit"})
            await host.call_tool("refresh", {})
            await host.shutdown()
            counter = json.loads((host.data_dir / "counter.json").read_text(encoding="utf-8"))
        assert counter == {"fetches": 2}

    async def test_a_flush_with_nothing_to_count_writes_nothing(self):
        async with make_host() as host:
            await host.emit("plugin.disabling", {"reason": "user"})
            assert not (host.data_dir / "counter.json").exists()


class TestCrashTool:
    async def test_it_is_absent_without_the_build_flag(self):
        async with make_host() as host:
            assert "crash" not in {t["name"] for t in host.tools}

    async def test_it_is_listed_on_the_auto_gate_with_the_build_flag(self):
        async with make_host(crash_tool=True) as host:
            tool = next(t for t in host.tools if t["name"] == "crash")
        assert tool["gate"] == "auto" and not tool["read_only"]

    def test_the_flag_comes_from_build_flags_next_to_the_executable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(today_plugin, "_home", lambda: tmp_path)
        assert today_plugin._flags() == {}
        (tmp_path / "build-flags.json").write_text('{"crash_tool": true}', encoding="utf-8")
        assert today_plugin._flags() == {"crash_tool": True}
        (tmp_path / "build-flags.json").write_text("[1]", encoding="utf-8")
        assert today_plugin._flags() == {}
        (tmp_path / "build-flags.json").write_text("not json", encoding="utf-8")
        assert today_plugin._flags() == {}


class TestSelfTest:
    def test_it_prints_the_ok_line_without_starting_the_plugin(self, capsys):
        today_plugin.main(["--self-test"])
        assert capsys.readouterr().out == f"today ok protocol {PROTOCOL_VERSION}\n"

    def test_a_missing_tool_fails_it(self, monkeypatch):
        monkeypatch.setattr(today_plugin, "plugin", today_plugin.Plugin(name="today", version="1.0.0"))
        with pytest.raises(SystemExit, match="missing tools"):
            today_plugin.self_test()

    def test_without_the_flag_main_runs_the_plugin(self, monkeypatch):
        ran = []
        monkeypatch.setattr(today_plugin.plugin, "run", lambda: ran.append(True))
        today_plugin.main([])
        assert ran == [True]
