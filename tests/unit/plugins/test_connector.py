"""Unit tests for privacyfence.plugins.connector: tool exposure and the two-step gate.

The plugin is a fake ``RpcPeer`` whose ``request`` is an ``AsyncMock``. The gate is the real
``gate.gated_call`` with a real ``PendingApprovalRegistry``, and only the popups are stubbed
(guidelines on faking the gate), except where only the arguments sent into the gate matter, where
``gated_call`` itself is spied on.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from freezegun import freeze_time

from privacyfence import approval_ui, auto_accept, gate, write_effects
from privacyfence.approval_window_html import NARROW, WIDE
from privacyfence.approvals import ApprovalPending, PendingApprovalRegistry
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connector import ToolParam
from privacyfence.gate import GateDeniedError, reason_scope
from privacyfence.local_files import LocalFileAccessError
from privacyfence.plugins import connector as plugin_connector
from privacyfence.plugins.blocks import to_card_blocks
from privacyfence.plugins.connector import (
    EXECUTE_FAILED,
    INVALID_PREVIEW,
    LOST_CALL,
    PREPARE_FAILED,
    REASON_PARAM_DESCRIPTION,
    WRITE_RESULT_WITHHELD,
    PluginConnector,
    default_title,
)
from privacyfence.plugins.constants import INLINE_RESULT_BYTES, WRITE_RESULT_MAX_BYTES
from privacyfence.plugins.protocol import RpcError, args_digest
from privacyfence.plugins.tools import tool_signature, validate_tool_defs
from privacyfence.web_approval_ui import WebApprovalUI

from ...helpers import assert_all_tools_leave_an_audit_trail, policy_rules

PRINCIPAL = {"id": "local", "display_name": "You", "storage_dir": "/tmp/plugin-data/today/user"}
MANIFEST = SimpleNamespace(max_gate_floor="auto")
SCOPE_TYPES = [{"name": "calendar", "description": "A calendar the events come from"}]

LOOKUP = {
    "name": "lookup", "description": "Look up the events of one day.",
    "parameters": {
        "type": "object",
        "properties": {
            "day": {"type": "string", "description": "The day, as YYYY-MM-DD."},
            "limit": {"type": "integer"},
        },
        "required": ["day"],
    },
    "read_only": True, "destructive": False, "gate": "review", "scopes": ["calendar"],
}
NOTE = {
    "name": "note", "description": "Add a note to the day.",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "pinned": {"type": "boolean"}, "weight": {"type": "number"}},
        "required": ["text"],
    },
    "read_only": False, "destructive": False, "gate": "popup", "scopes": [],
    "effect": "Adds a note to today's page.", "title": "Add note",
}
PING = {
    "name": "ping", "description": "Check that the plugin answers.",
    "parameters": {"type": "object", "properties": {}},
    "read_only": True, "destructive": False, "gate": "auto", "scopes": [],
}
FORGET = {
    "name": "forget", "description": "Delete a note.",
    "parameters": {"type": "object", "properties": {"note_id": {"type": "string"}}, "required": ["note_id"]},
    "read_only": False, "destructive": True, "gate": "popup", "scopes": [],
}
TOOLDEFS = [LOOKUP, NOTE, PING]

LOOKUP_PREVIEW = [{"type": "fields", "items": [{"label": "Day", "value": "Monday"}]}]
LOOKUP_PAYLOAD = [{"type": "text", "text": "Standup at 9"}]
NOTE_PREVIEW = [{"type": "fields", "items": [{"label": "Note", "value": "buy milk"}]}]
PING_PAYLOAD = [{"type": "text", "text": "pong"}]


def lookup_result(payload=None, scopes=None) -> dict:
    return {
        "preview": LOOKUP_PREVIEW,
        "payload": LOOKUP_PAYLOAD if payload is None else payload,
        "scopes": {"calendar": ["work"]} if scopes is None else scopes,
    }


PREPARE = {
    "lookup": lookup_result(),
    "note": {"preview": NOTE_PREVIEW, "scopes": {}},
    "ping": {"preview": [], "payload": PING_PAYLOAD, "scopes": {}},
}


class FakePeer:
    """Stands in for a running plugin's ``RpcPeer``. ``prepare[tool]`` and ``execute`` are a
    result, an exception to raise, or a callable taking the params."""

    def __init__(self) -> None:
        self.closed = False
        self.prepare = dict(PREPARE)
        self.execute = {"result": {"ok": True}}
        self.request = AsyncMock(side_effect=self._request)

    @staticmethod
    def _answer(value, params):
        if isinstance(value, BaseException):
            raise value
        return value(params) if callable(value) else value

    async def _request(self, method, params, *, timeout=None):
        if method == "tool.prepare":
            return self._answer(self.prepare[params["tool"]], params)
        if method == "tool.execute":
            return self._answer(self.execute, params)
        raise AssertionError(method)

    def calls(self, method):
        return [c.args[1] for c in self.request.call_args_list if c.args[0] == method]


def make_connector(
    peer, *, defs=None, events=None, reviewed=None, peer_provider=None, owns_approval=None,
) -> PluginConnector:
    conn = PluginConnector(
        "today", "Today", MANIFEST,
        peer_provider or (lambda: peer),
        lambda: dict(PRINCIPAL),
        (events if events is not None else []).append,
        scope_types=SCOPE_TYPES,
        reviewed=reviewed,
        owns_approval=owns_approval,
    )
    conn.set_tools(validate_tool_defs("today", TOOLDEFS if defs is None else defs, SCOPE_TYPES, MANIFEST))
    return conn


def read_audit(audit_dir) -> list[dict]:
    week_file = audit_dir / f"{current_week()}.jsonl"
    if not week_file.exists():
        return []
    return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]


async def wait_until(predicate, timeout=2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class Popups:
    """Stubs for the gate's popups. ``decision`` answers every card."""

    def __init__(self, monkeypatch, decision="accept") -> None:
        self.decision = decision
        self.read = []
        self.write = []
        self.pii = []
        monkeypatch.setattr(gate, "show_read_popup", self._read)
        monkeypatch.setattr(gate, "show_popup", self._write)
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", self._pii)

    def _read(self, *args, **kwargs):
        self.read.append((args, kwargs))
        return self.decision, None

    def _write(self, *args, **kwargs):
        self.write.append((args, kwargs))
        return self.decision, None

    def _pii(self, categories):
        self.pii.append(categories)
        return True


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


@pytest.fixture
def registry():
    reg = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
    approval_ui.init_approval_ui(WebApprovalUI(registry=reg))
    return reg


