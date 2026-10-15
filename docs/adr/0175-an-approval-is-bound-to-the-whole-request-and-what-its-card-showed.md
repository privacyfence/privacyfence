# ADR 0175: An approval is bound to the whole request and to what its card showed

## Status

Accepted — 2026-10-14. Implemented.
Amends ADR 0073. The ledger and coalescing key is no longer the connector's policy args alone.

## Context

ADR 0073 keys the decision ledger and the coalescing of pending cards by `(connector, tool,
canonical(args))`, where `args` is the dict a connector hands the gate for rule matching. Connectors
leave the written content out of it: a Slack message's `text`, a Gmail body, cc and bcc, a Drive or
Calendar write's fields, and the `limit` and `cursor` of a read. So one approved Slack message could
be redeemed for a different message, and an approved read could be replayed for a wider page of
results with no personal-data check, because a ledger hit returns the stored decision without
running the PII gate again. A scan of the connectors found 41 of 93 approval-card sites with
effect-bearing parameters missing from `args`.

## Decision

- **The key is a hash of three things** (`approvals.binding_key`): the agent's whole tool-call
  arguments minus `reason`, the connector's policy args, and a digest of everything the card shows
  and the PII scan read (`approvals.card_digest`). A difference in any of them misses the ledger and
  does not coalesce; it registers a new card.
- **The whole request is captured by the dispatcher.** `McpDispatcher.call` wraps the connector call
  in `gate.request_args_scope(args)`, which stores the canonical JSON of the arguments once, before
  the connector runs, so a connector that mutates its dict cannot change what was approved.
  `gate.gated_call` reads it with `current_request_args()`; outside the dispatcher (a direct call in
  a test or script) it is `None`.
- **The card digest** covers the gate, summary, sender, details text, a hash of the scanned text,
  preview, new-info, visibility, tables, blocks, the table-only flag, preview MIME type and delivery.
  The PDF and preview bytes are folded in by their own hash, and only for a write (gate `popup`): a
  read's thumbnail is fetched best-effort and can differ between identical calls, while the read's
  content is already in the details and scan text. It hashes the `details_text` argument, never the
  derived `details`, whose fallback is a `str()`-style dump of an arbitrary object.
- **Rule matching is unchanged.** `ReviewContext.args` is still the connector's policy args; large
  content does not belong there. The dispatcher's own retry dedupe is untouched.

## Alternatives considered

- **Add the missing parameters to each connector's `args`.** It needs every future tool to get it
  right, and `args` doubles as the rule-matching context. The structural key covers every tool at
  once.
- **Key on `raw_data` / `filtered_data`.** They are arbitrary objects whose `str()` can carry memory
  addresses, so the key would differ between identical calls and a deferred re-issue would never
  find its decision.
- **Re-check the PII gate only when a read is replayed from the ledger.** Unnecessary once changed
  content cannot hit the ledger at all; the user chose a new card on any change.

## Consequences

- An approved write releases exactly the request that was shown. A different argument, however
  small, gets a new card.
- A re-issued read whose content changed while its card was pending (a new message in a busy channel,
  live Peek stats, an inbox search) gets a new card instead of collecting the old decision. A
  deferred read of live content, such as a busy Slack channel or Peek stats, can meet a new card on
  each re-issue; deciding within the hold window avoids it.
- An agent that spells out an optional argument it left out before (`"limit": 50`) changes the
  request and gets a new card.
- A new tool is covered with no per-tool work.
- A Drive upload from a local path whose card shows only the name and size is bound to the path, not
  to the file's bytes; swapping the file between approval and re-issue is not detected.

## Verification

- `approvals.binding_key`, `approvals.card_digest`, `gate.gated_call`, `gate.request_args_scope`,
  `web/mcp_dispatch.py` (`McpDispatcher.call`).
- `tests/unit/test_gate.py::TestApprovalBinding`.
- `tests/unit/test_systemic_gate_invariants.py::TestApprovalBindingCoversTheWholeRequest` (every
  parameter of every gated tool changes the key; the dispatcher and the gate use the scope and the
  key).

## Related

- ADR [0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md)
