# ADR 0168: A `stable` branch follows the latest stable release for the plugin marketplace

## Status

Accepted — 2026-10-12. Implemented.

## Context

The `privacyfence-sync` plugin is listed in `.claude-plugin/marketplace.json`, which a marketplace
client reads from the repository's default branch, `main` (ADR 0163). `main` runs ahead of the
latest release, so someone who adds the marketplace to stay on stable gets unreleased plugin
changes. Claude Cowork's "Add marketplace" dialog has no branch field, but it accepts
`owner/repo#ref`, and a tag as the ref pins one version rather than following new ones.

## Decision

`build.yml`'s `finalize-release` job, as its last step and only for the stable channel, pushes the
tagged commit to `refs/heads/stable` as a fast-forward. Adding the marketplace as
`privacyfence/privacyfence#stable` then follows the latest stable release. The step is last so a
failure cannot hold back the release manifest or the download routes; the release itself is already
published by then. The push is not forced: a release that does not descend from the previous stable
one fails the step visibly instead of rewriting `stable`.

## Consequences

- Pre-release tags never move `stable`.
- `stable` must exist and be writable by the workflow's `GITHUB_TOKEN`: a ruleset that protects every
  branch would make the push fail, so `stable` needs to be exempt. The first stable release after
  this lands creates it if it does not exist yet; before that, create it at the latest release tag.
- The release zip and `#v<version>` stay the exact-version routes (`docs/privacyfence-sync.md`).
