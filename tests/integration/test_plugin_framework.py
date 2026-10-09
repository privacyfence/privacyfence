"""The plugin framework end to end (ADR 0120-0126): a real ``echo`` plugin process, a real
``PluginHost``, a real ``WebServer`` and the official MCP client.

Nothing about the plugin is faked. The daemon's side runs in this process on a loopback socket;
the gate's popups are stubbed (as the guidelines ask) so a card is answered at once, and
confirmation cards are answered on the approvals registry. The browser-level checks of the page
sandbox live in test_plugin_pages_browser.py.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from privacyfence import auto_accept
from privacyfence.plugins import storage, supervisor as supervisor_mod
from privacyfence.plugins.blocks import to_card_blocks
from privacyfence.plugins.protocol import RpcError
from privacyfence.principal import LOCAL_PRINCIPAL
from tests.fixtures.plugins.echo.harness import (
    Stack,
    calendar_event,
    install_echo,
    mcp_session,
    until,
    web_session,
)
from tests.helpers import policy_rules

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

TOOLS = [
    "echo_auto_read", "echo_review_read", "echo_popup_write", "echo_destructive", "echo_confirm", "echo_source",
]
SANDBOX_CSP = (
    "sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    stack = Stack(tmp_path, monkeypatch, serve=True)
    install_echo(stack.plugins)
    await stack.start()
    await stack.enable()
    # The introspection run that enable needs also received a shutdown; start from a clean record.
    (storage.install_dir("echo") / "events.jsonl").unlink(missing_ok=True)
    try:
        yield stack
    finally:
        await stack.stop()


def events_of(stack) -> list[dict]:
    path = storage.install_dir("echo") / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def notes_of(stack) -> list[str]:
    path = storage.principal_dir("echo", LOCAL_PRINCIPAL) / "notes.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def decisions(stack, tool: str) -> list[str]:
    return [e["decision"] for e in stack.audit() if e["tool"] == tool]


class TestListing:
    async def test_the_plugins_tools_are_listed_with_a_reason_on_the_gated_ones(self, stack):
        async with mcp_session(stack.server) as mcp:
            listed = {t.name: t for t in (await mcp.session.list_tools()).tools}

        assert set(TOOLS) <= set(listed)
        assert "reason" not in listed["echo_auto_read"].input_schema.get("required", [])
        for gated in ("echo_review_read", "echo_popup_write", "echo_destructive", "echo_confirm", "echo_source"):
            assert "reason" in listed[gated].input_schema["required"], gated
        assert listed["echo_destructive"].annotations.destructive_hint is True
        assert listed["echo_review_read"].annotations.read_only_hint is True


class TestEchoAutoRead:
    async def test_released_without_a_card(self, stack):
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_auto_read", text="hello")

        assert result.is_error is False
        assert result.structured_content == {"blocks": [{"type": "text", "text": "hello"}]}
        assert stack.popups.read == [] and stack.popups.write == []
        [entry] = [e for e in stack.audit() if e["tool"] == "echo_auto_read"]
        assert entry["decision"] == "auto_accepted" and entry["connector"] == "echo"
        assert entry["claude_reason"] == "an end-to-end test"


class TestEchoReviewRead:
    async def test_returns_exactly_the_approved_data(self, stack):
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_review_read", dataset="alpha", text="approved text")

        released = result.structured_content["blocks"]
        assert released == [
            {"type": "heading", "text": "Dataset", "level": 2},
            {"type": "text", "text": "approved text"},
        ]
        [(args, kwargs)] = stack.popups.read
        shown = kwargs["preview_blocks"]
        # The card shows the preview, then exactly the blocks the AI received.
        assert shown[-len(released):] == to_card_blocks(released)
        # The plugin's own execute ran, and what it returned instead was never released.
        assert (storage.install_dir("echo") / "review_read-executed").read_text(encoding="utf-8") == "card"
        assert "NOT-APPROVED" not in json.dumps(result.structured_content)

    async def test_a_denied_card_releases_nothing(self, stack):
        stack.popups.decision = "deny"
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_review_read", dataset="alpha")

        assert result.is_error is True
        assert not (storage.install_dir("echo") / "review_read-executed").exists()
        assert decisions(stack, "echo_review_read") == ["rejected"]


class TestEchoPopupWrite:
    async def test_single_use(self, stack):
        # The MCP layer answers an identical call within 30 s from its own cache, so the repeat is
        # made on the plugin's connector, where the single-use rule lives.
        connector = stack.host.connectors()["echo"]
        first = await stack.run(connector.call("echo_popup_write", {"text": "buy milk"}))
        second = await stack.run(connector.call("echo_popup_write", {"text": "buy milk"}))

        assert first == {"stored": 1, "via": "card"}
        assert second == {"stored": 2, "via": "card"}
        # The approval was collected once: the identical second call asked again.
        assert len(stack.popups.write) == 2
        assert notes_of(stack) == ["buy milk", "buy milk"]
        assert decisions(stack, "echo_popup_write") == ["approved", "approved"]

    async def test_denied_write_never_runs(self, stack):
        stack.popups.decision = "deny"
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_popup_write", text="never")

        assert result.is_error is True
        assert notes_of(stack) == []

    async def test_destructive_tool_runs_after_its_popup(self, stack):
        async with mcp_session(stack.server) as mcp:
            await mcp.call("echo_popup_write", text="keep")
            result = await mcp.call("echo_destructive")

        assert result.structured_content == {"cleared": True}
        assert notes_of(stack) == []
        assert len(stack.popups.write) == 2


class TestPluginScopeRule:
    def allow(self, dataset: str) -> None:
        auto_accept.add_policy_v2_rules(policy_rules({
            "plugin.echo.review_read": [{"predicate": "plugin:echo:dataset", "value": dataset}],
        }))

    async def test_matching_call_skips_the_card(self, stack):
        self.allow("alpha")
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_review_read", dataset="alpha", text="by rule")

        assert result.structured_content["blocks"][-1] == {"type": "text", "text": "by rule"}
        assert stack.popups.read == []
        [entry] = [e for e in stack.audit() if e["tool"] == "echo_review_read"]
        assert entry["decision"] == "auto_accepted" and entry["auto_accept_rule"]
        assert (storage.install_dir("echo") / "review_read-executed").read_text(encoding="utf-8") == "card"

    @pytest.mark.parametrize("dataset", ["beta", "alpha,beta"])
    async def test_non_matching_call_still_shows_the_card(self, stack, dataset):
        self.allow("alpha")
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_review_read", dataset=dataset)

        assert result.is_error is False
        assert len(stack.popups.read) == 1
        assert decisions(stack, "echo_review_read") == ["approved"]


class TestConfirmRoundTrip:
    async def confirm(self, stack, mcp) -> str:
        result = await mcp.call("echo_confirm", text="Publish the day")
        assert result.is_error is False
        approval_id = result.structured_content["approval_id"]
        assert stack.registry.get(approval_id) is not None
        return approval_id

    async def await_status(self, mcp, approval_id: str) -> dict:
        result = await mcp.session.call_tool(
            "privacyfence_await_approval", {"approval_ids": [approval_id], "timeout_seconds": 10}
        )
        assert result.is_error is False
        return result.structured_content

    async def test_the_decision_reaches_the_agent(self, stack):
        async with mcp_session(stack.server) as mcp:
            approval_id = await self.confirm(stack, mcp)
            assert stack.registry.get(approval_id).kind == "confirm"
            stack.registry.answer(approval_id, "confirm")

            status = await self.await_status(mcp, approval_id)

        assert status[approval_id] == "approved"
        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_confirm"]
        assert summaries == ["echo_publish; requested", "echo_publish; approved"]

    async def test_a_cancelled_card_is_denied(self, stack):
        async with mcp_session(stack.server) as mcp:
            approval_id = await self.confirm(stack, mcp)
            stack.registry.answer(approval_id, "cancel")

            status = await self.await_status(mcp, approval_id)

        assert status[approval_id] == "denied"

    async def test_stopping_expires_an_unanswered_card_before_returning(self, stack):
        async with mcp_session(stack.server) as mcp:
            approval_id = await self.confirm(stack, mcp)

        await stack.stop()

        assert stack.registry.await_status(approval_id) == "expired"
        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_confirm"]
        assert summaries == ["echo_publish; requested", "echo_publish; expired"]

    async def test_refused_while_a_session_is_unattended(self, stack):
        stack.unattended = True
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_confirm", text="Publish the day")

        assert result.is_error is True
        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_confirm"]
        assert summaries == ["echo_publish; refused"]


class TestSourceCallAudit:
    async def test_no_content_in_audit(self, stack):
        stack.calendar.list_events.return_value = [calendar_event("Merger talks with ACME-SECRET")]
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_source")

        # The AI got the events, because a human approved the card that showed them.
        assert "ACME-SECRET" in json.dumps(result.structured_content)
        [entry] = [e for e in stack.audit() if e["decision"] == "plugin_source"]
        assert entry["connector"] == "plugin:echo" and entry["tool"] == "calendar.list_events"
        assert "bytes=" in entry["summary"] and "primary" in entry["summary"]
        # The audit log never holds what the source returned.
        raw = (stack.audit_dir / f"{entry['week']}.jsonl").read_text(encoding="utf-8")
        assert "ACME-SECRET" not in raw and "Merger" not in raw

    async def test_a_failed_source_call_is_audited_with_its_code(self, stack):
        stack.calendar_state = (False, None)
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_source")

        assert result.is_error is True
        [entry] = [e for e in stack.audit() if e["decision"] == "plugin_source"]
        assert entry["summary"].endswith("error=connector_unavailable")


class TestEchoPage:
    async def test_sandbox_csp(self, stack):
        client = await web_session(stack.server)
        try:
            response = await client.get("/plugins/echo/")
        finally:
            await client.aclose()

        assert response.status_code == 200
        assert "document.cookie" in response.text
        assert response.headers["content-security-policy"] == SANDBOX_CSP
        assert "allow-same-origin" not in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["content-type"].startswith("text/html")

    async def test_no_session_no_page(self, stack):
        import httpx2

        async with httpx2.AsyncClient(base_url=stack.server.base_url) as client:
            response = await client.get("/plugins/echo/")

        assert response.status_code == 404
        assert "document.cookie" not in response.text


class TestToolsListChanged:
    async def test_dropping_and_re_adding_a_tool_is_accepted_and_announced(self, stack):
        async with mcp_session(stack.server) as mcp:
            assert "echo_review_read" in await mcp.tool_names()
            before = mcp.list_changed()

            await stack.page("/drop", tool="review_read")
            await until(lambda: mcp.list_changed() > before)
            assert "echo_review_read" not in await mcp.tool_names()

            dropped = mcp.list_changed()
            await stack.page("/readd", tool="review_read")
            await until(lambda: mcp.list_changed() > dropped)
            assert "echo_review_read" in await mcp.tool_names()
            result = await mcp.call("echo_review_read", dataset="alpha")
            assert result.is_error is False

        connector = stack.host.connectors()["echo"]
        assert connector.last_tools_rejection is None
        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_lifecycle"]
        assert "tools changed: -review_read" in summaries and "tools changed: +review_read" in summaries

    async def test_a_tool_nobody_reviewed_is_rejected_and_the_old_list_stays(self, stack):
        async with mcp_session(stack.server) as mcp:
            before = sorted(n for n in await mcp.tool_names() if n.startswith("echo_"))
            announced = mcp.list_changed()

            await stack.page("/widen")
            await until(lambda: stack.row()["tools_note"] != "")

            assert sorted(n for n in await mcp.tool_names() if n.startswith("echo_")) == before
            assert mcp.list_changed() == announced
            missing = await mcp.call("echo_extra")

        assert missing.is_error is True
        connector = stack.host.connectors()["echo"]
        assert "was not in the list reviewed at enable" in connector.last_tools_rejection
        assert stack.row()["tools_note"] == f"last tools change rejected: {connector.last_tools_rejection}"
        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_lifecycle"]
        assert f"tools change rejected: {connector.last_tools_rejection}" in summaries


class TestReadReplay:
    async def test_an_approved_review_read_repeated_in_the_window_releases_the_same_payload(self, stack):
        async with mcp_session(stack.server) as mcp:
            first = await mcp.call("echo_review_read", dataset="alpha")
        # The MCP layer's own 30 s dedupe would answer a repeat from its cache, so the repeat goes
        # to the connector, where the approval ledger replays it.
        second = await stack.run(stack.host.connectors()["echo"].call("echo_review_read", {"dataset": "alpha"}))

        assert first.structured_content == second
        assert second["blocks"][-1] == {"type": "text", "text": "read 1"}
        # One card, one prepare: the repeat never reached the plugin for a payload nobody saw.
        assert len(stack.popups.read) == 1


async def process_id(stack, after: str | None = None) -> str:
    """The plugin's process id, polled until a process other than ``after`` answers."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            pid = (await stack.page("/pid"))["body"]
        except (LookupError, RpcError):
            pid = None  # not running right now
        if pid is not None and pid != after:
            return pid
        await asyncio.sleep(0.05)
    raise AssertionError("the plugin never answered with a new process")


