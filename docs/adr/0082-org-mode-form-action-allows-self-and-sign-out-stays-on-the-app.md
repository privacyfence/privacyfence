# ADR 0082: Org mode's `form-action` is `'self'`, and a sign-out lands on the app's own page

## Status

Accepted — 2026-09-27. Implemented. Builds on
[ADR 0063](0063-the-web-ui-csp-uses-per-response-nonces-not-unsafe-inline.md), whose policy this
changes in one directive for org mode only; its body is unchanged.

## Context

The CSP hardening of commit `0301f51b` (ADR 0063) ended every page's policy with
`base-uri 'none'; form-action 'none'; frame-ancestors 'none'`. Org mode's `/connect` still posts
native HTML forms, and those are the only native forms the app renders:

| Form | Action | Mode |
|---|---|---|
| Sign out | `POST /logout` | org |
| Telegram, phone step | `POST /connect/telegram/start` | org |
| Telegram, code step | `POST /connect/telegram/code` | org |
| Telegram, two-step password | `POST /connect/telegram/2fa` | org |
| Telegram, Cancel (on the code and password steps) | `POST /connect/telegram/cancel` | org |

Local mode has none: its settings, approvals and passkey pages post with `fetch()`.

Under `form-action 'none'` a real browser refused every one of these submissions
("Refused to send form data to …/logout because it violates … form-action 'none'"), so signing
out from `/connect` and connecting Telegram did nothing. The route tests post to the routes
directly through `TestClient`, which enforces no CSP, so nothing caught it.

A browser checks `form-action` on every redirect a form submission follows, not only on the
first request. So what the policy has to allow is wherever each submission can end up:

- `/logout` redirected to `/login`, and `/login` redirects to the IdP's authorization endpoint
  (`org_identity.build_authorization_url`), which is on another origin. PrivacyFence has no
  RP-initiated end-session redirect, so the IdP's authorization endpoint was the only other
  origin involved.
- Each Telegram step redirects to `/connect` (303). A step posted after the session ended
  redirected to `/login?next=/connect`, and from there to the IdP too.
- A rejected CSRF or `Origin` check answers in place (401/403) and does not redirect.

A second fault sat behind the first. Every page is served with `Referrer-Policy: no-referrer`, and
with that policy a browser sends `Origin: null` on a native form post. `org_session.check_origin`
accepted only a missing or matching `Origin`, so once the form was allowed to post, each Telegram
step was refused as cross-origin (403). `/logout` has no `Origin` check and was not affected.
`fetch()` posts keep their real `Origin`, which is why no other page was affected.

## Decision

1. **Org mode sends `form-action 'self'`; local mode keeps `form-action 'none'`.**
   `web/csp.py`'s `build_csp()` chooses it the same way it already chooses the manifest and icon
   exceptions: from `app_origin`, which only org mode's app passes. `base-uri 'none'` and
   `frame-ancestors 'none'` are unchanged in both modes.
2. **The IdP's origin is not in `form-action`, because no form submission reaches it any more.**
   `POST /logout` now answers `303 /signed-out`, a page on the app's own origin with a
   **Sign in** link to `/login`. A Telegram step posted without a live session does the same,
   instead of `/login?next=/connect`. Every chain now ends on the app's own origin:
   `/logout → /signed-out`, `/connect/telegram/* → /connect`, and `/connect/telegram/* →
   /signed-out` for an ended session. Sign-in itself is unchanged: a page view without a session
   still redirects to `/login` and on to the IdP, which is a navigation, not a form submission,
   so `form-action` does not govern it.
3. **`org_session.check_origin` accepts `Origin: null` only when `Sec-Fetch-Site` is
   `same-origin`.** The browser sets that header and no page can forge it. A cross-site
   sandboxed frame, which also sends `null`, is still rejected. Any other mismatched `Origin` is
   rejected as before, and the double-submit CSRF token is still required. Local mode's
   `session_auth.check_origin` is unchanged, because it has no native form.

## Alternatives considered

