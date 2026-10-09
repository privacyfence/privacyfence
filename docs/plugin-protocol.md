# Plugin protocol reference

How PrivacyFence and a plugin talk to each other: the transport, the manifest, every message with
its data shapes, the limits and the timeouts. This is protocol version `1.1.0`. For installing and
running a plugin, see [`plugins.md`](plugins.md); for writing one, the
[`privacyfence-plugin-sdk`](https://github.com/privacyfence/privacyfence/tree/main/plugin-sdk) does
the protocol for you. The machine-readable description of every message is
[`plugin-protocol/protocol.schema.json`](plugin-protocol/protocol.schema.json) (JSON Schema
2020-12), and the reasons behind the design are in
[ADR 0120](adr/0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md),
[ADR 0121](adr/0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md),
[ADR 0122](adr/0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md),
[ADR 0123](adr/0123-the-plugin-source-api-is-ungated-but-audited-without-content.md),
[ADR 0124](adr/0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md),
[ADR 0125](adr/0125-the-org-mode-plugin-contract-is-reserved-in-protocol-1-and-rejected-in-local-mode.md)
[ADR 0126](adr/0126-the-plugin-sdk-lives-in-this-repository-and-is-published-from-the-same-tag.md),
[ADR 0127](adr/0127-a-plugin-approval-binds-to-its-content-digest-and-persists-until-revoked.md),
[ADR 0128](adr/0128-plugin-source-reads-never-truncate.md),
[ADR 0129](adr/0129-drive-binary-downloads-for-plugins-are-http-range-reads-with-no-size-cap.md),
[ADR 0130](adr/0130-plugin-outputs-are-a-folder-that-privacyfence-reads-through-its-own-tools.md)
and [ADR 0131](adr/0131-a-plugins-child-processes-run-under-its-account-unsupervised.md).

## Transport

PrivacyFence starts the plugin as a child process and exchanges JSON-RPC 2.0 messages over its
stdin and stdout.

- **Framing.** One JSON object per line, UTF-8, with no embedded newline, at most 16 MiB (16,777,216
  bytes) including the final `\n`. Stdout carries protocol messages only. Stderr goes to the
  plugin's log.
- **Batches.** A JSON array is answered with `invalid_request` and not processed.
- **Ids.** Each side numbers its own requests with integers or strings. The two id spaces are
  independent.
- **Notifications** have no `id` and get no response. An unknown notification is ignored. An
  unknown request gets `method_not_found`.
- **In flight.** At most 16 requests in each direction. A sender waits for a free slot; a receiver
  that is already handling 16 answers the next `invalid_request` ("too many requests in flight").
  The daemon counts the notifications it is still handling against the same 16, and drops a
  notification that arrives while all of them are busy.
- **Bad lines.** Three consecutive lines that are not valid JSON-RPC (bad JSON, over the size cap,
  or not an object) close the connection, and that counts as a crash. A line over the size cap is
  discarded up to its newline and counts once, however many pieces it arrives in.
- **Non-finite numbers.** `NaN`, `Infinity` and `-Infinity` are not JSON: neither side sends them,
  and a line that carries one is a parse error.
- **Unknown fields** are ignored by both sides, so a minor version can add fields. The exceptions
  are the organization-mode fields (see [Organization-mode fields](#organization-mode-fields)).
- **Environment.** The plugin runs with an allow-listed environment: `PATH`, `SYSTEMROOT`,
  `WINDIR`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LC_ALL`, `LC_CTYPE` and `TZ` (those that are set),
  plus `PRIVACYFENCE_PLUGIN=1`. Nothing else of the daemon's environment is passed on. A packaged
  plugin must therefore not depend on `PYTHONPATH` or any other variable. The working directory is
  the plugin's own directory.
- **Processes.** On macOS and Linux the plugin runs in a process group of its own, and the daemon
  kills that group whenever the plugin stops or exits, so nothing the plugin started outlives it.
  On Windows the daemon terminates the plugin's own process only. A plugin may start child processes;
  PrivacyFence does not supervise them (see [`plugins.md`](plugins.md#child-processes)).
- **Every start is checked.** Before each start, restarts after a crash included, the daemon checks
  again that only an administrator can change the plugin and that its executable and manifest
  still hash to what was reviewed. A plugin that fails is disabled with "executable is writable by
  non-administrators" or "executable or manifest changed, enable again", and is not restarted.

## Versioning

`protocol_version` is semver. The manifest's `protocol` is the major version only, as a string
(`"1"`). The daemon and the plugin must share the major version, or the plugin is not started and
Settings shows "protocol major mismatch". The effective version is the lower minor of the two.

Version `1.1` adds, without changing any `1.0` message: the approval methods and the
`approval.revoked` notification, cursor paging for every source operation, Drive Range reads, the
manifest keys `outputs` and `output_types`, and `PrincipalContext.output_dir` and `output_types`. A
`1.0` plugin keeps working. The daemon sends `approval.revoked` only to the running plugin that
requested the revoked approval, and `output_dir` and `output_types` only to a plugin with
`outputs: true`.

## Errors

A failed request is answered with a JSON-RPC error whose `data` carries the stable name:

```json
{"code": -32005, "message": "upstream_error",
 "data": {"code": "upstream_error", "detail": "the service returned an error", "retryable": false}}
```

`detail` never contains connector content or user content. Some errors add keys to `data`
(`reason`, below).

| Name | Code | Used for |
|---|---|---|
| `parse_error` | -32700 | A line that is not JSON |
| `invalid_request` | -32600 | A batch, a message that is not a request, too many in flight |
| `method_not_found` | -32601 | An unknown request method |
| `invalid_params` | -32602 | Parameters that fail validation |
| `internal_error` | -32603 | A handler failed, or the peer closed |
| `operation_not_allowed` | -32001 | A source operation outside the manifest's list |
| `connector_unavailable` | -32002 | The service is not connected (`data.reason`: `disabled`, `not_authenticated` or `unavailable`) |
| `unknown_principal` | -32003 | A principal other than `local` |
| `payload_too_large` | -32004 | A result over its limit, or one record larger than a page |
| `upstream_error` | -32005 | The service answered with an error (`data.reason` is `revision_changed` for a Drive file that changed) |
| `org_only_field` | -32006 | An organization-mode field in local mode |
| `confirmation_refused` | -32007 | A confirmation or approval refused (`data.reason` is `unattended_session` or `too_many_pending`) |
| `unknown_tool` | -32008 | A tool name the plugin does not have |
| `invalid_blocks` | -32009 | A block list that fails validation |
| `version_mismatch` | -32010 | The protocol major differs |
| `unknown_call` | -32011 | `tool.execute` for a call the plugin does not hold |
| `digest_mismatch` | -32012 | `tool.execute` whose arguments differ from the prepared ones |
| `timeout` | -32013 | No answer within the method's timeout |
| `introspection_only` | -32014 | A request that is refused while the plugin is being inspected |

## Manifest

Next to its executable a plugin ships `privacyfence-plugin.yaml`, loaded with `yaml.safe_load`
(at most 64 KiB). An unknown key is an error.

```yaml
name: today                      # 2-31 characters, [a-z][a-z0-9-], equals the directory name
display_name: Today              # 1-60 characters, no control or bidirectional characters, no line breaks or tabs
version: 1.2.0                   # MAJOR.MINOR.PATCH, optionally with -prerelease
protocol: "1"                    # the major version, as a string
command: ["today-plugin"]        # a non-empty list of strings
source_operations:               # optional, default []
  - calendar.list_events
tools: dynamic                   # required; the only value
max_gate_floor: auto             # optional: "review" (default) or "auto"
pages: true                      # optional, default false
service_credentials: false       # optional, default false; true is an error in local mode
outputs: true                    # optional, default false: the plugin publishes files (see Outputs)
output_types: [text/csv]         # optional, only with outputs: true; default [application/json, text/csv]
```

- `name` must not be reserved: `privacyfence`, `plugin`, `plugins`, `settings`, `mcp`, and the names
  of the connectors (`gmail`, `drive`, `contacts`, `calendar`, `tasks`, `apps_script`, `slack`,
  `jira`, `confluence`, `salesforce`, `telegram`), plus `apps`, `sheets` and `docs`, which built-in
  tools or services start with.
- `display_name` must not contain line breaks or tabs.
- `command[0]` is resolved inside the plugin's directory and must stay inside it; a symlink that
  points out is refused, and so is a command that resolves to the directory itself. On Windows,
  `.exe` is appended when it has no suffix and nothing by that exact name exists.
- `source_operations` may list only the six operations under [Source calls](#source-calls).
- `max_gate_floor: auto` lets the plugin declare tools on the `auto` gate (see
  [Tool definitions](#tool-definitions)). The owner sees the floor when enabling the plugin.
- `outputs: true` gives the plugin an output folder ([Outputs](#outputs)). `output_types` lists the
  media types it may publish, each one of `application/json` (`.json`), `text/csv` (`.csv`),
  `text/html` (`.html`, `.htm`), `text/plain` (`.txt`) and `text/markdown` (`.md`); another value is
  an error, and so is `output_types` without `outputs: true`. Settings shows both when the owner
  reviews the plugin.

## Messages

Direction "D to P" is daemon to plugin. Every request the daemon sends on behalf of a user carries
a `principal`; every request a plugin sends names a principal by id. In local mode the only
principal is `local`.

| Method | Direction | Kind | Purpose |
|---|---|---|---|
| `initialize` | D to P | request | The handshake |
| `tools.changed` | P to D | notification | The plugin's tool list changed |
| `tool.prepare` | D to P | request | Describe what a call would release or do |
| `tool.execute` | D to P | request | Run an approved call |
| `source.call` | P to D | request | Read from a connected service |
| `confirm.request` | P to D | request | Ask a human to confirm something |
| `confirm.await` | P to D | request | Wait for that answer |
| `approval.request` | P to D | request | Ask a human to approve a thing that stays approved |
| `approval.check` | P to D | request | Ask whether a thing is approved |
| `approval.await` | P to D | request | Wait for that answer |
| `approval.revoked` | D to P | notification | A human revoked an approval |
| `web.request` | D to P | request | A page request |
| `storage.purge` | D to P | request | Release and delete the plugin's data |
| `connector.state_changed` | D to P | notification | A connector was enabled, disabled, signed in or out |
| `principal.removed` | D to P | notification | Reserved; local mode never sends it |
| `plugin.disabling` | D to P | notification | The plugin is about to be stopped |
| `shutdown` | D to P | notification | Exit within the grace period |

### `initialize`

Parameters:

```json
{"protocol_version": "1.1.0", "purpose": "run", "mode": "local",
 "daemon": {"name": "privacyfence", "version": "5.6.0"},
 "plugin": {"name": "today", "manifest_version": "1.2.0"},
 "data_dir": "/var/lib/privacyfence/plugin-data/today/shared",
 "principals": [{"id": "local", "display_name": "…", "storage_dir": "/var/lib/privacyfence/plugin-data/today/user"}],
 "limits": {"max_line_bytes": 16777216, "max_in_flight": 16, "inline_result_bytes": 100000}}
```

`purpose` is `run` for a normal start and `introspect` when the owner is reviewing the plugin (see
[Inspection](#inspection)). `data_dir` is the plugin's install-wide directory and each principal's
`storage_dir` its per-principal one; both exist and are private to the service account. For a plugin
with `outputs: true` each principal also carries `output_dir` (its output folder, created with mode
`0700` before `initialize`) and `output_types` (the media types from the manifest, so the plugin never
declares them twice); neither is sent to any other plugin.

Result:

```json
{"protocol_version": "1.1.0",
 "plugin": {"name": "today", "version": "1.2.0"},
 "scope_types": [{"name": "calendar", "description": "Calendar id a call reads"}],
 "tools": [ToolDef, …]}
```

The daemon checks, in order, and does not restart the plugin when one fails:

1. The protocol major matches ("protocol major mismatch").
2. `plugin.name` and `plugin.version` equal the manifest's ("manifest invalid: name or version
   differs from the plugin's own").
3. The tool definitions are valid (below), and when the plugin has been enabled, every tool is in
   the reviewed set.

At most 20 scope types may be declared.

### Tool definitions

A `ToolDef` is:

| Field | Rule |
|---|---|
| `name` | `[a-z][a-z0-9_]{1,40}`, unique in the list |
| `description` | 1 to 1024 characters |
| `parameters` | A JSON Schema object (`"type": "object"`) |
| `read_only` | Boolean |
| `destructive` | Boolean; a destructive tool is not read-only and must use the `popup` gate |
| `gate` | `auto`, `review` or `popup` |
| `scopes` | The scope types a call returns values for; each must be declared in `scope_types` |
| `effect` | Optional, at most 200 characters: the sentence a card shows for what approving does, per tool |
| `title` | Optional, at most 120 characters: the human name on cards; the default is the tool name with spaces and a capital |

The whole list is refused on the first violation, at start and in `tools.changed` alike:

- At most 64 tools.
- The MCP tool name is `<plugin>_<tool>` and at most 64 characters, and must not equal a built-in
  tool's name.
- `parameters.properties` holds scalars only: each property's `type` is `string`, `integer`,
  `number` or `boolean`. Arrays, objects, `enum`, `oneOf` and nested schemas are refused, and so is
  a property named `reason`. Put fixed choices in the description, and take structured input as a
  JSON string. `required` lists property names.
- A tool on the `auto` gate, a read as much as a write, needs the manifest's `max_gate_floor: auto`.
- Gated tools (`review`, `popup`) get a required `reason` parameter added for the AI client, which
  the plugin never receives in `args`.
- After the plugin is enabled, a list may only drop tools: every tool must match a reviewed
  signature (name, gate, read-only, destructive and scopes). A `tools.changed` that adds or changes
  one is refused, the previous list stays in force, and Settings shows "last tools change
  rejected: …". A tool list at start that is outside the reviewed set disables the plugin with
  "executable or manifest changed, enable again".

A scope type's `description` is 1 to 500 characters, and the scope type name `output` is reserved.

The daemon exposes the tools to AI clients under the MCP name `<plugin>_<tool>`.

### `tools.changed`

`{"tools": [ToolDef, …]}`, validated as a whole. An accepted change replaces the list and the
daemon tells connected clients that the tool list changed. Accepted and rejected changes are
audited (see [Audit entries](#audit-entries)).

### `tool.prepare`

Parameters: `call_id`, `principal`, `tool` (the plugin's own tool name), `args` (without `reason`)
and `reason` (the AI client's sentence, or `null`).

Result:

```json
{"preview": [Block, …], "payload": [Block, …], "scopes": {"calendar": ["primary"]}}
```

`prepare` has no side effects. `payload` is required for a read-only tool and forbidden for any
other. `scopes` has an entry of 1 to 100 values (each at most 200 characters) for every scope type
the tool declares; a missing or empty one fails the call. The `preview` is limited to 64 KiB and 50
blocks; the JSON of `{"blocks": payload}` must be at most 100,000 bytes.

The daemon shows `preview` followed by `payload` on the approval card, applies the gate, and only
then calls `tool.execute`. An identical call is answered from the daemon's 30 second result cache.
A card's decision belongs to the `call_id` it showed: a prepared call is reused while its card is
pending (up to 15 minutes) and the decision ledger's five-minute replay window after that, and, for
a decided read, for one more replay window, so a repeat call gets the same payload without running
`prepare` again. A call the daemon prepares afresh, including after the plugin restarts, always gets
its own card. `tool.execute` can therefore name a `call_id` prepared more than 15 minutes earlier,
and a read's `call_id` more than once. A plugin that no longer holds the call answers
`unknown_call`; for a read the daemon still returns the prepared payload.

When `tool.prepare` fails, the plugin's own error detail never reaches the AI client. The connector
turns the error into one fixed sentence, which the daemon logs: `connector_unavailable` gives "A
service this plugin reads from is not connected.", `upstream_error` gives "A service this plugin
reads from returned an error.", `payload_too_large` gives "The plugin's result is too large to
return.", `timeout` gives "The plugin did not answer in time.", and any other error gives "The
plugin could not prepare this call." The test host returns the same sentence in
`outcome.error["detail"]`.

### `tool.execute`

Parameters: `call_id`, `principal`, `tool`, `args`, `args_digest` and `approval`:

```json
{"approval_id": "card-…", "decision": "approved", "via": "card", "decided_at": "2026-10-07T10:00:00Z"}
```

`args_digest` is `"sha256:"` followed by the hex SHA-256 of `json.dumps(args, sort_keys=True,
separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8. A plugin that does not hold the call
answers `unknown_call`, and one whose arguments differ from the prepared ones answers
`digest_mismatch`. `via` is `auto` for an `auto` tool and `card` for a gated one, including a call
a saved rule accepted.

Result: `{"result": <any>, "approval_id": "<optional>"}`.

- For a **read-only** tool the daemon ignores `result` and an error, and returns the prepared
  payload as `{"blocks": payload}`. A plugin cannot release anything the card did not show.
- For any other tool `result` is returned to the AI client, at most 100,000 bytes serialized. When
  `approval_id` is present it is added to the result, so the client can call
  `privacyfence_await_approval` with it. A write that was approved runs once and is never retried.

### Blocks

A block is data, never markup. Every string is stripped of control characters (except newline and
tab) and of the bidirectional-override characters, and PrivacyFence escapes it when it renders.
Any invalid block fails the whole list with `invalid_blocks`. A table cell is cut at 4,096
characters with a trailing "…".

| Type | Fields (any other is an error) |
|---|---|
| `heading` | `text`; optional `level` 2 or 3 |
| `fields` | `items`: 1 to 50 of `{label, value}`, both strings |
| `table` | `columns`: 1 to 20 of `{key, label}` with unique keys that match `[A-Za-z0-9_.-]{1,64}` (a key is never shown on the card); `rows`: objects that use declared keys only, with string, number, boolean or null values |
| `text` | `text` |
| `code` | `text`; optional `language` matching `[a-z0-9+#-]{1,20}` |
| `diff` | `format` (`"unified"`) and `text`; lines starting `+`, `-` and `@@` are marked on the card |

### Source calls

`source.call` reads from a connected service with the connector's own client, under the local
principal, with no approval card and without the plugin ever holding a token. Parameters:
`principal` (`local`), `operation`, `params` and, in organization mode only, `credential`.

Result:

```json
{"operation": "calendar.list_events", "data": …, "bytes": 1234, "next_cursor": null}
```

The checks run in this order, and the first failure is the error: the plugin is being inspected
(`introspection_only`); the parameters parse (`invalid_params`, `org_only_field`); the principal is
`local` (`unknown_principal`); the operation is one of the six and in the manifest's
`source_operations` (`operation_not_allowed`); the connector is enabled and signed in
(`connector_unavailable`); the cursor, when given, belongs to this call (`invalid_params`); the call
runs (`upstream_error` with the fixed detail "the service returned an error"); the result is a
whole result or a page that fits (`payload_too_large` only for a single record larger than a page,
or a Salesforce report over 12 MiB). Every outcome
writes one audit entry that holds the targets and the byte count, never the data.

| Operation | Parameters | `data` |
|---|---|---|
| `salesforce.report_run` | `report_id` (required); `filters`: a list of `{column, operator, value}`; `columns`: 1 to 100 column names to narrow the run; `page_by`: a report column whose values are unique per row, to read every row; `cursor` (needs `page_by`) | Salesforce's raw report JSON, `allData` included; with `page_by` it gains `page`: `{number, first_row, last_row, more}` |
| `jira.search` | `jql` (required); `page_size` 1 to 100, default 100; `max_results` 1 to 500 (an alias, used when `page_size` is absent and clamped to 100); `cursor` | The matching issues, as a list of objects |
| `drive.download` | `file_id` (required); `length` 1 to 8,388,608 (default the maximum); `offset` (at least 0) or `cursor`, not both | `{file_id, mime_type, revision, total_size_bytes, offset, length, eof, content_base64}` |
| `sheets.get_values` | `spreadsheet_id` and `range` (required); `value_render_option`: `FORMATTED_VALUE` (default), `UNFORMATTED_VALUE` or `FORMULA`; `cursor` | `{"values": [[…], …], "first_row": n}`, Google's raw values array for rows `first_row` onward |
| `confluence.get_page` | `page_id` (required); `cursor` | The page as an object; `body` is Confluence storage-format XHTML, a slice of it when paged, with `body_offset` and `body_total_chars` always present |
| `calendar.list_events` | `calendar_id` (default `primary`); `time_min` and `time_max` (RFC 3339, required); `page_size` 1 to 250, default 250; `max_results` (an alias, as for Jira); `cursor` | The events, as a list of objects |

#### Record shapes

`data` for `jira.search`, `calendar.list_events` and `confluence.get_page` holds these objects. Type is the
field's type as JSON.

A Jira issue:

| Field | Type | Notes |
|---|---|---|
| `key` | string | |
| `summary` | string | |
| `status` | string | |
| `issue_type` | string | |
| `priority` | string | |
| `assignee` | string | |
| `reporter` | string | |
| `description` | string | |
| `labels` | list of strings | |
| `created` | string | |
| `updated` | string | |
| `url` | string | |

A Calendar event:

| Field | Type | Notes |
|---|---|---|
| `id` | string | |
| `calendar_id` | string | |
| `title` | string | |
| `description` | string | |
| `start_time` | string | ISO 8601 |
| `end_time` | string | ISO 8601 |
| `all_day` | boolean | |
| `organizer_email` | string | |
| `attendees` | list of objects | See the attendee table. |
| `location` | string | |
| `hangout_link` | string | |
| `conference_link` | string | |
| `status` | string | "confirmed", "tentative" or "cancelled" |
| `html_link` | string | |
| `attachments` | list of objects | See the attachment table. |
| `visibility` | string | "default", "public", "private" or "confidential" |
| `color_id` | string | "1" to "11", or "" for the calendar's default color |
| `recurrence` | list of strings | Raw RRULE, EXDATE, RDATE and EXRULE lines; non-empty only on a series' own master event, empty otherwise (including on individual expanded instances, which carry `recurring_event_id` instead) |
| `recurring_event_id` | string | Non-empty if and only if this is one expanded instance of a recurring series: the id of that series' master event (a distinct id from this instance's own) |
| `original_start_time` | string | This instance's originally-scheduled start (ISO 8601 or date), before any per-instance reschedule; non-empty only on a recurring instance |

A Calendar attendee:

| Field | Type | Notes |
|---|---|---|
| `email` | string | |
| `display_name` | string | |
| `response_status` | string | "accepted", "declined", "tentative" or "needsAction" |
| `organizer` | boolean | |

A Calendar attachment:

| Field | Type | Notes |
|---|---|---|
| `file_id` | string | A Drive file id |
| `title` | string | |
| `mime_type` | string | |
| `file_url` | string | |
| `icon_link` | string | |

A Confluence page:

| Field | Type | Notes |
|---|---|---|
| `id` | string | |
| `title` | string | |
| `space_key` | string | |
| `space_name` | string | |
| `version` | integer | |
| `author` | string | |
| `created` | string | |
| `updated` | string | |
| `body` | string | |
| `url` | string | |
| `author_name` | string | |
| `mentions` | object of strings | |
| `body_offset` | integer | Where `body` starts in the whole body |
| `body_total_chars` | integer | The length of the whole body |

### Paging

Every source operation either returns all of its data or a page with a
`next_cursor`. A size limit is a page size, not a failure, and `next_cursor` is `null` exactly when
nothing is left.

- **The cursor is opaque** (at most 4,096 characters). It is bound to the operation and to the
  parameters it was issued for: `jql` and `page_size` for Jira; `calendar_id`, `time_min`, `time_max`
  and `page_size` for Calendar; `spreadsheet_id`, `range` and `value_render_option` for Sheets;
  `page_id` for Confluence; `file_id` for Drive; `report_id`, `page_by`, `columns` and `filters` for Salesforce. Pass it back with the same other parameters. A
  cursor that is malformed, was issued for another operation or other parameters, or whose position
  does not fit is `invalid_params`. A cursor is neither secret nor signed: binding it protects a
  plugin from carrying it to the wrong query, nothing more.
- **A page is as large as fits** in the `source.call` result limit minus room for the envelope. A
  single record larger than that is `payload_too_large`.

| Operation | What a page is |
|---|---|
| `jira.search` | Issues from one provider page (the provider's own page token), cut to fit; the cursor continues inside a provider page or moves to the next |
| `calendar.list_events` | The same, with the provider's page token |
| `sheets.get_values` | A run of rows from `first_row`; the next cursor starts after the last row returned |
| `confluence.get_page` | The page with `body` cut to a slice that fits; `body_offset` and `body_total_chars` say where the slice sits |
| `drive.download` | A byte range of the file, up to `length` (8 MiB at most) |
| `salesforce.report_run` | One report run sorted by `page_by`, starting after the previous page's last value, cut to fit; grouped reports are read as one table. A column that is not unique, does not advance or is not a column of the run is `invalid_params` with `reason` `bad_page_by`, `not_unique`, `not_advancing` or `not_flat`; a read that would lose rows (RowCount check) is `invalid_params` with `reason` `rows_lost`; more than `salesforce.report_max_pages` runs is `invalid_params` with `reason` `page_limit`. Without `page_by` it is one run as saved, not paged |

The audit entry's targets gain `; page` when a cursor was given, and the summary gains `; more` when
`next_cursor` is set.

**Drive.** A binary file is read with HTTP Range requests, one per call, with no limit on the file's
size. Each call re-reads the file's modified time (`revision`); a changed revision, or a chunk
shorter than asked, fails with `upstream_error` and `data.reason` `revision_changed`. An offset past
the end of the file is `invalid_params`. A Google-native file (Docs and Slides exported as text,
Sheets as CSV) cannot be ranged: it is exported whole to a private spool file, with no size cap of
PrivacyFence's own, served in chunks of at most 8 MiB, and deleted after 10 idle minutes. An export
Google refuses is `upstream_error`.

### Confirmations

`confirm.request` asks the human to confirm something on a card that no saved rule can accept.
Parameters: `principal` (`local`), `kind` (`[a-z][a-z0-9_]{0,30}`), `title` (at most 120
characters), `preview` (blocks) and `require_step_up` (default `true`: the card needs a passkey
where the install requires one for sensitive actions). Result: `{"approval_id": "…", "expires_at":
"…"}`, returned at once. The card appears on the approvals page with the plugin's display name, and
the human gets the usual notification.

The request is refused with `confirmation_refused` (`data.reason` `unattended_session`) while any
MCP session is unattended, because the request comes from the plugin process and the daemon cannot
tell which session caused it. A refusal is audited as `<kind>; refused`. At most 64 confirmations
may be pending at once, and at most 8 per plugin; a request over either cap is refused with
`confirmation_refused` (`data.reason` `too_many_pending`) before any card exists.

`confirm.await` takes `approval_id` (one of this plugin's own) and `timeout_ms` (0 to 300,000;
default 300,000) and answers `{"status": "approved" | "denied" | "expired", "decided_at": "…"}`, or
`timeout` when still pending. A confirmation carries no deny note. The same `approval_id` works with
`privacyfence_await_approval`.

### Approvals

`approval.request` asks a human to approve a thing and keeps the answer until a human revokes it.
Parameters: `principal` (`local`), `kind` (`[a-z][a-z0-9_-]{0,40}`), `subject_id` (1 to 200
characters, no control or bidirectional characters, no line breaks or tabs), `digest` (`sha256:` and 64 lowercase hex digits
of the content), `title` (at most 120 characters), `preview` (blocks), optional `page` (a path of the
plugin's own page, needs `pages: true`) and `require_step_up` (default `true`). An approval binds to
`(plugin, principal, kind, subject_id, digest)`.

Result: `{"approval_id": "…", "status": "pending" | "approved", "expires_at": "…"}`. Rules:

- Something already approved answers `approved` with its id and shows no card. A card still pending
  for the same tuple returns its id with `pending`.
- No rule can accept the card, and the request is refused with `confirmation_refused`
  (`data.reason` `unattended_session`) while any session is unattended. The pending limits are the
  confirmation limits (64, 8 per plugin) and are counted apart, with the same `too_many_pending`
  reason.
- `page` is normalized like a page request. A path that is rejected, or that contains `?` or `#`
  once normalized (an encoded `%3F` or `%23` included), is `invalid_params`, as is `page` without
  `pages: true`.
- The finalizer stores the approval before the card is marked answered, so `approval.check` already
  says `approved` when `approval.await` does. Denied and expired requests leave no record.

`approval.check` takes `principal`, `kind`, `subject_id` and `digest` and answers `{"status":
"approved" | "revoked" | "unknown", "approval_id"?, "decided_at"?}`: `revoked` when the latest
record for the tuple was revoked, and `unknown` for an unseen tuple, a different digest, or a store
that could not be read. `approval.await` takes `approval_id` and `timeout_ms` (0 to 300,000) and
answers `{"status": "approved" | "denied" | "expired", "decided_at"?}`, or `timeout` while pending; for
an id that was answered without a card it answers `approved`. When a human revokes an approval in
Settings, a running plugin gets the notification `approval.revoked` with `approval_id`, `kind`,
`subject_id` and `digest`, best effort. Purging or uninstalling a plugin deletes its approvals.

**The embedding rule.** The card shows PrivacyFence's fields (plugin, kind, subject, digest) first,
outside any frame. When `page` is given, the plugin's page loads in a sandboxed frame at
`/plugins/<name><page>?pf_approval=<approval_id>`; the query reaches the plugin unchanged, so the
page can show the right subject. Only a request that carries a `pf_approval` naming a pending
approval of this plugin for exactly this normalized path is served with `frame-ancestors 'self'` and
`X-Frame-Options: SAMEORIGIN`; any other request for a page gets the headers in [Pages](#pages) and
cannot be framed. The card's own response allows `frame-src 'self'` only when it has a page. The
digest is what binds: PrivacyFence cannot check that the preview or the page shows what was hashed.

### Outputs

A plugin with `outputs: true` writes files into `PrincipalContext.output_dir`. A file is published
when it is a regular file (a symbolic link is ignored) that is at most 8 path segments below the
folder, has no segment starting with `.`, and has an extension of one of the plugin's
`output_types`. A plugin writes `.name.tmp` and renames it; a leading-dot name is never published.

PrivacyFence reads the folder through two of its own tools, not through the protocol:

- `plugin_outputs_list` (`plugin`, `prefix`, `cursor`) runs without asking and returns `{plugin,
  files: [{path, size, modified, mime_type}], next_cursor}`, 200 files per page in path order.
  `next_cursor` is set only when more files follow.
- `plugin_outputs_read` (`plugin`, `path`, `offset`, `reason`) shows an approval card with the text
  and a PII scan, and returns `{plugin, path, mime_type, size, sha256, offset, length, next_offset,
  text}`, about 90,000 bytes per call, cut at a UTF-8 character boundary; `sha256` is of the whole
  file. The file is read once before the card, and the card's bytes are the ones released. A negative
  offset ("Offset must not be negative.") or one past the end ("Offset is past the end of the
  file.") is an error; invalid UTF-8 is decoded with replacement characters.
- A requested path is accepted only if it is canonical: no backslash, colon or NUL, no empty segment,
  no segment starting with `.`, at most 8 segments, no symbolic link on the way, and equal to the
  resolved path. Anything else is "No such output file.".
- An "Always allow" rule for the scope `plugin:<name>:output` takes values ending in `/`, which
  cover that folder and everything under it, or a path, which is that one file.
- Audit: the read is audited like any gated read; a `plugin_output` entry for `plugin:<name>` with
  `read <path>; offset=<n>; bytes=<n>` is written after the gate returns, so a denied read has none.

### Pages

`web.request` carries `principal`, `method` (always `GET`), `path` and `query`, and the plugin
answers `{"status", "headers", "body", "body_encoding"}` with `body_encoding` `utf8` (default) or
`base64`. Only a plugin whose manifest sets `pages: true` and that is running (its `initialize`
succeeded) is asked; any other page request gets 404.

- A HEAD request is forwarded as GET and its body dropped. Any other method gets 405 with `Allow:
  GET, HEAD` and never reaches the plugin.
- Pages are served only to the owner's signed-in human session. Anything else gets the same 404 as
  the Settings pages, and the plugin is not called.
- `path` is URL-decoded once and always starts with `/`. A `..` segment, a NUL, a backslash, `//` or
  a path over 512 characters is a 400 and never reaches the plugin. `query` holds strings, the last
  value of a repeated name winning.
- The daemon keeps the status only if it is 200, 204, 400, 404 or 500 (anything else becomes 502),
  the `content-type` only if it is `text/html`, `text/plain`, `text/css`, `application/javascript`,
  `application/json`, `image/png`, `image/svg+xml` or `image/jpeg`, optionally with
  `; charset=utf-8` (anything else becomes `application/octet-stream`), and the body only up to 8
  MiB (more becomes 502). Every other header the plugin sends, `set-cookie`, `cache-control` and
  any CSP included, is dropped. No answer within 10 seconds, or an error, becomes a 502 with the body
  "The plugin did not answer."
- Every response under `/plugins/` carries these headers (the embedding rule in
  [Approvals](#approvals) is the one exception, for `Content-Security-Policy`'s `frame-ancestors`
  and `X-Frame-Options`):

  ```
  Content-Security-Policy: sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Cache-Control: private, no-store
  X-Frame-Options: DENY
  Permissions-Policy: …
  Cross-Origin-Opener-Policy: same-origin
  ```

  The page runs in an opaque origin without `allow-same-origin`: it cannot read the session cookie
  or call PrivacyFence's APIs, and its requests for separate files carry no cookie and are refused.
  A page must inline its CSS, scripts and images (as `data:` URIs). A plugin page is a single
  self-contained page that keeps its state in the page itself (script or `#fragment`). Other pages
  of the same plugin open only from Settings or a typed URL: a link from a plugin page to another
  page, or back into PrivacyFence, does not carry the session and gets the owner-only 404.

### Storage

Every plugin has an install-wide directory, `plugin-data/<name>/shared` in the data root, and a
per-principal directory under the principal's own data directory. Both are created private to the
service account before `initialize`; the AI client cannot read them.

`storage.purge` takes `scope` (`all`, `install` or `principal`, with `principal`) and is answered
`{"purged": true}` within 30 seconds. When a human asks Settings to delete a plugin's data, the
daemon sends `{"scope": "all"}`, then deletes both directories whether or not the plugin answered,
and restarts the plugin if it was running.

### Events

- `connector.state_changed` — `{"connector": "calendar", "state": "enabled" | "disabled" |
  "signed_in" | "signed_out", "principal": "local"}`. `enabled` and `disabled` follow the
  connector's switch in Settings; `signed_in` and `signed_out` follow its sign-in while it is
  enabled.
- `plugin.disabling` — `{"reason": "user" | "crash_limit" | "hash_changed" | "admin"}`, sent before
  a plugin is stopped because someone disabled it (`user`) or its files changed (`hash_changed`).
  It is not sent for an ordinary daemon shutdown.
- `shutdown` — `{"grace_ms": 5000}`. The plugin exits within five seconds, or it is terminated, and
  killed two seconds after that.
- `principal.removed` — `{"principal": "…"}`, reserved; local mode never sends it.

Events are best effort.

### Inspection

When the owner reviews a plugin, the daemon starts it with `purpose: "introspect"`, reads the
`initialize` result and sends `shutdown`. During that start `source.call` and `confirm.request` are
refused with `introspection_only`. Inspection is how Settings lists the tools, gates and floor that
the owner approves.

## Organization-mode fields

Protocol 1 reserves the fields an organization deployment will need. In local mode the daemon never
sends `PrincipalContext.roles`, and refuses a `source.call` that carries `credential`, or a manifest
that sets `service_credentials: true`, with `org_only_field`. `initialize` carries `mode`, and a
plugin that handles several principals works unchanged where more than one exists. Organization
mode does not start plugins.

## Limits and timeouts

| Limit | Value |
|---|---|
| Line | 16 MiB |
| Requests in flight, each direction | 16 |
| Consecutive invalid lines before disconnect | 3 |
| Tool result and payload, serialized | 100,000 bytes |
| Preview | 64 KiB, 50 blocks |
| Table cell | 4,096 characters |
| Tools per plugin | 64 |
| Scope values per scope type | 100, each at most 200 characters |
| `source.call` result | 12 MiB |
| Drive chunk | 8 MiB (no limit on the file) |
| Cursor | 4,096 characters |
| `jira.search` and `calendar.list_events` page size | 100 and 250 |
| Approval subject | 200 characters |
| Output folder depth | 8 segments |
| Output list page, read page | 200 files, 90,000 bytes |
| Page body | 8 MiB |
| Page path | 512 characters |
| Pending confirmations, pending approvals | 64 each |

| Request | Timeout (seconds) |
|---|---|
| `initialize` | 10 |
| `tool.prepare` | 30 |
| `tool.execute` | 60 |
| `web.request` | 10 |
| `storage.purge` | 30 |
| `source.call` | 120 |
| `confirm.request` | 5 |
| `approval.request` | 5 |
| `confirm.await`, `approval.await` | the `timeout_ms` it carries, at most 300 |

A plugin that crashes is restarted after 1, 2, 4, 8, 16 and then 30 seconds, and disabled after five
crashes in ten minutes. The numbers are also in the schema's `x-limits`.

## Audit entries

Plugin activity is written to the audit log with `connector` set to `plugin:<name>`:

| Decision | When | `summary` |
|---|---|---|
| `plugin_source` | Every `source.call`, success or error | The targets and `bytes=<n>`, or `error=<code>`; never the data |
| `plugin_confirm` | A confirmation is requested, answered or refused | `<kind>; requested`, `<kind>; approved`, `<kind>; denied`, `<kind>; expired` or `<kind>; refused`; `request_id` is empty |
| `plugin_approval` | An approval is requested, answered, refused or revoked | `<kind>; requested`, `<kind>; approved`, `<kind>; denied`, `<kind>; expired`, `<kind>; refused` or `<kind>; revoked`; never the subject or the digest |
| `plugin_output` | A plugin output file is read | `read <path>; offset=<n>; bytes=<n>`, written after the gate returns |
| `plugin_lifecycle` | A plugin is enabled, disabled, removed or purged, or its tool list changes | For example `enabled`, `disabled: <reason>`, `tools changed: +a,-b`, `tools change rejected: <detail>`, `data purged (ack)`, `approvals deleted: <n>`, `removed; data and rules deleted` |

A gated plugin tool call is audited like any connector tool call, with the operation key
`plugin.<name>.<tool>`; an `auto` tool writes an auto-accepted entry.
