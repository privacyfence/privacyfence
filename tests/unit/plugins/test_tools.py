"""A plugin's tool list is validated as a whole, with floors the plugin cannot talk its way past."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from privacyfence.plugins import constants as c
from privacyfence.plugins.protocol import ToolDef
from privacyfence.plugins.tools import (
    ToolDefError,
    tool_signature,
    validate_scope_types,
    validate_tool_defs,
)

pytestmark = pytest.mark.unit

SCOPES = [{"name": "project", "description": "A project."}]


def _tool(**over):
    base = {
        "name": "get_agenda",
        "description": "Today's agenda.",
        "parameters": {"type": "object", "properties": {}},
        "read_only": True,
        "destructive": False,
        "gate": "review",
    }
    base.update(over)
    return base


def _manifest(floor="review"):
    return SimpleNamespace(max_gate_floor=floor)


def _validate(defs, *, floor="review", scopes=SCOPES, plugin="cal", reviewed=None):
    return validate_tool_defs(plugin, defs, scopes, _manifest(floor), reviewed=reviewed)


def _refused(defs, text, **kw):
    with pytest.raises(ToolDefError, match=text):
        _validate(defs, **kw)


class TestFloors:
    def test_destructive_must_be_popup(self):
        _refused([_tool(read_only=False, destructive=True, gate="review")], "destructive tool get_agenda must use the popup gate")

    def test_write_auto_needs_manifest_floor(self):
        _refused([_tool(read_only=False, gate="auto")], "needs max_gate_floor: auto", floor="review")

    def test_read_auto_needs_manifest_floor(self):
        _refused([_tool(gate="auto")], "needs max_gate_floor: auto", floor="popup")

    def test_auto_allowed_with_floor(self):
        assert _validate([_tool(gate="auto")], floor="auto")[0].gate == "auto"

    def test_read_only_and_destructive_refused(self):
        _refused([_tool(destructive=True, gate="popup")], "both read-only and destructive")


class TestNames:
    def test_pattern(self):
        _refused([_tool(name="Bad-Name")], "pattern")

    def test_mcp_name_length(self):
        _refused([_tool(name="t" * 41)], "longer than 64", plugin="p" * 30)

    def test_duplicates(self):
        _refused([_tool(), _tool()], "defined twice")

    def test_collision_with_builtin_refused(self):
        _refused([_tool(name="script_get_content")], "collides with a built-in tool", plugin="apps")

    def test_collision_with_meta_tool_refused(self):
        _refused([_tool(name="status")], "collides with a built-in tool", plugin="privacyfence")


class TestScopes:
    def test_undeclared_scope_type(self):
        _refused([_tool(scopes=["nope"])], "does not declare")

    def test_declared_scope_accepted(self):
        assert _validate([_tool(scopes=["project"])])[0].scopes == ("project",)

    def test_scope_types_valid(self):
        assert validate_scope_types(SCOPES) == SCOPES

    @pytest.mark.parametrize(
        "raw",
        [
            "x",
            [{"name": "a", "description": "d"}] * 2,
            [{"name": "Bad", "description": "d"}],
            [{"name": "a", "description": ""}],
            ["x"],
            [{"name": f"s{i}", "description": "d"} for i in range(21)],
        ],
    )
    def test_scope_types_refused(self, raw):
        with pytest.raises(ToolDefError):
            validate_scope_types(raw)


def _params(**props):
    return {"type": "object", "properties": props}


class TestParameters:
    def test_reason_property_refused(self):
        _refused([_tool(parameters=_params(reason={"type": "string"}))], "named reason")

    def test_non_object_refused(self):
        _refused([_tool(parameters={"type": "array"})], "must be an object schema")

    def test_properties_not_dict_refused(self):
        _refused([_tool(parameters={"type": "object", "properties": []})], "properties object")

    @pytest.mark.parametrize(
        "schema",
        [{"type": "array"}, {"type": "object"}, {"type": "string", "enum": ["a"]}, {"oneOf": []}, "x"],
    )
    def test_non_scalar_refused(self, schema):
        _refused([_tool(parameters=_params(p=schema))], "parameter p of get_agenda: only string, integer, number and boolean")

    def test_four_scalars_accepted(self):
        props = {k: {"type": k} for k in ("string", "integer", "number", "boolean")}
        defs = _validate([_tool(parameters={**_params(**props), "required": ["string"]})])
        assert len(defs) == 1

    def test_bad_required_refused(self):
        _refused([_tool(parameters={**_params(a={"type": "string"}), "required": ["b"]})], "required")


class TestLimits:
    def test_not_a_list(self):
        _refused({}, "must be a list")

    def test_too_many_tools(self):
        _refused([_tool(name=f"tool_{i}") for i in range(65)], "at most 64")

    def test_long_description(self):
        _refused([_tool(description="x" * (c.MAX_DESCRIPTION_CHARS + 1))], "description")

    def test_long_effect(self):
        _refused([_tool(effect="x" * (c.MAX_EFFECT_CHARS + 1))], "effect")

    def test_long_title(self):
        _refused([_tool(title="x" * (c.MAX_TITLE_CHARS + 1))], "title")


class TestReviewed:
    def _reviewed(self, *defs):
        return frozenset(tool_signature(ToolDef.from_wire(d)) for d in defs)

    def test_signature(self):
        sig = tool_signature(ToolDef.from_wire(_tool(scopes=["project"])))
        assert sig == ("get_agenda", "review", True, False, ("project",))

    def test_new_tool_refused(self):
        reviewed = self._reviewed(_tool())
        _refused([_tool(), _tool(name="other")], "was not in the list reviewed", reviewed=reviewed)

    def test_changed_gate_refused(self):
        reviewed = self._reviewed(_tool())
        _refused([_tool(gate="auto")], "was not in the list reviewed", reviewed=reviewed, floor="auto")

    def test_removed_tool_allowed(self):
        reviewed = self._reviewed(_tool(), _tool(name="other"))
        assert [d.name for d in _validate([_tool()], reviewed=reviewed)] == ["get_agenda"]


class TestWholeListRejected:
    def test_one_bad_tool_rejects_all(self):
        with pytest.raises(ToolDefError):
            _validate([_tool(), _tool(name="second", description="")])
