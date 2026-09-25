# ADR 0063: the web UI's CSP uses nonces, not `'unsafe-inline'`

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-10 in `0301f51b`, "CSP
hardening -- nonce-based script/style-src, object/frame-src, header replace-not-extend", merged as
[#265](https://github.com/privacyfence/privacyfence/pull/265); the plan it implemented is
`git show ba1ec76e^:docs/security-remediation-plan.md`, Phase 3.1). Implemented.

## Context

Every page the embedded web app serves (approvals, approval cards, settings, security, org-mode
browser pages) is built from Python strings with inline `<script>` and `<style>` elements, and all
of its fonts, icons and PDF previews are `data:` URIs. The original policy was
`default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; ...`, so any missed
escape in those string-built templates was script execution with nothing behind it. These pages
display content the agent chose (subjects, file names, summaries) and carry the controls that
release its requests, so an injection there is a self-approval path.

## Decision

The policy is built by `web/csp.py`'s `build_csp(nonce)` and is exactly:

```
default-src 'none'; script-src 'nonce-<n>'; style-src-elem 'nonce-<n>';
style-src-attr 'unsafe-inline'; img-src data:; font-src data:; object-src data:;
frame-src data:; connect-src 'self'; worker-src 'self'; base-uri 'none';
form-action 'none'; frame-ancestors 'none'
```

- `web/server.py`'s `_SecurityHeadersMiddleware` mints a fresh nonce (`csp.new_nonce()`,
  `secrets.token_urlsafe(18)`) per request, puts it on `request.state` before the app runs, and
  reads it back at send time, so a route may override it. Every inline `<script>`/`<style>` element
  carries that nonce (`csp.nonce_for`).
- An approval card or dialog document is rendered once and served unchanged until decided, so its
  nonce is fixed at render time; the serving route calls `csp.set_nonce` so the header matches the
  nonce already in the body.
- `style-src-attr` keeps `'unsafe-inline'`: CSP has no nonce for `style="..."` attributes, and the
  UI uses computed inline style attributes throughout. `style-src-elem` is nonced, so an injected
  `<style>` block (CSS exfiltration via attribute selectors) is still blocked.
- `object-src data:` and `frame-src data:` both exist for the approval card's
  `<embed type="application/pdf" src="data:...">`: Chromium's PDF viewer navigates an internal
  frame, which `frame-src` governs. `worker-src 'self'` lets `/sw.js` register (ADR 0064).
- The middleware *replaces* the security headers (`MutableHeaders`) rather than appending, so a
  response never carries two CSP headers that a browser would intersect.

## Alternatives considered

- **Keep `'unsafe-inline'`.** Rejected: it leaves no second defense behind an escaping bug.
- **`'unsafe-hashes'` for style attributes.** Not adopted: it needs a hash of every distinct
  attribute value, which does not fit attributes built with per-call dynamic content.
- **Move every style attribute into classes and drop `'unsafe-inline'` entirely.** Deferred as a
  large, cosmetic refactor; the residual risk is bounded because an attribute alone cannot make a
  request `img-src`/`connect-src` do not already block.

## Consequences

- An injected `<script>` or `<style>` element without the response's nonce does not run.
- Every new HTML builder must take the nonce and put it on each inline element, or its script
  silently does not run in a real browser. `TestClient` does not enforce CSP, so only the browser
  smoke tests catch that.
- A card's nonce is per document, not per response, for as long as that card is served.

## Verification

- `tests/unit/web/test_server.py`: `TestSecurityHeaders` (policy present and locked down) and
  `TestCspNonce` (nonce, not `'unsafe-inline'`; `data:` for object/frame; the nonce differs across
  requests and matches the body).
- `tests/unit/web/test_csp.py`: nonce generation, `nonce_for`'s fallback, `set_nonce`.
- `tests/integration/test_browser_smoke.py`: `TestSecurityHeadersCsp` (an un-nonced script never
  runs, a nonced one does) and `TestPdfPreview` (the PDF pane renders under the real policy).

## Related

- [ADR 0010](0010-local-mode-serves-plain-http-on-localhost.md) — the local origin this policy
  protects.
- [ADR 0064](0064-browser-notifications-stay-on-the-machine.md) — the service worker `worker-src`
  admits.
