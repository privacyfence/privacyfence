#!/usr/bin/env python3
"""Generate docs/always-allow-rules-reference.md from the live policy scope catalogue.

The doc lists, per gated tool, which "Always allow" buttons an approval card can offer. It is
generated rather than hand-written so it cannot drift from ``policy/registry.py`` (which tool maps
to which operation and verb) and ``policy/propose.py``'s ``PROPOSABLE_SCOPES`` (which scopes a card
may propose).

Run it and commit the result whenever either of those changes what a tool's card would propose --
``tests/unit/test_generate_always_allow_reference.py`` fails CI when the checked-in doc and a fresh
run disagree.

Requires PrivacyFence to be importable (``pip install -e .``): this reads the real ``policy``
package, not just stdlib.
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

Which **Always allow** buttons an approval card can offer, tool by tool. This page is an appendix
to [Approvals and policy](approvals-and-policy.md#always-allow-and-policy-rules), which explains
how rules work and where else you can create them.

**Generated** by
[`scripts/generate_always_allow_reference.py`](../scripts/generate_always_allow_reference.py) from
[`src/privacyfence/policy/registry.py`](../src/privacyfence/policy/registry.py) and
[`src/privacyfence/policy/propose.py`](../src/privacyfence/policy/propose.py). Don't hand-edit this
file — run the generator and commit its output; a CI test fails if the checked-in copy and a fresh
run disagree.

## How to read this page

Every connector tool has a **gate**: `auto` (runs without asking), `review` (a read you approve
before its content is released) or `popup` (a write you confirm before it happens). `auto` tools
never show a card, so they are left out of this page. The rest are split into
[read tools](#read-tools) (`review`) and [write tools](#write-tools) (`popup`).

The **Always allow buttons** column lists every rule scope the card can propose for that tool.
A scope is only offered when it actually contains the item on the card — "this folder" appears
only when the file has a parent folder, "if I own it" only when you own the file — and **every
scope that matches gets its own button**, so a file you own that also sits in a folder can show
both "Always allow — this folder" and "Always allow — if I own it". When no scope matches, or the
column is empty, the card offers only **Deny** and **Allow once**.

"unconditional" means the button has no scope at all: the rule it writes accepts every future call
of that operation (Gmail drafting is the one case).

Clicking a button opens a confirmation dialog that states the rule as a sentence and lists every
tool it covers. Confirming writes one rule to the `auto_accept:` section of `settings.yaml`. That
rule covers the scope's value (the folder, label, calendar, …) and only the one operation you just
approved — other operations on the same resource still ask. Cancelling the dialog still approves
the request on the card, once.

---

"""

_READ_INTRO = """## Read tools

"""

_WRITE_INTRO = """## Write tools

A write tool with a non-empty column can offer a rule scoped to the resource the call touched —
the folder, label, calendar, project, list, channel or chat — except the six Gmail draft tools,
whose rule is unconditional. Every other write tool offers only **Deny** and **Allow once**.
A few Drive, Docs and Sheets tools also start a short same-file grace window when you click
**Allow once**; that is not a rule, see
[Same-file grace window](approvals-and-policy.md#same-file-grace-window).

"""

_FOOTER = """
## Rules you can't create from a card

Some operations never offer an **Always allow** button because nothing on the card names a
resource to scope the rule to: Apps Script projects, Gmail filters, and creating a Slack group
chat. You can still allow them from **Settings → Auto-accept → Add a rule**, or by letting the AI
system propose a rule with `privacyfence_propose_policy_change`. Either way the rule is written
only after you confirm it. See [Approvals and policy](approvals-and-policy.md#always-allow-and-policy-rules).
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
    return ", ".join(scope.hint or "unconditional" for scope in scopes)


def _tools_by_connector(gate: str) -> dict[str, list[str]]:
    # Grouped by the same connector `_scope_candidates` resolves scopes against
    # (`propose.connector_of_operation`), not the tool's own namespace -- Sheets/Docs tools
    # address a Drive file and are governed by `drive.folder` rules, so they belong in the same
    # section a plain Drive file's tools do.
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
        lines.append("| Tool | Always allow buttons |")
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
