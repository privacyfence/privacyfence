"""Validation of the tool definitions a plugin reports (ADR 0121, ADR 0122).

A plugin's tool list is validated as a whole: the first violation rejects every tool, so a plugin
can never expose a partly checked list. After enable, a later list may only contain tools whose
signature the human reviewed, so a plugin cannot add a tool or loosen a gate unseen.
"""
from __future__ import annotations

from typing import Any

from privacyfence import auto_accept
from privacyfence.plugins.constants import (
    MAX_TOOLS,
    MCP_TOOL_NAME_MAX,
    SCOPE_TYPE_RE,
    TOOL_NAME_RE,
    mcp_tool_name,
)
from privacyfence.plugins.protocol import RpcError, ToolDef
from privacyfence.web import mcp_tools

MAX_SCOPE_TYPES = 20
MAX_SCOPE_TYPE_DESCRIPTION_CHARS = 500
SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})


class ToolDefError(Exception):
    """A tool list was refused; ``detail`` says why and never echoes more than names and limits."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def tool_signature(defn: ToolDef) -> tuple:
    return (defn.name, defn.gate, defn.read_only, defn.destructive, tuple(defn.scopes))


def validate_scope_types(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise ToolDefError("scope_types must be a list")
    if len(raw) > MAX_SCOPE_TYPES:
        raise ToolDefError(f"scope_types has more than {MAX_SCOPE_TYPES} entries")
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ToolDefError("each scope type must be an object")
        name = entry.get("name")
        description = entry.get("description")
        if not isinstance(name, str) or not SCOPE_TYPE_RE.fullmatch(name):
            raise ToolDefError("scope type name does not match the scope type pattern")
        if name == "output":
            raise ToolDefError("scope type output is reserved")
        if name in seen:
            raise ToolDefError(f"scope type {name} is declared twice")
        if not isinstance(description, str) or not 1 <= len(description) <= MAX_SCOPE_TYPE_DESCRIPTION_CHARS:
            raise ToolDefError(f"scope type {name} needs a description of 1 to {MAX_SCOPE_TYPE_DESCRIPTION_CHARS} characters")
        seen.add(name)
        out.append({"name": name, "description": description})
    return out


def _check_parameters(defn: ToolDef) -> None:
    params = defn.parameters
    if params.get("type") != "object":
        raise ToolDefError(f"parameters of {defn.name} must be an object schema")
    props = params.get("properties", {})
    if not isinstance(props, dict):
        raise ToolDefError(f"parameters of {defn.name} must have a properties object")
    if "reason" in props:
        raise ToolDefError(f"tool {defn.name} must not declare a parameter named reason")
    for pname, schema in props.items():
        if not isinstance(schema, dict) or schema.get("type") not in SCALAR_TYPES or "enum" in schema:
            raise ToolDefError(
                f"parameter {pname} of {defn.name}: only string, integer, number and boolean are supported"
            )
    required = params.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) and r in props for r in required):
        raise ToolDefError(f"required of {defn.name} must list declared parameter names")


def validate_tool_defs(
    plugin: str,
    defs: Any,
    scope_types: list[dict],
    manifest: Any,
    *,
    reviewed: frozenset[tuple] | None = None,
) -> list[ToolDef]:
    if not isinstance(defs, list):
        raise ToolDefError("tools must be a list")
    if len(defs) > MAX_TOOLS:
        raise ToolDefError(f"a plugin may define at most {MAX_TOOLS} tools")
    declared = {s["name"] for s in scope_types}
    out: list[ToolDef] = []
    seen: set[str] = set()
    for raw in defs:
        try:
            defn = ToolDef.from_wire(raw)
        except RpcError as exc:
            raise ToolDefError(exc.detail) from None
        if defn.name in seen:
            raise ToolDefError(f"tool {defn.name} is defined twice")
        seen.add(defn.name)
        if not TOOL_NAME_RE.fullmatch(defn.name):  # pragma: no cover -- from_wire checks it too
            raise ToolDefError(f"tool name {defn.name} does not match the tool name pattern")
        mcp_name = mcp_tool_name(plugin, defn.name)
        if len(mcp_name) > MCP_TOOL_NAME_MAX:
            raise ToolDefError(f"tool {mcp_name} is longer than {MCP_TOOL_NAME_MAX} characters")
        if mcp_name in auto_accept.STATIC_TOOL_NAMES or mcp_name in mcp_tools.META_TOOL_NAMES:
            raise ToolDefError(f"tool {mcp_name} collides with a built-in tool")
        _check_parameters(defn)
        if defn.read_only and defn.destructive:
            raise ToolDefError(f"tool {defn.name} cannot be both read-only and destructive")
        if defn.destructive and defn.gate != "popup":
            raise ToolDefError(f"destructive tool {defn.name} must use the popup gate")
        if defn.gate == "auto" and manifest.max_gate_floor != "auto":
            raise ToolDefError(f"tool {defn.name} needs max_gate_floor: auto to use the auto gate")
        for scope in defn.scopes:
            if scope not in declared:
                raise ToolDefError(f"tool {defn.name} uses scope type {scope}, which the plugin does not declare")
        if reviewed is not None and tool_signature(defn) not in reviewed:
            raise ToolDefError(
                f"tool {defn.name} was not in the list reviewed at enable; review and enable the plugin again"
            )
        out.append(defn)
    return out
