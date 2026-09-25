# ADR 0053: The website is hosted on GitHub Pages, behind the Cloudflare proxy

## Status

Accepted — 2026-09-25 (recorded when the site gained its build step; the hosting itself predates
it). Implemented in `.github/workflows/pages.yml`.

## Context

privacyfence.eu is a static site: hand-written pages, the generated `/docs/`, and a few generated
files, all produced by `scripts/build_site.py`. The project already runs on GitHub (source,
issues, releases, Actions), and its domain is on Cloudflare, which also runs the download Worker
at `downloads.privacyfence.eu`. The project has no budget and one maintainer, and prefers
account-owned credentials to personal ones.

Decided as C2, D2 and D6 of the website plan ([PR #683](https://github.com/privacyfence/privacyfence/pull/683)),
when adding a docs generator raised the question of moving to a host with its own build.

## Decision

1. The site is served by **GitHub Pages**, deployed by `pages.yml` with the official Pages
   actions (OIDC, no stored token), **only from `main`**: the `github-pages` environment allows
   no other ref, a release event re-dispatches the workflow on `main`, and `releases/*` branches
   never deploy.
2. `privacyfence.eu` is **proxied by Cloudflare**. The canonical host is the apex; `www` is a
   Cloudflare redirect rule (301) to it. Cloudflare's AI-bot blocking, Bot Preference Sync and
   Web Analytics are off, so the served `robots.txt` and pages are exactly what the build wrote
   (ADR 0048, ADR 0050).
3. `pages.yml` rebuilds on a push to `main` that touches the site, after every release, every six
   hours (download counts and the pre-rendered `/download/`), and on demand.

## Alternatives considered

- **Cloudflare Pages.** Would put the site next to the Worker, but adds a second deploy pipeline
  and an API token with Pages rights to hold and rotate, for no feature the site needs.
- **Netlify or Vercel.** A third-party account and its own build environment, outside the
  audited, hash-locked Actions build.
- **Read the Docs for `/docs/`.** A second domain or subdomain, its own theme and build, and a
  hosted service injecting its own scripts, which conflicts with the site's consent rules.
- **Keep the site on GitHub Pages without Cloudflare.** Loses the apex-plus-`www` redirect and
  the edge cache the domain already has, and splits DNS from the download Worker's zone.

## Consequences

- No server-side logic: everything dynamic (download links, counts) comes from the download
  Worker, and every page must work as static files.
- Response headers are GitHub Pages' defaults plus whatever Cloudflare adds; there is no
  per-path header or redirect file.
- Cloudflare can change a response at the edge (a bot rule, an injected beacon) without a commit.
  The post-deploy checks after a website change (crawler user agents get 200, the served
  `robots.txt` is the built one, no `cloudflareinsights` script) are how that is caught.

## Verification

- `.github/workflows/pages.yml`: the deploy job, its triggers, and the release re-dispatch.
- `.github/workflows/website-build.yml`: every pull request builds the same site without
  deploying it.

## Related

- [PR #683](https://github.com/privacyfence/privacyfence/pull/683) — the website plan (C2, D2, D6).
- [ADR 0048](0048-every-crawler-is-allowed-including-ai-training.md),
  [ADR 0050](0050-website-analytics-is-ga4-behind-consent.md),
  [ADR 0052](0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md).
