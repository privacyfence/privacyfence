# ADR 0028: clients without the shim move files through single-use capability URLs, not the bearer token

## Status

Accepted — 2026-09-23; implemented in [#611](https://github.com/privacyfence/privacyfence/pull/611)
(`ead0535e`). Extends [ADR 0007](0007-local-file-bridge.md) to the callers its shim-based design
cannot reach, and amends its no-bridge fallback. #611 first recorded this as D5/D6 inside ADR 0007;
it was moved here when the ADR rules in [`README.md`](README.md) were adopted.

## Context

ADR 0007 moves files across the privilege-separation boundary through the `.mcpb` shim. Some
callers have no shim in their request path at all: Claude Code pointed straight at `/mcp`, any
other direct HTTP MCP client, and every org-mode caller (org mode has never had a shim). For them
ADR 0007 left only a fallback: uploads failed with an error, and downloads returned a one-time link
authenticated with the same bearer token as `/mcp`.

That token is available to a real MCP client, but not to what such a client often hands the actual
byte-moving to: a sandboxed agent shelling out to `curl`, which has neither the MCP session's
bearer token nor any way to attach a header.

## Decision

Add a second, orthogonal way into the same two staging stores: a **capability URL**, with a
32-byte token in the path and no header of any kind. The token itself is the one-time credential.

- **Upload.** A new meta-tool, `privacyfence_create_upload_slot` (an ordinary, bearer-authenticated
  `tools/call`), mints an `UploadStagingStore` slot for the calling principal and returns
  `{upload_id, upload_url, method: "PUT", max_bytes, expires_at, example}`. The caller `PUT`s the
  bytes to `…/mcp-files/slots/<token>` with no header, then passes `upload_id` to the tool that
  needs the file (`drive_upload_file`'s `upload_id`, or an `"upload:<upload_id>"` entry in a
  `gmail_*_with_attachments` tool's `attachments`). **Filling a slot needs no principal; claiming
  it does**: `local_files.require_local_files` claims it through `UploadStagingStore.claim()`,
  principal-checked as before. A capability bypasses the bearer header, not principal binding.
- **Download.** `GET …/mcp-files/fetch/<token>` claims a `DownloadStagingStore` entry with no
  header. Local mode's no-bridge fallback (`local_files._deliver_link`) uses it instead of the
  bearer-authenticated link. In org mode, `DownloadDeliveryConfig.agent_links` (default `true`)
  makes every staged download use it, because an `/mcp` caller is always an agent. The
  cookie-authenticated `/downloads/{token}` route stays, for a human opening the link
  (`agent_links: false`).
- **New routes, no new stores.** `UploadStagingStore.afill_capability()` and
  `DownloadStagingStore.claim_capability()` use the same encrypted-at-rest, single-use, TTL-bound
  stores as ADR 0007, with the principal check made optional rather than duplicated. Both routes
  live under the same `/mcp-files` prefix as ADR 0007's bearer pair, in one dispatcher
  (`web/routes_file_bridge.py`'s `_FileBridgeRouter`) that routes by path segment (`slots`,
  `fetch` → no auth; everything else → the bearer stack). Two Starlette `Mount`s cannot share a
  prefix: the first registered swallows every request under it, auth stack included.
- **Org mode gains exactly two things**, both orthogonal to ADR 0007's reasons for leaving it
  alone: `upload_id`/`"upload:<id>"` work there (a slot claim is not a filesystem read), and
  `agent_links` chooses the download URL shape. An org-mode `local_path` still means a path on the
  server and never touches `local_files.py`.

## Alternatives considered

- **Keep the bearer-authenticated link for downloads.** Rejected: the process that fetches is often
  not the one holding the token, and it cannot attach a header.
- **A second, separate staging store for capability transfers.** Rejected in favour of making the
  principal check optional in the existing stores, so encryption, TTL and single-use behaviour stay
  in one place.
- **Two `Mount`s under `/mcp-files`.** Does not work in Starlette (see Decision), and the first
  version of this change was bitten by it.
- **In-place extension of ADR 0007** (what the source plan prescribed). Superseded by this ADR when
  the one-decision-per-ADR rule was adopted.

## Consequences

- Claude Code and org-mode clients can upload and download files. Local mode's non-shim uploads no
  longer fail; the error now points at `privacyfence_create_upload_slot` (or `content_base64` for
  `drive_upload_file`).
- **A secret now travels through the model's own context**, the one place this design adds one.
  What a leaked URL exposes: an upload URL lets someone else fill *your* pending slot with *their*
  bytes, which still pass the destination tool's preview, PII scan and approval gate before
  anything happens. A download URL exposes one already-approved file, once, until it expires
  (10 minutes for uploads; `download_delivery.link_ttl_seconds`, 5 minutes by default, for
  downloads). Neither grants standing access.
- ADR 0007's "never overwrite an existing file" (its D4) is the shim's guarantee and has no
  counterpart here. A client fetching a capability URL decides for itself where and whether to
  write the bytes — as with any HTTP download.
- New surface: two unauthenticated routes, each authorized by its own token alone. Together with
  ADR 0007's bearer pair: four endpoints, all single-use and TTL-bound, over two encrypted stores.

## Verification

- `tests/unit/web/test_routes_file_bridge.py`: the capability routes work with no auth header and
  remain single-use and TTL-bound; the bearer routes are unchanged.
- `tests/unit/test_upload_staging.py`, `tests/unit/test_download_staging.py`: `afill_capability`,
  `claim_capability`.
- `tests/unit/test_local_files.py`: the `upload:` reference in the resolution order.
- `tests/unit/connectors/test_drive_connector.py`, `test_gmail_connector.py`,
  `test_confluence_connector.py`: `upload_id`/`"upload:<id>"` claims, including in org mode and
  the wrong-principal case, and `agent_links`' two link shapes.
- `tests/unit/web/test_routes_mcp.py`, `tests/unit/web/test_mcp_tools.py`: the meta-tool's round
  trip and manifest entry.
- `tests/unit/web/test_server.py`, `tests/unit/web/test_server_org_mode.py`: the capability routes
  are reachable with no bearer token through both modes' real `build_app()` wiring. This caught
  `_FileBridgeRouter`'s original bug (see its docstring).

## Related

- [ADR 0007](0007-local-file-bridge.md) — the shim-based bridge, the staging stores, and the
  bearer-authenticated pair.
- [ADR 0017](0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md) — org-mode
  download delivery; the approval gate is the privacy boundary a leaked download URL sits behind.
- [ADR 0008](0008-one-principal-per-os-user.md) — per-principal staging, which the claim step
  enforces.
- `docs/org-mode-download-delivery.md` ("Uploads and downloads for clients without the shim") and
  `docs/getting-started.md` (the `curl -T` flow).
- Source plan: `local-mode-fixes-plan.md` Phase 4, never merged to `main`; read it with
  `git show 453ae02e:local-mode-fixes-plan.md`.
