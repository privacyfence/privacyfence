# ADR 0147: Grist bulk writes take a CSV file and are reviewed as counts and a sample

## Status

Accepted — 2026-10-10. Implemented.

## Context

`grist_add_records` and `grist_update_records` each take an inline JSON array of at most 100
records. Loading or refreshing a table of thousands of rows takes dozens of calls, each with its
own approval card, and the whole payload passes through the model's context twice: once to read the
data and once to write it back out as tool arguments.

## Decision

Two tools take a CSV file instead of inline records, and write it in chunks behind one approval
card.

- **`grist_import_csv`** adds every row as a new record. **`grist_update_csv`** matches each row to
  an existing record by one key column, changes the cells that differ, and can add the rows that
  match nothing when `create_missing` is true. Nothing deletes, so ADR 0145 still holds. The four
  existing write tools are unchanged.
- **The file is handed over as `drive_upload_file` takes one** (ADR 0007): `local_path`, or
  `upload_id` from `privacyfence_create_upload_slot` (ADR 0028). On an organization install
  `local_path` is refused, because the daemon's disk is not the user's. The slot is consumed only
  after the gate has passed (ADR 0102).
- **The file is parsed completely before the card is shown** (`grist_csv.py`, pure functions, no
  I/O). The header names columns by id or label; every cell is coerced to its column's type with
  ASCII-only forms; formula columns and types other than Text, Numeric, Int, Bool, Date, Choice and
  Any are refused. Any problem refuses the whole file, naming the line and column and never the
  cell's value, because the text of a failed call is logged. The limits are 20000 rows, 100
  columns, 10000 characters per cell and 50 MB.
- **One popup card per call, bound to the file** (ADR 0073). The card names the server, document
  and table, and shows the counts, a sample of 20 rows (import) or 20 changed cells and unmatched
  rows (update), and the file's SHA-256. The approval's `args` carry that SHA-256 and the options,
  so another file, key column or `create_missing` needs a new approval. `raw_data` holds no row
  values, because it can reach the audit trail. The informational write-content flags scan the first
  million characters of the file.
- **Update reads first, then writes by record id.** The connector looks up the CSV's keys with
  filtered reads of at most 200 keys each, and refuses a key that matches more than one record. It
  compares each cell with the current one (`3` and `3.0` are the same number), so the card counts
  exactly the cells that change. The updates are written first, then the adds.
- **Writes go in chunks** of at most 500 rows and one million characters of JSON, through the
  existing `add_records` and `update_records` client methods. A refused chunk raises an error that
  says how far the write got and never carries Grist's own text; the client has logged that.

## Alternatives considered

- **Bigger inline JSON batches on the existing tools.** The whole payload would still pass through
  the model's context and output tokens. A file does not.
- **`POST /docs/{id}/apply` with `BulkAddRecord` or `BulkUpdateRecord`**, which would make one
  atomic action. It is the user-actions API, broader than the records endpoints the connector
  reviews, and ADR 0145's only-adds rule would then rest on our own filtering. Chunks over the
  records endpoint are not atomic, so the partial-failure message says how far the write got.
- **`PUT /records` upsert with `require`.** The card could not say beforehand how many rows would
  update, be added, or match several records. The connector reads first and writes by record id.
- **Several key columns.** One key column covers the common case and keeps the lookup a single
  `filter`. Composite keys are a later decision.
- **Every row on the card.** That is unreadable for thousands of rows. The card shows counts, a
  sample and the file's SHA-256, and the approval is bound to that SHA-256 (ADR 0073).
- **The real PII scan with forced confirmation (`upload_pii_scan_text`).** Nearly every CSV of
  records would trip it, and the data goes to the user's own Grist. The informational write-content
  flags still run over the file.

## Consequences

- Writes are not atomic. A failure part-way leaves the earlier chunks in the table. An import says
  which CSV line the written rows reach; an update can be run again with the same file and finishes
  the rest, because it compares with the table again.
- A file has at most 20000 rows and 50 MB. A larger table is loaded in several files, each with its
  own approval.
- There is one key column. A table keyed by several columns needs a helper column.
- An empty cell cannot clear a value: an update leaves it unchanged.
- A `grist.document` rule with create or update verbs saved from Settings from now on also covers
  the CSV tools for that document, because `grist_import_csv` is a create and `grist_update_csv` an
  update. Stored rules hold operation keys, so existing rules are not widened.
- No new endpoint, OAuth scope or client method.

## Verification

- `tests/unit/test_grist_csv.py`: every parse rule and error message, the key index and key
  comparison, and that no message contains a cell's text.
- `tests/unit/connectors/test_grist_connector.py`: `TestCsvFileHandoff`, `TestImportCsv` and
  `TestUpdateCsv` (the card, chunking, partial-failure messages, the approval bound to the file, and
  the upload slot surviving a pending approval and being consumed after it).
- `tests/unit/policy`, `tests/unit/test_write_effects.py` and
  `tests/unit/connectors/test_readme_manifest_alignment.py`: the registrations and gates.

## Related

- [`grist_csv.py`](../../src/privacyfence/grist_csv.py), [`connectors/grist.py`](../../src/privacyfence/connectors/grist.py)
- ADR [0007](0007-local-file-bridge.md), ADR [0028](0028-clients-without-the-shim-get-capability-urls.md), ADR [0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md), ADR [0102](0102-an-upload-slot-is-consumed-after-the-gate-not-before.md)
- ADR [0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md), ADR [0145](0145-the-grist-connector-only-adds.md), ADR [0146](0146-grist-records-page-by-record-id.md)
