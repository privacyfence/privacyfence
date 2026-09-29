# ADR 0108: An OAuth sign-in popup keeps its opener, and an HTTP Basic client need not repeat its client ID in the body

## Status

Accepted — 2026-09-29. Implemented. Part of
[issue 393](https://github.com/privacyfence/privacyfence/issues/393).

## Context

The maintainer's first Gemini Enterprise check against the test organization deployment
(PrivacyFence 5.1.1.dev20, 2026-09-29) connected only after two server-side problems were worked
around by hand.

**The popup lost its opener.** Gemini Enterprise opens the sign-in in a popup. At the end, its
redirect page (`https://vertexaisearch.cloud.google.com/oauth-redirect`) hands the authorization code
back to the Cloud console through `window.opener`, and Google's backend then redeems it at `/token`.
Every PrivacyFence response carried `Cross-Origin-Opener-Policy: same-origin` (SEC-18, commit
`137a015f`). That included the 302s from `/authorize` and `/oauth/idp/callback`, which the popup passes
through. A COOP mismatch on any response in a navigation chain, redirects included, moves the popup
into a new browsing-context group and nulls `window.opener`. The code never reached the console, the
console showed "We encountered some problems during authentication", and the access log showed a
successful `/oauth/idp/callback` followed by no `POST /token` at all. Deleting the header on those two
paths at the reverse proxy fixed it at once: Google's backend then called `/token` and got a 200.
claude.ai and ChatGPT never hit this, because their redirect URIs are server-side endpoints that
redeem the code themselves.

**HTTP Basic clients were refused.** Gemini Enterprise's "Use HTTP Basic Authentication" option,
ticked by default, sends the client ID and secret only in the `Authorization` header. RFC 6749
section 2.3.1 allows that. The MCP SDK's `ClientAuthenticator` looks the client up by the form
body's `client_id` before it reads the header, so `/token` answered 401 "Missing client_id"
(reproduced against the test deployment). `/revoke` has the same assumption a second time: after
authenticating, its form model requires a `client_secret` field it never reads.

## Decision

1. **`/authorize` and `/oauth/idp/callback` send `Cross-Origin-Opener-Policy: unsafe-none`.** Every
   other response keeps `same-origin`. The path set is `_OAUTH_POPUP_PATHS` in `web/server.py`.
2. **`/token` and `/revoke` accept a client ID given only in an HTTP Basic header.**
   `web/routes_mcp.py`'s `_BasicAuthClientId` wraps those two SDK endpoints. On a POST with a
   `Basic` header and no `client_id` in the form body, it adds the header's client ID to the body,
   and on `/revoke` also an empty `client_secret` field when there is none. It changes nothing else:
   the SDK still authenticates the client from the header, checks that a body ID matches it, and
   compares the secret.

## Alternatives considered

- **Keep COOP everywhere, and document the reverse-proxy edit.** Rejected: every organization
  deployment would need a proxy change to work with one AI client, and the header protects nothing
  on these paths. Both only redirect. The `/oauth/idp/callback` error responses are plain text, with
  no script and nothing another window could reach through a shared browsing-context group.
- **`same-origin-allow-popups` instead of `unsafe-none`.** Rejected: it keeps the opener only for
  popups that *this* document opens. Here PrivacyFence's response is the popup, opened by Google's
  console, and any value other than `unsafe-none` on the popup's own responses severs the link.
- **Tell Gemini Enterprise admins to untick HTTP Basic** and register the client as
  `client_secret_post`. Rejected as the fix: that is Google's default, RFC 6749 allows it, and the
  failure is PrivacyFence's. It stays the workaround for releases without this ADR.
- **Subclass or replace the SDK's `ClientAuthenticator` and handlers.** Rejected: `create_auth_routes`
  builds them internally. Replacing them means copying the SDK's token and revocation handlers,
  which are protocol machinery this project deliberately takes from the SDK (ADR 0011). A wrapper
  that only completes the form keeps all the SDK's checks.
- **Patch the SDK upstream only.** Still worth doing, but it doesn't help deployments until a
  fixed SDK is released and pinned. The wrapper no-ops once the SDK reads the header itself.

## Consequences

- A popup-based OAuth client can finish PrivacyFence's sign-in on a standard deployment, with no
  proxy changes.
- A `client_secret_basic` client works whether or not it repeats its client ID in the body.
- `/authorize` and `/oauth/idp/callback` no longer isolate their browsing context. The risk accepted
  is limited to what those two redirect-only responses expose, which is nothing a cross-origin
  opener could read.
- `_BasicAuthClientId` buffers the `/token` or `/revoke` body when a Basic header is present, up to
  1 MiB, the SDK's own body cap. Past that, it passes the body through unchanged for the SDK to
  reject.

## Verification

- `tests/unit/web/test_server_org_mode.py::TestCrossOriginOpenerPolicyOrgMode`: `unsafe-none` on
  both popup paths, `same-origin` on `/login`, `/approvals`, `/token` and the metadata document.
- `tests/unit/web/test_org_oauth_basic_client_auth.py`: a Basic-only code exchange, refresh and
  revocation succeed; a wrong secret and a mismatched body ID are still refused; the header parsing
  and the middleware's body handling, oversized bodies included.

## Related

- [Issue 393](https://github.com/privacyfence/privacyfence/issues/393) (Gemini Enterprise).
- [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md): the SDK's authorization-server
  routes.
- Commit `137a015f` (SEC-18): where COOP was added.
