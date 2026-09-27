"""Which connector tools are classified as destructive (``ToolSpec.destructive``, ADR 0086), and
that every connector tool is advertised truthfully (ADR 0088).

``destructiveHint`` is what a client reads to decide whether a tool deletes something. Only the
tools that delete are marked: an overwrite is a write, not a deletion. The set is pinned here
exactly, so a new deleting tool fails this test until someone classifies it on purpose, and an
existing tool cannot become destructive (or stop being so) by accident.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence.connector import ToolSpec

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Same connector discovery test_systemic_gate_invariants.py uses: a new connector is covered the
# moment it exists.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import generate_tools_reference  # noqa: E402

# The only tools that delete: a calendar event, and rows or columns of a spreadsheet.
DESTRUCTIVE_TOOLS = frozenset({"calendar_delete_event", "drive_sheets_delete_dimensions"})


def _all_tool_specs() -> dict[str, ToolSpec]:
    specs: dict[str, ToolSpec] = {}
    for cls in generate_tools_reference._connector_classes():
        for spec in cls(MagicMock()).tool_specs():
            specs[spec.name] = spec
    return specs


ALL_SPECS = _all_tool_specs()


def test_the_destructive_set_is_exactly_the_deleting_tools():
    assert {name for name, spec in ALL_SPECS.items() if spec.destructive} == DESTRUCTIVE_TOOLS


def test_every_destructive_tool_is_a_write():
    assert not [name for name, spec in ALL_SPECS.items() if spec.destructive and spec.read_only]


@pytest.mark.parametrize("name", sorted(ALL_SPECS))
def test_every_connector_tool_is_advertised_truthfully(name):
    """ADR 0088: what ``/mcp`` tells a client about each real connector tool is what the tool does
    -- a read is read-only and idempotent, a write is neither, and only the pinned deleting tools
    are destructive. There is no mode that advertises a write as read-only."""
    from privacyfence.web import mcp_tools

    spec = ALL_SPECS[name]
    annotations = mcp_tools.to_mcp_tool(spec).annotations
    assert annotations.read_only_hint is spec.read_only
    assert annotations.idempotent_hint is spec.read_only
    assert annotations.destructive_hint is (name in DESTRUCTIVE_TOOLS)
