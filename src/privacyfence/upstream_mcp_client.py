"""The client for a hosted upstream MCP server, and its tool catalog.

``UpstreamMcpClient`` opens a fresh Streamable HTTP session for every call and authenticates it with the
calling person's OAuth bearer token (``BearerAuth``), so a connection is never shared across calls or
people. The bearer goes only to the declared server's origin and PrivacyFence sends no claim of its own.
``UpstreamToolCatalog`` turns an administrator's saved tool list into ``ToolSpec`` values.

See ADR 0329 (hosted MCP servers with per-person OAuth) and ADR 0331 (the administrator's saved list).
"""
from __future__ import annotations

import copy
import logging
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .connector import ToolParam, ToolSpec
from .upstream_mcp_config import UpstreamServer
from .upstream_oauth_tokens import UpstreamOAuthError, origin_of
from .upstream_tools import SavedTool, classify, person_gate

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 1_000_000
MAX_DESCRIPTION_CHARS = 700
_REASON_DESCRIPTION = "One sentence: why are you calling this tool right now?"
_OAUTH_KINDS = frozenset({"not_connected", "auth_expired", "insufficient_scope"})

# ``force_refresh`` in, a bearer token out; raises ``UpstreamOAuthError``.
BearerSource = Callable[[bool], Awaitable[str]]


class CallerNotAllowedError(Exception):
    """The caller's principal type may not use this upstream (raised by the runtime, caught by the connector)."""

    def __init__(self, principal_type: str) -> None:
        super().__init__(principal_type)
        self.principal_type = principal_type


class UpstreamMcpClientError(Exception):
    """A call to the upstream failed. ``kind`` is ``not_connected``, ``auth_expired``, ``insufficient_scope``,
    ``unreachable``, ``too_large`` or ``tool_error``."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class CallResult:
    text: str
    structured: dict | None
    dropped_blocks: int


class BearerAuth(httpx2.Auth):
    """Adds ``Authorization: Bearer <token>`` to a request for ``origin`` and to no other.

    After each response it records the first error status it saw (``status``, else the last status)
    and whether a 403 asked for more scope (``insufficient_scope``), so the client can tell an expired
    token from a missing scope even when the SDK reports both as a failed request."""

    def __init__(self, token: str, origin: str) -> None:
        self._token = token
        self._origin = origin
        self.status: int | None = None
        self.insufficient_scope = False

    def auth_flow(self, request: httpx2.Request) -> Any:
        if origin_of(str(request.url)) == self._origin:
            request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if self.status is None or self.status < 400:
            self.status = response.status_code
        if response.status_code == 403 and "insufficient_scope" in response.headers.get("www-authenticate", ""):
            self.insufficient_scope = True


class UpstreamMcpClient:
    def __init__(self, upstream: UpstreamServer, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._upstream = upstream
        self._transport = transport

    async def _token(self, bearer: BearerSource, force_refresh: bool) -> str:
        try:
            return await bearer(force_refresh)
        except UpstreamOAuthError as exc:
            raise UpstreamMcpClientError(exc.kind if exc.kind in _OAUTH_KINDS else "unreachable") from None

    async def _run(self, bearer: BearerSource, action: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        """Open a session, run ``action(session)`` once, close.

        A 401 while the session is being initialised is retried once with a refreshed token on a fresh
        session; a 401 after that point is ``auth_expired`` and the call is never sent again."""
        upstream = self._upstream
        origin = origin_of(upstream.url)
        for attempt in (0, 1):
            auth = BearerAuth(await self._token(bearer, attempt == 1), origin)
            initialized = False
            try:
                async with httpx2.AsyncClient(
                    auth=auth, timeout=httpx2.Timeout(60.0, connect=10.0), transport=self._transport,
                ) as http:
                    async with streamable_http_client(upstream.url, http_client=http) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            initialized = True
                            return await action(session)
            except Exception:  # noqa: BLE001 -- the cause may quote the upstream; the caller gets a fixed kind
                if auth.insufficient_scope:
                    raise UpstreamMcpClientError("insufficient_scope") from None
                if auth.status == 401:
                    if not initialized and attempt == 0:
                        continue
                    raise UpstreamMcpClientError("auth_expired") from None
                logger.warning("Upstream %s could not be reached", upstream.id)
                raise UpstreamMcpClientError("unreachable") from None
        raise AssertionError("unreachable")  # pragma: no cover -- the second attempt always returns or raises

    async def list_tools(self, *, bearer: BearerSource) -> list[types.Tool]:
        async def action(session: ClientSession) -> list[types.Tool]:
            return list((await session.list_tools()).tools)

        return await self._run(bearer, action)  # type: ignore[no-any-return]

    async def call_tool(self, tool: str, arguments: dict[str, Any], *, bearer: BearerSource) -> CallResult:
        async def action(session: ClientSession) -> types.CallToolResult:
            return await session.call_tool(tool, arguments)

        result = await self._run(bearer, action)
        texts: list[str] = []
        dropped = 0
        for block in result.content:
            if isinstance(block, types.TextContent):
                texts.append(block.text)
            else:
                dropped += 1
        text = "\n".join(texts)
        if len(text) > MAX_RESULT_CHARS:
            raise UpstreamMcpClientError("too_large")
        if result.is_error:
            raise UpstreamMcpClientError("tool_error", text)
        structured = result.structured_content
        return CallResult(
            text=text, structured=structured if isinstance(structured, dict) else None, dropped_blocks=dropped,
        )


def _clean(text: str) -> str:
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))


def person_gate_of(tool: SavedTool) -> str:
    """The gate a person meets for ``tool``: ``review`` or ``popup``, never ``auto`` (ADR 0331)."""
    return person_gate(tool.gate, classify(tool.read_only, tool.destructive)[0])


def _gate_sentence(gate: str) -> str:
    if gate == "review":
        return " Requires user approval: PrivacyFence shows the result to the person before it is returned."
    return " Requires user approval: PrivacyFence asks the person before the call is sent."


class UpstreamToolCatalog:
    def __init__(self, upstream: UpstreamServer, saved: tuple[SavedTool, ...] = ()) -> None:
        self._upstream = upstream
        self._saved = tuple(saved)

    def update(self, saved: tuple[SavedTool, ...]) -> None:
        self._saved = tuple(saved)

    def saved_tool(self, exposed_name: str) -> SavedTool | None:
        for tool in self._saved:
            if f"{self._upstream.id}_{tool.name}" == exposed_name:
                return tool
        return None

    def _spec(self, tool: SavedTool) -> ToolSpec:
        label = self._upstream.label
        description = _clean(tool.description or "")[:MAX_DESCRIPTION_CHARS]
        description += f" Returns what the {label} server answers." + _gate_sentence(person_gate_of(tool))
        schema = copy.deepcopy(tool.input_schema)
        properties = schema.setdefault("properties", {})
        properties["reason"] = {"type": "string", "description": _REASON_DESCRIPTION}
        schema["required"] = [*schema.get("required", []), "reason"]
        return ToolSpec(
            name=f"{self._upstream.id}_{tool.name}",
            description=description,
            params=[ToolParam("reason", "str", required=True, description=_REASON_DESCRIPTION)],
            read_only=tool.verb == "read",
            destructive=tool.verb == "delete",
            input_schema=schema,
        )

    def specs(self) -> list[ToolSpec]:
        return [self._spec(tool) for tool in self._saved]
