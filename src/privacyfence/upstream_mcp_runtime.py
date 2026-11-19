"""Runtime wiring for hosted, OAuth-authenticated upstream MCP servers (organization mode).

``daemon_main`` loads the operator file and calls ``configure``. From there this module is the one place
that knows the live upstream set (ADR 0329, ADR 0331): the registration store, token provider and client of
each upstream, the connector list for a principal, who may call, the administrator's tool-list flow
(``refresh`` builds a pending list in memory, ``save`` makes it the saved list and registers it into the
policy tables, ``set_gates`` changes the gates of saved tools), and the ``privacyfence_status`` and Admin
page rows.

There is no network call at start and no lifespan: a saved list is registered from disk, and an upstream
with no saved list exposes no tools until an administrator saves one. Until ``save``, AI clients keep seeing
the previously saved list whatever the server now says (the rug-pull guard).
"""
from __future__ import annotations

import dataclasses
import functools
import logging
import os
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

from . import upstream_mcp_registry
from .audit_log import AuditEntry, current_week, get_audit_logger
from .connectors.upstream_mcp import _UNAVAILABLE, UpstreamMcpConnector
from .org_mode import ConfigurationError
from .principal import Principal, current_principal
from .scoped_tokens import current_token_scope
from .upstream_mcp_client import (
    BearerSource,
    CallerNotAllowedError,
    UpstreamMcpClient,
    UpstreamMcpClientError,
    UpstreamToolCatalog,
)
from .upstream_mcp_config import UpstreamServer
from .upstream_oauth_discovery import RegistrationStore
from .upstream_oauth_tokens import (
    OAuthClient,
    UpstreamTokenProvider,
    load_token,
    token_path,
    token_paths,
)
from .upstream_tools import (
    GATES,
    SavedTool,
    ToolEntry,
    ToolStore,
    classify,
    diff,
    tool_digest,
    unsupported_reason,
)

logger = logging.getLogger(__name__)

MAX_ROW_DESCRIPTION_CHARS = 200
_SECRET_AUTH_METHODS = frozenset({"client_secret_post", "client_secret_basic"})

NOT_CONNECTED_TO_REFRESH = "Connect {label} on the Connect page (/connect) first, then refresh."
CONNECT_HINT = "Connect {label} on the Connect page (/connect)."
ADMIN_HINT = "An administrator has to refresh the tool list on the Admin page."


