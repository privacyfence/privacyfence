# ADR 0250: An AI client can propose a scoped token; only a person creates it

## Status

Accepted — 2026-10-28. Implemented.

## Context

A person who has worked something out with Claude, approving calls by hand or through auto-accept,
often wants to turn it into a routine. The routine reaches `/mcp` with a scoped token
([ADR 0220](0220-a-routines-rule-profile-is-a-scoped-token.md)), and building that token by hand in
**Settings → Scoped tokens → New token** is slow for a long recipe.

Before this decision nothing let an AI client help: the only way to make a token was the New token
form, and the only meta-tool that changes policy, `privacyfence_propose_policy_change`, blocks until
a person answers a card. A token is a bearer credential that acts as the person with no approval
card ([ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)),
so a proposal flow must not let a client obtain one, widen one, or learn that one exists.

## Decision

- `privacyfence_propose_scoped_token` takes a name, a lifetime, rules and tools (or `from_session`,
  see 0252) and the two personal-data flags, validates them without creating anything, and returns a
  review link at once. It returns nothing else: not the token, and not whether anyone created one.
- A proposal is held in memory, per principal, for one hour, at most five at a time. It can be used
  once: creating or declining it removes it.
- The person opens the link in **Settings → Scoped tokens**. The review can only narrow the proposal:
  untick a rule, a verb or a tool, shorten the days. A request that widens it in any respect is
  refused (`narrowing_error`). `duplicate` rules never appear in a proposal, because a client never
  sees the person's token ids.
- A personal-data flag the proposal set is shown **unticked**, with a hint that the client asked for
  it. Only the person can tick it.
- Creating is a second step: a dialog says the person is delegating access, and its button works only
  after they type the token's name exactly. The server checks the typed name too.
- Passkey step-up, where it is in force, is bound to the exact request plus the proposal id, so an
  approval for one proposal cannot be replayed for another.
- Creating and delivering reuse the New token path: the clipboard through the companion locally, a
  once-only dialog in organization mode. Declining discards the proposal.
- The tool is never offered to a scoped token, is refused in an unattended session (audited as
  `denied_unattended`), and on a local install only the computer's owner can use it.
- Every step is audited: `scoped_token_proposed`, `scoped_token_proposal_accepted`,
  `scoped_token_proposal_declined`, `scoped_token_proposal_expired`. None counts as an approval.

## Alternatives considered

- **Block until the person finishes, like `privacyfence_propose_policy_change`.** Rejected: a review
  with a typed name and a passkey outruns MCP client tool-call timeouts, and Claude could not set up
  the routine's environment with the token anyway.
- **A status or await tool.** Rejected: Claude has no use for the outcome, and reporting it would tell
  a client whether its delegation was accepted. For the same reason the proposal-time dry run skips the
  duplicate-label and 50-token checks, so a re-proposed name never reveals that a token exists.
- **Return the secret to the client.** Rejected: it would sit in the chat transcript
  ([ADR 0104](0104-the-companion-copies-the-ai-client-token.md),
  ADR 0164).
- **Let the review edit freely.** Rejected: it would turn the review into a second New token form
  filled in by an AI client. Widening stays on the ordinary form.
- **Pre-tick the personal-data boxes.** Rejected: it would make the AI's request the default.
- **Step-up alone as the confirmation.** Rejected: step-up is optional per install. The typed name
  makes the confirmation deliberate on every install and stops a reflex click. An agent driving a
  browser session is bounded only by the human-session check and step-up, as for New token.
- **A confirm dialog in the approvals list.** Rejected: the decide route carries only `result`,
  `choice` and `note`, its step-up fingerprint covers none of the form, and confirms are swept only on
  a gated call.
- **`duplicate` rules in a proposal.** Rejected: see Decision.

## Consequences

- "The client never receives the token" is a property of the protocol, not of a setting. A local
  agent with a shell can still read the clipboard, the exposure ADR 0104 already accepts.
- A person can turn a session into a routine's token in a few clicks, and cannot be talked into a
  wider one than the client asked for.
- A proposal does not survive a restart, and an hour is all a person has to open the link.
- A flood of proposals is bounded at five per principal.

## Verification

`tests/unit/test_scoped_token_proposals.py` (`TestRegistry`, `TestNarrowing`, `TestBundleFromCalls`,
`TestReviewData`, `TestRecord`); `tests/unit/web/test_mcp_dispatch.py::TestProposeScopedToken`;
`tests/unit/web/test_routes_mcp.py::TestProposeScopedTokenOverTheWire`;
`tests/unit/web/test_routes_settings.py::TestScopedTokenProposalRoutes` and
`tests/unit/web/test_routes_org_settings.py::TestScopedTokenProposalRoutes`;
`tests/unit/test_settings_window_html.py::TestScopedTokenProposal`;
`tests/integration/test_settings_scoped_token_proposal_browser.py`. Code:
`src/privacyfence/scoped_token_proposals.py`, `src/privacyfence/web/mcp_dispatch.py`
(`propose_scoped_token`), `src/privacyfence/web/routes_settings.py`.

## Related

[ADR 0013](0013-no-mcp-tool-mints-a-sign-in-credential.md) (no MCP tool mints a sign-in credential)
still holds: the tool mints nothing, and only a signed-in person in Settings creates the token.
ADR 0104, ADR 0164, [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md),
[ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md), [ADR 0195](0195-importing-personal-settings-is-merge-only-previewed-and-step-up-gated-on-the-file-hash.md), ADR 0220,
[ADR 0236](0236-a-scoped-token-holds-its-own-rules.md); 0251, 0252, 0253.
