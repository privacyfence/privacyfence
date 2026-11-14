"""Startup wiring for upstream MCP servers (organization mode, ADR 0285, 0286, 0287).

``daemon_main`` loads the operator file, registers the declared tools and builds the claim signer,
then calls ``configure``. From there this module is the one place that knows the live upstream
set: the connector list for a principal, the caller claim for one dispatched call (the only code
that reads the caller contextvars on behalf of an upstream call, so connector code never does),
the audiences a person holds, the ``privacyfence_status`` rows, and the lifespan that fetches each
upstream's tool list once at startup, retrying with backoff until it succeeds. There is no periodic
refresh: a description changing mid-session is a rug-pull vector.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

import httpx2

from .agent_identity import current_agent, current_client_id
from . import group_membership
from .group_membership import MembershipResolver
from .connectors.upstream_mcp import UpstreamMcpConnector
from .principal import Principal, current_principal
from .scoped_tokens import current_token_scope
from .identity_claim import CallerNotAllowedError, ClaimSigner, IdentityCaller
from .upstream_mcp_client import UpstreamMcpClient, UpstreamToolCatalog
from .upstream_mcp_config import GroupMapStore, UpstreamServer

logger = logging.getLogger(__name__)

BACKOFF_START_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0

_sleep = asyncio.sleep  # tests patch this, not asyncio.sleep


@dataclass
class _Entry:
    upstream: UpstreamServer
    catalog: UpstreamToolCatalog
    client: UpstreamMcpClient
    listed: bool = False


@dataclass
class _Runtime:
    entries: dict[str, _Entry]
    signer: ClaimSigner
    groups: GroupMapStore
    resolver: MembershipResolver
    membership_max_additions: int = group_membership.DEFAULT_MAX_ADDITIONS


_runtime: _Runtime | None = None


def configure(
    *, upstreams: Sequence[UpstreamServer], signer: ClaimSigner, groups: GroupMapStore,
    resolver: MembershipResolver,
    membership_max_additions: int = group_membership.DEFAULT_MAX_ADDITIONS,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    global _runtime
    entries = {
        upstream.id: _Entry(
            upstream, UpstreamToolCatalog(upstream), UpstreamMcpClient(upstream, signer, transport=transport),
        )
        for upstream in upstreams
    }
    _runtime = _Runtime(entries=entries, signer=signer, groups=groups, resolver=resolver,
                       membership_max_additions=membership_max_additions)


def configured() -> bool:
    return _runtime is not None


def reset() -> None:
    """Forget the configuration (tests)."""
    global _runtime
    _runtime = None


def signer() -> ClaimSigner | None:
    return _runtime.signer if _runtime is not None else None


def upstream_ids() -> frozenset[str]:
    return frozenset(_runtime.entries) if _runtime is not None else frozenset()


def connectors_for_principal() -> list[UpstreamMcpConnector]:
    """One connector per upstream; the catalog and client are shared, the caller is resolved per call."""
    if _runtime is None:
        return []
    return [
        UpstreamMcpConnector(
            entry.upstream, entry.catalog, entry.client, caller=functools.partial(caller_for, entry.upstream),
        )
        for entry in _runtime.entries.values()
    ]


def audiences_for_email(upstream_id: str, email: str, verified: bool) -> frozenset[str]:
    """The audiences ``email`` holds on ``upstream_id`` now; empty unless the email is verified."""
    assert _runtime is not None, "upstream_mcp_runtime is not configured"  # nosec B101
    address = email.strip().lower()
    if not verified or not address:
        return frozenset()
    return _runtime.resolver.audiences_for(address, _runtime.groups.group_audiences(upstream_id))


def all_group_audiences() -> dict[str, frozenset[str]]:
    """Group name to audiences, merged over every upstream (the same group in two upstreams unions its audiences)."""
    if _runtime is None:
        return {}
    merged: dict[str, frozenset[str]] = {}
    for upstream_id in _runtime.entries:
        for group, audiences in _runtime.groups.group_audiences(upstream_id).items():
            merged[group] = merged.get(group, frozenset()) | audiences
    return merged


def max_additions() -> int:
    """The snapshot addition cap from the operator file's ``membership`` section."""
    if _runtime is None:
        return group_membership.DEFAULT_MAX_ADDITIONS
    return _runtime.membership_max_additions


