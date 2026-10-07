"""Protocol validators fail closed on malformed plugin output and never echo what a plugin sent.

Every ``from_wire`` accepts the documented transcripts, rejects each malformed variant with a typed
``RpcError`` and ignores unknown keys. The published JSON schema describes the same property names as
the dataclasses handle, so the two cannot drift.
"""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

from privacyfence.plugins import constants as c
from privacyfence.plugins import protocol as p
from privacyfence.principal import LOCAL_PRINCIPAL, Principal

pytestmark = pytest.mark.unit

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "docs" / "plugin-protocol" / "protocol.schema.json"


def _tool(**over):
    base = {
        "name": "get_agenda",
        "description": "Today's agenda.",
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
        "destructive": False,
        "gate": "review",
        "scopes": ["calendar"],
    }
    base.update(over)
    return base


def _raises(code, fn, *args, **kwargs):
    with pytest.raises(p.RpcError) as info:
        fn(*args, **kwargs)
    assert info.value.code == code
    return info.value


class TestRpcError:
    def test_to_error_shape(self):
        err = p.RpcError("unknown_tool", "no such tool", retryable=True, extra={"reason": "x"})
        assert err.to_error() == {
            "code": -32008,
            "message": "unknown_tool",
            "data": {"code": "unknown_tool", "detail": "no such tool", "retryable": True, "reason": "x"},
        }

    def test_extra_cannot_override_the_envelope(self):
        err = p.RpcError("timeout", "slow", extra={"code": "evil", "detail": "evil", "retryable": True})
        assert err.to_error()["data"] == {"code": "timeout", "detail": "slow", "retryable": False}

    def test_unknown_name_travels_as_internal_error(self):
        error = p.RpcError("something_new").to_error()
        assert error["code"] == c.ERROR_CODES["internal_error"]
        assert error["data"]["code"] == "something_new"

    def test_str_includes_code_and_detail(self):
        assert str(p.RpcError("timeout", "slow")) == "timeout: slow"
        assert str(p.RpcError("timeout")) == "timeout"

    def test_is_an_exception_with_defaults(self):
        err = p.RpcError("internal_error")
        assert (err.detail, err.retryable, err.extra) == ("", False, {})


class TestPrincipalContext:
    WIRE = {"id": "local", "display_name": "", "storage_dir": "/data/p"}

    def test_valid_round_trip(self):
        ctx = p.PrincipalContext.from_wire(self.WIRE)
        assert ctx.to_wire() == self.WIRE

    def test_unknown_keys_are_ignored(self):
        assert p.PrincipalContext.from_wire({**self.WIRE, "extra": 1}).id == "local"

    def test_roles_in_local_mode_is_org_only(self):
        _raises("org_only_field", p.PrincipalContext.from_wire, {**self.WIRE, "roles": ["admin"]})

    def test_roles_in_org_mode(self):
        ctx = p.PrincipalContext.from_wire({**self.WIRE, "roles": ["admin"]}, mode="org")
        assert ctx.roles == ("admin",)
        assert ctx.to_wire()["roles"] == ["admin"]

    @pytest.mark.parametrize(
        "bad",
        [
            "x",
            {"display_name": "", "storage_dir": "/d"},
            {"id": "", "display_name": "", "storage_dir": "/d"},
            {"id": 1, "display_name": "", "storage_dir": "/d"},
            {"id": "a", "storage_dir": "/d"},
            {"id": "a", "display_name": "", "storage_dir": ""},
        ],
    )
    def test_invalid(self, bad):
        _raises("invalid_params", p.PrincipalContext.from_wire, bad)

    def test_unknown_mode_is_a_programming_error(self):
        with pytest.raises(ValueError):
            p.PrincipalContext.from_wire(self.WIRE, mode="cloud")


