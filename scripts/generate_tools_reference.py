#!/usr/bin/env python3
"""Generate docs/tools-reference.md from the connectors' own tool definitions.

Every connector tool PrivacyFence can offer an MCP client, grouped by connector, with its direction
(read or write), the gate it goes through, and the first sentence of the description the client
itself is shown. Each column comes from code rather than from a person:

- the tool list, the direction and the description come from each connector's ``tool_specs()``
  (``src/privacyfence/connectors/*.py``) -- the same ``ToolSpec`` objects ``/mcp`` advertises;
- the gate comes from ``auto_accept.TOOL_TO_GATE``, the table ``privacyfence_check_policy`` and the
  policy registry (``policy/registry.py``'s ``TOOL_REGISTRY``) both read;
- every count in the doc is computed from those two sources at render time.

Rendering refuses to write a doc it knows is wrong: a tool that a connector offers but
``TOOL_TO_GATE`` does not list (or the reverse) raises instead of rendering a row with a guessed
gate.

Run it and commit the result whenever a connector gains, loses or re-gates a tool, or its
description changes. ``tests/unit/test_docs_tools_reference.py`` fails CI when the checked-in doc
and a fresh render disagree, the same drift guard ``scripts/generate_always_allow_reference.py``
uses.

Requires PrivacyFence to be importable (``pip install -e .``): it reads the real connector classes.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from privacyfence import connectors as connectors_pkg  # noqa: E402
from privacyfence.auto_accept import TOOL_TO_GATE  # noqa: E402
from privacyfence.connector import Connector  # noqa: E402
from privacyfence.policy.registry import TOOL_REGISTRY  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "tools-reference.md"

# Order and display name of each connector section. Keyed by ``Connector.name``; a connector with
# no entry here makes render() raise rather than silently land in an unnamed section.
CONNECTOR_TITLES: dict[str, str] = {
    "gmail": "Gmail",
    "drive": "Google Drive (including Sheets and Docs)",
    "calendar": "Google Calendar",
    "contacts": "Google Contacts",
    "tasks": "Google Tasks",
    "apps_script": "Apps Script",
    "slack": "Slack",
    "telegram": "Telegram",
    "salesforce": "Salesforce",
    "jira": "Jira",
    "confluence": "Confluence",
}

# Short names for the summary table, same keys.
CONNECTOR_SHORT: dict[str, str] = {
    "gmail": "Gmail",
    "drive": "Google Drive",
    "calendar": "Google Calendar",
    "contacts": "Google Contacts",
    "tasks": "Google Tasks",
    "apps_script": "Apps Script",
    "slack": "Slack",
    "telegram": "Telegram",
    "salesforce": "Salesforce",
    "jira": "Jira",
    "confluence": "Confluence",
}

GATE_ORDER: tuple[str, ...] = ("auto", "review", "popup")


@dataclass(frozen=True)
class ToolRow:
    connector: str
    name: str
    direction: str
    gate: str
    summary: str


def _connector_classes() -> list[type[Connector]]:
    """Every concrete ``Connector`` subclass defined in ``privacyfence.connectors``."""
    found: dict[str, type[Connector]] = {}
    for module_info in pkgutil.iter_modules(connectors_pkg.__path__):
        module = importlib.import_module(f"{connectors_pkg.__name__}.{module_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Connector) and obj is not Connector and not inspect.isabstract(obj):
                if obj.__module__ == module.__name__:
                    found[obj.__qualname__] = obj
    return [found[key] for key in sorted(found)]


# Sentence end: a period followed by whitespace, unless the period closes a common abbreviation.
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.")
_MAX_SUMMARY_CHARS = 240


def first_sentence(description: str) -> str:
    """The first sentence of a tool description, on one line, safe inside a Markdown table cell."""
    text = " ".join(description.split())
    end = len(text)
    for match in re.finditer(r"\.(\s|$)", text):
        candidate = text[: match.start() + 1]
        if candidate.lower().endswith(_ABBREVIATIONS):
            continue
        end = match.start() + 1
        break
    sentence = text[:end].strip()
    if len(sentence) > _MAX_SUMMARY_CHARS:
        # A sentence that long is a list of details (drive_write_doc_content's formatting syntax);
        # keep the clause before the list starts.
        for separator in (": ", " — ", " -- "):
            head, found, _rest = sentence.partition(separator)
            if found and len(head) <= _MAX_SUMMARY_CHARS:
                sentence = head.rstrip(".") + "."
                break
    return sentence.replace("|", "\\|")


def collect_rows() -> list[ToolRow]:
    rows: list[ToolRow] = []
    seen: set[str] = set()
    for cls in _connector_classes():
        # The same construction tests/unit/connectors/test_readme_manifest_alignment.py uses:
        # tool_specs() is static data and never touches the client it was given.
        connector = cls(MagicMock())  # type: ignore[call-arg]
        name = connector.name
        if name not in CONNECTOR_TITLES:
            raise RuntimeError(f"connector {name!r} has no entry in CONNECTOR_TITLES -- add one")
        for spec in connector.tool_specs():
            if spec.name not in TOOL_TO_GATE:
                raise RuntimeError(f"{spec.name} is offered by {name} but has no gate in auto_accept.TOOL_TO_GATE")
            if spec.name in seen:
                raise RuntimeError(f"{spec.name} is offered by more than one connector")
            seen.add(spec.name)
            rows.append(ToolRow(
                connector=name,
                name=spec.name,
                direction="read" if spec.read_only else "write",
                gate=TOOL_TO_GATE[spec.name],
                summary=first_sentence(spec.description),
            ))
    missing = sorted(set(TOOL_TO_GATE) - seen)
    if missing:
        raise RuntimeError(f"auto_accept.TOOL_TO_GATE lists tools no connector offers: {', '.join(missing)}")
    for row in rows:
        entry = TOOL_REGISTRY.get(row.name)
        if entry is None or entry.gate != row.gate:
            raise RuntimeError(f"{row.name}: policy registry and TOOL_TO_GATE disagree on its gate")
    return rows


def _by_connector(rows: list[ToolRow]) -> dict[str, list[ToolRow]]:
    grouped: dict[str, list[ToolRow]] = {name: [] for name in CONNECTOR_TITLES}
    for row in rows:
        grouped[row.connector].append(row)
    for name in grouped:
        grouped[name].sort(key=lambda r: (GATE_ORDER.index(r.gate), r.name))
    return {name: tools for name, tools in grouped.items() if tools}


def _anchor(title: str) -> str:
    """GitHub's heading anchor for ``title``: lowercase, punctuation dropped, spaces to hyphens."""
    slug = re.sub(r"[^\w\- ]", "", title.lower())
    return slug.replace(" ", "-")


