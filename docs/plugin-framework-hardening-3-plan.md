# Plan: plugin framework hardening, round 3 (daemon-internal fixes, tests and docs)

## Goal

A security and test review of the plugin framework (2026-10-09, on PR #874 at 392e3ebc) found 47
problems. This plan fixes the 23 of class `other`: daemon-internal hardening, web-session and
upload-slot bugs, audit and logging gaps, the example plugin and SDK release checks, test quality,
and non-normative docs. None of them changes what a plugin author builds against. The 24
`protocol` findings, two of them high, are in Plan A (`plan/plugin-protocol-hardening-2`), which
must land before plugin development starts. This plan can follow it.

What changes:

1. **Sessions and redirects.** An expired PrivacyFence session no longer opens plugin pages or
   the page browser, and the bootstrap link can no longer redirect off the site (F29, F30).
2. **Source calls cannot starve the daemon.** Source calls run on their own small thread pool, with
   a per-plugin cap and a daemon-side deadline. Every call is audited, a cancelled one included,
   and the audit summary quotes plugin-chosen text. Drive file ids are validated (F25, F28, F33, F34).
3. **Spooled data does not linger.** A periodic sweep runs, a plugin's spool is dropped when it
   stops, is disabled, purged or uninstalled, and Drive exports are capped per plugin. Sheets
   snapshot cursors are bound to their sheet, and lone surrogates no longer break page sizing (F26,
   F31, F32).
4. **Upload slots are bounded.** There is a per-principal count and byte cap, the PUT body is
   streamed, the hold has a ceiling, and the tool description is accurate (F27).
5. **Outputs paths on Windows and macOS** (junctions, case variants), and an atomic SDK publish
   (F35-F37).
6. **Audit, logs and the RPC reader.** Approval audit rows carry the id, subject and digest. No
   plugin-sent detail reaches the daemon log. A deeply nested line counts as a parse error (F38-F40,
   F46).
7. **The example, the SDK release, test quality, strict typing and docs** (F41-F45, F47).

Tracking issue: [privacyfence/privacyfence#846](https://github.com/privacyfence/privacyfence/issues/846).
The full findings list with reproduction notes is the appendix at the end of this plan.

## Current state

### The base

- This plan branch is cut from `origin/feature/plugin-files-and-page-links` at 392e3ebc, not from
  `main`. That branch is [PR #874](https://github.com/privacyfence/privacyfence/pull/874), and the
  code this plan talks about exists only there. `manual_before` `mb1-pr874-merged` asks the
  maintainer to merge it first, or to accept that this plan's PR carries #874's diff until #874
  merges. p0 merges `origin/main`.
- **Plan A** (`plan/plugin-protocol-hardening-2`) edits several of the same files:
  `source_ops.py`, `spool.py`, `outputs.py`, `host.py`, `connector.py`, `blocks.py` and `rpc.py`'s
  neighbours. The two plans share no design, but they do share lines. `manual_before`
  `mb2-plan-a-first` recommends running Plan A first. p0 merges `origin/main`, so if Plan A's PR is
  merged by then, this plan builds on it. Otherwise whichever PR lands second merges `main` and
  resolves the conflicts.
- `docs/adr/` ends at 0141. Plan A takes 0142 to 0147. This plan's one ADR takes the next free
  number at p10, expected to be **0148**.
- Line numbers are at 392e3ebc.

### What the review checked and ran

The same as Plan A: ruff, bandit and mypy strict pass, `tests/unit` gives 15437 passed and 36
skipped, and the plugin integration files give 132 passed.

### The code each phase changes

- **Web sessions.**
  - `web/server.py:1070-1073` gives both plugin route sets
    `is_owner_session=lambda r: _is_human_session(r, sessions)`.
  - `session_auth.LocalSessionStore.provenance()` (`session_auth.py:187-205`) does not check expiry.
    Only `touch()` (`:166-184`) and `authenticated()` do, and no plugin route calls them.
    `server.py:140` imports `authenticated` as `_session_authenticated`.
  - `_BootstrapMiddleware` redirects to `request.url.path` (`server.py:781`).
- **Source calls.**
  - `source_ops.handle_source_call` runs adapters through `asyncio.to_thread` (`:643`), the default
    pool, which is shared with connector I/O and the PII detector (`gate.py:318-327`, `:949-959`).
  - The audit entry is written on success (`:691`) and on `RpcError`/`Exception` (`:672-689`), but
    not on `asyncio.CancelledError`.
  - Targets are formatted raw (`:270`, `:546`, `:562`).
- **Spool.**
  - `DownloadSpool.sweep` (`spool.py:240`) runs only from spool calls.
  - `clear` (`:254`) runs at host stop.
  - The spool is created lazily (`host.py:538-541`).
  - `purge` (`host.py:875-895`) does not touch it.
  - `_entries` (exports) has no per-plugin cap; `_SNAPSHOTS_PER_PLUGIN = 4` covers Sheets only.
- **Upload slots.**
  - `UploadStagingStore.create_slot` (`upload_staging.py:139-175`) has no count check.
  - All three fill paths, `fill`, `afill` (the one production uses) and `afill_capability`
    (`:181-239`), buffer the plaintext in a `bytearray`. `_finish_fill` (`:259-285`) claims the slot,
    then writes `nonce + ciphertext`.
  - `local_files` passes `hold_until` to `peek` on every read (`:254-256`), and `peek` stores it
    (`:300`). `create_slot` is called at `local_files.py:389` and `:487`.
- **RPC.** `rpc._handle_line` catches `ValueError` only around `json.loads` (`:259-263`); the SDK
  does the same (`_rpc.py:261-265`).

### Constraints

The same as Plan A:

- no plan or finding ids in code (`test_code_no_history.py`);
- comments only for a non-obvious *why*;
- ADRs are frozen once accepted;
- §2.7 is the definition of done.

Two rows of `dod_conditional` apply:

- **p1** changes `web/server.py`. Run `pytest tests/integration -v`; `test_mcp_daemon_contract.py`
  and `test_shim_mcp_contract.py` must pass.
- **p2** changes `drive_client.py`. The PR dispatches `connector-live-check.yml` and links the run.

## Design

### E1. Sessions on plugin routes and the bootstrap redirect (F29, F30)

- `build_app` gives both plugin route sets
  `is_owner_session=lambda r: _session_authenticated(r, sessions) and _is_human_session(r, sessions)`.
  `_session_authenticated` is `session_auth.authenticated`, imported at `server.py:140`.
  - It checks expiry and calls `touch()`, so an expired session gets the routes' existing 404.
  - Plugin page views refresh the idle timer like any other page.
- `session_auth.py` is not changed. Making `provenance()` or `principal_id()` expiry-aware would
  change `_local_principal_resolver` (`server.py:589-596`): an expired `os-<uid>` session would fall
  back to the owner principal.
- `_BootstrapMiddleware`: `RedirectResponse("/" + request.url.path.lstrip("/"), status_code=303)`.

### E2. Source calls (F25, F28, F33, F34)

- **Executor.** A module-level `concurrent.futures.ThreadPoolExecutor(max_workers=SOURCE_WORKERS,
  thread_name_prefix="plugin-source")`, created lazily, with `SOURCE_WORKERS = 4` in `source_ops.py`.
  It is daemon-internal, not a protocol limit.
  - Adapters run on it with
    `ctx = contextvars.copy_context(); await loop.run_in_executor(executor, ctx.run, fn, *args)`.
    This keeps the contextvars `to_thread` copied (the principal scope).
  - `shutdown_executor()` calls `shutdown(wait=False, cancel_futures=True)` and resets the global to
    `None`, so the next call creates a fresh pool.
  - `PluginHost.stop_all` calls it, and `tests/conftest.py` resets it.
- **Per-plugin cap.** A `dict[str, asyncio.Semaphore]` keyed by plugin name, with
  `PER_PLUGIN_SOURCE_CALLS = 2`. That is half the pool, so two plugins always make progress beside
  each other. A call waits for its slot.
- **Deadline.** `SOURCE_CALL_DEADLINE_SECONDS = 110.0`, a module constant below the plugin's own
  120 s wait.
  - The whole handling of one call (the semaphore wait, the executor queue and the run) sits inside
    `async with asyncio.timeout(SOURCE_CALL_DEADLINE_SECONDS):`.
  - On expiry it answers
    `RpcError("timeout", "the connector did not answer in time", retryable=True)` and audits
    `error=timeout`.
  - The thread keeps running until the client returns (Python cannot kill it); the pool bound keeps
    that harmless. Tests patch `source_ops.SOURCE_CALL_DEADLINE_SECONDS`.
- **Audit on every outcome.** The audit write moves into a `finally`, with an `outcome` variable set
  to the result bytes, the error code, or `cancelled` (for `asyncio.CancelledError`, which is
  re-raised).
- **Quoting.** Every params value that a summary lambda formats is first cut to 200 characters,
  then written as `json.dumps(value, ensure_ascii=True)`. Those values are `report_id`, `page_by`,
  `file_id`, `spreadsheet_id`, `range`, `calendar_id`, `time_min`, `time_max` and the Confluence page
  id. Numbers and counts stay as they are. A refused operation that is in `SOURCE_OPERATIONS` is
  recorded under its own name, not `unknown`.
- **Drive file ids.** `_validate_drive_download` refuses a `file_id` not fullmatching
  `[A-Za-z0-9_-]{1,256}` with `invalid_params` `"params.file_id is not a Drive file id"`, the style
  of the module's other messages. `drive_client.download_range` and `_stream_full_content` use
  `urllib.parse.quote(file_id, safe="")` in the URL. `testing/_source.py` applies the same pattern.

### E3. Spool lifecycle (F26, F31, F32)

- `PluginHost.start` creates the spool eagerly with `_get_spool()`. `DownloadSpool.__init__` already
  empties its directory (`spool.py:88-91`), so a crashed run's leftovers go.
- `start` also starts a task that calls `spool.sweep()` every `SPOOL_SWEEP_SECONDS = 60` (a
  `host.py` constant) until `stop_all` cancels it.
- New `DownloadSpool.drop_plugin(name)` deletes every entry and file of that plugin. It is called
  from:
  - `PluginHost._stop` (any intentional stop);
  - `_on_state` when the state becomes `disabled` or `backoff`;
  - `purge`;
  - `_uninstall`.
- Drive exports: at most `_EXPORTS_PER_PLUGIN = 4` per plugin. Over the cap, the least recently used
  is evicted, as `_SNAPSHOTS_PER_PLUGIN` does.
- Sheets snapshot binding:
  - `_RowsEntry` gains `bound: str`, a sha256 hex of `json.dumps(_bound_sheets(params), sort_keys=True)`.
  - `put_rows` and `rows_page` take `bound`. On a mismatch, `rows_page` raises a new
    `SnapshotMismatch(ValueError)` defined in `spool.py`; `KeyError` stays "not held".
  - `_run_sheets` maps `SnapshotMismatch` to `_bad_cursor()` (`invalid_params` "cursor is not
    valid") and `KeyError` to `_cursor_expired()`, as today.
- Lone surrogates: new `_scrub_surrogates(value)` recursively replaces each code point in U+D800 to
  U+DFFF in every string with U+FFFD.
  - It is applied to the client's return value in each `_run_*` function, on the line right after
    the client call, before `_fit_prefix`, `put_rows`, Confluence's slice search or
    `_run_provider_paged`'s sizing.

### E4. Upload slots (F27)

- **Caps.** In `upload_staging.py`: `MAX_SLOTS_PER_PRINCIPAL = 32` and
  `MAX_STAGED_BYTES_PER_PRINCIPAL = 256 * 1024 * 1024`, counted over live slots, a slot counting by
  its `max_bytes`.
  - Over either cap, `create_slot` raises a new `UploadSlotLimitError(ValueError)`, defined in
    `upload_staging.py`. `local_files` imports upload_staging, so upload_staging cannot import
    `LocalFileAccessError`.
  - `local_files.py` catches it at both `create_slot` call sites (`:389` and `:487`) and raises
    `LocalFileAccessError("Too many pending uploads; use or let the earlier ones expire first.")`.
- **Streamed fill.** One private streaming writer serves `fill`, `afill` and `afill_capability`.
  Plaintext is cut into fixed 64 KiB records, the last one shorter. The file format:
  1. a 4-byte magic `b"PFU2"`;
  2. then, per record: a 4-byte big-endian ciphertext length, a 12-byte random nonce, and the AES-GCM
     ciphertext with its tag. The associated data is the 8-byte big-endian record index followed
     by one byte, `1` for the final record and `0` otherwise.

  The reader:
  - checks the magic;
  - decrypts the records in order, checking each index;
  - requires exactly one final record, the last one;
  - refuses trailing bytes, a missing final record, reordering and a truncated record.

  It keeps the existing key handling. The previous format (`nonce + ciphertext`) is not read;
  slots live minutes, so none survives an upgrade that matters.
- **Order of operations.** `_finish_fill`'s claim-before-write order stays. Records are written to a
  temp file next to the slot file and `os.replace`d at the end. On `UploadTooLargeError` or any
  error, the temp file is deleted and the claim released.
- `fill` therefore never buffers the whole upload. `peek` and `claim` still return the whole
  plaintext, as today; that is bounded by `max_bytes`.
- **Hold ceiling.** `peek(..., hold_until=None, hold_ceiling_seconds: float | None = None)`. When
  both are given, the stored hold is `min(hold_until, slot.created_at + hold_ceiling_seconds)`.
  `local_files` passes `hold_ceiling_seconds=2 * UPLOAD_HOLD_SECONDS`.
- **Tool description.** `privacyfence_create_upload_slot`'s description (`web/mcp_tools.py`)
  replaces "claimable only by your next tool call in this conversation" with "claimable by one tool
  call of yours within its lifetime".

### E5. Outputs paths and SDK publish (F35, F36, F37)

- **Junctions.** `list_outputs` and `_canonical_file` skip, or refuse, an entry whose
  `st_file_attributes` (when present) has `stat.FILE_ATTRIBUTE_REPARSE_POINT`. The walk keeps a set
  of visited `(st_dev, st_ino)` for directories and skips a repeat.
- **Exact names.** `_canonical_file` compares each segment with the names from
  `os.scandir(parent)` exactly (`==`, no case folding, no normalisation). No match gives "No such
  output file". `test_outputs.py:247-256` drops its `else` branch and requires refusal on every
  platform. The case variant is created only where the filesystem allows it; a case-insensitive
  filesystem creates the file once and asks for the other spelling.
- **Atomic publish.** `OutputsClient.publish`:
  1. `tempfile.mkstemp(dir=target.parent, prefix=".pf-", suffix=".tmp")`;
  2. write and `fsync`;
  3. `os.link(tmp, target)`, where `FileExistsError` becomes the existing "already exists" error;
  4. finally `os.unlink(tmp)`.

  When `os.link` fails with any `OSError` other than `FileExistsError` (Windows filesystems, FUSE
  or SMB mounts without hard links), fall back to
  `os.open(target, O_CREAT | O_EXCL | O_WRONLY | O_BINARY)`. `FileExistsError` there is the same
  "already exists" error. Copy the temp file's bytes into it, `fsync`, then unlink the temp. This
  works on every filesystem and never overwrites.
- `plugin-sdk/.../testing/_outputs.py` (`:41-60`) gets the same junction skip and visited set as
  `list_outputs`, so the test host does not diverge.

### E6. Audit, logs, RPC (F38, F39, F40, F46)


- **Approval audit rows.** `_audit_approval(plugin, kind, status, *, approval_id="", subject_id="",
  digest="")` writes the summary `"{kind}; {status}; id={approval_id}; subject={json.dumps(clean_line(subject_id)[:200])}; {digest}"`,
  leaving out empty parts. It is called with these on request, approve, deny and revoke.
  `ApprovalService`'s `audit` callback signature gains the keywords. `_audit_confirm` gains
  `approval_id`.
- **Log hygiene.**
  - `connector.py:459` logs `exc.code if isinstance(exc, RpcError) else type(exc).__name__`.
  - `BlockError` messages name the block index and field name only when the name matches
    `BLOCK_KEY_RE`; otherwise "a field".
  - `connector.py:373` logs `exc.code`.
- **RPC.** `rpc._handle_line` and the SDK's `_rpc` treat `RecursionError` like `ValueError`
  (`parse_error`, counted).
- The `rpc.py` module docstring gains one sentence for F46: responses to unknown ids and
  notifications without a handler count as valid lines, because they cost the plugin its own line
  budget only.

### E7. Example plugin and SDK release (F41, F42)

- **`examples/plugins/today`** (tests: `tests/unit/examples/test_today_plugin.py`):
  - Check each argument's type at the start of every tool and return a clear error.
  - `refresh` moves to the `review` gate. `max_gate_floor` stays `auto`, because `status`
    (`today_plugin.py:225`) is an `auto` tool.
  - Remove the `crash` tool, its flag reading and `build-flags.json`.
  - `_write_json` writes a temp file and `os.replace`s it.
  - `README.md` drops what it says about the crash tool.
- **`scripts/build_example_plugin.py`** drops `FLAGS_FILENAME` (`:26`), the flags file it writes
  (`:61`) and `--with-crash-tool` (`:68`). `tests/unit/test_build_example_plugin.py:74-105` drops its
  flags assertions and asserts no `build-flags.json` is produced.
- **`.github/workflows/publish-pypi.yml`, `build-sdk` job.** After the build step (which writes to
  `plugin-sdk/dist/`), and inside the same `refs/tags/*` guard the file uses at `:209`:
  1. `VERSION=$(cd plugin-sdk && python3 -m setuptools_scm)`; fail if it is `0.0.0`.
  2. `python3 scripts/r2_release.py check-tag --tag "$GITHUB_REF_NAME" --version "$VERSION"`.
  3. `python3 -m venv /tmp/sdk-smoke && /tmp/sdk-smoke/bin/pip install plugin-sdk/dist/*.whl`.
  4. `/tmp/sdk-smoke/bin/python -c "import importlib.metadata as m, importlib.resources as r, privacyfence_plugin_sdk.testing; assert m.version('privacyfence-plugin-sdk') == '$VERSION'; assert any(r.files('privacyfence_plugin_sdk.testing').joinpath('samples').iterdir())"`.

  Check `r2_release.py check-tag`'s actual arguments with `--help`, and use them in the build job's
  own `check-tag` shape.
- **`plugin-sdk/pyproject.toml`:**
  - `Issues` becomes `https://github.com/privacyfence/privacyfence/issues`.
  - Build-system requirements get upper bounds of the next major (`setuptools>=77,<81`,
    `setuptools-scm>=8,<10`).
  - This is a judgement call: exact pins with hashes need a lock this repository does not keep for
    build-system requirements.
- **New test** `tests/unit/plugin_sdk/test_packaging.py::test_sdk_imports_nothing_from_privacyfence`.
  It greps every SDK source file for `import privacyfence` or `from privacyfence` and fails on a
  match (the `privacyfence_plugin_sdk` package name excluded).

### E8. Test quality (F43, F44)

**Timing-dependent tests.** Replace each sleep-then-assert-absence with a positive marker:

- `test_rpc.py:337-346` and `:637-645`: send a follow-up request and await its answer; the earlier
  line has then been handled.
- `test_supervisor.py:453` and `test_host.py:1950`: poll for the condition the sleep stood in for,
  with `for _ in range(500): if <cond>: break; await asyncio.sleep(0.01)` and then `assert <cond>`.
  The condition is that the peer's writer transport has `get_write_buffer_size() > 0`, i.e. the
  pipe is full; use whichever object those tests already hold.
- `test_plugin_pages_browser.py:295-306`: register `context.on("page")` before the click, then:
  1. after the click, evaluate on the page a script that resolves on the next animation frame,
     twice;
  2. assert no page event arrived, and that a `securitypolicyviolation` or console message about
     the blocked popup was recorded, whichever the existing test page already reports.

**New tests:**

- **Approval-frame cookie probe** (`test_plugin_approval_frame_browser.py`). The framed plugin page
  serves this HTML:

  ```html
  <!doctype html><meta charset="utf-8"><p id="out">pending</p><script>
  (async () => {
    const r = [];
    try { await fetch('/plugins/echo/probe-cors', {credentials: 'include'}); r.push('cors: ok'); }
    catch (e) { r.push('cors: failed'); }
    try { const x = await fetch('/api/settings/state', {mode: 'no-cors', credentials: 'include'}); r.push('nocors: ' + x.type); }
    catch (e) { r.push('nocors: failed'); }
    await new Promise(done => { const i = new Image(); i.onload = i.onerror = done; i.src = '/plugins/echo/probe-img'; });
    document.getElementById('out').textContent = r.join('; ');
  })();
  </script>
  ```

  Assert that the plugin host saw only the framed path, never `/probe-cors` or `/probe-img`, and
  that the page reports `cors: failed`.
- **Double-encoded path** (`test_routes_plugins.py`): `%252e%252e` reaches the plugin as
  `/%2e%2e/x`.
- **Outputs on POSIX** (skipped on Windows):
  - a FIFO made with `os.mkfifo` is not listed and its read is refused without blocking;
  - a symlink to `/dev/null` is refused;
  - a hard link to a file outside the folder is published as its own content. That is the behaviour
    `docs/plugins.md` states after p10 (E10). The test pins it.
- **Concurrent slot use** (`test_plugin_files.py`): two concurrent calls on one `upload:` slot under
  an auto-accept rule run execute exactly once.

The periodic sweep test is in p3, not here.

### E9. Strict mypy (F45)

Promote `privacyfence.plugins.rpc`, `privacyfence.plugins.protocol`, `privacyfence.plugins.constants`,
`privacyfence.plugins.cursors` and `privacyfence.plugins.blocks` to the strict list. Find out how
`scripts/mypy_strict_modules.py` discovers modules (it reads `pyproject.toml` mypy overrides with
strictness flags) and add an override block in the same shape. Fix the type errors without
`# type: ignore`, unless the error is in a third-party stub, in which case use one with the error
code. `rpc.py` and `constants.py` are always promoted. If the other three together need more than
60 fixes, promote only those that are clean after fixing, and list the rest in the commit message.

### E10. Docs (F47 and this plan's reference changes)

- `docs/plugins.md`:
  - a note that a page showing third-party content must escape it, because navigation is not
    blocked by the sandbox;
  - in the outputs section: a hard link inside the output folder is published as the file it
    points to, like any regular file.
- `docs/security-and-compliance.md`:
  - spool retention (periodic sweep, dropped on stop, disable, purge and uninstall, 4 exports per
    plugin);
  - upload slot caps and the hold ceiling;
  - the source-call deadline;
  - expired sessions on plugin routes.
- `docs/plugin-protocol.md`: only the `timeout` answer of `source.call` (now from the daemon too,
  `retryable: true`), which is existing protocol behaviour.

### Rejected alternatives

- **Raising `MAX_IN_FLIGHT` handling instead of an executor.** Rejected: the pool starvation
  affects every connector, not only plugins.
- **Hashing the Sheets bound parameters into the cursor.** Rejected: cursors are unsigned by design
  (ADR 0128), so the binding has to be held server-side.
- **Exact build-system pins.** Rejected for now (see E7); the CI smoke test catches a broken build.

## ADRs

- **0148** (or the next free number at p10): Upload slots are capped per principal, streamed to
  disk, and their hold has a ceiling. Amends ADR 0102.
- **No ADR** for:
  - the source executor (E2): it is an internal implementation detail, and changing it back is a
    local edit;
  - the server-side Sheets binding (E3): it applies ADR 0128's existing decision that cursors are
    unsigned, and decides nothing new.

## Manual steps

The step-by-step page is `docs/plugin-framework-hardening-3-plan-manual-steps.html`, published at
the `manual_steps_artifact` URL.

- **Before:**
  - `mb1-pr874-merged`: merge PR #874, or start with it open.
  - `mb2-plan-a-first`: Plan A's PR is merged (recommended), or you accept resolving conflicts at
    the end.
- **After:** `ma1-plugin-page-session`: in your browser, a plugin page needs signing in again after
  the idle timeout, and browsing plugin pages keeps the session alive.

## Risks and open questions

- **p1:** if plugin routes start returning 404 for a fresh signed-in session in
  `test_plugin_pages_browser.py`, the guard is wrong: stop.
- **p2:** `drive_client.py` changes need `connector-live-check.yml`. It is dispatchable, and p10
  dispatches it.
- **p4:** the streamed format is security-sensitive. If the existing key handling cannot give a
  per-file key (for example a single nonce space shared across files), stop and report it.
- **p9:** the promoted modules may need many annotations; E9 says what to do over 60 errors.
- **Plan A overlap:** see "The base".

## Implementation manifest

Every code phase's brief ends with the same three rules:

- no `CHANGELOG.md` line (p10 writes them);
- no plan item or finding ids in code, comments or test names;
- run `ruff check .` and the phase's tests before finishing.

Phases that run at the same time share no path in `touches`.

```yaml
plan_slug: plugin-framework-hardening-3
feature_branch: feature/plugin-framework-hardening-3
tracking_issue: 846
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/7NVHNxApy9cxRXBWrCWXfB
manual_steps_source: docs/plugin-framework-hardening-3-plan-manual-steps.html
manual_before:
  - id: mb1-pr874-merged
    title: Merge PR 874 (plugin files and page links, protocol 1.3) into main, or accept that this PR carries its diff
    why: This plan's branch is cut from feature/plugin-files-and-page-links because the code it fixes exists only there. p0 merges origin/main; with 874 open, this plan's PR also shows 874's changes until 874 merges.
    done_when: "PR 874 shows Merged (preferred), or you have decided to start with it open. Either way `git fetch origin && git ls-tree origin/feature/plugin-files-and-page-links docs/ | grep -c plugin-files-and-page-links-plan` prints 0 (true at head 392e3ebc), and on main merged with that branch `ls docs/adr | cut -c1-4 | sort | uniq -d` prints nothing."
  - id: mb2-plan-a-first
    title: Run Plan A (plugin-protocol-hardening-2) first and merge its PR, or accept resolving conflicts at the end
    why: Both plans edit source_ops.py, spool.py, outputs.py, host.py, connector.py and blocks.py. p0 merges origin/main, so with Plan A merged the phases build on it and conflict with nothing.
    done_when: "Plan A's PR shows Merged and `git fetch origin && git ls-tree origin/main docs/adr/ | grep -c '0147-'` prints 1; or you have decided to run this plan before Plan A merges."
manual_after:
  - id: ma1-plugin-page-session
    title: Check that a plugin page asks you to sign in after the idle timeout, and that browsing plugin pages keeps the session alive
    why: The unit tests use short timeouts and a test client; this checks the real 30-minute idle timer with the companion-minted session in your browser.
verify_after_merge:
  - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/web/test_routes_plugins.py tests/unit/web/test_routes_plugin_browser.py tests/unit/web/test_server.py tests/unit/test_upload_staging.py tests/unit/test_local_files.py tests/unit/test_code_no_history.py -q
  - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_plugin_paging.py tests/integration/test_plugin_files.py tests/integration/test_plugin_outputs.py -q
final_checks:
  - docs/plugin-framework-hardening-3-plan.md and docs/plugin-framework-hardening-3-plan-manual-steps.html are deleted and nothing links to them (grep -rn "plugin-framework-hardening-3-plan" docs scripts src plugin-sdk README.md prints nothing)
  - The upload-slot ADR exists with Status Accepted and its Amends line, is in docs/adr/README.md's index, and ADR 0102 has the "Amended by" status line and index cell
  - CHANGELOG.md has the [Unreleased] lines from p10 and no new version heading
  - The full /dod passes, including python3 scripts/check_coverage_floor.py coverage.json (left to CI if the container cannot finish the coverage run; say so in the PR) and python3 -m pytest tests/integration -v with PRIVACYFENCE_TEST_CHROMIUM set, no plugin browser test SKIPPED, and test_mcp_daemon_contract.py and test_shim_mcp_contract.py passing (p1 changes web/server.py)
  - connector-live-check.yml was dispatched against the feature branch (p2 changes drive_client.py) and the run is linked in the PR
  - The PR's platform-windows and platform-macos jobs are green (junction and exact-name checks run there)
phases:
  - id: p0-sync
    title: Merge main and list the plan document so the docs tests pass
    depends_on: []
    complexity: S
    touches:
      - docs/README.md
      - scripts/build_site.py
      - docs/plugin-framework-hardening-3-plan.md
    brief: |
      1. `git fetch origin main && git merge --no-ff origin/main` into the phase branch. Keep both sides on conflicts; if a conflict is in src/privacyfence/plugins/ or plugin-sdk/ and is not a trivial adjacency, stop with status=blocked and name the files.
      2. Run `python3 -m pytest tests/unit -q -k "docs or website or build_site"`. If a test fails because docs/plugin-framework-hardening-3-plan.md is not listed, add it to docs/README.md's contributor list and to CONTRIBUTOR_DOCS in scripts/build_site.py, in the shape `git log -S"plugin-files-and-page-links-plan" -- docs/README.md` shows PR 874 used. Do only what the failing tests ask.
      3. If Plan A has merged (origin/main has docs/adr/0147-*), note in the commit message that the later phases build on it; nothing else changes.
      4. Run `python3 -m pytest tests/unit -q` and `ruff check .`.
    acceptance:
      - python3 -m pytest tests/unit -q passes
  - id: p1-web-session
    title: Expired sessions do not open plugin routes; the bootstrap redirect stays on the site
    depends_on: [p0-sync]
    complexity: S
    touches:
      - src/privacyfence/web/server.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_routes_plugins.py
      - tests/unit/web/test_routes_plugin_browser.py
    brief: |
      Implement Design E1 exactly.
      1. server.py build_app: both plugin route sets get is_owner_session = _session_authenticated(r, sessions) and _is_human_session(r, sessions) (E1). Do not change session_auth.py.
      2. server.py _BootstrapMiddleware: redirect target "/" + request.url.path.lstrip("/").
      3. Tests: test_routes_plugins.py and test_routes_plugin_browser.py (an expired session, made with small idle/absolute timeouts as the existing session tests do, gets 404 without a plugin call; a live session's idle deadline moves after a plugin page view); test_server.py: drive _BootstrapMiddleware with a raw ASGI scope (type http, path "//evil.example/x", query_string b"bootstrap=bad") and assert 303 with location "/evil.example/x"; a second case with path "/\\evil.example/x" asserts the location does not start with "//" or "/\\" (it is quoted). Do not use TestClient for these: httpx parses "//host" as a host.
      4. Because web/server.py changed: run `python3 -m pytest tests/integration -v` with PRIVACYFENCE_TEST_CHROMIUM set; test_mcp_daemon_contract.py and test_shim_mcp_contract.py must pass.
      Stop with status=blocked if a fresh signed-in session gets 404 on plugin pages in the browser tests.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/web -q passes
      - python3 -m pytest tests/integration -q passes with nothing in tests/integration/test_plugin_*browser*.py skipped
  - id: p2-source-calls
    title: Own executor, per-plugin cap and deadline for source calls; audit every outcome with quoted targets; Drive file id validation
    depends_on: [p0-sync]
    complexity: M
    touches:
      - src/privacyfence/plugins/source_ops.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/drive_client.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_source.py
      - tests/conftest.py
      - tests/unit/plugins/test_source_ops.py
      - tests/unit/plugins/test_host.py
      - tests/unit/test_drive_client.py
      - tests/unit/plugin_sdk/test_testhost_surfaces.py
    brief: |
      Implement Design E2 exactly.
      1. source_ops.py: SOURCE_WORKERS = 4 lazily created executor run through contextvars.copy_context().run, shutdown_executor() resetting the global to None, PER_PLUGIN_SOURCE_CALLS = 2 semaphores, SOURCE_CALL_DEADLINE_SECONDS = 110.0 around the whole call (semaphore wait included) and its RpcError, the audit in a finally with outcome (cancelled re-raised), the truncate-then-json-quote of the named values, the operation name for a refused operation in SOURCE_OPERATIONS, the Drive file_id pattern and message — all exactly as E2.
      2. host.py stop_all calls source_ops.shutdown_executor(). tests/conftest.py: reset the executor and the semaphore dict (follow the file's existing reset pattern for module state).
      3. drive_client.py: quote file_id in download_range and _stream_full_content URLs. testing/_source.py: same file_id pattern.
      4. Tests: test_source_ops.py (17 slow calls from one plugin never run more than 2 at once, and two plugins' calls run side by side: count concurrent entries in a fake client; a blocked client answers timeout with source_ops.SOURCE_CALL_DEADLINE_SECONDS patched to 0.2; a call waiting on the semaphore behind two blocked calls also answers timeout; the principal contextvar is visible inside the adapter; a cancelled call writes an audit entry with cancelled; a range of 'A1; bytes=0' appears JSON-quoted in the summary; a refused jira.search is audited as jira.search; a file_id with '/' or '?' is invalid_params); test_drive_client.py (the URL carries the quoted id); test_testhost_surfaces.py (test host refuses the same file_id).
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_source_ops.py tests/unit/plugins/test_host.py tests/unit/test_drive_client.py tests/unit/plugin_sdk -q passes
      - python3 -m pytest tests/integration/test_plugin_paging.py tests/integration/test_plugin_framework.py -q passes
  - id: p3-spool-lifecycle
    title: Periodic sweep, drop a plugin's spool on stop/disable/purge/uninstall, export cap, snapshot binding, surrogate scrubbing
    depends_on: [p2-source-calls]
    complexity: M
    touches:
      - src/privacyfence/plugins/spool.py
      - src/privacyfence/plugins/source_ops.py
      - src/privacyfence/plugins/host.py
      - tests/unit/plugins/test_spool.py
      - tests/unit/plugins/test_source_ops.py
      - tests/unit/plugins/test_host.py
    brief: |
      Implement Design E3 exactly.
      1. host.py start: eager spool creation with _get_spool() (its constructor already empties the directory); sweep task every SPOOL_SWEEP_SECONDS = 60 (module constant in host.py), cancelled in stop_all; drop_plugin calls in _stop, _on_state (disabled or backoff), purge, _uninstall.
      2. spool.py: drop_plugin(name); _EXPORTS_PER_PLUGIN = 4 LRU eviction; _RowsEntry.bound and SnapshotMismatch(ValueError) raised by rows_page on a mismatch.
      3. source_ops.py: pass bound into put_rows/rows_page; _run_sheets maps SnapshotMismatch to _bad_cursor() and keeps KeyError -> _cursor_expired(); _scrub_surrogates applied to the client's return value in each _run_* function on the line after the client call (E3).
      4. Tests: test_spool.py (drop_plugin removes files and entries of that plugin only; the 5th export evicts the least recently used; a mismatched bound raises SnapshotMismatch, an unknown snapshot KeyError); test_host.py (disable, purge, uninstall and a crash each drop the plugin's spool files; the sweep task removes an idle file with host.SPOOL_SWEEP_SECONDS and spool.DRIVE_SPOOL_IDLE_SECONDS (the name spool.py imported) monkeypatched small, no manual sweep() call; the spool directory is emptied at start); test_source_ops.py (sheet A's snapshot under sheet B's bound parameters -> invalid_params; a Confluence page with '\ud800' in its title pages normally and the title holds U+FFFD).
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins -q passes
      - python3 -m pytest tests/integration/test_plugin_paging.py tests/integration/test_plugin_framework.py -q passes
  - id: p4-upload-slots
    title: Per-principal slot and byte caps, streamed encrypted fill, hold ceiling, accurate tool description
    depends_on: [p0-sync]
    complexity: M
    touches:
      - src/privacyfence/upload_staging.py
      - src/privacyfence/local_files.py
      - src/privacyfence/web/mcp_tools.py
      - tests/unit/test_upload_staging.py
      - tests/unit/test_local_files.py
      - tests/unit/web/test_mcp_tools.py
      - tests/integration/test_plugin_files.py
    brief: |
      Implement Design E4 exactly.
      1. upload_staging.py: the two caps and UploadSlotLimitError(ValueError); one private streaming writer used by fill, afill and afill_capability, with the PFU2 record format, AAD, temp file and os.replace exactly as E4, keeping _finish_fill's claim-before-write order and releasing the claim on failure; the matching reader used by peek and claim; peek's hold_ceiling_seconds keyword.
      2. local_files.py: catch UploadSlotLimitError at both create_slot call sites (:389, :487) and raise LocalFileAccessError with the E4 message; pass hold_ceiling_seconds=2 * UPLOAD_HOLD_SECONDS to peek.
      3. web/mcp_tools.py: the description sentence from E4; update any test that pins the old text.
      4. Tests: test_upload_staging.py (33rd slot refused with UploadSlotLimitError; byte cap refused; a 20 MB afill never holds more than one 64 KiB record of plaintext: wrap the writer's encrypt call and record the largest plaintext it saw; round trip; a file with a record cut short, with the final record removed, with two records swapped, and with trailing bytes are each refused; an upload over max_bytes leaves no temp file and the slot unclaimed); test_local_files.py (repeated peeks never push the hold past created_at + 2 * UPLOAD_HOLD_SECONDS); test_plugin_files.py still passes unchanged.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/test_upload_staging.py tests/unit/test_local_files.py tests/unit/web/test_mcp_tools.py -q passes
      - python3 -m pytest tests/integration/test_plugin_files.py -q passes
  - id: p5-outputs-paths-and-publish
    title: Skip junctions and repeats in outputs, exact-name canonical check, atomic SDK publish
    depends_on: [p0-sync]
    complexity: S
    touches:
      - src/privacyfence/plugins/outputs.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_outputs.py
      - tests/unit/plugins/test_outputs.py
      - tests/unit/plugin_sdk/test_plugin.py
    brief: |
      Implement Design E5 exactly.
      1. outputs.py: reparse-point skip, visited (st_dev, st_ino) set, exact-name segment comparison in _canonical_file.
      2. SDK plugin.py OutputsClient.publish: mkstemp + fsync + os.link + unlink, with the O_CREAT|O_EXCL copy fallback from E5 for any other OSError, same error on an existing target. testing/_outputs.py: the junction skip and visited set from E5.
      3. Tests: test_outputs.py (a directory loop via a bind of the same inode is not possible on Linux without root, so test the visited-set by monkeypatching os.scandir to return a repeated directory; reparse-point flag via a fake stat result; the case-variant test requires refusal, see E5 for case-insensitive filesystems); SDK test_plugin.py (two threads publishing the same relpath: exactly one succeeds, the other gets the existing-file error, and the file content is one of the two complete payloads; no .pf-*.tmp left behind; with os.link patched to raise PermissionError the fallback publishes and still refuses an existing target).
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_outputs.py tests/unit/plugin_sdk/test_plugin.py -q passes
  - id: p6-audit-logs-rpc
    title: Approval audit rows with id, subject and digest; no plugin-sent detail in logs; RecursionError is a parse error
    depends_on: [p3-spool-lifecycle]
    complexity: M
    touches:
      - src/privacyfence/plugins/host.py
      - src/privacyfence/plugins/approvals.py
      - src/privacyfence/plugins/confirm.py
      - src/privacyfence/plugins/connector.py
      - src/privacyfence/plugins/blocks.py
      - src/privacyfence/plugins/rpc.py
      - plugin-sdk/src/privacyfence_plugin_sdk/_rpc.py
      - tests/unit/plugins/test_host.py
      - tests/unit/plugins/test_approvals.py
      - tests/unit/plugins/test_confirm.py
      - tests/unit/plugins/test_connector.py
      - tests/unit/plugins/test_blocks.py
      - tests/unit/plugins/test_rpc.py
      - tests/unit/plugin_sdk/test_rpc.py
    brief: |
      Implement Design E6 exactly.
      1. host.py _audit_approval/_audit_confirm keywords and summary format; plugins/approvals.py and plugins/confirm.py pass approval_id, subject_id and digest through their audit callbacks on request, decision and revoke; host.revoke_approval passes them.
      2. connector.py: log exc.code or the type name at the two lines in E6; blocks.py: BlockError messages per E6.
      3. rpc.py and SDK _rpc.py: RecursionError handled like ValueError; the F46 docstring sentence in rpc.py (in words, no id).
      4. Tests: test_host.py/test_approvals.py (a requested, approved and revoked approval each write a row containing id=, the JSON-quoted subject and the digest); test_connector.py (caplog: a failed read-only execute whose plugin error detail is "SECRET-CONTENT" logs no "SECRET-CONTENT"); test_blocks.py (an undeclared row key "evil\nkey" is not echoed); test_rpc.py daemon and SDK (a 200000-deep '[' line answers parse_error and the peer stays open until the third invalid line).
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk -q passes
  - id: p7-example-and-release
    title: Safer example plugin; SDK version assertion, wheel smoke test and import isolation test
    depends_on: [p0-sync]
    complexity: S
    touches:
      - examples/plugins/today/**
      - scripts/build_example_plugin.py
      - tests/unit/test_build_example_plugin.py
      - tests/unit/examples/test_today_plugin.py
      - .github/workflows/publish-pypi.yml
      - plugin-sdk/pyproject.toml
      - tests/unit/plugin_sdk/test_packaging.py
    brief: |
      Implement Design E7 exactly.
      1. examples/plugins/today: argument type checks, refresh on the review gate (max_gate_floor stays auto: status is an auto tool), remove the crash tool, its flag reading and build-flags.json, atomic _write_json, README.md updated; tests in tests/unit/examples/test_today_plugin.py.
      2. scripts/build_example_plugin.py: drop FLAGS_FILENAME, the flags file and --with-crash-tool; tests/unit/test_build_example_plugin.py asserts no build-flags.json is produced.
      3. publish-pypi.yml build-sdk job: the four E7 steps inside the refs/tags/* guard, with the wheel path plugin-sdk/dist/*.whl and check-tag's real arguments (python3 scripts/r2_release.py check-tag --help).
      4. plugin-sdk/pyproject.toml: Issues URL and build-system upper bounds.
      5. tests/unit/plugin_sdk/test_packaging.py: test_sdk_imports_nothing_from_privacyfence.
      6. python3 -m build plugin-sdk must succeed; delete plugin-sdk/dist, build/ and *.egg-info afterwards.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk tests/unit/examples tests/unit/test_build_example_plugin.py -q passes
      - python3 -m build plugin-sdk exits 0
      - grep -rn "build-flags" examples/ prints nothing
  - id: p8-test-quality
    title: Replace sleep-then-assert-absence tests; add the missing negative and lifecycle tests
    depends_on: [p1-web-session, p4-upload-slots, p5-outputs-paths-and-publish, p6-audit-logs-rpc]
    complexity: M
    touches:
      - tests/unit/plugins/test_rpc.py
      - tests/unit/plugins/test_supervisor.py
      - tests/unit/plugins/test_host.py
      - tests/unit/plugins/test_outputs.py
      - tests/unit/web/test_routes_plugins.py
      - tests/fixtures/plugins/echo/echo_plugin.py
      - tests/integration/test_plugin_pages_browser.py
      - tests/integration/test_plugin_approval_frame_browser.py
      - tests/integration/test_plugin_files.py
    brief: |
      Implement Design E8. Tests only; no source change. If a new test fails because of a real bug, stop with status=blocked and describe it instead of changing source.
      1. Rewrite the four timing-dependent spots named in E8 with positive markers. If the frame probe needs the echo plugin to serve the probe page, add a page path for it in tests/fixtures/plugins/echo/echo_plugin.py following its existing page pattern.
      2. Add the new tests listed in E8, using the probe HTML given there verbatim: frame cookie probe, double-encoded route, FIFO/device/hard-link outputs (POSIX only, skipped on Windows), concurrent upload slot under an auto-accept rule. The periodic sweep test is p3's; do not add another.
      3. Run each changed file 5 times in a row (`for i in 1 2 3 4 5; do python3 -m pytest <file> -q || break; done`) to check they are stable.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/web/test_routes_plugins.py -q passes
      - PRIVACYFENCE_TEST_CHROMIUM set, python3 -m pytest tests/integration/test_plugin_pages_browser.py tests/integration/test_plugin_approval_frame_browser.py tests/integration/test_plugin_files.py -q passes with nothing skipped
      - grep -n "wait_for_timeout(1000)" tests/integration/test_plugin_pages_browser.py prints nothing
  - id: p9-mypy-strict
    title: Promote the plugin RPC, protocol, constants, cursors and blocks modules to strict mypy
    depends_on: [p8-test-quality]
    complexity: M
    touches:
      - pyproject.toml
      - src/privacyfence/plugins/rpc.py
      - src/privacyfence/plugins/protocol.py
      - src/privacyfence/plugins/constants.py
      - src/privacyfence/plugins/cursors.py
      - src/privacyfence/plugins/blocks.py
    brief: |
      Implement Design E9 exactly. Type-only changes: no behaviour change; the unit tests must pass unchanged.
      1. Read scripts/mypy_strict_modules.py and the existing strict override blocks in pyproject.toml; add the five modules in the same shape.
      2. Run python3 scripts/mypy_strict_modules.py and fix the errors (no # type: ignore except on third-party stubs, with the error code).
      3. rpc.py and constants.py are always promoted. If protocol.py, cursors.py and blocks.py together need more than 60 fixes, promote only those clean after fixing, and list the rest in the commit message.
      pyproject.toml is changed but no dependency changes, so no lock regeneration.
    acceptance:
      - python3 scripts/mypy_strict_modules.py exits 0
      - python3 scripts/mypy_strict_modules.py --list | grep -c "plugins/rpc.py\|plugins/constants.py" prints 2
      - python3 -m pytest tests/unit/plugins -q passes
  - id: p10-docs-adr-retire
    title: Reference docs, the upload-slot ADR, changelog, retire the plan
    depends_on: [p5-outputs-paths-and-publish, p7-example-and-release, p9-mypy-strict]
    complexity: S
    touches:
      - docs/plugins.md
      - docs/security-and-compliance.md
      - docs/plugin-protocol.md
      - docs/adr/**
      - docs/README.md
      - scripts/build_site.py
      - CHANGELOG.md
      - docs/plugin-framework-hardening-3-plan.md
      - docs/plugin-framework-hardening-3-plan-manual-steps.html
    brief: |
      1. Docs per Design E10 (including the hard-link sentence in docs/plugins.md's outputs section), present tense, no history (tests/unit/test_docs_no_history.py).
      2. ADR: next free number (expected 0148; if Plan A has not merged and 0142-0147 are free on this branch, still take 0148 so the two plans never collide), "Upload slots are capped per principal, streamed to disk, and their hold has a ceiling", Accepted, Amends ADR 0102; "Amended by" status line and index cell on 0102; index row.
      3. CHANGELOG.md under ## [Unreleased]: Security: expired sessions no longer open plugin pages; the bootstrap link stays on the site. Changed: upload slots are capped per principal; plugin source reads run on their own pool with a deadline and every read is audited; spooled plugin data is deleted when the plugin stops. One line each, in the section's style.
      4. Delete docs/plugin-framework-hardening-3-plan.md and docs/plugin-framework-hardening-3-plan-manual-steps.html; remove what p0 added to docs/README.md and scripts/build_site.py.
      5. Dispatch connector-live-check.yml (no inputs) against feature/plugin-framework-hardening-3 with the GitHub MCP actions_run_trigger tool, wait for it, and put the run URL and result in the commit message so the PR can link it (p2 changed drive_client.py). A failure that names a connector this plan did not touch is reported, not fixed.
      6. Run the full tests/unit suite, ruff, bandit and mypy strict modules.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - grep -rn "plugin-framework-hardening-3-plan" docs scripts src plugin-sdk README.md prints nothing
      - the commit message holds the connector-live-check.yml run URL
```

## Appendix: findings of the 2026-10-09 plugin framework review

A security and test review of the plugin framework, run on `feature/plugin-files-and-page-links` at
392e3ebc (PR #874, protocol 1.3.0). It covered the daemon side, the SDK and its test host, the
example and fixture plugins, the normative docs and ADRs 0102 and 0121 to 0141, and the tests.
`ruff check .`, `bandit -c pyproject.toml -r src` and `python3 scripts/mypy_strict_modules.py`
passed. `python3 -m pytest tests/unit -q` gave 15437 passed and 36 skipped; none of the skips is
in a plugin test. The plugin integration files and the SDK conformance test gave 132 passed.

The review produced 47 findings: 2 high, 15 medium, 27 low and 3 info. 24 are class `protocol`
(Plan A, `plan/plugin-protocol-hardening-2`) and 23 are class `other` (Plan B,
`plan/plugin-framework-hardening-3`). Both plans carry the full list, so each plan stands on its own.
The **Plan** column says which plan fixes each finding.

"Repro" names a script that was run in the review container. The scripts are not in the
repository. Each entry below says what the script did and what it printed, and the phase that fixes
the finding writes the same case as a test. Line numbers are at 392e3ebc.

| Id | Sev | Class | Plan | Title |
|---|---|---|---|---|
| F1 | high | protocol | A | A plugin confirmation or approval card with `require_step_up: false` can be approved from an unattested session |
| F2 | high | protocol | A | A plugin can write the daemon's policy, plugin state, approvals, passkeys and audit log; the docs say it can only read |
| F3 | medium | protocol | A | Introspection runs unreviewed plugin code as the service account from a non-sensitive action |
| F4 | medium | protocol | A | `command[1:]`, shebang interpreters and files beside the executable are neither permission-checked nor hashed |
| F5 | medium | protocol | A | AI-client arguments reach `tool.prepare`/`tool.execute` without any check against the tool's schema |
| F6 | medium | protocol | A | A non-destructive write on the `review` gate is shown on the read card, with no Effect row |
| F7 | medium | protocol | A | The daemon's file block can be imitated (heading copy, zero-width, homoglyph, table rows) |
| F8 | medium | protocol | A | Block text keeps invisible format characters: hidden text reaches the AI and evades the PII scan |
| F9 | medium | protocol | A | `tools.changed` has no rate limit: about 540 audit lines a second and a tools-list-changed per message |
| F10 | medium | protocol | A | `sheets.get_values` has no size bound and holds the whole range in memory several times |
| F11 | medium | protocol | A | `plugin_outputs_read` re-hashes the whole file for every page; output files have no size or count limit |
| F12 | medium | protocol | A | The SDK test host diverges from the daemon in ways that hide plugin bugs |
| F13 | low | protocol | A | `args_digest` is Python `json.dumps`: not reproducible in other languages, and a lone surrogate raises |
| F14 | low | protocol | A | Daemon parameter-schema checks are looser than the docs; plugin-chosen parameter names reach the audit log |
| F15 | low | protocol | A | `validate_scope_types` is never called; `anything` is reserved in practice but not checked |
| F16 | low | protocol | A | The SDK and test host accept an empty `title`/`effect` that the daemon refuses |
| F17 | low | protocol | A | A write result keeps a plugin-chosen `approval_id` when it is not withheld |
| F18 | low | protocol | A | Doc, schema, `x-limits` and ADR 0126 disagree with the code in nine places |
| F19 | low | protocol | A | Jira and Calendar continuations re-fetch and skip by count: duplicates or gaps, or a non-retryable error |
| F20 | low | protocol | A | The documented source-call check order is not the code's |
| F21 | low | protocol | A | Output file names with control or bidi characters are published and shown raw |
| F22 | low | protocol | A | Plugin names may be Windows device names (`con`, `nul`, `com1`, ...) |
| F23 | low | protocol | A | A `display_name` can be "PrivacyFence" or "Gmail"; confirm and tool cards do not show the installed name |
| F24 | info | protocol | A | A file's name, size, type and SHA-256 reach the plugin at prepare, before the gate (undocumented) |
| F25 | medium | other | B | Source calls run on the shared default thread pool and have no daemon-side deadline |
| F26 | medium | other | B | Spool files outlive the documented 10 idle minutes; Drive exports have no per-plugin cap; purge skips the spool |
| F27 | medium | other | B | Upload slots: no per-principal cap, the PUT body is buffered in RAM, and every peek extends expiry |
| F28 | medium | other | B | A cancelled `source.call` writes no audit entry although the upstream read completes |
| F29 | medium | other | B | Expired sessions still open plugin pages and the page browser |
| F30 | low | other | B | Open redirect: `//host/path?bootstrap=x` answers `303 Location: //host/path` |
| F31 | low | other | B | A Sheets snapshot cursor is not bound to its spreadsheet, so later pages are audited under another name |
| F32 | low | other | B | A lone surrogate makes a correctly sized page fail with `payload_too_large` |
| F33 | low | other | B | Plugin-chosen text goes into audit summaries unquoted; a refused operation is logged as `unknown` |
| F34 | low | other | B | Drive `file_id` is put into URLs unquoted and unvalidated (safe today only through call order) |
| F35 | low | other | B | Windows: a directory junction in an output folder is walked by `list_outputs` (unverified) |
| F36 | low | other | B | macOS: case or normalisation variants of an output path count as canonical, and a test accepts it |
| F37 | low | other | B | SDK `OutputsClient.publish` is not atomic and uses a fixed temp name |
| F38 | low | other | B | Plugin approval and confirmation audit rows carry no approval id, subject or digest |
| F39 | low | other | B | Plugin-sent error detail and plugin-chosen keys are written to the daemon log |
| F40 | low | other | B | A deeply nested JSON line kills the RPC reader (RecursionError) instead of counting as a parse error |
| F41 | low | other | B | The example plugin teaches unsafe practice |
| F42 | low | other | B | SDK release: no version assertion, no wheel smoke test, unpinned build tools, wrong Issues URL |
| F43 | low | other | B | Timing-dependent tests that pass vacuously on a slow or Windows runner |
| F44 | low | other | B | Test gaps not covered by a fix elsewhere |
| F45 | low | other | B | No plugin module is under strict mypy |
| F46 | info | other | B | Unsolicited responses and unknown notifications reset the invalid-line streak |
| F47 | info | other | B | Navigation and DNS prefetch remain exfiltration channels from a plugin page (already documented) |

### F1 (high, protocol): an unattested session can approve a plugin card that opted out of step-up

- **Where:** `src/privacyfence/web/routes_approvals.py:688-697`; `approvals.py:634-665` (`register_confirm(sensitive=...)`); `plugins/confirm.py:117`; `plugins/approvals.py:301`.
- **Scenario:** a plugin calls `approval.request` or `confirm.request` with `require_step_up: false`, a documented option. The card is registered with `sensitive=False`. On the decide route, `confirm` is not in `_STEP_UP_RESULTS` and `sensitive_confirm` is False, so neither `human_session_guard` nor the passkey runs.
  1. An agent running as the logged-in user mints an unattested session over the control channel (a bare `MINT`).
  2. It learns the card id: a plugin write result carries `approval_id` (ADR 0137), and pending cards are listed to the session.
  3. It POSTs `result: confirm`.
  4. The approval is stored in `plugin-approvals.json` until it is revoked.

  The docs say `require_step_up` only means "the card needs a passkey" (`docs/plugin-protocol.md:581`), and that an unattested session can never approve (`docs/security-and-compliance.md:70-78`, ADR 0062).
- **Repro:** yes. `test_unattested_plugin_approval.py` printed `decide status from unattested session: 200 {'status': 'ok'}` and then `stored approval: ('153040…', None)`.
- **Fix:** separate "a human session is required" from "a passkey is required". Plugin confirmation and approval cards always need a human session; `require_step_up` controls only the passkey (Plan A p2).
- **Mitigation today:** do not pass `require_step_up=False` in any plugin. Both the SDK and the protocol default to `True`.

### F2 (high, protocol): a plugin can write what the service account can write

- **Where:** `docs/security-and-compliance.md:316-366`; ADR 0121; `plugins/state.py` (`plugins-state.json`, plain JSON); `privilege_separation.py` (the authority dir is owned by the service account); `supervisor.py:346-355` (spawn with no change of user).
- **Scenario:** a plugin runs as the service account, so it can:
  - rewrite `config/settings.yaml`, which is hot-reloaded: auto-accept rules for every connector, not only its own;
  - edit `plugins-state.json`: enable another installed plugin that the owner never enabled, add reviewed tool signatures, raise `max_gate_floor`;
  - edit `plugin-approvals.json`;
  - enroll a passkey;
  - rewrite the audit log, together with its HMAC key;
  - read other plugins' storage, outputs and spool files.

  The docs say only "can read what the service account can read". They also say "a plugin can get its own data to the AI only through a card or a rule you wrote" and that the spool is "a directory the plugin cannot see". The review-time controls (reviewed signatures, the gate floor, approval digests, the audit trail) therefore protect against a buggy plugin only, not a hostile one or a compromised dependency of one.
- **Repro:** partial, by code reading. The state store has no integrity protection, and the plugin process has the same uid as the files.
- **Fix (decided in Plan A, ADR 0145):**
  - State the write capability exactly, as an accepted residual risk.
  - Fix the claims that overstate what the daemon enforces.
  - Fix the writable areas a plugin may rely on as a protocol contract: `data_dir`, its principal `storage_dir`, `output_dir`, and a per-plugin temporary directory the daemon now sets in `TMPDIR`/`TEMP`/`TMP`. A later OS-level confinement can then enforce the contract without breaking plugins.
  - Confinement itself (a separate account, Landlock, `sandbox-exec`, AppContainer) is follow-up work.
- **Mitigation today:** keep `plugins.enabled: false` on any install where a plugin you would not trust with full control of PrivacyFence is installed. Enabling a plugin gives it that control.

### F3 (medium, protocol): introspection is classed as harmless but runs unreviewed code

- **Where:** `src/privacyfence/web/routes_settings.py:266-268` (`inspect_plugin` is in `_NON_SENSITIVE_ACTIONS`); `plugins/host.py:766-813`; ADR 0121 ("it can neither read data nor change state").
- **Scenario:** an administrator has installed a plugin, and the owner has deliberately not enabled it. Any authenticated web session, an unattested one included, can trigger `inspect_plugin`. That spawns the plugin as the service account. Refusing `source.call` and `confirm.request` limits only the protocol: the process can do everything in F2.
- **Repro:** partial (code path).
- **Fix:** make `inspect_plugin` sensitive, so it needs a human session and step-up like `enable_plugin`. Correct the claim in a new ADR (Plan A p4, p11).

### F4 (medium, protocol): only `command[0]` is checked and hashed

- **Where:** `plugins/manifest.py:110-113`, `:157-166` (`resolve_command`); `trust.py:94-169`; `host.py:798-812` (the review dict has no `command`).
- **Scenario:** `command: ["run", "/home/user/x.py"]` or `["run", "../../../../home/user/x.py"]` is accepted. So is a script whose shebang names a user-writable interpreter, and so is a world-writable `lib.py` beside the executable. Neither the admin-only check nor the hash covers any of these, so the user (and therefore the AI client) can change code that runs as the service account without disabling the plugin. The review dialog does not show `command`. Files beside the executable are a known gap (issue 860, `docs/plugins.md`).
- **Repro:** yes. `r2_command_args.py`: the manifest is accepted, `/home/user/evil.py checked? False`, `lib.py checked? False`.
- **Fix:**
  - Refuse an absolute, `~`, drive, UNC or `..` element in `command[1:]`.
  - Show `command` on the review dialog.
  - Permission-check every file and directory in the plugin folder, and refuse symlinks inside it.
  - Hash the whole folder (`files_sha256`).
  - On POSIX, permission-check a `#!` interpreter (Plan A p4 for the arguments and the dialog, p5 for the rest).

### F5 (medium, protocol): tool arguments are not validated

- **Where:** `plugins/connector.py:261-302` (only file parameters are checked; `plugin_args` is the client's raw dict); `web/routes_mcp.py:403-491`; `web/mcp_dispatch.py:197-243`; SDK `plugin.py:524-525`, `:865-879`.
- **Scenario:** for a tool declaring `{"day": {"type": "string"}}` with `required: ["day"]`, the AI sends `{}`, `{"day": {"$gt": ""}, "extra": [1, {"deep": true}]}` or `{"day": 12345}`. All three reach prepare and execute unchanged. A plugin that renders `str(args["to"])` on the card and iterates `args["to"]` in execute shows the human one thing and acts on another. In the shipped example, `today_add_note` with `event_id: 123` is stored, and from then on the Today page answers 500 on every load.
- **Repro:** yes. `args_unvalidated.py` printed `prepare args: {'undeclared': {'nested': [1, 2]}, 'x': ['a', 'b']}`. `r4_today_typed_args.py` printed `page status: 500`.
- **Fix:** validate in `PluginConnector.call` before prepare, with a fixed message and no card. Refuse:
  - unknown keys;
  - a missing required key;
  - a type mismatch (`bool` is not an integer, and an integer must be an `int`);
  - a non-finite number, or an integer beyond ±(2^53−1);
  - a string holding a lone surrogate, or longer than the declared `maxLength` (default 65,536).

  An integer sent as a whole float (`5.0`) is accepted as `5`, and file parameters are left to the
  file check. The SDK and the test host apply the same check (Plan A p3b).

### F6 (medium, protocol): a review-gated write is shown as a read

- **Where:** `plugins/tools.py:149-154`; `plugins/connector.py:402-413`; `gate.py:1080-1134` (the review branch calls `show_read_popup`); `card_builder.py:199-204`.
- **Scenario:** only a destructive tool is forced onto `popup`, so a non-destructive write may use `review`. Its card is the read card: no Effect row, disclosure wording, and a PII scan over metadata only. The human sees a "read" card for an action.
- **Repro:** yes. `test_review_write.py` printed `read popups: 1 write popups: 0` and then `executed: 1`.
- **Fix:** a non-read-only plugin tool always gets the write card (with its Effect row and the write PII scan) whatever its gate. `review` keeps meaning "a rule may accept it". No wire change (Plan A p7b).

### F7 (medium, protocol): the file-block look-alike refusal is bypassable

- **Where:** `plugins/files.py:151-184` (`refuse_reserved_labels`); `connector.py:342-348`, `:399`; `approval_window_html.py:330-336`, `:371-390`.
- **Scenario:** a plugin preview can still forge the daemon's block in four ways:
  1. a `heading` with the exact text "Read and checked by PrivacyFence from the file's bytes";
  2. labels with zero-width characters;
  3. homoglyph labels;
  4. a two-column `table` whose rows read File and SHA-256.

  The daemon's rows go through the same `to_card_blocks` and `_render_block` as the plugin's, so the HTML is identical. Tools without a file parameter, confirm cards and approval cards get no refusal at all.
- **Repro:** yes. `lookalike.py`: `heading copy: ACCEPTED`, `zero-width label: ACCEPTED`, `homoglyph label: ACCEPTED`, `table 2-col: ACCEPTED`, `daemon heading html == plugin heading html: True`.
- **Fix:** draw PrivacyFence's own rows from a separate list that plugin blocks cannot express. Render them in a distinct container with a "Checked by PrivacyFence" badge. Wrap every plugin block in a bordered "From the plugin" region (Plan A p7a). The label refusal for file tools stays as a second line of defence, with NFKC, invisible-character stripping, headings and tables added (Plan A p7b).

### F8 (medium, protocol): invisible characters survive block sanitising

- **Where:** `plugins/blocks.py:21-28` (`_STRIP_RE`); SDK `blocks.py:23`; `docs/plugin-protocol.md` (Blocks).
- **Scenario:** `clean_text` keeps U+200B to U+200D, U+2060, U+FEFF, U+00AD, the tag block U+E0000 to U+E007F, and U+2028/U+2029 in multi-line text. Instructions written in tag characters inside upstream content (a Jira issue, an email) are invisible on the card but reach the AI in the released payload. Zero-width characters inside a value defeat the PII detector, both in the review gate's scan and in the ADR 0137 write-result check.
- **Repro:** yes. The detector returns `'4111​1111​1111​1111' []` and `'192​.168.1.10' []`.
- **Fix:** strip every Cc, Cf, Co, Zl and Zp code point except `\n` and `\t`, with no normalisation (NFC would refuse legitimate NFD names, and Cn differs between Python versions). Do this in block text, titles, `subject_id` and the SDK (Plan A p6), and in write results before the PII scan (Plan A p7b).

### F9 (medium, protocol): `tools.changed` flood

- **Where:** `plugins/host.py:604-611`; `connector.py:182-204`; `rpc.py:339-356`.
- **Scenario:** every valid `tools.changed` notification is validated and applied. Each one writes an audit line, calls the tools-changed listener (a `notifications/tools/list_changed` to every MCP client) and pushes a Settings snapshot, even when the list is identical. The invalid-line limit does not apply, because the notification is valid.
- **Repro:** yes. `repro_flood.py tools`: a raw plugin flooding for 3 s gave `audit lines: 1625 tools_changed listener calls: 1625`, with the event loop lagging up to 0.2 s.
- **Fix:**
  - An identical list is a no-op: no audit line, no listener call.
  - At most `TOOLS_CHANGED_PER_MINUTE = 10` notifications per plugin per minute, whatever their outcome. Excess notifications are dropped unvalidated, with one audit line per minute saying changes were dropped.
  - New row in the limits table (Plan A p8).

### F10 (medium, protocol): unbounded Sheets ranges

- **Where:** `plugins/source_ops.py:410-418` (whole range fetched, then `_fit_prefix` serialises it repeatedly); `spool.py:185` (`put_rows` builds the file in memory); `spool.py:213-218` (every page re-reads the whole snapshot).
- **Scenario:** `range: "A:ZZ"` on a large sheet. Memory grows linearly with the range, I/O grows quadratically across pages, and the event loop stalls during serialisation.
- **Repro:** yes. `r4_sheets_memory.py` / `r4b_sheets_loop_stall.py`, 300k rows × 10 columns: 68 MiB snapshot, 399 MiB traced peak, a 546 ms loop stall, and 339 MiB re-read from disk over 6 pages.
- **Fix:** refuse a range whose values exceed `SHEETS_MAX_RANGE_BYTES = 64 MiB` (measured as compact UTF-8 JSON) with `payload_too_large` and `data.reason: "range_too_large"`. This is an explicit exception to ADR 0128: the read is refused, never truncated. Write the snapshot row by row with a line-offset index, so a page seeks instead of re-reading. Serialise off the event loop (Plan A p8).

### F11 (medium, protocol): outputs reads are quadratic and unbounded

- **Where:** `plugins/outputs.py:177-200` (whole-file sha256 on every page); `:76-120` (the whole tree is walked and sorted for every list page); `:325`, `:340`.
- **Scenario:** a 512 MiB CSV needs about 5,965 pages of 90 KB, and every page hashes all 512 MiB. Listing 50k files is quadratic in the same way.
- **Repro:** yes. `r1_outputs_cost.py`: `page 0 of 512MiB file: 4.34s … pages needed=5965`, `list page 1 of 50k files: 0.64s … pages needed=250`.
- **Fix:**
  - New limits `OUTPUT_MAX_FILE_BYTES = 256 MiB` and `OUTPUT_MAX_FILES = 10_000`. Over them, a file is not published and is reported as "too large to publish"; the SDK refuses to publish past either limit.
  - Cache the digest keyed on `(st_dev, st_ino, st_size, st_mtime_ns)`.
  - At most 2 concurrent output reads per plugin (Plan A p9).

### F12 (medium, protocol): test-host parity

- **Where:** `plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py:495-507`, `:625-631`, `:684-705`; `testing/_confirm.py`; `testing/_approvals.py`; `testing/_pages.py:27-33`; `tests/integration/test_sdk_testhost_conformance.py`.
- **Scenario:**
  - The host enforces `required`, and the daemon does not (F5).
  - The host sends `via: "rule"`, which the daemon never sends.
  - For a non-dict result, the host drops `approval_id`, while the daemon wraps it.
  - The host has no unattended switch, no 8-per-plugin or 64-total pending caps, and no ADR 0137 PII check on write results.
  - Its page headers lack `Cross-Origin-Opener-Policy` and `Permissions-Policy`.
  - The conformance test covers happy paths and one cap.

  A plugin can pass every test-host test and still fail in the daemon. Its handling of `confirmation_refused` can never be exercised.
- **Repro:** partial (code reading; F5 shows the daemon side).
- **Fix:** close each gap and add a conformance scenario for every refusal (Plan A p10).

### F13 (low, protocol): `args_digest` canonical form

- **Where:** `plugins/protocol.py:764-767`; SDK `plugin.py:107`; `connector.py:444-457`; `docs/plugin-protocol.md:380`.
- **Scenario:** Python and Node format `1e16` and `1.0` differently, and they sort keys differently (code points against UTF-16 units). A non-Python plugin following the spec answers `digest_mismatch`. A lone surrogate raises `UnicodeEncodeError`, after approval, outside the `except` in `_execute`.
- **Repro:** yes. `r5_digest_canon.py`: `digest equal: false`. `args_unvalidated.py`: `args_digest lone surrogate: UnicodeEncodeError`.
- **Fix:** protocol 1.4.0.
  - The daemon sends `args_digest` in `tool.prepare` as well. A plugin stores the digest it was given at prepare and compares it, as a string, with the one at execute; it never has to recompute it.
  - The canonical form stays as it is, documented as informative.
  - Lone surrogates are refused earlier by F5's check (Plan A p1, p3b).

### F14 (low, protocol): loose parameter-schema checks

- **Where:** `plugins/tools.py:88-115`; `connector.py:225-251`.
- **Scenario:** the daemon accepts:
  - `oneOf`, `anyOf`, `$ref`, `items` and `properties` on a scalar;
  - parameter names of any length or content (newlines, bidi characters, 5,000 characters);
  - parameter descriptions of any size (100 KB reaches the AI as tool text);
  - any number of parameters (5,000).

  A refusal detail echoes the raw name, control characters included, into the audit log and the Settings note.
- **Repro:** yes. `r1_tooldef_loose.py` showed `'parameter evil\nINJECTED LINE‮ of t1: …'`.
- **Fix:**
  - Parameter names must match `PARAM_NAME_RE = [A-Za-z_][A-Za-z0-9_]{0,63}`.
  - At most `MAX_PARAMS_PER_TOOL = 32` parameters per tool.
  - Each parameter description may be at most `MAX_DESCRIPTION_CHARS` long.
  - The keys allowed in a parameter schema are `type`, `description`, `maxLength`, `minLength`, `minimum`, `maximum` and `x-privacyfence-file`.
  - A refusal detail never echoes a name that fails the pattern (Plan A p3a).

### F15 (low, protocol): scope types are not validated at the handshake

- **Where:** `plugins/tools.py:43-65` (no caller); `protocol.py:274-285`; `policy/scopes.py:637`, `:651`.
- **Scenario:** 32 scope types, a duplicate `output`, empty or 100 KB descriptions, or a scope type named `anything` all pass the handshake. They then fail inside `on_ready` as a `ValueError`, which counts as a crash, so the plugin crash-loops to "crashed 5 times" instead of "manifest invalid". Introspection does not run `on_ready`, so the review dialog even shows the tool.
- **Repro:** yes. `r6`: `daemon path: ACCEPTED`. `anything_scope.py`: `set_tools raised: ValueError … cannot own`.
- **Fix:** call `validate_scope_types` in `PluginHost._validate`, and reserve `anything` next to `output` in the daemon, the SDK and the docs (Plan A p3a).

### F16 (low, protocol): the SDK and test host accept empty `title`/`effect`

- **Where:** SDK `plugin.py:691-694`; `testing/_host.py:194-197`; daemon `protocol.py:238-239`.
- **Scenario:** `@plugin.tool(..., effect="")` passes every test-host test. At install, initialize fails with "manifest invalid: tool.effect must not be empty".
- **Repro:** yes. `r3_empty_title.py`.
- **Fix:** the SDK and the test host require 1 to N characters (Plan A p10).

### F17 (low, protocol): a foreign `approval_id` passes on the non-withheld path

- **Where:** `plugins/connector.py:474-497`; ADR 0137.
- **Scenario:** a plugin returns `approval_id: "card-<another connector's card>"` with a result under 2 KB, and it reaches the AI. The "any other approval_id is dropped" rule is applied only on the withheld path.
- **Repro:** no (code reading).
- **Fix:** drop an `approval_id` that `owns_approval` does not confirm, on both paths (Plan A p7b).

### F18 (low, protocol): docs, schema and limits disagree with the code

- **Where:** `docs/plugin-protocol.md:67`, `:822-863`; `docs/plugin-protocol/protocol.schema.json`; `tests/unit/plugins/test_protocol.py:828-833`; ADR 0126.
- **Scenario:** nine disagreements.
  - (a) "The effective version is the lower minor", but the daemon never computes a minor and accepts `"1.banana"`.
  - (b) `x-limits` lacks `INVALID_LINES_LIMIT`, both pending caps, `MAX_SCOPE_TYPES`, `MAX_SCOPE_TYPE_DESCRIPTION_CHARS`, `CRASH_LIMIT`, `CRASH_WINDOW_SECONDS`, `RESTART_BACKOFF_SECONDS` and the 64 KiB manifest cap. The test checks only `x-limits` ⊆ constants.
  - (c) The limits table omits the per-plugin pending cap of 8.
  - (d) The schema gives `ConfirmRequestParams.kind` as 1 to 120 characters; the doc and the code use `[a-z][a-z0-9_]{0,30}`.
  - (e) The schema allows `approval.via: "rule"`, which the daemon never sends.
  - (f) The schema's `ToolDef.parameters` is a bare `object`.
  - (g) During introspection, `approval.*` gets `method_not_found` rather than `introspection_only`.
  - (h) ADR 0126 says the test host does not implement introspection, but it does.
  - (i) ADR 0126 says the validators are checked against the schema, but the tests compare names and five patterns only.
- **Repro:** yes (schema dump; `r6`).
- **Fix:**
  - Validate `protocol_version` as `MAJOR.MINOR.PATCH`, and compute the effective minor.
  - Generate `x-limits` from `constants`, and assert equality both ways.
  - Fix the schema entries.
  - Return `introspection_only` for every daemon-served method during introspection.
  - Add a schema-against-validator behavioural test.
  - Correct the ADR claims through a new ADR (Plan A p1, p3a, p11).

### F19 (low, protocol): Jira and Calendar continuations

- **Where:** `plugins/source_ops.py:203-221`.
- **Scenario:** an oversized provider page is fetched again on the next call and `k` items are skipped. If the order changed in between (ORDER BY updated), one key is served twice and another never, with no error. If the page shrank, the plugin gets `invalid_params` "cursor is not valid", which is not retryable.
- **Repro:** yes. `r7_jira_refetch.py`: `['PF-1','PF-2','PF-3','PF-3','PF-4','PF-5'] (PF-9 missing)`, and `shrunk page: invalid_params`.
- **Fix:** the cursor carries `h`, the first 16 hex characters of a sha256 over the provider page's item keys. On a mismatch, or a page shorter than `k`, the plugin gets `upstream_error` with `data.reason: "revision_changed"`, the reason ADR 0138 already uses for Sheets and Confluence (Plan A p8).

### F20 (low, protocol): the source check order in the docs

- **Where:** `docs/plugin-protocol.md:438-443`; ADR 0123; `source_ops.py:622-634`; `testing/_source.py:192-194`.
- **Scenario:** a call with bad per-operation parameters to a disabled connector returns `connector_unavailable`, not `invalid_params`. The daemon and the test host agree with each other, but not with the spec.
- **Repro:** no (code reading).
- **Fix:** change the doc to the code's order. Only the envelope is parsed first; operation parameters are checked after the connector state. A new ADR records this (Plan A p11).

### F21 (low, protocol): output names with control or bidi characters

- **Where:** `plugins/outputs.py:98-113`, `:126-130`, `:352-356`.
- **Scenario:** `evil‮gnp.csv` renders as "evilvsc.png", and `a\nb.csv` contains a line break. Both are published, read, and shown raw on the card.
- **Repro:** yes. `r1`: `published names: ["'a\\nb.csv'", "'evil\\u202egnp.csv'"]`.
- **Fix:** a path segment for which `clean_line(segment) != segment` is unpublished. This applies to the daemon and the test host (Plan A p6) and `OutputsClient.publish` (Plan A p9).

### F22 (low, protocol): Windows device names as plugin names

- **Where:** `plugins/constants.py:101-110`.
- **Scenario:** `con`, `nul`, `aux`, `prn`, `com1` and `lpt9` are accepted. On Windows, `logs\plugins\nul.log` and `plugin-data\con` resolve to devices.
- **Repro:** yes. `r4_names.py`.
- **Fix:** add `con prn aux nul com0`–`com9` and `lpt0`–`lpt9` to `RESERVED_PLUGIN_NAMES`, in the daemon, the SDK and the docs (Plan A p4).

### F23 (low, protocol): display-name impersonation

- **Where:** `plugins/manifest.py:98-102`; `confirm.py:109-124`; `connector.py:404-408`; `dialog_window_html.py:213-232`.
- **Scenario:** `display_name: "PrivacyFence"` or `"Gmail"` passes. A confirm card then reads "PrivacyFence: <title>" with no row naming the installed plugin. ADR 0127 shows "display (installed)" on approval cards for this reason, but confirm and tool cards do not.
- **Repro:** no (code reading).
- **Fix:**
  - Refuse a `display_name` that, compared with casefold, equals "PrivacyFence" or a built-in connector's label, or a reserved name.
  - Show `Plugin: <display_name> (<name>)` in PrivacyFence's region on confirm and tool cards (Plan A p4, p7a).

### F24 (info, protocol): file metadata before the gate

- **Where:** `plugins/connector.py:270-282`, `:363-364`; `files.py:131-140`; ADR 0141.
- **Scenario:** for a file named by the AI, the plugin's `tool.prepare` receives its name, size, sniffed type and sha256 before any human decision. This is by design (ADR 0141), but it is not stated where an owner would look for it.
- **Repro:** no.
- **Fix:** document it in `docs/security-and-compliance.md` and in the protocol's file section. No wire change (Plan A p11).

### F25 (medium, other): source calls and the shared thread pool

- **Where:** `plugins/source_ops.py:643` (`asyncio.to_thread`); `constants.py:17`; `gate.py:318-327`, `:949-959` (connector I/O and `detect_pii_categories` share the default pool).
- **Scenario:** 16 in-flight source calls of a few seconds each fill the default pool (8 workers on a 4-core machine). Every AI-side connector call and every PII scan then queues behind them. The 120 s timeout is only the plugin's own wait, so a hung upstream call holds a worker indefinitely.
- **Repro:** yes. `r1_threadpool_starvation.py`: an unrelated `to_thread` waited 9.80 s.
- **Fix:** a dedicated source executor of 4 workers, a per-plugin semaphore of 2, and a daemon-side deadline of 110 s (below the plugin's 120 s wait) that answers `timeout` (Plan B p2).

### F26 (medium, other): spool retention

- **Where:** `plugins/spool.py:161-179`, `:240`; `host.py:361`, `:538-541`, `:875-895`.
- **Scenario:**
  - `sweep` runs only inside spool calls, so content stays on disk for hours or days after the last call, including after disable, purge or uninstall.
  - Drive exports have no per-plugin count cap.
  - A crashed run's leftovers stay until the first `source.call`.
  - Purge does not touch the spool, although the docs promise 10 idle minutes.
- **Repro:** yes. `r3_spool_retention_and_count.py`: `export spool files held for one plugin: 50, 50 MiB`, and after 10,000 s idle the spool still holds 51 files.
- **Fix:**
  - A periodic sweep every 60 s.
  - `DownloadSpool.drop_plugin(name)`, called on stop, disable, purge and uninstall.
  - Create the spool eagerly at host start (its constructor empties the directory).
  - Keep at most 4 exports per plugin (LRU) (Plan B p3).

### F27 (medium, other): upload slots

- **Where:** `src/privacyfence/upload_staging.py:139-175`, `:220-239`; `local_files.py:254-256`, `:383-399`; `web/mcp_tools.py` (slot tool description).
- **Scenario:**
  - Thousands of slots of 50 MB each can be created.
  - Each PUT is buffered whole in RAM.
  - A slot read by a call whose gate never passes has its hold extended by 600 s on every peek, so it never expires.
  - The tool description promises the slot is bound to the conversation, but any call by the same principal can claim it.
- **Repro:** yes. `r3_upload_slots.py`: `slots created without refusal: 5000`, and `expiry pushed by a peek: +601s`.
- **Fix:**
  - At most 32 live slots and 256 MB staged per principal.
  - Stream the PUT body to the encrypted file in authenticated 64 KiB records.
  - Cap the hold at `created_at + 2 × UPLOAD_HOLD_SECONDS`.
  - Correct the description (Plan B p4).

### F28 (medium, other): a cancelled source call is not audited

- **Where:** `plugins/source_ops.py:672-689`; `rpc.py:157-162`.
- **Scenario:** the plugin exits or is stopped while a source call is in flight. The handler is cancelled and no `plugin_source` entry is written, although the worker thread finishes the upstream read and may spool it. ADR 0123 says every call is audited.
- **Repro:** yes. `r2_cancel_no_audit.py`: `upstream reads completed: ['S1'] / audit entries: []`.
- **Fix:** audit in a `finally` with `error=cancelled`, then re-raise (Plan B p2).

### F29 (medium, other): expired sessions on plugin routes

- **Where:** `src/privacyfence/web/server.py:1070-1073`; `session_auth.py:187-196`, `:281-298`; `routes_plugins.py:97`, `:117`; `routes_plugin_browser.py:49`, `:55`.
- **Scenario:** plugin routes are gated only by `is_human_session`, and `provenance()` ignores the idle (30 min) and absolute (24 h) timeouts. An expired cookie keeps opening `/plugins/<name>/…` and `/plugin-pages` until something else touches the session. Browsing plugin pages never refreshes the idle timer.
- **Repro:** yes. `repro_expired_session.py`: `plugin page after expiry: 200 <p>secret report</p>`, while `/approvals` gives 401.
- **Fix:** both route sets require `session_auth.authenticated()` (which checks expiry and refreshes the idle timer) as well as a human session. `session_auth.py` is not changed, because an expiry-aware `principal_id()` would resolve an expired `os-<uid>` session to the owner (Plan B p1).

### F30 (low, other): open redirect on `?bootstrap=`

- **Where:** `src/privacyfence/web/server.py:780`.
- **Scenario:** `GET //evil.example/phish?bootstrap=x` gives `303 Location: //evil.example/phish`, whether or not the code is valid.
- **Repro:** yes, in process and against live uvicorn.
- **Fix:** redirect to `"/" + path.lstrip("/")` (Plan B p1).

### F31 (low, other): a Sheets snapshot cursor can be rebound

- **Where:** `plugins/source_ops.py:420-434`.
- **Scenario:** a plugin builds a cursor for sheet B around sheet A's snapshot id. It is served A's rows, and the audit records B.
- **Repro:** yes. `r5_sheets_snapshot_rebind.py`.
- **Fix:** store a digest of `_bound_sheets(params)` with the snapshot, and refuse a mismatch with `invalid_params` "cursor is not valid" (Plan B p3).

### F32 (low, other): lone surrogates and page sizing

- **Where:** `plugins/source_ops.py:471-476`, `:652-654`; `spool.py:61-67`, `:222-229`.
- **Scenario:** one field holding a lone surrogate makes `_utf8_json` escape the whole result, so a page sized as UTF-8 comes out about 3 times over the limit and the read fails. This contradicts ADR 0128.
- **Repro:** yes. `r6_confluence_surrogate.py`: `refused: payload_too_large`. `r6b_sheets_surrogate.py`: `page 2 refused`.
- **Fix:** replace lone surrogates with U+FFFD in source results before measuring (Plan B p3).

### F33 (low, other): audit summary quoting

- **Where:** `plugins/source_ops.py:270`, `:546`, `:562`, `:617-619`, `:684`.
- **Scenario:** `range: "A1; bytes=0"` produces the summary `S; A1; bytes=0; bytes=12345`. A refused operation is audited as `unknown`.
- **Repro:** no (code reading).
- **Fix:** JSON-quote each plugin-chosen target value, and record the operation name when it is one of `SOURCE_OPERATIONS` (Plan B p2).

### F34 (low, other): Drive `file_id`

- **Where:** `src/privacyfence/drive_client.py:1504`, `:1537`; `plugins/source_ops.py` (`_validate_drive_download`).
- **Scenario:** `file_id` is put into the URL unquoted. Today it is protected only because `get_file_metadata` runs first, and no test pins that order.
- **Repro:** no.
- **Fix:** validate `file_id` against `[A-Za-z0-9_-]{1,256}` in `_validate_drive_download` and in the test host. Quote it with `urllib.parse.quote(file_id, safe="")` in the client (Plan B p2).

### F35 (low, other): Windows junctions in outputs

- **Where:** `plugins/outputs.py:105-109`.
- **Scenario:** on Windows, `lstat` of a junction reports a directory, so `list_outputs` follows it. That exposes names and sizes outside the folder, and a loop multiplies entries up to depth 8.
- **Repro:** no (no Windows host).
- **Fix:** skip an entry whose `st_file_attributes` has `FILE_ATTRIBUTE_REPARSE_POINT`, and track visited `(st_dev, st_ino)` (Plan B p5).

### F36 (low, other): case-insensitive output paths

- **Where:** `plugins/outputs.py:8-11`, `:145-150`; `tests/unit/plugins/test_outputs.py:247-256`.
- **Scenario:** on APFS, `Reports/a.csv` and `reports/a.csv` are both accepted as canonical, so cards and rules split across spellings. The test's `else` branch allows it.
- **Repro:** no (Linux host).
- **Fix:** compare each segment exactly with the names `os.scandir` returns, and make the test require refusal (Plan B p5).

### F37 (low, other): SDK publish is not atomic

- **Where:** `plugin-sdk/src/privacyfence_plugin_sdk/plugin.py:473-481`.
- **Scenario:** two concurrent publishes of one `relpath` both pass `exists()` and write the same `.name.tmp`.
- **Repro:** no.
- **Fix:** write to `tempfile.mkstemp` in the target directory, then `os.link(temp, target)`, which fails if the target exists, then unlink the temp (Plan B p5).

### F38 (low, other): approval audit rows

- **Where:** `plugins/host.py:322-326`, `:863`.
- **Scenario:** a stored approval and its revocation are audited only as `"{kind}; {status}"`.
- **Repro:** no.
- **Fix:** audit `approval_id`, the cleaned and capped `subject_id`, and `digest` (Plan B p6).

### F39 (low, other): log hygiene

- **Where:** `plugins/connector.py:459` (logs `exc`, which includes the plugin's detail); `connector.py:373` and `blocks.py:132` (echo plugin-chosen keys).
- **Scenario:** plugin-sent text that may carry connector content reaches the daemon log, contrary to the module docstrings.
- **Repro:** no.
- **Fix:** log `exc.code` only, and make block errors name positions, not values (Plan B p6).

### F40 (low, other): RecursionError in the RPC reader

- **Where:** `plugins/rpc.py:259-263`; SDK `_rpc.py:261-265`.
- **Scenario:** a line of 200,000 `[` characters raises RecursionError, which ends the reader with `reader_failed` instead of counting as a parse error.
- **Repro:** yes. `r7_deep_json.py`: `closed: True reason: ['reader_failed'] responses sent: 0`.
- **Fix:** treat `RecursionError` like `ValueError` (Plan B p6).

### F41 (low, other): the example plugin

- **Where:** `examples/plugins/today/today_plugin.py:246-270`, `:304-321`, `:507-519`, `:553`.
- **Scenario:**
  - Arguments are not type-checked.
  - `refresh` is an `auto`-gate write that takes an AI-chosen `calendar_id`.
  - A hidden `crash` tool is switched on by an unhashed `build-flags.json` beside the executable.
  - `_write_json` is not atomic.
- **Repro:** yes. `r4_today_typed_args.py`: page 500.
- **Fix:**
  - Type-check arguments.
  - Put `refresh` on the review gate.
  - Remove the flag file and the `crash` tool from the example.
  - Write atomically (Plan B p7).

### F42 (low, other): SDK release checks

- **Where:** `plugin-sdk/pyproject.toml`; `.github/workflows/publish-pypi.yml:231-252`.
- **Scenario:**
  - There is no assertion that the SDK version equals the tag.
  - There is no smoke install of the built wheel.
  - The build tools are unpinned.
  - `fallback_version = "0.0.0"` could be published.
  - `Issues` points at issue #846.
- **Repro:** no.
- **Fix:** assert the version, smoke-install the wheel in a fresh venv and import `privacyfence_plugin_sdk.testing`, refuse a 0.0.0 build, and fix the URL (Plan B p7).

### F43 (low, other): timing-dependent tests

- **Where:**
  - `tests/unit/plugins/test_rpc.py:337-346`, `:637-645`;
  - `test_supervisor.py:453`;
  - `test_host.py:1950`;
  - `tests/integration/test_plugin_pages_browser.py:295-306` (`wait_for_timeout(1000)`).
- **Scenario:** sleep-then-assert-absence tests pass vacuously on a slow or Windows runner.
- **Repro:** no.
- **Fix:** wait on a positive marker instead (a follow-up request answered, or a `page` event listener) (Plan B p8).

### F44 (low, other): remaining test gaps

- **Where:** `tests/integration/test_plugin_approval_frame_browser.py:180-198`; `tests/unit/web/test_routes_plugins.py`; `tests/unit/plugins/test_outputs.py`; `test_spool.py:215`, `:312`; `tests/integration/test_plugin_files.py`.
- **Scenario:** none of the following is tested:
  - that the framed page's own requests carry no cookie (the check holds today: `repro_frame_cookies.py`);
  - a double-encoded path at route level;
  - FIFO, device and hard-link entries in outputs;
  - two concurrent calls on one `upload:` slot under an auto-accept rule;
  - the periodic sweep (the tests call `sweep()` by hand).
- **Repro:** n/a.
- **Fix:** add the tests (Plan B p8; the sweep test is in Plan B p3).

### F45 (low, other): strict mypy

- **Where:** `scripts/mypy_strict_modules.py` / `pyproject.toml` mypy overrides.
- **Scenario:** six modules are strict, and none of them is a plugin module.
- **Repro:** `python3 scripts/mypy_strict_modules.py` lists the six.
- **Fix:** promote `privacyfence.plugins.rpc`, `.protocol`, `.constants`, `.cursors` and `.blocks` (Plan B p9).

### F46 (info, other): the invalid-line streak

- **Where:** `plugins/rpc.py:281-283`, `:339-342`.
- **Scenario:** a response with an unknown id, or a notification nobody handles, counts as valid. A plugin can stream them forever without reaching `INVALID_LINES_LIMIT`.
- **Repro:** yes. `repro_flood.py resp` (4 MB unsolicited responses for 3 s): plugin still running, loop lag 0.05 s.
- **Fix:** none needed. The plugin hurts only itself (its line budget is its own), and F9 bounds the one notification with side effects. Plan B p6 adds a sentence saying so to the `rpc.py` docstring.

### F47 (info, other): navigation as an exfiltration channel

- **Where:** `plugins/pages.py:22-35`.
- **Scenario:** a page, or HTML injected into one, can navigate to an outside URL or use `<link rel=dns-prefetch>`. `docs/plugin-protocol.md` already says the sandbox is not a data-loss boundary.
- **Fix:** add a note in `docs/plugins.md` that pages showing third-party content must escape it (Plan B p10).