class TestCrashRestart:
    async def test_a_crashed_plugin_comes_back(self, stack, monkeypatch):
        monkeypatch.setattr(supervisor_mod, "RESTART_BACKOFF_SECONDS", (0.0,))
        first_pid = await process_id(stack)

        with pytest.raises(RpcError):
            await stack.page("/crash")  # the page dies with the process
        second_pid = await process_id(stack, after=first_pid)

        assert second_pid != first_pid
        assert stack.row()["state"] == "running"
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_auto_read", text="back")
        assert result.structured_content == {"blocks": [{"type": "text", "text": "back"}]}


class TestDisabledAfterFive:
    async def test_five_crashes_in_a_row_disable_the_plugin(self, stack, monkeypatch):
        monkeypatch.setattr(supervisor_mod, "RESTART_BACKOFF_SECONDS", (0.0,))

        pid = None
        for _ in range(5):
            pid = await process_id(stack, after=pid)
            with pytest.raises(RpcError):
                await stack.page("/crash")  # the page dies with the process

        await until(lambda: stack.row()["reason"] == "crashed 5 times in 10 minutes")
        row = stack.row()
        assert row["state"] == "disabled" and row["enabled"] is False
        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_lifecycle"]
        assert "disabled: crashed 5 times in 10 minutes" in summaries
        async with mcp_session(stack.server) as mcp:
            assert not [n for n in await mcp.tool_names() if n.startswith("echo_")]


