# ADR 0151: Exports write a file and return only counts and a hash

## Status

Accepted — 2026-10-11. Implemented.

## Context

A scheduled sync job reads whole Grist tables and Salesforce reports. `grist_get_records` returns at
most 500 records and `salesforce_run_report` at most 2,000 rows per call, so one Grist table took
13 approval cards, and every row passed through the model's context on its way to the next step.

Rules already exist that let a scheduled run read without a card: a `grist.document` rule (ADR
0144), an approved-report rule and an approved Drive folder rule. The PII gate overrides such a rule
when the content trips the scan, and an unattended session then gets `denied_unattended` (ADR 0015).

## Decision

Two separate tools write a whole result to a file on the user's computer.

- **`grist_export_records`** writes every record of one table, optionally filtered, up to 50,000.
  **`salesforce_export_report`** writes every row of a report and pages through it internally with
  `page_by`; it takes the same `columns` and `filters` as `salesforce_run_report`, so an incremental
  pull and an IDs-only pull need no new parameter. The file is CSV or JSON, chosen by the file name's
  ending, and goes to `destination_dir` through `local_files.deliver_file` (ADR 0007).
- **The tool result holds no row.** It is `{rows, columns, sha256}` plus where the file landed.
- **One review card after the whole fetch.** It shows the counts, a sample of 20 rows and the
  destination. `raw_data` holds identifiers, counts, column names and the SHA-256, never a cell
  value, because it can reach the audit trail. The delivery size is checked before the card, so a
  call that cannot succeed costs no approval.
- **The approval is bound to the file** (ADR 0073): the SHA-256 is part of the approval's `args`.
- **Each tool reuses its read tool's operation key** (`grist.read_records`, `salesforce.run_report`).
  A document rule or an approved-report rule made for interactive reads therefore also covers the
  export of the same document or report. This is intended: the export reads the same data with the
  same rights, and only where the rows go differs.
- **The PII gate is unchanged.** The scan runs over the first million characters of the file. An
  export that trips it still gets a card despite a matching rule, and an unattended session is
  denied.
- **Organization-managed installs are refused**, before any network call, because the daemon's disk
  is not the user's.

## Alternatives considered

- **An `output_path` option on `grist_get_records` and `salesforce_run_report`.** The return shape
  would depend on an argument (ADR 0116 keeps a tool's return shape stable), and the card and tool
  description would have to explain two behaviours.
- **New operation keys for the exports.** The existing per-document and per-report rules would not
  cover them, and `approved_report_ids` sits in the frozen v1 scope-selector set.
- **One `output_path` argument.** `deliver_file` takes a folder and a name (ADR 0007), as
  `drive_download_file` does. Splitting them lets the default name come from the table or report.
- **Paging a Grist export by `after_id`.** Paging costs about the square of the page count in
  transfer (ADR 0146). One request of up to 50,001 rows costs Grist the same table load as one page.
- **Delivery by link in organization mode.** A staged link would hand a whole table out through the
  org daemon's staging store, which is a separate delivery decision.
- **Exempting exports from the PII gate.** The gate stays as it is.
- **Leaving formula columns out of a Grist export.** The export is a read of what the table shows.
  The tool description tells the caller which columns `grist_update_csv` can take back.

## Consequences

- A table or report of up to 50,000 rows (Grist) or `salesforce.report_max_pages` report runs
  (Salesforce) is one approval, or none under a matching rule.
- A rule made for interactive reads now also lets the export of that document or report through
  without a card, unless the PII scan trips.
- The whole result is held in memory, rendered and hashed before the card, and capped by
  `file_bridge.max_download_bytes`.
- Where a file of the same name exists, the shim-delivered file gets a numbered name; the result
  names the real path.
- An organization-managed install cannot export.

## Verification

- `tests/unit/connectors/test_grist_connector.py`: `TestExportRecords`.
- `tests/unit/connectors/test_salesforce_connector.py`: `TestExportReport`.
- `tests/unit/connectors/test_grist_connector.py`: `TestExportSql` (the same flow for ADR 0152).
- `tests/unit/test_export_files.py` and `tests/unit/test_local_files.py`: rendering, the sample, and
  the delivery size check.
- `tests/unit/policy` and `tests/unit/test_systemic_gate_invariants.py`: the operation keys, verbs
  and gates.

## Related

- [`export_files.py`](../../src/privacyfence/export_files.py), [`connectors/grist.py`](../../src/privacyfence/connectors/grist.py), [`connectors/salesforce.py`](../../src/privacyfence/connectors/salesforce.py)
- ADR [0007](0007-local-file-bridge.md), ADR [0015](0015-unattended-session-flag-is-advisory-only.md), ADR [0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md), ADR [0133](0133-salesforce-reports-page-by-a-unique-key-column.md), ADR [0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md), ADR [0146](0146-grist-records-page-by-record-id.md), ADR [0148](0148-grist-bulk-writes-take-a-csv-file-and-are-reviewed-as-counts-and-a-sample.md), ADR [0152](0152-grist-export-sql-reads-through-the-sql-endpoint.md)
