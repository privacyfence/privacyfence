# ADR 0337: An organization server is recognised by its own data

## Status

Accepted — 2026-11-23. Amends [ADR 0302](0302-8x-refuses-a-data-directory-that-is-not-ready-for-it.md). Implemented.

## Context

[ADR 0302](0302-8x-refuses-a-data-directory-that-is-not-ready-for-it.md) refuses a data directory with a desktop trace (`web_base_url` or `mcp_token`, at the root or under `authority/`) unless `org/org_bundle.json` says `"mode": "org"`. In 7.x the organization mode comes from the environment, and `--retire-bundle` deletes `org/org_bundle.json`. An organization server upgraded that way keeps its old `mcp_token` and `web_base_url`, loses the bundle, and PrivacyFence 8 refused it as a desktop install (issue #1182). What stays on such a volume is `org/org_bundle_adopted.json` and the 6.x `org/org_config.json`; 6.x kept `"mode"` only in the latter, and 7.x copied it into the bundle.

## Decision

`data_format._says_org(root)` returns true when `org/org_bundle.json` or `org/org_config.json` is a JSON object with `"mode": "org"`; one helper, `_file_says_org(path)`, reads both, and anything unreadable (missing, a directory, not JSON, not an object, another mode) says no. The desktop texts, `DESKTOP_TRACES` and the order of checks are unchanged.

## Alternatives considered

- **Read `PRIVACYFENCE_MODE`.** Rejected: the Docker image sets it to `org` for every container (`deploy/docker/Dockerfile`), so a desktop directory mounted into the image would be excused.
- **`org/org_bundle_adopted.json`, including its `retired` key.** Rejected: `org_bundle_signing.adopt_legacy_files` also runs on desktop installs, and a desktop install with a 6.x `org_config.json` gets one too; `org_config.json` already carries the mode itself.
- **A non-empty `users/`.** Rejected: a privilege-separated desktop install creates `users/os-<uid>/` for other OS users, and 7.x single-user mode writes `users/single-user/`.

## Consequences

- An upgraded organization server with a leftover `mcp_token` or `web_base_url` and a retired bundle starts.
- A desktop install whose `org/org_config.json` says `"mode": "org"` is excused as well; that file is the 6.x statement that the directory is an organization server's, as the bundle was. The marker is still not a security boundary.
- A volume that lost both files is still refused as a desktop install.

## Verification

`tests/unit/test_data_format.py`, `TestCheckUpgradeSource`. The reporter runs the fixed image on a copy of their volume.

## Related

ADR 0262, 0302, 0317.
