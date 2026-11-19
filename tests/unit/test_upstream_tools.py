"""Unit tests for the upstream tool list model (ADR 0331)."""
from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from privacyfence import upstream_tools as ut
from privacyfence.upstream_tools import SavedTool, ToolEntry, ToolStore

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


def entry(**overrides) -> ToolEntry:
    base = {"name": "search", "description": "Find things", "input_schema": SCHEMA,
            "read_only": True, "destructive": False}
    base.update(overrides)
    return ToolEntry(**base)


def saved_of(item: ToolEntry, gate: str = "review") -> SavedTool:
    class_default, verb = ut.classify(item.read_only, item.destructive)
    return SavedTool(item.name, item.description, item.input_schema, item.read_only, item.destructive,
                     gate, verb, ut.tool_digest(item))


@pytest.mark.parametrize(
    ("read_only", "destructive", "expected"),
    [
        (True, True, ("popup", "delete")),
        (True, False, ("review", "read")),
        (True, None, ("review", "read")),
        (False, True, ("popup", "delete")),
        (False, False, ("popup", "update")),
        (False, None, ("popup", "delete")),
        (None, True, ("popup", "delete")),
        (None, False, ("popup", "update")),
        (None, None, ("popup", "delete")),
    ],
)
def test_classify(read_only, destructive, expected):
    assert ut.classify(read_only, destructive) == expected


@pytest.mark.parametrize(
    ("configured", "class_default", "expected"),
    [
        ("review", "popup", "review"),
        ("review", "review", "review"),
        ("popup", "review", "popup"),
        ("popup", "popup", "popup"),
        ("auto", "review", "review"),
        ("auto", "popup", "popup"),
    ],
)
def test_person_gate(configured, class_default, expected):
    assert ut.person_gate(configured, class_default) == expected


def test_constants():
    assert ut.GATES == ("review", "popup", "auto")
    assert ut.MAX_TOOLS == 50


def test_unsupported_reason_none_for_good_tool():
    assert ut.unsupported_reason("canva", entry()) is None


def test_unsupported_bad_name():
    reason = ut.unsupported_reason("canva", entry(name="bad name"))
    assert reason == "its name is not 1-48 letters, digits, underscores or hyphens"


def test_unsupported_49_character_name():
    reason = ut.unsupported_reason("a", entry(name="n" * 49))
    assert reason == "its name is not 1-48 letters, digits, underscores or hyphens"
    assert ut.unsupported_reason("a", entry(name="n" * 48)) is None


def test_unsupported_exposed_name_too_long():
    reason = ut.unsupported_reason("u" * 20, entry(name="n" * 48))
    assert reason == "its exposed name is longer than 64 characters"


@pytest.mark.parametrize(
    "schema",
    [
        [],
        {"type": "string"},
        {"type": "object", "properties": []},
        {"type": "object", "required": "q"},
    ],
)
def test_unsupported_non_object_schema(schema):
    reason = ut.unsupported_reason("canva", entry(input_schema=schema))
    assert reason == "its input schema is not an object schema"


def test_unsupported_reason_property():
    schema = {"type": "object", "properties": {"reason": {"type": "string"}}}
    assert ut.unsupported_reason("canva", entry(input_schema=schema)) == (
        "its input schema already has a reason property"
    )


@pytest.mark.parametrize("key", ["$ref", "$defs", "definitions"])
def test_unsupported_ref_keys(key):
    schema = {"type": "object", "properties": {"a": {key: "#/x"}}}
    assert ut.unsupported_reason("canva", entry(input_schema=schema)) == (
        "its input schema uses $ref, $defs or definitions"
    )


def test_unsupported_ref_nested_two_levels_and_in_list():
    deep = {"type": "object", "properties": {"a": {"properties": {"b": {"definitions": {}}}}}}
    in_list = {"type": "object", "properties": {"a": {"anyOf": [{"type": "string"}, {"$ref": "#"}]}}}
    for schema in (deep, in_list):
        assert ut.unsupported_reason("canva", entry(input_schema=schema)) == (
            "its input schema uses $ref, $defs or definitions"
        )


def test_unsupported_schema_too_large():
    schema = {"type": "object", "properties": {"a": {"description": "x" * (32 * 1024)}}}
    assert ut.unsupported_reason("canva", entry(input_schema=schema)) == "its input schema is larger than 32 KB"


def test_tool_digest_changes_with_each_field_and_ignores_key_order():
    base = entry()
    digest = ut.tool_digest(base)
    assert len(digest) == 32
    variants = [
        replace(base, name="other"),
        replace(base, description="other"),
        replace(base, input_schema={"type": "object"}),
        replace(base, read_only=False),
        replace(base, destructive=True),
    ]
    assert len({digest, *(ut.tool_digest(v) for v in variants)}) == 6
    reordered = {"required": ["q"], "properties": {"q": {"type": "string"}}, "type": "object"}
    assert ut.tool_digest(replace(base, input_schema=reordered)) == digest


