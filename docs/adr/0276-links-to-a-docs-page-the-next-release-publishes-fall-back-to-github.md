# ADR 0276: Links to a docs page the next release publishes fall back to GitHub until it does

## Status

Accepted — 2026-11-01. Implemented: `scripts/build_site.py` (`DOCS_PENDING_RELEASE`,
`point_docs_links_at_github`).
Amends [ADR 0099](0099-an-ai-agents-website-page-lands-after-the-release-that-carries-its-doc.md).

## Context

`/docs/` is rendered from the newest stable release tag, while the hand-written pages deploy from
`main` ([ADR 0052](0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md)). A page that
links a doc merged to `main` after that tag therefore links a page that does not exist yet, and the
build's link check fails. [ADR 0099](0099-an-ai-agents-website-page-lands-after-the-release-that-carries-its-doc.md)
answered this for AI agent pages by holding the page back until the release carries its doc.

`docs/oauth-relay.md` is a different case. The relay terms and the privacy statement should link it,
and it describes a service deployed from `main` (`cloudflare/oauth-relay/`), not a released version,
so GitHub's `main` is not the wrong version, which was ADR 0099's other objection to a GitHub link.

## Decision

`build_site.DOCS_PENDING_RELEASE` names published docs that the newest stable tag may not carry; it
holds `oauth-relay`, and `oauth-relay.md` is removed from `build_site.CONTRIBUTOR_DOCS` so the next
release publishes it on `/docs/`.

When the build has no `/docs/` pages at all, every docs link goes to GitHub `main` as before. When it
has them, `point_docs_links_at_github(out, "main", only=DOCS_PENDING_RELEASE)` rewrites a link to
GitHub only if its stem is in the set and `_site/docs/<stem>/index.html` does not exist. `/docs/`
itself is never rewritten in that mode. Any other missing docs page still fails the link check.

## Alternatives considered

- **A fallback for every missing docs stem** — it would silently undo ADR 0099's guard for AI agent
  pages.
- **Waiting for the next release before linking** — the relay terms and privacy statement would stay
  without a link to how the relay is run, for no gain.
- **A hand-edited GitHub link** — ADR 0099 rejected it as a second edit someone must remember to
  revert.

## Consequences

- Until the release that renders it, the link opens `docs/oauth-relay.md` on GitHub; afterwards it
  opens `/docs/oauth-relay/` with no edit.
- Adding a name to the set is a deliberate act in code review, and is only right for a doc that is not
  tied to a released version.
- An AI agent page that links an unreleased connect doc still fails the link check.

## Verification

In `tests/unit/test_build_site.py`:
`test_pending_docs_links_fall_back_when_the_tag_lacks_them`,
`test_pending_docs_links_stay_when_the_tag_has_them`,
`test_oauth_relay_is_pending_and_published`, and
`test_docs_links_fall_back_to_github_when_docs_are_not_built` (the behaviour with `only=None`).

## Related

[ADR 0099](0099-an-ai-agents-website-page-lands-after-the-release-that-carries-its-doc.md),
[ADR 0052](0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md),
[ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md).
