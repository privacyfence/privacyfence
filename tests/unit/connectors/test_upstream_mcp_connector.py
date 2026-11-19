"""Unit tests for privacyfence.connectors.upstream_mcp.UpstreamMcpConnector."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from privacyfence import upstream_mcp_registry
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import upstream_mcp as module
from privacyfence.connectors.upstream_mcp import (
    UpstreamCallRefusedError,
    UpstreamMcpConnector,
    UpstreamToolError,
    UpstreamUnavailableError,
)
from privacyfence.safe_errors import public_message
from privacyfence.upstream_mcp_client import (
    CallerNotAllowedError,
    CallResult,
    UpstreamMcpClientError,
    UpstreamToolCatalog,
)
from privacyfence.upstream_mcp_config import UpstreamOAuthSettings, UpstreamServer
from privacyfence.upstream_tools import SavedTool, ToolEntry, classify, tool_digest

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_tool_definitions_complete

pytestmark = pytest.mark.unit

UPSTREAM = UpstreamServer(id="kh", label="Knowledge hub", url="https://kh.example.com/mcp", oauth=UpstreamOAuthSettings())
SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}


def _saved(name: str, *, gate: str | None = None, read_only=None, destructive=None) -> SavedTool:
    entry = ToolEntry(name, f"The {name} tool.", SCHEMA, read_only, destructive)
    default, verb = classify(read_only, destructive)
    return SavedTool(name, entry.description, SCHEMA, read_only, destructive, gate or default, verb, tool_digest(entry))


SAVED = (
    _saved("get_doc", read_only=True),                # review
    _saved("propose_change", destructive=False),      # popup, update
    _saved("search", gate="auto", read_only=True),    # auto: still review for the person
    _saved("wipe", gate="auto"),                      # auto on a write: still popup
)


def make_connector(result: CallResult | None = None, *, upstream=UPSTREAM, saved=SAVED):
    catalog = UpstreamToolCatalog(upstream, saved)
    client = MagicMock()
    client.call_tool = AsyncMock(return_value=result or CallResult(text="hello", structured=None, dropped_blocks=0))
    admit = MagicMock()
    bearer = AsyncMock(return_value="tok")
    return UpstreamMcpConnector(upstream, catalog, client, admit=admit, bearer=bearer), client, admit, bearer


@pytest.fixture(autouse=True)
def registered():
    upstream_mcp_registry.get_registry().apply(UPSTREAM, SAVED)


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


def _forwarded(tmp_path) -> list[dict]:
    path = tmp_path / f"{current_week()}.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [row for row in rows if row["decision"] == "upstream_forwarded"]


class TestConstruction:
    @pytest.mark.parametrize("connector", [UpstreamMcpConnector(), UpstreamMcpConnector(None), UpstreamMcpConnector(MagicMock())])
    def test_a_connector_without_an_upstream_has_the_catalogue_name_and_no_tools(self, connector):
        assert connector.name == "upstream_mcp"
        assert connector.tool_specs() == []

    def test_name_and_specs_come_from_the_upstream_and_the_saved_list(self):
        connector, _, _, _ = make_connector()
        assert connector.name == "kh"
        assert [s.name for s in connector.tool_specs()] == ["kh_get_doc", "kh_propose_change", "kh_search", "kh_wipe"]

    def test_a_connector_without_an_upstream_cannot_build_gate_fields_or_audit(self):
        with pytest.raises(RuntimeError, match="not configured"):
            UpstreamMcpConnector()._gate_fields(_saved("x"), {})
        with pytest.raises(RuntimeError, match="not configured"):
            UpstreamMcpConnector()._require_upstream()


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, client, _, _ = make_connector()
        with pytest.raises(ValueError, match="Unknown upstream tool: 'kh_nope'"):
            await connector.call("kh_nope", {})
        client.call_tool.assert_not_called()

    async def test_an_unconfigured_connector_knows_no_tool(self):
        with pytest.raises(ValueError, match="Unknown upstream tool"):
            await UpstreamMcpConnector().call("kh_search", {})

    async def test_a_connector_without_admit_refuses(self, gated_call_spy):
        connector, client, _, bearer = make_connector()
        bare = UpstreamMcpConnector(UPSTREAM, connector._catalog, client, bearer=bearer)
        with pytest.raises(RuntimeError, match="not configured"):
            await bare.call("kh_get_doc", {"query": "x"})
        client.call_tool.assert_not_called()

    async def test_a_connector_without_a_client_or_bearer_refuses(self, gated_call_spy):
        catalog = UpstreamToolCatalog(UPSTREAM, SAVED)
        for client, bearer in ((None, AsyncMock()), (MagicMock(), None)):
            connector = UpstreamMcpConnector(UPSTREAM, catalog, client, admit=MagicMock(), bearer=bearer)
            with pytest.raises(RuntimeError, match="not configured"):
                await connector.call("kh_get_doc", {"query": "x"})


class TestReview:
    async def test_forwards_then_gates_the_result_with_the_pii_scan_text(self, gated_call_spy):
        connector, client, admit, bearer = make_connector()
        assert await connector.call("kh_get_doc", {"query": "x"}) == "hello"
        admit.assert_called_once_with()
        client.call_tool.assert_awaited_once_with("get_doc", {"query": "x"}, bearer=bearer)
        (call,) = gated_call_spy
        assert call["gate"] == "review"
        assert call["connector"] == "kh"
        assert call["tool"] == "kh_get_doc"
        assert call["tool_name"] == "Knowledge hub: get_doc"
        assert call["summary"] == "Read on Knowledge hub"
        assert call["sender"] == "Knowledge hub"
        assert call["raw_data"] == call["filtered_data"] == "hello"
        assert call["pii_scan_text"] == call["details_text"] == "hello"
        assert call["args"] == {"query": "x"}

    async def test_the_scan_covers_structured_content(self, gated_call_spy):
        connector, _, _, _ = make_connector(CallResult(text="visible", structured={"b": 2, "a": 1}, dropped_blocks=0))
        assert await connector.call("kh_get_doc", {"query": "x"}) == {"b": 2, "a": 1}
        (call,) = gated_call_spy
        assert call["pii_scan_text"] == 'visible\n{"a": 1, "b": 2}'
        assert call["details_text"] == "visible"

    async def test_a_structured_only_result_is_scanned_and_shown_as_json(self, gated_call_spy):
        connector, _, _, _ = make_connector(CallResult(text="", structured={"a": 1}, dropped_blocks=0))
        await connector.call("kh_get_doc", {"query": "x"})
        (call,) = gated_call_spy
        assert call["pii_scan_text"] == '{"a": 1}'
        assert call["details_text"] == '{\n  "a": 1\n}'

    async def test_an_upstream_failure_never_reaches_the_gate(self, gated_call_spy):
        connector, client, _, _ = make_connector()
        client.call_tool.side_effect = UpstreamMcpClientError("unreachable")
        with pytest.raises(UpstreamUnavailableError):
            await connector.call("kh_get_doc", {"query": "x"})
        assert gated_call_spy == []


class TestPopup:
    async def test_the_gate_runs_before_the_client(self, monkeypatch):
        connector, client, _, _ = make_connector()
        order: list[str] = []
        calls: list[dict] = []

        async def fake_gate(**kwargs):
            order.append("gate")
            calls.append(kwargs)

        async def fake_call(*_args, **_kwargs):
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
        assert call["raw_data"] == args and call["filtered_data"] is None
        assert call["details_text"] == call["write_content_scan_text"] == shown
        assert call["summary"] == "Update on Knowledge hub"
        assert call["args"] == args

    async def test_a_denial_never_reaches_the_client(self, monkeypatch):
        connector, client, _, _ = make_connector()
        monkeypatch.setattr(module, "gated_call", AsyncMock(side_effect=RuntimeError("Request denied by user.")))
        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("kh_propose_change", {"query": "x"})
        client.call_tool.assert_not_called()


class TestAutoConfiguredToolsStillGate:
    @pytest.mark.parametrize(("tool", "gate"), [("kh_search", "review"), ("kh_wipe", "popup")])
    async def test_a_tool_configured_auto_goes_through_gated_call_with_its_class_default(self, tool, gate, gated_call_spy):
        connector, _, _, _ = make_connector()
        await connector.call(tool, {"query": "x"})
        (call,) = gated_call_spy
        assert call["gate"] == gate


class TestPreview:
    @pytest.mark.parametrize("tool", ["kh_get_doc", "kh_propose_change"])
    async def test_the_preview_names_arguments_and_holds_no_values(self, tool, gated_call_spy):
        connector, _, _, _ = make_connector()
        await connector.call(tool, {"title": "TOP-SECRET-VALUE", "query": "another-secret"})
        (call,) = gated_call_spy
        assert call["preview"] == {
            "Server": "Knowledge hub", "Tool": tool.removeprefix("kh_"), "Arguments": "query, title",
        }
        assert "TOP-SECRET-VALUE" not in json.dumps(call["preview"])

    async def test_the_preview_says_none_without_arguments(self, gated_call_spy):
        connector, _, _, _ = make_connector()
        await connector.call("kh_get_doc", {})
        assert gated_call_spy[0]["preview"]["Arguments"] == "none"


class TestErrors:
    @pytest.mark.parametrize(
        ("kind", "message"),
        [
            ("not_connected", "Knowledge hub is not connected. Connect Knowledge hub on the Connect page (/connect)."),
            ("auth_expired", "The Knowledge hub sign-in has expired or was revoked. Connect Knowledge hub on the "
                             "Connect page (/connect) to sign in again."),
            ("insufficient_scope", "Knowledge hub needs more access than PrivacyFence was granted. Connect "
                                   "Knowledge hub again on the Connect page (/connect)."),
            ("unreachable", "The Knowledge hub server could not be reached."),
            ("too_large", "The Knowledge hub result is larger than 1 MB; ask for less."),
        ],
    )
    async def test_each_client_error_kind_has_its_message(self, kind, message):
        connector, client, _, _ = make_connector()
        client.call_tool.side_effect = UpstreamMcpClientError(kind)
        with pytest.raises(UpstreamUnavailableError) as excinfo:
            await connector.call("kh_search", {"query": "x"})
        assert str(excinfo.value) == message
        assert public_message(excinfo.value) == message
        assert excinfo.value.__cause__ is None

    async def test_a_server_error_result_is_a_tool_error_cleaned_and_cut(self):
        connector, client, _, _ = make_connector()
        client.call_tool.side_effect = UpstreamMcpClientError("tool_error", "bad\x00\x1b[31m\nthing" + "x" * 600)
        with pytest.raises(UpstreamToolError) as excinfo:
            await connector.call("kh_search", {"query": "x"})
        detail = str(excinfo.value).removeprefix("The Knowledge hub server returned an error: ")
        assert detail.startswith("bad[31mthing")
        assert len(detail) == 500
        assert not any(ord(char) < 32 for char in detail)
        assert public_message(excinfo.value) == str(excinfo.value)

    async def test_a_caller_that_is_not_allowed_is_refused_before_any_card_or_request(self, gated_call_spy):
        connector, client, admit, _ = make_connector()
        admit.side_effect = CallerNotAllowedError("n8n_workflow")
        for tool in ("kh_get_doc", "kh_propose_change"):
            with pytest.raises(UpstreamCallRefusedError) as excinfo:
                await connector.call(tool, {"query": "x"})
            assert str(excinfo.value) == "PrivacyFence does not allow this caller to use the Knowledge hub server."
            assert public_message(excinfo.value) == str(excinfo.value)
        client.call_tool.assert_not_called()
        assert gated_call_spy == []

    def test_the_error_types_are_named_runtime_errors(self):
        for kind in (UpstreamCallRefusedError, UpstreamUnavailableError, UpstreamToolError):
            assert issubclass(kind, RuntimeError) and kind is not RuntimeError


class TestEveryToolIsAudited:
    async def test_every_saved_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, _, _, _ = make_connector()
        await assert_all_tools_leave_an_audit_trail(connector, module, monkeypatch, tmp_path, {})


SECRET_TOKEN = "eyJhbGciOiJSUzI1NiJ9.payload-secret.sig-secret"
SECRET_ARG = "needle-argument-value"


class TestForwardedAudit:
    @pytest.mark.parametrize(("tool", "gate"), [("kh_get_doc", "review"), ("kh_propose_change", "popup"), ("kh_search", "review")])
    async def test_the_person_gate_and_auth_are_recorded_in_metadata(self, audit_dir, gated_call_spy, tool, gate):
        connector, _, _, _ = make_connector()
        await connector.call(tool, {"query": "x"})
        (row,) = _forwarded(audit_dir)
        assert row["metadata"] == {"upstream": "kh", "gate": gate, "auth": "oauth"}
        assert row["upstream_status"] == "ok"

    async def test_the_row_fields(self, audit_dir, gated_call_spy):
        connector, _, _, _ = make_connector()
        await connector.call("kh_get_doc", {"query": "x"})
        (row,) = _forwarded(audit_dir)
        assert row["connector"] == "kh"
        assert row["tool"] == "kh_get_doc"
        assert row["tool_name"] == "Knowledge hub: get_doc"
        assert row["summary"] == "Forwarded get_doc to Knowledge hub"
        assert row["sender"] == "Knowledge hub"
        assert row["auto_accept_rule"] == "" and row["request_id"] == ""

    @pytest.mark.parametrize("kind", ["not_connected", "auth_expired", "insufficient_scope", "unreachable", "too_large", "tool_error"])
    async def test_a_client_error_kind_is_the_status(self, kind, audit_dir, gated_call_spy):
        connector, client, _, _ = make_connector()
        client.call_tool.side_effect = UpstreamMcpClientError(kind, "detail")
        with pytest.raises(RuntimeError):
            await connector.call("kh_get_doc", {"query": "x"})
        (row,) = _forwarded(audit_dir)
        assert row["upstream_status"] == kind

    async def test_a_refused_call_is_recorded_as_refused(self, audit_dir, gated_call_spy):
        connector, _, admit, _ = make_connector()
        admit.side_effect = CallerNotAllowedError("n8n_workflow")
        with pytest.raises(UpstreamCallRefusedError):
            await connector.call("kh_get_doc", {"query": "x"})
        (row,) = _forwarded(audit_dir)
        assert row["upstream_status"] == "refused"

    async def test_review_and_popup_both_write_the_forwarded_entry(self, gated_call_spy, audit_dir):
        connector, _, _, _ = make_connector()
        await connector.call("kh_get_doc", {"query": "x"})
        await connector.call("kh_propose_change", {"query": "x"})
        assert [row["tool"] for row in _forwarded(audit_dir)] == ["kh_get_doc", "kh_propose_change"]
        assert [call["gate"] for call in gated_call_spy] == ["review", "popup"]

    async def test_no_token_or_argument_value_reaches_the_log_or_audit(self, audit_dir, caplog, gated_call_spy):
        connector, client, _, _ = make_connector()

        async def fake_call(tool, args, *, bearer):
            assert await bearer(False) == "tok"
            raise UpstreamMcpClientError("tool_error", "upstream text")

        client.call_tool.side_effect = fake_call
        caplog.set_level("DEBUG")
        with pytest.raises(UpstreamToolError):
            await connector.call("kh_get_doc", {"query": SECRET_ARG})
        raw = "\n".join(path.read_text(encoding="utf-8") for path in audit_dir.glob("*.jsonl"))
        logs = "\n".join(record.getMessage() for record in caplog.records)
        for secret in (SECRET_TOKEN, "tok\"", SECRET_ARG, "upstream text"):
            assert secret not in raw
            assert secret not in logs

    async def test_an_audit_write_that_raises_changes_nothing(self, gated_call_spy, monkeypatch):
        connector, client, _, _ = make_connector()
        monkeypatch.setattr(module, "get_audit_logger", MagicMock(side_effect=OSError("disk")))
        assert await connector.call("kh_get_doc", {"query": "x"}) == "hello"
        client.call_tool.side_effect = UpstreamMcpClientError("unreachable")
        with pytest.raises(UpstreamUnavailableError):
            await connector.call("kh_get_doc", {"query": "x"})


class TestToolDefinitions:
    def test_tool_definitions_are_complete(self):
        connector, _, _, _ = make_connector()
        assert_tool_definitions_complete(connector, {})

    def test_the_reason_param_is_required_on_every_tool(self):
        connector, _, _, _ = make_connector()
        for spec in connector.tool_specs():
            (reason,) = [p for p in spec.params if p.name == "reason"]
            assert reason.required and "reason" in spec.input_schema["required"]