- **Add the IdP's authorization-endpoint origin to `form-action`, and keep `/logout → /login`.**
  The value would come from configuration the server already has (`idp.authorization_endpoint`),
  so it is workable. It was rejected because the redirect it would allow is a poor sign-out:
  with the IdP's own session still alive, bouncing to its authorization endpoint signs the person
  straight back in. The signed-out page removes that redirect and keeps the policy at `'self'`.
- **Script-driven submission (`fetch()` the POST, then navigate).** `connect-src 'self'` already
  allows it, and `form-action` could stay `'none'`. It was rejected because it rebuilds, in
  script, what a native form does: the Telegram steps' redirect-and-render flow, error display
  and keyboard submit. The pages would also stop working with scripts off, for no security gain
  over `'self'`. `web_shell`'s sign-out script still calls `form.submit()` after its push
  unsubscribe, so it would need rewriting too.
- **Keep `'none'` everywhere.** This leaves sign-out from `/connect` and Telegram connection
  broken in every browser.
- **A wildcard (`https:` or `*`).** It would let an injected `<form>` post its contents anywhere,
  which is what `form-action` exists to stop. The flows above need exactly one origin.
- **Accept `Origin: null` unconditionally, or relax `Referrer-Policy` on `/connect`.** Accepting
  `null` unconditionally also accepts it from a cross-site sandboxed frame. Relaxing the
  referrer policy weakens a header every page shares, to fix a check. The `Sec-Fetch-Site` test
  fixes only the case that is actually ours.

## Consequences

- An injected form on an org page can post only back to the app, where CSRF and `Origin` checks
  apply. On a local page it cannot post at all.
- Signing out no longer drops the person on the IdP's sign-in page. They land on
  `/signed-out`, and signing back in is one click. Ending the IdP's own session remains the IdP's
  business, as before.
- A future native form whose submission redirects off-origin (a new sign-in or end-session hop,
  say) will be refused by the browser. It must either land on the app first or add that one
  configured origin here, with a superseding ADR.
- A future native form in local mode needs `form-action` widened for local mode too, since local
  mode still sends `'none'`.

## Verification

- `tests/integration/test_browser_smoke.py`'s `TestNativeFormsSubmitUnderCsp` clicks every native
  form in headless Chromium under the real headers. It checks that sign-out ends the session and
  lands on `/signed-out`, that each Telegram step (start, code, 2fa, cancel) reaches its route
  through a faked `telegram_auth`, and that a step posted after the session ended lands on
  `/signed-out`. No `securitypolicyviolation` event or console CSP error may fire. It was run
  against three reversions:
  - with `main`'s `csp.py`, all four tests fail with Chromium's "Refused to send form data … form-action 'none'";
  - with `'self'` but the old `/logout → /login` redirect, sign-out fails with the violation
    reported on the redirect;
  - with `main`'s `check_origin`, the Telegram tests fail on the 403.
- `tests/unit/web/test_server.py`'s `TestBuildCsp` pins `form-action` per mode (`'none'` local,
  `'self'` org) together with `base-uri` and `frame-ancestors`. `TestSecurityHeaders` checks the
  served local header and `test_routes_push.py`'s `TestManifest` the served org header.
- `tests/unit/web/test_org_session.py` pins which `Origin: null` requests `check_origin` accepts.
  `test_routes_org_identity.py` and `test_routes_connect.py` pin the `/signed-out` redirects.

## Related

- [ADR 0063](0063-the-web-ui-csp-uses-per-response-nonces-not-unsafe-inline.md): the nonce-based
  CSP this changes one directive of.
- [ADR 0081](0081-org-mode-sends-a-count-only-web-push.md): the sign-out push unsubscribe that
  runs before the `/logout` form posts.
- Commit `0301f51b`: the hardening that introduced `form-action 'none'`.
- `src/privacyfence/web/csp.py`, `src/privacyfence/web/org_session.py`,
  `src/privacyfence/web/routes_org_identity.py`, `src/privacyfence/web/routes_connect.py`.
