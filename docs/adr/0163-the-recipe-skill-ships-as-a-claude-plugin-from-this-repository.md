# ADR 0163: The recipe skill ships as a Claude plugin from this repository

## Status

Accepted — 2026-10-11. Implemented.

## Context

Writing a sync, or porting an existing data-sync skill, by hand means reading the
`privacyfence-sync` README every time. A Claude skill that knows the recipe format lets the user ask
Claude for the recipe, its scripts and the list of rules instead.

## Decision

The `privacyfence-sync` plugin lives in `plugins/privacyfence-sync/` and carries the
`privacyfence-sync-recipes` skill and a byte-for-byte copy of `sync/README.md`, which is the
authority for the format. The repository's `.claude-plugin/marketplace.json` lists it for Claude
Code (`/plugin marketplace add privacyfence/privacyfence`). Each stable release attaches
`privacyfence-sync-plugin.zip`, built by `scripts/build_sync_plugin.py`, for Claude Cowork and
claude.ai; the build stamps the release version into `plugin.json` (the repository has none) and
refuses when the README copy is stale. Neither the plugin nor `sync/` enters the `privacyfence`
sdist.

## Alternatives considered

- **Generating `SKILL.md` from the README.** The skill is guidance on top of the format, not the
  format; the README is bundled verbatim instead, and a test keeps the copy exact.
- **Shipping the skill inside the `privacyfence-sync` wheel.** Plugins are installed by Claude
  clients, not by pip; a wheel is the wrong place for them.

## Consequences

- The marketplace tracks the default branch, so it can be newer than the installed
  `privacyfence-sync`; the docs say to prefer the release zip when they differ.
- Changing `sync/README.md` requires copying it into the plugin in the same change.

## Verification

- `tests/unit/test_sync_plugin.py`.
- `.github/workflows/build.yml` (`finalize-release` builds and attaches the zip), `MANIFEST.in`.

## Related

- [`plugins/privacyfence-sync/`](../../plugins/privacyfence-sync/), [`scripts/build_sync_plugin.py`](../../scripts/build_sync_plugin.py)
- ADR [0161](0161-privacyfence-sync-is-a-separate-plain-mcp-client.md)
