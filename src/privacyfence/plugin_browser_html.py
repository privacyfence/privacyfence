"""The ``/plugin-pages`` document body: every running plugin's page list, or one plugin's.

A pure renderer, with no web framework in it. Everything it prints is either a fixed sentence of
PrivacyFence's own or a field of a validated ``PageEntry``, and every value goes through
``html.escape``, the ``href`` included. The only script on the page is the shell's, so this
module emits none.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from .plugins.page_index import PageIndex
from .plugins.protocol import PageEntry

_MISSING = "—"

_CSS = """
.pf-plugin-pages { margin: 0 0 28px; container-type: inline-size; }
.pf-plugin-pages h2 { margin: 0 0 8px; font-size: 18px; color: var(--ink); }
.pf-plugin-pages table { width: 100%; border-collapse: collapse; }
.pf-plugin-pages th, .pf-plugin-pages td {
  text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid var(--line);
  overflow-wrap: anywhere;
}
.pf-plugin-pages th { color: var(--ink); font-weight: 600; background: var(--surface); }
.pf-plugin-pages td a { display: inline-block; min-height: 44px; line-height: 44px; }
.pf-plugin-pages-desc { margin-top: 2px; font-size: 13px; color: var(--ink); opacity: 0.75; }
.pf-plugin-pages-error, .pf-plugin-pages-empty { margin: 4px 0; color: var(--ink); }
@container (max-width: 640px) {
  .pf-plugin-pages table, .pf-plugin-pages tbody { display: block; }
  .pf-plugin-pages thead { display: none; }
  .pf-plugin-pages tr { display: block; padding: 8px 0; border-bottom: 1px solid var(--line); }
  .pf-plugin-pages td { display: block; padding: 2px 10px; border: 0; }
}
"""


def format_timestamp(value: str | None) -> str:
    """``YYYY-MM-DD HH:MM UTC`` for an RFC 3339 timestamp, converted to UTC; ``""`` for ``None``.
    A value that does not parse is returned as it came (the host has already validated entries,
    so this is only a safety net)."""
    if value is None:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError):
        return value


def _cell(value: str | None) -> str:
    return escape(value, quote=True) if value else _MISSING


def _row(name: str, entry: PageEntry) -> str:
    href = escape("/plugins/" + name + entry.path, quote=True)
    title = f'<a href="{href}" target="_blank" rel="noopener">{escape(entry.title, quote=True)}</a>'
    if entry.description:
        title += f'<div class="pf-plugin-pages-desc">{escape(entry.description, quote=True)}</div>'
    return (
        f"<tr><td>{title}</td><td>{_cell(entry.version)}</td>"
        f"<td>{_cell(format_timestamp(entry.created_at))}</td>"
        f"<td>{_cell(format_timestamp(entry.updated_at))}</td></tr>"
    )


def _section(index: PageIndex, *, single: bool) -> str:
    heading = "" if single else f"<h2>{escape(index.display_name, quote=True)}</h2>"
    if index.error:
        content = f'<p class="pf-plugin-pages-error">{escape(index.error, quote=True)}</p>'
    elif not index.entries:
        content = '<p class="pf-plugin-pages-empty">This plugin lists no pages.</p>'
    else:
        rows = "".join(_row(index.name, entry) for entry in index.entries)
        content = (
            "<table><thead><tr><th>Title</th><th>Version</th><th>Created</th><th>Updated</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return (
        f'<section class="pf-plugin-pages" data-plugin="{escape(index.name, quote=True)}">'
        f"{heading}{content}</section>"
    )


def render(indexes: list[PageIndex], *, single: bool, nonce: str) -> str:
    """The body for ``web_shell.wrap``. ``single`` is one plugin's own list (its display name as
    the heading, a link back to all plugins); otherwise every plugin's, under one heading each."""
    title = indexes[0].display_name if single and indexes else "Plugin pages"
    parts = [f'<style nonce="{escape(nonce, quote=True)}">{_CSS}</style>', f"<h1>{escape(title, quote=True)}</h1>"]
    if single:
        parts.append('<p><a href="/plugin-pages">All plugin pages</a></p>')
    if not single and not indexes:
        parts.append('<p id="no-plugins">No plugin with pages is running.</p>')
    parts.extend(_section(index, single=single) for index in indexes)
    return "".join(parts)


__all__ = ["format_timestamp", "render"]
