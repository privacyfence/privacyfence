# ADR 0098: Each AI agent has its own setup doc and its own website page, counted as the `ai-agent` content group

## Status

Accepted (recorded retroactively on 2026-09-28; decided by the maintainer on 2026-09-26, before
[PR 766](https://github.com/privacyfence/privacyfence/pull/766)). Implemented: the docs in
[PR 766](https://github.com/privacyfence/privacyfence/pull/766), the website in
[PR 785](https://github.com/privacyfence/privacyfence/pull/785).

## Context

Before 5.0.0, connecting an AI client was described in five places: the three `install-*.md`
pages, `how-it-works.md`'s "How an AI system connects", and the organization guide's "Add
PrivacyFence to an AI client". The website named the tested clients in its "Works with" strip
(`website/_data/clients.json`, guardrail 10) but linked none of them anywhere.

More clients are coming: ChatGPT
([issue 390](https://github.com/privacyfence/privacyfence/issues/390),
[issue 391](https://github.com/privacyfence/privacyfence/issues/391)) and Gemini
([issue 392](https://github.com/privacyfence/privacyfence/issues/392),
[issue 393](https://github.com/privacyfence/privacyfence/issues/393),
[issue 394](https://github.com/privacyfence/privacyfence/issues/394)). Each has its own way in
(an extension, a CLI command, a custom connector in a web app) and its own confirmation behaviour.
Which of them readers actually care about is a question the project wants answered from data,
and the site already measures that for platforms (`install-*` → content group `platform`) and
connectors (`connector`) through GA4 content groups
([ADR 0050](0050-website-analytics-is-ga4-behind-consent.md)).

## Decision

**Every supported AI client gets one published setup doc, `docs/connect-<slug>.md`, and one
website page, `/ai-agents/<slug>/`, both in the GA4 content group `ai-agent`.**

1. **Docs.** `connect-<slug>.md` is the one place that says how to connect that client, in local
   mode and in an organization deployment. Every such page has the same sections (deployments,
   local mode, organization mode, files, confirmations, how the client is identified,
   troubleshooting). The docs are listed in `docs/README.md`'s published section **"AI agent
   setup"**, after "Connector setup". The platform and mode pages keep only what is specific to
   them and link here.
2. **Content group.** `build_site.content_group()` gives every `connect-*` stem the group
   `ai-agent`, as `install-*` gets `platform`. The website's `/ai-agents/` pages carry the same
   group, so one GA4 dimension answers "which agent are people setting up?" across both.
3. **Website.** A top-nav **AI agents** link to `/ai-agents/`, an overview with one card per client
   and one for other MCP clients, and one page per `clients.json` entry at `/ai-agents/<slug>/`,
   each with a **Set it up** button to `/docs/connect-<slug>/`. A client's `slug` in
   `clients.json` names both its page and its doc. This mirrors the Connectors menu.
4. **Guardrail 14** (`tests/unit/test_website_agent_pages.py`) holds the three together: every
   `clients.json` entry has exactly one page and every page an entry, each page is in `ai-agent`
   and links its doc, that doc is listed under "AI agent setup", and the overview, the strip, the
   header and `llms.txt` link every page.

## Alternatives considered

- **One "Connecting AI clients" doc with a section per client.** Rejected: GA4 counts page paths,
  not sections, so interest in one client could not be told from interest in another. It also
  grows without bound as plans for ChatGPT and Gemini add clients.
- **Keep per-client steps inside `install-<os>.md` and the organization guide.** Rejected: that
  is the five-place spread this replaces. A client's instructions changed in one place and went
  stale in the others.
- **Docs only, no website pages.** Rejected: the "Works with" strip names the clients, and a
  visitor who clicks one should land on a page about it, not on the generic docs index. The
  website page is also where a client is compared with the others at a glance, which a setup doc
  is not.
- **Website pages only, with the steps on the website.** Rejected: steps must match the version
  the reader installed. `/docs/` is built from the latest stable tag
  ([ADR 0052](0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md)); hand-written
  website pages deploy from `main`. The website page summarises and links; the doc instructs.

## Consequences

- Adding a client means four things together: a `clients.json` entry, a website page, a
  `connect-<slug>.md`, and its line under "AI agent setup". Guardrail 14 fails until all four
  exist. When the website half may land is [ADR 0099](0099-an-ai-agents-website-page-lands-after-the-release-that-carries-its-doc.md).
- The `ai-agent` content group reports client interest separately from platform and connector
  interest, without any new tracking code.
- Guidance that applies to every client (how `/mcp` authenticates, what the client is told) stays
  in `how-it-works.md`; the per-client pages link to it rather than repeat it.

## Verification

- `docs/connect-claude-desktop.md`, `docs/connect-claude-code.md`, `docs/connect-claude-ai.md`;
  `docs/README.md` → "AI agent setup".
- `scripts/build_site.py`: `content_group()`, `load_clients()`, `agent_page()`, `PAGES`.
- `website/_data/clients.json`; `website/ai-agents/`.
- `tests/unit/test_website_agent_pages.py` (guardrail 14); `tests/unit/test_website_docs_pages.py`
  (the built `/docs/connect-*` page's content group).

## Related

- [ADR 0050](0050-website-analytics-is-ga4-behind-consent.md): the content groups this reuses.
- [ADR 0051](0051-privacyfence-eu-publishes-the-user-and-operator-docs-only.md): the published
  half of `docs/README.md` that "AI agent setup" is part of.
- [ADR 0101](0101-the-website-shows-no-third-party-logos.md): the pages are text only.
