"""Tests for mcp_tools.py's ``ToolSpec`` -> MCP ``Tool``/``CallToolResult``
translation, plus ``privacyfence_begin_unattended_session`` refused when
disabled, driven over the real transport. A policy-change proposal denied
inside an unattended session is covered by
``privacyfence_propose_policy_change``'s own unattended-denial coverage, see
McpDispatcher.propose_policy_change's comment in web/mcp_dispatch.py.

Split by layer, same as the existing web/ test suite:
``TestToolSchema``/``TestCallToolResult`` exercise mcp_tools.py's own pure
functions directly -- no dispatcher, no wire protocol, since nothing else in
the suite gave that translation layer its own test module before this.
``TestUnattendedSessionBehavior`` drives the real ``/mcp`` ASGI app with the
official ``mcp`` client end to end (test_routes_mcp.py's own pattern) so the
behavior above is proven the way a real MCP client would actually observe
it, not just at whichever internal layer happens to implement it --
test_mcp_dispatch.py already covers the same branches at the
McpDispatcher-unit level in more detail.
"""
from __future__ import annotations

import contextlib
import json

import httpx2
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from privacyfence import auto_accept
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.web import mcp_tools
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.routes_mcp import build_mcp_asgi_app, mcp_lifespan


# --------------------------------------------------------------------------- #
# Tool schema assertions -- ToolSpec -> JSON Schema / mcp.types.Tool
# --------------------------------------------------------------------------- #

class TestToolInputSchema:
    def test_maps_known_annotations_to_json_schema_types(self):
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("a", "str", required=True),
            ToolParam("b", "int", required=True),
            ToolParam("c", "bool", required=True),
            ToolParam("d", "float", required=True),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["properties"]["a"]["type"] == "string"
        assert schema["properties"]["b"]["type"] == "integer"
        assert schema["properties"]["c"]["type"] == "boolean"
        assert schema["properties"]["d"]["type"] == "number"

    def test_unknown_annotation_falls_back_to_string(self):
        # Mirrors tools.ts's own `case "str": default:` fallthrough -- see
        # _param_schema's own comment.
        spec = ToolSpec(name="t", description="d", params=[ToolParam("x", "list", required=True)])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["properties"]["x"]["type"] == "string"

    def test_required_params_are_listed_optional_ones_are_not(self):
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("required_one", "str", required=True),
            ToolParam("optional_one", "str", required=False),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["required"] == ["required_one"]

    def test_no_required_key_at_all_when_every_param_is_optional(self):
        spec = ToolSpec(name="t", description="d", params=[ToolParam("x", "str", required=False)])
        schema = mcp_tools.tool_input_schema(spec)
        assert "required" not in schema

    def test_optional_params_default_only_carries_forward_when_non_none(self):
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("with_default", "str", required=False, default="fallback"),
            ToolParam("without_default", "str", required=False, default=None),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["properties"]["with_default"]["default"] == "fallback"
        assert "default" not in schema["properties"]["without_default"]

    def test_a_required_params_own_default_is_never_surfaced(self):
        # `default` is informational for the client and only makes sense
        # for a param the caller may omit -- see tool_input_schema's own
        # docstring.
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("x", "str", required=True, default="ignored"),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert "default" not in schema["properties"]["x"]

    def test_description_is_carried_when_present_and_omitted_when_blank(self):
        spec = ToolSpec(name="t", description="d", params=[
            ToolParam("documented", "str", required=True, description="explains itself"),
            ToolParam("undocumented", "str", required=True, description=""),
        ])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema["properties"]["documented"]["description"] == "explains itself"
        assert "description" not in schema["properties"]["undocumented"]

    def test_no_params_yields_an_empty_object_schema(self):
        spec = ToolSpec(name="t", description="d", params=[])
        schema = mcp_tools.tool_input_schema(spec)
        assert schema == {"type": "object", "properties": {}}