def _count(rows: list[ToolRow], gate: str) -> int:
    return sum(1 for r in rows if r.gate == gate)


def _summary_table(grouped: dict[str, list[ToolRow]], rows: list[ToolRow]) -> str:
    lines = [
        "| Connector | Tools | `auto` | `review` | `popup` |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, tools in grouped.items():
        link = f"[{CONNECTOR_SHORT[name]}](#{_anchor(CONNECTOR_TITLES[name])})"
        counts = " | ".join(str(_count(tools, gate)) for gate in GATE_ORDER)
        lines.append(f"| {link} | {len(tools)} | {counts} |")
    totals = " | ".join(f"**{_count(rows, gate)}**" for gate in GATE_ORDER)
    lines.append(f"| **Total** | **{len(rows)}** | {totals} |")
    return "\n".join(lines) + "\n"


def _connector_section(name: str, tools: list[ToolRow]) -> str:
    lines = [
        f"## {CONNECTOR_TITLES[name]}",
        "",
        "| Tool | Direction | Gate | What it does |",
        "|---|---|---|---|",
    ]
    for row in tools:
        lines.append(f"| `{row.name}` | {row.direction} | `{row.gate}` | {row.summary} |")
    return "\n".join(lines) + "\n"


_HEADER = """# Tools reference

<!-- GENERATED FILE. Do not edit by hand: run `python scripts/generate_tools_reference.py` and
commit the result. tests/unit/test_docs_tools_reference.py fails when this file is stale. -->

Every connector tool PrivacyFence offers an MCP client, and the gate each one goes through. This
page is **generated** by
[`scripts/generate_tools_reference.py`](../scripts/generate_tools_reference.py) from the connectors'
own tool definitions and the gate table in
[`src/privacyfence/auto_accept.py`](../src/privacyfence/auto_accept.py). Don't edit it by hand.

A connector's tools appear only after its organization config is installed and you have signed in
to that service (see [Connecting a service](connecting-a-service.md)). The eight `privacyfence_*`
tools that PrivacyFence adds itself are not connector tools and have no gate; they are described in
[How PrivacyFence works](how-it-works.md#privacyfences-own-tools).

## Gates

| Gate | What happens when the AI system calls the tool |
|---|---|
| `auto` | Runs straight away with no card. Still recorded in the audit log. |
| `review` | A read. You see a card showing what would be released before the result goes back to the AI system, unless an auto-accept rule covers the call. |
| `popup` | A write or other change. You approve it on a card before it happens, unless an auto-accept rule covers the call. |

A tool's gate is fixed in code; no setting moves a tool to a different gate. Auto-accept rules,
PII detection, the privacy filter and passkey step-up all act within these gates: see
[Approvals and policy](approvals-and-policy.md). What the **Always allow** button proposes for each
`review` and `popup` tool is listed in the
[Always allow reference](always-allow-rules-reference.md).

**Direction** is what the tool itself declares: `read` tools change nothing in the connected
service; `write` tools create, change or send something. A few `write` tools are `auto` because
they create something empty and disclose nothing (for example `drive_create_blank_file`).

**What it does** is the first sentence of the description the AI system is shown for the tool.

## Summary

"""


def render() -> str:
    rows = collect_rows()
    grouped = _by_connector(rows)
    sections = "\n".join(_connector_section(name, tools) for name, tools in grouped.items())
    return _HEADER + _summary_table(grouped, rows) + "\n" + sections


def main() -> int:
    content = render()
    DOC_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {DOC_PATH.relative_to(REPO_ROOT)} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
