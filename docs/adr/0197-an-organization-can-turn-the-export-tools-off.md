# ADR 0197: An organization can turn the export tools off

## Status

Accepted — 2026-10-17. Implemented.
Amends ADR 0160. Exports on an organization-managed install were always available unless staging was off.

## Context

On an organization-managed install an export tool stages the whole file on the server until the
user claims its one-time link (ADR 0160). The organization that does not want that user data held on
the server had only `download_delivery.allow_disk_staging`, which refuses at call time and also
affects ordinary large downloads.

## Decision

`download_delivery.allow_exports` (default `true`) in the organization config. When `false` on an
organization-managed install, `grist_export_records`, `grist_export_sql`, `salesforce_export_report`,
`jira_export_issues` and `assets_export_objects` are left out of the connector's tool list, and a
call to one is refused before any request. Local mode ignores the setting.
`scripts/build_org_bundle.py --no-exports` writes it.

## Alternatives considered

- **Reuse `allow_disk_staging`.** It also governs Drive, Gmail and Confluence downloads.
- **Refuse only at call time.** The tools would still be offered to the AI client.

## Consequences

- Policy, auto-accept and scoped-token rules for the export tools stay defined but are unreachable.
- `privacyfence-sync` recipes that use an export step fail on such an organization.

## Verification

- `tests/unit/test_export_files.py`: `TestExportsOff`. `tests/unit/test_org_mode.py`: `allow_exports`.
  `tests/unit/test_build_org_bundle.py`: `test_no_exports_writes_false`.

## Related

- [`export_files.py`](../../src/privacyfence/export_files.py), [`org_mode.py`](../../src/privacyfence/org_mode.py)
- ADR [0160](0160-org-mode-exports-are-delivered-by-a-one-time-link.md)