class TestToolDef:
    def test_valid_round_trip(self):
        wire = _tool(effect="Sends it.", title="Agenda")
        tool = p.ToolDef.from_wire(wire)
        assert tool.to_wire() == wire

    def test_optional_fields_are_omitted_when_absent(self):
        wire = p.ToolDef.from_wire(_tool(scopes=[])).to_wire()
        assert "effect" not in wire and "title" not in wire
        assert wire["scopes"] == []

    def test_scopes_default_to_empty(self):
        wire = _tool()
        del wire["scopes"]
        assert p.ToolDef.from_wire(wire).scopes == ()

    @pytest.mark.parametrize(
        "over",
        [
            {"name": "Bad-Name"},
            {"name": 3},
            {"description": ""},
            {"description": "x" * (c.MAX_DESCRIPTION_CHARS + 1)},
            {"parameters": []},
            {"read_only": "yes"},
            {"destructive": None},
            {"gate": "never"},
            {"gate": 1},
            {"scopes": "calendar"},
            {"scopes": ["Bad-Scope"]},
            {"scopes": [1]},
            {"effect": "x" * (c.MAX_EFFECT_CHARS + 1)},
            {"title": "x" * (c.MAX_TITLE_CHARS + 1)},
            {"title": ""},
        ],
    )
    def test_invalid(self, over):
        _raises("invalid_params", p.ToolDef.from_wire, _tool(**over))

    @pytest.mark.parametrize("key", ["name", "description", "parameters", "read_only", "destructive", "gate"])
    def test_missing_required_key(self, key):
        wire = _tool()
        del wire[key]
        err = _raises("invalid_params", p.ToolDef.from_wire, wire)
        assert key in err.detail

    def test_non_object(self):
        _raises("invalid_params", p.ToolDef.from_wire, [])

    def test_limits_are_inclusive(self):
        wire = _tool(
            description="x" * c.MAX_DESCRIPTION_CHARS, effect="e" * c.MAX_EFFECT_CHARS, title="t" * c.MAX_TITLE_CHARS
        )
        assert p.ToolDef.from_wire(wire).title == "t" * c.MAX_TITLE_CHARS

    def test_detail_never_echoes_the_value(self):
        err = _raises("invalid_params", p.ToolDef.from_wire, _tool(name="SECRET-VALUE"))
        assert "SECRET" not in err.detail


class TestInitializeResult:
    WIRE = {
        "protocol_version": "1.0.0",
        "plugin": {"name": "today", "version": "1.2.0"},
        "scope_types": [{"name": "calendar", "description": "A calendar id"}],
        "tools": [_tool()],
    }

    def test_valid_round_trip(self):
        result = p.InitializeResult.from_wire(self.WIRE)
        assert result.plugin_name == "today"
        assert result.to_wire() == self.WIRE

    def test_scope_types_default_to_empty(self):
        wire = {k: v for k, v in self.WIRE.items() if k != "scope_types"}
        assert p.InitializeResult.from_wire(wire).scope_types == []

    def test_scope_type_description_is_optional(self):
        wire = {**self.WIRE, "scope_types": [{"name": "calendar"}]}
        assert p.InitializeResult.from_wire(wire).scope_types == [{"name": "calendar", "description": ""}]

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda w: w.pop("protocol_version"),
            lambda w: w.pop("plugin"),
            lambda w: w.pop("tools"),
            lambda w: w.update(plugin="today"),
            lambda w: w.update(plugin={"name": "today"}),
            lambda w: w.update(plugin={"version": "1"}),
            lambda w: w.update(protocol_version=""),
            lambda w: w.update(tools={}),
            lambda w: w.update(tools=[{"name": "x"}]),
            lambda w: w.update(scope_types="x"),
            lambda w: w.update(scope_types=[{"description": "d"}]),
            lambda w: w.update(scope_types=[{"name": "Bad"}]),
            lambda w: w.update(scope_types=[{"name": "ok", "description": 3}]),
            lambda w: w.update(scope_types=["ok"]),
        ],
    )
    def test_invalid(self, mutate):
        wire = copy.deepcopy(self.WIRE)
        mutate(wire)
        _raises("invalid_params", p.InitializeResult.from_wire, wire)

    def test_non_object(self):
        _raises("invalid_params", p.InitializeResult.from_wire, "x")