class UpstreamAdminError(Exception):
    """An administrator action was refused. ``str(exc)`` is the text to show; ``status`` the HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class UpstreamRefreshError(UpstreamAdminError):
    """The tool list could not be fetched (not connected, sign-in expired, server unreachable)."""


@dataclass(frozen=True)
class _PendingTool:
    entry: ToolEntry
    class_default: str
    verb: str
    state: str  # new | changed | unchanged | unsupported
    reason: str | None = None


@dataclass(frozen=True)
class _Pending:
    fetched_at: float
    tools: tuple[_PendingTool, ...]
    removed: tuple[str, ...]


@dataclass
class _Entry:
    upstream: UpstreamServer
    registrations: RegistrationStore
    tokens: UpstreamTokenProvider
    client: UpstreamMcpClient
    catalog: UpstreamToolCatalog
    saved: tuple[SavedTool, ...] = ()
    saved_at: str | None = None
    pending: _Pending | None = None


@dataclass
class _Runtime:
    entries: dict[str, _Entry]
    store: ToolStore
    last_fetch: float = field(default=0.0)


_runtime: _Runtime | None = None
_tools_changed: Callable[[], None] | None = None


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _client_source(upstream: UpstreamServer, registrations: RegistrationStore) -> Callable[[str], OAuthClient]:
    """The client a refresh authenticates as.

    A declared client with a secret picks ``client_secret_post`` or ``client_secret_basic`` from the
    server's metadata at Connect time, and after a restart ``client_for`` has not seen that metadata. The
    method that worked at Connect is on every saved token record, so it is read from there."""

    def source(upstream_id: str) -> OAuthClient:
        client = registrations.client_for(upstream_id)
        if client.client_secret is None or upstream.oauth.client_id is None:
            return client
        for path in token_paths(upstream_id):
            record = load_token(path)
            if record is not None and record.client_id == client.client_id and record.auth_method in _SECRET_AUTH_METHODS:
                return dataclasses.replace(client, auth_method=record.auth_method)
        return client

    return source


def configure(
    *,
    upstreams: Sequence[UpstreamServer],
    tool_store: ToolStore,
    registrations_dir: Path,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    """Build every upstream's parts and register its saved list (no network). Raises
    ``ConfigurationError`` for a declared secret variable that is empty."""
    global _runtime
    for upstream in upstreams:
        name = upstream.oauth.client_secret_env
        if name is not None and not os.environ.get(name):
            raise ConfigurationError(
                f'Upstream "{upstream.id}": oauth.client_secret_env names {name}, which is not set.'
            )
    entries: dict[str, _Entry] = {}
    for upstream in upstreams:
        registrations = RegistrationStore(registrations_dir, transport=transport, upstreams=[upstream])
        tokens = UpstreamTokenProvider(
            _client_source(upstream, registrations), on_invalid_client=registrations.forget, transport=transport,
        )
        entry = _Entry(
            upstream=upstream, registrations=registrations, tokens=tokens,
            client=UpstreamMcpClient(upstream, transport=transport), catalog=UpstreamToolCatalog(upstream),
        )
        entries[upstream.id] = entry
        if not upstream.oauth.scopes:
            logger.warning(
                "Upstream %s declares no oauth.scopes; it will ask for every scope the server lists.", upstream.id,
            )
        listed = tool_store.load(upstream.id)
        if listed is not None and listed.url != upstream.url:
            logger.warning(
                "Upstream %s: the saved tool list was made for another address and is ignored; refresh the tools.",
                upstream.id,
            )
            listed = None
        if listed is not None and listed.tools:
            entry.saved = listed.tools
            entry.saved_at = listed.saved_at
            entry.catalog.update(listed.tools)
            upstream_mcp_registry.get_registry().apply(upstream, listed.tools)
    _runtime = _Runtime(entries=entries, store=tool_store)


def configured() -> bool:
    return _runtime is not None


def reset() -> None:
    """Forget the configuration and the callback (tests)."""
    global _runtime, _tools_changed
    _runtime = None
    _tools_changed = None


def set_tools_changed_callback(callback: Callable[[], None] | None) -> None:
    """What to call after a save so AI clients are told the tool list changed."""
    global _tools_changed
    _tools_changed = callback


def upstream_ids() -> frozenset[str]:
    return frozenset(_runtime.entries) if _runtime is not None else frozenset()


def _entry(upstream_id: str) -> _Entry:
    entry = _runtime.entries.get(upstream_id) if _runtime is not None else None
    if entry is None:
        raise UpstreamAdminError("Unknown upstream.")
    return entry


def oauth_upstream(upstream_id: str) -> UpstreamServer | None:
    entry = _runtime.entries.get(upstream_id) if _runtime is not None else None
    return entry.upstream if entry is not None else None


def registrations_for(upstream_id: str) -> RegistrationStore:
    return _entry(upstream_id).registrations


def tokens_for(upstream_id: str) -> UpstreamTokenProvider:
    return _entry(upstream_id).tokens


# --------------------------------------------------------------------------- #
# Connectors and callers
# --------------------------------------------------------------------------- #


def admit_caller(upstream: UpstreamServer) -> None:
    """Raise ``CallerNotAllowedError`` when this caller's type may not use ``upstream``."""
    principal_type = "n8n_workflow" if current_token_scope() is not None else "claude_client"
    if principal_type not in upstream.allowed_principal_types:
        raise CallerNotAllowedError(principal_type)


