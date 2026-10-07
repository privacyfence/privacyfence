# Plan: plugin framework change requests (approvals, no truncation, outputs)

## Goal

These are the change requests from the data-lake design, in
[privacyfence/privacyfence#846 (comment)](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618),
built on top of the plugin framework that `feature/plugin-framework` implements. This work lands as
a **second, stacked PR**: its feature branch first merges the finished `feature/plugin-framework`,
then adds the changes below.

- **CR1. Approvals that persist.**
  - A plugin asks a human to approve a *thing* with `approval.request`: a piece of code, a template
    or a mapping, identified by `(kind, subject_id, digest)`.
  - The card shows PrivacyFence's own fields (plugin, kind, subject, digest), the plugin's preview
    blocks and, optionally, the plugin's own page embedded in a sandboxed frame.
  - An approval binds to the content's digest and stays valid until a human revokes it in
    Settings. The plugin checks it with `approval.check` and hears `approval.revoked`.
  - No rule can approve it, it is refused while an unattended session runs, and step-up is the
    default. `confirm.request` stays as it is.
- **CR2. Source reads never truncate.**
  - Every `source.call` operation either returns all of its data or a page plus a `next_cursor`.
  - The size limit becomes a page size, not a failure, and a cursor only works with the
    parameters it was issued for.
  - `jira.search` and `calendar.list_events` page through the provider's own page tokens.
  - `sheets.get_values` and `confluence.get_page` split oversized results.
  - `drive.download` reads binary files with HTTP Range requests, with no size cap.
  - The SDK gets a generic page iterator.
  - Salesforce report paging is
    [privacyfence/privacyfence#854](https://github.com/privacyfence/privacyfence/issues/854) and
    stays out of scope.
- **CR3. An output sandbox.**
  - A plugin with `outputs: true` publishes files into a per-principal output directory.
  - PrivacyFence exposes them through two of its own tools: `plugin_outputs_list` (auto) and
    `plugin_outputs_read` (review gate, PII scan, paged under the inline limit).
  - A rule scope `plugin:<name>:output` takes a path prefix, so "Always allow" can cover one
    folder of one plugin.
  - This is also how large results reach the AI (CR5), so the 100 KB inline limit on prepared
    payloads stays.
- **CR6. Child processes.** A plugin may start child processes. They run under the plugin's
  account, PrivacyFence does not supervise them, and the plugin is responsible for confining them.
  This is documentation and an ADR only.

CR4 is skipped, as the comment says. CR5 is covered by CR3.

The user decided four things when this plan was made:

- a stacked PR;
- Range reads for Drive, with no size cap;
- keep `confirm.request` as it is;
- `plugin_outputs_list` is auto and `plugin_outputs_read` is review.

## Current state

All line numbers are on `feature/plugin-framework` at `c418f305`, where p16 is merged and p17 and
p18 were still running when this plan was written. Phase p0 brings in whatever came after.

### Source API (`src/privacyfence/plugins/source_ops.py`, `spool.py`, `constants.py`)

- `SourceAdapter(connector, client_attr, run, targets, validate)` is at `source_ops.py:70`. `run`
  returns `(data, next_cursor)`.
- `_serve` (:309) does the following in order:
  1. checks introspection, the principal and the allowlist;
  2. checks the connector's state;
  3. `adapter.validate(params)`;
  4. runs the adapter in `asyncio.to_thread` under `principal_scope(LOCAL_PRINCIPAL)`;
  5. serializes the result. Over `MAX_SOURCE_RESULT_BYTES` (12 MiB, `constants.py:27`) the call
     fails with `payload_too_large`.

  The return shape is `{"operation", "data", "bytes", "next_cursor"}`. `handle_source_call`
  (:365) audits every call through `_audit` (:282).
- The adapters, one by one:
  - `jira.search`: `client.search_issues(jql, max_results)` with `max_results` between 1 and 500,
    default 100. The cursor is always `None`. `JiraClient.search_issues`
    (`jira_client.py:313-336`) follows `nextPageToken` internally up to `MAX_PAGES=10`, then
    drops the token.
  - `calendar.list_events`: `client.list_events(calendar_id, max_results, time_min, time_max)`
    with `max_results` between 1 and 250. The cursor is always `None`.
    `CalendarClient.list_events` (`calendar_client.py:442`) pages internally with `pageToken`
    (`:466-480`, `MAX_PAGES=10`, `:37`) and drops the token.
  - `sheets.get_values`: `{"values": client.get_sheet_values(...)}`. `confluence.get_page`:
    `dataclasses.asdict(client.get_page(page_id))`. Neither pages.
  - `drive.download`: `DownloadSpool.read_chunk(client, plugin, file_id, *, offset, cursor,
    length)` (`spool.py:86`).
    - The first call downloads the whole file with `download_file_bytes` and refuses anything
      over `DRIVE_MAX_FILE_BYTES` (64 MiB, `constants.py:29`, at `spool.py:146-160`).
    - Chunks of up to 8 MiB are then served from a spool file. The cursor is base64url JSON
      `{"f","r","o"}` (`spool.py:32-55`), and `modified_time` is the revision.
- Drive client:
  - `_stream_full_content` (`drive_client.py:1486`) does
    `AuthorizedSession(self._load_credentials())` and a GET on `.../files/{id}?alt=media&supportsAllDrives=true`,
    or on the export URL for a Google-native file per `_GOOGLE_DOC_EXPORTS` (:48).
  - No code in `src` sends a `Range` header.
  - `DriveFile.size` is 0 for native files (`drive_client.py:1131`).
  - ADR 0105 puts `AuthorizedSession` callers out of scope of the retrying transport, and
    `tests/unit/test_google_http.py` does not forbid them.
- Live checks: `scripts/qa_fixture_recorder.py` `check_jira` (:860), `check_drive` (:1158) and
  `check_calendar` (:1215) never call a paging or download method, so a new client method is not
  exercised by `connector-live-check.yml` unless a check calls it.
- Client tests:
  - `tests/unit/test_jira_client.py`: `TestSearchIssues` (:466), plus `TestRequest`.
  - `tests/unit/test_calendar_client.py`: `TestListEvents` (:616).
  - `tests/unit/test_drive_client.py`: `TestDownloadFileBytes` (:2514), which monkeypatches
    `drive_client_module.AuthorizedSession` with the `_FakeStreamResponse` helper (:2385).

### Confirmations, cards and pages

- `plugins/confirm.py`:
  - `ConfirmationService(*, registry_provider, unattended_active, executor, audit)` is at :74.
  - `request` (:91) calls `registry.register_confirm(sensitive=..., notify=True)`, then
    `registry.set_html(card.id, build_confirmation_html(..., body_blocks=to_card_blocks(preview)))`,
    then starts `_finalize_when_answered` (:144) in the executor.
  - `await_` (:121) polls `registry.await_status`.
  - `KIND_RE` is at :43, and `_AWAIT_STATUS` maps statuses.
- `approvals.py`:
  - `register_confirm(*, sensitive=False, notify=False)` is at :630.
  - `PendingApproval` (:235) has `id`, `kind`, `sensitive`, `expires_at`, `html`, `event`,
    `result`, `final_decision` and `decided_at`.
  - `finalize` is at :747 and `await_status` at :917.
  - `CONFIRM_RESULTS = ("confirm", "cancel")` is at :142.
- **A card is a standalone document.**
  - `GET /approvals/{id}` (`web/routes_approvals.py` `show_approval`, :522-573) serves
    `card.html` top-level. It calls `_set_csp_nonce(request, extract_csp_nonce(card.html))` and
    injects the bridge shim.
  - The list page only links to cards and never frames them (`approval_list_html.py:57-62`).
- **Framing is blocked twice today.**
  - Every app document's CSP has `frame-src data:` (`web/csp.py:135`, `build_csp` at :85-139).
    `tests/unit/web/test_server.py::TestCspNonce::test_object_src_and_frame_src_allow_data_uris`
    (:403) pins exactly `data:`.
  - Every `/plugins/` response has `plugins/pages.py:22-25`'s CSP, which ends in
    `frame-ancestors 'none'`. It also has `X-Frame-Options: DENY`, which
    `_SecurityHeadersMiddleware` (`web/server.py:416-499`, prefix `_PLUGIN_PAGES_PREFIX` :413)
    sets on every path.
  - The headers are replaced, not appended, so a route cannot add its own.
  - Tests pinning these:
    - `tests/unit/plugins/test_pages.py::TestCsp::test_exact_string` (:211);
    - `tests/unit/web/test_server.py::TestPluginPagesSandboxCsp` (:467-508);
    - `tests/unit/web/test_routes_plugins.py` `SANDBOX_HEADERS` (:22-28);
    - `tests/integration/test_plugin_framework.py` `SANDBOX_CSP` (:37);
    - the SDK test host's `_pages.SECURITY_HEADERS`, with
      `tests/unit/plugin_sdk/test_testhost_surfaces.py:22`.
- `csp.set_nonce(request, nonce)` and `csp.nonce_for(request)` (`web/csp.py:57-79`) pass
  per-response state from a route to the middleware through `request.state`.
- Plugin page routes (`web/routes_plugins.py`):
  - `build_routes(plugin_host, *, is_owner_session)` is at :63.
  - `plugin_page` (:69-84) checks the owner session (a plain 404 otherwise) and the name. It then
    calls `render_plugin_page(host, name, raw, parse_query(qs), current_principal())`
    (`pages.py:117`), which normalizes the path (`pages.normalize_path`), calls
    `host.web_request` and runs `filter_response`.
- The session cookie is `pf_session`, `SameSite=Strict` (`web/session_auth.py:329-330`). A frame
  that the same-origin card navigates carries it.
- Browser test helpers:
  - `tests/integration/test_plugin_pages_browser.py`: the `browser` fixture (:78-90), and a
    `server` fixture (:93-106) that builds a `WebServer` with a fake `plugin_host`. Sign-in is
    `srv.bootstrap.mint(provenance=PROVENANCE_HUMAN)`.
  - `tests/integration/test_browser_smoke.py::_watch_csp_violations` (:2088-2100).
  - `tests/integration/test_plugin_card_escaping_browser.py` asserts that a *confirm* card has no
    `iframe`. It stays true, because approvals get their own builder.

### Host, storage, state, settings

- `plugins/host.py`:
  - `PluginHost.__init__` is at :126. `ConfirmationService` is built at :157-162 with
    `audit=self._audit_confirm` (:240).
  - `_record(plugin, tool, summary, decision)` (:218) writes an `AuditEntry`.
  - `_handlers(plugin)` (:402-439) returns `{"source.call", "confirm.request", "confirm.await"}`
    and `{"tools.changed"}`.
  - `_initialize_params` (:361-378) calls `storage.ensure_dirs` and builds
    `principals=[principal_context(LOCAL_PRINCIPAL, per_principal[id])]`.
  - `inspect` (:547) builds the review dict at :579-599.
  - `purge` (:637) calls `storage.remove_all` at :649. It does not remove rules.
  - `_uninstall` (:300-307) removes the data, the `plugin:<name>:` rules and the state record.
  - `rows()` is at :666-693 and `connectors()` at :659. `submit` is at :197, and `_changed` (:210)
    fires the rows listener.
- `plugins/storage.py`: `install_dir` (:26), `principal_dir` (:30, giving
  `<user_dir>/plugin-data/<name>/user`), `ensure_dirs` (:34) and `remove_all` (:53, `rmtree` of
  `<data>/plugin-data/<name>` and `users/*/plugin-data/<name>`).
- `plugins/state.py`: `plugins-state.json`, `STATE_VERSION = 1`. `PluginRecord.from_json`
  requires every key.
- `plugins/manifest.py`: `_KEYS` (:26) and the `Manifest` dataclass (:37). An unknown key is an
  error.
- `plugins/protocol.py`:
  - `PrincipalContext` (:152) is `id, display_name, storage_dir, roles`.
  - `ConfirmRequestParams` (:399) and `SourceCallParams` (:370).
  - `principal_context(principal, storage_dir, *, mode)` is at :495.
- Protocol version:
  - `PROTOCOL_VERSION = "1.0.0"` (`constants.py:13`); the supervisor compares only the major
    (`supervisor.py:288-309`).
  - The SDK's own copy is at `plugin-sdk/src/privacyfence_plugin_sdk/plugin.py:33`, pinned by
    `tests/unit/plugin_sdk/test_plugin.py::TestLimits::test_block_limits_and_version` (:108).
  - The schema's copy is `x-protocol-version`, pinned by
    `tests/unit/plugins/test_protocol.py::TestSchema` (:541).
- Settings:
  - `settings_controller.py` has `_submit_plugin` (:1217) and the plugin actions (:1231-1245). The
    snapshot key `"plugins": host.rows()` is at :1721.
  - `web/routes_settings.py` classifies plugin actions at :249-251 (sensitive) and :266-268 (not
    sensitive).
  - `web/org_settings_scope.py` lists the plugin `ActionScope`s at :115-119.
  - `settings_window_html.py`: `renderPluginRow` (:1191-1220), `renderPluginDialog` (:1239), and
    the click handlers at :1475-1515.
- Audit decisions: `constants.py:73-75` and the comment block at `audit_log.py:155` and
  `:312-336`.

### Policy (from the framework's dynamic registration)

- `auto_accept.register_dynamic_tools(owner, specs)` and `DynamicToolSpec`.
- `policy/scopes.register_plugin_selector`, which matches values as a set: every value the call
  returned must be in the rule.
- `policy/propose.register_dynamic_scopes(owner, tool, predicates)`.

None of these knows a path-prefix match. The output scope needs one.

### SDK and test host (`plugin-sdk/src/privacyfence_plugin_sdk/`)

- `plugin.py`:
  - `SourceClient.call` (:129) returns `SourceResult(data, bytes, next_cursor)`.
  - `download` (:149-201) loops over `next_cursor` and retries once on `revision_changed`. It
    writes `name.part` and then calls `os.replace`.
  - `ConfirmClient` is at :205.
  - `_EVENTS` (:62) lists the notifications the SDK accepts. `@plugin.on` refuses any other name.
  - `serve` (:669) maps the methods.
  - `Principal(id, display_name, storage_dir)` is at :75.
- Testing package:
  - `testing/_host.py`: `PluginTestHost` (:163). Its peer handlers (:255-259) are the extension
    point. It keeps its own `_EVENT_NAMES` (:55).
  - `testing/_source.py`:
    - `SourceFixtures` (:89) offers `.when(...).returns(data, next_cursor)`.
    - `encode_cursor` and `decode_cursor` (:39-44) mirror the spool's cursor.
    - `_serve_drive` is at :195.
    - It keeps its own `DRIVE_MAX_FILE_BYTES`.
  - `testing/_confirm.py`: `Confirmations` (:41).
  - Samples: `testing/samples/*.json`, all with `next_cursor: null`.
- Tests:
  - `tests/unit/plugin_sdk/test_plugin.py`: `TestLimits` (:87-108) and `TestDownload`
    (`chunk_handler` :515).
  - `tests/unit/plugin_sdk/test_testhost.py:290` compares the test host's limits with the
    daemon's.
  - `tests/unit/plugins/test_sdk_samples.py`.
  - `tests/integration/test_sdk_testhost_conformance.py`: `SdkSide` (:68), `DaemonSide` (:97) and
    `TestSameDownload`.
- The echo fixture lives in `tests/fixtures/plugins/echo/`:
  - `echo_plugin.py`, whose pages include `/download` and `/source`;
  - `harness.py`, with `Stack`, `install_echo`, `mcp_session`, `Popups`, `FakeDrive` and `until`.

  `tests/integration/test_plugin_framework.py::TestConfirmRoundTrip` (:188) shows how to drive a
  card: `stack.registry.answer(approval_id, "confirm")`, then `privacyfence_await_approval`.

### Docs and ADRs

These come in with p0. When this plan was written, the framework's retirement phase (p18) had not
yet written them:

- ADRs 0120–0126;
- the new `plugins.md` and `plugin-protocol.md` reference docs in docs/;
- the CHANGELOG entry.

The first free ADR number after them is 0127.

## Design

The D-numbers are plan-internal: **never write them, phase ids, "CR1"-style labels, "#846" or a
section sign into code, tests, workflows or examples** (`tests/unit/test_code_no_history.py`). Cite
ADRs by number, or the issue by full URL.

### D1. Protocol 1.1

- `PROTOCOL_VERSION = "1.1.0"` in `constants.py`, in the SDK's `plugin.py` and in the schema's
  `x-protocol-version`. The major version stays 1, and a 1.0 plugin keeps working. The daemon
  only ever sends `approval.revoked` and `PrincipalContext.output_dir` when the plugin's manifest
  asks for the feature, and the plugin has used the feature.
- New constants in `constants.py`, verbatim:

  ```python
  SOURCE_PAGE_BUDGET_BYTES = MAX_SOURCE_RESULT_BYTES - 64 * 1024   # room for the envelope
  CURSOR_MAX_CHARS = 4096
  JIRA_PAGE_SIZE_MAX = 100
  CALENDAR_PAGE_SIZE_MAX = 250
  SUBJECT_ID_MAX_CHARS = 200
  DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")      # always .fullmatch()
  APPROVAL_KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,40}")   # always .fullmatch()
  OUTPUT_TYPES: dict[str, tuple[str, ...]] = {
      "application/json": (".json",),
      "text/csv": (".csv",),
      "text/html": (".html", ".htm"),
      "text/plain": (".txt",),
      "text/markdown": (".md",),
  }
  DEFAULT_OUTPUT_TYPES = ("application/json", "text/csv")
  OUTPUT_READ_PAGE_BYTES = 90_000                     # under INLINE_RESULT_BYTES with the envelope
  OUTPUT_LIST_PAGE = 200
  OUTPUT_MAX_DEPTH = 8
  AUDIT_PLUGIN_APPROVAL = "plugin_approval"
  AUDIT_PLUGIN_OUTPUT = "plugin_output"
  ```

  Add `"approval.request": 5.0` to `TIMEOUT_SECONDS`.
- **Remove** `DRIVE_MAX_FILE_BYTES`, together with its `x-limits` entry, the SDK test host's copy
  and every test that pins it. The size cap is gone (D4).
- New `$defs` in `protocol.schema.json`:
  - `ApprovalRequestParams`, `ApprovalRequestResult`;
  - `ApprovalCheckParams`, `ApprovalCheckResult`;
  - `ApprovalAwaitParams`, `ApprovalAwaitResult`;
  - `ApprovalRevokedParams`.

  Changed `$defs`:
  - `PrincipalContext` gets an optional `output_dir` string;
  - `Manifest` gets `outputs` and `output_types`;
  - `SourceCallParams` gets the paging params of D3;
  - `x-limits` gets the new numbers.

  Regenerate `types.py` with `python3 scripts/gen_plugin_sdk_types.py`.
- New dataclasses in `protocol.py`, each with `from_wire(obj, *, mode="local")` and `to_wire()` in
  the file's existing style: `ApprovalRequestParams`, `ApprovalCheckParams` and
  `ApprovalAwaitParams` (fields in D6). `PrincipalContext` gains `output_dir: str | None = None`,
  and `principal_context(..., output_dir: Path | None = None)` gains the same keyword.
- Manifest (`manifest.py`):
  - two new optional keys: `outputs: bool` (default false) and `output_types: list[str]`;
  - every entry of `output_types` must be a key of `OUTPUT_TYPES`, else `ManifestError("output
    type <t> is not supported")`;
  - `output_types` is only allowed with `outputs: true`;
  - the default is `DEFAULT_OUTPUT_TYPES`;
  - `Manifest` gains `outputs: bool = False` and `output_types: tuple[str, ...] = ()`. When
    `outputs` is true and `output_types` is absent, the field holds `DEFAULT_OUTPUT_TYPES`.

### D2. Cursors (`src/privacyfence/plugins/cursors.py`, new)

One envelope for every paged operation, bound to the parameters it was issued for:

```python
class CursorError(ValueError): ...

def params_digest(operation: str, bound: dict) -> str
    # sha256 hex of json.dumps({"op": operation, "p": bound}, sort_keys=True, separators=(",", ":"))

def encode(operation: str, bound: dict, state: dict) -> str
    # base64url (no padding) of json {"v": 1, "op": operation, "d": params_digest(operation, bound), "s": state}

def decode(cursor: str, operation: str, bound: dict) -> dict
    # returns state; CursorError("cursor is not valid") on bad base64/JSON/shape or length > CURSOR_MAX_CHARS;
    # CursorError("cursor belongs to a different call") when op or digest differ
```

`source_ops` turns a `CursorError` into `RpcError("invalid_params", str(exc))`. The cursor is not
secret and not signed. Binding it to the parameters stops a plugin from carrying a cursor over to
a different query by mistake; it is not a security boundary, since the plugin reads with its own
rights either way.

### D3. Paging per operation (`source_ops.py`)

Each adapter defines `bound(params) -> dict`: the validated parameters a cursor is tied to. Every
operation also accepts `cursor` (an optional string, at most `CURSOR_MAX_CHARS`). `next_cursor` is
`null` exactly when nothing is left.

**Fitting a page.** `_fit_prefix(items: list, budget: int) -> int` returns the largest `n` such
that `len(json.dumps(items[:n], default=str).encode()) <= budget`, found by binary search. If
`n == 0` and `items` is not empty, the result is `RpcError("payload_too_large", "a single record is
larger than the page limit")`. That is the only size refusal left. The budget is
`SOURCE_PAGE_BUDGET_BYTES`.

| Operation | Params (new in bold) | `bound` | Cursor state | Behaviour |
|---|---|---|---|---|
| `jira.search` | `jql`; **`page_size`** 1..`JIRA_PAGE_SIZE_MAX`, default 100; `max_results` is accepted as an alias for `page_size` when `page_size` is absent, and its upper bound drops from 500 to 100; **`cursor`** | `{jql, page_size}` | `{"t": provider token or null, "k": skip}` | `items, next_t = client.search_issues_page(jql, page_size, t)`, then `items = items[k:]`, then fit. If everything left on the provider page fits, `next = {"t": next_t, "k": 0}` when `next_t` is set, else `null`. If not, `next = {"t": t, "k": k + n}` |
| `calendar.list_events` | `calendar_id`, `time_min`, `time_max`; **`page_size`** 1..`CALENDAR_PAGE_SIZE_MAX`, default 250; `max_results` is an alias as above; **`cursor`** | `{calendar_id, time_min, time_max, page_size}` | same as Jira | `client.list_events_page(calendar_id, page_size, time_min, time_max, t)`; the rest as Jira |
| `sheets.get_values` | as today, plus **`cursor`** | `{spreadsheet_id, range, value_render_option}` | `{"k": first row}` | Fetch the whole range (the client returns it whole), take `rows[k:]`, fit, and return `{"values": rows[k:k+n], "first_row": k}`. `next = {"k": k+n}` while rows remain |
| `confluence.get_page` | as today, plus **`cursor`** | `{page_id}` | `{"o": body char offset}` | `page = asdict(...)`, then `body = page["body"]`. With the body empty, fit the rest of the page alone. Otherwise find the largest body slice `body[o:o+m]` that fits (binary search on `m`), and return the page with `body` replaced by the slice plus `body_offset: o` and `body_total_chars: len(body)`. Both fields are always present. `next = {"o": o+m}` while `o+m < len(body)` |
| `drive.download` | `file_id`, `length`, `offset` or `cursor` as today | `{file_id}` | `{"r": revision, "o": offset}` | D4. The old `{"f","r","o"}` cursor format is gone |
| `salesforce.report_run` | unchanged | — | — | Unchanged. It is the one operation that can still return `payload_too_large` for a large report. Paging is [privacyfence/privacyfence#854](https://github.com/privacyfence/privacyfence/issues/854). The protocol doc says so |

The size check in `_serve` (`len(payload) > MAX_SOURCE_RESULT_BYTES`) stays as a backstop. The
adapters already fit their pages under the budget.

The audit `targets` gain `; page` when a cursor was given. The summary keeps `bytes=<n>`, and
adds `; more` when `next_cursor` is set.

**New client methods.** These are the only client changes for paging. Each method makes exactly
one provider request:

```python
# jira_client.py
def search_issues_page(self, jql: str, page_size: int = 100,
                       page_token: str | None = None) -> tuple[list[JiraIssue], str | None]:
    # one self._request(self._client.enhanced_jql, jql, nextPageToken=page_token, limit=page_size);
    # returns (parsed issues, result.get("nextPageToken") if not result.get("isLast", True) else None);
    # empty jql -> JiraClientError; any Exception -> JiraClientError("search_issues_page failed: ...")

# calendar_client.py
def list_events_page(self, calendar_id: str, page_size: int = 250, time_min: str = "",
                     time_max: str = "", page_token: str | None = None) -> tuple[list[CalendarEvent], str | None]:
    # one service.events().list(calendarId, singleEvents=True, orderBy="startTime", maxResults=page_size,
    #   timeMin/timeMax when non-empty, pageToken when set).execute(); returns (parsed, nextPageToken or None);
    # HttpError -> CalendarClientError(f"list_events_page({calendar_id}) failed: ...")
```

`search_issues` and `list_events` keep their behaviour. Refactor them to call the page methods in
their loops, so the request code exists once. Their existing tests must pass unchanged.

### D4. Drive Range reads (`drive_client.py`, `spool.py`)

New client method:

```python
def download_range(self, file_id: str, offset: int, length: int) -> bytes:
    # binary files only. AuthorizedSession(self._load_credentials()).get(
    #   f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true",
    #   headers={"Range": f"bytes={offset}-{offset + length - 1}"}, stream=True)
    # 206 -> the body; 416 -> b"" (offset at or past the end);
    # 200 -> DriveClientError("download_range: the server ignored the Range header") (never read a whole file here);
    # any other error -> DriveClientError(f"download_range({file_id}) failed: ...")
```

`DownloadSpool` keeps its name and gains one method. The old `read_chunk` and its
`encode_cursor`/`decode_cursor` stay, rebuilt on top of the new method, until the paging phase
switches `source_ops` over and deletes them. Every merge in between keeps working:

```python
class DownloadSpool:
    def read_chunk_at(self, client, plugin: str, file_id: str, *, offset: int, length: int,
                      expected_revision: str | None) -> tuple[dict, int | None, str]
        # returns (data dict as today, next_offset or None at eof, revision)
```

`read_chunk_at` works like this:

1. `metadata = client.get_file_metadata(file_id)`, and `revision = metadata.modified_time`.
2. If `expected_revision` is set and differs from `revision`, raise
   `RpcError("upstream_error", "the file changed while it was being downloaded",
   extra={"reason": "revision_changed"})`, as today.
3. **Binary file** (`metadata.mime_type` is not in `drive_client._GOOGLE_DOC_EXPORTS`):
   - `total = metadata.size`;
   - if `offset > total`, the request is `invalid_params`;
   - otherwise `chunk = client.download_range(file_id, offset, min(length, total - offset))`
     when `total > offset`, else `b""`.

   There is no spool and no size cap. If `len(chunk)` differs from the requested length, raise
   `RpcError("upstream_error", "the file changed while it was being downloaded",
   extra={"reason": "revision_changed"})`.
4. **Google-native file:** keep today's spool path (`download_file_bytes` into a `0600` spool
   file, keyed by revision, swept after `DRIVE_SPOOL_IDLE_SECONDS`), with **no size cap**.
   Google limits exports itself, and an oversized export comes back as a client error
   (`upstream_error`).

`source_ops` keeps the cursor (D2/D3) and calls `read_chunk_at` with `offset = state["o"]` and
`expected_revision = state["r"]`.
It encodes `next = {"r": revision, "o": next_offset}` while `next_offset` is not `None`.

`connector-live-check.yml` must exercise the new methods, so `scripts/qa_fixture_recorder.py`
changes:

- `check_drive` calls `download_range(seed_file_id, 0, 16)` when the QA manifest has a binary
  seed file. It reads the manifest's existing `drive` section; if there is no binary seed file,
  it skips that call with a note in the report.
- `check_jira` calls `search_issues_page(jql, 1)`, using the same JQL as its existing fallback.
- `check_calendar` calls `list_events_page(calendar_id, 1, time_min, time_max)`.

Each records only what its checks already record, so `EXPECTED_FIXTURES` does not change.

### D5. Approvals store (`src/privacyfence/plugins/approvals.py`, new)

The file is `paths.data_dir()/plugin-approvals.json`, written with
`atomic_write_json(mode=0o600, indent=2)`:

```json
{"version": 1, "approvals": [
  {"approval_id": "…", "plugin": "datalake", "principal": "local", "kind": "processor-code",
   "subject_id": "pipeline-dashboard/clean.py", "digest": "sha256:…", "title": "…",
   "decided_at": "2026-10-08T10:00:00Z", "revoked_at": null}]}
```

```python
@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str; plugin: str; principal: str; kind: str; subject_id: str; digest: str
    title: str; decided_at: str; revoked_at: str | None

class ApprovalStore:
    def __init__(self, path: Path) -> None
    def find(self, plugin, principal, kind, subject_id, digest) -> ApprovalRecord | None   # latest by decided_at
    def add(self, record: ApprovalRecord) -> None
    def revoke(self, plugin: str, approval_id: str, *, now: str) -> ApprovalRecord | None
    def for_plugin(self, plugin: str) -> list[ApprovalRecord]      # newest first
    def forget_plugin(self, plugin: str) -> int                     # number removed
```

- Every write is a locked read-modify-write, the same pattern as `state.py`.
- A corrupt file, or one with the wrong version, reads as empty and is logged at warning. That
  fails closed: `check` then answers `unknown`.
- Only approved decisions are stored. Denied and expired requests leave no record.

### D6. Approval protocol and service (`approvals.py`, continued)

| Method | Dir | Params | Result | Errors |
|---|---|---|---|---|
| `approval.request` | P→D | `principal, kind (APPROVAL_KIND_RE), subject_id (1..SUBJECT_ID_MAX_CHARS, no control or bidi characters), digest (DIGEST_RE), title (≤ MAX_TITLE_CHARS), preview: Block[], page? (string; normalized with pages.normalize_path; no "?"), require_step_up (default true)` | `{approval_id, status: "pending"\|"approved", expires_at?}` | `introspection_only`, `invalid_params`, `invalid_blocks`, `unknown_principal`, `confirmation_refused` (`reason: unattended_session`) |
| `approval.check` | P→D | `principal, kind, subject_id, digest` | `{status: "approved"\|"revoked"\|"unknown", approval_id?, decided_at?}` | `invalid_params`, `unknown_principal` |
| `approval.await` | P→D | `approval_id, timeout_ms?` (≤ `CONFIRM_AWAIT_MAX_MS`) | `{status: "approved"\|"denied"\|"expired", decided_at?}` | `timeout`, `invalid_params` |
| `approval.revoked` | D→P notif | `approval_id, kind, subject_id, digest` | — | — |

`ApprovalService(*, store: ApprovalStore, registry_provider, unattended_active, executor, audit)`
has three methods: `async request(plugin, display_name, manifest, params, *, introspecting)`,
`async check(plugin, params)` and `async await_(plugin, params)`.

`request` works like this:

1. Refuse while introspecting. Parse the params with `ApprovalRequestParams.from_wire`. A `page`
   when `manifest.pages` is false is `invalid_params` ("page needs pages: true in the manifest").
2. If `store.find(...)` returns an unrevoked record, return `{approval_id: record.approval_id,
   status: "approved"}` and show no card.
3. If a card for the same `(plugin, principal, kind, subject_id, digest)` is still pending, return
   its id with `status: "pending"`.
4. If `unattended_active()`, refuse with `confirmation_refused`, `reason: unattended_session`.
5. Otherwise:
   - `card = registry.register_confirm(sensitive=require_step_up, notify=True)`;
   - `card.frame_src = f"/plugins/{plugin}{page}?pf_approval={card.id}"` when a page is given,
     else `""`;
   - `registry.set_html(card.id, build_plugin_approval_html(...))` (D7);
   - remember the pending tuple;
   - start the finalizer in the executor, as `ConfirmationService` does;
   - audit `"<kind>; requested"`;
   - return `{approval_id, status: "pending", expires_at}`.
6. The finalizer:
   - `"confirm"` → `registry.finalize(id, "accept")`, then `store.add(record)` with
     `decided_at = now`, then audit `"<kind>; approved"`;
   - `"cancel"` → `finalize(id, "deny")` and audit `"<kind>; denied"`;
   - a timeout → `finalize(id, "expired")` and audit `"<kind>; expired"`.

`check` returns `approved` for a record without `revoked_at`, `revoked` when the latest record for
the tuple has `revoked_at`, and `unknown` otherwise.

`await_` works like `ConfirmationService.await_`.

Revocation goes through `PluginHost.revoke_approval(name, approval_id)` (D10).

Audit decision is `AUDIT_PLUGIN_APPROVAL`, via a host `_audit_approval(plugin, kind, status)`, the
same shape as `_audit_confirm`. The summary never holds the subject or the digest, only the kind
and the status. The tuple stays in the store.

**Never auto-accepted.** The card is `kind="confirm"` with no operation key, so
`reevaluate_all` skips it, exactly as for confirmations (ADR 0122).

### D7. The approval card (`dialog_window_html.py`, `approvals.py` field)

- `PendingApproval` gains `frame_src: str = ""` (`approvals.py`). Nothing else in `approvals.py`
  changes.
- New builder:

  ```python
  def build_plugin_approval_html(*, title: str, fields: list[tuple[str, str]], body_blocks: list[dict],
                                 frame_src: str = "", frame_title: str = "") -> str
  ```

  It uses the same `_document` as `build_confirmation_html`, with these parts in order:
  1. the escaped title;
  2. a `fields` table of escaped label and value pairs: Plugin, Kind, Subject, Digest (the full
     `sha256:` string, in a `pf-code` style);
  3. the blocks through `build_preview_body_html(blocks=…)`;
  4. when `frame_src` is set, `<iframe class="pf-plugin-frame" sandbox="allow-scripts"
     src="<escaped frame_src>" referrerpolicy="no-referrer" title="<escaped frame_title>"
     loading="eager"></iframe>`;
  5. the Deny and Approve buttons.

  The frame's height is a CSS token-based size (`min-block-size: 24rem`) in
  `resources/approval_window/styles.css`, with no colour literals.
- The fields render **outside** the frame and before it, so the plugin page cannot hide or
  change what is being approved.
- `frame_src` is built only by `ApprovalService` from a validated plugin name, a normalized path
  and the card id, and is escaped once more here.

### D8. Letting exactly that frame load (`web/csp.py`, `web/server.py`, `web/routes_approvals.py`, `web/routes_plugins.py`, `plugins/pages.py`)

The goal is to loosen framing only for one card's response and only for that plugin response.

- `web/csp.py` gains `set_frame_self(request)` and `frame_self_for(scope_or_request) -> bool`,
  next to `set_nonce`/`nonce_for`, through the same `request.state` mechanism.
  `build_csp(nonce, *, app_origin="", frame_self=False)`: when `frame_self` is true,
  `frame-src data: 'self'`; otherwise `frame-src data:`, unchanged.
- `routes_approvals.show_approval` calls `csp.set_frame_self(request)` when `card.frame_src` is
  not empty, next to `_set_csp_nonce`. The middleware passes `frame_self=frame_self_for(...)` to
  `build_csp`.
- `web/csp.py` also gains `set_plugin_embed(request)` and `plugin_embed_for(...)`.
  `plugins/pages.py` gains a second constant:

  ```python
  CSP_EMBEDDED = ("sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; form-action 'none'; "
                  "base-uri 'none'; frame-ancestors 'self'")
  ```

  For a `/plugins/` path with the embed flag set, `_SecurityHeadersMiddleware` sets
  `CSP_EMBEDDED` and `X-Frame-Options: SAMEORIGIN` instead of `CSP` and `DENY`. Every other
  header, and every other path, is unchanged.
- `routes_plugins.plugin_page` sets the embed flag only when the query has `pf_approval=<id>` and
  `await host.approval_embed_allowed(name, approval_id, normalized_path)` returns true. That
  requires a pending (not finalized) approval card whose `frame_src` path equals `/plugins/<name>`
  plus the normalized path, owned by that plugin.

  The query, `pf_approval` included, is forwarded to the plugin unchanged, so the page can render
  the right subject. A `pf_approval` that does not qualify is ignored: the page is served with the
  normal `frame-ancestors 'none'` and the frame stays blank.
- `PageHost` (`pages.py:44`) gains `approval_embed_allowed(name, approval_id, path) -> bool`.

### D9. Outputs (`src/privacyfence/plugins/outputs.py`, new)

**Directory.** Per principal: `paths.user_dir(principal)/plugin-data/<name>/outputs`, created
`0700` before `initialize` when `manifest.outputs` is true. It is passed as
`PrincipalContext.output_dir`. `storage.output_dir(name, principal) -> Path` is new, and
`storage.remove_all` already covers it, because it lives under `plugin-data/<name>`.

**Visibility rules** (`OutputIndex`):

- A file is published when it is a regular file (checked with `os.lstat`, so symlinks are
  ignored), under the directory once resolved, at most `OUTPUT_MAX_DEPTH` segments deep, with no
  path segment starting with `.`, and with an extension that belongs to one of the plugin's
  `output_types` (via `OUTPUT_TYPES`).
- Plugins write `.name.tmp` and then rename. The SDK does this (D11).
- Paths are POSIX-style, relative to the output directory, with no leading `/`.

```python
@dataclass(frozen=True)
class OutputFile:
    path: str; size: int; modified: str; mime_type: str      # modified: RFC 3339 UTC

def list_outputs(root: Path, output_types: tuple[str, ...], *, prefix: str = "",
                 after: str | None = None, limit: int = OUTPUT_LIST_PAGE) -> tuple[list[OutputFile], str | None]
    # sorted by path; returns files with path > after and path.startswith(prefix); next = last path or None

def read_output(root: Path, output_types: tuple[str, ...], path: str, *, offset: int = 0,
                max_bytes: int = OUTPUT_READ_PAGE_BYTES) -> dict
    # validates path (same rules; else ValueError("No such output file.")); reads bytes [offset, offset+max_bytes),
    # backs off to a UTF-8 character boundary; returns {"path", "mime_type", "size", "sha256" (whole file),
    #  "offset", "length", "next_offset" (None at end), "text"}; offset past the end -> ValueError
```

**Tools.** `PluginOutputsConnector(Connector)`, `name = "plugin_outputs"`, built by the host
(D10) and present in `connectors()` while at least one **enabled** plugin has `outputs: true`.

| Tool | Gate | Params | Returns |
|---|---|---|---|
| `plugin_outputs_list` | auto, read-only | `plugin` (str, required), `prefix` (str, default ""), `cursor` (str, default "") | `{"plugin", "files": [{path, size, modified, mime_type}], "next_cursor"}` |
| `plugin_outputs_read` | review, read-only | `plugin`, `path` (both required), `offset` (int, default 0), plus the gated `reason` param | `read_output(...)` plus `"plugin"` |

- Tool descriptions follow ADR 0115: a summary, a `Returns` sentence, the sibling tool, and the
  approval wording.
  - `plugin_outputs_list`: "List files a plugin has published to its output folder. Returns path,
    size, modified time and type, 200 per page, with next_cursor for more. Use
    plugin_outputs_read to read one. Runs without asking."
  - `plugin_outputs_read`: "Read one published plugin output file, about 90 KB per call. Returns
    the text, its offset and next_offset to continue, and the whole file's sha256. Use
    plugin_outputs_list to find paths. Shows an approval card unless a rule allows this folder."
- The `plugin` param is the plugin's name. An unknown, disabled or non-outputs plugin raises
  `RuntimeError("No plugin named <x> publishes outputs.")`.
- Cursors for the list are `cursors.encode("plugin_outputs.list", {"plugin": p, "prefix":
  prefix}, {"a": last_path})`.
- Gate (read):

  ```python
  gated_call(connector="plugin_outputs", tool="plugin_outputs_read",
             tool_name=f"Read {display_name} output", summary=path, sender="",
             raw_data={"plugin": name, "path": path}, filtered_data=result, gate="review",
             preview={"Plugin": display_name, "File": path, "Size": f"{size} bytes", "Part": f"{offset}-{offset+length}"},
             details_text=result["text"], pii_scan_text=result["text"], args=args)
  ```

  The file is read **once** before the card, and the released result is those bytes.
- `plugin_outputs_list` writes the auto audit entry in the `connectors/tasks.py:447` shape, with
  `decision="auto_accepted"`. Every read is audited by `gated_call`. An extra
  `AUDIT_PLUGIN_OUTPUT` entry, `connector=f"plugin:{name}"` with summary
  `f"read {path}; offset={offset}; bytes={length}"`, attributes the read to the plugin's output
  source, as the issue asks.
- **Registration.** While the outputs connector exists, the host registers it with
  `auto_accept.register_dynamic_tools("plugin_outputs", [...])`:
  - list: gate auto, no operation key;
  - read: gate review, operation `plugin_outputs.read`, verb `READ`, layout WIDE.

  It unregisters when the last outputs plugin is disabled or removed.

### D10. Output scope (`policy/scopes.py`, `policy/propose.py`)

- Predicate `f"plugin:{name}:output"` (`constants.scope_predicate(name, "output")`), registered
  by `scopes.register_plugin_output_selector(name)` and removed by
  `unregister_plugin_output_selector(name)`. The selector has
  `scope_type=f"{name}.output"`, `kind=IDENTITY` and `resolves_from=ARGS`.
- Matching:

  ```python
  def matches(value, ctx):
      raw = ctx.raw_data if isinstance(ctx.raw_data, dict) else {}
      if raw.get("plugin") != name or not isinstance(raw.get("path"), str):
          return False
      path = raw["path"]
      for v in _values_of(value):
          if not isinstance(v, str) or not v:
              continue
          if v.endswith("/") and path.startswith(v):
              return True
          if path == v:
              return True
      return False
  ```

  A value ending in `/` is a folder prefix; any other value is one exact file. An empty value
  never matches.
- Proposals. `propose.register_dynamic_scope_entry(owner: str, tool: str, entry: ProposableScope)`
  and `unregister_dynamic_scope_entries(owner)` are a generic addition next to the framework's
  `register_dynamic_scopes`. Per outputs plugin, the host registers one entry for tool
  `plugin_outputs_read`:
  - predicate `plugin:<name>:output`, `scope_type=f"{name}.output"`, `connector="plugin_outputs"`,
    verbs `(READ,)`;
  - `value_of = lambda ctx: (posixpath.dirname(ctx.raw_data["path"]) + "/" if "/" in path else
    path) if ctx.raw_data.get("plugin") == name else NO_VALUE`;
  - `hint="this folder"`, or `"this file"` at the root;
  - `entry_id` and `group` are `f"plugin:{name}:output@plugin_outputs.read"`, and
    `widenable=False`.

  `connector_of_operation("plugin_outputs.read")` must return `"plugin_outputs"`.
- The selector's matches are a prefix match. They are **not** the set match that the framework's
  `register_plugin_selector` uses. That is why this is a separate function, and the framework's
  is unchanged.
- Uninstall (`_uninstall`) already removes `plugin:<name>:` rules, so output rules go with the
  plugin.

### D11. Host wiring (`plugins/host.py`, `plugins/storage.py`)

- **Construction:**
  - `self._approvals = ApprovalService(store=ApprovalStore(paths.data_dir() / "plugin-approvals.json"), registry_provider=…, unattended_active=lambda: self._unattended(), executor=<the same _DaemonThreadExecutor instance as confirmations>, audit=self._audit_approval)`;
  - `self._outputs_connector: PluginOutputsConnector | None = None`.
- **Handlers.** `_handlers` adds `approval.request`, `approval.check` and `approval.await`. A
  `confirmation_refused` from `approval.request` is also audited `"<kind>; refused"`, as for
  confirms.
- **Initialize.**
  - `_initialize_params` creates `storage.output_dir(name, LOCAL_PRINCIPAL)` (mode `0700`) when
    `manifest.outputs` is true, and passes `output_dir=` to `principal_context`.
  - The review summary from `inspect` gains `"outputs": bool` and
    `"output_types": [...]`.
- **Embedding.** `approval_embed_allowed(name, approval_id, path)` delegates to
  `ApprovalService.embed_allowed(plugin, approval_id, frame_path)`, which checks the pending tuple
  and that the card is not finalized.
- **Revocation.** `async revoke_approval(name, approval_id)`:
  - `store.revoke`;
  - audit `"<kind>; revoked"`;
  - if the plugin is running, `peer.notify("approval.revoked", {approval_id, kind, subject_id,
    digest})`, best effort;
  - `_changed()`.

  A revoke of an unknown id raises `ValueError("No such approval.")` through `_action`.
- **Purge and uninstall.** `purge` and `_uninstall` also call `store.forget_plugin(name)` and
  audit `"approvals deleted: <n>"` when `n > 0`.
- **Rows.** `rows()` adds `"approvals"` to each row: a list of `{approval_id, kind, subject_id,
  digest, decided_at, revoked_at}`, newest first, at most 200. It also adds `"outputs": bool`.
- **Outputs connector.** `connectors()` includes the outputs connector under `"plugin_outputs"`
  while any **enabled** plugin with `outputs: true` exists. It is rebuilt on every state change,
  and `_notify_tools()` fires when it appears or disappears, together with the registration in D9
  and D10.

### D12. Settings (`settings_controller.py`, `web/routes_settings.py`, `web/org_settings_scope.py`, `settings_window_html.py`)

- New action `revoke_plugin_approval {name, approval_id}`:
  - non-sensitive, because it only takes trust away (ADR 0070's rationale for disable);
  - `ActionScope(modes=frozenset({LOCAL_MODE}))`;
  - `SettingsController.revoke_plugin_approval(name, approval_id)` goes through
    `_submit_plugin`.
- Each plugin row gets an **Approvals** block, collapsed by default and shown when the row has at
  least one approval. It is a list with one line per approval, showing kind, subject, the digest
  shortened to `sha256:` plus its first 12 hex digits (the full digest in a `title` attribute) and
  the date, followed by "Revoked <date>" or a **Revoke** button
  (`dataAttr('revoke_plugin_approval', {name, approval_id})`).
- The review dialog shows "Publishes output files: JSON, CSV" when `review.outputs` is true.
- Use design-system primitives and tokens only. Add a `TestPhoneLayout` case.

### D13. SDK (`plugin-sdk/src/privacyfence_plugin_sdk/`)

- `PROTOCOL_VERSION = "1.1.0"`.
- `Principal` gains `output_dir: Path | None`.
- `_EVENTS` gains `"approval.revoked"`, and so does the test host's `_EVENT_NAMES`.
- `ctx.source.pages(operation, **params) -> AsyncIterator[SourceResult]`:
  - calls `call` with the params, then with `cursor=next_cursor`, until `next_cursor` is `None`;
  - never sends `max_results`, so callers use `page_size`.

  `ctx.source.collect(operation, **params) -> list` is only for `jira.search` and
  `calendar.list_events`. It concatenates the `data` lists and raises `ValueError` for any other
  operation.
- `download` is unchanged in use. It passes the opaque cursor through, and its tests use the new
  cursor format from the test host.
- `ctx.approvals`:
  - `request(kind, subject_id, content: bytes | str, title, preview, page=None,
    require_step_up=True) -> ApprovalTicket(approval_id, status)` computes the digest itself
    (`sha256:` over the UTF-8 or raw bytes);
  - `check(kind, subject_id, content) -> str`;
  - `await_(approval_id, timeout_ms=None) -> ConfirmResult`;
  - `digest(content) -> str` is exposed as a static helper.
- `ctx.outputs`:
  - `publish(relpath: str, data: bytes | str) -> str` writes `<output_dir>/<dir>/.<name>.tmp`,
    fsyncs, then `os.replace`s it to the final name. It refuses an existing final path
    (`FileExistsError`; a new version is a new name), a path with `..` or a leading `/`, and an
    extension the manifest's `output_types` do not allow, which the plugin declares in the SDK as
    `Plugin(..., output_types=(...))`. It returns the relative path.
  - `ctx.outputs.dir -> Path`.
  - Both raise `RuntimeError("this plugin has no output folder")` when `output_dir` is `None`.
- New exports: `ApprovalTicket`.
- README: paging, approvals, outputs, the child-process rules (D14), and that pages shown in an
  approval card get `pf_approval` in the query.

**Test host** (`testing/`):

- `_source.py`:
  - `when(op, **params).returns_pages([data1, data2, …])` serves page *i* for the cursor the
    previous page returned;
  - test-host cursors use the same `cursors.encode` format, which `_source.py` copies as a small
    private function;
  - drive chunks use the D3/D4 cursor;
  - `DRIVE_MAX_FILE_BYTES` is removed.
- `_approvals.py` (new):
  - `host.approvals`, a list of requests;
  - `await host.decide_approval(approval_id, "approve"|"deny"|"expire")`;
  - `host.revoke_approval(approval_id)` sends `approval.revoked`;
  - check semantics as D6.
- `host.output_dir`: a tmp output folder when `PluginTestHost(..., outputs=True,
  output_types=(...))`, plus `host.list_outputs(prefix="")`, which uses the same rules as D9 (a
  private copy).
- Samples: `jira.search.json` and `calendar.list_events.json` get a second file each,
  `<op>.page2.json`. The first sample's `next_cursor` is a real envelope pointing at the second.

### D14. Child processes (docs and ADR only)

In the plugins doc, under "Child processes":

- A plugin may start child processes.
- They run under the plugin's (the service) account, with the plugin's environment.
- PrivacyFence does not supervise them, restart them, count their crashes or stop them. Stopping
  the plugin kills its process group on POSIX and its process on Windows, as today.
- The plugin is responsible for confining them, for example with no network, read-only inputs
  and one scratch folder.
- A child holds the same trust as the plugin.

The trust ADR's threat model gains this in a new ADR that amends 0121 (D-ADRs below).

### D15. Echo and today

- **echo** (`tests/fixtures/plugins/echo/`) gains:
  - a tool `approve`. Its prepare previews a subject. Its execute calls `ctx.approvals.request(
    "echo-template", "templates/a", content=<args.text>, title="Approve template", preview=[…],
    page="/approval")` and returns the ticket;
  - a page `/approval` that shows the `pf_approval` query value;
  - a page `/approval-check?text=` that returns `ctx.approvals.check(...)` as JSON;
  - an event recorder for `approval.revoked`;
  - a tool `publish` (popup) that calls `ctx.outputs.publish("reports/<args.name>.csv", <args.text>)`;
  - the manifest keys `outputs: true` and `output_types: [text/csv, application/json]`;
  - a page `/pages?op=` that collects every page of a source operation through
    `ctx.source.pages` and returns the page count and the item count.
- **today** (`examples/plugins/today/`) gains:
  - a `today_export` tool (popup) that publishes `exports/<date>.csv` with today's events;
  - a `today_approve_layout` tool that requests approval (`kind: page-layout`, `subject_id:
    today/layout`) for the page's layout template, with `page="/approval"` showing the template;
  - a page note that the layout is "approved" or "not approved", using `approval.check`;
  - the manifest keys `outputs: true` and `output_types: [text/csv]`.

  Its README gains steps 13–16 of the smoke test.

## ADRs

The retirement phase writes these, numbered from the next free number after the framework's
ADRs (expected 0127):

- **0127.** A plugin approval binds to `(plugin, principal, kind, subject_id, digest)`, persists
  until revoked in Settings, is never auto-accepted, is refused while unattended, and has step-up
  on by default. The card shows PrivacyFence's fields outside a sandboxed frame of the plugin's
  page. Framing is loosened only for that card's response (`frame-src 'self'`) and for a plugin
  response tied to a pending approval of that plugin (`frame-ancestors 'self'`). Rejected:
  - allowing framing of every plugin page;
  - rendering plugin HTML inside the card document;
  - folding `confirm.request` into it.
- **0128.** Plugin source reads never truncate. Every operation pages with an opaque cursor bound
  to its parameters, and a size limit is a page size. The only remaining size refusal is a single
  record larger than a page, plus Salesforce report runs until
  [privacyfence/privacyfence#854](https://github.com/privacyfence/privacyfence/issues/854).
  Amends 0123.
- **0129.** Drive binary downloads for plugins are HTTP Range reads with no size cap. Only
  Google-native exports keep a spool. Rejected: streaming whole files to a spool with no cap,
  where the first chunk waits for the whole file. Amends 0123.
- **0130.** Plugin outputs are a per-principal folder that PrivacyFence reads through its own
  tools: `plugin_outputs_list` is auto and `plugin_outputs_read` is review. The
  `plugin:<name>:output` scope is a path prefix. Large results go through outputs, and the 100 KB
  inline limit on prepared payloads stays (ADR 0092). Rejected: raising the inline limit.
- **0131.** A plugin's child processes run under the plugin's account, unsupervised, and the
  plugin confines them. Amends 0121.

## Manual steps

The checklist page is linked in the manifest (`manual_steps_artifact`).

- **Before:** `mb1-framework-run-finished`. The plugin-framework `/implement` run must have
  merged every phase, through the retirement phase, and opened its PR. p0 merges that branch.
- **After:** `ma1-smoke-test-cr`. On a packaged install, with Drive, Jira and Calendar connected:
  - approve the `today` layout, including the embedded page, then check, revoke and re-check it;
  - read a `today` export through `plugin_outputs_list`/`plugin_outputs_read` and add a folder
    rule;
  - the `today` README's steps 13–16 hold the exact clicks.

## Risks and open questions

Each one has what a worker sees. Stop with `status=blocked` instead of improvising.

- **The framework run is not finished** (p0). If `origin/feature/plugin-framework` has no
  `docs/adr/0120-*.md`, or the plugins reference doc is missing from docs/, stop.
- **Merge conflicts in p0.** `docs/README.md` and `scripts/build_site.py` conflict on the plan
  entries. Resolve mechanically: keep this plan's entry, and drop the framework plan's entry,
  which the framework's retirement phase removed. Any other conflict is semantic: stop.
- **Drive Range with `AuthorizedSession`.** If a test shows that `AuthorizedSession` drops the
  `Range` header, or that Drive answers 200 for a binary file in the fake, stop. Do not fall back
  to downloading whole files.
- **The card frame and the CSP.** If the browser test in p6 reports a `frame-src` or
  `frame-ancestors` violation with the flags set, or a frame that loads with no `pf_approval`
  match, stop. Do not loosen the CSP globally.
- **History rule.** No D-numbers, phase ids, CR labels, `#846`, digits-only hex colours or section
  signs in code, tests, examples or workflows.
- **Docs references.** No phase before the retirement phase writes a docs-slash-name-dot-md path
  that does not exist on its branch at that moment. The plugins and protocol reference docs exist
  only after p0.
- **ADR numbers.** If 0127 is taken when the retirement phase runs, renumber and fix every
  citation (`grep -rn "ADR 01[23]" src plugin-sdk tests examples`).

## Implementation manifest

```yaml
plan_slug: plugin-framework-cr
feature_branch: feature/plugin-framework-cr
tracking_issue: 846
max_parallel: 3
manual_steps_artifact: https://claude.ai/artifact/VLCjHAdogU15FKmWXTmPAz
manual_steps_source: docs/plugin-framework-cr-plan-manual-steps.html
manual_before:
- id: mb1-framework-run-finished
  title: The plugin-framework /implement run has merged every phase (including its retire phase) and opened its PR
  why: p0 merges origin/feature/plugin-framework into this branch; started earlier, this branch would miss phases and the ADRs and reference docs every later phase builds on.
  done_when: The plugin-framework PR to main is open, and docs/adr/README.md on feature/plugin-framework lists ADR 0120.
manual_after:
- id: ma1-smoke-test-cr
  title: Run the change-request smoke test (today plugin steps 13-16) on a packaged install
  why: CI cannot click a real approval card with an embedded plugin page and a passkey, revoke it in Settings, or read a published output through a real AI client.
verify_after_merge:
- ruff check .
- python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/policy tests/unit/test_auto_accept.py tests/unit/test_code_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py -q
final_checks:
- docs/plugin-framework-cr-plan.md and docs/plugin-framework-cr-plan-manual-steps.html are deleted and nothing links to them
- the five ADRs from the plan's ADRs section exist with Status Accepted and are in docs/adr/README.md's index
- CHANGELOG.md has [Unreleased] entries for approvals, paging, outputs and child processes and no new version heading
- The full /dod passes, including python3 scripts/check_coverage_floor.py coverage.json and python3 -m pytest tests/integration -v
- connector-live-check.yml dispatched against feature/plugin-framework-cr is green (client files changed; link the run in the PR)
- build.yml dispatched against feature/plugin-framework-cr is green (today example changed)
phases:
- id: p0-sync
  title: Merge the finished plugin framework into this branch
  depends_on: []
  complexity: S
  touches:
  - docs/README.md
  - scripts/build_site.py
  brief: |
    1. git fetch origin feature/plugin-framework. Check that `git show origin/feature/plugin-framework:docs/adr/README.md`
       lists ADR 0120 and that `git ls-tree origin/feature/plugin-framework docs/` shows the plugins reference doc
       (plugins.md) and the protocol reference doc (plugin-protocol.md). If not, stop with status=blocked: the
       framework run has not finished (plan Risks, first bullet).
    2. git merge --no-ff origin/feature/plugin-framework (a merge commit; never rebase). Resolve conflicts in
       docs/README.md and scripts/build_site.py mechanically: keep the entry for this plan
       (plugin-framework-cr-plan.md) and drop the entry for the framework's own plan, which its retire phase
       removed. Any conflict in any other file: abort and stop with status=blocked.
    3. Record in the PHASE-REPORT: the merged SHA, the ADR numbers the framework took (ls docs/adr | tail), and
       whether 0127 is free.
    4. Run ruff check . and python3 -m pytest tests/unit -q; both must pass (the known base failure listed in the
       framework ledger, tests/unit/web/test_agent_attestation.py::TestAuditRows::test_org_mode_audit_page_shows_the_principals_own_rows_with_agents,
       may still fail if it fails on origin/main too; say so in the report).
  acceptance:
  - git log --oneline -1 --merges shows the merge of origin/feature/plugin-framework
  - ls docs/adr/0120-*.md succeeds
  - python3 -m pytest tests/unit/test_website_docs_allowlist.py tests/unit/test_docs_references_exist.py tests/unit/plugins -q passes
- id: p1-protocol
  title: Protocol 1.1 constants, cursors, message types, manifest keys, schema and SDK types
  depends_on: [p0-sync]
  complexity: M
  touches:
  - src/privacyfence/plugins/constants.py
  - src/privacyfence/plugins/cursors.py
  - src/privacyfence/plugins/protocol.py
  - src/privacyfence/plugins/manifest.py
  - docs/plugin-protocol/protocol.schema.json
  - plugin-sdk/src/privacyfence_plugin_sdk/types.py
  - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_source.py
  - tests/unit/plugins/test_constants.py
  - tests/unit/plugins/test_cursors.py
  - tests/unit/plugins/test_protocol.py
  - tests/unit/plugins/test_manifest.py
  - tests/unit/plugin_sdk/test_plugin.py
  - tests/unit/plugin_sdk/test_testhost.py
  brief: |
    1. constants.py per Design D1: PROTOCOL_VERSION "1.1.0", the new constants verbatim, and "approval.request" in
       TIMEOUT_SECONDS. Keep DRIVE_MAX_FILE_BYTES in constants.py (spool.py still imports it; the Drive phase
       deletes it), but drop it from the schema's x-limits in step 5.
    2. cursors.py per D2 (CursorError, params_digest, encode, decode), with a module docstring citing ADR 0128.
    3. protocol.py per D1: ApprovalRequestParams, ApprovalCheckParams, ApprovalAwaitParams with the D6 field rules
       (APPROVAL_KIND_RE, SUBJECT_ID_MAX_CHARS and control/bidi rejection by running the subject through
       blocks' sanitizer and refusing if it changed, DIGEST_RE, title, preview via the validate_blocks parameter,
       page: a string starting with "/", no "?", at most MAX_PAGE_PATH_CHARS -- full normalization happens in the
       service), PrincipalContext.output_dir and principal_context(..., output_dir=None).
    4. manifest.py per D1: outputs, output_types, the Manifest fields, the error texts.
    5. protocol.schema.json per D1 (new and changed $defs, x-limits, x-protocol-version 1.1.0, drop
       DRIVE_MAX_FILE_BYTES from x-limits); run python3 scripts/gen_plugin_sdk_types.py to regenerate types.py.
    6. SDK: plugin.py PROTOCOL_VERSION "1.1.0" only (one line); testing/_host.py and testing/_source.py: add copies
       of the new limits only where the limits tests compare them, and leave DRIVE_MAX_FILE_BYTES in _source.py
       (the SDK test host phase removes it).
    7. Tests: test_cursors.py (round trip; wrong op; wrong params; bad base64; overlong; tampered state still
       decodes but a different digest is refused), test_protocol.py (each new dataclass valid/invalid; TestSchema
       still passes with the new defs), test_manifest.py (outputs/output_types rules), test_constants.py, and
       update tests/unit/plugin_sdk/test_plugin.py and test_testhost.py limit/version pins for 1.1.0.
  acceptance:
  - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/test_gen_plugin_sdk_types.py -q passes
  - python3 scripts/gen_plugin_sdk_types.py --check exits 0
  - grep -c DRIVE_MAX_FILE_BYTES docs/plugin-protocol/protocol.schema.json prints 0
- id: p2-client-pages
  title: Single-page Jira and Calendar client methods and the live check calls
  depends_on: [p0-sync]
  complexity: S
  touches:
  - src/privacyfence/jira_client.py
  - src/privacyfence/calendar_client.py
  - scripts/qa_fixture_recorder.py
  - tests/unit/test_jira_client.py
  - tests/unit/test_calendar_client.py
  - tests/unit/test_qa_fixture_recorder.py
  brief: |
    1. jira_client.py: search_issues_page exactly per Design D3; refactor search_issues to call it in its loop so
       the request code exists once. Every existing TestSearchIssues and TestRequest test passes unchanged.
    2. calendar_client.py: list_events_page per D3; refactor list_events onto it the same way. Every existing
       TestListEvents test passes unchanged.
    3. scripts/qa_fixture_recorder.py: check_jira also calls search_issues_page(<the JQL its fallback already
       uses>, 1) and check_calendar also calls list_events_page(<its calendar id>, 1, <its time range or "">, ...),
       each reporting pass/fail like the surrounding checks and recording nothing new (EXPECTED_FIXTURES
       unchanged).
    4. Tests: TestSearchIssuesPage (one request, token returned, isLast -> None, errors), TestListEventsPage (one
       request, pageToken passed, nextPageToken returned, HttpError), and a recorder test that the two calls are
       made (follow the file's existing check tests).
    No change to src/privacyfence/connectors/**. The §2.7 live-check row is covered by the final check that
    dispatches connector-live-check.yml.
  acceptance:
  - python3 -m pytest tests/unit/test_jira_client.py tests/unit/test_calendar_client.py tests/unit/test_qa_fixture_recorder.py -q passes
  - git diff --stat HEAD~1 -- src/privacyfence/connectors shows nothing
- id: p3-drive-range
  title: Drive Range reads with no size cap
  depends_on: [p1-protocol, p2-client-pages]
  complexity: M
  touches:
  - src/privacyfence/drive_client.py
  - src/privacyfence/plugins/spool.py
  - src/privacyfence/plugins/constants.py
  - scripts/qa_fixture_recorder.py
  - tests/unit/test_drive_client.py
  - tests/unit/plugins/test_spool.py
  - tests/unit/test_qa_fixture_recorder.py
  - tests/unit/plugin_sdk/test_testhost.py
  brief: |
    1. drive_client.py: download_range exactly per Design D4, built like _stream_full_content (AuthorizedSession
       from _load_credentials, stream=True). Tests in tests/unit/test_drive_client.py class TestDownloadRange,
       monkeypatching drive_client_module.AuthorizedSession like TestDownloadFileBytes (:2514) does: 206 body,
       Range header value, 416 -> b"", 200 -> DriveClientError, other errors.
    2. spool.py: add DownloadSpool.read_chunk_at with the D4 signature and behaviour (binary: Range, no spool, no
       cap, short read -> revision_changed; native: spool as today without the cap). Re-implement the existing
       read_chunk (same signature and old cursor format, which source_ops still uses until the paging phase) as a
       thin wrapper over read_chunk_at. Update the module docstring (no "Drive has no range reads").
    3. constants.py: delete DRIVE_MAX_FILE_BYTES. tests/unit/plugin_sdk/test_testhost.py: drop only its comparison
       of the test host's DRIVE_MAX_FILE_BYTES with the daemon constant (the test host keeps its own copy until the
       SDK test host phase).
    4. Do not touch source_ops.py or host.py.
    5. scripts/qa_fixture_recorder.py: check_drive calls download_range(seed_file_id, 0, 16) when its manifest
       section has a binary seed file (read the existing drive section keys; if none fits, skip with a note).
    6. Tests: tests/unit/plugins/test_spool.py extended for read_chunk_at: TestBinaryRange (no spool file written; a
       100 MiB fake size reads its last chunk without any full download; short read -> revision_changed),
       TestRevision, TestNativeSpool (spooled, swept), TestOldCursorStillWorks.
    Stop condition: plan Risks "Drive Range with AuthorizedSession".
  acceptance:
  - python3 -m pytest tests/unit/test_drive_client.py tests/unit/plugins/test_spool.py tests/unit/plugins/test_source_ops.py tests/unit/test_qa_fixture_recorder.py tests/unit/test_google_http.py -q passes
  - grep -rn DRIVE_MAX_FILE_BYTES src/ prints nothing
- id: p4-source-paging
  title: Cursor paging for every source operation
  depends_on: [p1-protocol, p2-client-pages, p3-drive-range]
  complexity: M
  touches:
  - src/privacyfence/plugins/source_ops.py
  - src/privacyfence/plugins/spool.py
  - tests/unit/plugins/test_source_ops.py
  - tests/unit/plugins/test_spool.py
  - tests/unit/plugins/test_sdk_samples.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/samples/sheets.get_values.json
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/samples/confluence.get_page.json
  brief: |
    1. source_ops.py per Design D3: a `bound` callable on SourceAdapter, the cursor param on every adapter, the
       page_size params with the max_results aliases, _fit_prefix, the per-operation state machines in the D3
       table, drive via DownloadSpool.read_chunk_at with the D4 cursor state, the audit summary additions, and the
       backstop size check. Use cursors.encode/decode; CursorError -> RpcError("invalid_params", str(exc)).
    2. spool.py: delete the old read_chunk wrapper and encode_cursor/decode_cursor.
    3. Update the SDK samples whose data shape D3 changes (sheets.get_values.json gains "first_row": 0;
       confluence.get_page.json gains "body_offset": 0 and "body_total_chars": <len of its body>) and
       tests/unit/plugins/test_sdk_samples.py if its shape check needs the new keys.
    3. Tests in tests/unit/plugins/test_source_ops.py: TestPaging per operation -- jira (two provider pages,
       skip within a page when the budget is small: monkeypatch SOURCE_PAGE_BUDGET_BYTES, last page null),
       calendar (same), sheets (rows split), confluence (body split; body_offset/body_total_chars always present;
       a small page has them 0 and len), drive (cursor carries revision and offset; random offset still works),
       TestCursorBinding (cursor from another query -> invalid_params), TestSingleRecordTooLarge, TestAlias
       (max_results maps to page_size; max_results above 100 for jira is invalid_params). Keep every existing
       test class passing (update expected data shapes where D3 changed them: sheets first_row, confluence
       body_offset/body_total_chars).
  acceptance:
  - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk -q passes
  - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py tests/integration/test_plugin_framework.py tests/integration/test_plugin_refusals.py -q passes
  - grep -n "encode_cursor\|decode_cursor" src/privacyfence/plugins/spool.py prints nothing
- id: p5-approvals-core
  title: Approval store, service, card builder and the pending-card frame field
  depends_on: [p1-protocol]
  complexity: M
  worker_model: opus
  worker_model_reason: Approvals must never be auto-accepted, must bind to the digest and survive restarts exactly; the card puts plugin content next to the trusted fields.
  touches:
  - src/privacyfence/plugins/approvals.py
  - src/privacyfence/approvals.py
  - src/privacyfence/dialog_window_html.py
  - src/privacyfence/resources/approval_window/styles.css
  - tests/unit/plugins/test_approvals.py
  - tests/unit/test_approvals.py
  - tests/unit/test_dialog_window_html.py
  brief: |
    1. src/privacyfence/approvals.py: add PendingApproval.frame_src: str = "" (Design D7). Nothing else.
    2. dialog_window_html.py: build_plugin_approval_html per D7, reusing _document; styles.css: pf-plugin-frame
       sizing with tokens only (tests/unit/test_design_system.py must pass).
    3. src/privacyfence/plugins/approvals.py: ApprovalRecord, ApprovalStore (D5) and ApprovalService (D6) incl.
       embed_allowed(plugin, approval_id, frame_path) (D11), the finalizer, the pending-tuple map, the audit
       callback with "<kind>; requested|approved|denied|expired|refused" statuses, and page normalization through
       plugins.pages.normalize_path. Copy the executor/finalizer structure of plugins/confirm.py; do not change
       confirm.py.
    4. Tests: tests/unit/plugins/test_approvals.py with a real PendingApprovalRegistry -- TestStore (round trip,
       corrupt file fails closed, revoke, forget_plugin, mode 0600 on POSIX), TestRequest (card shown; already
       approved -> no card; pending duplicate -> same id; unattended refused; introspection refused; page without
       pages: true refused; frame_src set only with a page), TestNeverAutoAccepted (a rule matching every
       operation leaves the card pending), TestFinalize (confirm -> stored approved; cancel -> nothing stored;
       expiry), TestCheck (approved / revoked / unknown; a different digest is unknown), TestEmbedAllowed (pending
       and matching path only; finalized -> false; other plugin -> false). tests/unit/test_dialog_window_html.py:
       fields escaped and outside the frame, frame attributes exact (sandbox="allow-scripts", no
       allow-same-origin), no frame without frame_src. tests/unit/test_approvals.py: frame_src default.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_approvals.py tests/unit/test_approvals.py tests/unit/test_dialog_window_html.py tests/unit/test_design_system.py -q passes
  - TestNeverAutoAccepted and TestFinalize pass
- id: p6-approval-frame
  title: Let exactly the approval card frame its plugin page
  depends_on: [p5-approvals-core]
  complexity: M
  worker_model: opus
  worker_model_reason: It loosens two framing defences (the card's frame-src and the plugin page's frame-ancestors) and must do so for one response each, never globally.
  touches:
  - src/privacyfence/web/csp.py
  - src/privacyfence/web/server.py
  - src/privacyfence/web/routes_approvals.py
  - src/privacyfence/web/routes_plugins.py
  - src/privacyfence/plugins/pages.py
  - tests/unit/web/test_csp.py
  - tests/unit/web/test_server.py
  - tests/unit/web/test_routes_plugins.py
  - tests/unit/web/test_routes_approvals.py
  - tests/unit/plugins/test_pages.py
  - tests/integration/test_plugin_approval_frame_browser.py
  brief: |
    1. web/csp.py per Design D8: set_frame_self/frame_self_for, set_plugin_embed/plugin_embed_for, and
       build_csp(..., frame_self=False). Default output byte-for-byte unchanged.
    2. web/server.py _SecurityHeadersMiddleware: pass frame_self; for /plugins/ paths with the embed flag use
       pages.CSP_EMBEDDED and X-Frame-Options SAMEORIGIN; nothing else changes.
    3. routes_approvals.show_approval: csp.set_frame_self(request) when card.frame_src.
    4. pages.py: CSP_EMBEDDED verbatim; PageHost gains approval_embed_allowed. routes_plugins.plugin_page: the
       pf_approval check per D8 (await host.approval_embed_allowed(...)), then csp.set_plugin_embed(request); the
       query is forwarded unchanged.
    5. Tests: test_csp.py (frame_self adds 'self' to frame-src only); test_server.py (default headers unchanged:
       TestCspNonce::test_object_src_and_frame_src_allow_data_uris and TestPluginPagesSandboxCsp still pass as they
       are; new TestPluginEmbed: flag -> CSP_EMBEDDED + SAMEORIGIN, no flag -> unchanged; TestCardFrameSelf);
       test_routes_plugins.py (pf_approval allowed -> embedded headers; not allowed / finalized / other plugin /
       other path -> normal headers; query forwarded); test_routes_approvals.py (a card with frame_src gets
       frame-src data: 'self'; one without keeps data:); test_pages.py (CSP_EMBEDDED exact string).
       tests/integration/test_plugin_approval_frame_browser.py (integration, browser; reuse the browser and server
       fixtures pattern of tests/integration/test_plugin_pages_browser.py and _watch_csp_violations from
       test_browser_smoke.py): a fake host with a pending approval whose frame_src is /plugins/echo/approval;
       open /approvals/<id> as the human: the frame loads, its document origin is "null", no CSP violation; the
       same plugin URL opened in an iframe from a page without a matching pending approval stays blocked
       (violation or empty frame).
    Stop condition: plan Risks "The card frame and the CSP".
  acceptance:
  - python3 -m pytest tests/unit/web tests/unit/plugins/test_pages.py -q passes
  - python3 -m pytest tests/integration/test_plugin_approval_frame_browser.py tests/integration/test_plugin_pages_browser.py -q reports PASSED, not SKIPPED (use PRIVACYFENCE_TEST_CHROMIUM)
- id: p7-output-policy
  title: The plugin output scope selector and its proposals
  depends_on: [p1-protocol]
  complexity: S
  worker_model: opus
  worker_model_reason: A prefix scope that matches more than the folder it names silently widens what auto-accepts.
  touches:
  - src/privacyfence/policy/scopes.py
  - src/privacyfence/policy/propose.py
  - tests/unit/policy/test_plugin_output_scope.py
  brief: |
    1. policy/scopes.py: register_plugin_output_selector / unregister_plugin_output_selector per Design D10 (the
       matches function verbatim). Leave register_plugin_selector unchanged.
    2. policy/propose.py: register_dynamic_scope_entry / unregister_dynamic_scope_entries per D10, consulted by
       proposals_for for "plugin_outputs.read" the way the framework's dynamic entries are, and
       connector_of_operation("plugin_outputs.read") == "plugin_outputs". No static entry changes.
    3. Tests tests/unit/policy/test_plugin_output_scope.py: folder prefix matches files below it and not
       "reports2/x"; exact file matches only itself; empty value never matches; other plugin never matches; the
       proposal for "reports/2026/q3.csv" is "reports/2026/" and for "q3.csv" is "q3.csv"; a rule built from the
       proposal matches its call (hypothesis, as the framework's TestProposalMatchesItsCall does) and not a
       sibling folder; unregister removes the selector and the entry.
  acceptance:
  - python3 -m pytest tests/unit/policy tests/unit/test_generate_always_allow_reference.py -q passes
- id: p8-outputs
  title: Output index, the plugin_outputs connector and its tools
  depends_on: [p1-protocol, p7-output-policy]
  complexity: M
  touches:
  - src/privacyfence/plugins/outputs.py
  - src/privacyfence/plugins/storage.py
  - tests/unit/plugins/test_outputs.py
  - tests/unit/plugins/test_storage.py
  brief: |
    1. storage.py: output_dir(name, principal) per Design D9.
    2. outputs.py per D9: OutputFile, list_outputs, read_output (UTF-8 boundary back-off; sha256 of the whole
       file; ValueError texts verbatim), and PluginOutputsConnector(Connector) with the two ToolSpecs (exact
       descriptions from D9, reason param on the read tool exactly as the connectors spell it), the list cursor via
       cursors.encode/decode, the gated_call for reads, the auto audit for lists and the extra AUDIT_PLUGIN_OUTPUT
       entry for reads, and a `register()` / `unregister()` pair that does the D9 dynamic tool registration and,
       per outputs plugin, the D10 selector and proposal entry. The constructor takes a provider
       `plugins: Callable[[], dict[str, tuple[str, Path, tuple[str, ...]]]]` mapping plugin name -> (display name,
       output root, output types) for enabled outputs plugins.
    3. Tests tests/unit/plugins/test_outputs.py: TestVisibility (dot files, .tmp, symlinks, wrong extension,
       depth, outside root all hidden), TestList (sorting, prefix, cursor paging at OUTPUT_LIST_PAGE), TestRead
       (paging, UTF-8 boundary, sha256, offset past end), TestTools (list is auto and audited; read goes through
       gated_call with metadata-only preview and details_text = text, using the gated_call_spy pattern; unknown
       plugin message), TestRuleAllowsFolder (with real policy rules via auto_accept.add_policy_v2_rules: a read
       under the folder is released without a card, outside it shows one), TestRegistration (register/unregister
       restore the tables), and assert_all_tools_leave_an_audit_trail over the connector.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_outputs.py tests/unit/plugins/test_storage.py -q passes
  - coverage of outputs.py >= 95% (python3 -m pytest tests/unit/plugins --cov=src/privacyfence/plugins --cov-branch)
- id: p9-host-cr
  title: Host wiring for approvals, outputs and output directories
  depends_on: [p4-source-paging, p5-approvals-core, p6-approval-frame, p8-outputs]
  complexity: M
  touches:
  - src/privacyfence/plugins/host.py
  - src/privacyfence/audit_log.py
  - tests/unit/plugins/test_host.py
  brief: |
    1. host.py per Design D11: ApprovalService construction sharing the confirm executor, _audit_approval, the
       three approval handlers (+ "refused" audit), approval_embed_allowed, revoke_approval, output_dir in
       _initialize_params and output fields in the inspect summary, forget_plugin in purge and _uninstall with the
       audit line, rows() approvals and outputs fields, and the outputs connector in connectors() with its
       register/unregister and _notify_tools on appearance/disappearance.
    2. audit_log.py: comment-only, document "plugin_approval" and "plugin_output" decisions next to the other
       plugin_* entries (what connector/tool/summary hold), and add "removed; data and rules deleted" and
       "approvals deleted: <n>" to the plugin_lifecycle list.
    3. Tests in tests/unit/plugins/test_host.py (reuse Env/FakeAudit): TestApprovalRouted (request -> card;
       approve -> check approved; revoke -> notification sent and check revoked), TestApprovalEmbedAllowed,
       TestPurgeForgetsApprovals, TestUninstallForgetsApprovals, TestOutputDir (created 0700 only with outputs:
       true; passed in initialize), TestOutputsConnector (appears when an outputs plugin is enabled, disappears
       when disabled, tools-changed listener fired both times), TestRowsApprovals.
  acceptance:
  - python3 -m pytest tests/unit/plugins -q passes
  - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_plugin_refusals.py tests/integration/test_sdk_testhost_conformance.py -q passes
- id: p10-settings-cr
  title: Settings approvals list with Revoke, and outputs on the review dialog
  depends_on: [p9-host-cr]
  complexity: M
  touches:
  - src/privacyfence/settings_controller.py
  - src/privacyfence/web/routes_settings.py
  - src/privacyfence/web/org_settings_scope.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_controller.py
  - tests/unit/web/test_routes_settings.py
  - tests/unit/web/test_org_settings_scope.py
  - tests/unit/test_settings_window_html.py
  - tests/integration/test_plugin_settings_browser.py
  brief: |
    1. Design D12: revoke_plugin_approval in SettingsController (via _submit_plugin), _NON_SENSITIVE_ACTIONS,
       ACTION_SCOPES (local only); the Approvals block in renderPluginRow with Revoke; "Publishes output files:
       JSON, CSV" (labels from the review's output_types: application/json JSON, text/csv CSV, text/html HTML,
       text/plain text, text/markdown Markdown) in renderPluginDialog.
    2. Tests: TestSensitiveActionsCoverAllAllowedActions still passes; revoke is not sensitive; controller submits
       to the host; the window HTML renders the approvals list and the Revoke action payload; a TestPhoneLayout
       case for a row with three approvals in tests/integration/test_plugin_settings_browser.py.
  acceptance:
  - python3 -m pytest tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/web/test_org_settings_scope.py tests/unit/test_settings_window_html.py tests/unit/test_design_system.py -q passes
  - python3 -m pytest tests/integration/test_plugin_settings_browser.py -q reports PASSED, not SKIPPED
- id: p11-sdk-cr
  title: SDK paging iterator, approvals and outputs clients
  depends_on: [p1-protocol]
  complexity: M
  touches:
  - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
  - plugin-sdk/src/privacyfence_plugin_sdk/responses.py
  - plugin-sdk/src/privacyfence_plugin_sdk/__init__.py
  - plugin-sdk/README.md
  - tests/unit/plugin_sdk/test_plugin.py
  - tests/unit/plugin_sdk/conftest.py
  brief: |
    1. plugin.py per Design D13: Principal.output_dir (from PrincipalContext.output_dir), "approval.revoked" in
       _EVENTS, ctx.source.pages / collect, ctx.approvals (request/check/await_/digest), ctx.outputs
       (publish/dir) with Plugin(..., output_types=...), and the error texts given there. responses.py:
       ApprovalTicket. __init__.py: export it.
    2. README.md: sections on paging, approvals (including that an approval page receives pf_approval in its
       query), outputs, and child processes (Design D14's rules). Link the schema JSON and the issue URL; never a
       docs-slash-name-dot-md path.
    3. Tests in tests/unit/plugin_sdk/test_plugin.py with the existing FakeDaemon: TestPages (iterates until null;
       passes cursor; collect concatenates; collect refuses sheets), TestApprovals (digest of str and bytes;
       request/check/await wire messages), TestOutputs (atomic publish leaves no .tmp; existing path refused;
       ".." refused; wrong extension refused; no output_dir -> RuntimeError), TestRevokedEvent.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk -q passes
  - python3 -m pytest tests/unit/test_docs_references_exist.py tests/unit/test_code_no_history.py -q passes
- id: p12-sdk-testhost-cr
  title: Test host paging fixtures, approvals, outputs and samples
  depends_on: [p11-sdk-cr, p4-source-paging]
  complexity: M
  touches:
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/**
  - tests/unit/plugin_sdk/test_testhost.py
  - tests/unit/plugin_sdk/test_testhost_surfaces.py
  - tests/unit/plugins/test_sdk_samples.py
  brief: |
    1. testing/ per Design D13 "Test host": returns_pages, the cursor envelope (private copy of cursors.encode/
       decode), drive cursors in the D3/D4 shape, DRIVE_MAX_FILE_BYTES removed, _approvals.py with
       decide_approval/revoke_approval and check semantics, outputs support (PluginTestHost(..., outputs=True,
       output_types=...), host.output_dir, host.list_outputs), "approval.revoked" in _EVENT_NAMES, and the two
       new page-2 samples with real envelopes in the first samples' next_cursor.
    2. Tests: test_testhost.py and test_testhost_surfaces.py additions for each; tests/unit/plugins/test_sdk_samples.py
       checks the page-2 samples against the adapters' shapes and that the first sample's cursor decodes with
       cursors.decode for the sample's own params.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk tests/unit/plugins/test_sdk_samples.py -q passes
- id: p13-e2e-cr
  title: Echo additions and end-to-end tests for approvals, outputs and paging
  depends_on: [p9-host-cr, p10-settings-cr, p12-sdk-testhost-cr]
  complexity: M
  touches:
  - tests/fixtures/plugins/echo/**
  - tests/integration/test_plugin_approvals.py
  - tests/integration/test_plugin_outputs.py
  - tests/integration/test_plugin_paging.py
  - tests/integration/test_sdk_testhost_conformance.py
  brief: |
    1. echo per Design D15 (tools approve and publish, pages /approval, /approval-check, /pages, the
       approval.revoked recorder, manifest outputs keys). Keep every existing echo tool and page working.
    2. tests/integration/test_plugin_approvals.py (reuse harness.Stack and mcp_session): request -> card with
       frame_src; approve via stack.registry.answer(id, "confirm") -> /approval-check reports approved; same
       content again -> no new card; changed content -> new card; revoke via host.revoke_approval -> echo's
       recorder saw approval.revoked and check reports revoked; restart the plugin -> still revoked/approved as
       stored; refused while unattended; never auto-accepted with an allow-everything rule.
    3. tests/integration/test_plugin_outputs.py: echo_publish writes reports/x.csv; plugin_outputs_list shows it
       (no card, audited); plugin_outputs_read shows a card (Popups), releases the text, offsets page a file
       larger than OUTPUT_READ_PAGE_BYTES; a folder rule "reports/" then releases without a card and
       "other/" still shows one; disabling echo removes the plugin_outputs tools (tools/list_changed).
    4. tests/integration/test_plugin_paging.py: through echo's /pages page with stack's fake calendar (two provider
       pages) and a fake Jira on the Stack (add a FakeJira to harness.py next to FakeDrive), every item arrives
       once; a Drive binary file of 100 MiB fake size (FakeDrive.download_range) streams to the end with no
       full download.
    5. tests/integration/test_sdk_testhost_conformance.py: add TestSamePaging (returns_pages vs the daemon's
       calendar paging: same item counts and same number of calls) and TestSameApprovals (request/decide/check
       statuses equal).
  acceptance:
  - python3 -m pytest tests/integration/test_plugin_approvals.py tests/integration/test_plugin_outputs.py tests/integration/test_plugin_paging.py tests/integration/test_sdk_testhost_conformance.py tests/integration/test_plugin_framework.py -q passes
- id: p14-today-cr
  title: today example export and layout approval
  depends_on: [p11-sdk-cr, p9-host-cr]
  complexity: S
  touches:
  - examples/plugins/today/**
  - tests/unit/examples/**
  - tests/fixtures/plugins/today/**
  brief: |
    1. examples/plugins/today per Design D15: today_export, today_approve_layout, the page note, the manifest keys,
       README steps 13-16 exactly matching the checklist page's ma1 steps (approve the layout with the embedded
       page; Settings shows it under Approvals; revoke and see the page note change; export and read it through
       plugin_outputs_list/read and add a folder rule). Self-contained page, named or rgb() colours only.
    2. Tests in tests/unit/examples/test_today_plugin.py with PluginTestHost(outputs=True,
       output_types=("text/csv",)): export publishes exports/<date>.csv; approve_layout requests with page
       "/approval"; the page reflects check status; approval.revoked handled.
  acceptance:
  - python3 -m pytest tests/unit/examples -q passes
  - python3 scripts/build_example_plugin.py --help exits 0
- id: p15-retire
  title: ADRs, reference docs, changelog, retire the plan
  depends_on: [p0-sync, p1-protocol, p2-client-pages, p3-drive-range, p4-source-paging, p5-approvals-core, p6-approval-frame, p7-output-policy, p8-outputs, p9-host-cr, p10-settings-cr, p11-sdk-cr, p12-sdk-testhost-cr, p13-e2e-cr, p14-today-cr]
  complexity: M
  touches:
  - docs/adr/**
  - docs/plugin*.md
  - docs/README.md
  - docs/approvals-and-policy.md
  - docs/security-and-compliance.md
  - docs/configuration-reference.md
  - CHANGELOG.md
  - docs/plugin-framework-cr-plan-manual-steps.html
  - scripts/build_site.py
  brief: |
    1. ADRs: the five in the plan's ADRs section, numbered from the next free number (ls docs/adr), template from
       docs/adr/README.md, Status "Accepted — <today>. Implemented.", "Amends NNNN" lines where stated (and one
       Status line added to each amended ADR pointing forward, the only edit allowed to an accepted ADR), linking
       the issue comment URL and source files, never the plan. Add all five to docs/adr/README.md's index. If the
       numbers differ from 0127-0131, fix every citation (grep -rn "ADR 01[23]" src plugin-sdk tests examples).
    2. The plugins reference doc (plugins.md in docs/): approvals in Settings and Revoke, outputs (folder, what is
       published, the two tools, folder rules), child processes (Design D14), large downloads. The protocol
       reference doc (plugin-protocol.md in docs/): protocol 1.1, the approval methods and notification, cursor
       paging per operation (the D3 table, including the Salesforce exception with the issue link), Drive Range,
       manifest outputs keys, PrincipalContext.output_dir, the embedding rule. approvals-and-policy.md: plugin
       approvals never auto-accepted, the plugin:<name>:output folder scope. security-and-compliance.md: the card
       frame (what is loosened, for which response), outputs as a read path, child processes.
    3. CHANGELOG.md: [Unreleased] lines for approvals, paging, outputs and child processes.
    4. Delete the plan and its manual-steps HTML; remove the plan's entry from docs/README.md and from
       scripts/build_site.py's CONTRIBUTOR_DOCS; grep the repo for "plugin-framework-cr-plan" and remove every
       reference.
  acceptance:
  - test ! -e docs/plugin-framework-cr-plan.md && test ! -e docs/plugin-framework-cr-plan-manual-steps.html
  - grep -rn "plugin-framework-cr-plan" . --exclude-dir=.git prints nothing
  - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_allowlist.py tests/unit/test_code_no_history.py tests/unit/test_changelog_section.py tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py -q passes
```
