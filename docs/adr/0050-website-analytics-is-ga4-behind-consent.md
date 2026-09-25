# ADR 0050: Website analytics is Google Analytics 4, loaded only after consent; Google is the only search tooling

## Status

Accepted — 2026-09-25. Implemented in `website/site.js`.

## Context

The site needs to know which pages people read, where they come from (search, AI assistants,
links) and which connectors and platforms interest them. Installer downloads are already counted
by the download Worker, without cookies ([ADR 0024](0024-pre-releases-are-publicly-downloadable.md)
and `cloudflare/downloads/migrations/0001_download_counts.sql`), and stay the headline number.

The maintainer works in Google's tools: Google Search Console was already set up for the domain,
and Google Analytics 4 links to it, which puts search queries and on-site behaviour in one place.
GA4 sets cookies (`_ga`, `_ga_<id>`) and reads device storage, which on an EU site requires prior
opt-in consent (ePrivacy Directive Art. 5(3)). PrivacyFence is a privacy product, so how its own
site measures visitors is part of its credibility.

Decided as D3 and D4 of the website plan ([PR #683](https://github.com/privacyfence/privacyfence/pull/683)).

## Decision

1. **GA4, behind a self-hosted consent banner.** The banner offers *Accept* and *Decline* with
   equal weight and nothing pre-selected. The choice is kept in `localStorage`
   (`pf-analytics-consent`), never a cookie, and is reopened from *Cookie settings* in every
   page's footer. No third-party consent platform.
2. **Nothing reaches Google before "Accept".** The GA snippet is not in any page; `site.js`
   injects it only after consent (Consent Mode's *basic* implementation). With no choice, or
   after *Decline*, the site makes no request to any third party and sets no cookie.
   Withdrawing consent disables GA and deletes its cookies.
3. **Minimal GA4 settings:** Google Signals, advertising personalization and data sharing off;
   data retention 2 months; no User-ID, and no custom dimension carrying anything
   visitor-specific. Only two custom signals are sent: the page's content group
   (`marketing`, `download`, `connector`, `platform`, `docs`) and a `download_click` event with
   platform, architecture and channel.
4. **Google is the only search tooling:** Search Console, linked to GA4. No Bing Webmaster Tools
   and no IndexNow.
5. **Cloudflare Web Analytics stays off** for the domain, because on a proxied site Cloudflare
   can inject its beacon at the edge, where no repository test can see it.
6. `/privacy/` names Google as processor, the cookies and their lifetime, the retention period,
   the EU–US Data Privacy Framework transfer and how to withdraw consent.

## Alternatives considered

- **Cloudflare Web Analytics** (the previous choice). Cookieless and needs no banner, but it
  gives no search-query data, no per-event data such as download clicks, and would be a second
  vendor next to Search Console. Rejected for one Google toolset.
- **GA4 with Consent Mode *advanced*.** Sends cookieless pings to Google before consent, so
  Google can model the visitors who decline. Rejected: it is a request to a third party the
  visitor has not agreed to, on a privacy product's site.
- **Self-hosted analytics (Plausible, Matomo, Umami).** Would need a server to run and maintain,
  which the project does not have and does not want.
- **Bing Webmaster Tools and IndexNow.** Rejected for now: they add Bing-side reporting and
  faster Bing indexing, not indexing itself; Bingbot finds the sitemap through `robots.txt`.
  Bing's index feeds ChatGPT search and Copilot, so if those assistants lag behind Google-backed
  answers, re-adding Bing Webmaster Tools (an import from Search Console) is the first remedy.

## Consequences

- GA4 sees only visitors who accept, so its numbers are trends and comparisons, not totals.
  Downloads keep coming from the Worker's cookieless counter.
- A privacy product's own site runs Google Analytics. Accepted; consent-only loading, minimal
  settings and disclosure on `/privacy/` are what make it defensible. No page may claim the site
  is tracker-free.
- Every page, including the generated documentation, must load `site.js` and declare its
  content group.

## Verification

- `tests/integration/test_website_consent.py`: on every page, no request to any origin but the
  site and the project's download Worker, and no cookie, before a choice; *Accept* loads
  googletagmanager.com, on this and the next page; *Decline* loads nothing and persists; *Cookie
  settings* reopens the choice and withdrawing disables GA; the config carries the content group
  and signals/ads personalization off; `download_click` is sent only after consent.
- `tests/unit/test_website_pages.py`: no page contains a Google script tag or the measurement ID;
  only `site.js` does. Every page declares a content group and has the *Cookie settings* footer
  button.

## Related

- [ADR 0024](0024-pre-releases-are-publicly-downloadable.md) — the download Worker, whose counts
  stay the headline KPI.
- [PR #683](https://github.com/privacyfence/privacyfence/pull/683) — the website plan (decisions
  D3, D4, I1).
