# ADR 0013: no MCP tool may mint a sign-in credential

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-19 in commit `79868ad1`,
"Retire privacyfence_get_sign_in_link", the self-approval review's Phase 2). Implemented: the tool
is gone, and `privacyfence_status` has never returned a link. The reasoning is kept in
[`docs/security-and-compliance.md`](../security-and-compliance.md)'s "Sign-in links are no longer
issued over MCP" section; this ADR records the decision and the rule that follows from it.

## Context

A bootstrap code for the local web UI turns into a `pf_session` cookie, and that session can
*release* a gated call through the approval decide route, not just request one. ADR 0002's Context
describes the resulting shape: in local mode the agent was the courier for the credential that
authenticates to the UI that checks the agent.

`privacyfence_get_sign_in_link` was a meta-tool over `/mcp` that minted exactly that credential
for the calling MCP client. It was deliberately not gated on a human approval, because that
approval lives behind the very UI a locked-out user is trying to reach. What bounded it instead:
local mode only, a single-use short-lived code, an allowlisted `page`, a loopback-bound UI and a
`sign_in_link_issued` audit entry. Its justification was that a locked-out human had no other way
in: the daemon is headless (P10) and the companion app was optional, with "nothing installs or
starts it automatically yet".

Separately, issue #396's threat-model follow-up had already kept `privacyfence_status` from
minting a link, on the grounds that a credential would be issued because a *model* decided to
check status, not because a human asked.

Two things then changed:

- [ADR 0003](0003-separated-installs-only.md) made the companion mandatory and autostarted on all
  three platforms, so the tool's premise stopped being true anywhere PrivacyFence ships.
- Session provenance (security-and-compliance.md, "A session is not a human") meant a link minted
  on the agent's behalf would be `unattested` and could not approve anything. The tool would hand
  over a credential that no longer did what its description promised, and it would still grant
  read access to the review screen.

## Decision

Nothing reachable over `/mcp` mints a sign-in credential (a bootstrap code, a sign-in link, or a
session) for PrivacyFence's own web UI. `privacyfence_get_sign_in_link` is retired, and no
successor tool under any name may reintroduce it.

- `privacyfence_status` reports setup state only. Its `sign_in_url` is always `null`. For an
  un-onboarded local install `next_step` is `open_privacyfence_companion`, and in org mode it is
  `contact_your_administrator` (`McpDispatcher.status` in `src/privacyfence/web/mcp_dispatch.py`).
- A human gets in through the companion's **Open Approvals**/**Open Settings** items. When the
  companion menu is out of reach, the human runs `privacyfence-app --print-sign-in-link` in their
  own terminal (`daemon_main.run_print_sign_in_link()`). The companion then asks for confirmation
  at the login session before that link counts as `human`.

## Alternatives considered

- **Gate the tool on a human approval.** Rejected because it is circular: the approval is shown in
  the UI the user cannot reach.
- **Keep the tool as a documented, bounded cost.** This was the previous posture. It was dropped
  once both premises above expired. Hardening its bounds further was not pursued, and the sources
  do not record whether it was considered.
- **Let `privacyfence_status` return a link for un-onboarded installs.** Rejected in #396's
  follow-up for the reason given above.

## Consequences

- The model can tell a human *that* setup is needed. It can never hand the human a way in, so the
  onboarding copy (README, the not-authorized page in `web/session_auth.py`) points at the
  companion.
- This removes the *audited, sanctioned* path, not the underlying reachability. A process running
  as the user can still mint a code through the control channel. What makes that insufficient is
  provenance and the passkey (ADR 0002 decision 6: make minting insufficient, not uncallable). To
  cover the gap left by the removed tool, every mint is now audited, whichever channel asks.
- A client without a working companion (a broken tray icon, an SSH session) depends on the human
  knowing about `--print-sign-in-link`.
- Any future proposal for an "ask Claude for a link" convenience has to supersede this ADR.

## Verification

- `tests/unit/web/test_mcp_tools.py`:
  `test_nothing_on_this_server_mints_a_sign_in_link_any_more` asserts that no `META_TOOLS` name
  contains `sign_in`, checked across the whole manifest so that a renamed tool is also caught.
  `test_the_retired_tool_is_not_callable_at_all` calls the old name over a real MCP session.
- `tests/unit/web/test_mcp_dispatch.py` (`TestStatus`): `sign_in_url` is `None`, and `next_step`
  is set per mode.
- `tests/unit/web/test_session_auth.py::test_does_not_send_the_reader_back_to_their_ai_client`.
- `tests/unit/test_daemon_main.py`: the dispatcher has no `get_sign_in_link` attribute.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) covers the companion as the
  sign-in channel (decision 2) and decision 6's "insufficient, not uncallable" layering.
- [ADR 0003](0003-separated-installs-only.md) makes the companion mandatory, which removed the
  tool's justification.
- Issue [#396](https://github.com/privacyfence/privacyfence/issues/396): `privacyfence_status` and
  its threat-model follow-up.
- Issue [#423](https://github.com/privacyfence/privacyfence/issues/423) part 3: the
  not-authorized page's "ask Claude" lead, which this decision replaced.
- Commits: `2f251a2c` (tool added, 2026-09-14), `799145f6` (`--print-sign-in-link`), `79868ad1`
  (tool retired).
