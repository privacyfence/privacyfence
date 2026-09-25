# ADR 0052: The docs are built with Zensical, from the latest stable release tag

## Status

Accepted — 2026-09-25. Implemented in `scripts/build_site.py` and `website/_docs/`. `/docs/` goes
live with the first stable release that carries the published doc set.

## Context

privacyfence.eu's marketing pages are hand-written HTML (ADR 0049). The published docs
(ADR 0051) are about twenty Markdown files, some with wide generated tables and long code blocks,
and need navigation, search, a table of contents and a readable layout from a 320 px phone up.
The site's build runs in GitHub Actions, and the project is Python; there is no Node toolchain
in the website build.

The docs in `main` describe the next release, not the one people can download. The docs describe
only the current version, with no upgrade paths or "as of" notes (decision G1 of the website plan).

Decided as C1 and C4 of the website plan ([PR #683](https://github.com/privacyfence/privacyfence/pull/683)).
The generator was chosen by a trial on the real doc set (2026-09-25): a strict build of every
published doc, the mobile drawer navigation, the table of contents and wide tables at 360 px.

## Decision

1. **Generator: Zensical**, pinned to one exact version in the `docs` extra and installed only
   from the hash-locked `requirements/docs.lock.txt`. It renders in strict mode: a link to a page
   that does not exist fails the build.
2. **Everything site-specific happens in our own export step**, not in generator plugins:
   choosing the docs, rewriting links, the landing page, per-page description and content group
   (as front matter), the version note, and the site's header, footer, tokens and consent code
   (a theme override in `website/_docs/` using the generator's standard block names). The config
   is written by `scripts/build_site.py` in the MkDocs format. Swapping the generator is a change
   to one function and the lock, not to the docs or the theme.
3. **Self-hosted only**: the theme's web fonts are off and no repository link is configured, so a
   docs page makes no third-party request (the site's consent rules, ADR 0050).
4. **Version: the newest stable release tag** (`vX.Y.Z`, the same channel rule as
   `scripts/r2_release.py`), never `main`. Each page says which version it describes.
   `pages.yml` rebuilds after every release, which is what moves `/docs/` to the new version.
5. **Stale-tag guard**: if that tag predates the published doc set (it has no
   `docs/how-it-works.md`), the build skips `/docs/` and `llms-full.txt` with a warning, and
   `llms.txt` points at the docs on GitHub. The first stable release after the rewrite turns
   `/docs/` on without a website change.

## Alternatives considered

- **MkDocs with Material for MkDocs.** The best-known option and the plan's fallback. MkDocs 1.x
  has had no release since 2024 and Material is in maintenance mode, its team having moved to
  Zensical, which reads the same configuration and theme blocks. Because of decision 2, falling
  back to it is a small change if Zensical's pre-1.0 releases prove unstable.
- **Sphinx (with MyST).** Mature, but its theming, navigation and search are a different system
  from the rest of the site's, and its Markdown support is an extension layer.
- **Docusaurus, Hugo, Astro Starlight.** A Node or Go toolchain in the website build, to be
  locked and audited separately, for a Python project.
- **Hand-rolled Markdown to HTML in `build_site.py`.** No search, drawer navigation or table of
  contents without writing them.
- **Publish from `main`.** The site would document unreleased behavior (new settings, changed
  defaults) to people running the last release.
- **Versioned docs (`/docs/4.6/`, `/docs/4.7/`).** Contradicts G1 (current version only), multiplies
  what search engines index, and needs a maintained version switcher.

## Consequences

- A doc change reaches the site with the next stable release, not on merge. A fix that must go
  out sooner goes out as a release.
- A new stable tag republishes the docs even if the website did not change.
- The generator's pre-1.0 releases can change the rendered site; the exact pin and the lock make
  every upgrade a reviewed change, and the PR build renders the docs before it merges.

## Verification

- `.github/workflows/website-build.yml` builds on every pull request three ways: the newest stable
  tag (what would deploy), `v4.5.0` (must hit the stale-tag guard), and a throwaway local tag on
  the PR's commit (renders the PR's docs through the real tag resolution). The website guardrails
  then run against that last build.
- `tests/unit/test_build_site.py`: tag resolution, the stale-tag guard, link rewriting.
- `tests/unit/test_website_docs_pages.py`: every docs page carries the site chrome, consent code,
  content group, JSON-LD and version note, and no third-party resource.
- `.github/workflows/dependency-audit.yml`: `requirements/docs.lock.txt` is fresh and audited.

## Related

- [PR #683](https://github.com/privacyfence/privacyfence/pull/683) — the website plan (C1, C4, G1).
- [ADR 0051](0051-privacyfence-eu-publishes-the-user-and-operator-docs-only.md) — which docs are
  published.
- [ADR 0053](0053-the-website-is-hosted-on-github-pages-behind-cloudflare.md) — where the built site
  is served from.
