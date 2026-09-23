# Org-mode download delivery

Org mode cannot write files directly to an interactive user's local filesystem. Connector tools therefore return downloaded content either inline in the MCP result or through a short-lived staged download link served by the org-mode daemon.

## Delivery selection

`src/privacyfence/org_mode.py` owns the delivery policy.

The relevant defaults are:

- `inline_max_bytes = 8_000_000`
- `link_ttl_seconds = 300`
- `allow_disk_staging = true`

If a payload is at or below the configured inline limit, PrivacyFence can return it inline. Larger payloads are staged and returned as a temporary authenticated download URL. Setting `inline_max_bytes` to `0` forces the staging path.

Setting `allow_disk_staging` to `false` disables the staging path itself: a payload too large to return inline is refused outright rather than ever being written to disk.

Configuration validation requires a non-negative inline limit and a positive link TTL.

## Staged downloads

Staged content is managed by `src/privacyfence/download_staging.py` and delivered through the web download route.

The staging path:

- encrypts staged content at rest;
- assigns an opaque, short-lived token;
- serves the content from `GET /downloads/{token}`;
- enforces the configured TTL: a token past its expiry is never claimable, whatever else has happened;
- keeps download authorization separate from the connector's original provider credential;
- returns an identical 404 for a missing, expired, or wrong-principal token, so the response never discloses which case applies.

Expiry itself is enforced opportunistically, not by a background reaper: `DownloadStagingStore` sweeps expired entries (removing both the registry entry and the on-disk ciphertext) only at the top of `stage()` and `claim()`, mirroring the same pattern `PendingApprovalRegistry` already uses for approvals. Claim-time authorization is unaffected — the sweep runs before every claim check, so an expired token is always rejected. What can lag is disk cleanup: a staged file nobody ever claims, on a principal whose staging store sees no further activity, keeps its encrypted ciphertext on disk until *something* stages or claims again for that principal.

A daemon restart is a separate case, not a fix for that lag: it discards the in-memory registry, so the ciphertext for anything staged-but-unclaimed loses the only record of its name, principal, and expiry — it cannot become claimable again no matter what happens next. `DownloadStagingStore.__init__` accounts for exactly this: before a fresh instance accepts its first `stage()` call, it scans every principal's `downloads_dir()` and deletes whatever it finds there, since a file already on disk at that point cannot correspond to any entry the new, empty registry has. This is what actually keeps a restart from turning "lost the in-memory entry" into "orphaned the ciphertext forever."

At-rest encryption protects a staged file against recovery from disk, a backup, or forensic imaging of the storage medium. It does not protect against compromise of the live daemon process itself: content necessarily exists in plaintext in memory for the brief window between decrypting it and streaming it to the requester.

## Local mode

The org-mode delivery rules above are specific to centralized deployments where the daemon and user filesystem are different machines. Local mode's daemon runs on the user's own machine -- but on a privilege-separated install (the only kind PrivacyFence ships, [ADR 0003](adr/0003-separated-installs-only.md)) it runs as its *own* OS account, not the user's, so "same machine" does not mean "same filesystem access." A plain `open()`/`os.path.expanduser()` against a path the agent supplied resolves against the service account's own home (`/var/empty` on macOS), not the human's.

Local mode's answer is the **local file bridge** ([ADR 0007](adr/0007-local-file-bridge.md)): the `.mcpb` shim, which runs as the user in every Claude Desktop setup and already carries the request path's own authenticated connection to the daemon's `/mcp` endpoint, does the actual filesystem read/write on the daemon's behalf.

