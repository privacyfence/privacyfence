"""Startup wiring for upstream MCP servers (organization mode, ADR 0285, 0286, 0287).

``daemon_main`` loads the operator file, registers the declared tools and builds the claim signer,
then calls ``configure``. From there this module is the one place that knows the live upstream
set: the connector list for a principal, the caller claim for one dispatched call (the only code
that reads the caller contextvars on behalf of an upstream call, so connector code never does),
the domains a person holds, the ``privacyfence_status`` rows, and the lifespan that fetches each
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

from . import upstream_claims
from .connectors.upstream_mcp import UpstreamMcpConnector
from .principal import Principal, current_principal
from .scoped_tokens import current_token_scope
from .upstream_claims import CallerClaim, ClaimSigner
from .upstream_mcp_client import UpstreamMcpClient, UpstreamToolCatalog
from .upstream_mcp_config import GrantStore, UpstreamServer

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
    grants: GrantStore


_runtime: _Runtime | None = None


def configure(
    *, upstreams: Sequence[UpstreamServer], signer: ClaimSigner, grants: GrantStore,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    global _runtime
    entries = {
        upstream.id: _Entry(
            upstream, UpstreamToolCatalog(upstream), UpstreamMcpClient(upstream, signer, transport=transport),
        )
        for upstream in upstreams
    }
    _runtime = _Runtime(entries=entries, signer=signer, grants=grants)


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


def caller_for(upstream: UpstreamServer) -> CallerClaim:
    """Who is calling ``upstream`` right now, and which of its data domains they may see."""
    assert _runtime is not None, "upstream_mcp_runtime is not configured"  # nosec B101
    principal = current_principal()
    scope = current_token_scope()
    if scope is None:
        verified = principal.email_verified
        domains = _runtime.grants.domains_for(upstream.id, principal.email, verified)
    else:
        verified = bool(getattr(scope, "principal_email_verified", False))
        held = _runtime.grants.domains_for(upstream.id, scope.principal_email, verified)
        chosen = getattr(scope, "upstream_domains", {}).get(upstream.id, ())
        domains = tuple(sorted(set(held) & set(chosen)))
    return CallerClaim(
        sub=principal.id,
        email=principal.email if verified else "",
        email_verified=verified,
        domains=domains,
        act=upstream_claims.actor_from_context(),
    )


def held_domains(principal: Principal, email_verified: bool) -> dict[str, frozenset[str]]:
    """The domains ``principal`` holds, per upstream id (what a scoped token may narrow)."""
    if _runtime is None:
        return {}
    return {
        upstream_id: frozenset(_runtime.grants.domains_for(upstream_id, principal.email, email_verified))
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
