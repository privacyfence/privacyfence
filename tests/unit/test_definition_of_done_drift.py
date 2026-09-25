"""Drift guard between the definition of done and the two files that repeat it.

docs/coding-and-testing-guidelines.md §2.7 is the authoritative definition of done. The PR template
repeats it as a checklist and the /dod command runs it, and both used to fall behind when §2.7
gained a row. This checks that every command §2.7 names also appears in each copy, so adding one to
§2.7 without the other two fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDELINES = REPO_ROOT / "docs" / "coding-and-testing-guidelines.md"
COPIES = (
    REPO_ROOT / ".github" / "pull_request_template.md",
    REPO_ROOT / ".claude" / "commands" / "dod.md",
)

# A code span is a command when it starts with one of these; §2.7's other spans are paths, file
# names and identifiers, which the copies may legitimately phrase differently.
_COMMAND_PREFIXES = ("pytest ", "python3 ", "ruff ", "bandit ", "mypy ", "npm ", "scripts/")

# §2.7 wraps long commands across lines inside a single code span, so spans may contain newlines.
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _section_2_7() -> str:
    text = GUIDELINES.read_text(encoding="utf-8")
    start = text.index("### 2.7 ")
    end = text.index("\n### ", start + 1)
    return text[start:end]


def _commands(section: str) -> list[str]:
    spans = (_normalize(span) for span in _CODE_SPAN_RE.findall(section))
    return sorted({span for span in spans if span.startswith(_COMMAND_PREFIXES)})


def test_section_2_7_names_the_commands_this_guard_depends_on():
    # If the extraction ever matched nothing, every parametrized case below would vanish silently.
    commands = _commands(_section_2_7())
    assert "ruff check ." in commands
    assert "npm run dry-run" in commands
    assert any(command.startswith("pytest -v --cov=") for command in commands)


@pytest.mark.parametrize("copy", COPIES, ids=lambda path: path.name)
@pytest.mark.parametrize("command", _commands(_section_2_7()))
def test_every_section_2_7_command_appears_in_each_copy(command, copy):
    assert f"`{command}`" in _normalize(copy.read_text(encoding="utf-8")), (
        f"§2.7 of {GUIDELINES.name} names `{command}`, but {copy.relative_to(REPO_ROOT)} does not"
    )
