# ADR 0007: local file access crosses the privilege-separation boundary through the `.mcpb` shim

## Status

Accepted; implemented (local-mode-fixes-plan.md Phases 1 and 4). Amends the "Local mode" section of
[`docs/org-mode-download-delivery.md`](../org-mode-download-delivery.md), which said a separated
install's daemon "can write a requested download to a user-selected/local destination because the
daemon runs on the user's machine" -- true of the machine, false of the account, since ADR 0003.
Also records one deliberate, bounded exception to `mcpb/shim/src/proxy.ts`'s "nothing here
inspects or depends on what the request actually asks for" design rule -- see Decision 2.

Phase 4 ("Clients without the bridge", below) extends this ADR rather than opening a new one: it's
the same D1 problem (a local path an agent named has to become bytes, or bytes have to become a
file) for the one case D1's own shim-based answer structurally cannot reach -- a caller with no
`.mcpb` in its request path at all.

## Context

ADR 0003 made every shipped local-mode install privilege-separated: the daemon runs as its own OS
account (`_privacyfence` on macOS), not the human's. Every connector tool with a local-path
parameter -- `drive_upload_file`'s `local_path`, `drive_download_file`'s `destination_dir`,
`gmail_download_attachment`'s `destination_dir`, the three `gmail_*_with_attachments` tools'
`attachments`, `confluence_download_attachment`'s `destination_dir` -- still did a plain `open()`,
`os.path.isfile`, `os.path.getsize` or `os.path.expanduser` against that path, inside the daemon
process. Before ADR 0003 that was correct: the daemon and the human were the same OS user. After
it, every one of those calls resolves against `/var/empty` (macOS's `HOME` for the service
account) or fails with `EACCES` on anything under the real user's home directory. Four related bugs
compounded the failure into something that looked, from the log, like PrivacyFence guessing at
random directories and giving up silently -- see this ADR's own PR for the full diagnosis
(`local-mode-fixes-plan.md` SS1's Problem 1 and B1-B4); they are fixed in the same change this ADR
records, not narrated again here.

Local mode has exactly one component positioned to fix this without giving anything up back:

- the `.mcpb` shim (`mcpb/shim/`) runs as the human, in every Claude Desktop setup including local
  agent mode (whose `.../local-agent-mode-sessions/.../outputs` paths are host paths the shim's own
  process can already reach);
- it already sits on the request path of every tool call, as the thing that owns the HTTP
  connection to the daemon's `/mcp` endpoint and pumps JSON-RPC messages between Claude Desktop and
  the daemon (`proxy.ts`'s `proxyTransports`).

So the daemon can ask the shim for bytes, over the connection that's already there and already
authenticated with the same bearer token every tool call uses.

## Decision

### D1: files cross the privilege-separation boundary through the shim, not the filesystem

The daemon never reads or writes a user-supplied local path directly once it cannot access the
user's files (`local_files.can_access_user_files()` -- false whenever privilege separation is
enabled, in local mode). Instead:

- **Upload** (`drive_upload_file`'s `local_path`, the three Gmail attachment tools): the daemon
  answers a `tools/call` it can't fully service yet with a `need_uploads` response instead of
  gating or approving anything. The shim reads the named path(s) itself, as the user, and `PUT`s
  the bytes to a new daemon-side staging endpoint over the same authenticated connection. The
  daemon then re-runs the *same* tool call, now with the bytes staged and claimable.
- **Download** (`drive_download_file`, `gmail_download_attachment`,
  `confluence_download_attachment`): the tool runs its full preview/PII-scan/approval flow exactly
  as before -- nothing about *when* a human is asked to approve anything changes -- and only the
  final disk write is redirected. Instead of writing to `destination_dir`, the daemon stages the
  file (encrypted at rest, single-use, TTL-bound -- reusing `download_staging.DownloadStagingStore`,
  the same store org mode's staged-link delivery already uses) and tells the shim where to fetch it
  and where the agent asked it to be saved. The shim fetches it and writes it, as the user.

**Rejected alternatives**, and why:

- *A group-shared transfer directory under `handoff/`.* Every member of the service group could
  then read every other principal's staged uploads/downloads -- fine today (there is exactly one
  local principal), but it would need undoing the moment Phase 3 (`local-mode-fixes-plan.md`) gives
  each OS user their own principal. Per-principal staging directories
  (`paths.uploads_dir(principal)`/`downloads_dir(principal)`) cost nothing extra and get that
  isolation for free from day one.
- *ACLs or Full Disk Access granted to the service account for user homes.* This is precisely the
  guarantee ADR 0003 exists to make hold: "the agent cannot approve its own request" depends on the
  daemon's account having no standing access to the human-authority files or the human's own data.
  Granting it back defeats the separation for the sake of working around a downstream symptom of
  the separation.
- *Base64 inside MCP messages for everything.* Already how `content_base64` works for small
  uploads with no local path at all, and it stays available. It does not scale: a multi-megabyte
  PDF base64-encoded into a JSON-RPC message costs a third again in size, has no streaming story
  over stdio, and burns the model's own context budget reading a result that describes bytes it
  never needed to see.
- *File I/O inside the companion* (the menu-bar/tray process ADR 0002 already runs as the human).
  Rejected because the companion is not on the request path of a tool call and is not guaranteed to
  be running at all (a headless server-triggered call, an unattended session) -- routing file
  transfer through it would make every download/upload depend on a process whose only other job is
  showing a menu.

### D2: the shim stays schema-agnostic, with one named, bounded exception

`proxy.ts`'s design rule -- the shim has no knowledge of tool names, schemas, or what a request is
*for* -- is kept, with exactly one addition: the shim now also recognizes a vendor `_meta` key,
`privacyfence.eu/file-bridge`, on a response to a `tools/call` it forwarded, and recognizes that a
message with `method: "tools/call"` is the kind of message whose `params.arguments` it may need to
walk (guard G1, below). It never branches on a tool name or a parameter name, and every other
message shape passes through `proxyTransports` exactly as it did before this ADR -- an interceptor
that recognizes nothing behaves byte-for-byte identically to no interceptor at all
(`mcpb/shim/test/proxy.test.ts` is the standing proof of that for every message shape that isn't
file-bridge traffic).

This is a real, deliberate narrowing of "no protocol knowledge" -- recorded here rather than left
implicit, per this ADR's own Status note, and per `proxy.ts`'s own module docstring, which now
names this ADR at the point it makes the exception.

### D3: guard G1 -- the shim only touches paths the agent itself named in this request

A `path`/`dest_dir` the daemon sends back is accepted only if it equals, after trimming, some
string value found by a deep walk of the *original* request's `params.arguments` -- including
elements of any string value that itself parses as a JSON array (the Gmail `attachments` parameter
is a JSON-encoded array of paths passed as one string argument, not a JSON array parameter). A path
that isn't found this way is refused outright, for both the upload and download directions.

This costs nothing in practice -- the agent runs as the same user the shim does, so it could always
have read or written that same path directly -- but it stops a buggy or compromised daemon from
turning the shim into a general-purpose file reader/writer by sending back a path the agent never
mentioned. It is the one piece of real authorization logic this ADR adds to the shim, and it is
why the shim has to look at `params.arguments` at all (D2's bounded exception).

### D4: never overwrite an existing file

A download that lands at a name already occupied in `dest_dir` gets `name (1).ext`, `name
(2).ext`, and so on -- never a silent overwrite. The shim writes as the user, with the user's own
filesystem permissions; overwriting is data loss the approval the human granted (which described
*what* would be downloaded, not that an existing file with the same name would be destroyed) never
covered.

### Wire protocol v1 (summary -- see `local_files.py`'s and `fileBridge.ts`'s own docstrings for the
authoritative detail)

- Capability advertisement: the shim sends `X-PrivacyFence-File-Bridge: 1` on every `/mcp` request;
  the daemon treats a tool call as bridge-capable only when that header is present on *that*
  request.
- Upload: daemon -> shim `need_uploads` (`{v, op: "need_uploads", files: [{path, slot, upload_path,
  max_bytes}]}`) -> shim `PUT <upload_path>` with the file's raw bytes -> shim resends the original
  `tools/call` with `params._meta["privacyfence.eu/file-bridge"] = {v, uploads: {path: slot}}`
  merged in -> daemon claims the staged bytes and runs the tool normally (preview, PII scan, gate,
  approval, upload) from there. At most one handshake round per request id.
- Download: daemon stages the approved file and answers with `{v, op: "deliver", files: [{dest_dir,
  name, download_path, size_bytes, sha256, result_path_pointer}]}` -> shim `GET <download_path>`,
  verifies size and sha256, writes to `dest_dir` under the collision-avoiding name (D4), then
  rewrites the result (`structuredContent` at `result_path_pointer` becomes the real path,
  `delivery` becomes `"local_disk"`, the `_meta` file-bridge key is stripped) before forwarding to
  Claude Desktop.
- No-bridge fallback (a client with no shim -- Claude Code, another direct HTTP client, or an old
  `.mcpb`): uploads raise a clear, actionable error pointing at Phase 4's
  `privacyfence_create_upload_slot` (or, for `drive_upload_file` alone, `content_base64`).
  Downloads stage the file and return a one-time `download_url` -- Phase 4's capability route (see
  below), not a bearer-token-authenticated one.
- Staging endpoints (`web/routes_file_bridge.py`): `PUT /mcp-files/uploads/{slot}` and
  `GET /mcp-files/downloads/{token}`, mounted behind the identical bearer-token auth stack `/mcp`
  itself uses (`routes_mcp.build_mcp_asgi_app`'s three-layer middleware, reused rather than
  reimplemented) -- the principal for both routes always comes from that token, never a cookie.
  Upload staging (`upload_staging.UploadStagingStore`) mirrors `download_staging.
  DownloadStagingStore` exactly: AES-256-GCM at rest keyed by HKDF(token), an in-memory registry, a
  startup sweep of anything orphaned by a previous process life, single-use claims bound to the
  requesting principal, and the identical "missing/expired/wrong-principal/already-claimed" 404
  with no oracle distinguishing the four.

### D5 (Phase 4): clients without the bridge get capability URLs, not bearer tokens

A caller with no `.mcpb` shim in its request path at all -- Claude Code pointed straight at `/mcp`,
another direct HTTP MCP client, or every org-mode caller (org mode has never had a shim in the
request path, see "Why is org mode untouched by this phase?" below) -- cannot do the upload
handshake (there's no shim to intercept a `need_uploads` response and read the file itself) and,
for a bearer-token download link, has to already be attaching the exact same `Authorization`
header every other `/mcp` request uses. That's true of a real MCP client, but not of what such a
client often hands the *work* of actually moving bytes to: a sandboxed agent shelling out to
`curl`, which has no access to the MCP session's own bearer token and no way to attach a header at
all.

So Phase 4 adds a second, orthogonal way to reach the same two staging stores -- a **capability
URL**: a 32-byte token embedded directly in the path, needing no header of any kind, because the
token itself is the one-time credential.

- **Upload**: the new `privacyfence_create_upload_slot` meta-tool (an ordinary, bearer-
  authenticated `tools/call`, exactly like any other) mints an `UploadStagingStore` slot for the
  calling principal and returns `{upload_id, upload_url, method: "PUT", max_bytes, expires_at,
  example}`. `upload_url` is `.../mcp-files/slots/<token>` -- `PUT` the file's bytes there directly,
  no bearer header needed. The caller then passes `upload_id` back to whichever tool actually needs
  the file (`drive_upload_file`'s own `upload_id` parameter, or an `"upload:<upload_id>"` entry in
  a `gmail_*_with_attachments` tool's `attachments` array), which claims it the ordinary,
  principal-checked way (`local_files.require_local_files`'s `upload:` handling, reusing
  `UploadStagingStore.claim()` unchanged) -- so *filling* a slot needs no principal, but *claiming*
  it still does. A capability token is not a bypass of principal-binding, only of the bearer
  header specifically.
- **Download**: `.../mcp-files/fetch/<token>` claims a `DownloadStagingStore` entry the same
  no-principal way. Local mode's own no-bridge fallback (`local_files._deliver_link`) uses this
  route instead of the old bearer-authenticated one; org mode's `DownloadDeliveryConfig.
  agent_links` (default `true`) makes every staged-link download use it too, since an org-mode
  caller is always an agent, never a browser with a session cookie -- the cookie-authenticated
  `/downloads/{token}` route (routes_downloads.py) stays for a human who opens the link themselves,
  gated behind `agent_links: false`.
- **New routes, no new stores.** Both capability routes are handled by
  `upload_staging.UploadStagingStore.afill_capability`/`download_staging.DownloadStagingStore.
  claim_capability` -- the exact same encrypted-at-rest, single-use, TTL-bound stores Phase 1
  built, with the principal check made optional (`None` skips it) rather than duplicated into a
  second store. Mounted under the *same* `/mcp-files` prefix as Phase 1's bearer pair, in the same
  `Mount` (`web/routes_file_bridge.py`'s `_FileBridgeRouter`) -- two `Mount`s cannot share one
  prefix in Starlette (the first one registered swallows every request under it, auth stack
  included, regardless of what the second one would have matched), so one dispatcher decides by
  path segment (`/slots`, `/fetch` -> no auth; everything else -> the Phase 1 bearer stack) instead.
- **What a leaked capability URL actually exposes**, since this is the one place this ADR adds a
  secret that travels through a model's own context rather than an HTTP header a client manages
  itself: an upload URL lets someone else fill *your* pending slot with *their* bytes -- which still
  has to pass the destination tool's own preview/PII-scan/approval gate before anything happens
  with it, exactly like any other upload. A download URL exposes one already-approved file, once,
  for at most its TTL (10 minutes for uploads, `download_delivery.link_ttl_seconds` -- 5 minutes by
  default -- for downloads). Neither URL grants standing access to anything: both tokens are
  single-use and expire.

### D6 (Phase 4, restated for D4's neighbor): never overwrite an existing file, still applies

D4's "never overwrite" guard is the shim's own guarantee, on the write side of the *bridge*
specifically -- Phase 4 doesn't touch that path (a bridge-capable client keeps using the shim, and
D4's collision-avoiding rename logic is unchanged). It's restated here only to note the boundary:
a capability download URL is fetched by whatever client asked for it, which decides for itself how
(or whether) to write the bytes to disk -- there is no shim standing between this daemon and that
client's own choice about overwriting, because there is no shim in this client's request path at
all. Nothing in that gap is new risk this ADR introduces: a client fetching a URL and choosing where
to save it is true of any HTTP download, with or without PrivacyFence.

## Rationale

### Why not fix this by granting the service account read/write access to the user's home directory?

That is ADR 0003's whole premise, inverted. "The agent cannot approve its own request" holds
because the account making tool calls has no standing access to the human's files or
human-authority state; a filesystem ACL that hands it back defeats the separation for every file,
not just the ones a tool call happens to need.

### Why does the shim need to see `params.arguments` at all, when D2 says it stays schema-agnostic?

Guard G1 is the answer: without checking that a path the daemon names was one the agent's own
request already contained, there would be no defense against a compromised or buggy daemon using
the shim as a file oracle for the user's whole filesystem. Reading `params.arguments` for exactly
this one check is a narrower, named exception than "the shim understands tool schemas" -- it never
has to know what a `local_path` or `destination_dir` parameter *means*, only whether a string the
daemon sent back was already present, verbatim, in the request the agent made.

### Why can the daemon not just ask the shim once per file, synchronously, instead of a two-round handshake?

The daemon has no way to open a connection *to* the shim -- the shim is purely an outbound HTTP
client to the daemon's `/mcp` plus an inbound stdio server to Claude Desktop (see `index.ts`'s
module docstring); nothing in this architecture lets the daemon initiate a request. The
need-uploads/resend shape works within the existing request/response direction: the shim already
gets to see and react to every message the daemon sends back, so "answer with what's needed,
receive it, ask again" is the only shape available without adding a second listener to the shim
(which D1's rejected-alternatives reasoning also argues against -- it would need to be
authenticated and authorized independently of the one channel that already is).

### Why was org mode untouched by Phase 1, and what does Phase 4 actually add to it?

Org mode's daemon runs on whatever machine hosts PrivacyFence's server deployment, not the user's
machine, and has never had a `.mcpb` shim in the request path at all -- its clients talk to `/mcp`
directly. Phase 1 left it alone entirely: its existing delivery story
(`org_mode.DownloadDeliveryConfig`: inline for small files, a one-time staged link for larger ones
-- the same `DownloadStagingStore` Phase 1's download side reuses) was unaffected;
`local_files.can_access_user_files()` is unconditionally `False` in org mode, but nothing in org
mode's own connector branches called into `local_files.py` at all (every
`if self.download_mode != "org": ... else: ...` split in `drive.py`/`gmail.py`/`confluence.py` kept
the `org` branch calling exactly the code it called before Phase 1).

That is still true of *filesystem* paths -- an org-mode `local_path` still means "read from
wherever PrivacyFence's own server runs," unchanged, and still never touches `local_files.py`.
Phase 4 adds exactly two things to org mode, both orthogonal to that: `upload_id`/`"upload:<id>"`
now work in org mode too (a capability slot claim is not a filesystem read, so it needs none of
what made Phase 1 org-mode-inapplicable -- see D5 above), and `DownloadDeliveryConfig.agent_links`
changes which URL shape a staged download's `download_url` uses. Neither reopens the "why is org
mode different" question Phase 1 answered; D5 explains why a capability claim was never subject to
it in the first place.

## Consequences

**Every platform (local mode, privilege-separated):**

- `drive_upload_file`, `drive_download_file`, `gmail_download_attachment`, the three
  `gmail_*_with_attachments` tools, and `confluence_download_attachment` work again against paths
  under the real user's home directory. Before this ADR they failed with a generic "Tool call
  failed" message (B1) after guessing at directories that could never have worked.
- The `.mcpb` shipped with the DMG/`.pkg`/installer/`.deb` must be current -- an old shim has no
  `X-PrivacyFence-File-Bridge` header, so the daemon falls back to the no-bridge error message
  (SS1.4) rather than silently failing. The 4.1.6 release notes tell users to reinstall the
  extension in Claude Desktop.
- macOS TCC: the shim, as a child of Claude.app, writing into `~/Downloads`/`~/Documents`/
  `~/Desktop` can trigger a "Claude would like to access..." permission prompt, attributed to
  Claude rather than PrivacyFence. Expected; documented in the README's troubleshooting section.

**Everywhere (local mode, unseparated -- dev checkouts, pip/pipx installs):**

- No behavior change. `local_files.can_access_user_files()` is `True`, so every connector's direct
  read/write branch runs exactly as it did before this ADR.

**Org mode:** filesystem `local_path`/`destination_dir` behavior is unchanged -- see Rationale
above. Phase 4 adds `upload_id`/`"upload:<id>"` support and `DownloadDeliveryConfig.agent_links`,
neither of which is a filesystem-path concern (see D5).

**Non-shim clients (Claude Code, any other direct HTTP MCP client), since Phase 4:** uploads work
via `privacyfence_create_upload_slot` and the `upload_id`/`"upload:<id>"` parameter it feeds into;
downloads (local mode's no-bridge fallback, and every org-mode staged link by default) use the
capability route instead of a bearer-authenticated one. See D5.

**Security posture, explicitly:**

- The daemon, as the service account, still never reads or writes user files directly -- it asks
  for bytes and receives them, or hands bytes over and is told where they landed, but the actual
  `open()` against the user's filesystem happens only in the shim, running as the user.
- Every uploaded byte still passes the same preview, PII scan and approval gate the tool call
  always ran, because the handshake happens *before* any of that -- an upload that never completes
  never reaches a gate at all. A capability-slot upload is no different: the slot's bytes are only
  ever read once the destination tool's own `require_local_files`/`upload:` claim runs, at the same
  point in that tool's own flow a bridge-fetched `local_path` would be.
- A download still reaches disk only after approval; staging happens after the gate, exactly where
  the direct-write branch used to write immediately after it. A capability download link is no
  different: it names an already-staged, already-approved file.
- New surface is guard G1 (D3), two bearer-authenticated HTTP endpoints (Phase 1), and, since Phase
  4, two capability HTTP endpoints authorized by their own token alone (D5) -- all four single-use
  and TTL-bound, both stores encrypted at rest.

## Out of scope

- **Phase 2** (companion as daemon manager, upgrade-leaves-daemon-stopped fixes) and **Phase 3**
  (one principal per OS user, kernel-authenticated) are separate changes in
  `local-mode-fixes-plan.md`. Phase 1 is written so Phase 3 gets per-principal isolation of staged
  transfers for free (every staging directory and every claim is already principal-scoped), without
  Phase 1 depending on Phase 3 landing first.
- **Streaming uploads/downloads through disk instead of memory** on the daemon side.
  `upload_staging.UploadStagingStore.fill()` buffers an incoming upload fully in memory before
  encrypting it to disk; `local_files.deliver_file()`'s bridge path holds a download's bytes in
  memory for the same reason. This bounds Phase 1 to files under
  `file_bridge.max_download_bytes`/each tool's own upload cap (both configurable, both far below
  what would meaningfully pressure daemon memory for a single-user desktop install) rather than
  arbitrary size. Larger transfers streamed through disk on the daemon side, if ever needed, are
  future work.

## Verification

- `tests/unit/test_local_files.py`, `tests/unit/test_upload_staging.py`,
  `tests/unit/test_download_staging.py`, `tests/unit/web/test_routes_file_bridge.py`: the Python
  half of the protocol, including the resolution order (already-uploaded, then `upload:` reference,
  then direct, then bridge, then fallback error), auth/principal-binding/TTL/single-use/413 on the
  Phase 1 HTTP routes, the Phase 4 capability routes working with *no* auth header at all while
  still being single-use and TTL-bound, and that a >100KB PDF is PII-scanned from full bytes rather
  than throwing on a truncated prefix (B4).
- `mcpb/shim/test/fileBridge.test.ts`: the TypeScript half -- the upload handshake round trip, G1
  rejecting an undeclared path, a JSON-array-string `attachments` value satisfying G1, a download
  collision producing `name (1).ext`, a sha mismatch producing `isError` and no partial file left
  behind, messages with no file-bridge `_meta` passing through unchanged, a second `need_uploads`
  for one id producing `isError` rather than looping. Untouched by Phase 4 -- the capability routes
  are reached directly by an HTTP client, never through the shim.
- `tests/integration/test_shim_mcp_contract.py` (extended): the real built shim against a real
  unseparated daemon with the bridge forced on via a test-only setting, proving the Python and
  TypeScript halves agree on the wire format rather than each unit-testing its own guess at it.
- Connector-level tests (`test_drive_connector.py`/`test_gmail_connector.py`/
  `test_confluence_connector.py`): with the bridge on and no uploads staged yet, the upload/
  attachment tools raise before any approval UI is invoked (assert the fake approval UI is never
  called) -- this is the concrete form of "B2: the approval popup no longer shows a file the daemon
  can't read." Extended for Phase 4 with `upload_id`/`"upload:<id>"` claims (including in org mode,
  and including the wrong-principal case) and `DownloadDeliveryConfig.agent_links`'s two link
  shapes.
- `tests/unit/web/test_routes_mcp.py`/`tests/unit/web/test_mcp_tools.py`: `privacyfence_create_
  upload_slot`'s own wire round trip and manifest entry.
- `tests/unit/web/test_server.py`/`tests/unit/web/test_server_org_mode.py`: the capability routes
  are actually reachable with no bearer token in both modes' real `build_app()` wiring, not just in
  routes_file_bridge.py's own isolated tests -- this is what caught `_FileBridgeRouter`'s own
  original bug (see that class's docstring): scope path is not sub-app-relative under a plain ASGI
  `Mount`, so a naive dispatcher just proxied every capability request into the bearer-authenticated
  app instead of ever reaching the unauthenticated one.

## Related

- [ADR 0003](0003-separated-installs-only.md) -- the privilege-separation boundary this ADR crosses
  safely; nothing here relaxes what that ADR guarantees (see Consequences above).
- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) -- decision 6's "the agent cannot
  approve its own request," the guarantee D1's rejected alternatives are weighed against.
- [`docs/org-mode-download-delivery.md`](../org-mode-download-delivery.md) -- org-mode's own
  delivery story, whose `DownloadStagingStore` Phase 1's download side reuses; its "Local mode"
  section was rewritten by Phase 1, and it gained its own "Uploads and downloads for clients
  without the shim (Phase 4)" section from this phase.