class TestToMcpTool:
    def test_carries_name_and_description_through_unchanged(self):
        spec = ToolSpec(name="gmail_send", description="Sends an email.")
        tool = mcp_tools.to_mcp_tool(spec)
        assert tool.name == "gmail_send"
        assert tool.description == "Sends an email."

    def test_every_tool_is_advertised_read_only_regardless_of_the_specs_own_flag(self):
        # MCP tool annotations are a UI hint, not the real security
        # boundary -- gate.py enforces that server-side. A write tool
        # (read_only=False) must still be advertised uniformly read-only so
        # a client doesn't throw its own redundant confirmation in front of
        # gate.py's real one (ADR 0076).
        read_tool = mcp_tools.to_mcp_tool(ToolSpec(name="r", description="d", read_only=True))
        write_tool = mcp_tools.to_mcp_tool(ToolSpec(name="w", description="d", read_only=False))
        for tool in (read_tool, write_tool):
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True

    def test_input_schema_matches_tool_input_schema_directly(self):
        spec = ToolSpec(name="t", description="d", params=[ToolParam("x", "int", required=True)])
        tool = mcp_tools.to_mcp_tool(spec)
        assert tool.input_schema == mcp_tools.tool_input_schema(spec)


class TestCallToolResult:
    def test_none_becomes_empty_content(self):
        result = mcp_tools.to_call_tool_result(None)
        assert result.content == []
        assert result.structured_content is None

    def test_a_plain_string_becomes_text_content_with_no_structured_content(self):
        result = mcp_tools.to_call_tool_result("hello")
        assert len(result.content) == 1
        assert result.content[0].text == "hello"
        assert result.structured_content is None

    def test_a_dict_becomes_both_text_and_structured_content(self):
        value = {"a": 1, "b": "two"}
        result = mcp_tools.to_call_tool_result(value)
        assert json.loads(result.content[0].text) == value
        assert result.structured_content == value

    def test_a_non_dict_json_value_becomes_text_only_no_structured_content(self):
        result = mcp_tools.to_call_tool_result([1, 2, 3])
        assert json.loads(result.content[0].text) == [1, 2, 3]
        assert result.structured_content is None

    def test_a_non_json_native_value_is_rendered_via_str_fallback(self):
        # json.dumps(..., default=str) -- anything that isn't natively
        # serializable (e.g. a set) still produces readable text instead of
        # raising.
        result = mcp_tools.to_call_tool_result({"weird": {1, 2}})
        assert "1" in result.content[0].text and "2" in result.content[0].text


def test_error_result_sets_is_error_and_carries_the_message():
    result = mcp_tools.error_result("boom")
    assert result.is_error is True
    assert result.content[0].text == "boom"


# --------------------------------------------------------------------------- #
# Meta-tool manifest -- names, required "reason", and the frozenset kept in
# sync with the actual tuple (routes_mcp.py's list_tools/dispatch both trust
# these to agree).
# --------------------------------------------------------------------------- #

