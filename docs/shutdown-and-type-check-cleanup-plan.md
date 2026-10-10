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
  `.github/workflows/tests.yml:406-413`, `pyproject.toml:361-383` (`[tool.mypy]` comment),
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
  - Add a module constant `_CLOSED_EVENT = "__closed__"` (never a real event name).
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
- `routes_approvals.py` (org-mode stream): `build_routes` gains the keyword parameter
  `stopping: threading.Event | None = None`. In `event_source`, at the top of the `while True:`
  body, before `request.is_disconnected()`, add `if stopping is not None and stopping.is_set(): break`.
  Add `import threading` to the module's imports if it is missing.
- `server.py`:
  - `build_app` gains `stopping: threading.Event | None = None`, placed right after `on_loop`,
    and passes it to `_build_org_app(..., stopping=stopping)`. `_build_org_app` gains the same
    keyword parameter and passes it to `routes_approvals.build_routes(..., stopping=stopping)`.
  - `WebServer.__init__`: `self._stopping = threading.Event()`, passed as `stopping=self._stopping`
    to `build_app`.
  - `WebServer.stop()` becomes, in this order:
    1. `self._stopping.set()`.
    2. `loop = self._loop`. If `loop is not None` and `self.state_stream is not None`, call
       `loop.call_soon_threadsafe(self.state_stream.close)`, inside
       `try: ... except RuntimeError: pass` (the loop may already be closed).
    3. `open_connections = len(self._server.server_state.connections)` and
       `started = time.monotonic()`.
    4. The existing `should_exit` / join / `force_exit` / still-running-warning block, unchanged.
    5. Right after that block, before the `control_channel` lines:
       `elapsed = time.monotonic() - started`, and if `elapsed >= SHUTDOWN_GRACE_SECONDS`, log
       `logger.warning("The web server took %.1f s to stop; %d connection(s) were open when it was asked to", elapsed, open_connections)`.
       That is the diagnostic #867 asked for, and it shows up in any CI log where it still
       happens.
    6. The existing `control_channel` and `mcp_url` cleanup, unchanged.
  - Import `time` at module level if it is not already imported.
- The org stream wakes once a second (`_STREAM_POLL_SECONDS`), so it ends within 1 s of `stop()`,
  which is still inside the grace. The local stream is woken at once through `close()`.
- `/mcp` (Streamable HTTP) is not changed. Its session manager has its own lifespan. If it turns
  out to hold shutdown, the warning in step 5 names it in CI, and that is a new issue.

Rejected: lowering `SHUTDOWN_GRACE_SECONDS` (it hides the cause and cuts a real slow response
short), and making the stream poll faster (it costs CPU all the time for a once-per-process
event).

### D4. Rules for the mypy fixes (#865)

Workers in p4 to p7 follow these rules. No phase changes runtime behaviour.

1. Fix the type, not the check: add or correct an annotation, narrow with an `if x is None:`
   branch that already exists in spirit, rename a reused variable, or widen a parameter to the
   type callers really pass.
2. `typing.cast` is allowed only where an invariant a few lines above guarantees the type. The cast
   gets a short comment naming that invariant.
3. `# type: ignore[<code>]  # <reason>` is allowed only for a mismatch in third-party stubs
   (Starlette/uvicorn ASGI callables, telethon) or for a module generated at build time. Always
   give the specific error code and a reason. Never a bare `# type: ignore`.
4. Never `assert` in `src/` to narrow a type. Bandit flags it, and `python -O` strips it.
5. No `[[tool.mypy.overrides]]` with `ignore_errors` and no new global mypy setting.
6. If a fix would change what the code does at runtime (a branch taken, a value returned, an
   exception raised), stop with `status=blocked` and name the line. The one exception is the
   `binascii` items in p6 (D4.p6 below), which are spelled out.

Specific fixes decided here:

- **daemon_main (p4)**: in `build_connectors`, rename each block's `client` and `connector` to
  `<name>_client` and `<name>_connector`, where `<name>` is `gmail`, `drive`, `calendar`,
  `contacts`, `tasks`, `apps_script`, `slack`, `salesforce`, `jira`, `confluence` or `telegram`.
  None of these names is bound in `daemon_main.py` today. `:1606`
  (`add_done_callback(_log_cache_warm_failure)`): `asyncio.run_coroutine_threadsafe` returns a
  `concurrent.futures.Future`, so annotate `_log_cache_warm_failure`'s parameter as
  `"concurrent.futures.Future[None]"` and import `concurrent.futures`. `:876` is fixed in p7, not
  in p4.
- **server.py (p7)**: type `step_up` as `StepUpConfig | LiveStepUpConfig | None` in both
  `build_app` (`server.py:853`) and `WebServer.__init__` (`server.py:1224`), importing
  `LiveStepUpConfig` from `..step_up_config`. This also clears `daemon_main.py:876`. Change
  `SHUTDOWN_GRACE_SECONDS = 2.0` to `SHUTDOWN_GRACE_SECONDS = 2`, since uvicorn types
  `timeout_graceful_shutdown` as `int | None`. The Starlette/uvicorn ASGI mismatches at
  `:493` and `:1398` use rule 3. The `list[Route]` / `list[BaseRoute]` mismatches at `:961`,
  `:1049` and `:1105` are fixed by typing `extra_routes` as `list[BaseRoute]` in `build_app` and
  `_build_org_app` (import `BaseRoute` from `starlette.routing`).
