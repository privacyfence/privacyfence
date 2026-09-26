"""Deferred-approval registry: the domain object on top of gate.py's
decision loop.

Principal dimension: ``WebApprovalUI`` stays a true singleton deliberately
(``approval_ui.py``'s own module docstring) -- in org mode one instance
serves every principal, so this registry carries the principal dimension
internally instead. web/routes_approvals.py's org-mode routes reach it from
more than one principal at once, so every ``PendingApproval`` stamps
``principal_id`` at registration time (from ``current_principal()`` -- the
same contextvar pattern every other per-principal registry in this codebase
uses, so gate.py's own call sites into
``register_or_coalesce``/``register_confirm`` need no principal argument),
and every read/write method below takes an optional ``principal_id`` to
filter or authorize against. ``None`` (the default everywhere) means "no
filter" -- gate.py's own internal calls keep seeing every approval
regardless of principal, which is also exactly correct for local mode's
single implicit principal. web/routes_approvals.py's org-mode routes are the
one real caller that ever passes a real ``principal_id``, and they do so on
every method they call: every approval, card, preview or decision read is
authorized against ``current_principal()``.

The coalescing/ledger key carries the principal too, for the same reason
web/mcp_dispatch.py's retry-dedupe cache does: ``(connector, tool,
canonical(args))`` alone collides across two principals who happen to call
the same tool with the same arguments, and one would be handed the other's
decision. ``_by_key`` is keyed on ``(principal_id, dedupe_key)`` here for
the same reason.

Two kinds of caller reach this module:

- gate.py, for the *main* gate decision of a review/popup-gated call -- see
  ``register_or_coalesce``/``consume_ledger``/``finalize``/``wait_async``,
  which together implement the deferred protocol: register a pending
  approval, wait up to ``hold_window`` seconds, and if nobody decided in
  time, let the caller return a structured "approval_pending" result instead
  of blocking further. A later, identical call finds the decision via
  ``consume_ledger`` and releases without a second prompt.
- web_approval_ui.py, for *every* card and confirmation dialog it shows
  (whether or not it's the "main" decision for some gated call) -- see
  ``register_confirm``/``answer``/``get``/``list_pending``, the multi-item
  store behind every card the browser shows.

One registry instance backs both uses (``WebApprovalUI.deferred_registry``),
so a card gate.py is waiting on and a card a human is looking at in the
browser are always the same object.

Two decision layers, not one, because a single gated call can involve more
than one human interaction (the main popup, then possibly a PII
"are you sure?" confirmation, then possibly an "Always allow" rule
confirmation -- see gate.py's own module docstring):

- ``answer()`` resolves one *UI step* -- whatever card or confirmation is
  currently on screen. It never touches the ledger; it only wakes up
  whichever thread is blocked showing that one dialog (WebApprovalUI's
  ``show_popup``/``show_read_popup``/the two confirmation methods, which
  block until answered -- see that module).
- ``finalize()`` resolves the *whole approval* -- the outcome gate.py's own
  interaction driver arrives at after however many UI steps it took. Only
  finalize() writes the decision ledger (keyed by
  ``(connector, tool, canonical(args))``, single-use for writes, so one
  approval never releases a second identical write -- ADR 0073) and
  wakes ``wait_async()`` -- the thing gate.py's hold window actually awaits.

A decided outcome reaches a caller one of two ways: through the ledger
(``consume_ledger()``, a re-issued call), or directly, when the human
decides within the hold window while the original call is still waiting
(gate.py then calls ``mark_collected()``). Both mark the entry
``ledger_collected``, and for a write both consume it, so single-use holds
on every path that releases a write, and ``pop_expired_ledger_events()``
reports only an outcome that no call ever collected (ADR 0073).

Both events are ``threading.Event`` rather than ``asyncio.Event``: this
registry is touched from the asyncio event loop (gate.py, the web routes'
request handlers) *and* from plain OS threads (WebApprovalUI's blocking
calls run on gate.py's ``_popup_executor``; a rules-changed broadcast can
fire from any thread that called ``auto_accept.reload_rules()``, e.g. the
AppKit main thread). ``threading.Event`` is safe to set/wait from any of
them; ``wait_async()`` bridges back into the event loop via
``asyncio.to_thread`` for the one caller (gate.py) that needs a non-blocking
await.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .agent_identity import UNKNOWN_AGENT, AgentIdentity, current_agent
from .agent_label import label_for
from .principal import current_principal

logger = logging.getLogger(__name__)

# Defaults, all overridable by daemon_main.py from settings.yaml's
# web.approvals.* keys.
DEFAULT_HOLD_WINDOW_SECONDS = 30.0
DEFAULT_PENDING_TTL_SECONDS = 15 * 60.0
DEFAULT_LEDGER_TTL_SECONDS = 5 * 60.0
DEFAULT_MAX_PENDING = 50
# DEFAULT_MAX_PENDING alone is a single shared budget across every principal a
# registry serves. In org mode (one registry, many principals -- see
# module docstring) that means one noisy or malicious principal issuing a
# burst of distinct gated calls can fill the entire registry and lock
# every other principal's own gated calls into "too many approvals are
# already pending" -- a denial-of-service against everyone else sharing
# the daemon, not just against the caller doing it. This is the per-
# principal share of that same budget: low enough that no single principal
# can exhaust DEFAULT_MAX_PENDING alone, checked first in
# register_or_coalesce() so a principal hits their own limit before ever
# threatening the shared one. DEFAULT_MAX_PENDING stays in force
# unchanged as the secondary, whole-registry backstop it always was --
# local mode's single implicit principal ("local") is bounded by both caps
# at once, which is harmless since it's the only principal there is.
DEFAULT_MAX_PENDING_PER_PRINCIPAL = 20

# Adaptive hold, on by default. A sequential agent that issues
# gated calls one at a time never fills the binder -- it stalls the full
# hold_window on call #1, gets a pending result, and only issues call #2
# after relaying that and waiting on a human. Once this principal already
# has one unfinalized approval outstanding, holding a second (or third, or
# twelfth) one for hold_window too buys nothing -- the human demonstrably
# isn't answering within a hold window, or the first would already be
# decided -- so gate.py's _resolve_decision() collapses that later call's
# own wait to zero instead, returning "approval_pending" immediately so
# Claude can move on to the next independently-ready call. See
# PendingApprovalRegistry.has_other_live() and gate.py's own docstring.
DEFAULT_ADAPTIVE_HOLD = True

# Every UI-step decision a card/confirmation can resolve to -- the same
# vocabulary approval_window_html.py's own JS uses. "auto_accepted" is never produced by a UI step (no
# button says that); it's finalize()'s own sentinel for "a rule appeared
# that already covers this, so no human ever needed to answer" -- gate.py's
# interaction driver returns it directly to finalize() without going through
# answer() at all. See gate.py's own module docstring.
CARD_RESULTS = ("accept", "deny", "accept_all")
CONFIRM_RESULTS = ("confirm", "cancel")

# The approval binder's own batch decide endpoint: a deliberately narrower vocabulary than CARD_RESULTS above --
# "accept_all" needs its own scoped rule-creation confirmation (a second UI
# step that only exists per-item), and is never offered from the list, so a
# batch item is decided as a plain accept or a deny only. See
# PendingApprovalRegistry.answer_batch's own docstring.
BATCH_RESULTS = ("accept", "deny")

# Every value PendingApproval.kind can take -- "card" (web_prompt.
# block_on_card), "confirm" (block_on_confirm), "choice" (block_on_choice).
# PendingApproval.is_batchable()/blocked_reason() below classify by explicit
# membership in _BATCHABLE_KINDS/_NON_BATCHABLE_KINDS, never by complement
# (kind not in _BATCHABLE_KINDS) -- the same reasoning web/routes_settings.py's
# own _SENSITIVE_ACTIONS/_NON_SENSITIVE_ACTIONS pair gives for staying
# explicit rather than derived: TestBatchableKindsCoverAllApprovalKinds
# (test_approvals.py) fails the moment a new kind lands in ALL_APPROVAL_KINDS
# without a matching entry in both sets, so it can never silently default to
# either "batchable" or "not batchable" by accident.
ALL_APPROVAL_KINDS: frozenset[str] = frozenset({"card", "confirm", "choice"})

# A "card" approval (accept/deny/accept_all) is the one shape the binder can
# safely decide in bulk. "confirm"/"choice" resolve a different, mid-flight
# result vocabulary (web_prompt.py's block_on_confirm/block_on_choice) --
# batching those has no meaning, since there's no "accept"/"deny" to apply.
_BATCHABLE_KINDS: frozenset[str] = frozenset({"card"})
_NON_BATCHABLE_KINDS: frozenset[str] = frozenset({"confirm", "choice"})

_NON_BATCHABLE_KIND_REASON: dict[str, str] = {
    "confirm": "This is a confirmation dialog, not an approval — it can't be decided from the list.",
    "choice": "This is a selection dialog, not an approval — it can't be decided from the list.",
}


class IdenticalWriteAwaitingApprovalError(RuntimeError):
    """Raised by register_or_coalesce() for a write (``gate_kind ==
    "popup"``) whose identical twin is still being waited on by another call
    -- that call collects the decision, and coalescing onto it would let one
    human decision release two writes (ADR 0073). Fail-closed: nothing is
    registered or released for the refused call. Composed only of static
    text, so safe_errors.public_message() forwards it verbatim, like
    TooManyPendingApprovalsError below."""


class TooManyPendingApprovalsError(RuntimeError):
    """Raised by register_or_coalesce() when either cap is already reached
    -- fail-closed rather than queueing: rejecting further gated calls is the
    natural backstop against a runaway agent. The
    per-principal cap (``max_pending_per_principal``) is checked first and
    is the one that matters day to day in org mode; the whole-registry cap
    (``max_pending``) is the secondary backstop it always was -- see
    DEFAULT_MAX_PENDING_PER_PRINCIPAL's own comment for why both exist."""


