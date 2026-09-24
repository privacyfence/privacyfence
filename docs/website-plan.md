# Website, documentation and SEO plan (privacyfence.eu)

**Active plan.** This file is temporary, per [`README.md`](README.md)'s documentation rules and
`CLAUDE.md` "Decisions, plans and ADRs": it lives here while the work below is open and is deleted
by the PR that lands the last wave, after every decision it records has an ADR (see
[ADRs this plan creates](#adrs-this-plan-creates)).

**Scope: documentation and website only.** The code changes the audit found — product fixes and
legacy/migration-code removal — are in [`product-cleanup-plan.md`](product-cleanup-plan.md). This
plan depends on it: Wave 1 documents the code *after* that plan's phases (see [Waves](#waves)).

It combines three inputs:

- the external "Website & Docs Strategy" review (2026-09-23);
- the maintainer's answers to the follow-up questionnaire (2026-09-23);
- a **source-code audit of every document** in the repo (2026-09-24): each doc's claims checked
  against `src/`, `scripts/`, `installer/`, `debian/`, `mcpb/`, `cloudflare/` and the workflows,
  with file:line evidence. Its findings drive [Wave 1](#wave-1--user-documentation),
  [Wave 2](#wave-2--contributor-documentation) and, for the code, `product-cleanup-plan.md`.

## Contents

- [Decisions](#decisions)
- [Documentation principles](#documentation-principles)
- [Audit summary](#audit-summary)
- [Target documentation set](#target-documentation-set)
- [Target site](#target-site)
- [Guardrails](#guardrails)
- [Canonical product description](#canonical-product-description-draft-for-review)
- [Waves](#waves) — order, dependencies, and each wave's scope
- [Measurement](#measurement)
- [ADRs this plan creates](#adrs-this-plan-creates)
- [Inputs needed from the maintainer](#inputs-needed-from-the-maintainer)
- [Retiring this plan](#retiring-this-plan)

## Decisions

Settled on 2026-09-23 (questionnaire) and 2026-09-24 (doc-set direction). Question IDs are there
only so a later reader can tell a deliberate choice from a default.

| # | Decision |
|---|---|
| A1 | The site serves **enterprise evaluation first**, with a one-click path for individuals (Download stays the primary CTA). |
| A2 | **No commercial offering.** Nothing on the site implies support, SLA or a paid tier. CTAs are Download, Docs, Source. |
| A3 | Success = **installer downloads** (already counted), **search impressions/clicks**, **GitHub stars**. |
| A4 | Minimal maintainer writing time. **Claude drafts all copy and all doc rewrites; the maintainer reviews.** |
| B1 | Category: **"privacy and approval gateway for AI assistants"** (not "AI governance"). |
| B2 | "Give AI assistants access. Not authority." becomes the **tagline**; the H1 names the category. |
| B3 | Limitations (ADR 0025) get a **named section on `/security/`**, linked from `/enterprise/` and the footer. |
| B4 | Organization mode is presented as **production-ready, first-class**. |
| B5 | **English only.** |
| B6 | Name the tested clients — **Claude Desktop and Claude Code** on any install, **claude.ai through organization mode only** (local mode listens on localhost; confirmed 2026-09-24) — and say "MCP-compatible" generally. ChatGPT and Gemini are expected in the next version: the client list is kept in one data file so adding them is a one-line change **when they ship, not before**. |
| C1 | **Python docs generator** (Zensical or MkDocs + Material, chosen in [Wave 3](#wave-3--build-pipeline-and-docs-site)) for `/docs/`; **hand-written HTML** for marketing pages. |
| C2 | **Stay on GitHub Pages** (behind the existing Cloudflare proxy, see D2). |
| C3 | Publish **user and operator docs only** — the [published set](#published-docsprivacyfenceeudocs) below. ADRs, contributor docs and `downloads-and-release-kpi.md` stay GitHub-only. |
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
| D7 | Target search intents: **MCP security / gateway**, **secure Claude access to Gmail/Drive/Slack/Salesforce/Jira**, **human-in-the-loop approval for AI agents**, **PII protection before data reaches an LLM**. No compliance-framework targeting. |
| D8 | Claude designs the **OpenGraph image**. |
| E1 | Pages in scope: `/how-it-works/`, `/security/`, `/enterprise/`, `/connectors/` **plus per-connector pages**, and a visible **`/faq/`**. |
| E2 | Publisher is a **named private person** (the maintainer). |
| E3 | Add a **website privacy policy** and an **imprint**. |
| E4 | Reuse existing screenshots where current; add a **new SVG architecture/request-flow diagram**. (The audit found the two approval screenshots are stale — see [Wave 1](#wave-1--user-documentation).) |
| E5 | Contact: GitHub issues **and `info@privacyfence.eu`**. |
| F1 | Plan lives in **this file**. |
| F2 | ADRs for **docs publishing scope**, **crawler policy**, **docs generator + docs version**, **hosting**. (The upgrade-path ADR G1 needs is created by `product-cleanup-plan.md`.) |
| F3 | **One PR per wave.** |
| F4 | No release deadline; README changes reach PyPI with whichever stable release comes next. |
| G1 | **There are no existing users. The docs describe only the current version** — no upgrade paths, no "as of vX", no "no longer", no migration guide, no history. `migration-guide.md` is deleted after its few non-migration facts are moved. |
| G2 | **Every document is reviewed against the source code** for validity, completeness and clarity, and fixed or merged in Waves 1–2. The goal is a simple, straightforward, coherent doc set for new users. |
| G3 | **Legacy and migration code is removed** (approved 2026-09-24) — implemented by [`product-cleanup-plan.md`](product-cleanup-plan.md), which also carries G4 (Telegram in the PyPI build). The docs describe the uninstall rule that plan implements: removing the package leaves data in the system root; purging deletes it. |

## Documentation principles

These apply to every doc touched by this plan, and become the rules in `docs/README.md`:

1. **Current behavior only.** A doc says what the code does today. History has exactly two homes:
   `CHANGELOG.md` (what changed, per release) and `docs/adr/` (why, and what was rejected). No
   phase names (P9, Phase 4), issue/finding IDs (#428, SEC-09, TST-16, B19, D7), version
   qualifiers (as of 4.2, through 4.1), or "used to / no longer / formerly / now" framing.
2. **One topic, one home.** Each fact lives in one doc; others link to it. The audit found the
   privilege-separation layout explained in four places, org mode in six, and install steps in two.
3. **Written for the reader, not the implementer.** User and operator docs name what people see
   and type (menu items, commands, settings keys, paths), not internal function or class names.
   Contributor detail goes in contributor docs.
4. **Checked against code, and kept that way by tests** where a table can be generated or a claim
   can be asserted (see [Guardrails](#guardrails)).
5. **Every limit and default stated**, with its real value — sizes, timeouts, defaults that differ
   between code and the seeded `settings.yaml`, supported OS versions and architectures.

ADRs are the one exception to principle 1: they are frozen records and are not edited.

## Audit summary

Nine parallel reviews covered all 30 documents (7,700 lines) plus `README.md`, `SECURITY.md` and
`CONTRIBUTING.md`. Every finding cites a doc line and the code that contradicts it. Headline
numbers, then the findings that shape the plan:

| Area | Docs | Wrong/stale claims found | Verdict |
|---|---|---|---|
| Install & README | README, getting-started, migration-guide | ~14 | getting-started becomes the one install doc; README quick start shrinks to links; migration-guide deleted |
| Platform | platform-support, dev-vs-live-setup | ~8 | split: ~120-line user page + contributor packaging doc; ~275 lines of bug history deleted |
| Architecture & tools | TECHNICAL_REFERENCE | ~12 | split into how-it-works, tools reference, configuration reference; history removed |
| Security | security-and-compliance, SECURITY.md, claude-knowledge-boundary | ~8 | rewrite to ~250 lines; knowledge boundary merged in |
| Organization mode | 3 org-mode docs | ~9 + 13 gaps | merge into one deployment guide |
| Approvals & policy | 5 docs | ~10 | merge into one approvals-and-policy guide + 2 appendices |
| Connector setup | 5 guides | ~8 | keep 5, one shared template + one shared "connecting a service" page |
| Contributor process | testing-policy, coding guidelines, release-testing, CONTRIBUTING, docs/README | ~15 | revise; testing-policy cut to ~⅓ |
| QA & release infra | 3 QA docs, downloads KPI, screenshots README | ~10 | merge 3 QA docs; restore the deleted QA seed checklist |

Findings that change the plan's shape:

- **Transitional content is everywhere.** Roughly 150 passages across user docs describe upgrades,
  retired features or implementation phases. G1 removes all of them.
- **Real product bugs, not doc bugs,** were found. The most user-visible: **Apps Script cannot be
  connected from Settings at all**, and **Telegram does not work on a PyPI install**. They are
  fixed by [`product-cleanup-plan.md`](product-cleanup-plan.md)'s P phases.
- **Legacy/migration code** still ships behind the transitional docs (policy v1→v2 conversion,
  deprecated MCP tool aliases, legacy path moves, installer steps that register autostarts only to
  disable them). With no users it is dead weight and is removed by that plan's L phases.
- **Dangling references**: code comments and tests cite doc sections that no longer exist (e.g.
  `qa_fixture_recorder.py` cites a `qa-environment-setup.md` §1–§10 checklist deleted in
  `6de7f7cd`; `web/mcp_tools.py:44` cites a TECHNICAL_REFERENCE section that doesn't exist).
- **The approval screenshots show a deleted UI.** `gmail-read-thread.png` and `sheets-write.png`
  are native macOS windows from a removed script, yet are on the README and the homepage.
- **claude.ai**: local mode listens on localhost only, so claude.ai can reach PrivacyFence only
  through an organization deployment (confirmed, B6). No doc says so today.

## Target documentation set

File names are kept wherever a doc survives, because the repo holds hundreds of links to doc paths
(`testing-policy.md` alone is linked from 55 files). Merged-away docs are deleted and every link to them
is updated in the same PR; guardrail 9 fails the build on any link to a missing doc.

### Published (`docs/` → `privacyfence.eu/docs/`)

| Doc | Built from | What changes |
|---|---|---|
| `getting-started.md` | itself + README quick start + migration-guide's troubleshooting facts | The single install doc. Per platform: install, connect **Claude Desktop** (`.mcpb`), **Claude Code** (with the real per-OS path to `privacyfence-app --print-mcp-token`), and a pointer that **claude.ai** needs an organization deployment; first approval including the **passkey step-up** (on by default, scope `writes_and_pii_reads`); a troubleshooting table (`PENDING USER`, `PENDING SIGNOUT`, `enable --for-user`, "daemon stopped → Start PrivacyFence…"); **uninstall** per platform. Fixes: the shim never starts the daemon on packaged installs; `.deb` runs a system service, not XDG autostart; data lives under the system root, not `~/.privacyfence`; Windows needs a sign-out; macOS postinstall never fails the install. |
| `platform-support.md` | its own lines 1–63 + per-platform essentials | ~120 lines: support matrix with **minimum OS** (macOS 13) and **architectures** (Apple Silicon DMG, x64 Windows, amd64 `.deb`), one data-location table, logs, start/stop/status commands (`launchctl`/`systemctl`/`sc`), Linux dialog dependency, adding a second account. Build internals move to `packaging.md`; "Known open items" (bug history) deleted, the two real open items move to `release-testing.md`. |
| `how-it-works.md` (new) | TECHNICAL_REFERENCE architecture, MCP and meta-tools sections | Daemon, companion, shim vs direct HTTP, how clients get a token (control channel `MINT MCP`, `--print-mcp-token`), the ten meta-tools with their real annotations, unattended sessions. |
| `approvals-and-policy.md` (new) | approval-list-ui-ux (user parts), approval-window-content-reference, always-allow prose, TECHNICAL_REFERENCE auto-accept, file-type-support (user parts), privacy filter | Outline: how requests are gated (auto / review / confirm; the 30 s wait and `approval_pending`) · the approvals list · anatomy of a card (Enter never approves, Esc denies) · the PII check (overrides rules, second confirmation, re-read of own content skips it) · always-allow and policy rules (every matching candidate gets a button; `conditions:` key; `not_shared_drive`; the scope catalogue; the 5-minute same-file window) · privacy filter allow/redact/block (org default `block`, local default `allow`) · file previews with real limits · notifications (`web.notifications.*`, code default `minimal` vs seeded `standard`). |
| `tools-reference.md` (new) | TECHNICAL_REFERENCE connector tables | Every connector tool and its gate, **generated** from `auto_accept.TOOL_TO_GATE` and the catalogue (the audit found the hand-written tables correct today — 114 tools — but their counts already wrong: "41 calls" vs 42, "twenty-five scope types" vs 24 rows with 3 missing). |
| `always-allow-rules-reference.md` | generated (keep) | Appendix. Fix the generator's fixed intro/outro text (`scripts/generate_always_allow_reference.py`): wrong "only the first becomes a button", wrong "widening chips", wrong "writes never get a bare rule", v2 wording, stale links. |
| `pii-detection-keywords.md` | itself | Appendix. Fix: Steuer-IdNr needs the `Steuer` prefix; IBANs with spaces aren't detected; headers aren't scanned; extracted text capped at 20,000 characters. |
| `security-and-compliance.md` | itself + claude-knowledge-boundary | Rewrite to ~250 lines for operators and reviewers: deployment modes, trust boundary, step-up table (with org-mode defaults off), privilege separation (the one layout table other docs link to), recovery, audit integrity, what the AI sees before approval, and **"What PrivacyFence does not claim"** (ADR 0025). Fixes the recovery-code, `mcp_token` location, Linux menu, "four files" and script-path errors. Rationale stays in ADRs. |
| `configuration-reference.md` (new) | `settings.yaml.example`, `daemon_main.py` defaults, `build_org_bundle.py` flags | Every `settings.yaml` key and org-bundle key with type, default, and where code and seeded defaults differ (`web.mcp.enabled`, `web.settings.enabled`, notifications). Includes `file_bridge.max_download_bytes`, CLI setup flags. A test keeps it in sync with `settings.yaml.example`'s keys. |
| `org-mode-setup-guide.md` → title "Organization deployment" | 3 org-mode docs + migration-guide's org step-up facts | One guide, 14 parts: overview · prerequisites (Ubuntu 24.04 or Python ≥ 3.11, `python3-venv`) · service account and install · identity provider · connector apps · signing key and bundle (with its config table) · reverse proxy and TLS (Caddy **and** nginx) · hardened systemd unit · first sign-in and validation · install-wide vs per-user policy · approvals and step-up · file delivery (`agent_links`, `/mcp-files/fetch/<token>`) · operations (backup paths, upgrade, monitoring probe, key rotation, limits: 200 principals, 50 MB uploads, 2000 DCR clients) · troubleshooting. Fixes: settings paths under `authority/`; `/settings` *is* mounted; bind `127.0.0.1`. |
| `connecting-a-service.md` (new) | shared steps of the 5 connector guides | Local and org: where the org-config button is (General page), Authenticate/Reconnect, what each status pill means. No restart needed (connectors swap in live). |
| `google-cloud-setup.md`, `slack-setup.md`, `salesforce-setup.md`, `atlassian-setup.md`, `telegram-setup.md` | themselves | One shared template: what you need · register the app · values table (local redirect, org redirects — every Google `/oauth/callback/<service>` URI, Apps Script included after cleanup P1 — scopes, bundle flags) · build and distribute the bundle · users connect (link) · provider-specific troubleshooting. Fixes: Apps Script path (after cleanup P1), restricted-scope verification, Salesforce My Domain URLs, Telegram credentials present in every install type (after cleanup P4). claude.ai as a client is covered in the organization deployment guide. Note that `build_org_bundle.py` comes from the repo. |
| `README.md` (root) | itself | Shrink (C5); fix the known drift. |
| `SECURITY.md` (root) | itself | Trim to ~50 lines; no-SLA reasoning once; no links to contributor files. |

### Contributor docs (GitHub only)

| Doc | What changes |
|---|---|
| `dev-vs-live-setup.md` | Its central advice is wrong: a source run on a machine with a packaged install refuses to start regardless of port. Rewrite: develop on a separate machine/VM or disable the packaged install; document `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED`. |
| `coding-and-testing-guidelines.md` | Fix `_REGISTRY.reset()`, the "add a connector" steps, per-module-test exceptions; fold unnumbered tail into §§; add shim `npm test`/`typecheck` to §2.7. |
| `testing-policy.md` | Cut to ~⅓: one numbering scheme (layers, not tiers), the facts without phase history. Fix markers, triggers (`releases/**`), playwright in `[test]`. Add `qa-record-fixture.yml`, `dependency-audit.yml`, the Worker `verify` job, `/dod` and `/qa-record`. |
| `release-testing.md` | The single home for manual checks (absorbs testing-policy §3 and the platform open items). Names the real gates: `release.yml` dry run, `pre_release_check.py`, graphical-session coverage. Per-OS numbered steps. |
| `packaging.md` (new) | Build/signing/CI-tier internals from platform-support and TECHNICAL_REFERENCE (Task Scheduler XML, `.pkg` signing, ACLs). |
| `connector-qa.md` (new) | Merges connector-live-check-setup, qa-environment-setup, connector-qa-testing. **Restores the seed-data checklist** deleted in `6de7f7cd` that `qa_fixture_recorder.py` and `qa_environment.yaml.example` still depend on. States once that live credentials exist only on the self-hosted runner (ADR 0019). |
| `downloads-and-release-kpi.md` | Fix `pickPreRelease`, `r2_release.py` subcommands, the stats-visibility rule, alpha downloads counting in the headline; dedupe with CLAUDE.md (this doc keeps the credentials; CLAUDE.md links). |
| `images/screenshots/README.md` | Describes the new approval-screenshot generator (below). |
| `CONTRIBUTING.md`, `docs/README.md` | Small fixes; `docs/README.md` becomes a two-part index (published / contributor) plus the [principles](#documentation-principles). |

### Deleted

`migration-guide.md`, `TECHNICAL_REFERENCE.md`, `approval-list-ui-ux.md`,
`approval-window-content-reference.md`, `file-type-support.md`, `claude-knowledge-boundary.md`,
`org-mode-operational-readiness.md`, `org-mode-download-delivery.md`,
`connector-live-check-setup.md`, `qa-environment-setup.md`, `connector-qa-testing.md` — each only
after its surviving content is in the target above and every link to it is updated.

## Target site

| URL | Source | Wave |
|---|---|---|
| `/` | `website/index.html` (hand-written) | 0 (meta), 4 (copy) |
| `/how-it-works/` | hand-written + new SVG diagram | 4 |
| `/security/` | hand-written summary; every claim links into `/docs/security-and-compliance/` | 4 |
| `/enterprise/` | hand-written local-vs-organization comparison | 4 |
| `/connectors/` | hand-written overview | 4 |
| `/connectors/google-workspace/`, `/slack/`, `/salesforce/`, `/jira-confluence/`, `/telegram/` | hand-written | 5 |
| `/faq/` | hand-written | 5 |
| `/download/` | existing page, current stable **pre-rendered** at build | 3 |
| `/docs/…` | the [published set](#published-docsprivacyfenceeudocs), rendered at the latest stable tag | 3 |
| `/privacy/`, `/imprint/` | hand-written | 0 |
| `/robots.txt`, `/sitemap.xml` | static in wave 0, generated from wave 3 | 0 → 3 |
| `/llms.txt`, `/llms-full.txt` | generated | 3 |

Google Workspace is one connector page because it is one OAuth setup (Gmail, Drive/Docs/Sheets,
Calendar, Contacts, Tasks, Apps Script); Jira and Confluence share one because they share
`atlassian-setup.md`.

## Guardrails

Each is a static unit test in the style of `tests/unit/test_website_download_cta.py` (no browser,
runs everywhere). Each wave adds the ones that protect its own work.

| # | Guardrail | Wave |
|---|---|---|
| 1 | **Canonical description**: `website/canonical-description.md` appears verbatim (whitespace-normalized, tags stripped) in README.md and `website/index.html`. | 0 |
| 2 | **No history in current docs**: published docs and contributor docs (not `adr/`, not `CHANGELOG.md`) contain no phase/finding IDs or version-qualified history — patterns like `Phase \d`, `\bP\d{1,2}\b`, `#\d{3}`, `(SEC\|TST)-\d+`, `(as of\|since\|through\|until) v?\d+\.\d`, `migration guide`. Narrow on purpose; a short allowlist covers legitimate hits. | 1 |
| 3 | **Tools reference is generated**: `tools-reference.md` equals the generator's output (same pattern as `always-allow-rules-reference.md`). | 1 |
| 4 | **Configuration reference covers every key** in `settings.yaml.example` and every `build_org_bundle.py` option. | 1 |
| 5 | **Docs allowlist is exhaustive**: every `docs/*.md` is either in the site nav or in an explicit contributor-only set. | 3 |
| 6 | **No broken links** in the built `_site/` (docs build in strict mode + a link walker for marketing pages). | 3 |
| 7 | **Every website file is deployed** (the existing copy-step guard, moved to the build script's manifest). | 3 |
| 8 | **Connector pages match connectors**: every module in `src/privacyfence/connectors/` maps to one connector page, one setup guide and one README row. | 5 |
| 9 | **No dangling doc references**: every `docs/…md` path mentioned anywhere in the repo (code comments, tests, workflows, CLAUDE.md, other docs) exists. | 1 |
| 10 | **Clients are data**: the supported-clients list lives in one file used by homepage, `/faq/` and JSON-LD. | 4 |
| 11 | **No claims ADR 0025 rules out**: no website page contains "certified", "compliant with", "SLA" or "guarantee" outside the `/security/` limitations section. | 4 |

Also: no third-party resources on the site apart from the Cloudflare Web Analytics beacon — fonts,
images and scripts stay self-hosted, as today.

## Canonical product description (draft for review)

> PrivacyFence is an open-source privacy and approval gateway between AI assistants and your
> business systems. It connects MCP-compatible assistants such as Claude Desktop and Claude Code to
> Gmail, Google Drive, Calendar, Slack, Salesforce, Jira, Confluence, Telegram and more, and decides
> independently of the AI what the assistant may see and do: sensitive reads and consequential
> actions wait for a person's approval, routine requests can be automated by policy, optional PII
> detection runs locally before personal data reaches the AI, and every decision is audited.
>
> It runs on an employee's own computer (macOS, Windows, Linux) or as a central deployment on
> infrastructure the organization controls, which also lets web clients such as claude.ai connect.
> Connector credentials stay with PrivacyFence, never with the AI client, and no data passes through
> PrivacyFence-operated servers — there are none.

Homepage: `<title>PrivacyFence — privacy and approval gateway for AI assistants (MCP)</title>`,
H1 "The approval gateway between AI assistants and your business systems.", tagline
"Give AI assistants access. Not authority."

## Waves

```
Wave 0  crawlability & legal ───────────────────────────────────────┐
product-cleanup-plan.md ──┬─► Wave 1 user docs ─┐                    │
                          │   Wave 2 contributor docs ─┐             │
                          └────────────────────────────┴─► Wave 3 docs site ─► Wave 4 core pages ─► Wave 5 connector pages & FAQ
```

- **Wave 0** is independent and can land first.
- **[`product-cleanup-plan.md`](product-cleanup-plan.md)** changes behavior the docs describe, so
  the doc sections its phases touch are written against the post-cleanup code. Wave 1 may start
  before every phase has merged, but a doc section describing a phase's behavior lands only after
  that phase (or describes the merged behavior and says in its PR which phase it is waiting on).
- **Waves 1 and 2** can run in parallel. Wave 1 must land before Wave 3, because Wave 3 publishes it.
- **Wave 3** renders from the latest *stable tag* (C4), so the new docs appear on the site only
  after a stable release that contains Wave 1. Plan a release between Wave 1 and Wave 3's launch,
  or accept that `/docs/` launches with the first release after Wave 1.

### Wave 0 — crawlability and legal

Unchanged in scope from the first revision, minus the README drift fixes (now in Wave 1):

- `website/canonical-description.md`, guardrail 1, and the README opener replaced with it.
- Homepage and download page: canonical link (apex), OpenGraph/Twitter tags, `og:image`
  (`assets/og.png`, 1200×630, per D8). JSON-LD `SoftwareApplication` (`SecurityApplication`,
  macOS/Windows/Linux, Apache-2.0, price 0, GitHub + PyPI in `sameAs`) with a `Person` publisher,
  plus `WebSite`.
- `robots.txt` (allow all, sitemap line) and a static `sitemap.xml`.
- `/privacy/` (GitHub Pages, Cloudflare proxy, Web Analytics, and the downloads Worker's
  counters-only table) and `/imprint/`.
- Cloudflare Web Analytics beacon as a visible `<script>` in the repo.
- Footer on both pages: Privacy · Imprint · GitHub · Apache 2.0 · `info@privacyfence.eu`.
- `pages.yml` copy step and its guard test updated.
- **ADR: crawler and training-bot policy.**

Outside the repo (maintainer, ~30 min): Cloudflare → Bots: "Block AI bots" off **and "Managed
robots.txt" off**; Redirect Rule `www` → apex (301); Web Analytics token; Bing Webmaster import
from Search Console and sitemap submission in both; confirm `info@privacyfence.eu` delivers;
record the baseline (3 months of Search Console, download total, stars).

Done when: `curl -A` as GPTBot, OAI-SearchBot, ClaudeBot and Googlebot gets 200 on `/`,
`/robots.txt`, `/sitemap.xml`; Rich Results Test parses the JSON-LD; the OG card renders.

### Wave 1 — user documentation

Produces the [published set](#published-docsprivacyfenceeudocs). One PR, drafted by Claude,
reviewed by the maintainer doc by doc.

1. Move every still-current fact out of `migration-guide.md` first (step-up keys and "Turn on"
   button → configuration reference; `--step-up-scope` → org guide; human vs unattested sessions
   and passkey-for-proposed-rules → security; `PENDING USER` and `--for-user` → getting-started
   troubleshooting; unattended `.deb` installs → platform-support). Then delete it.
2. Write the new docs and rewrite the surviving ones per the table, applying every audit finding
   for that doc. Where `product-cleanup-plan.md` changes behavior, describe the post-change
   behavior.
3. Delete the merged-away docs; update every link to them across the repo (code comments, tests,
   workflows, CLAUDE.md) — guardrail 9 proves none are left.
4. Add the tools-reference generator (guardrail 3), configuration-reference test (4), history
   lint (2) and dangling-reference test (9).
5. `docs/README.md` becomes the two-part index plus the documentation principles.
6. **Approval screenshots regenerated from the web UI** by a script (extend
   `qa_readme_screenshots.py`), replacing `gmail-read-thread.png` and `sheets-write.png` — native
   windows from the deleted `qa_popup_smoke.py` (`docs/images/screenshots/`, `pages.yml:71-72`).
   Needed by README, homepage and Wave 4.
7. README drift fixes that Wave 0 didn't take (quick start → links, "macOS/Linux implementation",
   "ask that client to open PrivacyFence for you", "one persistent macOS daemon", Windows sign-out,
   packaged script paths); README shrink (C5) happens in Wave 4 once `/docs/` URLs exist.

Done when: every published doc has been read end to end by the maintainer; guardrails 2, 3, 4, 9
pass; a fresh install on each platform can be completed using only `getting-started.md` (this is
a manual check — record it in `release-testing.md`).

### Wave 2 — contributor documentation

The [contributor table](#contributor-docs-github-only) above, one PR. Includes restoring the QA
seed-data checklist from `6de7f7cd^` (updated to the current recorder), fixing the dangling
references in `qa_fixture_recorder.py`, `qa_environment.yaml.example`,
`qa_authenticate_connectors.py` and `tests/unit/test_gate_real_evaluator.py`, and deduplicating
CLAUDE.md against `downloads-and-release-kpi.md` and `testing-policy.md` (CLAUDE.md keeps the
process, the docs keep the facts, each links to the other).

### Wave 3 — build pipeline and docs site

As in the first revision:

- **Generator choice first (half a day):** Zensical, falling back to pinned MkDocs + Material;
  all transforms happen in our own export step so the generator stays swappable.
- **`scripts/build_site.py --out _site`**: resolve the latest stable tag (reusing
  `r2_release.py`'s channel logic; `fetch-depth: 0`) · export allowlisted docs at that tag ·
  rewrite outbound links to GitHub blob URLs at that tag · render docs (strict) with the site's
  theme, a version note, `TechArticle`/`BreadcrumbList` JSON-LD · assemble marketing pages from
  shared header/footer partials · pre-render `/download/` from
  `https://downloads.privacyfence.eu/api/releases/stable` (warn, don't fail, if unreachable) and
  fill JSON-LD `softwareVersion` · generate `sitemap.xml`, `robots.txt`, `llms.txt`,
  `llms-full.txt` · run the link walker.
- **CI:** `pages.yml` runs the script (the existing release → redispatch-to-`main` path refreshes
  docs after each release); a new build-only job on PRs and on `main`/`releases/**` pushes;
  `docs` extra with `requirements/docs.lock.txt`, audited like the other locks.
- Guardrails 5–7; a `CHANGELOG.md` line; **ADRs:** docs publishing scope, docs generator + docs
  version, hosting.

Done when: `/docs/getting-started/` shows the latest stable's text; `/download/` shows the current
version without JavaScript; the sitemap lists docs pages and both search consoles accept it.

### Wave 4 — positioning and core pages

- **Homepage** rewrite (B1/B2): the three "see / do / independent decision" cards stay; a
  "Works with" strip from the clients data file (guardrail 10); CTAs Download · How it works ·
  Security · Organization deployment · Source.
- **`/how-it-works/`**: the SVG diagram — AI client → MCP (the `.mcpb` shim for Claude Desktop, or
  HTTP `/mcp` directly for Claude Code and organization mode) → PrivacyFence (policy → approval →
  PII check → audit; credentials inside) → connectors → services — then one read and one write
  walked through with the regenerated screenshots (Wave 1).
- **`/security/`**: business-language summary following `security-and-compliance.md`'s sections,
  each linking its anchor; closes with "What PrivacyFence does not claim" (ADR 0025).
  Guardrail 11.
- **`/enterprise/`**: local vs organization mode side by side, prerequisites, links to the
  organization deployment guide.
- **`/connectors/`**: per connector, what the AI can read, what is reviewed, which writes need
  approval — summarizing `tools-reference.md` and linking to it.
- **README shrink** (C5): canonical description · why · 5–6 capabilities · diagram · platforms ·
  connectors table · 10-line quick start · links to `privacyfence.eu/docs/…` · limitations ·
  license.

### Wave 5 — connector pages and FAQ

- **Five connector pages**, each answering one D7 search intent in its own words, with what the
  assistant can do, what is reviewed vs automatic, PII behavior for that connector's content, a
  screenshot where one exists, and "Set it up" → its setup guide. Guardrail 8.
- **`/faq/`** (visible Q&A, no FAQ schema): Does my data go through PrivacyFence's servers? Which
  AI clients work? What does the AI see before I approve? Can routine requests run without
  approval? Is PrivacyFence certified? Local or organization mode? Is it free? How do I verify a
  download?
- Optional: IndexNow key file and a post-deploy ping, only if Bing indexing proves slow.

## Measurement

Monthly, against the Wave 0 baseline: downloads (existing KPI), Search Console and Bing
impressions/clicks for the D7 intents, AI referrals in Cloudflare Web Analytics (`chatgpt.com`,
`claude.ai`, `perplexity.ai`, `copilot.microsoft.com`), GitHub stars. After each wave, ask ChatGPT,
Claude and Perplexity "What is PrivacyFence?" and "How do I install PrivacyFence on Windows?" and
note whether the answers match the canonical description and the current install flow.

## ADRs this plan creates

Next free numbers when each PR lands (ADR 0029 is taken; 0030+ as of writing).

| ADR | Wave |
|---|---|
| Crawler and training-bot policy: allow all | 0 |
| Which docs are published on privacyfence.eu | 3 |
| Docs generator, and docs built from the latest stable tag | 3 |
| Website hosting: GitHub Pages behind the Cloudflare proxy | 3 |

Positioning (B1) and the documentation principles are not ADRs: the first lives in
`website/canonical-description.md`, the second in `docs/README.md`.

## Inputs needed from the maintainer

- **Imprint**: postal (or service) address and the name as it should appear.
- **Cloudflare**: the Wave 0 dashboard steps; confirm the Pages custom domain is the apex.
- **Mailbox**: confirm `info@privacyfence.eu` delivers.
- **Review**: every doc (Waves 1–2) and every page (Waves 0, 4, 5) in its PR.
- **ChatGPT/Gemini**: say when the release that adds them ships.

## Retiring this plan

The Wave 5 PR deletes this file after confirming the ADRs above exist and `docs/README.md` no
longer names an active plan.
