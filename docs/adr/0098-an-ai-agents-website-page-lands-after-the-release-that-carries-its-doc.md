# ADR 0098: An AI agent's website page lands after the stable release whose `/docs/` carries its setup doc

## Status

Accepted (recorded retroactively on 2026-09-28; decided by the maintainer on 2026-09-26, before
[PR 766](https://github.com/privacyfence/privacyfence/pull/766)). Followed for the Claude
clients: docs in [PR 766](https://github.com/privacyfence/privacyfence/pull/766), release 5.0.0
([PR 782](https://github.com/privacyfence/privacyfence/pull/782)), website in
[PR 785](https://github.com/privacyfence/privacyfence/pull/785).

## Context

Every page under `/ai-agents/<slug>/` links its setup doc at `/docs/connect-<slug>/`
([ADR 0097](0097-each-ai-agent-has-its-own-setup-doc-and-website-page.md)). The hand-written
website pages deploy from `main` on merge (`pages.yml`), but `/docs/` is rendered from the newest
**stable release tag**, not from `main`
([ADR 0052](0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md)). A setup doc merged
to `main` therefore does not exist on the site until the next stable release.

The build's link check (guardrail 6) fails on any internal link that does not resolve in the
output. A website page merged before the release would link a doc the deployed `/docs/` does not
have, and the website build on `main` would go red, blocking every later website change until the
release.

## Decision

**A client's `/ai-agents/<slug>/` page and its `website/_data/clients.json` entry merge only after
a stable release whose `/docs/` carries `connect-<slug>.md`.** The order for every new client is:
docs PR → stable release → website PR. The website PR checks, before it is opened, that
`/docs/connect-<slug>/` is live on privacyfence.eu.

`clients.json`'s own `about` text states the rule where the next person adding a client will read
it.

## Alternatives considered

- **Build the agent pages' doc links from `main`'s docs, or render `/docs/` from `main`.**
  Rejected: that undoes ADR 0052, whose point is that the site documents the version people can
  download. A client connected with instructions for an unreleased version fails.
- **Link to the doc on GitHub until the release, then switch.** Rejected: a second edit per client
  that someone has to remember, and GitHub's rendering of `main` has the same wrong-version
  problem.
- **Exempt `/ai-agents/` links from the link check.** Rejected: the check is what catches a real
  broken link. An exemption would also have let the website name a client as supported before any
  released version supported it.
- **Merge the website PR early behind a feature flag.** Rejected: the site is static HTML with no
  flag mechanism, and adding one to hide a page for a few days costs more than waiting.

## Consequences

- A newly supported client appears on the website one release after its docs merge, never before
  a version that supports it is downloadable.
- Adding a client takes at least two PRs across a release. Plans that add clients sequence their
  work that way.
- A client whose docs are renamed or removed must have its website page changed in the same
  release cycle, or the link check fails once the release deploys.

## Verification

- `scripts/build_site.py`, step 6 of its module docstring (the link check) and step 3 (`/docs/`
  from the newest stable tag).
- `.github/workflows/website-build.yml`: builds every PR against the newest stable tag, which is
  what deploys.
- `website/_data/clients.json`'s `about` text.
- `tests/unit/test_website_agent_pages.py` (guardrail 14): every page's **Set it up** button points
  at `/docs/connect-<slug>/`.

## Related

- [ADR 0099](0099-unverified-client-setup-instructions-never-merge-to-main.md): the docs PR itself
  waits until the instructions are verified.
