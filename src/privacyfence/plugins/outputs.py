"""Files a plugin publishes, and the two tools that expose them (ADR 0130).

A plugin with ``outputs: true`` writes into a per-principal folder (``storage.output_dir``).
PrivacyFence reads it through its own tools: ``plugin_outputs_list`` is auto and audited,
``plugin_outputs_read`` is a review-gated read of one file, a page at a time. The approval is keyed
to the file's sha256, so a file rewritten after its card gets a new card.

Only canonical paths are accepted: a path reaches the policy context, and a folder rule is a
string-prefix match on it, so ``a/./b.csv``, ``a\\..\\b.csv`` or a path through a symlink must
never be a second spelling of a file a rule covers. Anything that is not exactly the path the
file resolves to is "No such output file."
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from privacyfence import auto_accept
from privacyfence.audit_log import AuditEntry, current_week, get_audit_logger
from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.approval_window_html import NARROW, WIDE
from privacyfence.gate import current_reason, gated_call
from privacyfence.plugins import cursors
from privacyfence.plugins.constants import (
    AUDIT_PLUGIN_OUTPUT,
    OUTPUT_LIST_PAGE,
    OUTPUT_MAX_DEPTH,
    OUTPUT_READ_PAGE_BYTES,
    OUTPUT_TYPES,
)
from privacyfence.policy import propose, scopes
from privacyfence.policy.registry import Verb

logger = logging.getLogger(__name__)

OWNER = "plugin_outputs"
LIST_TOOL = "plugin_outputs_list"
READ_TOOL = "plugin_outputs_read"
READ_OPERATION = "plugin_outputs.read"
_LIST_OPERATION = "plugin_outputs.list"        # the cursor's operation name
NO_SUCH_FILE = "No such output file."
_REASON = "One sentence: why are you calling this tool right now?"
_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class OutputFile:
    path: str
    size: int
    modified: str       # RFC 3339 UTC
    mime_type: str


def _suffixes(output_types: tuple[str, ...]) -> dict[str, str]:
    """Extension -> MIME type for the plugin's declared types."""
    return {ext: mime for mime in output_types for ext in OUTPUT_TYPES.get(mime, ())}


def _mime_of(name: str, suffixes: dict[str, str]) -> str | None:
    return suffixes.get(os.path.splitext(name)[1].lower())


