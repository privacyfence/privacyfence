"""AI-proposed scoped tokens: the pending-proposal registry and everything a review of one needs.

A proposal is held in memory only, per principal, for ``PROPOSAL_TTL_SECONDS``. The MCP tool that
creates one returns a review link and nothing else; a person narrows and confirms it in Settings,
and the existing create path mints the token. This module has no web imports.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Sequence

from . import agent_label, audit_log
from .agent_identity import AgentIdentity, agent_scope
from .principal import Principal, principal_scope

if TYPE_CHECKING:
    from .policy.scope_rows import ScopeRow

logger = logging.getLogger(__name__)

PROPOSAL_TTL_SECONDS = 3600
MAX_PENDING_PER_PRINCIPAL = 5
REVIEW_PATH = "/settings/scoped_tokens/proposals/{proposal_id}"
ID_PREFIX = "stp-"
GONE_MESSAGE = "This token proposal has expired or was already used."
WIDENED_MESSAGE = "A proposed token can only be narrowed here. To add rules or tools, use New token."
_TOO_MANY_MESSAGE = "Too many token proposals are waiting. Review or decline them in Settings → Scoped tokens."
_REASON_LIMIT = 500
_TOOL_NAME = "privacyfence_propose_scoped_token"
_MISSING_COPY_SENTENCE = "A rule that no longer exists"


@dataclass(frozen=True)
class TokenProposal:
    id: str
    principal: Principal
    request: dict[str, Any]
    source: str
    agent: AgentIdentity
    reason: str
    uncovered_tools: tuple[str, ...]
    created_at: float
    expires_at: float


class ProposalError(ValueError):
    """A proposal cannot be added."""


class ProposalRegistry:
    def __init__(self, on_expired: Callable[[TokenProposal], None] | None = None) -> None:
        self._on_expired = on_expired
        self._lock = threading.Lock()
        self._items: dict[str, TokenProposal] = {}

    def _sweep_locked(self, now: float) -> list[TokenProposal]:
        gone = [p for p in self._items.values() if p.expires_at <= now]
        for proposal in gone:
            del self._items[proposal.id]
        return gone

    def _report(self, gone: list[TokenProposal]) -> None:
        if self._on_expired is None:
            return
        for proposal in gone:
            self._on_expired(proposal)

    def add(self, proposal: TokenProposal, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            gone = self._sweep_locked(now)
            live = sum(1 for p in self._items.values() if p.principal.id == proposal.principal.id)
            if live >= MAX_PENDING_PER_PRINCIPAL:
                error = True
            else:
                error = False
                self._items[proposal.id] = proposal
        self._report(gone)
        if error:
            raise ProposalError(_TOO_MANY_MESSAGE)

    def get(self, principal_id: str, proposal_id: str, now: float | None = None) -> TokenProposal | None:
        now = time.time() if now is None else now
        with self._lock:
            gone = self._sweep_locked(now)
            found = self._items.get(proposal_id)
            if found is not None and found.principal.id != principal_id:
                found = None
        self._report(gone)
        return found

    def take(self, principal_id: str, proposal_id: str, now: float | None = None) -> TokenProposal | None:
        now = time.time() if now is None else now
        with self._lock:
            gone = self._sweep_locked(now)
            found = self._items.get(proposal_id)
            if found is not None and found.principal.id != principal_id:
                found = None
            if found is not None:
                del self._items[proposal_id]
        self._report(gone)
        return found

    def restore(self, proposal: TokenProposal, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            gone = self._sweep_locked(now)
            if proposal.expires_at > now:
                self._items[proposal.id] = proposal
        self._report(gone)

    def pending_for(self, principal_id: str, now: float | None = None) -> list[TokenProposal]:
        now = time.time() if now is None else now
        with self._lock:
            gone = self._sweep_locked(now)
            mine = [p for p in self._items.values() if p.principal.id == principal_id]
        self._report(gone)
        return sorted(mine, key=lambda p: (p.created_at, p.id))


_registry: ProposalRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ProposalRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ProposalRegistry(on_expired=lambda p: record(
                "scoped_token_proposal_expired", p, f"Token proposal '{p.request['label']}' expired",
            ))
        return _registry


def set_registry(registry: ProposalRegistry | None) -> None:
    global _registry
    with _registry_lock:
        _registry = registry


def new_proposal(
    *, principal: Principal, request: dict[str, Any], source: str, agent: AgentIdentity, reason: str,
    uncovered_tools: Sequence[str] = (), now: float | None = None,
) -> TokenProposal:
    now = time.time() if now is None else now
    return TokenProposal(
        id=ID_PREFIX + secrets.token_hex(16),
        principal=principal,
        request=request,
        source=source,
        agent=agent,
        reason=reason[:_REASON_LIMIT],
        uncovered_tools=tuple(uncovered_tools),
        created_at=now,
        expires_at=now + PROPOSAL_TTL_SECONDS,
    )


def narrowing_error(proposed: dict[str, Any], final: dict[str, Any]) -> str | None:
    """``None`` when ``final`` asks for no more than ``proposed``, otherwise ``WIDENED_MESSAGE``."""
    new_rules = {(r["group"], r["value"]): set(r["verbs"]) for r in proposed["rules"] if r["kind"] == "new"}
    copy_ids = {r["rule_id"] for r in proposed["rules"] if r["kind"] == "copy"}
    for rule in final["rules"]:
        if rule["kind"] == "copy":
            ok = rule["rule_id"] in copy_ids
        elif rule["kind"] == "new":
            allowed = new_rules.get((rule["group"], rule["value"]))
            ok = bool(rule["verbs"]) and allowed is not None and set(rule["verbs"]) <= allowed
        else:
            ok = False
        if not ok:
            return WIDENED_MESSAGE
    if not set(final["tools"]) <= set(proposed["tools"]) or final["days"] > proposed["days"]:
        return WIDENED_MESSAGE
    for flag in ("allow_personal_data", "allow_personal_data_reads"):
        if final.get(flag) and not proposed.get(flag):
            return WIDENED_MESSAGE
    return None


def bundle_from_calls(
    calls: Sequence[tuple[str, dict[str, Any], str]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """The ``(rules, tools, uncovered_tools)`` that would cover ``(tool, agent_args, my_email)`` calls."""
    from . import auto_accept  # function-local: import cycle
    from .policy import suggest

    tools: set[str] = set()
    uncovered: set[str] = set()
    verbs: dict[tuple[str, str], set[str]] = {}
    for tool, args, my_email in calls:
        gate = auto_accept.TOOL_TO_GATE.get(tool)
        if gate is None:
            continue
        tools.add(tool)
        if gate == "auto":
            continue
        pick = next((
            s for s in suggest.suggested_rules(tool, args, my_email)
            if not s.get("requires_args") and s.get("value_known") and isinstance(s.get("value"), list)
        ), None)
        if pick is None:
            uncovered.add(tool)
            continue
        verbs.setdefault((pick["group"], ",".join(pick["value"])), set()).add(pick["verb"])
    rules = [
        {"kind": "new", "group": group, "value": value, "verbs": sorted(found)}
        for (group, value), found in sorted(verbs.items())
    ]
    return rules, sorted(tools), sorted(uncovered)


def review_data(proposal: TokenProposal, own_rows: Sequence[ScopeRow]) -> dict[str, Any]:
    """What the review page renders for ``proposal``."""
    from .policy import catalogue, describe, scope_rows  # function-local: import cycle

    by_id = {row.id: row for row in own_rows}
    rules: list[dict[str, Any]] = []
    for rule in proposal.request["rules"]:
        if rule["kind"] == "new":
            compiled = scope_rows.add_rules([], catalogue.rules_for_catalogue_entry(
                rule["group"],
                catalogue.parse_value_list(rule["value"], rule["group"]),
                catalogue.parse_verbs(rule["verbs"]),
            ))[0]
            sentence = "; ".join(describe.row_sentence(row) for row in compiled)
        else:
            row = by_id.get(rule.get("rule_id", ""))
            sentence = describe.row_sentence(row) if row is not None else _MISSING_COPY_SENTENCE
        rules.append({"request": rule, "sentence": sentence})
    request = proposal.request
    return {
        "id": proposal.id,
        "label": request["label"],
        "days": request["days"],
        "tools": list(request["tools"]),
        "allow_personal_data": request["allow_personal_data"],
        "allow_personal_data_reads": request["allow_personal_data_reads"],
        "rules": rules,
        "agent_label": agent_label.label_for(proposal.agent).text,
        "reason": proposal.reason,
        "source": proposal.source,
        "uncovered_tools": list(proposal.uncovered_tools),
        "expires_at": proposal.expires_at,
    }


def record(decision: str, proposal: TokenProposal, summary: str) -> None:
    try:
        with principal_scope(proposal.principal), agent_scope(proposal.agent):
            audit_log.get_audit_logger().record(audit_log.AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=audit_log.current_week(),
                request_id=proposal.id,
                connector="policy",
                tool=_TOOL_NAME,
                tool_name=_TOOL_NAME,
                summary=summary,
                sender=proposal.principal.email or proposal.principal.id,
                decision=decision,
                auto_accept_rule="",
                latency_seconds=max(0.0, time.time() - proposal.created_at),
                claude_reason=proposal.reason,
            ))
    except Exception as exc:
        logger.warning("Audit log write failed: %s", exc)
