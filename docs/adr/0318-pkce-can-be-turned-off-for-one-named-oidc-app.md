# ADR 0318: PKCE can be turned off for one named OIDC app

## Status

Accepted — 2026-11-16. Implemented: `src/privacyfence/oidc_provider.py`, `src/privacyfence/web/routes_oidc.py`, `src/privacyfence/daemon_main.py`.
Amends [ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md). Issue [#1143](https://github.com/privacyfence/privacyfence/issues/1143).

## Context

- Forgejo v15.0.11 sends no PKCE to an OpenID Connect login source, as reported in issue
  [#1143](https://github.com/privacyfence/privacyfence/issues/1143). ADR 0273 made PKCE S256 mandatory for every
  app, so Forgejo cannot be listed in `PRIVACYFENCE_OIDC_CLIENTS`.
- RFC 9700, OAuth 2.0 Security Best Current Practice, section 2.1.1
  (<https://www.rfc-editor.org/rfc/rfc9700>): public clients MUST use PKCE; for confidential clients PKCE is
  RECOMMENDED, not required. An authorization server MUST mitigate PKCE downgrade attacks: a token request that
  carries a `code_verifier` is accepted only if the authorization request carried a `code_challenge`.
- Every OIDC app already has a mandatory secret of at least 32 characters, and `/oidc/token` always
  authenticates the app. So every app is a confidential client; there are no public clients.

## Decision

1. **A per-app variable.** `PRIVACYFENCE_OIDC_CLIENT_<NAME>_REQUIRE_PKCE`. Unset or empty means `true`; exactly
   `true` or `false` are accepted; anything else refuses the start with
   `PRIVACYFENCE_OIDC_CLIENT_<NAME>_REQUIRE_PKCE must be true or false`. `OidcClient` gains `require_pkce`,
   default `True`.
2. **Authorize.** When the app requires PKCE, or the request carries a `code_challenge` or a
   `code_challenge_method`, the request is validated as before: S256 and a well-formed challenge, or the
   `invalid_request` redirect `PKCE with S256 is required.` So an app with the setting off that sends `plain`,
   only a method, only a challenge or a malformed challenge is still refused. Otherwise the code is issued with
   no challenge. `nonce` stays optional.
3. **Redeem.** A code issued with a challenge is redeemed as today. A code issued without one is redeemed only
   when the token request has no `code_verifier` field at all; a verifier, even an empty one, is refused with
   `invalid_grant`, which is the downgrade rule of RFC 9700. The client and redirect checks apply to both.
4. **Discovery is unchanged.** `code_challenge_methods_supported` stays `["S256"]`.
5. **A start-up warning.** The daemon logs `OIDC client <name> signs in without PKCE` for each app with the
   setting off, naming the variable. There is no log line per sign-in about PKCE and nothing in the audit trail.

## Alternatives considered

- **Allowing PKCE off only when a nonce is sent.** RFC 9700 allows a confidential client to omit PKCE. Requiring
  a nonce would add a second condition that Forgejo may not meet, and that has not been checked against Forgejo.
- **Turning PKCE off for every client at once (a global switch).** The weaker setting would then apply to apps
  that do send PKCE, such as Peek and Grist.
- **Advertising `none` or dropping `code_challenge_methods_supported` in discovery.** The provider still supports
  S256, and apps that send it are still checked.
- **A separate public/confidential flag.** Every client already has a mandatory secret and is authenticated at
  `/oidc/token`, so every client is confidential.

## Consequences

- For an app with PKCE off, a stolen code can be redeemed by anyone holding that app's secret, or injected into
  the app's own callback, for example a code leaked from a log or a Referer header. Only the app's `state` and
  `nonce` checks stand in the way.
- For such an app, ADR 0273's argument for having no consent page ("useless without that client's PKCE
  verifier") rests on the app's secret and its `state` check alone.
- The operator accepts this per app, and the start-up warning keeps it visible.

## Verification

- `tests/unit/test_oidc_provider.py`: `test_require_pkce_defaults_on`, `test_require_pkce_false_parses`,
  `test_require_pkce_must_be_true_or_false`, `test_a_grant_without_a_challenge_redeems_without_a_verifier`,
  `test_a_grant_without_a_challenge_refuses_a_verifier`,
  `test_a_grant_without_a_challenge_still_checks_client_and_redirect`,
  `test_a_grant_with_a_challenge_refuses_a_missing_verifier`.
- `tests/unit/web/test_routes_oidc.py`: `test_pkce_off_client_full_flow_without_pkce`,
  `test_pkce_off_client_that_sends_s256_is_still_checked`, `test_pkce_off_client_refuses_partial_or_plain_pkce`,
  `test_pkce_off_client_refuses_a_verifier_without_a_challenge`, `test_pkce_still_required_for_a_default_client`.
- `tests/unit/test_daemon_main.py`: `test_an_oidc_client_with_pkce_off_logs_a_warning`.

## Related

[ADR 0273](0273-privacyfence-is-an-oidc-provider-for-apps-the-operator-names.md),
[ADR 0311](0311-the-oidc-provider-sends-approved-group-membership-to-apps-that-opt-in.md).
