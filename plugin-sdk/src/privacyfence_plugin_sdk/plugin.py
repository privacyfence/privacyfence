"""The plugin runtime: tool registry, prepared-call store, page routing and the stdio runner."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import blocks as _blocks
from ._files import (
    FILE_MEDIA_TYPES,
    FILE_PARAM_KEY,
    MAX_FILE_BYTES,
    MAX_FILE_PARAMS_PER_TOOL,
    IncomingFile,
    file_specs,
    parse_files,
    refuse_reserved_labels,
)
from ._page_index import validate_page_entries
from ._rpc import Peer, RpcError, open_stdio
from .responses import (
    ApprovalTicket,
    Bytes,
    ConfirmResult,
    DownloadedFile,
    Html,
    PageEntry,
    SourceError,
    SourceResult,
    Text,
    ToolDefinitionError,
)

logger = logging.getLogger("privacyfence_plugin_sdk")

PROTOCOL_VERSION = "1.3.0"
_PROTOCOL_MAJOR = 1

# --- _limits: copied from the protocol; a test compares each with the daemon's constants ---------
_MAX_LINE_BYTES = 16 * 1024 * 1024
_MAX_IN_FLIGHT = 16
_INVALID_LINES_LIMIT = 3
_INLINE_RESULT_BYTES = 100_000
_MAX_TITLE_CHARS = 120
_MAX_EFFECT_CHARS = 200
_MAX_DESCRIPTION_CHARS = 1024
_MAX_TOOLS = 64
_MAX_SCOPE_VALUES = 100
_MAX_SCOPE_VALUE_CHARS = 200
_MCP_TOOL_NAME_MAX = 64
_MAX_SCOPE_TYPES = 20
_MAX_SCOPE_TYPE_DESCRIPTION_CHARS = 500
_MAX_PAGE_PATH_CHARS = 512
_PREPARED_CALL_LIFETIME_SECONDS = 1200.0
_CONFIRM_AWAIT_MAX_MS = 300_000
_SOURCE_CALL_TIMEOUT_SECONDS = 120.0
_CONFIRM_REQUEST_TIMEOUT_SECONDS = 5.0
_PLUGIN_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")
_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")
_SCOPE_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")
_GATES = ("auto", "review", "popup")
_RESERVED_PLUGIN_NAMES = frozenset({
    "privacyfence", "plugin", "plugins", "settings", "mcp",
    "gmail", "drive", "contacts", "calendar", "tasks", "apps_script",
    "slack", "jira", "confluence", "salesforce", "telegram",
    "apps", "sheets", "docs",
})
# --- end of _limits ---------------------------------------------------------------------------

_MAX_PREPARED_CALLS = 256
_WAIT_MAX_ROUNDS = 12

_PARAM_TYPES = frozenset({"string", "integer", "number", "boolean"})
_FORBIDDEN_PARAM_KEYS = frozenset({"enum", "oneOf", "anyOf", "allOf", "items", "properties", "$ref"})
_EVENTS = (
    "connector.state_changed", "plugin.disabling", "shutdown", "principal.removed", "approval.revoked",
)
_PAGED_OPERATIONS = frozenset({"jira.search", "calendar.list_events"})
_OUTPUT_EXTENSIONS = {
    "application/json": (".json",),
    "text/csv": (".csv",),
    "text/html": (".html", ".htm"),
    "text/plain": (".txt",),
    "text/markdown": (".md",),
}
_OUTPUT_MAX_DEPTH = 8
_NO_OUTPUT_FOLDER = "this plugin has no output folder"
_CHUNK_ATTEMPTS = 2
_FILE_PROTOCOL_MINOR = 3  # the first protocol minor version whose daemon sends files

__all__ = [
    "PROTOCOL_VERSION", "Context", "PageRequest", "Plugin", "Prepared", "Principal", "ToolHandle",
]


def args_digest(args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    id: str
    display_name: str
    storage_dir: Path
    output_dir: Path | None = None
    output_types: tuple[str, ...] = ()


@dataclass
class Prepared:
    """What a tool call would release or do. ``state`` stays in the plugin until execute."""

    preview: list[dict]
    payload: list[dict] | None = None
    scopes: dict[str, list[str]] | None = None
    state: Any = None


@dataclass(frozen=True)
class PageRequest:
    principal: Principal
    method: str
    path: str
    query: dict[str, str]


def _source_rpc_error(exc: SourceError) -> RpcError:
    return RpcError(exc.code, exc.detail, extra={"reason": exc.reason} if exc.reason else None)


def _call_error(exc: RpcError) -> SourceError:
    return SourceError(exc.code, exc.detail, exc.extra.get("reason") if isinstance(exc.extra, dict) else None)


class _Host:
    """The plugin's view of the daemon: a late-bound peer."""

    def __init__(self) -> None:
        self.peer: Peer | None = None
        self.introspecting = False

    async def request(self, method: str, params: dict, timeout: float | None = None) -> Any:
        peer = self.peer
        if peer is None or peer.closed:
            raise SourceError("internal_error", "the plugin is not connected to PrivacyFence")
        try:
            return await peer.request(method, params, timeout=timeout)
        except RpcError as exc:
            raise _call_error(exc) from None


