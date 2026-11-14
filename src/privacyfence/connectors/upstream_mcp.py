"""Upstream MCP connector: the declared tools of one operator-configured upstream MCP server.

One ``UpstreamMcpConnector`` fronts one ``UpstreamServer`` (ADR 0283). Its tools are the declared
tools the upstream listed at startup (``UpstreamToolCatalog``); each is handled by the gate the
operator declared for it:

* ``auto``: the call goes to the upstream at once and an ``auto_accepted`` audit row is written.
  The upstream decides which data the caller may see, from the signed claim.
* ``review``: the call goes to the upstream, then ``gated_call`` shows the result to the person
  (and runs the PII scan on it) before it is returned.
* ``popup``: ``gated_call`` asks the person first, with the arguments they would send; only an
  approval reaches the upstream. A denial raises before the client is touched.

Connector code never reads the caller (ADR 0036, amended by ADR 0284). Who is calling, and which
data domains they hold, is resolved beside the transport: the ``caller`` callable this connector is
given (supplied by ``upstream_mcp_runtime``) returns the ``CallerClaim`` that ``UpstreamMcpClient``
signs onto the request. This module imports no caller context.

Approval previews name the arguments but never their values. An upstream's error text reaches the AI
client (cut to 500 characters, control characters removed), like its results; the two error types
below are named ``RuntimeError`` subclasses, so ``safe_errors.public_message`` passes them.
"""
from __future__ import annotations

import json
import logging
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolSpec
from ..gate import current_reason, gated_call
from ..upstream_claims import CallerClaim
from ..upstream_mcp_client import CallResult, UpstreamMcpClient, UpstreamMcpClientError, UpstreamToolCatalog
from ..upstream_mcp_config import DeclaredTool, UpstreamServer

logger = logging.getLogger(__name__)

MAX_ERROR_DETAIL_CHARS = 500


class UpstreamUnavailableError(RuntimeError):
    """The upstream could not be reached, or its result was too large. Text is written here."""


class UpstreamToolError(RuntimeError):
    """The upstream answered with an error. Its text is the upstream's own answer to the caller."""


def _clean(text: str) -> str:
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))


class UpstreamMcpConnector(Connector):
    def __init__(
        self,
        upstream: UpstreamServer | None = None,
        catalog: UpstreamToolCatalog | None = None,
        client: UpstreamMcpClient | None = None,
        caller: Callable[[], CallerClaim] | None = None,
    ) -> None:
        # Only stores its arguments: the tool catalog builds ``cls(None)`` and ``cls(MagicMock())``.
        self._upstream = upstream
        self._catalog = catalog
        self._client = client
        self._caller = caller

    @property
    def name(self) -> str:
        return self._upstream.id if isinstance(self._upstream, UpstreamServer) else "upstream_mcp"

    def tool_specs(self) -> list[ToolSpec]:
        return self._catalog.specs() if isinstance(self._catalog, UpstreamToolCatalog) else []

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        declared = self._declared(tool)
        if declared is None:
            raise ValueError(f"Unknown upstream tool: {tool!r}")
        if declared.gate == "auto":
            return await self._auto(declared, args)
        if declared.gate == "review":
            return await self._review(declared, args)
        return await self._popup(declared, args)

    def _declared(self, tool: str) -> DeclaredTool | None:
        if not isinstance(self._upstream, UpstreamServer):
            return None
        for declared in self._upstream.tools:
            if declared.exposed_name == tool:
                return declared
        return None

    # ------------------------------------------------------------------ #
    # Gates
    # ------------------------------------------------------------------ #

    async def _auto(self, declared: DeclaredTool, args: dict[str, Any]) -> Any:
        t0 = time.time()
        result = await self._forward(declared, args)
        self._auto_audit(declared, args, t0)
        return _value(result)

    async def _review(self, declared: DeclaredTool, args: dict[str, Any]) -> Any:
        result = await self._forward(declared, args)
        value = _value(result)
        text = _text(result)
        return await gated_call(
            **self._gate_fields(declared, args),
            raw_data=value,
            filtered_data=value,
            gate="review",
            pii_scan_text=text,
            details_text=text,
        )

    async def _popup(self, declared: DeclaredTool, args: dict[str, Any]) -> Any:
        shown = json.dumps(args, indent=2, sort_keys=True, default=str)
        await gated_call(
            **self._gate_fields(declared, args),
            raw_data=args,
            filtered_data=None,
            gate="popup",
            details_text=shown,
            write_content_scan_text=shown,
        )
        return _value(await self._forward(declared, args))

    def _gate_fields(self, declared: DeclaredTool, args: dict[str, Any]) -> dict[str, Any]:
        upstream = self._require_upstream()
        return {
            "connector": self.name,
            "tool": declared.exposed_name,
            "tool_name": f"{upstream.label}: {declared.name}",
            "summary": f"{declared.verb.capitalize()} on {upstream.label}",
            "sender": upstream.label,
            "preview": {
                "Server": upstream.label,
                "Tool": declared.name,
                "Arguments": ", ".join(sorted(args)) or "none",
            },
            "args": args,
        }

    def _require_upstream(self) -> UpstreamServer:
        if not isinstance(self._upstream, UpstreamServer):
            raise RuntimeError("This upstream connector is not configured.")
        return self._upstream

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _forward(self, declared: DeclaredTool, args: dict[str, Any]) -> CallResult:
        upstream = self._require_upstream()
        if self._client is None or self._caller is None:
            raise RuntimeError("This upstream connector is not configured.")
        try:
            return await self._client.call_tool(declared.name, args, self._caller())
        except UpstreamMcpClientError as exc:
            logger.warning("Upstream %s call to %s failed: %s", upstream.id, declared.name, exc.kind)
            if exc.kind == "too_large":
                raise UpstreamUnavailableError(
                    f"The {upstream.label} result is larger than 1 MB; ask for less."
                ) from None
            if exc.kind == "tool_error":
                detail = _clean(exc.detail)[:MAX_ERROR_DETAIL_CHARS]
                raise UpstreamToolError(f"The {upstream.label} server returned an error: {detail}") from None
            raise UpstreamUnavailableError(f"The {upstream.label} server could not be reached.") from None

    def _auto_audit(self, declared: DeclaredTool, args: dict[str, Any], created_at: float) -> None:
        upstream = self._require_upstream()
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=declared.exposed_name,
                tool_name=f"{upstream.label}: {declared.name}",
                summary=f"{declared.verb.capitalize()} on {upstream.label}",
                sender=upstream.label,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:  # noqa: BLE001 -- an audit failure never blocks a read
            logger.warning("Audit log write failed: %s", exc)


def _value(result: CallResult) -> Any:
    return result.structured if result.structured is not None else result.text


def _text(result: CallResult) -> str:
    if result.text or result.structured is None:
        return result.text
    return json.dumps(result.structured, indent=2, sort_keys=True, default=str)