- **Downloads** (`drive_download_file`, `gmail_download_attachment`, `confluence_download_attachment`): the tool's full preview/PII-scan/approval flow is unchanged -- only the final write is redirected. Once approved, the daemon stages the file in the *same* `DownloadStagingStore` org mode's own staged links use (per-principal, encrypted at rest, single-use, TTL-bound -- see "Staged downloads" above, all of which applies unchanged) and tells the shim where to fetch it and where the agent asked it saved. The shim fetches it over `GET /mcp-files/downloads/{token}` (same bearer-token auth as `/mcp`, a sibling of the org-mode browser route `GET /downloads/{token}` above but authenticated the same way `/mcp` itself is, not by a browser session cookie) and writes it as the user, never overwriting an existing file at that name.
- **Uploads** (`drive_upload_file`'s `local_path`, the Gmail attachment tools): the daemon answers a call it can't service yet with a `need_uploads` response instead of gating anything; the shim reads the named path itself and `PUT`s the bytes to `/mcp-files/uploads/{slot}` (a sibling upload-side store, `upload_staging.UploadStagingStore`, with the identical encrypted-at-rest/single-use/TTL/no-oracle-404 properties `DownloadStagingStore` has); the daemon then re-runs the same tool call with the bytes staged and claimable, and the normal preview/PII-scan/gate/approval flow runs from there, on real content this time rather than a bare file-size guess.
- A client with no `.mcpb` shim (Claude Code, another direct HTTP client, or an old extension) gets a clear upload error telling it to update the extension or pass `content_base64`, and downloads still work via a one-time link it can fetch with its own bearer token.
- Unseparated installs (a dev checkout, or a pip/pipx install that never enabled privilege separation) are unaffected: the daemon *is* the user there, so every local-mode tool keeps reading and writing local paths directly, exactly as before this ADR.

See ADR 0007 for the full wire protocol, the guard that stops the shim from touching a path the agent didn't itself name, and the security reasoning for why this adds no capability the agent didn't already have.

## Uploads and downloads for clients without the shim (Phase 4)

Claude Code, another direct HTTP MCP client, or org mode itself all talk to `/mcp` with no `.mcpb`
shim in the request path at all -- ADR 0007's Phase 1 upload handshake and bearer-authenticated
download link don't help them, since there's no shim to intercept a `need_uploads` response or
attach the same bearer header a second request needs. Phase 4 ("Clients without the bridge") adds
a pair of unauthenticated **capability** routes any HTTP client can reach directly, no PrivacyFence
extension and no custom header required -- the 32-byte token embedded in the URL is itself the
one-time credential:

- **Uploads**: call the `privacyfence_create_upload_slot` meta-tool with a `filename` (and,
  optionally, `size_bytes`) to get back `{upload_id, upload_url, method: "PUT", max_bytes,
  expires_at, example}`. `PUT` the file's raw bytes to `upload_url` (e.g. the shown `curl -T`
  example) -- single-use, principal-bound at creation, a 10-minute TTL. Then pass `upload_id` back
  to the tool that needed the file: `drive_upload_file`'s own `upload_id` parameter (the rule
  becomes "exactly one of `local_path`, `content_base64`, `upload_id`"), or an `"upload:<upload_id>"`
  entry in one of the `gmail_*_with_attachments` tools' `attachments` array. This works in every
  mode, org mode included -- unlike a plain `local_path`/filesystem attachment path, a capability
  slot claim needs neither `can_access_user_files()` nor a bridge-capable shim, only the token
  itself (`local_files.require_local_files`'s `upload:` handling).
- **Downloads**: `DownloadDeliveryConfig.agent_links` (default `true`) makes org mode's own staged
  link a capability URL (`/mcp-files/fetch/<token>`) instead of the cookie-authenticated browser
  route (`/downloads/{token}`) described above -- every `/mcp` caller is an agent, not a browser
  with a session cookie, so the capability link is what a tool result's `download_url` actually
  needs to be useful. Setting `agent_links: false` in `org_config.json`'s `download_delivery`
  section keeps the older browser-only link instead, for an org that wants a human to be the one
  who opens a staged download rather than the agent fetching it directly on their behalf. Local
  mode's own Phase 1 no-bridge fallback (`local_files._deliver_link`, reached when a caller has no
  bridge-capable shim) uses the identical capability route, replacing the bearer-authenticated
  `/mcp-files/downloads/{token}` link it returned before Phase 4.

Both new routes (`PUT /mcp-files/slots/{slot}`, `GET /mcp-files/fetch/{token}`) are mounted
alongside the Phase 1 pair under the same `/mcp-files` prefix, but carry no auth middleware of
their own -- authorization is entirely the token: single-use, TTL-bound, and (for uploads)
re-checked against the creating principal the moment a tool call actually claims the bytes, so a
leaked `upload_url` lets someone else fill *your* pending slot with their own bytes, never read or
claim anything of yours. See [ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md) for the full
security reasoning.

## Preview and PII limits are separate

The delivery threshold is not the same as the pre-approval preview/PII scan limit. Connector tools may fetch or inspect only a bounded prefix for preview and privacy scanning while still delivering the complete approved file through the inline or staged path.

See [`file-type-support.md`](file-type-support.md) for preview/extraction behavior and [`security-and-compliance.md`](security-and-compliance.md) for the data-handling boundary.