class TestPrepareResult:
    WIRE = {
        "preview": [{"type": "heading", "text": "Send"}],
        "payload": [{"type": "text", "text": "hello"}],
        "scopes": {"calendar": ["primary"]},
    }

    def test_valid_round_trip(self):
        result = p.PrepareResult.from_wire(self.WIRE)
        assert result.to_wire() == self.WIRE

    def test_payload_and_scopes_are_optional(self):
        result = p.PrepareResult.from_wire({"preview": []})
        assert result.payload is None and result.scopes == {}
        assert result.to_wire() == {"preview": [], "scopes": {}}

    def test_null_payload_is_absent(self):
        assert p.PrepareResult.from_wire({"preview": [], "payload": None}).payload is None

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda w: w.pop("preview"),
            lambda w: w.update(preview="x"),
            lambda w: w.update(preview=["x"]),
            lambda w: w.update(payload={"type": "text"}),
            lambda w: w.update(payload=[1]),
            lambda w: w.update(scopes=[]),
            lambda w: w.update(scopes={"Bad-Type": []}),
            lambda w: w.update(scopes={"calendar": "primary"}),
            lambda w: w.update(scopes={"calendar": [""]}),
            lambda w: w.update(scopes={"calendar": ["x" * (c.MAX_SCOPE_VALUE_CHARS + 1)]}),
            lambda w: w.update(scopes={"calendar": ["v"] * (c.MAX_SCOPE_VALUES + 1)}),
        ],
    )
    def test_invalid(self, mutate):
        wire = copy.deepcopy(self.WIRE)
        mutate(wire)
        _raises("invalid_params", p.PrepareResult.from_wire, wire)

    def test_payload_over_the_inline_limit(self):
        wire = {"preview": [], "payload": [{"type": "text", "text": "x" * c.INLINE_RESULT_BYTES}]}
        _raises("payload_too_large", p.PrepareResult.from_wire, wire)

    def test_validate_blocks_is_called_with_the_right_caps(self):
        seen = []

        def validator(blocks, *, max_bytes):
            seen.append((blocks, max_bytes))
            return [{"type": "text", "text": "clean"}]

        result = p.PrepareResult.from_wire(self.WIRE, validate_blocks=validator)
        assert [m for _, m in seen] == [c.MAX_PREVIEW_BYTES, None]
        assert result.preview == result.payload == [{"type": "text", "text": "clean"}]

    def test_validator_failure_is_invalid_blocks(self):
        def validator(blocks, *, max_bytes):
            raise ValueError("unknown block type")

        err = _raises("invalid_blocks", p.PrepareResult.from_wire, self.WIRE, validate_blocks=validator)
        assert err.detail == "unknown block type"

    def test_validator_failure_on_the_payload(self):
        def validator(blocks, *, max_bytes):
            if max_bytes is None:
                raise ValueError("bad payload")
            return blocks

        _raises("invalid_blocks", p.PrepareResult.from_wire, self.WIRE, validate_blocks=validator)


class TestExecuteResult:
    def test_valid_round_trip(self):
        wire = {"result": {"ok": True}, "approval_id": "a1"}
        assert p.ExecuteResult.from_wire(wire).to_wire() == wire

    def test_result_may_be_missing_and_approval_id_omitted(self):
        result = p.ExecuteResult.from_wire({})
        assert result.to_wire() == {"result": None}

    @pytest.mark.parametrize("bad", ["x", {"approval_id": 3}, {"approval_id": ""}])
    def test_invalid(self, bad):
        _raises("invalid_params", p.ExecuteResult.from_wire, bad)


class TestSourceCallParams:
    WIRE = {"principal": "local", "operation": "jira.search", "params": {"jql": "x"}}

    def test_valid_round_trip(self):
        assert p.SourceCallParams.from_wire(self.WIRE).to_wire() == self.WIRE

    def test_params_default_to_empty(self):
        wire = {"principal": "local", "operation": "jira.search"}
        assert p.SourceCallParams.from_wire(wire).params == {}

    def test_unknown_operation_is_left_to_the_handler(self):
        wire = {"principal": "local", "operation": "slack.anything"}
        assert p.SourceCallParams.from_wire(wire).operation == "slack.anything"

    def test_credential_in_local_mode_is_org_only(self):
        _raises("org_only_field", p.SourceCallParams.from_wire, {**self.WIRE, "credential": {"token": "t"}})

    def test_credential_in_org_mode(self):
        params = p.SourceCallParams.from_wire({**self.WIRE, "credential": {"k": 1}}, mode="org")
        assert params.to_wire()["credential"] == {"k": 1}

    def test_org_only_error_does_not_echo_the_credential(self):
        err = _raises("org_only_field", p.SourceCallParams.from_wire, {**self.WIRE, "credential": "hunter2"})
        assert "hunter2" not in err.detail

    @pytest.mark.parametrize(
        "bad",
        [
            [],
            {"operation": "jira.search"},
            {"principal": "local"},
            {"principal": "", "operation": "x"},
            {"principal": "local", "operation": 3},
            {"principal": "local", "operation": "x", "params": []},
        ],
    )
    def test_invalid(self, bad):
        _raises("invalid_params", p.SourceCallParams.from_wire, bad)


