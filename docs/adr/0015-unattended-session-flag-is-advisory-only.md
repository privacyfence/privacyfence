# ADR 0015: the self-declared unattended-session flag is advisory and never authorizes

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-07-13 in
`docs/cowork-scheduled-tasks-design.md`, added in `f1c2e202`, implemented in `179891d6` on
2026-07-14, and deleted in `82607602` on 2026-07-15. Read it with
`git show 82607602^:docs/cowork-scheduled-tasks-design.md`, sections "Part 3" and "Alternatives
considered"). Implemented. The mechanism is described in
[`docs/TECHNICAL_REFERENCE.md`](https://github.com/privacyfence/privacyfence/blob/5deef1d8/docs/TECHNICAL_REFERENCE.md)'s "Scheduled / unattended Cowork tasks"
section. That section covers the mechanism but not the rejected alternatives recorded here.

## Context

Claude Cowork can run scheduled tasks (Routines) with nobody watching. When such a run reaches a
`review` or `popup` gate that no auto-accept rule covers, the call waited for a human who was not
there. In the design doc's day that call held the shared `_popup_lock` indefinitely, blocking
unrelated interactive approvals queued behind it. The task never finished cleanly.

Whether a run is scheduled or interactive is known only on Claude's side. As the design doc put
it, the transport tells the daemon nothing about *why* the client connected.

## Decision

The client declares an unattended run itself, with `privacyfence_begin_unattended_session`. The
flag it sets is **advisory**: it never changes what is authorized, only what happens when nothing
authorizes a call.

- With the flag set, a call that no auto-accept rule covers is denied immediately instead of
  prompting. The same applies to a call that a rule covers but the PII gate still routes to a
  human. The denial is audited as `denied_unattended`, kept separate from `rejected` so that "no
  human was asked" is never confused with "a human said no". It never produces an
  `approval_pending` result either.
- Every call that auto-accepts without the flag still auto-accepts with it.
- Config-changing meta-tools (`privacyfence_propose_policy_change` and the deprecated
  `privacyfence_propose_auto_accept_rule_change`) are refused outright in an unattended session,
  since a config change always needs a human confirmation.
- The flag is scoped to one MCP session (`McpDispatcher._unattended_sessions`). It is carried into
  the gate through a `contextvars` flag (`gate.unattended_scope` / `gate.is_unattended()`) and
  cleared by `privacyfence_end_unattended_session` or when the session ends.
- The tool works only when an administrator has opted in with `unattended_sessions.enabled` in
  `org_config.json`. It is off by default, and otherwise it errors.

## Alternatives considered

- **Detect "scheduled" from the transport instead of relying on self-declaration.** Rejected
  because the daemon receives no signal about why the client started. The design doc accepted
  the trade-off explicitly: Claude could forget to call the tool, or call it in an interactive
  session. That is acceptable only because the flag never controls authorization, so misuse
  degrades UX (a fast denial where a prompt was wanted, or a hang where fail-fast was wanted), not
  security.
- **Queue approvals for a human to resolve asynchronously, after the task ran.** Rejected because
  the result of a scheduled task would then depend on a decision made after the task had finished,
  "which doesn't compose with 'the task's Cowork run reports a result now.'" The design doc left it
  open as a separate future design. The deferred `approval_pending`/`privacyfence_await_approval`
  flow that exists today serves interactive sessions. `gate.gated_call` still denies an unattended
  session before registering any pending approval.

## Consequences

- The flag cannot be used to escalate. The worst a wrong or malicious declaration can do is deny
  the caller's own calls, or fail to stop a hang.
- A scheduled task gets an immediate, actionable error. It can skip the step and report it, and
  can plan ahead with `privacyfence_check_policy` instead of discovering the gate at run time.
- Admins opt in per organization, so unattended mode is a deliberate deployment choice. The opt-in
  moved from `settings.yaml` to `org_config.json` in `78ccdd1b` (2026-07-21).
- The design doc also decided on a live menu-bar indicator of active unattended sessions. That
  surface went away with the menu bar (P10). `SettingsController._on_unattended_changed` still
  receives the count, but no page shows it today, so an active unattended session is visible only
  through the audit log (`unattended_session_started`/`_ended`, `denied_unattended`).
- The design doc's open questions (a wall-clock TTL on the flag, and per-connector scoping of the
  opt-in) were not decided by it and are not decided here.

## Verification

- `src/privacyfence/gate.py`: `is_unattended()`, `unattended_scope`, `_deny_unattended()`, the
  `is_unattended()` check in `gated_call` placed before any pending-approval registration, and the
  refusals in the propose-change paths.
- `src/privacyfence/web/mcp_dispatch.py`: `begin_unattended_session` (errors unless enabled),
  `end_unattended_session`, `end_session`.
- `tests/unit/test_gate.py`: `test_matching_rule_still_auto_accepts_silently_even_when_unattended`,
  `test_rule_matched_but_pii_detected_still_denies_unattended`,
  `test_review_gate_denies_without_popup_when_unattended`,
  `test_not_unattended_still_shows_popup_as_before`.
- `tests/unit/web/test_mcp_dispatch.py`: per-session scoping
  (`test_a_different_sessions_unattended_flag_does_not_leak`) and clearing on session end.

## Related

- `git show 82607602^:docs/cowork-scheduled-tasks-design.md`, the source design.
- Commits `179891d6` (implementation) and `78ccdd1b` (opt-in moved to `org_config.json`).
- [ADR 0005](0005-moving-the-approval-decision-off-the-device.md) concerns where an approval is
  decided. It does not cover unattended runs.