- **telegram_client (p5)**: annotate `self._client: Any = None` at `telegram_client.py:121` (telethon
  ships no types), and remove the seven `# type: ignore[union-attr]` comments that no longer
  apply.
- **app_credentials (p5)**: `from . import _telegram_credentials` at `app_credentials.py:22` gets
  `# type: ignore[attr-defined]  # generated at build time; absent in a source checkout`.
- **atlassian_users (p5)**: the two `CDATA_CONTENT_ELEMENTS` / `RCDATA_CONTENT_ELEMENTS` overrides
  at `:147-148` get `# type: ignore[misc]  # HTMLParser declares these Final; storage format has no CDATA elements`.
- **p6 `binascii`**: `drive_client.py:1697` and `connectors/drive.py:1572` refer to `base64.binascii.Error`.
  Replace each with `binascii.Error` and add `import binascii`. It is the same class at runtime,
  so behaviour does not change.

### D5. What is enforced, written down (#865)

- The whole-tree run becomes blocking: in `tests.yml` the step is renamed
  `Type-check (mypy, whole tree, blocking)` and loses `continue-on-error`. The strict ratchet step
  stays as it is.
- `ruff format` is not enforced and is not added anywhere. §1.9 of the coding guidelines says so,
  in this text (added as a paragraph at the end of §1.9's prose, before the Node/TypeScript part):

  > `ruff format` is not enforced and is not run in CI: it would rewrite most of the tree and
  > conflict with every open branch. Match the surrounding code's style instead
  > (`CONTRIBUTING.md`, "Code Style"). See ADR 0143.

  p9 turns "ADR 0143" into the link `[ADR 0143](adr/0143-ruff-format-is-not-enforced.md)` once
  the ADR exists. `tests/unit/test_docs_links.py` and `tests/unit/test_docs_references_exist.py`
  fail on a link or a `docs/adr/....md` path to a file that does not exist yet, and
  `verify.fast` runs them after every merge. So until p9, every mention of ADR 0142 or 0143
  (docs, workflow and `pyproject.toml` comments, script docstrings) is plain text: "ADR 0142",
  never a path. `tests/unit/test_docs_no_history.py` also rejects issue numbers (`#865`) and
  "as of/since <version>" in the contributor docs, so none of the new doc text uses them.

- The enforced static checks, as §1.9 and every other listing will state them, are:
  `ruff check .`, `bandit -c pyproject.toml -r src`, `mypy src/privacyfence` (whole tree, default
  strictness) and `python3 scripts/mypy_strict_modules.py` (strict flags for promoted modules).

## ADRs

The plugin framework reserves 0136-0141 (`6142f9de`). The next free number is 0142.

- **ADR 0142: the whole-tree mypy run is blocking.** `mypy src/privacyfence` at the project's
  default settings gates every merge, beside the per-module strict ratchet. Rejected: keeping it
  informational, since #865 shows the noise hid new errors; and adding modules to the strict list
  one at a time, which leaves most of the tree unchecked for years. Consequence: `mypy` is not
  pinned (`pyproject.toml` `lint` extra, `mypy>=1.10`), so a new mypy release can turn `main` red.
  The fix is then a follow-up PR, not a re-advisory.
- **ADR 0143: `ruff format` is not enforced.** Rejected: one formatting-only PR plus
  `ruff format --check .` in CI. It touches 354 of 575 files, conflicts with every open branch
  and with `feature/plugin-framework-parked`, and buys no correctness. The `ruff check` rule set
  in `[tool.ruff.lint]` stays the style gate.

D2 and D3 are implementation detail, and need no ADR.

## Manual steps

None. Nothing needs a console, a secret or a real device. The Windows behaviour (#867's "done
when") is checked by the PR's own `platform-windows` job, which runs the tightened
`TestStop` test from p3 automatically.

## Risks and open questions

- **p2/p3, uvicorn internals.** `self._server.server_state.connections` exists on uvicorn 0.54
  (`uvicorn/server.py`, `ServerState`). If `python3 -c "import uvicorn.server as s; s.ServerState().connections"`
  fails in the worker's environment, stop with `status=blocked`.
- **p3, the tightened test.** If `test_an_open_event_stream_does_not_keep_the_server_running`,
  with its new `< SHUTDOWN_GRACE_SECONDS` bound, still fails locally after the D3 changes, the
  stream is not the only thing holding shutdown. Do not loosen the bound: stop with
  `status=blocked` and paste the warning line from step 5.
- **p4 to p7, mypy version drift.** The counts were measured with mypy 2.4.0. If
  `mypy src/privacyfence` reports errors in a phase's files that the plan does not list (more
  than 3 extra in one file, or any in a file the plan does not list at all), fix the ones in the
  phase's own files under D4's rules. If one of them needs a runtime change, stop with
  `status=blocked`.
- **p4 to p7, a "type" error that is a real bug.** D4 rule 6 covers it. The worker stops rather
  than deciding.
- **p8, branch protection.** The job name `static-analysis` does not change, so
  `scripts/update_branch_protection.py` needs only its comment edited. If the worker finds a step
  name referenced as a required check anywhere (`grep -rn "Type-check (mypy" .github scripts`), it
  stops with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: shutdown-and-type-check-cleanup
feature_branch: fix/shutdown-and-type-check-cleanup
max_parallel: 2
manual_before: []
manual_after: []
verify_after_merge:
  - |-
    python3 -m pytest tests/unit/test_companion.py tests/unit/web/test_server.py tests/unit/web/test_state_stream.py tests/unit/web/test_routes_org_approvals.py tests/unit/test_daemon_main.py -q
  - |-
    python3 scripts/mypy_strict_modules.py
final_checks:
  - |-
    docs/shutdown-and-type-check-cleanup-plan.md is deleted and `grep -rn "shutdown-and-type-check-cleanup-plan" --include=*.md --include=*.py --include=*.yml --include=*.yaml .` finds nothing
  - |-
    docs/adr/0142-the-whole-tree-mypy-run-is-blocking.md and docs/adr/0143-ruff-format-is-not-enforced.md exist, each with Status "Accepted", and both are listed in docs/adr/README.md's index
  - |-
    CHANGELOG.md has the #867 line under `## [Unreleased]` → `### Fixed`, and no new `## [X.Y.Z]` heading
  - |-
    "mypy src/privacyfence" exits 0, and .github/workflows/tests.yml has no `continue-on-error` on the whole-tree mypy step
  - |-
    grep -rn 'setattr(companion.threading, "Thread"' tests/ finds nothing
phases:
  - id: p1-companion-thread-patch
    title: "#864: companion tests patch a module-local threading, not the global Thread"
    depends_on: []
    complexity: S
    touches:
      - tests/unit/test_companion.py
    brief: |
      Read the plan's "Current state → #864" and Design D1 first.
      1. In tests/unit/test_companion.py, add `from types import SimpleNamespace` to the imports
         (sorted with the other stdlib imports) if it is not already imported.
      2. Replace each of the three lines
         `monkeypatch.setattr(companion.threading, "Thread", _Thread)` (today at lines 420, 1256
         and 1275, in test_the_check_runs_off_the_startup_path and the two tray tests that follow
         TestTray's recovery-code test) with
         `monkeypatch.setattr(companion, "threading", SimpleNamespace(**{**vars(threading), "Thread": _Thread}))`.
         Change nothing else in those tests.
      3. Run `python3 -m pytest tests/unit/test_companion.py -q`. Every test must pass. If one
         fails with an AttributeError on `companion.threading.<name>`, the SimpleNamespace is
         missing that name: that means `vars(threading)` was not spread. Fix the spread, do not
         add names by hand.
      4. Run `ruff check tests/unit/test_companion.py`.
      No CHANGELOG line (test-only change). Commit message: "Patch companion's own threading in
      its tests, not the global Thread (#864)".
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
      1. Write the failing test first. In tests/unit/web/test_server.py, add a new class
         `TestEachServerKeepsItsOwnLoop` right after `class TestStop`, using TestStop's
         `_free_port` and `_wait_until_connectable` helpers (call them as `TestStop._free_port()`
         and `TestStop._wait_until_connectable(...)`), and
         `monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)` as TestStop's first test does.
         Test `test_a_late_shutdown_does_not_clear_a_newer_servers_loop`:
           - Build server A and server B, each `WebServer(WebApprovalUI(), host="localhost", port=<free port>)`.
           - `a.start()`, `loop_a = a.wait_until_ready()`, `b.start()`, `loop_b = b.wait_until_ready()`.
           - `a.stop()`, then assert `b.wait_until_ready() is loop_b`, `loop_b is not loop_a`,
             `loop_b.is_running()`, and `state_stream.get_loop() is loop_b` (import
             `from privacyfence.web import state_stream`).
           - `b.stop()` in a `finally`, and `a.stop()` in a `finally` too, guarded so it is not
             called twice.
         Run it and see it fail on `b.wait_until_ready() is loop_b` (it returns None today).
      2. In tests/unit/web/test_state_stream.py, class TestCallOnMainDispatcher, add
         `test_clear_loop_leaves_a_different_loop_alone`: `set_loop(loop)` with
         `loop = asyncio.new_event_loop()`, call `clear_loop(asyncio.new_event_loop())`
         (close both loops in a finally), assert `get_loop() is loop`, then `clear_loop(loop)`
         and assert `get_loop() is None`. Import `clear_loop` and `get_loop` alongside the
         existing imports.
      3. Implement D2 exactly: `clear_loop` in src/privacyfence/web/state_stream.py;
         `_state_stream_loop_lifespan(ready_event, on_loop)` and its docstring change, the
         `on_loop` parameter on `build_app` (after `loop_ready`), and `WebServer._loop`,
         `WebServer._set_loop`, `on_loop=self._set_loop`, and `wait_until_ready` returning
         `self._loop` in src/privacyfence/web/server.py. Add `on_loop` to build_app's docstring
         next to wherever `loop_ready` is described, in one sentence: "``on_loop`` is called
         with this app's loop once it is captured and with ``None`` on shutdown; WebServer
         keeps it as its own."
      4. Run `python3 -m pytest tests/unit/web/test_server.py tests/unit/web/test_state_stream.py tests/unit/test_daemon_main.py tests/unit/web/test_routes_settings.py -q`.
         All pass. If a test in test_routes_settings.py relies on the global being cleared to
         None after a server stops, stop with status=blocked and name it.
      5. `ruff check .` and `python3 scripts/mypy_strict_modules.py` pass.
      6. Because web/server.py changed in its lifecycle, run `python3 -m pytest tests/integration -q -k "mcp_daemon_contract or shim_mcp_contract"`.
         Both contract tests must pass (skips for a missing Node are acceptable only if they
         also skip on main).
      No CHANGELOG line (no user-visible change: the daemon runs one server). Commit message:
      "Keep each web server's own event loop (#866)".
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
      Read the plan's "Current state → #867" and Design D3 first. p2 is merged: WebServer has
      `self._loop`.
      1. Tighten the existing test first. In tests/unit/web/test_server.py,
         TestStop.test_an_open_event_stream_does_not_keep_the_server_running: change
         `assert time.monotonic() - started < SHUTDOWN_GRACE_SECONDS + 3` to
         `assert time.monotonic() - started < SHUTDOWN_GRACE_SECONDS`. Also add the `caplog`
         fixture to the test, wrap `server.stop()` in `with caplog.at_level("WARNING", logger="privacyfence.web.server"):`,
         and assert `"took" not in caplog.text`. Run it: it fails today (about 2.3 s).
      2. Add, in TestStop, `test_a_slow_stop_is_logged_with_the_open_connection_count`:
         `monkeypatch.setattr(server_module, "SHUTDOWN_GRACE_SECONDS", 0)` where
         `server_module` is `privacyfence.web.server` (import it as
         `from privacyfence.web import server as server_module` inside the test), start a
         server the same way as the test above, open the same raw-socket SSE request, call
         `server.stop()` under `caplog.at_level("WARNING", logger="privacyfence.web.server")`,
         and assert that `"The web server took"` and `"1 connection(s) were open"` are in
         `caplog.text`. If uvicorn counts the connection differently (for example 0, because the
         request finished first), stop with status=blocked and paste caplog.text. Do not
         loosen the assertion.
      3. In tests/unit/web/test_state_stream.py, add a class `TestClose` with:
         - `test_close_ends_an_open_subscription`: build a StateStream the way the file's
           existing subscribe tests do, start `subscribe(lambda: _false())` (an async
           is_disconnected that returns False; copy the file's existing helper if there is one),
           read the two initial events with `__anext__()`, call `stream.close()`, and assert
           that the next `__anext__()` raises StopAsyncIteration within 0.5 s
           (`asyncio.wait_for(..., 0.5)`).
         - `test_a_subscription_opened_after_close_ends_after_its_initial_events`: call
           `close()` first, then subscribe, read the initial events, and assert the next
           `__anext__()` raises StopAsyncIteration.
      4. In tests/unit/web/test_routes_org_approvals.py, class TestApprovalsStream, add
         `test_stream_ends_once_the_server_is_stopping`, modelled on
         `test_stream_ends_once_the_session_is_gone`: build the app with a set
         `threading.Event()` passed as `stopping=` (extend the module's `_app()` helper with a
         keyword `stopping=None` that it passes to `routes_approvals.build_routes`), get the
         endpoint and request with `self._stream(...)`, `response = await endpoint(request)`,
         and assert `await response.body_iterator.__anext__()` raises StopAsyncIteration.
      5. Implement D3 exactly: `_CLOSED_EVENT`, `self._closed`, `close()`, and the two checks
         in `subscribe` in src/privacyfence/web/state_stream.py; the `stopping` parameter and
         check in src/privacyfence/web/routes_approvals.py `build_routes`/`event_source`;
         `stopping` on `build_app` and `_build_org_app`, `self._stopping`, and the new
         `stop()` steps 1-6 in src/privacyfence/web/server.py. Add to build_routes' docstring one
         sentence: "``stopping``, when set, ends the approvals stream on its next tick, so a
         server that is shutting down is not held for its graceful-shutdown period."
      6. Add under CHANGELOG.md's `## [Unreleased]` a `### Fixed` subsection (create it only if
         it does not exist) with exactly this entry:
         "- Stopping PrivacyFence with a settings or approvals tab open no longer waits two
         seconds for the tab's live-update connection: the connection now ends as soon as the
         web server is asked to stop."
         Wrap at 100 columns like the 5.6.0 "Fixed" entry. Never add a `## [X.Y.Z]` heading.
      7. Run `python3 -m pytest tests/unit/web tests/unit/test_daemon_main.py -q`. All pass.
      8. Because web/server.py's lifecycle changed: `python3 -m pytest tests/integration -q -k "mcp_daemon_contract or shim_mcp_contract"`
         passes.
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
        python3 -m pytest tests/unit/web tests/unit/test_daemon_main.py -q passes
      - |-
        grep -n "connection(s) were open when it was asked to" src/privacyfence/web/server.py matches
      - |-
        awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -c "live-update connection" prints 1
      - |-
        ruff check . and bandit -c pyproject.toml -r src pass

  - id: p4-mypy-daemon-main
    title: "#865: daemon_main.py passes mypy (except the step_up line p7 fixes)"
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/daemon_main.py
    brief: |
      Read Design D4 (rules 1-6 and the "daemon_main (p4)" item) first.
      1. Run `mypy src/privacyfence 2>&1 | grep '^src/privacyfence/daemon_main.py:.*error'`.
         Expect 29 errors: 28 in build_connectors (lines ~1338-1521), one at ~1606, and one at
         ~876 (`step_up` ... `LiveStepUpConfig`). Leave the line-876 error alone; p7 fixes it
         in server.py.
      2. In `build_connectors`, rename each connector block's `client` → `<name>_client` and
         `connector` → `<name>_connector`, `<name>` being gmail, drive, calendar, contacts,
         tasks, apps_script, slack, salesforce, jira, confluence, telegram. Rename every use in
         that block, the `connectors.append(...)` call included. Rename nothing outside
         `build_connectors`. Blocks that never bind `connector` (Salesforce appends
         `SalesforceConnector(client)` directly) only rename `client`.
      3. Annotate `_log_cache_warm_failure(future: "concurrent.futures.Future[None]")` and add
         `import concurrent.futures` to the imports (keep them sorted).
      4. Re-run step 1's command: exactly one error remains, the `step_up` one. Any other
         remaining error is fixed under D4's rules. If it needs a runtime change, stop with
         status=blocked.
      5. `python3 -m pytest tests/unit/test_daemon_main.py -q` passes, unchanged.
         `ruff check .` passes.
      No CHANGELOG line. Commit message: "Give each connector block its own names so mypy can
      type build_connectors (#865)".
    acceptance:
      - |-
        mypy src/privacyfence 2>&1 | grep -c '^src/privacyfence/daemon_main.py:.*error' prints 1
      - |-
        mypy src/privacyfence 2>&1 | grep '^src/privacyfence/daemon_main.py:.*error' | grep -c step_up prints 1
      - |-
        python3 -m pytest tests/unit/test_daemon_main.py -q passes
      - |-
        ruff check . passes

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
      Read Design D4 first: rules 1-6, and the telegram_client, app_credentials,
      atlassian_users and p6 `binascii` items (the binascii fix for drive_client.py belongs to
      this phase).
      1. `mypy src/privacyfence 2>&1 | grep -E '^src/privacyfence/(slack_client|telegram_client|jira_client|atlassian_users|drive_client|app_credentials|google_errors|oauth_loopback)\.py:.*error'`.
         Expect 32 errors: slack_client 14, telegram_client 7, jira_client 4,
         atlassian_users 3, and 1 each in the other four.
      2. telegram_client.py: `self._client: Any = None` at the `self._client = None` line
         (~121, import Any from typing if missing). Delete the now-unneeded
         `# type: ignore[union-attr]` comments on the `self._client.<method>(...)` lines.
      3. app_credentials.py:22 and atlassian_users.py:147-148: add exactly the ignores D4 gives.
      4. drive_client.py:~1697: `base64.binascii.Error` → `binascii.Error`, with `import binascii`.
      5. slack_client.py: `var-annotated` errors get the element type the API returns
         (`list[dict[str, Any]]` for raw Slack message/member lists, `dict[str, Any]` for a raw
         response). `oldest: str = None` / `latest: str = None` (~681-682) become
         `str | None = None`, which also clears the call-site errors at ~910. The
         `no-redef` at ~1596 (`members`) is a second annotation of the same name: keep the
         first annotation and drop the second. The `str-bytes-safe` at ~205: if the value can
         really be bytes there, decode it with `.decode("utf-8", "replace")`; that is a runtime
         change only for bytes input, which today prints `b'...'`. Since it is a behaviour
         change, stop with status=blocked and name the line if the value can be bytes on a real
         path; if it can only ever be str, narrow the annotation.
      6. jira_client.py: the four `assignment` errors are variables initialised to `None` and
         later assigned a dict/str. Annotate each at its first assignment
         (`x: dict[str, Any] | None = None`, `x: str | None = None`).
      7. google_errors.py:82, oauth_loopback.py:94: rule 1 (narrow or annotate). Do not change
         behaviour.
      8. Re-run step 1's command: it prints nothing. Then `python3 -m pytest tests/unit -q -k "slack or telegram or jira or atlassian or drive or credentials or google_errors or oauth_loopback"`
         passes, `ruff check .` and `bandit -c pyproject.toml -r src` pass.
      This touches *_client.py files, and §2.7's conditional row asks for a live connector check.
      These are type-only changes with no runtime change: write "type annotations only, no
      runtime change, live check not owed" in the commit body instead of dispatching one.
      No CHANGELOG line. Commit message: "Make the client modules pass mypy (#865)".
    acceptance:
      - |-
        mypy src/privacyfence 2>&1 | grep -cE '^src/privacyfence/(slack_client|telegram_client|jira_client|atlassian_users|drive_client|app_credentials|google_errors|oauth_loopback)\.py:.*error' prints 0
      - |-
        grep -n "type: ignore\]" src/privacyfence/slack_client.py src/privacyfence/telegram_client.py src/privacyfence/jira_client.py prints nothing (no bare ignores)
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
      Read Design D4 first (rules 1-6 and the `binascii` item for connectors/drive.py).
      1. `mypy src/privacyfence 2>&1 | grep -E '^src/privacyfence/(connectors/apps_script|connectors/salesforce|connectors/drive|policy/propose|gate|approvals|approval_ui)\.py:.*error'`.
         Expect 17: apps_script 6, salesforce 2, drive 2, propose 2, gate 2, approval_ui 2,
         approvals 1.
      2. connectors/drive.py:~1572: `base64.binascii.Error` → `binascii.Error` with
         `import binascii`. connectors/drive.py:~141: a variable first bound to a
         `list[list[str]]` and later to a `str`. Give the second use its own name.
      3. policy/propose.py:~601 and ~623: change `_rules_from_pairs`'s third parameter
         annotation from `dict[str, tuple[tuple[str, Any], ...]]` to
         `Mapping[str, tuple[tuple[str, Any], ...]]` (import Mapping from collections.abc or
         typing, whichever the module already uses).
      4. connectors/apps_script.py:~75-76: the value is `ScriptFile | dict`. Branch on
         `isinstance(f, ScriptFile)` using the attribute access for ScriptFile and `.get(...)`
         for dict, the same two reads the current expression does. If the current code relies
         on a runtime type mypy cannot see, so that one branch can never run, stop with
         status=blocked.
      5. connectors/salesforce.py:~196-200: annotate or narrow so the `> None` comparison and
         the return tuple type-check. The values come from a parsed cursor; if one can really
         be None on a real path, that is a runtime question: stop with status=blocked.
      6. gate.py:~1149 and ~1275: `_pending_result(registry, ...)` with
         `PendingApprovalRegistry | None`. If the caller already checked it is not None a few
         lines earlier, narrow with a local variable. Otherwise stop with status=blocked.
      7. approval_ui.py:~147,~150 (`Missing return statement`): methods meant to be overridden.
         Make each body `raise NotImplementedError` if it is a stub with only a docstring or
         `...`; that is not a behaviour change, since nothing calls the base method. If either
         has a real body, stop with status=blocked.
      8. approvals.py:~974: rule 1.
      9. Re-run step 1's command: it prints nothing. `python3 -m pytest tests/unit -q` passes;
         `ruff check .` and `bandit -c pyproject.toml -r src` pass.
      This touches connectors/**: as in p5, write "type annotations only, no runtime change,
      live check not owed" in the commit body. No CHANGELOG line. Commit message: "Make
      connectors, policy, gate and approvals pass mypy (#865)".
    acceptance:
      - |-
        mypy src/privacyfence 2>&1 | grep -cE '^src/privacyfence/(connectors/apps_script|connectors/salesforce|connectors/drive|policy/propose|gate|approvals|approval_ui)\.py:.*error' prints 0
      - |-
        python3 -m pytest tests/unit -q passes
      - |-
        ruff check . and bandit -c pyproject.toml -r src pass

  - id: p7-mypy-web-and-text
    title: "#865: web server, web routes and text helpers pass mypy"
    depends_on: [p3-streams-end-on-stop, p4-mypy-daemon-main]
    complexity: S
    touches:
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_approvals.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/markdown_to_html.py
      - src/privacyfence/email_markdown.py
      - src/privacyfence/resource_names.py
    brief: |
      Read Design D4 first (rules 1-6 and the "server.py (p7)" item). p3 and p4 are merged.
      1. `mypy src/privacyfence 2>&1 | grep -E '^src/privacyfence/(web/server|web/routes_approvals|web/routes_settings|web/mcp_dispatch|markdown_to_html|email_markdown|resource_names|daemon_main)\.py:.*error'`.
         Expect about 16: server 7 (line numbers moved after p2/p3), routes_settings 2,
         markdown_to_html 2, routes_approvals 1, mcp_dispatch 1, email_markdown 1,
         resource_names 1, daemon_main 1 (the step_up line).
      2. web/server.py: apply D4's server.py item exactly: `step_up: StepUpConfig | LiveStepUpConfig | None`
         in `build_app` and `WebServer.__init__`; `SHUTDOWN_GRACE_SECONDS = 2`; `extra_routes:
         list[BaseRoute]` in `build_app` and `_build_org_app`; rule-3 ignores with a reason for
         the two ASGI-callable mismatches (the `ProxyHeadersMiddleware` wrap and the one at
         ~493). If `create_approvals_app`'s `extra_routes` parameter is `list[BaseRoute] | None`,
         this now matches. If any other module needs to change to satisfy these, stop with
         status=blocked.
      3. markdown_to_html.py:~80,~84 and email_markdown.py:~104 (`Match | None` has no
         `.group`): read the surrounding code. If the match cannot fail there (the same regex
         matched just before), bind the match to a variable and narrow with an existing
         condition. If it can fail, stop with status=blocked.
      4. web/mcp_dispatch.py:~238 and the other single errors: rule 1. `no-redef` means the
         second annotation of the same name: keep the first one.
      5. Re-run step 1's command: it prints nothing. Then run `mypy src/privacyfence`: it reports
         "Success: no issues found" if p5 and p6 are merged too. If they are not, the only
         errors are in p5's and p6's files.
      6. `python3 -m pytest tests/unit -q` passes. Because web/server.py changed:
         `python3 -m pytest tests/integration -q -k "mcp_daemon_contract or shim_mcp_contract"`
         passes. `ruff check .`, `bandit -c pyproject.toml -r src` and
         `python3 scripts/mypy_strict_modules.py` pass.
      No CHANGELOG line. Commit message: "Make the web server, web routes and text helpers pass
      mypy (#865)".
    acceptance:
      - |-
        mypy src/privacyfence 2>&1 | grep -cE '^src/privacyfence/(web/server|web/routes_approvals|web/routes_settings|web/mcp_dispatch|markdown_to_html|email_markdown|resource_names|daemon_main)\.py:.*error' prints 0
      - |-
        grep -n "SHUTDOWN_GRACE_SECONDS = 2$" src/privacyfence/web/server.py matches
      - |-
        python3 -m pytest tests/unit -q passes
      - |-
        ruff check ., bandit -c pyproject.toml -r src and python3 scripts/mypy_strict_modules.py pass

  - id: p8-mypy-blocking-and-docs
    title: "#865: the whole-tree mypy run blocks in CI, and the docs say which checks are enforced"
    depends_on: [p5-mypy-clients, p6-mypy-connectors-and-core, p7-mypy-web-and-text]
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
    brief: |
      Read Design D5 and the plan's "Current state → #865" list of places first.
      1. Confirm `mypy src/privacyfence` prints "Success: no issues found". If not, stop with
         status=blocked and paste the errors: an earlier phase is incomplete.
      2. .github/workflows/tests.yml, static-analysis job: rename the step
         `Type-check (mypy, informational)` to `Type-check (mypy, whole tree, blocking)`, delete
         its `continue-on-error: true` line, and replace the comment above it with:
         "# Blocking: the whole tree at [tool.mypy]'s default settings (ADR 0142). The strict
         flags for promoted modules are the next step's job."
         The run command stays `mypy src/privacyfence`. Run
         `grep -rn "Type-check (mypy" .github scripts`: if anything other than tests.yml matches,
         stop with status=blocked.
      3. pyproject.toml `[tool.mypy]` comment (lines ~363-382): replace the paragraph from
         "mypy is adopted incrementally" through "...nothing at all.)" with a comment saying: the
         whole tree is checked at these settings and blocks the merge (ADR 0142); per-module
         `[[tool.mypy.overrides]]` blocks below add strict flags to promoted modules, and
         `scripts/mypy_strict_modules.py` checks those, also blocking, in the next CI step;
         see url_safety.py's override for why the flags are spelled out. Keep every
         override block and its own comment unchanged.
      4. scripts/mypy_strict_modules.py module docstring: in the paragraph beginning "This
         script is what makes promotion blocking", replace "beside the informational whole-tree
         run, so the ratchet's promoted modules genuinely gate the merge while the ~87
         pre-existing errors in the rest of the tree stay visible-but-advisory, exactly as
         before." with "beside the whole-tree run (also blocking, ADR 0142), so the promoted
         modules are held to the strict flags on top of the defaults the whole tree must meet."
         In the second paragraph ("mypy is meant to go ..."), replace "The whole-tree mypy step
         ... is `continue-on-error: true`, so on its own" with "The whole-tree mypy step checks
         only the default settings, so on its own". In the `--follow-imports=silent` paragraph,
         replace "dragging the whole tree's pre-existing findings into a blocking step and making
         promotion impossible" with "reporting strict-flag findings for modules that are not
         promoted". Change no code.
      5. scripts/pre_release_check.py: after the `results["mypy (promoted modules)"]` entry, add
         `results["mypy (whole tree)"] = run("mypy (whole tree)", [sys.executable, "-m", "mypy", "src/privacyfence"], cwd=REPO_ROOT)`
         (import sys if missing). Update the module docstring's paragraph (lines ~13-19) to say
         that the whole-tree run and the promoted-module run are both included because both
         block in CI.
      6. scripts/update_branch_protection.py comment at ~72-81: replace the sentence about the
         whole-tree step's `continue-on-error` with one saying all of static-analysis's steps
         (ruff, the whole-tree mypy run, the promoted-module mypy run, bandit) are blocking, so
         requiring the job requires all four. Change no code.
      7. docs/coding-and-testing-guidelines.md §1.9: the command block lists
         `ruff check .`, `mypy src/privacyfence`, `python3 scripts/mypy_strict_modules.py`. Rewrite
         the paragraph after it to: Ruff (`ruff check .`), Bandit, the whole-tree mypy run and the
         promoted-module mypy run are all blocking CI checks; the whole tree is checked at
         `[tool.mypy]`'s settings (ADR 0142), and promoting a module to the strict flags is still
         a one-block edit in `pyproject.toml`. Then add the `ruff format` paragraph from D5
         verbatim. In §2.7's "Every PR" checklist item, list
         `mypy src/privacyfence` as a fourth command and replace "the whole-tree `mypy
         src/privacyfence` run in that same job is informational only, while" with "the whole-tree
         run checks the default settings and".
      8. .github/pull_request_template.md:17-21: same change as §2.7: add `mypy src/privacyfence`,
         and replace "The whole-tree `mypy src/privacyfence` run is informational only; the
         modules" with "`mypy src/privacyfence` checks the whole tree at the default settings; the
         modules".
      9. docs/testing-policy.md:58: the static-analysis row becomes "`ruff check .`,
         `bandit -c pyproject.toml -r src`, whole-tree `mypy src/privacyfence`,
         `scripts/mypy_strict_modules.py` (all blocking); `ruff format` is not run (ADR 0143)".
         Line ~270: add `mypy src/privacyfence` next to `mypy_strict_modules.py`.
      10. docs/release-testing.md:~35-36: add "whole-tree `mypy`" to the list of commands
          pre_release_check.py runs.
      11. .claude/toolkit.yaml `verify.dod`: insert `- python3 -m mypy src/privacyfence` right
          before `- python3 scripts/mypy_strict_modules.py`. Then validate the file with
          `python3 "$(ls -d /root/.claude/plugins/synced/*/devflow/ 2>/dev/null | head -1)scripts/validate_profile.py" .claude/toolkit.yaml`
          if that path exists. If it does not exist, `python3 -c "import yaml,sys; yaml.safe_load(open('.claude/toolkit.yaml'))"`
          must succeed.
      12. ADR mentions: ADR 0142 and 0143 do not exist until p9. Cite them as plain text only
          ("ADR 0142"), never as a Markdown link and never as a `docs/adr/...md` path: the docs
          link and reference tests would fail (see the end of Design D5). Do not write issue
          numbers (`#865`) or "since <version>" into any file under docs/ or into
          CONTRIBUTING.md, because tests/unit/test_docs_no_history.py rejects them.
      13. `python3 -m pytest tests/unit/test_pre_release_check.py tests/unit/test_update_branch_protection.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q`
          passes, `ruff check .` passes, and `python3 scripts/mypy_strict_modules.py` passes.
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
        grep -n "ruff format\` is not enforced" docs/coding-and-testing-guidelines.md matches
      - |-
        grep -n "mypy src/privacyfence" .claude/toolkit.yaml scripts/pre_release_check.py .github/pull_request_template.md matches in all three files
      - |-
        grep -rn "informational" docs/coding-and-testing-guidelines.md docs/testing-policy.md .github/pull_request_template.md .github/workflows/tests.yml | grep -i mypy prints nothing
      - |-
        python3 -m pytest tests/unit/test_pre_release_check.py tests/unit/test_update_branch_protection.py tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q passes
      - |-
        grep -rn "adr/014[23]" docs/coding-and-testing-guidelines.md docs/testing-policy.md pyproject.toml .github scripts prints nothing (plain-text mentions only until p9)

  - id: p9-retire-plan
    title: "Write ADRs 0142 and 0143 and retire the plan"
    depends_on: [p1-companion-thread-patch, p8-mypy-blocking-and-docs]
    complexity: S
    touches:
      - docs/adr/0142-the-whole-tree-mypy-run-is-blocking.md
      - docs/adr/0143-ruff-format-is-not-enforced.md
      - docs/adr/README.md
      - docs/coding-and-testing-guidelines.md
      - docs/shutdown-and-type-check-cleanup-plan.md
    brief: |
      Read the plan's ADRs section and docs/adr/README.md (rules and template) first.
      1. Write docs/adr/0142-the-whole-tree-mypy-run-is-blocking.md from the template: title
         "ADR 0142: The whole-tree mypy run is blocking"; Status "Accepted — <today's date>.
         Implemented in this PR."; Context: 93 errors in 23 files on 5.6.0, the
         continue-on-error step hid new ones (#865); Decision: `mypy src/privacyfence` at
         `[tool.mypy]`'s settings blocks every merge, beside the strict per-module ratchet in
         `scripts/mypy_strict_modules.py`, which is unchanged; Rejected: keep it informational,
         and promote module by module only (both with the reasons in the plan's ADRs section);
         Consequences: mypy is not pinned, so a new release can turn main red, and the answer is
         a fix PR, not a return to continue-on-error. Link #865, `.github/workflows/tests.yml`
         and `scripts/mypy_strict_modules.py`. Never link the plan document.
      2. Write docs/adr/0143-ruff-format-is-not-enforced.md the same way: title "ADR 0143:
         `ruff format` is not enforced". Context: on 5.6.0 it would rewrite 354 of 575 files
         (#865). Decision: not run in CI and not required; `ruff check` with `[tool.ruff.lint]`'s
         rules stays the style gate, and contributors match the surrounding code. Rejected: one
         formatting-only PR plus `ruff format --check .` in CI, because of the churn and the
         conflicts with every open branch and `feature/plugin-framework-parked`. Consequence:
         revisit when no long-lived branch is open.
      3. Add both to the index table in docs/adr/README.md after 0135, with "Accepted". Do not
         add rows for 0136-0141; they stay reserved for the parked plugin framework.
      4. In docs/coding-and-testing-guidelines.md §1.9, turn the plain-text "ADR 0142" and
         "ADR 0143" mentions p8 added into links: `[ADR 0142](adr/0142-the-whole-tree-mypy-run-is-blocking.md)`
         and `[ADR 0143](adr/0143-ruff-format-is-not-enforced.md)`. Leave the plain-text
         mentions in workflow, pyproject.toml and script comments as they are.
      5. Delete docs/shutdown-and-type-check-cleanup-plan.md (`git rm`).
         `grep -rn "shutdown-and-type-check-cleanup-plan" .` must find nothing outside .git.
      6. Run `python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py -q`:
         it passes, so the new links resolve. Then `ruff check .`.
      7. The CHANGELOG line for #867 was added in p3. Confirm it is under `## [Unreleased]`.
         If it is missing, add it as p3's step 6 says.
      Commit message: "Record ADRs 0142 and 0143 and retire the shutdown/type-check plan".
    acceptance:
      - |-
        test -f docs/adr/0142-the-whole-tree-mypy-run-is-blocking.md && test -f docs/adr/0143-ruff-format-is-not-enforced.md
      - |-
        grep -c "0142\|0143" docs/adr/README.md prints at least 2
      - |-
        grep -c "adr/0142-the-whole-tree-mypy-run-is-blocking.md\|adr/0143-ruff-format-is-not-enforced.md" docs/coding-and-testing-guidelines.md prints at least 2
      - |-
        test ! -e docs/shutdown-and-type-check-cleanup-plan.md
      - |-
        python3 -m pytest tests/unit -q passes
      - |-
        ruff check . passes
```