class TestMetaToolManifest:
    def test_meta_tool_names_frozenset_matches_the_meta_tools_tuple_exactly(self):
        assert mcp_tools.META_TOOL_NAMES == {t.name for t in mcp_tools.META_TOOLS}
        assert len(mcp_tools.META_TOOL_NAMES) == len(mcp_tools.META_TOOLS)

    @pytest.mark.parametrize(
        "tool", [t for t in mcp_tools.META_TOOLS if t.name != mcp_tools.AWAIT_APPROVAL_TOOL.name],
        ids=lambda t: t.name,
    )
    def test_every_meta_tool_that_acts_or_discloses_requires_a_reason(self, tool: types.Tool):
        # Every meta-tool's "reason" is logged in an audit entry with no
        # popup for a human to see it in first -- see each tool's own
        # description. Schema-enforced, not just documented.
        # privacyfence_await_approval is the one exception: it's a pure
        # status poll on approvals another gated call already created (and
        # already carries its own reason) -- there's no new action or
        # disclosure here for a reason to explain.
        assert "reason" in tool.input_schema["properties"]
        assert "reason" in tool.input_schema.get("required", [])

    def test_check_policy_requires_connector_tool_and_reason(self):
        schema = mcp_tools.CHECK_POLICY_TOOL.input_schema
        assert set(schema["required"]) == {"connector", "tool", "reason"}

    def test_await_approval_requires_only_approval_ids(self):
        schema = mcp_tools.AWAIT_APPROVAL_TOOL.input_schema
        assert schema["required"] == ["approval_ids"]
        assert schema["properties"]["approval_ids"]["type"] == "array"

    def test_nothing_on_this_server_mints_a_sign_in_link_any_more(self):
        """No meta-tool mints a sign-in link (ADR 0013): ADR 0003 makes the
        companion mandatory on all three platforms, and a live session is not
        something to hand the party it governs. Asserted
        against the manifest as a whole rather than by name alone, so a
        differently-named tool that mints one lands here too."""
        assert not any("sign_in" in tool.name for tool in mcp_tools.META_TOOLS)
        assert not hasattr(mcp_tools, "GET_SIGN_IN_LINK_TOOL")
        assert not hasattr(mcp_tools, "sign_in_link_result")

    def test_status_tool_requires_only_reason(self):
        # The one meta-tool guaranteed to exist even
        # with zero connectors -- no params of its own beyond the shared
        # audited "reason", same posture as list_policy.
        schema = mcp_tools.PRIVACYFENCE_STATUS_TOOL.input_schema
        assert schema["required"] == ["reason"]
        assert set(schema["properties"]) == {"reason"}

    def test_status_tool_is_in_the_meta_tool_manifest(self):
        assert mcp_tools.PRIVACYFENCE_STATUS_TOOL in mcp_tools.META_TOOLS
        assert mcp_tools.PRIVACYFENCE_STATUS_TOOL.name in mcp_tools.META_TOOL_NAMES

    def test_check_policy_documents_matched_rule_id_in_its_description(self):
        # check_policy's result carries matched_rule_id: no schema to assert against (it's part of
        # the free-form result dict), so the description is the one place this is documented.
        assert "matched_rule_id" in mcp_tools.CHECK_POLICY_TOOL.description

    def test_list_policy_and_propose_policy_change_are_in_the_meta_tool_manifest(self):
        assert mcp_tools.LIST_POLICY_TOOL in mcp_tools.META_TOOLS
        assert mcp_tools.PROPOSE_POLICY_CHANGE_TOOL in mcp_tools.META_TOOLS

    def test_create_upload_slot_is_in_the_meta_tool_manifest(self):
        assert mcp_tools.CREATE_UPLOAD_SLOT_TOOL in mcp_tools.META_TOOLS
        assert mcp_tools.CREATE_UPLOAD_SLOT_TOOL.name in mcp_tools.META_TOOL_NAMES

    def test_create_upload_slot_requires_filename_and_reason(self):
        schema = mcp_tools.CREATE_UPLOAD_SLOT_TOOL.input_schema
        assert set(schema["required"]) == {"filename", "reason"}
        assert "size_bytes" in schema["properties"]
        assert "size_bytes" not in schema["required"]

    def test_propose_policy_change_requires_only_operation_and_reason(self):
        # rule_id/group/value/verbs are each conditionally required depending on operation --
        # gate.propose_policy_change enforces that at call time (ValueError before any popup),
        # not the schema.
        schema = mcp_tools.PROPOSE_POLICY_CHANGE_TOOL.input_schema
        assert schema["properties"]["operation"]["enum"] == ["add", "update", "remove"]
        assert set(schema["required"]) == {"operation", "reason"}
        assert schema["properties"]["value"]["type"] == "array"
        assert schema["properties"]["verbs"]["type"] == "array"

    def test_list_policy_requires_only_reason(self):
        schema = mcp_tools.LIST_POLICY_TOOL.input_schema
        assert schema["required"] == ["reason"]


# --------------------------------------------------------------------------- #
# Unattended-session behavior, driven end to end over the real /mcp
# Streamable HTTP transport (mirrors test_routes_mcp.py's own fixtures).
# --------------------------------------------------------------------------- #

