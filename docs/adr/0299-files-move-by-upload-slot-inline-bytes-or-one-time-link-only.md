# ADR 0299: Files move by upload slot, inline bytes or one-time link only

## Status

Accepted — 2026-11-14. Supersedes [ADR 0007](0007-local-file-bridge.md), [ADR 0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md); amends [ADR 0028](0028-clients-without-the-shim-get-capability-urls.md), [ADR 0160](0160-org-mode-exports-are-delivered-by-a-one-time-link.md). Implemented.

## Context

[ADR 0007](0007-local-file-bridge.md) gave desktop mode a local file bridge, and [ADR 0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md) connected Claude Desktop to it. Without desktop mode the bridge, the path parameters and the `download_mode` plumbing have no caller. Organization mode already moves files through upload slots and the one-time link of [ADR 0160](0160-org-mode-exports-are-delivered-by-a-one-time-link.md).

## Decision

Files reach the server only through the upload slot (`privacyfence_create_upload_slot` and `PUT /mcp-files/slots/{slot}`), inline bytes, and the one-time link.

- The file bridge, its bearer router, the `/mcp-files/uploads` and `/mcp-files/downloads` handlers, the `X-PrivacyFence-File-Bridge` header and the `_meta` handshake are removed. The capability routes, `/mcp-files/slots/{slot}` and `/mcp-files/fetch/{token}` stay.
- Every tool schema loses `destination_dir` and plain `local_path`; attachment and upload parameters take `upload:<id>` references only. Every connector's `download_mode` goes.
- The message for a tool that gets a path becomes: `This tool takes a file through an upload slot. Call privacyfence_create_upload_slot, send the file to the returned address, and pass the returned upload: reference.`
- The dispatcher drops undeclared arguments, so an agent with a cached schema that still sends `destination_dir` or `local_path` is ignored instead of failing.
- Where the bridge's dedupe-replay flag is gone, the organization staging paths set it, so a repeated call does not mint a second link.
- `privacyfence_status` loses its `deprecation` field and the `open_privacyfence_companion` next step, and the server instructions point to the Connect page or the administrator. These are breaking changes for clients that read them.

## Alternatives considered

- **Keeping path parameters that the server ignores.** Rejected: the tool descriptions would promise a behaviour that does not exist.
- **Failing on a stale argument.** Rejected: a cached schema would raise a `TypeError` instead of working through an upload slot.

## Consequences

- Agents that passed a local path must use an upload slot.
- Tool descriptions and the tools reference are rewritten to say so.

## Verification

`tests/unit/test_local_files.py` and `tests/unit/web/test_routes_file_bridge.py`.

## Related

ADR 0007, 0012, 0028, 0115, 0160, 0296.
