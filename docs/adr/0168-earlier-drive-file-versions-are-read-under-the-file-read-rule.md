# ADR 0168: Earlier Drive file versions are read under the file-read rule

## Status

Accepted — 2026-10-12. Implemented.

## Context

An agent asked "what changed in this sheet since last Monday?" could read a Drive file and its
metadata through the Drive connector, but not Google's version history: not who saved changes, when
they did, or what the file looked like before. Google has no public API that says who edited which
cell. The Drive Revisions API (`revisions.list`, `revisions.get`) lists a file's saved versions with
their time and author and gives access to each version's content, and it sits under the full Drive
scope the connector already holds, so it needs no new OAuth scope and no re-consent.

## Decision

- `drive_list_revisions` is auto-approved, with no card. It returns each version's id, time, author,
  size and `keep_forever`, newest first, optionally from a `modified_after` date. With a date it
  also returns the newest version saved before it, marked `before_cutoff`, as the side to compare
  against. If the `file_metadata` privacy category is not `allow`, each entry collapses to
  `{id, before_cutoff}`, the same way `drive_get_file_metadata` collapses to `{id}`.
- `drive_get_revision_content` is reviewed like `drive_get_file_content` and shares its operation
  key, `drive.read_file_contents`, with the READ verb. An "Always allow" read rule (a trusted
  folder, files I own) therefore covers earlier versions with no rule update. The gate's `raw_data`
  carries the file's current `DriveFile`, so folder and owner predicates match.
- A Sheet version is fetched as xlsx through the revision's export link and turned into per-tab
  tables by the existing extraction; a Doc or Slides version as plain text; a text file, PDF, .docx,
  .pptx or .xlsx from the revision's own media; other files only report their size. The export
  link is fetched only from `docs.google.com` or `www.googleapis.com`, so the user's token never
  goes to another host.
- The agent compares versions itself. PrivacyFence does not diff them.

## Alternatives considered

- **The Drive Activity API.** It is a new API and a new OAuth scope, which means re-consent for
  every user, and it still gives no old cell values.
- **A new operation key `drive.read_revision`.** Stored read rules would show "Newer tools aren't
  covered yet" (ADR 0166), and the decision was that the rules that let the AI read a file also
  cover its earlier versions.
- **Reviewing the version list.** It holds the same kind of metadata `drive_get_file_metadata`
  already returns without a card.
- **CSV for a Sheet version.** The CSV export covers only the first tab.
- **A `page_token` parameter on the list.** It goes against ADR 0116, which keeps list tools to a
  bare list and pages inside the client.

## Consequences

- Existing read rules now cover earlier versions of the files they cover; a user who allowed reading
  a folder allows reading its files' history.
- A Sheet version comes back as per-tab tables, while `drive_get_file_content` gives first-tab CSV,
  and a Doc version as plain text, where `drive_get_file_content` gives Markdown. To compare, the
  agent reads the newest revision with `drive_get_revision_content` too.
- Google merges a Doc's, Sheet's or Slides' frequent edits into fewer versions, and a version does
  not say which cell or paragraph it changed.
- The list reads every page because the API cannot order or filter. A file with more than 10,000
  versions makes `drive_list_revisions` fail rather than return a list that lacks the newest ones
  (ADR 0116's page budget).
- The content of a version is never downloaded for images, video and other binaries.

## Verification

- `tests/unit/test_drive_client.py`: `TestParseUtc`, `TestListRevisions`, `TestGetRevisionContent`,
  `TestFetchUrlCapped`.
- `tests/unit/connectors/test_drive_connector.py`: `TestListRevisions`, `TestGetRevisionContent`,
  `TestToolDefinitions`, `TestEveryToolIsAudited`.
- `tests/unit/policy/test_describe.py` (the read rule now covers both tools) and `CORPUS` in
  `tests/unit/policy/test_propose.py`.

## Related

- ADR [0116](0116-list-tools-page-inside-the-client-and-keep-their-return-shape.md),
  ADR [0159](0159-jira-export-issues-is-covered-by-the-project-search-rule.md) (a tool that shares an
  existing operation key), ADR
  [0166](0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md)
- [`connectors/drive.py`](../../src/privacyfence/connectors/drive.py),
  [`drive_client.py`](../../src/privacyfence/drive_client.py)
