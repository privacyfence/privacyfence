# ADR 0156: grist_export_sql reads through Grist's SQL endpoint

## Status

Accepted — 2026-10-11. Implemented.
Amends ADR 0146. Its decision to page `grist_get_records` by record id stands; ADR 0145 is unchanged.

## Context

ADR 0146 considered `POST /api/docs/{docId}/sql` for paging `grist_get_records` and rejected it
there: Grist allows it only for a caller who may copy the whole document (HTTP 403 otherwise), its
cells come back as SQLite stores them, and its OAuth scope is not documented. Those reasons were
about paging records.

A scheduled sync now needs a join across tables in one approval, which the records endpoint cannot
do. ADR 0155 gives the export tools their shape: one review card, a file, and a result of counts and
a hash. A caller with full copy access can also read the document's internal tables (`_grist_*`,
`_gristsys_*`), which hold the action history with old and deleted values.

## Decision

`grist_export_sql` runs one read-only query through the SQL endpoint and writes the result as ADR
0155 describes.

- **`SELECT` only, checked in the connector.** The statement is stripped of one trailing `;` and
  must start with `SELECT` or `WITH`, be at most 10,000 characters, and contain no `;`, `--` or
  `/*`. Values go in a separate JSON array of up to 100 scalars, bound to `?` placeholders.
- **The query is always sent as a subquery** of `SELECT * FROM (…) LIMIT 50001`, so Grist sees one
  `SELECT`, and a result of more than 50,000 rows is refused.
- **Grist's internal tables are refused.** Any statement containing `_grist` is rejected, so a
  document read rule never reaches the action history.
- **A 403 becomes a message.** It says Grist runs SQL only for people who can copy the whole
  document and points to `grist_export_records`.
- **Cells are written as Grist stores them** (dates as seconds since 1970, true/false as 1/0).
  The tool description says so; no conversion is attempted without column types.
- **Same operation key as a records read** (`grist.read_records`), verb read, review gate. A
  `grist.document` read rule covers it: the same document, and Grist's own access check is stricter
  than a records read.
- **The CI live check proves the endpoint with an API key only.** `--check`'s `run_sql` row
  compares the ids from `SELECT id …` with one records read; it passed in
  [the connector-live-check run](https://github.com/privacyfence/privacyfence/actions/runs/38130756839)
  ("2 id(s) match one records read"). Whether an OAuth sign-in's scope covers `/sql` is checked by
  hand against a real server and recorded in the pull request, not here.
- **ADR 0145 is unchanged.** The endpoint is used for `SELECT` only; the connector still never
  deletes.

## Alternatives considered

- **Paging `grist_get_records` through `/sql`.** Still rejected, for ADR 0146's reasons.
- **Allowing `_grist` tables.** A document read rule was made for the document's records, not the
  action history.
- **A `timeout` in the request body.** The Grist server caps it by configuration PrivacyFence cannot
  see, so the server default applies.
- **Parsing the SQL to prove it read-only.** Prefix, length and character checks plus the subquery
  wrap and Grist's own read-only endpoint are enough; a parser would be a larger attack surface than
  the rule it enforces.

## Consequences

- A join across tables is one approval, or none under a document rule.
- Only people who can copy the whole document can use the tool; others get the 403 message.
- Cells of date and boolean columns need converting by the reader, and a file written from a query
  is not guaranteed to round-trip through `grist_update_csv`.
- If an OAuth sign-in's scope does not include `/sql`, the tool fails there with the 403 message;
  widening the scope is a separate decision.
- A large join can hit Grist's SQL time limit or the client's 30-second HTTP timeout.

## Verification

- `tests/unit/connectors/test_grist_connector.py`: `TestExportSql` (validation, the subquery wrap,
  the `_grist` refusal, the 403 message, the card and the approval bound to the file), and
  `TestExportRecords` for the flow it shares.
- `tests/unit/test_grist_client.py`: `GristClient.run_sql`.
- `tests/unit/test_qa_fixture_recorder.py`: the `run_sql` check row.
- `connector-live-check.yml`: the `grist | run_sql` row, passing in
  [run 38130756839](https://github.com/privacyfence/privacyfence/actions/runs/38130756839).

## Related

- [`grist_client.py`](../../src/privacyfence/grist_client.py), [`connectors/grist.py`](../../src/privacyfence/connectors/grist.py), [`scripts/qa_fixture_recorder.py`](../../scripts/qa_fixture_recorder.py)
- ADR [0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md), ADR [0145](0145-the-grist-connector-only-adds.md), ADR [0146](0146-grist-records-page-by-record-id.md), ADR [0155](0155-exports-write-a-file-and-return-only-counts-and-a-hash.md)
