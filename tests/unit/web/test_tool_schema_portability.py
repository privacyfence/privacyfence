"""Every tool ``/mcp`` advertises -- each connector's and the ``privacyfence_*`` meta-tools -- stays
within what the strictest supported AI client accepts, so a new tool can't be one that some client
rejects, renames or silently drops.

The limits are the intersection of the clients' own: the input schema's root is an object, with no
``$ref``/``$defs``/``definitions`` and no tuple-form ``items`` (clients that translate the schema
into their own function-calling format resolve neither); the name matches
``^[a-zA-Z0-9_-]{1,64}$`` (OpenAI's function-name rule, the strictest); the description is non-empty
and at most 1024 characters (OpenAI's function-description limit); and every tool carries all three
annotation hints, so no client falls back to the MCP defaults (a tool without ``readOnlyHint`` is
assumed to write, and without ``destructiveHint`` to destroy). Which values the hints take is not
this file's business -- see test_mcp_tools.py.

The tools are built from every connector class the tools-reference generator discovers, the way
test_systemic_gate_invariants.py does, so a new connector is covered the moment it exists; one test
asserts that a live ``tools/list`` advertises exactly this set.
"""
from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx2
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from privacyfence.web import mcp_tools
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.routes_mcp import build_mcp_asgi_app, mcp_lifespan

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import generate_tools_reference  # noqa: E402

TOKEN = "portability-test-token"
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_DESCRIPTION_CHARS = 1024
FORBIDDEN_SCHEMA_KEYS = frozenset({"$ref", "$defs", "definitions"})

CONNECTORS = {
    connector.name: connector
    for connector in (cls(MagicMock()) for cls in generate_tools_reference._connector_classes())
}
ADVERTISED: dict[str, types.Tool] = {
    tool.name: tool
    for tool in (
        *(mcp_tools.to_mcp_tool(spec) for connector in CONNECTORS.values() for spec in connector.tool_specs()),
        *mcp_tools.META_TOOLS,
    )
}
TOOLS = [pytest.param(tool, id=name) for name, tool in sorted(ADVERTISED.items())]


def _walk(node: Any, path: str = "$"):
    """Yields ``(path, dict)`` for every JSON object nested anywhere in ``node``."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


@contextlib.asynccontextmanager
async def _connected_session(dispatcher: McpDispatcher):
    app, session_manager = build_mcp_asgi_app(dispatcher, token=TOKEN)
    async with mcp_lifespan(session_manager):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http_client:
            async with streamable_http_client("http://testserver/mcp", http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


async def test_the_live_tool_list_is_exactly_the_set_checked_here():
    async with _connected_session(McpDispatcher(lambda: CONNECTORS)) as session:
        listed = await session.list_tools()
    assert sorted(tool.name for tool in listed.tools) == sorted(ADVERTISED)
    for tool in listed.tools:
        assert tool.input_schema == ADVERTISED[tool.name].input_schema, tool.name


def test_no_two_tools_share_a_name():
    names = [spec.name for connector in CONNECTORS.values() for spec in connector.tool_specs()]
    names += [tool.name for tool in mcp_tools.META_TOOLS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("tool", TOOLS)
def test_input_schema_root_is_an_object(tool: types.Tool):
    assert tool.input_schema.get("type") == "object"


@pytest.mark.parametrize("tool", TOOLS)
def test_input_schema_has_no_references(tool: types.Tool):
    found = [f"{path}.{key}" for path, node in _walk(tool.input_schema) for key in FORBIDDEN_SCHEMA_KEYS & node.keys()]
    assert not found


@pytest.mark.parametrize("tool", TOOLS)
def test_input_schema_has_no_tuple_form_items(tool: types.Tool):
    found = [path for path, node in _walk(tool.input_schema) if isinstance(node.get("items"), list)]
    assert not found


@pytest.mark.parametrize("tool", TOOLS)
def test_name_is_portable(tool: types.Tool):
    assert NAME_RE.fullmatch(tool.name)


@pytest.mark.parametrize("tool", TOOLS)
def test_description_is_non_empty_and_bounded(tool: types.Tool):
    description = tool.description or ""
    assert description.strip()
    assert len(description) <= MAX_DESCRIPTION_CHARS, f"{len(description)} characters"


@pytest.mark.parametrize("tool", TOOLS)
def test_all_three_annotation_hints_are_present(tool: types.Tool):
    annotations = tool.annotations
    assert annotations is not None
    assert annotations.read_only_hint is not None
    assert annotations.destructive_hint is not None
    assert annotations.idempotent_hint is not None
