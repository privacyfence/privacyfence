# Plan: plugin protocol hardening before third-party plugin development (protocol 1.4)

## Goal

A security and test review of the plugin framework (2026-10-09, on PR #874 at 392e3ebc) found 47
problems. This plan fixes the 24 of class `protocol`. These are the ones that change, or force a
decision about, what plugin authors build against:

- the wire protocol;
- the manifest and tool-definition rules;
- the SDK and its test host;
- the limits table and error reasons;
- the trust model.

They must land before the maintainer starts writing plugins, because each one becomes a breaking
change once third-party plugins exist. The other 23 findings are in Plan B
(`plan/plugin-framework-hardening-3`); they do not change what plugin authors see.

What changes for the owner and for plugin authors:

1. **An unattended agent can no longer approve a plugin's request.** Plugin confirmation and
   approval cards always need a signed-in human; `require_step_up` now only decides whether a
   passkey is asked for as well (finding F1, **high**).
2. **The trust model is stated honestly and fixed as a contract.** An enabled plugin can write
   everything the service account can write. The docs say so, and the areas a plugin may write
   become a protocol contract, with a per-plugin `TMPDIR`. Introspection (the "Review" button)
   becomes a sensitive action. The whole plugin folder is permission-checked and hashed, not only
   the executable, and `command` arguments may not point outside it (F2 **high**, F3, F4).
3. **Protocol 1.4.0.**
   - The daemon validates the AI's arguments against the tool's reviewed schema before
     `tool.prepare`.
   - It sends `args_digest` with `tool.prepare`, so no plugin has to reproduce Python's JSON
     canonical form.
   - Tool definitions and scope types are checked as the docs already say.
   - Three new limits apply: `tools.changed` rate, Sheets range size, output file size and count.
   - Jira and Calendar continuations fail with `revision_changed` instead of skipping or
     duplicating items.

   (F5, F9-F11, F13-F15, F19)
4. **Cards cannot be faked by a plugin.**
   - PrivacyFence's own rows are drawn in their own region, which a plugin cannot reach.
   - Invisible characters are stripped from everything a plugin shows or returns.
   - A write on the `review` gate gets the write card.
   - Every plugin card names the installed plugin.

   (F6-F8, F17, F21-F23)
5. **The SDK test host behaves like the daemon** for arguments, refusals, caps, write results and
   page headers, and the docs, schema and `x-limits` match the code (F12, F16, F18, F20, F24).

Tracking issue: [privacyfence/privacyfence#846](https://github.com/privacyfence/privacyfence/issues/846).
The full findings list with reproduction notes is the appendix at the end of this plan.

## Current state

### The base

- This plan branch is cut from `origin/feature/plugin-files-and-page-links` at 392e3ebc, not from
  `main`. That branch is [PR #874](https://github.com/privacyfence/privacyfence/pull/874) (protocol
  **1.3.0**, ADRs 0140 and 0141). The PR was open and blocked at the time of writing, and the code
  this plan talks about exists only there. `manual_before` `mb1-pr874-merged` asks the maintainer to
  merge it first, or to accept that this plan's PR carries #874's diff until #874 merges. p0 merges
  `origin/main` either way.
- `docs/adr/` ends at **0141**. This plan's ADRs take **0142 to 0147**. Plan B's ADR, if it lands
  later, takes the next free number.
- Line numbers below are at 392e3ebc. Symbols were checked there; line numbers may drift by a few.

### What the review checked and ran

- `ruff check .`, `bandit -c pyproject.toml -r src` and `python3 scripts/mypy_strict_modules.py`
  pass.
- `python3 -m pytest tests/unit -q`: 15437 passed, 36 skipped (none in plugin tests).
- `pytest tests/integration/test_plugin_*.py tests/integration/test_sdk_testhost_conformance.py`:
  132 passed (Chromium via `PRIVACYFENCE_TEST_CHROMIUM`).

### The code each phase changes

- **Wire version.**
  - `src/privacyfence/plugins/constants.py:13` `PROTOCOL_VERSION = "1.3.0"`.
  - `supervisor.py:379-380` compares only the major (`split(".")[0]`); `InitializeResult.from_wire`
    (`protocol.py`) accepts any string.
  - The SDK states its own version in `plugin-sdk/src/privacyfence_plugin_sdk/plugin.py`; grep
    `PROTOCOL_VERSION`.
  - `scripts/gen_plugin_sdk_types.py` generates `plugin-sdk/.../types.py` from
    `docs/plugin-protocol/protocol.schema.json` (`--check` is in CI).
- **Digest.**
  - `protocol.py:764-767` `args_digest(args)` is `json.dumps(sort_keys=True, separators=(",", ":"),
    ensure_ascii=False)`.
  - The daemon sends it only in `tool.execute` (`connector.py:444-457`).
  - The SDK recomputes it at prepare and at execute (`plugin.py:107`, `:907`, `:940-946`).
- **Arguments.**
  - `PluginConnector.call` (`connector.py:261-302`) checks file parameters only and passes the
    client's raw dict on as `plugin_args`.
  - `_check_parameters` (`tools.py:88-115`) checks `type` and `enum` only.
  - `validate_scope_types` (`tools.py:43-65`) has no caller.
  - `PluginTestHost.call_tool` refuses a missing required argument (`testing/_host.py:495-507`);
    the daemon does not.
- **Plugin cards.**
  - `register_confirm(*, sensitive=False, notify=False)` (`src/privacyfence/approvals.py:634`).
  - Plugin confirmations (`plugins/confirm.py:117`) and approvals (`plugins/approvals.py:301`)
    pass `sensitive=parsed.require_step_up`.
  - The decide route (`web/routes_approvals.py:683-697`) runs `human_session_guard` only for
    `_STEP_UP_RESULTS = ("accept", "accept_all")` or a sensitive confirm.
- **Trust.**
  - `trust.py` checks and hashes `command[0]` and the manifest (`_paths_to_check`, `_inspect`,
    `discover`).
  - `manifest.resolve_command` (`manifest.py:157-166`) keeps `command[1:]` as given.
  - `inspect_plugin` is in `_NON_SENSITIVE_ACTIONS` (`web/routes_settings.py:266-268`).
  - `child_env` (`supervisor.py:64-68`) passes the daemon's `TEMP`/`TMP`/`TMPDIR`.
  - `PluginStateStore` (`state.py`, `STATE_VERSION = 1`) records `executable_sha256` and
    `manifest_sha256`.
- **Card rendering.**
  - `PluginConnector._gate` (`connector.py:393-413`) builds
    `to_card_blocks(file_blocks + plugin_blocks)` with the daemon's `files.checked_heading()` and
    `files.card_block()` rows. The plugin's rows go under `files.plugin_heading()`, all as plain
    blocks.
  - `approval_window_html._render_block` (`:371`) renders every block alike.
  - `files.refuse_reserved_labels` (`files.py:176`) checks `fields` labels only.
  - `blocks._STRIP_RE` (`blocks.py:21-28`) strips C0/C1 and bidi controls only.
  - `gate.gated_call`'s `review` branch shows the read card (`show_read_popup`, `gate.py:1077-1134`).
    Its `popup` branch shows the write card with the Effect row (`gate.py:1210-1260`). Both
    branches let an auto-accept rule accept.
- **Limits.**
  - `host.py:604-611` applies every `tools.changed`.
  - `source_ops._run_sheets` (`source_ops.py:410-434`) fetches the whole range, and
    `spool.put_rows` (`spool.py:185`) holds it all.
  - `outputs.py:177-200` hashes the whole file on every page read.
  - Jira and Calendar continuations re-fetch and skip (`source_ops.py:203-221`).

### Constraints from the repository

- `tests/unit/test_code_no_history.py` refuses plan item and finding ids (`p3`, `F5`, `ma1`) in
  code, comments and test names.
- Comments explain only a non-obvious *why*. ADRs are frozen once Accepted: an amendment is a new
  ADR plus an "Amended by" status line and index cell on the old one (`docs/adr/README.md`).
- `docs/coding-and-testing-guidelines.md` §2.7 is the definition of done. A change to
  `web/server.py` (none planned here) would add the integration rows.
- p0 of PR #874 found that a plan document sitting on a branch fails two docs tests unless the plan
  is listed in `docs/README.md` and `scripts/build_site.py`'s `CONTRIBUTOR_DOCS`. This plan's p0
  does the same, and p11 undoes it.

## Design

### D1. Protocol 1.4.0: version handling and the digest at prepare

- `PROTOCOL_VERSION = "1.4.0"`, in the daemon constants and in the SDK. Minor, not major. Every
  tightened rule below refuses only input that the docs already said was invalid, or that only a
  buggy or hostile plugin sends, and no third-party plugin exists yet. ADR 0143 records this.
- `InitializeResult.from_wire` refuses a `protocol_version` that does not fullmatch
  `[0-9]+\.[0-9]+\.[0-9]+`, with `RpcError("invalid_params", "protocol_version is not
  MAJOR.MINOR.PATCH")`. The supervisor turns that into the existing `_Fatal("manifest invalid: …")`.
  `InitializeResult` gains a property `minor -> int`.
- The **effective minor** is `min(plugin minor, 4)`. `PluginHost` passes it to `PluginConnector`
  as the keyword `protocol_minor: int`, from `on_ready`.
- `tool.prepare` params gain `"args_digest": args_digest(plugin_args)`. They are sent only when the
  effective minor is ≥ 4. The Python 1.3 SDK would ignore the key (`_prepare` reads `params.get`),
  but the 1.3 schema has `additionalProperties: false`, which a non-Python plugin may enforce. The canonical form
  is unchanged (it is documented as informative). Lone surrogates can no longer reach it, because
  D3 refuses them.
- **SDK.**
  - At prepare, if `params` has `args_digest` (a `str` matching `sha256:[0-9a-f]{64}`), the SDK
    stores it as the entry's digest; otherwise it computes it as today.
  - The prepared entry gains an `args` field holding the prepare args.
  - At execute, the SDK compares the given digest with the stored one, as strings. It also compares
    `json.dumps(args, sort_keys=True)` of the given and stored args, which avoids `True == 1 == 1.0`.
    Either mismatch answers `digest_mismatch`, as today.
  - A non-Python plugin can therefore check with plain string comparisons, without reproducing
    any JSON canonical form.
- **Test host.** It sends `args_digest` in prepare exactly as the daemon does.
- **Schema.** `x-protocol-version` becomes `"1.4.0"`. `ToolPrepareParams` gains the optional
  `args_digest` (the `DIGEST_RE` pattern), and `types.py` is regenerated.

### D2. Plugin cards always need a human session (F1)

- `PendingApproval` gains `human_only: bool = False`. `register_confirm(*, sensitive=False,
  notify=False, human_only=False)` stores it.
- `PluginConfirmationService` (`plugins/confirm.py:117`) and `ApprovalService`
  (`plugins/approvals.py:301`) call
  `register_confirm(sensitive=parsed.require_step_up, notify=True, human_only=True)`.
- In `routes_approvals.decide`, next to `sensitive_confirm`:
  `human_confirm = approval is not None and approval.human_only and result == CONFIRM_RESULTS[0]`.
  The guard condition becomes `if result in _STEP_UP_RESULTS or sensitive_confirm or human_confirm:`.
  The `what` string is `"approve a plugin's request"` whenever `human_confirm` holds, a sensitive
  plugin card included. Otherwise it stays as today.
- The passkey step (`approval_step_up.guard_decision`) is unchanged: it still follows `sensitive`.
- The `register_confirm` docstring gains one sentence for `human_only`: a plugin's card is the whole
  gate on what the plugin does next, so it always needs a human session; `require_step_up` only adds
  the passkey.
- `docs/plugin-protocol.md` (confirm and approval sections), `docs/security-and-compliance.md`
  ("Confirmations", "Approvals and the card frame") and ADR 0142 say: a human session always,
  plus a passkey when `require_step_up` is true.

### D3. The AI's arguments are checked against the reviewed schema (F5, F14, F15)

**Tool-definition rules** (daemon `tools._check_parameters`, SDK registration and the test host
alike):

- Parameter names fullmatch `PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")` (new
  constant). Refusal: `"a parameter of {tool} has a name that does not match the parameter name
  pattern"`. The bad name is never echoed.
- At most `MAX_PARAMS_PER_TOOL = 32` parameters. Refusal:
  `"tool {tool} has more than 32 parameters"`.
- The keys of a parameter schema must be a subset of `{"type", "description", "minLength",
  "maxLength", "minimum", "maximum", "x-privacyfence-file"}`. Refusal: `"parameter {pname} of
  {tool} uses an unsupported schema key"`.
  - `minLength`/`maxLength` are allowed only on `string`. Each is an `int` (not `bool`), 0 ≤ n ≤
    `MAX_ARG_STRING_CHARS`, with min ≤ max.
  - `minimum`/`maximum` are allowed only on `integer`/`number`. Each is a finite int or float (not
    `bool`), with min ≤ max.
  - Violations: `"parameter {pname} of {tool} has an invalid {key}"`.
- A parameter `description` is a `str` of at most `MAX_DESCRIPTION_CHARS`. Refusal:
  `"parameter {pname} of {tool} has a description over 1024 characters"`.
- Scope types: `PluginHost._validate` calls `validate_scope_types(raw scope types)` before
  `validate_tool_defs` and raises `ToolDefError`, so a bad list is "manifest invalid" at the
  handshake, never a crash loop. `validate_scope_types` reserves `anything` next to `output`, with
  the message `"scope type {name} is reserved"`. The SDK's `Plugin.scope_type` refuses both.
- During introspection, `approval.request`, `approval.check`, `approval.await` and `confirm.await`
  join the refuse map in `Supervisor.introspect` (`introspection_only`).

**Constraints the AI can see.** `PluginConnector._tool_spec` appends the declared constraints to
each parameter's MCP description, so the AI is told the limits it is held to:

- `" (at least {minLength} characters)"`;
- `" (at most {maxLength} characters)"`;
- `" (from {minimum} to {maximum})"`, or with only one bound, `" (at least {minimum})"` /
  `" (at most {maximum})"`.

`ToolParam` is not changed.

**Argument check.** A new module `src/privacyfence/plugins/arguments.py` holds
`check_args(defn: ToolDef, args: dict, *, title: str) -> dict`.

- `PluginConnector.call` calls it on `plugin_args` (after file parameters are split off) before
  `_reuse_or_prepare`, and uses its return value as `plugin_args` from then on (for the prepare
  key, prepare, the digest and execute).
- Parameters that carry `x-privacyfence-file` are skipped by every rule below. They are neither
  "undeclared" nor "missing", because `connector.call` and `files.resolve_file` already handle
  them.
- An `integer` parameter given a finite `float` with `is_integer()` (for example `5.0`, valid JSON
  Schema) is accepted and replaced by `int(value)` in the returned dict.
- It raises `ValueError` with these exact messages, and no card is shown:

| Case | Message |
|---|---|
| a key not in `properties` | `"{title} got an argument it does not declare."` |
| a `required` key missing | `"{title} needs the argument {pname}."` |
| `string` not a `str` | `"{title}: {pname} must be text."` |
| `integer` not an `int` (after the float rule above), or a `bool` | `"{title}: {pname} must be a whole number."` |
| `number` not an `int`/`float`, a `bool`, or non-finite | `"{title}: {pname} must be a number."` |
| `boolean` not a `bool` | `"{title}: {pname} must be true or false."` |
| a string with a lone surrogate (`"\ud800" <= c <= "\udfff"`) | `"{title}: {pname} is not valid text."` |
| a string longer than its `maxLength`, else longer than `MAX_ARG_STRING_CHARS = 65_536` | `"{title}: {pname} is longer than {n} characters."` |
| a string shorter than its `minLength` | `"{title}: {pname} is shorter than {n} characters."` |
| an integer beyond ±(2**53 − 1), or a number outside `minimum`/`maximum` | `"{title}: {pname} is out of range."` |

- `None` for an optional parameter counts as absent, and the key is dropped before prepare, the
  same as the client leaving it out.
- The SDK has the same check, in a new private module `plugin-sdk/src/privacyfence_plugin_sdk/_args.py`
  with the same function name and the same file-parameter skip. Its `ToolHandle` prepare path
  calls it and answers `RpcError("invalid_params", <the same sentence>)`. The daemon has already
  normalised the args, so the SDK's normalised dict equals what it was sent.
- `PluginTestHost.call_tool` uses that SDK function in place of its own required check, and raises
  what the daemon raises (`ValueError` with the same message).
- `tests/unit/plugins/test_arguments.py` and `tests/unit/plugin_sdk/test_args.py` run one shared
  parametrised table (the same tuples, copied) so the two cannot drift. A parity test in
  `tests/unit/plugin_sdk/test_sdk_files_parity.py` style imports both and compares the outcomes.

### D4. Manifest, names and the review dialog (F3, F4a, F22, F23a)

- **`inspect_plugin` becomes sensitive.** Move it from `_NON_SENSITIVE_ACTIONS` to
  `_SENSITIVE_ACTIONS` in `web/routes_settings.py`. Replace its comment with: "introspection runs
  the plugin's code as the service account (ADR 0144)". The Settings page already sends sensitive
  actions through the step-up flow. If the review button does not (it posts the action outside the
  generic handler), stop: the brief is wrong.
- **`command[1:]`.** In `manifest.load_manifest`, every element after the first is checked as a
  set of candidate paths: the element itself, the part after its first `=`, the part after a
  leading `@`, and, for an element starting with `-` and longer than 2 characters, the part from
  its third character (`-I/abs`). The element is refused when any candidate:
  - satisfies `os.path.isabs`;
  - starts with `~`, `/` or `\`;
  - matches `^[A-Za-z]:`;
  - or when the element has a `..` segment anywhere, split on `/`, `\` and `=`.

  Message: `ManifestError("command arguments must not be absolute paths or leave the plugin
  folder")`.
- **The review dialog** gains `"command": list(manifest.command)` in `PluginHost.inspect`'s review
  dict. `settings_window_html.py` shows it as a "Command" row, joined with spaces and escaped
  through `esc()`, next to the hashes.
- **Plugin names.** `RESERVED_PLUGIN_NAMES` gains `con`, `prn`, `aux`, `nul`, `com0` … `com9` and
  `lpt0` … `lpt9`. `PLUGIN_NAME_RE` already refuses `conin$`. The SDK's `_RESERVED_PLUGIN_NAMES`
  (`plugin.py:73`) gains the same names. `tests/unit/plugins/test_constants.py`, or the existing
  SDK parity test, asserts the two sets are equal.
- **Display names.** `load_manifest` refuses a `display_name` whose `.casefold().strip()` is in
  `_RESERVED_DISPLAY_NAMES = frozenset({"privacyfence", "privacy fence", "gmail", "google drive",
  "drive", "google contacts", "contacts", "google calendar", "calendar", "google tasks", "tasks",
  "slack", "jira", "confluence", "salesforce", "telegram", "google sheets", "sheets",
  "google docs", "docs", "apps script", "plugin", "plugins", "settings"})`. Message:
  `"display_name must not be the name of PrivacyFence or a built-in connector"`.
- **UX cost, accepted.** Review and Enable now each ask for a passkey when step-up is on. ADR 0144
  says so.

### D5. The whole plugin folder is checked and hashed; the writable-areas contract (F4b, F2)

**Which text the owner sees.** Today `_inspect` logs a trust problem's detail and shows
`NOT_ADMIN_ONLY` (`trust.py:162-166`). That stays for permission problems. The new link,
file-count and interpreter refusals are shown verbatim as the plugin's `problem`, because they hold
no attacker-chosen text beyond the cleaned, capped relative path.

**Every file is admin-only.** `trust._paths_to_check(plugin_dir, executable)` adds every entry
under `plugin_dir`, walked with `os.scandir` and no symlink following:

- each directory is checked with the directory rule (`admin_only_plugin_dir_write_problem` on
  Windows, the POSIX rule elsewhere);
- each regular file with the executable rule (`admin_only_write_problem`).

Refusals:

- A symlink or junction anywhere under the plugin folder: `"plugin folder holds a link: <relative
  path>"`. The relative path is shown only when `clean_line(path) == path` and it is at most 200
  characters; otherwise "a file".
- More than `PLUGIN_MAX_FILES = 20_000` entries: `"plugin folder holds more than 20000 files"`.

**The whole folder is hashed.** New `trust.folder_sha256(plugin_dir) -> str`:

- For every regular file (sorted by its POSIX-style relative path, encoded UTF-8 with
  `surrogateescape`), feed `relpath + "\0" + sha256_file(file) + "\n"` into one sha256.
- `DiscoveredPlugin` gains `files_sha256`.
- The hash is computed in `_inspect` after the permission check passes.
- **Cost.** `before_spawn` (`host.py:642-652`) uses a new `trust.discover_one(plugins_dir, name,
  trust_check=...)`, which inspects only the plugin being started. A rescan and `inspect` still
  hash every discovered plugin. That is bounded by `PLUGIN_MAX_FILES` per plugin and happens only
  on a rescan, a review or a start; this is accepted.

**State v2.** `STATE_VERSION = 2`, and `PluginRecord` gains `files_sha256: str`.

- `_parse` (`state.py:130`) accepts versions 1 and 2. A version-1 file loads with
  `files_sha256 = ""`. `check_hashes` treats `""` as drift, so every plugin enabled before the
  upgrade is disabled once and must be reviewed and enabled again. This is the conservative
  choice. The CHANGELOG line says so, and also says that a downgraded daemon reads the version-2
  file as corrupt and keeps every plugin disabled (fail closed).
- `HASH_DRIFT_REASON` and the matching `_STOP_REASONS["hash_changed"]` text become `"plugin files
  changed, enable again"`.
- `PluginHost.enable` takes and compares `files_sha256` exactly like the other two hashes.
- The settings route passes it through: the enable action's payload gains `files_sha256`.
- The review dict and the dialog show "Folder SHA-256".

**Shebang.** On POSIX, if the executable's first two bytes are `#!`:

- The interpreter is the first token of the line.
- If it is `/usr/bin/env` (or ends in `/env`), the interpreter is the next token not starting with
  `-`, resolved with `shutil.which(token, path=os.environ.get("PATH", os.defpath))`, the same
  `PATH` value `child_env` passes on.
- The resolved real path must pass `admin_only_write_problem`. Otherwise: `"the executable's
  interpreter can be changed by a non-administrator"`.
- An interpreter that cannot be resolved: `"the executable's interpreter was not found"`.
- Windows has no shebang, so the check is skipped there.

**Writable areas, the contract** (ADR 0145, `docs/plugin-protocol.md` "Files a plugin may write"):

- A plugin may create and change files only under:
  - `data_dir`;
  - each principal's `storage_dir`;
  - `output_dir` (when `outputs: true`);
  - its temporary directory.
- New `storage.tmp_dir(name) -> Path`, which is `paths.data_dir() / "plugin-tmp" / name`. It is
  created 0700 by `ensure_dirs`, and emptied (`shutil.rmtree` then recreated) before every spawn.
- `child_env` takes the plugin's name and sets `TMPDIR`, `TEMP` and `TMP` to it. The daemon's own
  values are no longer passed.
- `storage.remove_all` also removes it.
- Writing anywhere else works today but is outside the contract, and a later release may confine
  plugins to it.

### D6. Invisible characters (F8, F21)

- `blocks.clean_text(value)` removes every code point whose `unicodedata.category` is `Cc`, `Cf`,
  `Co`, `Zl` or `Zp`, except `\n` and `\t`.
  - It does **not** normalise. NFC would refuse legitimate NFD names, common on macOS, through the
    `clean_line(x) != x` identity checks.
  - It does not strip `Cn` (unassigned), whose set depends on the Python version's Unicode tables
    and so would differ between the daemon's and a plugin's Python.
  - Removing `Cf` also removes ZWJ and ZWNJ. Emoji sequences split into their parts, and some
    Persian and Indic spellings lose a joiner. This is accepted: what the card shows and what the AI
    receives must be the same text.
- `clean_line` maps runs of `\n`/`\t` to one space, as today. `Zl`/`Zp` are now removed before
  that.
- `_STRIP_RE` goes. The SDK's `blocks.py` (`:23`) gets the identical function. Both modules carry
  the same docstring sentence: format characters (zero-width, tags, soft hyphen) are removed so that
  what the card shows is what the AI receives.
- New `blocks.clean_json(value)`: recursively applies `clean_text` to every `str` (dict keys
  included) in a JSON value and returns the cleaned copy.
- `PluginConnector._screen_write_result` runs `clean_json` first, then measures, scans and returns
  the cleaned value.
- Outputs: a path segment for which `clean_line(segment) != segment` is not published. That covers
  control, bidi, format and line-break characters. The rule applies in `outputs.list_outputs` (skip
  the segment) and `_canonical_file` ("No such output file"), in `OutputsClient.publish`
  (`ValueError("output path holds a control or invisible character")`, wired in p9) and in
  `testing/_outputs.py`.

### D7. Card integrity (F6, F7, F17, F23b)

**Regions on the card.** Card blocks gain an optional `"origin"` key.

- `blocks.to_card_blocks(blocks, *, origin: str = "plugin")` sets `"origin": origin` on every card
  block it returns.
- `PluginConnector._gate` builds
  `to_card_blocks(file_blocks, origin="privacyfence") + to_card_blocks(prepared.preview + payload, origin="plugin")`
  and no longer inserts `files.plugin_heading()`.
- `files.CHECKED_HEADING` stays as the first PrivacyFence row.
- Block validation can never produce an `origin` key, because unknown fields are refused, so only
  daemon code sets `privacyfence`.

**Rendering.** In `approval_window_html.py`, the function that renders `blocks` (`:487`) groups
consecutive blocks by `origin`:

- `privacyfence` → `<section class="pf-own" aria-label="Checked by PrivacyFence"><p class="pf-badge">Checked by PrivacyFence</p>…</section>`;
- `plugin` → `<section class="pf-plugin" aria-label="From the plugin"><p class="pf-region-label">From the plugin</p>…</section>`;
- no `origin` (built-in connectors) → as today.

CSS: `.pf-own` has a 3px solid left border in the accent token and a tinted background.
`.pf-plugin` has a 1px dashed border in the line token and 8px padding. Both work in dark mode
through the existing tokens. Confirm cards (`dialog_window_html.build_confirmation_html`) and the
plugin approval card get the same two regions:

- PrivacyFence's region holds `Plugin: <display_name> (<name>)`, plus the existing kind/subject/
  digest rows on approval cards;
- the plugin's preview goes in the plugin region.

**The installed name on tool cards.** `PluginConnector._gate` sends
`preview={"Plugin": f"{display_name} ({plugin})", "Tool": title}`.

**Look-alike refusal, second line of defence.** `files.refuse_reserved_labels` becomes
`refuse_reserved_text(validated)`.

- It still applies only to tools with a file parameter, the cards that carry PrivacyFence's file
  rows. Elsewhere the regions are the defence, and everyday labels such as "File" stay usable: the
  `today` example's preview uses one (`today_plugin.py:415`).
- After `unicodedata.normalize("NFKC", clean_text(s)).casefold().strip()` it refuses:

- a `fields` label in `RESERVED_LABELS`;
- a `heading` text equal to `CHECKED_HEADING`, `"Checked by PrivacyFence"` or `PLUGIN_HEADING`;
- a `table` whose first column's header, or any first-column cell, is in `RESERVED_LABELS`.

Message (unchanged wording style): `"a preview row may not imitate PrivacyFence's own rows"`. The
SDK `_files.refuse_reserved_labels` and the test host get the same rename and rules.

**A write on the `review` gate gets the write card.**

- `PluginConnector` uses `effective_gate = "popup" if (not defn.read_only and defn.gate == "review") else defn.gate`
  in `_gate` (the `gate=` passed to `gated_call`) and in `_dynamic_spec`.
- Both branches allow rules, and the reviewed signature still records `review`. What changes:
  - the card gains its Effect row;
  - the PII handling is the write one (`gate.py:957-975`, `:1067-1070`): a PII hit in the summary is
    shown as information and does not force a confirmation or override a rule. This is how every
    built-in write already behaves, and ADR 0146 says so. A plugin that wants the read-side PII
    confirmation for a write has no way to ask for it.

**Foreign approval ids.** In `PluginConnector._execute`, when `result.approval_id` is not `None`
and `owns_approval(approval_id)` is false, the id is dropped before the result is built, and
`logger.info("Plugin %s returned an approval id it does not own", plugin)` is logged. This applies
on both the withheld and the non-withheld path.

### D8. Rate, size and continuation limits (F9, F10, F19)

**`tools.changed`.**

- `PluginConnector.handle_tools_changed` returns one of `"accepted"`, `"unchanged"`, `"rejected"`
  or `"dropped"`.
- The rate check comes first. At most `TOOLS_CHANGED_PER_MINUTE = 10` notifications of any outcome
  (accepted, unchanged or rejected) in any 60 s; the connector keeps a `collections.deque` of their
  monotonic times. Over the limit the notification is `"dropped"` without being validated.
- A raw `params["tools"]` equal (`==`) to the last raw list applied is `"unchanged"`: it is not
  validated, and gets no audit line and no listener call.
- A dropped list is lost; the daemon keeps the last applied list. `docs/plugin-protocol.md` tells
  authors to send at most one change per few seconds and to resend the newest list after a minute
  if they flooded.
- The first drop in a window writes one lifecycle audit line, `"tools changes dropped: more than 10
  in a minute"`, and `_dropped_window_start` is remembered so later drops in that window write
  nothing.
- `PluginHost`'s `tools_changed` handler calls `_notify_tools()` only for `"accepted"`, and
  `_changed()` only for `"accepted"` or `"rejected"`.

**Sheets ranges.**

- `SHEETS_MAX_RANGE_BYTES = 64 * 1024 * 1024`.
- `_run_sheets` computes each row's compact UTF-8 JSON length once (`len(_utf8_json(row))`, the
  module's existing encoder) into a list.
- If the sum plus row separators exceeds the cap, it raises
  `RpcError("payload_too_large", "the range is larger than 64 MiB; read it in smaller ranges", extra={"reason": "range_too_large"})`.
  Nothing is spooled. The `plugin_source` audit entry records `error=range_too_large`.
- The first page is the longest prefix that fits by cumulative sum, which replaces the repeated
  `_fit_prefix` serialisation.
- `DownloadSpool.put_rows(plugin, rows, encoded)` takes the already-encoded rows. It writes them as
  lines to the file in one pass and keeps an `array("q")` of line offsets in `_RowsEntry`.
- `rows_page` seeks to `offsets[first]` and reads lines until the budget, never the whole file.

**Jira and Calendar continuation.**

- When a provider page did not fit, the cursor state gains
  `"h": hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]`, where `keys` are that provider
  page's issue keys or event ids, in order.
- On continuation the page is fetched again. If `len(items) < k`, or the hash of its keys differs
  from `h`, the call raises
  `RpcError("upstream_error", "the results changed since the previous page; read them again from the start", extra={"reason": "revision_changed"})`.
- The state keys are exact (`_state_keys`, `source_ops.py:139-141`): `{"t", "k", "h"}` when
  `k > 0`, and `{"t", "k"}` when `k == 0` (the normal next-page cursor at `:219`). Anything else is
  `invalid_params` "cursor is not valid".
- `testing/_source.py` pages these two operations (`_PAGED_SAMPLES`, `:28`), so it mirrors the rule.

### D9. Output limits (F11)

- `OUTPUT_MAX_FILE_BYTES = 256 * 1024 * 1024` and `OUTPUT_MAX_FILES = 10_000`.
- `list_outputs` stops collecting after 10,000 published files in sorted path order. The list result
  gains `"limited": true` and the message `"Only the first 10000 files of this output folder are
  listed."`.
- A file over 256 MiB is listed with `"too_large": true`. Reading it answers
  `"This output file is larger than 256 MiB; PrivacyFence does not publish it."`.
- Digest cache: a module-level `_DigestCache` in `outputs.py` (an `OrderedDict` LRU of at most 256
  entries, behind a `threading.Lock`), keyed on `(plugin, relpath)` with the value
  `(st_dev, st_ino, st_size, st_mtime_ns, sha256)`.
  - `read_output` (`outputs.py:168-200`) takes it as a keyword `digests: _DigestCache | None`;
    `PluginOutputsConnector` passes the module instance.
  - A read reuses the digest when the fstat it already takes matches.
  - `tests/conftest.py` clears it.
  - A plugin that rewrites a file and restores its size and mtime with `os.utime` gets a stale
    digest on the card. This is accepted: the plugin is trusted (ADR 0121), and the bytes shown are
    always read fresh.
- An `asyncio.Semaphore(2)` per plugin wraps the read's `to_thread`.
- `OutputsClient.publish` refuses data over 256 MiB, and a 10,001st file, with `ValueError`.
  `testing/_outputs.py` mirrors this.

### D10. Test-host parity (F12, F16)

`PluginTestHost`:

- sends `via: "card"` for gated tools;
- wraps a non-dict write result with `approval_id` as `{"result": …, "approval_id": …}`;
- has `host.unattended: bool` (default `False`), which makes `confirm.request` and
  `approval.request` answer `confirmation_refused` with `data.reason: "unattended_session"`;
- enforces `MAX_PENDING_CONFIRMS_PER_PLUGIN = 8`, which answers `confirmation_refused` with
  `data.reason: "too_many_pending"`. The 64-total cap is meaningless with one plugin and is
  documented as daemon-only;
- applies the 2,048-byte write-result cap with the daemon's withheld shape;
- takes `PluginTestHost(write_result_pii=callable)`. When given, it receives the result's JSON text,
  and a non-empty return withholds the result. The docs say the daemon runs its PII detector here;
- carries page headers equal to the daemon's `pages.py` header dict, `Cross-Origin-Opener-Policy`
  and `Permissions-Policy` included;
- has an SDK and test host that refuse an empty `title` or `effect` (1 to 120 and 1 to 200
  characters).

`tests/integration/test_sdk_testhost_conformance.py` gains one scenario per refusal:

- an argument refusal (D3);
- unattended;
- too many pending;
- a withheld write;
- `unknown_call` / `digest_mismatch`;
- an invalid prepare result;
- `tools.changed` rejected.

Each runs against both the echo plugin in the daemon and the test host, and asserts the same code
and reason.

### D11. Docs, schema and ADRs (F2, F18, F20, F24)

- `docs/plugin-protocol/protocol.schema.json` `x-limits` is generated from
  `privacyfence.plugins.constants` by `scripts/gen_plugin_sdk_types.py` (a new `--limits` step
  inside the same `--check`). It holds every number in the doc's limits table.
  `tests/unit/plugins/test_protocol.py` asserts equality both ways.
- Schema fixes:
  - `ConfirmRequestParams.kind` pattern `^[a-z][a-z0-9_]{0,30}$`;
  - `approval.via` enum `["auto", "card"]`;
  - `ToolDef.parameters` with the D3 rules (property names pattern, the allowed keys,
    `maxProperties: 32`);
  - `ToolFile.name` with the one-line pattern.
- New test `tests/unit/plugins/test_schema_validators.py`.
  - It uses `jsonschema`, declared in the `test` extra of `pyproject.toml` as
    `"jsonschema>=4.18,<5"` (today it is only an undeclared dependency of `mcp`). The locks are
    regenerated with `scripts/update_dependency_locks.sh`, as `dod_conditional` requires.
  - The test holds its own inline samples: for each of `ToolDef`, `InitializeResult`,
    `ToolPrepareResult`, `ConfirmRequestParams` and `PageEntry`, at least three valid and five
    invalid messages. For each one, the schema's verdict equals the daemon validator's.
  - The `testing/samples/*.json` files are `source.call` results and are not used.
- `docs/plugin-protocol.md`:
  - the version rule (D1) and "what changed in 1.4";
  - arguments (D3);
  - the writable-areas section (D5);
  - the card regions and the cleaning rule (D6, D7);
  - the limits table rows (D8, D9);
  - the source-call check order as the code does it: envelope, introspection, operation allowed,
    principal, connector state, then operation parameters;
  - the file metadata sent at prepare (F24);
  - `introspection_only` for every daemon-served method.
- `docs/plugins.md`: the author-facing summary of 1.4.
- `docs/security-and-compliance.md`, "Plugins":
  - replace "can read what the service account can read" with the D5 residual-risk wording;
  - drop "only through a card or a rule you wrote" and "a directory the plugin cannot see"
    (`spool.py:5` docstring too);
  - state D2, D4 and D5;
  - add F24's sentence.

### Rejected alternatives

- **Protocol 2.0.** Rejected. Nothing outside this repository speaks the protocol yet, and a major
  bump would refuse every existing fixture and example for no gain.
- **Refusing `gate: review` for writes.** Rejected in favour of D7's mapping: it would break valid
  plugins, and `review` and `popup` differ only in the card.
- **Signing the plugin state file.** Rejected: the key would be readable by the plugin (same
  account).
- **OS confinement now** (a separate account, Landlock, `sandbox-exec`, AppContainer). Too large
  for this plan, and it differs per platform. D5 fixes the contract it would enforce; ADR 0145
  names it as the follow-up.
- **JCS (RFC 8785) for the digest.** Rejected for D1: sending the digest is simpler and needs no
  float formatting rules.
- **Truncating a large Sheets range** instead of refusing it. Rejected by ADR 0128 (reads never
  truncate).

## ADRs

All written in p11, status Accepted, each with "Amends" lines. The ADRs they amend get "Amended by"
status lines and index cells.

- **0142**: A plugin's confirmation and approval cards always need a human session;
  `require_step_up` adds only the passkey. Amends 0122 and 0127.
- **0143**: Protocol 1.4: the daemon checks a tool's arguments against its reviewed schema, sends
  the args digest at prepare, and checks tool definitions and scope types as documented. Amends 0122
  and 0126 (the test-host claims).
- **0144**: The whole plugin folder is permission-checked and hashed, command arguments stay inside
  it, display names cannot be PrivacyFence's or a connector's, and introspection is sensitive.
  Amends 0121.
- **0145**: An enabled plugin can write what the service account can write; the areas it may write
  are a contract, and confinement is follow-up work. Amends 0121 and 0131.
- **0146**: PrivacyFence's own rows on a plugin card sit in their own region, plugin text loses
  invisible characters, and a write always gets the write card. Amends 0127, 0137 and 0141.
- **0147**: Plugin limits for tool-list changes, Sheets ranges and output files, `revision_changed`
  for Jira and Calendar continuations, and the documented source-call check order. Amends 0123,
  0128, 0130 and 0138.

If another branch took any of 0142 to 0147 first, p11 takes the next free numbers in the same
order and says so in its commit message.

## Manual steps

The step-by-step page is `docs/plugin-protocol-hardening-2-plan-manual-steps.html`, published at
the `manual_steps_artifact` URL.

- **Before:** `mb1-pr874-merged`. Merge PR #874, or decide to start with it open. This plan's branch
  is cut from #874's branch, so either works; with #874 open, this plan's PR shows #874's diff too.
- **After:**
  - `ma1-card-regions`: run the card browser test with `PRIVACYFENCE_CARD_SCREENSHOT_DIR` set, and
    look at the six screenshots it saves (a file tool card, a confirm card and an approval card, each
    in light and dark).
  - `ma2-reenable-own-plugins`: on your own install, every enabled plugin is disabled once by the
    state upgrade. Review and enable each again, and check the dialog shows the command and the
    folder hash.

## Risks and open questions

- **p1 (protocol version):** a non-Python plugin may enforce the 1.3 schema's
  `additionalProperties: false` on `tool.prepare` params. That is why D1 sends `args_digest` only
  from effective minor 4. If the echo or conformance tests fail on a digest mismatch, stop: the
  stored-digest path is wrong.
- **p3b (arguments):** an existing test or fixture may pass an undeclared or mistyped argument (the
  `today` example's tests, echo's `file_put`). Fix the test input, never the check. A built-in MCP
  client path that injects keys into a plugin tool's args (other than `reason`, already stripped)
  makes the "does not declare" rule fire on every call. In that case stop and report the key.
- **p4 (introspection):** if the Settings review button does not use the generic sensitive-action
  path, stop (D4).
- **p5 (folder walk):** on Windows, `%ProgramFiles%` child folders carry inherited entries. The
  per-file rule must use the same function as the executable today. If `platform-windows` fails on
  the trust tests, stop and report the DACL entry, rather than loosening the rule.
- **p7a (cards):** `approval_window_html.py` and `dialog_window_html.py` are shared by every
  connector. A block without `origin` must render byte-for-byte as before; the existing card
  snapshot tests prove it. If a snapshot of a built-in connector changes, stop.
- **p8 (Sheets):** if `get_sheet_values` returns something other than a list of lists, stop.
- **Plan B overlap:** Plan B edits `source_ops.py`, `spool.py`, `outputs.py`, `host.py`,
  `connector.py`, `blocks.py` and `rpc.py` too. Run this plan first. Whichever PR lands second
  merges `main` and resolves; neither plan's design depends on the other's code.

## Implementation manifest

Every code phase's brief ends with the same three rules:

- no `CHANGELOG.md` line (p11 writes them);
- no plan item or finding ids in code, comments or test names (`tests/unit/test_code_no_history.py`);
- run `ruff check .` and the phase's tests before finishing.

Phases that run at the same time share no path in `touches`.

```yaml
plan_slug: plugin-protocol-hardening-2
feature_branch: feature/plugin-protocol-hardening-2
tracking_issue: 846
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/NDh6xWxS2B6chdT19EVmYC
manual_steps_source: docs/plugin-protocol-hardening-2-plan-manual-steps.html
manual_before:
  - id: mb1-pr874-merged
    title: Merge PR 874 (plugin files and page links, protocol 1.3) into main, or accept that this PR carries its diff
    why: This plan's branch is cut from feature/plugin-files-and-page-links because the code it fixes exists only there. p0 merges origin/main; with 874 open, this plan's PR also shows 874's changes until 874 merges.
    done_when: "PR 874 shows Merged (preferred), or you have decided to start with it open. Either way `git fetch origin && git ls-tree origin/feature/plugin-files-and-page-links docs/ | grep -c plugin-files-and-page-links-plan` prints 0 (true at head 392e3ebc), and on main merged with that branch `ls docs/adr | cut -c1-4 | sort | uniq -d` prints nothing."
manual_after:
  - id: ma1-card-regions
    title: Look at the six card screenshots (file, confirm and approval cards, light and dark) that the card browser test saves
    why: CI asserts the HTML structure; only a person can judge that PrivacyFence's region and the plugin's region read as different things and that the badge is legible in both themes.
  - id: ma2-reenable-own-plugins
    title: On your own install, review and enable each plugin again after the state upgrade
    why: State v2 disables every enabled plugin once. This confirms the dialog shows Command and Folder SHA-256 and that a real plugin in the administrator-only folder passes the whole-folder check on your platform.
verify_after_merge:
  - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/web/test_routes_plugins.py tests/unit/web/test_routes_approvals.py tests/unit/web/test_routes_settings.py tests/unit/test_approvals.py tests/unit/test_settings_window_html.py tests/unit/test_gen_plugin_sdk_types.py tests/unit/test_code_no_history.py -q
  - python3 scripts/gen_plugin_sdk_types.py --check
  - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_plugin_approvals.py tests/integration/test_plugin_files.py tests/integration/test_plugin_refusals.py tests/integration/test_sdk_testhost_conformance.py -q
final_checks:
  - docs/plugin-protocol-hardening-2-plan.md and docs/plugin-protocol-hardening-2-plan-manual-steps.html are deleted and nothing links to them (grep -rn "plugin-protocol-hardening-2-plan" docs scripts src plugin-sdk README.md prints nothing)
  - ADRs 0142 to 0147 (or the numbers p11 took) exist with Status Accepted and their Amends lines, are in docs/adr/README.md's index, and every ADR they amend has the "Amended by" status line and index cell
  - CHANGELOG.md has the [Unreleased] lines from p11 and no new version heading
  - python3 scripts/gen_plugin_sdk_types.py --check exits 0
  - The full /dod passes, including python3 scripts/check_coverage_floor.py coverage.json (left to CI if the container cannot finish the coverage run; say so in the PR) and python3 -m pytest tests/integration -v with PRIVACYFENCE_TEST_CHROMIUM set and no plugin browser test SKIPPED
  - The PR's platform-windows and platform-macos jobs are green (the whole-folder trust check and device names run there)
  - python3 -m build plugin-sdk succeeds (delete plugin-sdk/dist and any build/ or *.egg-info afterwards)
  - "connector-live-check.yml: no phase changes a *_client.py file; if git diff --name-only origin/main...HEAD | grep -E 'src/privacyfence/[a-z_]+_client\\.py$' prints anything, dispatch connector-live-check.yml against the feature branch and link it"
phases:
  - id: p0-sync
    title: Merge main and list the plan document so the docs tests pass
    depends_on: []
    complexity: S
    touches:
      - docs/README.md
      - scripts/build_site.py
      - docs/plugin-protocol-hardening-2-plan.md
    brief: |
      1. `git fetch origin main && git merge --no-ff origin/main` into the phase branch (it is cut from the plan branch, which is cut from feature/plugin-files-and-page-links). Resolve conflicts by keeping both sides; if a conflict is in src/privacyfence/plugins/ or plugin-sdk/ and is not a trivial adjacency, stop with status=blocked and name the files.
      2. Run `python3 -m pytest tests/unit -q -k "docs or website or build_site"`. If a test fails because docs/plugin-protocol-hardening-2-plan.md is not listed, add it to docs/README.md's contributor list (one line, same format as the other plan entries were, see `git log -S"plugin-files-and-page-links-plan" -- docs/README.md` for the shape PR 874 used) and to `CONTRIBUTOR_DOCS` in scripts/build_site.py. Do only what the failing tests ask.
      3. If origin/main already has an ADR numbered 0142 or above, edit this plan's "ADRs" section to the next free numbers, in order, and say so in the commit message.
      4. Run `python3 -m pytest tests/unit -q` and `ruff check .`. No CHANGELOG line, no plan or finding ids in code.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - git log -1 --merges shows origin/main merged (or "Already up to date" recorded in the commit message of the listing commit)
  - id: p1-protocol-1-4
    title: Protocol 1.4.0 version handling and args_digest sent with tool.prepare
    depends_on: [p0-sync]
    complexity: M
    touches:
      - src/privacyfence/plugins/constants.py
      - src/privacyfence/plugins/protocol.py
      - src/privacyfence/plugins/connector.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/plugins/supervisor.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/types.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
      - docs/plugin-protocol/protocol.schema.json
      - examples/plugins/today/README.md
      - tests/unit/plugins/test_protocol.py
      - tests/unit/plugins/test_constants.py
      - tests/unit/plugins/test_host.py
      - tests/unit/plugins/test_connector.py
      - tests/unit/plugins/test_supervisor.py
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/unit/plugin_sdk/test_testhost.py
    brief: |
      Implement Design D1 exactly. Existing assertions of "1.3.0" (tests/unit/plugins/test_constants.py:170, tests/unit/plugins/test_host.py:331) and the text "protocol 1.3.0" in examples/plugins/today/README.md:41 become 1.4.0.
      1. constants.py: PROTOCOL_VERSION = "1.4.0". SDK: the same constant (grep PROTOCOL_VERSION in plugin-sdk/src).
      2. protocol.py InitializeResult.from_wire: refuse a protocol_version not fullmatching [0-9]+\.[0-9]+\.[0-9]+ with RpcError("invalid_params", "protocol_version is not MAJOR.MINOR.PATCH"); add property `minor` (int of the second part). supervisor.py needs no change beyond what the existing _Fatal("manifest invalid: …") path already does; check test_supervisor covers "1.banana" -> disabled with reason starting "manifest invalid".
      3. host.py on_ready: pass protocol_minor=min(result.minor, 4) to PluginConnector. connector.py: new keyword-only __init__ argument protocol_minor: int = 3 stored on self; in _prepare add params["args_digest"] = args_digest(args) only when self._protocol_minor >= 4.
      4. SDK plugin.py: the prepared entry gains an args field; at prepare, if params has a str "args_digest" fullmatching sha256:[0-9a-f]{64}, store it as the entry's digest, else compute args_digest(args) as today; at execute compare given digest == stored digest (strings) and json.dumps(args, sort_keys=True) of given and stored args; mismatch -> the existing digest_mismatch error. Announce 1.4.0 in the initialize result.
      5. testing/_host.py: send args_digest in tool.prepare params exactly like the daemon (always, the host is 1.4).
      6. protocol.schema.json: x-protocol-version "1.4.0"; ToolPrepareParams gains optional args_digest with the DIGEST_RE pattern; run `python3 scripts/gen_plugin_sdk_types.py` to regenerate types.py, then `--check`.
      7. Tests: test_protocol.py (version format refused, minor property); test_connector.py (args_digest present in prepare params at minor 4, absent at minor 3); test_supervisor.py ("1.banana" disables with "manifest invalid"); SDK test_plugin.py (stored digest from prepare used; a different digest at execute -> digest_mismatch; args changed -> digest_mismatch; a prepare without args_digest still works); test_testhost.py (host sends it).
      Stop with status=blocked if test_sdk_testhost_conformance.py or the echo integration tests fail on a digest mismatch: the stored-digest path is wrong.
      No CHANGELOG line; no plan or finding ids in code or test names; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_protocol.py tests/unit/plugins/test_connector.py tests/unit/plugins/test_supervisor.py tests/unit/plugin_sdk -q passes
      - python3 scripts/gen_plugin_sdk_types.py --check exits 0
      - grep -n '"1.4.0"' src/privacyfence/plugins/constants.py plugin-sdk/src/privacyfence_plugin_sdk/*.py prints at least two lines
      - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py tests/integration/test_plugin_framework.py -q passes
  - id: p2-human-session-plugin-cards
    title: Plugin confirmation and approval cards always need a human session
    depends_on: [p0-sync]
    complexity: S
    touches:
      - src/privacyfence/approvals.py
      - src/privacyfence/web/routes_approvals.py
      - src/privacyfence/plugins/confirm.py
      - src/privacyfence/plugins/approvals.py
      - tests/unit/web/test_routes_approvals.py
      - tests/unit/plugins/test_confirm.py
      - tests/unit/plugins/test_approvals.py
      - tests/unit/test_approvals.py
    brief: |
      Implement Design D2 exactly.
      1. approvals.py: PendingApproval gains `human_only: bool = False`; register_confirm gains keyword `human_only: bool = False` stored on the approval; add the one docstring sentence from D2.
      2. plugins/confirm.py:117 and plugins/approvals.py:301: register_confirm(sensitive=parsed.require_step_up, notify=True, human_only=True).
      3. web/routes_approvals.py decide (around line 683-697): add `human_confirm = approval is not None and approval.human_only and result == CONFIRM_RESULTS[0]`; guard when `result in _STEP_UP_RESULTS or sensitive_confirm or human_confirm`; `what` is "approve a plugin's request" for human_confirm. Leave approval_step_up.guard_decision unchanged. The batch decide route already runs human_session_guard for its results ("accept", "deny"), so it needs no change; add one test that a plugin confirm card decided through the batch route from an unattested session is refused.
      4. Tests (copy the session set-up of the existing unattested-session tests in test_routes_approvals.py): a plugin confirm card and a plugin approval card, each with require_step_up false and true, decided `confirm` from (a) an unattested session -> refused with the human-session response and the approval not stored, (b) a human session with step-up off -> 200. The existing test_the_always_allow_dialog_is_untouched must still pass (human_only stays False for the PII and "Always allow" dialogs). tests/integration/test_plugin_approvals.py does not drive the decide route; the unit tests are the coverage.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_approvals.py tests/unit/plugins/test_confirm.py tests/unit/plugins/test_approvals.py tests/unit/test_approvals.py -q passes
      - "a test in tests/unit/web/test_routes_approvals.py posts result=confirm for a plugin card with require_step_up false from an unattested session and asserts a non-200 status and that the plugin approval store is empty"
  - id: p3a-tool-definitions
    title: Tool-definition and scope-type checks as documented, constraints shown to the AI, introspection refusals
    depends_on: [p1-protocol-1-4]
    complexity: M
    touches:
      - src/privacyfence/plugins/tools.py
      - src/privacyfence/plugins/constants.py
      - src/privacyfence/plugins/connector.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/plugins/supervisor.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/_files.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
      - tests/unit/plugins/test_tools.py
      - tests/unit/plugins/test_host.py
      - tests/unit/plugins/test_supervisor.py
      - tests/unit/plugins/test_connector.py
      - tests/unit/plugins/test_constants.py
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/unit/plugin_sdk/test_testhost.py
    brief: |
      Implement the "Tool-definition rules" and "Constraints the AI can see" parts of Design D3 exactly (names, constants, messages are in D3).
      1. constants.py: PARAM_NAME_RE, MAX_PARAMS_PER_TOOL = 32, MAX_ARG_STRING_CHARS = 65_536. The SDK keeps its copies of limits in plugin-sdk/src/privacyfence_plugin_sdk/_files.py (next to MAX_FILE_BYTES); add the three there and extend the existing daemon/SDK constants parity test.
      2. tools.py _check_parameters: name pattern, count, allowed keys, min/max rules, description cap, with the exact D3 messages; validate_scope_types reserves "anything" ("scope type {name} is reserved", also for output).
      3. host.py _validate: call validate_scope_types on the result's scope types (convert to the list-of-dicts shape it takes) before validate_tool_defs, so a failure raises ToolDefError -> "manifest invalid". supervisor.py introspect: add approval.request, approval.check, approval.await, confirm.await to the refuse map.
      4. connector.py _tool_spec: append the constraint phrases from D3 to each parameter's description.
      5. SDK: the same tool-definition rules in both places the SDK checks parameters: tool registration in plugin.py and testing/_host.py's _check_parameters (its _FORBIDDEN_PARAM_KEYS at :71 becomes the D3 allowed-key rule); Plugin.scope_type refuses "output" and "anything".
      6. Tests: test_tools.py rows for each new refusal and that a refusal never echoes a bad parameter name (assert "\n" not in detail for a name holding a newline); test_host.py: 21 scope types and a scope type named "anything" disable the plugin with a reason starting "manifest invalid"; test_supervisor.py: introspection answers introspection_only for approval.request; test_connector.py: a parameter with maxLength 10 and minimum/maximum shows the phrases in its MCP description; SDK test_plugin.py/test_testhost.py mirror rows.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk -q passes
      - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_plugin_refusals.py -q passes
  - id: p3b-arguments
    title: Check the AI's arguments against the reviewed schema in the daemon, the SDK and the test host
    depends_on: [p3a-tool-definitions]
    complexity: M
    touches:
      - src/privacyfence/plugins/arguments.py
      - src/privacyfence/plugins/connector.py
      - plugin-sdk/src/privacyfence_plugin_sdk/_args.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
      - examples/plugins/today/**
      - tests/unit/examples/**
      - tests/fixtures/plugins/**
      - tests/unit/plugins/test_arguments.py
      - tests/unit/plugins/test_connector.py
      - tests/unit/plugin_sdk/test_args.py
      - tests/unit/plugin_sdk/test_testhost_params.py
      - tests/unit/plugin_sdk/test_sdk_files_parity.py
      - tests/integration/test_plugin_framework.py
      - tests/integration/test_sdk_testhost_conformance.py
    brief: |
      Implement the "Argument check" part of Design D3 exactly (the table, the file-parameter skip, the float-to-int rule, None for optional parameters).
      1. New src/privacyfence/plugins/arguments.py with check_args(defn, args, *, title) -> dict. connector.call: drop None values of optional parameters from plugin_args, then plugin_args = check_args(...) before the prepare key is built and before _reuse_or_prepare.
      2. New plugin-sdk/src/privacyfence_plugin_sdk/_args.py with the same function, messages and file-parameter skip; the SDK prepare path calls it and answers RpcError("invalid_params", message).
      3. testing/_host.py call_tool: replace its required-argument check with _args.check_args, raising ValueError with the same message.
      4. Fix any example, fixture or test input that sent undeclared or mistyped arguments (fix the input, not the check). echo's file_put (required file parameter) and the today example's add_note with a string event_id must keep working.
      5. Tests: tests/unit/plugins/test_arguments.py and tests/unit/plugin_sdk/test_args.py with the same parametrised table (one row per D3 table line, plus None-for-optional, 5.0 for an integer accepted as 5, 5.5 refused, a required file parameter absent from args accepted by check_args, a valid call); test_connector.py: an undeclared key, a list for a string and a missing required argument raise ValueError and the plugin receives no tool.prepare; a parity test in test_sdk_files_parity.py asserting daemon and SDK check_args give the same message or the same returned dict for every row.
      Stop with status=blocked if a built-in path adds keys other than reason to a plugin tool's args (the "does not declare" rule would fire on every call); name the key.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/examples -q passes
      - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_plugin_files.py tests/integration/test_plugin_refusals.py tests/integration/test_sdk_testhost_conformance.py -q passes
      - "python3 -c 'import privacyfence.plugins.arguments as a; print(a.check_args.__name__)' prints check_args"
  - id: p4-manifest-review-and-names
    title: Introspection is sensitive; command arguments stay inside the plugin; reserved device and display names; Command on the review dialog
    depends_on: [p3b-arguments]
    complexity: M
    touches:
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/plugins/manifest.py
      - src/privacyfence/plugins/constants.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/settings_window_html.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/plugins/test_manifest.py
      - tests/unit/plugins/test_constants.py
      - tests/unit/plugins/test_host.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/integration/test_plugin_settings_browser.py
    brief: |
      Implement Design D4 exactly.
      1. routes_settings.py: move "inspect_plugin" from _NON_SENSITIVE_ACTIONS to _SENSITIVE_ACTIONS; replace its comment with "Introspection runs the plugin's code as the service account (ADR 0144)." (the ADR is written in the last phase; the number is fixed by this plan). TestSensitiveActionsCoverAllAllowedActions must still pass. Confirm the Settings review button posts through the generic action handler that does the step-up round trip (settings_window_html.py); if it does not, stop with status=blocked.
      2. manifest.py load_manifest: refuse command[1:] elements per D4 with the exact message.
      3. constants.py RESERVED_PLUGIN_NAMES and SDK plugin.py _RESERVED_PLUGIN_NAMES: add the Windows device names from D4; add a test asserting the two sets are equal (put it next to the existing SDK/daemon constants parity test).
      4. manifest.py: _RESERVED_DISPLAY_NAMES and the display_name refusal per D4.
      5. host.py inspect: add "command": list(manifest.command) to the review dict. settings_window_html.py: a "Command" row in the review dialog, escaped with esc(), joined by spaces.
      6. Tests: test_routes_settings.py (inspect_plugin from an unattested session is refused like enable_plugin; with step-up on it gets 428); test_manifest.py (each command[1:] shape refused: "/x", "~/x", "C:\\x", "\\\\host\\x", "../x", "a/../../x"; "lib/x.py" and "--flag" accepted; each reserved display name refused case-insensitively, "Calendar Helper" accepted); test_constants.py (con, nul, com1, lpt9 refused as names); test_settings_window_html.py (Command row present and escaped); test_plugin_settings_browser.py runs with step-up off and a human session, so Review needs no passkey there; if it fails with 428 or 403 on Review, stop with status=blocked.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/plugins/test_manifest.py tests/unit/plugins/test_constants.py tests/unit/plugins/test_host.py tests/unit/test_settings_window_html.py tests/unit/plugin_sdk -q passes
      - PRIVACYFENCE_TEST_CHROMIUM set, python3 -m pytest tests/integration/test_plugin_settings_browser.py -q passes with nothing skipped
      - grep -n '"inspect_plugin"' src/privacyfence/web/routes_settings.py shows it only inside _SENSITIVE_ACTIONS
  - id: p5-whole-folder-trust-and-tmp
    title: Check and hash the whole plugin folder, check the shebang interpreter, state v2, per-plugin TMPDIR
    depends_on: [p4-manifest-review-and-names]
    complexity: M
    worker_model: opus
    worker_model_reason: A security check that walks a filesystem tree on three platforms (POSIX modes, Windows DACLs, links and junctions) and a state-file migration that must fail closed; mistakes either lock out every plugin or let a writable file through.
    touches:
      - src/privacyfence/plugins/trust.py
      - src/privacyfence/plugins/state.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/plugins/storage.py
      - src/privacyfence/plugins/supervisor.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/plugins/test_trust.py
      - tests/unit/plugins/test_state.py
      - tests/unit/plugins/test_host.py
      - tests/unit/plugins/test_storage.py
      - tests/unit/plugins/test_supervisor.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/test_settings_window_html.py
      - tests/fixtures/plugins/echo/harness.py
      - tests/integration/test_plugin_framework.py
    brief: |
      Implement Design D5 exactly (messages, constants and file layout are in D5).
      1. trust.py: extend _paths_to_check to every directory and regular file under the plugin folder (os.scandir, follow_symlinks=False); refuse links and junctions (on Windows also any entry whose st_file_attributes has FILE_ATTRIBUTE_REPARSE_POINT); PLUGIN_MAX_FILES = 20_000; directories use the plugin-dir rule, files the executable rule (the same functions trust.py already uses for those two). Add folder_sha256 and DiscoveredPlugin.files_sha256, computed in _inspect only after the checks pass. Add the POSIX shebang check from D5.
      2. state.py: STATE_VERSION = 2; _parse accepts versions 1 and 2; PluginRecord.files_sha256; a version-1 file loads with files_sha256 = "" and check_hashes treats "" (or any difference) as drift; writes are version 2. HASH_DRIFT_REASON and supervisor.py's _STOP_REASONS["hash_changed"] become "plugin files changed, enable again"; update tests that pin the old text.
      2b. trust.py discover_one(plugins_dir, name, trust_check=...) inspecting one plugin; host.py before_spawn uses it. The link, file-count and interpreter refusals are shown verbatim as the problem; permission problems keep NOT_ADMIN_ONLY (D5 "Which text the owner sees").
      3. host.py: review dict gains "files_sha256"; enable takes files_sha256 and compares it with the review and the disk exactly like the other two; routes_settings.py passes it from the enable payload; settings_window_html.py shows "Folder SHA-256" and sends it with the enable action.
      4. storage.py: tmp_dir(name), created 0700 by ensure_dirs, removed by remove_all; supervisor.py child_env(name) sets TMPDIR, TEMP and TMP to it and no longer copies the daemon's values; the supervisor empties it (rmtree, recreate 0700) before each spawn. Update every caller of child_env.
      5. The test harness (tests/fixtures/plugins/echo/harness.py) uses a trust_check stub, so the whole-folder check is exercised in test_trust.py with tmp dirs: add cases for a world-writable sibling file (POSIX, skip on Windows), a symlink inside the folder, a nested directory, 20_001 files (use a monkeypatched PLUGIN_MAX_FILES = 3 instead of creating files), folder_sha256 changing when a sibling changes and staying when nothing changes, a shebang naming a world-writable interpreter, /usr/bin/env python3 resolved through PATH.
      6. test_state.py: a version-1 file disables the enabled plugin with HASH_DRIFT_REASON; round trip of version 2. test_host.py: enable refuses a mismatched files_sha256 with CHANGED_SINCE_REVIEW. test_supervisor.py/test_storage.py: TMPDIR/TEMP/TMP point at the plugin's tmp dir and it is empty at each start.
      Stop with status=blocked if platform-windows fails on trust tests because of an inherited DACL entry on a file under the plugin folder; report the entry instead of loosening the rule.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/web/test_routes_settings.py tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_plugin_harness.py -q passes
      - grep -n "STATE_VERSION = 2" src/privacyfence/plugins/state.py prints one line
  - id: p6-invisible-text
    title: Strip format and private-use characters from everything a plugin shows or returns; unpublish such output names
    depends_on: [p3b-arguments]
    complexity: M
    touches:
      - src/privacyfence/plugins/blocks.py
      - src/privacyfence/plugins/outputs.py
      - plugin-sdk/src/privacyfence_plugin_sdk/blocks.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_outputs.py
      - tests/unit/plugins/test_blocks.py
      - tests/unit/plugins/test_outputs.py
      - tests/unit/plugin_sdk/test_blocks.py
      - tests/unit/plugin_sdk/test_outputs_parity.py
    brief: |
      Implement Design D6 exactly.
      1. blocks.py: rewrite clean_text per D6 (categories Cc, Cf, Co, Zl, Zp removed except \n and \t, then NFC); drop _STRIP_RE; clean_line unchanged in behaviour otherwise; add clean_json(value). The SDK blocks.py gets the identical clean_text/clean_line (copy, same docstring sentence).
      2. outputs.py list_outputs skips, and _canonical_file refuses ("No such output file"), a path segment with clean_line(segment) != segment. testing/_outputs.py mirrors it. The SDK's OutputsClient.publish (plugin.py) gets the same check in p9, because plugin.py is changed by p4 in this wave.
      3. Tests: test_blocks.py and SDK test_blocks.py share a table: U+200B, U+200D, U+2060, U+FEFF, U+00AD, U+E0041, U+2028, U+202E, U+E000 removed; "é" as e + U+0301 becomes U+00E9; \n and \t kept; a test that "4111​1111​1111​1111" (with U+200B) becomes "4111111111111111"; test_outputs.py: "evil\u202egnp.csv", "a\nb.csv", "zero\u200bwidth.csv" are neither listed nor readable; a parity test (new tests/unit/plugin_sdk/test_outputs_parity.py) that the daemon and test host agree on those names.
      The PII scan of write results is wired in p7 (connector.py); do not edit connector.py here.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_blocks.py tests/unit/plugins/test_outputs.py tests/unit/plugin_sdk -q passes
      - "tests/unit/plugins/test_blocks.py has a case asserting clean_text('a' + chr(0x200B) + 'b' + chr(0xE0041) + 'c') == 'abc', and it passes"
  - id: p7a-card-regions
    title: PrivacyFence's region and the plugin's region on every plugin card, and the installed name on tool, confirm and approval cards
    depends_on: [p2-human-session-plugin-cards, p5-whole-folder-trust-and-tmp, p6-invisible-text]
    complexity: M
    touches:
      - src/privacyfence/plugins/blocks.py
      - src/privacyfence/plugins/connector.py
      - src/privacyfence/plugins/confirm.py
      - src/privacyfence/plugins/approvals.py
      - src/privacyfence/approval_window_html.py
      - src/privacyfence/dialog_window_html.py
      - tests/unit/plugins/test_blocks.py
      - tests/unit/plugins/test_connector.py
      - tests/unit/plugins/test_confirm.py
      - tests/unit/plugins/test_approvals.py
      - tests/unit/test_approval_window_html.py
      - tests/unit/test_dialog_window_html.py
      - tests/integration/test_plugin_card_escaping_browser.py
      - tests/integration/test_plugin_files.py
    brief: |
      Implement the "Regions on the card", "Rendering" and "The installed name on tool cards" parts of Design D7 exactly.
      1. blocks.py to_card_blocks gains keyword origin (default "plugin") and sets it on every returned block.
      2. connector.py _gate: card blocks = to_card_blocks(file_blocks, origin="privacyfence") + to_card_blocks(prepared.preview + payload, origin="plugin"); remove files.plugin_heading() from that list; preview={"Plugin": f"{self.display_name} ({self._plugin})", "Tool": title}.
      3. approval_window_html.py: group consecutive blocks by origin into the two section wrappers with the exact classes, aria-labels and labels from D7; blocks without origin render exactly as before. Add the D7 CSS using existing tokens. dialog_window_html.py build_confirmation_html and the plugin approval card: PrivacyFence region with "Plugin: <display> (<name>)" (escaped), the plugin preview in the plugin region; plugins/confirm.py and plugins/approvals.py pass the installed name.
      4. Tests: test_approval_window_html.py (a card with both origins has both sections in order; a built-in connector card's HTML is unchanged against the existing expectation); test_dialog_window_html.py (installed name row); test_connector.py (preview carries "Echo (echo)"-style name). test_plugin_card_escaping_browser.py: assert the plugin region is a different element from PrivacyFence's region; add test_plugin_card_screenshots: it renders an echo file_put card, an echo confirm card and an echo approve card, and when the environment variable PRIVACYFENCE_CARD_SCREENSHOT_DIR is set it saves full-page PNGs of each in light and dark (page.emulate_media(color_scheme=...)) named file-light.png, file-dark.png, confirm-light.png, confirm-dark.png, approval-light.png, approval-dark.png; when it is unset it still renders and asserts both regions exist, saving nothing.
      Stop with status=blocked if a built-in connector's card snapshot changes.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/test_approval_window_html.py tests/unit/test_dialog_window_html.py -q passes
      - PRIVACYFENCE_TEST_CHROMIUM set, python3 -m pytest tests/integration/test_plugin_card_escaping_browser.py tests/integration/test_plugin_files.py tests/integration/test_plugin_approval_frame_browser.py -q passes with nothing skipped
  - id: p7b-card-refusals-and-gate
    title: Stronger look-alike refusal, write card for review-gated writes, foreign approval ids dropped, cleaned write results
    depends_on: [p7a-card-regions]
    complexity: M
    touches:
      - src/privacyfence/plugins/connector.py
      - src/privacyfence/plugins/files.py
      - plugin-sdk/src/privacyfence_plugin_sdk/_files.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
      - tests/unit/plugins/test_connector.py
      - tests/unit/plugins/test_files.py
      - tests/unit/plugin_sdk/test_sdk_files.py
      - tests/unit/plugin_sdk/test_sdk_files_parity.py
      - tests/integration/test_plugin_card_escaping_browser.py
    brief: |
      Implement the "Look-alike refusal", "A write on the review gate gets the write card" and "Foreign approval ids" parts of Design D7, and D6's write-result cleaning, exactly.
      1. files.py: rename refuse_reserved_labels to refuse_reserved_text with the D7 rules (NFKC of clean_text, casefold, strip; fields labels, the three reserved headings, a table's first column header or cells); it still applies only to tools with a file parameter. SDK _files.py, plugin.py and testing/_host.py: the same rename and rules.
      2. connector.py: effective_gate mapping in _gate and _dynamic_spec; _execute drops an approval_id that owns_approval does not confirm, on both paths, with the D7 log line; _screen_write_result applies blocks.clean_json first and returns the cleaned value.
      3. Tests: test_files.py (each look-alike shape refused for a file tool: heading copy of each reserved heading, NFKC-equal label "ＦＩＬＥ", a label with U+200B inside, a table with a reserved first-column cell; a non-file tool may use "File" as a label); test_connector.py (a review-gated non-read-only tool goes through gated_call with gate "popup"; a foreign approval_id is dropped on the non-withheld path; a write result holding a card number with U+200B inside is withheld by the PII check); SDK parity test for refuse_reserved_text; test_plugin_card_escaping_browser.py: a file tool whose preview has a heading "Checked by PrivacyFence" is refused before any card is shown.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk -q passes
      - grep -rn "refuse_reserved_labels" src plugin-sdk/src prints nothing
      - PRIVACYFENCE_TEST_CHROMIUM set, python3 -m pytest tests/integration/test_plugin_card_escaping_browser.py tests/integration/test_plugin_files.py -q passes with nothing skipped
  - id: p8-source-and-tools-limits
    title: tools.changed rate limit, Sheets range cap with an indexed snapshot, revision_changed for Jira and Calendar continuations
    depends_on: [p7b-card-refusals-and-gate]
    complexity: M
    touches:
      - src/privacyfence/plugins/connector.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/plugins/source_ops.py
      - src/privacyfence/plugins/spool.py
      - src/privacyfence/plugins/constants.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_source.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_cursors.py
      - tests/unit/plugins/test_connector.py
      - tests/unit/plugins/test_host.py
      - tests/unit/plugins/test_source_ops.py
      - tests/unit/plugins/test_spool.py
      - tests/unit/plugin_sdk/test_testhost_surfaces.py
      - tests/integration/test_plugin_paging.py
    brief: |
      Implement Design D8 exactly.
      1. constants.py: TOOLS_CHANGED_PER_MINUTE = 10, SHEETS_MAX_RANGE_BYTES = 64 * 1024 * 1024.
      2. connector.py handle_tools_changed returns "accepted" | "unchanged" | "rejected" | "dropped" per D8: the rate check first (every outcome counts), then the raw-list equality check before validation, the one audit line per window; host.py tools_changed handler calls _notify_tools only for accepted and _changed only for accepted or rejected.
      3. source_ops.py _run_sheets: per-row encoded lengths, cap check with the exact RpcError, prefix by cumulative sum; spool.py put_rows(plugin, rows, encoded) writing lines in one pass with an array("q") offset index in _RowsEntry; rows_page seeks by offset. Keep the existing snapshot limits (4 per plugin) and expiry.
      4. source_ops.py Jira and Calendar continuation: state keys {t, k, h} when k > 0 and {t, k} when k == 0, the revision_changed RpcError, anything else "cursor is not valid". testing/_source.py (it pages these two operations, _PAGED_SAMPLES) and testing/_cursors.py mirror the rule.
      5. Tests: test_connector.py/test_host.py (11 different lists in a minute -> 10 applied, the 11th dropped, one "tools changes dropped" audit line; 20 identical lists -> one audit line, one listener call, validate_tool_defs called once, and the 11th onwards dropped); test_source_ops.py (a range just over the cap -> payload_too_large with reason range_too_large and nothing spooled; an order change between pages -> revision_changed for jira and calendar; a shrunk page -> revision_changed); test_spool.py (rows_page reads only its slice: patch the file read to count bytes, assert it is under 2x the page); test_plugin_paging.py: update expectations for the new continuation behaviour only where the old ones asserted re-fetch-and-skip.
      Stop with status=blocked if get_sheet_values returns anything but a list of row lists.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk -q passes
      - python3 -m pytest tests/integration/test_plugin_paging.py tests/integration/test_plugin_framework.py -q passes
  - id: p9-output-limits
    title: Output file size and count limits, digest cache, concurrent read cap
    depends_on: [p8-source-and-tools-limits]
    complexity: M
    touches:
      - src/privacyfence/plugins/outputs.py
      - src/privacyfence/plugins/constants.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/_files.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_outputs.py
      - tests/conftest.py
      - tests/unit/plugins/test_outputs.py
      - tests/unit/plugin_sdk/test_outputs_parity.py
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/integration/test_plugin_outputs.py
    brief: |
      Implement Design D9 exactly.
      1. constants.py: OUTPUT_MAX_FILE_BYTES = 256 * 1024 * 1024, OUTPUT_MAX_FILES = 10_000; mirror in the SDK's limit constants in _files.py and the parity test.
      2. outputs.py: list limit with "limited": true and the exact message; too_large flag and the read refusal message; the module-level _DigestCache passed to read_output as in D9, cleared in tests/conftest.py; asyncio.Semaphore(2) per plugin around the read's to_thread.
      3. SDK OutputsClient.publish: refuse over-size data and the 10,001st file with ValueError; also refuse a relpath with a segment for which clean_line(segment) != segment, with ValueError("output path holds a control or invisible character") (import clean_line from the SDK's blocks module). testing/_outputs.py mirrors the size and count limits.
      4. Tests: test_outputs.py (OUTPUT_MAX_FILE_BYTES and OUTPUT_MAX_FILES monkeypatched small: listing stops and says so, a too-large file is listed with too_large and its read refused; reading 5 pages of one file hashes it once: patch sha256_file or the hashing helper and count calls; a changed mtime re-hashes); SDK test_plugin.py (publish refusals); parity test extended.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_outputs.py tests/unit/plugin_sdk -q passes
      - python3 -m pytest tests/integration/test_plugin_outputs.py -q passes
  - id: p10-test-host-parity
    title: Make the SDK test host refuse, cap and wrap like the daemon; conformance scenarios for every refusal
    depends_on: [p9-output-limits]
    complexity: M
    touches:
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_host.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_confirm.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_approvals.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/_pages.py
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/__init__.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - tests/fixtures/plugins/echo/echo_plugin.py
      - tests/fixtures/plugins/echo/harness.py
      - tests/unit/plugin_sdk/test_testhost.py
      - tests/unit/plugin_sdk/test_testhost_surfaces.py
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/integration/test_sdk_testhost_conformance.py
    brief: |
      Implement Design D10 exactly.
      1. testing/_host.py: via "card"; non-dict result wrapping; unattended attribute; 2,048-byte cap with the daemon's withheld shape (copy the message constant from connector.py's WRITE_RESULT_WITHHELD text into the SDK testing module and add a parity test that the two strings are equal); write_result_pii keyword.
      2. testing/_confirm.py and _approvals.py: unattended -> confirmation_refused reason unattended_session; 8 pending per plugin -> confirmation_refused reason too_many_pending (use the same data.reason strings as plugins/confirm.py; grep them).
      3. testing/_pages.py: SECURITY_HEADERS equal to the daemon's header dict in plugins/pages.py (add COOP and Permissions-Policy); a parity test comparing the full dicts.
      4. SDK plugin.py and testing/_host.py: title and effect need at least one character.
      5. echo_plugin.py: add what the new conformance scenarios need (a tool that asks for a confirmation, a write tool returning a non-dict result with an approval id, a tool whose prepare can return an invalid preview on demand), each driven by an argument, following the file's existing pattern.
      6. tests/integration/test_sdk_testhost_conformance.py: one scenario per refusal listed in D10, each run against the daemon stack (harness Stack) and the test host, asserting the same code and data.reason.
      No CHANGELOG line; no plan or finding ids; ruff check . passes.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk -q passes
      - python3 -m pytest tests/integration/test_sdk_testhost_conformance.py tests/integration/test_plugin_framework.py -q passes
      - "grep -c 'def test_' tests/integration/test_sdk_testhost_conformance.py is at least 7 higher than at the phase start"
  - id: p11-docs-adrs-retire
    title: Reference docs, schema limits and validators test, ADRs 0142-0147, changelog, retire the plan
    depends_on: [p2-human-session-plugin-cards, p10-test-host-parity]
    complexity: M
    touches:
      - docs/plugin-protocol.md
      - docs/plugin-protocol/protocol.schema.json
      - docs/plugins.md
      - docs/security-and-compliance.md
      - docs/adr/**
      - docs/README.md
      - scripts/build_site.py
      - scripts/gen_plugin_sdk_types.py
      - plugin-sdk/src/privacyfence_plugin_sdk/types.py
      - plugin-sdk/README.md
      - src/privacyfence/plugins/spool.py
      - tests/unit/plugins/test_protocol.py
      - tests/unit/plugins/test_protocol_doc.py
      - tests/unit/plugins/test_schema_validators.py
      - tests/unit/test_gen_plugin_sdk_types.py
      - pyproject.toml
      - requirements/*.lock.txt
      - src/privacyfence/web/routes_settings.py
      - CHANGELOG.md
      - docs/plugin-protocol-hardening-2-plan.md
      - docs/plugin-protocol-hardening-2-plan-manual-steps.html
    brief: |
      Implement Design D11 and the ADRs section.
      1. scripts/gen_plugin_sdk_types.py: generate x-limits from privacyfence.plugins.constants (every number in the doc's limits table, the new ones from D3, D5, D8, D9 included) inside the same run and --check; test_protocol.py asserts x-limits equals the constants both ways. Regenerate types.py.
      2. protocol.schema.json fixes from D11; new tests/unit/plugins/test_schema_validators.py per D11 (add "jsonschema>=4.18,<5" to the test extra in pyproject.toml and run scripts/update_dependency_locks.sh, which needs uv; commit the regenerated requirements/*.lock.txt; if the script fails, stop with status=blocked).
      3. docs/plugin-protocol.md, docs/plugins.md, plugin-sdk/README.md, docs/security-and-compliance.md: every change listed in D11, in the docs' present-tense reference style (no history, no plan ids: tests/unit/test_docs_no_history.py); spool.py module docstring: drop "a spool directory the plugin cannot see" for "an owner-only directory under the data root".
      4. If p0 or this phase had to renumber the ADRs, update the "ADR 0144" in the inspect_plugin comment in src/privacyfence/web/routes_settings.py to the number actually taken.
      4b. ADRs 0142-0147 from the "ADRs" section (template in docs/adr/README.md), each Accepted with Amends lines; "Amended by" status lines and index cells on 0121, 0122, 0123, 0126, 0127, 0128, 0130, 0131, 0137, 0138, 0141 as listed; index rows. Each ADR's Context cites the finding as a plain description (no F-ids) and links source files, never the plan.
      5. CHANGELOG.md under ## [Unreleased]: Security: plugin confirmation and approval cards need a human session; Changed: plugin protocol 1.4 (argument checks, args digest at prepare, limits), whole-folder plugin checks (enabled plugins must be reviewed and enabled again once; a downgraded PrivacyFence keeps them disabled), cards show PrivacyFence's region and the installed plugin; one line each, in the section's existing style.
      6. Delete docs/plugin-protocol-hardening-2-plan.md and docs/plugin-protocol-hardening-2-plan-manual-steps.html; remove what p0 added to docs/README.md and scripts/build_site.py.
      7. Run the full tests/unit suite, ruff, bandit, mypy strict modules and gen --check.
    acceptance:
      - python3 -m pytest tests/unit -q passes
      - python3 scripts/gen_plugin_sdk_types.py --check exits 0
      - grep -rn "plugin-protocol-hardening-2-plan" docs scripts src plugin-sdk README.md prints nothing
      - ls docs/adr | grep -E '^014[2-7]-' prints six files
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
