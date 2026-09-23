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
from ..auto_accept import TOOL_TO_GATE, TOOL_TO_OPERATION, get_policy_v2_rules
from .. import local_files
from ..connector import Connector
from ..gate import preflight_auto_accept, propose_policy_change, propose_rule_change, reason_scope, unattended_scope
from ..policy import catalogue as policy_catalogue
from ..policy import describe as policy_describe
from ..policy import propose as policy_propose
from ..policy import registry as policy_registry
from ..principal import current_principal

logger = logging.getLogger(__name__)


def _policy_rule_row(rule) -> dict:
    """One ``privacyfence_list_policy`` rule row -- the same fields
    ``settings_controller.SettingsController._auto_accept_state`` renders for the Auto-accept
    Settings page (P6), minus that method's own name-resolution machinery (a friendly display name
    for an opaque id like a Drive folder), which is a web-page-only affordance the bridge has no use
    for: a model reading ``covered_tools``/``sentence`` already gets the width of the rule, and raw
    ids are exactly what it would pass back into ``privacyfence_propose_policy_change`` anyway."""
    connectors_of_rule = sorted({policy_propose.connector_of_operation(op) for op in rule.operations})
    return {
        "id": rule.id,
        "sentence": policy_describe.rule_sentence(rule),
        "connector": connectors_of_rule[0] if connectors_of_rule else "",
        "scope_type": policy_describe.scope_type_of(rule),
        "value": rule.value,
        "operations": sorted(rule.operations),
        "verbs": [
            {"verb": verb.value, "family": policy_registry.VERB_FAMILY[verb].value}
            for verb in policy_describe.rule_verbs(rule)
        ],
        "conditions": [[name, value] for name, value in rule.conditions],
        "covered_tools": sorted(policy_describe.covered_tools(rule)),
    }


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
        # decides which next_step an un-onboarded install reports (open the
        # companion, or ask an administrator -- org mode has no local sign-in
        # path at all). daemon_main.py passes "org" from
        # _start_org_web_server; every other call site (and every existing
        # test) keeps the local-mode default.
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
        # issue #396 Part C: fans a real MCP ``tools/list_changed``
        # notification out to every live Streamable HTTP session --
        # wired to web/routes_mcp.py's own broadcaster (which is the one
        # module that actually holds each session's live ``ServerSession``,
        # see build_mcp_server's own docstring) once that transport is
        # actually up. None in org mode today (routes_mcp.py's org-mode
        # build path doesn't wire one -- no per-principal live-session
        # story exists yet), and in any test that never calls
        # set_tools_changed_broadcaster -- notify_tools_changed() is then
        # simply a no-op, the same posture every other unwired seam here
        # already takes.
        self._tools_changed_broadcaster: Callable[[], None] | None = None

    @property
    def connectors(self) -> dict[str, Connector]:
        return self._connectors_provider()

    def set_unattended_changed_listener(self, callback: Callable[[], None] | None) -> None:
        self._unattended_changed_listener = callback

    def set_connectors_state_provider(self, callback: Callable[[], list[dict[str, Any]]] | None) -> None:
        """``callback`` is ``SettingsController.status_connectors`` in
        production -- typed narrowly as a bare ``Callable`` here rather than
        importing settings_controller.py just for the annotation, which would
        be a circular import: that module reaches this one."""
        self._connectors_state_provider = callback

    def set_tools_changed_broadcaster(self, callback: Callable[[], None] | None) -> None:
        """``callback`` is ``build_mcp_server``'s own local
        ``_broadcast_tools_changed`` closure (web/routes_mcp.py) --
        untyped/unimported here for the same circular-import reason
        ``set_connectors_state_provider`` above gives, and because that
        closure's own state (the live ``ServerSession`` per MCP session)
        has no business living on this dispatcher."""
        self._tools_changed_broadcaster = callback

    def notify_tools_changed(self) -> None:
        """Called by ``SettingsController.refresh_connectors()`` (via
        ``set_connectors_changed_listener``, wired in daemon_main.py)
        whenever the live connector set actually changes -- fans a real
        ``tools/list_changed`` notification out to every open MCP session,
        closing issue #396's own "I set it all up and Claude still can't
        see it" gap. A no-op wherever no broadcaster is wired (org mode
        today, or any test that never calls
        set_tools_changed_broadcaster) -- there is nothing live to notify
        in that case."""
        if self._tools_changed_broadcaster is not None:
            self._tools_changed_broadcaster()

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
            # B3: a failed call must not be replayed for the rest of the
            # dedupe window -- a caller already awaiting this exact `fut`
            # (the `return await fut` branch above) still gets the
            # exception fine, since that's the future object itself, not
            # this dict entry; popping only stops a *new* call in the same
            # window from being handed the same stale failure instead of
            # actually retrying.
            self._inflight.pop(key, None)
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
        if local_files.call_produced_deliveries():
            # B3: a result that staged a file-bridge download carries a
            # single-use download_staging token in its _meta -- reusing it
            # from the dedupe cache would hand a second caller a token the
            # first claim (or the shim writing the first response to disk)
            # already consumed. See local_files.call_produced_deliveries's
            # own docstring.
            self._inflight.pop(key, None)
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
                "gate": "auto", "verdict": "auto_accept", "matched_rule": None, "matched_rule_id": None,
                "reason": "Unconditionally auto-accepted -- never reaches the review gate.",
                "pii_gate_may_apply": False,
            }
        else:
            operation_key = TOOL_TO_OPERATION.get(tool, f"{connector_name}.{tool}")
            my_email = getattr(connector, "my_email", "")
            verdict, matched_rule, matched_rule_id, reason = preflight_auto_accept(
                operation_key, args, my_email,
            )
            if gate == "review":
                reason += (
                    " Read calls also pass through the PII detection gate, which scans actual "
                    "content and can force a popup even when a rule matches -- this can't be "
                    "predicted before the read happens."
                )
            result = {
                "gate": gate, "verdict": verdict, "matched_rule": matched_rule or None,
                "matched_rule_id": matched_rule_id or None,
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
        """``privacyfence_list_auto_accept_rules``'s handler -- deprecated alias of
        ``list_policy`` below (P9). Through P8 this returned a raw read of the v1
        ``auto_accept_rules``/``auto_accept_grants`` config sections; now that every rule lives in
        the v2 ``auto_accept:`` section regardless of which surface created it, there is no longer
        a separate v1 view to show -- reading the old sections directly would show stale content
        (whatever they held before the one-time migration folded them into v2), not what actually
        auto-accepts. Returns exactly what ``list_policy`` does."""
        return self.list_policy(claude_reason)

    def list_policy(self, claude_reason: str = "") -> dict:
        """privacyfence_list_policy's handler (P7 of the policy v2 redesign): the on-disk v2
        ``auto_accept:`` section, sentence-rendered the same way ``settings_controller.
        SettingsController._auto_accept_state`` renders it for the Auto-accept Settings page, plus
        the scope catalogue ``privacyfence_propose_policy_change``'s ``group``/``verbs`` validate
        against -- so a model can discover a real rule id and a real (group, verbs) pair before
        proposing anything, the same "list before you propose" contract ``list_rules``/
        ``propose_rule_change`` above already have."""
        _ = self.connectors
        rule_rows = sorted((_policy_rule_row(rule) for rule in get_policy_v2_rules()), key=lambda row: row["sentence"])
        result = {"rules": rule_rows, "scope_groups": policy_catalogue.scope_catalogue()}
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary="Listed current policy rules",
                sender="",
                decision="policy_listed",
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for list_policy: %s", exc)
        return result

    async def propose_policy_change(self, session_key: Hashable, params: dict) -> dict:
        """privacyfence_propose_policy_change's handler -- same ``unattended_scope``/
        ``ConnectorRegistry``-bootstrap reasoning as ``propose_rule_change`` above applies here too:
        see that method's own comment."""
        _ = self.connectors
        with unattended_scope(session_key in self._unattended_sessions):
            return await propose_policy_change(
                operation=params["operation"],
                reason=params.get("reason", ""),
                rule_id=params.get("rule_id", ""),
                group=params.get("group", ""),
                value=params.get("value"),
                verbs=params.get("verbs"),
            )

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

        # The self-approval plan's Phase 2 repointed this at the companion.
        # It used to read "ask_for_sign_in_link", which told the model to
        # offer to mint a live session credential and hand it over -- the
        # tool that did so is retired (web/mcp_tools.py's own module
        # docstring), and what is left for an un-onboarded install is the
        # one affordance that does not route through this process at all.
        result["next_step"] = "open_privacyfence_companion"
        result["sign_in_url"] = None
        result["message"] = (
            "PrivacyFence is running, but nothing is authenticated yet -- an empty or partial "
            "tool list means \"not set up\", not \"nothing to do here\". Ask the human to open "
            "PrivacyFence's companion app -- the menu-bar icon on macOS, the tray icon on "
            "Windows, the PrivacyFence entry in the applications menu on Linux -- and choose "
            "Open Settings, then authenticate at least one connector (Gmail, Slack, etc.). "
            "There is no link for you to hand them: a sign-in link is exactly the credential "
            "that governs this process, so PrivacyFence no longer issues one to it. "
            "PrivacyFence-governed tools stay unavailable until they finish."
        )
        self._audit_status_check("status_checked", claude_reason)
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

    @staticmethod
    def _audit_status_check(decision: str, claude_reason: str = "") -> None:
        # ``decision`` is always "status_checked" -- privacyfence_status
        # never mints a sign-in link itself, and since the self-approval
        # plan's Phase 2 retired privacyfence_get_sign_in_link, nothing
        # reachable over /mcp does (issue #396's own threat-model follow-up
        # asked for the narrower version of this: that the credential only
        # be issued because a human asked). Kept as a parameter rather than
        # hardcoded so a future distinct status-only decision doesn't need
        # this call site touched again.
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary="Checked PrivacyFence setup status",
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