class TestCleanShutdown:
    async def test_stop_all_sends_shutdown_and_leaves_no_crash(self, stack):
        await stack.run(stack.host.stop_all())

        names = [e["event"] for e in events_of(stack)]
        assert names == ["shutdown"]
        assert stack.row()["state"] == "disabled"
        assert stack.host.connectors() == {}

    async def test_disabling_tells_the_plugin_first(self, stack):
        await stack.run(stack.host.disable("echo"))

        assert [e["event"] for e in events_of(stack)] == ["plugin.disabling", "shutdown"]
        assert stack.row()["reason"] == "disabled by you"


class TestEvents:
    async def test_connector_state_changes_reach_the_plugin(self, stack):
        # The first call only records a baseline; the second is a sign-out.
        stack.host.on_connectors_changed([{"key": "calendar", "enabled": True, "authed": True}])
        stack.host.on_connectors_changed([{"key": "calendar", "enabled": True, "authed": False}])

        await until(lambda: len(events_of(stack)) >= 1)
        [event] = events_of(stack)
        assert event["event"] == "connector.state_changed"
        assert event["params"]["connector"] == "calendar" and event["params"]["state"] == "signed_out"


class TestPurgeOnUninstall:
    async def test_purge_asks_the_plugin_then_removes_the_data(self, stack):
        async with mcp_session(stack.server) as mcp:
            await mcp.call("echo_popup_write", text="remember me")
        assert notes_of(stack) == ["remember me"]

        outcome = await stack.run(stack.host.purge("echo"))

        assert outcome == "ack"
        # The plugin is restarted after a purge, so its directories exist again, empty.
        assert {p.name for p in storage.install_dir("echo").iterdir()} <= {"events.jsonl"}
        assert not (storage.install_dir("echo") / "review_read-executed").exists()
        assert list(storage.principal_dir("echo", LOCAL_PRINCIPAL).iterdir()) == []
        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_lifecycle"]
        assert "data purged (ack)" in summaries

    async def test_removing_the_directory_deletes_data_rules_and_state(self, stack):
        auto_accept.add_policy_v2_rules(policy_rules({
            "plugin.echo.review_read": [{"predicate": "plugin:echo:dataset", "value": "alpha"}],
        }))
        async with mcp_session(stack.server) as mcp:
            await mcp.call("echo_popup_write", text="remember me")
        await stack.run(stack.host.stop_all())
        import shutil

        shutil.rmtree(stack.plugins / "echo")

        await stack.run(stack.host.rescan())

        assert not storage.install_dir("echo").exists()
        assert not storage.principal_dir("echo", LOCAL_PRINCIPAL).exists()
        assert not [r for r in auto_accept.get_policy_v2_store_rules() if r.predicate.startswith("plugin:echo:")]
        assert "echo" not in stack.host._store.load()
        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_lifecycle"]
        assert "removed; data and rules deleted" in summaries


class TestAuditTrail:
    async def test_every_echo_tool_leaves_an_audit_entry(self, stack):
        calls = {
            "echo_auto_read": {},
            "echo_review_read": {"dataset": "alpha"},
            "echo_popup_write": {"text": "note"},
            "echo_destructive": {},
            "echo_source": {},
            "echo_confirm": {"text": "Publish"},
        }
        async with mcp_session(stack.server) as mcp:
            for tool, args in calls.items():
                result = await mcp.call(tool, **args)
                assert result.is_error is False, tool

        for tool in calls:
            entries = [e for e in stack.audit() if e["tool"] == tool and e["connector"] == "echo"]
            assert len(entries) == 1, tool
            assert entries[0]["decision"] in ("auto_accepted", "approved"), tool

    async def test_enable_and_disable_are_audited(self, stack):
        await stack.run(stack.host.disable("echo"))

        summaries = [e["summary"] for e in stack.audit() if e["decision"] == "plugin_lifecycle"]
        assert summaries == ["enabled", "disabled"]
