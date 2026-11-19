"""Upstream MCP connector: the saved tools of one hosted, OAuth-authenticated upstream MCP server.

One ``UpstreamMcpConnector`` fronts one ``UpstreamServer`` for one person (ADR 0329). Its tools are the
administrator's saved list (``UpstreamToolCatalog``, ADR 0331). Every call goes through ``gated_call``
with the person gate of the tool, which is ``review`` or ``popup``, never ``auto``: a stored Always allow
rule is what lets a call run without a card.

* ``review``: the call goes to the upstream, then ``gated_call`` shows the result to the person (and
  runs the PII scan on it) before it is returned.
* ``popup``: ``gated_call`` asks the person first, with the arguments they would send; only an
  approval reaches the upstream. A denial raises before the client is touched.

Connector code never reads the caller (ADR 0036). Who may call is decided by the ``admit`` callable and the
person's token by the ``bearer`` callable, both supplied by ``upstream_mcp_runtime``. PrivacyFence sends no
claim of its own to the upstream.

Approval previews name the arguments but never their values. An upstream's error text reaches the AI
client (cut to 500 characters, control characters removed), like its results; the error types below are
named ``RuntimeError`` subclasses, so ``safe_errors.public_message`` passes them.
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
from ..upstream_mcp_client import (
    BearerSource,
    CallerNotAllowedError,
    CallResult,
    UpstreamMcpClient,
    UpstreamMcpClientError,
    UpstreamToolCatalog,
    person_gate_of,
)
from ..upstream_mcp_config import UpstreamServer
from ..upstream_tools import SavedTool

logger = logging.getLogger(__name__)

MAX_ERROR_DETAIL_CHARS = 500


class UpstreamUnavailableError(RuntimeError):
    """The upstream could not be used (not connected, expired sign-in, unreachable, too large). Text is written here."""


class UpstreamCallRefusedError(RuntimeError):
    """PrivacyFence does not let this kind of caller use the upstream. Text is written here."""


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
        *,
        admit: Callable[[], None] | None = None,
        bearer: BearerSource | None = None,
    ) -> None:
        # Only stores its arguments: the tool catalog builds ``cls(None)`` and ``cls(MagicMock())``.
        self._upstream = upstream
        self._catalog = catalog
        self._client = client
        self._admit = admit
        self._bearer = bearer

    @property
    def name(self) -> str:
        return self._upstream.id if isinstance(self._upstream, UpstreamServer) else "upstream_mcp"

    def tool_specs(self) -> list[ToolSpec]:
        return self._catalog.specs() if isinstance(self._catalog, UpstreamToolCatalog) else []

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        saved = self._catalog.saved_tool(tool) if isinstance(self._catalog, UpstreamToolCatalog) else None
        if saved is None:
            raise ValueError(f"Unknown upstream tool: {tool!r}")
        upstream = self._require_upstream()
        if self._admit is None:
            raise RuntimeError("This upstream connector is not configured.")
        gate = person_gate_of(saved)
        try:
            self._admit()
        except CallerNotAllowedError:
            logger.warning("Upstream %s call to %s refused: caller type not allowed", upstream.id, saved.name)
            self._forward_audit(saved, gate, "refused", 0.0)
            raise UpstreamCallRefusedError(
                f"PrivacyFence does not allow this caller to use the {upstream.label} server."
            ) from None
        if gate == "review":
            return await self._review(saved, gate, args)
        return await self._popup(saved, gate, args)

    # ------------------------------------------------------------------ #
    # Gates
    # ------------------------------------------------------------------ #

    async def _review(self, saved: SavedTool, gate: str, args: dict[str, Any]) -> Any:
        result = await self._forward(saved, gate, args)
        value = _value(result)
        return await gated_call(
            **self._gate_fields(saved, args),
            raw_data=value,
            filtered_data=value,
            gate="review",
            pii_scan_text=_scanned(result),
            details_text=_text(result),
        )

    async def _popup(self, saved: SavedTool, gate: str, args: dict[str, Any]) -> Any:
        shown = json.dumps(args, indent=2, sort_keys=True, default=str)
        await gated_call(
            **self._gate_fields(saved, args),
            raw_data=args,
            filtered_data=None,
            gate="popup",
            details_text=shown,
            write_content_scan_text=shown,
        )
        return _value(await self._forward(saved, gate, args))

    def _gate_fields(self, saved: SavedTool, args: dict[str, Any]) -> dict[str, Any]:
        upstream = self._require_upstream()
        return {
            "connector": self.name,
            "tool": f"{upstream.id}_{saved.name}",
            "tool_name": f"{upstream.label}: {saved.name}",
            "summary": f"{saved.verb.capitalize()} on {upstream.label}",
            "sender": upstream.label,
            "preview": {
                "Server": upstream.label,
                "Tool": saved.name,
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

    async def _forward(self, saved: SavedTool, gate: str, args: dict[str, Any]) -> CallResult:
        """Send the call and write the one ``upstream_forwarded`` execution record, whatever the outcome."""
        upstream = self._require_upstream()
        if self._client is None or self._bearer is None:
            raise RuntimeError("This upstream connector is not configured.")
        label = upstream.label
        started = time.time()
        status = "ok"
        try:
            try:
                return await self._client.call_tool(saved.name, args, bearer=self._bearer)
            except UpstreamMcpClientError as exc:
                status = exc.kind
                logger.warning("Upstream %s call to %s failed: %s", upstream.id, saved.name, exc.kind)
                if exc.kind == "tool_error":
                    detail = _clean(exc.detail)[:MAX_ERROR_DETAIL_CHARS]
                    raise UpstreamToolError(f"The {label} server returned an error: {detail}") from None
                raise UpstreamUnavailableError(_UNAVAILABLE[exc.kind].format(label=label)) from None
        finally:
            self._forward_audit(saved, gate, status, time.time() - started)

    def _forward_audit(self, saved: SavedTool, gate: str, status: str, latency: float) -> None:
        """One execution record per forwarded call. Holds no token, argument value or upstream text."""
        upstream = self._require_upstream()
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=f"{upstream.id}_{saved.name}",
                tool_name=f"{upstream.label}: {saved.name}",
                summary=f"Forwarded {saved.name} to {upstream.label}",
                sender=upstream.label,
                decision="upstream_forwarded",
                auto_accept_rule="",
                latency_seconds=latency,
                claude_reason=current_reason(),
                upstream_status=status,
                metadata={"upstream": upstream.id, "gate": gate, "auth": "oauth"},
            ))
        except Exception as exc:  # noqa: BLE001 -- an audit failure never changes the call outcome
            logger.warning("Audit log write failed: %s", exc)


_UNAVAILABLE = {
    "not_connected": "{label} is not connected. Connect {label} on the Connect page (/connect).",
    "auth_expired": (
        "The {label} sign-in has expired or was revoked. Connect {label} on the Connect page (/connect) "
        "to sign in again."
    ),
    "insufficient_scope": (
        "{label} needs more access than PrivacyFence was granted. Connect {label} again on the Connect "
        "page (/connect)."
    ),
    "unreachable": "The {label} server could not be reached.",
    "too_large": "The {label} result is larger than 1 MB; ask for less.",
}


def _value(result: CallResult) -> Any:
    return result.structured if result.structured is not None else result.text


def _scanned(result: CallResult) -> str:
    """Everything the caller receives, for the PII scan: the text plus any structured content."""
    if result.structured is None:
        return result.text
    dumped = json.dumps(result.structured, sort_keys=True, default=str)
    return f"{result.text}\n{dumped}" if result.text else dumped


def _text(result: CallResult) -> str:
    if result.text or result.structured is None:
        return result.text
    return json.dumps(result.structured, indent=2, sort_keys=True, default=str)