class SourceClient:
    """``ctx.source``: reads from a connected service on behalf of the principal."""

    def __init__(self, host: _Host, principal: Principal, data_dir: Path) -> None:
        self._host = host
        self._principal = principal
        self._data_dir = data_dir

    async def call(self, operation: str, **params: Any) -> SourceResult:
        if self._host.introspecting:
            raise SourceError("introspection_only", "source calls are refused while PrivacyFence introspects")
        if not self._principal.id:
            raise SourceError("unknown_principal", "this context has no principal")
        result = await self._host.request(
            "source.call",
            {"principal": self._principal.id, "operation": operation, "params": params},
            _SOURCE_CALL_TIMEOUT_SECONDS,
        )
        if not isinstance(result, dict):
            raise SourceError("internal_error", "malformed source.call result")
        size = result.get("bytes")
        cursor = result.get("next_cursor")
        return SourceResult(
            data=result.get("data"),
            bytes=size if isinstance(size, int) else 0,
            next_cursor=cursor if isinstance(cursor, str) else None,
        )

    async def pages(self, operation: str, **params: Any) -> AsyncIterator[SourceResult]:
        """Yield each page of ``operation`` until the host gives no ``next_cursor``.

        Pass ``page_size`` to size the pages. ``max_results`` and ``cursor`` are not accepted.
        """
        if "max_results" in params or "cursor" in params:
            raise ValueError("pages() takes page_size, not max_results or cursor")
        cursor: str | None = None
        while True:
            call_params = params if cursor is None else {**params, "cursor": cursor}
            result = await self.call(operation, **call_params)
            yield result
            if result.next_cursor is None:
                return
            cursor = result.next_cursor

    async def report_pages(
        self, report_id: str, *, page_by: str, columns: list[str] | None = None,
        filters: list[dict] | None = None,
    ) -> AsyncIterator[SourceResult]:
        """Yield each page of a Salesforce report, read in order of the unique column ``page_by``, until the host gives no ``next_cursor``."""
        params: dict[str, Any] = {"report_id": report_id, "page_by": page_by}
        if columns is not None:
            params["columns"] = columns
        if filters is not None:
            params["filters"] = filters
        seen: set[str] = set()
        async for page in self.pages("salesforce.report_run", **params):
            try:
                index = page.data["reportMetadata"]["detailColumns"].index(page_by)
                rows = page.data["factMap"]["T!T"]["rows"]
                keys = [json.dumps(row["dataCells"][index].get("value"), sort_keys=True) for row in rows]
            except (KeyError, ValueError, TypeError, IndexError, AttributeError):
                raise SourceError("internal_error", "malformed salesforce.report_run page") from None
            for key in keys:
                if key in seen:
                    raise SourceError(
                        "invalid_params", f"page_by {page_by!r} is not unique: a value repeats across pages",
                        reason="not_unique",
                    )
                seen.add(key)
            yield page

    async def collect(self, operation: str, **params: Any) -> list:
        """All items of a paged ``jira.search`` or ``calendar.list_events``, concatenated."""
        if operation not in _PAGED_OPERATIONS:
            raise ValueError(f"collect supports only {', '.join(sorted(_PAGED_OPERATIONS))}, not {operation}")
        items: list = []
        async for page in self.pages(operation, **params):
            if not isinstance(page.data, list):
                raise SourceError("internal_error", f"malformed {operation} page")
            items.extend(page.data)
        return items

    async def download(self, file_id: str, dest: Path | None = None) -> DownloadedFile:
        """Download a Drive file in chunks into the directory ``dest`` (default ``data_dir/downloads``).

        The file is named after ``file_id`` with characters outside ``[A-Za-z0-9._-]`` replaced.
        A file that changes while it downloads is restarted once; a second change raises
        ``SourceError`` with ``reason == "revision_changed"``.
        """
        directory = Path(dest) if dest is not None else self._data_dir / "downloads"
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", file_id)[:100].lstrip(".") or "download"
        target = directory / name
        part = directory / (name + ".part")
        for attempt in range(_CHUNK_ATTEMPTS):
            try:
                return await self._download_once(file_id, target, part)
            except SourceError as exc:
                part.unlink(missing_ok=True)
                if exc.reason == "revision_changed" and attempt + 1 < _CHUNK_ATTEMPTS:
                    continue
                raise
            except BaseException:
                part.unlink(missing_ok=True)
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def _download_once(self, file_id: str, target: Path, part: Path) -> DownloadedFile:
        cursor: str | None = None
        size = 0
        revision = ""
        mime_type = "application/octet-stream"
        with open(part, "wb") as handle:
            while True:
                if cursor is None:
                    result = await self.call("drive.download", file_id=file_id)
                else:
                    result = await self.call("drive.download", file_id=file_id, cursor=cursor)
                data = result.data
                if not isinstance(data, dict):
                    raise SourceError("internal_error", "malformed drive.download result")
                try:
                    chunk = base64.b64decode(data.get("content_base64", ""), validate=True)
                except (binascii.Error, ValueError, TypeError):
                    raise SourceError("internal_error", "malformed drive.download chunk") from None
                handle.write(chunk)
                size += len(chunk)
                revision = str(data.get("revision", revision))
                mime_type = str(data.get("mime_type", mime_type))
                if data.get("eof") is True:
                    break
                if not result.next_cursor:
                    raise SourceError("internal_error", "drive.download gave no cursor before the end")
                cursor = result.next_cursor
        os.replace(part, target)
        return DownloadedFile(path=target, size=size, revision=revision, mime_type=mime_type)