def canonical_key(connector: str, tool: str, args: dict[str, Any] | None) -> str:
    """The decision-ledger / coalescing key: ``(connector, tool,
    canonical(args))``. Same shape mcp_dispatch.py's own retry-dedupe key
    uses -- already retry-stable, since every
    caller into gate.gated_call() has "reason" popped out of ``args`` before
    it gets here (see gate.py's ``reason_scope`` docstring), so re-issuing
    the identical tool call always reproduces the identical key.
    """
    return f"{connector}:{tool}:{json.dumps(args or {}, sort_keys=True, default=str)}"


def is_pending_result(result: Any) -> bool:
    """True for exactly the shape gate.py's ``_pending_result()`` returns
    (``{"status": "approval_pending", ...}``) -- the one gated_call() result
    shape that is not a real answer yet. mcp_dispatch.py's own retry-dedupe
    cache ("reuse the answer to an identical in-flight or just-finished
    call") checks this before caching a completed result: caching a
    *pending* result would mean the identical re-call Claude is supposed to
    make to actually collect the decision just gets handed the same
    stale "still pending" blob back for up to that cache's own TTL, instead
    of ever reaching gate.gated_call() again to check the decision ledger.
    """
    return isinstance(result, dict) and result.get("status") == "approval_pending"


def _iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class PendingApproval:
    id: str
    kind: str                      # "card" | "confirm"
    # Whichever principal's request context was active at registration
    # time (see module docstring) -- "local" for every approval in local
    # mode.
    principal_id: str = field(default_factory=lambda: current_principal().id)
    connector: str = ""
    tool: str = ""
    gate_kind: str = ""            # "review" | "popup" -- "" for a bare confirm dialog
    request_id: str = ""
    summary: str = ""
    tool_name: str = ""
    dedupe_key: str | None = None  # None for a confirm dialog: never coalesced, never ledgered
    # True on a confirm dialog that *is* the gate rather than a second step
    # inside one -- see register_confirm()'s own ``sensitive`` parameter.
    sensitive: bool = False
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0        # pending-TTL deadline
    html: str = ""
    # Stamped at registration by gate.py, from the exact ``preview`` dict it
    # hands to show_popup()/show_read_popup() -- metadata only, per
    # docs/coding-and-testing-guidelines.md §1.5 ("preview dicts carry
    # metadata only... never body/content"), the same contract that already
    # governs every preview a card renders. Known the moment this approval
    # is registered, unlike ``html`` above, which stays "" until a
    # _popup_executor worker frees up to run build_card_html -- a consumer
    # that wants to disclose what's pending (a fragment endpoint, a future
    # binder row) can read this without waiting on that worker at all.
    preview: dict[str, Any] = field(default_factory=dict)
    # Re-evaluation context for the rules-changed broadcast (see
    # reevaluate_all()).
    operation_key: str | None = None
    review_ctx: Any = None
    pii_forces_confirmation: bool = False
    pii_detected: bool = False
    pii_categories: list[str] = field(default_factory=list)
    claude_reason: str = ""
    # The AI system that made this request, captured from current_agent() at
    # creation -- the same way claude_reason is carried. Audit rows about this
    # approval that are written later, from inside some unrelated call (gate.py's
    # expiry sweep), must use this rather than whatever agent that call runs
    # as, or they would attribute this request to someone else's.
    agent: AgentIdentity = UNKNOWN_AGENT

    # UI-step state -- see module docstring.
    event: threading.Event = field(default_factory=threading.Event, repr=False)
    result: str | None = None
    chosen_index_result: int | None = None

    # Approval-level (finalize) state -- see module docstring.
    finalize_event: threading.Event = field(default_factory=threading.Event, repr=False)
    final_decision: str | None = None       # "accept" | "deny" | "accept_all" | "auto_accepted" | "expired"
    # The one piece of extra context the final decision can carry: the
    # auto-accept rule name, for "accept_all" (a rule was just created) and
    # "auto_accepted" (an existing rule matched) alike -- "" otherwise. A
    # single string slot, not gate.py's UI-level button index: by the time
    # an interaction finalizes, any button-index bookkeeping it needed has
    # already been resolved into a rule name (or discarded), so there's
    # nothing else worth carrying here. See gate.py's own module docstring.
    final_rule_name: str = ""
    decided_at: float | None = None
    ledger_expires_at: float | None = None
    # True once no later call may take this entry from the ledger: a write
    # whose outcome was released (single-use), or an entry the expiry sweep
    # has already drained. A read's entry stays unconsumed until its TTL.
    ledger_consumed: bool = False
    # True once this approval's outcome reached any caller, by the ledger or
    # directly through the hold window -- separate from ledger_consumed
    # because a collected read still replays. pop_expired_ledger_events()
    # drops a collected entry silently: the release is already audited, so
    # "expired" is kept for an outcome nobody ever collected (ADR 0073).
    ledger_collected: bool = False
    # How many gated calls are waiting on this approval in wait_async() right
    # now. Counted under the registry lock by register_or_coalesce(waiting=
    # True) and released by release_waiter(). A write is never coalesced onto,
    # or taken from the ledger, while this is non-zero: the waiting call
    # collects its outcome (ADR 0073).
    waiters: int = 0
    # Audit provenance for the approval binder's batch decide endpoint
    # -- "" for every ordinary single-decide answer. Stamped by answer() itself (not a
    # separate setter) so it can never be set without also resolving the
    # UI step it describes. Carried forward into LedgerHit by
    # consume_ledger() below, and from there into the audit entry that
    # actually releases the call -- see gate.py's own module docstring.
    decided_via: str = ""
    batch_id: str = ""

    def answer(
        self, result: str, chosen_index: int | None = None, *, decided_via: str = "", batch_id: str = "",
    ) -> bool:
        """Resolve this UI step. Idempotent: the first answer wins, so two
        concurrent answers to the same card can't both take effect."""
        if self.event.is_set():
            return False
        self.result = result
        self.chosen_index_result = chosen_index
        self.decided_via = decided_via
        self.batch_id = batch_id
        self.event.set()
        return True

    def is_finalized(self) -> bool:
        return self.finalize_event.is_set()

    def is_batchable(self) -> bool:
        """The approval binder's own gate: a ``kind == "card"``
        approval that isn't itself PII-forced. Excluded, each for a
        different reason (see this module's own ``_NON_BATCHABLE_KINDS``/
        ``pii_forces_confirmation`` field docstrings): "confirm"/"choice"
        dialogs resolve a different, mid-flight result vocabulary, and a
        PII-forced card demands a second confirmation that only exists
        *after* this card is answered -- batching those would spray a fresh
        confirm dialog into the list for every batched item instead of
        resolving anything. Classified by explicit membership in
        ``_BATCHABLE_KINDS``, never by complement -- see that set's own
        comment."""
        return self.kind in _BATCHABLE_KINDS and not self.pii_forces_confirmation

    def blocked_reason(self) -> str:
        """Why this approval can't be selected in the binder -- "" exactly
        when ``is_batchable()`` is True. Surfaced on the row so a human sees
        *why* there's no checkbox, not just its absence. Falls back to a
        generic reason for a kind ``_NON_BATCHABLE_KIND_REASON`` has no
        specific entry for (fail-closed: still not batchable, just with a
        less specific explanation) -- TestBatchableKindsCoverAllApprovalKinds
        is what actually keeps that fallback from ever firing in practice."""
        if self.is_batchable():
            return ""
        if self.pii_forces_confirmation:
            return "This request needs its own PII confirmation — it can't be decided from the list."
        return _NON_BATCHABLE_KIND_REASON.get(
            self.kind, "This request can't be decided from the list.",
        )

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "connector": self.connector,
            "tool": self.tool,
            "tool_name": self.tool_name,
            # "review" (read) | "popup" (write) | "" for a bare confirm
            # dialog -- safe to expose unconditionally (it names a category,
            # never gated content) and is exactly the "direction" field
            # web_shell.py's notification-detail allowlist needs: never derived from
            # ``summary``, which is the one field that can carry real gated
            # content (see that field's own docstring below).
            "gate_kind": self.gate_kind,
            # A settings.yaml rule-scoped key (e.g. "drive.read_file_contents"),
            # never gated content -- the approval binder's own grouping key
            # (it groups by (connector, operation_key)). "" for a bare
            # confirm/choice dialog, which has none.
            "operation_key": self.operation_key or "",
            # Category-level fact already shown on the card itself (a tinted
            # banner naming the matched categories) -- never the categories
            # themselves, which is what pii_categories carries; this is only
            # "was anything flagged at all", which the binder needs to
            # explain a PII-forced row's own blocked_reason.
            "pii_detected": self.pii_detected,
            # Whether the approval binder may offer this approval
            # for selection at all -- see is_batchable()'s own docstring.
            "batchable": self.is_batchable(),
            # Human-readable reason there's no checkbox on this row -- ""
            # when batchable is True. Never gated content (see
            # blocked_reason()'s own docstring): it names a category of
            # approval, not anything about this particular one's data.
            "blocked_reason": self.blocked_reason(),
            # The row's own title/content line -- can carry real gated data
            # (an event title, a contact name, a document title -- see
            # gate.py's call sites). Only ever shown by a consumer that's
            # allowed to at its own detail level; web_shell.py's notification
            # body never reads it below "detailed".
            "summary": self.summary,
            # Who is asking, as a tiered label (agent_label.AgentLabel.to_dict():
            # tier, headline, claim, icon_id) -- the approval list's live
            # re-render shows it on every row. Never gated content: it names
            # the caller, which the card itself already shows.
            "agent": label_for(self.agent).to_dict(),
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "decided": self.is_finalized(),
        }


