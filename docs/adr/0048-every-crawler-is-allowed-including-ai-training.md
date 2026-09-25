# ADR 0048: privacyfence.eu allows every crawler, AI search and AI training included

## Status

Accepted — 2026-09-25. Implemented in `website/robots.txt`; the Cloudflare side is a dashboard
setting outside the repository.

## Context

The project's success measures are installer downloads, search impressions and GitHub stars.
More and more people find software by asking an AI assistant rather than a search engine, and an
assistant can only recommend and describe PrivacyFence correctly if its crawler could read the
site, and its training data included it.

`privacyfence.eu` is served by GitHub Pages through a Cloudflare proxy. Cloudflare's bot settings
can block AI crawlers at the edge, and its "managed robots.txt" (Bot Preference Sync) prepends
Cloudflare's own rules to the served `robots.txt`, so the file in this repository would not be
what crawlers read. Nothing on the site is private: every page is public documentation for
open-source software.

Decided as D1 and D2 of the website plan ([PR #683](https://github.com/privacyfence/privacyfence/pull/683)).

## Decision

1. `robots.txt` allows every user agent everything (`User-agent: *` / `Allow: /`) and names the
   sitemap. There is no per-bot list, and training crawlers (GPTBot, ClaudeBot, Google-Extended,
   CCBot and the like) are not treated differently from search crawlers.
2. Cloudflare's AI-bot blocking, Bot Preference Sync, AI Labyrinth and Bot Fight Mode are off for
   the domain, so the served `robots.txt` is the repository's file and no crawler is challenged.
3. The canonical host is the apex, `https://privacyfence.eu`; `www` redirects to it, and every
   page's canonical link names the apex.

## Alternatives considered

- **Allow search crawlers, block training crawlers.** The common default. Rejected: the content
  is meant to be read and repeated. A model that learned an outdated or second-hand description
  is the problem this site is trying to avoid, not a risk it is trying to limit.
- **Keep Cloudflare's managed rules and add allow rules on top.** Rejected: the served file would
  then depend on a dashboard setting nobody reviews in a PR, and Cloudflare's block list changes
  without notice.

## Consequences

- AI assistants can quote the site and the docs, and describe PrivacyFence from its own words.
- The content can be used to train models, with no attribution. Accepted.
- Turning Cloudflare's bot protection back on silently overrides decision 1. The post-deploy
  check (`curl -A` as GPTBot, ClaudeBot, OAI-SearchBot and Googlebot gets 200 on `/`,
  `/robots.txt` and `/sitemap.xml`) is how that is noticed.

## Verification

- `tests/unit/test_website_pages.py::test_robots_allows_every_crawler_and_names_the_sitemap`
  pins the file's rules exactly.
- The same module checks that every page's canonical link is on the apex.

## Related

- [ADR 0025](0025-no-certified-security-framework.md) — the site claims no certification, which
  is what makes quoting it verbatim safe.
- [PR #683](https://github.com/privacyfence/privacyfence/pull/683) — the website plan (decisions D1, D2, D6).