class ConfirmClient:
    """``ctx.confirm``: asks a human to confirm something on a card no rule can auto-accept."""

    def __init__(self, host: _Host, principal: Principal) -> None:
        self._host = host
        self._principal = principal

    async def request(self, kind: str, title: str, preview: list[dict], require_step_up: bool = True) -> str:
        if self._host.introspecting:
            raise SourceError("introspection_only", "confirmations are refused while PrivacyFence introspects")
        try:
            checked = _blocks.validate_blocks(preview)
        except ValueError as exc:
            raise SourceError("invalid_blocks", str(exc)) from None
        result = await self._host.request(
            "confirm.request",
            {
                "principal": self._principal.id,
                "kind": kind,
                "title": title,
                "preview": checked,
                "require_step_up": require_step_up,
            },
            _CONFIRM_REQUEST_TIMEOUT_SECONDS,
        )
        approval_id = result.get("approval_id") if isinstance(result, dict) else None
        if not isinstance(approval_id, str) or not approval_id:
            raise SourceError("internal_error", "malformed confirm.request result")
        return approval_id

    async def await_(self, approval_id: str, timeout_ms: int | None = None) -> ConfirmResult:
        params: dict[str, Any] = {"approval_id": approval_id}
        wait = _CONFIRM_AWAIT_MAX_MS
        if timeout_ms is not None:
            wait = max(0, min(int(timeout_ms), _CONFIRM_AWAIT_MAX_MS))
            params["timeout_ms"] = wait
        result = await self._host.request("confirm.await", params, wait / 1000 + 10.0)
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise SourceError("internal_error", "malformed confirm.await result")
        decided = result.get("decided_at")
        return ConfirmResult(status=result["status"], decided_at=decided if isinstance(decided, str) else None)

    async def wait(self, approval_id: str) -> ConfirmResult:
        """Wait until the card is approved, denied or expired."""
        for _ in range(_WAIT_MAX_ROUNDS):
            try:
                return await self.await_(approval_id)
            except SourceError as exc:
                if exc.code != "timeout":
                    raise
        raise SourceError("timeout", "the confirmation was not decided within an hour")


