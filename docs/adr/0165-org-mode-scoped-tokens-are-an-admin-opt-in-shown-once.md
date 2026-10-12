# ADR 0165: in organization mode scoped tokens are an administrator opt-in, shown once

## Status

Accepted — 2026-10-11. Implemented.
Amends [ADR 0072](0072-org-mode-persists-only-sealed-refresh-tokens.md).

## Context

[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)
adds scoped tokens in both modes. In organization mode the server is shared, there is no companion
on the person's computer to put a secret on the clipboard, and an administrator is responsible for
what credentials exist. ADR 0072 decided that org mode persists only sealed refresh tokens; a
scoped token is a second kind of credential at rest.

## Decision

- Scoped tokens are **off** until the administrator turns them on in the organization config
  bundle: `scoped_tokens.enabled` (`--enable-scoped-tokens` / `--disable-scoped-tokens` in
  `scripts/build_org_bundle.py`) and `scoped_tokens.max_days` (`--scoped-token-max-days`, 1-365,
  default 90), the longest a token may last. The Settings page offers 90 days or the cap if lower.
- Creation sits behind the signed-in session, the CSRF check and the origin check, and the passkey
  step-up wherever the install requires one for sensitive actions.
- The secret is shown **once**, in the response that creates the token (`Cache-Control: no-store`),
  in a dialog with a Copy button. It is never logged, audited or stored; only its SHA-256 is, in
  the person's `scoped_tokens.json`. This amends ADR 0072: org mode now keeps hashed scoped tokens
  at rest besides sealed refresh tokens.
- An administrator revokes all of a person's tokens from the server's command line:
  `privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID`. The running daemon notices on the token's
  next use. The provider's `revoke_all_for_principal` also revokes them, but nothing calls it yet.

## Alternatives considered

- **A narrowed OAuth grant** — see ADR 0164: no scope negotiation in the SDK flow, and the 30-day
  refresh chain cannot carry a 90-day job.
- **Always on in organization mode** — the administrator decides whether long-lived bearer
  credentials exist on their server at all.
- **Showing the secret again later** — only a hash is kept.

## Consequences

An OAuth grant ends within its 30-day refresh chain. A scoped token lives until it expires (90 days
by default, at most the administrator's cap) unless it is revoked. **Offboarding a person therefore
includes running `privacyfence-app --revoke-scoped-tokens`.** Organization mode has no automatic
"sign out everywhere" today; wiring `revoke_all_for_principal` to an action would close that gap.
A person who loses a token creates a new one.

## Verification

`TestScopedTokenWiring` in `tests/unit/web/test_server.py`, `TestScopedTokenRoutes` in
`tests/unit/web/test_routes_org_settings.py`, `TestScopedTokenFlags` in
`tests/unit/test_build_org_bundle.py`, `TestRevokeScopedTokens` in `tests/unit/test_daemon_main.py`
and the revoke-all case in `tests/unit/web/test_oauth_provider.py`.

## Related

[ADR 0072](0072-org-mode-persists-only-sealed-refresh-tokens.md),
[ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md).
