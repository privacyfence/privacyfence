# ADR 0180: Jira and Salesforce CSV exports neutralise formula cells

## Status

Accepted — 2026-10-14. Implemented.
Amends ADR 0155. Grist exports are not changed.

## Context

ADR 0155 makes exports write a file the person later opens, often in a spreadsheet. Jira issue
fields and Salesforce record values are written by other people; a cell such as
`=HYPERLINK(...)` or `@SUM(...)` is run as a formula when the CSV is opened (SEC-C4). The audit-log
export already starts such cells with `'` (`audit_log._FORMULA_TRIGGER_CHARS`).

## Decision

- **`export_files.render` takes `spreadsheet_safe`** (default false). For CSV, a `str` cell whose
  first character is one of `=`, `+`, `-`, `@`, tab or carriage return is written with a leading
  `'`. The list is the audit export's, imported from `audit_log`, so every spreadsheet-bound export
  uses one list. JSON output is unchanged.
- **Jira and Salesforce pass `spreadsheet_safe=True`.**
- **Grist does not.** A Grist export must round-trip through `grist_update_csv`, and a leading `'`
  would be written back into the table. This is the maintainer's decision.

## Alternatives considered

- **Neutralise every export.** Rejected: it corrupts Grist round trips.
- **Neutralise only cells that parse as formulas.** Rejected: spreadsheets differ on what counts,
  and a short fixed list is checkable.

## Consequences

- A text value such as `"-5"` from Jira or Salesforce is written as `'-5`. Numbers (non-`str`
  cells) are untouched.
- A Grist export of a cell starting with `=` still opens as a formula in a spreadsheet. That risk
  is accepted so Grist round trips stay exact.

## Verification

- `export_files.render`, `connectors/jira.py`, `connectors/salesforce.py`.
- `tests/unit/test_export_files.py` (`formula_leading_cells`), and one export test in each of
  `tests/unit/connectors/test_jira_connector.py` and `test_salesforce_connector.py`.

## Related

- ADR [0155](0155-exports-write-a-file-and-return-only-counts-and-a-hash.md)
