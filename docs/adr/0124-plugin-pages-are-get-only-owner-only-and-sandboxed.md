# ADR 0124: Plugin pages are GET-only, owner-session-only, self-contained and sandboxed

## Status

Accepted — 2026-10-07. Implemented: `src/privacyfence/web/routes_plugins.py`,
`src/privacyfence/plugins/pages.py`, `src/privacyfence/web/server.py` (`_SecurityHeadersMiddleware`).

## Context

A plugin may want to show its owner something: a status, a table, a log. The simplest way is a web
page, served by the daemon next to Settings. But the page's author is the plugin, which is not
trusted with the owner's session: a page served from the app's own origin could read its cookies,
call its APIs and approve its own cards. An AI client must not be able to read the pages either,
since the pages may show data the plugin holds.

## Decision

A plugin that sets `pages: true` in its manifest is asked for `GET /plugins/<name>/<path>`; the
daemon forwards the request as `web.request` and filters the answer.

- **Methods.** GET and HEAD are served (HEAD is forwarded as GET and the body dropped). Any other
  method gets 405 with `Allow: GET, HEAD` and never reaches the plugin.
- **Who.** Only the owner's human session is served. An MCP bearer token, a request with no session
  cookie and any other principal get the same 404 the Settings pages give, and the plugin is not
  called. A plugin that is not running, or has `pages: false`, is a 404.
- **Paths.** The path is URL-decoded once; a `..` segment, a NUL, a backslash, `//` or a path over
  512 characters is a 400 that never reaches the plugin. The query is strings only, the last value
  of a repeated name winning.
- **Response filtering.** The status must be 200, 204, 400, 404 or 500 (anything else is a 502);
  the content type must be on an allow-list of eight types, or it becomes `application/octet-stream`;
  the body is capped at 8 MiB. The plugin's own `cache-control`, `set-cookie`, CSP and every other
  header are dropped. A plugin that times out (10 seconds) or errors gets a 502 with a fixed body.
- **Sandbox.** Every response under `/plugins/` carries
  `Content-Security-Policy: sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`,
  `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`, and the app's `Permissions-Policy` and `Cross-Origin-Opener-Policy`.
  The headers are set by the security-headers middleware, which overwrites any header a route sets.
  There is no `allow-same-origin`, so the page runs in an opaque origin: it cannot read the session
  cookie or call the app's APIs.
- **Self-contained pages.** The session cookie is `SameSite=Strict`, and an opaque-origin page's
  subresource requests carry no cookie, so they get the owner-only 404. A page must inline its CSS,
  scripts and images (`data:` URIs). Links and navigations between pages work, because they are
  top-level navigations that carry the cookie.

## Alternatives considered

- **Serve plugin pages with the app's CSP and origin.** Rejected. Plugin script could act as the
  owner.
- **Allow `allow-same-origin` and rely on the CSP.** Rejected. It would put the page back in the
  app's origin.
- **A separate port or origin for pages.** Rejected. It is another surface to secure and to
  authenticate, for a feature that works inside the sandbox.
- **POST and other methods.** Rejected. A page that changes state would need a cross-site request
  forgery defence the sandbox cannot provide; state changes go through gated tools.
- **Trust the plugin's headers.** Rejected. A plugin could set a cookie, a permissive CSP or a long
  cache lifetime.

## Consequences

- A plugin page is read-only, and cannot fetch its own CSS or script as a separate file.
- A plugin page cannot call back into PrivacyFence or into its own plugin by script: the sandbox
  allows scripts but not same-origin requests.
- A plugin author sees the same rules in a test, because the SDK's test host applies them.

## Verification

`tests/unit/plugins/test_pages.py` (normalization and filtering), `tests/unit/web/test_server.py`
(the plugin-page branch of the security-headers middleware),
`tests/integration/test_plugin_framework.py` (the status codes, the 405 and the headers) and
`tests/integration/test_plugin_pages_browser.py` (a real page in a real headless browser, unable to
read the cookie).

## Related

- [The tracking issue](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0033](0033-one-route-layer-per-surface-with-an-auth-adapter-per-mode.md)
- [ADR 0120](0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md)
