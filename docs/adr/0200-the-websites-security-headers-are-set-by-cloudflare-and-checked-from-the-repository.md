# ADR 0200: The website's security headers are set by Cloudflare and checked from the repository

## Status

Accepted — 2026-10-17. Implemented.
Amends [ADR 0053](0053-the-website-is-hosted-on-github-pages-behind-cloudflare.md): the site sent no security headers.

## Context

privacyfence.eu is served by GitHub Pages behind the Cloudflare proxy (ADR 0053). GitHub Pages cannot
set response headers, so the site sent no HSTS, no `X-Content-Type-Options`, no `Referrer-Policy` and
no Content-Security-Policy. The hand-written pages need no inline script or style, but the generated
`/docs/` emits inline bootstrap scripts and inline table-alignment styles, so one policy for the whole
site would break it.

## Decision

Cloudflare sends the headers; the repository holds the expected set and checks the live site.

- `scripts/site_security_headers.py` defines `HSTS` (`max-age=31536000; includeSubDomains`, no
  `preload`), `NOSNIFF`, `REFERRER_POLICY` (`strict-origin-when-cross-origin`), `CSP_SITE`,
  `CSP_DOCS` and `headers_for_path(path)`. Running it prints the three Cloudflare rules to create.
- `CSP_SITE` allows `'self'` and Google Tag Manager and Analytics hosts only, `object-src 'none'`,
  `frame-ancestors 'none'`. `CSP_DOCS` is the same with `'unsafe-inline'` added to `script-src` and `style-src`.
  `/docs` and `/docs/...` get `CSP_DOCS`; everything else gets `CSP_SITE`.
- Three Modify Response Header Transform Rules on the zone: `pf-security-headers-all` (the three
  plain headers), `pf-csp-site` and `pf-csp-docs`. `www.privacyfence.eu` is a 301 to the apex and needs none.
- `scripts/check_site_headers.py <base-url>` fetches `/`, `/docs/` and `/download/` without following
  redirects and compares each header to `headers_for_path`. The `deploy` job in
  `.github/workflows/pages.yml` runs it as `Check the security headers` after every deploy, and the
  workflow also runs when either script changes.
- The test server `tests/website_site.py` sends `headers_for_path`, so every browser test runs under the policy.

## Alternatives considered

- **The CSP from the issue verbatim.** It blocks `/docs/`' inline scripts and its many inline styles, so
  it would have broken the docs the day the rule went live.
- **Hashes for the inline scripts.** They change whenever the docs generator is bumped, and nobody
  would notice until `/docs/` broke.
- **`'unsafe-inline'` site-wide.** The hand-written pages need none, so they keep the strict policy.
- **A `<meta http-equiv>` CSP in the header partial.** It cannot carry `frame-ancestors` and cannot cover HSTS.
- **HSTS `preload`.** Irreversible for the browsers' lists; a separate decision.

## Consequences

- The rules live in the Cloudflare dashboard, so Cloudflare can drop or change them without a commit.
  `pages.yml`'s post-deploy `Check the security headers` step is how that is caught, on the next deploy;
  between deploys nothing watches.
- The `/docs/` policy allows inline scripts and styles because the docs generator emits them. Script
  injection into `/docs/` is therefore not blocked by CSP; the other directives still apply.
- Adding a third-party origin, or an inline script to a hand-written page, now fails a test before it
  fails in a visitor's browser.
- A change to the expected headers is a change to the script plus an edit in the Cloudflare dashboard.
- HSTS with `includeSubDomains` commits every subdomain of privacyfence.eu to HTTPS.

## Verification

- `tests/unit/test_website_security_headers.py`: exact constants, path selection, and that built pages
  outside `/docs/` have no inline script, style or event handler and load scripts only from allowed origins.
- `tests/integration/test_website_csp.py`: no CSP violation on any page under the policy, consent banner still works;
  run by `.github/workflows/website-build.yml`.
- `tests/unit/test_check_site_headers.py`: the checker against matching, missing and wrong headers.
- `.github/workflows/pages.yml`: the post-deploy check.

## Related

- Issue [#926](https://github.com/privacyfence/privacyfence/issues/926).
- Amends [ADR 0053](0053-the-website-is-hosted-on-github-pages-behind-cloudflare.md).