def _bearer_source(entry: _Entry, path: Path) -> BearerSource:
    async def bearer(force_refresh: bool) -> str:
        return await entry.tokens.bearer(path, force_refresh)

    return bearer


def connectors_for_principal() -> list[UpstreamMcpConnector]:
    """One connector per upstream with a saved list and a sign-in of the current principal."""
    if _runtime is None:
        return []
    principal = current_principal()
    connectors: list[UpstreamMcpConnector] = []
    for entry in _runtime.entries.values():
        path = token_path(principal, entry.upstream.id)
        if not entry.saved or not path.exists():
            continue
        connectors.append(UpstreamMcpConnector(
            entry.upstream, entry.catalog, entry.client,
            admit=functools.partial(admit_caller, entry.upstream), bearer=_bearer_source(entry, path),
        ))
    return connectors


# --------------------------------------------------------------------------- #
# The administrator's tool list
# --------------------------------------------------------------------------- #


def _clean(text: str, limit: int) -> str:
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))[:limit]


def _audit(entry: _Entry, decision: str, tool: str, tool_name: str, summary: str) -> None:
    """One audit entry per administrator action; counts only, and a failed write changes nothing."""
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id="",
            connector=entry.upstream.id,
            tool=tool,
            tool_name=tool_name,
            summary=summary,
            sender="privacyfence",
            decision=decision,
            auto_accept_rule="",
            latency_seconds=0.0,
        ))
    except Exception as exc:  # noqa: BLE001 -- an audit failure never changes the outcome
        logger.warning("Audit log write failed: %s", exc)


def _to_entry(tool: Any) -> ToolEntry:
    hints = tool.annotations
    return ToolEntry(
        name=tool.name,
        description=tool.description or "",
        input_schema=tool.input_schema,
        read_only=hints.read_only_hint if hints is not None else None,
        destructive=hints.destructive_hint if hints is not None else None,
    )


def _offered_gate(entry: _Entry, tool: _PendingTool) -> str:
    """The gate the pending list offers: the saved gate only for a tool that did not change."""
    saved = {item.name: item.gate for item in entry.saved if tool.state == "unchanged"}
    return saved.get(tool.entry.name, tool.class_default)


def _pending_row(entry: _Entry, pending: _Pending) -> dict[str, Any]:
    prefix = entry.upstream.id
    rows = [
        {
            "name": tool.entry.name, "exposed_name": f"{prefix}_{tool.entry.name}", "state": tool.state,
            "verb": tool.verb, "default_gate": tool.class_default, "gate": _offered_gate(entry, tool),
            "description": _clean(tool.entry.description, MAX_ROW_DESCRIPTION_CHARS),
            **({"reason": tool.reason} if tool.reason is not None else {}),
        }
        for tool in pending.tools
    ]
    names = lambda state: [row["name"] for row in rows if row["state"] == state]  # noqa: E731
    return {
        "fetched_at": pending.fetched_at,
        "new": names("new"),
        "changed": names("changed"),
        "removed": list(pending.removed),
        "unsupported": [{"name": tool.entry.name, "reason": tool.reason} for tool in pending.tools if tool.state == "unsupported"],
        "tools": rows,
    }


