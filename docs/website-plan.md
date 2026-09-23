# Website, documentation site and SEO plan (privacyfence.eu)

**Active plan.** This file is temporary, per [`README.md`](README.md)'s documentation rules and
`CLAUDE.md` "Decisions, plans and ADRs": it lives here while the work below is open and is deleted
by the PR that lands the last wave, after every decision it records has an ADR (see
[ADRs this plan creates](#adrs-this-plan-creates)).

It turns the external "Website & Docs Strategy" review (2026-09-23) and the maintainer's answers to
the follow-up questionnaire into concrete work. Where the two differ, the answers win.

## Decisions

Settled on 2026-09-23. Question IDs refer to the questionnaire; they are here only so a later
reader can tell a deliberate choice from a default.

| # | Decision |
|---|---|
| A1 | The site serves **enterprise evaluation first**, with a one-click path for individuals (Download stays the primary CTA). |
| A2 | **No commercial offering.** Nothing on the site implies support, SLA or a paid tier. CTAs are Download, Docs, Source. |
| A3 | Success = **installer downloads** (already counted), **search impressions/clicks**, **GitHub stars**. |
| A4 | Minimal maintainer writing time. **Claude drafts all copy; the maintainer reviews.** Scope is therefore set by review capacity, not writing capacity. |
| B1 | Category: **"privacy and approval gateway for AI assistants"** (not "AI governance"). |
| B2 | "Give AI assistants access. Not authority." becomes the **tagline**; the H1 names the category. |
| B3 | Limitations (ADR 0025) get a **named section on `/security/`**, linked from `/enterprise/` and the footer. |
| B4 | Organization mode is presented as **production-ready, first-class**. |
| B5 | **English only.** |
| B6 | Name the tested clients — **Claude Desktop and claude.ai** — and say "MCP-compatible" generally. ChatGPT and Gemini are expected in the next version: the client list is kept in one data file so adding them is a one-line change **when they ship, not before**. |
| C1 | **Python docs generator** (Zensical or MkDocs + Material, see [Wave 1](#wave-1--build-pipeline-and-docs-site)) for `/docs/`; **hand-written HTML** for marketing pages. |
| C2 | **Stay on GitHub Pages** (behind the existing Cloudflare proxy, see D2). |
| C3 | Publish **user and operator docs only**: getting started, migration, platform support, technical reference, security & compliance, knowledge boundary, org-mode guides, approval/policy/PII references, connector setup guides. **Not** ADRs, dev/QA docs, or `downloads-and-release-kpi.md`. |
| C4 | The website's docs come from the **latest stable release tag**, not `main`. |
| C5 | **Shrink `README.md`** to ~120–150 lines. |
| C6 | One **canonical product description**, enforced by a **unit test** against README and homepage. |
| C7 | Doc links that leave the published set are **rewritten at build time** to GitHub blob URLs at the same tag. |
| D1 | **Allow all crawlers**, search and training alike. |
| D2 | `privacyfence.eu` is **proxied by Cloudflare; AI-bot blocking is off**. |
| D3 | Google Search Console is set up; **Bing Webmaster Tools is not yet**. |
| D4 | **Cloudflare Web Analytics** (cookieless). |
| D5 | Generate **`llms.txt` and `llms-full.txt`** at build time. |
| D6 | Canonical host is the **apex**, `https://privacyfence.eu`. |
| D7 | Target search intents: **MCP security / gateway**, **secure Claude access to Gmail/Drive/Slack/Salesforce/Jira**, **human-in-the-loop approval for AI agents**, **PII protection before data reaches an LLM**. No compliance-framework (EU AI Act/GDPR) targeting. |
| D8 | Claude designs the **OpenGraph image**. |
| E1 | Pages in scope: `/how-it-works/`, `/security/`, `/enterprise/`, `/connectors/` **plus per-connector pages**, and a visible **`/faq/`**. |
| E2 | Publisher is a **named private person** (the maintainer). |
| E3 | Add a **website privacy policy** and an **imprint**. |
| E4 | Reuse existing screenshots; add a **new SVG architecture/request-flow diagram**. |
| E5 | Contact: GitHub issues **and `info@privacyfence.eu`**. |
| F1 | Plan lives in **this file**. |
| F2 | ADRs for **docs publishing scope**, **crawler policy**, **docs generator + docs version**, **hosting**. |
| F3 | **One PR per wave.** |
| F4 | No release deadline; README changes reach PyPI with whichever stable release comes next. |

## Target site

| URL | Source | Wave |
|---|---|---|
| `/` | `website/index.html` (hand-written) | 0 (meta), 2 (copy) |
| `/how-it-works/` | hand-written + new SVG diagram | 2 |
| `/security/` | hand-written summary; every claim links into `/docs/security-and-compliance/` | 2 |
| `/enterprise/` | hand-written local-vs-organization comparison | 2 |
| `/connectors/` | hand-written overview | 2 |
| `/connectors/google-workspace/`, `/slack/`, `/salesforce/`, `/jira-confluence/`, `/telegram/` | hand-written | 3 |
| `/faq/` | hand-written | 3 |
| `/download/` | existing page, current stable **pre-rendered** at build | 1 |
| `/docs/…` | rendered from `docs/*.md` **at the latest stable tag**, allowlisted | 1 |
| `/privacy/`, `/imprint/` | hand-written | 0 |
| `/robots.txt`, `/sitemap.xml` | static in wave 0, generated from wave 1 | 0 → 1 |
| `/llms.txt`, `/llms-full.txt` | generated | 1 |

Google Workspace is one page because it is one OAuth setup and one mental model for a buyer
(Gmail, Drive/Docs/Sheets, Calendar, Contacts, Tasks, Apps Script); Jira and Confluence share one
because they share `docs/atlassian-setup.md`.

## Guardrails

The external review's core finding is drift: README, PyPI, website and docs describing four
slightly different products. Every wave adds the test that stops its own kind of drift, in the
style of `tests/unit/test_website_download_cta.py` (static, no browser, runs everywhere):

1. **Canonical description** (wave 0): `website/canonical-description.md` holds the 2–3 paragraph
   product description. A unit test asserts README.md and `website/index.html` both contain it
   (whitespace-normalized, tags stripped). PyPI inherits it via README.
2. **Docs allowlist is exhaustive** (wave 1): every `docs/*.md` is either in the site's nav or in
   the test's explicit `INTERNAL_DOCS` set. A new doc cannot land without someone deciding which.
3. **No broken links** (wave 1): the docs build runs strict; a small link checker walks the built
   `_site/` for every internal `href` (marketing pages included).
4. **Every website file is deployed** (wave 1): the existing "listed in the copy step" guard moves
   to the build script's explicit manifest. Nothing reaches the public site by glob.
5. **Connector pages match connectors** (wave 3): every module in `src/privacyfence/connectors/` maps
   to exactly one connector page and one README table row.
6. **Clients are data** (wave 2): the supported-clients list (B6) lives in one file used by the
   homepage, `/faq/` and JSON-LD; a test asserts no page hard-codes a client name outside it.
7. **No claims ADR 0025 rules out** (wave 2): a test fails if any website page contains
   "certified", "compliant with", "SLA" or "guarantee" outside the `/security/` limitations
   section.

Also: no third-party resources on the site apart from the Cloudflare Web Analytics beacon — fonts,
images and scripts stay self-hosted, as today. A privacy gateway's own site loading Google Fonts
would be an easy criticism.

## Canonical product description (draft for review)

> PrivacyFence is an open-source privacy and approval gateway between AI assistants and your
> business systems. It connects MCP-compatible assistants such as Claude Desktop and claude.ai to
> Gmail, Google Drive, Calendar, Slack, Salesforce, Jira, Confluence, Telegram and more, and decides
> independently of the AI what the assistant may see and do: sensitive reads and consequential
> actions wait for a person's approval, routine requests can be automated by policy, optional PII
> detection runs locally before personal data reaches the AI, and every decision is audited.
>
> It runs on an employee's own computer (macOS, Windows, Linux) or as a central deployment on
> infrastructure the organization controls. Connector credentials stay with PrivacyFence, never with
> the AI client, and no data passes through PrivacyFence-operated servers — there are none.

Homepage: `<title>PrivacyFence — privacy and approval gateway for AI assistants (MCP)</title>`,
H1 "The approval gateway between AI assistants and your business systems.", tagline
"Give AI assistants access. Not authority."

This replaces the README's current opener ("a local enterprise AI governance layer"), which is
wrong for organization mode and uses the category B1 did not pick.

## Wave 0 — correctness and crawlability

No new pages beyond the two legal ones; everything here is small and safe to ship first.

**Repo changes (one PR):**

- README drift fixes:
  - opener → the canonical description;
  - "open-source macOS/Linux implementation" (Status and scope) → macOS/Windows/Linux;
  - Quick start's "ask that client to open PrivacyFence for you" → the companion-first flow the
    platform steps below it already describe (install, open the companion's Settings, authenticate
    there; the MCP client is told to send you to the companion, never hands you a sign-in link).
- `website/canonical-description.md` + guardrail 1.
- `website/index.html` and `website/download/index.html`: `<link rel="canonical">` (apex),
  OpenGraph and Twitter card tags, `og:image` → `assets/og.png` (1200×630, designed per D8).
- JSON-LD on the homepage: `SoftwareApplication` (name, description = canonical first sentence,
  `applicationCategory: SecurityApplication`, `operatingSystem: macOS, Windows, Linux`,
  `license: Apache-2.0`, `offers.price: 0`, `downloadUrl`, `sameAs`: GitHub, PyPI), `publisher` as
  `Person` (E2), plus `WebSite`. `softwareVersion` is added in wave 1, when the build knows it.
- `website/robots.txt`: `User-agent: *` / `Allow: /` / `Sitemap: https://privacyfence.eu/sitemap.xml`
  (D1). Static `website/sitemap.xml` for the four existing pages.
- `/privacy/`: what the site and its infrastructure record — GitHub Pages hosting, the Cloudflare
  proxy, Cloudflare Web Analytics (cookieless, aggregate), the downloads Worker (per-day counters
  by version/platform only; no IP, cookie, or User-Agent — `cloudflare/downloads/migrations/0001_download_counts.sql`).
  Contact `info@privacyfence.eu`.
- `/imprint/`: maintainer's name, postal address, `info@privacyfence.eu`. **Needs the maintainer's
  postal (or service) address** — see [Inputs needed](#inputs-needed-from-the-maintainer).
- Cloudflare Web Analytics beacon on every page, added as a visible `<script>` in the repo rather
  than Cloudflare's automatic edge injection, so the site's source shows everything it runs.
- Footer on both pages: Privacy · Imprint · GitHub · Apache 2.0 · `info@privacyfence.eu`.
- `pages.yml` copy step and its guard test updated for the new files.
- **ADR: crawler and training-bot policy** (allow all; the same content is public on GitHub/PyPI,
  so blocking the site alone protects nothing; Cloudflare's own AI-bot controls stay off).

**Outside the repo (maintainer, ~30 minutes):**

- Cloudflare → Security → Bots: confirm "Block AI bots" off, and **"Managed robots.txt" off** — it
  prepends its own training-crawler disallows to ours, silently contradicting D1.
- Cloudflare → Rules → Redirect Rules: `www.privacyfence.eu/*` → `https://privacyfence.eu/$1`, 301.
- Cloudflare → Analytics → Web Analytics: add the site, copy the beacon token into the PR.
- Bing Webmaster Tools: import the site from Google Search Console; submit the sitemap in both.
- Confirm `info@privacyfence.eu` receives mail (Cloudflare Email Routing or equivalent).
- Record a baseline before merging: last 3 months of Search Console queries/clicks, and the
  current download total and star count.

**Done when:** after deploy, `curl -A "GPTBot"`, `-A "OAI-SearchBot"`, `-A "ClaudeBot"` and
`-A "Googlebot"` against `/`, `/robots.txt` and `/sitemap.xml` all return 200; Rich Results Test
parses the JSON-LD; a link pasted into Slack/LinkedIn shows the OG card.

## Wave 1 — build pipeline and docs site

**Generator choice (first task, half a day):** Material for MkDocs announced maintenance mode, and
its authors point to Zensical (reads `mkdocs.yml`, same theme family) as the successor. Build the
allowlisted docs with Zensical first; fall back to MkDocs + Material, pinned, if it can't render the
current docs faithfully (tables, anchors, admonitions). Record which and why in the ADR. Everything
below is written so the generator is swappable: all transforms happen in our own export step,
before the generator sees the files, and no generator plugins are used.

**Pipeline** — `scripts/build_site.py --out _site`, replacing the inline copy step in `pages.yml`:

1. **Resolve the latest stable tag** (no `a`/`b`/`rc` suffix; reuse `scripts/r2_release.py`'s
   channel logic rather than reimplementing it). Needs `fetch-depth: 0`, like every workflow that
   resolves versions.
2. **Export allowlisted docs at that tag** (`git archive <tag> docs/`), keeping only files in the
   nav plus the images they reference. The nav/allowlist itself comes from `main`; a nav entry
   absent at the tag (a doc newer than the release) is skipped with a warning — it appears with
   the next release, which is exactly C4.
3. **Rewrite links** in the exported Markdown (C7): anything pointing outside the published set
   (`../src/…`, `../SECURITY.md`, `../CHANGELOG.md`, unpublished docs, ADRs) →
   `https://github.com/privacyfence/privacyfence/blob/<tag>/<path>`. About 15 such links today.
4. **Render docs** into `_site/docs/` (strict mode), themed with the website's colors and
   self-hosted fonts. Each page carries a small note: "Documentation for PrivacyFence *X.Y.Z*.
   The development version is on GitHub." plus `TechArticle` and `BreadcrumbList` JSON-LD.
5. **Assemble marketing pages** from `website/`, with shared header/footer partials
   (`website/_partials/`) inlined by the script so navigation can't drift between pages.
6. **Pre-render the download page**: fetch `https://downloads.privacyfence.eu/api/releases/stable`
   (the same endpoint `download.js` uses) and write version, date, filenames and SHA-256 into the
   HTML; `download.js` still refreshes it live. If the API is unreachable the build keeps the
   current placeholders and logs a warning rather than failing the deploy — the 6-hourly scheduled
   run corrects it. Also fill JSON-LD `softwareVersion`.
7. **Generate** `sitemap.xml` (marketing + docs, `lastmod` from git), `robots.txt`, `llms.txt`
   (canonical description + links to the key pages and docs) and `llms-full.txt` (all allowlisted
   docs at the tag, concatenated).
8. Run the internal link checker over `_site/` (guardrail 3).

**CI:**

- `pages.yml`: install the `docs` extra, run the script; add `scripts/build_site.py` and the
  site config to its `paths:` filter. The existing `release: published` → redispatch-to-`main`
  path already rebuilds after every release, which is what refreshes docs to the new tag. Deploy
  stays `main`-only.
- New build-only job (no deploy) on pull requests and on pushes to `main` and `releases/**`, so a
  broken site fails the PR instead of the deploy.
- New optional extra `docs` in `pyproject.toml`, locked in `requirements/docs.lock.txt` and covered
  by the same dependency audit as the other locks.

**Also in this wave:** guardrails 2–4; a `CHANGELOG.md` `[Unreleased]` line ("Documentation is
now published at privacyfence.eu/docs"); `docs/README.md` gains a line saying which docs are
published and where the allowlist lives.

**ADRs:** docs publishing scope (the allowlist, and why ADRs, dev/QA docs and the KPI/Cloudflare
doc stay GitHub-only); docs generator + docs version (stable tag vs `main`, and the rejected
Astro/Docusaurus/custom-renderer options); hosting (stay on GitHub Pages; headers and redirects
come from the Cloudflare proxy, configured outside the repo — listed in the ADR so they aren't
tribal knowledge).

**Done when:** `privacyfence.eu/docs/getting-started/` renders the latest stable's text; view-source
of `/download/` shows the current version without JavaScript; `sitemap.xml` lists docs pages;
submitting it in Search Console and Bing reports no errors.

## Wave 2 — positioning and core pages

All copy drafted by Claude, reviewed by the maintainer in the PR.

- **Homepage** rewrite around B1/B2: title/H1/tagline above; the three "control what AI can see /
  do / keep the decision independent" cards stay (they're good); add a "Works with" strip driven by
  the clients data file (guardrail 6); CTAs: Download · How it works · Security · Organization
  deployment · Source.
- **`/how-it-works/`**: the new SVG diagram — AI client → MCP (the `.mcpb` shim for Claude Desktop, or HTTP
  `/mcp` directly for claude.ai and organization mode) → PrivacyFence (policy → approval → PII check → audit; credentials inside) →
  connectors → services — then one read and one write walked through with the existing
  screenshots (`gmail-read-thread.png`, `sheets-write.png`).
- **`/security/`**: business-language summary following `docs/security-and-compliance.md`'s own
  sections (enforcement boundary, credential isolation, review before disclosure/action, policy
  automation, PII, audit integrity, privilege separation, organization-mode identity), each linking
  its docs anchor; closes with **"What PrivacyFence does not claim"** — ADR 0025 in plain words:
  no certification, no business continuity plan, no SLA, single maintainer, adopt through risk
  acceptance (B3). Guardrail 7 lands here.
- **`/enterprise/`**: local vs organization mode side by side (who runs it, identity, where
  credentials and policy live, platforms), prerequisites, links to the org-mode setup guide and
  operational-readiness doc. Presented as production-ready (B4); the limitations link is in the
  page, not buried.
- **`/connectors/`**: one table per connector — what the AI can read, what is shown for review,
  which writes need approval, what never leaves without approval — summarizing the TECHNICAL_REFERENCE
  privacy matrix and linking to it.
- **README shrink** (C5), to: canonical description · why · 5–6 capabilities · architecture diagram
  · platforms · connectors table · 10-line quick start · links (Download, Docs, Security,
  Organization deployment, Contributing) · status/limitations · license. Install detail moves to
  (already existing) `docs/getting-started.md`; links point to `privacyfence.eu/docs/…`. Before
  deleting any README section, check its content exists in a published doc — the README is the
  only home of some of it today.

**Done when:** every nav link resolves (guardrail 3); the README renders correctly on GitHub; the
maintainer has reviewed every page.

## Wave 3 — connector pages and FAQ

- **Five connector pages** (see [Target site](#target-site)). Each answers one D7 search intent in
  its own words — e.g. "Secure Claude access to Gmail and Google Drive" — with: what the assistant
  can do, what is reviewed and what is automatic, PII behavior for that connector's content types
  (from `docs/file-type-support.md`), a screenshot where one exists, and "Set it up" → that
  connector's setup doc. Guardrail 5.
- **`/faq/`**: visible Q&A, no FAQ schema (no longer produces rich results). Seed questions: Does
  my data go through PrivacyFence's servers? Which AI clients work? What does the AI see before I
  approve? (from `claude-knowledge-boundary.md`) Can routine requests run without approval? Is
  PrivacyFence certified? (ADR 0025) Local or organization mode? Is it free? How do I verify a
  download? (SHA-256 checksums, as `/download/` already explains).
- Optional: IndexNow key file plus a post-deploy ping in `pages.yml`, so Bing picks up changes
  faster. Only if wave 0–2 measurements show slow Bing indexing.

## Measurement

Monthly, against the wave-0 baseline:

- **Downloads**: existing KPI (GitHub + Worker counters; `docs/downloads-and-release-kpi.md`).
- **Search**: Search Console and Bing impressions/clicks for the D7 intents; which pages rank.
- **AI referrals**: Cloudflare Web Analytics referrers `chatgpt.com`, `claude.ai`,
  `perplexity.ai`, `copilot.microsoft.com` — no extra instrumentation needed.
- **Stars**: GitHub.
- **Spot check**: ask ChatGPT, Claude and Perplexity "What is PrivacyFence?" and "How do I install
  PrivacyFence on Windows?" after each wave; note whether answers match the canonical description
  and current install flow.

## ADRs this plan creates

Next free numbers at the time each PR lands (0029+ as of writing; parallel branches may take some).

| ADR | Wave |
|---|---|
| Crawler and training-bot policy: allow all | 0 |
| Which docs are published on privacyfence.eu (allowlist) | 1 |
| Docs generator, and docs built from the latest stable tag | 1 |
| Website hosting: GitHub Pages behind the Cloudflare proxy | 1 |

Positioning (B1) is deliberately not an ADR (F2): it lives in `website/canonical-description.md`
and is easy to change.

## Inputs needed from the maintainer

- **Imprint**: postal (or service) address to publish; confirm the name as it should appear.
- **Cloudflare**: the dashboard steps in wave 0 (bot settings, managed robots.txt, www redirect,
  Web Analytics token); confirm the Pages custom domain is the apex.
- **Mailbox**: confirm `info@privacyfence.eu` delivers.
- **Review**: every page's copy, in its wave's PR.
- **ChatGPT/Gemini**: tell Claude when the release that adds them ships, to update the clients file.

## Retiring this plan

The wave-3 PR deletes this file, after confirming the four ADRs above exist and
`docs/README.md`'s "active plan" sentence is reverted.
