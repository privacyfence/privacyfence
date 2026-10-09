# Plan: plugin framework hardening and the plugin page browser

## Goal

This plan fixes everything a third-party review (Review A) found in the plugin framework, and adds
the plugin page browser (Review B, protocol 1.2). It is the fifth PR in the stack, after
[privacyfence/privacyfence#855](https://github.com/privacyfence/privacyfence/pull/855),
[#856](https://github.com/privacyfence/privacyfence/pull/856),
[#868](https://github.com/privacyfence/privacyfence/pull/868) and
[#869](https://github.com/privacyfence/privacyfence/pull/869). Its plan branch is cut from
`origin/feature/plugins-nav-menu` (#869's head, `378445da`), so the feature branch
`feature/plugin-framework-hardening` carries the whole plugin framework stack.
Tracking issue: [privacyfence/privacyfence#846](https://github.com/privacyfence/privacyfence/issues/846).

What changes for the user:

- **Plugin output reads (A2).** A repeated `plugin_outputs_read` of a file that changed after its
  card was approved shows a new card. Today the old approval releases the new bytes.
- **A plugin that stops reading (A3).** A plugin that stops reading its input no longer freezes
  **Disable**, **Delete this plugin's data** or daemon shutdown. After 10 seconds it is treated as
  crashed, and stopping it takes about 3 seconds at most. One stuck plugin no longer delays
  connector events to the others.
- **Overlapping actions (A4).** Settings actions on plugins run one at a time, so a rescan during a
  purge or a disable can no longer leave a second, unreachable plugin process running.
- **Pending cards (S1).** A plugin's approval and confirmation cards that are still waiting expire
  when the plugin is disabled (by you or by the crash limit), its data is deleted, or it is
  removed. A late click can no longer store an approval for a plugin that is gone.
- **Write results (S2).** The result of a plugin's write tool reaches the AI only when it is at most
  2,048 bytes and the PII check finds nothing. Otherwise the AI is told the action ran and its
  result was withheld.
- **Windows owner (S3).** On Windows a plugin's executable and folders must also be owned by
  SYSTEM, Administrators or TrustedInstaller, as POSIX already requires root.
- **Running log (S4).** A plugin's log stays within 5 MiB per file while the plugin runs, not only
  at the next start.
- **Data path (D1, D2, D3).** A Sheets range is downloaded once per read, not once per page, and
  every page comes from the same version. A Confluence page that changes between pages is reported
  instead of joined. A Drive file of a Google type that cannot be downloaded (a Form, a folder) is
  refused instead of returned as an empty file.
- **SDK and test host parity (P1 to P7).** The SDK keeps a prepared call as long as PrivacyFence
  can still run it (20 minutes), holds at most 256, and gains `ctx.confirm.wait()` and
  `ctx.approvals.wait()` that wait out a card's whole life. The `today` example uses it, so a late
  approval still publishes. `PluginTestHost` takes the manifest's `source_operations` and `pages`,
  checks source-call parameters as PrivacyFence does, can model the PII check that overrides an
  "Always allow" rule, and measures sizes in UTF-8 bytes as PrivacyFence now does everywhere.
- **Plugin page browser (Review B).** A plugin can list its pages (`pages.list`, protocol 1.2.0,
  `@plugin.page_index` in the SDK). PrivacyFence serves a page browser at `/plugin-pages`, which
  the Plugins menu and the Settings card open in the same tab. Each listed page opens in a new tab
  in the unchanged ADR 0124 sandbox.

**The Salesforce and Confluence path injection (Review A item 1) is not in this plan.** A
`report_id` of `../../query?q=…` reached any Salesforce REST path, and a Confluence `page_id` with
`../` reached any Confluence path. That was fixed on `main` by
[#870](https://github.com/privacyfence/privacyfence/pull/870) (branch
`fix/salesforce-report-id-path`, merge commit `7ea7e100`): `_validate_salesforce_id(report_id, "report_id")`
inside `SalesforceClient.run_report`, a digits-only `page_id` (`_PAGE_ID_RE`) in
`ConfluenceClient.get_page`, and tests with `../` ids. It also covers the `salesforce_run_report`
MCP tool. This plan only brings it into the stack (p0 merges `origin/main`) and adds one
plugin-side regression test through `source.call` (p6).

**About the stacked PR.** `/implement` opens its PR to `main`. Until #855, #856, #868 and #869
merge, this PR's diff contains all of them as well. p0's merge commits change paths outside its
`touches`. That is expected: the orchestrator's diff review of p0 checks only that each merge
brought its source in unchanged.

## Current state

Line numbers are on `feature/plugins-nav-menu` at `378445da`. The highest ADR there and on
`origin/main` is 0131, and no branch on origin has 0132 or higher. The plugin SDK has not been
published to PyPI (the latest release is 5.5.0), so its test host defaults can change without a
compatibility path.

### Output reads (A2)

- `plugins/outputs.py:338` reads the file (`read_output`) on every call, before the gate.
  `:341-357` calls `gated_call(... args={"plugin", "path": result["path"], "offset"})` with no
  `dedupe_extra`. `:363` returns the fresh read.
- `gate.py:1035-1037` builds the ledger key from the args, plus `dedupe_extra` when given.
  `approvals.py:785-829` (`consume_ledger`) keeps a read-gate decision reusable until `ledger_ttl`.
  A ledger hit returns before any card or PII confirmation (`gate.py:504-516`).
- So a file rewritten after its card is released without a card if the AI repeats the read within
  `ledger_ttl`. ADR 0130 says "the released text is those bytes".

### Send timeouts, stop and events (A3)

- `plugins/rpc.py:157-170` `_send`: `async with self._write_lock:` then `write` and
  `await drain()`, with no timeout. `request()` (`:103-119`) limits only the wait for the reply;
  `notify()` (`:121-124`) has no limit. `close()` (`:126-137`) awaits `wait_closed()` unbounded.
- `plugins/supervisor.py:399-410` `_stop_child` sends `plugin.disabling` and `shutdown` before it
  waits, terminates or kills. `contextlib.suppress(RpcError)` does nothing against a hang.
  `Supervisor.stop` (`:251-262`) awaits `_stop_child`. A plugin that answers `initialize` and then
  stops reading, plus one 400 KB notification, keeps `stop()` hung for good (reproduced).
- `plugins/events.py:60-67` `send` notifies peers one after another, so one stuck peer blocks the
  rest.
- `daemon_main.py:2030` runs `stop_all` with `.result(timeout=10)`.

### Host actions (A4)

- `PluginHost` (`host.py:135-183`) has no lock. Every Settings action is its own task
  (`settings_controller.py:1224-1256` → `host.submit`).
- `_stop` (`host.py:645-658`) clears `plugin.supervisor` first, then awaits `supervisor.stop()`
  (up to about 7 s). Meanwhile `_start_eligible` (`:430-446`) starts any enabled plugin whose
  `supervisor is None`. `_start_plugin` (`:573-630`) overwrites `plugin.supervisor` unconditionally
  (`:623`) and checks `record is None` only (`:576`), not `record.enabled`.
- So a rescan during a purge or disable leaves an orphan process, and two overlapping stops
  corrupt `plugin.intentional_stop`. `_action` (`:662-679`) is a synchronous context manager.

### Pending cards (S1)

- `plugins/approvals.py:409-438` (`_decide`) stores a confirmed approval with no check that the
  plugin still exists. Cards expire only in `close()` (`approvals.py:388-397`, `confirm.py:169-178`),
  called only from `host.stop_all` (`host.py:319-323`).
- `host.disable` (`:778-783`), `host.purge` (`:808-829`), `host._gone`/`_uninstall` (`:350-372`)
  and `host._on_state` (`:632-643`) never expire cards. A card confirmed after a purge is stored;
  one confirmed after an uninstall is stored and later handed to a reinstall of the same name
  (`ApprovalService.request`, `:279-281`).
- Once a card is finalized, a click is a no-op (`answer()` returns False, `src/privacyfence/approvals.py:334` and `:676`).

### Write results (S2)

- `plugins/connector.py:390-402`: a write's result goes to the AI as it is, up to
  `INLINE_RESULT_BYTES` (100,000), with `approval_id` merged in. The card shows only the preview,
  and an `auto` write has no card (`:231-236`). `approval_id` is any string (`protocol.py:384`).
- `docs/security-and-compliance.md:348` says a plugin gets its data to the AI "only through a card
  or a rule you wrote"; `docs/plugin-protocol.md:307` says a write result is returned "at most
  100,000 bytes serialized".
- `pii_detector.detect_pii_categories(text) -> list[str]` (`pii_detector.py:400`) returns `[]` when
  detection is off. The gate runs it with `asyncio.to_thread` (`gate.py:957-959`).

### Windows owner (S3)

- `privilege_separation.py:2114-2128` `_windows_admin_only_write_problem` reads only the DACL.
  `admin_only_write_problem` (`:2131`), `admin_only_ancestor_write_problem` (`:2150`) and
  `admin_only_plugin_dir_write_problem` (`:2168`) route there; `plugins/trust.py:131-141` calls
  them.
- `windows_acl.py:48-53` notes that an owner implicitly holds `WRITE_DAC`. POSIX already requires
  root ownership (`test_privilege_separation.py:3925`).

### Running log (S4)

- `supervisor.py:124-136` `_rotate_log` rotates only at spawn (`_open_log`, `:139-145`, called at
  `:273`). The child writes straight into the fd (`stderr=log_fd`, `:280`) with no bound.
- `constants.py:74-75`: `LOG_MAX_BYTES = 5 MiB`, `LOG_BACKUP_COUNT = 3`. `docs/plugins.md:213`
  promises 5 MiB.

### Source reads (D1, D2, D3, P5)

- `source_ops.py:327-341` `_run_sheets` calls `client.get_sheet_values(...)` on every page and
  slices `values[first:]`; the cursor state is `{"k": end}`.
- `spool.py:95-103`: a Google-native type outside `_GOOGLE_DOC_EXPORTS` (`drive_client.py:48-52`)
  takes the binary branch with `size` 0 and returns an empty success with no cursor.
- `source_ops.py:355-386` `_run_confluence` calls `get_page` on every page; the state is
  `{"o": start}`. `ConfluencePage.version` (`confluence_client.py:140`) is never checked.
- `source_ops.py:150-151` `_encoded_size` and `:553-557` (the cap and `"bytes"`) use
  `json.dumps(..., default=str)` with `ensure_ascii=True`, so "é" counts 6 bytes; `"bytes"` is
  `len(payload)` in characters. `rpc.py:159` also escapes. Every other size check in the daemon,
  the SDK and the test host already uses UTF-8 (`protocol.py:142`, `connector.py:96-97`, SDK
  `_rpc.py:187`, `testing/_source.py:200-201`).
- `source_ops.py:58-67`: a client exception in `_CLIENT_ERRORS` (`SalesforceClientError`,
  `ConfluenceClientError`, …) becomes `RpcError("upstream_error", "the service returned an error")`
  (`:547-551`). `source_ops` checks `report_id` and `page_id` only for type and length (`:212`,
  `:347-348`).

### SDK and test host (P1 to P4, P6, P7)

- P1: daemon `constants.py:73` `PREPARED_CALL_LIFETIME_SECONDS = 900.0`; SDK `plugin.py:52` the
  same. The daemon keeps a gated call for `pending_ttl + ledger_ttl` = 1,200 s
  (`connector.py:282`), so a deferred write approved at minute 14 can run at minute 19, after the
  SDK dropped it (`LOST_CALL`). `TestLimits` pins the two constants equal
  (`tests/unit/plugin_sdk/test_plugin.py:96`). `confirm.py:91` and `plugins/approvals.py:251` use
  the constant for `retain_finished_seconds`.
- P2: SDK `plugin.py:522` prepared store is a plain dict, swept only by time; read-only entries are
  never deleted after execute (`:806-807`).
- P3: the test host allows any of the six source operations (`testing/_source.py:177-178`), serves
  pages unconditionally (`testing/_host.py:540-551`) and treats "registered a page" as
  `pages: true` (`:241`). The daemon refuses each (`source_ops.py:518-519`, `host.py:892`,
  `plugins/approvals.py:263`).
- P4: the test host validates parameters only for Drive (`testing/_source.py:217-231`).
- P6: the test host auto-accepts by rule with no PII step (`testing/_host.py:463-471`); the daemon
  lets a PII match override an "Always allow" rule for a `review` gate (`gate.py:957-959`, `:1069`).
- P7: `CONFIRM_AWAIT_MAX_MS = 300_000` (`constants.py:65`) but a card lives 900 s.
  `ConfirmClient.await_` (`plugin.py:288-297`) and `ApprovalsClient.await_` (`:361-371`) do one
  await. `examples/plugins/today/today_plugin.py:370-378` swallows the timeout, so an approval
  after minute 5 never publishes.

### Protocol and pages (Review B)

- `constants.py:13` `PROTOCOL_VERSION = "1.1.0"`; `TIMEOUT_SECONDS` at `:55-64`. The schema
  `docs/plugin-protocol/protocol.schema.json` has `x-protocol-version`, `x-limits` (with
  `TIMEOUT_SECONDS` inside it; there is no `x-timeouts`) and `$defs`.
  `TestSchema.test_x_limits_match_the_constants` (`tests/unit/plugins/test_protocol.py:721`)
  requires every `x-limits` key to exist in `constants.py` with an equal value.
  `scripts/gen_plugin_sdk_types.py` generates `plugin-sdk/src/privacyfence_plugin_sdk/types.py`
  from `$defs` only.
- "1.1.0" is asserted at `tests/unit/plugins/test_constants.py:145`, `test_protocol.py:198`,
  `test_host.py:324`, `tests/unit/plugin_sdk/test_plugin.py:130`, and written at SDK
  `plugin.py:34`, `docs/plugin-protocol.md:4,177,195`, the schema `:5` and
  `examples/plugins/today/README.md:41`.
- `host.py:841` `page_links()` returns `(display name, "/plugins/<name>/")`; `rows()` (`:850`) sets
  `"page_url"` at `:872`; `web_request()` is at `:887`.
- `web_shell.py:517-540` `updatePluginsMenu` (case-insensitive sort, then exact); `:542-549` a
  click handler closes the menus when a plugin link is picked; `:613-626` `_plugins_html`. Every
  menu link has `target="_blank"`. `settings_window_html.py:1231-1233` is the card's "Open page".
  `routes_settings.py:823` and `routes_approvals.py:930` consume `rows()` and `page_links()`.
- `server.py:417` `_PLUGIN_PAGES_PREFIX = "/plugins/"`; `:493` gives every response under it the
  sandbox CSP; `:1063-1068` mounts `routes_plugins`; `:1093` passes
  `plugin_pages=lambda: plugin_host.page_links()`. `build_app` returns before `:1063` in org mode.
- `test_host.py` runs real child processes: the stdlib stub `tests/fixtures/plugins/stub/stub_plugin.py`
  (answers `method_not_found` to an unknown request) and the inline SDK script `SDK_PLUGIN`
  (`test_host.py:44`). There is no fake peer.
- `tests/integration/test_plugin_settings_browser.py` builds `WebServer(..., controller=...)` with a
  `_TwoRowHost` and no `plugin_host`, with `page_url "/plugins/alpha/"` (`:60`).
- `plugins/pages.py:61-76` `normalize_path`; `pages.py` imports `protocol.RpcError`, so
  `protocol.py` cannot import `pages` at module level.

## Design

### Stack and the hotfix (p0)

The hotfix is merged into `main` (#870). p0 merges `origin/main`, which carries it, and
`origin/feature/plugins-nav-menu` if that moved. After the merges,
`grep -n '_validate_salesforce_id(report_id' src/privacyfence/salesforce_client.py` must print a
line; otherwise p0 stops with `status=blocked`. Merge conflicts are resolved mechanically only in
`docs/README.md`, `scripts/build_site.py` and `CHANGELOG.md` (keep both sides' bullets under
`## [Unreleased]`: `main`'s `### Security` line from the hotfix and the stack's plugin bullets; no
version heading). If `docs/adr/0132-*` exists after the merges, another branch took the number and
p0 stops with `status=blocked`.

### Output reads are keyed to the file's hash (A2)

- `PluginOutputsConnector._read` (`plugins/outputs.py:341`): add `dedupe_extra=result["sha256"]`
  to the `gated_call(...)` call.
- The whole-file sha256 is enough. The key already holds `offset`, and `read_output` hashes and
  slices in the same pass over one descriptor, so the same hash and offset always give the same
  page bytes. Hashing the page alone was rejected: it needs a second digest, and the whole-file
  hash is already in the result.
- A change anywhere in the file gets a new card, even outside the current page. That is
  conservative and acceptable.
- Module docstring gains: "The approval is keyed to the file's sha256, so a file rewritten after
  its card gets a new card."
- No ADR: this makes the code match ADR 0130.

### A plugin that stops reading is a crash (A3)

**Constants** in `plugins/constants.py`, next to `SHUTDOWN_GRACE_SECONDS`:

```python
SEND_TIMEOUT_SECONDS = 10.0                # ADR 0132: a message the plugin does not read in time is a crash
SHUTDOWN_NOTIFY_TIMEOUT_SECONDS = 1.0
CLOSE_WAIT_SECONDS = 1.0
```

`SEND_TIMEOUT_SECONDS` is plugin-visible and goes into the schema's `x-limits` in p13 (the one
schema phase). The other two are daemon-internal.

**`rpc.py`**

- `_send(self, message, *, timeout: float | None = None)`. The limit is
  `SEND_TIMEOUT_SECONDS if timeout is None else timeout`, read from the module global at call
  time, so a test can monkeypatch `rpc.SEND_TIMEOUT_SECONDS`. The JSON encoding and the
  `MAX_LINE_BYTES` check stay before the timed block. Then:

  ```python
  try:
      async with asyncio.timeout(limit):          # covers waiting for the lock AND drain
          async with self._write_lock:
              if self._closed:
                  raise RpcError("internal_error", "peer closed")
              self._writer.write(line)
              await self._writer.drain()
  except TimeoutError:
      self._shutdown("write_timeout", abort=True)
      raise RpcError("timeout", "plugin is not reading its input") from None
  except (ConnectionError, OSError) as exc:
      self._shutdown("write_failed")
      raise RpcError("internal_error", "peer closed") from exc
  ```

  The timeout covers the lock wait, because a writer queued behind a stuck drain is just as stuck.
  A partial line may already be in the transport, so closing the peer is the only correct outcome.
- `_shutdown(self, reason: str, *, abort: bool = False)`: when `abort` is set, call
  `getattr(getattr(self._writer, "transport", None), "abort", None)` if it is callable, otherwise
  `self._writer.close()`. The fallback is needed because the test writers in `test_rpc.py:505-516`
  have no `.transport`.
- `notify(self, method, params, *, timeout: float | None = None)` passes `timeout` through to
  `_send`. `request()` keeps the default; a send timeout there surfaces as `RpcError("timeout")`.
- `close()`: `await asyncio.wait_for(self._writer.wait_closed(), CLOSE_WAIT_SECONDS)` (module
  global read at call time); on `TimeoutError`, abort the transport as `_shutdown` does. Other
  exceptions stay suppressed.

**A send timeout is a crash.** No new state is needed: `on_close("write_timeout")` sets
`child.gone`, `_attempt` returns `"crashed"` and logs `plugin X: crashed (write_timeout)`, and the
existing backoff and crash limit apply. No new Settings string.

**`supervisor.py` `_stop_child`** becomes:

```python
peer = child.peer
delivered = False
if child.proc.returncode is None and not peer.closed:
    with contextlib.suppress(RpcError):
        if reason != "shutdown":
            await peer.notify("plugin.disabling", {"reason": reason}, timeout=SHUTDOWN_NOTIFY_TIMEOUT_SECONDS)
        await peer.notify("shutdown", {"grace_ms": int(SHUTDOWN_GRACE_SECONDS * 1000)},
                          timeout=SHUTDOWN_NOTIFY_TIMEOUT_SECONDS)
        delivered = True
if not delivered or not await self._exited(child, SHUTDOWN_GRACE_SECONDS):
    _signal_child(child.proc, kill=False)
    if not await self._exited(child, TERMINATE_GRACE_SECONDS):
        _signal_child(child.proc, kill=True)
await self._reap(child)
```

The 5 s grace is skipped only when the shutdown message did not get through. A local `delivered`
flag is used, not `peer.closed`, because a plugin that exits cleanly on `shutdown` also closes the
peer. `introspect`'s `finally: _stop_child(child, "shutdown")` gets the same bound.

**`events.py` `EventFanout.send`**:

```python
async def send(self, events: list[dict[str, str]]) -> None:
    """Notify every running plugin of ``events``; a plugin that cannot be reached is skipped."""
    await asyncio.gather(*(self._send_to(peer, events) for peer in list(self._peers())))

async def _send_to(self, peer, events: list[dict[str, str]]) -> None:
    for params in events:
        try:
            await peer.notify(EVENT_STATE_CHANGED, dict(params))
        except Exception:
            logger.debug("Could not send %s to a plugin", EVENT_STATE_CHANGED, exc_info=True)
            return
```

Order is kept per plugin; a peer gets nothing more after its first failure.

**`daemon_main.py:2030`**: `.result(timeout=PLUGIN_STOP_ALL_TIMEOUT_SECONDS)` with a module
constant `PLUGIN_STOP_ALL_TIMEOUT_SECONDS = 25.0`. The worst case of `stop_all` is 21 s: 1 s
waiting for the host lock (`STOP_ALL_LOCK_WAIT_SECONDS`) + 1 s for the shutdown notices
(`SHUTDOWN_NOTIFY_TIMEOUT_SECONDS`) + 5 s grace (`SHUTDOWN_GRACE_SECONDS`) + 2 s after terminate
(`TERMINATE_GRACE_SECONDS`) + 2 s log drain (`LOG_DRAIN_SECONDS`) + up to 5 s each for closing the
confirmation and approval services. Plugins stop in parallel, so this does not grow with their
number. 25 s leaves 4 s of margin.

### Host actions run one at a time (A4)

In `PluginHost`:

```python
STOP_ALL_LOCK_WAIT_SECONDS = 1.0                         # module constant in host.py
REASON_STOPPING = "PrivacyFence is stopping its plugins."  # module constant in host.py


class _HostStopping(ValueError):
    """An action that was queued before stop_all ran; it does nothing."""


# __init__
self._lock = asyncio.Lock()
self._stop_epoch = 0
self._held_epoch = 0

@contextlib.asynccontextmanager
async def _serialized(self) -> AsyncIterator[None]:
    epoch = self._stop_epoch
    async with self._lock:
        if epoch != self._stop_epoch:          # queued before stop_all ran: do nothing afterwards
            raise _HostStopping(REASON_STOPPING)
        self._held_epoch = epoch
        yield

@contextlib.asynccontextmanager
async def _action(self, name: str) -> AsyncIterator[_Plugin]:
    epoch = self._stop_epoch
    async with self._lock:
        plugin = self._plugins.get(name)
        if plugin is None:
            raise LookupError(f"No plugin named {name}.")
        try:
            if epoch != self._stop_epoch:
                raise _HostStopping(REASON_STOPPING)
            self._held_epoch = epoch
            yield plugin
        except Exception as exc:
            ...  # the existing recording, unchanged: ValueError -> last_error = str(exc)
        else:
            ...  # unchanged
```

- `_action` looks the plugin up only after it holds the lock, so a plugin a rescan removed while
  the action waited raises `LookupError`. A queued action that finds `stop_all` ran records
  `REASON_STOPPING` as the row's `last_error` (it is a `ValueError`, so the existing recording
  applies) and raises `_HostStopping`.
- The five call sites become `async with self._action(name) as plugin:`: `inspect`, `enable`,
  `disable`, `revoke_approval`, `purge`. `inspect` is included because it writes `plugin.review`
  and `plugin.discovered`, which `enable` reads. `revoke_approval` is included because its store
  write races `purge`'s `_forget_approvals`.
- `rescan()` becomes `async with self._serialized(): await self._rescan()` with the body moved
  unchanged into `_rescan`. `rescan` catches only `_HostStopping` and returns quietly; any other
  `ValueError` propagates as today. `start()` calls `rescan()` and holds no lock itself.
- `stop_all()`: `self._stop_epoch += 1`; then try `self._lock.acquire()` inside
  `asyncio.timeout(STOP_ALL_LOCK_WAIT_SECONDS)`. On timeout, log a warning
  (`"plugin actions are still running; stopping plugins without waiting for them"`) and continue
  without the lock. Release in a `finally` only if acquired. Then the existing body. A second
  `stop_all` is harmless. A new call to `rescan`/`start` after `stop_all` captures the new epoch,
  so existing tests that restart a host after `stop_all` keep working.
- `_start_plugin` guards, at its top, in this order:
  1. `if plugin.supervisor is not None:` log
     `"plugin %s already has a supervisor; not starting another"` at WARNING and return (return,
     not raise, so one plugin cannot abort the others in `_start_eligible`'s `gather`);
  2. `if self._held_epoch != self._stop_epoch: return` (an action that outlived `stop_all`'s wait
     does not restart anything after shutdown);
  3. `record is None or not record.enabled` → return (fixes the purge restarting a plugin that the
     crash limit disabled during the purge's wait). `enable` writes `store.enable` before calling
     `_start_plugin`, so it is unaffected.
- Not under the lock: the supervisor's own crash and restart loop, `_on_state`, RPC handlers,
  MCP tool calls, `web_request`, `connectors()`, `rows()`, `page_links()`, event sends, and the
  page browser's `list_pages`/`list_all_pages`. Nothing that runs under the lock calls a public
  host method, so it cannot deadlock.
- Accepted: a purge waiting up to 30 s for its ack delays a queued Disable by up to 30 s.

### Pending cards end with the plugin (S1)

- `ConfirmationService.expire_plugin(self, plugin: str) -> int` (`plugins/confirm.py`, next to
  `close`): under `self._lock` collect
  `[(i, o.registry) for i, o in self._owned.items() if o.plugin == plugin and i not in self._finished]`,
  then `return sum(1 for i, r in waiting if r.finalize(i, "expired"))`. The finalizer threads wake
  and audit "expired" as they do today.
- `ApprovalService.expire_plugin(self, plugin: str) -> int` (`plugins/approvals.py`): the same,
  plus a per-plugin generation counter:
  - `self._epochs: dict[str, int] = {}` in `__init__`;
  - `epoch: int` as the last field of `_Pending`, filled in `request()` with
    `self._epochs.get(plugin, 0)` inside the existing `with self._lock` block;
  - `expire_plugin` increments `self._epochs[plugin]` in the same locked block that collects the
    waiting ids, then finalizes each one `"expired"`;
  - in `_decide`, at the start of the confirm branch and before `self._store.add`:
    `with self._lock: current = self._epochs.get(owned.plugin, 0) == owned.epoch`. If not
    current, `registry.finalize(approval_id, "expired")` and store nothing.
- `PluginHost._expire_cards(self, name: str) -> None` calls `self._confirm.expire_plugin(name)` and
  `self._approvals.expire_plugin(name)`. No extra audit row (each finalizer audits "expired").
  Called from:
  - `disable()`, after `await self._stop(plugin, "user")`;
  - `purge()`, right before `await asyncio.to_thread(self._forget_approvals, plugin.name)`;
  - `_gone()`, as its first statement;
  - `_on_state()`, inside `if state == "disabled" and not plugin.intentional_stop:`.
- Never from `_stop`: it also runs on restart, and a card must survive a restart.
- Pending gate cards of plugin tools are out of scope: after disable `clear_tools` drops the
  prepared call, so an approval cannot release anything.

### Write results are capped and scanned (S2)

- `plugins/constants.py`: `WRITE_RESULT_MAX_BYTES = 2048   # ADR 0133`. A write result is an
  acknowledgement; the largest in the repo is about 70 bytes. It goes into `x-limits` in p13.
- `plugins/connector.py`:
  - `WRITE_RESULT_WITHHELD = "The action ran, but its result was withheld because it was larger than 2,048 bytes or may contain personal data."`
  - `from privacyfence.pii_detector import detect_pii_categories`.
  - `PluginConnector.__init__` gains the keyword argument
    `owns_approval: Callable[[str], bool] | None = None` (stored as `self._owns_approval`; `None`
    means "owns nothing").
  - In `_execute`, after `ExecuteResult.from_wire`, build the value exactly as today (with
    `approval_id` merged in), then
    `return await self._screen_write_result(defn, value, result.approval_id)`. Remove the
    `INLINE_RESULT_BYTES` raise and the now-unused import. Withholding returns a result instead of
    raising, because an error would invite the AI to retry a write that already ran.

  ```python
  async def _screen_write_result(self, defn: ToolDef, value: Any, approval_id: str | None) -> Any:
      if _wire_size(value) > WRITE_RESULT_MAX_BYTES:
          logger.info("Plugin %s: result of %s withheld: over %d bytes", self._plugin, defn.name, WRITE_RESULT_MAX_BYTES)
          return await self._withheld(approval_id)
      categories = await asyncio.to_thread(detect_pii_categories, json.dumps(value, ensure_ascii=False))
      if categories:
          logger.info("Plugin %s: result of %s withheld: possible personal data (%s)", self._plugin, defn.name, ", ".join(categories))
          return await self._withheld(approval_id)
      return value

  async def _withheld(self, approval_id: str | None) -> dict:
      out: dict[str, Any] = {"withheld": True, "message": WRITE_RESULT_WITHHELD}
      if approval_id is not None and self._owns_approval is not None:
          if await asyncio.to_thread(self._owns_approval, approval_id):
              out["approval_id"] = approval_id
      return out
  ```

- **A withheld result keeps `approval_id` when, and only when, PrivacyFence issued that id to this
  plugin** (decided by the maintainer). The cap and the scan apply to the merged value, so an
  oversized or PII-carrying `approval_id` withholds the result; the id then survives only if it
  is genuinely one of this plugin's confirmation or approval cards, which the AI needs to follow
  up. Any other `approval_id` is dropped.
  - `ConfirmationService.owns(self, plugin: str, approval_id: str) -> bool` (`plugins/confirm.py`):
    under `self._lock`, `owned = self._owned.get(approval_id)`;
    `return owned is not None and owned.plugin == plugin`.
  - `ApprovalService.owns(self, plugin: str, approval_id: str) -> bool` (`plugins/approvals.py`):
    true when `self._owned.get(approval_id)` (under `self._lock`) belongs to `plugin`, or when a
    record in `self._store.for_plugin(plugin)` has that `approval_id` and `revoked_at is None`
    (`request()` hands out a stored approval's id, `approvals.py:279-281`).
  - `host.py`, where `on_ready` builds `PluginConnector`: pass
    `owns_approval=lambda approval_id: self._confirm.owns(plugin.name, approval_id) or self._approvals.owns(plugin.name, approval_id)`.

  It covers popup and `auto` writes (both go through `_execute`). It logs only; the call's own
  gate or auto audit row already exists. With PII detection off, only the cap applies.
- `pii_detector.py` module docstring (`:11-13`, "Write tools … are never scanned") gains: "A
  plugin write tool's result is the exception: it is scanned before it reaches the AI (ADR 0133)."
- SDK test host (`testing/_host.py`, `:496-507`): `_WRITE_RESULT_MAX_BYTES = 2048` and
  `_WRITE_RESULT_WITHHELD` with the same sentence. When the compact UTF-8 JSON of `released` is
  over the cap, `outcome.released = {"withheld": True, "message": _WRITE_RESULT_WITHHELD}`, plus
  `"approval_id"` when it is a key of `self._confirmations._cards` or `self._approvals._cards`
  (the test host runs one plugin), and `outcome.result` stays raw. The test host cannot run the daemon's detector, so only the cap is
  mirrored; its docstring says so.
- Out of scope: the SDK still refuses an execute result over 100,000 bytes (`plugin.py:760`).

### Windows owner (S3)

- `windows_acl.py`:

  ```python
  SYSTEM_SID = "S-1-5-18"
  ADMINISTRATORS_SID = "S-1-5-32-544"
  TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
  TRUSTED_OWNER_SIDS = frozenset({SYSTEM_SID, ADMINISTRATORS_SID, TRUSTED_INSTALLER_SID})

  def read_owner_sid(path: Path) -> str | None:
      """The string SID of ``path``'s owner, or None when it cannot be read (or off Windows)."""
  ```

  `read_owner_sid` does `GetFileSecurity(str(path), OWNER_SECURITY_INFORMATION).GetSecurityDescriptorOwner()`
  and `win32security.ConvertSidToStringSid(sid)`, with the import inside the `try`; any exception
  returns None, as `read_owner` does. Add it and the four constants to `__all__`. SIDs, not names,
  so a localized group name cannot slip past.
- `privilege_separation.py`:

  ```python
  def _windows_owner_problem(path: Path) -> str | None:
      from . import windows_acl
      sid = windows_acl.read_owner_sid(path)
      if sid is None:
          return f"could not read {path}'s owner"
      if sid in windows_acl.TRUSTED_OWNER_SIDS:
          return None
      return f"{path} is owned by {windows_acl.read_owner(path) or sid}, not by SYSTEM, Administrators or TrustedInstaller"

  def _windows_plugin_path_problem(path: Path, *, can_rewrite=None) -> str | None:
      return _windows_admin_only_write_problem(path, can_rewrite=can_rewrite) or _windows_owner_problem(path)
  ```

  The `win32` branches of `admin_only_write_problem`, `admin_only_ancestor_write_problem` and
  `admin_only_plugin_dir_write_problem` call `_windows_plugin_path_problem` with their existing
  `can_rewrite`. An unreadable owner fails closed. `_windows_script_elevation_problem` (`:2071`)
  is unchanged. `trust.admin_only_problem` is unchanged: the result is the existing
  `NOT_ADMIN_ONLY` reason, with the detail in the daemon log.
- Issue [#860](https://github.com/privacyfence/privacyfence/issues/860) (check every file in the
  plugin folder) stays separate; its walk will call these same functions.
- No ADR: this enforces ADR 0121's "only administrators can rewrite".

### The running log is capped (S4)

- Constants: `LOG_PUMP_CHUNK_BYTES = 64 * 1024`, `LOG_DRAIN_SECONDS = 2.0`.
- `supervisor.py`:
  - `_rotate_log(path: Path, *, force: bool = False)`: skip the size test when `force`.
  - `class _StderrLog` holding `path` and a `threading.Lock` (two pumps of one supervisor can
    overlap after a restart: the old child's pump is still draining when the new one starts), with
    `append(self, data: bytes) -> None`, whose whole body runs under that lock. While `data` is
    non-empty: open with `O_WRONLY | O_CREAT | O_APPEND`, mode 0o600 (and `fchmod` on POSIX, as
    `_open_log` does); `size = os.fstat(fd).st_size`; if `size >= LOG_MAX_BYTES`, close and
    `_rotate_log(path, force=True)` and continue; otherwise write `data[:LOG_MAX_BYTES - size]`
    fully (loop on `os.write`), close, and keep the rest. `LOG_MAX_BYTES` is read from the module
    global at call time. Opening per chunk means no fd is shared with children and no open handle
    blocks a rename on Windows. The file never exceeds the cap; a line may be split across files.
  - `async def _pump_stderr(stream: asyncio.StreamReader, log: _StderrLog, name: str) -> None`:
    `while chunk := await stream.read(LOG_PUMP_CHUNK_BYTES):`
    `await asyncio.to_thread(log.append, chunk)` (no blocking file I/O on the event loop,
    `docs/coding-and-testing-guidelines.md` §1.7); on `OSError` log one
    warning `"plugin %s: could not write its log: %s"` and keep draining and discarding, so a full
    disk never blocks the child.
  - `Supervisor.__init__`: `self._stderr_log = _StderrLog(spec.log_path)` and
    `self._pumps: set[asyncio.Task] = set()`.
  - `_spawn`: keep `fd = _open_log(spec.log_path)` then `os.close(fd)` (directory, start rotation,
    0600); pass `stderr=asyncio.subprocess.PIPE`; after `create_subprocess_exec`,
    `task = asyncio.create_task(_pump_stderr(proc.stderr, self._stderr_log, spec.name))`, add it to
    `self._pumps` with done-callback `self._pumps.discard`, and set `child.stderr_pump = task`.
    `_Child` gets `stderr_pump: asyncio.Task | None = None`.
  - `_reap`: after the process has exited and before `peer.close()`,
    `if child.stderr_pump is not None: await asyncio.wait({child.stderr_pump}, timeout=LOG_DRAIN_SECONDS)`.
    Never cancel a pump; it ends at EOF.
  - Module docstring line 3: "its stderr goes through the daemon into a private log capped at
    5 MiB while it runs".
  - **Never `await proc.wait()`.** With `stderr=PIPE`, `asyncio`'s `Process.wait()` on Python 3.11
    (3.11.17 included) returns only once every pipe is closed, so a grandchild that still holds the
    stderr pipe would block it for good (the fix, gh-119710, is not in 3.11; `requires-python` is
    `>=3.11`). `returncode` is set as soon as the process itself exits. So:

    ```python
    _EXIT_POLL_SECONDS = 0.05


    async def _wait_exit(proc: asyncio.subprocess.Process, timeout: float | None = None) -> bool:
        """True once ``proc`` has exited (its returncode is set), False when ``timeout`` ran out.

        Polls the returncode instead of awaiting ``proc.wait()``, which on Python 3.11 also waits
        for every pipe to close, and a grandchild can hold the stderr pipe open."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while proc.returncode is None:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_EXIT_POLL_SECONDS)
        return True
    ```

    `_Child._watch_exit` (`supervisor.py:119-121`) becomes `await _wait_exit(self.proc)` then
    `self.mark_gone("exit")`; `Supervisor._exited` (`:412-418`) becomes
    `return await _wait_exit(child.proc, timeout)`; `_reap`'s `await child.proc.wait()` becomes
    `await _wait_exit(child.proc)`. Nothing else in `supervisor.py` awaits `proc.wait()`
    afterwards (`grep -n 'proc.wait()' src/privacyfence/plugins/supervisor.py` prints nothing).
- Disk use is bounded at 4 × 5 MiB. No ADR (ADR 0120 already says "a private rotating log").

### Source reads: one version, UTF-8 bytes (D1, D3, D2, P5)

**Sheets snapshot (D1).** Keep a snapshot; A1 range arithmetic was rejected (it cannot handle
named ranges, R1C1, quoted sheet names or whole-sheet ranges without a metadata call, and would not
fix mixed versions).

- `spool.py` `DownloadSpool`:
  - `_SNAPSHOTS_PER_PLUGIN = 4` (module constant).
  - `@dataclass class _RowsEntry: path: Path; count: int; last_used: float`, and
    `self._rows: dict[tuple[str, str], _RowsEntry] = {}` in `__init__`.
  - `put_rows` and `rows_page` call `self.sweep()` first, and every read or change of `_rows`
    happens under `self._lock` (the spool's existing `threading.Lock`); file writes and reads
    happen outside it.
  - `put_rows(self, plugin: str, rows: list) -> str`: `snapshot = secrets.token_hex(8)`; one line
    per row, each `_row_line(row)`, joined with `b"\n"`; `atomic_write_bytes(root/<plugin>/sheets-<snapshot>.jsonl, data, mode=0o600)`
    (create the plugin folder as `_fetch` does); index under `(plugin, snapshot)`; when the plugin
    holds more than `_SNAPSHOTS_PER_PLUGIN`, evict its least recently used and unlink the file.
  - `_row_line(row) -> bytes`: `json.dumps(row, default=str, ensure_ascii=False).encode()`, and on
    `UnicodeEncodeError` (a lone surrogate) `json.dumps(row, default=str).encode()`. Default
    separators, so the size matches `source_ops._encoded_size`.
  - `rows_page(self, plugin: str, snapshot: str, first: int, budget: int) -> tuple[list, int, int]`
    returns `(rows, end, total)`. `KeyError` when the snapshot is unknown or its file is gone.
    Updates `last_used`. Reads the file as bytes and splits it on `b"\n"` only (never
    `str.splitlines`, which also splits on U+2028, U+2029 and U+0085, which `ensure_ascii=False`
    leaves raw inside a JSON string). Reads lines from `first` and adds rows while
    `2 + sum(len(line)) + 2 * (n - 1) <= budget`: the exact byte size of the JSON list with default
    separators. When `first >= total` it returns `([], first, total)`. When `first < total` and not
    even one row fits, it raises `RpcError("payload_too_large", "a single record is larger than the page limit")`.
  - `sweep()` and `clear()` also cover `_rows` with `DRIVE_SPOOL_IDLE_SECONDS` (600).
- `source_ops.py` `_run_sheets`:
  - First call (`state is None`): fetch once, `_fit_prefix` as today. If everything fits, return
    with no cursor and write no spool file. Otherwise `snap = spool.put_rows(params["plugin"], values)`,
    return the first slice, and the cursor state `{"k": end, "s": snap}`.
  - Continuation: `_state_keys(state, {"k", "s"})`, `k = _state_count(state, "k")`; `s` must be a
    string matching `[0-9a-f]{16}` (fullmatch), else `_bad_cursor()`. Call
    `spool.rows_page(params["plugin"], s, k, SOURCE_PAGE_BUDGET_BYTES)` and make no provider call.
    `KeyError` → `RpcError("upstream_error", "the rows this cursor pointed at are no longer held; read the range again from the start", extra={"reason": "cursor_expired"})`.
    `k > total` → `_bad_cursor()`. Return `{"values": rows, "first_row": k}` and the next cursor
    `{"k": end, "s": s}` when `end < total`, else no cursor.

**Confluence version (D3).**

```python
def _page_changed() -> RpcError:
    return RpcError("upstream_error", "the page changed while it was being read", extra={"reason": "revision_changed"})
```

On a continuation `_state_keys(state, {"o", "v"})`, `start = _state_count(state, "o")`,
`version = _state_count(state, "v")`. After `get_page`, `if page["version"] != version: raise _page_changed()`,
before the `start > len(body)` check. The cursor state is `{"o": end, "v": page["version"]}`.
Cursors issued before the deploy become `invalid_params`; plugins restart with the daemon.

**Non-exportable Google types (D2).** In `DownloadSpool.read_chunk_at`, after the revision check
and before the binary branch:

```python
if metadata.mime_type.startswith("application/vnd.google-apps.") and metadata.mime_type not in _GOOGLE_DOC_EXPORTS:
    raise RpcError("invalid_params", f"files of type {metadata.mime_type} cannot be downloaded",
                   extra={"reason": "not_downloadable"})
```

The test host's `_serve_drive` does the same on the fixture's `mime_type` (p7).

**UTF-8 bytes (P5).** The daemon measures in UTF-8 everywhere; changing the test host to ASCII was
rejected (it halves the limit for accented text).

- `source_ops.py`: `def _utf8_json(value: Any) -> bytes` returns
  `json.dumps(value, default=str, ensure_ascii=False).encode()`, and on `UnicodeEncodeError`
  `json.dumps(value, default=str).encode()`. `_encoded_size(v) = len(_utf8_json(v))`. In `_serve`:
  `body = _utf8_json(data)`; the cap is `len(body) > MAX_SOURCE_RESULT_BYTES`;
  `seen["bytes"] = str(len(body))`; return `"data": json.loads(body), "bytes": len(body)`.
- `rpc.py` `_send`: encode with `ensure_ascii=False` (keeping `separators=(",", ":")` and
  `allow_nan=False`) and `.encode()`; on `UnicodeEncodeError` retry with `ensure_ascii=True`.
  Required: 12 MiB of CJK text escaped would be about 24 MiB, over `MAX_LINE_BYTES` (16 MiB).

**Hotfix regression through `source.call`.** With the hotfix in, `SalesforceClient.run_report`
raises `SalesforceClientError` for a malformed id before any HTTP call, and
`ConfluenceClient.get_page` raises `ConfluenceClientError`. Both are in `_CLIENT_ERRORS`, so
`source.call` answers `upstream_error` with detail `"the service returned an error"`.
`source_ops`' own parameter checks stay as they are (the client validates).

### Prepared calls and waits in the SDK (P1, P2, P7)

- **P1.** `plugins/constants.py`:

  ```python
  PENDING_CARD_SECONDS = 900.0       # = approvals.DEFAULT_PENDING_TTL_SECONDS
  DECISION_REPLAY_SECONDS = 300.0    # = approvals.DEFAULT_LEDGER_TTL_SECONDS
  PREPARED_CALL_LIFETIME_SECONDS = PENDING_CARD_SECONDS + DECISION_REPLAY_SECONDS   # 1200.0
  ```

  `plugins/confirm.py:91` and `plugins/approvals.py:251` (`retain_finished_seconds`) switch to
  `PENDING_CARD_SECONDS`, so their behaviour does not change. `connector.py:321` keeps
  `PREPARED_CALL_LIFETIME_SECONDS` (now 1,200; `:282` still overrides it with the registry's TTLs).
  SDK `plugin.py:52`: `_PREPARED_CALL_LIFETIME_SECONDS = 1200.0` (pinned by `TestLimits`).
  Residual risk, documented: a user who raises `approvals.pending_ttl_seconds` or
  `ledger_ttl_seconds` can make the daemon outlive this; such a write then fails with "lost track
  of this call", never silently.
- **P2.** SDK `plugin.py`, outside the `_limits` block: `_MAX_PREPARED_CALLS = 256`. In `_prepare`,
  after `_sweep()`, `while len(self._prepared) >= _MAX_PREPARED_CALLS:` pop the oldest (dict order
  is prepare order) and `logger.warning("prepared-call store is full (%d); dropped the oldest prepared call", _MAX_PREPARED_CALLS)`.
  In `_execute`: `if not handle.read_only or approval.get("via") == "auto": del self._prepared[call_id]`
  (the daemon never replays an auto read). An evicted id gets the existing `unknown_call`.
- **P7.** SDK `plugin.py`: `_WAIT_MAX_ROUNDS = 12`. Add to both `ConfirmClient` and
  `ApprovalsClient`:

  ```python
  async def wait(self, approval_id: str) -> ConfirmResult:
      """Wait until the card is approved, denied or expired."""
      for _ in range(_WAIT_MAX_ROUNDS):
          try:
              return await self.await_(approval_id)
          except SourceError as exc:
              if exc.code != "timeout":
                  raise
      raise SourceError("timeout", "the confirmation was not decided within an hour")
  ```

  `today_plugin.py:372`: `verdict = await ctx.confirm.wait(approval_id)`; keep
  `except SourceError:` with its comment.

### The test host follows the manifest, the parameters and the PII check (P3, P4, P6)

- **P3.** `PluginTestHost(plugin, ..., source_operations: Iterable[str] = (), pages: bool = False)`,
  mirroring the manifest's keys and defaults (the SDK has no YAML parser, so it cannot read the
  manifest file).
  - An operation outside `SOURCE_OPERATIONS` raises `ValueError(f"unknown source operation {op!r}")`;
    store a `frozenset`.
  - `SourceFixtures` gets the allowed set and refuses with exactly
    `RpcError("operation_not_allowed", "the plugin may not use this operation")` (replace the
    current text at `_source.py:178` with this).
  - `request()`/`get()`: after `normalize_path` and its 400, `if not self.pages` answer
    `404 b"Not Found"` with `SECURITY_HEADERS` and send no `web.request`. Implement it as
    `_pages.serve(..., enabled=self.pages)`.
  - `_host.py:241` becomes `lambda: self.pages`. `introspect()` is unaffected.
  - D2's test-host check (above) goes into `_serve_drive` in the same phase.
- **P4.** New `plugin-sdk/src/privacyfence_plugin_sdk/testing/_params.py`, headed "Copied from
  privacyfence.plugins.source_ops; tests/unit/plugin_sdk/test_testhost_params.py compares them."
  - `validate(operation: str, params: dict) -> None` raises `RpcError("invalid_params", <the daemon's exact detail>)`.
    It checks in the daemon's order (for example Sheets checks `value_render_option` first), and a
    present `None` counts as missing, as `_str` does.
  - It copies `_MAX_ID_CHARS = 256`, `_MAX_RANGE_CHARS = 512`, `_MAX_TIME_CHARS = 64`,
    `_MAX_JQL_CHARS = 8192`, `CURSOR_MAX_CHARS`, `JIRA_PAGE_SIZE_MAX`, `CALENDAR_PAGE_SIZE_MAX`, the
    legacy `max_results` bound, `_VALUE_RENDER_OPTIONS`, the 12 `REPORT_FILTER_OPERATORS`
    (`salesforce_client.py:112-115`) and `_FILTERS_SHAPE_ERROR` with its shape rules
    (`connectors/salesforce.py:160-189`).
  - `_source.py` `_serve`: after the allowlist and the dict check, if the best-matching fixture's
    error code is `connector_unavailable`, raise that first (the daemon checks the connector before
    params); then `_params.validate(operation, call_params)`; then match fixtures. Remove the
    duplicated checks in `_serve_drive` (`:223-230`), keeping its cursor and revision logic.
- **P6.** `PluginTestHost(..., pii: Callable[[str], bool] | None = None)`; `None` flags nothing.
  - `testing/_gate.py`: a copy of `flatten_text` (`plugins/blocks.py:256-271`) and
    `Card.pii_flagged: bool = False`.
  - In `_run_call`, for `tool["gate"] == "review"`: `text = flatten_text(card.payload)` for a
    read-only tool, else `json.dumps({"plugin": name, "tool": tool["name"], "scopes": card.scopes}, default=str, indent=2, ensure_ascii=False)`;
    `flagged = bool(self._pii and self._pii(text))`. When flagged, skip the rule match, set
    `card.pii_flagged = True`, show the card, and add `"pii_detected": True` to that audit entry.
    One approval stands for the card and the "Are you sure?" step. `auto` and `popup` gates are
    never scanned.

### Protocol 1.2.0 (p13)

- `PROTOCOL_VERSION = "1.2.0"` (daemon `constants.py` and SDK `plugin.py`). Version 1.2 adds one
  daemon-to-plugin request, `pages.list`, and caps what reaches the AI from a write tool's
  `tool.execute` (the message itself is unchanged).
- Who is asked: only a running plugin whose manifest has `pages: true`.
  `TIMEOUT_SECONDS["pages.list"] = 10.0`. Params: `{"principal": PrincipalContext}`. Result:
  `{"pages": [PageEntry, …]}`, at most `MAX_PAGE_INDEX_ENTRIES = 500`, in the order the browser
  shows them.
- New constants: `MAX_PAGE_INDEX_ENTRIES = 500`, `MAX_PAGE_VERSION_CHARS = 40`,
  `MAX_PAGE_DESCRIPTION_CHARS = 200`,
  `PAGE_ENTRY_PATH_RE = re.compile(r"/[\x21\x22\x24-\x5b\x5d-\x7e]*")   # always .fullmatch()`.

| Field | Rule |
|---|---|
| `path` | Required. 1 to `MAX_PAGE_PATH_CHARS` (512) characters, fullmatching `PAGE_ENTRY_PATH_RE`: printable ASCII with no space, `#` or backslash. The schema's `pattern` is `^` + that + `$`. It may carry a query after the first `?`. The part before `?` must pass `plugins.pages.normalize_path` unchanged (`normalize_path(p) == p`: already decoded, no NUL or `//`), and none of its segments may be `.` or `..`. |
| `title` | Required, 1 to `MAX_TITLE_CHARS` (120) characters, not only whitespace, with `blocks.clean_line(title) == title`. The schema carries the shared one-line pattern, as `display_name` does. |
| `version` | Optional, 1 to `MAX_PAGE_VERSION_CHARS` (40) characters, same one-line pattern. |
| `created_at`, `updated_at` | Optional. RFC 3339 with a time zone: `datetime.fromisoformat` accepts it after a trailing `Z` is replaced by `+00:00`, and the result has `tzinfo`. |
| `description` | Optional, 1 to `MAX_PAGE_DESCRIPTION_CHARS` (200) characters, same one-line pattern. |

- Validation is whole-list, like tool definitions: the first bad entry fails the result with
  `RpcError("invalid_params", detail)`, and `detail` starts with `pages[<i>].<field>` (for example
  `pages[3].title must not be empty`, the shape the existing helpers produce). A result over 500
  entries fails with a detail starting `pages`. Unknown fields are ignored.
- `protocol.py`: frozen `PageEntry` (fields above, `from_wire`, `to_wire`, `WIRE_KEYS`) and
  `PagesListResult(pages: tuple[PageEntry, ...])` with `from_wire(obj, *, mode="local")` and
  `WIRE_KEYS`, as `WebResponse` (`protocol.py:603-609`) does. Both in `__all__`.
  `normalize_path` is imported inside `PageEntry.from_wire` (`from privacyfence.plugins.pages import normalize_path`),
  because `pages.py` imports `protocol` at module level.
- Schema: `x-protocol-version` `"1.2.0"`; `$defs` `PagesListParams`, `PageEntry` and
  `PagesListResult`, with the one-line pattern on `title`, `version` and `description` and the path
  pattern on `path`; `x-limits` gains `MAX_PAGE_INDEX_ENTRIES`, `MAX_PAGE_VERSION_CHARS`,
  `MAX_PAGE_DESCRIPTION_CHARS`, `SEND_TIMEOUT_SECONDS` (10.0), `WRITE_RESULT_MAX_BYTES` (2048), and
  `TIMEOUT_SECONDS["pages.list"]` (10.0). Then regenerate `types.py` with
  `python3 scripts/gen_plugin_sdk_types.py`; never edit or hand-merge it. `types.py` gains a
  `PageEntry` TypedDict next to the SDK's `PageEntry` dataclass; that is harmless.
- **Only p13 edits `constants.PROTOCOL_VERSION`, the schema and `types.py`.** Every other phase
  that adds a constant leaves the schema alone; `test_x_limits_match_the_constants` checks only
  that each `x-limits` key exists in `constants.py`.

### SDK page index (p14)

- `PageEntry(path: str, title: str, version: str | None = None, created_at: str | None = None, updated_at: str | None = None, description: str | None = None)`,
  a frozen dataclass in `responses.py`, exported from `__init__`, with `to_wire()` that leaves out
  `None` fields.
- `Plugin.page_index(fn)`: a decorator registering `async def fn(ctx) -> list[PageEntry]` on
  `_Registry.page_index`. A second registration raises
  `ValueError("page_index is already registered")`.
- `serve()` adds `"pages.list": self._pages_list` to its handlers only when a page index is
  registered; without one the request stays `method_not_found`. `_pages_list` checks the plugin is
  initialized, builds a `Context` for the principal as `_web_request` does, awaits the function,
  converts each entry with `to_wire()`, validates and returns `{"pages": [...]}`. A violation
  raises `RpcError("invalid_params", detail)`.
- The SDK's copy of the rules lives in a new private module `_page_index.py`:
  `validate_page_entries(entries: list[dict]) -> list[dict]` and
  `normalized_page_path(raw: str) -> str | None`, with private constants
  `_MAX_PAGE_INDEX_ENTRIES`, `_MAX_PAGE_PATH_CHARS`, `_MAX_TITLE_CHARS`, `_MAX_PAGE_VERSION_CHARS`,
  `_MAX_PAGE_DESCRIPTION_CHARS`, `_PAGE_ENTRY_PATH_RE`. It uses the SDK's own `clean_line`
  (`blocks.py`). `testing/_pages.normalize_path` becomes a thin call to `normalized_page_path`, so
  the SDK holds one copy. The SDK never imports `privacyfence` (ADR 0126).
- `PluginTestHost.list_pages(principal: str | None = None) -> list[dict]` sends `pages.list` with
  `_TIMEOUTS["pages.list"]` (add the key, 10.0). It raises `LookupError` when `self.pages` is false
  (the manifest stand-in from P3). `method_not_found` gives `[{"path": "/", "title": <plugin name>}]`.
  An invalid result raises `AssertionError` with the validation detail.
- The `today` example registers a page index listing `/` only (`/approval` is framed by the
  approval card and is not a page to browse), and its README's protocol line reads 1.2.0.

### Host page index (p15)

New module `src/privacyfence/plugins/page_index.py`:

```python
PAGE_INDEX_NO_ANSWER = "The plugin could not list its pages."
PAGE_INDEX_INVALID = "The plugin returned an invalid page list."

@dataclass(frozen=True)
class PageIndex:
    name: str                       # plugin name
    display_name: str
    entries: tuple[PageEntry, ...]  # empty when error is set
    error: str = ""                 # PAGE_INDEX_NO_ANSWER, PAGE_INDEX_INVALID or ""

def index_from_result(name: str, display_name: str, result: Any = None, error: RpcError | None = None) -> PageIndex
```

- `index_from_result` mapping (it follows the rpc codes as A3 leaves them):
  - `error.code == "method_not_found"` → one entry `PageEntry(path="/", title=display_name)`;
  - `error.code == "invalid_params"`, or `PagesListResult.from_wire(result)` raising `RpcError` →
    `PAGE_INDEX_INVALID`;
  - every other `RpcError` (`timeout`, including a send timeout; `internal_error` for a closed peer
    or a crash inside `page_index`; `invalid_request` when the SDK is busy) →
    `PAGE_INDEX_NO_ANSWER`.
  - Errors are logged at WARNING with the plugin name and the code, never the result.
- `host.py` gains only:
  - `async def list_pages(self, name: str, principal: Principal) -> PageIndex`: modelled on
    `web_request` (`host.py:887`). A plugin that is not running, or has `pages: false`, raises
    `LookupError`. It sends
    `peer.request("pages.list", {"principal": self._request_context(name, manifest, principal)})`
    with `manifest` the plugin's discovered manifest, as `web_request` does.
  - `async def list_all_pages(self, principal: Principal) -> list[PageIndex]`: every running plugin
    with `pages: true`, concurrently (`asyncio.gather`); a plugin whose `list_pages` raises
    `LookupError` is left out. Sorted by `(display_name.casefold(), display_name, name)`, the
    Plugins menu's order.
  - Neither method takes the host's action lock.
  - `page_links()` returns `(display name, f"/plugin-pages/{name}")`; `rows()` sets
    `"page_url": f"/plugin-pages/{name}"` under the same conditions as today.

### Page browser, menu and card (p16)

- New `src/privacyfence/web/routes_plugin_browser.py`:

  ```python
  def build_routes(
      plugin_host: PluginHost, *, is_owner_session: Callable[[Request], bool],
      notifications_enabled: bool = True, notifications_detail: str = "minimal",
  ) -> list[BaseRoute]
  ```

  - `Route("/plugin-pages", all_pages, methods=["GET"], name="plugin_pages")` and
    `Route("/plugin-pages/{name}", one_plugin, methods=["GET"], name="plugin_pages_one")`.
  - Both answer `PlainTextResponse("Not Found", status_code=404)` unless `is_owner_session(request)`,
    as `routes_plugins.py` does.
  - `one_plugin` checks `PLUGIN_NAME_RE.fullmatch(name)` (404 otherwise), then
    `await plugin_host.list_pages(name, current_principal())`; `LookupError` gives the same 404.
    `all_pages` calls `await plugin_host.list_all_pages(current_principal())`. `current_principal`
    is imported with `from ..principal import current_principal`, as `routes_plugins.py:33` and
    `:102` do.
  - Both render `web_shell.wrap(body, title="PrivacyFence — Plugin pages", active="plugin-pages", nonce=csp.nonce_for(request), notifications_enabled=notifications_enabled, notifications_detail=notifications_detail, plugin_pages=tuple(plugin_host.page_links()))`
    as an `HTMLResponse` with `Cache-Control: no-store`.
- `server.py`: mount with
  `_owner_only_routes(build_plugin_browser_routes(plugin_host, is_owner_session=lambda request: _is_human_session(request, sessions), notifications_enabled=…, notifications_detail=…))`
  right after the plugin page routes (`:1063-1068`), only when `plugin_host is not None`, passing
  the same notification settings the approvals app gets. `/plugin-pages` does not start with
  `/plugins/`, so it gets the app's nonce CSP, not the sandbox.
- New `src/privacyfence/plugin_browser_html.py`, a pure renderer with no Starlette import:

  ```python
  def render(indexes: list[PageIndex], *, single: bool, nonce: str) -> str
  def format_timestamp(value: str | None) -> str   # "YYYY-MM-DD HH:MM UTC", converted to UTC; "" for None
  ```

  - `<h1>` reads `Plugin pages`, or the plugin's display name when `single`; when `single`,
    `<p><a href="/plugin-pages">All plugin pages</a></p>` follows it.
  - With no plugin (`not single` and no indexes): `<p id="no-plugins">No plugin with pages is running.</p>`.
  - Each index is `<section class="pf-plugin-pages" data-plugin="<name>">` with an `<h2>` of the
    display name (left out when `single`), then one of: `<p class="pf-plugin-pages-error">` with
    the error sentence; `<p class="pf-plugin-pages-empty">This plugin lists no pages.</p>`; or a
    `<table>` with the header row Title, Version, Created, Updated.
  - A row's Title cell is `<a href="/plugins/<name><path>" target="_blank" rel="noopener">{title}</a>`,
    with `<div class="pf-plugin-pages-desc">{description}</div>` below when present. Version is
    `version` or `—`; Created and Updated use `format_timestamp`, or `—`.
  - Every value goes through `html.escape(…, quote=True)`, the `href` included; the `href` is the
    literal `"/plugins/" + name + path`.
  - No inline script. Styles reuse the shell's tokens (`var(--ink)`, `var(--line)`,
    `var(--surface)`) in one `<style nonce="…">` block.
- **Menu** (`web_shell.py`): `_plugins_html()` and `_STREAM_JS`'s `updatePluginsMenu()` put
  `All plugin pages` → `/plugin-pages` first whenever the menu shows, then one item per plugin →
  its `page_url` (`/plugin-pages/<name>`). No `target` or `rel` on any menu item. The click handler
  at `:542-549` stays; its comment becomes "Close the menus when a plugin link is picked: the link
  navigates, and a page restored from the back/forward cache would otherwise show them open."
- **Card** (`settings_window_html.py:1231-1233`): the link reads `Pages`, has
  `aria-label="Pages of <display name>"`, no `target` or `rel`, and points at `p.page_url`.

### Pages conformance (p17)

The echo fixture (`tests/fixtures/plugins/echo/echo_plugin.py`) registers a page index with two
entries: `/` titled "Echo home", and `/events?limit=1` titled "Echo events" with version "1" and
`updated_at` "2026-10-09T10:00:00Z" (both pages exist). One scenario in
`tests/integration/test_sdk_testhost_conformance.py` compares the test host's `list_pages()` with
the daemon host's `list_pages(...).entries` converted with `to_wire()`. The fallback title differs
(display name on the daemon, plugin name on the test host); the file's "Known differences"
docstring gains that line, and the fallback is covered by unit tests on each side.

### Reference docs, ADRs and changelog (p18 to p21)

Code phases change docstrings only. Every reference-doc, ADR and `CHANGELOG.md` edit happens in
four chained docs phases, p18 to p21, which run just before the retire phase p22. That is a
deliberate departure from "the last phase writes the ADRs": one docs phase per ADR keeps each
small, and chaining them keeps `docs/adr/README.md`, `CHANGELOG.md` and the reference docs out of
every code phase, so no two concurrent phases edit them. The ADR numbers follow the order the
phases write them (0132 in p18 to 0135 in p21). They edit by section heading, not line number.

`docs/security-and-compliance.md`'s "Plugins" section names the plugin ADRs as two pairs of
Markdown links, "[ADR 0120](…) to [ADR 0126](…), and in [ADR 0127](…) to [ADR 0131](…)". Each
docs phase replaces the last link of the second pair with a link to the ADR it writes (p18:
`[ADR 0132](adr/0132-a-plugin-that-stops-reading-its-input-for-10-seconds-is-treated-as-crashed.md)`, p19: `[ADR 0133](adr/0133-a-plugin-writes-result-is-capped-and-pii-scanned-before-it-reaches-the-ai.md)`, p20:
`[ADR 0134](adr/0134-paged-source-reads-read-one-version-and-measure-utf8-bytes.md)`, p21: `[ADR 0135](adr/0135-plugin-pages-are-listed-by-the-plugin-and-browsed-in-privacyfence.md)`). All new `CHANGELOG.md`
lines go under `## [Unreleased]` → `### Added` (the plugin framework is unreleased), extending the
existing plugin bullets; no version heading.

**p18, ADR 0132 (the send-timeout crash rule) and the runtime behaviour.**

- New `docs/adr/0132-a-plugin-that-stops-reading-its-input-for-10-seconds-is-treated-as-crashed.md`, title "ADR 0132: A plugin that stops reading its input for 10
  seconds is treated as crashed". Status: "Accepted — <the date the phase runs>. Implemented:
  `src/privacyfence/plugins/rpc.py` (`_send`), `src/privacyfence/plugins/supervisor.py`
  (`_stop_child`), `src/privacyfence/plugins/constants.py` (`SEND_TIMEOUT_SECONDS`)." then on its
  own line "Amends [ADR 0120](0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md)."
  Context: a plugin that stopped reading its stdin froze Disable, Delete data and daemon shutdown,
  because a write to it waited for good and the stop notices were sent before any kill. Decision:
  every message the daemon writes to a plugin must be taken within `SEND_TIMEOUT_SECONDS` (10 s,
  in the schema's `x-limits`); otherwise the peer is closed and the plugin is treated as crashed
  (the existing backoff and crash limit); stop notices get 1 s each, and a plugin that did not take
  them is terminated without the 5 s grace. Alternatives: a timeout scaled by message size (more
  complex, and a plugin that does not read for 10 s is not serving its 10 s requests either);
  waiting without a limit (today's freeze). Consequences: a plugin whose loop is blocked for more
  than 10 s while the daemon sends it more than about 128 KiB is restarted as a crash; stopping a
  stuck plugin takes about 3 s; connector events reach plugins concurrently. Verification:
  `tests/unit/plugins/test_rpc.py`, `tests/unit/plugins/test_supervisor.py`,
  `tests/unit/plugins/test_host.py`. Related: issue 846, ADR 0120.
- ADR 0120's Status gains on its own line: "Amended by [ADR 0132](0132-a-plugin-that-stops-reading-its-input-for-10-seconds-is-treated-as-crashed.md): a plugin that stops reading its input is a crash."
- `docs/adr/README.md`: a row for 0132 ("A plugin that stops reading its input for 10 seconds is
  treated as crashed | Accepted; amends 0120"), and 0120's status cell becomes "Accepted; amended
  by 0132".
- `docs/security-and-compliance.md`: the ADR link replacement above (0131 → 0132).

- `docs/plugin-protocol.md`:
  - `## Transport`, after the bullet on bad lines: "A plugin that does not read a message the
    daemon sends within 10 seconds (`SEND_TIMEOUT_SECONDS`) is treated as crashed
    ([ADR 0132](adr/0132-a-plugin-that-stops-reading-its-input-for-10-seconds-is-treated-as-crashed.md))."
  - `### tool.prepare`, at the end: "The SDK keeps a prepared call for 20 minutes, the card's
    pending lifetime plus the window in which an approval is replayed, and at most 256 at a time.
    If you raise `approvals.pending_ttl_seconds` or `approvals.ledger_ttl_seconds`, a write
    approved after that fails with "lost track of this call" rather than running."
  - `### Confirmations` and `### Approvals`: "A card stays pending up to 15 minutes, longer than one
    await: call `confirm.await` (`approval.await`) again after `timeout` until it answers a final
    status. The SDK's `ctx.confirm.wait()` (`ctx.approvals.wait()`) does."
  - `## Limits and timeouts`: a row "Send timeout (a message the plugin does not read) | 10
    seconds".
- `docs/plugins.md`:
  - `## The plugins directory` (Windows paragraph) and `## Installing a plugin` (the Windows step):
    "…when no account but administrators, SYSTEM and TrustedInstaller can write to it and one of
    them owns it".
  - The state table row `executable is writable by non-administrators`: its details end with "On
    Windows, it is also refused when a file or folder is owned by an account other than SYSTEM,
    Administrators or TrustedInstaller."
  - `## Logs`: "The file is limited to 5 MiB while the plugin runs, with three older copies kept".
  - `## Disabling, deleting data, removing`: a sentence after the list: "Any of the plugin's
    approval or confirmation cards still waiting for you expire when you disable it, delete its
    data or remove it, and when PrivacyFence disables it after repeated crashes. Actions on plugins
    run one at a time: an action you start while another runs waits for it."
  - `## Why a plugin does not start`, after its table: "A plugin that stops reading what
    PrivacyFence sends it for 10 seconds is restarted like a crash."
  - `## Outputs`: "A read of a file that changed after you approved it shows a new card."
  - `## Writing a plugin` (the sentence listing `ctx.approvals` (`request`, `check`, `await_`)):
    add `wait` to that list, and add `ctx.confirm.wait()` with "waits until the card is approved,
    denied or expired".
  - `### Testing a plugin` and its differences list: pass the manifest's `source_operations` and
    `pages` to `PluginTestHost`; it checks source-call parameters as PrivacyFence does; `pii=` lets
    a test model the PII check that overrides an "Always allow" rule; it withholds a write result
    over 2,048 bytes but does not run PrivacyFence's PII detector on it.
  - The runnable example under `### Testing a plugin` (the block with
    `async with PluginTestHost(plugin) as host:` and `host.source.load(samples.get("calendar.list_events"))`)
    becomes `async with PluginTestHost(plugin, source_operations=("calendar.list_events",)) as host:`;
    any example there that calls `host.get(` or `host.request(` also passes `pages=True`.
- `docs/security-and-compliance.md`, "Trust model": the same Windows owner wording.
- `plugin-sdk/README.md`: "Prepared state is kept for 20 minutes, at most 256 at a time; the oldest
  goes first."; the Approvals example uses `ctx.approvals.wait(...)` and a one-line confirm
  example uses `ctx.confirm.wait(...)`; the testing section gets the same test host points as
  `plugins.md`, and its runnable example (`### Testing a plugin`, the
  `async with PluginTestHost(plugin) as host:` block) gets the same
  `source_operations=("calendar.list_events",)` (and `pages=True` where it calls `host.get`).
- `CHANGELOG.md`:
  - New bullet after "Plugin child processes": "- **Plugin hardening.** A plugin that stops
    reading its input is treated as crashed after 10 seconds, so **Disable**, **Delete this
    plugin's data** and shutdown no longer hang on it, and one stuck plugin no longer delays
    connector events to the others. Plugin actions in Settings run one at a time, so overlapping
    actions cannot leave a second copy of a plugin running. A plugin's waiting approval and
    confirmation cards expire when it is disabled, its data is deleted or it is removed. A
    plugin's log stays within 5 MiB while it runs. On Windows a plugin's executable and folders
    must also be owned by SYSTEM, Administrators or TrustedInstaller. A repeated read of a plugin
    output file that changed after its card shows a new card."
  - Extend "Plugin rules shared by PrivacyFence and the SDK" with: "`PluginTestHost` takes the
    manifest's `source_operations` and `pages`, checks source-call parameters as PrivacyFence
    does, and can model the PII check that overrides an "Always allow" rule (`pii=`). The SDK keeps
    a prepared call for 20 minutes, at most 256 at a time, and `ctx.confirm.wait()` and
    `ctx.approvals.wait()` wait out a card's whole life, so the `today` example publishes after a
    late approval."

**p19, ADR 0133 (write results).**

- New `docs/adr/0133-a-plugin-writes-result-is-capped-and-pii-scanned-before-it-reaches-the-ai.md`,
  title "ADR 0133: A plugin write's result is capped at 2,048 bytes and PII-scanned before it
  reaches the AI". Status: "Accepted — <the date the phase runs>. Implemented:
  `src/privacyfence/plugins/connector.py` (`_screen_write_result`),
  `src/privacyfence/plugins/constants.py` (`WRITE_RESULT_MAX_BYTES`),
  `plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py`." then on its own line
  "Amends [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md)."
  Decision: "The result of a non-read-only plugin tool, with any `approval_id` merged in, reaches
  the AI only if its JSON is at most 2,048 bytes and the local PII detector finds nothing;
  otherwise the AI gets a fixed sentence saying the action ran and its result was withheld."
  Alternatives: a second card after execute (doubles the cards per write); no result at all
  (breaks the confirmation and approval id hand-back); raising an error (invites a retry of a
  write that already ran). Consequences: a plugin that returned large write results now hands
  them to the AI through an output file; with PII detection off only the cap applies; the SDK
  still accepts up to 100,000 bytes, so a plugin's own tests do not catch the cap unless they use
  `PluginTestHost`; a withheld result keeps `approval_id` only when PrivacyFence issued that id
  to this plugin (one of its confirmation or approval cards, or a stored approval of it), and any
  other `approval_id` is dropped, so the field cannot carry data past the cap. Verification:
  `tests/unit/plugins/test_connector.py`,
  `tests/unit/plugin_sdk/test_testhost.py`. Related: issue 846, ADR 0122, ADR 0130.
- ADR 0122's Status gains on its own line: "Amended by [ADR 0133](0133-a-plugin-writes-result-is-capped-and-pii-scanned-before-it-reaches-the-ai.md): a write's result is capped and PII-scanned."
- `docs/adr/README.md`: a row for 0133 ("A plugin write's result is capped at 2,048 bytes and
  PII-scanned before it reaches the AI | Accepted; amends 0122"), and 0122's status cell becomes
  "Accepted; amended by 0133".
- `docs/plugin-protocol.md` `### tool.execute`: replace "at most 100,000 bytes serialized" with:
  "For any other tool `result` (with `approval_id` merged in) reaches the AI only when its JSON is
  at most 2,048 bytes (`WRITE_RESULT_MAX_BYTES`) and PrivacyFence's PII check finds nothing.
  Otherwise the AI gets `{"withheld": true, "message": "The action ran, but its result was withheld because it was larger than 2,048 bytes or may contain personal data."}`,
  plus `approval_id` when that id is one PrivacyFence issued to this plugin (a confirmation or
  approval card, or a stored approval); any other `approval_id` is dropped. Put larger results in
  an output file." `## Limits and timeouts` gains "Write tool result
  reaching the AI, serialized | 2,048 bytes".
- `docs/security-and-compliance.md` "What a plugin can do": "a write's result reaches the AI only
  when it is at most 2,048 bytes and the PII check finds nothing", and the sentence "A plugin can
  therefore get its own data to the AI only through a card or a rule you wrote" stays true with
  that addition. The ADR link replacement above (0132 → 0133).
- `docs/plugins.md` `## Writing a plugin`, after the sentence "A tool that is not read-only
  attaches the function that does the work with `@greet.execute`.": "What that function returns
  reaches the AI only when it is at most 2,048 bytes and the PII check finds nothing; otherwise
  the AI is told the action ran and its result was withheld."
- `CHANGELOG.md`: new bullet after "Plugin hardening": "- **Plugin write results.** The result of
  a plugin's write tool reaches your AI client only when it is at most 2,048 bytes and the PII
  check finds nothing; otherwise the AI is told the action ran and its result was withheld."

**p20, ADR 0134 (paged reads).**

- New `docs/adr/0134-paged-source-reads-read-one-version-and-measure-utf8-bytes.md`, title "ADR
  0134: Paged plugin source reads read one version of the data and measure UTF-8 bytes". Status:
  "Accepted — <date>. Implemented: `src/privacyfence/plugins/source_ops.py`,
  `src/privacyfence/plugins/spool.py` (`put_rows`, `rows_page`),
  `src/privacyfence/plugins/rpc.py`." then "Amends [ADR 0128](0128-plugin-source-reads-never-truncate.md)."
  Decision: a Sheets read fetches the range once and serves later pages from a private snapshot in
  the owner-only spool (at most 4 per plugin, 10 idle minutes, `cursor_expired` when gone); a
  Confluence cursor carries the page version and a change is `revision_changed`; sizes on the
  source path and on the JSON-RPC line are UTF-8 bytes. Alternatives: A1 range arithmetic
  (named ranges, R1C1, quoted names, whole-sheet ranges, and still two versions); re-fetching and
  comparing (no saving); counting escaped ASCII everywhere (halves the limit for accented text).
  Consequences: sheet content rests on disk in the spool as Drive exports already do (ADR 0123,
  ADR 0129); cursors issued before the change are invalid; a lone surrogate falls back to escaped
  JSON. Verification: `tests/unit/plugins/test_source_ops.py`, `tests/unit/plugins/test_spool.py`,
  `tests/unit/plugins/test_rpc.py`. Related: issue 846, ADR 0128, ADR 0129.
- ADR 0128's Status gains: "Amended by [ADR 0134](0134-paged-source-reads-read-one-version-and-measure-utf8-bytes.md): one version per read, UTF-8 bytes."
- `docs/adr/README.md`: row 0134 ("Paged plugin source reads read one version of the data and
  measure UTF-8 bytes | Accepted; amends 0128"); 0128's cell becomes "Accepted; amends 0123;
  amended by 0134".
- `docs/plugin-protocol.md`:
  - `## Errors`: the `upstream_error` reasons gain `cursor_expired` (a Sheets snapshot is gone;
    read again from the start) and `revision_changed` covers "a Drive file or Confluence page that
    changed"; `invalid_params` gains the reason `not_downloadable`.
  - `### Source calls`: "Sizes are UTF-8 bytes of the JSON." and, for `drive.download`: "Other
    Google-native types (Forms, Drawings, folders, shortcuts, …) are `invalid_params` with
    `data.reason` `not_downloadable`."
  - `### Paging`: the `sheets.get_values` row adds "the whole range is read once; later pages come
    from a private copy held for 10 idle minutes"; the `confluence.get_page` row adds "a page that
    changes between pages is `revision_changed`".
- `docs/plugins.md` `## Large reads and downloads`: one sentence each for the Sheets copy and the
  Confluence version check.
- `docs/security-and-compliance.md`: the ADR link replacement above (0133 → 0134), and the spool sentence
  (where Drive exports are described) adds Sheets snapshots.
- `CHANGELOG.md`: extend "Plugin reads no longer truncate." with "A Sheets range is downloaded
  once per read and every page comes from that copy, a Confluence page that changes between pages
  is reported instead of joined, sizes count UTF-8 bytes, and a Drive file of a Google type that
  cannot be downloaded (a Form, a folder) is refused instead of read as empty."

**p21, ADR 0135 (pages) and protocol 1.2 docs.**

- New `docs/adr/0135-plugin-pages-are-listed-by-the-plugin-and-browsed-in-privacyfence.md`, title
  "ADR 0135: A plugin lists its pages, and PrivacyFence serves the page browser". Status:
  "Accepted — <date>. Implemented: `src/privacyfence/plugins/page_index.py`,
  `src/privacyfence/plugins/host.py` (`list_pages`, `list_all_pages`),
  `src/privacyfence/web/routes_plugin_browser.py`, `src/privacyfence/plugin_browser_html.py`,
  `plugin-sdk/src/privacyfence_plugin_sdk/_page_index.py`." then "Amends [ADR 0124](0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md)."
  Context: a plugin can have many pages; links between sandboxed pages carry no cookie (ADR 0124).
  Decision: protocol 1.2's `pages.list`; the `method_not_found` fallback of one page; the browser
  at `/plugin-pages` and `/plugin-pages/<name>`, owner-only, same-origin, with the app's CSP; each
  entry opens `/plugins/<name><path>` in a new tab; the ADR 0124 sandbox, cookie and subresource
  rules are unchanged. Alternatives: exactly the list in "What was rejected" below. Consequences:
  one extra request per plugin per browser view (10 s timeout each, concurrent); a plugin's page
  list is not reviewed at enable time; the browser is the place that links into plugin pages.
  Verification: the tests of p13 to p17. Related: issue 846, ADR 0124, ADR 0126.
- ADR 0124's Status gains: "Amended by [ADR 0135](0135-plugin-pages-are-listed-by-the-plugin-and-browsed-in-privacyfence.md): pages are also opened from the page browser."
- `docs/adr/README.md`: row 0135 ("A plugin lists its pages, and PrivacyFence serves the page
  browser | Accepted; amends 0124"); 0124's cell becomes "Accepted; amended by 0135".
- `docs/plugin-protocol.md`:
  - "1.1.0" becomes "1.2.0" at the top and in both `initialize` examples.
  - `## Versioning`, after the "Version `1.1` adds" paragraph: "Version `1.2` adds the
    `pages.list` request, with which a plugin lists its pages for the page browser; a plugin that
    does not implement it answers `method_not_found` and is listed with one page, `/`. It also
    caps what reaches the AI from a write tool: a `tool.execute` result over 2,048 bytes
    (`WRITE_RESULT_MAX_BYTES`), or one the PII check flags, reaches the AI as a fixed sentence.
    The `tool.execute` message itself is unchanged, so a plugin written for 1.0 or 1.1 keeps
    working. `x-limits` also states `SEND_TIMEOUT_SECONDS` (10): a plugin that does not read a
    message within that time is treated as crashed."
  - `## Messages` table: a row `pages.list` | daemon to plugin | request | "List the plugin's
    pages for the page browser".
  - A new `### pages.list` section after `### Pages`: params, result, the PageEntry table from
    this Design, the 500 limit, the `method_not_found` fallback, and "The page browser at
    `/plugin-pages` lists the entries; each opens `/plugins/<name><path>` in a new tab."
  - `## Limits and timeouts`: rows for `MAX_PAGE_INDEX_ENTRIES` (500), `MAX_PAGE_VERSION_CHARS`
    (40), `MAX_PAGE_DESCRIPTION_CHARS` (200) and the `pages.list` timeout (10 seconds).
- `docs/plugins.md`: in "What a plugin is", the pages bullet says a plugin can list its pages and
  PrivacyFence's page browser (the Plugins menu, or `/plugin-pages`) opens each in a new tab; a
  `### Page index` paragraph under "Writing a plugin" with `@plugin.page_index` and `PageEntry`;
  the self-contained-pages text adds that links between a plugin's pages do not carry your
  session, so a multi-page plugin lists its pages with `page_index` instead of linking them, and
  that a single page can take a `?query` (page paths match exactly); "Open page" becomes "Pages";
  the test host differences list gains "`list_pages()` titles the fallback entry with the
  plugin's name; PrivacyFence uses its display name".
- `docs/security-and-compliance.md`: the ADR link replacement above (0134 → 0135); the "Plugin pages" paragraph gains
  "The page browser that lists them is a PrivacyFence page; plugin pages keep the sandbox, and a
  plugin's images and styles are inlined, never fetched."
- `plugin-sdk/README.md` `## Pages`: a "Page index" paragraph with a three-line example
  (`@plugin.page_index`, `async def pages(ctx): return [PageEntry("/", "Home")]`) and the
  `PluginTestHost.list_pages()` line; a note that `types.py` has a `PageEntry` TypedDict for the
  wire shape and `privacyfence_plugin_sdk.PageEntry` is the dataclass to return.
- `CHANGELOG.md`: replace the "Plugins menu" bullet's last two sentences with "The menu opens
  PrivacyFence's page browser in the same tab, and so does **Pages** on the plugin's card. The
  menu is hidden while no plugin has a page." and add after it: "- **Plugin page browser.** A
  PrivacyFence page lists each running plugin's pages with title, version and dates, and each
  page opens in a new tab in the plugin's sandbox. Plugin protocol 1.2 adds `pages.list` and the
  SDK adds `@plugin.page_index`; plugins written for 1.0 and 1.1 keep working and are listed with
  one page."

### What was rejected (these go into ADR 0135)

- **A same-origin bounce plus `allow-popups allow-popups-to-escape-sandbox` on plugin pages.** Any
  site, and any plugin page, could make the owner's browser load any plugin page as the owner,
  which is effectively `SameSite=Lax` for plugin pages; script in a plugin page could open any URL
  in an unsandboxed tab. For a plugin that serves HTML an AI wrote, that is an exfiltration and
  phishing surface.
- **`SameSite=Lax` for the session cookie.** It weakens every Settings and approvals route.
- **The plugin serves its own browser page.** Links from a sandboxed page carry no cookie (ADR
  0124), and fixing that is the bounce above.
- **Serving a plugin's images and CSS as separate URLs.** Unchanged: a sandboxed page's
  subresource requests carry no cookie, so a plugin inlines them.
- **A page index in the manifest.** The manifest is reviewed and hashed at enable time, so a
  plugin whose pages change at runtime would need re-review for every new page.

## ADRs

- **ADR 0132**, `docs/adr/0132-a-plugin-that-stops-reading-its-input-for-10-seconds-is-treated-as-crashed.md`:
  a plugin that does not take a message within `SEND_TIMEOUT_SECONDS` (10 s) is treated as
  crashed. Amends ADR 0120's definition of a crash.
- **ADR 0133**, `docs/adr/0133-a-plugin-writes-result-is-capped-and-pii-scanned-before-it-reaches-the-ai.md`:
  a plugin write's result reaches the AI only when it is at most 2,048 bytes and PII-free;
  otherwise a fixed sentence, keeping `approval_id` only when PrivacyFence issued it to that
  plugin. Amends ADR 0122.
- **ADR 0134**, `docs/adr/0134-paged-source-reads-read-one-version-and-measure-utf8-bytes.md`:
  paged plugin source reads read one version of the data (Sheets snapshot, Confluence version)
  and measure UTF-8 bytes. Amends ADR 0128.
- **ADR 0135**, `docs/adr/0135-plugin-pages-are-listed-by-the-plugin-and-browsed-in-privacyfence.md`:
  protocol 1.2's `pages.list` and the `/plugin-pages` browser; the ADR 0124 sandbox is unchanged.
  Amends ADR 0124.

The numbers are fixed and follow the order the docs phases write them (p18 to p21). Each amended ADR gets one Status line in the format of ADR 0121's
"Amended by [ADR 0131](…): …", and each new ADR an "Amends [ADR NNNN](…)." line, as ADR 0131 has.
No other decision here meets the ADR bar: A2, S1, S3 and S4 make the code match ADRs 0130, 0121
and 0120; the rest of A3 (bounded stop, concurrent events) and A4 are bug fixes inside ADR 0120's
supervisor; P1 to P7 align the SDK with the daemon (ADR 0126).

## Manual steps

There are no `manual_before` steps: the hotfix is already on `main`, and p0 merges it in. `manual_after` has one short packaged spot check (`ma1-spot-check-hardening`): the page
browser and a plugin page in a new tab, a responsive **Disable**, and one write tool result through
a real AI client. The step-by-step page is `docs/plugin-framework-hardening-plan-manual-steps.html`,
published as the manifest's `manual_steps_artifact`.

## Risks and open questions

- **The hotfix (p0, p6).** It is on `main` (#870). If after p0's merges
  `grep -n '_validate_salesforce_id(report_id' src/privacyfence/salesforce_client.py` prints
  nothing, p0 stops with `status=blocked`. If the hotfix validates somewhere other than `run_report`/`get_page` (so an HTTP call still happens, or
  the exception is not a `*ClientError`), p6's regression test fails; p6 stops with
  `status=blocked` and reports what the hotfix does.
- **Python 3.11 subprocess pipes (S4).** On Python 3.11 (3.11.17 included), `asyncio`'s
  `Process.wait()` returns only once every pipe of the process is closed; the fix (gh-119710) is
  not in 3.11. With stderr piped through the daemon, a grandchild holding the pipe would block
  every `wait()`. p4 therefore never awaits `proc.wait()` and polls `returncode` instead (Design
  "The running log is capped"), and runs `tests/unit/plugins/test_supervisor.py` under
  `/usr/bin/python3.11` as well. If that run hangs in `TestShutdown` or `TestProcessGroup`, or a
  3.11 environment cannot be set up, p4 stops with `status=blocked`.
- **Windows (A3, S3, S4).** The `platform-windows` job runs the send-timeout, log-pump and owner
  checks. A Proactor pipe must implement `abort()`; `os.replace` during rotation must not race
  another writer; `%ProgramFiles%` on a CI runner must be owned by one of the three SIDs. A red
  Windows job is this PR's failure to root-cause, never a skip.
- **False crash (A3).** A plugin whose event loop is blocked for more than 10 s while the daemon
  sends it more than about 128 KiB is now killed as a crash. Accepted and documented. Scaling the
  timeout by message size is out of scope.
- **Lock held for a purge (A4).** A queued Disable waits up to 30 s behind a purge's ack wait.
  Accepted. Moving the purge's wait outside the lock is out of scope.
- **Integration tests asserting write results (S2).** If any `tests/integration/test_plugin_*` or
  the conformance test asserts a write result over 2,048 bytes, or one that the PII detector would
  flag (an IBAN, a card number, an IP address, …), p12 stops with `status=blocked`.
- **Existing tests without required params (P4).** Fixture calls in existing test-host tests that
  omit required parameters start failing. p8 fixes those tests, never loosens the validator; a
  failing test outside p8's `touches` means `status=blocked`.
- **PII popup in the conformance harness (P6).** If `tests/fixtures/plugins/echo/harness.py`'s
  `decide` cannot answer `show_pii_confirmation_popup`, or PII detection is off in the stack, p9
  keeps its PII test SDK-only and says so.
- **Escaped bytes on the wire (P5).** A daemon test that asserts `\uXXXX` escapes on the wire is
  updated in p6 to expect UTF-8, never kept on ASCII; one outside p6's `touches` means
  `status=blocked`.
- **Lone surrogates (P5, D1).** A Sheets page with a lone surrogate falls back to escaped JSON for
  the whole result, which can be larger than measured and end as `payload_too_large`. Accepted.
- **The SDK was published (P3).** If `privacyfence-plugin-sdk` is on PyPI when p7 runs
  (`pip index versions privacyfence-plugin-sdk` lists a version), the strict defaults need a
  compatibility path: p7 stops with `status=blocked`.
- **Doc sections that moved.** The docs phases edit by section heading. A named section that is
  missing means `status=blocked`.
- **Transient URLs.** Between p15 and p16 the menu and card point at `/plugin-pages/<name>`, which
  only p16 serves. That is only on the feature branch.

## Implementation manifest

Every code phase's brief ends with the same two rules: no `CHANGELOG.md` line (the docs phases
write them), and no plan item IDs (`A2`, `S1`, `D3`, `P5`, `pb1` and the like) in code, comments
or test names, because `tests/unit/test_code_no_history.py` refuses them. Phases that run at the
same time never share a path in `touches`; every phase that edits `plugins/constants.py`, the
schema, `testing/_host.py`, `host.py` or the SDK's `plugin.py` is on one `depends_on` chain.

```yaml
plan_slug: plugin-framework-hardening
feature_branch: feature/plugin-framework-hardening
tracking_issue: 846
max_parallel: 3
manual_steps_artifact: https://claude.ai/artifact/9oCB3Hq694GQVfxX5nMC1w
manual_steps_source: docs/plugin-framework-hardening-plan-manual-steps.html
manual_before: []
manual_after:
- id: ma1-spot-check-hardening
  title: "On a packaged install of this PR's build: open the page browser and one plugin page, disable a running plugin, and run one write tool"
  why: "CI checks each piece headless. This checks the packaged app end to end: (a) Plugins menu -> the page browser opens in the same tab, and a plugin page from it opens in a new tab and works (the session cookie travels); (b) Disable on a running plugin responds within a few seconds; (c) the today example's today_add_note through a real AI client returns its small result ({\"notes\": n}), not the withheld sentence."
verify_after_merge:
- ruff check .
- python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/examples tests/unit/test_privilege_separation.py tests/unit/test_windows_acl.py tests/unit/test_web_shell.py tests/unit/test_settings_window_html.py tests/unit/web/test_server.py tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py tests/unit/test_gen_plugin_sdk_types.py -q
final_checks:
- docs/plugin-framework-hardening-plan.md and docs/plugin-framework-hardening-plan-manual-steps.html are deleted and nothing links to them (grep -rn plugin-framework-hardening-plan docs scripts README.md prints nothing)
- ADRs 0132, 0133, 0134 and 0135 exist with Status Accepted and are in docs/adr/README.md's index; ADR 0120, 0122, 0128 and 0124 each have their Amended-by Status line and index cell
- CHANGELOG.md has the [Unreleased] ### Added lines from the plan's docs phases and no new version heading
- python3 scripts/gen_plugin_sdk_types.py --check exits 0
- The full /dod passes, including python3 scripts/check_coverage_floor.py coverage.json and python3 -m pytest tests/integration -v
- "connector-live-check.yml: no phase of this plan changes a *_client.py file. If git diff --name-only origin/main...HEAD | grep -E 'src/privacyfence/[a-z_]+_client\\.py$' prints anything (no phase of this plan changes one, and the Salesforce/Confluence hotfix reached main through its own PR #870), connector-live-check.yml is dispatched against feature/plugin-framework-hardening and green, linked in the PR; otherwise the PR says it was not needed"
- build.yml dispatched against feature/plugin-framework-hardening is green (ma1 installs this build; link the run in the PR)
- The PR's platform-windows and platform-macos jobs are green (the owner check, the log pump and the send timeouts run there)
- python3 -m build plugin-sdk succeeds (delete plugin-sdk/dist and any build/ or *.egg-info afterwards)
phases:
- id: p0-sync
  title: Merge origin/main (with the Salesforce/Confluence hotfix) and the moved stack branch into this branch
  depends_on: []
  complexity: S
  touches:
  - docs/README.md
  - scripts/build_site.py
  - CHANGELOG.md
  brief: |
    1. git fetch origin main. Then git fetch origin feature/plugins-nav-menu; that fetch may fail because the branch
       no longer exists (it merged); record which.
    2. git merge --no-ff origin/main (a merge commit; never rebase). origin/main carries the hotfix (#870).
    3. If origin/feature/plugins-nav-menu was fetched and `git merge-base --is-ancestor origin/feature/plugins-nav-menu HEAD`
       fails, git merge --no-ff origin/feature/plugins-nav-menu.
    4. Conflicts: resolve docs/README.md and scripts/build_site.py mechanically (keep both sides' entries, including
       this plan's plugin-framework-hardening-plan.md entry), and CHANGELOG.md mechanically: keep both sides' bullets
       under ## [Unreleased] (main's ### Security line from the hotfix and the stack's plugin bullets), no version
       heading. Any conflict in another file: git merge --abort and stop with status=blocked, naming the file.
    5. grep -n '_validate_salesforce_id(report_id' src/privacyfence/salesforce_client.py must print a line;
       otherwise stop with status=blocked ("the Salesforce report_id hotfix is not in this branch").
    6. ls docs/adr/0132-* must fail (no such file). If it succeeds, another branch took the number: stop with
       status=blocked.
    7. The merges may change many files outside this phase's touches; that is expected. Record in the PHASE-REPORT
       each source merged, its SHA, and that the diff review should only check each merge brought its source in
       unchanged.
    8. Run ruff check . and python3 -m pytest tests/unit -q; both must pass.

    No CHANGELOG.md line in this phase (only the mechanical merge resolution). No plan item IDs (A2, S1, D3, P5,
    pb1 …) in code, comments or test names: tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - grep -n '_validate_salesforce_id(report_id' src/privacyfence/salesforce_client.py prints one line
  - ls docs/adr/0131-*.md succeeds and ls docs/adr/0132-* fails
  - grep -c '^## \[Unreleased\]' CHANGELOG.md prints 1
  - python3 -m pytest tests/unit/test_website_docs_allowlist.py tests/unit/test_docs_references_exist.py tests/unit/plugins tests/unit/test_salesforce_client.py tests/unit/test_confluence_client.py -q passes
- id: p1-outputs-replay
  title: An output read's approval is keyed to the file's sha256
  depends_on:
  - p0-sync
  complexity: S
  touches:
  - src/privacyfence/plugins/outputs.py
  - tests/unit/plugins/test_outputs.py
  brief: |
    1. outputs.py PluginOutputsConnector._read: add dedupe_extra=result["sha256"] to the gated_call(...) call, and the
       module docstring sentence from the plan's Design "Output reads are keyed to the file's hash".
    2. tests/unit/plugins/test_outputs.py:
       - Extend TestTools.test_read_goes_through_gated_call_with_metadata_only_preview (about line 549):
         assert call["dedupe_extra"] == hashlib.sha256(b"secret,1\n").hexdigest() (use the bytes that test writes).
       - Move the env fixture out of TestRuleAllowsFolder (about lines 635-646) to module level, unchanged, and keep
         TestRuleAllowsFolder using it.
       - New class TestReadReplay using env:
         test_a_repeat_read_of_an_unchanged_file_reuses_the_approval (put "v1", read twice, len(popups.read) == 1);
         test_a_file_rewritten_after_approval_gets_a_new_card (read "v1", rewrite to "v2-longer", read again:
         len(popups.read) == 2, popups.read[1][0][2] == "v2-longer", result["text"] == "v2-longer");
         test_a_denied_rewritten_file_is_not_released_by_the_earlier_approval (approve v1, rewrite, set
         popups.decision = "deny", the second read raises GateDeniedError).
    Stop condition: if tests/unit/test_gate.py::test_dedupe_extra_keeps_an_approval_to_its_own_key fails, stop with
    status=blocked.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_outputs.py tests/unit/test_gate.py -q passes
  - grep -n 'dedupe_extra=result\["sha256"\]' src/privacyfence/plugins/outputs.py prints one line
- id: p2-windows-owner
  title: The Windows plugin trust check requires a trusted owner
  depends_on:
  - p0-sync
  complexity: S
  touches:
  - src/privacyfence/windows_acl.py
  - src/privacyfence/privilege_separation.py
  - tests/unit/test_windows_acl.py
  - tests/unit/test_privilege_separation.py
  - tests/unit/plugins/test_trust.py
  - tests/platform/test_plugin_dir_permissions.py
  brief: |
    1. windows_acl.py: the four SID constants and read_owner_sid exactly as the plan's Design "Windows owner" gives them;
       add them to __all__.
    2. privilege_separation.py: _windows_owner_problem and _windows_plugin_path_problem as given; the win32 branches of
       admin_only_write_problem, admin_only_ancestor_write_problem and admin_only_plugin_dir_write_problem call
       _windows_plugin_path_problem with their existing can_rewrite. Leave _windows_script_elevation_problem on
       _windows_admin_only_write_problem.
    3. Tests:
       - test_privilege_separation.py: a module-level autouse fixture
         monkeypatch.setattr(windows_acl, "read_owner_sid", lambda path: windows_acl.ADMINISTRATORS_SID).
         In TestAdminOnlyWriteProblem: test_windows_refuses_a_path_a_user_owns (DACL admin-only, read_owner_sid ->
         "S-1-5-21-1-2-3-1001", read_owner -> "MACHINE\\alice"; expected
         f"{tmp_path} is owned by MACHINE\\alice, not by SYSTEM, Administrators or TrustedInstaller");
         test_windows_accepts_each_trusted_owner, parametrized over the three SIDs;
         test_windows_refuses_an_owner_it_cannot_read (expected f"could not read {tmp_path}'s owner"). One test each in
         TestAdminOnlyAncestorWriteProblem and TestAdminOnlyPluginDirWriteProblem showing a user owner is refused there
         too, and one test that _elevation_script_problem ignores the owner.
       - tests/unit/plugins/test_trust.py: the same autouse owner fixture; TestWindowsAncestors gains
         test_a_plugin_directory_a_user_owns_is_refused (admin-only ACL everywhere, read_owner_sid returns a user SID
         only for the plugin's real directory, admin_only_problem returns the owner problem).
       - tests/unit/test_windows_acl.py: test_read_owner_sid_answers_none next to the existing read_owner None test.
       - tests/platform/test_plugin_dir_permissions.py: test_windows_refuses_a_plugin_folder_this_user_owns, elevated,
         built like test_windows_accepts_a_plugin_installed_under_program_files: set the folder's owner to the current
         user's SID with win32security.SetFileSecurity(OWNER_SECURITY_INFORMATION) and assert "is owned by" in
         trust.admin_only_problem(...). It runs on the PR's platform-windows job, not here.
    Stop condition: if any existing Windows accept test still fails on Linux after the autouse fixture, stop with
    status=blocked and name it.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/test_privilege_separation.py tests/unit/test_windows_acl.py tests/unit/plugins/test_trust.py -q passes
  - python3 -m pytest tests/platform/test_plugin_dir_permissions.py -q exits 0 (its Windows tests skip on Linux)
  - grep -n 'def read_owner_sid' src/privacyfence/windows_acl.py prints one line
  - grep -n 'def _windows_owner_problem' src/privacyfence/privilege_separation.py prints one line
- id: p3-send-timeout
  title: A plugin that stops reading is a crash, stops are bounded, events go to plugins concurrently
  depends_on:
  - p0-sync
  complexity: M
  touches:
  - src/privacyfence/plugins/constants.py
  - src/privacyfence/plugins/rpc.py
  - src/privacyfence/plugins/supervisor.py
  - src/privacyfence/plugins/events.py
  - src/privacyfence/daemon_main.py
  - tests/fixtures/plugins/stub/stub_plugin.py
  - tests/unit/plugins/test_rpc.py
  - tests/unit/plugins/test_supervisor.py
  - tests/unit/plugins/test_events.py
  - tests/unit/plugins/test_constants.py
  brief: |
    Read the plan's Design "A plugin that stops reading is a crash" first; it is the spec.
    1. constants.py: SEND_TIMEOUT_SECONDS, SHUTDOWN_NOTIFY_TIMEOUT_SECONDS, CLOSE_WAIT_SECONDS as given. Do not touch
       the schema (p13 adds the x-limits entry).
    2. rpc.py: _send(timeout=None) with the timed block, _shutdown(reason, *, abort=False), notify(..., timeout=None),
       close() with the bounded wait_closed, all reading the constants as module globals at call time.
    3. supervisor.py _stop_child: the delivered-flag version as given.
    4. events.py: send/_send_to as given.
    5. daemon_main.py: PLUGIN_STOP_ALL_TIMEOUT_SECONDS = 25.0 as a module constant, used at the stop_all .result(...)
       call (about line 2030).
    6. tests/fixtures/plugins/stub/stub_plugin.py: a mode "stop-reading": answer initialize, then time.sleep in a loop
       until the file's existing 60 s deadline without reading stdin, SIGTERM left at its default. Add it to the
       docstring's mode list.
    7. Tests:
       - test_rpc.py: a _StuckWriter modelled on the inline Writer at about lines 505-516 (write does nothing, drain
         awaits a never-set Event, close/wait_closed exist, transport.abort records the call), the reader from
         _streams(), monkeypatch rpc.SEND_TIMEOUT_SECONDS to 0.2:
         test_a_send_that_does_not_drain_times_out (RpcError code "timeout" in under 1 s, peer.closed, the on_close
         reasons == ["write_timeout"], abort called);
         test_a_send_queued_behind_a_stuck_one_fails_fast (code "timeout" or detail "peer closed", nothing written);
         test_a_request_whose_send_times_out_leaves_nothing_pending (code "timeout", peer._pending == {});
         test_close_does_not_wait_for_a_stuck_transport (monkeypatch rpc.CLOSE_WAIT_SECONDS = 0.1; close() returns
         within 1 s when wait_closed never resolves).
       - test_supervisor.py, new class TestStuckReader with the file's Harness/make and the autouse fast_stop fixture:
         test_disable_kills_a_plugin_that_stopped_reading (mode stop-reading; monkeypatch
         sv.SHUTDOWN_NOTIFY_TIMEOUT_SECONDS = 0.2; filler = asyncio.create_task(h.sup.peer.notify("x", {"pad": "x" *
         512_000})); await asyncio.wait_for(h.sup.stop(reason="user"), 5); the process has exited,
         h.states[-1] == ("disabled", "disabled by you"), no backoff state, h.sleeps == [], elapsed under
         TERMINATE_GRACE_SECONDS + 1; then await asyncio.gather(filler, return_exceptions=True));
         test_a_send_timeout_while_running_is_a_crash (monkeypatch rpc.SEND_TIMEOUT_SECONDS = 0.3; on_sleep stops the
         supervisor at the first backoff; the big notify raises code "timeout"; ("backoff", "crashed 1 time") is in
         h.states, h.sleeps == [1.0], "crashed (write_timeout)" is in caplog.text, the first process is dead).
       - test_events.py: FakePeer gains stuck: asyncio.Event | None (notify awaits it, then raises
         RpcError("timeout", "plugin is not reading its input")):
         test_a_stuck_plugin_does_not_hold_up_the_others, test_order_is_kept_per_plugin,
         test_a_peer_gets_nothing_after_its_first_failure.
       - test_constants.py: the three new values are positive and SHUTDOWN_NOTIFY_TIMEOUT_SECONDS <
         SEND_TIMEOUT_SECONDS.
       Keep each new test under 5 s (the Windows job has a 30 s per-test timeout).
    Stop condition: if TestShutdown.test_grace_then_kill, test_graceful, test_daemon_shutdown_sends_no_disabling_notice
    or any TestProcessGroup test changes behaviour, the delivered logic is wrong: fix it inside this design or stop
    with status=blocked. Do not change those tests.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_rpc.py tests/unit/plugins/test_supervisor.py tests/unit/plugins/test_events.py tests/unit/plugins/test_constants.py -q passes
  - python3 -m pytest tests/integration/test_plugin_framework.py -q passes
  - grep -n 'write_timeout' src/privacyfence/plugins/rpc.py prints at least one line
  - grep -n 'PLUGIN_STOP_ALL_TIMEOUT_SECONDS' src/privacyfence/daemon_main.py prints two lines
- id: p4-log-pump
  title: The plugin log is capped while the plugin runs
  depends_on:
  - p3-send-timeout
  complexity: S
  touches:
  - src/privacyfence/plugins/constants.py
  - src/privacyfence/plugins/supervisor.py
  - tests/fixtures/plugins/stub/stub_plugin.py
  - tests/unit/plugins/test_supervisor.py
  - tests/unit/plugins/test_constants.py
  brief: |
    Read the plan's Design "The running log is capped" first; it is the spec.
    1. constants.py: LOG_PUMP_CHUNK_BYTES and LOG_DRAIN_SECONDS as given.
    2. supervisor.py: _rotate_log(force=), _StderrLog, _pump_stderr, the Supervisor.__init__ fields, the _spawn change
       (stderr=asyncio.subprocess.PIPE, the pump task, child.stderr_pump), the bounded drain in _reap, and the module
       docstring line, all as given. Never cancel a pump. _pump_stderr appends through asyncio.to_thread, and
       _StderrLog.append runs under its threading.Lock.
    2a. supervisor.py: _EXIT_POLL_SECONDS and _wait_exit exactly as given; _Child._watch_exit, Supervisor._exited and
       _reap use it instead of awaiting proc.wait() (Python 3.11's Process.wait() waits for every pipe to close).
    3. stub_plugin.py: a mode "spam-stderr": right after sending the initialize result,
       for _ in range(300): print("x" * 99, file=sys.stderr), then print("SPAM_DONE", file=sys.stderr, flush=True),
       then behave like "ok". Add it to the docstring's mode list.
    4. Tests in test_supervisor.py TestLog:
       - test_stderr_log_rotates_while_writing: monkeypatch sv.LOG_MAX_BYTES = 10; log = sv._StderrLog(tmp_path /
         "x.log"); log.append(b"a" * 25); sizes of x.log / x.log.1 / x.log.2 are 5 / 10 / 10.
       - test_running_log_stays_under_the_cap: mode spam-stderr, sv.LOG_MAX_BYTES = 4096, await h.sup.start(), poll up
         to 10 s until "SPAM_DONE" is in the log while the plugin runs; h.log_path.stat().st_size <= 4096, each of
         .1/.2/.3 <= 4096, .4 does not exist, h.sup.state == "running"; then stop().
       - test_constants.py: the two new values are positive.
       - TestShutdown.test_stop_does_not_wait_for_a_grandchilds_pipe: mode spawn-child (its sleeping child inherits
         stderr); await h.sup.start(), then await asyncio.wait_for(h.sup.stop(), 5) and assert it took under 1 s.
       - test_stderr_log_appends_from_two_threads: two threads each append 100 chunks of b"y" * 50 to one _StderrLog
         with sv.LOG_MAX_BYTES = 10_000; the sizes of the log and its backups add up to 10_000 and no file is over
         10_000.
       Keep test_stderr_goes_to_the_log, test_rotation, test_small_log_is_appended_to, test_log_is_private and
       TestProcessGroup unchanged; they must pass.
    5. Python 3.11: run python3.11 -m pytest tests/unit/plugins/test_supervisor.py -q (/usr/bin/python3.11 exists in
       the container). If its dependencies are missing, create a venv outside the repository
       (/usr/bin/python3.11 -m venv /tmp/pf-py311), install the project with its dev extras
       (/tmp/pf-py311/bin/pip install -e '.[dev]') and run /tmp/pf-py311/bin/python -m pytest
       tests/unit/plugins/test_supervisor.py -q instead.
    Stop conditions: if TestShutdown or TestProcessGroup hang under any Python, or a Python 3.11 environment cannot be
    set up, stop with status=blocked and report the Python version.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_supervisor.py tests/unit/plugins/test_constants.py -q passes
  - grep -n 'class _StderrLog' src/privacyfence/plugins/supervisor.py prints one line
  - grep -n 'stderr=asyncio.subprocess.PIPE' src/privacyfence/plugins/supervisor.py prints one line
  - grep -n 'proc.wait()' src/privacyfence/plugins/supervisor.py prints nothing
  - python3.11 -m pytest tests/unit/plugins/test_supervisor.py -q passes (or /tmp/pf-py311/bin/python -m pytest tests/unit/plugins/test_supervisor.py -q, per step 5)
- id: p5-host-lock
  title: Host actions run one at a time and never leave a second supervisor
  depends_on:
  - p3-send-timeout
  complexity: S
  touches:
  - src/privacyfence/plugins/host.py
  - tests/unit/plugins/test_host.py
  brief: |
    Read the plan's Design "Host actions run one at a time" first; it is the spec.
    1. host.py: STOP_ALL_LOCK_WAIT_SECONDS and REASON_STOPPING module constants, the private _HostStopping(ValueError); _lock, _stop_epoch, _held_epoch in
       __init__; _serialized() and the async _action() exactly as given; the five call sites (inspect, enable, disable,
       revoke_approval, purge) become `async with self._action(name) as plugin:`; rescan() wraps a new _rescan() (body
       moved unchanged) in _serialized() and catches only _HostStopping, returning quietly; stop_all() bumps the
       epoch and waits at most STOP_ALL_LOCK_WAIT_SECONDS for the lock as given; the three _start_plugin guards in the
       given order.
    2. Tests in test_host.py (real stub/SDK processes with the existing Env, env fixture, until() and
       env.add(..., mode=...); count processes by wrapping supervisor_mod.Supervisor._spawn with monkeypatch to collect
       every child.proc):
       - test_rescan_during_purge_leaves_one_supervisor (stub slow-shutdown, supervisor_mod.SHUTDOWN_GRACE_SECONDS and
         TERMINATE_GRACE_SECONDS = 0.5; purge task, one tick, await host.rescan(), await the purge; exactly one live
         process, it is host._plugins["stub"].supervisor's, the row is running);
       - test_disable_racing_rescan_leaves_nothing_running, parametrized on which starts first (every spawned process
         exits; the row is disabled / "disabled by you"; the store record is disabled; supervisor is None);
       - test_actions_run_one_at_a_time (SDK purge-hang with purge_timeout = 0.5; purge task then host.disable(SDK);
         the lifecycle audit order is "data purged (timeout)" then "disabled"; no process left);
       - test_start_plugin_refuses_a_second_supervisor (await host._start_plugin(plugin) on a running plugin: no new
         spawn, a WARNING with "already has a supervisor", the supervisor object unchanged);
       - test_action_queued_before_stop_all_does_nothing (purge-hang purge holds the lock; queue rescan() and
         enable(...) tasks; stop_all(); no process alive afterwards; the enable row's last_error == REASON_STOPPING);
       - test_stop_all_does_not_wait_for_a_long_action (host_mod.STOP_ALL_LOCK_WAIT_SECONDS = 0.2, purge_timeout = 2;
         stop_all returns in under 1.5 s with the plugin process dead; after the purge task ends still no process);
       - test_disable_completes_for_a_plugin_that_stopped_reading (stub stop-reading; a filler task
         host._plugins["stub"].supervisor.peer.notify("x", {"pad": "x" * 512_000}); await
         asyncio.wait_for(host.disable("stub"), 5); row disabled, process dead, no restart).
       Existing tests around lines 372-422, 744, 777, 1255-1330 and TestPurge (about 1145-1203) must pass unchanged.
    Stop condition: if any step makes a locked method call rescan, enable, disable, purge, inspect, revoke_approval or
    stop_all, it deadlocks: call the private body instead. If an existing test fails because it relies on an action
    after stop_all, check it starts a new call after the bump; never turn the epoch into a permanent closed flag. If
    that does not fix it, stop with status=blocked.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_host.py -q passes
  - python3 -m pytest tests/integration/test_plugin_framework.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py -q passes
  - grep -c 'async with self._action(' src/privacyfence/plugins/host.py prints 5
- id: p6-source-consistency
  title: Sheets snapshot, Confluence version, non-exportable Drive types, UTF-8 sizes, and the path-id regression test
  depends_on:
  - p3-send-timeout
  complexity: M
  touches:
  - src/privacyfence/plugins/source_ops.py
  - src/privacyfence/plugins/spool.py
  - src/privacyfence/plugins/rpc.py
  - tests/unit/plugins/test_source_ops.py
  - tests/unit/plugins/test_spool.py
  - tests/unit/plugins/test_rpc.py
  brief: |
    Read the plan's Design "Source reads: one version, UTF-8 bytes" first; it is the spec.
    1. spool.py: _SNAPSHOTS_PER_PLUGIN, _RowsEntry, _row_line, put_rows, rows_page, and _rows in sweep()/clear(), as
       given (default JSON separators; the size formula 2 + sum(len(line)) + 2 * (n - 1); put_rows and rows_page call
       self.sweep() first and touch _rows only under self._lock; lines split on b"\n" only). The not_downloadable check
       in read_chunk_at, as given.
    2. source_ops.py: _utf8_json and _encoded_size; _serve's cap, "bytes" and seen["bytes"] on the UTF-8 body;
       _run_sheets with the {"k", "s"} state and no provider call on a continuation; _page_changed and the {"o", "v"}
       Confluence state, version checked before the start > len(body) check.
    3. rpc.py _send: ensure_ascii=False with the ensure_ascii=True retry on UnicodeEncodeError, keeping
       separators=(",", ":") and allow_nan=False.
    4. Tests:
       - test_source_ops.py:
         TestPaging.test_sheets_splits_rows gains assert drive.get_sheet_values.call_count == 1;
         test_sheets_continuation_never_refetches (change return_value after page 1; later pages still serve the
         first snapshot);
         test_sheets_expired_snapshot_is_cursor_expired (env.spool.clear() between pages; code upstream_error,
         extra reason cursor_expired);
         test_sheets_single_page_writes_no_spool_file;
         TestCursorState sheets cases move to the {"k", "s"} state: an unknown 16-hex s gives cursor_expired, a bad
         shape gives "cursor is not valid";
         test_confluence_version_change_mid_read_is_revision_changed (side_effect returns version 1 then 2);
         TestCursorState.test_confluence_state_is_checked requires {"o", "v"}; test_confluence_splits_the_body keeps
         passing with the fake's version set to 3;
         TestAdapterDrive.test_a_google_form_is_not_downloadable (end to end: invalid_params, reason
         not_downloadable);
         TestPayloadCap.test_non_ascii_counts_utf8_bytes (monkeypatch source_ops.MAX_SOURCE_RESULT_BYTES = 100; a Sheets
         value of "é" * 20 succeeds; result["bytes"] == len(json.dumps(result["data"], ensure_ascii=False).encode()));
         update any size helper in this file that assumes ASCII;
         new class TestPathIdsNeverReachTheService:
           test_a_report_id_with_a_path_is_refused_before_any_request: sf = SalesforceClient.__new__(SalesforceClient);
           sf._call = MagicMock(); call salesforce.report_run with report_id "../../query?q=SELECT Id FROM Contact";
           err.code == "upstream_error", err.detail == "the service returned an error", sf._call.assert_not_called();
           test_a_page_id_with_a_path_is_refused_before_any_request: cf = ConfluenceClient.__new__(ConfluenceClient);
           cf._request = MagicMock(); cf._client = MagicMock(); call confluence.get_page with page_id "../x";
           err.code == "upstream_error", cf._request.assert_not_called(), cf._client.get.assert_not_called().
       - test_spool.py: put_rows then rows_page across two pages; the fifth snapshot of one plugin evicts the least
         recently used and unlinks its file; sweep after DRIVE_SPOOL_IDLE_SECONDS drops a snapshot (use the spool's
         clock argument); rows_page of one row bigger than the budget raises payload_too_large; a row holding
         "a\u2028b\u0085c" comes back from rows_page unchanged and counts as one row; parametrized
         application/vnd.google-apps.form, .folder and .shortcut raise invalid_params / not_downloadable and
         download_range and download_file_bytes are never called.
       - test_rpc.py: a sent line containing "é" carries the raw UTF-8 bytes; a message with a lone surrogate
         ("\ud800") still sends, escaped.
    Stop conditions: if a daemon test outside this phase's touches asserts \uXXXX escapes on the wire, stop with
    status=blocked. If either path-id test makes an HTTP call or gets a code other than upstream_error, the hotfix
    validates differently from the plan's assumption: stop with status=blocked and report what run_report and
    get_page do with those ids.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_source_ops.py tests/unit/plugins/test_spool.py tests/unit/plugins/test_rpc.py -q passes
  - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_sdk_testhost_conformance.py -q passes
  - grep -n 'cursor_expired' src/privacyfence/plugins/source_ops.py prints at least one line
  - grep -n 'not_downloadable' src/privacyfence/plugins/spool.py prints one line
  - grep -n 'ensure_ascii=False' src/privacyfence/plugins/rpc.py prints at least one line
- id: p7-testhost-manifest
  title: PluginTestHost takes source_operations and pages, refuses non-downloadable Drive types, pins UTF-8 sizes
  depends_on:
  - p0-sync
  complexity: M
  touches:
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_source.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_pages.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/__init__.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/pytest.py
  - tests/unit/plugin_sdk/test_testhost.py
  - tests/unit/plugin_sdk/test_testhost_surfaces.py
  - tests/unit/examples/test_today_plugin.py
  - tests/integration/test_sdk_testhost_conformance.py
  brief: |
    Read the plan's Design "The test host follows the manifest…" (P3 bullet) and the D2 test-host sentence first.
    0. Run pip index versions privacyfence-plugin-sdk, or fetch https://pypi.org/pypi/privacyfence-plugin-sdk/json.
       If any version is published, stop with status=blocked (plan Risks). If neither answers (no network to PyPI),
       also stop with status=blocked: the strict defaults are only safe for an unpublished SDK.
    1. testing/_host.py: the source_operations and pages constructor arguments, validation, frozenset; pass the allowed
       set to SourceFixtures; request()/get() answer 404 when pages is false via _pages.serve(..., enabled=self.pages);
       the approvals pages flag (about line 241) becomes lambda: self.pages. Update the class docstring.
       The docstring examples in testing/__init__.py (every `PluginTestHost(plugin)` that loads a calendar sample
       gets `source_operations=("calendar.list_events",)`; the pages example that calls host.get/host.request gets
       `pages=True`) and testing/pytest.py (`plugin_host(plugin, pages=True)` in its host.get("/") example) follow.
    2. testing/_source.py: refuse with exactly RpcError("operation_not_allowed", "the plugin may not use this
       operation") when the operation is outside SOURCE_OPERATIONS or not allowed (replace the text at about line 178);
       in _serve_drive, the not_downloadable check on the fixture's mime_type with the daemon's code, detail and extra.
    3. testing/_pages.py serve(): an enabled: bool = True keyword; when false, after the path check and its 400, answer
       404 b"Not Found" with SECURITY_HEADERS and send no web.request.
    4. Update every PluginTestHost(...) constructor in tests/unit/plugin_sdk/test_testhost.py,
       tests/unit/plugin_sdk/test_testhost_surfaces.py, tests/unit/examples/test_today_plugin.py (make_host, about line
       36: source_operations=("calendar.list_events",), pages=True) and tests/integration/test_sdk_testhost_conformance.py
       (the sdk fixture at about line 241: source_operations=("calendar.list_events", "drive.download"), pages=True,
       matching the daemon fixture's install_echo(..., source_operations=["calendar.list_events", "drive.download"])
       at about line 248, because a drive download scenario runs (about line 97); leave the TestSameFloor
       constructor at about line 454 unchanged) so each test gets the operations and pages it uses.
    5. New tests in test_testhost.py: test_an_operation_outside_source_operations_is_refused (exact code and detail);
       test_an_unknown_source_operation_is_a_value_error; test_pages_off_is_a_404_that_never_reaches_the_plugin (the
       page handler did not run); test_an_approval_page_needs_pages; test_a_google_form_is_not_downloadable;
       test_non_ascii_counts_utf8_bytes (monkeypatch the _source module's MAX_SOURCE_RESULT_BYTES to 100; a fixture
       value of "é" * 20 is served and its "bytes" equals len(json.dumps(data, ensure_ascii=False).encode())).

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk tests/unit/examples -q passes
  - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py -q passes
  - grep -rn 'import privacyfence\b\|from privacyfence\b' plugin-sdk/src prints nothing
- id: p8-testhost-params
  title: The test host checks source-call parameters with the daemon's rules
  depends_on:
  - p7-testhost-manifest
  complexity: M
  touches:
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_params.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_source.py
  - tests/unit/plugin_sdk/test_testhost_params.py
  - tests/unit/plugin_sdk/test_testhost.py
  - tests/unit/plugin_sdk/test_testhost_surfaces.py
  - tests/unit/examples/test_today_plugin.py
  - tests/integration/test_sdk_testhost_conformance.py
  brief: |
    Read the plan's Design "The test host follows the manifest…" (P4 bullet) first. Copy rules from
    src/privacyfence/plugins/source_ops.py (lines 92-116, 174-184, 212-229, 240-245, 265-274, 311-320, 347-348,
    392-399), salesforce_client.py:112-115 and connectors/salesforce.py:160-189; never import privacyfence in the SDK.
    1. New testing/_params.py with the header line from the Design, the copied constants and validate(operation,
       params) raising RpcError("invalid_params", <the daemon's exact detail>) in the daemon's order.
    2. testing/_source.py _serve: after the allowlist and the dict check, raise a matching connector_unavailable
       fixture error first; then _params.validate; then match fixtures. Remove the duplicated checks in _serve_drive
       (about lines 223-230), keeping its cursor and revision logic.
    3. New tests/unit/plugin_sdk/test_testhost_params.py (it may import both packages, as conftest.py allows):
       CASES, about 40 (operation, params) pairs, at least 3 per operation, valid and invalid (bools as ints, empty
       strings, values over each length limit, offset with cursor, an unknown filter operator, a bad filter shape, a
       bad value_render_option); test_same_verdict compares SOURCE_ADAPTERS[op].validate(params)'s (code, detail), or
       "ok", with _params.validate's; test_every_operation_has_cases (the case operations == SOURCE_OPERATIONS);
       test_copied_limits_match (each copied constant, REPORT_FILTER_OPERATORS and _FILTERS_SHAPE_ERROR equal the
       daemon's).
    4. Existing tests that now fail because a fixture call lacks a required parameter: add the parameter in the test.
       Never loosen the validator.
    Stop condition: a failing test outside this phase's touches, or a daemon validator that differs from what the
    plan lists (for example the hotfix added a report_id rule in source_ops): stop with status=blocked.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk tests/unit/examples -q passes
  - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py -q passes
  - grep -rn 'import privacyfence\b\|from privacyfence\b' plugin-sdk/src prints nothing
- id: p9-testhost-pii
  title: The test host can model the PII check that overrides an Always-allow rule
  depends_on:
  - p8-testhost-params
  complexity: S
  touches:
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_gate.py
  - tests/unit/plugin_sdk/test_testhost.py
  - tests/integration/test_sdk_testhost_conformance.py
  brief: |
    Read the plan's Design "The test host follows the manifest…" (P6 bullet) first.
    1. testing/_gate.py: copy flatten_text from src/privacyfence/plugins/blocks.py:256-271; Card.pii_flagged: bool =
       False.
    2. testing/_host.py: the pii constructor argument; in _run_call the review-gate scan as given; the "pii_detected":
       True audit field; auto and popup never scanned.
    3. Tests in test_testhost.py: test_pii_overrides_a_matching_rule (pii=lambda t: "IBAN" in t, a rule matches,
       card_shown is True, card.pii_flagged, decision approved); test_pii_ignored_on_popup_and_auto.
    4. tests/integration/test_sdk_testhost_conformance.py TestSameOutcomes: test_scope_rule_with_pii_shows_a_card with
       review_read and text "GB82WEST12345698765432"; the SDK side passes pii=lambda t: "GB82WEST" in t; the daemon side
       answers the extra PII confirmation.
    Stop condition: if tests/fixtures/plugins/echo/harness.py cannot answer show_pii_confirmation_popup, or PII
    detection is off in the stack, leave step 4 out and say so in the PHASE-REPORT (plan Risks).

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk -q passes
  - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py -q passes
  - grep -n 'pii_flagged' plugin-sdk/src/privacyfence_plugin_sdk/testing/_gate.py prints at least one line
- id: p10-cards-expire
  title: A plugin's pending approval and confirmation cards expire when it is disabled, purged or removed
  depends_on:
  - p5-host-lock
  complexity: M
  touches:
  - src/privacyfence/plugins/confirm.py
  - src/privacyfence/plugins/approvals.py
  - src/privacyfence/plugins/host.py
  - tests/unit/plugins/test_confirm.py
  - tests/unit/plugins/test_approvals.py
  - tests/unit/plugins/test_host.py
  brief: |
    Read the plan's Design "Pending cards end with the plugin" first; it is the spec.
    1. confirm.py: ConfirmationService.expire_plugin as given.
    2. approvals.py: _epochs, the epoch field last in _Pending (filled in request() under the existing lock),
       ApprovalService.expire_plugin, and the epoch check at the start of _decide's confirm branch.
    3. host.py: _expire_cards and its four call sites (disable after _stop; purge right before _forget_approvals;
       _gone first statement; _on_state inside the crash/hash-drift disable branch). Never from _stop.
    4. Tests:
       - test_confirm.py class TestExpirePlugin with the harness fixture: test_expires_only_that_plugins_confirmations
         (two requests, PLUGIN and "other"; expire_plugin(PLUGIN) == 1; mine "expired", other "pending";
         registry.answer(mine, "confirm") is False; after polling (PLUGIN, "publish_note", "expired") in
         harness.audits).
       - test_approvals.py class TestExpirePlugin with Harness, _key() and settled():
         test_expire_plugin_expires_and_stores_nothing; test_a_confirm_after_the_epoch_moved_is_not_stored (set
         harness.service._epochs[PLUGIN] = 1, answer confirm, settled(): await_status "expired", store.find(*_key()) is
         None, audits[-1] == (PLUGIN, "processor-code", "expired")); test_other_plugins_cards_are_untouched.
       - test_host.py class TestPendingCardsEndWithThePlugin with Env, env.add(SDK, sdk=True), the /approval and
         /confirm pages, until() and env.audit.summaries(...): test_disable_expires_pending_cards;
         test_purge_expires_pending_cards_and_stores_nothing (open /approval, await host.purge(SDK),
         env.registry.answer(id, "confirm") is False, host._approval_store.for_plugin(SDK) == []);
         test_removal_expires_pending_cards (like TestUninstallForgetsApprovals but unanswered; remove the folder while
         running as TestUninstall does, rescan, then "expired" and an empty store).
    Stop condition: every existing test in test_approvals.py and test_confirm.py (TestClose,
    test_close_expires_and_removes_the_tuple and the decide/answer tests included) must pass unchanged. The epoch
    check stays as defence in depth on top of the finalize-then-discard rollback; if it changes the outcome of an
    existing test, stop with status=blocked instead of editing that test.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_confirm.py tests/unit/plugins/test_approvals.py tests/unit/plugins/test_host.py -q passes
  - grep -n 'def expire_plugin' src/privacyfence/plugins/confirm.py src/privacyfence/plugins/approvals.py prints two lines
  - grep -c '_expire_cards(' src/privacyfence/plugins/host.py prints 5
- id: p11-sdk-prepared-and-wait
  title: Prepared-call lifetime covers the replay window, a capped SDK store, and confirm/approval wait()
  depends_on:
  - p4-log-pump
  - p8-testhost-params
  - p10-cards-expire
  complexity: M
  touches:
  - src/privacyfence/plugins/constants.py
  - src/privacyfence/plugins/confirm.py
  - src/privacyfence/plugins/approvals.py
  - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
  - examples/plugins/today/today_plugin.py
  - tests/unit/plugins/test_constants.py
  - tests/unit/plugin_sdk/test_plugin.py
  - tests/unit/examples/test_today_plugin.py
  brief: |
    Read the plan's Design "Prepared calls and waits in the SDK" first; it is the spec.
    1. constants.py: PENDING_CARD_SECONDS, DECISION_REPLAY_SECONDS and PREPARED_CALL_LIFETIME_SECONDS as given.
       confirm.py and approvals.py: retain_finished_seconds = PENDING_CARD_SECONDS (import it; drop the unused import).
    2. SDK plugin.py: _PREPARED_CALL_LIFETIME_SECONDS = 1200.0; _MAX_PREPARED_CALLS = 256 outside the limits block,
       the eviction loop and warning in _prepare, the auto-read deletion in _execute; _WAIT_MAX_ROUNDS = 12 and wait()
       on ConfirmClient and ApprovalsClient exactly as given.
    3. today_plugin.py: verdict = await ctx.confirm.wait(approval_id); keep except SourceError and its comment.
    4. Tests:
       - test_constants.py TestLifetimes.test_prepared_lifetime_is_pending_plus_replay (the two constants equal
         privacyfence.approvals.DEFAULT_PENDING_TTL_SECONDS and DEFAULT_LEDGER_TTL_SECONDS; the sum is 1200).
       - test_plugin.py: test_prepared_call_survives_until_the_replay_window_ends (advance the clock by
         _PREPARED_CALL_LIFETIME_SECONDS - 1, execute succeeds); test_store_evicts_the_oldest_beyond_the_cap
         (monkeypatch _MAX_PREPARED_CALLS = 2, prepare c1, c2, c3; executing c1 is unknown_call, c3 works);
         test_an_auto_read_is_dropped_after_execute; TestSourceAndConfirm.test_confirm_wait_retries_timeouts (the
         handler answers confirm.await twice with {"error": {"code": -32013, "message": "timeout", "data": {"code":
         "timeout", "detail": "still pending", "retryable": true}}} then approved: 3 confirm.await messages, status
         approved); test_confirm_wait_raises_other_errors; the same two for approvals.
       - test_today_plugin.py TestPublishConfirmation.test_a_late_approval_still_publishes: monkeypatch both
         privacyfence_plugin_sdk.plugin._CONFIRM_AWAIT_MAX_MS = 50 and
         privacyfence_plugin_sdk.testing._confirm._AWAIT_MAX_MS = 50 (the test host's own cap, which otherwise holds
         each await for 300 s); before the host starts, wrap
         privacyfence_plugin_sdk.testing._confirm.Confirmations.await_ with monkeypatch to count its calls; publish,
         await asyncio.sleep(0.2), decide_confirmation(..., "approve"), until the day is published; assert the
         wrapper counted at least 2 confirm.await requests (one alone would mean the old single await passed).
         Any approvals wait test that goes through the test host also monkeypatches
         privacyfence_plugin_sdk.testing._approvals._AWAIT_MAX_MS (about line 25). Those two test-host modules are
         only monkeypatched, never edited.
       TestLimits keeps pinning _PREPARED_CALL_LIFETIME_SECONDS to the daemon's constant.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_constants.py tests/unit/plugins/test_confirm.py tests/unit/plugins/test_approvals.py tests/unit/plugin_sdk tests/unit/examples -q passes
  - python3 -c "from privacyfence.plugins import constants as c; assert c.PREPARED_CALL_LIFETIME_SECONDS == 1200.0" exits 0
  - grep -n 'async def wait' plugin-sdk/src/privacyfence_plugin_sdk/plugin.py prints two lines
- id: p12-write-result-cap
  title: A write tool's result is capped at 2,048 bytes and PII-scanned before it reaches the AI
  depends_on:
  - p9-testhost-pii
  - p11-sdk-prepared-and-wait
  complexity: M
  touches:
  - src/privacyfence/plugins/constants.py
  - src/privacyfence/plugins/connector.py
  - src/privacyfence/plugins/confirm.py
  - src/privacyfence/plugins/approvals.py
  - src/privacyfence/plugins/host.py
  - src/privacyfence/pii_detector.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
  - tests/unit/plugins/test_connector.py
  - tests/unit/plugins/test_constants.py
  - tests/unit/plugins/test_confirm.py
  - tests/unit/plugins/test_approvals.py
  - tests/unit/plugins/test_host.py
  - tests/unit/plugin_sdk/test_testhost.py
  brief: |
    Read the plan's Design "Write results are capped and scanned" first; it is the spec.
    0. Read every write-tool result that tests/integration/test_plugin_*.py and
       tests/integration/test_sdk_testhost_conformance.py assert (grep -n '"result"\|approval_id\|released' in
       them, and the write tools of tests/fixtures/plugins/echo/echo_plugin.py and examples/plugins/today). If one is
       over 2,048 bytes serialized, or holds something the PII detector flags (run
       privacyfence.pii_detector.detect_pii_categories on its JSON: an IBAN, a card number, an IP address, …), stop
       with status=blocked (plan Risks).
    1. constants.py: WRITE_RESULT_MAX_BYTES = 2048 with the ADR comment. Do not touch the schema (p13 does).
    2. connector.py: WRITE_RESULT_WITHHELD, the detect_pii_categories import, the owns_approval keyword argument,
       _screen_write_result and _withheld, and _execute returning it; remove the INLINE_RESULT_BYTES raise and the
       import if unused.
    2a. confirm.py ConfirmationService.owns and approvals.py ApprovalService.owns as given; host.py passes
       owns_approval to PluginConnector in on_ready as given.
    3. pii_detector.py: the module docstring sentence as given (docstring only).
    4. testing/_host.py: _WRITE_RESULT_MAX_BYTES, _WRITE_RESULT_WITHHELD and the released-value cap as given (keeping
       approval_id only when it is a key of self._confirmations._cards or self._approvals._cards), with the docstring
       note that only the cap is mirrored.
    5. Tests:
       - test_connector.py: rewrite TestErrors.test_write_result_over_the_inline_limit (about line 856) so
         {"result": "x" * 2049} returns {"withheld": True, "message": WRITE_RESULT_WITHHELD} with no exception and
         tool.execute called once; add test_write_result_at_the_cap_is_returned (a value whose _wire_size is exactly
         2048), test_write_result_with_personal_data_is_withheld ({"result": {"iban": "DE89370400440532013000"}}),
         test_an_oversized_approval_id_is_withheld ({"result": {}, "approval_id": "a" * 3000}: withheld, and the
         unknown approval_id is not in the result),
         test_a_withheld_result_keeps_an_approval_id_the_plugin_owns (make_connector with owns_approval=lambda i: i ==
         "abc"; {"result": "x" * 3000, "approval_id": "abc"} gives {"withheld": True, "message": WRITE_RESULT_WITHHELD,
         "approval_id": "abc"}), test_a_withheld_result_drops_an_approval_id_the_plugin_does_not_own (owns_approval
         returns False: no "approval_id" key),
         test_auto_write_result_is_screened_too (gate "auto", MANIFEST.max_gate_floor = "auto"),
         test_pii_detection_off_returns_the_result (monkeypatch plugin_connector.detect_pii_categories to return []).
         Keep test_write_result_with_approval_id and test_non_object_write_result_with_approval_id unchanged.
       - test_constants.py: WRITE_RESULT_MAX_BYTES == 2048.
       - test_confirm.py: test_owns_only_its_own_plugins_cards (a request by PLUGIN: owns(PLUGIN, id) is True,
         owns("other", id) and owns(PLUGIN, "nope") are False). test_approvals.py: the same, plus owns(PLUGIN, id) is
         True for a stored approval's id after the card is gone, and False once it is revoked.
       - test_host.py: a running plugin's connector answers owns_approval for an id the plugin's /confirm page
         requested (use the existing SDK plugin and pages, as TestPendingCardsEndWithThePlugin does).
       - test_testhost.py: a write whose result is over 2,048 bytes gives outcome.released == {"withheld": True,
         "message": ...} and outcome.result raw; test_limits_match_the_daemons_constants gains
         host_module._WRITE_RESULT_MAX_BYTES == constants.WRITE_RESULT_MAX_BYTES.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_connector.py tests/unit/plugins/test_constants.py tests/unit/plugins/test_confirm.py tests/unit/plugins/test_approvals.py tests/unit/plugins/test_host.py tests/unit/plugin_sdk tests/unit/test_pii_detector.py -q passes
  - grep -n 'def owns' src/privacyfence/plugins/confirm.py src/privacyfence/plugins/approvals.py prints two lines
  - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_sdk_testhost_conformance.py -q passes
  - grep -n 'def _screen_write_result' src/privacyfence/plugins/connector.py prints one line
- id: p13-protocol-1-2
  title: Protocol 1.2.0 - pages.list, PageEntry, schema x-limits and regenerated SDK types
  depends_on:
  - p12-write-result-cap
  complexity: M
  touches:
  - src/privacyfence/plugins/constants.py
  - src/privacyfence/plugins/protocol.py
  - docs/plugin-protocol/protocol.schema.json
  - plugin-sdk/src/privacyfence_plugin_sdk/types.py
  - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
  - tests/unit/plugins/test_constants.py
  - tests/unit/plugins/test_protocol.py
  - tests/unit/plugins/test_host.py
  - tests/unit/plugin_sdk/test_plugin.py
  brief: |
    Read the plan's Design "Protocol 1.2.0 (p13)" first; it is the spec. This is the only phase that edits
    PROTOCOL_VERSION, the schema and types.py.
    1. constants.py: PROTOCOL_VERSION = "1.2.0"; MAX_PAGE_INDEX_ENTRIES, MAX_PAGE_VERSION_CHARS,
       MAX_PAGE_DESCRIPTION_CHARS, PAGE_ENTRY_PATH_RE; TIMEOUT_SECONDS["pages.list"] = 10.0.
       SDK plugin.py: PROTOCOL_VERSION = "1.2.0" (nothing else in that file).
    2. protocol.py: PageEntry and PagesListResult as given, with WIRE_KEYS and __all__, using the existing helpers;
       normalize_path imported inside PageEntry.from_wire; the "." / ".." segment rule; timestamps through
       datetime.fromisoformat after replacing a trailing "Z" with "+00:00", requiring tzinfo; titles not only
       whitespace and clean_line(t) == t; details starting with "pages[<i>].<field>".
    3. protocol.schema.json: x-protocol-version "1.2.0"; $defs PagesListParams, PageEntry, PagesListResult (one-line
       pattern on title, version, description; path pattern "^" + PAGE_ENTRY_PATH_RE.pattern + "$"); x-limits gains
       MAX_PAGE_INDEX_ENTRIES, MAX_PAGE_VERSION_CHARS, MAX_PAGE_DESCRIPTION_CHARS, SEND_TIMEOUT_SECONDS,
       WRITE_RESULT_MAX_BYTES, and TIMEOUT_SECONDS gains "pages.list". Then run python3 scripts/gen_plugin_sdk_types.py
       to regenerate types.py; never edit or hand-merge types.py.
    4. Tests:
       - "1.1.0" becomes "1.2.0" at tests/unit/plugins/test_constants.py:145, test_protocol.py:198, test_host.py:324
         and tests/unit/plugin_sdk/test_plugin.py:130. Leave manifest protocol: values (test_approvals.py:28,
         test_plugin_approval_frame_browser.py:55) alone.
       - test_protocol.py: PagesListResult cases: a valid list with every field; 501 entries; each rule broken once
         (path without "/", with "#", with "\\", with "..", with "/./x", with "%2F", with "\t", "\n" or a space; title
         empty, "   ", 121 chars, with "\n", with "‮"; version 41 chars; created_at "yesterday" and
         "2026-10-09T10:00:00" without a zone; description 201 chars), each asserting code invalid_params and that
         detail starts with "pages[<i>].<field>"; add PagesListParams, PageEntry and PagesListResult to PROTOCOL_DEFS
         and to the parametrize lists of test_def_properties_match_the_dataclass and
         test_required_keys_are_all_handled; test_schema_patterns_match_the_constants asserts the PageEntry path
         pattern; test_one_line_pattern_refuses_what_clean_line_changes adds PageEntry title, version and description.
       - test_constants.py: the new constants' values.
    Stop condition: if test_gen_plugin_sdk_types.py or test_protocol.py's TestSchema needs a change to its own logic
    (not only to expected values or lists of definitions), stop with status=blocked.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_protocol.py tests/unit/plugins/test_constants.py tests/unit/plugins/test_host.py tests/unit/plugin_sdk/test_plugin.py tests/unit/test_gen_plugin_sdk_types.py -q passes
  - python3 scripts/gen_plugin_sdk_types.py --check exits 0
  - grep -rn '1\.1\.0' src plugin-sdk/src docs/plugin-protocol tests/unit/plugins/test_constants.py tests/unit/plugins/test_protocol.py tests/unit/plugins/test_host.py tests/unit/plugin_sdk/test_plugin.py prints nothing
  - python3 -c "import json; d=json.load(open('docs/plugin-protocol/protocol.schema.json')); l=d['x-limits']; assert l['SEND_TIMEOUT_SECONDS']==10.0 and l['WRITE_RESULT_MAX_BYTES']==2048 and l['TIMEOUT_SECONDS']['pages.list']==10.0" exits 0
- id: p14-sdk-page-index
  title: SDK PageEntry, @plugin.page_index, PluginTestHost.list_pages and the today example's index
  depends_on:
  - p13-protocol-1-2
  complexity: M
  touches:
  - plugin-sdk/src/privacyfence_plugin_sdk/__init__.py
  - plugin-sdk/src/privacyfence_plugin_sdk/responses.py
  - plugin-sdk/src/privacyfence_plugin_sdk/_page_index.py
  - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
  - plugin-sdk/src/privacyfence_plugin_sdk/testing/_pages.py
  - examples/plugins/today/today_plugin.py
  - examples/plugins/today/README.md
  - tests/unit/plugin_sdk/test_plugin.py
  - tests/unit/plugin_sdk/test_testhost.py
  - tests/unit/plugin_sdk/test_testhost_surfaces.py
  - tests/unit/examples/test_today_plugin.py
  brief: |
    Read the plan's Design "SDK page index (p14)" and the PageEntry table in "Protocol 1.2.0 (p13)" first. The SDK
    must not import privacyfence (ADR 0126): copy the rules.
    1. responses.py: PageEntry; __init__.py exports it.
    2. New _page_index.py: validate_page_entries and normalized_page_path with the private constants, the same rules
       and the same "pages[<i>].<field>" details as protocol.py's PageEntry, using the SDK's blocks.clean_line.
       testing/_pages.normalize_path calls normalized_page_path.
    3. plugin.py: _Registry.page_index; Plugin.page_index; _pages_list registered in serve() only when an index exists.
    4. testing/_host.py: list_pages as given; _TIMEOUTS["pages.list"] = 10.0.
    5. today_plugin.py: a @plugin.page_index returning [PageEntry("/", "Today")]. examples/plugins/today/README.md:
       the self-test line reads protocol 1.2.0, and one sentence says the plugin lists its page for the page browser.
    6. Tests:
       - test_plugin.py: registering page_index twice raises; a plugin with an index answers pages.list with the wire
         entries; without one pages.list is method_not_found; an invalid entry gives invalid_params (one case each
         for "\t", "/./x" and "#"); TestLimits.test_block_limits_and_version also asserts the _page_index private
         limits and _PAGE_ENTRY_PATH_RE.pattern equal privacyfence.plugins.constants'.
       - test_testhost_surfaces.py: list_pages returns the entries; without an index the fallback [{"path": "/",
         "title": <name>}]; an invalid entry raises AssertionError; pages=False raises LookupError.
       - test_testhost.py: the _TIMEOUTS parity test (about line 340) passes with the new key.
       - test_today_plugin.py: list_pages() == [{"path": "/", "title": "Today"}].

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugin_sdk tests/unit/examples tests/unit/test_build_example_plugin.py -q passes
  - grep -rn 'import privacyfence\b\|from privacyfence\b' plugin-sdk/src prints nothing
  - grep -n '1\.1\.0' examples/plugins/today/README.md prints nothing
- id: p15-host-page-index
  title: PluginHost.list_pages and list_all_pages; page links point at /plugin-pages
  depends_on:
  - p14-sdk-page-index
  complexity: S
  touches:
  - src/privacyfence/plugins/page_index.py
  - src/privacyfence/plugins/host.py
  - tests/fixtures/plugins/stub/stub_plugin.py
  - tests/unit/plugins/test_host.py
  brief: |
    Read the plan's Design "Host page index (p15)" first; it is the spec.
    1. New plugins/page_index.py: PAGE_INDEX_NO_ANSWER, PAGE_INDEX_INVALID, PageIndex and index_from_result with the
       given mapping and WARNING log (plugin name and code only).
    2. host.py: list_pages and list_all_pages as given (not under self._lock); page_links() and rows() use
       f"/plugin-pages/{name}".
    3. stub_plugin.py: modes "pages-invalid" (answers pages.list with {"pages": [{"path": "/"}]}) and "pages-slow"
       (never answers pages.list); add them to the docstring.
    4. Tests in test_host.py, new class TestPageIndex: SDK_PLUGIN gains a @plugin.page_index with two entries; two
       entries come back; the stub's default mode gives the one-entry fallback titled with the display name; a timeout
       (monkeypatch.setitem(rpc.TIMEOUT_SECONDS, "pages.list", 0.05), stub pages-slow) gives PAGE_INDEX_NO_ANSWER;
       pages-invalid gives PAGE_INDEX_INVALID and the WARNING log does not contain the result; a stopped plugin and a
       pages: false plugin raise LookupError; list_all_pages orders by (casefold, exact, name) and leaves out a plugin
       whose list_pages raises LookupError (monkeypatch it) while returning the others;
       test_list_pages_does_not_wait_for_a_host_action (hold host._lock in a task; list_pages completes within 2 s).
       Update the existing page_links/page_url assertions (about lines 1382 and 1391) to "/plugin-pages/<name>".

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/plugins/test_host.py -q passes
  - grep -n '/plugins/{name}/' src/privacyfence/plugins/host.py prints nothing
  - grep -n '_lock' src/privacyfence/plugins/page_index.py prints nothing
- id: p16-page-browser
  title: The /plugin-pages browser, the Plugins menu and the Settings card link
  depends_on:
  - p15-host-page-index
  complexity: M
  touches:
  - src/privacyfence/web/routes_plugin_browser.py
  - src/privacyfence/plugin_browser_html.py
  - src/privacyfence/web/server.py
  - src/privacyfence/web_shell.py
  - src/privacyfence/settings_window_html.py
  - tests/unit/web/test_routes_plugin_browser.py
  - tests/unit/test_plugin_browser_html.py
  - tests/unit/test_web_shell.py
  - tests/unit/test_settings_window_html.py
  - tests/unit/web/test_server.py
  - tests/integration/test_plugin_settings_browser.py
  brief: |
    Read the plan's Design "Page browser, menu and card (p16)" first; it is the spec.
    1. plugin_browser_html.py: render and format_timestamp as given.
    2. web/routes_plugin_browser.py: build_routes as given. server.py: mount it as given, right after the plugin page
       routes (about lines 1063-1068), only with a plugin host.
    3. web_shell.py: _plugins_html and updatePluginsMenu with "All plugin pages" first and no target/rel; keep the
       close-on-pick click handler and reword its comment as given. settings_window_html.py: the card link as given.
    4. Tests:
       - test_plugin_browser_html.py: a title "<img src=x>" and a path with '"' are escaped; href is
         "/plugins/<name><path>" with target="_blank" rel="noopener"; error and empty sections; "—" for missing
         fields; format_timestamp converts "+02:00" to UTC; no "<script" in the output; the single view has the "All
         plugin pages" link and no <h2>.
       - test_routes_plugin_browser.py, built like tests/unit/web/test_routes_plugins.py builds its app, with a fake
         host exposing list_pages, list_all_pages, page_links and web_request: owner session -> 200 with each entry; no
         session, an MCP bearer, a non-owner principal and an unattested session -> 404 "Not Found" and the fake host
         not called; an invalid name and an unknown plugin -> 404; the app's nonce CSP (no "sandbox") and
         Cache-Control: no-store.
       - test_web_shell.py and test_settings_window_html.py: the new hrefs, no target="_blank", the "All plugin pages"
         first item, the "Pages" label.
       - test_server.py: update the menu-link assertion (about line 516) to the same-tab link; /plugin-pages is mounted
         only with a plugin host; test_org_app_has_no_plugins_route also checks /plugin-pages; /plugin-pages joins
         TestCacheControlOnSensitivePages.
       - tests/integration/test_plugin_settings_browser.py: _ROWS page_url becomes "/plugin-pages/alpha"; the "Open
         page" assertion becomes "Pages"; the menu-close selectors follow the new href; a fake plugin host (page_links,
         list_pages, list_all_pages, web_request returning a small HTML body) passed as WebServer(..., plugin_host=fake);
         a phone-layout case _phone_cases(["plugin-pages"]) with _assert_phone_layout; a test that signs in, opens
         /plugin-pages/alpha from the Plugins menu, clicks an entry and, with context.expect_page(), gets a new tab
         showing the plugin's page (not "Not Found").
       Run the integration file with PRIVACYFENCE_TEST_CHROMIUM set (the session-start hook exports it); it must pass,
       not skip.
    Stop condition: if the new tab gets "Not Found" while signed in (the link does not carry the cookie), report the
    request headers and stop with status=blocked; do not change the cookie, the CSP or the sandbox.

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/unit/web/test_routes_plugin_browser.py tests/unit/test_plugin_browser_html.py tests/unit/test_web_shell.py tests/unit/test_settings_window_html.py tests/unit/web/test_server.py -q passes
  - with PRIVACYFENCE_TEST_CHROMIUM set, python3 -m pytest -rs tests/integration/test_plugin_settings_browser.py passes, and python3 -m pytest -rs tests/integration/test_plugin_settings_browser.py | grep -c SKIPPED prints 0
  - grep -n '_blank' src/privacyfence/web_shell.py prints nothing
- id: p17-pages-conformance
  title: The test host and the daemon agree on pages.list
  depends_on:
  - p15-host-page-index
  complexity: S
  touches:
  - tests/fixtures/plugins/echo/echo_plugin.py
  - tests/integration/test_sdk_testhost_conformance.py
  brief: |
    Read the plan's Design "Pages conformance (p17)" first.
    1. echo_plugin.py: the page index with the two entries the Design gives.
    2. test_sdk_testhost_conformance.py: one scenario comparing the test host's list_pages() with the daemon host's
       list_pages(...).entries in wire form; the docstring's "Known differences" gains "the fallback entry's title is
       the display name on the daemon, the plugin name on the test host".

    No CHANGELOG.md line in this phase. No plan item IDs (A2, S1, D3, P5, pb1 …) in code, comments or test names:
    tests/unit/test_code_no_history.py refuses them.
  acceptance:
  - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py tests/integration/test_plugin_framework.py -q passes
  - with PRIVACYFENCE_TEST_CHROMIUM set, python3 -m pytest -rs tests/integration/test_plugin_approval_frame_browser.py passes, and python3 -m pytest -rs tests/integration/test_plugin_approval_frame_browser.py | grep -c SKIPPED prints 0
- id: p18-docs-adr-0132
  title: ADR 0132 (send-timeout crash rule), ADR 0120 amendment, runtime reference docs and changelog
  depends_on:
  - p1-outputs-replay
  - p2-windows-owner
  - p6-source-consistency
  - p12-write-result-cap
  complexity: M
  touches:
  - docs/adr/0132-a-plugin-that-stops-reading-its-input-for-10-seconds-is-treated-as-crashed.md
  - docs/adr/0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md
  - docs/adr/README.md
  - docs/plugin-protocol.md
  - docs/plugins.md
  - docs/security-and-compliance.md
  - plugin-sdk/README.md
  - CHANGELOG.md
  brief: |
    1. Write ADR 0132 from docs/adr/README.md's template with the title, Status, context, decision, alternatives,
       consequences, verification and related items the plan's Design "p18, ADR 0132 (the send-timeout crash rule)
       and the runtime behaviour" gives. The date is today's (date -u +%F). ADR 0120: add only the Amended-by Status
       line as given (ADR 0121's format). docs/adr/README.md: the 0132 row and 0120's status cell.
    2. docs/security-and-compliance.md: replace the [ADR 0131](adr/0131-…) link that ends the Plugins section's second
       pair of ADR links with the [ADR 0132](adr/0132-…) link, as the Design's docs introduction says.
    3. Every other bullet of that Design part, by section heading, wording as written, including the runnable
       PluginTestHost examples in docs/plugins.md (### Testing a plugin) and plugin-sdk/README.md (### Testing a
       plugin), which gain source_operations=("calendar.list_events",) (and pages=True where they call host.get).
       Check each statement against the merged code before writing it.
    4. CHANGELOG.md: the two p18 changes under ## [Unreleased] -> ### Added, as written. No version heading.
    5. No "#<number>", "P<digit>", "since 1.x" or "as of 1.x" in the published docs (tests/unit/test_docs_no_history.py).
    Stop condition: docs/adr/0132-* already exists, or a section named in the Design is missing: stop with
    status=blocked.

    No plan item IDs (A2, S1, D3, P5, pb1 …) in the docs, code or test names.
  acceptance:
  - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py -q passes
  - grep -n 'SEND_TIMEOUT_SECONDS' docs/plugin-protocol.md prints at least one line
  - grep -n 'TrustedInstaller' docs/security-and-compliance.md prints at least one line
  - grep -n 'Plugin hardening' CHANGELOG.md prints one line
  - grep -n '0132' docs/adr/README.md docs/adr/0120-*.md docs/security-and-compliance.md shows the index row, 0120's cell, the Amended-by line and the link
  - grep -n 'source_operations=("calendar.list_events",)' docs/plugins.md plugin-sdk/README.md prints at least one line per file
- id: p19-docs-adr-0133
  title: ADR 0133 (write results), ADR 0122 amendment, reference docs and changelog
  depends_on:
  - p18-docs-adr-0132
  complexity: S
  touches:
  - docs/adr/0133-a-plugin-writes-result-is-capped-and-pii-scanned-before-it-reaches-the-ai.md
  - docs/adr/0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md
  - docs/adr/README.md
  - docs/plugin-protocol.md
  - docs/plugins.md
  - docs/security-and-compliance.md
  - CHANGELOG.md
  brief: |
    1. Write ADR 0133 from docs/adr/README.md's template with the title, Status, decision, alternatives, consequences,
       verification and related items the plan's Design "p19, ADR 0133" gives. The date is today's (date -u +%F).
    2. ADR 0122: add only the Amended-by Status line as given (ADR 0121's format). Nothing else in its body changes.
    3. docs/adr/README.md: the 0133 row and 0122's status cell as given.
    4. docs/plugin-protocol.md, docs/security-and-compliance.md (including replacing the [ADR 0132](adr/0132-…) link
       at the end of the Plugins section's second pair of ADR links with [ADR 0133](adr/0133-…)), docs/plugins.md,
       CHANGELOG.md: as given, by section heading. No version heading in CHANGELOG.md.
    Stop condition: docs/adr/0133-*.md already exists with another title: stop with status=blocked.

    No plan item IDs (A2, S1, D3, P5, pb1 …) in the docs, code or test names.
  acceptance:
  - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py -q passes
  - grep -n '0133' docs/adr/README.md docs/adr/0122-*.md shows the index row, 0122's cell and the Amended-by line
  - grep -n '2,048 bytes' docs/plugin-protocol.md docs/security-and-compliance.md prints at least one line per file
  - grep -n '100,000 bytes serialized' docs/plugin-protocol.md prints nothing
- id: p20-docs-adr-0134
  title: ADR 0134 (paged reads), ADR 0128 amendment, reference docs and changelog
  depends_on:
  - p19-docs-adr-0133
  complexity: S
  touches:
  - docs/adr/0134-paged-source-reads-read-one-version-and-measure-utf8-bytes.md
  - docs/adr/0128-plugin-source-reads-never-truncate.md
  - docs/adr/README.md
  - docs/plugin-protocol.md
  - docs/plugins.md
  - docs/security-and-compliance.md
  - CHANGELOG.md
  brief: |
    1. Write ADR 0134 from docs/adr/README.md's template as the plan's Design "p20, ADR 0134" gives it. The date is
       today's.
    2. ADR 0128: add only the Amended-by Status line as given.
    3. docs/adr/README.md: the 0134 row and 0128's status cell as given.
    4. docs/plugin-protocol.md (## Errors, ### Source calls, ### Paging), docs/plugins.md (## Large reads and
       downloads), docs/security-and-compliance.md (including the link replacement 0133 -> 0134) and CHANGELOG.md: as
       given, by section heading.
    Stop condition: docs/adr/0134-*.md already exists with another title: stop with status=blocked.

    No plan item IDs (A2, S1, D3, P5, pb1 …) in the docs, code or test names.
  acceptance:
  - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py -q passes
  - grep -n '0134' docs/adr/README.md docs/adr/0128-*.md shows the index row, 0128's cell and the Amended-by line
  - grep -n 'cursor_expired' docs/plugin-protocol.md prints at least one line
  - grep -n 'not_downloadable' docs/plugin-protocol.md prints at least one line
- id: p21-docs-adr-0135
  title: ADR 0135 (page browser), ADR 0124 amendment, protocol 1.2 docs and changelog
  depends_on:
  - p20-docs-adr-0134
  - p16-page-browser
  - p17-pages-conformance
  complexity: M
  touches:
  - docs/adr/0135-plugin-pages-are-listed-by-the-plugin-and-browsed-in-privacyfence.md
  - docs/adr/0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md
  - docs/adr/README.md
  - docs/plugin-protocol.md
  - docs/plugins.md
  - docs/security-and-compliance.md
  - plugin-sdk/README.md
  - CHANGELOG.md
  brief: |
    1. Write ADR 0135 from docs/adr/README.md's template as the plan's Design "p21, ADR 0135" gives it, with the
       Alternatives from "What was rejected (these go into ADR 0135)". The date is today's.
    2. ADR 0124: add only the Amended-by Status line as given.
    3. docs/adr/README.md: the 0135 row and 0124's status cell as given.
    4. docs/plugin-protocol.md: "1.1.0" -> "1.2.0" (top and both initialize examples), the "Version 1.2 adds"
       paragraph exactly as written (it names WRITE_RESULT_MAX_BYTES and SEND_TIMEOUT_SECONDS), the Messages row, the new ### pages.list section after ### Pages, the limits and
       timeouts rows.
    5. docs/plugins.md, docs/security-and-compliance.md (including the link replacement 0134 -> 0135),
       plugin-sdk/README.md and CHANGELOG.md: as given, by section heading. No version heading in CHANGELOG.md.
    Stop condition: docs/adr/0135-*.md already exists with another title, or a section named in the Design is missing:
    stop with status=blocked.

    No plan item IDs (A2, S1, D3, P5, pb1 …) in the docs, code or test names.
  acceptance:
  - python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py tests/unit/plugins/test_protocol_doc.py -q passes
  - grep -n '0135' docs/adr/README.md docs/adr/0124-*.md shows the index row, 0124's cell and the Amended-by line
  - grep -n '1\.1\.0' docs/plugin-protocol.md prints nothing
  - grep -n '### `pages.list`' docs/plugin-protocol.md prints one line
  - grep -n 'Plugin page browser' CHANGELOG.md prints one line
- id: p22-retire
  title: Retire the plan
  depends_on:
  - p21-docs-adr-0135
  complexity: S
  touches:
  - docs/plugin-framework-hardening-plan.md
  - docs/plugin-framework-hardening-plan-manual-steps.html
  - docs/README.md
  - scripts/build_site.py
  brief: |
    1. Check that ADRs 0132, 0133, 0134 and 0135 exist, are Accepted and are in docs/adr/README.md's index, and that
       CHANGELOG.md has the docs phases' lines under ## [Unreleased] and no new version heading. If not, stop with
       status=blocked naming what is missing.
    2. Delete docs/plugin-framework-hardening-plan.md and docs/plugin-framework-hardening-plan-manual-steps.html.
    3. Remove this plan's entries from docs/README.md and from scripts/build_site.py CONTRIBUTOR_DOCS.
    4. grep -rn plugin-framework-hardening-plan . --exclude-dir=.git must print nothing; fix any remaining reference
       inside this phase's touches, otherwise stop with status=blocked.

    No plan item IDs (A2, S1, D3, P5, pb1 …) in the docs, code or test names.
  acceptance:
  - python3 -m pytest tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py tests/unit/test_build_site.py -q passes
  - grep -rn plugin-framework-hardening-plan . --exclude-dir=.git prints nothing
  - ls docs/adr/0132-*.md docs/adr/0133-*.md docs/adr/0134-*.md docs/adr/0135-*.md succeeds
```
