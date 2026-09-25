# ADR 0051: privacyfence.eu publishes the user and operator docs only

## Status

Accepted — 2026-09-25. Implemented in `scripts/build_site.py`.

## Context

`docs/` holds two audiences in one directory. People installing, using, deploying or reviewing
PrivacyFence need the install pages, the configuration and tools references, the organization
deployment guide and the security document. People changing the code need the coding and testing
guidelines, the test policy, packaging and release internals, connector QA, and the download
Worker's operations doc (which names the Cloudflare account and the R2 bucket layout). The ADRs
are a third kind again: frozen records of why, full of rejected alternatives and history.

privacyfence.eu gains a `/docs/` section rendered from these files. Whatever it publishes is what
search engines and AI assistants will quote as "the PrivacyFence documentation".

Decided as C3 and C7 of the website plan ([PR #683](https://github.com/privacyfence/privacyfence/pull/683));
the split itself was settled in the documentation rewrite (#692, #691).

## Decision

1. `/docs/` publishes exactly the docs listed in the **"User and operator docs"** half of
   `docs/README.md`, in its order and under its section headings, which become the site
   navigation. The `/docs/` landing page is that half of the index.
2. Every other top-level `docs/*.md` is contributor-only and is named in `CONTRIBUTOR_DOCS` in
   `scripts/build_site.py`. `docs/adr/**`, `docs/images/screenshots/README.md`, `CONTRIBUTING.md`
   and `CLAUDE.md` are never published. A doc in neither list fails the build's test.
3. A link from a published doc to anything outside the published set (an ADR, a contributor doc,
   `CHANGELOG.md`, a source file) is rewritten at build time to its GitHub URL **at the same tag**
   the docs are built from, so it points at the version the reader is reading about.
4. `llms-full.txt` carries the published set and nothing else; `llms.txt` lists it.

## Alternatives considered

- **Publish all of `docs/`.** Contributor process and release internals would compete with the
  user docs in search results and in AI answers, and the ADRs describe rejected designs and past
  behavior that the user docs deliberately leave out (they describe the current version only).
- **Publish nothing; keep linking GitHub.** GitHub renders Markdown, but has no site navigation,
  no search across the set, no per-page description or structured data, and the site's own
  analytics and consent cannot reach it.
- **A separate allowlist file for the published set.** It would restate `docs/README.md`'s
  published half and drift from it. Reading the index keeps one list, which a reader of the
  repository already sees.

## Consequences

- Moving a doc between audiences is a change to `docs/README.md` (and `CONTRIBUTOR_DOCS`),
  reviewed like any other.
- Published docs can link freely to ADRs and contributor docs; the reader lands on GitHub at the
  matching tag.
- A published doc linking a path that does not exist at that tag fails the build rather than
  shipping a dead link.

## Verification

- `tests/unit/test_website_docs_allowlist.py` (guardrail 5): every `docs/*.md` is in exactly one
  of the two halves, and `CONTRIBUTOR_DOCS` matches the index's contributor half.
- `tests/unit/test_website_links.py` (guardrail 6): every published doc's links stay in the
  published set or resolve to an existing repository path; the built site has no broken internal
  link.
- `tests/unit/test_website_docs_pages.py`: every published doc is rendered, and nothing else is.

## Related

- [PR #683](https://github.com/privacyfence/privacyfence/pull/683) — the website plan (C3, C7, D5).
- [ADR 0052](0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md) — how and from which
  version the docs are built.
- [ADR 0048](0048-every-crawler-is-allowed-including-ai-training.md) — what crawlers may read.
