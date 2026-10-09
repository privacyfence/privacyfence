# ADR 0132: Salesforce reports page by a unique key column

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/salesforce_client.py` (`run_report_page`,
`build_keyset_metadata`, `report_page_keys`, `ReportPagingError`), `src/privacyfence/connectors/salesforce.py`
(`salesforce_run_report`), `src/privacyfence/plugins/source_ops.py` (`salesforce.report_run`),
`plugin-sdk/src/privacyfence_plugin_sdk/plugin.py` (`report_pages`).
Amends [ADR 0128](0128-plugin-source-reads-never-truncate.md).

## Context

Salesforce's Analytics API returns at most 2,000 detail rows per report run, and an asynchronous
run has the same cap. `salesforce_run_report` and the plugin source operation `salesforce.report_run`
stopped there and told the caller to narrow the report with filters. [ADR 0128](0128-plugin-source-reads-never-truncate.md)
left it as the one source operation that did not page.
A read that quietly returns the first 2,000 rows of a larger report is worse than one that fails
([issue #854](https://github.com/privacyfence/privacyfence/issues/854)).

## Decision

A caller passes `page_by`, a report column whose value is unique per row, and reads the whole
report one page at a time. Without `page_by` nothing changes.

- Each page is one run of the saved report, sorted by `page_by` ascending, with one more filter:
  `page_by` greater than the last key of the previous page. That filter is ANDed onto the saved
  report's boolean logic and the caller's filters, so paging only narrows. The saved report is never
  changed and no SOQL is used.
- A grouped report is flattened for the run: it is read as one table with the grouping columns
  first. The grouping values were already visible as group labels, so the run shows nothing new.
  A report grouped by week, month, quarter or year, a joined report and a report with a row limit
  are refused, because reading them as one table would show more than the saved report.
- The cursor is stateless, bound to the operation and the parameters, and carries the number of
  runs done, the rows returned, the last key and the rows still to come.
- A paged read finishes or fails with no rows. It fails on a `page_by` that is not a column of the
  run, has a blank or unusable value, is not unique, does not advance, or cannot be read as one
  table; on a read that loses rows; and past `salesforce.report_max_pages` runs (default 50).
- Lost rows are detected by continuity of the report's `RowCount` aggregate, which Salesforce
  computes over every matching row: the next run must match exactly the rows that were left, and
  the last page must return all it matched. This needs no text collation and catches equal keys on
  both sides of a page boundary, blank keys and case-only differences.
- No error message holds a cell value, because an error reaches the AI client before any approval
  card.
- Each page of `salesforce_run_report` is its own card. On a plugin, a page that does not fit the
  result budget is cut to fit, and the next run starts after the last key served.

## Alternatives considered

- **Offset paging, or Salesforce's asynchronous runs.** The Analytics API has no offset, and
  asynchronous runs are capped at 2,000 detail rows too.
- **SOQL over the report's objects.** The business maintains the report; re-deriving its filters,
  joins and columns in SOQL would drift from it.
- **Refusing non-tabular reports.** Simpler, but the reports that need paging are often summary
  reports. Flattening them shows no new data.
- **A stateful server-side cursor that de-duplicates across pages.** It needs daemon state with a
  lifetime and cleanup. The stateless keyset cursor, the refusal of a repeated last key and the
  `RowCount` check give the same guarantee, and the SDK also refuses a key repeated across pages.
- **Splitting a run into sub-pages with a row offset.** Re-running a page and skipping rows breaks
  when rows change in between. Continuing after the last key served needs no offset.
- **Comparing key order in Python** to detect a column that does not advance. Salesforce's text
  collation differs from Python's, so valid reports would be rejected. The check is whether the
  previous last key came back.

## Consequences

- A report of any size can be read, at the cost of one report run per page against the org's hourly
  report-run limits.
- `page_by` must be unique per row with no blanks. An Auto Number field added to the report is
  best. Record-ID columns are not guaranteed to work, because Salesforce compares text
  case-insensitively; the `RowCount` check then refuses the read instead of losing a row.
- `salesforce.report_max_pages` is a cost cap, not a boundary. The cursor is unsigned, so a caller
  can reset the run count; it reads with its own rights either way
  ([ADR 0128](0128-plugin-source-reads-never-truncate.md)).
- Aggregates on a later page cover only the rows from that page on, not the whole report.
- Pages may change between calls when the report's data changes; the cursor does not freeze it. A
  change that loses rows fails the read through the `RowCount` check.

## Verification

`tests/unit/test_salesforce_client.py` and `tests/unit/test_salesforce_report_paging.py` (the 4,500-row
read, filter logic, flattening and each refusal), `TestRunReportPaged` in
`tests/unit/connectors/test_salesforce_connector.py`, the Salesforce tests in
`tests/unit/plugins/test_source_ops.py`, `TestSalesforce` in `tests/integration/test_plugin_paging.py`,
`tests/unit/plugin_sdk/test_plugin.py` for `report_pages`, and the `run_report_page` row of
`scripts/qa_fixture_recorder.py`, which checks `sortBy`, `greaterThan`, `RowCount` and flattening
against the QA org.

## Related

- [Issue #854](https://github.com/privacyfence/privacyfence/issues/854)
- [ADR 0128](0128-plugin-source-reads-never-truncate.md)
