# ADR 0243: Peek provisioning needs a verified email and stops renewing after 90 days

## Status

Accepted — 2026-10-27. Implemented.
Relates to 0241, 0242.

## Context

Provisioning (ADR 0241) identifies a Peek account by email. An unverified address from an identity
provider could adopt someone else's Peek account. Tokens expire, so they must be renewed, which
needs the email without a fresh sign-in. Renewing forever would leave a live token for someone who
left without a release. Peek's account model is described at
<https://github.com/andras-tkcs/peek/blob/main/docs/src/content/docs/auth-access.md>.

## Decision

1. Provisioning needs an email the IdP marks verified (`email_verified` true or `"true"`). Without
   one the status is `unverified_email` (or `no_email` when there is none). An organization whose
   provider issues only verified addresses can opt in with `--peek-trust-unverified-email`.
2. The email is stored in `credentials/peek_provisioning.json` so the sweep can renew without a
   sign-in.
3. Tokens last 90 days and are renewed 30 days ahead. Renewal stops for anyone not seen within 90
   days, so their token lapses at most 90 days later.

See `src/privacyfence/peek_provisioning.py` and `src/privacyfence/org_mode.py`.

## Alternatives considered

- **Trust every IdP email.** An unverified email could adopt someone else's Peek account. It is an
  explicit opt-in instead.
- **Renew forever.** Someone who left without a release would keep a live token.

## Consequences

- Providers that do not report verification need the opt-in flag.
- A stored email sits on disk per provisioned person, readable only by the daemon (0600).
- A person away more than 90 days is provisioned again at their next sign-in.
