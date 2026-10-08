# Plan: plugin framework follow-ups (card text, SDK parity, small hardening)

## Goal

This plan covers the follow-ups the user chose from the plugin framework PRs. It is the third PR in
the stack, after
[privacyfence/privacyfence#855](https://github.com/privacyfence/privacyfence/pull/855) and
[privacyfence/privacyfence#856](https://github.com/privacyfence/privacyfence/pull/856). Its feature
branch first merges the finished `feature/plugin-framework-cr`, then adds the changes below.
Tracking issue: [privacyfence/privacyfence#846](https://github.com/privacyfence/privacyfence/issues/846).

What changes for the user:

- **T1. Restart hint.** The enable dialog in Settings → Plugins says that a newly enabled plugin's
  tools may only show up in a new conversation, or after the AI client restarts. During pre-merge
  testing, Claude kept its old tool list until it was restarted. The daemon does send
  `tools/list_changed`; the client does not act on it in an open conversation.
- **Card text and slots.**
  - **R5.** A line break or tab in a plugin's display name, an approval subject, or a confirmation
    or approval title can no longer split an approval card's header.
  - **R6.** A confirmation or approval card whose set-up fails no longer leaks one of the plugin's
    pending-card slots.
- **Small hardening.**
  - **R2.** Reading a published output file never follows a file that was swapped for a symbolic
    link after the path check.
  - **R3.** A Drive Range read stops as soon as the server sends more than it asked for.
- **R9. SDK parity.** The plugin SDK and its `PluginTestHost` behave like PrivacyFence in six
  places, so a plugin that passes its own tests is not refused at install or at run time:
  - size measurements;
  - NaN and Infinity;
  - the scope type description limit and the reserved `output` scope type;
  - the line limit when sending;
  - how a source error from a tool reaches PrivacyFence;
  - reserved plugin names and an introspection run.
- **Docs and comments.**
  - **R1.** `approval.revoked` delivery is worded correctly.
  - **R8.** The Windows sibling-file gap is documented (the fix is issue
    [privacyfence/privacyfence#860](https://github.com/privacyfence/privacyfence/issues/860)).
  - **R10.** The rest:
    - the `crashed N times` restarting reason;
    - the Jira, Confluence and Calendar record shapes;
    - three more reserved names;
    - a LICENSE in the SDK wheel;
    - corrected audit-log comments, the publish workflow's job counts and one ADR citation.

Out of scope, filed as issues:

| Item | Issue |
|---|---|
| R4 | [privacyfence/privacyfence#858](https://github.com/privacyfence/privacyfence/issues/858) |
| R7 | [privacyfence/privacyfence#859](https://github.com/privacyfence/privacyfence/issues/859) |
| R8 code | [privacyfence/privacyfence#860](https://github.com/privacyfence/privacyfence/issues/860) |
| R11 | [privacyfence/privacyfence#861](https://github.com/privacyfence/privacyfence/issues/861) |
| R12 | [privacyfence/privacyfence#862](https://github.com/privacyfence/privacyfence/issues/862) |
| R13 | [privacyfence/privacyfence#863](https://github.com/privacyfence/privacyfence/issues/863) |
| N3 | [privacyfence/privacyfence#864](https://github.com/privacyfence/privacyfence/issues/864) |
| N5 | [privacyfence/privacyfence#865](https://github.com/privacyfence/privacyfence/issues/865) |
| N1 | [privacyfence/privacyfence#866](https://github.com/privacyfence/privacyfence/issues/866) |
| N2 | [privacyfence/privacyfence#867](https://github.com/privacyfence/privacyfence/issues/867) |

Dropped by the user: R14 (the `today` example's purge and publish waits), R15 (HEAD and 405
ordering), R16 (`register_dynamic_tools` ordering) and N4 (the test host's revoke audit row).

**About the stacked PR.** As in the change-request plan, `/implement` opens its PR to `main`. Until
#855 and #856 merge, this PR's diff contains both of them as well. p0's merge commit changes paths
outside its `touches`. That is expected: the orchestrator's diff review of p0 checks only that the
merge brought in its source unchanged.

## Current state

Line numbers are on `feature/plugin-framework-cr` at `f6ebb5ec` (#856's head).

### Card text (R5)

- `plugins/blocks.py:20-32`: `clean_text` strips C0 and C1 controls except `\n` and `\t`, DEL, and
  the bidi controls. `\r` is stripped. U+2028 and U+2029 are not.
- `plugins/manifest.py:96-98` refuses a `display_name` when `clean_text(display_name) !=
  display_name`. A newline or tab therefore passes.
- `plugins/protocol.py:471-475` (`_approval_tuple`) applies the same rule to `subject_id`.
- `plugins/confirm.py:105-108` builds the card title as `clean_text(f"{display_name}:
  {parsed.title}")`.
- `plugins/approvals.py:273-276` and `:302-315` build the title, the `Plugin` field, the frame
  title and the stored record title the same way. The `Subject` field shows `parsed.subject_id` as
  it is.
- `ToolDef.from_wire` (`protocol.py:242-243`) reads a tool's `title` and `effect` with `_opt_str`,
  which does no cleaning. Both reach the gate card (`connector.py:335`, the "Tool" field, and the
  effect sentence at `:182`) and the audit `tool_name`, so they can split a header too. The SDK's
  `Plugin.tool` (`plugin.py:527-560`) and the test host's `_check_tool` do not check them either.
- SDK test host `plugin-sdk/src/privacyfence_plugin_sdk/testing/_approvals.py:29` and `:91-94`
  check `subject_id` with its own `_HIDDEN_RE`, which is the daemon's rule.

### Slot leak (R6)

`plugins/confirm.py:109-117` and `plugins/approvals.py:294-316` follow this order:

1. `_reserve(plugin)` takes a slot.
2. Inside `try`: `register_confirm`. Its `except BaseException` releases the slot.
3. **After** that `try`: `registry.set_html(...)` and the `_owned` (and `_pending`) insertion.

If `set_html` or building the HTML raises, the slot is never released and the card stays
registered with no finalizer. `_audit` cannot raise: it catches and logs (`confirm.py:228-232`,
`approvals.py:478-482`).

### Outputs and Drive (R2, R3)

- `plugins/outputs.py:122-147` `_canonical_file`:
  - `lstat`s each path component, requiring directories, then a regular file at the end;
  - resolves the path;
  - compares the result with the canonical path.

  `read_output` (`:164-197`) then `open(file, "rb")`s the resolved path. A file swapped for a
  symlink between those two steps is followed.
- `drive_client.py:1522-1552` `download_range`: `chunks = [chunk for chunk in
  resp.iter_content(chunk_size=8 MiB) if chunk]` reads the whole 206 body before any length check.

### SDK parity (R9)

Measured against the daemon:

- **Separators.**
  - The SDK measures sizes with default JSON separators:
    - `plugin-sdk/.../blocks.py:146` (preview cap);
    - `plugin.py:730` (prepare payload, 100,000 bytes);
    - `plugin.py:782` (execute result).
  - The daemon is compact everywhere: `plugins/blocks.py:194`, `protocol.py:142`,
    `connector.py:96`.
  - So the SDK refuses payloads near a limit that the daemon would accept.
- **NaN and Infinity.**
  - The daemon's `_cell` refuses non-finite floats (`plugins/blocks.py:69-72`, "must be a finite
    number"). The SDK's (`blocks.py:120-126`) accepts any float.
  - Neither side sets `allow_nan=False` (`rpc.py:155`, `_rpc.py:179`) or rejects the constants on
    `json.loads` (`rpc.py:227`, `_rpc.py:227`). Both therefore emit and accept the non-JSON tokens
    `NaN` and `Infinity`.
- **Scope types.**
  - The daemon requires a scope type description of 1 to 500 characters and reserves the scope
    type `output` (`plugins/tools.py:23`, `:53-58`).
  - The SDK's `scope_type` (`plugin.py:518-525`) and the test host (`testing/_host.py:96-100`)
    check neither.
- **Line limit on send.**
  - The daemon's `_send` (`rpc.py:153-166`) refuses a line over `MAX_LINE_BYTES` with
    `payload_too_large`. Its `_send_quiet` (`:168-177`) then answers a too-big result with an error
    instead of leaving the caller to time out. Its `_read_line` (`:179-201`) counts one oversize
    line as one invalid line.
  - The SDK's `_send` (`_rpc.py:178-182`) has no check. Its `_send_quiet` (`:184-188`) turns any
    failure into `_shutdown("write_failed")`. Its `_read_line` (`:190-201`) uses `readline()`, so
    one oversize line can count as several.
- **Source errors.**
  - A `SourceError` (`responses.py:19-26`) raised out of a tool handler is not caught by
    `_prepare`/`_execute` (`plugin.py:715`, `:779-780`). `_serve` (`_rpc.py:285-296`) turns it into
    `internal_error` "handler failed".
  - So the daemon's `connector_unavailable` sentence (`connector.py:64-68`) never reaches the AI for
    an SDK plugin.
- **Reserved names.**
  - The daemon refuses a reserved plugin name at manifest load (`manifest.py:91-92`).
  - It refuses an MCP tool name equal to a built-in tool or meta-tool (`tools.py:111-114`).
  - The SDK's `Plugin(name, version)` (`plugin.py:500-505`) and the test host check neither.
  - `RESERVED_PLUGIN_NAMES` (`constants.py:80-84`) covers every connector name and
    `privacyfence`. The 126 built-in tool and meta-tool names, however, have 12 distinct first
    segments, and one of them, `apps` (from `apps_script_*`), is not reserved. A plugin named `apps`
    would collide.
- **Introspection.**
  - The daemon reviews a plugin with `Supervisor.introspect()` (`supervisor.py:232-249`): purpose
    `introspect`, and `source.call` and `confirm.request` refused with `introspection_only` "not
    available while introspecting".
  - The test host always initializes with purpose `run` (`testing/_host.py:294`).
- **Pinned limits.** `tests/unit/plugin_sdk/test_plugin.py::TestLimits` (`:86-97`) and
  `tests/unit/plugin_sdk/test_testhost.py::test_limits_match_the_daemons_constants` (`:316-330`) pin
  the SDK's copied limits to `privacyfence.plugins.constants`.

### Docs and comments (R1, R8, R10)

- **`docs/plugin-protocol.md`:**
  - `:67-68` says `approval.revoked` goes to "a plugin whose manifest asks for the feature". No
    manifest key exists for approvals.
  - `:126-128` lists the reserved names.
  - The source-call table at `:333-340` says only "a list of objects" for Jira and Calendar, and
    "the page as an object" for Confluence. The records are `dataclasses.asdict` of `JiraIssue`
    (`jira_client.py:107-119`), `CalendarEvent` (`calendar_client.py:137-161`, with
    `CalendarAttendee` `:114-118` and `CalendarAttachment` `:122-`) and `ConfluencePage`
    (`confluence_client.py:135-147`), plus `body_offset` and `body_total_chars`.
- **`docs/plugins.md`:**
  - The state table (`:236-253`) has no row for the **Restarting** reasons `crashed 1 time` /
    `crashed <n> times` (`supervisor.py:331`).
  - The Windows paragraph (`:66-77`) does not say that only the executable and its folders are
    checked.
- **`src/privacyfence/audit_log.py`:**
  - `:321` says `plugin_confirm` is written by `plugins/confirm.py`.
  - `:330` says `plugin_approval` is written by `plugins/approvals.py and plugins/host.py`.
  - `:344` says `plugin_lifecycle` is written by `plugins/host.py and plugins/supervisor.py`.
  - In fact `plugins/host.py` writes all three: it is the only module that uses
    `AUDIT_PLUGIN_CONFIRM`, `AUDIT_PLUGIN_APPROVAL` and `AUDIT_PLUGIN_LIFECYCLE`. `confirm.py` and
    `approvals.py` call a host-supplied audit callback.
- **`.github/workflows/publish-pypi.yml`:** `:30` says "the two publish jobs", and `:349` and `:353`
  say "the two PyPI jobs". There are four publish jobs: `publish-testpypi`, `publish-pypi`,
  `publish-sdk-testpypi` and `publish-sdk-pypi`.
- **`src/privacyfence/plugins/storage.py:1`** cites ADR 0120 only. Per-plugin storage is decided in
  ADR 0120 and ADR 0121.
- **`plugin-sdk/pyproject.toml`** has `license = "Apache-2.0"` but there is no `plugin-sdk/LICENSE`,
  so the wheel ships no license text. The repository root has the Apache 2.0 `LICENSE`.

### Settings (T1)

`src/privacyfence/settings_window_html.py:1269-1305` `renderPluginDialog` builds the enable
dialog. Its "facts" lines are `<div class="pf-plugin-facts">` (`:1288-1296`), followed by the
Cancel and Enable buttons. `tests/integration/test_plugin_settings_browser.py:154-172`
(`test_enable_dialog`) asserts the dialog's text.

## Design

### D1. Restart hint (T1)

In `renderPluginDialog`, right after the `max_gate_floor === 'auto'` facts line and before
`<div class="pf-modal-buttons">`, add one line:

```js
html += '<div class="pf-plugin-facts">After you enable it, start a new conversation in your AI client. Some clients list new tools only after they restart.</div>';
```

`docs/plugins.md` gets one paragraph at the end of "Using a plugin's tools" (in the retire phase,
D10).

Rejected: a hint on the running plugin's row. It stays on screen forever and is noise once the
client has caught up.

### D2. One-line text (R5)

Add to `src/privacyfence/plugins/blocks.py`, below `clean_text`:

```python
_LINE_BREAK_RE = re.compile(r"[\n\t\u2028\u2029]+")


def clean_line(value: str) -> str:
    """``clean_text``, then every run of line breaks and tabs replaced by one space: for text a card
    shows on one line (a title, a header field)."""
    return _LINE_BREAK_RE.sub(" ", clean_text(value))
```

Use it as follows:

- **Refuse when `clean_line(x) != x`:**
  - `manifest.py` `display_name`, with the message `"display_name must not contain line breaks,
    tabs, control or bidirectional characters"`;
  - `protocol.py` `_approval_tuple` `subject_id`, with the message `f"{where}.subject_id must not
    contain line breaks, tabs, control or bidirectional characters"`.
- **Refuse when `clean_line(x) != x`, tool definitions:** in `protocol.py` `ToolDef.from_wire`, a
  `title` or `effect` that is not `None` is refused with
  `f"tool.{field} must not contain line breaks, tabs, control or bidirectional characters"`
  (`field` is `title` or `effect`), raised the way the other `ToolDef` field errors are. The SDK's
  `Plugin.tool` raises `ToolDefinitionError` with the same text, and the test host's `_check_tool`
  refuses it with the same text; both are in p5, which owns those files.
- **Replace (titles are free text):** every `clean_text(...)` call that builds card text in
  `confirm.py` (the title and its "empty after cleaning" check) and `approvals.py` (the title, its
  empty check, the `Plugin` field, the frame title, and the `_Pending` title stored in the record)
  becomes `clean_line(...)`.
- **SDK test host:** in `testing/_approvals.py`, the `subject_id` check also refuses
  `\n`, `\t`, U+2028 and U+2029, with the same message as the daemon. Add those four characters to
  the condition next to `_HIDDEN_RE`.

### D3. No slot leak (R6)

Restructure both `ConfirmationService.request` (`confirm.py`) and `ApprovalService.request`
(`approvals.py`) the same way:

```python
self._reserve(plugin)
registry = None
card = None
try:
    registry = self._registry_provider()
    card = registry.register_confirm(sensitive=parsed.require_step_up, notify=True)
    # approvals.py only: card.frame_src = ...
    registry.set_html(card.id, build_...(...))
    with self._lock:
        self._owned[card.id] = ...
        # approvals.py only: self._pending[key] = card.id
except BaseException:
    if registry is not None and card is not None:
        registry.finalize(card.id, "deny")
    self._release(plugin)
    raise
self._audit(plugin, parsed.kind, "requested")
# ... finalizer scheduling unchanged
```

The `_owned` insertion is the last statement in the `try`, so nothing has to be removed from
`_owned` or `_pending` on failure. No audit entry is written for a card that never finished set-up.
`registry.finalize` works on a card that has no HTML yet.

### D4. Outputs read without following a swap (R2)

- **`_canonical_file`** returns a third value: the last component's `os.lstat` result (the file's
  `stat_result`). Keep that result in a variable inside the loop. Update its one caller.
- **`read_output`** opens the file like this:

```python
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
try:
    fd = os.open(file, flags)
except OSError:
    raise ValueError(NO_SUCH_FILE) from None
with os.fdopen(fd, "rb") as handle:
    opened = os.fstat(handle.fileno())
    if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(checked, opened):
        raise ValueError(NO_SUCH_FILE)
    # the existing hashing/window loop, reading from handle
```

  `checked` is the `lstat` result from `_canonical_file`. `os.path.samestat` compares `st_dev` and
  `st_ino`, which Python fills on Linux, macOS and Windows (from the file index) for both `lstat`
  and `fstat`. `O_NONBLOCK` keeps a file swapped for a FIFO from blocking the open; the `S_ISREG`
  check then refuses it. The existing `except OSError: raise ValueError(NO_SUCH_FILE)` still wraps
  the read loop.

### D5. Bounded Range read (R3)

In `download_range`, replace the list comprehension with:

```python
chunks: list[bytes] = []
received = 0
for chunk in resp.iter_content(chunk_size=min(length + 1, 8 * 1024 * 1024)):
    if not chunk:
        continue
    chunks.append(chunk)
    received += len(chunk)
    if received > length:
        raise DriveClientError("download_range: the server returned more than the requested range")
```

The existing `except DriveClientError: raise` passes it through. A short body (fewer than `length`
bytes) is still returned as it is; the spool's checks handle that today.

### D6. SDK wire parity (R9: separators, NaN, line limit)

1. **Separators.** These SDK size measurements use `separators=(",", ":")` and keep
   `ensure_ascii=False`:
   - `blocks.py:146`;
   - `plugin.py:730`;
   - `plugin.py:782`.
2. **Non-finite cells.** In the SDK's `blocks.py` cell check (`:120-126`), a `float` that is not
   `math.isfinite` raises `ValueError(f"{where}: must be a finite number")`. Use the same `where`
   prefix the surrounding checks use.
3. **NaN and Infinity on the wire, both sides.**
   - **Encoding:** every `json.dumps` in `src/privacyfence/plugins/rpc.py` `_send` and
     `plugin-sdk/.../_rpc.py` `_send` passes `allow_nan=False`.
   - **Decoding:** every `json.loads` of a received line passes `parse_constant=_no_constant`. Both
     modules get a module-level helper:

     ```python
     def _no_constant(name: str) -> None:
         raise ValueError(f"{name} is not JSON")
     ```

     A received line carrying `NaN`, `Infinity` or `-Infinity` is then a parse error: one invalid
     line, answered with `parse_error`, as invalid JSON is today.
   - **SDK size checks:** the checks at `plugin.py:730` and `:782` also pass `allow_nan=False`.
     `:782` already turns a `ValueError` into "the execute result is not JSON". `:730` needs no
     handler: the payload has been through `validate_blocks` first, which (after item 2) refuses a
     non-finite number with `invalid_blocks`.
   - **Daemon side effect:** `allow_nan=False` in the daemon's `_send` also covers `source.call`
     results, so a connector result that carries `NaN` reaches the plugin as `internal_error`
     "message is not JSON" instead of as invalid JSON. That is intended.
4. **Line limit on send (SDK `_rpc.py`).** Port the daemon's `rpc.py:153-201` behaviour into the
   SDK's `Peer`, using the SDK's own names (`self._max_line_bytes`, `self._send_lock`,
   `self._shutdown`, and `self._closed`, a bool; `_rpc.py:105-117`). Add `readuntil` and
   `readexactly` to the `_Reader` Protocol (`_rpc.py:72-73`):
   - **`_send`:**
     - `json.dumps` failing with `TypeError` or `ValueError` raises
       `RpcError("internal_error", "message is not JSON")`;
     - a line longer than `self._max_line_bytes` raises
       `RpcError("payload_too_large", "message exceeds the line limit")`;
     - `ConnectionError` or `OSError` while writing calls `self._shutdown("write_failed")` and
       raises `RpcError("internal_error", "peer closed")`.
   - **`_send_quiet`:** returns at once if `self._closed`. When `_send` raises `RpcError` and
     the message has `"result"`, it sends the error reply for that id instead, through
     `_send_quiet`. Otherwise it logs at debug level.
   - **`_read_line`:**
     - `readuntil(b"\n")`;
     - on `LimitOverrunError`, mark the line oversize, `readexactly(exc.consumed)` and continue;
     - on `IncompleteReadError`, take `exc.partial`, which is `None` at a clean end;
     - return `_OVERSIZE` once for the whole oversize line.

     The thread fallback reader (`_rpc.py:346`) is left as it is.

### D7. SDK names, scope types and source errors (R9, R10)

1. **`src/privacyfence/plugins/constants.py`.**
   - `RESERVED_PLUGIN_NAMES` gains `"apps"`, `"sheets"` and `"docs"`:
     - `apps` because built-in `apps_script_*` tools start with it;
     - `sheets` and `docs` because a plugin with those names would look like a built-in Google
       service.
   - Move `MAX_SCOPE_TYPE_DESCRIPTION_CHARS = 500` here from `plugins/tools.py:23`. `tools.py`
     imports it from `constants`.
2. **Daemon test.** Add a test to `tests/unit/plugins/test_constants.py`:
   - For every name in `auto_accept.STATIC_TOOL_NAMES | web.mcp_tools.META_TOOL_NAMES`,
     `name.split("_")[0]` must be in `RESERVED_PLUGIN_NAMES`.
   - A plugin's MCP names are `<plugin>_<tool>`, and plugin names contain no `_`. This test
     therefore makes the reserved-name check sufficient (stricter than needed) to rule out a
     collision with a built-in tool, which is why the SDK needs no copy of the built-in tool list.
3. **SDK `plugin.py`.**
   - Add `_RESERVED_PLUGIN_NAMES` (a frozenset equal to the daemon's set) and
     `_MAX_SCOPE_TYPE_DESCRIPTION_CHARS = 500`.
   - `Plugin.__init__`: after the name pattern check, a reserved name raises
     `ValueError(f"plugin name {name!r} is reserved")`.
   - `scope_type`:
     - `name == "output"` raises `ValueError("scope type output is reserved")`;
     - a description that is not a string of 1 to 500 characters raises
       `ValueError(f"scope type {name} needs a description of 1 to 500 characters")`;
     - these run before the existing duplicate check.
4. **SDK source errors.** `_prepare` and `_execute` wrap the tool handler call (`await handle(ctx,
   args)` and `await handle._execute(...)`) like this:

   ```python
   except SourceError as exc:
       raise RpcError(exc.code, exc.detail, extra={"reason": exc.reason} if exc.reason else None) from None
   ```

   `RpcError` already maps a code it does not know to `internal_error`. Import `SourceError` from
   `.responses`.
5. **Daemon prepare sentences.** Add `"upstream_error": "A service this plugin reads from returned
   an error."` to `_PREPARE_ERRORS` in `src/privacyfence/plugins/connector.py`, and the same entry
   to the test host's `_PREPARE_SENTENCES` (`testing/_host.py:47-52`).
6. **Test host size check.** The test host's own payload check (`testing/_host.py:463`) measures
   with `separators=(",", ":")`, like the daemon.
7. **Tool title and effect in the SDK.** `Plugin.tool` and the test host's `_check_tool` refuse a
   `title` or `effect` with `clean_line(x) != x`, as Design D2 says, with D2's message. The SDK has
   no `clean_line`: add one to the SDK's `blocks.py` with the same body as D2 (the SDK's own
   `clean_text` equivalent, then `[\n\t\u2028\u2029]+` to one space), and use it in both places.
8. **Test host `_check_tool_defs`.**
   - It imports `from ..plugin import _RESERVED_PLUGIN_NAMES, _MAX_SCOPE_TYPE_DESCRIPTION_CHARS` and
     refuses a plugin whose name is in `_RESERVED_PLUGIN_NAMES` with
     `ToolDefinitionError(f"name {name!r} is reserved")`. `Plugin.__init__` already refuses such a
     name, so a test reaches this branch by setting `p.name = "apps"` after construction; the branch
     stays because a plugin's runner can report another name than the one it was built with.
   - For each scope type, it applies the daemon's rules with the daemon's messages from
     `plugins/tools.py:53-58`: `"scope type output is reserved"`, and the 1 to 500 description
     rule.
9. **Pins.**
   - `TestLimits` in `tests/unit/plugin_sdk/test_plugin.py` gains
     `("_RESERVED_PLUGIN_NAMES", "RESERVED_PLUGIN_NAMES")` and
     `("_MAX_SCOPE_TYPE_DESCRIPTION_CHARS", "MAX_SCOPE_TYPE_DESCRIPTION_CHARS")`.

Rejected: shipping the built-in tool-name list in the SDK. It changes with every connector tool and
would drift. The reserved first segment covers it, and the test in item 2 keeps it covered.

### D8. Test host introspection (R9)

Add to `PluginTestHost`:

```python
async def introspect(self) -> list[dict]:
    """Start the plugin with purpose ``introspect``, as PrivacyFence does when you review a plugin,
    check its tool list, stop it and return the tools. ``source.call`` and ``confirm.request`` are
    refused with ``introspection_only``. Runs before the host is entered, and leaves it ready for
    ``async with``."""
```

- Refactor `_start` to take `purpose: str = "run"` and to pass it in the `initialize` params.
  When `purpose == "introspect"`, the peer's handlers are only:

  ```python
  {"source.call": _refuse_while_introspecting, "confirm.request": _refuse_while_introspecting}
  ```

  `_refuse_while_introspecting` is a module-level `async def _refuse_while_introspecting(_params:
  dict) -> Any` that raises `RpcError("introspection_only", "not available while introspecting")`,
  the daemon's handlers and text (`supervisor.py:235-238`). The SDK itself refuses those calls
  while introspecting (`plugin.py:145`, `:255`, `:313`), so plugin code never reaches them; they
  are there for a plugin not built with the SDK.
- `introspect()`:
  - raises `RuntimeError("introspect() runs before the host starts")` if `self._started` is true or
    a peer is running;
  - calls `self.plugin.tool_definitions()`;
  - creates `self._tmp`;
  - resets `self._principals = {}`;
  - awaits `self._start(purpose="introspect")`;
  - returns `self.tools`;
  - always awaits `self._teardown()` in `finally`.

  `_start` must reset `self._principals = {}` at its top, so that a later `__aenter__` starts
  clean.

### D9. Comments, workflow and LICENSE (R10)

- **`audit_log.py`:**
  - The `plugin_confirm` comment (`:321`) reads "plugins/host.py, for the cards plugins/confirm.py
    raises".
  - The `plugin_approval` comment (`:330`) reads "plugins/host.py, for plugins/approvals.py".
  - The `plugin_lifecycle` comment (`:344`) reads "plugins/host.py, including the state changes
    plugins/supervisor.py reports".
  - The rest of each comment is unchanged.
- **`publish-pypi.yml`:** comments only; no job, step or key changes.
  - Lines 28-32 (the comment above the top-level `permissions:`) become:

    ```yaml
    # Restrictive default for GITHUB_TOKEN, per CodeQL's actions/missing-workflow-
    # permissions check -- `build`, `build-sdk` and `publish-r2` inherit this; `dedupe`,
    # `wait_for_build` and the four publish jobs below (publish-testpypi, publish-pypi,
    # publish-sdk-testpypi, publish-sdk-pypi) declare their own job-level `permissions:`, which
    # replaces this default for them rather than adding to it.
    ```
  - Line 349 "Independent of the two PyPI jobs above (doesn't `needs:` either one, and isn't gated
    on" becomes "Independent of the four PyPI jobs above (doesn't `needs:` any of them, and isn't
    gated on".
  - Line 353 "like the two PyPI jobs, still wait on" becomes "like the four PyPI jobs, still wait
    on".
- **`plugins/storage.py:1`:** "(ADR 0120)" becomes "(ADR 0121)": ADR 0121 decides the data
  directories and their removal; ADR 0120 does not mention storage.
- **SDK license.**
  - Copy the root `LICENSE` and `NOTICE` byte for byte to `plugin-sdk/LICENSE` and
    `plugin-sdk/NOTICE` (Apache-2.0 section 4(d) asks for the NOTICE to travel with the work).
  - Add `license-files = ["LICENSE", "NOTICE"]` under `[project]` in `plugin-sdk/pyproject.toml`,
    below `license`.
  - Add a test, `tests/unit/plugin_sdk/test_packaging.py`, asserting that each pair is
    byte-identical.

### D10. Reference docs (retire phase)

- **`docs/plugin-protocol.md`, Versioning (`:67-68`).** Replace the last sentence with: "The daemon
  sends `approval.revoked` only to the running plugin that requested the revoked approval, and
  `output_dir` and `output_types` only to a plugin with `outputs: true`."
- **Manifest rules (`:126-128`).**
  - Add `apps`, `sheets` and `docs` to the reserved list, with the reason in one clause: "`apps`,
    `sheets` and `docs`, which built-in tools or services start with".
  - The `display_name` rule (find it in the same section) says it must not contain line breaks or
    tabs.
- **Approvals section.** The `subject_id` rule says the same as the `display_name` rule.
- **Transport section.** Add: "`NaN`, `Infinity` and `-Infinity` are not JSON: neither side sends
  them, and a line that carries one is a parse error."
- **Tool definitions section.** If it does not already state the scope type description limit (1
  to 500 characters) and the reserved scope type `output`, add one sentence that does.
- **Source calls: a new subsection "#### Record shapes"**, directly after the operations table. It
  holds three tables with the columns Field, Type and Notes:
  1. a Jira issue: every `dataclasses.fields(JiraIssue)`;
  2. a Calendar event: every `dataclasses.fields(CalendarEvent)`, with sub-tables for
     `CalendarAttendee` and `CalendarAttachment`;
  3. a Confluence page: every `dataclasses.fields(ConfluencePage)`, plus `body_offset` and
     `body_total_chars`.

  Type is the Python annotation written as JSON (`str` → string, `bool` → boolean, `int` → integer,
  `list[str]` → list of strings, a dataclass → object, see its table). Notes holds the dataclass's
  own comment for that field where it has one, and is empty otherwise; do not invent meanings.
- **Prepare sentences.** At the end of the `### tool.prepare` section, add: "When `tool.prepare`
  fails, the AI client sees one fixed sentence, never the plugin's own error detail:
  `connector_unavailable` gives "A service this plugin reads from is not connected.",
  `upstream_error` gives "A service this plugin reads from returned an error.",
  `payload_too_large` gives "The plugin's result is too large to return.", `timeout` gives "The
  plugin did not answer in time.", and any other error gives "The plugin could not prepare this
  call."" (the strings of `_PREPARE_ERRORS` and `PREPARE_FAILED` in `plugins/connector.py`).
- **`docs/plugins.md`:**
  - At the end of "Using a plugin's tools", add: "Your AI client learns about a newly enabled
    plugin's tools from PrivacyFence's tool-list change notice, but some clients show them only in
    a new conversation, or after you quit and restart the client. The enable dialog says so too."
  - In the state table, after the `crashed 5 times in 10 minutes` row, add: "| Restarting |
    `crashed 1 time`, `crashed <n> times` | The plugin exited and starts again after a pause (see
    below). The log shows why. |"
  - At the end of the Windows paragraph in "The plugins directory", add: "Only the executable and
    the folders on its path are checked. Other files in the plugin's folder, such as libraries the
    executable loads, are not; a default install under `%ProgramFiles%` gives them the folder's
    administrator-only permissions. Checking every file is tracked in
    [issue 860](https://github.com/privacyfence/privacyfence/issues/860)." (`docs/plugins.md` is a
    published doc: `tests/unit/test_docs_no_history.py` refuses `#<number>` in it, so the link text
    is "issue 860".)
  - Where `plugins.md` repeats the reserved names, update it as above.
- **SDK testing docs.** In `plugins.md`'s "Where the test host differs" list (under "Testing a
  plugin") and the same list in `plugin-sdk/README.md`:
  - the bullet "It has no `host.introspect()` (what Settings does when you review a plugin), and it
    ignores a `tools.changed` the plugin sends." becomes "It ignores a `tools.changed` the plugin
    sends.";
  - the bullet "It does not check a tool's MCP name against PrivacyFence's built-in tools;
    PrivacyFence refuses a tool whose name collides with one." becomes "It refuses a reserved
    plugin name, as PrivacyFence does, which also keeps every tool's MCP name clear of
    PrivacyFence's built-in tools.";
  - add one sentence before the list: "`await PluginTestHost(plugin).introspect()` starts the plugin
    the way Settings does when you review it, and returns its tool list."
- **New test `tests/unit/plugins/test_protocol_doc.py`.**
  - It reads `docs/plugin-protocol.md` and takes the text from `#### Record shapes` to the next line
    starting with `### `.
  - It asserts that `` f"`{f.name}`" `` appears in that text for every field of `JiraIssue`,
    `CalendarEvent`, `CalendarAttendee`, `CalendarAttachment` and `ConfluencePage`, plus
    `` `body_offset` `` and `` `body_total_chars` ``.
- **`CHANGELOG.md` `## [Unreleased]`.** Add these lines, in the existing sections, wording as
  written:
  - Changed: "Settings → Plugins: the enable dialog says that a newly enabled plugin's tools may
    only appear in a new conversation, or after the AI client restarts."
  - Changed: "Plugins: `apps`, `sheets` and `docs` are reserved plugin names, and the plugin
    protocol refuses `NaN` and `Infinity`, which are not JSON."
  - Changed: "Plugin SDK:
    - it measures sizes as PrivacyFence does;
    - it refuses non-finite table cells and reserved plugin names, and checks scope type
      descriptions;
    - it enforces the line limit when sending;
    - a source error raised from a tool reaches PrivacyFence as that error;
    - `PluginTestHost.introspect()` runs the review start."
  - Changed: "Plugins: a plugin whose display name, tool title or tool effect contains a line break
    or a tab is refused, and so is an approval subject that does."
  - Fixed: "Plugins: a line break or tab in a confirmation or approval title no longer splits an
    approval card's header."
  - Fixed: "Plugins: a confirmation or approval card whose set-up failed no longer keeps one of the
    plugin's pending-card slots."
  - Security: "Plugin outputs: a published file swapped for a symbolic link or another file after
    it was checked is not read." (add a `### Security` section in Keep a Changelog order if there
    is none)
  - Fixed: "Drive: a Range read stops as soon as the server sends more than the requested bytes."

## ADRs

None. Every change brings code or docs in line with a decision already recorded in ADRs 0120–0131,
or follows the JSON specification (`NaN`/`Infinity`). Reserving three more names follows ADR
0122's existing rule that a plugin tool must not collide with a built-in tool. Nothing here is hard to reverse,
changes a trust boundary or picks between alternatives the ADRs left open.

## Manual steps

There are no `manual_before` steps. `manual_after` has one short packaged spot check: the enable
dialog's new line, and one plugin output read through a real AI client. The step-by-step page is
`docs/plugin-framework-polish-plan-manual-steps.html`, published as the manifest's
`manual_steps_artifact`.

## Risks and open questions

- **#855 or #856 merged before p0 runs, or the CR branch is deleted.** p0 checks whether `origin/main` already contains
  `origin/feature/plugin-framework-cr`'s head. If it does, p0 merges `origin/main` instead. If it
  finds neither shape (for example the CR branch was rewritten), it stops with `status=blocked`.
- **Windows file identity (D4).** `os.path.samestat` relies on `st_ino`/`st_dev` from `lstat` and
  `fstat`. If the PR's `platform-windows` job fails the new identity tests for an unchanged file,
  that is this PR's failure to root-cause, never a skip.
- **`apps` is used by an existing fixture.** `tests/fixtures/plugins/echo-variants/builtin-collision`
  is a plugin named `apps` whose tool collides with `apps_script_get_content`
  (`tests/integration/test_plugin_refusals.py:243-248`). Reserving `apps` makes it a scan-time
  refusal; p5 owns both files and changes the case as its brief says. The built-in collision check
  stays covered by `tests/unit/plugins/test_tools.py`. Any other plugin named `apps`, `sheets` or
  `docs` outside p5's `touches` means `status=blocked`.
- **Doc sections that moved.** The retire phase edits by section heading, not line number. A
  section named in D10 that is missing means `status=blocked`.

## Implementation manifest

Every brief below ends with the same two rules: no `CHANGELOG.md` line (the retirement phase writes
them all), and no plan item IDs (`D3`, `R5`, `T1` and the like) in code, comments or test names,
because `tests/unit/test_code_no_history.py` refuses them.

```yaml
plan_slug: plugin-framework-polish
feature_branch: feature/plugin-framework-polish
tracking_issue: 846
max_parallel: 3
manual_steps_artifact: https://claude.ai/artifact/V8Z9VaBXXt6P4KePUDW82e
manual_steps_source: docs/plugin-framework-polish-plan-manual-steps.html
manual_before: []
manual_after:
- id: ma1-spot-check-polish
  title: Spot-check the enable dialog's restart line and one plugin output read on a packaged install of this PR's build
  why: CI renders the dialog in a headless browser but cannot check a real AI client picking up a newly enabled plugin's tools, or a real output read through it.
verify_after_merge:
- ruff check .
- python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/test_drive_client.py tests/unit/test_settings_window_html.py tests/unit/test_audit_log.py tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py -q
final_checks:
- docs/plugin-framework-polish-plan.md and docs/plugin-framework-polish-plan-manual-steps.html are deleted and nothing links to them (grep -rn plugin-framework-polish-plan docs scripts README.md prints nothing)
- No new ADR (the plan's ADRs section is "None"); docs/adr/README.md is unchanged by this branch apart from p0's merge
- CHANGELOG.md has the [Unreleased] lines from the plan's Design D10 and no new version heading
- The full /dod passes, including python3 scripts/check_coverage_floor.py coverage.json and python3 -m pytest tests/integration -v
- connector-live-check.yml dispatched against feature/plugin-framework-polish is green (drive_client.py changed; link the run in the PR)
- build.yml dispatched against feature/plugin-framework-polish is green (ma1 installs this build; link the run in the PR)
- python3 -m build plugin-sdk succeeds and the wheel lists LICENSE and NOTICE under its dist-info (unzip -l plugin-sdk/dist/*.whl | grep -E 'LICENSE|NOTICE')
- The PR's platform-windows job is green (the outputs identity check and the SDK line reader run there)
phases:
- id: p0-sync
  title: Merge the finished change-request branch into this branch
  depends_on: []
  complexity: S
  touches:
  - docs/README.md
  - scripts/build_site.py
  brief: |
    1. git fetch origin main. Then git fetch origin feature/plugin-framework-cr; if that fetch fails because the branch
       no longer exists, the source is origin/main.
    2. Otherwise: if `git merge-base --is-ancestor origin/feature/plugin-framework-cr origin/main` succeeds (#856
       already merged), the source is origin/main; if not, the source is origin/feature/plugin-framework-cr.
    3. Check that `git show <source>:docs/adr/README.md` lists ADR 0131; if not, stop with status=blocked (plan Risks,
       first bullet).
    4. git merge --no-ff <source> (a merge commit; never rebase). If git says "Already up to date", record that and go on.
       Resolve conflicts in docs/README.md and scripts/build_site.py mechanically: keep the entry for this plan
       (plugin-framework-polish-plan.md) and drop entries for plugin-framework-plan.md and plugin-framework-cr-plan.md if
       they reappear. Any conflict in another file: git merge --abort and stop with status=blocked.
    5. The merge may change many files outside this phase's touches; that is expected. Record in the PHASE-REPORT the
       source, the merged SHA, and that the diff review should only check the merge brought the source in unchanged.
    6. Run ruff check . and python3 -m pytest tests/unit -q; both must pass.

    No CHANGELOG.md line in this phase. No plan item IDs in code, comments or test names.
  acceptance:
  - git log --oneline -1 --merges shows the merge of the source, or the PHASE-REPORT says the branch was already up to date
  - ls docs/adr/0131-*.md succeeds
  - python3 -m pytest tests/unit/test_website_docs_allowlist.py tests/unit/test_docs_references_exist.py tests/unit/plugins -q passes
- id: p1-card-text
  title: One-line card text and tool titles, and no slot leak on a failed card set-up
  depends_on:
  - p0-sync
  complexity: M
  touches:
  - src/privacyfence/plugins/blocks.py
  - src/privacyfence/plugins/manifest.py
  - src/privacyfence/plugins/protocol.py
  - src/privacyfence/plugins/confirm.py
  - src/privacyfence/plugins/approvals.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_approvals.py
  - tests/unit/plugins/test_blocks.py
  - tests/unit/plugins/test_manifest.py
  - tests/unit/plugins/test_protocol.py
  - tests/unit/plugins/test_confirm.py
  - tests/unit/plugins/test_approvals.py
  - tests/unit/plugin_sdk/test_testhost_surfaces.py
  brief: |
    1. blocks.py: add _LINE_BREAK_RE and clean_line exactly as the plan's Design D2 gives them, below clean_text.
    2. manifest.py: the display_name check becomes `if clean_line(display_name) != display_name:` with D2's message.
    3. protocol.py: _approval_tuple's subject_id check becomes `if clean_line(subject_id) != subject_id:` with D2's
       message; ToolDef.from_wire refuses a non-None title or effect with clean_line(x) != x, with D2's tool message,
       raised the same way as the neighbouring ToolDef field errors.
    4. confirm.py: the title and the "empty after cleaning" check use clean_line instead of clean_text.
       approvals.py: the title, its empty check, the ("Plugin", ...) field, frame_title and the _Pending title use clean_line.
       Leave clean_text calls that are not card text alone.
    5. confirm.py ConfirmationService.request and approvals.py ApprovalService.request: restructure the reserve /
       register / set_html / _owned block exactly as Design D3 shows. Do not change the finalizer scheduling below it.
    6. testing/_approvals.py: the subject_id check also refuses "\n", "\t", "\u2028" and "\u2029" (write these as
       escapes in the source), with D2's message, where-prefixed as the existing message is.
    7. Tests:
       - test_blocks.py: clean_line turns "a\n\tb" into "a b" and "a\u2028b" into "a b", strips bidi controls, keeps
         ordinary spaces.
       - test_manifest.py: a display_name with "\n" and one with "\t" are refused with the new message.
       - test_protocol.py: an approval.request subject_id with "\n" is refused with the new message; a tool definition
         whose title contains "\n", and one whose effect contains "\t", are refused with D2's tool message.
       - test_confirm.py and test_approvals.py use the files' existing Harness, which holds a real
         PendingApprovalRegistry as harness.registry:
         (a) a title "Line one\nLine two" reaches the card as "Line one Line two": read it with
             harness.registry.get(<id>).html, as TestTitleCleaning in test_confirm.py does;
         (b) monkeypatch harness.registry.set_html to raise RuntimeError; request() raises RuntimeError; afterwards the
             service's _active_total is 0, the one registered card (`(card,) = harness.registry._pending.values()`) has
             harness.registry.await_status(card.id) == "denied" (as test_scheduling_failure_denies_the_card checks), and
             no "requested" audit entry was recorded.
       - test_testhost_surfaces.py: the test host refuses an approval subject_id containing "\n", following the
         existing subject_id refusal test at about lines 462-468.
    Stop condition: if an existing test in these files builds a display_name, title, effect or subject_id with "\n" or
    "\t" and expects it accepted, change it only when it is in this phase's touches; otherwise stop with status=blocked.

    No CHANGELOG.md line in this phase. No plan item IDs in code, comments or test names.
  acceptance:
  - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk -q passes
  - grep -n 'def clean_line' src/privacyfence/plugins/blocks.py prints one line
  - grep -c 'clean_line' src/privacyfence/plugins/approvals.py prints at least 5
- id: p2-outputs-drive
  title: Outputs read without following a swapped file, and a bounded Drive Range read
  depends_on:
  - p0-sync
  complexity: S
  touches:
  - src/privacyfence/plugins/outputs.py
  - src/privacyfence/drive_client.py
  - tests/unit/plugins/test_outputs.py
  - tests/unit/test_drive_client.py
  brief: |
    1. outputs.py: implement Design D4 (_canonical_file also returns the final lstat result; read_output opens with
       O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_BINARY where present, fstats, refuses a non-regular file or one for which
       os.path.samestat(checked, opened) is false, then runs the existing loop on that handle). Update every caller of
       _canonical_file in outputs.py.
    2. drive_client.py download_range: implement Design D5 (bounded loop, chunk_size=min(length + 1, 8 MiB), the exact
       DriveClientError text).
    3. Tests:
       - test_outputs.py:
         (a) the existing read tests stay green unchanged;
         (b) runs everywhere: monkeypatch outputs._canonical_file with a wrapper that calls the real one, then
             os.replace()s a different regular file (written next to it with other bytes) over the checked path, then
             returns the real result; read_output raises ValueError(NO_SUCH_FILE);
         (c) the same, but replacing the file with a symlink to a file outside the root; mark this test with the file's
             existing @symlinks marker (test_outputs.py near line 42), as its other symlink tests are.
       - test_drive_client.py: in the existing TestDownloadRange class, using the existing _FakeStreamResponse helper
         (about line 2385): a 206 response whose iter_content yields length + 10 bytes in 4-byte chunks makes
         download_range raise DriveClientError with D5's text, and the generator was not consumed past the chunk that
         crossed length (count the yields); a body of exactly length bytes is returned whole.

    No CHANGELOG.md line in this phase. No plan item IDs in code, comments or test names.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_outputs.py tests/unit/test_drive_client.py -q passes
  - grep -n 'O_NOFOLLOW' src/privacyfence/plugins/outputs.py prints one line
  - grep -n 'samestat' src/privacyfence/plugins/outputs.py prints one line
  - grep -n 'more than the requested range' src/privacyfence/drive_client.py prints one line
- id: p3-settings-hint
  title: The enable dialog's restart line
  depends_on:
  - p0-sync
  complexity: S
  touches:
  - src/privacyfence/settings_window_html.py
  - tests/unit/test_settings_window_html.py
  - tests/integration/test_plugin_settings_browser.py
  brief: |
    1. settings_window_html.py renderPluginDialog: add the one line from Design D1, in the position D1 names.
    2. tests/unit/test_settings_window_html.py: assert the exact sentence "After you enable it, start a new conversation
       in your AI client. Some clients list new tools only after they restart." is in the rendered settings HTML/JS
       (follow how that file asserts other dialog strings).
    3. tests/integration/test_plugin_settings_browser.py test_enable_dialog: add "start a new conversation in your AI
       client" to the expected tuple.
    4. Run python3 -m pytest tests/integration/test_plugin_settings_browser.py -q with the container Chromium
       (PRIVACYFENCE_TEST_CHROMIUM is set by the session-start hook); it must pass, not skip.

    No CHANGELOG.md line in this phase. No plan item IDs in code, comments or test names.
  acceptance:
  - python3 -m pytest tests/unit/test_settings_window_html.py tests/integration/test_plugin_settings_browser.py -q passes with no skips in test_enable_dialog
- id: p4-sdk-wire
  title: SDK wire parity - separators, NaN and Infinity, the line limit on send
  depends_on:
  - p0-sync
  complexity: M
  touches:
  - plugin-sdk/src/privacyfence_plugin_sdk/blocks.py
  - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
  - plugin-sdk/src/privacyfence_plugin_sdk/_rpc.py
  - src/privacyfence/plugins/rpc.py
  - tests/unit/plugin_sdk/test_blocks.py
  - tests/unit/plugin_sdk/test_plugin.py
  - tests/unit/plugin_sdk/test_rpc.py
  - tests/unit/plugins/test_rpc.py
  brief: |
    1. Design D6.1: compact separators in SDK blocks.py (the preview size measurement, about line 146) and plugin.py
       (the payload measurement, about line 730, and the execute-result measurement, about line 782).
    2. Design D6.2: SDK blocks.py refuses a non-finite float cell with "<where>: must be a finite number".
    3. Design D6.3: the _no_constant helper, allow_nan=False and parse_constant=_no_constant in both rpc modules;
       allow_nan=False in the two SDK size measurements. No new exception handler at the payload measurement.
    4. Design D6.4: port the daemon's _send/_send_quiet/_read_line behaviour (src/privacyfence/plugins/rpc.py:153-201)
       into the SDK Peer with the SDK's own attribute names (_max_line_bytes, _send_lock, _shutdown, _closed), and add
       readuntil and readexactly to the _Reader Protocol. Do not change the thread fallback reader.
    5. Tests:
       - plugin_sdk/test_blocks.py: a table cell float("nan") and float("inf") are refused; a preview whose compact JSON
         size is exactly the SDK preview cap passes (build it with a padded text block and measure with compact
         separators in the test).
       - plugin_sdk/test_plugin.py: an execute result containing float("nan") fails with "the execute result is not
         JSON".
       - plugin_sdk/test_rpc.py: build the peers under test with asyncio.StreamReader(limit=1024) and max_line_bytes=1024
         (extend make_peer with a limit argument; the default reader limit of 64 KiB would hide the change):
         (a) a handler returning a result larger than the limit makes the caller receive a payload_too_large error and
             the peer stays open;
         (b) one incoming 5000-byte line with invalid_lines_limit=2, followed by a valid request, is answered (one
             oversize line counts once);
         (c) an incoming line '{"jsonrpc":"2.0","id":1,"method":"x","params":{"v":NaN}}' gets a parse_error reply;
         (d) a handler returning {"v": float("nan")} makes the caller receive internal_error "message is not JSON".
       - plugins/test_rpc.py: the daemon equivalents of (c) and (d).

    No CHANGELOG.md line in this phase. No plan item IDs in code, comments or test names.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk tests/unit/plugins/test_rpc.py -q passes
  - grep -c 'allow_nan=False' src/privacyfence/plugins/rpc.py plugin-sdk/src/privacyfence_plugin_sdk/_rpc.py shows at least 1 for each file
  - grep -n 'def _no_constant' src/privacyfence/plugins/rpc.py plugin-sdk/src/privacyfence_plugin_sdk/_rpc.py prints two lines
- id: p5-sdk-names-errors
  title: Reserved names, scope type and tool text rules, and source errors in the SDK and test host
  depends_on:
  - p4-sdk-wire
  complexity: M
  touches:
  - src/privacyfence/plugins/constants.py
  - src/privacyfence/plugins/tools.py
  - src/privacyfence/plugins/connector.py
  - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
  - plugin-sdk/src/privacyfence_plugin_sdk/blocks.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
  - tests/unit/plugins/test_constants.py
  - tests/unit/plugins/test_tools.py
  - tests/unit/plugins/test_connector.py
  - tests/unit/plugin_sdk/test_plugin.py
  - tests/unit/plugin_sdk/test_blocks.py
  - tests/unit/plugin_sdk/test_testhost.py
  - tests/integration/test_sdk_testhost_conformance.py
  - tests/integration/test_plugin_refusals.py
  - tests/fixtures/plugins/echo-variants/builtin-collision/**
  brief: |
    1. First run: grep -rnE "Plugin\(\s*['\"](apps|sheets|docs)['\"]|^name: (apps|sheets|docs)$" tests examples plugin-sdk.
       The only expected hit is tests/fixtures/plugins/echo-variants/builtin-collision (handled in step 9). Any other
       hit outside this phase's touches: stop with status=blocked (plan Risks).
    2. constants.py and tools.py: Design D7.1 (three names added; MAX_SCOPE_TYPE_DESCRIPTION_CHARS moved and imported).
    3. tests/unit/plugins/test_constants.py: the Design D7.2 test, with a docstring that says why it is sufficient.
    4. SDK plugin.py: Design D7.3 (constants, reserved-name check in Plugin.__init__, scope_type rules), D7.4
       (SourceError mapping in _prepare and _execute) and D7.7 (title and effect in Plugin.tool, using a clean_line
       added to the SDK's blocks.py).
    5. connector.py _PREPARE_ERRORS and testing/_host.py _PREPARE_SENTENCES: Design D7.5.
    6. testing/_host.py: Design D7.6 (compact separators in the payload check at about line 463), D7.7 (title and
       effect in _check_tool) and D7.8 (_check_tool_defs: the import line D7.8 gives, reserved names, scope type rules).
    7. Pins: Design D7.9 in tests/unit/plugin_sdk/test_plugin.py TestLimits.
    8. Existing test that changes: tests/unit/plugin_sdk/test_testhost.py about line 168 asserts
       failed.error["code"] == "internal_error" with the comment "the plugin let the SourceError escape"; after D7.4 the
       code is "upstream_error". Change the assertion and the comment to say the SourceError's code is passed through.
    9. tests/integration/test_plugin_refusals.py, case "tool name colliding with a built-in tool" (about lines 243-248):
       with "apps" reserved, the builtin-collision variant is refused at scan. Change the case to
       rejected_at_scan=True with a reason that contains "name 'apps' is reserved" (use the exact reason the scan
       produces for a reserved name, as the file's other rejected_at_scan=True cases do), and rename the case to
       "plugin named after the start of a built-in tool". Leave the fixture's files as they are unless the case needs a
       change to them.
    10. Tests:
       - test_plugin.py: Plugin("apps", "1.0.0") raises "is reserved"; scope_type("output", "x") raises "scope type
         output is reserved"; scope_type("cal", "") and scope_type("cal", "x" * 501) raise the 1 to 500 message; a tool
         registered with title "A\nB" raises ToolDefinitionError with D2's tool message; a prepare handler raising
         SourceError("connector_unavailable", "the connector is not connected", "not_connected") makes _prepare raise
         RpcError with code connector_unavailable and extra reason not_connected; an unknown code becomes internal_error.
       - test_blocks.py (SDK): clean_line turns "a\n\tb" into "a b".
       - test_testhost.py: call _check_tool_defs directly:
         `from privacyfence_plugin_sdk.testing._host import _check_tool_defs`, then
         `_check_tool_defs(p, {"plugin": {"name": p.name, "version": p.version}, "scope_types": [{"name": "output",
         "description": "x"}], "tools": []}, "review")` raises ToolDefinitionError "scope type output is reserved"; the
         same with {"name": "cal", "description": "x" * 501} raises the 1 to 500 message; with p.name set to "apps"
         after construction it raises "name 'apps' is reserved".
       - test_connector.py: a tool.prepare failing with upstream_error gives the AI "A service this plugin reads from
         returned an error."
       - test_tools.py: still green with the moved constant.
       - test_sdk_testhost_conformance.py: one new case for the existing echo_source tool, whose handler already lets
         SourceError escape (tests/fixtures/plugins/echo/echo_plugin.py:113-125). Test host side:
         host.source.fail("calendar.list_events", "connector_unavailable", "not_connected", **CALENDAR_WINDOW), then the
         call's outcome.error["detail"]. Daemon side: stack.calendar_state = (False, None) (as
         tests/integration/test_plugin_framework.py:259 does), then the MCP result's text. Both are "A service this
         plugin reads from is not connected." Extend the file's Outcome record with the sentence if it does not carry
         one yet.

    No CHANGELOG.md line in this phase. No plan item IDs in code, comments or test names.
  acceptance:
  - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk -q passes
  - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py tests/integration/test_plugin_refusals.py -q passes
  - python3 -c "from privacyfence.plugins import constants as c; assert {'apps','sheets','docs'} <= c.RESERVED_PLUGIN_NAMES" exits 0
- id: p6-testhost-introspect
  title: PluginTestHost.introspect()
  depends_on:
  - p5-sdk-names-errors
  complexity: S
  touches:
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
  - tests/unit/plugin_sdk/test_testhost.py
  brief: |
    1. testing/_host.py: Design D8 (_start(purpose="run"), the module-level _refuse_while_introspecting, the introspect
       handlers, introspect(), the _principals reset at the top of _start).
    2. Tests in test_testhost.py:
       - for the file's existing sample plugin, `await PluginTestHost(plugin).introspect()` returns the same list as
         host.tools inside a later `async with` on the same host object;
       - after `await host.introspect()`, `plugin._host.introspecting is True` (the SDK records the purpose there);
       - `await _refuse_while_introspecting({})` raises RpcError with code introspection_only and detail "not available
         while introspecting" (import it from privacyfence_plugin_sdk.testing._host);
       - calling introspect() inside `async with host` raises RuntimeError with D8's text.

    No CHANGELOG.md line in this phase. No plan item IDs in code, comments or test names.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk -q passes
  - grep -n 'async def introspect' plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py prints one line
- id: p7-comments-license
  title: Audit-log comments, publish workflow comments, storage ADR cite, SDK LICENSE and NOTICE
  depends_on:
  - p0-sync
  complexity: S
  touches:
  - src/privacyfence/audit_log.py
  - .github/workflows/publish-pypi.yml
  - src/privacyfence/plugins/storage.py
  - plugin-sdk/LICENSE
  - plugin-sdk/NOTICE
  - plugin-sdk/pyproject.toml
  - tests/unit/plugin_sdk/test_packaging.py
  brief: |
    1. Design D9, each bullet exactly as written. Comments only in audit_log.py, publish-pypi.yml and storage.py.
    2. cp LICENSE plugin-sdk/LICENSE; cp NOTICE plugin-sdk/NOTICE; add license-files = ["LICENSE", "NOTICE"] under
       [project] in plugin-sdk/pyproject.toml, below license.
    3. tests/unit/plugin_sdk/test_packaging.py: each pair of files is byte-identical (repo root via
       Path(__file__).resolve().parents[3]).
    4. Run python3 -m build plugin-sdk and check unzip -l plugin-sdk/dist/*.whl lists LICENSE and NOTICE under the
       dist-info; then delete plugin-sdk/dist and any build/ or *.egg-info the build left (do not commit them).

    No CHANGELOG.md line in this phase. No plan item IDs in code, comments or test names.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk/test_packaging.py tests/unit/test_audit_log.py -q passes
  - git diff origin/feature/plugin-framework-polish...HEAD -- .github/workflows/publish-pypi.yml | grep -E '^[+-][^+-]' | grep -vE '^[+-]\s*#' prints nothing (comment lines only)
  - git status --porcelain plugin-sdk prints nothing after the commit
- id: p8-retire
  title: Reference docs, changelog, and retire the plan
  depends_on:
  - p1-card-text
  - p2-outputs-drive
  - p3-settings-hint
  - p5-sdk-names-errors
  - p6-testhost-introspect
  - p7-comments-license
  complexity: M
  touches:
  - docs/plugin-protocol.md
  - docs/plugins.md
  - plugin-sdk/README.md
  - CHANGELOG.md
  - tests/unit/plugins/test_protocol_doc.py
  - docs/plugin-framework-polish-plan.md
  - docs/plugin-framework-polish-plan-manual-steps.html
  - docs/README.md
  - scripts/build_site.py
  brief: |
    1. docs/plugin-protocol.md, docs/plugins.md and plugin-sdk/README.md: every bullet of Design D10, by section
       heading. Read the dataclasses in jira_client.py, calendar_client.py and confluence_client.py for the Record
       shapes tables; Notes only from the dataclasses' own comments.
    2. tests/unit/plugins/test_protocol_doc.py: Design D10's test.
    3. CHANGELOG.md: Design D10's lines under ## [Unreleased], in its existing ### sections (add a ### Changed or
       ### Security section in Keep a Changelog order if there is none). No version heading.
    4. Delete docs/plugin-framework-polish-plan.md and docs/plugin-framework-polish-plan-manual-steps.html; remove this
       plan's entries from docs/README.md and scripts/build_site.py CONTRIBUTOR_DOCS.
    5. Run the acceptance commands below.
    Stop condition: a section named in D10 is missing (plan Risks, last bullet).

    No plan item IDs in code, comments, test names or the reference docs.
  acceptance:
  - python3 -m pytest tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py tests/unit/plugins/test_protocol_doc.py -q passes
  - grep -rn plugin-framework-polish-plan docs scripts README.md prints nothing
  - grep -n 'Record shapes' docs/plugin-protocol.md prints one line
  - grep -n 'introspect' plugin-sdk/README.md prints at least one line
```
