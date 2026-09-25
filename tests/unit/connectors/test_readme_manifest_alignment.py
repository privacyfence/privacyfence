"""Drift detection between the published tools reference and the actual code.

docs/tools-reference.md documents, per tool, whether it's a read or a write and what gate it goes
through -- PrivacyFence's public privacy promise. It is generated
(scripts/generate_tools_reference.py), and tests/unit/test_docs_tools_reference.py checks it is not
stale. This module checks the same promise from the other side, independently of the generator:
it parses the doc's rows and cross-checks them against each connector's real ToolSpec.read_only
flag, against auto_accept.TOOL_TO_GATE (the static "auto"/"review"/"popup" table a preflight caller
relies on -- see privacyfence_check_policy), and against the gate= each connectors/*.py call site
actually passes to gated_call(). The connector list here is written out by hand, and a test checks
it matches the connector classes the generator discovers, so a connector the generator missed (or
one this list missed) fails here rather than silently dropping out of the doc.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence.auto_accept import TOOL_TO_GATE
from privacyfence.connectors.apps_script import AppsScriptConnector
from privacyfence.connectors.calendar import CalendarConnector
from privacyfence.connectors.confluence import ConfluenceConnector
from privacyfence.connectors.contacts import ContactsConnector
from privacyfence.connectors.drive import DriveConnector
from privacyfence.connectors.gmail import GmailConnector
from privacyfence.connectors.jira import JiraConnector
from privacyfence.connectors.salesforce import SalesforceConnector
from privacyfence.connectors.slack import SlackConnector
from privacyfence.connectors.tasks import TasksConnector
from privacyfence.connectors.telegram import TelegramConnector

CONNECTOR_CLASSES = [
    GmailConnector, DriveConnector, SlackConnector, CalendarConnector,
    ContactsConnector, SalesforceConnector, JiraConnector, ConfluenceConnector,
    TasksConnector, TelegramConnector, AppsScriptConnector,
]

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "docs" / "tools-reference.md"

# Matches the per-connector tool rows (`| `tool` | read | `gate` | description |`), not the summary
# table's rows, whose first cell is a link rather than a code span.
_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*(read|write)\s*\|\s*`(auto|review|popup)`\s*\|", re.MULTILINE)


# Matches a tool="..." kwarg followed (non-greedily, across the rest of that
# same gated_call(...) invocation) by its gate="..." kwarg -- the two are
# always kwargs of one call, so the nearest gate= after a given tool= is that
# tool's own gate. Tools with no match here never call gated_call() at all,
# i.e. they're unconditionally "auto".
_SRC_TOOL_GATE_RE = re.compile(r'tool="(?P<tool>\w+)",.*?gate="(?P<gate>review|popup)"', re.DOTALL)


def _doc_privacy_matrix() -> dict[str, tuple[str, str]]:
    text = DOC_PATH.read_text(encoding="utf-8")
    return {tool: (direction, gate) for tool, direction, gate in _ROW_RE.findall(text)}


def _all_code_tools() -> dict[str, bool]:
    """Return {tool_name: read_only} across every registered connector."""
    tools: dict[str, bool] = {}
    for cls in CONNECTOR_CLASSES:
        connector = cls(MagicMock())
        for spec in connector.tool_specs():
            tools[spec.name] = spec.read_only
    return tools


def _source_declared_gates() -> dict[str, str]:
    """Parse connectors/*.py for the gate= each tool actually passes to gated_call()."""
    gates: dict[str, str] = {}
    seen_files: set[Path] = set()
    for cls in CONNECTOR_CLASSES:
        path = Path(inspect.getfile(cls))
        if path in seen_files:
            continue
        seen_files.add(path)
        text = path.read_text(encoding="utf-8")
        for match in _SRC_TOOL_GATE_RE.finditer(text):
            gates[match.group("tool")] = match.group("gate")
    return gates


@pytest.fixture(scope="module")
def doc_matrix():
    matrix = _doc_privacy_matrix()
    assert len(matrix) > 30, "doc parser found suspiciously few rows -- check _ROW_RE"
    return matrix


@pytest.fixture(scope="module")
def code_tools():
    return _all_code_tools()


def test_every_code_tool_is_documented(doc_matrix, code_tools):
    undocumented = sorted(set(code_tools) - set(doc_matrix))
    assert undocumented == [], (
        f"Tools exist in connector code but are missing from docs/tools-reference.md: {undocumented}"
    )


def test_every_documented_tool_still_exists_in_code(doc_matrix, code_tools):
    stale = sorted(set(doc_matrix) - set(code_tools))
    assert stale == [], (
        f"docs/tools-reference.md documents tools that no longer exist in any connector: {stale}"
    )


@pytest.mark.parametrize("tool", sorted(_all_code_tools()))
def test_read_only_flag_matches_documented_direction(tool, doc_matrix, code_tools):
    if tool not in doc_matrix:
        pytest.skip(f"{tool} undocumented in docs (see test_every_code_tool_is_documented)")
    direction, _gate = doc_matrix[tool]
    expected_read_only = direction == "read"
    assert code_tools[tool] == expected_read_only, (
        f"{tool}: docs say dir={direction!r} (expects read_only={expected_read_only}) "
        f"but ToolSpec.read_only={code_tools[tool]!r}"
    )


def test_every_code_tool_is_in_tool_to_gate(code_tools):
    missing = sorted(set(code_tools) - set(TOOL_TO_GATE))
    assert missing == [], (
        f"Tools exist in connector code but are missing from auto_accept.TOOL_TO_GATE: {missing}"
    )


def test_tool_to_gate_has_no_stale_entries(code_tools):
    stale = sorted(set(TOOL_TO_GATE) - set(code_tools))
    assert stale == [], (
        f"auto_accept.TOOL_TO_GATE lists tools that no longer exist in any connector: {stale}"
    )


@pytest.mark.parametrize("tool", sorted(TOOL_TO_GATE))
def test_tool_to_gate_matches_documented_gate(tool, doc_matrix):
    if tool not in doc_matrix:
        pytest.skip(f"{tool} undocumented in docs (see test_every_code_tool_is_documented)")
    _direction, documented_gate = doc_matrix[tool]
    assert TOOL_TO_GATE[tool] == documented_gate, (
        f"{tool}: docs say gate={documented_gate!r} but "
        f"auto_accept.TOOL_TO_GATE[{tool!r}]={TOOL_TO_GATE[tool]!r}"
    )


@pytest.mark.parametrize("tool", sorted(_all_code_tools()))
def test_tool_to_gate_matches_gated_call_source(tool, code_tools):
    """TOOL_TO_GATE must match what connectors/*.py actually passes to gated_call() --
    the docs can drift, but this checks the real call sites directly."""
    source_gates = _source_declared_gates()
    expected = source_gates.get(tool, "auto")
    assert TOOL_TO_GATE.get(tool) == expected, (
        f"{tool}: connectors/*.py passes gate={expected!r} to gated_call() (or never calls it, "
        f"implying \"auto\") but auto_accept.TOOL_TO_GATE[{tool!r}]={TOOL_TO_GATE.get(tool)!r}"
    )


def test_connector_list_matches_the_generators_discovery():
    """CONNECTOR_CLASSES above is written out by hand; the generator discovers connectors from the
    package. Each is a check on the other: a new connector must show up in both."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import generate_tools_reference as gen

    discovered = {cls.__qualname__ for cls in gen._connector_classes()}
    assert discovered == {cls.__qualname__ for cls in CONNECTOR_CLASSES}