@pytest.fixture
def deferred_registry():
    """Makes a registry with the given TTLs and, at teardown, denies every card still open, so a
    failing test cannot leave a popup worker waiting forever."""
    made: list[PendingApprovalRegistry] = []

    def make(**ttls) -> PendingApprovalRegistry:
        reg = PendingApprovalRegistry(**ttls)
        approval_ui.init_approval_ui(WebApprovalUI(registry=reg))
        made.append(reg)
        return reg

    yield make
    for reg in made:
        for approval in reg.list_pending():
            reg.answer(approval.id, "deny")


@pytest.fixture
def popups(monkeypatch):
    return Popups(monkeypatch)


@pytest.fixture
def rules_file(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.dump({}), encoding="utf-8")
    auto_accept.init_config_path(str(path))
    return path


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls: list[dict] = []

    async def spy(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(plugin_connector, "gated_call", spy)
    return calls


class TestExposure:
    def test_name_is_the_plugin_name(self):
        assert make_connector(FakePeer()).name == "today"

    def test_tool_names_carry_the_plugin_prefix(self):
        names = [s.name for s in make_connector(FakePeer()).tool_specs()]
        assert names == ["today_lookup", "today_note", "today_ping"]

    def test_annotations_follow_the_definitions(self):
        conn = make_connector(FakePeer(), defs=[LOOKUP, NOTE, PING, FORGET])
        flags = {s.name: (s.read_only, s.destructive) for s in conn.tool_specs()}
        assert flags == {
            "today_lookup": (True, False),
            "today_note": (False, False),
            "today_ping": (True, False),
            "today_forget": (False, True),
        }

    def test_description_is_unchanged(self):
        specs = {s.name: s for s in make_connector(FakePeer()).tool_specs()}
        assert specs["today_lookup"].description == LOOKUP["description"]

    def test_parameters_are_scalar_tool_params(self):
        specs = {s.name: s for s in make_connector(FakePeer()).tool_specs()}
        assert specs["today_lookup"].params[:2] == [
            ToolParam("day", "str", required=True, default=None, description="The day, as YYYY-MM-DD."),
            ToolParam("limit", "int", required=False, default=None, description=""),
        ]
        assert [(p.name, p.annotation, p.required) for p in specs["today_note"].params[:3]] == [
            ("text", "str", True), ("pinned", "bool", False), ("weight", "float", False),
        ]

    def test_reason_param_on_gated_tools_only(self):
        specs = {s.name: s for s in make_connector(FakePeer()).tool_specs()}
        reason = ToolParam("reason", "str", required=True, description=REASON_PARAM_DESCRIPTION)
        assert specs["today_lookup"].params[-1] == reason
        assert specs["today_note"].params[-1] == reason
        assert REASON_PARAM_DESCRIPTION == "One sentence: why are you calling this tool right now?"
        assert all(p.name != "reason" for p in specs["today_ping"].params)

    def test_policy_rows_are_registered(self):
        make_connector(FakePeer())
        assert auto_accept.TOOL_TO_GATE["today_lookup"] == "review"
        assert auto_accept.TOOL_TO_GATE["today_note"] == "popup"
        assert auto_accept.TOOL_TO_GATE["today_ping"] == "auto"
        assert auto_accept.TOOL_TO_OPERATION["today_lookup"] == "plugin.today.lookup"
        assert auto_accept.TOOL_TO_OPERATION["today_note"] == "plugin.today.note"
        assert "today_ping" not in auto_accept.TOOL_TO_OPERATION
        assert gate._TOOL_LAYOUT["today_lookup"] == WIDE
        assert gate._TOOL_LAYOUT["today_note"] == WIDE
        assert gate._TOOL_LAYOUT["today_ping"] == NARROW
        assert write_effects.EFFECT_BY_TOOL["today_note"] == "Adds a note to today's page."

    def test_a_write_without_effect_gets_the_default_sentence(self):
        make_connector(FakePeer(), defs=[FORGET])
        assert write_effects.EFFECT_BY_TOOL["today_forget"] == "Runs Forget in the Today plugin."

    def test_clear_tools_unregisters_everything(self):
        conn = make_connector(FakePeer())
        conn.clear_tools()
        assert conn.tool_specs() == []
        assert "today_lookup" not in auto_accept.TOOL_TO_GATE

    def test_default_title(self):
        assert default_title("list_open_items") == "List open items"


class TestGateFlow:
    async def test_auto_read_no_card_and_audited(self, audit_dir, registry, popups):
        peer = FakePeer()
        conn = make_connector(peer)

        result = await conn.call("today_ping", {})

        assert result == {"blocks": PING_PAYLOAD}
        assert popups.read == [] and popups.write == []
        entries = read_audit(audit_dir)
        assert [(e["tool"], e["decision"], e["auto_accept_rule"]) for e in entries] == [
            ("today_ping", "auto_accepted", "auto"),
        ]
        assert entries[0]["connector"] == "today"
        [prepare] = peer.calls("tool.prepare")
        [execute] = peer.calls("tool.execute")
        assert execute["approval"]["approval_id"] == "auto-" + prepare["call_id"]
        assert execute["approval"]["via"] == "auto"
        assert execute["approval"]["decision"] == "approved"

    async def test_read_returns_prepared_payload_verbatim(self, audit_dir, registry, popups):
        peer = FakePeer()
        peer.execute = {"result": {"blocks": [{"type": "text", "text": "something else"}]}}
        conn = make_connector(peer)

        result = await conn.call("today_lookup", {"day": "2026-10-07"})

        assert result == {"blocks": LOOKUP_PAYLOAD}
        assert len(popups.read) == 1
        [execute] = peer.calls("tool.execute")
        assert execute["approval"]["via"] == "card"

    async def test_prepare_and_execute_carry_the_call(self, audit_dir, registry, popups):
        peer = FakePeer()
        conn = make_connector(peer)
        args = {"day": "2026-10-07", "limit": 3}

        with reason_scope("checking my day"):
            await conn.call("today_lookup", args)

        [prepare] = peer.calls("tool.prepare")
        [execute] = peer.calls("tool.execute")
        assert prepare["tool"] == "lookup"
        assert prepare["args"] == args
        assert prepare["reason"] == "checking my day"
        assert prepare["principal"] == PRINCIPAL
        assert execute["call_id"] == prepare["call_id"]
        assert execute["args_digest"] == args_digest(args)
        assert execute["approval"]["approval_id"] == "card-" + prepare["call_id"]
        datetime.fromisoformat(execute["approval"]["decided_at"].replace("Z", "+00:00"))

    async def test_a_failed_audit_write_does_not_fail_the_call(self, monkeypatch, caplog):
        def broken_logger():
            raise OSError("disk full")

        monkeypatch.setattr(plugin_connector, "get_audit_logger", broken_logger)
        assert await make_connector(FakePeer()).call("today_ping", {}) == {"blocks": PING_PAYLOAD}
        assert "Audit log write failed" in caplog.text

    async def test_no_reason_is_sent_as_null(self, audit_dir, registry, popups):
        peer = FakePeer()
        await make_connector(peer).call("today_ping", {})
        assert peer.calls("tool.prepare")[0]["reason"] is None

    async def test_write_executes_once(self, audit_dir, registry, popups):
        peer = FakePeer()
        conn = make_connector(peer)

        first = await conn.call("today_note", {"text": "buy milk"})

        assert first == {"ok": True}
        assert len(popups.write) == 1
        assert len(peer.calls("tool.execute")) == 1

        # The approved write was single use: the same call again prepares afresh and asks again.
        await conn.call("today_note", {"text": "buy milk"})
        prepares = peer.calls("tool.prepare")
        assert len(prepares) == 2
        assert prepares[0]["call_id"] != prepares[1]["call_id"]
        assert len(popups.write) == 2
        assert len(peer.calls("tool.execute")) == 2

    async def test_pending_reuses_prepared_call(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        peer = FakePeer()
        conn = make_connector(peer)

        with pytest.raises(ApprovalPending) as raised:
            await conn.call("today_lookup", {"day": "2026-10-07"})
        approval = registry.get(raised.value.result["approval_id"])
        registry.answer(approval.id, "accept")
        assert await wait_until(lambda: approval.final_decision is not None)

        peer.prepare["lookup"] = lookup_result(payload=[{"type": "text", "text": "never shown"}])
        result = await conn.call("today_lookup", {"day": "2026-10-07"})

        assert result == {"blocks": LOOKUP_PAYLOAD}
        assert len(peer.calls("tool.prepare")) == 1
        [execute] = peer.calls("tool.execute")
        assert execute["call_id"] == peer.calls("tool.prepare")[0]["call_id"]

    async def test_released_read_replays_same_payload(self, audit_dir, popups):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=900.0, ledger_ttl=300.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        peer = FakePeer()
        conn = make_connector(peer)
        args = {"day": "2026-10-07"}
        start = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)

        with freeze_time(start, tick=True) as frozen:
            first = await conn.call("today_lookup", args)
            peer.prepare["lookup"] = lookup_result(payload=[{"type": "text", "text": "fresh"}])

            frozen.move_to(start + timedelta(seconds=registry.ledger_ttl - 30))
            second = await conn.call("today_lookup", args)

            assert first == second == {"blocks": LOOKUP_PAYLOAD}
            assert len(peer.calls("tool.prepare")) == 1
            assert len(popups.read) == 1

            # Every release keeps the prepared call for another ledger window from now, so it
            # outlives any ledger entry for this call. Past the ledger, the same payload goes on
            # a new card.
            frozen.move_to(start + timedelta(seconds=registry.ledger_ttl + 30))
            third = await conn.call("today_lookup", args)

            assert third == {"blocks": LOOKUP_PAYLOAD}
            assert len(peer.calls("tool.prepare")) == 1
            assert len(popups.read) == 2

            frozen.move_to(start + timedelta(seconds=2 * registry.ledger_ttl + 60))
            fourth = await conn.call("today_lookup", args)

        assert fourth == {"blocks": [{"type": "text", "text": "fresh"}]}
        assert len(peer.calls("tool.prepare")) == 2
        assert len(popups.read) == 3
        assert popups.read[2][1]["preview_blocks"] == to_card_blocks(LOOKUP_PREVIEW + [{"type": "text", "text": "fresh"}])

    async def test_late_collect_releases_only_what_was_approved(self, audit_dir, deferred_registry):
        # Approved near the end of the card's pending lifetime and collected after it: the repeat
        # call still reuses the prepared call the human saw, never a fresh prepare under that
        # approval.
        registry = deferred_registry(hold_window=0.05, pending_ttl=900.0, ledger_ttl=300.0)
        peer = FakePeer()
        conn = make_connector(peer)
        start = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)

        with freeze_time(start, tick=True) as frozen:
            approvals = []
            for tool, args in (("today_lookup", {"day": "2026-10-07"}), ("today_note", {"text": "buy milk"})):
                with pytest.raises(ApprovalPending) as raised:
                    await conn.call(tool, args)
                approvals.append(registry.get(raised.value.result["approval_id"]))
            for prepared in conn._prepared.values():
                assert prepared.keep_until >= start.timestamp() + registry.pending_ttl + registry.ledger_ttl

            frozen.move_to(start + timedelta(seconds=700))
            for approval in approvals:
                registry.answer(approval.id, "accept")
            assert await wait_until(lambda: all(a.final_decision is not None for a in approvals))
            peer.prepare["lookup"] = lookup_result(payload=[{"type": "text", "text": "never shown"}])

            frozen.move_to(start + timedelta(seconds=950))
            read = await conn.call("today_lookup", {"day": "2026-10-07"})
            write = await conn.call("today_note", {"text": "buy milk"})

            assert read == {"blocks": LOOKUP_PAYLOAD}
            assert write == {"ok": True}
            prepares = peer.calls("tool.prepare")
            assert len(prepares) == 2
            assert [e["call_id"] for e in peer.calls("tool.execute")] == [p["call_id"] for p in prepares]

            # The write was single use: the same call again prepares afresh and gets its own card.
            with pytest.raises(ApprovalPending) as raised:
                await conn.call("today_note", {"text": "buy milk"})
            assert raised.value.result["approval_id"] != approvals[1].id
            assert len(peer.calls("tool.prepare")) == 3
            assert len(peer.calls("tool.execute")) == 2

    async def test_a_new_connector_instance_does_not_inherit_an_approval(self, audit_dir, deferred_registry):
        # A crash restart, purge restart or re-enable builds a new PluginConnector while the
        # ledger still holds the old approvals: each fresh prepare goes on a new card.
        registry = deferred_registry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        peer = FakePeer()
        before = make_connector(peer)
        calls = (("today_lookup", {"day": "2026-10-07"}), ("today_note", {"text": "buy milk"}))
        approvals = []
        for tool, args in calls:
            with pytest.raises(ApprovalPending) as raised:
                await before.call(tool, args)
            approvals.append(registry.get(raised.value.result["approval_id"]))
        for approval in approvals:
            registry.answer(approval.id, "accept")
        assert await wait_until(lambda: all(a.final_decision is not None for a in approvals))

        peer.prepare["lookup"] = lookup_result(payload=[{"type": "text", "text": "never shown"}])
        after = make_connector(peer)
        for (tool, args), approval in zip(calls, approvals, strict=True):
            with pytest.raises(ApprovalPending) as raised:
                await after.call(tool, args)
            assert raised.value.result["approval_id"] != approval.id

        assert len(peer.calls("tool.prepare")) == 4
        assert peer.calls("tool.execute") == []

    async def test_a_removed_and_added_tool_does_not_inherit_an_approval(self, audit_dir, registry, popups):
        peer = FakePeer()
        conn = make_connector(peer)
        args = {"day": "2026-10-07"}

        assert await conn.call("today_lookup", args) == {"blocks": LOOKUP_PAYLOAD}
        assert conn.handle_tools_changed({"tools": [NOTE, PING]})
        assert conn.handle_tools_changed({"tools": TOOLDEFS})
        fresh = [{"type": "text", "text": "fresh"}]
        peer.prepare["lookup"] = lookup_result(payload=fresh)

        assert await conn.call("today_lookup", args) == {"blocks": fresh}
        assert len(peer.calls("tool.prepare")) == 2
        assert len(popups.read) == 2
        assert popups.read[1][1]["preview_blocks"] == to_card_blocks(LOOKUP_PREVIEW + fresh)

    async def test_released_read_without_a_registry_is_not_kept(self, audit_dir, gated_call_spy):
        approval_ui.init_approval_ui(SimpleNamespace(deferred_registry=None))
        peer = FakePeer()
        conn = make_connector(peer)

        await conn.call("today_lookup", {"day": "2026-10-07"})
        await conn.call("today_lookup", {"day": "2026-10-07"})

        assert len(peer.calls("tool.prepare")) == 2

    async def test_preview_dict_has_no_plugin_content(self, gated_call_spy):
        peer = FakePeer()
        conn = make_connector(peer)

        await conn.call("today_lookup", {"day": "2026-10-07"})
        await conn.call("today_note", {"text": "buy milk"})

        read, write = gated_call_spy
        assert read["preview"] == {"Plugin": "Today", "Tool": "Lookup"}
        assert write["preview"] == {"Plugin": "Today", "Tool": "Add note"}
        assert read["tool_name"] == "Lookup"
        assert read["summary"] == "Day: Monday"
        assert read["raw_data"] == {"plugin": "today", "tool": "lookup", "scopes": {"calendar": ["work"]}}
        assert read["filtered_data"] == {"blocks": LOOKUP_PAYLOAD}
        assert write["filtered_data"] is None
        assert write["pii_scan_text"] is None
        assert read["gate"] == "review" and write["gate"] == "popup"
        assert read["connector"] == "today" and read["tool"] == "today_lookup"
        assert read["args"] == {"day": "2026-10-07"}

    async def test_summary_falls_back_to_the_title(self, gated_call_spy):
        peer = FakePeer()
        peer.prepare["lookup"] = {**lookup_result(), "preview": []}
        await make_connector(peer).call("today_lookup", {"day": "2026-10-07"})
        assert gated_call_spy[0]["summary"] == "Lookup"

    async def test_denied_never_executes(self, monkeypatch, audit_dir, registry):
        popups = Popups(monkeypatch, decision="deny")
        peer = FakePeer()
        conn = make_connector(peer)

        with pytest.raises(GateDeniedError):
            await conn.call("today_note", {"text": "buy milk"})
        with pytest.raises(GateDeniedError):
            await conn.call("today_lookup", {"day": "2026-10-07"})

        assert peer.calls("tool.execute") == []

        # A repeat read reuses the denied prepared call, so the ledger replays the same denial
        # with no new card. A write's denial is single use: a repeat prepares afresh and asks again.
        for _ in range(2):
            with pytest.raises(GateDeniedError, match="reused the user.s denial"):
                await conn.call("today_lookup", {"day": "2026-10-07"})
        assert [p.tool for p in conn._prepared.values()] == ["today_lookup"]
        with pytest.raises(GateDeniedError):
            await conn.call("today_note", {"text": "buy milk"})
        assert len(peer.calls("tool.prepare")) == 3
        assert len(popups.read) == 1
        assert len(popups.write) == 2
        assert peer.calls("tool.execute") == []

    async def test_review_scans_payload_for_pii(self, audit_dir, registry, popups):
        peer = FakePeer()
        peer.prepare["lookup"] = lookup_result(
            payload=[{"type": "text", "text": "Please wire the deposit to DE89370400440532013000, thanks."}],
        )
        conn = make_connector(peer)

        await conn.call("today_lookup", {"day": "2026-10-07"})

        [(args, _)] = popups.read
        assert args[4], "the payload's IBAN should reach the read card as a PII category"
        assert popups.pii, "a PII match forces the second confirmation"
        assert read_audit(audit_dir)[-1]["pii_detected"] is True

    async def test_scope_rule_auto_accepts_matching_call(self, audit_dir, registry, popups, rules_file):
        auto_accept.add_policy_v2_rules(policy_rules({
            "plugin.today.lookup": [{"predicate": "plugin:today:calendar", "value": "work"}],
        }))
        peer = FakePeer()
        conn = make_connector(peer)

        result = await conn.call("today_lookup", {"day": "2026-10-07"})

        assert result == {"blocks": LOOKUP_PAYLOAD}
        assert popups.read == []
        assert read_audit(audit_dir)[-1]["decision"] == "auto_accepted"

    async def test_scope_rule_does_not_accept_other_value(self, audit_dir, registry, popups, rules_file):
        auto_accept.add_policy_v2_rules(policy_rules({
            "plugin.today.lookup": [{"predicate": "plugin:today:calendar", "value": "work"}],
        }))
        peer = FakePeer()
        peer.prepare["lookup"] = lookup_result(scopes={"calendar": ["work", "personal"]})
        conn = make_connector(peer)

        await conn.call("today_lookup", {"day": "2026-10-07"})

        assert len(popups.read) == 1
        assert read_audit(audit_dir)[-1]["decision"] == "approved"

    async def test_identical_calls_in_flight_share_one_prepare(self, gated_call_spy):
        peer = FakePeer()
        release = asyncio.Event()

        async def slow_prepare(method, params, *, timeout=None):
            if method == "tool.prepare":
                await release.wait()
                return lookup_result()
            return {"result": None}

        peer.request.side_effect = slow_prepare
        conn = make_connector(peer)
        tasks = [asyncio.create_task(conn.call("today_lookup", {"day": "2026-10-07"})) for _ in range(2)]
        await asyncio.sleep(0.01)
        release.set()
        results = await asyncio.gather(*tasks)

        assert results[0] == results[1] == {"blocks": LOOKUP_PAYLOAD}
        assert len(peer.calls("tool.prepare")) == 1

    async def test_a_shared_prepare_failure_reaches_every_waiter(self, gated_call_spy):
        peer = FakePeer()
        release = asyncio.Event()

        async def failing_prepare(method, params, *, timeout=None):
            await release.wait()
            raise RpcError("internal_error")

        peer.request.side_effect = failing_prepare
        conn = make_connector(peer)
        tasks = [asyncio.create_task(conn.call("today_lookup", {"day": "2026-10-07"})) for _ in range(2)]
        await asyncio.sleep(0.01)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert [str(r) for r in results] == [PREPARE_FAILED, PREPARE_FAILED]
        assert conn._preparing == {}

    async def test_a_cancelled_prepare_is_not_kept(self, gated_call_spy):
        peer = FakePeer()
        started = asyncio.Event()

        async def hanging_prepare(method, params, *, timeout=None):
            started.set()
            await asyncio.Event().wait()

        peer.request.side_effect = hanging_prepare
        conn = make_connector(peer)
        task = asyncio.create_task(conn.call("today_lookup", {"day": "2026-10-07"}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert conn._preparing == {} and conn._prepared == {}

    async def test_an_expired_pending_call_is_prepared_again(self, gated_call_spy):
        peer = FakePeer()
        conn = make_connector(peer)

        async def pending(**kwargs):
            raise ApprovalPending({"status": "approval_pending"})

        plugin_connector.gated_call, original = pending, plugin_connector.gated_call
        try:
            with pytest.raises(ApprovalPending):
                await conn.call("today_lookup", {"day": "2026-10-07"})
        finally:
            plugin_connector.gated_call = original
        [prepared] = conn._prepared.values()
        prepared.keep_until = time.time() - 1

        await conn.call("today_lookup", {"day": "2026-10-07"})

        assert len(peer.calls("tool.prepare")) == 2

    async def test_a_gate_error_drops_the_prepared_call(self, monkeypatch):
        peer = FakePeer()
        conn = make_connector(peer)

        async def broken(**kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(plugin_connector, "gated_call", broken)
        with pytest.raises(OSError):
            await conn.call("today_lookup", {"day": "2026-10-07"})
        assert conn._prepared == {}

    async def test_removed_tools_lose_their_prepared_calls(self, monkeypatch):
        peer = FakePeer()
        conn = make_connector(peer)

        async def pending(**kwargs):
            raise ApprovalPending({"status": "approval_pending"})

        monkeypatch.setattr(plugin_connector, "gated_call", pending)
        for tool, args in (("today_lookup", {"day": "x"}), ("today_note", {"text": "y"})):
            with pytest.raises(ApprovalPending):
                await conn.call(tool, args)

        assert conn.handle_tools_changed({"tools": [LOOKUP, PING]})
        assert [p.tool for p in conn._prepared.values()] == ["today_lookup"]


class TestCardShowsPayload:
    async def test_preview_blocks_carry_preview_and_payload(self, gated_call_spy):
        await make_connector(FakePeer()).call("today_lookup", {"day": "2026-10-07"})
        assert gated_call_spy[0]["preview_blocks"] == to_card_blocks(LOOKUP_PREVIEW + LOOKUP_PAYLOAD)
        assert gated_call_spy[0]["pii_scan_text"] == "Standup at 9"

    async def test_wide_card_passes_blocks_to_the_popup(self, audit_dir, registry, popups):
        conn = make_connector(FakePeer())
        await conn.call("today_lookup", {"day": "2026-10-07"})
        await conn.call("today_note", {"text": "buy milk"})

        (_, read_kwargs), (_, write_kwargs) = popups.read[0], popups.write[0]
        assert read_kwargs["layout"] == WIDE and write_kwargs["layout"] == WIDE
        assert read_kwargs["preview_blocks"] == to_card_blocks(LOOKUP_PREVIEW + LOOKUP_PAYLOAD)
        assert write_kwargs["preview_blocks"] == to_card_blocks(NOTE_PREVIEW)

    @pytest.mark.parametrize("tool, args, shown", [
        ("today_lookup", {"day": "2026-10-07"}, ["Monday", "Standup at 9"]),
        ("today_note", {"text": "buy milk"}, ["buy milk"]),
    ])
    async def test_rendered_card_shows_the_blocks(self, audit_dir, tool, args, shown):
        registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        conn = make_connector(FakePeer())

        with pytest.raises(ApprovalPending) as raised:
            await conn.call(tool, args)
        approval = registry.get(raised.value.result["approval_id"])
        assert await wait_until(lambda: bool(approval.html))

        for text in shown:
            assert text in approval.html
        registry.answer(approval.id, "deny")
        assert await wait_until(lambda: approval.final_decision is not None)


class TestErrors:
    async def test_unknown_tool(self):
        with pytest.raises(ValueError, match="^Unknown tool: today_nope$"):
            await make_connector(FakePeer()).call("today_nope", {})

    async def test_plugin_not_running(self):
        conn = make_connector(FakePeer(), peer_provider=lambda: None)
        with pytest.raises(RuntimeError, match="^The Today plugin is not running.$"):
            await conn.call("today_ping", {})

    async def test_closed_peer_is_not_running(self):
        peer = FakePeer()
        peer.closed = True
        with pytest.raises(RuntimeError, match="^The Today plugin is not running.$"):
            await make_connector(peer).call("today_ping", {})

    @pytest.mark.parametrize("code, sentence", [
        ("connector_unavailable", "A service this plugin reads from is not connected."),
        ("payload_too_large", "The plugin's result is too large to return."),
        ("timeout", "The plugin did not answer in time."),
        ("upstream_error", "A service this plugin reads from returned an error."),
        ("unknown_tool", "The plugin could not prepare this call."),
        ("invalid_params", "The plugin could not prepare this call."),
    ])
    async def test_prepare_error_codes_become_fixed_sentences(self, code, sentence):
        peer = FakePeer()
        peer.prepare["ping"] = RpcError(code, "detail with connector content")
        with pytest.raises(RuntimeError) as raised:
            await make_connector(peer).call("today_ping", {})
        assert str(raised.value) == sentence

    @pytest.mark.parametrize("tool, result", [
        ("today_lookup", {**lookup_result(), "scopes": {}}),
        ("today_lookup", {**lookup_result(), "scopes": {"calendar": []}}),
        ("today_lookup", {"preview": LOOKUP_PREVIEW, "scopes": {"calendar": ["work"]}}),
        ("today_note", {"preview": NOTE_PREVIEW, "payload": LOOKUP_PAYLOAD, "scopes": {}}),
        ("today_lookup", {**lookup_result(), "preview": [{"type": "script", "text": "x"}]}),
        ("today_lookup", "not an object"),
    ])
    async def test_invalid_prepare_result(self, gated_call_spy, tool, result):
        peer = FakePeer()
        peer.prepare[tool.removeprefix("today_")] = result
        with pytest.raises(RuntimeError) as raised:
            await make_connector(peer).call(tool, {"day": "x", "text": "y"})
        assert str(raised.value) == INVALID_PREVIEW
        assert gated_call_spy == []

    async def test_oversized_payload(self, gated_call_spy):
        peer = FakePeer()
        peer.prepare["lookup"] = lookup_result(payload=[{"type": "text", "text": "x" * INLINE_RESULT_BYTES}])
        with pytest.raises(RuntimeError, match="^The plugin's result is too large to return.$"):
            await make_connector(peer).call("today_lookup", {"day": "x"})

    async def test_undeclared_scopes_are_not_passed_on(self, gated_call_spy):
        peer = FakePeer()
        peer.prepare["lookup"] = lookup_result(scopes={"calendar": ["work"], "other": ["z"]})
        await make_connector(peer).call("today_lookup", {"day": "x"})
        assert gated_call_spy[0]["raw_data"]["scopes"] == {"calendar": ["work"]}

    @pytest.mark.parametrize("code", ["unknown_call", "digest_mismatch"])
    async def test_write_execute_lost_the_call(self, gated_call_spy, code):
        peer = FakePeer()
        peer.execute = RpcError(code)
        with pytest.raises(RuntimeError) as raised:
            await make_connector(peer).call("today_note", {"text": "y"})
        assert str(raised.value) == LOST_CALL

    @pytest.mark.parametrize("error, sentence", [
        (RpcError("timeout"), "The plugin did not answer in time."),
        (RpcError("internal_error"), EXECUTE_FAILED),
    ])
    async def test_write_execute_failures(self, gated_call_spy, error, sentence):
        peer = FakePeer()
        peer.execute = error
        with pytest.raises(RuntimeError) as raised:
            await make_connector(peer).call("today_note", {"text": "y"})
        assert str(raised.value) == sentence
        assert len(peer.calls("tool.execute")) == 1

    async def test_write_execute_with_an_invalid_result(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = ["not", "an", "object"]
        with pytest.raises(RuntimeError, match=f"^{EXECUTE_FAILED}$"):
            await make_connector(peer).call("today_note", {"text": "y"})

    async def test_write_result_over_the_cap_is_withheld(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": "x" * (WRITE_RESULT_MAX_BYTES + 1)}
        result = await make_connector(peer).call("today_note", {"text": "y"})
        assert result == {"withheld": True, "message": WRITE_RESULT_WITHHELD}
        assert len(peer.calls("tool.execute")) == 1

    async def test_write_result_at_the_cap_is_returned(self, gated_call_spy):
        peer = FakePeer()
        value = "x" * (WRITE_RESULT_MAX_BYTES - 2)
        assert plugin_connector._wire_size(value) == WRITE_RESULT_MAX_BYTES
        peer.execute = {"result": value}
        assert await make_connector(peer).call("today_note", {"text": "y"}) == value

    async def test_write_result_with_a_lone_surrogate_is_withheld_not_raised(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": "\ud800" * WRITE_RESULT_MAX_BYTES}
        result = await make_connector(peer).call("today_note", {"text": "y"})
        assert result == {"withheld": True, "message": WRITE_RESULT_WITHHELD}

    async def test_write_result_with_a_small_lone_surrogate_is_returned(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": "\ud800"}
        assert await make_connector(peer).call("today_note", {"text": "y"}) == "\ud800"

    async def test_write_result_with_personal_data_is_withheld(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": {"iban": "DE89370400440532013000"}}
        result = await make_connector(peer).call("today_note", {"text": "y"})
        assert result == {"withheld": True, "message": WRITE_RESULT_WITHHELD}

    async def test_auto_write_result_is_screened_too(self, gated_call_spy):
        stamp = {
            "name": "stamp", "description": "Stamp the day.",
            "parameters": {"type": "object", "properties": {}},
            "read_only": False, "destructive": False, "gate": "auto", "scopes": [],
        }
        peer = FakePeer()
        peer.prepare["stamp"] = {"preview": [], "payload": None, "scopes": {}}
        peer.execute = {"result": {"iban": "DE89370400440532013000"}}
        conn = make_connector(peer, defs=[stamp])
        assert await conn.call("today_stamp", {}) == {"withheld": True, "message": WRITE_RESULT_WITHHELD}

    async def test_pii_detection_off_returns_the_result(self, gated_call_spy, monkeypatch):
        monkeypatch.setattr(plugin_connector, "detect_pii_categories", lambda text: [])
        peer = FakePeer()
        peer.execute = {"result": {"iban": "DE89370400440532013000"}}
        assert await make_connector(peer).call("today_note", {"text": "y"}) == {"iban": "DE89370400440532013000"}

    async def test_an_oversized_approval_id_is_withheld(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": {}, "approval_id": "a" * 3000}
        result = await make_connector(peer).call("today_note", {"text": "y"})
        assert result == {"withheld": True, "message": WRITE_RESULT_WITHHELD}
        assert "approval_id" not in result

    async def test_a_withheld_result_keeps_an_approval_id_the_plugin_owns(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": "x" * 3000, "approval_id": "abc"}
        conn = make_connector(peer, owns_approval=lambda approval_id: approval_id == "abc")
        assert await conn.call("today_note", {"text": "y"}) == {
            "withheld": True, "message": WRITE_RESULT_WITHHELD, "approval_id": "abc",
        }

    async def test_a_withheld_result_drops_an_approval_id_the_plugin_does_not_own(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": "x" * 3000, "approval_id": "abc"}
        conn = make_connector(peer, owns_approval=lambda approval_id: False)
        assert await conn.call("today_note", {"text": "y"}) == {"withheld": True, "message": WRITE_RESULT_WITHHELD}

    async def test_an_owned_approval_id_the_detector_reads_as_an_iban_is_returned(self, gated_call_spy):
        issued = "ee45cccd1eda4bdbb43f29457a3845c7"
        peer = FakePeer()
        peer.execute = {"result": {"note": "saved"}, "approval_id": issued}
        conn = make_connector(peer, owns_approval=lambda approval_id: approval_id == issued)
        assert await conn.call("today_note", {"text": "y"}) == {"note": "saved", "approval_id": issued}

    async def test_an_approval_id_the_plugin_does_not_own_is_still_screened(self, gated_call_spy):
        issued = "ee45cccd1eda4bdbb43f29457a3845c7"
        peer = FakePeer()
        peer.execute = {"result": {"note": "saved"}, "approval_id": issued}
        conn = make_connector(peer, owns_approval=lambda approval_id: False)
        assert await conn.call("today_note", {"text": "y"}) == {"withheld": True, "message": WRITE_RESULT_WITHHELD}

    async def test_plugin_stopped_before_execute(self, gated_call_spy):
        peer = FakePeer()
        conn = make_connector(peer)

        async def stop_then_release(**kwargs):
            peer.closed = True
            return kwargs["filtered_data"]

        plugin_connector.gated_call, original = stop_then_release, plugin_connector.gated_call
        try:
            with pytest.raises(RuntimeError, match="^The Today plugin is not running.$"):
                await conn.call("today_note", {"text": "y"})
            # A read still releases the payload the human approved.
            peer.closed = False
            assert await conn.call("today_lookup", {"day": "x"}) == {"blocks": LOOKUP_PAYLOAD}
        finally:
            plugin_connector.gated_call = original

    async def test_read_execute_errors_are_ignored(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = RpcError("unknown_call")
        assert await make_connector(peer).call("today_lookup", {"day": "x"}) == {"blocks": LOOKUP_PAYLOAD}

    async def test_write_result_with_approval_id(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": {"status": "queued"}, "approval_id": "a1"}
        assert await make_connector(peer).call("today_note", {"text": "y"}) == {
            "status": "queued", "approval_id": "a1",
        }

    async def test_non_object_write_result_with_approval_id(self, gated_call_spy):
        peer = FakePeer()
        peer.execute = {"result": "queued", "approval_id": "a1"}
        assert await make_connector(peer).call("today_note", {"text": "y"}) == {
            "result": "queued", "approval_id": "a1",
        }


class TestToolsChanged:
    def test_violation_keeps_previous_list(self):
        events: list[str] = []
        conn = make_connector(FakePeer(), events=events)
        before = [s.name for s in conn.tool_specs()]

        accepted = conn.handle_tools_changed({"tools": [LOOKUP, {**NOTE, "gate": "review", "destructive": True}]})

        assert accepted is False
        assert [s.name for s in conn.tool_specs()] == before
        assert auto_accept.TOOL_TO_GATE["today_note"] == "popup"
        assert events == ["tools change rejected: destructive tool note must use the popup gate"]
        assert conn.last_tools_rejection == "destructive tool note must use the popup gate"

    def test_a_list_that_is_not_a_list_is_rejected(self):
        events: list[str] = []
        conn = make_connector(FakePeer(), events=events)
        assert conn.handle_tools_changed("nonsense") is False
        assert events == ["tools change rejected: tools must be a list"]

    def test_an_unreviewed_tool_is_rejected(self):
        events: list[str] = []
        reviewed = frozenset(tool_signature(d) for d in validate_tool_defs("today", TOOLDEFS, SCOPE_TYPES, MANIFEST))
        conn = make_connector(FakePeer(), events=events, reviewed=reviewed)

        assert conn.handle_tools_changed({"tools": [*TOOLDEFS, FORGET]}) is False
        assert "today_forget" not in [s.name for s in conn.tool_specs()]
        assert events[-1].startswith("tools change rejected: tool forget was not in the list reviewed at enable")

    def test_a_registration_conflict_is_rejected(self):
        events: list[str] = []
        auto_accept.register_dynamic_tools("someone-else", [auto_accept.DynamicToolSpec(
            tool="today_forget", gate="auto", operation=None, verb=None, layout=NARROW, effect="",
            scope_predicates=(),
        )])
        conn = make_connector(FakePeer(), events=events)

        assert conn.handle_tools_changed({"tools": [*TOOLDEFS, FORGET]}) is False
        assert events[-1].startswith("tools change rejected: tool today_forget is already registered")
        assert auto_accept.TOOL_TO_GATE["today_note"] == "popup"

    def test_an_accepted_change_is_applied_and_audited(self):
        events: list[str] = []
        conn = make_connector(FakePeer(), events=events)
        conn.handle_tools_changed("nonsense")

        assert conn.handle_tools_changed({"tools": [LOOKUP, PING, FORGET]}) is True

        assert [s.name for s in conn.tool_specs()] == ["today_lookup", "today_ping", "today_forget"]
        assert "today_note" not in auto_accept.TOOL_TO_GATE
        assert events[-1] == "tools changed: +forget,-note"
        assert conn.last_tools_rejection is None

    def test_an_unchanged_list_says_so(self):
        events: list[str] = []
        conn = make_connector(FakePeer(), events=events)
        assert conn.handle_tools_changed({"tools": TOOLDEFS}) is True
        assert events == ["tools changed: none added or removed"]


PUBLISH = {
    "name": "publish", "description": "Publish a page.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "html": {
                "type": "string", "description": "The page.",
                "x-privacyfence-file": {"max_bytes": 1000, "media_types": ["text/html"]},
            },
        },
        "required": ["html"],
    },
    "read_only": False, "destructive": False, "gate": "popup", "scopes": [],
}
PAGE = b"<!doctype html><p>hello</p>"
PUBLISH_PREVIEW = [{"type": "fields", "items": [{"label": "Target", "value": "the site"}]}]


class TestFileParameter:
    @pytest.fixture(autouse=True)
    def _data_dir(self, tmp_path, monkeypatch):
        from privacyfence import paths
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

    @staticmethod
    def make(**kwargs):
        peer = FakePeer()
        peer.prepare["publish"] = {"preview": PUBLISH_PREVIEW, "scopes": {}}
        return peer, make_connector(peer, defs=[PUBLISH, NOTE], **kwargs)

    @staticmethod
    def slot(data=PAGE, filename="page.html") -> str:
        from privacyfence import local_files
        from privacyfence.principal import LOCAL_PRINCIPAL
        from privacyfence.upload_staging import get_upload_staging_store
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, filename, max_bytes=100_000)
        store.fill(token, LOCAL_PRINCIPAL.id, [data])
        return "upload:" + local_files._encode_token(token)

    @staticmethod
    def context():
        from privacyfence import local_files
        return local_files.call_context(bridge_available=False, uploads={})

    def test_the_file_parameter_is_described_to_the_ai(self):
        _, conn = self.make()
        [spec] = [s for s in conn.tool_specs() if s.name == "today_publish"]
        html = next(p for p in spec.params if p.name == "html")
        assert html.annotation == "str" and html.required
        assert html.description.startswith("The page. A file, not its content:")
        assert "accepted types: text/html" in html.description

    async def test_prepare_gets_the_metadata_and_execute_the_bytes(self, audit_dir, registry, popups):
        import base64
        peer, conn = self.make()
        ref = self.slot()

        with self.context():
            await conn.call("today_publish", {"title": "Home", "html": ref})

        [prepare] = peer.calls("tool.prepare")
        [execute] = peer.calls("tool.execute")
        sha = hashlib.sha256(PAGE).hexdigest()
        meta = {"name": "page.html", "size": len(PAGE), "media_type": "text/html",
                "sniffed_type": "text/html", "sha256": sha}
        assert prepare["files"] == {"html": meta}
        assert execute["files"] == {"html": {**meta, "content_base64": base64.b64encode(PAGE).decode()}}
        assert prepare["args"] == execute["args"] == {"title": "Home"}
        assert execute["args_digest"] == args_digest({"title": "Home"})
        assert ref not in json.dumps(peer.calls("tool.prepare") + peer.calls("tool.execute"))

    async def test_the_first_card_block_is_the_file_block(self, audit_dir, registry, popups, gated_call_spy):
        peer, conn = self.make()
        ref = self.slot()

        with self.context():
            await conn.call("today_publish", {"html": ref})

        [call] = gated_call_spy
        blocks = call["preview_blocks"]
        file_fields = {b["label"]: b["value"] for b in blocks[:6]}
        assert file_fields == {
            "File": "page.html", "Source": "Upload slot", "Size": f"{len(PAGE):,} bytes",
            "Declared type": "text/html", "Detected type": "text/html",
            "SHA-256": hashlib.sha256(PAGE).hexdigest(),
        }
        assert blocks[6] == {"type": "field", "label": "Target", "value": "the site"}
        assert call["args"] == {"html": ref}

    async def test_the_slot_is_consumed_after_approval(self, audit_dir, registry, popups):
        peer, conn = self.make()
        ref = self.slot()

        with self.context():
            await conn.call("today_publish", {"html": ref})
        with self.context(), pytest.raises(LocalFileAccessError, match="expired or was already used"):
            await conn.call("today_publish", {"html": ref})

        assert len(peer.calls("tool.execute")) == 1

    async def test_a_denied_call_leaves_the_slot_and_writes_no_file_audit_row(self, monkeypatch, audit_dir, registry):
        Popups(monkeypatch, decision="deny")
        peer, conn = self.make()
        ref = self.slot()

        with self.context(), pytest.raises(GateDeniedError):
            await conn.call("today_publish", {"html": ref})

        assert peer.calls("tool.execute") == []
        assert [e for e in read_audit(audit_dir) if e["decision"] == "plugin_file"] == []
        Popups(monkeypatch, decision="accept")
        with self.context():
            await conn.call("today_publish", {"html": ref})
        assert len(peer.calls("tool.execute")) == 1

    async def test_an_approved_call_writes_one_file_audit_row(self, audit_dir, registry, popups):
        peer, conn = self.make()

        with reason_scope("publishing the home page"), self.context():
            await conn.call("today_publish", {"html": self.slot()})

        [row] = [e for e in read_audit(audit_dir) if e["decision"] == "plugin_file"]
        assert row["connector"] == "plugin:today" and row["tool"] == "today_publish"
        assert row["tool_name"] == "Publish"
        assert row["summary"] == (
            f"html: page.html; bytes={len(PAGE)}; sha256={hashlib.sha256(PAGE).hexdigest()}; type=text/html"
        )
        assert row["claude_reason"] == "publishing the home page"
        assert row["auto_accept_rule"] == "" and row["sender"] == ""
        assert PAGE.decode() not in json.dumps(row)

    async def test_a_failed_file_audit_write_does_not_fail_the_call(self, monkeypatch, caplog, registry, popups):
        def broken_logger():
            raise OSError("disk full")

        monkeypatch.setattr(plugin_connector, "get_audit_logger", broken_logger)
        peer, conn = self.make()

        with self.context():
            await conn.call("today_publish", {"html": self.slot()})

        assert len(peer.calls("tool.execute")) == 1
        assert "Audit log write failed" in caplog.text

    async def test_a_changed_local_file_gets_a_new_prepare(self, audit_dir, registry, deferred_registry, tmp_path, monkeypatch):
        from privacyfence import local_files
        monkeypatch.setattr(local_files.privilege_separation, "is_enabled", lambda: False)
        deferred_registry(hold_window=0.01, pending_ttl=5.0, ledger_ttl=5.0)
        peer, conn = self.make()
        page = tmp_path / "page.html"
        page.write_bytes(b"<html>one</html>")

        with self.context(), pytest.raises(ApprovalPending):
            await conn.call("today_publish", {"html": str(page)})
        with self.context(), pytest.raises(ApprovalPending):
            await conn.call("today_publish", {"html": str(page)})
        assert len(peer.calls("tool.prepare")) == 1

        page.write_bytes(b"<html>two</html>")
        with self.context(), pytest.raises(ApprovalPending):
            await conn.call("today_publish", {"html": str(page)})

        assert len(peer.calls("tool.prepare")) == 2
        assert [p["files"]["html"]["sha256"] for p in peer.calls("tool.prepare")] == [
            hashlib.sha256(b"<html>one</html>").hexdigest(), hashlib.sha256(b"<html>two</html>").hexdigest(),
        ]

    async def test_a_missing_required_file_is_refused(self, audit_dir, registry, popups):
        peer, conn = self.make()
        for args in ({}, {"html": ""}):
            with pytest.raises(LocalFileAccessError, match=r"Publish needs a file in html\."):
                await conn.call("today_publish", args)
        assert peer.calls("tool.prepare") == []

    async def test_an_optional_file_may_be_left_out(self, audit_dir, registry, popups):
        optional = json.loads(json.dumps(PUBLISH))
        optional["parameters"]["required"] = []
        peer = FakePeer()
        peer.prepare["publish"] = {"preview": PUBLISH_PREVIEW, "scopes": {}}
        conn = make_connector(peer, defs=[optional])

        await conn.call("today_publish", {"title": "Home"})

        [prepare] = peer.calls("tool.prepare")
        [execute] = peer.calls("tool.execute")
        assert "files" not in prepare and "files" not in execute
        assert [e for e in read_audit(audit_dir) if e["decision"] == "plugin_file"] == []

    async def test_a_tool_without_a_file_parameter_sends_no_files_key(self, audit_dir, registry, popups):
        peer, conn = self.make()

        await conn.call("today_note", {"text": "buy milk"})

        assert "files" not in peer.calls("tool.prepare")[0]
        assert "files" not in peer.calls("tool.execute")[0]

    async def test_local_files_needed_propagates_out_of_call(self, audit_dir, registry, popups):
        from privacyfence import local_files
        peer, conn = self.make()
        local_files.force_bridge_for_tests(True)

        with local_files.call_context(bridge_available=True, uploads={}):
            with pytest.raises(local_files.LocalFilesNeeded) as caught:
                await conn.call("today_publish", {"html": "~/page.html"})

        assert caught.value.paths == ["~/page.html"]
        assert peer.calls("tool.prepare") == []


async def test_every_tool_leaves_an_audit_trail(monkeypatch, tmp_path):
    conn = make_connector(FakePeer())
    await assert_all_tools_leave_an_audit_trail(conn, plugin_connector, monkeypatch, tmp_path)
