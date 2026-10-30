# ADR 0253: AI token proposals are off in organization mode until the administrator turns them on

## Status

Accepted — 2026-10-28. Implemented.
Amended by [0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md): the AI token proposals switch is on the Admin page (`scoped_tokens.allow_ai_proposals` in `org/admin_config.json`).

## Context

A token proposal (0250) is a new way for an AI client to start the minting of a credential. Scoped
tokens in organization mode are already an administrator opt-in
([ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md)). Locally the only person
is the computer's owner, who already has the New token form.

## Decision

- `ScopedTokenPolicy.allow_ai_proposals` is true locally, and in organization mode only when the
  bundle sets `scoped_tokens.allow_ai_proposals` to `true` **and** scoped tokens are enabled. A
  non-boolean value is ignored with a warning and counts as false.
- `privacyfence-build-org-bundle` has `--enable-scoped-token-proposals` /
  `--disable-scoped-token-proposals`.
- Where it is off, the tool is not listed and a call is refused with a message naming the
  administrator; the org review routes answer 403 or redirect to `/settings`.

## Alternatives considered

- **On by default in organization mode.** Rejected: this is a new way to mint credentials, so the
  administrator opts in.

## Consequences

- An organization that enabled scoped tokens before this change sees no new tool until it rebuilds
  its bundle.
- Turning scoped tokens off turns proposals off with them.

## Verification

`tests/unit/test_scoped_tokens.py` (`test_org_policy_needs_allow_ai_proposals_true`,
`test_org_policy_ignores_allow_ai_proposals_without_enabled`,
`test_org_policy_warns_on_non_bool_allow_ai_proposals`);
`tests/unit/test_build_org_bundle.py` (`test_enable_and_disable_scoped_token_proposals_flags`,
`test_merge_keeps_allow_ai_proposals`); `tests/unit/web/test_routes_org_settings.py::TestScopedTokenProposalRoutes`.
Code: `src/privacyfence/scoped_tokens.py` (`policy_from_org_config`), `scripts/build_org_bundle.py`.

## Related

ADR 0165, 0250.
