# ADR 0283: Only a person can extend a scoped token, from today and capped by max_days

## Status

Accepted — 2026-11-14. Implemented.
Amends [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md) and [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md).

## Context

A scoped token's expiry was fixed at creation. A token that had lapsed could only be replaced by a new token with a
new secret, which every client using it then had to be given again.

## Decision

- **Extend on every scoped token, in both modes.** The new expiry is today plus the chosen number of days, 1 to
  `max_days`, and never earlier than the current expiry. The secret, rules and tools stay as they are.
- **Same gates as creating a token:** a signed-in person, CSRF, origin check, confirmation, and the passkey step-up
  where it is required.
- **No AI-client or MCP path.** Only a person, in Settings, can extend a token.
- **`max_days` caps each extension, not the total life of a token.** The Admin label says so.

## Alternatives considered

- **Adding the days to the old expiry.** An already-lapsed token would stay expired, and a long-lived token could be
  pushed far past `max_days` from today.
- **Extending only delivered tokens.** A rule that depends on how a token reached its client is hard to explain, and
  the other tokens have the same problem.
- **An always-passkey gate.** Extending grants no new access, so it is held to the gate of creating a token and no
  stricter.
- **Minting a new secret on extend.** That is what creating a new token already does, and it is the cost this ADR
  removes.

## Consequences

- Repeated extends keep one secret alive with no rotation; revoking is the way to end it.
- `privacyfence-app --revoke-scoped-tokens` writes the token file from another process, so an extend landing in the
  same instant can undo a revoke (the window already exists for rename and create). If a token reappears, run the
  revoke again.

## Verification

- `tests/unit/test_scoped_tokens.py`: `TestStore`.
- `tests/unit/web/test_routes_settings.py`: `TestScopedTokenRoutes`.
- `tests/unit/web/test_routes_org_settings.py`: `test_extend`.
- `tests/unit/test_settings_window_html.py`: `test_a_token_can_be_extended`.

## Related

- Issue: [issue 1098](https://github.com/privacyfence/privacyfence/issues/1098); sources `src/privacyfence/scoped_tokens.py`, `src/privacyfence/web/routes_settings.py`.
- [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
  [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md).
