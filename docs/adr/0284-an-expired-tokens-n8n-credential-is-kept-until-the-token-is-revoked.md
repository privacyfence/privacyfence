# ADR 0284: An expired token's n8n credential is kept until the token is revoked

## Status

Accepted — 2026-11-14. Implemented.
Amends [ADR 0280](0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md) and [ADR 0281](0281-a-sweeper-deletes-the-credential-of-a-revoked-expired-or-undelivered-token.md).

## Context

ADR 0281 deleted a delivered token's n8n credential at expiry. A workflow whose token had lapsed then needed a new
proposal and a new credential id, even though [ADR 0283](0283-only-a-person-can-extend-a-scoped-token-from-today-capped-by-max-days.md)
lets a person extend the same token.

## Decision

- **The sweeper deletes a delivered credential only when its token is revoked** (state `absent`). An expired token's
  credential and record are kept, so Extend revives the same credential.
- **`privacyfence_runtime_credential_status` answers `expired`** for such a token, with `token_expired_at` and a
  message asking the person to extend it. A revoked token still answers `not_delivered`.

## Alternatives considered

- **Delete the credential at expiry and recreate it on Extend.** The credential id would change, so the workflow
  would still need to be edited.
- **Auto-delete 30 days after expiry.** It adds a second clock and a rule to explain, for a dead secret that is
  already refused.

## Consequences

- A dead secret stays in n8n until the token is extended or revoked. Its requests to `/mcp` keep getting 401.
- A credential the old sweeper already deleted is not recreated, so such a token needs a new delivery.

## Verification

- `tests/unit/test_runtime_credential_sweeper.py`: `TestRules`.
- `tests/unit/test_runtime_credentials.py`: `TestStatusFor`.

## Related

- Issue: [issue 1098](https://github.com/privacyfence/privacyfence/issues/1098); sources `src/privacyfence/runtime_credential_sweeper.py`, `src/privacyfence/runtime_credentials.py`.
- [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md),
  [ADR 0280](0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md),
  [ADR 0281](0281-a-sweeper-deletes-the-credential-of-a-revoked-expired-or-undelivered-token.md),
  [ADR 0283](0283-only-a-person-can-extend-a-scoped-token-from-today-capped-by-max-days.md).
