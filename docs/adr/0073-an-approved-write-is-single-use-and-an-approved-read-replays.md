# ADR 0073: An approved write is single-use; an approved read replays within the ledger TTL

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-08-28 in
`docs/https-connector-refactor-plan.md` §5.4 and decision row D3, read it with
`git show 96cd5af4^:docs/https-connector-refactor-plan.md`, and implemented for the ledger path
in `36c5b7ce`, merged in [#188](https://github.com/privacyfence/privacyfence/pull/188)).
The hold-window path, write-only coalescing and the collected-outcome expiry audit below were
decided in https://github.com/privacyfence/privacyfence/issues/739 and are implemented.

## Context

Deferred approval separates the approval from the release. A gated call that nobody decides within
the hold window returns `{"status": "approval_pending", ...}`. The human decides later, and the
agent then re-issues the identical call to collect the outcome. `approvals.PendingApprovalRegistry`
bridges the two with a decision ledger. `finalize()` records the outcome under
`(principal_id, canonical_key(connector, tool, args))`, and `gate.py`'s `_resolve_decision` checks
`consume_ledger()` before it registers a new approval.

Before this, approval and release happened at the same moment. With a ledger, a recorded decision
can be replayed for as long as its entry lives, so the question is how many releases one human
decision is worth.

## Decision

The answer depends on the gate:

- **Writes (`gate_kind == "popup"`) are single-use.** The first `consume_ledger()` that finds the
  entry marks it `ledger_consumed`, removes it from `_by_key` and `_pending`, and returns it. A
  second identical write, even within the TTL, goes back through the gate and gets its own approval.
  One approval never performs two writes.
- **Single-use holds on every path that releases a write.** A decision reaches its caller either
  through the ledger (a re-issued call) or directly, when the human decides within the hold window
  while the original call is still waiting in `wait_async`. The second path consumes the entry
  exactly as `consume_ledger()` does, so a write decided inside the hold window cannot be replayed
  by an identical write afterwards.
- **Identical writes share one approval only while nobody is waiting on it.** A write re-issued
  after it returned `approval_pending` coalesces onto the outstanding approval, which is how a
  deferred write collects its decision. A second identical write that arrives while another call
  is still waiting on that approval is refused, and nothing is released for it. Reads coalesce
  whenever an identical approval is outstanding, however many calls are waiting.
- **Reads (`gate_kind == "review"`) replay until the ledger TTL.** An identical re-read finds the
  same entry and reuses it until `ledger_expires_at` (`decided_at + ledger_ttl`, default
  `DEFAULT_LEDGER_TTL_SECONDS` = 5 minutes, configurable as `web.approvals.ledger_ttl_seconds`).
  Re-reading data a human has already released discloses nothing new.

`expired` in the audit log means a decided outcome that no call ever collected. An entry whose
outcome reached a caller, through the hold window or the ledger (a replayed read included), is
removed silently when its TTL lapses; the release is already on record as that approval's own
row.

In both cases the entry is bound to the principal, connector, tool and canonical arguments. It
cannot be redirected to another call, another argument set or another principal. It replays
whatever was decided, whether that was an approval or a denial.

## Alternatives considered

- **Reuse any decision until the TTL, writes included.** An agent that retries or loops within five
  minutes would send the same email, or make the same edit, a second time on one approval. For a
  read, the same repetition returns data the human has already seen, which is why the two cases are
  treated differently.
- **Make reads single-use too.** Every retry of an identical read (a client reconnecting, an agent
  re-fetching what it just fetched) would prompt the human again for content they have just
  released, which adds friction without protecting anything.
- **Never coalesce writes.** Every re-issue of a deferred write would open a new card, and a
  deferred write could never collect the decision the human already made.
- **Give a concurrent identical write its own card.** The ledger holds one approval per
  `(principal, canonical key)`, and the human would face two cards they cannot tell apart, where
  approving both performs the write twice.
- **Audit a replayed read's lapse under a new decision name.** It adds audit vocabulary that
  records nothing new: the release is already audited when the decision is made.

## Consequences

- Repeating a write needs a second approval, or a standing auto-accept rule that covers it.
- An agent that fires the same write twice in parallel gets one result and one refusal. Retrying
  after the first call returns is unaffected.
- A ledger entry that lapses uncollected is removed and audited as `expired` by `gate.py`'s expiry
  sweep (`pop_expired_ledger_events()`), so an `expired` row always means nothing was released on
  that approval.
- The TTL is the only limit on read replay. Raising `ledger_ttl_seconds` widens the replay window
  for reads and does not change writes.

## Verification

- `tests/unit/test_gate.py`'s `test_write_gate_ledger_entry_is_single_use`: an approved
  `gmail_create_draft` releases once from the ledger, and the third identical call returns
  `approval_pending` again.
- `tests/unit/test_approvals.py`'s `test_review_gate_ledger_entry_is_reusable` and
  `test_popup_gate_ledger_entry_is_single_use`.
- The hold-window path, in `tests/unit/test_gate.py`'s
  `test_write_decided_within_the_hold_window_is_single_use` (an identical write after one decided
  inside the hold window gets a card of its own) and `tests/unit/test_approvals.py`'s
  `test_mark_collected_consumes_a_popup_entry`, `test_mark_collected_leaves_a_review_entry_replayable`
  and `test_a_popup_entry_with_a_waiter_is_not_handed_out_by_the_ledger`.
- Write coalescing, in `tests/unit/test_gate.py`'s
  `test_concurrent_identical_write_is_refused_while_the_first_waits` and
  `test_reissued_write_after_pending_still_collects_its_decision`, and `tests/unit/test_approvals.py`'s
  `TestWriteCoalescingWithWaiters`.
- The expiry audit, in `tests/unit/test_gate.py`'s
  `test_replayed_read_leaves_no_expired_row_after_the_ledger_ttl`,
  `test_write_decided_within_the_hold_window_leaves_no_expired_row`,
  `test_read_decided_within_the_hold_window_leaves_no_expired_row` and
  `test_uncollected_decision_is_still_audited_as_expired`, and `tests/unit/test_approvals.py`'s
  `test_a_replayed_review_entry_is_not_reported_once_the_ledger_ttl_lapses`,
  `test_an_entry_collected_within_the_hold_window_is_not_reported` and
  `test_an_uncollected_popup_entry_is_still_reported`.

## Related

- `src/privacyfence/approvals.py` (`consume_ledger`, `finalize`) and `src/privacyfence/gate.py`
  (`_resolve_decision`): the implementation.
- [ADR 0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md): standing rules, the
  sanctioned way to repeat a write without a prompt.
