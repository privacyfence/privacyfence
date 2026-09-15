"""Connector-call dispatch for the ``/mcp`` endpoint -- dedupe/staleness
logic, the meta-tools (check_policy/list_rules/propose_rule_change/
begin-end-unattended-session), and manifest building, all scoped to one
Streamable HTTP session.

Originally written (P2) as a self-contained Python port of what was then
``bridge/src/tools.ts``'s schema mapping and ``ipc_server.IPCServer``'s own
``_call_connector``/``_check_policy``/``_list_rules``/
``_propose_rule_change``/``_build_manifest``/begin-end-unattended-session --
deliberately not a shared refactor of ``IPCServer`` at the time, so as not
to put that module's own already-green test suite at risk mid-migration.
P5 deleted the bridge and ``ipc_server.py`` entirely once both had a stable
release behind them, so
this module (alongside its P3 collaborator, ``approvals.py``) is now simply
the one connector-call dispatcher there is, not "the /mcp counterpart" of
anything else. The session key this dispatch is scoped to is a fresh UUID
per Streamable HTTP session (routes_mcp.py, via the low-level Server's own
per-session lifespan) -- the same role ``id(writer)`` played for a bridge
connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Hashable

from ..approvals import PendingApprovalRegistry, is_pending_result
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..auto_accept import TOOL_TO_GATE, TOOL_TO_OPERATION, get_auto_accept_evaluator, get_current_config
from ..connector import Connector
from ..gate import propose_rule_change, reason_scope, unattended_scope
from ..principal import current_principal

logger = logging.getLogger(__name__)


class McpDispatcher:
    """Owns dedupe/unattended-session state for the ``/mcp`` endpoint and
    dispatches every connector call and meta-tool through it.

    ``connectors_provider`` is called fresh on every dispatch rather than
    captured once, so a connector rebuild pushed live elsewhere (e.g.
    ``SettingsController.refresh_connectors``, which calls
    ``ConnectorHost.set_connectors``) is picked up here too, with nothing
    else needing a second push -- see daemon_main.py's wiring.
    """

    _DEDUPE_TTL_SECONDS = 30
    _DEDUPE_EXEMPT_TOOLS = frozenset({"gmail_create_label"})
    # privacyfence_await_approval's own timeout_seconds is clamped into this
    # range regardless of what the caller asked for -- fail-safe against a
    # client-supplied value of 0 (busy-poll) or something absurdly large
    # (holding the connection open indefinitely). Polled, not evented: the
    # registry has no pub/sub of its own (see approvals.py), and polling a
    # few in-memory dict lookups is cheap enough not to need one here either.
    _AWAIT_APPROVAL_MIN_TIMEOUT = 1
    _AWAIT_APPROVAL_MAX_TIMEOUT = 120
    _AWAIT_APPROVAL_POLL_SECONDS = 0.5
    # privacyfence_status's own minted sign-in link is cached in-process for
    # this long rather than re-minted on every call (issue #396 Phase 2) --
    # each real mint rewrites WebServer's discovery file and leaves whatever
    # code was there before to expire unused, so a model that calls status
    # repeatedly while planning a task shouldn't churn through one bootstrap
    # code per call. Comfortably under session_auth.BOOTSTRAP_TTL_SECONDS
    # (10 minutes) so a cached link handed back here is never one a human
    # opens only to find it just expired.
    _STATUS_LINK_CACHE_SECONDS = 5 * 60

    def __init__(
        self,
        connectors_provider: Callable[[], dict[str, Connector]],
        *,
        mode: str = "local",
        unattended_sessions_enabled: bool = False,
        registry: PendingApprovalRegistry | None = None,
    ) -> None:
        self._connectors_provider = connectors_provider
        # "local" or "org" -- privacyfence_status's own mode field, and what
        # decides whether it even attempts to mint a sign-in link (org mode
        # never has one -- see get_sign_in_link's own docstring). daemon_main.py
        # passes "org" from _start_org_web_server; every other call site (and
        # every existing test) keeps the local-mode default.
        self._mode = mode
        self._inflight: dict[str, tuple[Any, float]] = {}
        self._last_write_at: dict[tuple[str, str], float] = {}
        self._unattended_sessions_enabled = unattended_sessions_enabled
        self._unattended_sessions: set[Hashable] = set()
        self._unattended_changed_listener: Callable[[], None] | None = None
        # The deferred-approval registry privacyfence_await_approval polls
        # (P3) -- None when nothing in this install can ever produce a
        # pending approval (native-only local mode with /mcp still enabled),
        # in which case every id this tool is asked about is simply
        # "unknown".
        self._registry = registry
        # privacyfence_get_sign_in_link's own callback -- daemon_main.py
        # wires this to WebServer.mint_bootstrap_url once the server that
        # method belongs to actually exists (it's built after this
        # dispatcher is), same two-step wiring set_unattended_changed_
        # listener below already uses for a callback the constructor can't
        # supply yet either. Stays None in org mode (no local-mode
        # WebServer to wire it to at all) and in a test that never calls
        # the setter -- get_sign_in_link's own docstring covers both.
        # privacyfence_status (below) reuses this same seam rather than a
        # second one of its own.
        self._bootstrap_link_provider: Callable[[str], str | None] | None = None
        # (url, minted_at) for privacyfence_status's own cached sign-in link
        # -- see _STATUS_LINK_CACHE_SECONDS above. None until the first
        # mint, or after a mint that came back empty.
        self._status_link_cache: tuple[str, float] | None = None
        # privacyfence_status's own per-connector view -- {name, enabled,
        # authenticated, blocked_by} rows, reusing SettingsController's
        # already-tracked connector/config/failure state (issue #396 Phase
        # 1) rather than this dispatcher trying to derive enabled/blocked_by
        # itself from nothing but the built Connector objects it's handed.
        # None in org mode (no per-principal settings surface exists yet to
        # source this from -- see daemon_main._connectors_for_principal's
        # own comment) and in a test that never wires one; status() falls
        # back to reporting only the connectors it can actually see as
        # already-authenticated, which is the best it can do without this.
        self._connectors_state_provider: Callable[[], list[dict[str, Any]]] | None = None

    @property
    def connectors(self) -> dict[str, Connector]:
        return self._connectors_provider()

    def set_unattended_changed_listener(self, callback: Callable[[], None] | None) -> None:
        self._unattended_changed_listener = callback

    def set_bootstrap_link_provider(self, callback: Callable[[str], str | None] | None) -> None:
        """``callback`` is ``WebServer.mint_bootstrap_url`` in production --
        typed narrowly as ``str -> str | None`` here rather than importing
        web/server.py (which would be a circular import: server.py already
        imports this module's ``McpDispatcher``)."""
        self._bootstrap_link_provider = callback

    def set_connectors_state_provider(self, callback: Callable[[], list[dict[str, Any]]] | None) -> None:
        """``callback`` is ``SettingsController.status_connectors`` in
        production -- typed narrowly as a bare ``Callable`` here, same as
        ``set_bootstrap_link_provider`` above, rather than importing
        settings_controller.py just for the annotation."""
        self._connectors_state_provider = callback

    # ------------------------------------------------------------------ #
    # Manifest
    # ------------------------------------------------------------------ #

    def build_manifest(self) -> dict:
        """Same shape as ``IPCServer._build_manifest`` -- kept for parity/
        debugging even though routes_mcp.py's ``list_tools`` handler builds
        MCP ``Tool`` objects (mcp_tools.to_mcp_tool) rather than consuming
        this dict directly."""
        return {
            "connectors": [
                {"name": c.name, "tools": [spec.to_dict() for spec in c.tool_specs()]}
                for c in self.connectors.values()
            ]
        }

    # ------------------------------------------------------------------ #
    # Connector calls -- dedupe/staleness logic ported from
    # IPCServer._call_connector; see that method's own docstring (module
    # docstring of ipc_server.py) for the full rationale.
    # ------------------------------------------------------------------ #

    async def call(self, session_key: Hashable, connector_name: str, tool: str, args: dict) -> Any:
        # "reason" must never reach the dedupe key or connector.call() --
        # see ipc_server.py's _call_connector docstring for why (a freshly
        # regenerated reason string on every retry would defeat dedupe).
        args = dict(args)
        reason = args.pop("reason", "")
        connector = self.connectors.get(connector_name)
        if connector is None:
            raise ValueError(f"Unknown connector: {connector_name!r}")

        # P7: principal_id folds into both the dedupe key and the
        # last-write timestamp below.
        # Without it, two different org-mode principals calling the same
        # tool with the same arguments within _DEDUPE_TTL_SECONDS would
        # share one cache entry -- the second caller getting handed the
        # first caller's actual result, not a mere inefficiency but a
        # cross-principal data leak. Harmless in local mode (there's only
        # ever the one principal), but this dispatcher is shared process-
        # wide, so it has to be correct once a second principal exists.
        principal_id = current_principal().id
        now = time.time()
        self._prune_stale(now)
        key = self._dedupe_key(principal_id, connector_name, tool, args)
        entry = self._inflight.get(key)
        if entry is not None:
            fut, recorded_at = entry
            still_fresh = (now - recorded_at) < self._DEDUPE_TTL_SECONDS
            read_is_stale = self._is_read_only(connector, tool) and (
                recorded_at <= self._last_write_at.get((principal_id, connector_name), 0.0)
            )
            reusable = not fut.done() or (
                still_fresh and tool not in self._DEDUPE_EXEMPT_TOOLS and not read_is_stale
            )
            if reusable:
                logger.info(
                    "Deduping repeat call to %s/%s: reusing in-flight/recent result", connector_name, tool,
                )
                return await fut

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = (fut, now)
        try:
            with unattended_scope(session_key in self._unattended_sessions), reason_scope(reason):
                result = await connector.call(tool, args)
        except asyncio.CancelledError:
            self._inflight.pop(key, None)
            if not fut.done():
                fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved -- see ipc_server.py's identical comment
            raise
        fut.set_result(result)
        if is_pending_result(result):
            # P3: never cache a {"status": "approval_pending", ...} result
            # -- see approvals.is_pending_result's own docstring. Popped
            # immediately, same as the CancelledError branch above, so the
            # re-issued call Claude is expected to make once a human
            # decides (§5.2 point 6) reaches gate.gated_call() again
            # instead of being handed this same stale pending blob back.
            self._inflight.pop(key, None)
            return result
        if not self._is_read_only(connector, tool):
            self._last_write_at[(principal_id, connector_name)] = time.time()
        return result

    def _prune_stale(self, now: float) -> None:
        stale = [
            key for key, (fut, recorded_at) in self._inflight.items()
            if fut.done() and (now - recorded_at) >= self._DEDUPE_TTL_SECONDS
        ]
        for key in stale:
            del self._inflight[key]

    @staticmethod
    def _dedupe_key(principal_id: str, connector_name: str, tool: str, args: dict) -> str:
        return f"{principal_id}:{connector_name}:{tool}:{json.dumps(args, sort_keys=True, default=str)}"

    @staticmethod
    def _is_read_only(connector: Connector, tool: str) -> bool:
        for spec in connector.tool_specs():
            if spec.name == tool:
                return spec.read_only
        return False

    # ------------------------------------------------------------------ #
    # Meta-tools -- ported from IPCServer._check_policy/_list_rules/
    # _propose_rule_change (see ipc_server.py for the full rationale on
    # each; identical behavior, same audit entries).
    # ------------------------------------------------------------------ #

    def check_policy(self, connector_name: str, tool: str, args: dict, claude_reason: str = "") -> dict:
        connector = self.connectors.get(connector_name)
        if connector is None:
            raise ValueError(f"Unknown connector: {connector_name!r}")

        gate = TOOL_TO_GATE.get(tool)
        if gate is None:
            raise ValueError(f"Unknown tool: {tool!r}")

        if gate == "auto":
            result = {
                "gate": "auto", "verdict": "auto_accept", "matched_rule": None,
                "reason": "Unconditionally auto-accepted -- never reaches the review gate.",
                "pii_gate_may_apply": False,
            }
        else:
            operation_key = TOOL_TO_OPERATION.get(tool, f"{connector_name}.{tool}")
            my_email = getattr(connector, "my_email", "")
            verdict, matched_rule, reason = get_auto_accept_evaluator().preflight_from_args(
                operation_key, args, my_email
            )
            if gate == "review":
                reason += (
                    " Read calls also pass through the PII detection gate, which scans actual "
                    "content and can force a popup even when a rule matches -- this can't be "
                    "predicted before the read happens."
                )
            result = {
                "gate": gate, "verdict": verdict, "matched_rule": matched_rule or None,
                "reason": reason, "pii_gate_may_apply": gate == "review",
            }

        self._audit_policy_check(connector_name, tool, result, claude_reason)
        return result

    @staticmethod
    def _audit_policy_check(connector_name: str, tool: str, result: dict, claude_reason: str = "") -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector=connector_name,
                tool=tool,
                tool_name="",
                summary=f"Preflight check: verdict={result['verdict']!r}",
                sender="",
                decision="policy_check",
                auto_accept_rule=result.get("matched_rule") or "",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for policy check: %s", exc)

    def list_rules(self, claude_reason: str = "") -> dict:
        # Forces this principal's ConnectorRegistry entry (and the
        # auto_accept.init_config_path() call daemon_main.py's per-
        # principal factory makes as a side effect of building it) to
        # exist first -- same reasoning as propose_rule_change above:
        # get_current_config() raises "auto_accept config path not
        # initialized" without it, for a principal whose first-ever MCP
        # call in this process is this one.
        _ = self.connectors
        result = get_current_config()
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary="Listed current auto-accept rules/grants",
                sender="",
                decision="rules_listed",
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for list_rules: %s", exc)
        return result

    def get_sign_in_link(self, page: str, claude_reason: str = "") -> dict:
        """privacyfence_get_sign_in_link's handler: mint a fresh SEC-06
        bootstrap link for local mode's own web UI, via whatever
        ``set_bootstrap_link_provider`` was last wired to -- unset (org
        mode, or a test that never wires one) raises the same
        "not available in this configuration" ``ValueError`` posture
        ``begin_unattended_session`` already takes for a disabled feature,
        rather than returning a link that doesn't work."""
        if self._bootstrap_link_provider is None:
            raise ValueError(
                "No sign-in link is available in this configuration -- organization mode signs "
                "in through its own /login page instead of a one-time bootstrap link."
            )
        page = page or "approvals"
        if page not in ("approvals", "settings"):
            raise ValueError(f"page must be 'approvals' or 'settings', got {page!r}")
        url = self._bootstrap_link_provider(f"/{page}")
        if url is None:
            raise ValueError(
                "No sign-in link is available in this configuration -- organization mode signs "
                "in through its own /login page instead of a one-time bootstrap link."
            )
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary=f"Issued a one-time sign-in link for /{page}",
                sender="",
                decision="sign_in_link_issued",
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for get_sign_in_link: %s", exc)
        return {"url": url}

    def status(self, claude_reason: str = "") -> dict:
        """privacyfence_status's handler (issue #396 Phase 2): the one
        meta-tool guaranteed to answer even when ``connectors == []`` makes
        every other tool -- meta-tools included, for a client that only
        lists them alongside real connector tools -- look identical to
        "PrivacyFence has nothing to do with this". ``setup_complete`` is
        true once at least one connector is authenticated; a partially set
        up install (one connector authenticated, another not) still reports
        ``setup_complete: true`` -- volunteering that a *specific* other
        connector needs attention is left to the model reading the per-
        connector rows, not this method deciding it's worth a nag (open
        question #4 in the issue: whether partial setup deserves a nudge on
        every call was left to a later pass)."""
        connectors = self._status_connector_rows()
        setup_complete = any(c["authenticated"] for c in connectors)
        result: dict[str, Any] = {
            "mode": self._mode, "setup_complete": setup_complete, "connectors": connectors,
        }

        if setup_complete:
            result["next_step"] = None
            result["message"] = (
                "PrivacyFence is set up -- at least one connector is authenticated. An empty "
                "tool list for a *different* connector means that one specifically isn't "
                "authenticated yet, not that this install needs setting up from scratch."
            )
            self._audit_status_check("status_checked", claude_reason)
            return result

        if self._mode != "local":
            result["next_step"] = "contact_your_administrator"
            result["sign_in_url"] = None
            result["message"] = (
                "PrivacyFence is running in organization mode, but no connector is authenticated "
                "for this account yet. There is no local sign-in link here -- organization mode "
                "signs in through its own identity provider, so ask the human to check with their "
                "administrator about getting connectors authorized for their account."
            )
            self._audit_status_check("status_checked", claude_reason)
            return result

        result["next_step"] = "authenticate_connectors"
        url, freshly_minted = self._mint_status_link()
        result["sign_in_url"] = url
        if url is not None:
            result["message"] = (
                "PrivacyFence is running, but nothing is authenticated yet -- an empty or partial "
                "tool list means \"not set up\", not \"nothing to do here\". Share this sign-in "
                "link with the human so they can open PrivacyFence's Settings and authenticate at "
                "least one connector (Gmail, Slack, etc.); PrivacyFence-governed tools stay "
                "unavailable until they do."
            )
        else:
            result["message"] = (
                "PrivacyFence is running, but nothing is authenticated yet, and no sign-in link "
                "is currently available. Ask the human to open PrivacyFence's Settings directly "
                "on this machine and authenticate at least one connector."
            )
        self._audit_status_check("sign_in_link_issued" if freshly_minted else "status_checked", claude_reason)
        return result

    def _status_connector_rows(self) -> list[dict[str, Any]]:
        if self._connectors_state_provider is not None:
            return self._connectors_state_provider()
        # No provider wired (org mode today -- see this constructor's own
        # comment -- or a test that never called set_connectors_state_
        # provider): the best available answer is "every connector this
        # dispatcher can actually see is authenticated, nothing is known
        # about any other", since only built Connector objects are visible
        # at all without SettingsController's own config/failure state.
        return [
            {"name": name, "enabled": True, "authenticated": True, "blocked_by": None}
            for name in sorted(self.connectors)
        ]

    def _mint_status_link(self) -> tuple[str | None, bool]:
        """Returns ``(url, freshly_minted)``. Reuses a cached link within
        ``_STATUS_LINK_CACHE_SECONDS`` instead of minting a fresh SEC-06
        bootstrap code (and rewriting WebServer's discovery file) on every
        single status() call while un-onboarded -- see this class's own
        comment on ``_status_link_cache`` for why."""
        if self._bootstrap_link_provider is None:
            return None, False
        now = time.time()
        if self._status_link_cache is not None:
            cached_url, minted_at = self._status_link_cache
            if now - minted_at < self._STATUS_LINK_CACHE_SECONDS:
                return cached_url, False
        url = self._bootstrap_link_provider("/settings")
        if url is None:
            self._status_link_cache = None
            return None, False
        self._status_link_cache = (url, now)
        return url, True

    @staticmethod
    def _audit_status_check(decision: str, claude_reason: str = "") -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary=(
                    "Issued a one-time sign-in link for /settings (via privacyfence_status)"
                    if decision == "sign_in_link_issued" else "Checked PrivacyFence setup status"
                ),
                sender="",
                decision=decision,
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for status: %s", exc)

    async def await_approval(self, approval_ids: list[str], timeout_seconds: int = 30) -> dict[str, str]:
        """privacyfence_await_approval's handler: long-poll ``approval_ids``
        against the registry and return their current status once anything
        changes, or once the (clamped) timeout elapses -- whichever comes
        first. Status only, never content (§5.2 point 7 -- see
        approvals.PendingApprovalRegistry.await_status's own docstring for
        the exact vocabulary). Scoped to current_principal() (P9): an id
        belonging to another principal reads as "unknown", the same
        cross-principal check §10.5 requires of every other approval read."""
        ids = [str(i) for i in (approval_ids or [])]
        if not ids:
            return {}
        if self._registry is None:
            return {approval_id: "unknown" for approval_id in ids}
        principal_id = current_principal().id
        timeout = max(
            self._AWAIT_APPROVAL_MIN_TIMEOUT,
            min(int(timeout_seconds or 0), self._AWAIT_APPROVAL_MAX_TIMEOUT),
        )
        deadline = time.time() + timeout
        while True:
            statuses = {
                approval_id: self._registry.await_status(approval_id, principal_id=principal_id)
                for approval_id in ids
            }
            if time.time() >= deadline or any(s != "pending" for s in statuses.values()):
                return statuses
            await asyncio.sleep(self._AWAIT_APPROVAL_POLL_SECONDS)

    async def propose_rule_change(self, session_key: Hashable, params: dict) -> dict:
        # TST-02 regression: this used to be a @staticmethod that called
        # gate.propose_rule_change() with no unattended_scope() around it at
        # all -- unlike call() above, which always wraps a connector
        # dispatch in unattended_scope(session_key in self._unattended_
        # sessions). That meant privacyfence_propose_auto_accept_rule_change
        # never saw itself as unattended even after this exact session had
        # called privacyfence_begin_unattended_session, and it fell through
        # to a real (never-to-be-answered) show_rule_confirmation_popup()
        # instead of the immediate denial its own tool description promises
        # ("If ... this connection is in an unattended session, the call
        # throws"). Needs session_key threaded through from
        # routes_mcp._dispatch_meta_tool for is_unattended() to see it.
        #
        # Forces this principal's ConnectorRegistry entry to exist (a
        # no-op if a connector tool call already built it this session) --
        # daemon_main.py's own per-principal factory is what calls
        # auto_accept.init_config_path() for whichever principal
        # ConnectorRegistry.get() builds, and gate.propose_rule_change()
        # below (target="rule"/"grant") reaches add_auto_accept_rule/
        # mutate_grants, both of which raise "auto_accept config path not
        # initialized" if that never happened. Unlike a real connector
        # tool call (routes_mcp._dispatch_connector_tool already touches
        # self.connectors before dispatching), this meta tool has no
        # connector of its own to force that same lazy bootstrap, so it
        # has to ask for it directly -- discovered by docs/automated-test-
        # strategy-plan.md Phase 8's own release-workflow smoke test: a
        # principal whose very first MCP call was this one had never had
        # this side effect run at all.
        _ = self.connectors
        with unattended_scope(session_key in self._unattended_sessions):
            return await propose_rule_change(
                target=params["target"],
                operation=params["operation"],
                reason=params.get("reason", ""),
                operation_key=params.get("operation_key", ""),
                rule_name=params.get("rule_name", ""),
                value=params.get("value"),
                old_value=params.get("old_value"),
                connector=params.get("connector", ""),
                config_key=params.get("config_key", ""),
                resource_id=params.get("resource_id", ""),
                name=params.get("name"),
                tab=params.get("tab"),
                capabilities=params.get("capabilities"),
            )

    # ------------------------------------------------------------------ #
    # Unattended sessions -- ported from IPCServer.begin/end_unattended_
    # session/unattended_session_count/_audit_unattended_session_event.
    # Cleared explicitly by end_unattended_session, or by end_session()
    # (routes_mcp.py calls this from the per-MCP-session lifespan's own
    # finally block -- the direct counterpart of ipc_server.py's
    # _handle_connection finally block clearing id(writer)).
    # ------------------------------------------------------------------ #

    def begin_unattended_session(self, session_key: Hashable, claude_reason: str = "") -> dict:
        if not self._unattended_sessions_enabled:
            raise ValueError(
                "Unattended sessions are disabled. An administrator must set "
                "unattended_sessions.enabled: true in the organization config bundle "
                "(org_config.json) before this connection can be marked unattended."
            )
        self._unattended_sessions.add(session_key)
        logger.warning(
            "Unattended session started on MCP session %r -- unmatched review/popup calls on "
            "this session will now be denied immediately instead of prompting",
            session_key,
        )
        self._audit_unattended_session_event("unattended_session_started", claude_reason)
        self._fire_unattended_changed()
        return {"unattended": True}

    def end_unattended_session(self, session_key: Hashable, claude_reason: str = "") -> dict:
        changed = session_key in self._unattended_sessions
        self._unattended_sessions.discard(session_key)
        logger.info("Unattended session ended on MCP session %r", session_key)
        if changed:
            self._audit_unattended_session_event("unattended_session_ended", claude_reason)
            self._fire_unattended_changed()
        return {"unattended": False}

    def end_session(self, session_key: Hashable) -> None:
        """Called once, when the MCP session this key identifies ends (see
        module docstring) -- whatever unattended-session state it carried
        dies with it, the same way a dropped bridge connection used to."""
        had_unattended = session_key in self._unattended_sessions
        self._unattended_sessions.discard(session_key)
        if had_unattended:
            self._audit_unattended_session_event("unattended_session_ended")
            self._fire_unattended_changed()

    def unattended_session_count(self) -> int:
        return len(self._unattended_sessions)

    def _fire_unattended_changed(self) -> None:
        if self._unattended_changed_listener is not None:
            self._unattended_changed_listener()

    @staticmethod
    def _audit_unattended_session_event(decision: str, claude_reason: str = "") -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary="This MCP session's unattended-session state changed",
                sender="",
                decision=decision,
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for unattended-session event: %s", exc)
