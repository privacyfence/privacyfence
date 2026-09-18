"""Markdown -> HTML / plain-text rendering for rich-text email bodies.

Deliberately minimal and independent from drive_client.py's Markdown -> Google
Docs API parser: supports bold, italic, links, lists, paragraphs, two heading
levels, and ==highlight== only -- the subset that makes sense in an email
body. Not attempting CommonMark compliance, tables, or nested lists.

Gmail's own web compose UI has no true semantic H1/H2 -- only inline
font-size presets ("Small / Normal / Large / Huge"). `#`/`##` are mapped to
those presets (Large/Huge) rather than raw `<h1>`/`<h2>` tags, since raw
heading tags render inconsistently across mail clients; see
markdown_to_html's HEADING_SIZES.
"""

from __future__ import annotations

import html as _html
import re as _re
from typing import NamedTuple

from privacyfence.url_safety import is_safe_url

# Matches drive_client.py's `==text==` highlight syntax for consistency
# across the two tools, though the two parsers are otherwise independent.
_HIGHLIGHT_COLOR = "#fff59d"

_INLINE_RE = _re.compile(
    r"\*\*\*(.+?)\*\*\*"          # bold + italic
    r"|\*\*(.+?)\*\*"             # bold
    r"|\*(.+?)\*"                 # italic
    r"|==(.+?)=="                 # highlight
    r"|\[([^\]]+)\]\(([^)]+)\)"   # link [text](url)
)


class _InlineRun(NamedTuple):
    text: str
    bold: bool = False
    italic: bool = False
    highlight: bool = False
    url: str = ""


def _parse_inline_runs(text: str) -> list[_InlineRun]:
    runs: list[_InlineRun] = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            runs.append(_InlineRun(text[last : m.start()]))
        if m.group(1):  # bold + italic
            runs.append(_InlineRun(m.group(1), bold=True, italic=True))
        elif m.group(2):  # bold
            runs.append(_InlineRun(m.group(2), bold=True))
        elif m.group(3):  # italic
            runs.append(_InlineRun(m.group(3), italic=True))
        elif m.group(4):  # highlight
            runs.append(_InlineRun(m.group(4), highlight=True))
        elif m.group(5):  # link
            url = m.group(6)
            runs.append(_InlineRun(m.group(5), url=url if is_safe_url(url) else ""))
        last = m.end()
    if last < len(text):
        runs.append(_InlineRun(text[last:]))
    return runs


class _Block(NamedTuple):
    kind: str  # "para", "list", or "heading"
    ordered: bool
    lines: list[str]
    level: int = 0  # heading level (1 or 2); unused for "para"/"list"


# `#`/`##` only -- Gmail's compose UI offers just two font-size presets above
# Normal (Large, Huge), so there's nothing for a third `###`+ level to map
# to. A `###`+ prefix is therefore left unrecognized and falls through to a
# plain paragraph, same as before this syntax existed.
_HEADING_RE = _re.compile(r"^(#{1,2})\s+(.*)")


def _parse_blocks(markdown: str) -> list[_Block]:
    """Split markdown source into paragraph, list, and heading blocks.

    A blank line always ends the current block. Consecutive non-blank lines
    that aren't list items or headings join one paragraph (rendered with a
    line break between them, not reflowed into one line) -- short emails are
    usually intentionally line-broken, unlike long-form prose. Consecutive
    list-item lines of the same kind (bullet vs numbered) join one list
    block; switching kind starts a new block. A heading line always starts
    its own single-line block, even directly adjacent to a paragraph or
    another heading. Nested/indented lists are not supported.
    """
    blocks: list[_Block | None] = []
    for raw_line in markdown.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            blocks.append(None)  # sentinel: ends the current block
            continue

        bullet_match = _re.match(r"^[-*+]\s+(.*)", line)
        numbered_match = _re.match(r"^\d+\.\s+(.*)", line)
        if bullet_match or numbered_match:
            ordered = numbered_match is not None
            item_text = (bullet_match or numbered_match).group(1)
            top = blocks[-1] if blocks else None
            if top is not None and top.kind == "list" and top.ordered == ordered:
                top.lines.append(item_text)
            else:
                blocks.append(_Block("list", ordered, [item_text]))
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            blocks.append(_Block("heading", False, [heading_match.group(2)], level))
            continue

        top = blocks[-1] if blocks else None
        if top is not None and top.kind == "para":
            top.lines.append(line)
        else:
            blocks.append(_Block("para", False, [line]))

    return [b for b in blocks if b is not None]


def _render_inline(text: str) -> str:
    chunks = []
    for run in _parse_inline_runs(text):
        rendered = _html.escape(run.text)
        if run.url:
            rendered = f'<a href="{_html.escape(run.url)}">{rendered}</a>'
        if run.bold:
            rendered = f"<b>{rendered}</b>"
        if run.italic:
            rendered = f"<i>{rendered}</i>"
        if run.highlight:
            rendered = f'<span style="background-color:{_HIGHLIGHT_COLOR}">{rendered}</span>'
        chunks.append(rendered)
    return "".join(chunks)


# `#` (Heading 1) -> Gmail's "Large" preset, `##` (Heading 2) -> "Huge".
# Keyword CSS sizes, not px, matching what Gmail's own compose UI emits for
# these two presets so a heading built here renders the same as one a human
# picked from Gmail's own Size menu, in Gmail and elsewhere alike.
HEADING_SIZES = {1: "large", 2: "xx-large"}


def markdown_to_html(markdown: str) -> str:
    """Render the supported Markdown subset (bold, italic, ==highlight==,
    links, lists, paragraphs, `#`/`##` headings) as an HTML fragment for an
    email's text/html part. All literal text is HTML-escaped; only the
    constructs above ever produce markup, and link hrefs are restricted to
    http/https/mailto.
    """
    if not markdown or not markdown.strip():
        return ""
    parts: list[str] = []
    for block in _parse_blocks(markdown):
        if block.kind == "list":
            tag = "ol" if block.ordered else "ul"
            items = "".join(f"<li>{_render_inline(item)}</li>" for item in block.lines)
            parts.append(f"<{tag}>{items}</{tag}>")
        elif block.kind == "heading":
            size = HEADING_SIZES[block.level]
            parts.append(
                f'<p><b style="font-size:{size}">{_render_inline(block.lines[0])}</b></p>'
            )
        else:
            parts.append(f"<p>{'<br>'.join(_render_inline(line) for line in block.lines)}</p>")
    return "".join(parts)


def _plain_inline(text: str) -> str:
    chunks = []
    for run in _parse_inline_runs(text):
        chunk = run.text
        if run.url:
            chunk = f"{chunk} ({run.url})"
        chunks.append(chunk)
    return "".join(chunks)


def markdown_to_plain(markdown: str) -> str:
    """Strip the same Markdown subset back to readable plain text, for the
    text/plain alternative part when a caller supplies only body_markdown.
    Links render as "text (url)", matching html_to_text.py's convention for
    the same inbound/outbound tradeoff.
    """
    if not markdown or not markdown.strip():
        return ""
    parts: list[str] = []
    for block in _parse_blocks(markdown):
        if block.kind == "list":
            lines = [
                f"{f'{i + 1}.' if block.ordered else '-'} {_plain_inline(item)}"
                for i, item in enumerate(block.lines)
            ]
            parts.append("\n".join(lines))
        else:
            parts.append("\n".join(_plain_inline(line) for line in block.lines))
    return "\n\n".join(parts)
