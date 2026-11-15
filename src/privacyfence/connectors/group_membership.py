"""The ``privacyfence_update_group_membership`` tool: an n8n job sends the Workspace group membership.

A scoped token an administrator created (plan 3.8) carries this tool; nothing else may call it, so a
prompt-injected AI client cannot change who holds an audience. The store applies the snapshot
(``group_membership.MembershipStore.apply_snapshot``): removals at once, additions to ``all`` at once,
additions to any other audience wait for an administrator. Audit summaries hold counts and group
names, never email addresses.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .. import group_membership, scoped_tokens, upstream_mcp_runtime
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolSpec
from ..gate import current_reason

logger = logging.getLogger(__name__)

TOOL_NAME = "privacyfence_update_group_membership"

_DESCRIPTION = (
    "Send the full membership snapshot of every Workspace group PrivacyFence maps to audiences. "
    "Removals apply at once; an addition to a group that grants a restricted audience is held for "
    "an administrator to approve on the Admin page, and a snapshot that adds too many people is "
    "rejected whole. Works only with a scoped token an administrator created. "
    "Returns {status, added, removed, parked, ignored_groups, reason}. "
    "Auto-approved: only a scoped token an administrator created may call it."
)


class GroupMembershipConnector(Connector):
    def __init__(self, _unused: Any = None) -> None:
        # The tool catalogue builds ``cls(MagicMock())`` and ``cls(None)``; nothing is stored.
        pass

    @property
    def name(self) -> str:
        return "privacyfence"

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(
            name=TOOL_NAME,
            description=_DESCRIPTION,
            params=[],
            read_only=False,
            destructive=False,
            input_schema={
                "type": "object",
                "properties": {
                    "groups": {
                        "type": "object",
                        "description": "Group address to the list of member email addresses; the full snapshot.",
                        "additionalProperties": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "required": ["groups"],
            },
        )]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool != TOOL_NAME:
            raise ValueError(f"Unknown tool: {tool!r}")
        scope = scoped_tokens.current_token_scope()
        if scope is None or TOOL_NAME not in scope.tools:
            self._audit("group_membership_denied", "Group membership update denied: not a scoped token", "", 0.0)
            raise RuntimeError("privacyfence_update_group_membership can only be called with a scoped token.")
        started = time.time()
        result = await asyncio.to_thread(
            group_membership.get_store().apply_snapshot,
            args.get("groups"),
            group_audiences=upstream_mcp_runtime.all_group_audiences(),
            now=started,
            max_additions=upstream_mcp_runtime.max_additions(),
        )
        ignored = list(result.ignored_groups)
        if result.status == "applied":
            summary = (
                f"Group membership applied: {result.added} added, {result.removed} removed, "
                f"{result.parked} parked, {len(ignored)} groups ignored"
            )
            decision = "group_membership_applied"
        else:
            summary = f"Group membership rejected: {result.reason}"
            decision = "group_membership_rejected"
            logger.warning("Group membership snapshot rejected: %s", result.reason)
        self._audit(decision, summary, scope.label, time.time() - started)
        return {
            "status": result.status,
            "added": result.added,
            "removed": result.removed,
            "parked": result.parked,
            "ignored_groups": ignored,
            "reason": result.reason,
        }

    def _audit(self, decision: str, summary: str, sender: str, latency: float) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=TOOL_NAME,
                tool_name="Update group membership",
                summary=summary,
                sender=sender,
                decision=decision,
                auto_accept_rule="auto",
                latency_seconds=latency,
                claude_reason=current_reason(),
            ))
        except Exception as exc:  # noqa: BLE001 -- an audit failure never changes the outcome
            logger.warning("Audit log write failed: %s", exc)
