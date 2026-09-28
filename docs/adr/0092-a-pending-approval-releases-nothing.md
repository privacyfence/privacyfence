# ADR 0092: a pending approval releases nothing, and one request_id ties its audit trail together

## Status

Accepted — 2026-09-28, decided by the maintainer.
Amends [ADR 0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md) for what a
call does while its approval is pending and for the audit rows of a decision that is collected
later. What the decision ledger replays, and for how long, is unchanged.

## Context

When a human does not decide within the hold window, `gate.gated_call()` hands the agent an
`{"status": "approval_pending", ...}` result, and the agent re-issues the identical call once the
human decides. Until now `gated_call` *returned* that result. Connector methods that end in
`return await gated_call(...)` passed it through. But about 55 connector methods call
`await gated_call(...)` only for the gate, discard the result, and then act: fetch and deliver a
file, create a calendar event, send a draft. For those, a pending approval did not stop anything.
The call went ahead with no decision and was audited only as `approval_pending`.

5.0.0a1/a2 manual QA surfaced it as an audit puzzle: a Claude Code `drive_download_file` over `/mcp`
(no `.mcpb` shim, so the file leaves through a one-time link, ADR 0028) was logged
`approval_pending`, `bridge_download_served`, `expired`, and no `approved`. The file was handed out
by the call that went pending. Nothing in the log tied the served file to that call, and the
`expired` row did not say whether anyone had decided.

## Decision

- **`gated_call` raises `approvals.ApprovalPending` on pending, never returns it.** The exception
  carries the pending result. `web/mcp_dispatch.py`'s `McpDispatcher.call()` is the one place that
  turns it back into the tool result, and it never caches it. A connector that awaits `gated_call`
  only for the gate now stops there by construction; no connector call site changes. The exception
  is a plain `Exception`, not a `RuntimeError`, so an `except RuntimeError` around a connector's
  own client calls cannot swallow it.
- **A decision is audited under the request_id of the call that created its approval.** A ledger
  hit (the approved re-issue) and a coalesced wait record `approved`/`rejected`/... with the
  `request_id` of the `approval_pending` row, as the audit log's own field docs already promised.
  `LedgerHit` carries it.
- **A staged file is audited under the request that released it.** An approved-like row sets the
  released request_id for the rest of that tool call (`audit_log.released_request_scope`, entered by
  the dispatcher around every connector call). `download_staging.stage()` records it and the calling
  agent, and `bridge_download_served`/`staged_download_served` are written under that request_id and
  agent. The fetch itself carries neither: a capability link has no bearer.
- **An uncollected decision says what it was.** The `expired` row for a decision nobody collected
  sets `decided_at` and a new `expired_decision` field (`approved`, `rejected`, ...), so it reads
  differently from a card nobody answered. Audit schema 7. The Excel export gains `Request ID` and
  `Expired Decision` columns.

## Consequences

- A pending call now does exactly what the protocol always described: nothing, until the human
  decides and the identical call is re-issued. For a client without the shim, the download link
  comes from the re-issued call, not the first one.
- The trail for an approved staged-link download reads `approval_pending` → `approved` →
  `bridge_download_served`, all with one `request_id`, and the approval is collected, so it never
  shows up as `expired`.
- A read that replays from the ledger more than once writes several rows with the same
  `request_id`. `event_id` stays unique per line.

## Rejected

- **Audit-only: record the silent release as "approved earlier".** The release never consulted any
  approval. The audit would have claimed a decision that was never made.
- **Fix each discarding call site to check the result.** About 55 sites across ten connectors, and
  the next new one repeats the bug. An exception makes the safe behavior the default.
- **Stop discarding by returning `filtered_data` from every write site.** The same per-site fix
  with the same failure mode.
- **A `BaseException` subclass**, so no `except Exception` could catch it. No connector wraps
  `gated_call` in a broad handler, and a `BaseException` would also slip past the dispatcher's and
  gate's own cleanup paths that expect ordinary exceptions.

## Verification

- `src/privacyfence/approvals.py` (`ApprovalPending`, `LedgerHit.request_id`),
  `src/privacyfence/gate.py` (`gated_call`, `_resolve_decision`, `_pop_registry_expirations`,
  `_audit`), `src/privacyfence/web/mcp_dispatch.py` (`McpDispatcher.call`),
  `src/privacyfence/download_staging.py` (`StagedDownload`, `claim_staged`),
  `src/privacyfence/web/routes_file_bridge.py`, `src/privacyfence/web/routes_downloads.py`,
  `src/privacyfence/audit_log.py` (`expired_decision`, `released_request_scope`).
- `tests/integration/test_pending_approval_releases_nothing.py` drives the real local-mode `/mcp`
  stack with a client without the shim: a pending download hands out nothing, a pending
  `calendar_create_event` never reaches the provider, an approved re-issue is audited under one
  request_id through to the served file, and an uncollected approval expires saying it was approved.
