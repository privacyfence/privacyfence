# ADR 0134: Paged plugin source reads read one version of the data and measure UTF-8 bytes

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/plugins/source_ops.py`,
`src/privacyfence/plugins/spool.py` (`put_rows`, `rows_page`),
`src/privacyfence/plugins/rpc.py`.
Amends [ADR 0128](0128-plugin-source-reads-never-truncate.md).

## Context

[ADR 0128](0128-plugin-source-reads-never-truncate.md) made every source read return all of its data
or a page with a cursor. Two things in that design let a paged read go wrong without any error.
Each Sheets page was a fresh request for a row range, so a sheet edited between pages gave a copy
made of two versions. A Confluence page was fetched again for each slice of its body, with the same
result. And sizes were counted as escaped ASCII, in which an accented letter costs six bytes where
UTF-8 costs two, so a page of accented text was cut at roughly a third of the size that fits on the
JSON-RPC line.

## Decision

- A Sheets read fetches the range once. When it does not fit one page, the rows are held as a
  private snapshot in the owner-only spool and later pages are served from it. A plugin holds at
  most 4 snapshots; a snapshot is deleted after 10 idle minutes. A cursor whose snapshot is gone is
  `upstream_error` with `data.reason` `cursor_expired`, and the plugin reads the range again from
  the start.
- A Confluence cursor carries the page's version. If the version changed between pages, the read
  fails with `upstream_error` and `data.reason` `revision_changed` instead of joining two versions.
- Sizes on the source path and on the JSON-RPC line are UTF-8 bytes of the JSON. A lone surrogate
  has no UTF-8 form and falls back to the escaped form of the JSON.
- A Drive file of a Google type that cannot be exported or downloaded (a Form, a Drawing, a folder,
  a shortcut) is `invalid_params` with `data.reason` `not_downloadable`, not an empty read.

## Alternatives considered

- **A1 range arithmetic.** Rejected. Asking Google for the next rows by range has to parse named
  ranges, R1C1 notation, quoted sheet names and whole-sheet ranges, and still reads two versions.
- **Fetching again and comparing.** Rejected. It saves nothing: the plugin pays for the second read
  and still has no version to continue from.
- **Counting escaped ASCII everywhere.** Rejected. It halves the usable limit for accented text.

## Consequences

- Sheet content rests on disk in the spool for up to 10 idle minutes, as Drive exports already do
  ([ADR 0123](0123-the-plugin-source-api-is-ungated-but-audited-without-content.md),
  [ADR 0129](0129-drive-binary-downloads-for-plugins-are-http-range-reads-with-no-size-cap.md)).
- Cursors issued before this change are invalid.
- A page of non-ASCII text now holds more rows or characters than before.

## Verification

`tests/unit/plugins/test_source_ops.py`, `tests/unit/plugins/test_spool.py`,
`tests/unit/plugins/test_rpc.py`.

## Related

- [Issue 846](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0128](0128-plugin-source-reads-never-truncate.md)
- [ADR 0129](0129-drive-binary-downloads-for-plugins-are-http-range-reads-with-no-size-cap.md)
