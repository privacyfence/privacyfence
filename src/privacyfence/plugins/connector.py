"""A running plugin's tools, exposed and gated like any connector's (ADR 0121, ADR 0122).

``PluginConnector`` wraps one plugin as a ``Connector``, so the MCP listing, dispatch, dedupe,
``reason`` handling and ``tools/list_changed`` reach plugin tools with no change to the web layer.

Every gated call runs in two steps. ``tool.prepare`` returns what the call would release or do;
the gate shows that on a card; only an approved call is sent ``tool.execute``. Three invariants
hold:

- **A read releases the prepared payload.** The human approved exactly those blocks, so whatever
  ``tool.execute`` returns for a read-only tool is ignored. A plugin cannot hand the AI anything
  the card did not show.
- **An approval belongs to one prepared call.** The gate's dedupe key carries the prepared
  call's id, so the decision ledger replays an approval only to a repeat call that reuses that
  same prepared call. A fresh prepare always gets its own card, so a released payload is always
  the one a human saw. A prepared call is kept for the card's pending lifetime plus the ledger's
  replay window, and a decided read for another replay window, so a repeat call in those windows
  reuses it instead of asking the plugin again, and gets the same payload or the same denial.
- **A write runs once.** An approved write's prepared call is dropped before ``tool.execute`` is
  sent, and execute is never retried. A second identical call prepares afresh and gets its own
  card.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from privacyfence import approval_ui, auto_accept
from privacyfence.approval_window_html import NARROW, WIDE
from privacyfence.approvals import ApprovalPending, canonical_key
from privacyfence.audit_log import AuditEntry, current_week, get_audit_logger
from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.gate import GateDeniedError, current_reason, gated_call
from privacyfence.pii_detector import detect_pii_categories
from privacyfence.plugins.blocks import fields_dict, flatten_text, to_card_blocks, validate_blocks
from privacyfence.plugins.constants import (
    PREPARED_CALL_LIFETIME_SECONDS,
    WRITE_RESULT_MAX_BYTES,
    mcp_tool_name,
    operation_key,
    scope_predicate,
)
from privacyfence.plugins.protocol import ExecuteResult, PrepareResult, RpcError, ToolDef, args_digest
from privacyfence.plugins.rpc import RpcPeer
from privacyfence.plugins.tools import ToolDefError, validate_tool_defs
from privacyfence.policy.registry import Verb

logger = logging.getLogger(__name__)

# The connectors' own spelling of the gated-tool reason parameter, so a plugin tool reads the same
# to the AI client as every other gated tool.
REASON_PARAM_DESCRIPTION = "One sentence: why are you calling this tool right now?"

_ANNOTATIONS = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}

# A failed tool.prepare, as the daemon logs it (the AI client gets the generic tool-failure
# message). Fixed sentences only: the plugin's own error detail could carry connector content.
_PREPARE_ERRORS = {
    "connector_unavailable": "A service this plugin reads from is not connected.",
    "payload_too_large": "The plugin's result is too large to return.",
    "timeout": "The plugin did not answer in time.",
    "upstream_error": "A service this plugin reads from returned an error.",
}
PREPARE_FAILED = "The plugin could not prepare this call."
INVALID_PREVIEW = "The plugin returned an invalid preview."
LOST_CALL = "The plugin lost track of this call; ask again."
EXECUTE_FAILED = "The plugin could not complete this call."
WRITE_RESULT_WITHHELD = (
    "The action ran, but its result was withheld because it was larger than 2,048 bytes "
    "or may contain personal data."
)


@dataclass
class PreparedCall:
    tool: str                      # MCP name
    call_id: str
    preview: list[dict]
    payload: list[dict] | None
    scopes: dict[str, list[str]]
    created_at: float
    keep_until: float


def default_title(tool: str) -> str:
    text = tool.replace("_", " ")
    return text[:1].upper() + text[1:]


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _wire_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


class PluginConnector(Connector):
    def __init__(
        self,
        plugin: str,
        display_name: str,
        manifest: Any,
        peer_provider: Callable[[], RpcPeer | None],
        principal_context_provider: Callable[[], dict],
        on_audit_lifecycle: Callable[[str], None],
        *,
        scope_types: list[dict] | None = None,
        reviewed: frozenset[tuple] | None = None,
        owns_approval: Callable[[str], bool] | None = None,
    ) -> None:
        """``scope_types`` is what the plugin declared at ``initialize``; ``reviewed`` is the
        signature set recorded at enable, which every later ``tools.changed`` must stay within.
        ``owns_approval`` says whether an approval id is one PrivacyFence issued to this plugin;
        ``None`` means it owns none."""
        self._plugin = plugin
        self.display_name = display_name
        self._manifest = manifest
        self._peer_provider = peer_provider
        self._principal_context = principal_context_provider
        self._on_audit_lifecycle = on_audit_lifecycle
        self._scope_types = list(scope_types or [])
        self._reviewed = reviewed
        self._owns_approval = owns_approval
        self._defs: dict[str, ToolDef] = {}         # MCP name -> definition
        self._prepared: dict[str, PreparedCall] = {}
        self._preparing: dict[str, asyncio.Future[PreparedCall]] = {}
        self.last_tools_rejection: str | None = None

    @property
    def name(self) -> str:
        return self._plugin

    # ── Exposure ──────────────────────────────────────────────────────

    def set_tools(self, defs: list[ToolDef]) -> None:
        """Expose ``defs`` and register them with every policy table, replacing the previous list.

        ``register_dynamic_tools`` unregisters this plugin's previous rows itself, and only after
        its checks pass, so a refused list (``ValueError``) leaves the previous one in force.
        """
        auto_accept.register_dynamic_tools(self._plugin, [self._dynamic_spec(d) for d in defs])
        self._defs = {mcp_tool_name(self._plugin, d.name): d for d in defs}
        live = set(self._defs)
        self._prepared = {k: v for k, v in self._prepared.items() if v.tool in live}

    def clear_tools(self) -> None:
        auto_accept.unregister_dynamic_tools(self._plugin)
        self._defs = {}
        self._prepared.clear()

    def tool_specs(self) -> list[ToolSpec]:
        return [self._tool_spec(mcp_name, d) for mcp_name, d in self._defs.items()]

    def handle_tools_changed(self, params: dict) -> bool:
        """Apply a ``tools.changed`` list as a whole. Returns whether it was accepted.

        A rejected list leaves the previous one in force and is audited with its reason; Settings
        shows ``last_tools_rejection`` until the next accepted change.
        """
        try:
            raw = params.get("tools") if isinstance(params, dict) else None
            defs = validate_tool_defs(
                self._plugin, raw, self._scope_types, self._manifest, reviewed=self._reviewed
            )
            before = {d.name for d in self._defs.values()}
            self.set_tools(defs)
        except (ToolDefError, ValueError) as exc:
            detail = exc.detail if isinstance(exc, ToolDefError) else str(exc)
            self.last_tools_rejection = detail
            self._on_audit_lifecycle(f"tools change rejected: {detail}")
            return False
        after = {d.name for d in defs}
        changes = [f"+{n}" for n in sorted(after - before)] + [f"-{n}" for n in sorted(before - after)]
        self.last_tools_rejection = None
        self._on_audit_lifecycle(f"tools changed: {','.join(changes) or 'none added or removed'}")
        return True

    def _title(self, defn: ToolDef) -> str:
        return defn.title or default_title(defn.name)

    def _dynamic_spec(self, defn: ToolDef) -> auto_accept.DynamicToolSpec:
        gated = defn.gate != "auto"
        effect = "" if defn.read_only else (
            defn.effect or f"Runs {self._title(defn)} in the {self.display_name} plugin."
        )
        return auto_accept.DynamicToolSpec(
            tool=mcp_tool_name(self._plugin, defn.name),
            gate=defn.gate,
            operation=operation_key(self._plugin, defn.name) if gated else None,
            verb=Verb.READ if defn.read_only else Verb.UPDATE,
            layout=WIDE if gated else NARROW,
            effect=effect,
            scope_predicates=tuple((scope_predicate(self._plugin, s), s) for s in defn.scopes),
            destructive=defn.destructive,
        )

    @staticmethod
    def _tool_spec(mcp_name: str, defn: ToolDef) -> ToolSpec:
        props = defn.parameters.get("properties", {})
        required = set(defn.parameters.get("required", []))
        params = [
            ToolParam(
                name=pname,
                annotation=_ANNOTATIONS[schema["type"]],
                required=pname in required,
                default=None,
                description=str(schema.get("description", "")),
            )
            for pname, schema in props.items()
        ]
        if defn.gate != "auto":
            params.append(ToolParam("reason", "str", required=True, description=REASON_PARAM_DESCRIPTION))
        return ToolSpec(
            name=mcp_name,
            description=defn.description,
            params=params,
            read_only=defn.read_only,
            destructive=defn.destructive,
        )

    # ── Calls ─────────────────────────────────────────────────────────

    def _running_peer(self) -> RpcPeer:
        peer = self._peer_provider()
        if peer is None or peer.closed:
            raise RuntimeError(f"The {self.display_name} plugin is not running.")
        return peer

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        defn = self._defs.get(tool)
        if defn is None:
            raise ValueError(f"Unknown tool: {tool}")
        peer = self._running_peer()
        args = dict(args)
        key = canonical_key(self._plugin, tool, args)
        prepared = await self._reuse_or_prepare(key, peer, tool, defn, args)
        title = self._title(defn)
        summary = _summary(prepared.preview, title)

        if defn.gate == "auto":
            self._auto_audit(tool, title, summary, prepared.created_at)
            approval = {
                "approval_id": "auto-" + prepared.call_id, "decision": "approved",
                "via": "auto", "decided_at": _now_rfc3339(),
            }
        else:
            await self._gate(key, tool, defn, title, summary, prepared, args)
            approval = {
                "approval_id": "card-" + prepared.call_id, "decision": "approved",
                "via": "card", "decided_at": _now_rfc3339(),
            }
        return await self._execute(defn, prepared, args, approval)

    async def _reuse_or_prepare(
        self, key: str, peer: RpcPeer, tool: str, defn: ToolDef, args: dict,
    ) -> PreparedCall:
        existing = self._prepared.get(key)
        if existing is not None:
            if time.time() < existing.keep_until:
                return existing
            del self._prepared[key]
        # Two identical calls racing each other share one prepare, so they cannot release two
        # different payloads under one approval.
        inflight = self._preparing.get(key)
        if inflight is not None:
            return await asyncio.shield(inflight)
        future: asyncio.Future[PreparedCall] = asyncio.get_running_loop().create_future()
        self._preparing[key] = future
        try:
            prepared = await self._prepare(peer, tool, defn, args)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            future.set_exception(exc)
            future.exception()  # marks it retrieved when nobody else is waiting
            raise
        else:
            future.set_result(prepared)
            if defn.gate != "auto":
                registry = approval_ui.get_approval_ui().deferred_registry
                if registry is not None:
                    # Long enough for the card to stay pending and its decision to be replayed.
                    prepared.keep_until = time.time() + registry.pending_ttl + registry.ledger_ttl
                self._prepared[key] = prepared
            return prepared
        finally:
            self._preparing.pop(key, None)

    async def _prepare(self, peer: RpcPeer, tool: str, defn: ToolDef, args: dict) -> PreparedCall:
        call_id = uuid.uuid4().hex
        created_at = time.time()
        try:
            raw = await peer.request("tool.prepare", {
                "call_id": call_id,
                "principal": self._principal_context(),
                "tool": defn.name,
                "args": args,
                "reason": current_reason() or None,
            })
        except RpcError as exc:
            raise RuntimeError(_PREPARE_ERRORS.get(exc.code, PREPARE_FAILED)) from None
        try:
            result = PrepareResult.from_wire(raw, validate_blocks=validate_blocks)
        except RpcError as exc:
            logger.warning("Plugin %s returned an invalid prepare result: %s", self._plugin, exc.detail)
            if exc.code == "payload_too_large":
                raise RuntimeError(_PREPARE_ERRORS["payload_too_large"]) from None
            raise RuntimeError(INVALID_PREVIEW) from None
        if (result.payload is None) == defn.read_only:
            logger.warning("Plugin %s: payload is required for read-only tools only", self._plugin)
            raise RuntimeError(INVALID_PREVIEW)
        scopes: dict[str, list[str]] = {}
        for scope_type in defn.scopes:
            values = result.scopes.get(scope_type)
            if not values:
                logger.warning("Plugin %s did not return scope %s", self._plugin, scope_type)
                raise RuntimeError(INVALID_PREVIEW)
            scopes[scope_type] = list(values)
        return PreparedCall(
            tool=tool, call_id=call_id, preview=result.preview, payload=result.payload, scopes=scopes,
            created_at=created_at, keep_until=created_at + PREPARED_CALL_LIFETIME_SECONDS,
        )

    async def _gate(
        self, key: str, tool: str, defn: ToolDef, title: str, summary: str, prepared: PreparedCall, args: dict,
    ) -> None:
        payload = prepared.payload or []
        registry = approval_ui.get_approval_ui().deferred_registry
        try:
            await gated_call(
                connector=self._plugin, tool=tool, tool_name=title,
                summary=summary, sender="",
                raw_data={"plugin": self._plugin, "tool": defn.name, "scopes": prepared.scopes},
                filtered_data={"blocks": payload} if defn.read_only else None,
                gate=defn.gate,
                preview={"Plugin": self.display_name, "Tool": title},
                preview_blocks=to_card_blocks(prepared.preview + payload),
                pii_scan_text=flatten_text(payload) if defn.read_only else None,
                args=args,
                dedupe_extra=prepared.call_id,
            )
        except (ApprovalPending, asyncio.CancelledError):
            # Still pending, or the caller went away while it was: the card may yet be approved,
            # and its release must find the payload it shows.
            raise
        except GateDeniedError:
            if defn.read_only and registry is not None:
                # Kept for the window in which the ledger replays this denial, so a repeat call
                # gets the same denial rather than a new card. A write's denial is single use.
                prepared.keep_until = min(prepared.keep_until, time.time() + registry.ledger_ttl)
            else:
                self._drop(key, prepared)
            raise
        except Exception:
            self._drop(key, prepared)
            raise
        if defn.read_only and registry is not None:
            # The same window in which the decision ledger replays this approval, so a repeat
            # call releases the payload the human saw rather than a fresh, unseen prepare.
            prepared.keep_until = time.time() + registry.ledger_ttl
        else:
            self._drop(key, prepared)

    def _drop(self, key: str, prepared: PreparedCall) -> None:
        if self._prepared.get(key) is prepared:
            del self._prepared[key]

    async def _execute(self, defn: ToolDef, prepared: PreparedCall, args: dict, approval: dict) -> Any:
        try:
            peer = self._running_peer()
            raw = await peer.request("tool.execute", {
                "call_id": prepared.call_id,
                "principal": self._principal_context(),
                "tool": defn.name,
                "args": args,
                "args_digest": args_digest(args),
                "approval": approval,
            })
        except (RpcError, RuntimeError) as exc:
            if defn.read_only:
                logger.warning("Plugin %s: tool.execute for %s failed: %s", self._plugin, defn.name, exc)
                return {"blocks": prepared.payload}
            if isinstance(exc, RuntimeError):
                raise
            if exc.code in ("unknown_call", "digest_mismatch"):
                raise RuntimeError(LOST_CALL) from None
            if exc.code == "timeout":
                raise RuntimeError(_PREPARE_ERRORS["timeout"]) from None
            raise RuntimeError(EXECUTE_FAILED) from None
        if defn.read_only:
            return {"blocks": prepared.payload}
        try:
            result = ExecuteResult.from_wire(raw)
        except RpcError:
            raise RuntimeError(EXECUTE_FAILED) from None
        value: Any = result.result
        if result.approval_id is not None:
            if isinstance(result.result, dict):
                value = {**result.result, "approval_id": result.approval_id}
            else:
                value = {"result": result.result, "approval_id": result.approval_id}
        return await self._screen_write_result(defn, value, result.approval_id)

    async def _screen_write_result(self, defn: ToolDef, value: Any, approval_id: str | None) -> Any:
        """A write result is an acknowledgement: over the cap, or carrying personal data, it is
        withheld. It is returned, not raised, because an error would invite a retry of a write
        that already ran. Only the size and the categories are logged, never the content."""
        if _wire_size(value) > WRITE_RESULT_MAX_BYTES:
            logger.info("Plugin %s: result of %s withheld: over %d bytes",
                        self._plugin, defn.name, WRITE_RESULT_MAX_BYTES)
            return await self._withheld(approval_id)
        categories = await asyncio.to_thread(detect_pii_categories, json.dumps(value, ensure_ascii=False))
        if categories:
            logger.info("Plugin %s: result of %s withheld: possible personal data (%s)",
                        self._plugin, defn.name, ", ".join(categories))
            return await self._withheld(approval_id)
        return value

    async def _withheld(self, approval_id: str | None) -> dict:
        out: dict[str, Any] = {"withheld": True, "message": WRITE_RESULT_WITHHELD}
        if approval_id is not None and self._owns_approval is not None:
            if await asyncio.to_thread(self._owns_approval, approval_id):
                out["approval_id"] = approval_id
        return out

    def _auto_audit(self, tool: str, tool_name: str, summary: str, created_at: float) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender="",
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)


def _summary(preview: list[dict], title: str) -> str:
    for label, value in fields_dict(preview).items():
        return f"{label}: {value}"
    return title


__all__ = ["PluginConnector", "PreparedCall", "REASON_PARAM_DESCRIPTION", "default_title"]
