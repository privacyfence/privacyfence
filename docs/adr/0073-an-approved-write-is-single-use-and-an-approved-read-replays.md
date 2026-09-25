# ADR 0073: An approved write is single-use; an approved read replays within the ledger TTL

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-08-28 in
`docs/https-connector-refactor-plan.md` §5.4 and decision row D3, read it with
`git show 96cd5af4^:docs/https-connector-refactor-plan.md`, and implemented in `36c5b7ce`,
merged in [#188](https://github.com/privacyfence/privacyfence/pull/188)). Implemented.

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
- **Reads (`gate_kind == "review"`) replay until the ledger TTL.** An identical re-read finds the
  same entry and reuses it until `ledger_expires_at` (`decided_at + ledger_ttl`, default
  `DEFAULT_LEDGER_TTL_SECONDS` = 5 minutes, configurable as `web.approvals.ledger_ttl_seconds`).
  Re-reading data a human has already released discloses nothing new.

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

## Consequences

- Repeating a write needs a second approval, or a standing auto-accept rule that covers it.
- A ledger entry that lapses is removed and audited as `expired` by `gate.py`'s expiry sweep
  (`pop_expired_ledger_events()`), which skips entries marked `ledger_consumed`. Only a write's
  release sets that mark, so a read entry is audited `expired` when its TTL lapses even if it was
  replayed in the meantime.
- The TTL is the only limit on read replay. Raising `ledger_ttl_seconds` widens the replay window
  for reads and does not change writes.

## Verification

- `tests/unit/test_gate.py`'s `test_write_gate_ledger_entry_is_single_use`: an approved
  `gmail_create_draft` releases once from the ledger, and the third identical call returns
  `approval_pending` again.
- `tests/unit/test_approvals.py`'s `test_review_gate_ledger_entry_is_reusable` and
  `test_popup_gate_ledger_entry_is_single_use`.

## Related

- `src/privacyfence/approvals.py` (`consume_ledger`, `finalize`) and `src/privacyfence/gate.py`
  (`_resolve_decision`): the implementation.
- [ADR 0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md): standing rules, the
  sanctioned way to repeat a write without a prompt.