def _native_tool_names(upstream: UpstreamServer, entry: _Entry | None, allowed: frozenset[str] | None) -> tuple[str, ...]:
    """The upstream's own names of the exposed tools (the declared name, without the ``<id>_`` prefix)."""
    if entry is None:
        return ()
    exposed = {spec.name for spec in entry.catalog.specs()}
    return tuple(sorted(
        declared.name for declared in upstream.tools
        if declared.exposed_name in exposed and (allowed is None or declared.exposed_name in allowed)
    ))


def caller_for(upstream: UpstreamServer) -> IdentityCaller:
    """Who is calling ``upstream`` right now, and which of its audiences they may see (plan 3.1, 3.3)."""
    assert _runtime is not None, "upstream_mcp_runtime is not configured"  # nosec B101
    principal = current_principal()
    scope = current_token_scope()
    entry = _runtime.entries.get(upstream.id)
    if scope is None:
        principal_type = "claude_client"
        audiences = audiences_for_email(upstream.id, principal.email, principal.email_verified)
        act: dict[str, str] = {}
        client_id = current_client_id()
        if client_id:
            act["sub"] = client_id
        agent = current_agent()
        if agent.is_attested():
            act["agent"] = agent.id
        caller = IdentityCaller(
            sub=principal.id, principal_type=principal_type,
            email=principal.email if principal.email_verified else None,
            audiences=tuple(sorted(audiences)), tools=_native_tool_names(upstream, entry, None),
            act=act or None,
        )
    else:
        principal_type = "n8n_workflow"
        verified = bool(getattr(scope, "principal_email_verified", False))
        held = audiences_for_email(upstream.id, scope.principal_email, verified)
        chosen = getattr(scope, "upstream_audiences", {}).get(upstream.id, ())
        caller = IdentityCaller(
            sub=f"n8n-token:{scope.id}", principal_type=principal_type,
            audiences=tuple(sorted(held & frozenset(chosen))),
            tools=_native_tool_names(upstream, entry, frozenset(scope.tools)),
            approved_by=scope.principal_id, token_id=scope.id,
        )
    if principal_type not in upstream.allowed_principal_types:
        raise CallerNotAllowedError(principal_type)
    return caller


def held_audiences(principal: Principal, email_verified: bool) -> dict[str, frozenset[str]]:
    """The audiences ``principal`` holds, per upstream id (what a scoped token may narrow)."""
    if _runtime is None:
        return {}
    return {
        upstream_id: audiences_for_email(upstream_id, principal.email, email_verified)
        for upstream_id in _runtime.entries
    }


def status() -> list[dict[str, object]]:
    """The ``upstreams`` rows of ``privacyfence_status``."""
    if _runtime is None:
        return []
    rows: list[dict[str, object]] = []
    for entry in _runtime.entries.values():
        rows.append({
            "id": entry.upstream.id,
            "label": entry.upstream.label,
            "tools_exposed": len(entry.catalog.specs()),
            "state": "ready" if entry.listed else "waiting_for_tool_list",
            "missing_tools": entry.catalog.missing() if entry.listed else [],
        })
    return rows


async def _fetch_tool_list(entry: _Entry, notify_tools_changed: Callable[[], None]) -> None:
    delay = BACKOFF_START_SECONDS
    while True:
        try:
            tools = await entry.client.list_tools()
        except Exception:  # noqa: BLE001 -- the client already logged the cause; retry whatever failed
            logger.warning(
                "Upstream %s did not list its tools; retrying in %d seconds", entry.upstream.id, int(delay),
            )
            await _sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_SECONDS)
            continue
        entry.catalog.update(tools)
        entry.listed = True
        missing = entry.catalog.missing()
        if missing:
            logger.warning("Upstream %s lacks declared tools: %s", entry.upstream.id, ", ".join(missing))
        logger.info("Upstream %s ready with %d tool(s)", entry.upstream.id, len(entry.catalog.specs()))
        notify_tools_changed()
        return


@contextlib.asynccontextmanager
async def lifespan(notify_tools_changed: Callable[[], None]) -> AsyncIterator[None]:
    """One fetch task per upstream while the server runs; cancelled on shutdown."""
    tasks = [
        asyncio.create_task(_fetch_tool_list(entry, notify_tools_changed))
        for entry in (_runtime.entries.values() if _runtime is not None else ())
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
