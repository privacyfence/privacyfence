# ADR 0162: grist_update_csv can clear a value from an empty cell

## Status

Accepted — 2026-10-11. Implemented.
Amends ADR 0148. Its consequence "an empty cell cannot clear a value" now holds only while `clear_empty` is off.

## Context

ADR 0148 made an empty CSV cell leave the value unchanged. A value that became empty at the source
then cannot be cleared except by per-row `grist_update_records` calls, which a recipe cannot express.

## Decision

`grist_update_csv` takes `clear_empty` (default false). With it true, an empty cell clears the
value: it is read as `None` and counts as a changed cell only when the current value is neither
`None` nor `""`. Columns not in the CSV are never touched. New rows (`create_missing`) never get
explicit nulls. The card counts the cells to clear ("Cells to clear") and shows a cleared cell as
`(empty)`; the approval args carry the flag, so a clearing update needs its own approval. An empty
key cell is still refused. `grist_import_csv` does not get the flag. ADR 0145 holds: clearing a cell
is an update, not a delete.

## Alternatives considered

- **Clearing by default.** It would silently change what every existing CSV update does.
- **A sentinel string in the CSV for "clear".** A value that looks like data could clear a cell by
  accident; a per-call flag the card counts is explicit.

## Consequences

- A recipe can propagate a value emptied at the source in one approval.
- The default is unchanged, so earlier approvals and rules behave as before.

## Verification

- `tests/unit/test_grist_csv.py` (`test_keep_empty_*`, `test_default_still_leaves_empty_cells_out`).
- `tests/unit/connectors/test_grist_connector.py` (`test_clear_empty_*`, `test_card_counts_cleared_cells`).

## Related

- [`grist_csv.py`](../../src/privacyfence/grist_csv.py), [`connectors/grist.py`](../../src/privacyfence/connectors/grist.py)
- ADR [0145](0145-the-grist-connector-only-adds.md), ADR [0148](0148-grist-bulk-writes-take-a-csv-file-and-are-reviewed-as-counts-and-a-sample.md)