class ApprovalsClient:
    """``ctx.approvals``: asks a human to approve a thing that stays approved until it changes."""

    def __init__(self, host: _Host, principal: Principal) -> None:
        self._host = host
        self._principal = principal

    @staticmethod
    def digest(content: bytes | str) -> str:
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    async def request(
        self,
        kind: str,
        subject_id: str,
        content: bytes | str,
        title: str,
        preview: list[dict],
        page: str | None = None,
        require_step_up: bool = True,
    ) -> ApprovalTicket:
        """Ask for approval of ``content``. A page shown on the card receives ``pf_approval`` in its query."""
        if self._host.introspecting:
            raise SourceError("introspection_only", "approvals are refused while PrivacyFence introspects")
        try:
            checked = _blocks.validate_blocks(preview)
        except ValueError as exc:
            raise SourceError("invalid_blocks", str(exc)) from None
        params: dict[str, Any] = {
            "principal": self._principal.id,
            "kind": kind,
            "subject_id": subject_id,
            "digest": self.digest(content),
            "title": title,
            "preview": checked,
            "require_step_up": require_step_up,
        }
        if page is not None:
            params["page"] = page
        result = await self._host.request("approval.request", params, _CONFIRM_REQUEST_TIMEOUT_SECONDS)
        approval_id = result.get("approval_id") if isinstance(result, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(approval_id, str) or not approval_id or not isinstance(status, str):
            raise SourceError("internal_error", "malformed approval.request result")
        return ApprovalTicket(approval_id=approval_id, status=status)

    async def check(self, kind: str, subject_id: str, content: bytes | str) -> str:
        """``approved``, ``revoked`` or ``unknown`` for exactly this ``content``."""
        result = await self._host.request(
            "approval.check",
            {"principal": self._principal.id, "kind": kind, "subject_id": subject_id,
             "digest": self.digest(content)},
            _CONFIRM_REQUEST_TIMEOUT_SECONDS,
        )
        status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(status, str):
            raise SourceError("internal_error", "malformed approval.check result")
        return status

    async def await_(self, approval_id: str, timeout_ms: int | None = None) -> ConfirmResult:
        params: dict[str, Any] = {"approval_id": approval_id}
        wait = _CONFIRM_AWAIT_MAX_MS
        if timeout_ms is not None:
            wait = max(0, min(int(timeout_ms), _CONFIRM_AWAIT_MAX_MS))
            params["timeout_ms"] = wait
        result = await self._host.request("approval.await", params, wait / 1000 + 10.0)
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise SourceError("internal_error", "malformed approval.await result")
        decided = result.get("decided_at")
        return ConfirmResult(status=result["status"], decided_at=decided if isinstance(decided, str) else None)

    async def wait(self, approval_id: str) -> ConfirmResult:
        """Wait until the card is approved, denied or expired."""
        for _ in range(_WAIT_MAX_ROUNDS):
            try:
                return await self.await_(approval_id)
            except SourceError as exc:
                if exc.code != "timeout":
                    raise
        raise SourceError("timeout", "the confirmation was not decided within an hour")


class OutputsClient:
    """``ctx.outputs``: publishes files the principal's agent can list and read."""

    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    @property
    def dir(self) -> Path:
        if self._principal.output_dir is None:
            raise RuntimeError(_NO_OUTPUT_FOLDER)
        return self._principal.output_dir

    def publish(self, relpath: str, data: bytes | str) -> str:
        """Write ``data`` to ``relpath`` under the output folder, atomically; returns ``relpath``.

        Refuses an existing file (a new version is a new name), ``..`` or a leading ``/``, a dot-prefixed
        segment, and an extension the manifest's ``output_types`` do not allow.
        """
        root = self.dir
        segments = relpath.split("/") if isinstance(relpath, str) else []
        if (
            not segments or "\\" in relpath or ":" in relpath or "\0" in relpath
            or any(not seg or seg.startswith(".") for seg in segments)
        ):
            raise ValueError("an output path is relative, uses / and has no empty or dot-prefixed segment")
        if len(segments) > _OUTPUT_MAX_DEPTH:
            raise ValueError(f"an output path has at most {_OUTPUT_MAX_DEPTH} segments")
        allowed = tuple(
            ext for mime in self._principal.output_types for ext in _OUTPUT_EXTENSIONS.get(mime, ())
        )
        if os.path.splitext(segments[-1])[1].lower() not in allowed:
            raise ValueError(f"the extension of {segments[-1]} is not one of: {', '.join(allowed) or 'none'}")
        target = root.joinpath(*segments)
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"{relpath} already exists; publish a new version under a new name")
        temp = target.parent / f".{target.name}.tmp"
        try:
            with open(temp, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return relpath


class Context:
    """What a handler gets: who is asking, where its data lives, and the calls back to PrivacyFence."""

    def __init__(
        self, host: _Host, principal: Principal, data_dir: Path,
        files: Mapping[str, IncomingFile] | None = None,
    ) -> None:
        self._host = host
        self.files: Mapping[str, IncomingFile] = MappingProxyType(dict(files or {}))
        self.principal = principal
        self.data_dir = data_dir
        self.source = SourceClient(host, principal, data_dir)
        self.confirm = ConfirmClient(host, principal)
        self.approvals = ApprovalsClient(host, principal)
        self.outputs = OutputsClient(principal)

    @property
    def introspecting(self) -> bool:
        return self._host.introspecting


ExecuteFn = Callable[[Context, Prepared, dict], Awaitable[Any]]


class ToolHandle:
    """A registered tool. ``@handle.execute`` attaches the function that runs after approval."""

    def __init__(
        self, fn: Callable[[Context, dict], Awaitable[Prepared]], definition: dict
    ) -> None:
        self._fn = fn
        self.definition = definition
        self.name: str = definition["name"]
        self.read_only: bool = definition["read_only"]
        self._execute: ExecuteFn | None = None

    async def __call__(self, ctx: Context, args: dict) -> Prepared:
        return await self._fn(ctx, args)

    def execute(self, fn: ExecuteFn) -> ExecuteFn:
        if self._execute is not None:
            raise ToolDefinitionError(f"tool {self.name} already has an execute function")
        self._execute = fn
        return fn


@dataclass
class _PreparedEntry:
    tool: str
    digest: str
    prepared: Prepared
    expires: float
    files: dict[str, str] = field(default_factory=dict)  # parameter -> SHA-256


@dataclass
class _Registry:
    tools: dict[str, ToolHandle] = field(default_factory=dict)
    scope_types: dict[str, str] = field(default_factory=dict)
    pages: dict[str, Callable[[Context, PageRequest], Awaitable[Any]]] = field(default_factory=dict)
    events: dict[str, list[Callable[[Context, dict], Awaitable[None]]]] = field(default_factory=dict)
    purge: Callable[[Context, str, str | None], Awaitable[None]] | None = None
    page_index: Callable[[Context], Awaitable[list[PageEntry]]] | None = None


def _page_key(path: str) -> str:
    return "/" if path in ("", "/") else path


def _check_file_param(tool: str, name: str, schema: dict) -> None:
    where = f"parameter {name} of {tool}"
    if schema.get("type") != "string":
        raise ToolDefinitionError(f"{where}: a file parameter must have type string")
    spec = schema[FILE_PARAM_KEY]
    if not isinstance(spec, dict) or set(spec) != {"max_bytes", "media_types"}:
        raise ToolDefinitionError(f"{where}: {FILE_PARAM_KEY} takes max_bytes and media_types only")
    max_bytes = spec["max_bytes"]
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_FILE_BYTES:
        raise ToolDefinitionError(f"{where}: max_bytes must be 1 to {MAX_FILE_BYTES}")
    types = spec["media_types"]
    if (
        not isinstance(types, list)
        or not types
        or not all(isinstance(t, str) and t in FILE_MEDIA_TYPES for t in types)
        or len(set(types)) != len(types)
    ):
        raise ToolDefinitionError(f"{where}: media_types must be a non-empty list of distinct supported types")


def _validate_parameters(tool: str, params: dict, required: list[str]) -> dict:
    properties: dict[str, dict] = {}
    for name, spec in params.items():
        if not isinstance(name, str) or not name:
            raise ToolDefinitionError(f"parameter names of {tool} must be non-empty strings")
        if name == "reason":
            raise ToolDefinitionError(f"{tool} may not declare a parameter named reason; PrivacyFence adds it")
        if not isinstance(spec, dict) or spec.get("type") not in _PARAM_TYPES or _FORBIDDEN_PARAM_KEYS & set(spec):
            raise ToolDefinitionError(
                f"parameter {name} of {tool}: only string, integer, number and boolean are supported"
            )
        if FILE_PARAM_KEY in spec:
            _check_file_param(tool, name, spec)
        properties[name] = dict(spec)
    if sum(FILE_PARAM_KEY in spec for spec in properties.values()) > MAX_FILE_PARAMS_PER_TOOL:
        raise ToolDefinitionError(f"tool {tool} may take at most {MAX_FILE_PARAMS_PER_TOOL} file parameter")
    unknown = [r for r in required if r not in properties]
    if unknown:
        raise ToolDefinitionError(f"{tool}: required names an undeclared parameter {unknown[0]}")
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


class Plugin:
    """A PrivacyFence plugin: register tools, pages and handlers, then call ``run()``."""

    def __init__(self, name: str, version: str) -> None:
        if not _PLUGIN_NAME_RE.fullmatch(name):
            raise ValueError("plugin name must match [a-z][a-z0-9-]{1,30}")
        if name in _RESERVED_PLUGIN_NAMES:
            raise ValueError(f"plugin name {name!r} is reserved")
        if not isinstance(version, str) or not version:
            raise ValueError("plugin version must be a non-empty string")
        self.name = name
        self.version = version
        self._reg = _Registry()
        self._host = _Host()
        self._prepared: dict[str, _PreparedEntry] = {}
        self._clock: Callable[[], float] = time.monotonic
        self._data_dir = Path(".")
        self._principals: dict[str, Principal] = {}
        self._initialized = False
        self._daemon_minor = _FILE_PROTOCOL_MINOR
        self._warned_file_tools: set[str] = set()
        self._stop: asyncio.Event | None = None

    # ------------------------------------------------------------------ registration

    def scope_type(self, name: str, description: str) -> None:
        if not _SCOPE_TYPE_RE.fullmatch(name):
            raise ValueError("scope type name must match [a-z][a-z0-9_]{0,30}")
        if name == "output":
            raise ValueError("scope type output is reserved")
        if not isinstance(description, str) or not 1 <= len(description) <= _MAX_SCOPE_TYPE_DESCRIPTION_CHARS:
            raise ValueError(
                f"scope type {name} needs a description of 1 to {_MAX_SCOPE_TYPE_DESCRIPTION_CHARS} characters"
            )
        if name in self._reg.scope_types:
            raise ValueError(f"scope type {name} is invalid or already declared")
        if len(self._reg.scope_types) >= _MAX_SCOPE_TYPES:
            raise ValueError(f"at most {_MAX_SCOPE_TYPES} scope types are allowed")
        self._reg.scope_types[name] = description

    def tool(
        self,
        name: str,
        *,
        description: str,
        gate: str = "review",
        read_only: bool = False,
        destructive: bool = False,
        scopes: list[str] | tuple[str, ...] = (),
        params: dict[str, dict] | None = None,
        required: list[str] | tuple[str, ...] = (),
        title: str | None = None,
        effect: str | None = None,
    ) -> Callable[[Callable[[Context, dict], Awaitable[Prepared]]], ToolHandle]:
        definition = self._check_tool(
            name, description, gate, read_only, destructive, list(scopes), params or {}, list(required), title, effect
        )

        def register(fn: Callable[[Context, dict], Awaitable[Prepared]]) -> ToolHandle:
            handle = ToolHandle(fn, definition)
            self._reg.tools[name] = handle
            return handle

        return register

    def _check_tool(
        self, name: str, description: str, gate: str, read_only: bool, destructive: bool,
        scopes: list[str], params: dict, required: list[str], title: str | None, effect: str | None,
    ) -> dict:
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise ToolDefinitionError(f"tool name {name!r} must match [a-z][a-z0-9_]{{1,40}}")
        if name in self._reg.tools:
            raise ToolDefinitionError(f"tool {name} is already registered")
        if len(f"{self.name}_{name}") > _MCP_TOOL_NAME_MAX:
            raise ToolDefinitionError(f"the MCP name {self.name}_{name} is longer than {_MCP_TOOL_NAME_MAX} characters")
        if not isinstance(description, str) or not 1 <= len(description) <= _MAX_DESCRIPTION_CHARS:
            raise ToolDefinitionError(f"the description of {name} must be 1 to {_MAX_DESCRIPTION_CHARS} characters")
        if gate not in _GATES:
            raise ToolDefinitionError(f"gate of {name} must be one of {', '.join(_GATES)}")
        if read_only and destructive:
            raise ToolDefinitionError(f"tool {name} cannot be both read-only and destructive")
        if destructive and gate != "popup":
            raise ToolDefinitionError(f"destructive tool {name} must use the popup gate")
        for scope in scopes:
            if not isinstance(scope, str) or not _SCOPE_TYPE_RE.fullmatch(scope):
                raise ToolDefinitionError(f"scope {scope!r} of {name} does not match the scope type pattern")
        if title is not None and (not isinstance(title, str) or len(title) > _MAX_TITLE_CHARS):
            raise ToolDefinitionError(f"the title of {name} must be at most {_MAX_TITLE_CHARS} characters")
        if effect is not None and (not isinstance(effect, str) or len(effect) > _MAX_EFFECT_CHARS):
            raise ToolDefinitionError(f"the effect of {name} must be at most {_MAX_EFFECT_CHARS} characters")
        for field_name, text in (("title", title), ("effect", effect)):
            if text is not None and _blocks.clean_line(text) != text:
                raise ToolDefinitionError(
                    f"tool.{field_name} must not contain line breaks, tabs, control or bidirectional characters"
                )
        if len(self._reg.tools) >= _MAX_TOOLS:
            raise ToolDefinitionError(f"at most {_MAX_TOOLS} tools are allowed")
        parameters = _validate_parameters(name, params, required)
        if file_specs(parameters["properties"]):
            if read_only:
                raise ToolDefinitionError(f"tool {name} takes a file and cannot be read-only")
            if gate == "auto":
                raise ToolDefinitionError(f"tool {name} takes a file and must use the review or popup gate")
        definition: dict[str, Any] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "read_only": read_only,
            "destructive": destructive,
            "gate": gate,
            "scopes": scopes,
        }
        if effect is not None:
            definition["effect"] = effect
        if title is not None:
            definition["title"] = title
        return definition

    def page(self, path: str) -> Callable[[Callable[[Context, PageRequest], Awaitable[Any]]], Callable]:
        if not isinstance(path, str) or not (path == "" or path.startswith("/")):
            raise ValueError("a page path must start with /")
        key = _page_key(path)

        def register(fn: Callable[[Context, PageRequest], Awaitable[Any]]) -> Callable:
            if key in self._reg.pages:
                raise ValueError(f"page {key} is already registered")
            self._reg.pages[key] = fn
            return fn

        return register

    def page_index(
        self, fn: Callable[[Context], Awaitable[list[PageEntry]]]
    ) -> Callable[[Context], Awaitable[list[PageEntry]]]:
        """Register ``async def fn(ctx) -> list[PageEntry]``: the pages the page browser lists."""
        if self._reg.page_index is not None:
            raise ValueError("page_index is already registered")
        self._reg.page_index = fn
        return fn

    def on(self, event: str) -> Callable[[Callable[[Context, dict], Awaitable[None]]], Callable]:
        if event not in _EVENTS:
            raise ValueError(f"unknown event {event}; expected one of {', '.join(_EVENTS)}")

        def register(fn: Callable[[Context, dict], Awaitable[None]]) -> Callable:
            self._reg.events.setdefault(event, []).append(fn)
            return fn

        return register

    def on_purge(self, fn: Callable[[Context, str, str | None], Awaitable[None]]) -> Callable:
        if self._reg.purge is not None:
            raise ValueError("on_purge is already registered")
        self._reg.purge = fn
        return fn

    # ------------------------------------------------------------------ tool definitions

    def _offered_definitions(self) -> list[dict]:
        """``tool_definitions()`` without the tools this daemon is too old to send files to."""
        out = []
        for definition in self.tool_definitions():
            if self._daemon_minor < _FILE_PROTOCOL_MINOR and file_specs(definition["parameters"]["properties"]):
                if definition["name"] not in self._warned_file_tools:
                    self._warned_file_tools.add(definition["name"])
                    logger.warning(
                        "tool %s takes a file, which needs PrivacyFence with plugin protocol 1.3; it is not offered",
                        definition["name"],
                    )
                continue
            out.append(definition)
        return out

    def tool_definitions(self) -> list[dict]:
        """The ``ToolDef`` list sent at initialize and by ``tools_changed``; raises on a bad registry."""
        out = []
        for handle in self._reg.tools.values():
            if not handle.read_only and handle._execute is None:
                raise ToolDefinitionError(f"tool {handle.name} changes things and needs an execute function")
            for scope in handle.definition["scopes"]:
                if scope not in self._reg.scope_types:
                    raise ToolDefinitionError(f"scope type {scope} of {handle.name} is not declared")
            out.append(handle.definition)
        return out

    def _scope_types_wire(self) -> list[dict]:
        return [{"name": n, "description": d} for n, d in self._reg.scope_types.items()]

    async def tools_changed(self) -> None:
        """Tell PrivacyFence the tool list changed. Only tools reviewed at enable are accepted."""
        peer = self._host.peer
        if peer is None or peer.closed:
            raise RuntimeError("the plugin is not connected to PrivacyFence")
        await peer.notify("tools.changed", {"tools": self._offered_definitions()})

    # ------------------------------------------------------------------ runtime

    def _principal(self, raw: Any) -> Principal:
        """A principal from a ``PrincipalContext`` object, an id string, or ``None``."""
        if isinstance(raw, dict):
            principal = Principal(
                id=str(raw.get("id", "")),
                display_name=str(raw.get("display_name", "")),
                storage_dir=Path(str(raw.get("storage_dir", self._data_dir))),
                output_dir=Path(raw["output_dir"]) if isinstance(raw.get("output_dir"), str) else None,
                output_types=tuple(t for t in raw.get("output_types") or () if isinstance(t, str)),
            )
            self._principals[principal.id] = principal
            return principal
        if isinstance(raw, str) and raw:
            return self._principals.get(raw) or Principal(raw, "", self._data_dir)
        return Principal("", "", self._data_dir)

    def _ctx(self, raw: Any, files: Mapping[str, IncomingFile] | None = None) -> Context:
        return Context(self._host, self._principal(raw), self._data_dir, files)

    @staticmethod
    def _file_specs(handle: ToolHandle) -> dict[str, Mapping]:
        return file_specs(handle.definition["parameters"]["properties"])

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._prepared.items() if e.expires <= now]:
            del self._prepared[key]

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RpcError("invalid_request", "initialize has not been called")

    @staticmethod
    def _need(params: dict, key: str, kind: type) -> Any:
        value = params.get(key)
        if not isinstance(value, kind):
            raise RpcError("invalid_params", f"{key} must be of type {kind.__name__}")
        return value

    async def _initialize(self, params: dict) -> dict:
        version = self._need(params, "protocol_version", str)
        if version.partition(".")[0] != str(_PROTOCOL_MAJOR):
            raise RpcError("version_mismatch", f"this plugin speaks protocol {PROTOCOL_VERSION}")
        self._data_dir = Path(self._need(params, "data_dir", str))
        try:
            self._daemon_minor = int(version.split(".")[1])
        except (IndexError, ValueError):
            self._daemon_minor = 0
        self._host.introspecting = params.get("purpose") == "introspect"
        principals = params.get("principals", [])
        for raw in principals if isinstance(principals, list) else []:
            if isinstance(raw, dict):
                self._principal(raw)
        try:
            tools = self._offered_definitions()
        except ToolDefinitionError as exc:
            logger.error("tool definitions refused: %s", exc)
            raise RpcError("internal_error", "the plugin's tool definitions are invalid") from None
        self._initialized = True
        return {
            "protocol_version": PROTOCOL_VERSION,
            "plugin": {"name": self.name, "version": self.version},
            "scope_types": self._scope_types_wire(),
            "tools": tools,
        }

    async def _prepare(self, params: dict) -> dict:
        self._require_initialized()
        self._sweep()
        while len(self._prepared) >= _MAX_PREPARED_CALLS:
            del self._prepared[next(iter(self._prepared))]
            logger.warning("prepared-call store is full (%d); dropped the oldest prepared call", _MAX_PREPARED_CALLS)
        call_id = self._need(params, "call_id", str)
        handle = self._reg.tools.get(self._need(params, "tool", str))
        if handle is None:
            raise RpcError("unknown_tool", "the plugin has no such tool")
        args = self._need(params, "args", dict)
        files = parse_files(params.get("files"), self._file_specs(handle), with_content=False)
        ctx = self._ctx(self._need(params, "principal", dict), files)
        try:
            prepared = await handle(ctx, args)
        except SourceError as exc:
            raise _source_rpc_error(exc) from None
        if not isinstance(prepared, Prepared):
            raise RpcError("internal_error", "a tool function must return Prepared")
        try:
            preview = _blocks.validate_blocks(prepared.preview)
            payload = None
            if prepared.payload is not None:
                payload = _blocks.validate_blocks(prepared.payload, max_bytes=None)
            if files:
                refuse_reserved_labels(preview)
                refuse_reserved_labels(payload or [])
        except ValueError as exc:
            raise RpcError("invalid_blocks", str(exc)) from None
        if handle.read_only and payload is None:
            raise RpcError("invalid_params", "a read-only tool must return a payload")
        if not handle.read_only and payload is not None:
            raise RpcError("invalid_params", "only a read-only tool may return a payload")
        if (
            payload is not None
            and len(json.dumps({"blocks": payload}, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
            > _INLINE_RESULT_BYTES
        ):
            raise RpcError("payload_too_large", "the payload is larger than the inline result limit")
        scopes = self._check_scopes(handle, prepared.scopes)
        self._prepared[call_id] = _PreparedEntry(
            tool=handle.name,
            digest=args_digest(args),
            prepared=prepared,
            expires=self._clock() + _PREPARED_CALL_LIFETIME_SECONDS,
            files={p: f.sha256 for p, f in files.items()},
        )
        out: dict[str, Any] = {"preview": preview, "scopes": scopes}
        if payload is not None:
            out["payload"] = payload
        return out

    @staticmethod
    def _check_scopes(handle: ToolHandle, scopes: dict[str, list[str]] | None) -> dict[str, list[str]]:
        scopes = scopes or {}
        out: dict[str, list[str]] = {}
        for scope_type, values in scopes.items():
            if (
                not isinstance(values, (list, tuple))
                or len(values) > _MAX_SCOPE_VALUES
                or not all(isinstance(v, str) and 0 < len(v) <= _MAX_SCOPE_VALUE_CHARS for v in values)
            ):
                raise RpcError("invalid_params", f"scope values for {scope_type} are invalid")
            out[scope_type] = list(values)
        missing = [s for s in handle.definition["scopes"] if not out.get(s)]
        if missing:
            raise RpcError("invalid_params", f"the prepared call gave no values for scope {missing[0]}")
        return out

    async def _execute(self, params: dict) -> dict:
        self._require_initialized()
        self._sweep()
        call_id = self._need(params, "call_id", str)
        tool = self._need(params, "tool", str)
        args = self._need(params, "args", dict)
        given = self._need(params, "args_digest", str)
        approval = self._need(params, "approval", dict)
        entry = self._prepared.get(call_id)
        handle = self._reg.tools.get(tool)
        if entry is None or handle is None or entry.tool != tool:
            raise RpcError("unknown_call", "no prepared call with this id")
        if given != entry.digest or args_digest(args) != entry.digest:
            raise RpcError("digest_mismatch", "the arguments differ from the prepared call")
        files = parse_files(params.get("files"), self._file_specs(handle), with_content=True)
        if {p: f.sha256 for p, f in files.items()} != entry.files:
            raise RpcError("digest_mismatch", "the files differ from the prepared call")
        if not handle.read_only or approval.get("via") == "auto":
            del self._prepared[call_id]
        ctx = self._ctx(self._need(params, "principal", dict), files)
        result: Any = None
        if handle._execute is not None:
            try:
                result = await handle._execute(ctx, entry.prepared, approval)
            except SourceError as exc:
                raise _source_rpc_error(exc) from None
        try:
            size = len(json.dumps(result, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise RpcError("internal_error", "the execute result is not JSON") from None
        if size > _INLINE_RESULT_BYTES:
            raise RpcError("payload_too_large", "the result is larger than the inline result limit")
        return {"result": result}

    async def _web_request(self, params: dict) -> dict:
        self._require_initialized()
        request = PageRequest(
            principal=self._principal(self._need(params, "principal", dict)),
            method=str(params.get("method", "GET")),
            path=_page_key(str(params.get("path", "/"))),
            query={str(k): str(v) for k, v in (params.get("query") or {}).items()},
        )
        handler = self._reg.pages.get(request.path)
        if handler is None:
            return Text("Not found", status=404).to_wire()
        try:
            response = await handler(Context(self._host, request.principal, self._data_dir), request)
        except Exception:
            logger.warning("page %s failed", request.path, exc_info=True)
            return Text("Internal error", status=500).to_wire()
        if not isinstance(response, (Html, Text, Bytes)):
            logger.warning("page %s returned %s, not Html, Text or Bytes", request.path, type(response).__name__)
            return Text("Internal error", status=500).to_wire()
        return response.to_wire()

    async def _pages_list(self, params: dict) -> dict:
        self._require_initialized()
        fn = self._reg.page_index
        assert fn is not None  # the handler is registered only when an index exists
        ctx = Context(self._host, self._principal(self._need(params, "principal", dict)), self._data_dir)
        entries = await fn(ctx)
        if not isinstance(entries, list) or not all(isinstance(e, PageEntry) for e in entries):
            raise RpcError("invalid_params", "pages must be a list of PageEntry")
        try:
            pages = validate_page_entries([e.to_wire() for e in entries])
        except ValueError as exc:
            raise RpcError("invalid_params", str(exc)) from None
        return {"pages": pages}

    async def _purge(self, params: dict) -> dict:
        self._require_initialized()
        scope = self._need(params, "scope", str)
        if scope not in ("all", "install", "principal"):
            raise RpcError("invalid_params", "scope must be all, install or principal")
        principal = params.get("principal")
        principal = principal if isinstance(principal, str) and principal else None
        if scope == "principal" and principal is None:
            raise RpcError("invalid_params", "a principal purge needs a principal")
        if self._reg.purge is not None:
            await self._reg.purge(self._ctx(principal), scope, principal)
        return {"purged": True}

    def _event_handler(self, event: str) -> Callable[[dict], Awaitable[None]]:
        async def handle(params: dict) -> None:
            ctx = self._ctx(params.get("principal"))
            try:
                for fn in self._reg.events.get(event, []):
                    try:
                        await fn(ctx, params)
                    except Exception:
                        logger.warning("handler for %s failed", event, exc_info=True)
            finally:
                if event == "shutdown" and self._stop is not None:
                    self._stop.set()

        return handle

    async def serve(self, reader: Any, writer: Any) -> None:
        """Run the protocol over a reader and writer until ``shutdown`` or end of input."""
        self.tool_definitions()  # a bad registry fails here, not on the first initialize
        self._initialized = False
        self._stop = asyncio.Event()
        handlers = {
            "initialize": self._initialize,
            "tool.prepare": self._prepare,
            "tool.execute": self._execute,
            "web.request": self._web_request,
            "storage.purge": self._purge,
        }
        if self._reg.page_index is not None:
            handlers["pages.list"] = self._pages_list
        notifications = {event: self._event_handler(event) for event in _EVENTS}
        peer = Peer(
            reader,
            writer,
            handlers=handlers,
            notification_handlers=notifications,
            max_line_bytes=_MAX_LINE_BYTES,
            max_in_flight=_MAX_IN_FLIGHT,
            invalid_lines_limit=_INVALID_LINES_LIMIT,
        )
        self._host.peer = peer
        await peer.start()
        stop_task = asyncio.ensure_future(self._stop.wait())
        closed_task = asyncio.ensure_future(peer.wait_closed())
        try:
            await asyncio.wait({stop_task, closed_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_task.cancel()
            closed_task.cancel()
            self._host.peer = None
            await peer.close()

    async def serve_stdio(self) -> None:
        reader, writer = await open_stdio(_MAX_LINE_BYTES)
        await self.serve(reader, writer)

    def run(self) -> None:
        """Serve over stdin and stdout until PrivacyFence sends ``shutdown`` or closes the pipe."""
        asyncio.run(self.serve_stdio())