class TestConfirmRequestParams:
    WIRE = {
        "principal": "local",
        "kind": "export",
        "title": "Export the report",
        "preview": [{"type": "text", "text": "rows"}],
        "require_step_up": False,
    }

    def test_valid_round_trip(self):
        assert p.ConfirmRequestParams.from_wire(self.WIRE).to_wire() == self.WIRE

    def test_step_up_defaults_to_true(self):
        wire = {k: v for k, v in self.WIRE.items() if k != "require_step_up"}
        assert p.ConfirmRequestParams.from_wire(wire).require_step_up is True

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda w: w.pop("principal"),
            lambda w: w.pop("kind"),
            lambda w: w.pop("title"),
            lambda w: w.pop("preview"),
            lambda w: w.update(title=""),
            lambda w: w.update(title="t" * (c.MAX_TITLE_CHARS + 1)),
            lambda w: w.update(kind=""),
            lambda w: w.update(preview={}),
            lambda w: w.update(preview=[1]),
            lambda w: w.update(require_step_up="yes"),
        ],
    )
    def test_invalid(self, mutate):
        wire = copy.deepcopy(self.WIRE)
        mutate(wire)
        _raises("invalid_params", p.ConfirmRequestParams.from_wire, wire)

    def test_validator_failure_is_invalid_blocks(self):
        def validator(blocks, *, max_bytes):
            raise ValueError("nope")

        _raises("invalid_blocks", p.ConfirmRequestParams.from_wire, self.WIRE, validate_blocks=validator)

    def test_validator_result_is_used(self):
        result = p.ConfirmRequestParams.from_wire(self.WIRE, validate_blocks=lambda b, *, max_bytes: [])
        assert result.preview == []


class TestWebResponse:
    WIRE = {"status": 200, "headers": {"content-type": "text/html"}, "body": "<p>hi</p>", "body_encoding": "utf8"}

    def test_valid_round_trip(self):
        assert p.WebResponse.from_wire(self.WIRE).to_wire() == self.WIRE

    def test_headers_and_encoding_default(self):
        result = p.WebResponse.from_wire({"status": 204, "body": ""})
        assert result.headers == {} and result.body_encoding == "utf8"

    def test_base64_body(self):
        body = base64.b64encode(b"\x00\x01").decode()
        assert p.WebResponse.from_wire({"status": 200, "body": body, "body_encoding": "base64"}).body == body

    def test_status_outside_http_range_is_left_to_the_daemon(self):
        assert p.WebResponse.from_wire({"status": 999, "body": ""}).status == 999

    @pytest.mark.parametrize(
        "bad",
        [
            "x",
            {"body": ""},
            {"status": 200},
            {"status": "200", "body": ""},
            {"status": True, "body": ""},
            {"status": 200, "body": 3},
            {"status": 200, "body": "", "headers": []},
            {"status": 200, "body": "", "headers": {"a": 1}},
            {"status": 200, "body": "", "body_encoding": "latin1"},
            {"status": 200, "body": "***", "body_encoding": "base64"},
        ],
    )
    def test_invalid(self, bad):
        _raises("invalid_params", p.WebResponse.from_wire, bad)

    def test_body_over_the_limit(self):
        _raises(
            "payload_too_large",
            p.WebResponse.from_wire,
            {"status": 200, "body": "x" * (c.MAX_PAGE_BODY_BYTES + 1)},
        )

    def test_base64_body_is_measured_decoded(self):
        body = base64.b64encode(b"x" * (c.MAX_PAGE_BODY_BYTES + 1)).decode()
        _raises("payload_too_large", p.WebResponse.from_wire, {"status": 200, "body": body, "body_encoding": "base64"})


class TestArgsDigest:
    def test_stable_under_key_order(self):
        assert p.args_digest({"a": 1, "b": [1, 2], "c": {"x": 1, "y": 2}}) == p.args_digest(
            {"c": {"y": 2, "x": 1}, "b": [1, 2], "a": 1}
        )

    def test_known_value(self):
        assert p.args_digest({}) == "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"

    def test_differs_on_content_and_keeps_non_ascii(self):
        assert p.args_digest({"a": "é"}) != p.args_digest({"a": "e"})
        assert p.args_digest({"a": "é"}).startswith("sha256:")


