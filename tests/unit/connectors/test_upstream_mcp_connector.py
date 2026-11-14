"""Unit tests for privacyfence.connectors.upstream_mcp.UpstreamMcpConnector."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import types

from privacyfence import upstream_mcp_registry
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import upstream_mcp as module
from privacyfence.connectors.upstream_mcp import (
    UpstreamMcpConnector,
    UpstreamToolError,
    UpstreamUnavailableError,
)
from privacyfence.safe_errors import public_message
from privacyfence.upstream_claims import CallerClaim
from privacyfence.upstream_mcp_client import CallResult, UpstreamMcpClientError, UpstreamToolCatalog
from privacyfence.upstream_mcp_config import DeclaredTool, UpstreamServer

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_tool_definitions_complete

pytestmark = pytest.mark.unit

CALLER = CallerClaim(
    sub="u1", email="a@b.example", email_verified=True, domains=("general",),
    act={"type": "ai_client", "attested": False, "name": "", "claimed_name": ""},
)


def _tool(name: str, gate: str, verb: str, effect: str = "") -> DeclaredTool:
    return DeclaredTool(
        name=name, exposed_name=f"kh_{name}", operation=f"kh.{name}", gate=gate, verb=verb, effect=effect,
    )


UPSTREAM = UpstreamServer(
    id="kh", label="Knowledge hub", url="https://kh.example.com/mcp", audience="kh",
    tools=(
        _tool("search", "auto", "search"),
        _tool("get_doc", "review", "read"),
        _tool("propose_change", "popup", "add", "Adds a change proposal to the knowledge hub."),
    ),
)


def _listed(name: str) -> types.Tool:
    return types.Tool(
        name=name, description=f"The {name} tool.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )


def make_connector(result: CallResult | None = None):
    catalog = UpstreamToolCatalog(UPSTREAM)
    catalog.update([_listed(tool.name) for tool in UPSTREAM.tools])
    client = MagicMock()
    client.call_tool = AsyncMock(return_value=result or CallResult(text="hello", structured=None, dropped_blocks=0))
    caller = MagicMock(return_value=CALLER)
    return UpstreamMcpConnector(UPSTREAM, catalog, client, caller), client, caller


@pytest.fixture(autouse=True)
def registered():
    upstream_mcp_registry.register([UPSTREAM])


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return kwargs.get("filtered_data")

    monkeypatch.setattr(module, "gated_call", fake)
    return calls


def _audit_rows(tmp_path) -> list[dict]:
    path = tmp_path / f"{current_week()}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, client, _ = make_connector()
        with pytest.raises(ValueError, match="Unknown upstream tool: 'kh_nope'"):
            await connector.call("kh_nope", {})
        client.call_tool.assert_not_called()

    async def test_unconfigured_connector_knows_no_tool(self):
        with pytest.raises(ValueError, match="Unknown upstream tool"):
            await UpstreamMcpConnector().call("kh_search", {})

    def test_name_and_specs_without_an_upstream(self):
        for connector in (UpstreamMcpConnector(), UpstreamMcpConnector(MagicMock()), UpstreamMcpConnector(None)):
            assert connector.name == "upstream_mcp"
            assert connector.tool_specs() == []

    def test_name_is_the_upstream_id(self):
        connector, _, _ = make_connector()
        assert connector.name == "kh"
        assert [s.name for s in connector.tool_specs()] == ["kh_search", "kh_get_doc", "kh_propose_change"]

    async def test_a_configured_connector_without_a_client_refuses(self):
        connector = UpstreamMcpConnector(UPSTREAM, None, None, None)
        with pytest.raises(RuntimeError, match="not configured"):
            await connector.call("kh_search", {"query": "x"})

    async def test_connector_without_upstream_cannot_build_gate_fields(self):
        with pytest.raises(RuntimeError, match="not configured"):
            UpstreamMcpConnector()._gate_fields(_tool("x", "auto", "read"), {})


class TestAutoTool:
    async def test_writes_an_audit_row_and_never_gates(self, gated_call_spy, audit_dir):
        connector, client, caller = make_connector()
        result = await connector.call("kh_search", {"query": "x"})
        assert result == "hello"
        assert gated_call_spy == []
        client.call_tool.assert_awaited_once_with("search", {"query": "x"}, CALLER)
        caller.assert_called_once_with()
        (row,) = _audit_rows(audit_dir)
        assert row["connector"] == "kh"
        assert row["tool"] == "kh_search"
        assert row["tool_name"] == "Knowledge hub: search"
        assert row["decision"] == "auto_accepted"
        assert row["auto_accept_rule"] == "auto"

    async def test_audit_failure_never_blocks_the_read(self, gated_call_spy, monkeypatch):
        connector, _, _ = make_connector()
        monkeypatch.setattr(module, "get_audit_logger", MagicMock(side_effect=OSError("disk")))
        assert await connector.call("kh_search", {"query": "x"}) == "hello"

    async def test_structured_result_wins_over_text(self, gated_call_spy, audit_dir):
        connector, _, _ = make_connector(CallResult(text="t", structured={"hits": 2}, dropped_blocks=0))
        assert await connector.call("kh_search", {"query": "x"}) == {"hits": 2}


class TestReviewTool:
    async def test_gates_the_result_with_pii_scan_text(self, gated_call_spy):
        connector, client, _ = make_connector()
        result = await connector.call("kh_get_doc", {"query": "x"})
        assert result == "hello"
        (call,) = gated_call_spy
        assert call["gate"] == "review"
        assert call["connector"] == "kh"
        assert call["tool"] == "kh_get_doc"
        assert call["tool_name"] == "Knowledge hub: get_doc"
        assert call["summary"] == "Read on Knowledge hub"
        assert call["sender"] == "Knowledge hub"
        assert call["raw_data"] == "hello"
        assert call["filtered_data"] == "hello"
        assert call["pii_scan_text"] == "hello"
        assert call["details_text"] == "hello"
        assert call["args"] == {"query": "x"}
        client.call_tool.assert_awaited_once()

    async def test_structured_result_is_the_gated_value(self, gated_call_spy):
        connector, _, _ = make_connector(CallResult(text="", structured={"a": 1}, dropped_blocks=0))
        assert await connector.call("kh_get_doc", {"query": "x"}) == {"a": 1}
        (call,) = gated_call_spy
        assert call["raw_data"] == {"a": 1}
        assert call["pii_scan_text"] == json.dumps({"a": 1}, indent=2, sort_keys=True)

    async def test_text_is_scanned_when_both_text_and_structured_exist(self, gated_call_spy):
        connector, _, _ = make_connector(CallResult(text="visible", structured={"a": 1}, dropped_blocks=0))
        await connector.call("kh_get_doc", {"query": "x"})
        assert gated_call_spy[0]["pii_scan_text"] == "visible"

    async def test_upstream_failure_never_reaches_the_gate(self, gated_call_spy):
        connector, client, _ = make_connector()
        client.call_tool.side_effect = UpstreamMcpClientError("unreachable")
        with pytest.raises(UpstreamUnavailableError):
            await connector.call("kh_get_doc", {"query": "x"})
        assert gated_call_spy == []


class TestPopupTool:
    async def test_gate_runs_before_the_client(self, monkeypatch):
        connector, client, _ = make_connector()
        order: list[str] = []
        calls: list[dict] = []

        async def fake_gate(**kwargs):
            order.append("gate")
            calls.append(kwargs)

        async def fake_call(*_args):
            order.append("client")
            return CallResult(text="done", structured=None, dropped_blocks=0)

        monkeypatch.setattr(module, "gated_call", fake_gate)
        client.call_tool.side_effect = fake_call
        args = {"query": "secret title", "n": 2}
        assert await connector.call("kh_propose_change", args) == "done"
        assert order == ["gate", "client"]
        (call,) = calls
        shown = json.dumps(args, indent=2, sort_keys=True)
        assert call["gate"] == "popup"
        assert call["raw_data"] == args
        assert call["filtered_data"] is None
        assert call["details_text"] == shown
        assert call["write_content_scan_text"] == shown
        assert call["summary"] == "Add on Knowledge hub"
        assert call["args"] == args

    async def test_denial_never_reaches_the_client(self, monkeypatch):
        connector, client, _ = make_connector()
        monkeypatch.setattr(module, "gated_call", AsyncMock(side_effect=RuntimeError("Request denied by user.")))
        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("kh_propose_change", {"query": "x"})
        client.call_tool.assert_not_called()


class TestPreview:
    @pytest.mark.parametrize("tool", ["kh_get_doc", "kh_propose_change"])
    async def test_preview_names_arguments_and_holds_no_values(self, tool, gated_call_spy):
        connector, _, _ = make_connector()
        await connector.call(tool, {"title": "TOP-SECRET-VALUE", "query": "another-secret"})
        (call,) = gated_call_spy
        assert call["preview"] == {
            "Server": "Knowledge hub",
            "Tool": tool.removeprefix("kh_"),
            "Arguments": "query, title",
        }
        assert "TOP-SECRET-VALUE" not in json.dumps(call["preview"])
        assert "another-secret" not in json.dumps(call["preview"])

    async def test_preview_says_none_without_arguments(self, gated_call_spy):
        connector, _, _ = make_connector()
        await connector.call("kh_get_doc", {})
        assert gated_call_spy[0]["preview"]["Arguments"] == "none"


class TestErrors:
    @pytest.mark.parametrize(
        ("error", "kind", "message"),
        [
            (UpstreamMcpClientError("unreachable"), UpstreamUnavailableError,
             "The Knowledge hub server could not be reached."),
            (UpstreamMcpClientError("too_large"), UpstreamUnavailableError,
             "The Knowledge hub result is larger than 1 MB; ask for less."),
            (UpstreamMcpClientError("tool_error", "no such page"), UpstreamToolError,
             "The Knowledge hub server returned an error: no such page"),
        ],
    )
    async def test_each_client_error_kind_maps_to_its_type_and_message(self, error, kind, message):
        connector, client, _ = make_connector()
        client.call_tool.side_effect = error
        with pytest.raises(kind) as excinfo:
            await connector.call("kh_search", {"query": "x"})
        assert str(excinfo.value) == message
        assert type(excinfo.value) is kind
        assert public_message(excinfo.value) == message
        assert excinfo.value.__cause__ is None

    async def test_tool_error_detail_is_cleaned_and_cut(self):
        connector, client, _ = make_connector()
        client.call_tool.side_effect = UpstreamMcpClientError("tool_error", "bad\x00\x1b[31m\nthing" + "x" * 600)
        with pytest.raises(UpstreamToolError) as excinfo:
            await connector.call("kh_search", {"query": "x"})
        detail = str(excinfo.value).removeprefix("The Knowledge hub server returned an error: ")
        assert detail.startswith("bad[31mthing")
        assert len(detail) == 500
        assert not any(ord(char) < 32 for char in detail)

    def test_error_types_are_named_runtime_errors(self):
        assert issubclass(UpstreamUnavailableError, RuntimeError)
        assert issubclass(UpstreamToolError, RuntimeError)
        assert UpstreamToolError is not RuntimeError


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, _, _ = make_connector()
        await assert_all_tools_leave_an_audit_trail(
            connector, module, monkeypatch, tmp_path, {"kh_search": {"query": "x"}},
        )


class TestToolDefinitions:
    def test_tool_definitions_are_complete(self):
        connector, _, _ = make_connector()
        assert_tool_definitions_complete(connector, {})

    def test_the_reason_param_is_required_on_every_tool(self):
        connector, _, _ = make_connector()
        for spec in connector.tool_specs():
            (reason,) = [p for p in spec.params if p.name == "reason"]
            assert reason.required
            assert "reason" in spec.input_schema["required"]
