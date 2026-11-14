"""The client for an operator-declared upstream MCP server, and its tool catalog.

``UpstreamMcpClient`` opens a fresh Streamable HTTP session for every call, signed with a claim
minted for that one request (``upstream_claims.ClaimAuth``), so a connection is never shared across
calls or principals. ``UpstreamToolCatalog`` turns the tools an upstream listed at startup into
``ToolSpec`` values for the declared tools only, and skips any whose schema this release cannot pass
through safely.

See ADR 0285 (upstream MCP servers as a connector) and ADR 0286 (the signed caller claim).
"""
from __future__ import annotations

import copy
import json
import logging
import unicodedata
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .connector import ToolParam, ToolSpec
from .upstream_claims import CallerClaim, ClaimAuth, ClaimSigner, service_caller
from .upstream_mcp_config import READ_VERBS, DeclaredTool, UpstreamServer

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 1_000_000
MAX_DESCRIPTION_CHARS = 700
MAX_SCHEMA_BYTES = 32 * 1024
_REASON_DESCRIPTION = "One sentence: why are you calling this tool right now?"
_REF_KEYS = frozenset({"$ref", "$defs", "definitions"})


class UpstreamMcpClientError(Exception):
    """A call to the upstream failed. ``kind`` is ``unreachable``, ``too_large`` or ``tool_error``."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class CallResult:
    text: str
    structured: dict | None
    dropped_blocks: int


class UpstreamMcpClient:
    def __init__(
        self, upstream: UpstreamServer, signer: ClaimSigner, *, transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._upstream = upstream
        self._signer = signer
        self._transport = transport

    async def _run(self, caller: CallerClaim, tool: str | None, action: Any) -> Any:
        """Open a session, run ``action(session)`` once, close. Any transport failure is ``unreachable``."""
        auth = ClaimAuth(self._signer, self._upstream, caller, tool)
        try:
            async with httpx2.AsyncClient(
                auth=auth, timeout=httpx2.Timeout(60.0, connect=10.0), transport=self._transport,
            ) as http:
                async with streamable_http_client(self._upstream.url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await action(session)
        except Exception:  # noqa: BLE001 -- the cause may quote the upstream; the caller gets a fixed message
            logger.warning("Upstream %s could not be reached", self._upstream.id, exc_info=True)
            raise UpstreamMcpClientError("unreachable") from None

    async def list_tools(self) -> list[types.Tool]:
        async def action(session: ClientSession) -> list[types.Tool]:
            return list((await session.list_tools()).tools)

        return await self._run(service_caller(), None, action)

    async def call_tool(self, tool: str, arguments: dict[str, Any], caller: CallerClaim) -> CallResult:
        async def action(session: ClientSession) -> types.CallToolResult:
            return await session.call_tool(tool, arguments)

        result = await self._run(caller, tool, action)
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
        return CallResult(text=text, structured=structured if isinstance(structured, dict) else None, dropped_blocks=dropped)


def _clean(text: str) -> str:
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))


def _has_ref(node: object) -> bool:
    if isinstance(node, dict):
        return any(key in _REF_KEYS or _has_ref(value) for key, value in node.items())
    if isinstance(node, list):
        return any(_has_ref(item) for item in node)
    return False


def _skip_reason(schema: object) -> str | None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return "its input schema is not an object schema"
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or not isinstance(schema.get("required", []), list):
        return "its input schema is not an object schema"
    if "reason" in properties:
        return "its input schema already has a reason property"
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        return "its input schema is larger than 32 KB"
    if _has_ref(schema):
        return "its input schema uses $ref, $defs or definitions"
    return None


def _gate_sentence(upstream: UpstreamServer, gate: str) -> str:
    if gate == "auto":
        return f" Auto-approved: the {upstream.label} server decides which data the caller may see."
    if gate == "review":
        return " Requires user approval: PrivacyFence shows the result to the person before it is returned."
    return " Requires user approval: PrivacyFence asks the person before the call is sent."


class UpstreamToolCatalog:
    def __init__(self, upstream: UpstreamServer) -> None:
        self._upstream = upstream
        self._tools: list[types.Tool] = []

    def update(self, tools: list[types.Tool]) -> None:
        self._tools = list(tools)

    def _build(self) -> tuple[list[ToolSpec], list[str]]:
        by_name = {tool.name: tool for tool in self._tools}
        specs: list[ToolSpec] = []
        missing: list[str] = []
        declared: dict[str, DeclaredTool] = {d.name: d for d in self._upstream.tools}
        for name, tool in declared.items():
            listed = by_name.get(name)
            if listed is None:
                missing.append(tool.exposed_name)
                continue
            reason = _skip_reason(listed.input_schema)
            if reason is not None:
                logger.warning("Upstream tool %s is skipped: %s", tool.exposed_name, reason)
                missing.append(tool.exposed_name)
                continue
            specs.append(self._spec(tool, listed))
        return specs, missing

    def _spec(self, declared: DeclaredTool, listed: types.Tool) -> ToolSpec:
        label = self._upstream.label
        description = _clean(listed.description or "")[:MAX_DESCRIPTION_CHARS]
        description += f" Returns what the {label} server answers." + _gate_sentence(self._upstream, declared.gate)
        schema = copy.deepcopy(listed.input_schema)
        properties = schema.setdefault("properties", {})
        properties["reason"] = {"type": "string", "description": _REASON_DESCRIPTION}
        schema["required"] = [*schema.get("required", []), "reason"]
        return ToolSpec(
            name=declared.exposed_name,
            description=description,
            params=[ToolParam("reason", "str", required=True, description=_REASON_DESCRIPTION)],
            read_only=declared.verb in READ_VERBS,
            destructive=declared.verb == "delete",
            input_schema=schema,
        )

    def specs(self) -> list[ToolSpec]:
        return self._build()[0]

    def missing(self) -> list[str]:
        return self._build()[1]
