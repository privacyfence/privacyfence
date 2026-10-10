# Plan: web-server shutdown fixes and type-check cleanup (#864, #865, #866, #867)

## Goal

Four follow-ups noted while the plugin framework was being built. Each one makes CI quieter or
more trustworthy, and one makes stopping the daemon faster:

- [#864](https://github.com/privacyfence/privacyfence/issues/864): `tests/unit/test_companion.py`
  stops patching `threading.Thread` for the whole process.
- [#866](https://github.com/privacyfence/privacyfence/issues/866): each `WebServer` keeps its own
  event loop. A late shutdown of one server can no longer clear another server's loop.
- [#867](https://github.com/privacyfence/privacyfence/issues/867): an open event stream ends as soon
  as the server is asked to stop, instead of holding shutdown until uvicorn's 2 s grace runs out.
  A slow stop logs how many connections were open.
- [#865](https://github.com/privacyfence/privacyfence/issues/865): the whole-tree
  `mypy src/privacyfence` run becomes clean and blocking. `ruff format` is documented as not
  enforced. The docs say which static checks are enforced.

Everything lands in one PR that closes all four issues.

## Current state

Measured on `main` at `eca5130d` (5.6.0, after the plugin framework was parked off `main` in
`6142f9de`).

### #864: companion tests patch the global `Thread`

`tests/unit/test_companion.py:420`, `:1256` and `:1275` call
`monkeypatch.setattr(companion.threading, "Thread", _Thread)`. `companion.threading` *is* the
`threading` module, so this replaces `threading.Thread` for every thread started anywhere in the
process while the test runs, pytest-timeout's timer thread included. The issue's own "done when"
grep (`patch("threading.Thread"`) already finds nothing on `main`; the real pattern is the
`setattr` above. `src/privacyfence/companion.py` imports `threading` at line 97. Besides `Thread`
it also uses `threading.Event` (lines 180, 819, 900, 912), so the module object that replaces it
must keep the module's other names.

The plugin branch fixed the same problem in `tests/unit/plugins/test_host.py` (commit `504b35a9`,
on `feature/plugin-framework-parked`, line 1801), and that fix is the pattern to copy:

```python
monkeypatch.setattr(host_mod, "threading", SimpleNamespace(**{**vars(threading), "Thread": FakeThread}))
```

No other test patches `Thread`: `grep -rn '"Thread"' tests` finds only the three lines above.

### #866: `wait_until_ready` returns a module global

- `src/privacyfence/web/state_stream.py:60` keeps one module-level `_loop`, with `set_loop` (63),
  `get_loop` (68) and `call_soon_threadsafe` (78). `call_soon_threadsafe` is the process-wide
  `settings_controller.set_main_dispatcher` target (`server.py:1039`). That is why the global
  exists at all, and it stays.
- `src/privacyfence/web/server.py:813` `_state_stream_loop_lifespan(ready_event)` calls
  `_state_stream.set_loop(loop)` on startup and `_state_stream.set_loop(None)` in its `finally`.
  The `finally` clears the global unconditionally.
- `server.py:1440` `WebServer.wait_until_ready()` waits on `self._loop_ready`, then returns
  `_state_stream.get_loop()`, which is the global and not this server's loop.
- So with server A started, then server B, then A's shutdown finishing, the global is `None`.
  `B.wait_until_ready()` returns `None`, and `call_soon_threadsafe` stops marshalling onto B's
  loop and runs callbacks inline instead.
- Callers: `daemon_main.py:1942` (`web_loop = server.wait_until_ready(timeout=5)`), and the fake in
  `tests/unit/test_daemon_main.py:2500`. `tests/conftest.py:65` resets `state_stream._loop = None`
  between tests.

### #867: open event streams hold shutdown for the full grace

- `server.py:147` sets `SHUTDOWN_GRACE_SECONDS = 2.0`, passed to uvicorn as
  `timeout_graceful_shutdown` (`server.py:1407`). `WebServer.stop()` (`server.py:1481`) sets
  `should_exit`, joins for grace + 5 s, then forces the exit.
- uvicorn 0.54's graceful shutdown closes idle keep-alive connections at once, but it waits for
  every in-flight response. An SSE response is in flight until its generator ends.
- `StateStream.subscribe` (`state_stream.py:145`) loops until `is_disconnected()` or `touch()`
  fails, waking every `_APPROVALS_POLL_SECONDS = 1.0` (`state_stream.py:51`). Nothing in it knows
  that the server is stopping. The org-mode stream, `approvals_stream` → `event_source` in
  `src/privacyfence/web/routes_approvals.py:594-631` (mounted by `_build_org_app`,
  `server.py:1068`), behaves the same way with `_STREAM_POLL_SECONDS = 1.0`
  (`routes_approvals.py:141`).
- So any open stream costs a full 2 s grace, after which uvicorn cancels the task and logs
  "Exception in ASGI application" with a `CancelledError`.
- Measured from the per-line timestamps of the `Tests` run on `eca5130d` (jobs `114151389759`
  and `114151389719`): `tests/unit/web/test_server.py::TestStop::test_an_open_event_stream_does_not_keep_the_server_running`
  takes 2.26 s on Windows and 2.56 s on macOS. The grace is reached on **every** platform, not
  only Windows. Its assertion (`test_server.py:981`) allows `SHUTDOWN_GRACE_SECONDS + 3`, so the
  test does not notice.
- The "every plugin integration test" in the issue referred to `tests/integration/test_plugin_framework.py`,
  which left `main` with the framework. Only the last 37% of each log could be read, and in it
  neither platform logs "Exception in ASGI application".
- Total runtime of that run: Windows 379 s, macOS 259 s.

### #865: static-check noise

- `.github/workflows/tests.yml:406-413`: the `static-analysis` job runs `mypy src/privacyfence` with
  `continue-on-error: true`. `:415-424` runs `python scripts/mypy_strict_modules.py` blocking.
- `mypy src/privacyfence` (mypy 2.4.0, the project's `[tool.mypy]` settings) reports **93 errors
  in 23 files**:

  | File | Errors | File | Errors |
  |---|---|---|---|
  | `daemon_main.py` | 29 | `connectors/salesforce.py` | 2 |
  | `slack_client.py` | 14 | `connectors/drive.py` | 2 |
  | `web/server.py` | 7 | `approval_ui.py` | 2 |
  | `telegram_client.py` | 7 | `web/routes_approvals.py` | 1 |
  | `connectors/apps_script.py` | 6 | `web/mcp_dispatch.py` | 1 |
  | `jira_client.py` | 4 | `resource_names.py` | 1 |
  | `atlassian_users.py` | 3 | `oauth_loopback.py` | 1 |
  | `web/routes_settings.py` | 2 | `google_errors.py` | 1 |
  | `policy/propose.py` | 2 | `email_markdown.py` | 1 |
  | `markdown_to_html.py` | 2 | `drive_client.py` | 1 |
  | `gate.py` | 2 | `approvals.py` | 1 |
  | | | `app_credentials.py` | 1 |

  By code: 29 `arg-type`, 25 `assignment`, 12 `attr-defined`, 9 `union-attr`, 8 `var-annotated`,
  and 10 others.
- 28 of `daemon_main.py`'s 29 errors come from one pattern. In `build_connectors`
  (`daemon_main.py:1263`), every connector block reuses the names `client` and `connector`, so
  mypy fixes their types from the Gmail block (`:1314`, `:1320`) and rejects every later block.
  The 29th (`:876`) passes a `LiveStepUpConfig` to `WebServer(step_up=...)`, typed
  `StepUpConfig | None` at `server.py:853` (`build_app`) and `server.py:1224` (`WebServer.__init__`).
  `LiveStepUpConfig` (`step_up_config.py:432`) stands in for a `StepUpConfig` by delegation.
- `ruff format --check .` (ruff 0.16.10) would reformat **354 of 575 files**: 130 of 135 in `src/`,
  202 of 243 in `tests/` and 22 of 27 in `scripts/`. It runs nowhere today.
- The `plugin-sdk/src` `mypy --strict` item in #865 no longer applies on `main`: the SDK left with
  the parked framework (`6142f9de`). It goes back with the framework.
- Places that describe the whole-tree run as informational, all of which change in p8:
  `.github/workflows/tests.yml:406-413`, `pyproject.toml:276-279` (`lint` extra comment) and
  `:361-396` (`[tool.mypy]` comments), `tests/unit/test_definition_of_done_drift.py:62-63`
  (exempts `mypy src/privacyfence` from the profile as "informational"),
  `scripts/mypy_strict_modules.py` module docstring, `scripts/pre_release_check.py:13-19`,
  `scripts/update_branch_protection.py:72-81` comment, `docs/coding-and-testing-guidelines.md`
  §1.9 (lines 147-163) and §2.7 (lines 348-354), `docs/testing-policy.md:58` and `:270`,
  `docs/release-testing.md:33-37`, `.github/pull_request_template.md:17-21`, and
  `.claude/toolkit.yaml` `verify.dod`.

## Design

### D1. Companion tests patch a module-local `threading` (#864)

In `tests/unit/test_companion.py`, each of the three
`monkeypatch.setattr(companion.threading, "Thread", _Thread)` lines becomes

```python
monkeypatch.setattr(companion, "threading", SimpleNamespace(**{**vars(threading), "Thread": _Thread}))
```

`threading` is already imported at the top of the test module (line 18). Add
`from types import SimpleNamespace` to the imports if it is not there. `companion.threading.Event`
and every other name keep working, because they are copied from the real module. Nothing in
`src/` changes.

### D2. Each `WebServer` keeps its own loop (#866)

- `state_stream.py`: add, next to `set_loop`:

  ```python
  def clear_loop(loop: asyncio.AbstractEventLoop) -> None:
      """Clear the captured loop only if it is still ``loop``, so a server whose shutdown finishes
      late never clears the loop a newer server in the same process captured since."""
      global _loop
      if _loop is loop:
          _loop = None
  ```

  `set_loop` and `get_loop` stay as they are, used by tests and by `call_soon_threadsafe`.
- `server.py` `_state_stream_loop_lifespan`: new signature
  `(ready_event: threading.Event | None = None, on_loop: Callable[[asyncio.AbstractEventLoop | None], None] | None = None)`.
  On startup: `loop = asyncio.get_running_loop()`, `_state_stream.set_loop(loop)`, then
  `if on_loop is not None: on_loop(loop)`, then `ready_event.set()` as today. In `finally`: first
  `if on_loop is not None: on_loop(None)`, then `_state_stream.clear_loop(loop)` (this replaces
  `_state_stream.set_loop(None)`). In the docstring, replace "Cleared on shutdown so a stale loop
  reference from a previous server instance ... is never mistaken for a live one." with "On
  shutdown it clears the module global only if it is still this app's loop (see
  ``state_stream.clear_loop``), so a late shutdown never clears a newer server's loop."
- `build_app`: new keyword parameter `on_loop: Callable[[asyncio.AbstractEventLoop | None], None] | None = None`,
  placed right after `loop_ready`, and passed on as `_state_stream_loop_lifespan(loop_ready, on_loop)`.
- `WebServer.__init__`: add `self._loop: asyncio.AbstractEventLoop | None = None` next to
  `self._loop_ready`, and pass `on_loop=self._set_loop` to `build_app`. Add the method

  ```python
  def _set_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
      self._loop = loop
  ```

- `WebServer.wait_until_ready`: return `self._loop` instead of `_state_stream.get_loop()`. Add one
  sentence to its docstring: "It is this server's own loop, never another server's."

Rejected: removing the module global outright. `settings_controller.set_main_dispatcher` registers
one plain function for the process (see the comment at `state_stream.py:55-59`), so the global
stays. Making its clear conditional is enough for the other half of the bug.

### D3. Event streams end when the server is asked to stop (#867)

- `state_stream.py`:
  - Add a module constant `_CLOSED_EVENT = "__closed__"`, which is never a real event name.
  - `StateStream.__init__` sets `self._closed = False`.
  - Add the method

    ```python
    def close(self) -> None:
        """End every open subscription and refuse new ones. Called on this stream's own loop by
        WebServer.stop() before uvicorn's graceful shutdown starts: an open SSE response would
        otherwise hold that shutdown for its whole grace period."""
        self._closed = True
        self._broadcast(_CLOSED_EVENT, None)
    ```

  - In `subscribe`, at the top of the `while True:` body, before `is_disconnected()`, add
    `if self._closed: break`. After `event, data = await asyncio.wait_for(...)`, add
    `if event == _CLOSED_EVENT: break` before `yield _sse(event, data)`.
- `routes_approvals.py`, `/api/approvals/stream`, in both modes. The stream lives in
  `_build_route_list` (`routes_approvals.py:478`; `approvals_stream` → `event_source` at
  `:594-631`). `create_app` (local mode, `:807`) and `build_routes` (org mode, `:952`) both call
  it, at `:927` and `:992`.
  - `_build_route_list`, `create_app` and `build_routes` each gain the keyword parameter
    `stopping: threading.Event | None = None`. `create_app` and `build_routes` pass it on as
    `stopping=stopping`.
  - In `event_source`, at the top of the `while True:` body, before `request.is_disconnected()`,
    add `if stopping is not None and stopping.is_set(): break`.
  - Add `import threading` to the module's imports if it is missing.
- `server.py`:
  - `build_app` gains `stopping: threading.Event | None = None`, placed right after `on_loop`. It
    passes `stopping=stopping` to `create_approvals_app(...)` (that is `routes_approvals.create_app`,
    imported at `server.py:127`) and to `_build_org_app(...)`. `_build_org_app` gains the same
    keyword parameter and passes it to `routes_approvals.build_routes(..., stopping=stopping)`.
  - `WebServer.__init__`: `self._stopping = threading.Event()`, passed as `stopping=self._stopping`
    to `build_app`.
  - `WebServer.stop()` becomes, in this order:
    1. `self._stopping.set()`.
    2. `loop = self._loop`. If `loop is not None` and `self.state_stream is not None`, call
       `loop.call_soon_threadsafe(self.state_stream.close)`, inside
       `try: ... except RuntimeError: pass`, because the loop may already be closed.
    3. `open_connections = len(self._server.server_state.connections)` and
       `started = time.monotonic()`.
    4. The existing `should_exit` / join / `force_exit` / still-running-warning block, unchanged.
    5. Right after that block, before the `control_channel` lines:
       `elapsed = time.monotonic() - started`. If `elapsed >= SHUTDOWN_GRACE_SECONDS`, log
       `logger.warning("The web server took %.1f s to stop; %d connection(s) were open when it was asked to", elapsed, open_connections)`.
       That is the diagnostic #867 asked for, and it shows up in any CI log where a slow stop
       still happens.
    6. The existing `control_channel` and `mcp_url` cleanup, unchanged.
  - Import `time` at module level if it is not already imported.
- The `/api/approvals/stream` loop wakes once a second (`_STREAM_POLL_SECONDS`), so it ends
  within 1 s of `stop()`, inside the grace. `/api/state/stream` is woken at once by `close()`.
  The reviewer simulated D3 against a real `WebServer`: `stop()` with an open
  `/api/state/stream` took 0.20 s (2.51 s today). With the grace patched to 0, the stop took
  0.15 s with `server_state.connections` at 1.
- `/mcp` (Streamable HTTP) is not changed, because its session manager has its own lifespan. If
  it turns out to hold shutdown, the warning in step 5 names it in CI, and that becomes a new
  issue.

Rejected: lowering `SHUTDOWN_GRACE_SECONDS` (it hides the cause and cuts a real slow response
short), and making the stream poll faster (it costs CPU all the time for a once-per-process
event).

### D4. The mypy fixes (#865)

Rules for p4 to p7:

1. Fix the type, not the check: add or correct an annotation, narrow with a condition, or rename
   a reused variable.
2. `typing.cast` is allowed where an invariant guarantees the type. The cast gets a short comment
   naming the invariant and where it is enforced (`file:line`).
3. `# type: ignore[<codes>]  # <reason>` is allowed only for a mismatch in third-party stubs or
   for a module generated at build time. Always give every error code on that line and a
   reason. Never write a bare `# type: ignore`.
4. Never `assert` in `src/` to narrow a type: Bandit flags it, and `python -O` strips it.
5. No `[[tool.mypy.overrides]]` with `ignore_errors`, and no new global mypy setting.
6. Runtime behaviour does not change, except in the two places marked **(runtime)** below,
   which are decided here. If any other fix would change what the code does at runtime (a branch
   taken, a value returned, an exception raised), stop with `status=blocked` and name the line.

Every error in `mypy src/privacyfence` on `eca5130d`, with its fix. Line numbers are from
`eca5130d`; in `web/server.py` and `web/routes_approvals.py` they move after p2/p3, so p7 finds
those lines by their content.

**p4: `daemon_main.py` (29)**

- 28 errors in `build_connectors` (`:1338`-`:1521`): rename each connector block's `client` to
  `<name>_client` and `connector` to `<name>_connector`, where `<name>` is `gmail`, `drive`,
  `calendar`, `contacts`, `tasks`, `apps_script`, `slack`, `salesforce`, `jira`, `confluence` or
  `telegram`. None of these names is bound in `daemon_main.py` today. A block that never binds
  `connector` (Salesforce appends `SalesforceConnector(client)` directly) renames only `client`.
- `:1606` (`add_done_callback`): `asyncio.run_coroutine_threadsafe` returns a
  `concurrent.futures.Future`. Annotate `_log_cache_warm_failure(future: "concurrent.futures.Future[None]")`
  and `import concurrent.futures`.
- `:876` (`step_up=local_step_up`, a `LiveStepUpConfig` where `StepUpConfig | None` is expected):
  pass `step_up=cast(StepUpConfig, local_step_up)` with the comment
  `# LiveStepUpConfig mirrors every StepUpConfig attribute by delegation (step_up_config.py:432)`.
  Import `cast` from `typing` and `StepUpConfig` from `.step_up_config` if they are missing.
  Rejected: widening `step_up` in `server.py`. It would cascade into `build_settings_routes`,
  `routes_security.build_routes`, `routes_approvals.create_app` and `local_enrollment_state`.

**p5: client modules (32)**

- `slack_client.py` (14):
  - `:205`: `{response.data}` becomes `{response.data!r}`. `str()` and `repr()` give the same
    text for a dict or bytes, so the output does not change.
  - `:542`, `:652` `page_raw`, and `:795` `raw_messages`: annotate `list[dict[str, Any]]`.
  - `:597`, `:720`, `:757`, `:1137`, `:1209` `raw`: annotate with the type of what is assigned to
    it later. That is `list[dict[str, Any]]` for a list of Slack objects and `dict[str, Any]` for
    one response. Read the assignment that follows to tell which.
  - `:681-682`: `oldest: str = None` and `latest: str = None` become `oldest: str | None = None`
    and `latest: str | None = None`. This also clears the two `:910` call-site errors.
  - `:1596` `members` (`no-redef`): in the cache-hit branch just above (`:1593`), rename the
    unpacked name to `cached_members` (`cached_members, fetched_at = cached` and
    `return cached_members`). Keep `members: list[str] = []` as it is.
- `telegram_client.py` (7): annotate `self._client: Any = None` at `:121` (telethon ships no
  types), and delete the seven `# type: ignore[union-attr]` comments on the
  `self._client.<method>(...)` lines.
- `jira_client.py` (4): `JiraIssue.display_description`, `JiraIssue.extra_fields` (`:161`, `:164`)
  and `JiraComment.display_body` (`:178`) are class attributes on purpose, so that `asdict()` (what
  the agent receives) never carries them. They must not become dataclass fields: that would fail
  `tests/unit/test_jira_client.py:624-629` and `tests/unit/connectors/test_jira_connector.py:1731-1732`.
  `ClassVar` does not work either, because mypy rejects assigning one through an instance.
  - Delete the three class-level `= None` lines and keep their comments.
  - Declare the attributes in a `__post_init__`:
    `JiraIssue.__post_init__(self) -> None` sets `self.display_description: str | None = None`
    and `self.extra_fields: dict[str, Any] | None = None`, and `JiraComment.__post_init__` sets
    `self.display_body: str | None = None`.
  - If either class already has a `__post_init__`, add the lines to it.
  - Stop condition: if `extra_fields` is ever assigned something other than a dict or None,
    stop with `status=blocked`.
- `atlassian_users.py` (3):
  - `:109`: bind `params: dict[str, str | int] = {"query": query, "maxResults": max_results}` and
    pass `params=params`.
  - `:147-148`: the `CDATA_CONTENT_ELEMENTS` and `RCDATA_CONTENT_ELEMENTS` overrides get
    `# type: ignore[misc]  # HTMLParser declares these Final; storage format has no CDATA elements`.
- `drive_client.py:1697`: `base64.binascii.Error` becomes `binascii.Error`, and add
  `import binascii`. It is the same class at runtime.
- `app_credentials.py:22`: `from . import _telegram_credentials` gets
  `# type: ignore[attr-defined]  # generated at build time; absent in a source checkout`.
- `google_errors.py:82`: bind
  `raw = getattr(getattr(http_error, "resp", None), "status", None)`, then
  `if raw is None: return None`, then `try: status = int(raw)` with the existing
  `except (TypeError, ValueError): return None`. The result is the same: today `int(None)`
  raises TypeError, which already returns None.
- `oauth_loopback.py:94`: `self.server_name = cast(str, host)  # AF_INET TCPServer: server_address is (str, int)`,
  with `cast` imported from `typing`.

**p6: connectors, policy, gate, approvals (17)**

- `connectors/apps_script.py:75-76` (6): the value is `ScriptFile | dict`. Replace each combined
  expression with `if isinstance(f, ScriptFile):` reading the attributes, and an `else:` reading
  `.get(...)` with the same keys and defaults the current expression uses. Use the loop
  variable's real name. Stop condition: if the current expression does something other than
  "attribute on ScriptFile, `.get` on dict", stop with `status=blocked`.
- `connectors/salesforce.py:196-200` (2): annotate `counts: list[Any] = [state.get(key) for key in ("n", "a", "r")]`.
  The `any(...)` check at `:195` already guarantees ints, and `last` is already narrowed to
  `str`.
- `connectors/drive.py` (2):
  - `:141`: annotate `table: dict[str, Any] = {"rows": ...}` at `:139`. The function already
    returns `dict`.
  - `:1572`: `base64.binascii.Error` becomes `binascii.Error`, and add `import binascii`.
- `policy/propose.py:601,623` (2): change `_rules_from_pairs`'s third parameter annotation from
  `dict[str, tuple[tuple[str, Any], ...]]` to `Mapping[str, tuple[tuple[str, Any], ...]]`. Import
  `Mapping` from wherever the module already imports typing names, or from `collections.abc`.
- `gate.py:1149,1275` (2): pass
  `cast(PendingApprovalRegistry, registry)  # _resolve_decision returns _PENDING only when registry is not None (gate.py:480)`.
- `approval_ui.py:147,150` (2): annotate `def _unconfigured(self) -> NoReturn:` (`:141`), with
  `from typing import NoReturn`. It always raises.
- `approvals.py:974` (1): `should_auto_accept(cast(str, approval.operation_key), ...)` with
  `# filtered to operation_key is not None just above (approvals.py:968)`.

**p7: web server, web routes, text helpers (15)**

- `web/server.py` (7):
  - `:493`: type the inner `send_with_headers` parameter as `message: Message`, with `Message`
    imported from `starlette.types`.
  - `:961`, `:1049`, `:1105`: annotate the local `extra_routes` variables in `build_app`
    (`:935`) and `_build_org_app` (`:1088`) as `list[BaseRoute]`, with `BaseRoute` from
    `starlette.routing`.
  - `:1398`: the `ProxyHeadersMiddleware` wrap has two errors on one line, so it gets
    `# type: ignore[arg-type,assignment]  # uvicorn's ASGI types are narrower than Starlette's`.
  - `:1407`: `SHUTDOWN_GRACE_SECONDS = 2.0` becomes `SHUTDOWN_GRACE_SECONDS = 2`. uvicorn types
    `timeout_graceful_shutdown` as `int | None`. Every use in the module and in tests is
    arithmetic or a comparison, so an int behaves the same.
- `web/routes_settings.py` (2):
  - `:774`: `parts = [p for p in parts if p]` becomes `present = [p for p in parts if p]`, and
    the return uses `present`.
  - **(runtime)** `:927`, `org_config_upload`: `form.get("csrf")` can be an `UploadFile`. Today
    that reaches `hmac.compare_digest` and raises TypeError, a 500. It becomes
    `csrf = form.get("csrf")` and
    `if not isinstance(csrf, str) or not _csrf_matches(request, csrf): return JSONResponse({"error": "unauthorized"}, status_code=401)`.
    A new test, `TestOrgConfigUpload.test_a_csrf_sent_as_a_file_is_unauthorized` in
    `tests/unit/web/test_routes_settings.py`, posts the token as a file part and expects 401.
- `web/routes_approvals.py:895` (1): the same `present = [...]` rename as `routes_settings.py:774`.
- `web/mcp_dispatch.py:238` (1): in the `if entry is not None:` branch, rename the unpacked
  `fut` to `existing` (`existing, recorded_at = entry`, `not existing.done()`,
  `return await existing`). Keep the later `fut: asyncio.Future = ...` as it is.
- `markdown_to_html.py:80,84` (2): replace each `if all(_X_RE.match(line) for line in lines):`
  block with
  `matches = [m for line in lines if (m := _X_RE.match(line))]`,
  `if len(matches) == len(lines):`, and items built from `m.group(1) for m in matches`. Do this
  for `_UL_ITEM_RE` and `_OL_ITEM_RE`, and keep the order and the HTML output exactly as they
  are.
- `email_markdown.py:104` (1): `item_match = bullet_match or numbered_match`,
  `if item_match is not None:`, `item_text = item_match.group(1)`.
- `resource_names.py:105` (1): `if fresh:` becomes `if fresh and hit is not None:`. `fresh`
  already implies it.

**(runtime)** also covers the `binascii` lines, but they are not a behaviour change.

### D5. What is enforced, written down (#865)

- The whole-tree run becomes blocking. In `tests.yml` the step is renamed
  `Type-check (mypy, whole tree, blocking)` and loses `continue-on-error`. The strict ratchet
  step stays as it is.
- `ruff format` is not enforced and is not added anywhere. §1.9 of the coding guidelines says so
  in this text, added as a paragraph at the end of §1.9's prose, before the Node/TypeScript part:

  > `ruff format` is not enforced and is not run in CI: it would rewrite most of the tree and
  > conflict with every open branch. Match the surrounding code's style instead
  > (`CONTRIBUTING.md`, "Code Style"). See ADR 0143.

- p9 turns "ADR 0143" (and p8's "ADR 0142" in §1.9) into links once the ADRs exist. The checks
  that make plain text necessary until then:
  - `tests/unit/test_docs_links.py` and `tests/unit/test_docs_references_exist.py` fail on a link
    or a `docs/adr/....md` path to a file that does not exist yet, and `verify.fast` runs them
    after every merge. So until p9, every mention of ADR 0142 or 0143 (docs, workflow and
    `pyproject.toml` comments, script docstrings) is plain text: "ADR 0142", never a path.
  - `tests/unit/test_docs_no_history.py` rejects issue numbers (`#865`) and
    "as of/since <version>" in the contributor docs, so no new doc text uses them.
- The enforced static checks, as §1.9 and every other listing will state them, are four:
  `ruff check .`, `bandit -c pyproject.toml -r src`, `mypy src/privacyfence` (whole tree, default
  strictness) and `python3 scripts/mypy_strict_modules.py` (strict flags for promoted modules).
- `tests/unit/test_definition_of_done_drift.py:62-63` exempts `mypy src/privacyfence` from the
  profile, because "§2.7 calls it informational". p8 removes that exemption: the profile then
  carries `python3 -m mypy src/privacyfence`, which contains the command.

### D6. Plan-document bookkeeping

`tests/unit/test_website_docs_allowlist.py` requires every `docs/*.md` to be listed as published
or as a contributor doc. The plan commit lists this plan in `scripts/build_site.py`
`CONTRIBUTOR_DOCS` and in `docs/README.md`'s contributor half, as `ff4ea4a6` did for the plugin
plans. p9 deletes the plan and removes both entries.

## ADRs

The plugin framework reserves 0136-0141 (`6142f9de`). The next free number is 0142.

- **ADR 0142: the whole-tree mypy run is blocking.** `mypy src/privacyfence` at the project's
  default settings gates every merge, alongside the per-module strict ratchet.
  - Rejected: keeping it informational, since #865 shows the noise hid new errors.
  - Rejected: only adding modules to the strict list one at a time, which leaves most of the
    tree unchecked for years.
  - Consequence: mypy is not pinned (`pyproject.toml` `lint` extra, `mypy>=1.10`), so a new mypy
    release can turn `main` red. The fix is then a follow-up PR, not a return to advisory.
- **ADR 0143: `ruff format` is not enforced.**
  - Rejected: one formatting-only PR plus `ruff format --check .` in CI. It touches 354 of 575
    files, conflicts with every open branch and with `feature/plugin-framework-parked`, and buys
    no correctness.
  - The `ruff check` rule set in `[tool.ruff.lint]` stays the style gate.

D2, D3 and the D4 fixes are implementation detail and need no ADR.

## Manual steps

None. Nothing needs a console, a secret or a real device.

- #867's "done when" (the Windows run) is checked by the PR's own `platform-windows` job, which
  runs the tightened `TestStop` test from p3.
- The connector live check that §2.7 owes for p5/p6 is dispatched by p9 (`connector-live-check.yml`
  from `ci.dispatchable`).

## Risks and open questions

- **p3, uvicorn internals.** `Server.server_state.connections` exists on uvicorn 0.54. If
  `python3 -c "import uvicorn.server as s; s.ServerState().connections"` fails in the worker's
  environment, stop with `status=blocked`.
- **p3, the tightened test.** If `test_an_open_event_stream_does_not_keep_the_server_running` still
  fails its new `< SHUTDOWN_GRACE_SECONDS` bound after D3, something besides the stream is holding
  shutdown. Do not loosen the bound: stop with `status=blocked` and paste the warning line from
  step 5.
- **p4 to p7, mypy version drift.** The counts were measured with mypy 2.4.0, the version `pip`
  resolves today. If a phase's files show errors D4 does not list, the worker fixes them under
  D4's rules. If such a fix needs a runtime change, the worker stops with `status=blocked`.
- **p8, branch protection.** The job name `static-analysis` does not change, so
  `scripts/update_branch_protection.py` needs only its comment edited. If
  `grep -rn "Type-check (mypy" .github scripts` matches anything outside `tests.yml`, stop with
  `status=blocked`.
- **p9, the live check.** `connector-live-check.yml` runs on the self-hosted runner and shares a
  concurrency group with `qa-record-fixture.yml`, so a queued run is normal. If dispatching is
  refused (no permission, 403), stop with `status=blocked`. The live check is owed under §2.7.

## Implementation manifest

```yaml
plan_slug: shutdown-and-type-check-cleanup
feature_branch: fix/shutdown-and-type-check-cleanup
max_parallel: 2
manual_before: []
manual_after: []
verify_after_merge:
  - |-
    python3 -m pytest tests/unit/test_companion.py tests/unit/web/test_server.py tests/unit/web/test_state_stream.py tests/unit/web/test_routes_org_approvals.py tests/unit/web/test_routes_settings.py tests/unit/test_daemon_main.py tests/unit/test_jira_client.py -q
  - |-
    python3 scripts/mypy_strict_modules.py
final_checks:
  - |-
    docs/shutdown-and-type-check-cleanup-plan.md is deleted and `grep -rn "shutdown-and-type-check-cleanup-plan" --include=*.md --include=*.py --include=*.yml --include=*.yaml .` finds nothing
  - |-
    docs/adr/0142-the-whole-tree-mypy-run-is-blocking.md and docs/adr/0143-ruff-format-is-not-enforced.md exist, each with Status "Accepted", and both are in docs/adr/README.md's index
  - |-
    `awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -c "live-update connection"` prints 1, and CHANGELOG.md has no new `## [X.Y.Z]` heading
  - |-
    `mypy src/privacyfence` exits 0, and `grep -n -B3 "run: mypy src/privacyfence" .github/workflows/tests.yml | grep -c continue-on-error` prints 0
  - |-
    `grep -rn 'setattr(companion.threading, "Thread"' tests/` finds nothing
  - |-
    The PR body links the connector-live-check.yml run p9 dispatched
phases:
  - id: p1-companion-thread-patch
    title: "#864: companion tests patch a module-local threading, not the global Thread"
    depends_on: []
    complexity: S
    touches:
      - tests/unit/test_companion.py
    brief: |
      Read the plan's "Current state → #864" and Design D1 first.
      1. In tests/unit/test_companion.py, replace each of the three lines
         `monkeypatch.setattr(companion.threading, "Thread", _Thread)` with
         `monkeypatch.setattr(companion, "threading", SimpleNamespace(**{**vars(threading), "Thread": _Thread}))`.
         They are in test_the_check_runs_off_the_startup_path (~420),
         test_the_recovery_item_runs_off_the_menu_thread (~1256) and
         test_the_service_actions_run_off_their_own_thread (~1275). `SimpleNamespace` and
         `threading` are already imported (lines ~18-22); add an import only if one is missing.
         Change nothing else in those tests.
      2. Run `python3 -m pytest tests/unit/test_companion.py -q`. Every test must pass. If one
         fails with an AttributeError on `companion.threading.<name>`, `vars(threading)` was not
         spread: fix the spread, do not add names by hand.
      3. Run `ruff check tests/unit/test_companion.py`.
      No CHANGELOG line (test-only). Commit message: "Patch companion's own threading in its
      tests, not the global Thread (#864)".
    acceptance:
      - |-
        python3 -m pytest tests/unit/test_companion.py -q passes
      - |-
        grep -rn 'setattr(companion.threading, "Thread"' tests/ prints nothing
      - |-
        grep -c 'setattr(companion, "threading", SimpleNamespace' tests/unit/test_companion.py prints 3
      - |-
        ruff check tests/unit/test_companion.py passes

  - id: p2-server-own-loop
    title: "#866: each WebServer keeps and returns its own event loop"
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/web/state_stream.py
      - src/privacyfence/web/server.py
      - tests/unit/web/test_state_stream.py
      - tests/unit/web/test_server.py
    brief: |
      Read the plan's "Current state → #866" and Design D2 first.
      1. Failing test first. In tests/unit/web/test_server.py, add a class
         `TestEachServerKeepsItsOwnLoop` right after `class TestStop`, with the test
         `test_a_late_shutdown_does_not_clear_a_newer_servers_loop(self, tmp_path, monkeypatch)`:
           - `monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)` (import
             `from privacyfence import paths` as TestStop's first test does).
           - Build `a` and `b`, each `WebServer(WebApprovalUI(), host="localhost", port=TestStop._free_port())`.
           - `a.start()`; `TestStop._wait_until_connectable("localhost", a.port)`;
             `loop_a = a.wait_until_ready()`; the same for `b` giving `loop_b`.
           - `a.stop()`. Then assert `b.wait_until_ready() is loop_b`, `loop_b is not loop_a`,
             `loop_b.is_running()`, and `state_stream.get_loop() is loop_b`
             (`from privacyfence.web import state_stream`).
           - Use try/finally so `b.stop()` always runs, and `a.stop()` runs in the finally only
             if it was not already called.
         Run it and see it fail on `b.wait_until_ready() is loop_b`: today it returns None. If
         `WebServer` has no `port` attribute, keep the port in a local variable instead.
      2. In tests/unit/web/test_state_stream.py, class TestCallOnMainDispatcher, add
         `test_clear_loop_leaves_a_different_loop_alone`:
           - `loop = asyncio.new_event_loop()`, `other = asyncio.new_event_loop()`, and close both
             in a finally.
           - `set_loop(loop)`, `clear_loop(other)`, assert `get_loop() is loop`.
           - `clear_loop(loop)`, assert `get_loop() is None`.
         Import `clear_loop` and `get_loop` with the existing state_stream imports.
      3. Implement D2 exactly:
           - `clear_loop` in src/privacyfence/web/state_stream.py.
           - In src/privacyfence/web/server.py: `_state_stream_loop_lifespan(ready_event, on_loop)`
             and its docstring change, the `on_loop` parameter on `build_app` (after
             `loop_ready`), `WebServer._loop`, `WebServer._set_loop`, `on_loop=self._set_loop`,
             and `wait_until_ready` returning `self._loop`.
           - Add `on_loop` to build_app's docstring, next to wherever `loop_ready` is described,
             in this sentence: "``on_loop`` is called with this app's loop once it is captured,
             and with ``None`` on shutdown; WebServer keeps it as its own."
      4. Run `python3 -m pytest tests/unit/web tests/unit/test_daemon_main.py -q`. All tests pass.
      5. `ruff check .` and `python3 scripts/mypy_strict_modules.py` pass.
      6. web/server.py's lifecycle changed, so run
         `python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -q`.
         Both files pass; a skip counts only if they also skip on main.
      No CHANGELOG line: the daemon runs one server, so nothing changes for users. Commit
      message: "Keep each web server's own event loop (#866)".
    acceptance:
      - |-
        python3 -m pytest "tests/unit/web/test_server.py::TestEachServerKeepsItsOwnLoop::test_a_late_shutdown_does_not_clear_a_newer_servers_loop" -q passes
      - |-
        python3 -m pytest "tests/unit/web/test_state_stream.py::TestCallOnMainDispatcher::test_clear_loop_leaves_a_different_loop_alone" -q passes
      - |-
        python3 -m pytest tests/unit/web tests/unit/test_daemon_main.py -q passes
      - |-
        grep -n "return _state_stream.get_loop()" src/privacyfence/web/server.py prints nothing
      - |-
        grep -n "_state_stream.set_loop(None)" src/privacyfence/web/server.py prints nothing
      - |-
        ruff check . passes

  - id: p3-streams-end-on-stop
    title: "#867: open event streams end when the server is asked to stop; slow stops are logged"
    depends_on: [p2-server-own-loop]
    complexity: M
    touches:
      - src/privacyfence/web/state_stream.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_approvals.py
      - tests/unit/web/test_state_stream.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_routes_org_approvals.py
      - CHANGELOG.md
    brief: |
      Read the plan's "Current state → #867" and Design D3 first. p2 is merged, so WebServer
      has `self._loop`.
      1. Tighten the existing test first. In tests/unit/web/test_server.py,
         TestStop.test_an_open_event_stream_does_not_keep_the_server_running:
           - Change `assert time.monotonic() - started < SHUTDOWN_GRACE_SECONDS + 3` to
             `assert time.monotonic() - started < SHUTDOWN_GRACE_SECONDS`.
           - Add the `caplog` fixture, wrap `server.stop()` in
             `with caplog.at_level("WARNING", logger="privacyfence.web.server"):`, and assert
             `"The web server took" not in caplog.text`.
         Run it: it fails today, at about 2.3 s.
      2. Add to TestStop `test_a_slow_stop_is_logged_with_the_open_connection_count(self, tmp_path, monkeypatch, caplog)`:
           - `from privacyfence.web import server as server_module` inside the test, then
             `monkeypatch.setattr(server_module, "SHUTDOWN_GRACE_SECONDS", 0)`.
           - Start a server, wait until it is connectable, and open the same raw-socket SSE
             request as the test above.
           - Call `server.stop()` under
             `caplog.at_level("WARNING", logger="privacyfence.web.server")`.
           - Assert that `"The web server took"` and `"1 connection(s) were open"` are both in
             `caplog.text`.
         uvicorn also logs an ERROR "Cancel 0 running task(s)" from its own logger when the
         grace is 0; that is expected, so do not assert on it. If the count is not 1, stop with
         status=blocked and paste caplog.text. Do not loosen the assertion.
      3. In tests/unit/web/test_state_stream.py, add a class `TestClose`. Build each stream as
         `StateStream(settings_snapshot=lambda: {"a": 1}, list_pending=lambda: [])`, so that
         subscribe yields exactly two initial events (settings, then approvals), and use the
         file's `_Disconnector(after=10**6)` as is_disconnected. Add:
           - `test_close_ends_an_open_subscription`: `gen = stream.subscribe(_Disconnector(after=10**6))`,
             `await gen.__anext__()` twice, `stream.close()`, then assert
             `await asyncio.wait_for(gen.__anext__(), 0.5)` raises StopAsyncIteration.
           - `test_a_subscription_opened_after_close_ends_after_its_initial_events`: call
             `stream.close()` first, subscribe, read the two initial events, and assert the next
             `__anext__()` (inside `asyncio.wait_for(..., 0.5)`) raises StopAsyncIteration.
      4. In tests/unit/web/test_routes_org_approvals.py:
           - Extend the module's `_app()` helper with a keyword `stopping=None` that it passes to
             `routes_approvals.build_routes(..., stopping=stopping)`.
           - In class TestApprovalsStream, add `test_stream_ends_once_the_server_is_stopping`,
             modelled on `test_stream_ends_once_the_session_is_gone`: `stopping = threading.Event()`,
             `stopping.set()`, `app, sessions, web_ui = _app(stopping=stopping)`, then
             `endpoint, request = self._stream(app, sessions.create(ALICE))`,
             `response = await endpoint(request)`, and assert
             `await response.body_iterator.__anext__()` raises StopAsyncIteration.
             Import threading if missing.
      5. Implement D3 exactly:
           - src/privacyfence/web/state_stream.py: `_CLOSED_EVENT`, `self._closed`, `close()`,
             and the two checks in `subscribe`.
           - src/privacyfence/web/routes_approvals.py: `stopping` on `_build_route_list`,
             `create_app` and `build_routes`, passed through, and the check in `event_source`.
             Add this sentence to build_routes' and create_app's docstrings: "``stopping``, when
             set, ends ``/api/approvals/stream`` on its next tick, so a server that is shutting
             down is not held for its graceful-shutdown period."
           - src/privacyfence/web/server.py: `stopping` on `build_app` (passed to
             `create_approvals_app` and `_build_org_app`) and on `_build_org_app` (passed to
             `routes_approvals.build_routes`), `self._stopping`, and the new `stop()` steps 1-6.
      6. Under CHANGELOG.md's `## [Unreleased]`, add a `### Fixed` subsection if there is none,
         with exactly this entry, wrapped at 100 columns like the 5.6.0 "Fixed" entry:
         "- Stopping PrivacyFence with a settings or approvals tab open no longer waits two
         seconds for the tab's live-update connection: the connection now ends as soon as the
         web server is asked to stop."
         Never add a `## [X.Y.Z]` heading.
      7. `python3 -m pytest tests/unit/web tests/unit/test_daemon_main.py -q`: all pass.
      8. web/server.py's lifecycle changed, so run
         `python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -q`.
         It passes.
      9. `ruff check .`, `bandit -c pyproject.toml -r src` and
         `python3 scripts/mypy_strict_modules.py` pass.
      Commit message: "End open event streams as soon as the web server is asked to stop (#867)".
    acceptance:
      - |-
        python3 -m pytest tests/unit/web/test_server.py::TestStop -q passes
      - |-
        grep -n "SHUTDOWN_GRACE_SECONDS + 3" tests/unit/web/test_server.py prints nothing
      - |-
        python3 -m pytest tests/unit/web/test_state_stream.py::TestClose -q passes
      - |-
        python3 -m pytest "tests/unit/web/test_routes_org_approvals.py::TestApprovalsStream::test_stream_ends_once_the_server_is_stopping" -q passes
      - |-
        grep -c "stopping=stopping" src/privacyfence/web/routes_approvals.py prints at least 2, and grep -c "stopping=stopping" src/privacyfence/web/server.py prints at least 3
      - |-
        python3 -m pytest tests/unit/web tests/unit/test_daemon_main.py -q passes
      - |-
        grep -n "connection(s) were open when it was asked to" src/privacyfence/web/server.py matches
      - |-
        awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -c "live-update connection" prints 1
      - |-
        ruff check . and bandit -c pyproject.toml -r src pass

  - id: p4-mypy-daemon-main
    title: "#865: daemon_main.py passes mypy"
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/daemon_main.py
    brief: |
      Read Design D4: the rules, then "p4: daemon_main.py".
      1. `mypy src/privacyfence 2>&1 | grep '^src/privacyfence/daemon_main.py:.*error'` shows 29
         errors: 28 in build_connectors, one at ~876 (step_up) and one at ~1606.
      2. Apply D4's p4 fixes exactly: the per-block renames in `build_connectors` (rename
         nothing outside it), the `_log_cache_warm_failure` annotation with
         `import concurrent.futures`, and the `cast(StepUpConfig, local_step_up)` at ~876 with
         its comment. Keep imports sorted.
      3. Re-run step 1's command: it prints nothing. If an error remains that D4 does not cover,
         fix it under D4's rules, or stop with status=blocked if the fix needs a runtime change.
      4. `python3 -m pytest tests/unit/test_daemon_main.py -q` passes with no test changed.
         `ruff check .` and `bandit -c pyproject.toml -r src` pass.
      No CHANGELOG line. Commit message: "Give each connector block its own names so mypy can
      type build_connectors (#865)".
    acceptance:
      - |-
        mypy src/privacyfence 2>&1 | grep -c '^src/privacyfence/daemon_main.py:.*error' prints 0
      - |-
        python3 -m pytest tests/unit/test_daemon_main.py -q passes
      - |-
        ruff check . and bandit -c pyproject.toml -r src pass

  - id: p5-mypy-clients
    title: "#865: the client modules pass mypy"
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/slack_client.py
      - src/privacyfence/telegram_client.py
      - src/privacyfence/jira_client.py
      - src/privacyfence/atlassian_users.py
      - src/privacyfence/drive_client.py
      - src/privacyfence/app_credentials.py
      - src/privacyfence/google_errors.py
      - src/privacyfence/oauth_loopback.py
    brief: |
      Read Design D4: the rules, then "p5: client modules".
      1. `mypy src/privacyfence 2>&1 | grep -E '^src/privacyfence/(slack_client|telegram_client|jira_client|atlassian_users|drive_client|app_credentials|google_errors|oauth_loopback)\.py:.*error'`
         shows 32 errors: slack_client 14, telegram_client 7, jira_client 4, atlassian_users 3,
         and 1 each in the other four.
      2. Apply every D4 p5 fix exactly, file by file. For jira_client.py, keep the comments that
         explain why the three attributes are not dataclass fields, and move them next to the
         new `__post_init__` lines.
      3. Re-run step 1's command: it prints nothing. Any extra error goes under D4's rules.
      4. `python3 -m pytest tests/unit -q` passes. Watch tests/unit/test_jira_client.py and
         tests/unit/connectors/test_jira_connector.py in particular: an `asdict()` that now
         includes display_description/extra_fields/display_body means the jira fix is wrong.
         `ruff check .` and `bandit -c pyproject.toml -r src` pass.
      This phase touches *_client.py. The §2.7 live check is dispatched once for the whole
      branch by p9, so do not dispatch it here. No CHANGELOG line. Commit message: "Make the
      client modules pass mypy (#865)".
    acceptance:
      - |-
        mypy src/privacyfence 2>&1 | grep -cE '^src/privacyfence/(slack_client|telegram_client|jira_client|atlassian_users|drive_client|app_credentials|google_errors|oauth_loopback)\.py:.*error' prints 0
      - |-
        grep -nE "type: ignore($|[^[])" src/privacyfence/slack_client.py src/privacyfence/telegram_client.py src/privacyfence/jira_client.py src/privacyfence/atlassian_users.py src/privacyfence/app_credentials.py prints nothing
      - |-
        python3 -m pytest tests/unit -q passes
      - |-
        ruff check . and bandit -c pyproject.toml -r src pass

  - id: p6-mypy-connectors-and-core
    title: "#865: connectors, policy, gate and approvals pass mypy"
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/connectors/apps_script.py
      - src/privacyfence/connectors/salesforce.py
      - src/privacyfence/connectors/drive.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/gate.py
      - src/privacyfence/approvals.py
      - src/privacyfence/approval_ui.py
    brief: |
      Read Design D4: the rules, then "p6: connectors, policy, gate, approvals".
      1. `mypy src/privacyfence 2>&1 | grep -E '^src/privacyfence/(connectors/apps_script|connectors/salesforce|connectors/drive|policy/propose|gate|approvals|approval_ui)\.py:.*error'`
         shows 17 errors: apps_script 6, salesforce 2, drive 2, propose 2, gate 2,
         approval_ui 2, approvals 1.
      2. Apply every D4 p6 fix exactly, file by file.
      3. Re-run step 1's command: it prints nothing. Any extra error goes under D4's rules.
      4. `python3 -m pytest tests/unit -q` passes. `ruff check .` and
         `bandit -c pyproject.toml -r src` pass.
      This phase touches connectors/**. The §2.7 live check is dispatched once by p9, so do not
      dispatch it here. No CHANGELOG line. Commit message: "Make connectors, policy, gate and
      approvals pass mypy (#865)".
    acceptance:
      - |-
        mypy src/privacyfence 2>&1 | grep -cE '^src/privacyfence/(connectors/apps_script|connectors/salesforce|connectors/drive|policy/propose|gate|approvals|approval_ui)\.py:.*error' prints 0
      - |-
        grep -n "def _unconfigured(self) -> NoReturn" src/privacyfence/approval_ui.py matches
      - |-
        python3 -m pytest tests/unit -q passes
      - |-
        ruff check . and bandit -c pyproject.toml -r src pass

  - id: p7-mypy-web-and-text
    title: "#865: web server, web routes and text helpers pass mypy"
    depends_on: [p3-streams-end-on-stop]
    complexity: S
    touches:
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_approvals.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/markdown_to_html.py
      - src/privacyfence/email_markdown.py
      - src/privacyfence/resource_names.py
      - tests/unit/web/test_routes_settings.py
    brief: |
      Read Design D4: the rules, then "p7: web server, web routes, text helpers". p3 is merged.
      Line numbers in web/server.py and web/routes_approvals.py have moved, so find those lines
      by their content.
      1. `mypy src/privacyfence 2>&1 | grep -E '^src/privacyfence/(web/server|web/routes_approvals|web/routes_settings|web/mcp_dispatch|markdown_to_html|email_markdown|resource_names)\.py:.*error'`
         shows 15 errors: server 7, routes_settings 2, markdown_to_html 2, routes_approvals 1,
         mcp_dispatch 1, email_markdown 1, resource_names 1.
      2. Failing test first, for the one runtime change. In
         tests/unit/web/test_routes_settings.py, class TestOrgConfigUpload, add
         `test_a_csrf_sent_as_a_file_is_unauthorized(self, client, sessions)`:
           - `_authed(client, sessions)`.
           - `r = client.post("/api/settings/org_config/upload", files={"csrf": ("csrf.txt", b"x", "text/plain"), "file": ("org_config.json", b'{"version": 1}', "application/json")})`.
           - Assert `r.status_code == 401`.
         Today it fails with a 500 (or raises in the TestClient).
      3. Apply every D4 p7 fix exactly, file by file, including the **(runtime)** CSRF change in
         `org_config_upload`.
      4. Re-run step 1's command: it prints nothing. Then run `mypy src/privacyfence`. Once p4,
         p5 and p6 are merged too, it reports "Success: no issues found"; until then, the only
         errors left are in those phases' files.
      5. `python3 -m pytest tests/unit -q` passes. web/server.py changed, so run
         `python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -q`;
         it passes. `ruff check .`, `bandit -c pyproject.toml -r src` and
         `python3 scripts/mypy_strict_modules.py` pass.
      6. Add a line to CHANGELOG.md? No: the CSRF change turns a 500 into a 401 for a malformed
         request that no page sends, so it is not user-visible.
      Commit message: "Make the web server, web routes and text helpers pass mypy (#865)".
    acceptance:
      - |-
        mypy src/privacyfence 2>&1 | grep -cE '^src/privacyfence/(web/server|web/routes_approvals|web/routes_settings|web/mcp_dispatch|markdown_to_html|email_markdown|resource_names)\.py:.*error' prints 0
      - |-
        python3 -m pytest "tests/unit/web/test_routes_settings.py::TestOrgConfigUpload::test_a_csrf_sent_as_a_file_is_unauthorized" -q passes
      - |-
        grep -n "^SHUTDOWN_GRACE_SECONDS = 2$" src/privacyfence/web/server.py matches
      - |-
        python3 -m pytest tests/unit -q passes
      - |-
        ruff check ., bandit -c pyproject.toml -r src and python3 scripts/mypy_strict_modules.py pass

  - id: p8-mypy-blocking-and-docs
    title: "#865: the whole-tree mypy run blocks in CI, and the docs say which checks are enforced"
    depends_on: [p4-mypy-daemon-main, p5-mypy-clients, p6-mypy-connectors-and-core, p7-mypy-web-and-text]
    complexity: S
    touches:
      - .github/workflows/tests.yml
      - pyproject.toml
      - scripts/mypy_strict_modules.py
      - scripts/pre_release_check.py
      - scripts/update_branch_protection.py
      - docs/coding-and-testing-guidelines.md
      - docs/testing-policy.md
      - docs/release-testing.md
      - .github/pull_request_template.md
      - .claude/toolkit.yaml
      - tests/unit/test_definition_of_done_drift.py
    brief: |
      Read Design D5 and the plan's "Current state → #865" list of places first. Until p9, cite
      ADR 0142 and ADR 0143 as plain text only, never as a link or a docs/adr/ path (D5). Do not
      write issue numbers or "since <version>" into docs/ files.
      1. `mypy src/privacyfence` prints "Success: no issues found". If not, stop with
         status=blocked and paste the errors: an earlier phase is incomplete.
      2. .github/workflows/tests.yml, static-analysis job:
           - Rename the step `Type-check (mypy, informational)` to
             `Type-check (mypy, whole tree, blocking)`.
           - Delete its `continue-on-error: true` line.
           - Replace the comment above it with these two lines:
             "# Blocking: the whole tree at [tool.mypy]'s default settings (ADR 0142). The strict"
             "# flags for promoted modules are the next step's job."
           - Keep the command `mypy src/privacyfence`.
         Then run `grep -rn "Type-check (mypy" .github scripts`. If anything other than tests.yml
         matches, stop with status=blocked.
      3. pyproject.toml:
           a. The `lint` extra's comment (~276-279): replace "mypy runs in CI twice --
              non-blocking over the whole tree, and blocking over the modules promoted by the
              [[tool.mypy.overrides]] ratchet below" with "mypy runs in CI twice, both blocking:
              over the whole tree, and with strict flags over the modules promoted by the
              [[tool.mypy.overrides]] ratchet below".
           b. The `[tool.mypy]` comment (~363-382): replace the paragraph from "mypy is adopted
              incrementally" through "...gated nothing at all.)" with a comment that says:
              - the whole tree is checked at these settings and blocks the merge (ADR 0142);
              - the `[[tool.mypy.overrides]]` blocks below add strict flags to promoted modules,
                which `scripts/mypy_strict_modules.py` checks, also blocking, in the next CI
                step;
              - see url_safety.py's override for why the flags are spelled out.
           c. Any sentence further down (~395-396) that speaks of "chasing the whole-tree error
              count to zero" or of the whole-tree run being advisory: reword it to say the
              whole tree is already clean at the default settings.
         Keep every override block and its own settings unchanged.
      4. scripts/mypy_strict_modules.py, module docstring only:
           - In "This script is what makes promotion blocking...", replace "beside the
             informational whole-tree run, so the ratchet's promoted modules genuinely gate the
             merge while the ~87 pre-existing errors in the rest of the tree stay
             visible-but-advisory, exactly as before." with "beside the whole-tree run (also
             blocking, ADR 0142), so the promoted modules are held to the strict flags on top
             of the defaults the whole tree must meet."
           - In "mypy is meant to go ...", replace the sentence saying the whole-tree step is
             `continue-on-error: true` with "The whole-tree mypy step checks only the default
             settings, so on its own".
           - In the `--follow-imports=silent` paragraph, replace "dragging the whole tree's
             pre-existing findings into a blocking step and making promotion impossible" with
             "reporting strict-flag findings for modules that are not promoted".
      5. scripts/pre_release_check.py: after the `results["mypy (promoted modules)"]` entry, add
         `results["mypy (whole tree)"] = run("mypy (whole tree)", ["python3", "-m", "mypy", "src/privacyfence"], cwd=REPO_ROOT)`,
         formatted like its neighbours. Rewrite the module docstring's paragraph (~13-19) to say
         that `ruff check .`, `bandit`, `mypy src/privacyfence` and
         `scripts/mypy_strict_modules.py` are all included because all four block in CI.
         Write `mypy src/privacyfence` in backticks.
      6. scripts/update_branch_protection.py comment (~72-81): replace the sentence about the
         whole-tree step's `continue-on-error` with one saying that all of static-analysis's
         steps (ruff, the whole-tree mypy run, the promoted-module mypy run, bandit) block, so
         requiring the job requires all four. Change no code.
      7. docs/coding-and-testing-guidelines.md:
           a. §1.9: insert `mypy src/privacyfence` as a new line between `ruff check .` and
              `python3 scripts/mypy_strict_modules.py` in the command block.
           b. Rewrite the paragraph after the block. It says that Ruff (`ruff check .`),
              Bandit, the whole-tree mypy run and the promoted-module mypy run are all blocking
              CI checks; that the whole tree is checked at `[tool.mypy]`'s settings (ADR 0142);
              and that promoting a module to the strict flags is still a one-block edit in
              `pyproject.toml`.
           c. Then add D5's `ruff format` paragraph verbatim.
           d. §2.7 "Every PR": add `mypy src/privacyfence` as a fourth command; change "blocks on
              all three" to "blocks on all four"; and replace "the whole-tree `mypy
              src/privacyfence` run in that same job is informational only, while the modules
              with a `[[tool.mypy.overrides]]` entry are what the third command checks" with
              "`mypy src/privacyfence` checks the whole tree at the default settings, and the
              modules with a `[[tool.mypy.overrides]]` entry are what
              `scripts/mypy_strict_modules.py` checks with strict flags".
      8. .github/pull_request_template.md (~17-21): mirror §2.7's new wording, with the same
         four commands and no "informational" or "third command" left.
      9. docs/testing-policy.md:
           - The static-analysis row (~58) becomes "`ruff check .`,
             `bandit -c pyproject.toml -r src`, whole-tree `mypy src/privacyfence`,
             `scripts/mypy_strict_modules.py` (all blocking); `ruff format` is not run
             (ADR 0143)".
           - At ~270, add `mypy src/privacyfence` next to `mypy_strict_modules.py`.
      10. docs/release-testing.md (~35-36): add `mypy src/privacyfence` to the list of commands
          pre_release_check.py runs.
      11. .claude/toolkit.yaml `verify.dod`: insert `- python3 -m mypy src/privacyfence` right
          before `- python3 scripts/mypy_strict_modules.py`. Validate the file with
          `python3 -c "import yaml; yaml.safe_load(open('.claude/toolkit.yaml'))"`, and also with
          the devflow `validate_profile.py` if the toolkit is installed in the session.
      12. tests/unit/test_definition_of_done_drift.py: delete the `_NOT_IN_PROFILE` exemption
          for `mypy src/privacyfence` and its comment (~62-63), and any use of it that becomes
          dead. If other code depends on `_NOT_IN_PROFILE` being non-empty, keep the name as an
          empty `frozenset()`/`set()` of the same type, with no comment.
      13. Run `python3 -m pytest tests/unit/test_pre_release_check.py tests/unit/test_update_branch_protection.py tests/unit/test_definition_of_done_drift.py tests/unit/test_mypy_strict_modules.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q`
          (drop a file from the list only if it does not exist). It passes. `ruff check .` and
          `python3 scripts/mypy_strict_modules.py` pass.
      No CHANGELOG line (internal). Commit message: "Make the whole-tree mypy run blocking and
      write down which static checks are enforced (#865)".
    acceptance:
      - |-
        mypy src/privacyfence exits 0
      - |-
        grep -n -B3 "run: mypy src/privacyfence" .github/workflows/tests.yml | grep -c continue-on-error prints 0
      - |-
        grep -n "Type-check (mypy, whole tree, blocking)" .github/workflows/tests.yml matches
      - |-
        grep -n 'is not enforced and is not run in CI' docs/coding-and-testing-guidelines.md matches
      - |-
        grep -l "mypy src/privacyfence" .claude/toolkit.yaml scripts/pre_release_check.py .github/pull_request_template.md docs/testing-policy.md docs/release-testing.md lists all five files
      - |-
        grep -n '"src/privacyfence"' scripts/pre_release_check.py matches
      - |-
        grep -rni "informational" docs/coding-and-testing-guidelines.md docs/testing-policy.md .github/pull_request_template.md .github/workflows/tests.yml tests/unit/test_definition_of_done_drift.py | grep -i mypy prints nothing
      - |-
        grep -rn "adr/014[23]" docs/coding-and-testing-guidelines.md docs/testing-policy.md docs/release-testing.md pyproject.toml .github scripts prints nothing
      - |-
        python3 -m pytest tests/unit -q passes

  - id: p9-retire-plan
    title: "Write ADRs 0142 and 0143, dispatch the connector live check, and retire the plan"
    depends_on: [p1-companion-thread-patch, p8-mypy-blocking-and-docs]
    complexity: S
    touches:
      - docs/adr/0142-the-whole-tree-mypy-run-is-blocking.md
      - docs/adr/0143-ruff-format-is-not-enforced.md
      - docs/adr/README.md
      - docs/coding-and-testing-guidelines.md
      - docs/README.md
      - scripts/build_site.py
      - docs/shutdown-and-type-check-cleanup-plan.md
      - CHANGELOG.md
    brief: |
      Read the plan's ADRs section, Design D6, and docs/adr/README.md (rules and template)
      first.
      1. Write docs/adr/0142-the-whole-tree-mypy-run-is-blocking.md from the template:
           - Title: "ADR 0142: The whole-tree mypy run is blocking".
           - Status: "Accepted — <today's date>. Implemented in the PR that adds this ADR."
           - Context: `mypy src/privacyfence` reported 93 errors in 23 files, and the
             `continue-on-error` step let new errors hide among them (#865).
           - Decision: `mypy src/privacyfence` at `[tool.mypy]`'s settings blocks every merge,
             alongside the strict per-module ratchet in `scripts/mypy_strict_modules.py`, which
             is unchanged.
           - Rejected: keeping it informational, and promoting module by module only, each with
             the reason from the plan's ADRs section.
           - Consequences: mypy is not pinned, so a new release can turn main red; the answer is
             a fix PR, not a return to `continue-on-error`.
         Link #865 as https://github.com/privacyfence/privacyfence/issues/865, and link
         `../../.github/workflows/tests.yml` and `../../scripts/mypy_strict_modules.py` as
         relative paths. Never link the plan document.
      2. Write docs/adr/0143-ruff-format-is-not-enforced.md the same way:
           - Title: "ADR 0143: `ruff format` is not enforced".
           - Context: `ruff format --check .` would rewrite 354 of 575 files (#865).
           - Decision: it is not run in CI and not required. `ruff check` with
             `[tool.ruff.lint]`'s rules stays the style gate, and contributors match the
             surrounding code.
           - Rejected: one formatting-only PR plus `ruff format --check .` in CI, because of the
             churn and the conflicts with every open branch and `feature/plugin-framework-parked`.
           - Consequence: revisit when no long-lived branch is open.
         Link `../../pyproject.toml` for `[tool.ruff.lint]`.
      3. Add both to the index table in docs/adr/README.md after 0135, with "Accepted". Do not
         add rows for 0136-0141: they stay reserved for the parked plugin framework.
      4. In docs/coding-and-testing-guidelines.md §1.9, turn p8's plain-text "ADR 0142" and
         "ADR 0143" into `[ADR 0142](adr/0142-the-whole-tree-mypy-run-is-blocking.md)` and
         `[ADR 0143](adr/0143-ruff-format-is-not-enforced.md)`. Leave the plain-text mentions
         in workflow, pyproject.toml, script and testing-policy text as they are.
      5. D6: delete docs/shutdown-and-type-check-cleanup-plan.md (`git rm`).
           - Remove its entry and comment from `CONTRIBUTOR_DOCS` in scripts/build_site.py.
           - Remove the "Open plan, deleted by its own last phase" bullet from docs/README.md's
             contributor half.
           - `grep -rn "shutdown-and-type-check-cleanup-plan" --include=*.md --include=*.py --include=*.yml --include=*.yaml .`
             must find nothing.
      6. The §2.7 live check owed for p5/p6 (they changed *_client.py and connectors/**):
           - Dispatch `connector-live-check.yml` (no inputs) on this phase's branch, with the
             GitHub MCP workflow-dispatch tool, as the pr-steward skill and ci.dispatchable in
             .claude/toolkit.yaml describe.
           - It shares a concurrency group with qa-record-fixture.yml, so a queued run is
             normal.
           - Wait for it to finish, and put the run URL and its conclusion in your phase report
             so the orchestrator can link it in the PR body.
           - If dispatching is refused, stop with status=blocked. If the run fails, report the
             failing connector and the failure, and stop with status=blocked.
      7. `python3 -m pytest tests/unit -q` passes, so the new ADR links and the plan's removal
         check out. `ruff check .` passes.
      8. Confirm that p3's CHANGELOG entry is under `## [Unreleased]`. If it is missing, add it
         as p3's step 6 says.
      Commit message: "Record ADRs 0142 and 0143 and retire the shutdown/type-check plan".
    acceptance:
      - |-
        test -f docs/adr/0142-the-whole-tree-mypy-run-is-blocking.md && test -f docs/adr/0143-ruff-format-is-not-enforced.md
      - |-
        grep -c "0142-the-whole-tree-mypy-run-is-blocking.md\|0143-ruff-format-is-not-enforced.md" docs/adr/README.md prints 2
      - |-
        grep -c "adr/0142-the-whole-tree-mypy-run-is-blocking.md\|adr/0143-ruff-format-is-not-enforced.md" docs/coding-and-testing-guidelines.md prints at least 2
      - |-
        test ! -e docs/shutdown-and-type-check-cleanup-plan.md, and grep -rn "shutdown-and-type-check-cleanup-plan" scripts docs prints nothing
      - |-
        the phase report names a connector-live-check.yml run URL with conclusion success
      - |-
        python3 -m pytest tests/unit -q passes
      - |-
        ruff check . passes
```
