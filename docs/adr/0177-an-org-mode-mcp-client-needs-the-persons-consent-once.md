# ADR 0177: An org-mode MCP client needs the person's consent once

## Status

Accepted — 2026-10-14. Implemented.
Amends ADR 0108: the consent pages keep the sign-in popup's opener.

## Context

Org mode authenticates every MCP client against one fixed client registered at the identity
provider, and any client can register itself at `/register` (ADR 0011). The IdP callback
(`handle_idp_callback`) used to mint a PrivacyFence code and redirect straight to the registering
client's redirect URI. When the member already had an IdP session, the IdP re-authenticated them
silently, so anyone able to start `/authorize` for a client they had registered, and to get a
member's browser to follow the link, received a code for that member. Nobody was ever shown which
client was asking. This is a confused-deputy problem (SEC-B1 in the 2026-10-14 review).

## Decision

- **A consent page stands between the IdP callback and the code.** After the IdP sign-in and the
  authorization policy check, `OrgOAuthProvider.handle_idp_callback` mints a code only if the
  person has already allowed this client and redirect URI. Otherwise it stores a pending consent
  under an unguessable `consent_id` (5 minutes, single-use, capped like pending authorizations)
  and returns `/oauth/consent/<consent_id>`, which the route redirects to.
- **The page names what is asking.** `GET /oauth/consent/<id>` shows the client's sanitized
  registered name (`An unnamed AI client` when it has none), the host it will return to (the
  scheme for a custom-scheme redirect) and the person's own email or id, with an **Allow** and a
  **Don't allow** button. Every value is HTML-escaped.
- **Only a CSRF-checked POST decides.** `POST /oauth/consent/<id>` rejects a foreign `Origin`
  (403), a decision other than `allow` or `deny` (400), and a missing, expired or wrong-CSRF
  consent (400; the pending consent is consumed either way). Allow records the consent and issues
  the code; deny redirects to the client with `error=access_denied`.
- **The redirect is a 200 page, not a 302.** Org mode's CSP carries `form-action 'self'`, which
  browsers also apply to every redirect a form submission follows, so a form POST cannot end on
  the client's origin. The response is a "Returning to <host>" page with a meta refresh and a
  `Continue` link to the client's redirect URL.
- **Consent is remembered per person, client and redirect URI** in
  `oauth_consents.json` under that person's own directory (`ConsentStore`). A missing or malformed
  file reads as no consent, so the page is shown again.
- **The IdP leg expires.** `handle_idp_callback` now refuses a pending authorization older than
  its 5-minute TTL; it was only pruned when another `/authorize` came in.
- **The consent pages keep the opener (amends ADR 0108).** `_SecurityHeadersMiddleware` sends
  `Cross-Origin-Opener-Policy: unsafe-none` for `/oauth/consent/*` as well as the sign-in popup
  paths, because a client that hands the code back through `window.opener` (Gemini Enterprise)
  now reaches it through those pages.

## Alternatives considered

- **Bind the consent to a browser cookie.** Rejected: the consent id exists only in the browser
  that completed the IdP sign-in, which the attacker cannot do for the victim, so a cookie adds
  nothing.
- **An admin allow-list of redirect hosts.** Rejected: a configuration surface nobody has asked
  for. The page names the host so the person can judge.
- **A script `fetch` and navigation instead of the continue page.** Rejected: it adds script to a
  page that needs none.

## Consequences

- Every person sees the page once per client and redirect URI; later sign-ins of that client are
  as quiet as before.
- A remembered consent survives signing out everywhere. There is no revocation yet; removing the
  file under the person's directory is the only way to clear it.
- A client that registers a second redirect URI asks again for that one.

## Verification

- `web/oauth_consent.py` (`ConsentStore`, `ConsentView`, the two pages),
  `web/oauth_provider.py` (`handle_idp_callback`, `consent_view`, `decide_consent`),
  `web/routes_mcp.py` (`mount_org_oauth`), `web/server.py` (`_keeps_popup_opener`).
- `tests/unit/web/test_oauth_consent.py`, `test_oauth_provider.py` (`-k consent`, `after_ttl`),
  `test_org_mcp_e2e.py` (`unconsented_client_stops_at_consent_page`, foreign `Origin`, bad
  decision) and `test_server_org_mode.py` (`CrossOriginOpenerPolicy`).

## Related

- ADR [0011](0011-org-mode-runs-its-own-oauth-authorization-server.md)
- ADR [0108](0108-oauth-sign-in-popups-keep-their-opener-and-basic-clients-need-not-repeat-their-id.md)
