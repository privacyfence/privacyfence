# ADR 0267: An organization server may opt in to the built-in Google sign-in

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/org_mode.py`, `src/privacyfence/org_identity.py`, `src/privacyfence/relay_client.py`, `cloudflare/oauth-relay/`.
Relates to [ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md), [ADR 0263](0263-the-6-to-7-upgrade-is-reversible.md) and [ADR 0264](0264-an-organization-server-can-start-from-environment-variables.md).

## Context

Even with built-in connector apps, a multi-user server needs an OIDC app registered at an identity
provider before its first sign-in. For a small Google Workspace team that is the last step standing
between DNS and a working server.

## Decision

- **Opt-in only.** `PRIVACYFENCE_IDP=builtin-google` (or a legacy bundle's `"idp": {"builtin":
  "google"}`) uses PrivacyFence's own `google_signin` client
  ([ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md)) as the IdP. Nothing turns it
  on by default, and an IdP of the organization's own, from the bundle or the environment, always
  wins. The client is a separate Google project from the connector apps, with scopes `openid email
  profile` only, so a suspension of one cannot take the other down.
- **Refusals at start:** combined with any `PRIVACYFENCE_IDP_*` setting; without
  `PRIVACYFENCE_ADMIN_EMAILS`; in a build without the built-in app; or with an `http` or loopback
  server address.
- **Default deny.** Any Google account can finish Google's side, so the server decides alone: only
  administrators named by email ([ADR 0265](0265-admins-can-be-named-by-verified-email.md)) and people
  whose verified address is in an allowed domain (Admin → Sign-in, seeded once by
  `PRIVACYFENCE_ALLOWED_DOMAINS`), with a matching `hd`. Everyone else is refused even when the
  allowed list is empty. `trust_unverified_email` cannot be set. Personal Gmail accounts and groups
  are not supported with it.
- **Through the relay.** The IdP redirects use three relay grants, `idp`, `idp-login` and `stepup`,
  with fixed forward paths. Before the server's registration succeeds, `/login` says it is registering
  and does not send the person to Google.
- **Each sign-in domain is proven in DNS.** The domains of the administrator addresses and the
  allowed domains are sent with the registration (at most 20; a bare public suffix, and `gmail.com`,
  are refused). Each needs a TXT record at `_privacyfence-relay.<domain>` with the value
  `pf-relay=<hex SHA-256 of the server's relay token>`, checked by DNS over HTTPS on every
  registration. The relay forwards a sign-in code only when Google's `hd` redirect parameter is a
  verified domain of that host (a redirect carrying `error` and no code is forwarded so the server can
  show its own error); a server-side check against the same verified set is repeated on the server. A
  Worker variable can switch the relay-side check off if Google stops sending `hd`.
  `privacyfence-app --relay-dns-records` and the server log print the records to add; `/login` says
  when it is waiting for DNS.
- **Cross-server safety.** Every server shares one client, so each accepts only a sign-in it started:
  the code flow with PKCE and a nonce stays mandatory, an ID token must come from the server's own
  code exchange, and `state` is single-use.
- **The relay token anchors the records.** `org/relay_token.txt` must be kept in backups; replacing it
  means publishing new records.

## Alternatives considered

- **No built-in sign-in; every server registers its own IdP app.** That stays the recommended robust
  choice, and the only choice for personal accounts, groups or an Internal consent screen.
- **On by default.** Rejected: it would put every server's availability and sign-in policy on
  PrivacyFence's services without the operator choosing so.
- **The same Google project as the connector apps.** Rejected for the blast radius above.
- **Allowing personal Gmail accounts.** Rejected: they have no `hd`, so a domain cannot be proven.

## Consequences

- **A single point of failure.** If the relay or the `privacyfence-signin` project is down or
  suspended, nobody can sign in to any server using it, and a passkey does not help because sign-in
  itself is the IdP. The docs present it as the quick start for small Workspace teams and recommend an
  organization's own IdP app for Workspace-level control or independence from PrivacyFence.
- A hostile operator can still show "PrivacyFence" on its own Google sign-in page, but cannot harvest
  other organizations' accounts through this client: the relay forwards a code only for domains the
  operator proved. The connector clients are not bounded this way
  ([ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md)).
- The relay-side check relies on `hd`, which is observed behaviour rather than a documented contract.

## Verification

`tests/unit/test_org_mode.py`, `tests/unit/test_org_identity.py`, `tests/unit/test_relay_client.py`,
`cloudflare/oauth-relay/test/relay.test.ts`. Code: `src/privacyfence/org_mode.py`
(`BUILTIN_GOOGLE_IDP`), `src/privacyfence/org_identity.py` (`check_authz_policy`),
`src/privacyfence/relay_client.py` (`signin_domains`, `missing_dns_records`),
`cloudflare/oauth-relay/src/index.ts`.

## Related

ADR 0255, 0259, 0263, 0264, 0265.
