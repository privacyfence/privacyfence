# ADR 0275: The single-user person is admin@privacyfence.localhost

## Status

Accepted — 2026-10-31. Implemented: `src/privacyfence/web/single_user_auth.py` (`SINGLE_USER_PRINCIPAL`).
Amends [ADR 0272](0272-a-single-user-install-provisions-its-one-person-as-verified.md).

## Context

A single-user install (organization mode, no identity provider) has one person, whose address
[ADR 0272](0272-a-single-user-install-provisions-its-one-person-as-verified.md) fixed as
`admin@localhost`. Grist's access API (`PATCH /api/orgs/{site}/access`) refuses any address whose
domain has no dot: grist-core `app/gen-server/lib/homedb/UsersManager.ts` calls `isEmail()`, whose
regex `EMAIL_RE` is in `app/common/gutil.ts`. So grist-provisioner's team-site add for
`admin@localhost` failed (Grist 400, provisioner 502 `could not add the account to the Grist team
site`), and a local stack with `PRIVACYFENCE_GRIST_AUTO_PROVISION=true` never connected Grist.

## Decision

The single-user person's address is `admin@privacyfence.localhost`. The principal's id stays
`single-user` and its display name stays `Administrator`. The address is the one constant
`SINGLE_USER_PRINCIPAL` in `src/privacyfence/web/single_user_auth.py`.

`.localhost` is reserved by RFC 6761, so the address can never belong to a real organization or
receive mail; `privacyfence.` gives it the dotted domain Grist requires; it still reads as "this
computer".

## Alternatives considered

- **`admin@localhost.localdomain`** — `localdomain` is a convention, not a reserved name.
- **`.test`, `.invalid` or `example.*` addresses** — they read as test data in the UI.
- **A `PRIVACYFENCE_SINGLE_USER_EMAIL` variable** — ADR 0272 relies on a fixed address in code, and a
  choice only adds ways to collide with a real account.
- **Patching Grist** — the grist-core fork carries no patches by design.

## Consequences

- No migration is needed, because nothing is keyed by the email: passkeys are keyed by principal id,
  and provisioning state lives under `users/single-user/`, with `email` rewritten at every sign-in.
- A passkey made on 7.0.0a1 keeps working and keeps its old label (`admin@localhost`) in the
  authenticator.
- Scoped tokens and MCP OAuth refresh tokens issued before the change show the old address (whoami,
  audit sender, approval labels) until they expire, at most 30 days for refresh chains.
- A development stack that already linked Peek or Grist to `admin@localhost` gets 409 (the external id
  is linked to another email) until `privacyfence-app --peek-release single-user` and
  `--grist-release single-user` are run, or fresh Peek and Grist volumes are used.
- Peek and Grist install admins must still use another address (ADR 0272).
- A local stack's grist-provisioner needs `PROVISIONER_ALLOWED_DOMAINS=privacyfence.localhost`.

## Verification

`tests/unit/web/test_single_user_auth.py::TestTheAddress::test_single_user_email_passes_grist_email_check`.

## Related

[ADR 0272](0272-a-single-user-install-provisions-its-one-person-as-verified.md),
[ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md) (the OIDC `email`
claim), and grist-core `app/common/gutil.ts` `EMAIL_RE`.
