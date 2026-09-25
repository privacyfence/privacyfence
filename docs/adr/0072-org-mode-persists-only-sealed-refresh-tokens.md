# ADR 0072: Org mode persists only refresh tokens, each sealed to its bearer

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-16 in
[#402](https://github.com/privacyfence/privacyfence/issues/402) and implemented in `b6be2407`,
merged in [#437](https://github.com/privacyfence/privacyfence/pull/437)). Implemented.

## Context

Org mode's authorization server (`web/oauth_provider.py`'s `OrgOAuthProvider`, ADR 0011) kept every
token in process memory. A daemon restart therefore sent every connected MCP client back through
`authorize -> IdP redirect -> sign-in -> code exchange`. For a person at a browser this is a small
inconvenience. A scheduled or background tool call has nobody present to complete the redirect,
so for those callers a restart meant an outage.

Issue #402 weighed persisting tokens and sessions. It first closed as "don't persist" on two
premises: persisting would create a new, org-wide category of secret at rest, and encryption at
rest does not help on one host because the key ends up next to the data. The implementation in #437
answered both. Org mode already stores third-party refresh tokens (Google, Atlassian, Apps Script)
per principal on the same disk, and a PrivacyFence session token is worth less than those. The key
problem also goes away if the daemon holds no key at all. The issue was corrected to record that.

## Decision

- **Only refresh tokens are persisted**, in `org_dir()/oauth_refresh.json` via
  `web/sealed_refresh_store.py`'s `SealedRefreshStore`. Authorization codes, pending authorizations
  and access tokens (one-hour TTL, `_ACCESS_TOKEN_TTL_SECONDS`, re-minted by the refresh path) stay
  in memory, and so do browser sessions (`web/org_session.py`'s `OrgSessionStore`), because a
  person who can sign in again is present for those. DCR client registrations were already persisted
  (`oauth_clients.json`) and are unchanged.
- **Each record is sealed to the token that owns it.** It is looked up by `sha256(token)` and
  encrypted with AES-256-GCM under a key derived with HKDF-SHA256 from the refresh token itself
  (`_derive_key`, the same construction `download_staging.py` uses). No key is stored anywhere.
  Only `principal_id` (so "sign out everywhere" can find the records) and `chain_expires_at` (so
  lapsed chains can be pruned) are in the clear. The client binding, claims and issuance are inside
  the ciphertext.
- **Revocation is one path.** Every revocation (logout, `/revoke`, rotation, the chain's 30-day
  absolute lifetime `_REFRESH_TOKEN_ABSOLUTE_LIFETIME_SECONDS`) goes through
  `OrgOAuthProvider._revoke_pair_locked`, which calls `SealedRefreshStore.discard`. The store is
  capped at `_MAX_RECORDS`. Over the cap, the chains closest to expiry are dropped first.

## Alternatives considered

- **Persist nothing** (the original design and #402's first resolution). A restart breaks every
  scheduled or background call until a human signs each client in again.
- **Persist tokens (and sessions) encrypted at rest under a key the daemon holds.** That key has to
  live where the daemon can read it (an environment variable, `org_dir()`, the host keyring), so
  anyone who can read the token file can usually read the key too. It moves the secret without
  protecting it, and it would put every token type on disk when only one needs to survive.

## Consequences

- A restart no longer interrupts clients that hold a live refresh chain. They lose the access token
  and silently refresh.
- A stolen disk image yields hashes and ciphertext that cannot be decrypted without a token that
  was already valid. The store adds no exposure for someone who already holds the token.
- Persistence brings one new correctness risk: a revocation path that skips `_revoke_pair_locked`
  would leave a record that outlives its intended lifetime. New revocation paths must go through it.
- Browser sessions still end at a restart.

## Verification

- `tests/unit/web/test_sealed_refresh_store.py`: nothing but `principal_id` and chain expiry is in
  the clear, a forged token cannot match a record, lapsed chains are pruned, the cap evicts
  closest-to-expiry first, and discard reaches disk.
- `tests/unit/web/test_oauth_provider.py`: `test_a_refresh_token_still_works_after_a_restart`,
  `test_the_access_token_itself_does_not_survive`,
  `test_a_rehydrated_token_is_still_bound_to_its_own_client`,
  `test_the_chain_absolute_lifetime_still_applies_to_a_rehydrated_token`.

## Related

- [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md): the authorization server
  whose state this is. It lists sealed refresh tokens as a consequence, and this ADR records why.
- [#402](https://github.com/privacyfence/privacyfence/issues/402): the trade-off discussion and its
  correction.
