"""Read one section, or one fenced block of it, out of a Markdown document by heading name.

Shared by the tests that parse operator documents for the configs they tell people to copy, so each
document stays the only copy and a renumbered section does not break the lookup.
"""

from __future__ import annotations

import re
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6}) +(?:\d+\. +)?(.+?)\s*$")


def section(doc: Path, title: str) -> str:
    """The body of the section headed ``title``, up to the next heading at its level or above."""
    lines = doc.read_text(encoding="utf-8").splitlines()
    start: int | None = None
    level = 0
    in_fence = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        # A "# comment" inside a fenced config is not a heading.
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            continue
        if start is None:
            if match.group(2) == title:
                start, level = index + 1, len(match.group(1))
        elif len(match.group(1)) <= level:
            return "\n".join(lines[start:index])
    assert start is not None, f"no section headed {title!r} in {doc.name}"
    return "\n".join(lines[start:])


def fenced_block(doc: Path, title: str, language: str) -> str:
    """The one fenced block of ``language`` (``""`` for a plain fence) in the section headed ``title``."""
    blocks: list[str] = re.findall(rf"^```{language}\n(.*?)^```$", section(doc, title), flags=re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, f"section {title!r} has {len(blocks)} ```{language} blocks, expected one"
    return blocks[0]