def test_declaration_digest_inputs_and_literal():
    args = ("https://mcp.example.com/mcp", "canva_search", "read", "review", "a" * 32)
    digest = ut.declaration_digest(*args)
    assert len(digest) == 32
    changes = [
        ("https://other.example.com/mcp", *args[1:]),
        (args[0], "canva_other", *args[2:]),
        (*args[:2], "delete", *args[3:]),
        (*args[:3], "popup", args[4]),
        (*args[:4], "b" * 32),
    ]
    assert len({digest, *(ut.declaration_digest(*c) for c in changes)}) == 6
    assert digest == "acdbf54dbe4e18d4f560581179781a03"


def test_diff_classifies_everything():
    keep, edit, gone = entry(name="keep"), entry(name="edit"), entry(name="gone")
    saved = (saved_of(keep), saved_of(edit), saved_of(gone))
    fresh, edited, bad = entry(name="fresh"), entry(name="edit", description="new text"), entry(name="bad name")
    result = ut.diff(saved, ((keep, None), (edited, None), (fresh, None), (bad, "its name is bad")))
    assert result.new == (fresh,)
    assert result.changed == (edited,)
    assert result.unchanged == (keep,)
    assert result.removed == ("gone",)
    assert result.unsupported == ((bad, "its name is bad"),)


def test_diff_caps_supported_tools_at_fifty():
    entries = tuple(entry(name=f"t{i}") for i in range(52))
    result = ut.diff((), tuple((e, None) for e in entries))
    assert result.new == entries[:50]
    assert result.unsupported == ((entries[50], "more than 50 tools"), (entries[51], "more than 50 tools"))


def test_diff_unsupported_tools_do_not_count_toward_the_cap():
    bad = entry(name="bad name")
    entries = tuple(entry(name=f"t{i}") for i in range(50))
    result = ut.diff((), ((bad, "no"), *((e, None) for e in entries)))
    assert len(result.new) == 50
    assert result.unsupported == ((bad, "no"),)


def test_diff_saved_tool_now_unsupported_is_removed():
    item = entry()
    result = ut.diff((saved_of(item),), ((item, "its input schema is larger than 32 KB"),))
    assert result.removed == ("search",)
    assert result.unchanged == ()


def test_store_round_trip(tmp_path):
    store = ToolStore(tmp_path / "upstream_tools")
    tools = (saved_of(entry()), saved_of(entry(name="write", read_only=None, destructive=None), "popup"))
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    store.save("canva", "https://mcp.example.com/mcp", tools, now)
    loaded = store.load("canva")
    assert loaded is not None
    assert loaded.upstream_id == "canva"
    assert loaded.url == "https://mcp.example.com/mcp"
    assert loaded.saved_at == now.isoformat()
    assert loaded.tools == tools
    path = tmp_path / "upstream_tools" / "canva.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_store_absent_and_bad_id(tmp_path):
    store = ToolStore(tmp_path)
    assert store.load("missing") is None
    assert store.load("../x") is None
    with pytest.raises(ValueError):
        store.save("../x", "u", (), datetime.now(timezone.utc))


def _write(tmp_path, payload) -> ToolStore:
    (tmp_path / "canva.json").write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return ToolStore(tmp_path)


def _good() -> dict:
    tool = saved_of(entry())
    return {"version": 1, "upstream_id": "canva", "url": "u", "saved_at": "t", "tools": [ut._tool_to_json(tool)]}


def test_store_good_payload_loads(tmp_path):
    assert _write(tmp_path, _good()).load("canva") is not None


def test_store_malformed_files_return_none(tmp_path):
    bad_tool_type = _good()
    bad_tool_type["tools"][0]["name"] = 5
    bad_schema = _good()
    bad_schema["tools"][0]["input_schema"] = []
    bad_hint = _good()
    bad_hint["tools"][0]["read_only"] = "yes"
    missing_field = _good()
    del missing_field["tools"][0]["digest"]
    tool_not_object = _good()
    tool_not_object["tools"] = ["x"]
    bad_url = _good()
    bad_url["url"] = 3
    bad_saved_at = _good()
    bad_saved_at["saved_at"] = None
    wrong_version = _good()
    wrong_version["version"] = 2
    wrong_id = _good()
    wrong_id["upstream_id"] = "other"
    tools_none = _good()
    tools_none["tools"] = None
    for payload in ("{not json", "[]", bad_tool_type, bad_schema, bad_hint, missing_field, tool_not_object,
                    bad_url, bad_saved_at, wrong_version, wrong_id, tools_none):
        assert _write(tmp_path, payload).load("canva") is None


def test_store_save_replaces_file_atomically(tmp_path):
    store = ToolStore(tmp_path)
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    store.save("canva", "u1", (saved_of(entry()),), now)
    store.save("canva", "u2", (), now)
    loaded = store.load("canva")
    assert loaded is not None
    assert (loaded.url, loaded.tools) == ("u2", ())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["canva.json"]
