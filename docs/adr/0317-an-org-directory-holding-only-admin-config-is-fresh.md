# ADR 0317: An org directory holding only admin_config.json is fresh

## Status

Accepted — 2026-11-16. Amends [ADR 0302](0302-8x-refuses-a-data-directory-that-is-not-ready-for-it.md). Implemented.

## Context

A deployment tool can write `org/admin_config.json` before the first start, for example to set the `upstream_mcp.<id>.groups` map that [the upstream MCP guide](../upstream-mcp.md) tells operators to edit in that file. [ADR 0302](0302-8x-refuses-a-data-directory-that-is-not-ready-for-it.md) counted any non-empty `org/` as person data, so PrivacyFence 8 refused such a fresh Docker data directory with "has not been prepared for PrivacyFence 8" (issue #1141). That is wrong for a fresh install: none of the pre-7 converters that [ADR 0262](0262-7x-is-the-migration-checkpoint-for-8x.md) lets 8.x delete touches `admin_config.json`, and `admin_config.load()` validates the file at every start.

## Decision

`org/` does not count as person data when it is a real directory (not a symlink) whose one and only entry is `admin_config.json`, a regular file (not a symlink, not a directory). Anything else in `org/` (a second file such as `org_bundle.json`, a temp file, a subdirectory) keeps the current behaviour, and so does any non-empty `users`, `credentials` or `authority`. The check does not read the file; `admin_config.load()` validates it later at start. The desktop-trace check is unchanged.

## Alternatives considered

- **A supported seeding variable**, for example a seed file merged into the admin config at first start. Rejected: it is a new config surface for something operators already do by editing the file, and a deployment that re-merges its group map at every run would still write the file directly.
- **Seeders write `data_format.json` themselves.** Rejected: the marker is 7.x's proof that the data was converted, and a deployment tool would be forging it.
- **Ignore `admin_config.json` wherever it appears in `org/`.** Rejected: `org/org_bundle.json` next to it can carry legacy settings that the bundle import (a pre-7 converter) merges in.
- **Validate the seed's content in the check.** Rejected: `admin_config.load()` already refuses a bad file at start. Duplicating it ties the check to the admin-config schema.

## Consequences

- A seeded admin config is used as is. The first-start import from the bundle and the environment (allowed domains from `PRIVACYFENCE_ALLOWED_DOMAINS`, offered services, the single-user step-up default) does not run, and the seed's missing sections take the plain defaults (`admin_config.ensure`).
- A seed without `"version": 1` refuses the start through `admin_config.load`.
- The marker is still not a security boundary.

## Verification

`tests/unit/test_data_format.py`.

## Related

ADR 0262, 0302.
