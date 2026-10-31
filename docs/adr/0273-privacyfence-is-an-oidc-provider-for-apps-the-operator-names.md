# ADR 0273: PrivacyFence is an OpenID Connect provider for apps the operator names

## Status

Accepted — 2026-10-31. Implemented: `src/privacyfence/oidc_provider.py`, `src/privacyfence/web/routes_oidc.py`.
Relates to [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md), [ADR 0061](0061-the-mcp-token-and-the-browser-session-are-audience-separated.md) and [ADR 0224](0224-an-org-registration-is-attested-by-its-redirect-uris.md).

## Context

Peek and Grist, run beside PrivacyFence in the `privacyfence-suite` stack, can sign people in through
an OpenID Connect provider. PrivacyFence already knows who a person is, from the organization's
identity provider, the built-in Google sign-in or the passkey on `localhost`, and its OAuth server
serves only MCP clients. Registering Peek and Grist with the identity provider or with Google would be
setup the suite is meant to remove.

## Decision

- **PrivacyFence is an OIDC provider under `<issuer>/oidc`,** apart from the MCP authorization server.
  Its routes are in `routes_oidc.py`; the state is in `OidcProvider` (`oidc_provider.py`).
- **Only apps the operator names can use it.** `PRIVACYFENCE_OIDC_CLIENTS` and the per-app
  `…_REDIRECT_URIS` and `…_SECRET(_FILE)` variables are the whole registry. Redirect addresses are
  compared exactly and are never redirected to unless they match.
- **No consent page, no dynamic registration, no refresh tokens.** Access tokens and ID tokens last
  5 minutes, codes 60 seconds and once.
- **PKCE with S256 is mandatory.**
- **`email_verified` follows `org_identity.session_email_verified`:** true when the identity provider
  said so, or when the operator trusts unverified email and the address is in an allowed domain. A
  single-user session is verified, because the address is fixed in code and the install has one
  person. Peek and Grist link accounts by email, so an unverified session is refused by them.
- **MCP tokens and `/oidc` tokens are separate stores,** so neither works where the other is expected.

## Alternatives considered

- **Reusing the MCP authorization server's `/authorize` and `/token`.** They serve dynamically
  registered clients with consent and opaque tokens, and are audience-separated from browser sessions
  (ADR 0061). Mixing the two would weaken both.
- **Dynamic client registration.** Any page could register itself and sign people in silently.
- **A consent page.** The operator named the app, so a "allow Peek?" click protects nothing. Without
  it any site can make a signed-in browser mint a code, but the code goes only to an exactly matched
  registered address in the person's own browser and is useless without that client's PKCE verifier.
- **Optional PKCE.** Both clients send S256; a no-PKCE path would only keep code injection possible.
- **Refresh tokens and long-lived access tokens.** The apps keep their own sessions.
- **ES256 or EdDSA.** RS256 is accepted by every OIDC client library by default.
- **Relaying the upstream identity provider's ID token.** Its audience is PrivacyFence, and it does not
  exist in single-user or built-in-Google mode.
- **`prompt`, `max_age` and POST to the authorization endpoint.** The two clients use none of them.

## Consequences

- **A "signed in to PrivacyFence?" signal.** With no consent page, a site can make a signed-in browser
  request a code and see whether it came back as a code or as a redirect to `/login`. Only the
  registered address receives a code.
- **Cookies on single-user `localhost`.** Browsers do not separate cookies by port, so `pf_org_session`
  is also sent to Peek and Grist on other `localhost` ports. It is HttpOnly and PrivacyFence's own POST
  routes check `Origin`. The exposure is accepted for single-user installs on one machine.
- **Single-user needs one network namespace.** The issuer is `http://localhost:<port>/oidc` and the
  apps require it exactly, so they must reach PrivacyFence at that address.
- No `end_session_endpoint`: the apps sign out locally.

## Verification

`tests/unit/web/test_routes_oidc.py`, `tests/unit/test_oidc_provider.py` and
`tests/unit/web/test_server_org_mode.py` cover the flow, PKCE, single-use codes, client
authentication and the token separation. Operator documentation: `docs/oidc-provider.md`.

## Related

ADR 0011, 0013, 0061, 0177, 0224, 0256, 0264, 0274.