class TestPrincipalContextBuilder:
    def test_local_omits_roles(self):
        assert p.principal_context(LOCAL_PRINCIPAL, Path("/data/local")) == {
            "id": "local",
            "display_name": "",
            "storage_dir": str(Path("/data/local")),
        }

    def test_org_adds_roles(self):
        admin = Principal(id="u1", display_name="Ada", is_admin=True)
        user = Principal(id="u2", display_name="Bob")
        assert p.principal_context(admin, Path("/d"), mode="org")["roles"] == ["admin"]
        assert p.principal_context(user, Path("/d"), mode="org")["roles"] == []

    def test_result_parses_back(self):
        wire = p.principal_context(LOCAL_PRINCIPAL, Path("/d"))
        assert p.PrincipalContext.from_wire(wire).id == "local"

    def test_unknown_mode(self):
        with pytest.raises(ValueError):
            p.principal_context(LOCAL_PRINCIPAL, Path("/d"), mode="x")


@pytest.fixture(scope="module")
def schema():
    with SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


class TestSchema:
    PROTOCOL_DEFS = [
        "InitializeParams", "InitializeResult", "ToolPrepareParams", "ToolPrepareResult", "ToolExecuteParams",
        "ToolExecuteResult", "SourceCallParams", "SourceCallResult", "ConfirmRequestParams",
        "ConfirmRequestResult", "ConfirmAwaitParams", "ConfirmAwaitResult", "WebRequestParams",
        "WebRequestResult", "StoragePurgeParams", "StoragePurgeResult", "ToolsChangedParams",
        "ConnectorStateChangedParams", "PrincipalRemovedParams", "PluginDisablingParams", "ShutdownParams",
    ]

    def test_declares_2020_12(self, schema):
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    @pytest.mark.parametrize("name", PROTOCOL_DEFS + ["PrincipalContext", "ToolDef", "Block", "Error", "Manifest"])
    def test_every_listed_def_exists(self, schema, name):
        assert name in schema["$defs"]

    @pytest.mark.parametrize(
        ("name", "cls"),
        [
            ("PrincipalContext", p.PrincipalContext),
            ("ToolDef", p.ToolDef),
            ("InitializeResult", p.InitializeResult),
            ("ToolPrepareResult", p.PrepareResult),
            ("ToolExecuteResult", p.ExecuteResult),
            ("SourceCallParams", p.SourceCallParams),
            ("ConfirmRequestParams", p.ConfirmRequestParams),
            ("WebRequestResult", p.WebResponse),
        ],
    )
    def test_def_properties_match_the_dataclass(self, schema, name, cls):
        assert set(schema["$defs"][name]["properties"]) == set(cls.WIRE_KEYS)

    @pytest.mark.parametrize(
        ("name", "cls"),
        [
            ("PrincipalContext", p.PrincipalContext),
            ("ToolDef", p.ToolDef),
            ("InitializeResult", p.InitializeResult),
            ("ToolPrepareResult", p.PrepareResult),
            ("ToolExecuteResult", p.ExecuteResult),
            ("SourceCallParams", p.SourceCallParams),
            ("ConfirmRequestParams", p.ConfirmRequestParams),
            ("WebRequestResult", p.WebResponse),
        ],
    )
    def test_required_keys_are_all_handled(self, schema, name, cls):
        assert set(schema["$defs"][name].get("required", [])) <= set(cls.WIRE_KEYS)

    def test_block_variants_and_manifest_are_closed(self, schema):
        variants = schema["$defs"]["Block"]["oneOf"]
        assert [v["properties"]["type"]["const"] for v in variants] == list(c.BLOCK_TYPES)
        assert all(v["additionalProperties"] is False for v in variants)
        assert schema["$defs"]["Manifest"]["additionalProperties"] is False

    def test_error_codes_match_the_constants(self, schema):
        error = schema["$defs"]["Error"]["properties"]
        assert set(error["message"]["enum"]) == set(c.ERROR_CODES)
        assert set(error["code"]["enum"]) == set(c.ERROR_CODES.values())

    def test_x_limits_match_the_constants(self, schema):
        limits = schema["x-limits"]
        for name, value in limits.items():
            assert getattr(c, name) == value, name
        assert limits["TIMEOUT_SECONDS"] == c.TIMEOUT_SECONDS
        assert schema["x-protocol-version"] == c.PROTOCOL_VERSION

    def test_manifest_properties(self, schema):
        assert set(schema["$defs"]["Manifest"]["properties"]) == {
            "name", "display_name", "version", "protocol", "command", "source_operations", "tools",
            "max_gate_floor", "pages", "service_credentials",
        }

    def test_documented_example_transcripts_validate(self):
        p.InitializeResult.from_wire(TestInitializeResult.WIRE)
        p.PrepareResult.from_wire(TestPrepareResult.WIRE)
        p.WebResponse.from_wire(TestWebResponse.WIRE)
