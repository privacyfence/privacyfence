# ADR 0160: Org-mode exports are delivered by a one-time link

## Status

Accepted — 2026-10-11. Implemented.
Amends ADR 0155. Its refusal of exports on an organization-managed install is replaced by a one-time link.

## Context

ADR 0155's export tools refused on an organization-managed install, because the server has no
`destination_dir` on the user's computer. Organization users run scheduled syncs too. Org-mode
Drive downloads already hand a file out through a one-time staged link (ADR 0017, ADR 0028).

## Decision

On an organization-managed install an export tool stages the file on the server and returns the
same one-time download link a large Drive download gets: `download_url`, `expires_at` and a note,
and no `path`. `destination_dir` is ignored there. The tool refuses, before any network call, only
when the organization turned staging off (`download_delivery.allow_disk_staging`). The approval
binding, the card, the PII scan and the `{rows, columns, sha256}` result are unchanged; the card's
"Saved to" row says a one-time link and the audit `delivery` is `staged_link`.

## Alternatives considered

- **Inline delivery in org mode.** The export's point is that no row enters the tool result
  (ADR 0155); an inline file would put every row there.
- **Keeping the refusal.** The link mechanism, its encryption at rest and single use already carry
  org-mode Drive downloads.

## Consequences

- All four export tools work in both modes. A client that cannot fetch the link cannot use the file.
- The `file_bridge.max_download_bytes` limit applies, and is checked before the card.
- The Peek download tool's "Not available on an organization-managed install" is a different tool
  and is unchanged.

## Verification

- `tests/unit/test_local_files.py`: `TestDeliverExport`. `tests/unit/test_export_files.py`:
  `TestRefuseOrgWithoutStaging`, `TestSavedTo`. The `test_org_mode_*` tests in
  `tests/unit/connectors/test_grist_connector.py` and `test_salesforce_connector.py`.

## Related

- [`export_files.py`](../../src/privacyfence/export_files.py), [`local_files.py`](../../src/privacyfence/local_files.py),
  [`org_mode.py`](../../src/privacyfence/org_mode.py)
- ADR [0017](0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md), ADR [0028](0028-clients-without-the-shim-get-capability-urls.md), ADR [0155](0155-exports-write-a-file-and-return-only-counts-and-a-hash.md)
