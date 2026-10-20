# ADR 0206: A scoped token's last use and last denial are kept in their own file

## Status

Accepted — 2026-10-20. Implemented: `ScopedTokenStore.note_use`, `note_denial` and `activity_for`.
Relates to [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md).

## Context

A person who hands a scoped token to a scheduler wants to know whether it is still used and
whether it is being denied, which means a rule is missing. Settings showed neither. Recording it
puts a write on the path of every scoped call, and `scoped_tokens.json` is rewritten by create,
rename, revoke and migration; the store detects outside edits to it by its file stamp.

## Decision

Activity is stored in `scoped_token_activity.json` beside `scoped_tokens.json` in the principal's
`authority/` directory: per token id, `last_used_at`, `last_denied_at` and `last_denied_tool`.
No secret is in it.

- `note_use` and `note_denial` update memory on every call and write the file at most once every
  60 seconds per person, under the store's lock, with `secure_files.atomic_write_json`. A runner
  looping on denied calls costs one write a minute; a crash loses at most a minute of activity.
- The first call after a start loads the file and merges it per token, so one token's write cannot
  drop another's stored values.
- Every allowed scoped call counts as a use, meta-tools included; every refusal counts as a
  denial, including a `check_policy` probe for a tool outside the token's scope.
- A use is recorded from the MCP route through `asyncio.to_thread`. A denial is recorded inline
  from the gate's synchronous deny paths; the throttled write there is accepted. A failure to
  record is logged and never reaches the call.
- Settings shows "Last used", "Never used" and "Last denied (tool)" under each token.
  `privacyfence_status` is unchanged.

## Alternatives considered

- **Usage fields inside `scoped_tokens.json`.** The file is rewritten by other operations;
  writing it on every call would race with them and churn the stamp the store uses to detect
  outside edits.
- **Deriving last use from the audit log.** It would scan a rotated, hash-chained log on every
  Settings render.
- **Writing on every call.** A denied-call loop would write the disk continuously.

## Consequences

- Settings answers "is this token used, and is it being denied" without the audit log.
- A denial and the 60-second write can add a short disk write to the gate's deny path, at most
  once a minute per person.
- Entries for revoked tokens stay in the file; `activity_for` hides them.
- Any test that makes a scoped call must point `paths.data_dir` at a temporary folder, or it
  writes into the source checkout.

## Verification

`tests/unit/test_scoped_tokens.py` (`TestTokenActivity`: throttle, merge, revoked, errors, corrupt
file); `tests/unit/web/test_routes_mcp.py` and `test_routes_mcp_agent_identity.py` (use recorded
for an allowed call only); `tests/unit/test_gate.py` (denial recorded, a failing store does not
change the denial); `tests/unit/web/test_server.py` and `tests/unit/test_settings_window_html.py`
(the page data and the activity line). The file is listed in
`docs/security-and-compliance.md`.

## Related

[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
[ADR 0198](0198-deleting-an-auto-accept-rule-removes-it-from-scoped-tokens.md).
Source plan: `git show 7251c292^:docs/sync-scheduling-support-plan.md`.
