# ADR 0302: 8.x refuses a data directory that is not ready for it

## Status

Accepted — 2026-11-14. Amends [ADR 0262](0262-7x-is-the-migration-checkpoint-for-8x.md). Implemented.

Amended by [ADR 0317](0317-an-org-directory-holding-only-admin-config-is-fresh.md), 2026-11-16.

Amended by [ADR 0337](0337-an-organization-server-is-recognised-by-its-own-data.md), 2026-11-23.

## Context

[ADR 0262](0262-7x-is-the-migration-checkpoint-for-8x.md) makes 7.x the migration checkpoint and has 8.x refuse data that 7.x has not prepared, but 8.x also has to refuse a desktop install's data, which 7.x never converts. Both cases must stop with a message that says what to do, before anything loads configuration.

## Decision

Before every one-shot command and before configuration loads, `data_format.check_upgrade_source(root)` runs, and a failure stops the start. `DATA_FORMAT` stays 7; `MINIMUM_FORMAT` is 7, so a downgrade to 7.x keeps working. In order:

1. The existing "marker is newer than this release" check runs first, on the marker read from `root`.
2. If a desktop trace (`web_base_url` or `mcp_token`, at the data root or under `authority/`) exists and `org/org_bundle.json` does not contain `"mode": "org"`, start is refused with the desktop text. This holds with or without a marker. The bundle exception keeps an organization server that was converted in place on 6.x working.
3. Otherwise, if the marker is missing or its format is below `MINIMUM_FORMAT`: when person data exists (`users`, `org`, `credentials` or `authority`; an `org/` holding only `admin_config.json` does not count, [ADR 0317](0317-an-org-directory-holding-only-admin-config-is-fresh.md)) start is refused with the checkpoint text; when none exists the directory is fresh, and the marker is written at once. Writing it at first start means a single-user install restarted before its setup finished is not refused.

Desktop text, exactly:

> This data directory ({root}) belongs to a PrivacyFence desktop install. PrivacyFence 8 runs only as an organization server or the Docker image and does not read desktop data. Keep using PrivacyFence 7 on this computer, or move to organization mode: https://privacyfence.eu/docs/moving-to-organization-mode/

Checkpoint text, exactly:

> This data directory ({root}) has not been prepared for PrivacyFence 8. Install the latest PrivacyFence 7, start it once, check that the Admin page says "Ready for PrivacyFence 8: yes", then start this release again: https://privacyfence.eu/docs/moving-to-organization-mode/

`{root}` is the data directory path. The links carry no anchor, and a test asserts that `docs/moving-to-organization-mode.md` exists.

## Alternatives considered

- **Bumping `DATA_FORMAT` to 8.** Rejected: 7.x could not read data that 8.0 wrote, making the migration irreversible. That is left to the plan that removes the pre-7 converters.
- **Refusing only on a missing marker.** Rejected: a 7.x local install can carry a marker, and would pass.
- **Refusing every directory without a marker.** Rejected: it would refuse a fresh Docker volume.

## Consequences

- A fresh directory gets its marker at first start, written atomically.
- The pre-7 converters stay; this check adds a refusal and removes none of them.
- Tests that call `main()` must patch the data directory so a developer's checkout cannot change the result.

## Verification

`tests/unit/test_data_format.py`.

## Related

ADR 0262, 0301, 0303.