@dataclass(frozen=True)
class LedgerHit:
    """consume_ledger()'s return value -- a named record rather than a bare
    tuple, so adding a field can never make a positional consumer silently
    misread one. ``decided_via``/``batch_id`` are audit provenance for a
    decision released through the binder's batch decide endpoint, and
    default to "" -- the ordinary decided-inline-or-via-single-decide
    case's own shape."""

    decision: str
    rule_name: str
    decided_at: float
    decided_via: str = ""
    batch_id: str = ""


class PendingApprovalRegistry:
    """See module docstring. Every public method is safe to call from any
    thread; internal state is protected by one ``threading.Lock`` (cheap,
    dict-sized critical sections only -- never held across a wait)."""

    def __init__(
        self,
        *,
        hold_window: float = DEFAULT_HOLD_WINDOW_SECONDS,
        pending_ttl: float = DEFAULT_PENDING_TTL_SECONDS,
        ledger_ttl: float = DEFAULT_LEDGER_TTL_SECONDS,
        max_pending: int = DEFAULT_MAX_PENDING,
        max_pending_per_principal: int = DEFAULT_MAX_PENDING_PER_PRINCIPAL,
        base_url: str | None = None,
        adaptive_hold: bool = DEFAULT_ADAPTIVE_HOLD,
    ) -> None:
        self.hold_window = hold_window
        self.pending_ttl = pending_ttl
        self.ledger_ttl = ledger_ttl
        self.max_pending = max_pending
        self.max_pending_per_principal = max_pending_per_principal
        self.base_url = base_url
        self.adaptive_hold = adaptive_hold
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}
        # (principal_id, dedupe_key) -> approval id -- see module docstring's
        # own note on why principal_id is folded into this key, not just
        # dedupe_key alone.
        self._by_key: dict[tuple[str, str], str] = {}
        self._created_listeners: list[Callable[[PendingApproval], None]] = []

    def add_created_listener(self, listener: Callable[[PendingApproval], None]) -> None:
        """Call ``listener(approval)`` each time ``register_or_coalesce`` creates a new card (never
        for a coalescing hit, and never for a confirm dialog, which follows a card someone is
        already looking at). Called on the registering thread, after the lock is released, so a
        listener must return quickly and hand any slow work to a thread of its own. An exception
        from a listener is logged and never reaches the gated call. Org mode's web push
        (web_push.PushNotifier.on_new_approval) is the one listener."""
        self._created_listeners.append(listener)

    def _notify_created(self, approval: PendingApproval) -> None:
        for listener in list(self._created_listeners):
            try:
                listener(approval)
            except Exception:
                logger.exception("A new-approval listener failed; the approval itself is unaffected")

    def set_base_url(self, base_url: str | None) -> None:
        self.base_url = base_url

    def approval_url(self, approval_id: str) -> str | None:
        if not self.base_url:
            return None
        return f"{self.base_url}/approvals/{approval_id}"

    def binder_url(self) -> str | None:
        """The list page itself -- what gate.py's
        _pending_result() points Claude at instead of N separate approval_
        url()s once more than one of this principal's approvals is waiting
        at once."""
        if not self.base_url:
            return None
        return f"{self.base_url}/approvals"

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register_or_coalesce(
        self,
        *,
        dedupe_key: str,
        connector: str,
        tool: str,
        gate_kind: str,
        request_id: str,
        summary: str = "",
        tool_name: str = "",
        preview: dict[str, Any] | None = None,
        operation_key: str | None = None,
        review_ctx: Any = None,
        pii_forces_confirmation: bool = False,
        pii_detected: bool = False,
        pii_categories: list[str] | None = None,
        claude_reason: str = "",
        waiting: bool = False,
    ) -> tuple[PendingApproval, bool]:
        """Returns ``(approval, created)``. ``created=False`` means an
        identical, not-yet-finalized approval was already outstanding for
        this exact ``(connector, tool, args)`` and the caller is coalescing
        onto it -- the caller must not show a
        second card, only await the existing one.

        ``waiting=True`` counts the caller as a waiter on the returned
        approval (``PendingApproval.waiters``) inside the same critical
        section that registered or found it, so no second call can slip in
        between registration and the wait. A caller that passes it must call
        release_waiter() once it stops waiting, whatever the outcome.

        Raises IdenticalWriteAwaitingApprovalError for a write (``gate_kind
        == "popup"``) whose identical outstanding approval already has a
        waiter: that waiter collects the decision, and one decision never
        releases two writes (ADR 0073). A write re-issued after its first
        call returned ``approval_pending`` (no waiter left) still coalesces,
        which is how a deferred write collects its decision. Reads coalesce
        however many calls are waiting.

        Raises TooManyPendingApprovalsError if either cap is reached and
        this is a genuinely new key (never raised for a coalescing hit --
        that doesn't grow the pending set for either cap). The per-
        principal cap is checked first, since it's the one meant
        to actually bind day to day; the whole-registry cap is the
        secondary backstop -- see DEFAULT_MAX_PENDING_PER_PRINCIPAL's own
        comment.
        """
        principal_id = current_principal().id
        key = (principal_id, dedupe_key)
        with self._lock:
            self._expire_stale_locked()
            existing_id = self._by_key.get(key)
            if existing_id is not None:
                existing = self._pending.get(existing_id)
                if existing is not None and existing.gate_kind == "popup" and existing.waiters > 0:
                    # Finalized or not: a finalized write with a waiter is
                    # about to be collected by that waiter (mark_collected).
                    raise IdenticalWriteAwaitingApprovalError(
                        "An identical write is already awaiting approval -- wait for that "
                        "call's result instead of issuing it again."
                    )
                if existing is not None and not existing.is_finalized():
                    if waiting:
                        existing.waiters += 1
                    return existing, False
            live_for_principal = sum(
                1 for a in self._pending.values()
                if not a.is_finalized() and a.principal_id == principal_id
            )
            if live_for_principal >= self.max_pending_per_principal:
                raise TooManyPendingApprovalsError(
                    f"Too many approvals are already pending for this principal "
                    f"({self.max_pending_per_principal}) -- decide some or wait for them to "
                    "expire before issuing more."
                )
            live = sum(1 for a in self._pending.values() if not a.is_finalized())
            if live >= self.max_pending:
                raise TooManyPendingApprovalsError(
                    f"Too many approvals are already pending ({self.max_pending}) -- decide some "
                    "or wait for them to expire before issuing more."
                )
            now = time.time()
            approval = PendingApproval(
                id=uuid.uuid4().hex, kind="card", principal_id=principal_id,
                connector=connector, tool=tool, gate_kind=gate_kind,
                request_id=request_id, summary=summary, tool_name=tool_name, dedupe_key=dedupe_key,
                created_at=now, expires_at=now + self.pending_ttl,
                preview=dict(preview or {}),
                operation_key=operation_key, review_ctx=review_ctx,
                pii_forces_confirmation=pii_forces_confirmation, pii_detected=pii_detected,
                pii_categories=list(pii_categories or []), claude_reason=claude_reason,
                agent=current_agent(),
                waiters=1 if waiting else 0,
            )
            self._pending[approval.id] = approval
            self._by_key[key] = approval.id
        self._notify_created(approval)
        return approval, True

    def register_confirm(self, *, sensitive: bool = False) -> PendingApproval:
        """A PII/"Always allow" confirmation dialog -- never coalesced
        (``dedupe_key=None``), never subject to the pending cap (it's a
        short-lived follow-up to a card someone is already looking at, not
        a new gated call), never ledgered.

        ``sensitive`` marks the one
        kind of confirm dialog that sentence is *not* true of: the one an
        MCP meta-tool raises (``gate.propose_policy_change``), where no card came first and confirming is
        the whole of the gate on a change to what auto-accepts in future.
        web/routes_approvals.py's decide route holds those to the same two
        checks web/routes_settings.py already holds its own
        ``_SENSITIVE_ACTIONS`` to -- an attributable session, and a passkey
        where one is required -- rather than to the "a confirm releases
        nothing of its own" reasoning the default records. Left ``False``
        for the PII and "Always allow" dialogs, which really are second
        steps inside a decision their own card already gated."""
        with self._lock:
            now = time.time()
            approval = PendingApproval(
                id=uuid.uuid4().hex, kind="confirm", created_at=now, expires_at=now + self.pending_ttl,
                sensitive=sensitive,
            )
            self._pending[approval.id] = approval
            return approval

    def set_html(self, approval_id: str, html: str) -> None:
        approval = self._pending.get(approval_id)
        if approval is not None:
            approval.html = html

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def answer(
        self, approval_id: str, result: str, chosen_index: int | None = None, *,
        principal_id: str | None = None, decided_via: str = "", batch_id: str = "",
    ) -> bool:
        """Resolve one UI step -- called by web/routes_approvals.py's
        decide endpoint when a human clicks a button. See
        PendingApproval.answer.

        ``principal_id``, when given (web/routes_approvals.py's org-mode
        routes always pass one -- see module docstring), rejects a
        decision on an approval belonging to a *different* principal exactly as if it
        didn't exist -- every decision is authorized against
        ``current_principal()``, defense in depth on top of the
        approval id's own 128 bits of entropy.

        ``decided_via``/``batch_id`` are "" for every ordinary single-decide
        caller -- only answer_batch() below passes real values."""
        with self._lock:
            approval = self._pending.get(approval_id)
        if approval is None:
            return False
        if principal_id is not None and approval.principal_id != principal_id:
            return False
        return approval.answer(result, chosen_index, decided_via=decided_via, batch_id=batch_id)

    def answer_batch(
        self, items: list[tuple[str, str]], *, principal_id: str | None = None,
        decided_via: str = "", batch_id: str = "",
    ) -> list[dict[str, str]]:
        """The approval binder's own batch decide endpoint,
        shared between web/routes_approvals.py's local-mode and org-mode
        routes so neither reimplements this classify-then-
        answer sequence. ``items`` is ``(approval_id, result)`` pairs, each
        ``result`` already validated by the caller to be one of
        BATCH_RESULTS.

        Returns one outcome per item, in submitted order, each
        ``{"id": ..., "outcome": ...}`` where outcome is one of:

        - ``"applied"`` -- this decision was just recorded.
        - ``"already_decided"`` -- the UI step was already answered
          (a rule resolved it, a duplicate id in the same batch, or a
          genuine race with another decide) -- not an error.
        - ``"unknown"`` -- no such approval, *or* it belongs to a
          different principal (``principal_id`` given and mismatched) --
          the two are indistinguishable, per every other read/write here
          (module docstring).
        - ``"not_batchable"`` -- exists, belongs to this principal, but
          isn't a plain batchable card (PendingApproval.is_batchable()) --
          a confirm/choice dialog or a PII-forced card. Never silently
          coerced into "applied" or "unknown".

        Never raises for a bad item -- a batch partially applying is the
        normal case (module docstring's own note), not a failure worth
        aborting the rest of the batch over."""
        reports: list[dict[str, str]] = []
        for approval_id, result in items:
            approval = self.get(approval_id, principal_id=principal_id)
            if approval is None:
                reports.append({"id": approval_id, "outcome": "unknown"})
            elif not approval.is_batchable():
                reports.append({"id": approval_id, "outcome": "not_batchable"})
            elif self.answer(
                approval_id, result, principal_id=principal_id, decided_via=decided_via, batch_id=batch_id,
            ):
                reports.append({"id": approval_id, "outcome": "applied"})
            else:
                reports.append({"id": approval_id, "outcome": "already_decided"})
        return reports

    def finalize(self, approval_id: str, decision: str, rule_name: str = "") -> bool:
        """Resolve the whole approval -- called once, by gate.py's own
        interaction driver, when the full multi-step dance (main popup, any
        PII/rule confirmation) has concluded, and by reevaluate_all() below
        for a rule that appeared while nobody had answered yet. Writes the
        decision ledger (single-use for gate_kind="popup" -- see module
        docstring) and
        wakes wait_async(). Idempotent, same "first decision wins" contract
        as answer()."""
        with self._lock:
            approval = self._pending.get(approval_id)
            if approval is None or approval.is_finalized():
                return False
            approval.final_decision = decision
            approval.final_rule_name = rule_name
            approval.decided_at = time.time()
            if approval.dedupe_key is not None:
                approval.ledger_expires_at = approval.decided_at + self.ledger_ttl
            approval.finalize_event.set()
            # Also wake the UI step: a finalize that didn't go through
            # answer() (pop_expired_events()'s own TTL sweep, or
            # reevaluate_all() below finding a rule that now covers this)
            # otherwise leaves any thread blocked in web_prompt.block_on_card
            # -- gate.py's _run_in_popup_executor worker showing this exact
            # card -- waiting on card.event.wait() forever, since
            # PendingApproval.answer() is otherwise the only thing that sets
            # that event.
            # block_on_card already maps a result outside CARD_RESULTS to
            # "deny", and finalize() is idempotent, so the woken worker's own
            # eventual finalize() call is a harmless no-op: this call's real
            # final_decision (e.g. "expired"/"auto_accepted") stands.
            approval.event.set()
            return True

    def consume_ledger(self, dedupe_key: str) -> LedgerHit | None:
        """A re-issued (or coalesced-and-since-finalized) identical call's
        first stop: is there already a decision on file for this exact
        ``(connector, tool, args)``, *for the calling principal*? Returns a
        LedgerHit or None. Single-use entries
        (writes) are removed on the read that consumes them; read-gate
        entries stay reusable until ``ledger_ttl``: re-reading data a human
        already released discloses nothing new (ADR 0073).

        Scoped to ``current_principal()`` implicitly (module docstring) --
        gate.py's own call site needs no change, and this is the one method
        where that scoping isn't optional: without it, two principals
        issuing the identical tool call with identical arguments would
        share one ledger entry, releasing one principal's approved decision
        to the other's re-issued call (see module docstring)."""
        principal_id = current_principal().id
        key = (principal_id, dedupe_key)
        with self._lock:
            self._expire_stale_locked()
            approval_id = self._by_key.get(key)
            if approval_id is None:
                return None
            approval = self._pending.get(approval_id)
            if approval is None or not approval.is_finalized() or approval.ledger_consumed:
                return None
            if approval.ledger_expires_at is not None and time.time() > approval.ledger_expires_at:
                return None
            if approval.gate_kind == "popup" and approval.waiters > 0:
                # The call still waiting on this write collects it through
                # mark_collected(); handing it out here as well would
                # release one decision twice.
                return None
            approval.ledger_collected = True
            if approval.gate_kind == "popup":
                approval.ledger_consumed = True
                del self._by_key[key]
                self._pending.pop(approval_id, None)
            assert approval.final_decision is not None  # nosec B101  # is_finalized() already proved this
            assert approval.decided_at is not None  # nosec B101  # set alongside final_decision, always together
            return LedgerHit(
                decision=approval.final_decision, rule_name=approval.final_rule_name,
                decided_at=approval.decided_at, decided_via=approval.decided_via, batch_id=approval.batch_id,
            )

    # ------------------------------------------------------------------ #
    # Waiting (gate.py's hold window)
    # ------------------------------------------------------------------ #

    async def wait_async(self, approval: PendingApproval, timeout: float) -> bool:
        """True if ``approval`` was finalized within ``timeout`` seconds,
        else False (still pending -- see gate.py's own use of this)."""
        import asyncio

        return await asyncio.to_thread(approval.finalize_event.wait, timeout)

    def mark_collected(self, approval: PendingApproval) -> None:
        """Record that ``approval``'s outcome reached a caller directly: the
        human decided within the hold window while that caller was still in
        wait_async(). The hold-window counterpart of consume_ledger(), with
        the same effect: the entry is ``ledger_collected`` (so its TTL lapse
        is not audited as "expired"), and a write is also consumed, so an
        identical write afterwards goes back through the gate instead of
        replaying this decision (ADR 0073). A read stays replayable, and
        every other call coalesced onto it still gets the same result.

        Must be called before release_waiter(): while the caller is still
        counted as a waiter, consume_ledger() and register_or_coalesce()
        refuse to hand this write to anyone else."""
        with self._lock:
            if not approval.is_finalized():
                return
            approval.ledger_collected = True
            if approval.gate_kind == "popup" and not approval.ledger_consumed:
                approval.ledger_consumed = True
                self._drop_key_locked(approval)
                self._pending.pop(approval.id, None)

    def release_waiter(self, approval: PendingApproval) -> None:
        """Undo one ``register_or_coalesce(waiting=True)``: the caller has
        stopped waiting on ``approval``, whether it collected an outcome,
        timed out into ``approval_pending`` or was cancelled."""
        with self._lock:
            if approval.waiters > 0:
                approval.waiters -= 1

    def has_other_live(self, principal_id: str, exclude_id: str) -> bool:
        """True if some *other* not-yet-finalized approval already exists
        for ``principal_id`` -- gate.py's adaptive hold window:
        when this is true for a call that just registered, waiting the full
        ``hold_window`` on it buys nothing, since this principal already has
        something else waiting on a decision. Same "not finalized" test
        register_or_coalesce() already uses for the per-principal pending
        cap, just without the exclusion for ``exclude_id`` itself that cap
        check doesn't need (it runs before the new approval exists)."""
        with self._lock:
            return any(
                a.id != exclude_id and not a.is_finalized() and a.principal_id == principal_id
                for a in self._pending.values()
            )

    # ------------------------------------------------------------------ #
    # Read side -- web/routes_approvals.py, privacyfence_await_approval
    # ------------------------------------------------------------------ #

    def get(self, approval_id: str, *, principal_id: str | None = None) -> PendingApproval | None:
        """``principal_id``, when given, makes a mismatched approval
        indistinguishable from a nonexistent one -- the authorization check
        web/routes_approvals.py's card/preview routes and
        web/mcp_dispatch.py's ``privacyfence_await_approval`` rely on.
        ``None`` (local mode, and gate.py's own internal use) means
        "no filter"."""
        with self._lock:
            approval = self._pending.get(approval_id)
        if approval is None:
            return None
        if principal_id is not None and approval.principal_id != principal_id:
            return None
        return approval

    def list_pending(self, principal_id: str | None = None) -> list[PendingApproval]:
        """Every card/confirmation not yet answered at the UI-step level --
        newest first. Used by web/routes_approvals.py's/web/routes_org_
        approvals.py's list view. ``principal_id`` restricts the
        result to that principal's own approvals only -- ``None``
        (local mode's own call) returns every approval regardless of
        principal, correct for local mode's single implicit principal."""
        with self._lock:
            items = [
                a for a in self._pending.values()
                if not a.event.is_set() and (principal_id is None or a.principal_id == principal_id)
            ]
        items.sort(key=lambda a: a.created_at, reverse=True)
        return items

    def await_status(self, approval_id: str, *, principal_id: str | None = None) -> str:
        """One of "approved"/"denied"/"pending"/"expired"/"unknown" -- the
        vocabulary privacyfence_await_approval reports back to Claude:
        status only, never content, since the only path to gated data is the
        original gated call. ``principal_id`` is the cross-principal check
        -- an id belonging to another
        principal reads as "unknown", never leaking that it exists at all."""
        approval = self.get(approval_id, principal_id=principal_id)
        if approval is None:
            return "unknown"
        if not approval.is_finalized():
            return "pending"
        if approval.final_decision == "expired":
            return "expired"
        if approval.final_decision == "deny":
            return "denied"
        return "approved"  # accept | accept_all | auto_accepted

    # ------------------------------------------------------------------ #
    # Rules-changed re-evaluation broadcast
    # ------------------------------------------------------------------ #

    def reevaluate_all(self, should_auto_accept: Callable[[str, Any], tuple[bool, str]]) -> list[PendingApproval]:
        """Called whenever the live rule/grant set changes (see gate.py's
        subscription to auto_accept.add_rules_changed_listener). Any
        not-yet-answered card whose operation is now covered by a rule --
        and whose PII gate isn't independently forcing a human look, per
        pii_forces_confirmation -- is finalized as "auto_accepted" right
        here, without waiting for a human to open it. Returns the list of
        approvals this call resolved, so the caller (gate.py) can audit each
        one and wake anything still awaiting it.

        Scoped to ``current_principal()``: ``should_auto_accept`` is
        itself one principal's own evaluator (auto_accept.py's own
        ``_REGISTRY``, resolved via ``current_principal()`` inside gate.py's
        ``_on_rules_changed`` at the moment *that* principal's rules
        changed), so re-evaluating another principal's pending cards
        against it would apply the wrong rule set entirely -- not merely a
        privacy leak but a correctness bug, the same class this module's
        own dedupe-key fix (see module docstring) exists to close.
        """
        principal_id = current_principal().id
        resolved: list[PendingApproval] = []
        with self._lock:
            candidates = [
                a for a in self._pending.values()
                if a.kind == "card" and not a.is_finalized() and not a.event.is_set()
                and a.operation_key is not None and not a.pii_forces_confirmation
                and a.principal_id == principal_id
            ]
        for approval in candidates:
            try:
                auto_ok, matched_rule = should_auto_accept(approval.operation_key, approval.review_ctx)
            except Exception:
                logger.exception("should_auto_accept raised during rules-changed re-evaluation")
                continue
            if not auto_ok:
                continue
            if self.finalize(approval.id, "auto_accepted", matched_rule):
                resolved.append(approval)
        return resolved

    # ------------------------------------------------------------------ #
    # Expiry -- opportunistic, mirroring mcp_dispatch.McpDispatcher's own
    # _prune_stale pattern (called at the top of every registration/lookup,
    # not on a background timer).
    # ------------------------------------------------------------------ #

    def _drop_key_locked(self, approval: PendingApproval) -> None:
        """Must be called with self._lock held. Removes ``approval``'s
        ``_by_key`` entry, but only while it still points at ``approval``:
        once a key has been freed, a newer approval for the same call may
        own it."""
        if approval.dedupe_key is None:
            return
        key = (approval.principal_id, approval.dedupe_key)
        if self._by_key.get(key) == approval.id:
            del self._by_key[key]

    def _expire_stale_locked(self) -> None:
        """Must be called with self._lock held. Frees dedupe keys whose
        approval expired (pending TTL) or whose ledger entry did (ledger
        TTL) -- so a fresh call for the same key gets a clean new approval
        instead of perpetually finding a dead one. Does NOT delete
        unconsumed, not-yet-expired PendingApproval rows -- pop_expired_
        events()/pop_expired_ledger_events() (below) are what actually
        drain those, since each needs to become exactly one "expired" audit
        entry and this method is called far too often (every registration)
        to be that list's only producer."""
        now = time.time()
        for key, approval_id in list(self._by_key.items()):
            approval = self._pending.get(approval_id)
            if approval is None:
                del self._by_key[key]
                continue
            if not approval.is_finalized() and now > approval.expires_at:
                del self._by_key[key]
            elif approval.is_finalized() and approval.ledger_expires_at is not None and now > approval.ledger_expires_at:
                del self._by_key[key]

    def pop_expired_events(self) -> list[PendingApproval]:
        """Un-finalized approvals whose pending TTL has lapsed -- gate.py
        calls this opportunistically and audits each as "expired" (no
        decision means pending, then expired, which counts as denied --
        never auto-approved). Each is reported at most once (finalized here, as
        "expired", so a later human click on the same stale link is
        rejected the same way any late decision is)."""
        now = time.time()
        expired: list[PendingApproval] = []
        with self._lock:
            for approval in self._pending.values():
                if approval.is_finalized() or now <= approval.expires_at:
                    continue
                approval.final_decision = "expired"
                approval.decided_at = now
                approval.finalize_event.set()
                # See finalize()'s own comment: this sweep resolves the whole
                # approval directly rather than through finalize(), so it has
                # to wake the UI-step event itself too, or a worker thread
                # blocked showing this exact card (web_prompt.block_on_card)
                # never returns.
                approval.event.set()
                if approval.dedupe_key is not None:
                    self._by_key.pop((approval.principal_id, approval.dedupe_key), None)
                expired.append(approval)
        return expired

    def pop_expired_ledger_events(self) -> list[PendingApproval]:
        """Drains every finalized approval whose ledger TTL has lapsed, and
        returns the ones whose outcome no call ever collected -- the
        "decided but nobody came back for it" case. gate.py audits each of
        those as "expired" too: the human's real decision was made, but
        nothing was released on the strength of it, which is exactly what
        "expired" already means for a pending approval that ran out the
        clock.

        An entry whose outcome did reach a caller (``ledger_collected``: a
        replayed read, or any decision delivered within the hold window) is
        removed silently. Its release is already audited as that call's own
        row, and reporting it here would record data that was released as
        "expired" (ADR 0073)."""
        now = time.time()
        events: list[PendingApproval] = []
        with self._lock:
            for approval in list(self._pending.values()):
                if (
                    approval.is_finalized() and not approval.ledger_consumed
                    and approval.final_decision != "expired"
                    # A call still in wait_async() is about to collect it.
                    and approval.waiters == 0
                    and approval.ledger_expires_at is not None and now > approval.ledger_expires_at
                ):
                    approval.ledger_consumed = True
                    self._drop_key_locked(approval)
                    self._pending.pop(approval.id, None)
                    if not approval.ledger_collected:
                        events.append(approval)
        return events
