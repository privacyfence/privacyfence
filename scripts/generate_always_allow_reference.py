#!/usr/bin/env python3
"""Generate docs/always-allow-rules-reference.md from the live policy v2 catalogue.

Through P8 of the policy v2 redesign this doc was hand-maintained prose, cross-checked by eye
against ``auto_accept.py``'s v1 suggestion tables (``TOOL_TO_GATE``/``TOOL_TO_OPERATION``/
``suggest_rule()``/``suggest_write_rule()``) -- its own header said so, and named those tables as
"authoritative" the moment it drifted. P9 retires that whole suggestion machinery in favor of
``policy.propose.proposals_for()``, the one scope catalogue every "Always allow" surface (the
popup, Settings, the MCP bridge) now shares -- so this script walks that catalogue instead of a
person walking it by hand, the same "the generator is the artefact" posture the redesign
proposal's own Phase 0 inventory used.

Run it and check the result in whenever ``policy/registry.py``'s ``TOOL_TO_VERB``/
``VERB_SCOPE_SUBJECT`` or ``policy/propose.py``'s ``PROPOSABLE_SCOPES`` change what a tool's
"Always allow" button would propose -- ``tests/unit/test_generate_always_allow_reference.py``
fails CI the moment the checked-in doc and a fresh run of this script disagree, the same drift
guard ``scripts/changelog_section.py`` and this repo's other generated-and-committed files use.

Requires PrivacyFence to be installed (``pip install -e .``) -- unlike ``scripts/changelog_section.py``,
this reads the real ``policy`` package, not just stdlib.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from privacyfence.policy import describe, propose  # noqa: E402
from privacyfence.policy.registry import TOOL_REGISTRY  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "always-allow-rules-reference.md"

_HEADER = """# "Always allow" — per-tool reference

