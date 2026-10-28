# ADR 0252: Each MCP session keeps its approved calls in memory so a token can be proposed from them

## Status

Accepted — 2026-10-28. Implemented.

## Context

The usual reason to propose a token is "do again, unattended, what I just approved". Nothing records
a session's approved calls: audit entries carry no arguments, so no rule value (a recipient, a
folder, a channel) could be recovered from them.

## Decision

- `McpDispatcher` keeps, per MCP session, a ledger of `(tool, agent arguments, my_email)` for each
  call that was approved or auto-gated: those that ran with a released request id, and those of an
  `auto`-gate tool. Computed arguments are dropped. Calls made with a scoped token are not recorded.
- It keeps the newest 200 calls per session, and drops the ledger when the session ends. Nothing is
  written to disk.
- `privacyfence_propose_scoped_token` with `from_session=true` turns the ledger into a bundle
  (`bundle_from_calls`): for each tool the narrowest suggested rule with a known list value, merged
  by scope and value with its verbs; an `auto`-gate tool needs no rule; a tool with no usable rule is
  listed as uncovered and shown to the person.

## Alternatives considered

- **Derive the bundle from the audit log.** Rejected: entries carry no arguments.
- **Persist the ledger.** Rejected: it would put raw arguments on disk.

## Consequences

- The memory cost: up to 200 calls' raw arguments, possibly message text, per live session, never on
  disk. They are held only as long as the session lives.
- A session restart, or a daemon restart, loses the ledger; the client can still pass rules and tools.
- The bundle covers only what was approved, never more than the person already allowed.

## Verification

`tests/unit/web/test_mcp_dispatch.py::TestProposeScopedToken` (`test_from_session_builds_from_approved_calls_only`,
`test_from_session_includes_auto_tools`, `test_ledger_drops_computed_args`,
`test_session_ledger_is_dropped_on_end_session`, `test_session_ledger_keeps_the_newest_200`);
`tests/unit/test_scoped_token_proposals.py::TestBundleFromCalls`. Code:
`src/privacyfence/web/mcp_dispatch.py`.

## Related

ADR 0250, [ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md).
