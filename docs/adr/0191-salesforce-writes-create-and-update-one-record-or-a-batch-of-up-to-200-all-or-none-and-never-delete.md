# ADR 0191: Salesforce writes create and update one record or a batch of up to 200, all or none, and never delete

## Status

Accepted — 2026-10-16. Implemented: `src/privacyfence/salesforce_client.py`,
`src/privacyfence/salesforce_writes.py`, `src/privacyfence/connectors/salesforce.py`.

## Context

The Salesforce connector could only read. The OAuth scope it already holds, `api refresh_token`,
allows writes, so nobody has to connect again; what a user can change is decided by their own
Salesforce profile and permission sets. An assistant that can look up a record is asked, next, to
fix a field or to add the records it just found missing. The question is how much writing to offer,
and in what shapes.

## Decision

- **Four tools, all behind a popup approval card** (`gate="popup"`, `read_only=False`,
  `destructive=False`, a required `reason`): `salesforce_create_record`,
  `salesforce_update_record`, `salesforce_create_records` and `salesforce_update_records`.
- **A batch holds 1 to 200 records of one object type and is written all or none.** The client
  sends it to `composite/sobjects` with `allOrNone` set, so if Salesforce refuses one record it
  saves none and the error names each refused record by its position.
- **Batches are inline JSON.** Records are passed as a JSON array in the tool call, at most
  1,000,000 characters.
- **Updates find records by `Id` or by an External ID field** (`key_field`, default `Id`). The key
  is never changed itself.
- **Updates only change; they never create.** A key that matches no record refuses the whole call.
  Records are created with the create tools.
- **Nothing deletes.** No tool, and no option on a tool, removes a record.
- **One object type per call**, so that the describe check and the rule scope cover one type.
- **Results carry ids and counts only.** They never say which fields or records already held the
  given values; that is Salesforce data, which the caller gets through a reviewed read.

## Alternatives considered

- **CSV-file bulk tools (`salesforce_import_csv`)** — the maintainer chose inline JSON only. A batch
  of at most 200 records keeps a card readable and avoids chunked, partly-failed writes.
- **Bulk API 2.0** — it runs asynchronous ingest jobs with no per-record result before commit. A
  card could not promise that the job is all or none, and polling adds a long-running call.
- **Native upsert (`PATCH /sobjects/{type}/{extIdField}/{value}`) or a `create_missing` option on
  `salesforce_update_records`** — an upsert cannot tell beforehand which records it would create. A
  rule that allows only *update* on an object type would then auto-accept creates. Unmatched keys
  are refused instead.
- **Delete** — a delete can only be previewed as ids. A mistaken create or update can be fixed in
  Salesforce, and Salesforce keeps field history where the org turns it on.
- **Atomic multi-object batches** — one object type per call keeps the describe check and the rule
  scope to one type.

## Consequences

- An assistant can change the org only through a card the user approved, and only what the user's
  own Salesforce permissions allow.
- A refused batch saves nothing, so there is no partly-written state to clean up. A batch over 200
  records is split by the caller into several approvals.
- A mistaken write is undone by a second write or by hand in Salesforce; there is no delete tool to
  undo a mistaken create. Records created by mistake are deleted in Salesforce itself.
- The org's own automation (flows, triggers) may still react to an approved write; the card says so.
- Salesforce limits apply as they stand: the composite API needs API version 42.0 or later, which
  the client's default exceeds.

## Verification

`tests/unit/test_salesforce_client.py::TestCreateRecord`, `::TestUpdateRecord`,
`::TestCompositeWrites`, `::TestFindRecords`;
`tests/unit/connectors/test_salesforce_connector.py::TestCreateRecord`, `::TestUpdateRecord`,
`::TestCreateRecords`, `::TestUpdateRecords`;
`tests/unit/test_write_effects.py` and `tests/unit/connectors/test_readme_manifest_alignment.py`
for the popup gate and the effect text of each tool;
`scripts/qa_fixture_recorder.py --lifecycle salesforce` (`tests/unit/test_qa_fixture_recorder.py::TestLifecycleSalesforce`)
for the create, update and batch calls against a real org. The lifecycle check never deletes the
records it creates.

## Related

[ADR 0145](0145-the-grist-connector-only-adds.md),
[ADR 0148](0148-grist-bulk-writes-take-a-csv-file-and-are-reviewed-as-counts-and-a-sample.md),
[ADR 0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md),
[ADR 0192](0192-a-salesforce-write-is-checked-against-the-objects-describe-and-its-current-values-before-the-card.md),
[ADR 0193](0193-the-salesforce-object-type-rule-also-covers-creating-and-updating-records-of-that-type.md),
`src/privacyfence/salesforce_client.py`, `src/privacyfence/salesforce_writes.py`,
`src/privacyfence/connectors/salesforce.py`.
