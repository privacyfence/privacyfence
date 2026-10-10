# ADR 0145: The Grist connector only adds

## Status

Accepted — 2026-10-10. Implemented.

## Context

A Grist document is a database an assistant could change in many ways. Records can be added,
updated and deleted; tables and columns can be added, renamed, retyped and deleted. The OAuth
scope `doc.schema:write` allows all of the schema changes.

## Decision

The connector only adds. It lists tables and columns, reads records after review, adds and updates
records, and adds tables and columns, each write behind a popup card. Nothing deletes a record,
column or table, renames anything or changes a column's type. The client never calls
`records/delete` or any delete, rename or column-modify endpoint, and the connector registers no
destructive tool. Adding any of them later is a new decision.

## Alternatives considered

- **`grist_delete_records`** — a delete cannot be previewed as anything but ids.
- **Schema-modify tools** — a type change can rewrite every value in a column, and a rename can
  break formulas elsewhere in the document.

## Consequences

Cleaning up a document stays a human job in Grist. A mistaken add can be undone in Grist's own
history. The unused breadth of `doc.schema:write` is limited by what the connector does with it
(ADR 0142).

## Verification

- `tests/unit/test_connector_tool_annotations.py`: the destructive set is exactly the deleting
  tools, and no Grist tool is in it.
- `tests/unit/test_grist_client.py` and `tests/unit/connectors/test_grist_connector.py`: the client
  methods and tools are the seven the connector defines.
- `tests/unit/connectors/test_readme_manifest_alignment.py`: every tool's gate matches its source.

## Related

- [`connectors/grist.py`](../../src/privacyfence/connectors/grist.py), [`grist_client.py`](../../src/privacyfence/grist_client.py)
- ADR [0075](0075-apps-script-gets-no-run-tool.md) (a connector that leaves out a dangerous capability)
- ADR [0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md), ADR [0142](0142-grist-connects-by-oauth-with-an-app-in-the-bundle-and-by-api-key-otherwise.md)