def _rfc3339(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def list_outputs(
    root: Path, output_types: tuple[str, ...], *, prefix: str = "", after: str | None = None,
    limit: int = OUTPUT_LIST_PAGE,
) -> tuple[list[OutputFile], str | None]:
    """Published files sorted by path: those after ``after`` that start with ``prefix``.

    The second value is the last path of a full page when more follow, else ``None``. A file is
    published when it is a regular file (never a symlink) at most ``OUTPUT_MAX_DEPTH`` segments
    down, with no dot-prefixed segment, and an extension of one of ``output_types``.
    """
    suffixes = _suffixes(output_types)
    try:
        base = root.resolve(strict=True)
    except OSError:
        return [], None
    found: list[OutputFile] = []

    def walk(directory: str, parts: tuple[str, ...]) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            rel = (*parts, entry.name)
            if len(rel) > OUTPUT_MAX_DEPTH:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                walk(entry.path, rel)
            elif stat.S_ISREG(info.st_mode):
                mime = _mime_of(entry.name, suffixes)
                if mime is not None:
                    found.append(OutputFile("/".join(rel), info.st_size, _rfc3339(info.st_mtime), mime))

    walk(str(base), ())
    found.sort(key=lambda f: f.path)
    matching = [f for f in found if f.path.startswith(prefix) and (after is None or f.path > after)]
    page = matching[:limit]
    more = len(matching) > limit
    return page, (page[-1].path if more and page else None)


def _canonical_file(root: Path, output_types: tuple[str, ...], path: Any) -> tuple[Path, str, os.stat_result]:
    """The resolved file, its MIME type and its ``lstat`` result for a canonical ``path``, else
    ``ValueError``."""
    if not isinstance(path, str) or not path or "\\" in path or ":" in path or "\0" in path:
        raise ValueError(NO_SUCH_FILE)
    segments = path.split("/")
    if len(segments) > OUTPUT_MAX_DEPTH or any(not s or s.startswith(".") for s in segments):
        raise ValueError(NO_SUCH_FILE)
    mime = _mime_of(segments[-1], _suffixes(output_types))
    if mime is None:
        raise ValueError(NO_SUCH_FILE)
    try:
        base = root.resolve(strict=True)
        current = root
        checked = None
        for index, segment in enumerate(segments):
            current = current / segment
            checked = os.lstat(current)
            mode = checked.st_mode
            last = index == len(segments) - 1
            if not (stat.S_ISREG(mode) if last else stat.S_ISDIR(mode)):
                raise ValueError(NO_SUCH_FILE)
        resolved = (root / path).resolve(strict=True)
        canonical = PurePosixPath(resolved.relative_to(base)).as_posix()
    except (OSError, ValueError):
        raise ValueError(NO_SUCH_FILE) from None
    if canonical != path:
        raise ValueError(NO_SUCH_FILE)
    return resolved, mime, checked


def _complete_end(chunk: bytes) -> int:
    """Length of ``chunk`` without a trailing, cut-off UTF-8 character."""
    n = len(chunk)
    for back in range(1, min(4, n) + 1):
        byte = chunk[n - back]
        if byte & 0xC0 == 0x80:
            continue
        if byte < 0xC0:
            return n
        need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
        return n - back if need > back and n - back > 0 else n
    return n


def read_output(
    root: Path, output_types: tuple[str, ...], path: str, *, offset: int = 0,
    max_bytes: int = OUTPUT_READ_PAGE_BYTES,
) -> dict:
    """One page of a published file: bytes ``[offset, offset + max_bytes)``, cut back to a UTF-8
    character boundary unless it ends the file. ``sha256`` is that of the whole file."""
    file, mime, checked = _canonical_file(root, output_types, path)
    if offset < 0:
        raise ValueError("Offset must not be negative.")
    digest = hashlib.sha256()
    window = bytearray()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(file, flags)
    except OSError:
        raise ValueError(NO_SUCH_FILE) from None
    try:
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    try:
        with handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(checked, opened):
                raise ValueError(NO_SUCH_FILE)
            while block := handle.read(_HASH_CHUNK):
                digest.update(block)
                lo = max(offset - size, 0)
                if lo < len(block) and len(window) < max_bytes:
                    window += block[lo:lo + max_bytes - len(window)]
                size += len(block)
    except OSError:
        raise ValueError(NO_SUCH_FILE) from None
    if offset > size or (offset == size and size > 0):
        raise ValueError("Offset is past the end of the file.")
    chunk = bytes(window)
    if offset + len(chunk) < size:
        chunk = chunk[:_complete_end(chunk)]
    end = offset + len(chunk)
    return {
        "path": path, "mime_type": mime, "size": size, "sha256": digest.hexdigest(),
        "offset": offset, "length": len(chunk), "next_offset": end if end < size else None,
        "text": chunk.decode("utf-8", errors="replace"),
    }


PluginTable = dict[str, tuple[str, Path, tuple[str, ...]]]


class PluginOutputsConnector(Connector):
    """``plugin_outputs_list`` and ``plugin_outputs_read`` over every enabled outputs plugin.

    ``plugins`` maps plugin name -> (display name, output root, output types).
    """

    def __init__(self, plugins: Callable[[], PluginTable]) -> None:
        self._plugins = plugins
        self._registered: set[str] = set()

    @property
    def name(self) -> str:
        return OWNER

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=LIST_TOOL,
                description=(
                    "List files a plugin has published to its output folder. Returns path, size, "
                    "modified time and type, 200 per page, with next_cursor for more. Use "
                    "plugin_outputs_read to read one. Runs without asking."
                ),
                params=[
                    ToolParam("plugin", "str", description="Name of the plugin whose output folder to list."),
                    ToolParam("prefix", "str", required=False, default="",
                              description="Only list paths starting with this text, such as a folder name and a slash."),
                    ToolParam("cursor", "str", required=False, default="",
                              description="The next_cursor of the previous page; empty for the first page."),
                ],
                read_only=True,
            ),
            ToolSpec(
                name=READ_TOOL,
                description=(
                    "Read one published plugin output file, about 90 KB per call. Returns the text, "
                    "its offset and next_offset to continue, and the whole file's sha256. Use "
                    "plugin_outputs_list to find paths. Shows an approval card unless a rule allows "
                    "this folder."
                ),
                params=[
                    ToolParam("plugin", "str", description="Name of the plugin that published the file."),
                    ToolParam("path", "str", description="Path of the file from plugin_outputs_list, with forward slashes."),
                    ToolParam("offset", "int", required=False, default=0,
                              description="Byte offset to start at; use next_offset from the previous call."),
                    ToolParam("reason", "str", required=True, description=_REASON),
                ],
                read_only=True,
            ),
        ]

    # ── Registration ──────────────────────────────────────────────────

    def register(self) -> None:
        """Register both tools and, for each outputs plugin now enabled, its selector and
        proposal. Calling it again reconciles with the current plugin set."""
        auto_accept.register_internal_dynamic_tools(OWNER, [
            auto_accept.DynamicToolSpec(
                tool=LIST_TOOL, gate="auto", operation=None, verb=None, layout=NARROW,
                effect="", scope_predicates=(),
            ),
            auto_accept.DynamicToolSpec(
                tool=READ_TOOL, gate="review", operation=READ_OPERATION, verb=Verb.READ, layout=WIDE,
                effect="", scope_predicates=(),
            ),
        ])
        current = set(self._plugins())
        for name in self._registered - current:
            scopes.unregister_plugin_output_selector(name)
        propose.unregister_dynamic_scope_entries(OWNER)
        for name in sorted(current):
            scopes.register_plugin_output_selector(name)
            propose.register_dynamic_scope_entry(OWNER, READ_TOOL, propose.plugin_output_scope_entry(name))
        self._registered = current

    def unregister(self) -> None:
        propose.unregister_dynamic_scope_entries(OWNER)
        for name in self._registered:
            scopes.unregister_plugin_output_selector(name)
        self._registered = set()
        auto_accept.unregister_internal_dynamic_tools(OWNER)

    # ── Calls ─────────────────────────────────────────────────────────

    def _plugin(self, name: Any) -> tuple[str, Path, tuple[str, ...]]:
        entry = self._plugins().get(name) if isinstance(name, str) else None
        if entry is None:
            raise RuntimeError(f"No plugin named {name} publishes outputs.")
        return entry

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == LIST_TOOL:
            return await self._list(args.get("plugin"), args.get("prefix") or "", args.get("cursor") or "")
        if tool == READ_TOOL:
            return await self._read(args.get("plugin"), args.get("path"), int(args.get("offset") or 0))
        raise ValueError(f"Unknown plugin output tool: {tool!r}")

    async def _list(self, plugin: Any, prefix: str, cursor: str) -> dict:
        started = time.time()
        display, root, types = self._plugin(plugin)
        bound = {"plugin": plugin, "prefix": prefix}
        after = None
        if cursor:
            after = cursors.decode(cursor, _LIST_OPERATION, bound).get("a")
            if not isinstance(after, str):
                raise cursors.CursorError("cursor is not valid")
        files, last = await asyncio.to_thread(
            list_outputs, root, types, prefix=prefix, after=after, limit=OUTPUT_LIST_PAGE,
        )
        self._auto_audit(
            f"List {display} outputs", f"list {plugin}; prefix={prefix!r}; files={len(files)}", started,
        )
        return {
            "plugin": plugin,
            "files": [{"path": f.path, "size": f.size, "modified": f.modified, "mime_type": f.mime_type} for f in files],
            "next_cursor": cursors.encode(_LIST_OPERATION, bound, {"a": last}) if last else None,
        }

    async def _read(self, plugin: Any, path: Any, offset: int) -> dict:
        started = time.time()
        display, root, types = self._plugin(plugin)
        result = await asyncio.to_thread(read_output, root, types, path, offset=offset)
        result["plugin"] = plugin
        length = result["length"]
        await gated_call(
            connector=self.name,
            tool=READ_TOOL,
            tool_name=f"Read {display} output",
            summary=result["path"],
            sender="",
            raw_data={"plugin": plugin, "path": result["path"]},
            filtered_data=result,
            gate="review",
            preview={
                "Plugin": display, "File": result["path"], "Size": f"{result['size']} bytes",
                "Part": f"{offset}-{offset + length}",
            },
            details_text=result["text"],
            pii_scan_text=result["text"],
            args={"plugin": plugin, "path": result["path"], "offset": offset},
            dedupe_extra=result["sha256"],
        )
        self._record(
            f"plugin:{plugin}", READ_TOOL, f"Read {display} output",
            f"read {result['path']}; offset={offset}; bytes={length}", AUDIT_PLUGIN_OUTPUT, "", started,
        )
        return result

    # ── Audit ─────────────────────────────────────────────────────────

    def _auto_audit(self, tool_name: str, summary: str, started: float) -> None:
        self._record(self.name, LIST_TOOL, tool_name, summary, "auto_accepted", "auto", started)

    @staticmethod
    def _record(connector: str, tool: str, tool_name: str, summary: str, decision: str, rule: str, started: float) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=connector,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender="",
                decision=decision,
                auto_accept_rule=rule,
                latency_seconds=time.time() - started,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)


__all__ = [
    "NO_SUCH_FILE", "OutputFile", "PluginOutputsConnector", "list_outputs", "read_output",
]
