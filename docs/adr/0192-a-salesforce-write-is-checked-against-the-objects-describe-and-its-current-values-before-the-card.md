# ADR 0192: A Salesforce write is checked against the object's describe and its current values before the card

## Status

Accepted — 2026-10-16. Implemented: `src/privacyfence/salesforce_writes.py` (checks),
`src/privacyfence/connectors/salesforce.py` (`_create_record` and the three other write methods,
`SalesforceWriteRefusedError`, `SalesforceWriteUnconfirmedError`).
This is a deliberate exception to [ADR 0148](0148-grist-bulk-writes-take-a-csv-file-and-are-reviewed-as-counts-and-a-sample.md)
for approved writes only (see Decision).

## Context

An approval card is only worth reading if the call it describes can succeed and if it shows what
will change. A write that Salesforce would refuse for an unknown field or a wrong type should not
cost the user an approval. An update card that shows only the new values cannot be judged. And the
messages sent back to the assistant must not become a way to read Salesforce data that no one
reviewed: ADR 0148 set the rule that errors never carry the provider's own cell text.

## Decision

- **Before any card, every write is checked from the request and the object's own field
  definitions** (`describe`, through `SalesforceClient.describe_fields`). All of it happens before
  the first approval, so a malformed call costs no approval:
  - the JSON parses, is an object or an array of 1 to 200 objects, names no field twice, and holds
    only text, numbers, true/false or null;
  - each field exists on the object (any letter case; the card shows the canonical name), is
    `createable` or `updateable` for the operation, has a type PrivacyFence writes, takes a value of
    that type, and is `nillable` before it is cleared with null;
  - `key_field` is `Id` or a field marked External ID of type id, string or email.
- **Pre-approval messages never carry a value read from Salesforce.** They name the parameter, the
  record position and the field API name, and may repeat a key value the caller passed in.
- **An update reads the current values and shows them.** The card lists each field's current and
  new value (`Field | Current | New`, and `Record | Field | Current | New` for a batch). Only fields
  whose value differs are written; `values_differ` compares by type, so the same instant in two
  datetime notations, a 15- and an 18-character id, or a multipicklist in another order are not
  changes.
- **A call that would change nothing still gets a card and makes no write.** Refusing it before the
  card would tell the caller a current value with no review and no audit entry, and no result says
  which values already matched.
- **Batch updates match by `Id` or an External ID field.** A key that matches no record, or more
  than one, refuses the call before the card. That tells the caller whether a record with that key
  exists, as `salesforce_get_record` already does for an id. Keys are compared as Salesforce does:
  ids by their first 15 characters, External ID text without case.
- **The card is reproducible.** It is built from the request, the describe and the current values,
  in input order, so an identical re-issue of a deferred call reproduces its digest and can collect
  the decision. Because the card shows current values, a deferred approval is collected only while
  the record is unchanged; a change between approval and re-issue gets a new card.
- **After approval, a refused write raises `SalesforceWriteRefusedError`**, a named `RuntimeError`
  that `safe_errors.public_message` passes through. It carries Salesforce's status codes, field API
  names and messages cut to 200 characters (the first 10 failures; a rollback marker is left out).
  This departs from ADR 0148's "never the provider's own text": a validation rule's message is
  written by the org's administrator, it is the only way the caller can learn what to fix, and it
  reaches the client only after the write was approved.
- **A 5xx or a dropped connection after a write raises `SalesforceWriteUnconfirmedError`**, also a
  named error the caller sees. It says the write may or may not have been saved and to check the
  records before trying again, and never carries Salesforce's own text. Nothing is retried; only an
  expired session is refreshed and the request repeated, because Salesforce refused it before
  running it ([ADR 0105](0105-a-google-read-is-retried-once-when-its-connection-drops.md)).
- **The result and the audit data hold names and counts only.** `raw_data` carries the object type,
  the sorted field names and the record count; the policy `args` carry the object type, plus the
  record id or key field. No field value or hash of the payload is passed.

## Alternatives considered

- **Refusing an update that would change nothing before the card** — it would reveal that a field
  already holds a value, without review or audit.
- **Showing only new values on an update card** — the user could not tell a correction from an
  overwrite.
- **Leaving Salesforce's messages out of write errors, as Grist's CSV errors do (ADR 0148)** — the
  caller could not fix a validation-rule failure, and the text is shown only after approval.
- **Retrying a write after a 5xx or a dropped connection** — a write may have been saved, so a
  retry could create a record twice.
- **Letting Salesforce be the only validator** — every typo would cost an approval and a round
  trip, and the card could not show the field's label.

## Consequences

- An unknown field, a read-only field or a wrong type is corrected by the assistant without the
  user seeing a card.
- Each update costs one describe call and one read of the current values before the card.
- The assistant can learn that a key exists or does not exist, and the name of a field, before any
  approval; both are already visible through `salesforce_get_record`.
- A write whose answer is lost is reported as unconfirmed, and the assistant has to read the
  records to find out.
- Types PrivacyFence does not write (for example base64 and address compound fields) are refused
  with their type named.

## Verification

`tests/unit/test_salesforce_writes.py::TestParse`, `::TestResolveFields`, `::TestKeyField`,
`::TestValuesDiffer`, `::TestCellText`, `::TestNoValueInMessages`;
`tests/unit/test_salesforce_client.py::TestDescribeFields`, `::TestCreateRecord`,
`::TestCompositeWrites`, `::TestWriteFailures`, `::TestFindRecords`;
`tests/unit/connectors/test_salesforce_connector.py::TestCreateRecord`, `::TestUpdateRecord`,
`::TestCreateRecords`, `::TestUpdateRecords`.

## Related

[ADR 0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md),
[ADR 0105](0105-a-google-read-is-retried-once-when-its-connection-drops.md),
[ADR 0148](0148-grist-bulk-writes-take-a-csv-file-and-are-reviewed-as-counts-and-a-sample.md),
[ADR 0175](0175-an-approval-is-bound-to-the-whole-request-and-what-its-card-showed.md),
[ADR 0191](0191-salesforce-writes-create-and-update-one-record-or-a-batch-of-up-to-200-all-or-none-and-never-delete.md),
`src/privacyfence/salesforce_client.py`, `src/privacyfence/salesforce_writes.py`,
`src/privacyfence/connectors/salesforce.py`.
