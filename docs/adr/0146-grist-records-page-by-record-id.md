# ADR 0146: Grist records page by record id

## Status

Accepted — 2026-10-10. Implemented.

## Context

`grist_get_records` returned at most 500 records and said `truncated` when more matched, with no
way to read the rest. An assistant asked to join three tables of a few thousand rows each could see
only the first 500 of each. `filter` matches listed values only and `sort` gives no position, so
paging by hand was unreliable.

What Grist's REST API offers, read from the API specification (`grist-help`'s `api/grist.yml`) and
`grist-core`'s `DocApi.ts` and `DocApiUtils.ts`:

- `GET /docs/{docId}/tables/{tableId}/records` takes `filter` (exact values per column), `sort`,
  `limit`, `hidden` and `cellFormat`, and nothing else. There is no offset or cursor. The server
  loads the filtered table, then sorts, then cuts at `limit`. `id` is accepted as a sort column;
  without `sort` the order is unspecified.
- `POST /docs/{docId}/sql` runs a parameterized `SELECT`, but only for a caller who may copy the
  whole document (`canCopyEverything`). A user whose access rules hide any part of the document
  gets HTTP 403, and its OAuth scope is not stated in the specification. Its cells come back as
  SQLite stores them, not in the records endpoint's format.

## Decision

Keyset paging by record id, worked out by the client over the records endpoint.

- `grist_get_records` takes `after_id` (default 0) and returns `next_after_id`.
- Without `sort`, the client asks Grist for `sort=id`, so the default order is record-id order.
  With `after_id`, it asks for `after_id + limit + 1` rows and keeps those with an id above
  `after_id`. At most `after_id` records have an id at or below it, so that fetch always reaches
  `limit + 1` rows past it when they exist.
- `next_after_id` is the last record id of the page when more follow, and null on the last page.
  It is also null whenever `sort` is set: `after_id` cannot be combined with `sort`, and the tool
  refuses the pair.
- `after_id` is validated like `limit`: a whole number from 0 to 2^31 − 1, not a boolean.
- Each page is its own review card. The card's **Page** line says where the page starts (the first
  page, or after record #N), the record ids it holds when the order is by id, and whether more
  follow. There is no total count, on the card or in the result.

## Alternatives considered

- **An `offset` parameter.** The records endpoint has none. Emulating it by fetching
  `offset + limit` rows works, but a row added or deleted between calls shifts every later page,
  repeating or skipping rows.
- **The SQL endpoint** with a fixed `SELECT … WHERE id > ? ORDER BY id LIMIT ?` built by the
  client. It is refused for anyone without full copy access, its OAuth scope is not documented, it
  returns cells in a different format from the first page, and reproducing `filter` would mean
  building SQL from column ids. It is also a second, broader read path to review.
- **One large fetch, windowed by the client** (fetch everything, return a window). It returns the
  same rows as this decision but transfers the whole table on every page.
- **A `total` field.** Exact only with a full fetch of the table on every page, which the bounded
  fetch avoids. The assistant knows it is done when `truncated` is false.

## Consequences

- A table of several thousand rows can be read completely, one approved page at a time. A row added
  between pages has a higher id and arrives on a later page; a deleted row just stops appearing.
  Neither repeats or skips another row.
- Grist gives a new row the next id after the highest one, so an id can be reused only after the
  highest-id row is deleted. A row added that way after the reader has passed that id is missed;
  it was added after the read started.
- Page *n* transfers up to `after_id + limit + 1` rows, so reading a table costs about the square
  of its pages in transfer. Grist loads the whole table for every records call anyway, so the
  server-side cost is the same as for one page; for tables of a few thousand rows the traffic is
  small. Very large tables would want a cheaper path, which is a new decision.
- The default order without `sort` is now record-id order, which was previously unspecified.
- No new OAuth scope, endpoint or write capability. ADR 0145 is unchanged.

## Verification

- `tests/unit/test_grist_client.py`: `TestGetRecords` and `TestPaging` (first, middle and last
  page, a table with gaps, an exact multiple of the limit, an empty table, a row added and a row
  deleted between pages, against a fake server with Grist's filter, sort and limit rules).
- `tests/unit/connectors/test_grist_connector.py`: `TestGetRecords` (the Page line, the
  continuation field and each invalid `after_id`).

## Related

- [`grist_client.py`](../../src/privacyfence/grist_client.py), [`connectors/grist.py`](../../src/privacyfence/connectors/grist.py)
- ADR [0133](0133-salesforce-reports-page-by-a-unique-key-column.md) (keyset paging for Salesforce reports)
- ADR [0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md), ADR [0145](0145-the-grist-connector-only-adds.md)
