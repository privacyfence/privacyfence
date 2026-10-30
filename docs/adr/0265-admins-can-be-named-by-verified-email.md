# ADR 0265: Admins can be named by verified email

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/org_identity.py`.
Relates to [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md).

## Context

An administrator was an identity-provider user with a group claim. A server started from environment
variables ([ADR 0264](0264-an-organization-server-can-start-from-environment-variables.md)) with no
group-claim setup, or a small team on Google Workspace, needs a way to name the first administrator
without configuring groups.

## Decision

- **`PRIVACYFENCE_ADMIN_EMAILS`** (or `idp.admin_emails` in an upgraded server's legacy bundle) lists
  administrator addresses, alongside the group claim. A person is an administrator when the group
  rule matches, or when the ID token's `email` is in the list **and** `email_verified` is exactly
  `true`.
- **Matching is ASCII only:** strip and lower-case, and an address with any non-ASCII character never
  matches, on either side. `casefold` is not used, because it maps `ſ` to `s`.
- **Google needs a Workspace domain.** For issuer `https://accounts.google.com`, an email
  administrator's `hd` claim must equal the email's domain; a personal account registered with a work
  address has no `hd`.
- **Allowed domains trust only a verified address.** `check_authz_policy` accepts an email domain only
  when `email_verified` is `true` (and for Google the same `hd` rule). An identity provider that does
  not send `email_verified`, such as Microsoft Entra ID, sets `trust_unverified_email`
  (`PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL`), which lifts this requirement for allowed domains only.
  It never applies to administrators named by email, who use the group claim instead on such a
  provider.
- **Reserved principal ids.** An IdP `sub` equal to `local` or `single-user` (compared
  case-insensitively), or starting with `os-`, is mapped to `idp-` plus the first 32 hex digits of its
  SHA-256, so nobody signing in through an IdP lands in a single-user or per-OS-user data folder.

## Alternatives considered

- **The group claim only.** Rejected: it excludes providers that send no groups, and forces a small
  team to configure group claims first.
- **Any email claim without `email_verified`.** Rejected: on a multi-tenant provider an unverified
  address proves nothing, and anyone could claim an administrator's address.
- **The first person to sign in becomes administrator.** Rejected: whoever finds a new server first
  would own it.

## Consequences

- **Breaking:** allowed domains previously trusted the address as given. An organization on Entra ID
  sets `trust_unverified_email` to keep them working.
- A multi-tenant issuer is best paired with allowed domains or an Internal consent screen.
- Adding an IdP to a data directory that holds `users/single-user/` logs that this data is not
  reachable by IdP users.

## Verification

`tests/unit/test_org_identity.py`, `tests/unit/web/test_routes_org_identity.py`. Code:
`src/privacyfence/org_identity.py` (`principal_from_claims`, `check_authz_policy`,
`_normalized_email`).

## Related

ADR 0011, 0264.