What clicking **Always allow** proposes, tool by tool. **Generated** by
[`scripts/generate_always_allow_reference.py`](../scripts/generate_always_allow_reference.py) from
[`src/privacyfence/policy/registry.py`](../src/privacyfence/policy/registry.py) and
[`src/privacyfence/policy/propose.py`](../src/privacyfence/policy/propose.py) — the same scope
catalogue the popup, the Auto-accept Settings page, and the MCP bridge's
`privacyfence_propose_policy_change` all write through
([`docs/TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#auto-accept) has the schema and the three
surfaces). Don't hand-edit this file — run the generator and commit its output; a CI test fails if
the checked-in copy and a fresh run disagree.

## How this doc is organized

Every gated tool has a **gate**: `auto`, `review`, or `popup`. `auto` tools never show a popup or
any button — nothing to allow — so they're **left out of this doc entirely**. What remains splits
into two sections:

- **[Read tools](#read-tools)** (`review` gate) — the popup offers **Always allow** whenever at
  least one scope in the catalogue below plausibly contains the item just read; otherwise the
  button doesn't appear, and the row's own column is empty.
- **[Write tools](#write-tools)** (`popup` gate) — most write popups never offer Always allow at
  all; the ones that do propose a rule scoped to the one folder/label/calendar/project/space/task
  list the call just touched, narrowest first — never a bare "accept every future write of this
  type" toggle.

Where a tool's row lists more than one candidate, only the first whose scope actually contains the
item under review becomes a button — narrowest declared first (identity scopes before attribute
scopes before condition scopes), per
[`policy/propose.py`](../src/privacyfence/policy/propose.py)'s own declaration order. When two or
more candidates genuinely match the same item at once (e.g. a file you own that's also in an
approved folder), the popup renders one button per match instead of picking one.

**Always allow always writes a v2 rule to the `auto_accept:` section** — one row, scoped to
exactly the operation just gated at first; the confirmation dialog then offers further verbs as
named widening chips (e.g. "also allow format") before anything is written. See
[Auto-accept](TECHNICAL_REFERENCE.md#auto-accept) for the schema and the other two surfaces that
write the identical shape.

---

"""

_READ_INTRO = """## Read tools

"""

_WRITE_INTRO = """## Write tools

Most write tools never offer **Always allow** — auto-accepting a write silently is a materially
bigger blast radius than auto-accepting a read. Every write tool below with a non-empty column is
a narrow, deliberate exception, scoped to the one resource the call just touched. Every other
gated write tool offers exactly Deny / Allow once, with an empty **Always allow proposes** column.
A handful of tools also have a separate, non-persisted grace-window behavior tucked into their
"Allow once" instead — see
[Related but distinct mechanisms](TECHNICAL_REFERENCE.md#auto-accept) for what that is; it isn't
an Always-allow rule and doesn't belong in this column.

"""

_FOOTER = """
## Related but distinct mechanisms

These are easy to conflate with Always allow because they sit in the same popups or touch the same
config, but none of them are the "Always allow" button covered above.

**Temp-accept grace window** — an in-memory, non-persisted acceptance for six `popup`-gate writes
expected to fire repeatedly against the same file in a burst
(`privacyfence.auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS`), scoped to one file/spreadsheet for 5
minutes and gone on daemon restart. There's no separate button for it: these popups show only
Deny / Allow once, with a plain disclosure caption above the buttons explaining that Allow once
also arms the grace window.

**Bridge-proposed policy changes** (`privacyfence_propose_policy_change`) — lets Claude itself
propose adding/updating/removing a rule for *any* operation, including tools that never get an
Always-allow button of their own (a Gmail filter, a Slack group chat, an Apps Script project).
Every call still blocks on the same confirmation dialog Always allow uses — there's no way for a
rule to land without a human confirming it. See `privacyfence_list_policy`/
`privacyfence_propose_policy_change` in `src/privacyfence/web/mcp_tools.py` (or the tool's own MCP
description) for the exact request/response shape.
"""


def _scope_candidates(tool: str) -> list[propose.ProposableScope]:
    """Every ``PROPOSABLE_SCOPES`` entry ``tool``'s own verb could match, in the catalogue's own
    (narrowest-first) declaration order -- the same filter ``policy.propose.proposals_for`` applies
    against a real call's ``ctx``, minus the per-call value check (this is a static, per-tool view,
    not "would this specific item match")."""
    entry = TOOL_REGISTRY.get(tool)
    if entry is None or entry.operation is None or entry.verb is None:
        return []
    connector = propose.connector_of_operation(entry.operation)
    return [
        scope for scope in propose.PROPOSABLE_SCOPES
        if scope.connector == connector and entry.verb in scope.verbs and entry.operation not in scope.excludes
    ]


def _candidates_cell(tool: str) -> str:
    scopes = _scope_candidates(tool)
    if not scopes:
        return ""
    phrases = []
    for scope in scopes:
        hint = scope.hint or "unconditional"
        phrases.append(hint if not phrases else f"else {hint}")
    return ", ".join(phrases)


def _tools_by_connector(gate: str) -> dict[str, list[str]]:
    # Grouped by the same connector `_scope_candidates` resolves scopes against
    # (`propose.connector_of_operation`), not the tool's own namespace -- Sheets/Docs tools
    # address a Drive file and are governed by `drive.folder` rules, so they belong in the same
    # section a plain Drive file's tools do, the way the pre-P9 hand-written doc grouped them too.
    by_connector: dict[str, list[str]] = {}
    for tool, entry in TOOL_REGISTRY.items():
        if entry.gate != gate:
            continue
        connector = propose.connector_of_operation(entry.operation) if entry.operation else tool.split("_", 1)[0]
        by_connector.setdefault(connector, []).append(tool)
    for tools in by_connector.values():
        tools.sort()
    return by_connector


def _section(gate: str) -> str:
    lines: list[str] = []
    by_connector = _tools_by_connector(gate)
    for connector in sorted(by_connector):
        lines.append(f"### {describe.connector_label(connector)}\n")
        lines.append("| Tool | Always allow proposes |")
        lines.append("|---|---|")
        for tool in by_connector[connector]:
            lines.append(f"| `{tool}` | {_candidates_cell(tool)} |")
        lines.append("")
    return "\n".join(lines)


def render() -> str:
    return (
        _HEADER
        + _READ_INTRO + _section("review")
        + "\n---\n\n"
        + _WRITE_INTRO + _section("popup")
        + _FOOTER
    )


def main() -> int:
    content = render()
    DOC_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {DOC_PATH.relative_to(REPO_ROOT)} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
