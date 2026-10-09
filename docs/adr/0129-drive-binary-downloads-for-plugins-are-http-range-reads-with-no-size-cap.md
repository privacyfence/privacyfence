# ADR 0129: Drive binary downloads for plugins are HTTP Range reads with no size cap

## Status

Accepted — 2026-10-08. Implemented: `src/privacyfence/drive_client.py` (`download_range`),
`src/privacyfence/plugins/spool.py` (`DownloadSpool.read_chunk_at`),
`src/privacyfence/plugins/source_ops.py`.
Amends [ADR 0123](0123-the-plugin-source-api-is-ungated-but-audited-without-content.md).

## Context

`drive.download` fetched a whole file into a spool before serving its first chunk, and refused a
file over 64 MiB. A plugin that copies workbooks or archives hit the cap, and every download waited
for the entire file before the first byte arrived.
Requested in [the design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618).

## Decision

For a binary file, each chunk is one HTTP request with a `Range` header, so the first chunk
arrives at once and nothing is held on disk. There is no size cap. A short chunk, or a file whose
modified time changed, fails with `upstream_error` and reason `revision_changed`, which the SDK
retries from the start. A server that answers 200 instead of 206 is an error: a whole file is
never read in this path. Google-native files (Docs, Sheets, Slides) cannot be ranged, so they keep
the spool, now with no cap of PrivacyFence's own; Google limits exports itself.

## Alternatives considered

- **Stream whole files into a spool with no cap.** Rejected. The first chunk would still wait for
  the whole file, and the spool would hold it all on disk.
- **Keep a cap.** Rejected by the requester; the plugin owns the decision to read a large file.

## Consequences

- A cursor holds the revision and the next offset, so the plugin resumes without server state for
  binary files.
- The live check for Drive now exercises a Range read, so the QA folder must hold a non-Google file.

## Verification

`tests/unit/test_drive_client.py` (`download_range`), `tests/unit/plugins/test_spool.py`,
`tests/integration/test_plugin_paging.py`, and the Drive live check in
`scripts/qa_fixture_recorder.py`.

## Related

- [The design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618)
- [ADR 0105](0105-a-google-read-is-retried-once-when-its-connection-drops.md)
- [ADR 0123](0123-the-plugin-source-api-is-ungated-but-audited-without-content.md)
- [ADR 0128](0128-plugin-source-reads-never-truncate.md)
