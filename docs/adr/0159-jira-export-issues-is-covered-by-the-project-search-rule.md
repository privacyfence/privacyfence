# ADR 0159: jira_export_issues is covered by the project search rule

## Status

Accepted — 2026-10-11. Implemented.

## Context

`jira_search_issues_with_fields` returns at most 100 issues per call. A scheduled sync of a project
needs every issue a query matches, and the other connectors already have an export tool that writes
a file and returns counts (ADR 0155).

## Decision

`jira_export_issues` writes every issue a JQL query matches, up to 10,000, to a CSV or JSON file
after one review card, with the standard columns and any extra fields named. It uses the
`jira.read_issue` operation with the SEARCH verb, so the existing "this project" search rule
(`approved_project_keys_all_results`) and its card button cover it. The gate's `raw_data` carries
the issue keys and no field value. A query that matches nothing writes no file and returns rows 0
without a card.

## Alternatives considered

- **50,000 issues, like Grist.** Jira returns at most 100 issues per request when fields are
  requested, so 50,000 would be 500 sequential requests inside one tool call; 10,000 is 100.
- **A card for an empty result.** The project rule matches on the results' keys and cannot match
  none, so an incremental pull that finds nothing would need a human every time. That a JQL
  matches nothing is what the auto-approved `jira_search_issues` already returns.
- **A new operation key.** The existing per-project search rules would not cover it.

## Consequences

- One approval, or one rule, moves a whole project.
- A query over 10,000 issues is refused before more is read; the user narrows it and exports in parts.

## Verification

- `tests/unit/test_jira_client.py`: `TestExportIssues`. `tests/unit/connectors/test_jira_connector.py`:
  `TestExportIssues`. `tests/unit/test_qa_fixture_recorder.py` (the `export_issues` row).

## Related

- [`jira_client.py`](../../src/privacyfence/jira_client.py), [`connectors/jira.py`](../../src/privacyfence/connectors/jira.py),
  [`export_files.py`](../../src/privacyfence/export_files.py)
- ADR [0155](0155-exports-write-a-file-and-return-only-counts-and-a-hash.md), ADR [0157](0157-a-rule-can-let-personal-data-through-tools-whose-result-holds-no-row.md)