async def refresh(upstream_id: str, principal: Principal) -> dict[str, Any]:
    """List the server's tools with ``principal``'s own connection and keep the result as the pending list.

    Nothing is exposed or changed until ``save``. Raises ``UpstreamRefreshError`` with the text to show."""
    assert _runtime is not None  # nosec B101 -- _entry raised already when it is not configured
    entry = _entry(upstream_id)
    label = entry.upstream.label
    try:
        tools = await entry.client.list_tools(bearer=_bearer_source(entry, token_path(principal, upstream_id)))
    except UpstreamMcpClientError as exc:
        if exc.kind == "not_connected":
            raise UpstreamRefreshError(NOT_CONNECTED_TO_REFRESH.format(label=label), 409) from None
        raise UpstreamRefreshError(_UNAVAILABLE.get(exc.kind, _UNAVAILABLE["unreachable"]).format(label=label), 502) from None
    fetched = tuple(
        (item, unsupported_reason(upstream_id, item)) for item in (_to_entry(tool) for tool in tools)
    )
    result = diff(entry.saved, fetched)
    groups = (
        [(item, "new", None) for item in result.new]
        + [(item, "changed", None) for item in result.changed]
        + [(item, "unchanged", None) for item in result.unchanged]
        + [(item, "unsupported", reason) for item, reason in result.unsupported]
    )
    pending_tools = tuple(
        _PendingTool(item, classify(item.read_only, item.destructive)[0], classify(item.read_only, item.destructive)[1], state, reason)
        for item, state, reason in sorted(groups, key=lambda group: group[0].name)
    )
    # Two refreshes never share a ``fetched_at``, so a save is bound to the list the administrator saw.
    stamp = max(time.time(), _runtime.last_fetch + 1e-6)
    _runtime.last_fetch = stamp
    entry.pending = _Pending(stamp, pending_tools, result.removed)
    _audit(
        entry, "upstream_tools_refreshed", "refresh_tools", f"{label}: refresh tools",
        f"Refreshed the tools of {upstream_id}: {len(result.new)} new, {len(result.changed)} changed, "
        f"{len(result.removed)} removed, {len(result.unsupported)} unsupported",
    )
    return _pending_row(entry, entry.pending)


def _checked_gates(gates: Mapping[str, str]) -> None:
    if any(gate not in GATES for gate in gates.values()):
        raise UpstreamAdminError("Gate must be review, popup or auto.")


def _install(entry: _Entry, tools: tuple[SavedTool, ...]) -> None:
    """Write ``tools`` as the saved list and register it; the previous state returns when either step fails."""
    upstream = entry.upstream
    registry = upstream_mcp_registry.get_registry()
    previous = entry.saved
    try:
        registry.apply(upstream, tools)
    except ConfigurationError as exc:
        raise UpstreamAdminError(str(exc), 409) from None
    now = datetime.now(timezone.utc)
    assert _runtime is not None  # nosec B101
    try:
        _runtime.store.save(upstream.id, upstream.url, tools, now)
    except BaseException:
        registry.apply(upstream, previous)
        raise
    entry.saved = tools
    entry.saved_at = now.isoformat()
    entry.catalog.update(tools)
    if _tools_changed is not None:
        _tools_changed()


def save(upstream_id: str, gates: Mapping[str, str], fetched_at: float) -> None:
    """Make the pending list the saved list with the chosen ``gates`` (an unchosen tool keeps its default)."""
    entry = _entry(upstream_id)
    _checked_gates(gates)
    pending = entry.pending
    if pending is None:
        raise UpstreamAdminError("Refresh the tool list first.", 409)
    if fetched_at != pending.fetched_at:
        raise UpstreamAdminError("The tool list changed since you refreshed it. Refresh again.", 409)
    chosen = [
        SavedTool(
            name=tool.entry.name, description=tool.entry.description, input_schema=tool.entry.input_schema,
            read_only=tool.entry.read_only, destructive=tool.entry.destructive,
            gate=gates.get(tool.entry.name, _offered_gate(entry, tool)), verb=tool.verb,
            digest=tool_digest(tool.entry),
        )
        for tool in pending.tools if tool.state != "unsupported"
    ]
    _install(entry, tuple(chosen))
    entry.pending = None
    _audit(
        entry, "upstream_tools_saved", "save_tools", f"{entry.upstream.label}: save tools",
        f"Saved {len(chosen)} tool(s) of {upstream_id}: "
        f"{sum(tool.gate == 'review' for tool in chosen)} review, {sum(tool.gate == 'popup' for tool in chosen)} popup, "
        f"{sum(tool.gate == 'auto' for tool in chosen)} auto",
    )


