# ADR 0281: A sweeper deletes the runtime credential of a revoked, expired or undelivered token

## Status

Accepted — 2026-11-13. Implemented.
Relates to [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md) and [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md).
Amended by [ADR 0284](0284-an-expired-tokens-n8n-credential-is-kept-until-the-token-is-revoked.md).

## Context

A delivered token lives in two places. Revoking or expiring it in PrivacyFence must also remove the n8n credential,
and a crash or an n8n error between minting the token and creating the credential must never leave a live token with
no record. Revoke has several code paths with separate stores (the Settings routes, `privacyfence-app
--revoke-scoped-tokens`, `OAuthProvider.revoke_all_for_principal`), and expired tokens stay in the file.

## Decision

- **Records.** `org/agent_runtime_credentials.json` holds one record per delivery (principal, token id, proposal id,
  endpoint, project, credential id and name, workflow id, state, time, boot id) and never a secret. Only the daemon
  writes it. A file that cannot be parsed is never rewritten.
- **Two-phase write.** `runtime_credentials.deliver` writes the record in state `creating` before the token exists,
  creates the token under an id it chose, creates the n8n credential, then moves the record to `delivered` with a
  compare-and-set. If n8n fails it revokes the token first, then marks the record `abandoned` and wakes the sweeper;
  if the revoke fails the record stays and the answer says to revoke in Settings. If the record cannot be written at
  all, nothing is created and the answer is HTTP 500.
- **Sweeper.** `src/privacyfence/runtime_credential_sweeper.py` runs in the daemon whenever the n8n address and key
  are set, whether or not delivery is enabled, every 5 minutes and when woken. It reconciles records against the
  token store with `token_state` (`live`, `expired`, `absent`, `unknown`): a `delivered` record whose token is revoked
  or expired has its credential deleted and the record removed; a `creating` record not in flight is abandoned with a
  compare-and-set and cleaned up; an `abandoned` record has its token revoked, then its credential deleted. A record
  whose token state is unknown (an unreadable token file) is kept. If the n8n address moved, the token is revoked,
  the record is audited as orphaned and removed, and no DELETE goes to the other host.
- The organization revoke routes wake the sweeper so the credential goes at once. The CLI does not touch the records;
  the running daemon deletes the credentials within 5 minutes and the CLI says so.
- Every delivery, deletion and failure is audited (`runtime_credential_delivered`, `_delivery_failed`, `_deleted`,
  `_delete_failed`, `_orphaned`); a failed delete is audited at most once a day per credential.

## Alternatives considered

- **Hooking every revoke path.** The CLI and `revoke_all_for_principal` use separate code paths and fresh stores;
  reconciling against the store covers all of them, expiry included.
- **Keeping the delivery inside the token entry.** Revoke deletes the entry and its credential id with it, so the n8n
  side could never be cleaned up.
- **Writing the record only after the credential exists.** A crash in between would leave a live token without a record.

## Consequences

- A credential outlives its token by up to the sweep interval, holding a token that is already dead in PrivacyFence.
- An ambiguous n8n failure leaves a credential until the sweeper deletes it: hygiene, not exposure.
- A crash before the token exists leaves a record for a token that never existed; the sweeper finds it `absent`.

## Verification

- `tests/unit/test_runtime_credentials.py`: `TestStoreFile`, `TestDeliverFailures`, `TestCrashPoints`.
- `tests/unit/test_runtime_credential_sweeper.py`: `TestRules`, `TestDeleteFailures`, `TestThread`.
- `tests/unit/test_scoped_tokens.py`: `TestChosenTokenId`, `TestTokenState`.
- `tests/unit/test_daemon_main.py`: `TestOrgModeConnectorRegistry` (the sweeper starts only with address and key).

## Related

- Issue: [issue 1090](https://github.com/privacyfence/privacyfence/issues/1090); sources `src/privacyfence/runtime_credentials.py`, `src/privacyfence/runtime_credential_sweeper.py`.
- [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
  [ADR 0165](0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md), [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md).
