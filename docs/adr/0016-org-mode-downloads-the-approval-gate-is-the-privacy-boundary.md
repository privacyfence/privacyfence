# ADR 0016: org-mode downloads: the approval gate is the privacy boundary, staging a bounded cost

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-09 in
`docs/org-mode-download-delivery-plan.md`, which was added with its implementation in `3b144232`
and deleted in `04d08f4a` the same day. Read it with
`git show 04d08f4a^:docs/org-mode-download-delivery-plan.md`, section "What these tools are
actually for, and what that means for priority"). Implemented. The mechanism is described in
[`docs/org-mode-download-delivery.md`](../org-mode-download-delivery.md). That document's
"Local mode" section was later amended by [ADR 0007](0007-local-file-bridge.md). This ADR concerns
org mode only.

## Context

`drive_download_file`, `gmail_download_attachment` and `confluence_download_attachment` wrote file
bytes to a `destination_dir` on the daemon's own filesystem. In org mode the daemon runs headless
on a shared server that the human has no shell on, so the file landed under the service account's
home and never reached the person who asked for it. The gate preview's "None — file bytes are
never sent" line was accurate in local mode and misleading in org mode.

The plan chose between two options from an earlier discussion: returning bytes inline in the MCP
tool result (#1), or staging them server-side behind a signed-in-browser link (#3). The sources do
not record what option #2 was or why it was dropped.

## Decision

**The privacy boundary PrivacyFence enforces for a download is the approval gate that runs before
any bytes move.** The human reviews the preview and decides, once, whether the file goes to the
model. How approved bytes travel afterwards is a transport detail, not a second boundary. The
plan observed that the local-mode disk write had never been a content boundary either, because
nothing stopped Claude from reading the file it had just saved.

It follows that:

- **Inline delivery is the default**, not a convenience for small files. It is the direct way to
  do what the tools exist for. The cap is set by transport practicality, not by a privacy
  argument: `DownloadDeliveryConfig.inline_max_bytes` defaults to 8,000,000, which is larger than
  the 5 MB preview/prefetch caps.
- **Staging is the fallback, used only for files too large to return inline.** It is accepted as
  a named cost. A shared server, possibly administered by IT staff who are not meant to see the
  content and backed up by infrastructure the project does not control, now caches real file
  content for a short time. That cost is bounded, not ignored:
  - the file is encrypted at rest (AES-256-GCM under an HKDF key derived from a 256-bit token that
    is never persisted server-side);
  - the file can be claimed once;
  - it expires after a short TTL (`link_ttl_seconds` defaults to 300);
  - it is bound to the principal, and missing, expired, wrong-principal and already-claimed
    tokens all get the same 404.
- **Organizations can move the line.** `inline_max_bytes: 0` sends every download through a
  staged link, so no file content reaches the model's context. `allow_disk_staging: false` refuses
  oversized files rather than writing them to disk, even encrypted.
- The gate preview states which path applies, so the human approves with that knowledge.

## Alternatives considered

- **Staging as the primary path and inline as a small-file convenience.** This was the plan's
  first draft. It was reversed after feedback, because the tools exist to get approved bytes to
  the model, and inline delivery does that directly.
- **Staging with only a TTL and single-claim delete.** Rejected as insufficient on its own. It
  limits the exposure window but not "was the plaintext ever recoverable from this server's disk
  at all". Encryption with a key the server never stores was added to answer that.
- **`allow_disk_staging` off by default.** This was left as an open question in the plan. It
  shipped as `True`, treating encryption at rest as the primary mitigation. The sources do not
  record a further rationale.

## Consequences

- In org mode, downloaded content under the inline limit enters the model's context by design.
  This is intended, not a leak, and the gate preview says so. `docs/security-and-compliance.md`'s
  "Download staging" section describes the staging mechanism but not this inline posture, which
  the plan had required it to call out.
- Encryption at rest protects against disk, backup and forensic recovery. It does not protect
  against a compromise of the live daemon, which holds plaintext briefly around decrypt and
  stream.
- Staged state is ephemeral. A daemon restart discards the registry, and
  `DownloadStagingStore.__init__` deletes leftover ciphertext (`6ef2154a`). An interrupted
  download means re-running the tool call.
- The staging store later became reusable. Local mode's file bridge (ADR 0007) uses the same
  `DownloadStagingStore` for separated installs.

## Verification

- `src/privacyfence/org_mode.py`: `DownloadDeliveryConfig` (`fits_inline`, the defaults,
  `allow_disk_staging`).
- `src/privacyfence/download_staging.py` (`stage`, `claim`, the key derivation) and
  `src/privacyfence/web/routes_downloads.py` (`GET /downloads/{token}`, org mode only).
- The org-mode `new_info` preview wording in `connectors/drive.py` and `connectors/gmail.py`.
- `tests/unit/test_download_staging.py::test_the_file_on_disk_is_never_plaintext`, and
  `tests/unit/web/test_routes_downloads.py` (single-use, wrong-principal 404, no token in the
  audit entry, origin check).

## Related

- `git show 04d08f4a^:docs/org-mode-download-delivery-plan.md`, the source plan.
- Commit `3b144232`, where it was implemented.
- [ADR 0007](0007-local-file-bridge.md) amends the local-mode half of the live doc and reuses the
  staging store.
- [ADR 0010](0010-org-mode-runs-its-own-oauth-authorization-server.md): the org-mode deployment
  this applies to.
