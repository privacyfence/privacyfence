# ADR 0092: The inline download limit caps the tool result, at 100,000 bytes by default

## Status

Accepted — 2026-09-28. Amends [ADR 0017](0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md):
the split between inline delivery and a staged link, and the reasons for it, are unchanged; the
size of the line and what it measures are not.

## Context

In organization mode, `drive_download_file`, `gmail_download_attachment` and
`confluence_download_attachment` return a file inside the tool result (base64, in
`content_base64`) when it is small enough, and a one-time link otherwise
(`DownloadDeliveryConfig` in `src/privacyfence/org_mode.py`). ADR 0017 set the line at
`inline_max_bytes: 8,000,000`, compared with the file's raw size, on the grounds that the ceiling
was "transport practicality".

Manual QA of 5.0.0a2 found that the practical ceiling is the client, and that it is much lower:
claude.ai received a 1.7 MB PDF inline, about 2.3 MB once base64-encoded, and truncated the tool
result, so the task failed
([M1.3](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865753049)). Claude
Code took the same file inline without trouble
([M1.2](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865527889)), so the
limit depends on the client, but the hosted clients (claude.ai, Claude Desktop's remote
connectors) are the ones organization mode exists for.

Anthropic documents the limits in
[Build an MCP server for Claude](https://claude.com/docs/connectors/building), "Design within the
size and timeout limits": a maximum tool result size of **~150,000 characters** for claude.ai and
Claude Desktop, and 25,000 tokens (configurable with `MAX_MCP_OUTPUT_TOKENS`) for Claude Code.

The comparison also ignored base64: a file at the limit produced a tool result a third larger than
the limit.

## Decision

- **`inline_max_bytes` caps the tool result, not the file.** The number compared is the length of
  the JSON text `web/mcp_tools.to_call_tool_result` sends for the inline result: the base64
  payload plus its envelope (`inline_result_length`). It is computed exactly, without encoding,
  before the bytes are fetched (for the approval preview, the audit record and the
  staging-disabled refusal), and decided again on the bytes actually fetched, so a Google Workspace
  export that comes back larger than its metadata said goes out as a link rather than as an
  over-limit result (`DownloadDeliveryConfig.inline_result`).
- **The default is 100,000.** That is two thirds of the documented, approximate ~150,000
  characters, and still inlines files up to about 75 KB. Anything larger gets a link, which every
  client handles.
- **The bundle setting stays** (`--downloads-inline-max-bytes`, `download_delivery.inline_max_bytes`)
  for an organization whose clients accept more, and `0` still sends every download through a link.

## Alternatives considered

- **Keep 8,000,000 and document it as client-dependent.** Rejected: it fails by default on the
  clients organization mode is for, and fails silently, with a truncated file rather than an error.
- **150,000, the documented figure.** Rejected: the documentation says "~", the figure is in
  characters of a result the client may wrap, and a result a little over the line is truncated,
  not refused. A third of headroom costs only files between about 75 KB and 110 KB, which get a
  link instead.
- **256 KiB, a common conservative guess.** Rejected once the documented figure was found: 256 KiB
  of file is about 350,000 characters of base64, more than twice what claude.ai accepts.
- **Keep comparing the raw file size and lower the number to allow for base64.** Rejected: the
  envelope and JSON escaping of the file name are not proportional to the file, and an
  administrator reading the setting would have to know the conversion. Measuring the result is
  exact and says what the setting is for.
- **Pick the limit per client.** Not done: the client's identity is not trustworthy enough to
  size a response on (ADR 0035), and a link works for every client.

## Consequences

- On claude.ai and Claude Desktop, a download of a typical document now arrives as a one-time link
  the client fetches itself (ADR 0028), not inline.
- An existing bundle that sets `inline_max_bytes` keeps its number, which now counts base64: the
  same setting inlines files about a quarter smaller than before.
- The approval preview states the file size, its base64 size and the limit, so the person
  approving sees which path applies and why.