TOKEN = "mcp-tools-test-token"


@contextlib.asynccontextmanager
async def _connected_session(dispatcher: McpDispatcher, *, token: str = TOKEN):
    app, session_manager = build_mcp_asgi_app(dispatcher, token=token)
    transport = httpx2.ASGITransport(app=app)
    async with mcp_lifespan(session_manager):
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver", headers={"Authorization": f"Bearer {token}"},
        ) as http_client:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=http_client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def _dispatcher(connectors: dict[str, Connector] | None = None, **kwargs) -> McpDispatcher:
    store = dict(connectors or {})
    return McpDispatcher(lambda: store, **kwargs)


def _read_audit_entries(audit_dir) -> list[dict]:
    week_file = audit_dir / f"{current_week()}.jsonl"
    if not week_file.exists():
        return []
    return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]


class TestBeginUnattendedSessionRefusedWhenDisabled:
    async def test_is_a_tool_error_naming_the_reason(self):
        dispatcher = _dispatcher({})  # unattended_sessions_enabled defaults False
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_begin_unattended_session", {"reason": "scheduled run"})
        assert result.is_error is True
        assert "disabled" in result.content[0].text
        assert "organization config" in result.content[0].text

    async def test_never_actually_marks_the_session_unattended(self):
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher):
            pass
        assert dispatcher.unattended_session_count() == 0


class TestStatusOverRealTransport:
    """End to end through the real /mcp Streamable HTTP transport -- what the
    retired sign-in-link tool's own transport test used to cover here, now
    asserting the thing that replaced it: an un-onboarded install tells the
    model to send the human to the companion, and hands over no credential."""

    async def test_an_un_onboarded_install_points_at_the_companion(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [
            {"name": "gmail", "enabled": True, "authenticated": False, "blocked_by": "not_authenticated"},
        ])
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_status", {"reason": "checking setup"})

        assert result.is_error is False
        assert result.structured_content["next_step"] == "open_privacyfence_companion"
        assert result.structured_content["sign_in_url"] is None

    async def test_the_retired_tool_is_not_callable_at_all(self):
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            listed = await session.list_tools()
            result = await session.call_tool(
                "privacyfence_get_sign_in_link", {"page": "approvals", "reason": "locked out"},
            )

        assert not any(t.name == "privacyfence_get_sign_in_link" for t in listed.tools)
        assert result.is_error is True


class TestListAndProposePolicyOverRealTransport:
    """list_policy/propose_policy_change, driven end to end over the real /mcp
    transport -- the sole surface for reading/writing auto-accept policy
    (ADR 0004)."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        from privacyfence import gate
        init_audit_logger(str(tmp_path))
        self._audit_dir = tmp_path
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(config_path))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)

    async def test_list_policy_starts_empty_with_a_real_scope_catalogue(self):
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_list_policy", {"reason": "checking"})
        assert result.is_error is False
        assert result.structured_content["rules"] == []
        assert any(g["id"] == "drive.folder" for g in result.structured_content["scope_groups"])

        entries = _read_audit_entries(self._audit_dir)
        assert entries[0]["decision"] == "policy_listed"

    async def test_propose_then_list_round_trips_the_rule_by_id(self):
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            propose_result = await session.call_tool("privacyfence_propose_policy_change", {
                "operation": "add", "reason": "Trusting the sandbox folder.",
                "group": "drive.folder", "value": ["folder1"], "verbs": ["read"],
            })
            assert propose_result.is_error is False
            rule_id = propose_result.structured_content["rule_ids"][0]

            list_result = await session.call_tool("privacyfence_list_policy", {"reason": "checking"})
        rows = list_result.structured_content["rules"]
        assert [row["id"] for row in rows] == [rule_id]

    async def test_a_verb_the_group_cannot_govern_is_a_tool_error_not_a_popup(self):
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_propose_policy_change", {
                "operation": "add", "reason": "x",
                "group": "drive.folder", "value": ["folder1"], "verbs": ["send"],
            })
        assert result.is_error is True
        assert "cannot govern" in result.content[0].text