def set_gates(upstream_id: str, gates: Mapping[str, str]) -> None:
    """Change the gates of saved tools, with no refresh."""
    entry = _entry(upstream_id)
    _checked_gates(gates)
    if any(name not in {tool.name for tool in entry.saved} for name in gates):
        raise UpstreamAdminError("That tool is not in the saved list.")
    changed = tuple(dataclasses.replace(tool, gate=gates.get(tool.name, tool.gate)) for tool in entry.saved)
    _install(entry, changed)
    _audit(
        entry, "upstream_tool_gates_changed", "set_tool_gates", f"{entry.upstream.label}: change tool gates",
        f"Changed the gates of {len(gates)} tool(s) of {upstream_id}",
    )


# --------------------------------------------------------------------------- #
# Status and the Admin page
# --------------------------------------------------------------------------- #


def status() -> list[dict[str, object]]:
    """The ``upstreams`` rows of ``privacyfence_status``."""
    if _runtime is None:
        return []
    principal = current_principal()
    rows: list[dict[str, object]] = []
    for entry in _runtime.entries.values():
        label = entry.upstream.label
        connected = token_path(principal, entry.upstream.id).exists()
        row: dict[str, object] = {
            "id": entry.upstream.id,
            "label": label,
            "tools_exposed": len(entry.saved),
            "state": "ready" if entry.saved else "waiting_for_admin",
            "connected": connected,
        }
        if not entry.saved:
            row["admin_hint"] = ADMIN_HINT
        if not connected:
            row["connect_hint"] = CONNECT_HINT.format(label=label)
        rows.append(row)
    return rows


def _shown_url(url: str) -> str:
    """The scheme and host of ``url``, then ``/…`` when it has a path or query: either may carry a
    key, and the Admin page needs only which server calls go to."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    return origin + "/…" if parts.path.strip("/") or parts.query else origin


def _issuer_host(entry: _Entry, paths: list[Path]) -> str | None:
    """The pinned issuer's host, else the one a person's saved sign-in used; ``None`` before anyone connects."""
    if entry.upstream.oauth.issuer is not None:
        return urlsplit(entry.upstream.oauth.issuer).netloc
    for path in paths:
        record = load_token(path)
        if record is not None:
            return urlsplit(record.issuer).netloc
    return None


def admin_rows(principal: Principal | None = None) -> list[dict[str, object]]:
    """What the Admin page's Upstream MCP servers section shows. It carries no token, client id or person;
    ``principal`` (default: the current one) only decides ``admin_connected``."""
    if _runtime is None:
        return []
    who = principal if principal is not None else current_principal()
    rows: list[dict[str, object]] = []
    for entry in _runtime.entries.values():
        upstream = entry.upstream
        files = token_paths(upstream.id)
        rows.append({
            "id": upstream.id,
            "label": upstream.label,
            "url": _shown_url(upstream.url),
            "issuer_host": _issuer_host(entry, files),
            "registration": "declared" if upstream.oauth.client_id is not None else "dynamic",
            "scopes": list(upstream.oauth.scopes),
            "connected_people": len(files),
            "admin_connected": token_path(who, upstream.id).exists(),
            "allowed_principal_types": list(upstream.allowed_principal_types),
            "saved_at": entry.saved_at,
            "tools": [
                {
                    "name": tool.name, "exposed_name": f"{upstream.id}_{tool.name}", "verb": tool.verb,
                    "read_only": tool.read_only, "default_gate": classify(tool.read_only, tool.destructive)[0],
                    "gate": tool.gate, "description": _clean(tool.description, MAX_ROW_DESCRIPTION_CHARS),
                }
                for tool in entry.saved
            ],
            "pending": _pending_row(entry, entry.pending) if entry.pending is not None else None,
        })
    return rows

