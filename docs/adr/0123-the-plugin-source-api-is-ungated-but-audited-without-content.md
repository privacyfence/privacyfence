# ADR 0123: The plugin source API is ungated but audited without content

## Status

Accepted — 2026-10-07. Implemented: `src/privacyfence/plugins/source_ops.py`,
`src/privacyfence/plugins/spool.py`.
Amended by [ADR 0128](0128-plugin-source-reads-never-truncate.md) (paging) and [ADR 0129](0129-drive-binary-downloads-for-plugins-are-http-range-reads-with-no-size-cap.md) (Drive downloads).

## Context

The first consumer of the plugin framework copies data from connected services into its own store,
on a schedule, in volume: Salesforce report runs, Drive workbooks and similar. A card per read would
make that impossible, and a plugin that held connector tokens would make every plugin a credential
store. The enable step, where the owner approves the plugin's source operations, is the point where
a human decides whether it may read at all ([ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)).

## Decision

A plugin reads through `source.call`, naming one of six operations (`salesforce.report_run`,
`jira.search`, `drive.download`, `sheets.get_values`, `confluence.get_page`,
`calendar.list_events`). PrivacyFence runs it with the connector's own client, under the local
principal, and hands the result back. The plugin never holds a token.

It is not gated by a card, and every call is audited: one `plugin_source` entry per call, success
or error, with the operation, the target names and the byte count, and never the data. A client's
error message is never returned to the plugin or logged: the plugin gets
"the service returned an error" and the exception type name is logged. The checks run in order:
introspection, parameters, the principal, the manifest's `source_operations`, the connector's
state, then the call; a result over 12 MiB is refused.

**Result shapes.** Raw only where the plugin needs the bytes and the client already returns them:
Salesforce report runs (the analytics JSON, `allData` included) and Drive file bytes. Jira,
Confluence and Calendar results are the daemon's normalized shapes (the dataclasses its clients
return, as JSON), and Sheets returns the raw `values` array. Jira, Confluence and Calendar clients
expose only parsed results, so raw ones would need new client methods, each with live QA and
recorded fixtures, and would widen what a plugin receives past what PrivacyFence has already
reviewed. A later minor protocol version can add raw operations.

**Chunked Drive downloads.** Drive has no range reads, so the first call for a file fetches it,
refuses one over 64 MiB, writes it to a spool file under the data root (mode 0600) and serves it
in chunks of at most 8 MiB with a cursor. Every later call re-reads the file's modified time, and a
changed revision fails with `upstream_error` and `data.reason` `revision_changed`, and removes the
spool file. Idle spool files are deleted after 10 minutes and at shutdown.

## Alternatives considered

- **Gate every read with a card.** Rejected. It defeats the plugin's purpose, and the human already
  approved the operation list at enable.
- **Give the plugin a token.** Rejected. Tokens stay in the daemon.
- **Raw provider JSON for every operation.** Rejected. See the shapes above.
- **A higher single-message cap.** Rejected. Chunking keeps messages bounded and memory use
  predictable.

## Consequences

- An enabled plugin can read what its manifest's operations allow, silently. The audit log shows
  that it did, and how much, but not what.
- A plugin that needs a field the normalized shape drops has to wait for a raw operation.
- A source call needs the connector enabled and signed in; the error says which of disabled, not
  signed in or unavailable.

## Verification

`tests/unit/plugins/test_source_ops.py` (the allow-lists, the connector reasons, no leaked error
text, no content in the audit entry, one test class per operation),
`tests/unit/plugins/test_spool.py` (a 10 MiB file in chunks, a changed revision, the idle sweep, the
size caps) and `tests/unit/plugins/test_sdk_samples.py` (the SDK's samples against the adapters'
shapes).

## Related

- [The tracking issue](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0092](0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)
- [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)
- [ADR 0126](0126-the-plugin-sdk-lives-in-this-repository-and-is-published-from-the-same-tag.md)
