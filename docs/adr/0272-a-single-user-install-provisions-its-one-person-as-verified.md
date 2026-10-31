# ADR 0272: A single-user install provisions its one person as verified

## Status

Accepted — 2026-10-31. Implemented: `src/privacyfence/daemon_main.py` (`_start_org_web_server`), `src/privacyfence/web/single_user_auth.py`, `src/privacyfence/oauth_apps.py`.
Relates to [ADR 0243](0243-peek-provisioning-needs-a-verified-email-and-stops-renewing-after-90-days.md), [ADR 0260](0260-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md), [ADR 0271](0271-automatic-accounts-can-be-switched-on-from-the-environment.md).

## Context

Automatic accounts need a verified email ([ADR 0243](0243-peek-provisioning-needs-a-verified-email-and-stops-renewing-after-90-days.md)),
which an identity provider asserts. A single-user install has no identity provider: its one person,
`admin@localhost`, is a fixed principal in code, reachable only with the setup code or a passkey.
Automatic accounts were refused there, so a local stack could not connect Peek and Grist for it.

## Decision

- When the environment turns automatic accounts on ([ADR 0271](0271-automatic-accounts-can-be-switched-on-from-the-environment.md))
  on a single-user install, PrivacyFence queues the principal `single-user` as **verified**
  (`email_verified=True`), at start and at each sign-in.
- **Only against `localhost`.** The startup check requires loopback addresses for Peek, and for Grist
  and its provisioner. No shared server then ever sees two installs' `admin@localhost`, which would
  collide and could adopt someone else's account.
- The address is fixed in code and the install has exactly one person, so verification adds nothing.
  The Peek and Grist install admins must use other addresses, so this account is never refused as
  an admin.
- This ADR is the one place that treats `single-user` as verified; other providers that need the
  same assumption refer here.

## Alternatives considered

- **Provisioning against a shared Peek or Grist.** Rejected: the fixed `single-user` id and
  `admin@localhost` would collide across installs.
- **Requiring the trust-unverified-email checkbox.** Rejected: it has no meaning for an identity that
  never comes from a provider, and the Admin page hides it in single-user mode.

## Consequences

- Every daemon start counts as a sign-in, so the 90-day inactivity rule does not apply in
  single-user mode.
- After a move to an identity provider, the sweep keeps renewing `users/single-user/` for up to 90
  days after the last start; run `--peek-release single-user` and `--grist-release single-user`.
