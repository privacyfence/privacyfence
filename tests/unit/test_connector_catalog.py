"""The credential-free tool catalog: every connector tool plus the meta-tools, as /mcp lists them."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.connector_catalog import catalog_tools, connector_classes
from privacyfence.web import mcp_tools

pytestmark = pytest.mark.unit


def test_catalog_lists_every_connector_tool_and_every_meta_tool():
    expected = {
        spec.name for cls in connector_classes() for spec in cls(MagicMock()).tool_specs()
    } | mcp_tools.META_TOOL_NAMES
    assert {tool.name for tool in catalog_tools()} == expected


def test_catalog_has_no_duplicate_names():
    names = [tool.name for tool in catalog_tools()]
    assert len(names) == len(set(names))


def test_meta_tools_come_last():
    tools = catalog_tools()
    assert tools[-len(mcp_tools.META_TOOLS):] == list(mcp_tools.META_TOOLS)


def test_catalog_tools_match_the_live_mapping():
    by_name = {tool.name: tool for tool in catalog_tools()}
    for cls in connector_classes():
        for spec in cls(MagicMock()).tool_specs():
            assert by_name[spec.name] == mcp_tools.to_mcp_tool(spec)
