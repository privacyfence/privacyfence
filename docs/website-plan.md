# Website, documentation and SEO plan (privacyfence.eu)

**Active plan.** This file is temporary, per [`README.md`](README.md)'s documentation rules and
`CLAUDE.md` "Decisions, plans and ADRs": it lives here while the work below is open and is deleted
by the PR that lands the last wave, after every decision it records has an ADR (see
[ADRs this plan creates](#adrs-this-plan-creates)).

**Scope: documentation and website only.** The code changes the audit found were done by the
product cleanup plan, now retired (merged 2026-09-24, first shipped in `v4.5.0a1`; see [What the
product cleanup changed](#what-the-product-cleanup-changed)).

**How to read it.** The plan is written to be run by an orchestrating session that starts one
Claude Code session per wave. [Start here](#start-here) has everything a person has to do or
decide: open questions, manual steps outside the repo, and how the sessions are run. Everything
below it is the reference the sessions work from. Each [wave](#waves) is a self-contained brief:
what it owns, what it must not touch, what it waits for, and when it is done.

Inputs: the external "Website & Docs Strategy" review (2026-09-23); the maintainer's
questionnaire answers (2026-09-23); a source-code audit of every document (2026-09-24, see
[Audit summary](#audit-summary)); the maintainer's responsive-layout, analytics and #365 decisions
(2026-09-25).

## Contents

- [Start here](#start-here) — open questions, manual steps, running it as sessions
- [Decisions](#decisions)
- [Documentation principles](#documentation-principles)
- [Audit summary](#audit-summary)
- [Target documentation set](#target-documentation-set)
- [Target site](#target-site)
- [Responsive layout](#responsive-layout)
- [Analytics](#analytics-google-analytics-4-behind-consent)
- [Guardrails](#guardrails)
- [Canonical product description](#canonical-product-description-draft-for-review)
- [Waves](#waves)
- [Release history page](#release-history-page-releases-365)
- [Measurement](#measurement)
- [ADRs this plan creates](#adrs-this-plan-creates)
- [Retiring this plan](#retiring-this-plan)

## Start here

### Open questions

Answer these in the orchestrating session. It passes the answers to the wave sessions. A wave can
start before its questions are answered; it drafts with the default and cannot merge until the
question is answered. The PR says which placeholders are waiting.

| # | Question | Blocks | Default if unanswered |
|---|---|---|---|
| Q1 | Approve the [canonical product description](#canonical-product-description-draft-for-review), homepage `<title>` and H1, or edit them. | Wave 0 merge | The draft as written. |
| Q2 | **Imprint:** your name as it should appear, and a postal or service address. | Wave 0 merge | None. `/imprint/` cannot ship without it. |
| Q3 | **GA4 measurement ID** (`G-…`, from M4). | GA going live, not the Wave 0 merge | The consent banner and GA wiring ship with the ID empty and GA disabled; the banner is not shown until an ID is set. Setting the ID later is a one-line PR. |
| Q4 | **Repository description** for GitHub's About box (M6). Proposed: *"Open-source privacy and approval gateway for AI assistants (MCP): human approval, local PII checks and audit for Gmail, Drive, Slack, Salesforce, Jira and more."* | Nothing in the repo | The proposal. |
| Q5 | **Cut 4.5.0 stable once Wave 1 has merged?** `/docs/` publishes from the latest stable tag (C4), so the new docs go live only with a stable release that contains them. | `/docs/` going live (Wave 3 can merge without it, see its brief) | Yes: 4.5.0 is the first stable after Wave 1. |
| Q6 | **Wave 1 as one PR (F3) or three?** One PR is ~15 docs for one review. Three (1a install + platform + how it works; 1b security + organization deployment + configuration; 1c approvals + tools + connector guides) can run as three parallel sessions, each reviewed separately. | How Wave 1 is started | One PR, as decided in F3. The session may use sub-agents internally. |
| Q7 | **When do ChatGPT and Gemini support ship?** | Nothing. B6 adds them to the clients data file in the release that ships them. | Not before they ship. |

Wording you review in the PRs, not questions: the privacy policy and consent banner (Wave 0), every
doc (Waves 1–2), every page (Waves 0, 4, 5).

### Manual steps (outside the repo)

Only the maintainer can do these: they need dashboards, accounts or a tag push. A session never
blocks waiting for one silently. If a step is missing, it says which one in its PR.

| # | When | Step |
|---|---|---|
| M1 | Before Wave 0 merges | **Cloudflare → Bots:** "Block AI bots" off, and "Managed robots.txt" off (D1, D2). |
| M2 | Before Wave 0 merges | **Cloudflare → Web Analytics:** off for `privacyfence.eu`, including automatic setup. Otherwise Cloudflare injects its beacon at the edge, alongside GA ([Analytics](#analytics-google-analytics-4-behind-consent)). |
| M3 | Before Wave 0 merges | **Cloudflare:** Redirect Rule `www` → apex (301). Confirm the GitHub Pages custom domain is the apex (D6). |
| M4 | Before GA goes live (Q3) | **Google Analytics:** create the GA4 property and web stream. Set data retention to 2 months, and turn off Google Signals, ads personalization and data sharing. Send the measurement ID (Q3). |
| M5 | Before Wave 0 merges | **Mailbox:** confirm `info@privacyfence.eu` delivers (E5). |
| M6 | Any time | **GitHub → About:** set the description (Q4), website `https://privacyfence.eu`, and topics `mcp`, `mcp-server`, `claude`, `privacy`, `human-in-the-loop`, `pii`, `ai-security`. |
| M7 | After Wave 0 deploys | **Search Console:** submit `https://privacyfence.eu/sitemap.xml` and link the GA4 property. |
| M8 | After Wave 0 deploys | **Baseline:** record 3 months of Search Console impressions and clicks, the download total and the star count ([Measurement](#measurement)). |
| M9 | After Wave 0 deploys | **Live checks:** Wave 0's post-deploy "Done when" list: bot user agents, Rich Results Test, OG card, GA Realtime only after "Accept", and one real phone. |
| M10 | Every wave | **Review and merge** each wave's PR. |
| M11 | After Wave 1 merges | **Fresh install per platform** using only `getting-started.md`. Record it in `release-testing.md`'s manual checks. |
| M12 | Before the next stable release | **Windows uninstaller:** click through the "Delete PrivacyFence data" checkbox, which has only been compiled in CI ([#674](https://github.com/privacyfence/privacyfence/pull/674)). |
| M13 | After Wave 1 merges (Q5) | **Cut the stable release** through `/cut-release` or the Actions tab (`release.yml`). A Claude Code on the web container cannot push tags. |
| M14 | After Wave 3 deploys and M13 | Check that `/docs/getting-started/` shows the released text, and that Search Console accepts the regenerated sitemap. |
| M15 | Monthly | [Measurement](#measurement), and [#365's gate check](#release-history-page-releases-365) (`curl -s https://downloads.privacyfence.eu/api/stats/downloads`). |

### Running it as sessions

```
        ┌─ S0  Wave 0  crawlability, legal, analytics, responsive base ─┐
now ────┼─ S1  Wave 1  user docs ───────────────────────────────────────┼─► S3 Wave 3 ─► S4 Wave 4 ─► S5 Wave 5
        └─ S2  Wave 2  contributor docs ────────────────────────────────┘        │
                                                              (#365 gate) ───────┴─► S6 /releases/
```

| Session | Brief | Starts when | Suggested branch |
|---|---|---|---|
| S0 | [Wave 0](#wave-0--crawlability-legal-analytics-responsive-base) | now | `feature/website-wave-0-foundation` |
| S1 | [Wave 1](#wave-1--user-documentation) | now | `chore/docs-wave-1-user-docs` |
| S2 | [Wave 2](#wave-2--contributor-documentation) | now | `chore/docs-wave-2-contributor-docs` |
| S3 | [Wave 3](#wave-3--build-pipeline-and-docs-site) | S0, S1 and S2 merged | `feature/website-wave-3-docs-site` |
| S4 | [Wave 4](#wave-4--positioning-and-core-pages) | S3 merged | `feature/website-wave-4-core-pages` |
| S5 | [Wave 5](#wave-5--connector-pages-and-faq) | S4 merged | `feature/website-wave-5-connectors-faq` |
| S6 | [`/releases/`](#release-history-page-releases-365) | S3 merged **and** #365's gate open | `feature/website-releases-page` |

If the session environment assigns its own branch name, use that one instead. Branch names
follow CLAUDE.md's `<type>/<kebab-case>` rule.

**The orchestrator:**

- starts each session with the prompt below, pasting in the answers to the open questions so far;
- keeps status in a tracking issue (one checkbox per session, Q and M item), not in this file;
- passes a new answer to every running session it affects;
- after each merge, reads the PR's "For later waves" list. When an item changes a later brief, it
  folds it into this plan with a small plan-update PR before starting that session.

**Rules every session follows,** so three parallel sessions don't collide:

- **Branch from current `origin/main`**, and bring `main` in with a merge, never a rebase.
- **Stay inside the brief's "Owns" list.** Anything else found along the way goes into the PR's
  "For later waves" list, not into the diff. The shared files have one owner each:

  | File | Owner | Others |
  |---|---|---|
  | `README.md` | S1 (drift fixes), later S4 (shrink) | S0 replaces only the opening description paragraph. S1 merges `main` after S0 lands and keeps it. |
  | `docs/README.md` | S1 (published index + principles) | S2 adds the contributor half. If S1 has not merged yet, S2 merges `main` again before merging and resolves the conflict. |
  | `CLAUDE.md` | S2 (dedupe) | S1 changes only links to docs it deletes. |
  | `website/**`, `.github/workflows/pages.yml` | S0, then S3, S4, S5, S6 in turn | S1 and S2 never touch them. |
  | `CHANGELOG.md` `[Unreleased]` | everyone | Add your own lines. Conflicts are line merges. |
  | `docs/website-plan.md` | orchestrator | Sessions never edit it; the S5 PR deletes it. |

- **ADR numbers:** take the next free number when opening the PR. If another PR takes it first,
  renumber the file, its heading and `docs/adr/README.md`'s index in your PR.
- **Guardrails are new test files,** one per guardrail (`tests/unit/test_docs_*.py`,
  `tests/unit/test_website_*.py`, `tests/integration/test_website_*.py`), so parallel sessions do
  not edit the same test.
- **Questions go to the orchestrator, not into guesses.** An unanswered question means use the
  default in the table above and name the placeholder in the PR.
- **Done means** the brief's "Done when (in the PR)" list plus
  [`coding-and-testing-guidelines.md` §2.7](coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo);
  then drive the PR to green. The "after merge" items are the maintainer's M steps.

**Session prompt** (the orchestrator fills in the angle brackets):

```text
Run <Wave N | the /releases/ page> of docs/website-plan.md on main.
Read CLAUDE.md, then in the plan: "Start here" (the session rules), Decisions,
Documentation principles, Guardrails, and your brief (<link>) plus every section
it links to. Your brief's "Owns" list is your whole scope; anything else goes in
the PR's "For later waves" list.
Branch: <branch> from current origin/main.
Answers so far: <Q1: …, Q2: …, or "none, use the defaults">.
Open one PR. Its description has: what was done, anything in the brief not done
and why, placeholders waiting on open questions, and "For later waves".
Then drive the PR to green.
```

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
| C3 | Publish **user and operator docs only** — the [published set](#published-docs--privacyfenceeudocs) below. ADRs, contributor docs and `downloads-and-release-kpi.md` stay GitHub-only. |
| C4 | The website's docs come from the **latest stable release tag**, not `main`. |
| C5 | **Shrink `README.md`** to ~120–150 lines. |
| C6 | One **canonical product description**, enforced by a **unit test** against README and homepage. |
| C7 | Doc links that leave the published set are **rewritten at build time** to GitHub blob URLs at the same tag. |
| D1 | **Allow all crawlers**, search and training alike. |
| D2 | `privacyfence.eu` is **proxied by Cloudflare; AI-bot blocking is off**. |
| D3 | **Google only for search tooling**: Google Search Console (already set up) is the one search console. No Bing Webmaster Tools and no IndexNow (changed 2026-09-25; see [Analytics](#analytics-google-analytics-4-behind-consent)). |
| D4 | **Google Analytics 4** for site analytics, loaded **only after the visitor consents**, linked to Search Console (changed 2026-09-25 from Cloudflare Web Analytics; see [Analytics](#analytics-google-analytics-4-behind-consent)). |
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
| F2 | ADRs for **docs publishing scope**, **crawler policy**, **docs generator + docs version**, **hosting**. (The upgrade-path ADR G1 needs is [ADR 0041](adr/0041-only-the-current-install-layout-is-supported.md).) |
| F3 | **One PR per wave.** |
| F4 | No release deadline; README changes reach PyPI with whichever stable release comes next. |
| G1 | **There are no existing users. The docs describe only the current version** — no upgrade paths, no "as of vX", no "no longer", no migration guide, no history. `migration-guide.md` is deleted after its few non-migration facts are moved. |
| G2 | **Every document is reviewed against the source code** for validity, completeness and clarity, and fixed or merged in Waves 1–2. The goal is a simple, straightforward, coherent doc set for new users. |
| G3 | **Legacy and migration code is removed** (approved 2026-09-24) — implemented (ADR 0041; G4, Telegram in the PyPI build, is ADR 0040). The docs describe the uninstall rule: `uninstall` / removing the package leaves data in the system root; `uninstall --purge` / purging deletes it ([ADR 0042](adr/0042-uninstall-replaces-disable.md)). |
| H1 | **Every page is responsive and well aligned on desktop and mobile** (added 2026-09-25): the marketing pages, `/download/` and the rendered `/docs/` alike, from a 320 px phone to a wide desktop. |
| H2 | **No CSS framework** for the marketing pages: the existing self-hosted `styles.css` is rebuilt into a small token + layout-primitive system on plain modern CSS; `/docs/` gets responsiveness from its generator's theme (C1). Reasoning and rejected alternatives in [Responsive layout](#responsive-layout). |
| H3 | Responsiveness is **enforced by a browser test** (guardrail 12) from **Wave 0**, so every page added later is born under it. |

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
  connected from Settings at all**, and **Telegram does not work on a PyPI install**. Both are
  fixed ([#662](https://github.com/privacyfence/privacyfence/pull/662), [#664](https://github.com/privacyfence/privacyfence/pull/664)).
- **Legacy/migration code** still ships behind the transitional docs (policy v1→v2 conversion,
  deprecated MCP tool aliases, legacy path moves, installer steps that register autostarts only to
  disable them). With no users it was dead weight and has been removed ([#670](https://github.com/privacyfence/privacyfence/pull/670)–[#674](https://github.com/privacyfence/privacyfence/pull/674)).
- **Dangling references**: code comments and tests cite doc sections that no longer exist (e.g.
  `qa_fixture_recorder.py` cites a `qa-environment-setup.md` §1–§10 checklist deleted in
  `6de7f7cd`; `web/mcp_tools.py:44` cites a TECHNICAL_REFERENCE section that doesn't exist).
- **The approval screenshots show a deleted UI.** `gmail-read-thread.png` and `sheets-write.png`
  are native macOS windows from a removed script, yet are on the README and the homepage.
- **claude.ai**: local mode listens on localhost only, so claude.ai can reach PrivacyFence only
  through an organization deployment (confirmed, B6). No doc says so today.

### Re-checked against `main` after the audit

The audit read `main` as of 4.2.1. By `d89250d2` (2026-09-24) about twenty more PRs had merged —
the policy-surface consolidation (PSC-2…6), agent attribution (AGT-1…6), the Gmail signature
feature, the 4.3.0 release and the 4.4.0 release notes — adding ADRs 0030–0038. None of them
removes a finding above; these change what the target docs must cover:

| Change on `main` | Where the target set absorbs it |
|---|---|
| **Opening PrivacyFence opens Approvals** through the companion on every platform (#642, ADR 0031); `getting-started.md` and `platform-support.md` already describe it. | `getting-started.md` (first run, "icon isn't there"), `platform-support.md`, `how-it-works.md` (companion). |
| **Agent attribution** (#648, #651, ADRs 0035–0037): cards name the calling AI system through one placeholder, the audit log records `agent_source`, org admins pin OAuth clients to AI systems on an *AI systems* settings page, a local override is a relabel that never attests. New sections in `TECHNICAL_REFERENCE.md` ("Which AI system made the request") and `security-and-compliance.md`. | `approvals-and-policy.md` (anatomy of a card), `security-and-compliance.md` (how far to believe the name), `how-it-works.md` (fields), org deployment guide (the *AI systems* page). |
| **Sensitive settings writes need step-up in both modes** (#635, ADR 0034). | `security-and-compliance.md`'s step-up table and the org guide's "approvals and step-up" part: org mode's defaults still differ, but the settings-write gate no longer does. |
| **One route layer and one settings renderer for both modes** (#636, #637, #641, ADRs 0032–0033). | Org guide's `/settings` description: re-derive what org `/settings` shows from the single renderer instead of fixing the old wording line by line. |
| **Deprecated v1 MCP tools deleted** (#633). | `how-it-works.md` lists **eight** meta-tools, not ten; `migration-guide.md`'s last mention is already gone. |
| **Gmail drafts can append the user's signature**, shown on the card but not write-scanned (#658, ADR 0038). | `approvals-and-policy.md` (PII check exceptions) and the generated `tools-reference.md`. |
| **Build pre-flight before tagging** (#632, ADR 0030; `CLAUDE.md` and `/cut-release`). | `release-testing.md`'s "real gates", and Wave 2's `CLAUDE.md` dedupe. |

Re-checked again on 2026-09-25 at `7b61b6c1`: two more PRs merged, neither removes a finding.

| Change on `main` | Where the target set absorbs it |
|---|---|
| **The `.deb` is amd64 only** (#681, ADR 0044, amending ADR 0018): `debian/control` declares `amd64`, `build_deb.sh` refuses other hosts, `platform-support.md`'s matrix already says so. | `platform-support.md` (the architectures column is now correct at the source; keep it when cutting the page to ~120 lines), `getting-started.md`'s Linux section (no arm64 `.deb`; say what an arm64 Linux user can install instead, after checking whether the PyPI install is supported there), `/download/` and the JSON-LD's platform list. |
| **Org mode skips old-format registered OAuth clients** with a per-entry warning instead of converting them (#680). | Nothing to document (G1: no upgrade paths). The org guide's troubleshooting must not mention the old format. |

Re-checked a third time on 2026-09-25 after #682 (Windows/macOS packaged-test stability, eSigner
pin): ADR 0045 (the Windows installer ends its own processes, force-closes the rest) and ADR 0046
(release CI pins CodeSignTool by version and SHA-256). Both are build/installer internals:
`packaging.md` (Wave 2) takes the CodeSignTool pin that `platform-support.md` just gained, and
`release-testing.md` the installer's process handling. No user-doc change.

Two consequences for how the waves run:

- **Branch every wave from current `main`**, not from `claude/documentation-refactoring`, which
  only holds this plan and is behind `main`. Waves 1–2 rewrite docs that keep changing under them;
  each wave PR re-reads its target docs on `main` when it starts.
- **Latest stable is now 4.4.0**, and `v4.5.0a1` (the product cleanup) is the current
  pre-release. Wave 3 still publishes the first stable release that contains Wave 1, so the "plan
  a release between Wave 1 and Wave 3" note below stands: 4.5.0 is the natural candidate if Wave 1
  lands before it is cut.

## Target documentation set

File names are kept wherever a doc survives, because the repo holds hundreds of links to doc paths
(`testing-policy.md` alone is linked from 55 files). Merged-away docs are deleted and every link to them
is updated in the same PR; guardrail 9 fails the build on any link to a missing doc.

### Published (`docs/` → `privacyfence.eu/docs/`)

| Doc | Built from | What changes |
|---|---|---|
| `getting-started.md` | itself + README quick start + migration-guide's troubleshooting facts | The single install doc. Per platform: install, connect **Claude Desktop** (`.mcpb`), **Claude Code** (with the real per-OS path to `privacyfence-app --print-mcp-token`), and a pointer that **claude.ai** needs an organization deployment; first approval including the **passkey step-up** (on by default, scope `writes_and_pii_reads`); a troubleshooting table (`PENDING USER`, `PENDING SIGNOUT`, `enable --for-user`, "daemon stopped → Start PrivacyFence…"); **uninstall** per platform. Fixes: the shim never starts the daemon on packaged installs; `.deb` runs a system service, not XDG autostart; data lives under the system root, not `~/.privacyfence`; Windows needs a sign-out; macOS postinstall never fails the install. |
| `platform-support.md` | its own lines 1–63 + per-platform essentials | ~120 lines: support matrix with **minimum OS** (macOS 13) and **architectures** (Apple Silicon DMG, x64 Windows, amd64 `.deb`), one data-location table, logs, start/stop/status commands (`launchctl`/`systemctl`/`sc`), Linux dialog dependency, adding a second account. Build internals move to `packaging.md`; "Known open items" (bug history) deleted, the two real open items move to `release-testing.md`. |
| `how-it-works.md` (new) | TECHNICAL_REFERENCE architecture, MCP and meta-tools sections | Daemon, companion, shim vs direct HTTP, how clients get a token (control channel `MINT MCP`, `--print-mcp-token`), the eight meta-tools with their real annotations, how the calling AI system is identified (`agent_source`), what opening PrivacyFence does, unattended sessions. |
| `approvals-and-policy.md` (new) | approval-list-ui-ux (user parts), approval-window-content-reference, always-allow prose, TECHNICAL_REFERENCE auto-accept, file-type-support (user parts), privacy filter | Outline: how requests are gated (auto / review / confirm; the 30 s wait and `approval_pending`) · the approvals list · anatomy of a card (the caller's name and whether it is verified; Enter never approves, Esc denies) · the PII check (overrides rules, second confirmation, re-read of own content skips it) · always-allow and policy rules (every matching candidate gets a button; `conditions:` key; `not_shared_drive`; the scope catalogue; the 5-minute same-file window) · privacy filter allow/redact/block (org default `block`, local default `allow`) · file previews with real limits · notifications (`web.notifications.*`, code default `minimal` vs seeded `standard`). |
| `tools-reference.md` (new) | TECHNICAL_REFERENCE connector tables | Every connector tool and its gate, **generated** from `auto_accept.TOOL_TO_GATE` and the catalogue (the audit found the hand-written tables correct today — 114 tools — but their counts already wrong: "41 calls" vs 42, "twenty-five scope types" vs 24 rows with 3 missing). |
| `always-allow-rules-reference.md` | generated (keep) | Appendix. Fix the generator's fixed intro/outro text (`scripts/generate_always_allow_reference.py`): wrong "only the first becomes a button", wrong "widening chips", wrong "writes never get a bare rule", v2 wording, stale links. |
| `pii-detection-keywords.md` | itself | Appendix. Fix: Steuer-IdNr needs the `Steuer` prefix; IBANs with spaces aren't detected; headers aren't scanned; extracted text capped at 20,000 characters. |
| `security-and-compliance.md` | itself + claude-knowledge-boundary | Rewrite to ~250 lines for operators and reviewers: deployment modes, trust boundary, step-up table (with org-mode defaults off, and sensitive settings writes gated in both modes), privilege separation (the one layout table other docs link to), recovery, audit integrity, what the AI sees before approval, which AI system the audit names and how far to trust it, and **"What PrivacyFence does not claim"** (ADR 0025). Fixes the recovery-code, `mcp_token` location, Linux menu, "four files" and script-path errors. Rationale stays in ADRs. |
| `configuration-reference.md` (new) | `settings.yaml.example`, `daemon_main.py` defaults, `build_org_bundle.py` flags | Every `settings.yaml` key and org-bundle key with type, default, and where code and seeded defaults differ (`web.mcp.enabled`, `web.settings.enabled`, notifications). Includes `file_bridge.max_download_bytes`, CLI setup flags. A test keeps it in sync with `settings.yaml.example`'s keys. |
| `org-mode-setup-guide.md` → title "Organization deployment" | 3 org-mode docs + migration-guide's org step-up facts | One guide, 15 parts: overview · prerequisites (Ubuntu 24.04 or Python ≥ 3.11, `python3-venv`) · service account and install · identity provider · connector apps · signing key and bundle (with its config table) · reverse proxy and TLS (Caddy **and** nginx) · hardened systemd unit · first sign-in and validation · install-wide vs per-user policy · AI systems (pinning OAuth clients) · approvals and step-up · file delivery (`agent_links`, `/mcp-files/fetch/<token>`) · operations (backup paths, upgrade, monitoring probe, key rotation, limits: 200 principals, 50 MB uploads, 2000 DCR clients) · troubleshooting. Fixes: settings paths under `authority/`; `/settings` *is* mounted; bind `127.0.0.1`. |
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
| `release-testing.md` | The single home for manual checks (absorbs testing-policy §3 and the platform open items). Names the real gates: the `build.yml` pre-flight (ADR 0030), `release.yml` dry run, `pre_release_check.py`, graphical-session coverage. Per-OS numbered steps. |
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
| `/releases/` | release history ([#365](https://github.com/privacyfence/privacyfence/issues/365)), pre-rendered at build from `/api/releases` | after 3, gated |
| `/docs/…` | the [published set](#published-docs--privacyfenceeudocs), rendered at the latest stable tag | 3 |
| `/privacy/`, `/imprint/` | hand-written | 0 |
| `/robots.txt`, `/sitemap.xml` | static in wave 0, generated from wave 3 | 0 → 3 |
| `/llms.txt`, `/llms-full.txt` | generated | 3 |

Google Workspace is one connector page because it is one OAuth setup (Gmail, Drive/Docs/Sheets,
Calendar, Contacts, Tasks, Apps Script); Jira and Confluence share one because they share
`atlassian-setup.md`.

## Responsive layout

Requirement H1: every page reads and aligns well on a phone, a tablet and a desktop.

### Where the site stands (measured 2026-09-25)

Both pages rendered in headless Chromium at 320, 360, 390, 768, 1024 and 1440 px wide:

- **No page-level horizontal scroll** at any width; the verify-commands `<code>` on `/download/`
  overflows only inside its own `overflow-x: auto` box, which is intended.
- **Sections stack correctly** below 900 px (hero, principle grid, proof sections, privacy and CTA
  cards). The existing `styles.css` already has two breakpoints (900, 560 px) and `clamp()`
  headings.
- **Navigation is the real gap.** Below 900 px every header link except the CTA is
  `display: none`, with no menu to reach them. That holds on tablets (768 px) too. With four
  links it is tolerable; the target site has seven destinations (How it works, Security,
  Enterprise, Connectors, Docs, FAQ, Download), so without a real mobile menu most of the site
  would be unreachable from the header on a phone.
- **Tap targets are small**: the header CTA is 38–40 px and the desktop nav links 22 px high,
  below the 44 × 44 px WCAG 2.5.5 target (the AA-level 2.5.8 minimum is 24 px; we aim for 44).
- **Breakpoints are page-by-page.** The download page adds a third (720 px). Each new page would
  add its own rules to one flat file.

### Decision: no CSS framework (H2)

| Option | Why not |
|---|---|
| **Tailwind CSS** | Needs a build step with a pinned binary or npm toolchain in `pages.yml`, locked and audited like the other dependency locks, for a site of ~15 hand-written pages. Every page's markup is rewritten into utility classes, so the existing design is redone rather than kept. Utility classes in hand-written HTML are harder for a reviewer (A4) to read than semantic class names. |
| **Bootstrap 5** | ~25 KB of CSS for the grid and components we would override to keep the current look, and its collapsing navbar needs Bootstrap's JavaScript. The result looks like every other Bootstrap site. |
| **Pico CSS** / other classless frameworks | Small and JS-free, but they restyle every element. That conflicts with the existing design, and we would spend the effort fighting its defaults rather than using them. |
| **Plain modern CSS (chosen)** | CSS grid with `auto-fit`/`minmax()`, `clamp()` type and spacing, container queries and `:has()` are Baseline in every browser we target, so they do what a framework's grid did without a dependency. It keeps the "no third-party resources" rule and the current visual design, and adds no build step before Wave 3 has one. |

`/docs/` is not affected by this choice: Material for MkDocs and Zensical are responsive out of
the box, with a drawer nav, collapsible TOC and fluid tables. Wave 3's generator choice also checks
mobile nav and wide tables at 360 px. Wave 3 gives the docs theme the site's colour and type
tokens through its `extra_css` hook, so `/docs/` and the marketing pages read as one site.

If the page count or the design effort outgrows this (for example, a designer joins and wants a
component library), revisit it with a new ADR. Tailwind's standalone CLI is the likeliest
successor, because it can run inside Wave 3's `build_site.py` without Node.

### What "responsive" means here

These rules become the stylesheet's header comment. Guardrail 12 checks the measurable ones:

1. **Viewports tested:** 320, 360, 390 (phones), 768 (tablet portrait), 1024 (tablet landscape /
   small laptop), 1440 (desktop). Portrait and landscape are both covered through the widths.
2. **No page-level horizontal scroll** at any tested width
   (`document.documentElement.scrollWidth <= innerWidth`). Wide content (code, tables, diagrams)
   scrolls inside its own container.
3. **Every header destination is reachable at every width**: inline links on desktop, a menu
   button below the nav breakpoint. The menu is a `<details>`/`<summary>` disclosure: it works
   without JavaScript, can be reached and operated by keyboard, and is announced correctly by
   screen readers.
4. **Tap targets ≥ 44 × 44 px** for nav links, buttons and the menu toggle on widths below 1024 px.
5. **One set of layout primitives**, not per-page media queries: `.shell` (centred max-width
   container), `.stack` (vertical rhythm), `.cluster` (wrapping inline group), `.grid-auto`
   (`repeat(auto-fit, minmax(var(--min), 1fr))`), and `.split` (two columns that stack under a
   container-query threshold). The existing section classes (`hero`, `proof-grid`,
   `principle-grid`, `privacy-card`, `cta-card`, `download-grid`) are re-expressed on these, keeping
   their names so the markup barely changes.
6. **Design tokens on `:root`** for colour, type scale (`clamp()`-based), spacing scale, radius and
   the two breakpoints (nav collapse at 900 px, compact spacing at 560 px). Wave 3 shares these
   tokens with the docs theme.
7. **Images and the SVG diagram scale**: `max-width: 100%` with explicit `width`/`height`
   attributes (no layout shift). The Wave 4 architecture diagram has a vertical layout below about
   600 px, either as a second SVG in `<picture>` or with a `viewBox` that reads top-to-bottom,
   rather than being shrunk to an unreadable width.
8. **Text measure ≤ ~75 characters** for body copy on desktop; readable without zoom on a 320 px
   phone (body ≥ 16 px).
9. **`prefers-reduced-motion`** is honoured (already true); **dark mode** is out of scope for this
   plan.

### When it is built

| Wave | Responsive work |
|---|---|
| **0** | Foundation, because Wave 0 already edits both pages' header and footer and adds two pages (`/privacy/`, `/imprint/`). Rebuild `styles.css` into tokens + primitives (rules 5–6) without changing the desktop design; replace the hidden-links header with the `<details>` menu (rule 3); raise tap targets (rule 4); make the new footer links wrap cleanly. Add **guardrail 12** over every page under `website/`. |
| **3** | The header and footer become the shared partials `build_site.py` assembles, carrying the Wave 0 menu over unchanged. The docs theme gets the site tokens (`extra_css`), and the generator trial checks mobile nav and table overflow at 360 px. Guardrail 12 runs on the built `_site/` and takes its page list from the build manifest, including a sample of `/docs/` pages (getting-started, tools-reference for its wide tables, the org guide for its long code blocks). |
| **4–5** | New pages use only the primitives; guardrail 12 covers them automatically through the manifest. The diagram's narrow layout (rule 7) is part of `/how-it-works/`'s definition of done. |

Doing it in Wave 0 rather than bundling it into Wave 4's redesign means the nav problem is fixed
before the page count grows. Every later wave is then checked against the rules instead of
retrofitted to them.

## Analytics: Google Analytics 4 behind consent

Decided 2026-09-25 (D3, D4), replacing Cloudflare Web Analytics and dropping Bing Webmaster
Tools. The maintainer works in the Google ecosystem: Search Console is already set up, so one
vendor is simpler to run than three. GA4 also links to Search Console, which puts search queries
and on-site behavior in one place.

**What this changes, stated plainly so the ADR can record it:**

- **Consent is required.** GA4 sets cookies (`_ga`, `_ga_<id>`) and reads device storage. On an EU
  site that needs prior opt-in consent (ePrivacy Art. 5(3)). Cloudflare Web Analytics set no
  cookie and needed no banner. The site therefore gets a consent banner. It is
  self-hosted, with no third-party consent platform, and offers **Accept** and **Decline** with
  equal weight and nothing pre-selected. The choice is stored in `localStorage` and can be
  reopened from "Cookie settings" in the footer.
- **Nothing reaches Google before "Accept."** The GA snippet is injected only after consent. This
  is Consent Mode's *basic* implementation. The *advanced* mode, which sends cookieless pings
  before consent, is rejected: it is a third-party request the visitor has not agreed to.
  Guardrail 13 tests this.
- **GA4 settings:** Google Signals off, ads personalization and data sharing off, data retention
  2 months, no User-ID, no custom dimensions carrying anything visitor-specific.
- **`/privacy/`** names Google as a processor and lists the cookies and their lifetime, the
  EU–US Data Privacy Framework transfer, the retention period, and how to withdraw consent.
- **Coverage is partial.** GA4 counts only visitors who accept, so it shows trends and referral
  sources, not totals. Downloads keep coming from the Worker's own cookieless counter, which stays
  the headline KPI (A3).
- **Reputational trade-off, accepted.** A privacy product's own site runs Google Analytics. The
  mitigations (consent-only, minimal settings, disclosed on `/privacy/`) are what keep that
  defensible. `/faq/` must not claim the site is tracker-free.
- **Cloudflare's own Web Analytics must stay off.** For proxied sites Cloudflare can inject its
  beacon at the edge, outside the repo, where guardrail 13 cannot see it in the source. The Wave 0
  dashboard steps confirm it is disabled, and Wave 0's done-check fetches the live `/` and asserts
  no `cloudflareinsights` script is present.

**Dropping Bing Webmaster Tools** costs Bing-side reporting and sitemap submission, not
indexing. Bingbot still finds the sitemap through `robots.txt`'s `Sitemap:` line, and D1/D5
(crawlers allowed, `llms.txt`) are unchanged. Bing's index feeds ChatGPT search and Copilot,
so if the Measurement check shows those assistants lagging behind Google-backed answers,
re-adding Bing Webmaster Tools (a 5-minute import from Search Console) is the first remedy.

## Guardrails

Each is a static unit test in the style of `tests/unit/test_website_download_cta.py` (no browser,
runs everywhere). The exception is guardrail 12: layout can only be measured in a rendering engine,
so it is a Playwright test that runs wherever `test_download_page.py` already runs. Each wave adds
the guardrails that protect its own work.

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
| 12 | **Responsive layout** — the one guardrail that needs a browser: `tests/integration/test_website_layout.py` (`integration` + `browser` markers, same setup and skip posture as `test_download_page.py`, network stubbed) loads every page at the six [tested viewports](#what-responsive-means-here). It asserts no page-level horizontal scroll, every header destination reachable (visible, or visible after opening the menu), tap targets ≥ 44 px below 1024 px, and no element's box extending past the viewport outside a scroll container. It saves a full-page screenshot per page × width as a CI artifact, for the maintainer's review. | 0 (source pages), 3 (built `_site/`) |
| 13 | **Nothing third-party before consent**: in a browser test, loading every page with no stored choice makes no request to any origin other than the site itself (and `downloads.privacyfence.eu` on `/download/` and `/releases/`) and sets no cookie; accepting loads `googletagmanager.com`; declining loads nothing, and the choice persists across pages. | 0 |

Also: no third-party resources on the site apart from Google Analytics, which loads only after
consent (see [Analytics](#analytics-google-analytics-4-behind-consent)). Fonts, images, scripts and
the consent banner itself stay self-hosted, as today. Guardrail 13 enforces both halves.

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

The order and the session map are in [Running it as sessions](#running-it-as-sessions). Each brief
below is what its session reads. **Owns** is the whole scope. **Needs** lists the open questions
(Q) and manual steps (M) the PR waits for before merging. **Done when** is split between what the
PR proves and what the maintainer checks after the deploy.

### What the product cleanup changed

The retired product cleanup plan's phases merged 2026-09-24 and first shipped in `v4.5.0a1`. Wave 1
describes this behavior, and nothing earlier (G1):

| PR | Behavior the docs must describe |
|---|---|
| [#662](https://github.com/privacyfence/privacyfence/pull/662) | Apps Script connects from Settings (local) and org `/connect`, like the other Google connectors; org admins register `/oauth/callback/apps_script`. |
| [#663](https://github.com/privacyfence/privacyfence/pull/663) | The recovery-code route audits every attempt, needs a human-attested session on a separated install, and is rate-limited. |
| [#661](https://github.com/privacyfence/privacyfence/pull/661) | `build_org_bundle.py` binds `127.0.0.1` by default; `--agent-links/--no-agent-links`. |
| [#664](https://github.com/privacyfence/privacyfence/pull/664) | Telegram works on a PyPI install (ADR 0040). |
| [#665](https://github.com/privacyfence/privacyfence/pull/665) | Installers refuse an OS/CPU below the support matrix (ADR 0039); `platform-support.md` has the Minimum OS column. |
| [#670](https://github.com/privacyfence/privacyfence/pull/670) | A v1-format `settings.yaml` (`auto_accept_rules`/`auto_accept_grants`) is refused at startup, not converted (ADR 0041). |
| [#671](https://github.com/privacyfence/privacyfence/pull/671) | No legacy file-location moves; the shim reads only the current layout. |
| [#673](https://github.com/privacyfence/privacyfence/pull/673), [#672](https://github.com/privacyfence/privacyfence/pull/672), [#674](https://github.com/privacyfence/privacyfence/pull/674) | `uninstall [--purge]` (`-Purge` on Windows) replaces `disable` on all three platforms (ADR 0042): `apt remove` / `uninstall` keeps data under the system root, `apt purge` / `--purge` deletes it; the Windows uninstaller has a "Delete PrivacyFence data" checkbox; macOS's uninstall is `sudo …/macos_privilege_separation.sh uninstall [--purge]`. The `.deb` ships no daemon XDG autostart entry. |
| [#676](https://github.com/privacyfence/privacyfence/pull/676) | `enable --for-user` never rewrites the install's recorded owner (ADR 0043). |

Doc findings the cleanup PRs recorded and left for Wave 1:

- `security-and-compliance.md`: says a recovery code, "correctly or not, never grants a second
  attempt"; a wrong code does not consume the stored one ([#663](https://github.com/privacyfence/privacyfence/pull/663)).
- `org-mode-setup-guide.md` §4.2: gives one `/oauth/callback/google` redirect URI; the code builds
  one per service ([#662](https://github.com/privacyfence/privacyfence/pull/662)).
- `README.md` ≈l.337: points Windows users at `%LOCALAPPDATA%\Programs\PrivacyFence\`; the
  installer is admin-only ([#671](https://github.com/privacyfence/privacyfence/pull/671)).
- `TECHNICAL_REFERENCE.md`: says the Start Menu entry opens the web settings UI; since ADR 0031 it
  launches the companion ([#674](https://github.com/privacyfence/privacyfence/pull/674)).
- The Windows uninstaller's "Delete PrivacyFence data" checkbox has only been compiled in CI; it is
  in `release-testing.md`'s manual Windows checks and must be clicked through before the next
  stable release ([#674](https://github.com/privacyfence/privacyfence/pull/674)).

### Wave 0 — crawlability, legal, analytics, responsive base

- **Session:** S0 · **Starts:** now · **Parallel with:** S1, S2
- **Needs before merge:** Q1, Q2; M1–M3, M5. (Q3/M4 only for GA to go live.)
- **Owns:** `website/**`, `.github/workflows/pages.yml`, `website/canonical-description.md`, README's
  opening description paragraph (nothing else in README), the Wave 0 guardrail tests, three ADRs.
- **Must not touch:** `docs/**` other than new ADRs and `docs/adr/README.md`.

Deliverables:

- `website/canonical-description.md`, guardrail 1, and the README opener replaced with it.
- Homepage and download page: canonical link (apex), OpenGraph/Twitter tags, `og:image`
  (`assets/og.png`, 1200×630, per D8). JSON-LD `SoftwareApplication` (`SecurityApplication`,
  macOS/Windows/Linux, Apache-2.0, price 0, GitHub + PyPI in `sameAs`) with a `Person` publisher,
  plus `WebSite`.
- `robots.txt` (allow all, sitemap line) and a static `sitemap.xml`.
- `/privacy/` (GitHub Pages, Cloudflare proxy, Google Analytics with what it collects, its
  cookies, retention and the Google transfer, how to withdraw consent, and the downloads Worker's
  counters-only table) and `/imprint/`.
- **Google Analytics 4 behind a self-hosted consent banner** (see
  [Analytics](#analytics-google-analytics-4-behind-consent)): the GA snippet is in the repo, and
  the page injects it only after "Accept". A "Cookie settings" footer link reopens the choice.
  Guardrail 13.
- Footer on both pages: Privacy · Imprint · Cookie settings · GitHub · Apache 2.0 · `info@privacyfence.eu`.
- `pages.yml` copy step and its guard test updated.
- **Responsive foundation** ([When it is built](#when-it-is-built), Wave 0 row): `styles.css`
  rebuilt into tokens + layout primitives with the desktop look unchanged, the `<details>` header
  menu, 44 px tap targets, and **guardrail 12** over `/`, `/download/`, `/privacy/` and `/imprint/`.
- **ADRs: crawler and training-bot policy; website CSS without a framework; website analytics
  (GA4 behind consent, Google-only search tooling).**

The GA4 measurement ID goes in the repo; it is not a secret. The Cloudflare, Google and GitHub
dashboard work is M1–M7.

Done when (in the PR): guardrails 1, 12 and 13 pass, and the existing website tests pass. The PR
links guardrail 12's 360 px and 1440 px screenshots of every page for review.

Done when (after deploy, M9): `curl -A` as GPTBot, OAI-SearchBot, ClaudeBot and Googlebot gets
200 on `/`, `/robots.txt` and `/sitemap.xml`; the Rich Results Test parses the JSON-LD; the OG card
renders; one real phone looks right; GA4's Realtime report shows a visit only after "Accept"; the
served `/` contains no `cloudflareinsights` script.

### Wave 1 — user documentation

- **Session:** S1 (or S1a–c, see Q6) · **Starts:** now · **Parallel with:** S0, S2
- **Needs before merge:** nothing blocking; maintainer review of every doc (M10).
- **Owns:** every doc in the [published set](#published-docs--privacyfenceeudocs), the
  [deleted](#deleted) docs, README (except the opening paragraph S0 owns), `docs/README.md`'s
  published index and principles, `docs/images/screenshots/*.png` and the screenshot script, the
  Wave 1 guardrail tests and generator, and links to deleted docs anywhere in the repo.
- **Must not touch:** contributor docs' content (S2), `website/**`, CLAUDE.md beyond link updates.
- **Re-read on start:** each target doc on `main` when you begin it; they keep changing.

Produces the [published set](#published-docs--privacyfenceeudocs). One PR (F3, unless Q6 splits
it), drafted by Claude, reviewed by the maintainer doc by doc. Deliverables:

1. Move every still-current fact out of `migration-guide.md` first (step-up keys and "Turn on"
   button → configuration reference; `--step-up-scope` → org guide; human vs unattested sessions
   and passkey-for-proposed-rules → security; `PENDING USER` and `--for-user` → getting-started
   troubleshooting; unattended `.deb` installs → platform-support). Then delete it.
2. Write the new docs and rewrite the surviving ones per the table, applying every audit finding
   for that doc, plus the items in [What the product cleanup
   changed](#what-the-product-cleanup-changed).
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

Done when (in the PR): guardrails 2, 3, 4 and 9 pass; every item in the [cleanup
table](#what-the-product-cleanup-changed) and every audit finding for each doc is applied or listed
as not applied, with the reason; the maintainer has read every published doc end to end.

Done when (after merge): M11, a fresh install on each platform using only `getting-started.md`.
Then Q5/M13: the stable release that takes these docs live.

### Wave 2 — contributor documentation

- **Session:** S2 · **Starts:** now · **Parallel with:** S0, S1
- **Needs before merge:** maintainer review (M10).
- **Owns:** every doc in the [contributor table](#contributor-docs-github-only), CLAUDE.md,
  `docs/README.md`'s contributor half, and the dangling references named below.
- **Must not touch:** published docs (S1), `website/**`.

The [contributor table](#contributor-docs-github-only) above, one PR. It includes restoring the QA
seed-data checklist from `6de7f7cd^` (updated to the current recorder), fixing the dangling
references in `qa_fixture_recorder.py`, `qa_environment.yaml.example`,
`qa_authenticate_connectors.py` and `tests/unit/test_gate_real_evaluator.py`, and deduplicating
CLAUDE.md against `downloads-and-release-kpi.md` and `testing-policy.md` (CLAUDE.md keeps the
process, the docs keep the facts, each links to the other). `packaging.md` also takes the
CodeSignTool pin (ADR 0046); `release-testing.md` takes the Windows installer's process handling
(ADR 0045) and M12's checkbox check.

Done when (in the PR): guardrail 2 passes on contributor docs; guardrail 9 passes (if S1 has not
merged yet, for the references this PR fixes); `/dod` passes.

### Wave 3 — build pipeline and docs site

- **Session:** S3 · **Starts:** S0, S1 and S2 merged (the published/contributor split must be
  final for guardrail 5) · **Parallel with:** nothing
- **Needs before merge:** nothing. `/docs/` goes live only after M13 (see the stale-tag guard
  below).
- **Owns:** `scripts/build_site.py`, the docs generator config and theme overrides,
  `website/**` restructured into partials, `pages.yml`, the new PR build job, the `docs` extra and
  its lock, guardrails 5–7 and guardrail 12's move to `_site/`, three ADRs.
- **Must not touch:** doc content. A doc that fails strict rendering is fixed minimally, and the
  fix is named in the PR.

Deliverables:

- **Generator choice first (half a day):** Zensical, falling back to pinned MkDocs + Material;
  all transforms happen in our own export step so the generator stays swappable. The trial
  includes checking mobile nav, the TOC and wide tables at 360 px (H1).
- **Responsive, shared:** the Wave 0 header/menu and footer become the partials; the docs theme
  loads the site's tokens via `extra_css`; guardrail 12 moves to the built `_site/` with its page
  list from the build manifest (see [When it is built](#when-it-is-built)).
- **`scripts/build_site.py --out _site`**: resolve the latest stable tag (reusing
  `r2_release.py`'s channel logic; `fetch-depth: 0`) · export allowlisted docs at that tag ·
  rewrite outbound links to GitHub blob URLs at that tag · render docs (strict) with the site's
  theme, a version note, `TechArticle`/`BreadcrumbList` JSON-LD · assemble marketing pages from
  shared header/footer partials · pre-render `/download/` from
  `https://downloads.privacyfence.eu/api/releases/stable` (warn, don't fail, if unreachable) and
  fill JSON-LD `softwareVersion` · generate `sitemap.xml`, `robots.txt`, `llms.txt`,
  `llms-full.txt` · run the link walker.
- **Stale-tag guard:** if the latest stable tag predates Wave 1 (it has no `docs/how-it-works.md`),
  the build skips `/docs/` and `llms-full.txt` with a warning and leaves those docs links pointing
  at GitHub. Wave 3 can then merge before M13 without publishing the old doc set. The first stable
  release after Wave 1 turns `/docs/` on by itself.
- **CI:** `pages.yml` runs the script (the existing release → redispatch-to-`main` path refreshes
  docs after each release); a new build-only job on PRs and on `main`/`releases/**` pushes;
  `docs` extra with `requirements/docs.lock.txt`, audited like the other locks.
- Guardrails 5–7, and 12 on `_site/`; a `CHANGELOG.md` line; **ADRs:** docs publishing scope, docs generator + docs
  version, hosting.

Done when (in the PR): guardrails 5, 6 and 7 pass, and guardrail 12 passes on `_site/`; the PR
build job produces `_site/` both with a post-Wave 1 tag and with the stale-tag guard active;
`/download/` in the built output shows a version without JavaScript.

Done when (after deploy, M14): `/docs/getting-started/` shows the released text; Search Console
accepts the sitemap with the docs pages in it.

### Wave 4 — positioning and core pages

- **Session:** S4 · **Starts:** S3 merged · **Parallel with:** S6, if its gate is open
- **Needs before merge:** M10 (page review).
- **Owns:** the homepage, `/how-it-works/`, `/security/`, `/enterprise/`, `/connectors/`, the
  clients data file, the SVG diagram, README's shrink, guardrails 10–11.
- **Must not touch:** doc content (link to it; propose changes under "For later waves").

Deliverables:

- **Homepage** rewrite (B1/B2): the three "see / do / independent decision" cards stay; a
  "Works with" strip from the clients data file (guardrail 10); CTAs Download · How it works ·
  Security · Organization deployment · Source.
- **`/how-it-works/`**: the SVG diagram — AI client → MCP (the `.mcpb` shim for Claude Desktop, or
  HTTP `/mcp` directly for Claude Code and organization mode) → PrivacyFence (policy → approval →
  PII check → audit; credentials inside) → connectors → services — then one read and one write
  walked through with the regenerated screenshots (Wave 1). The diagram has a vertical layout
  under about 600 px ([rule 7](#what-responsive-means-here)).
- Every new page is built only from the Wave 0 layout primitives. Guardrail 12 covers it through
  the build manifest.
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

- **Session:** S5 · **Starts:** S4 merged
- **Needs before merge:** M10.
- **Owns:** the five connector pages, `/faq/`, guardrail 8, and this plan's retirement (see
  [Retiring this plan](#retiring-this-plan)).

Deliverables:

- **Five connector pages**, each answering one D7 search intent in its own words, with what the
  assistant can do, what is reviewed vs automatic, PII behavior for that connector's content, a
  screenshot where one exists, and "Set it up" → its setup guide. Guardrail 8.
- **`/faq/`** (visible Q&A, no FAQ schema): Does my data go through PrivacyFence's servers? Which
  AI clients work? What does the AI see before I approve? Can routine requests run without
  approval? Is PrivacyFence certified? Local or organization mode? Is it free? How do I verify a
  download?

Done when (in the PR): guardrail 8 passes, and guardrails 10–13 pass on the new pages; this plan
is deleted and its ADRs exist.

## Release history page (`/releases/`, #365)

- **Session:** S6 · **Starts:** S3 merged **and** both gate conditions below hold (checked monthly,
  M15) · **Parallel with:** S4 or S5
- **Owns:** `website/releases/**`, its entry in `build_site.py`'s manifest and pre-render step,
  the footer and `/download/` links to it. The PR closes #365.

[#365](https://github.com/privacyfence/privacyfence/issues/365) (carried over from the retired
release-publishing KPI plan) joins this plan. The issue stays open and is closed by the PR that
builds the page. Scope as the issue defines it:

- `website/releases/index.html` + `releases.js`, backed by the downloads Worker's existing
  `GET /api/releases` (no Worker change).
- Per release: version, channel, release date, available platforms, a link to its release notes
  (the GitHub Release, whose stable body is `CHANGELOG.md`'s section) and its downloads.
- Every download link points at `downloads.privacyfence.eu`, never R2 or GitHub. Only installers
  are presented as downloads; SBOMs, org-config scripts and the sdist/wheel are not, because the
  KPI counts installers only.
- Built from the API response, never hardcoded filenames, with `download.js`'s degradation paths
  (API down → GitHub Releases link; an empty channel renders nothing).

What this plan adds to the issue:

- **Pre-rendered at build** by Wave 3's `build_site.py`, like `/download/`: the list is readable
  without JavaScript and by crawlers, and `releases.js` refreshes it. It uses the shared
  header/footer partials and layout primitives. Guardrails 12 (responsive; long version tables
  scroll inside their container) and 13 apply. It is listed in `sitemap.xml`.
- **Deployed through the Wave 3 build manifest.** The issue's warning about `pages.yml`'s
  hand-written copy list is resolved there (guardrail 7).
- **Linked from** `/download/` (its header's "All releases" link, which currently points at GitHub
  Releases, and the "Want to test the next version?" section) and the footer. It does not go in
  the main nav.

**When:** after Wave 3, and only once both of the issue's gate conditions hold. As of 2026-09-25:

1. *A stable release exists*: **met** (`v4.2.0`, `v4.2.1`, `v4.3.0`, `v4.4.0`).
2. *The counts clear `stats.js`'s bar* (≥ 50 installer downloads or ≥ 10 GitHub stars): **not
   met.** The repo has 1 star. The download total could not be read from the planning session
   (the Worker is outside its network allowlist); the issue recorded 2 on 2026-09-13. Check with
   `curl -s https://downloads.privacyfence.eu/api/stats/downloads`.

If the gate opens before Wave 3 lands, build the page on the Wave 0 foundation with the
`pages.yml` copy step, and Wave 3 moves it into the manifest. The plan's retirement does not wait
for `/releases/`: if Wave 5 lands with the gate still closed, #365 remains the tracking issue.
Before this file is deleted, update the issue with the additions above.

## Measurement

Monthly, against the Wave 0 baseline: downloads (existing KPI), Search Console
impressions/clicks for the D7 intents, AI referrals in GA4's traffic-acquisition report
(`chatgpt.com`, `claude.ai`, `perplexity.ai`, `copilot.microsoft.com`, `gemini.google.com`), GitHub
stars. GA4 sees only visitors who accepted, so read its numbers as trends, not totals. The download
count (Worker-side, cookieless) stays the headline KPI. After each wave, ask ChatGPT,
Claude and Perplexity "What is PrivacyFence?" and "How do I install PrivacyFence on Windows?" and
note whether the answers match the canonical description and the current install flow.

## ADRs this plan creates

Next free numbers when each PR lands (0039–0043 were taken by the product cleanup, 0044 by the
amd64-only `.deb`, 0045–0046 by #682's installer and signing changes; 0047+ as of 2026-09-25).

| ADR | Wave |
|---|---|
| Crawler and training-bot policy: allow all | 0 |
| Website CSS: responsive on plain modern CSS, no framework (H2, with the rejected Tailwind/Bootstrap/Pico) | 0 |
| Website analytics: GA4 behind consent, Google-only search tooling (D3/D4, with the rejected Cloudflare Web Analytics and Bing Webmaster Tools) | 0 |
| Which docs are published on privacyfence.eu | 3 |
| Docs generator, and docs built from the latest stable tag | 3 |
| Website hosting: GitHub Pages behind the Cloudflare proxy | 3 |

Positioning (B1) and the documentation principles are not ADRs: the first lives in
`website/canonical-description.md`, the second in `docs/README.md`.

## Retiring this plan

The Wave 5 PR deletes this file after confirming the ADRs above exist and `docs/README.md` no
longer names an active plan. Before that, the orchestrator adds this plan's additions to #365's
scope (see [Release history page](#release-history-page-releases-365)) to the issue if S6 has not
run, and closes the tracking issue once M14 is done.
