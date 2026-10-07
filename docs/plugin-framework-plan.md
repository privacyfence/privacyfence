# Plan: plugin framework

## Goal

Give PrivacyFence a versioned interface through which a separately built and separately released
program, a **plugin**, extends it: [privacyfence/privacyfence#846](https://github.com/privacyfence/privacyfence/issues/846).
The first consumer is a data-lake plugin in its own repository, but nothing here is specific to it,
and PrivacyFence learns no data-lake concept.

A plugin is its own executable in an administrator-only plugins directory. The daemon starts it as
a child process and talks JSON-RPC 2.0 to it over stdin and stdout. Through that channel a plugin
can:

1. read from a connected service on behalf of a principal, with no approval gate, audited, and
   without ever holding a token (`source.call`);
2. contribute MCP tools, which PrivacyFence gates exactly like connector tools, in two steps:
   `tool.prepare` returns what the call would release or do, PrivacyFence applies the gate, and
   only then does it send `tool.execute`. A read releases the prepared payload itself, so the AI
   gets exactly what the human saw;
3. ask a human to confirm something on a card that no rule can auto-accept (`confirm.request`);
4. serve read-only pages at `GET /plugins/<name>/…` under a sandbox CSP;
5. keep its own data in directories owned by the service account, where the AI client cannot read
   it;
6. learn the principal; the org-mode fields (roles, service sign-ins) are reserved in the protocol
   and rejected in local mode.

The work also ships:

- `privacyfence-plugin-sdk`, a zero-dependency Python package under `plugin-sdk/`, published to
  PyPI from the same release tag. It includes a public in-memory test host,
  `privacyfence_plugin_sdk.testing`.
- `echo`, a test-only reference plugin (`tests/fixtures/plugins/echo/`) that exercises every method
  end to end.
- `today`, an example plugin (`examples/plugins/today/`) that a person can build and install on a
  packaged PrivacyFence for a manual smoke test.

Org mode is specified here but not built. The plugin host does not start in org mode.

The design spec this plan was made from is the artifact "PrivacyFence Plugin Framework" (revision
2, with the data lake's requests R1 and R2). This document copies everything a worker needs, and
corrects the spec where the code differs from what the spec assumed. Workers do not need the
artifact.

## Current state

Measurements are against `main` at `ddcf8f9e`.

### Tools, gate and policy

- **Tool listing.** `web/routes_mcp.py:367` `handle_list_tools` lists
  `mcp_tools.to_mcp_tool(spec)` for every `spec` of every `connector` in
  `dispatcher.connectors.values()`, plus `META_TOOLS` (`web/mcp_tools.py:318`). Annotations come
  only from `ToolSpec.read_only` and `.destructive` (`web/mcp_tools.py:44-55`;
  `connector.py:28-41`). `McpDispatcher.notify_tools_changed` (`web/mcp_dispatch.py:163`) sends
  `tools/list_changed`.
- **Tool calls.** `_dispatch_connector_tool` (`web/routes_mcp.py:485`) finds the connector by
  scanning `tool_specs()` (`_connector_for_tool`, :494). Then `McpDispatcher.call`
  (`web/mcp_dispatch.py:199`) pops `reason`, dedupes on principal, connector, tool and args, wraps
  the call in `unattended_scope` (`gate.py:737`), `reason_scope` (`gate.py:780`) and
  `released_request_scope`, and runs `await connector.call(tool, args)`. `ApprovalPending` becomes
  the result.
- **The connector map.** `McpDispatcher` takes a `connectors_provider` callable that it calls on
  every dispatch (`web/mcp_dispatch.py:89`). In local mode `daemon_main.py:791-807` defines that
  closure (`_connectors`). For the local principal it returns `connector_host.connectors`.
  **A plugin that is wrapped as a `Connector` and merged into that map gets listing, dispatch,
  dedupe, reason handling and `tools/list_changed` with no change to `web/routes_mcp.py` or
  `web/mcp_dispatch.py`.** This plan does exactly that (D9).
- **`gated_call`** (`gate.py:799`) is keyword-only. It takes `connector, tool, tool_name, summary,
  sender, raw_data, filtered_data, gate` (`"review"` or `"popup"`), plus `preview` (a `dict` of
  label to value, shown as card rows), `preview_blocks` (typed blocks for the right-hand pane),
  `details_text`, `pii_scan_text`, `args` and more. It returns `filtered_data` on accept and raises
  `GateDeniedError` or `ApprovalPending` otherwise. The operation key is
  `TOOL_TO_OPERATION.get(tool, f"{connector}.{tool}")`. The card layout is
  `_TOOL_LAYOUT.get(tool, NARROW)` (`gate.py:254`). A review gate scans `pii_scan_text` for PII.
- **Writes** call `gated_call(gate="popup", filtered_data=None)` and then do the write
  (`connectors/tasks.py:263-276`). Auto tools write their own audit entry; the pattern to copy is
  `connectors/tasks.py:447` `_auto_audit`.
- **Static tables.** Nothing registers tools at runtime. These are module-level dicts built at
  import:
  - `auto_accept.TOOL_TO_OPERATION` (:83), `auto_accept.TOOL_TO_GATE` (:175);
  - `policy/registry.py`: `TOOL_TO_VERB` (:141) and `TOOL_REGISTRY = _build_registry()` (:246);
  - `policy/scopes.py`: `SCOPE_SELECTORS` (:358) and `NEW_SCOPE_SELECTORS` (:555);
  - `policy/propose.py`: `PROPOSABLE_SCOPES` (:267) and `_SCOPES_BY_GROUP`;
  - `write_effects.EFFECT_BY_TOOL` (:42);
  - `gate._TOOL_LAYOUT` (:254).

  `tests/conftest.py`'s autouse `_reset()` resets none of them.
- **Matching.** `policy/engine.py:57` `_selector_for` looks a predicate up in both selector dicts.
  An unknown predicate never matches (fail closed). `ScopeSelector` (`policy/scopes.py:74`) is
  `(predicate, scope_type, kind, resolves_from, matches(value, ctx))`. `_values_of(value)`
  (`policy/scopes.py:88`) treats a list or a single value alike. `ReviewContext`
  (`auto_accept.py:308`) carries `connector, tool, args, raw_data`.
- **Proposals.** `propose.proposals_for(tool, ctx)` (`policy/propose.py:529`) walks
  `PROPOSABLE_SCOPES`, filtered by the connector and the verb from `TOOL_REGISTRY[tool]`.
  `RuleProposal.pairs` is `{(scope.predicate, operation)}` (:474), so an accepted proposal always
  covers exactly one operation key. `rules_for_proposal` (:586) looks up
  `_SCOPES_BY_GROUP[proposal.scope.widening_group]`.
- **Card rendering.** `approval_window_html._render_block` (:339) renders the block types `text`,
  `field`, `heading` (`{"type":"heading","label":…}`), `table` and `markdown`. A `table` is
  `{"caption", "headers": list[str], "rows": list[list[str]], "footer"}` (:217). Every string goes
  through `html.escape`. `build_preview_body_html(blocks=…)` (:355) gives blocks precedence over
  `details_text`. There is no `code` or `diff` block.
- **Confirmations.** `web_prompt.block_on_confirm` (`web_prompt.py:41`) does three things:
  `registry.register_confirm(sensitive=…)` (`approvals.py:630`), then `registry.set_html(card.id,
  html)`, then `card.event.wait()`. A confirm card has `kind="confirm"` and no operation key, so
  `reevaluate_all` (`approvals.py:944`) never auto-accepts it. A `sensitive=True` confirm needs a
  human session and, where required, a passkey (`web/routes_approvals.py:683`,
  `web/approval_step_up.py:121`). `registry.await_status(approval_id)` (`approvals.py:910`) is what
  `privacyfence_await_approval` reads. `dialog_window_html.build_confirmation_html(*, title,
  message_lines, cancel_label, confirm_label)` (:212) renders text only.
- **Unattended sessions** exist only when the org config sets `unattended_sessions.enabled`
  (`daemon_main.py:1887`). In local mode they never exist.

### Daemon, settings, web

- **Startup** (`daemon_main.run_app`, :1803) runs in this order:
  1. config and audit;
  2. `build_connectors` (:1249), then `ConnectorHost(connectors)` (:1888);
  3. `SettingsController(...)` (:1899);
  4. `_maybe_start_web_server(config, connector_host, …, controller=…)` (:1907), which builds the
     dispatcher at :809 and the server;
  5. `_wait_for_shutdown()`.

  The `finally` block only closes the audit logger and releases the instance lock.
- **Connector state** has one funnel in local mode: `SettingsController.refresh_connectors()`
  (`settings_controller.py:1158`). Enable, disable, sign-in and refresh all end there, and it calls
  `_connectors_changed_listener`, which is wired to `McpDispatcher.notify_tools_changed`
  (`daemon_main.py:835`). Per-connector state comes from `SettingsController._connectors_state`
  (:1734): `enabled`, `authed` and `blocked_by`. The failure strings are `"not_authenticated"`,
  `"no_org_config"` or a redacted message (`daemon_main._classify_connector_failure`, :1214).
  **The reason strings `disabled`, `not_signed_in` and `not_configured` that the issue uses do not
  exist.** This plan uses its own (D6).
- **Settings actions.** `POST /api/settings/{action}` (`web/routes_settings.py:~850`) is
  allow-listed by `web/org_settings_scope.ACTION_SCOPES` (:65). It is classified as
  `_SENSITIVE_ACTIONS` (:223, needs a human session and step-up) or `_NON_SENSITIVE_ACTIONS`
  (:251). `TestSensitiveActionsCoverAllAllowedActions` (`tests/unit/web/test_routes_settings.py:369`)
  enforces that every action is classified. `enable_connector` is sensitive and `disable_connector`
  is not (ADR 0070). The Settings page is client-side JS in `settings_window_html.py`: the nav list
  at :424, `renderSection` at :1161, `_ALL_SECTIONS` at :1495 and `_capabilities_for` at :1516.
- **Security headers.** `web/server.py:407` `_SecurityHeadersMiddleware` overwrites the CSP header
  on every response (`build_csp(nonce, app_origin)`, `web/csp.py:85`) and sets
  `X-Frame-Options: DENY`. **A route cannot set its own CSP.** Plugin pages need a path branch in
  that middleware.
- **Org mode** builds a different app (`web/server.py:922` branches to `_build_org_app`, :1064).
- **Paths.** `paths.data_dir()` (:122) is the system root when privilege separation is on.
  `paths.user_dir(principal)` (:211) is `data_dir()` for the local principal and
  `data_dir()/users/<id>` otherwise. The system roots are `/var/lib/privacyfence`,
  `/Library/Application Support/PrivacyFence` and `C:\ProgramData\PrivacyFence`
  (`privilege_separation.py:176-188`). The service account owns them, so the service account can
  write there.
- **Privilege separation** is always on for a packaged install
  (`docs/security-and-compliance.md:14`). `privilege_separation.is_enabled()` (:612) reports it.
- **Admin-only checks.** `privilege_separation._elevation_script_problem(script)` (:2083) dispatches
  per OS:
  - POSIX (`_posix_script_elevation_problem`, :1175): `st_uid == 0` and no group or other write
    bit;
  - macOS: the same, plus a bundle signature check;
  - Windows (`_windows_script_elevation_problem`, :2065): `windows_acl.read_dacl`, and no
    write-granting ACE to a non-trusted trustee (`windows_acl.is_trusted`, :207).

  It checks the file only, not its parent directories. Its callers are `privilege_separation.py:1692`
  and `:2214`, and `service_control.py:129`. Its tests are
  `tests/unit/test_privilege_separation.py::TestElevationScriptProblem` (~:3805).
- **Installs.** The Linux .deb installs to `/opt/privacyfence`, the macOS .pkg to
  `/Applications/PrivacyFenceApp.app`, and the Windows installer to `{autopf}\PrivacyFence`
  (`installer/privacyfence.iss:61`). Upgrades replace all three, so plugins must live elsewhere
  (D5).
- **Secure files.** `secure_files.secure_mkdir(path, mode=0o700, *, foreign_owner_ok=False)` (:62)
  and `atomic_write_json(path, data, *, mode=0o600)` (:224).
- **Principals.** `principal.LOCAL_PRINCIPAL_ID == "local"` (:42), `current_principal()` (:95) and
  `principal_scope` (:99). No principal-removal hook exists.
- **Config keys** have no schema. `resources/settings.yaml.example` is the source of truth, and
  `tests/unit/test_docs_configuration_reference.py` requires every key in it to appear in
  `docs/configuration-reference.md`.

### Clients the source API reuses

| Operation | Client method | Returns |
|---|---|---|
| `salesforce.report_run` | `SalesforceClient.run_report(report_id, columns=None, filters=None, summary_only=False)` (`salesforce_client.py:554`) | **Raw** analytics JSON, `allData` included |
| `jira.search` | `JiraClient.search_issues(jql, max_results=20)` (`jira_client.py:313`), up to 500 | Parsed `list[JiraIssue]` dataclasses |
| `drive.download` | `DriveClient.download_file(file_id, destination_dir)` (`drive_client.py:1522`), streams 8 MB chunks to disk | A file. Google-native files are exported per `_GOOGLE_DOC_EXPORTS` (:48): Docs and Slides to `text/plain`, Sheets to `text/csv`. No range reads |
| `sheets.get_values` | `DriveClient.get_sheet_values(spreadsheet_id, range_a1, value_render_option="FORMATTED_VALUE")` (`drive_client.py:2288`) | Raw `values` array |
| `confluence.get_page` | `ConfluenceClient.get_page(page_id)` (`confluence_client.py:497`) | Parsed `ConfluencePage`. `body` is the storage-format XHTML |
| `calendar.list_events` | `CalendarClient.list_events(calendar_id, max_results=20, time_min="", time_max="", query="")` (`calendar_client.py:442`), up to 250 | Parsed `list[CalendarEvent]` |

Clients are private attributes of the connectors: `SalesforceConnector._sf`
(`connectors/salesforce.py:203`), `JiraConnector._jira`, `DriveConnector._drive`
(`connectors/drive.py:147`), `ConfluenceConnector._confluence` and `CalendarConnector._calendar`.
**This plan changes no client and no connector**, so §2.7's live-QA row does not apply (D7).

### Audit, tests, CI, packaging

- **Audit.** `audit_log.AuditEntry` (:130) has one free-form `decision` string. Its known values
  are documented in the comment at :139-310. A new decision needs only a new string, and no schema
  bump while no field is added. Entries are written through `get_audit_logger().record(...)`.
- **Test layout.**
  - `tests/unit/` mirrors `src/privacyfence/`. `tests/unit/policy/`, `tests/unit/web/`,
    `tests/integration/` and `tests/platform/` are packages (`__init__.py`).
  - Markers: `unit integration system browser packaged live platform`.
  - `tests/integration/test_mcp_daemon_contract.py:108` `running_mcp_server` builds an in-process
    `McpDispatcher` and `WebServer` and drives them with the official `mcp` client. It is the
    pattern for the end-to-end tests.
- **Coverage.** `scripts/check_coverage_floor.py` has `OVERALL_FLOOR=95.1` and per-module floors.
  Only `src/privacyfence` is measured.
- **Static analysis.** `ruff check .` covers the whole repo. Bandit scans `src` only.
- **Repo-wide tests.**
  - `tests/unit/test_code_no_history.py` forbids history tags everywhere outside `docs/`,
    `CHANGELOG.md` and `tests/fixtures/`: "Phase N", `P9`, `#123`, `§x.y`, `B<n>`, `D<n>`,
    "as of vX". **Code, comments, workflows and tests must not mention this plan's D-numbers,
    phase ids or the issue number.** Cite ADRs or the issue's full URL.
  - `tests/unit/test_docs_references_exist.py`: every `docs/...md` path mentioned anywhere must
    exist. So nothing outside `docs/` may name this plan file.
  - `tests/unit/test_docs_links.py` checks links.
  - `tests/unit/test_website_docs_allowlist.py`: a new `docs/*.md` must be listed in
    `docs/README.md` or in `scripts/build_site.py`'s `CONTRIBUTOR_DOCS`.
  - `tests/unit/test_docs_no_history.py`.
- **Dependencies.** `pyproject.toml` declares no `jsonschema` (it is only transitive via `mcp`).
  `pyyaml` is a runtime dependency. Packages are found under `src` only, so `plugin-sdk/` and
  `examples/` are not packaged into the daemon.
- **PyPI.** `.github/workflows/publish-pypi.yml` builds the sdist and wheel and publishes them,
  TestPyPI first (`environment: testpypi`) and then PyPI (`environment: pypi`), with OIDC trusted
  publishing (ADR 0020), on stable tags only. `docs/releasing.md:210-235` documents the pending
  publisher registration.
- **CI.** `.github/workflows/tests.yml` runs `pytest` over `tests/` on Linux, plus full-suite
  `platform-windows` and `platform-macos` jobs. `.github/workflows/build.yml` has three jobs and no
  matrix: `build` (macOS, :42), `build-windows` (:344) and `build-deb` (:570). It runs on a tag or
  by dispatch, which the steward table allows against a branch.
- **ADRs.** The last is 0119, so this plan's ADRs take 0120-0126.

## Design

Every decision is made here. Briefs point to these subsections by number. **Do not copy D-numbers
into code, comments or tests**: `test_code_no_history.py` rejects them.

### D1. Package layout

New package `src/privacyfence/plugins/`. Every module starts with a docstring and
`from __future__ import annotations`.

| Module | Holds | Phase |
|---|---|---|
| `__init__.py` | Docstring only: "Out-of-process plugins (ADR 0120)." | p1 |
| `constants.py` | Every constant in D2 | p1 |
| `protocol.py` | Message dataclasses, envelope parsing, validators for every method (D3) | p1 |
| `_testing.py` | `register_reset(fn)`, `reset_all()` (D2) | p1 |
| `blocks.py` | Block validation, sanitizing, conversion to card blocks, `flatten_text` (D4) | p2 |
| `manifest.py` | `Manifest` dataclass, `load_manifest(plugin_dir)` (D5) | p2 |
| `rpc.py` | `RpcPeer`: JSON-RPC 2.0 over two asyncio streams (D3) | p3 |
| `supervisor.py` | `Supervisor`: spawn, handshake, backoff, crash limit, shutdown (D8) | p3 |
| `source_ops.py` | Source operation table and `handle_source_call` (D7) | p6 |
| `spool.py` | `DownloadSpool` for chunked `drive.download` (D7) | p6 |
| `tools.py` | `validate_tool_defs`, `mcp_tool_name`, `ToolDefError` (D9) | p7 |
| `trust.py` | Plugins directory, discovery, admin-only check, hashes (D5) | p8 |
| `state.py` | `PluginStateStore`, the enabled-plugin file (D5) | p8 |
| `confirm.py` | `ConfirmationService`: `confirm.request` / `confirm.await` (D11) | p11 |
| `connector.py` | `PluginConnector(Connector)`: tool exposure and the two-step gate (D9, D10) | p12 |
| `storage.py` | Plugin data directories and their removal (D12) | p13 |
| `host.py` | `PluginHost`: composes everything and is wired into the daemon (D13) | p13 |
| `pages.py` | `render_plugin_page`: request normalization, response filtering (D14) | p14 |
| `events.py` | `EventFanout`: connector state diffs, lifecycle notifications (D12) | p15 |

Other new files:

- `src/privacyfence/web/routes_plugins.py` (D14);
- `docs/plugin-protocol/protocol.schema.json` (D3);
- `plugin-sdk/` (D16);
- `scripts/gen_plugin_sdk_types.py`;
- `tests/fixtures/plugins/stub/`, `tests/fixtures/plugins/echo/`;
- `examples/plugins/today/`;
- `scripts/build_example_plugin.py`.

Module-level state in the package registers its reset with `_testing.register_reset`, so
`tests/conftest.py` gains exactly one line, `plugins._testing.reset_all()`, in `_reset()` (p1).

### D2. Constants (`plugins/constants.py`, verbatim)

```python
PROTOCOL_VERSION = "1.0.0"
PROTOCOL_MAJOR = 1

MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_IN_FLIGHT = 16
INVALID_LINES_LIMIT = 3
INLINE_RESULT_BYTES = 100_000              # ADR 0092
MAX_PREVIEW_BYTES = 64 * 1024
MAX_PREVIEW_BLOCKS = 50
MAX_CELL_CHARS = 4096
MAX_TITLE_CHARS = 120
MAX_EFFECT_CHARS = 200
MAX_DESCRIPTION_CHARS = 1024               # tests/unit/web/test_tool_schema_portability.py's limit
MAX_SOURCE_RESULT_BYTES = 12 * 1024 * 1024
DRIVE_CHUNK_BYTES = 8 * 1024 * 1024
DRIVE_MAX_FILE_BYTES = 64 * 1024 * 1024
DRIVE_SPOOL_IDLE_SECONDS = 600
MAX_PAGE_BODY_BYTES = 8 * 1024 * 1024
MAX_PAGE_PATH_CHARS = 512
MAX_TOOLS = 64
MAX_SCOPE_VALUES = 100
MAX_SCOPE_VALUE_CHARS = 200
MCP_TOOL_NAME_MAX = 64

TIMEOUT_SECONDS: dict[str, float] = {
    "initialize": 10.0,
    "tool.prepare": 30.0,
    "tool.execute": 60.0,
    "web.request": 10.0,
    "storage.purge": 30.0,
    "source.call": 120.0,
    "confirm.request": 5.0,
}
CONFIRM_AWAIT_MAX_MS = 300_000
SHUTDOWN_GRACE_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 2.0
RESTART_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
CRASH_LIMIT = 5
CRASH_WINDOW_SECONDS = 600.0
PREPARED_CALL_LIFETIME_SECONDS = 900.0     # = approvals' pending TTL, so a deferred card can still release
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

PLUGIN_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")     # always .fullmatch()
TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")       # always .fullmatch()
SCOPE_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")      # always .fullmatch()
RESERVED_PLUGIN_NAMES = frozenset({
    "privacyfence", "plugin", "plugins", "settings", "mcp",
    "gmail", "drive", "contacts", "calendar", "tasks", "apps_script",
    "slack", "jira", "confluence", "salesforce", "telegram",
})

SOURCE_OPERATIONS: tuple[str, ...] = (
    "salesforce.report_run", "jira.search", "drive.download",
    "sheets.get_values", "confluence.get_page", "calendar.list_events",
)

GATES = ("auto", "review", "popup")
BLOCK_TYPES = ("heading", "fields", "table", "text", "code", "diff")

AUDIT_PLUGIN_SOURCE = "plugin_source"
AUDIT_PLUGIN_CONFIRM = "plugin_confirm"
AUDIT_PLUGIN_LIFECYCLE = "plugin_lifecycle"

ERROR_CODES: dict[str, int] = {
    "parse_error": -32700, "invalid_request": -32600, "method_not_found": -32601,
    "invalid_params": -32602, "internal_error": -32603,
    "operation_not_allowed": -32001, "connector_unavailable": -32002, "unknown_principal": -32003,
    "payload_too_large": -32004, "upstream_error": -32005, "org_only_field": -32006,
    "confirmation_refused": -32007, "unknown_tool": -32008, "invalid_blocks": -32009,
    "version_mismatch": -32010, "unknown_call": -32011, "digest_mismatch": -32012,
    "timeout": -32013, "introspection_only": -32014,
}


def mcp_tool_name(plugin: str, tool: str) -> str:
    return f"{plugin}_{tool}"


def operation_key(plugin: str, tool: str) -> str:
    return f"plugin.{plugin}.{tool}"


def scope_predicate(plugin: str, scope_type: str) -> str:
    return f"plugin:{plugin}:{scope_type}"
```

`RESERVED_PLUGIN_NAMES` must include every name in `settings_controller.ALL_CONNECTORS`.
`tests/unit/plugins/test_constants.py` asserts that. Plugin names contain no underscore, so the
first underscore in an MCP tool name always splits plugin from tool.

`_testing.py`:

```python
_RESETS: list[Callable[[], None]] = []

def register_reset(fn: Callable[[], None]) -> None:   # idempotent: the same fn twice is kept once
def reset_all() -> None:                             # calls each registered fn in order
```

### D3. Wire protocol

**Transport.**

- JSON-RPC 2.0 flows both ways over the child's stdin and stdout. Stdout carries protocol messages
  only. Stderr goes to the plugin's log (D8).
- Framing: one JSON object per line, UTF-8, no embedded newline, at most `MAX_LINE_BYTES`
  including the `\n`.
- A batch (a JSON array) is answered with `invalid_request` and never processed.
- Each side numbers its own request ids, as integers or strings. The two id spaces are
  independent.
- A notification has no `id` and gets no response. An unknown notification is ignored. An unknown
  request gets `method_not_found`.
- **Errors** are `{"code": <int from ERROR_CODES>, "message": <name>, "data": {"code": <name>,
  "detail": <str>, "retryable": <bool>, …extra}}`. `detail` never contains connector content or
  user content.
- **Versioning.** `protocol_version` is semver. The manifest's `protocol` is the major only.
  Daemon and plugin must share the major, or the plugin is not started. The effective version is
  the lower minor of the two.
- **Unknown fields.** Receivers ignore unknown fields, except the org-only fields, which local
  mode rejects with `org_only_field`: `source.call.credential` and `PrincipalContext.roles`.

`RpcPeer` (`rpc.py`, p3):

```python
class RpcError(Exception):
    def __init__(self, code: str, detail: str = "", *, retryable: bool = False, extra: dict | None = None): ...
    def to_error(self) -> dict: ...            # the error object above

class RpcPeer:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 handlers: dict[str, Callable[[dict], Awaitable[Any]]],
                 notification_handlers: dict[str, Callable[[dict], Awaitable[None]]] | None = None,
                 on_close: Callable[[str], None] | None = None) -> None: ...
    async def start(self) -> None              # spawns the reader task
    async def request(self, method: str, params: dict, *, timeout: float | None = None) -> Any
        # timeout defaults to TIMEOUT_SECONDS.get(method); raises RpcError("timeout") on expiry,
        # RpcError(<data.code>) on an error response, RpcError("internal_error") if the peer closed
    async def notify(self, method: str, params: dict) -> None
    async def close(self) -> None
    @property
    def closed(self) -> bool
```

- More than `MAX_IN_FLIGHT` outstanding outgoing requests: `request` waits on a semaphore.
- More than `MAX_IN_FLIGHT` incoming requests being handled: the next one is answered
  `invalid_request` with detail `"too many requests in flight"`.
- `INVALID_LINES_LIMIT` consecutive lines that are not valid JSON-RPC (bad JSON, over the cap, or
  not an object) close the peer with `on_close("invalid_output")`.
- A handler that raises `RpcError` produces that error. Any other exception produces
  `internal_error` with detail `"handler failed"`, and the exception is logged at warning with
  `exc_info`.

**Methods.** Direction "D→P" means daemon to plugin. Every request that the daemon sends for a
user carries a `principal` `PrincipalContext`. Every request the plugin sends names a principal by
id.

`PrincipalContext` = `{id: str, display_name: str, storage_dir: str, roles?: list[str]}`. `roles`
is org only.

| Method | Dir | Kind | Params | Result | Errors |
|---|---|---|---|---|---|
| `initialize` | D→P | req | `protocol_version, purpose ("run"\|"introspect"), mode ("local"\|"org"), daemon {name, version}, plugin {name, manifest_version}, data_dir, principals: PrincipalContext[], limits {max_line_bytes, max_in_flight, inline_result_bytes}` | `protocol_version, plugin {name, version}, scope_types: [{name, description}], tools: ToolDef[]` | `version_mismatch`, `invalid_params` |
| `tools.changed` | P→D | notif | `tools: ToolDef[]` | — | (validated as a whole; D9) |
| `tool.prepare` | D→P | req | `call_id, principal, tool, args, reason (str\|null)` | `preview: Block[], payload?: Block[], scopes: {type: [str]}` | `unknown_tool`, `invalid_params`, `invalid_blocks`, `payload_too_large`, `connector_unavailable` |
| `tool.execute` | D→P | req | `call_id, principal, tool, args, args_digest, approval {approval_id, decision:"approved", via:"card"\|"rule"\|"auto", decided_at}` | `result: any, approval_id?: str` | `unknown_call`, `digest_mismatch` |
| `source.call` | P→D | req | `principal: str, operation, params, credential?` (org only) | `operation, data, bytes, next_cursor (str\|null)` | `operation_not_allowed`, `unknown_principal`, `connector_unavailable`, `payload_too_large`, `upstream_error`, `org_only_field`, `introspection_only`, `invalid_params` |
| `confirm.request` | P→D | req | `principal: str, kind, title, preview: Block[], require_step_up (default true)` | `approval_id, expires_at` | `confirmation_refused`, `invalid_blocks`, `unknown_principal`, `introspection_only` |
| `confirm.await` | P→D | req | `approval_id, timeout_ms?` (≤ `CONFIRM_AWAIT_MAX_MS`) | `status ("approved"\|"denied"\|"expired"), deny_note?, decided_at?` | `timeout`, `invalid_params` |
| `web.request` | D→P | req | `principal, method ("GET"), path, query: {str: str}` | `status, headers: {str: str}, body, body_encoding ("utf8"\|"base64")` | — |
| `storage.purge` | D→P | req | `scope ("all"\|"install"\|"principal"), principal?` | `{purged: true}` | — |
| `connector.state_changed` | D→P | notif | `connector, state ("enabled"\|"disabled"\|"signed_in"\|"signed_out"), principal` | — | — |
| `principal.removed` | D→P | notif | `principal` | — | — (reserved; local mode never sends it) |
| `plugin.disabling` | D→P | notif | `reason ("user"\|"crash_limit"\|"hash_changed"\|"admin")` | — | — |
| `shutdown` | D→P | notif | `grace_ms` | — | — |

Further method rules:

- `initialize` result `plugin.name` and `plugin.version` must equal the manifest's, or the start
  fails with reason `manifest invalid: name or version differs from the plugin's own`.
- With `purpose: "introspect"`, `source.call` and `confirm.request` are refused with
  `introspection_only`. After the result, the daemon sends `shutdown`.
- `tool.prepare` has no side effects. `reason` is stripped from `args`. `payload` is required for
  read-only tools and forbidden for others (`invalid_params`). The JSON of
  `{"blocks": payload}` must be at most `INLINE_RESULT_BYTES`, or the call fails with
  `payload_too_large`.
- `tool.execute`: `args_digest` is `"sha256:" + sha256(json.dumps(args, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()`. For read-only tools the
  daemon ignores `result` and returns the prepared payload (D10). A write's `result` must
  serialize to at most `INLINE_RESULT_BYTES`.
- `web.request`: other status values become 502. Of the response headers, only `content-type` and
  `cache-control` are kept (D14).
- `storage.purge` gets an acknowledgement, with a timeout (D12).

`ToolDef` = `{name, description, parameters (JSON Schema object), read_only: bool, destructive:
bool, gate: "auto"|"review"|"popup", scopes: [scope_type], effect?: str (≤ MAX_EFFECT_CHARS),
title?: str (≤ MAX_TITLE_CHARS)}`.

- `effect` is the "what approving does" sentence for a non-read-only tool, per tool, not per call.
  This **corrects** the spec, which had `effect` per `prepare` result:
  `write_effects.EFFECT_BY_TOOL` is a per-tool table and the card builder reads it per tool.
- `title` is the human-readable tool name on cards. The default is the tool name with `_` replaced
  by a space and the first letter capitalized.

**`protocol.py` API (p1).**

- Dataclasses: `PrincipalContext`, `ToolDef`, `InitializeResult`, `PrepareResult`,
  `ExecuteResult`, `SourceCallParams`, `ConfirmRequestParams`, `WebResponse`.
- Each has `from_wire(obj: Any, *, mode: str = "local") -> Self`, which raises
  `RpcError("invalid_params", <detail>)` (or `org_only_field`), and `to_wire() -> dict`.
- `args_digest(args: dict) -> str` as above.
- `principal_context(principal: Principal, storage_dir: Path, *, mode: str = "local") -> dict`.

Validators check types, required keys, and the patterns and limits in D2. They ignore unknown
keys. `RpcError` lives in `protocol.py`, and `rpc.py` imports it from there. **The validators are
hand-written; this adds no new dependency** (stdlib first). `tests/unit/plugins/test_protocol.py`
loads `docs/plugin-protocol/protocol.schema.json` with `json` and asserts two things: every
`$defs` entry named in the table below has exactly the property names its dataclass handles, and
every example transcript in the test validates with `from_wire`.

**`docs/plugin-protocol/protocol.schema.json`** is JSON Schema 2020-12 with `$defs` for:
`PrincipalContext`, `ToolDef`, `Block` (`oneOf` the six types, each with
`additionalProperties: false`), `Error`, `Manifest`, and `<Method>Params` / `<Method>Result` for
every method above (the names `InitializeParams`, `InitializeResult`, `ToolPrepareParams`,
`ToolPrepareResult`, `ToolExecuteParams`, `ToolExecuteResult`, `SourceCallParams`,
`SourceCallResult`, `ConfirmRequestParams`, `ConfirmRequestResult`, `ConfirmAwaitParams`,
`ConfirmAwaitResult`, `WebRequestParams`, `WebRequestResult`, `StoragePurgeParams`,
`StoragePurgeResult`, `ToolsChangedParams`, `ConnectorStateChangedParams`,
`PrincipalRemovedParams`, `PluginDisablingParams`, `ShutdownParams`). Add `x-limits` with the
numeric constants of D2 that the protocol exposes. The SDK's types are generated from this file
(D16).

### D4. Blocks (`plugins/blocks.py`)

A block is data, never markup. Blocks are validated on arrival, and anything invalid fails the
whole list with `invalid_blocks`.

| Type | Fields (others are errors) |
|---|---|
| `heading` | `text: str`, `level?: 2\|3` |
| `fields` | `items: [{label: str, value: str}]` (1–50 items) |
| `table` | `columns: [{key: str, label: str}]` (1–20), `rows: [{<key>: str\|int\|float\|bool\|null}]`. A row may only use declared keys |
| `text` | `text: str` |
| `code` | `text: str`, `language?: str` (`[a-z0-9+#-]{1,20}`) |
| `diff` | `format: "unified"`, `text: str` |

```python
class BlockError(ValueError): ...

def validate_blocks(blocks: Any, *, max_blocks: int = MAX_PREVIEW_BLOCKS,
                    max_bytes: int | None = MAX_PREVIEW_BYTES) -> list[dict]
    # Returns sanitized copies. Strips C0/C1 controls except \n and \t, and the bidi
    # controls U+202A-U+202E, U+2066-U+2069, U+200E, U+200F, U+061C from every string.
    # Truncates every table cell to MAX_CELL_CHARS with a trailing "…".
    # Raises BlockError on a wrong type or field, more than max_blocks, or a serialized
    # size over max_bytes (None means no byte cap; payloads use the INLINE_RESULT_BYTES check instead).

def to_card_blocks(blocks: list[dict]) -> list[dict]
    # heading -> {"type": "heading", "label": text}
    # fields  -> one {"type": "field", "label": l, "value": v} per item
    # table   -> {"type": "table", "headers": [labels], "rows": [[str(cell) or "" per column]]}
    # text    -> {"type": "text", "text": text}
    # code    -> {"type": "code", "text": text, "language": language or ""}
    # diff    -> {"type": "diff", "text": text}

def fields_dict(blocks: list[dict]) -> dict[str, str]
    # every fields item, in order, as {label: value}; later duplicates get " (2)", " (3)" suffixes

def flatten_text(blocks: list[dict]) -> str
    # all strings, one per line, for the PII scan
```

The card renderer gains `code` (a `<pre><code>` with escaped text) and `diff` (a `<pre>` where
each line starting with `+` gets class `pf-diff-add`, `-` gets `pf-diff-del`, and `@@` gets
`pf-diff-hunk`; the text is escaped). Their styles use design tokens only (p11).

### D5. Manifest, plugins directory, trust and state

**Manifest** `privacyfence-plugin.yaml`, loaded with `yaml.safe_load`. An unknown key is an error.

```yaml
name: today                      # PLUGIN_NAME_RE, not in RESERVED_PLUGIN_NAMES, equals the directory name
display_name: Today              # 1-60 chars
version: 1.2.0                   # semver MAJOR.MINOR.PATCH with optional -prerelease
protocol: "1"                    # major only, as a string
command: ["today-plugin"]        # non-empty list of str; command[0] resolves inside the plugin dir
source_operations:               # optional, default []; each in SOURCE_OPERATIONS
  - calendar.list_events
tools: dynamic                   # required; the only value
max_gate_floor: auto             # optional, "review" (default) or "auto"
pages: true                      # optional, default false
service_credentials: false       # optional, default false; true is rejected in local mode
```

```python
@dataclass(frozen=True)
class Manifest:
    name: str; display_name: str; version: str; protocol: str; command: tuple[str, ...]
    source_operations: frozenset[str]; max_gate_floor: str; pages: bool; service_credentials: bool

class ManifestError(ValueError): ...

MANIFEST_FILENAME = "privacyfence-plugin.yaml"

def load_manifest(plugin_dir: Path, *, mode: str = "local") -> Manifest
def resolve_command(manifest: Manifest, plugin_dir: Path) -> list[str]
    # command[0] joined to plugin_dir, then .resolve(); must stay inside plugin_dir.resolve(),
    # which also refuses a symlink pointing out. On Windows, append ".exe" if command[0] has
    # no suffix. Raises ManifestError otherwise.
```

**Plugins directory** (`trust.plugins_dir() -> Path`). It must be writable by administrators only,
and outside both the installed app (which upgrades replace) and the service account's data root
(which the service account can write). This **corrects** the spec's proposed paths.

| OS | Path |
|---|---|
| Linux | `/usr/local/lib/privacyfence/plugins` |
| macOS | `/Library/PrivacyFence/plugins` |
| Windows | `%ProgramFiles%\PrivacyFence Plugins` (from `os.environ["ProgramFiles"]`, falling back to `C:\Program Files`) |

`PRIVACYFENCE_PLUGINS_DIR` is **not** honoured: a configurable path could point somewhere the user
can write (D18). Tests inject the directory through `PluginHost(plugins_dir=…)` and a `trust_check`
seam (D13).

**Admin-only check.** Factor the per-file part of `_elevation_script_problem` into a public
`privilege_separation.admin_only_write_problem(path: Path) -> str | None`. On POSIX it applies the
root-owned and no-group-or-other-write rule to any path, a file or a directory. On Windows it
applies the DACL rule. It does not include macOS's bundle signature check, which stays in
`_macos_auto_enable_script_problem`. `_posix_script_elevation_problem` and
`_windows_script_elevation_problem` call the new function; their behaviour and tests are
unchanged.

`trust.admin_only_problem(plugin_dir: Path, executable: Path) -> str | None` checks, in this order,
and returns the first problem:

1. the executable;
2. the plugin directory;
3. every ancestor of the plugin directory up to and including `plugins_dir()`'s parent.

**Discovery.** `trust.discover(plugins_dir: Path) -> list[DiscoveredPlugin]`, sorted by name. Each
immediate subdirectory is one plugin, and hidden directories (`.`-prefixed) are skipped.

```python
@dataclass(frozen=True)
class DiscoveredPlugin:
    dir_name: str; path: Path; manifest: Manifest | None; problem: str | None
    executable_sha256: str; manifest_sha256: str   # "" when unreadable
```

`problem` is one of the exact reason strings in D6, or `None`. `sha256_file(path) -> str` streams
the file in 1 MiB blocks.

**State file** `paths.data_dir()/plugins-state.json`, written with `atomic_write_json(mode=0o600)`:

```json
{"version": 1, "plugins": {"today": {"enabled": true, "version": "1.2.0",
  "executable_sha256": "…", "manifest_sha256": "…", "max_gate_floor": "auto",
  "enabled_at": "2026-10-07T10:00:00+00:00", "disabled_reason": null}}}
```

`PluginStateStore(path: Path)` provides:

- `load() -> dict[str, PluginRecord]`. A missing file means empty. A corrupt file is logged at
  warning, read as empty, and the plugins stay disabled (fail closed).
- `enable(name, *, version, executable_sha256, manifest_sha256, max_gate_floor)`.
- `disable(name, reason: str)`.
- `forget(name)`.
- `check_hashes(discovered: DiscoveredPlugin) -> str | None`. A difference from the record returns
  `"executable or manifest changed, enable again"` and disables the plugin with that reason.

### D6. Plugin states and exact reason strings

The states are `discovered`, `rejected`, `disabled`, `starting`, `running` and `backoff`. Settings
shows the state and, unless it is `running`, the reason. These strings are exact:

| Reason | When |
|---|---|
| `manifest invalid: <detail>` | `ManifestError` (detail is the exception text) |
| `executable is writable by non-administrators` | `admin_only_problem` returned a problem; the problem text is logged, not shown |
| `protocol major mismatch` | `initialize` reported a different major, or the manifest's `protocol` ≠ `"1"` |
| `executable or manifest changed, enable again` | hash drift |
| `crashed 5 times in 10 minutes` | crash limit |
| `disabled by you` | user disable |
| `plugins need PrivacyFence's background service` | `privilege_separation.is_enabled()` is false |
| `plugins are turned off in settings` | `plugins.enabled: false` |

The `connector_unavailable` error's `data.reason` is one of:

- `disabled`: the connector's `enabled` is false;
- `not_authenticated`: the connector is enabled but not built, with `blocked_by ==
  "not_authenticated"` or no connector object;
- `unavailable`: any other `blocked_by`.

These mirror `SettingsController._connectors_state`'s fields.

### D7. Source API (`plugins/source_ops.py`, `plugins/spool.py`)

`source.call` from a plugin is handled by:

```python
async def handle_source_call(params: dict, *, plugin: str, manifest: Manifest, introspecting: bool,
                             connectors_provider: Callable[[], dict[str, Connector]],
                             connector_state: Callable[[str], ConnectorState],
                             spool: DownloadSpool) -> dict
```

The checks, in order (the first failure raises `RpcError`):

1. `introspecting` → `introspection_only`.
2. Params must parse (`SourceCallParams.from_wire`). `credential` present → `org_only_field`.
3. `principal != "local"` → `unknown_principal`.
4. The operation must be in `SOURCE_OPERATIONS`, and in `manifest.source_operations` →
   `operation_not_allowed`.
5. The connector must be enabled and built. `connector_state` is `(enabled: bool, blocked_by: str |
   None)` from `SettingsController._connectors_state` via the host. Otherwise
   `connector_unavailable` with `data.reason` per D6.
6. Run the adapter with `asyncio.to_thread`, under `principal_scope(LOCAL_PRINCIPAL)`. A client
   exception (the client's `*ClientError` or `RuntimeError`) becomes `upstream_error` with detail
   `"the service returned an error"`. The exception type name is logged, never its message.
7. A serialized `data` larger than `MAX_SOURCE_RESULT_BYTES` → `payload_too_large`.
8. Every outcome, success or error, writes **one** audit entry (below).

The result is `{"operation": op, "data": data, "bytes": len(json.dumps(data)), "next_cursor":
cursor}`.

Adapters, table `SOURCE_ADAPTERS: dict[str, SourceAdapter]`, with
`SourceAdapter(connector: str, client_attr: str, run: Callable[[Any, dict, DownloadSpool], tuple[Any, str | None]], targets: Callable[[dict], str])`.
The client is `getattr(connector, client_attr, None)`. `None` means `connector_unavailable`,
reason `unavailable`. This is the one place that reaches into a connector's private client. It
says so in a comment and changes no connector.

| Operation | `connector`/`client_attr` | Params (validated) | `data` | `targets` (audit) |
|---|---|---|---|---|
| `salesforce.report_run` | `salesforce`/`_sf` | `report_id: str` (required), `filters?: list` | `client.run_report(report_id, filters=filters)` as returned (**raw**) | `report_id` |
| `jira.search` | `jira`/`_jira` | `jql: str` (required), `max_results?: int` 1-500, default 100 | `[dataclasses.asdict(i) for i in client.search_issues(jql, max_results)]` (**normalized**) | `"jql:" + sha256(jql)[:16]`, plus the count |
| `drive.download` | `drive`/`_drive` | `file_id: str` (required), `length?: int` 1..`DRIVE_CHUNK_BYTES` (default the max), and either `offset?: int` ≥ 0 or `cursor?: str`, not both (`invalid_params`) | `{file_id, mime_type, revision, total_size_bytes, offset, length, eof, content_base64}` (**raw bytes**); `next_cursor` set until `eof` | `file_id`, `offset`, `length` |
| `sheets.get_values` | `drive`/`_drive` | `spreadsheet_id: str`, `range: str` (both required), `value_render_option?`: `FORMATTED_VALUE` (default), `UNFORMATTED_VALUE` or `FORMULA` | `{"values": client.get_sheet_values(...)}` (**raw values array**) | `spreadsheet_id`, `range` |
| `confluence.get_page` | `confluence`/`_confluence` | `page_id: str` (required) | `dataclasses.asdict(client.get_page(page_id))`; `body` is the storage XHTML (**normalized**) | `page_id` |
| `calendar.list_events` | `calendar`/`_calendar` | `calendar_id: str` (default `"primary"`), `time_min: str`, `time_max: str` (RFC 3339, required), `max_results?: int` 1-250, default 250 | `[dataclasses.asdict(e) for e in client.list_events(calendar_id, max_results, time_min, time_max)]` (**normalized**) | `calendar_id`, `time_min`, `time_max` |

`next_cursor` is `null` for every operation except `drive.download`. If a client returns non-JSON
values (a `datetime`), the adapter serializes them with `default=str`, because `asdict` output must
be JSON.

**Why normalized and not raw** (the spec asked for raw everywhere). Jira, Confluence and Calendar's
clients only expose parsed dataclasses. Raw responses would need new client methods. Each new
method brings §2.7's live-QA row, needs new recorded fixtures (there are none today for JQL search,
`list_events` or Sheets values), and widens what a plugin receives past what PrivacyFence has
already privacy-reviewed. The data lake's needs are Salesforce report runs, which stay raw, and
workbook bytes. The protocol documents each operation's `data` shape, and a later minor version
can add `*_raw` operations. This goes into ADR 0123.

**Chunked `drive.download`** (`DownloadSpool`, `spool.py`):

- `DownloadSpool(root: Path, *, clock=time.monotonic)`, where `root` is
  `paths.data_dir()/plugin-spool`, created with `secure_mkdir(mode=0o700)`.
- On the first call for a `(plugin, file_id)` with no cursor:
  - fetch metadata with the client's own metadata call (`DriveClient.get_file_metadata(file_id)`;
    a worker who finds it named differently stops with `status=blocked`);
  - refuse a file over `DRIVE_MAX_FILE_BYTES` with `payload_too_large`;
  - download the whole file with `download_file(file_id, <spool dir>)` into
    `root/<plugin>/<sha256(file_id)[:16]>-<revision>`;
  - a native Google file is exported per the client's existing `_GOOGLE_DOC_EXPORTS`. Range reads
    do not exist, so spooling is the uniform path.
- `revision` is the file's `headRevisionId` when present, else its `modifiedTime`.
- The cursor is `base64url(json.dumps({"f": file_id, "r": revision, "o": next_offset}))`. A cursor
  that doesn't decode is `invalid_params`.
- On every call with a cursor or offset, metadata is re-read. A revision different from the
  spool's (or from the cursor's) raises `upstream_error` with `data.reason="revision_changed"`, and
  the spool file is deleted.
- Chunks are read from the spool file at the offset.
- Spool files idle for more than `DRIVE_SPOOL_IDLE_SECONDS` are deleted by `sweep()`, which every
  call runs and which the host also runs at shutdown (`clear()`). `eof` reads keep the file until
  idle.

**Audit** (one entry per `source.call`, decision `AUDIT_PLUGIN_SOURCE`):

```python
AuditEntry(timestamp=…, week=current_week(), request_id="", connector=f"plugin:{plugin}",
           tool=operation, tool_name=f"{plugin} source read", summary=f"{targets}; bytes={n}",
           sender="", decision="plugin_source", auto_accept_rule="", latency_seconds=…,
           claude_reason="")
```

On an error, `summary` is `f"{targets}; error={code}"`. **No `data` content ever goes into an
entry or a log line.** Audit write failures are logged at warning and never fail the call.

### D8. Supervisor (`plugins/supervisor.py`)

```python
@dataclass
class LaunchSpec:
    name: str; argv: list[str]; cwd: Path; log_path: Path

class Supervisor:
    def __init__(self, spec: LaunchSpec, *, initialize_params: Callable[[str], dict],
                 handlers: dict[str, Callable[[dict], Awaitable[Any]]],
                 notification_handlers: dict[str, Callable[[dict], Awaitable[None]]],
                 on_state: Callable[[str, str | None], None],          # (state, reason)
                 on_ready: Callable[[InitializeResult], Awaitable[None]],
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None
    async def start(self) -> None          # spawn and handshake; restarts on exit per backoff
    async def introspect(self) -> InitializeResult   # one start with purpose "introspect", then stop
    async def stop(self, *, reason: str = "user") -> None
    @property
    def peer(self) -> RpcPeer | None
    @property
    def state(self) -> str
```

- **Spawn.**
  - `asyncio.create_subprocess_exec(*argv, cwd=cwd, stdin=PIPE, stdout=PIPE, stderr=<log file>,
    env=child_env())`, with `limit=MAX_LINE_BYTES` on the stdout reader.
  - POSIX adds `start_new_session=True`. Windows adds
    `creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`.
  - Stop kills the process group on POSIX (`os.killpg`) and the process on Windows.
- **Child environment** (`child_env() -> dict`) is built from an allowlist, never copied: `PATH`,
  `SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, plus
  `PRIVACYFENCE_PLUGIN=1`. Only the keys that are set are copied.
- **Log.**
  - `log_path` is `paths.data_dir()/logs/plugins/<name>.log`, opened in append mode with mode
    `0o600`. Its parent is created with `secure_mkdir`.
  - Before each spawn, a log over `LOG_MAX_BYTES` is rotated to `.1` … `.LOG_BACKUP_COUNT`.
  - The daemon writes one line per lifecycle event to its own logger. It never logs message
    bodies.
- **Handshake.** Send `initialize` with `initialize_params(purpose)` and a 10 s timeout. Then
  check, in order:
  1. major equal (else state `disabled`, reason `protocol major mismatch`, and no restart);
  2. name and version equal to the manifest (else reason `manifest invalid: name or version
     differs from the plugin's own`, no restart);
  3. tool definitions valid (`tools.validate_tool_defs`; else `manifest invalid: <detail>`, no
     restart).

  Then call `on_ready`.
- **Crashes.** The process exiting, or the peer closing with `invalid_output`, while `running` is
  a crash. The crash timestamps are kept, and those older than `CRASH_WINDOW_SECONDS` are dropped.
  At `CRASH_LIMIT` the state becomes `disabled` with reason `crashed 5 times in 10 minutes`, and
  `on_state` tells the host, which records it (D13). Otherwise the state becomes `backoff`, the
  supervisor sleeps `RESTART_BACKOFF_SECONDS[min(n-1, 5)]` (n = crashes in the window) and starts
  again.
- **Stop.**
  1. Send `plugin.disabling {reason}` when the reason is not `"shutdown"`.
  2. Send `shutdown {grace_ms: 5000}`.
  3. Wait `SHUTDOWN_GRACE_SECONDS` for exit.
  4. Terminate, wait `TERMINATE_GRACE_SECONDS`, then kill.

  Stop never counts as a crash.
- The test stub plugin `tests/fixtures/plugins/stub/stub_plugin.py` (no SDK, stdlib only) takes a
  mode from its first argument: `ok`, `crash-on-start`, `crash-after-init`, `bad-version`,
  `junk-stdout`, `slow-shutdown`, `echo-env` (prints its environment keys to stderr) and
  `wrong-name`. Tests launch it as `[sys.executable, stub_plugin.py, mode]`.

### D9. Tool definitions and exposure (`plugins/tools.py`, `plugins/connector.py`)

`validate_tool_defs(plugin: str, defs: Any, scope_types: list[dict], manifest: Manifest) ->
list[ToolDef]` raises `ToolDefError(detail)` on the first violation, so the whole list is rejected:

- the list type, and at most `MAX_TOOLS` entries;
- each `ToolDef.from_wire` passes, and names are unique;
- `TOOL_NAME_RE`;
- `len(mcp_tool_name(plugin, name)) ≤ MCP_TOOL_NAME_MAX`;
- the description is 1..`MAX_DESCRIPTION_CHARS`;
- `parameters` is a dict with `"type": "object"`, and has no property named `reason`;
- `destructive` ⇒ `gate == "popup"` (`"destructive tool <name> must use the popup gate"`);
- `not read_only and gate == "auto"` ⇒ `manifest.max_gate_floor == "auto"` (`"tool <name> needs
  max_gate_floor: auto to use the auto gate"`);
- `read_only and destructive` is an error;
- every name in `scopes` is declared in `scope_types`, and every scope type name matches
  `SCOPE_TYPE_RE`;
- `effect` and `title` lengths.

`validate_scope_types(raw: Any) -> list[dict]` checks the list of `{name, description}`, the names
against `SCOPE_TYPE_RE`, and at most 20 entries.

**Exposure.** `PluginConnector(Connector)` has `name = plugin`. `tool_specs()` returns, per
`ToolDef`, a `ToolSpec` with:

- `name = mcp_tool_name(plugin, tool)`;
- `description` unchanged;
- `params` converted from the JSON Schema properties to `ToolParam`. A property's `type` maps to
  `ToolParam.type` as-is (`string`, `integer`, `number`, `boolean`, `array`, `object`), and
  `required` membership is kept. Gated tools (`gate != "auto"`) get the same required `reason`
  `ToolParam` that connector tools use: copy it from `connector.py` and the connectors, where it
  already exists, and do not invent new wording;
- `read_only` and `destructive` as declared.

**Policy registration.** On every accepted tool list (initialize or `tools.changed`),
`PluginConnector.set_tools(defs)` calls `auto_accept.register_dynamic_tools(...)` (D15) with:

- `mcp_tool_name` → gate;
- `mcp_tool_name` → `operation_key`, for review and popup tools;
- verb `READ` for `read_only`, else `UPDATE`. Use the existing `policy.registry.Verb` members;
- `layout` WIDE when `read_only` and the tool has a payload, else NARROW;
- `effect`, else `f"Runs {title} in the {display_name} plugin."`;
- scope types.

It first unregisters the previous list. Then the host calls `on_tools_changed()`, which reaches
`McpDispatcher.notify_tools_changed` (D13).

**Rejected `tools.changed`.** The previous list stays in force. An audit
`plugin_lifecycle` entry with summary `"tools change rejected: <detail>"` is written, and Settings
shows the detail under the plugin as `last tools change rejected: <detail>` until the next
accepted change. An accepted change writes `"tools changed: +a,+b,-c"`.

### D10. Two-step gate (`plugins/connector.py`)

`PluginConnector.call(tool: str, args: dict) -> Any` (`tool` is the MCP name):

1. Unknown tool → `ValueError(f"Unknown tool: {tool}")` (the connector convention). The plugin not
   running → `RuntimeError("The <display_name> plugin is not running.")`.
2. **Reuse or prepare.** Key `k = approvals.canonical_key(self.name, tool, args)`, with `reason`
   already popped by the dispatcher. If `self._pending_prepared[k]` exists and is younger than
   `PREPARED_CALL_LIFETIME_SECONDS`, reuse it. Otherwise:
   - `call_id = uuid4().hex`;
   - `await peer.request("tool.prepare", {call_id, principal: principal_context(...), tool: <own
     name>, args, reason: current_reason() or None})`;
   - validate with `PrepareResult.from_wire`: `preview` through `validate_blocks` (byte cap
     `MAX_PREVIEW_BYTES`), `payload` through `validate_blocks(max_bytes=None)` plus the
     `INLINE_RESULT_BYTES` check;
   - every scope type the tool declares must appear in `scopes`, with 1..`MAX_SCOPE_VALUES` values
     each ≤ `MAX_SCOPE_VALUE_CHARS`, or the call fails with
     `RuntimeError("The plugin returned an invalid preview.")`.

   An `RpcError` from prepare becomes `RuntimeError(<a fixed sentence per code>)`:
   - `connector_unavailable`: "A service this plugin reads from is not connected.";
   - `payload_too_large`: "The plugin's result is too large to return.";
   - `timeout`: "The plugin did not answer in time.";
   - anything else: "The plugin could not prepare this call."
3. **Gate.**
   - `gate == "auto"`: no card. Write the auto audit entry (copy `connectors/tasks.py:447`'s
     `_auto_audit` shape with `connector=self.name`, `tool=<mcp name>`), then go to step 4 with
     `approval = {approval_id: "auto-" + call_id, decision: "approved", via: "auto", decided_at:
     now}`.
   - `review` or `popup`:

     ```python
     released = await gated_call(
         connector=self.name, tool=<mcp name>, tool_name=<title>,
         summary=<first fields item "label: value" or title>, sender="",
         raw_data={"plugin": self.name, "tool": <own name>, "scopes": scopes},
         filtered_data=({"blocks": payload} if read_only else None), gate=<gate>,
         preview=fields_dict(preview), preview_blocks=to_card_blocks(non-fields preview blocks + payload),
         pii_scan_text=(flatten_text(payload) if read_only else None),
         args=args,
     )
     ```

     Store the prepared call in `_pending_prepared[k]` **before** calling `gated_call`. If
     `gated_call` raises `ApprovalPending`, keep it and re-raise. On any other outcome (released,
     `GateDeniedError`, anything else), delete `_pending_prepared[k]`. `approval.via` is `"rule"`
     when the audit decision was an auto-accept and `"card"` otherwise. If `gated_call` exposes no
     way to tell them apart, use `"card"` for both and note it as an out-of-scope finding.
4. **Execute.**

   ```python
   await peer.request("tool.execute", {call_id, principal, tool, args,
                                       args_digest: args_digest(args), approval})
   ```

   - **Read-only:** execute errors are logged and ignored, and the return value is the prepared
     `{"blocks": payload}`, never the plugin's `result`.
   - **Write:** return `result`, plus `{"approval_id": …}` merged in when present, so the agent can
     call `privacyfence_await_approval`. A write that was released is executed at most once:
     execute is never retried, and `_pending_prepared` was already cleared in step 3.
5. `unknown_call` or `digest_mismatch` from execute become
   `RuntimeError("The plugin lost track of this call; ask again.")`.

**Why the read returns the prepared payload:** the human approved exactly those bytes. A plugin
that returned something else from execute would bypass the gate. ADR 0122.

**Why reuse the pending prepared call:** after an `ApprovalPending`, the agent repeats the call to
collect the decision. Re-running `prepare` could produce a payload the human never saw.

**Single use.** A popup write that is approved once is collected once (ADR 0073, through the
ledger). A second identical call re-prepares and gets a new card.

### D11. Confirmations (`plugins/confirm.py`)

```python
class ConfirmationService:
    def __init__(self, *, registry_provider: Callable[[], PendingApprovalRegistry],
                 unattended_active: Callable[[], bool], executor: Executor) -> None
    async def request(self, plugin: str, display_name: str, params: dict, *, introspecting: bool) -> dict
    async def await_(self, plugin: str, params: dict) -> dict
```

`request`:

1. `introspecting` → `introspection_only`.
2. `ConfirmRequestParams.from_wire` (title ≤ `MAX_TITLE_CHARS`; `kind` matches
   `[a-z][a-z0-9_]{0,30}`; preview through `validate_blocks`), else `invalid_params` or
   `invalid_blocks`.
3. `principal != "local"` → `unknown_principal`.
4. `unattended_active()` → `confirmation_refused`, `data.reason = "unattended_session"`.
5. `card = registry.register_confirm(sensitive=require_step_up)`.
6. `registry.set_html(card.id, dialog_window_html.build_confirmation_html(title=f"{display_name}:
   {title}", message_lines=[], cancel_label="Deny", confirm_label="Approve",
   body_blocks=to_card_blocks(preview)))`.
7. Remember `card.id → plugin`. Write an audit `plugin_confirm` entry with summary
   `f"{kind}; requested"`.
8. Return `{approval_id: card.id, expires_at: <card.expires_at as RFC 3339 UTC>}`.

It returns at once. The human decides on the existing approvals page, and the card shows the
plugin's display name.

`await_`:

1. The `approval_id` must belong to this plugin, else `invalid_params`.
2. Wait on `card.event` in `executor` (never the default pool), up to `timeout_ms`.
3. Timeout → `RpcError("timeout")`.
4. Map `card.result`: `"confirm"` → `approved`, `"cancel"` or `"deny"` → `denied`, expired →
   `expired`.
5. Add `deny_note` from `registry.denial_feedback(approval_id)`, in the delimited form of ADR 0083.
6. Audit `plugin_confirm` with summary `f"{kind}; {status}"`.

`dialog_window_html.build_confirmation_html` gains a keyword `body_blocks: list[dict] | None =
None`, rendered between the message and the buttons through
`approval_window_html.build_preview_body_html(blocks=body_blocks)`. Existing callers are unchanged.

A confirm card is `kind="confirm"` with no operation key, so no rule can ever auto-accept it. That
is the existing invariant of `privacyfence_propose_policy_change`.

`privacyfence_await_approval(approval_id)` works on the same id with no change, because it reads
`registry.await_status`.

In local mode `unattended_active` is `lambda: False`: unattended sessions are an org-mode setting,
and the host does not run in org mode. The refusal is tested with an injected `True`.

### D12. Storage, purge and events

**Directories** (`plugins/storage.py`):

- install-wide: `paths.data_dir()/plugin-data/<name>/shared`;
- per principal: `paths.user_dir(principal)/plugin-data/<name>/user`.

Both are created with `secure_mkdir(mode=0o700)` before `initialize`, which hands them over as
`data_dir` and `PrincipalContext.storage_dir`. The two paths stay distinct for the local principal,
whose `user_dir` is `data_dir()`.

```python
def install_dir(name: str) -> Path
def principal_dir(name: str, principal: Principal) -> Path
def ensure_dirs(name: str, principals: list[Principal]) -> tuple[Path, dict[str, Path]]
def remove_all(name: str) -> None      # shutil.rmtree of data_dir()/plugin-data/<name> and every
                                        # user_dir(p)/plugin-data/<name>; missing is fine
```

**Purge** (Settings action `purge_plugin_data`, sensitive):

1. If the plugin is running, send `storage.purge {scope: "all"}` and wait up to 30 s for
   `{purged: true}`.
2. Then, ack or not, `remove_all(name)`. A user's deletion request wins over a hung plugin.
3. Write a `plugin_lifecycle` audit entry with summary `"data purged (ack)"` or
   `"data purged (timeout)"`.
4. Restart the plugin if it was running.

**Uninstall.** A rescan that no longer finds an enabled or known plugin directory calls
`remove_all` directly and `state.forget(name)`, with audit `"removed; data deleted"`.

**Events** (`plugins/events.py`, `EventFanout`):

- `connector.state_changed`: `EventFanout.on_connectors_changed(rows)` is called from
  `SettingsController.refresh_connectors`'s existing listener chain. It diffs the previous and
  current `(enabled, authed)` per connector and notifies every running plugin:
  - `enabled` false→true: `enabled`;
  - true→false: `disabled`;
  - `authed` false→true while enabled: `signed_in`;
  - true→false while enabled: `signed_out`;
  - principal `"local"`.
- `plugin.disabling` and `shutdown`: sent by the supervisor's `stop` (D8).
- `principal.removed`: defined in the protocol and the SDK. Local mode never sends it, because the
  local principal cannot be removed.

All events are best-effort: a failed send is logged at debug.

### D13. Host and daemon wiring (`plugins/host.py`)

```python
class PluginHost:
    def __init__(self, *, plugins_dir: Path | None = None,
                 connectors_provider: Callable[[], dict[str, Connector]],
                 connector_state: Callable[[str], tuple[bool, str | None]],
                 registry_provider: Callable[[], PendingApprovalRegistry],
                 unattended_active: Callable[[], bool] = lambda: False,
                 trust_check: Callable[[Path, Path], str | None] = trust.admin_only_problem,
                 command_resolver: Callable[[Manifest, Path], list[str]] = manifest.resolve_command,
                 separation_enabled: Callable[[], bool] = privilege_separation.is_enabled,
                 feature_enabled: bool = True,
                 daemon_version: str = <privacyfence.__version__>) -> None
    async def start(self) -> None            # rescan, then start every enabled plugin that passes checks
    async def stop_all(self) -> None         # stop every plugin (reason "shutdown"), clear the spool
    async def rescan(self) -> None
    async def inspect(self, name: str) -> dict    # introspection start; returns the enable summary
    async def enable(self, name: str, *, executable_sha256: str, manifest_sha256: str) -> None
    async def disable(self, name: str) -> None
    async def purge(self, name: str) -> str        # "ack" | "timeout"
    def connectors(self) -> dict[str, Connector]   # running plugins' PluginConnectors
    def rows(self) -> list[dict]                    # for Settings (below)
    def set_tools_changed_listener(self, fn: Callable[[], None]) -> None
    def on_connectors_changed(self, rows: list[dict]) -> None   # forwards to EventFanout
```

- `plugins_dir` defaults to `trust.plugins_dir()`. When `feature_enabled` is false, or
  `separation_enabled()` is false, every plugin's row shows the matching D6 reason and nothing
  starts.
- Request handlers registered on every plugin's peer:
  - `source.call` → `handle_source_call` (D7);
  - `confirm.request` and `confirm.await` → `ConfirmationService` (D11).

  Notification handler: `tools.changed` (D9).
- `inspect(name)` refuses a plugin with a `problem`. It runs `Supervisor.introspect()` and returns:

  ```python
  {"name", "display_name", "version", "executable_sha256", "manifest_sha256",
   "max_gate_floor", "source_operations": [...], "pages": bool, "service_credentials": bool,
   "tools": [{"name": <mcp name>, "gate", "read_only", "destructive", "description"}]}
  ```
- `enable(...)` recomputes both hashes and refuses with `ValueError("The plugin changed since you
  reviewed it; review it again.")` if either differs from the hashes passed in. This closes the
  gap between what the human reviewed and what gets enabled. It then records state, starts the
  plugin, and audits `plugin_lifecycle` `"enabled"`.
- `disable` stops with reason `"user"`, records `disabled by you`, and audits `"disabled"`.
- Crash-limit and hash-drift disables are recorded and audited (`"disabled: <reason>"`).
- `rows()` gives one dict per discovered plugin: `{name, display_name, version, state, reason,
  enabled, pages, page_url ("/plugins/<name>/" when pages and running, else ""), tools_note}`.
- **Daemon wiring** (`daemon_main.run_app`, local mode only):
  1. Construct `PluginHost` after `SettingsController` (`connector_state` reads
     `settings_controller`'s connector rows).
  2. Pass it to `SettingsController` (new kwarg `plugin_host=None`) and to
     `_maybe_start_web_server` (new kwarg `plugin_host=None`, forwarded to `WebServer` and
     `build_app`, which store it).
  3. `_connectors()` returns `{**connector_host.connectors, **plugin_host.connectors()}` for the
     local principal.
  4. `plugin_host.set_tools_changed_listener(mcp_dispatcher.notify_tools_changed)`.
  5. Start the host once the web server is ready, by scheduling `plugin_host.start()` on the
     server's event loop. Find how `server.wait_until_ready` exposes the loop. If no loop is
     reachable from `run_app`, run the host on its own asyncio loop in a daemon thread named
     `plugin-host`, and route every host call from web handlers through
     `asyncio.run_coroutine_threadsafe`. Record which one you used in the PHASE-REPORT.
  6. In `run_app`'s `finally`, call `plugin_host.stop_all()` before `audit_logger.close()`,
     bounded to 10 s.

  In org mode no host is built, and plugin routes are not mounted.
- **Config:** `plugins.enabled` (default `true`). Add it to `resources/settings.yaml.example`
  (under `plugins:` with a one-line comment) and to `docs/configuration-reference.md` as
  `` `plugins.enabled` ``.

### D14. Plugin pages (`web/routes_plugins.py`, `plugins/pages.py`)

- Route `GET /plugins/{name}/{path:path}`, plus `GET /plugins/{name}` redirecting to
  `/plugins/{name}/`. It is mounted in local mode only, as one route module with the local auth
  adapter (ADR 0033), behind the same owner-only, human-session guard that the Settings page uses
  (`_owner_only_routes` and `session_auth.is_human_session`).
  - An MCP bearer or no session gets 403, with the same response the settings page gives.
  - Any other method gets 405, with `Allow: GET`, without reaching the plugin.
  - A plugin that is not running, or has `pages: false`, gets 404.
- Path normalization (`pages.normalize_path(raw: str) -> str | None`): URL-decode once. Reject
  `..` segments, NUL, a backslash, `//`, and more than `MAX_PAGE_PATH_CHARS`. Always start with
  `/`. A rejected path gets 400 and never reaches the plugin.
- `query` is the parsed query string: last value wins, strings only.
- `pages.filter_response(result: dict) -> tuple[int, dict[str, str], bytes]`:
  - status in {200, 204, 400, 404, 500}, else 502;
  - `content-type` from an allowlist (`text/html`, `text/plain`, `text/css`,
    `application/javascript`, `application/json`, `image/png`, `image/svg+xml`, `image/jpeg`,
    each optionally followed by `; charset=utf-8`), else `application/octet-stream`;
  - `cache-control` is dropped and always replaced (below);
  - the body is decoded per `body_encoding`, and over `MAX_PAGE_BODY_BYTES` it becomes 502;
  - plugin `set-cookie`, CSP and every other header are dropped.

  An RPC timeout or error becomes 502 with a plain-text body `"The plugin did not answer."`.
- **Headers** on every `/plugins/` response, success or error:

  ```
  Content-Security-Policy: sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Cache-Control: private, no-store
  X-Frame-Options: DENY
  ```

  `_SecurityHeadersMiddleware` (`web/server.py:407`) gets a branch: for a path starting with
  `/plugins/`, set this CSP instead of `build_csp(...)`. The other headers it sets stay. There is
  no `allow-same-origin`, so the page runs in an opaque origin and cannot read cookies or call the
  app's APIs.
- Settings links to `/plugins/<name>/` for a running plugin with `pages: true`.

### D15. Dynamic policy registration (`auto_accept.py`, `policy/*`, `gate.py`, `write_effects.py`)

New API in `auto_accept.py`:

```python
@dataclass(frozen=True)
class DynamicToolSpec:
    tool: str                 # MCP tool name
    gate: str                 # "auto" | "review" | "popup"
    operation: str | None     # operation key for review/popup, else None
    verb: "Verb | None"
    layout: str               # gate.WIDE | gate.NARROW
    effect: str               # "" for read-only tools
    scope_predicates: tuple[tuple[str, str], ...]   # (predicate, scope_type) per declared scope

def register_dynamic_tools(owner: str, specs: list[DynamicToolSpec]) -> None
def unregister_dynamic_tools(owner: str) -> None
def reset_dynamic_tools() -> None    # unregisters every owner; registered with plugins._testing
```

`register_dynamic_tools`:

- writes into `TOOL_TO_GATE` and `TOOL_TO_OPERATION`;
- calls `policy.registry.register_dynamic(tool, operation, verb, gate)`, which adds to
  `TOOL_TO_VERB` and `TOOL_REGISTRY`;
- calls `gate.register_dynamic_layout(tool, layout)`, which writes `_TOOL_LAYOUT`;
- calls `write_effects.register_dynamic_effect(tool, effect)`, which writes `EFFECT_BY_TOOL`;
- calls `policy.scopes.register_plugin_selector(predicate, scope_type)`, which adds a
  `ScopeSelector` to `NEW_SCOPE_SELECTORS`;
- calls `policy.propose.register_dynamic_scopes(owner, tool, predicates)`.

It refuses (`ValueError`) a tool name that already exists in `TOOL_TO_GATE` under another owner,
or that is static. It remembers what it added per owner, and `unregister_dynamic_tools` removes
exactly those entries. Static entries are never touched. Import cycles are avoided with
function-local imports, where `auto_accept.py` already uses them.

**Plugin scope selector** (`policy/scopes.py`):

```python
def register_plugin_selector(predicate: str, scope_type: str) -> None
def unregister_plugin_selector(predicate: str) -> None

def _plugin_scope_matches(scope_type: str) -> Callable[[Any, ReviewContext], bool]:
    def matches(value, ctx):
        returned = (ctx.raw_data or {}).get("scopes", {}).get(scope_type) if isinstance(ctx.raw_data, dict) else None
        allowed = {str(v) for v in _values_of(value) if v not in (None, "")}
        return bool(returned) and bool(allowed) and set(map(str, returned)) <= allowed
    return matches
# ScopeSelector(predicate, scope_type=predicate, kind=ScopeKind.IDENTITY,
#               resolves_from=ResolvesFrom.FETCHED, matches=_plugin_scope_matches(scope_type))
```

A rule matches only when every value the call returned is in the rule. A missing scope, an empty
list or an empty rule value never matches.

**Proposals** (`policy/propose.py`). `register_dynamic_scopes(owner, tool, predicates)` adds
`ProposableScope` entries to a dynamic table that `proposals_for` consults after
`PROPOSABLE_SCOPES` when `TOOL_REGISTRY[tool].operation` starts with `"plugin."`. Each entry
covers only that one operation:

- per declared scope type: `_scope(predicate, scope_type=predicate, connector=<plugin>,
  verbs=(verb,), value_of=lambda ctx: sorted(ctx.raw_data["scopes"][scope_type]) or NO_VALUE,
  hint=f"these {scope_type} values" if >1 else f"this {scope_type}", entry_id=f"{predicate}@{operation}",
  group=f"{predicate}@{operation}", widenable=False)`;
- a tool with no scope types: one `always_allow` entry, `scope_type=f"{plugin}.anything"`,
  `group=f"always_allow@{operation}"`, `hint=""`, `widenable=False`.

`_candidate_value` must accept these entries. A dynamic entry confirms its own value by
construction. `rules_for_proposal` must find the dynamic group in `_SCOPES_BY_GROUP`, or an
equivalent dynamic lookup. Unregister removes them. `connector_of_operation("plugin.<p>.<t>")`
must return `<p>`. If it splits on the first dot and returns `"plugin"`, special-case the
`plugin.` prefix.

**Not in protocol 1:** plugin scopes are **not** added to `policy/catalogue.py`, so
`privacyfence_propose_policy_change` cannot write plugin rules. Only the card's "Always allow"
button does. The generated `docs/always-allow-rules-reference.md` is unchanged, because dynamic
entries are not static.

**Property test** (`tests/unit/policy/test_plugin_scopes.py`, using `hypothesis`, which is already
a test extra): for random scope values, a rule built by `rules_for_proposal(proposals_for(tool,
ctx)[0])` matches the call it came from (`engine.find_matching_rule`), and does not match a call
that returned one extra value.

### D16. SDK (`plugin-sdk/`)

**Layout:**

```
plugin-sdk/pyproject.toml
plugin-sdk/README.md
plugin-sdk/src/privacyfence_plugin_sdk/__init__.py      # public names below
plugin-sdk/src/privacyfence_plugin_sdk/types.py         # generated
plugin-sdk/src/privacyfence_plugin_sdk/_rpc.py          # stdio JSON-RPC peer (asyncio), same framing
plugin-sdk/src/privacyfence_plugin_sdk/plugin.py        # Plugin, ToolHandle, Prepared, Context
plugin-sdk/src/privacyfence_plugin_sdk/blocks.py        # builders + local validation, same rules as D4
plugin-sdk/src/privacyfence_plugin_sdk/responses.py     # Html, Text, Bytes, DownloadedFile, errors
plugin-sdk/src/privacyfence_plugin_sdk/py.typed
plugin-sdk/src/privacyfence_plugin_sdk/testing/__init__.py     # PluginTestHost etc.
plugin-sdk/src/privacyfence_plugin_sdk/testing/samples/*.json  # one per source operation
plugin-sdk/src/privacyfence_plugin_sdk/testing/pytest.py      # plugin_host fixture
```

**`plugin-sdk/pyproject.toml`:**

- `name = "privacyfence-plugin-sdk"`, `requires-python = ">=3.11"`, `dependencies = []`;
- `dynamic = ["version"]` with `[tool.setuptools_scm] root = ".."` and `fallback_version =
  "0.0.0"`;
- build backend `setuptools.build_meta` with `setuptools>=69` and `setuptools-scm>=8`, matching
  the root `pyproject.toml`'s pins;
- `[tool.setuptools.package-data]` includes `testing/samples/*.json` and `py.typed`;
- `license = "Apache-2.0"`.

**Public API** (`from privacyfence_plugin_sdk import …`): `Plugin`, `Prepared`, `Html`, `Text`,
`Bytes`, `blocks`, `SourceError`, `ConfirmResult`, `DownloadedFile`, `PROTOCOL_VERSION`.

```python
plugin = Plugin(name="today", version="1.2.0")
plugin.scope_type("calendar", "Calendar id a call reads")

@plugin.tool("list_events", gate="review", read_only=True, scopes=["calendar"],
             description="…", params={"calendar_id": {"type": "string", "description": "…"}},
             required=[], title="List today's events", effect=None, destructive=False)
async def list_events(ctx, args) -> Prepared: ...

@list_events.execute                       # optional for read-only tools
async def run(ctx, prepared, approval) -> Any: ...

@plugin.page("/")                          # exact path match; "/" also serves ""
async def home(ctx, request) -> Html | Text | Bytes: ...

@plugin.on("connector.state_changed")      # or plugin.disabling, shutdown, principal.removed
async def stale(ctx, params) -> None: ...

@plugin.on_purge
async def purge(ctx, scope, principal) -> None: ...

plugin.run()                                # asyncio stdio loop until shutdown or EOF
await plugin.tools_changed()                # sends tools.changed with the current registry
```

- `Prepared(preview, payload=None, scopes=None, state=None)`. `state` stays in the plugin, keyed by
  `call_id`, for `PREPARED_CALL_LIFETIME_SECONDS`.
- On execute the SDK checks `args_digest` against the prepared args and answers `digest_mismatch`
  or `unknown_call` itself.
- `ctx` attributes:
  - `ctx.principal`: `.id`, `.display_name`, `.storage_dir: Path`;
  - `ctx.data_dir: Path`;
  - `ctx.introspecting: bool`;
  - `ctx.source.call(op, **params) -> SourceResult(data, bytes, next_cursor)`;
  - `ctx.source.download(file_id, dest: Path | None = None) -> DownloadedFile(path, size,
    revision, mime_type)`. It loops over the cursor, restarts once on `revision_changed`, and
    writes into `dest` or `ctx.data_dir/downloads/`;
  - `ctx.confirm.request(kind, title, preview, require_step_up=True) -> str`;
  - `ctx.confirm.await_(approval_id, timeout_ms=None) -> ConfirmResult(status, deny_note,
    decided_at)`.
- `SourceError(code, detail, reason)`.
- `blocks.heading(text, level=2)`, `blocks.fields(mapping)`, `blocks.table(columns: list[tuple[key,
  label]], rows: list[dict])`, `blocks.text(s)`, `blocks.code(s, language=None)` and
  `blocks.diff(s)` validate with the same rules as D4. They raise `ValueError`.
- The SDK validates its own tool definitions with the D9 rules before `initialize` returns, and
  raises `ToolDefinitionError` at registration.

**`types.py`** is generated by `scripts/gen_plugin_sdk_types.py` from `protocol.schema.json`. It
emits `TypedDict`s per `$defs` entry and `Literal`s for enums, with a header line `# Generated by
scripts/gen_plugin_sdk_types.py; do not edit.` The script's `--check` exits 1 when the file is
stale. The test `tests/unit/test_gen_plugin_sdk_types.py` runs `--check`.

**Test host** (`privacyfence_plugin_sdk.testing`, a public, semver-stable API):

- `PluginTestHost(plugin, mode="local", principals=None)` is an async context manager. It runs the
  plugin's real runner over an in-memory stream pair and plays the daemon side with the same
  rules:
  - tool definition floors (D9), raising `ToolDefinitionError` at start;
  - block validation and limits (D4, D2);
  - a simulated gate: auto, review card or popup card, decided by `decide`;
  - scope rules with D15's matching;
  - source fixtures;
  - pages with D14's headers, 405 for anything but GET, and path normalization;
  - confirmations, events, purge and shutdown.
- `host.tools`, `await host.introspect()`.
- `await host.call_tool(name, args, decide="approve"|"deny"|callable, principal=None) ->
  ToolOutcome(gate, card_shown, card(preview, payload, scopes), released, result, approval,
  audit: list[dict], error)`. `name` is the plugin's own tool name.
- `host.rules.allow_scope(scope_type, values)`.
- `host.source.load(fixture: dict)`, `.when(op, **params).returns(data)`, `.fail(op, code,
  reason=None)` and `.calls`. A call with no match raises `SourceFixtureMissing`.
- `samples.get(op) -> dict` and `samples.drive_download(data: bytes, *, revision="r1") ->
  fixture`. The latter serves chunks of `DRIVE_CHUNK_BYTES` with cursors exactly as D7 does.
- `await host.get(path)`, `await host.request(method, path) -> PageResponse(status, headers,
  body)`.
- `host.confirmations`, `await host.decide_confirmation(approval_id, "approve"|"deny",
  deny_note=None)`.
- `await host.emit(event, params)`, `await host.purge(scope="all")`, `await host.shutdown()`.
- `privacyfence_plugin_sdk.testing.pytest` defines the fixture `plugin_host`, which imports
  `pytest` lazily.

**Samples.** The spec wanted samples copied from the daemon's live fixtures. There are none for
four of the six operations, and the normalized shapes (D7) are produced by the daemon. So the
samples are **hand-written, redacted examples** committed under `testing/samples/`, one per
operation, with the exact `data` shape of D7. `tests/unit/plugins/test_sdk_samples.py` (p16) runs
each sample's `data` through the D7 adapter's shape check, so a drift between daemon and samples
fails CI. This **replaces** the spec's `scripts/sync_sdk_fixtures.py`, which is not built.

**SDK tests** live under `tests/unit/plugin_sdk/`, so `tests.yml` runs them with no workflow
change. `tests/unit/plugin_sdk/conftest.py` puts `plugin-sdk/src` on `sys.path`. The daemon's
`pyproject.toml` gains no dependency.

**Release.** `publish-pypi.yml` gains:

- a `build-sdk` job (`python -m build plugin-sdk`, artifact `plugin-sdk-dist`), running alongside
  `build`;
- `publish-sdk-testpypi` (environment `testpypi`), and then `publish-sdk-pypi` (environment
  `pypi`).

Each is gated exactly like the existing publish jobs: `needs` on `wait_for_build`, `dedupe` and
the stable-channel condition. They use the same pinned `pypa/gh-action-pypi-publish` and
`id-token: write`. The SDK never reaches R2. `docs/releasing.md` "Publishing to PyPI" gets the
second pending-publisher registration (project `privacyfence-plugin-sdk`, same workflow and
environments).

### D17. Example plugin `today` and reference plugin `echo`

**`echo`** (`tests/fixtures/plugins/echo/`, test-only, built on the SDK). Its manifest is
`echo/privacyfence-plugin.yaml` with `name: echo`, `max_gate_floor: review`, `pages: true` and
`source_operations: [calendar.list_events]`. Tests launch it with a `command_resolver` that returns
`[sys.executable, <dir>/echo_plugin.py]` and `PYTHONPATH=plugin-sdk/src`.

Its tools:

| Tool | Gate | Read-only | Destructive | Scopes |
|---|---|---|---|---|
| `auto_read` | auto | yes | no | — |
| `review_read` | review | yes | no | `dataset` |
| `popup_write` | popup | no | no | — |
| `destructive` | popup | no | yes | — |
| `confirm` | popup | no | no | — (opens a confirmation, returns `approval_id`) |
| `source` | review | yes | no | — (reads `calendar.list_events`) |

It also has a page at `/` (with an inline script that writes `document.cookie` into the DOM) and
event handlers that record what they received. Manifest variants for refusals live under
`tests/fixtures/plugins/echo-variants/<case>/`.

**`today`** (`examples/plugins/today/`): the P12 table of the issue, with these exact tools:

| Tool | Gate | Read-only | Notes |
|---|---|---|---|
| `status` | auto | yes | No event content |
| `refresh` | auto | no | Manifest `max_gate_floor: auto` |
| `list_events` | review | yes | Scope `calendar`; table payload |
| `add_note` | popup | no | Stored by the plugin only |
| `clear_notes` | popup | no | Destructive |
| `publish` | popup | no | `confirm.request` with a heading and a diff; returns `approval_id` |
| `crash` | auto | no | Only when `build-flags.json` has `"crash_tool": true` |

- Storage: per-principal `day.json` and `notes.json`, and install-wide `counter.json`.
- Page `/`: the published day, the notes, a stale banner, a `code` block with the raw manifest,
  and an inline script that shows whether `document.cookie` is readable.
- Events:
  - `connector.state_changed` for `calendar` marks the day stale;
  - `storage.purge` deletes its files;
  - `plugin.disabling` and `shutdown` flush the counter.
- `--self-test` prints `today ok protocol 1.0.0` and exits 0, without stdio.

`scripts/build_example_plugin.py today [--with-crash-tool] [--out DIR]` builds a PyInstaller
one-file executable `today-plugin` (`today-plugin.exe` on Windows) for the current OS into
`DIR/today/`, default `dist/plugins`. It also writes the manifest and `build-flags.json` there and
prints the folder. `--help` exits 0.

`build.yml`'s three jobs each gain one step after their own build:

```
python scripts/build_example_plugin.py today --out dist/plugins
```

followed by `dist/plugins/today/today-plugin --self-test` (on Windows,
`dist\plugins\today\today-plugin.exe --self-test`). Nothing is uploaded and nothing ships in an
installer.

### D18. Things deliberately not done

- No configurable plugins directory, and no environment override (trust boundary, ADR 0121).
- No org-mode plugin host, routes or service sign-ins. Those protocol fields are rejected.
- No write operations in the source API, and no raw variants of normalized operations.
- No `jsonschema` dependency, and no SDK fixture sync script.
- Plugin scopes are not proposable through `privacyfence_propose_policy_change`.
- No dedicated plugin OS account or sandboxing. The residual risk is recorded in ADR 0121.

## ADRs

The retirement phase writes these, numbered from the next free number. 0119 is the last today; if
a parallel branch takes numbers first, renumber.

- **0120.** Plugins are out-of-process executables speaking newline-delimited JSON-RPC 2.0 over
  stdio. Rejected: importing plugin packages in-process (the daemon is a frozen app), a local
  socket or port (another surface to secure), and Content-Length framing (harder for other
  languages, no benefit at these sizes).
- **0121.** A plugin is trusted code, installed by an administrator into an admin-only directory
  outside the app and the data root. The executable, the directory and its ancestors are checked
  on every start; sha256 hashes are recorded at enable, and drift disables the plugin. Plugins run
  only with privilege separation, enabling is sensitive, and the directory is not configurable.
  Residual risk: the service account can read credential files.
- **0122.** Plugin tools are gated in two steps: prepare returns typed blocks (never HTML), the
  daemon gates, and execute follows. A read releases the prepared payload itself, and a pending
  prepared call is reused until it is decided. Floors are enforced by the daemon.
- **0123.** The source API is ungated but audited without content. Results are raw only for
  Salesforce report runs and Drive bytes; Jira, Confluence and Calendar results are the daemon's
  normalized shapes. Drive downloads are chunked from a spool file. Rejected: raw provider JSON
  for every operation, and a higher single-message cap.
- **0124.** Plugin pages are GET-only, human-session-only, under `sandbox allow-scripts` without
  `allow-same-origin`, with plugin headers filtered.
- **0125.** The org-mode contract (roles, service sign-ins, admin-only management) is reserved in
  protocol 1 and rejected in local mode. Org mode does not start the host.
- **0126.** The SDK lives in this repository and is published from the same tag, with no runtime
  dependencies and a public test host whose samples are checked against the daemon. Rejected: a
  separate SDK repository, which would make the schema and SDK drift.

## Manual steps

The step-by-step page is the artifact linked in the manifest (`manual_steps_artifact`).

- **Before:** `mb1-pypi-pending-publishers`. Register `privacyfence-plugin-sdk` as a pending
  trusted publisher on TestPyPI and on PyPI. Nothing in the build needs it. Doing it first means
  the next stable tag publishes the SDK without a failed job.
- **After:**
  - `ma1-smoke-test-today`: the ten-step smoke test of `today` on a packaged install.
  - `ma2-macos-windows-plugin-dir`: check that the Settings Plugins section reads the right
    directory on macOS and Windows packaged installs. Folded into ma1 when ma1 runs on both.

Everything else runs in phases or on runners. `build.yml` is dispatched by the orchestrator in the
final checks, and the platform tests run in `tests.yml` on every push.

## Risks and open questions

Each risk has what a worker sees if it applies. Stop with `status=blocked` rather than
improvising.

- **Card wiring for `preview_blocks`.** D10 assumes `gated_call(preview_blocks=…)` reaches
  `build_preview_body_html(blocks=…)` for a WIDE card. If the card never shows the blocks (p12's
  `TestCardShowsPayload` fails and no `gated_call` parameter carries them), stop.
- **Telling auto-accepted from card-approved in `gated_call`.** If it is not observable, use
  `"card"` (D10) and report it. Do not change `gate.py` for it.
- **`connector_of_operation` and `_SCOPES_BY_GROUP`** may be shaped differently from D15's
  assumption. p4 is an Opus phase. If a dynamic entry cannot be added without changing the
  semantics of static entries, stop.
- **Event loop for the host** (D13 step 5). Either option is acceptable; record the choice. If
  neither works without changing `WebServer`'s threading model, stop.
- **`DriveClient` metadata method name** (D7). If `get_file_metadata` does not exist under that
  name, use the client's existing metadata call that returns `headRevisionId` or `modifiedTime`.
  If there is none, stop.
- **Coverage floor.** New modules must keep `scripts/check_coverage_floor.py` green. Every phase
  measures its own modules with `--cov=src/privacyfence/plugins` and gets them to ≥ 95% line and
  branch. OS-specific branches use `monkeypatch` on `sys.platform`, as `TestElevationScriptProblem`
  does, or `# pragma: no cover -- <reason>` where a branch truly cannot run on Linux.
- **Windows CI.** `platform-windows` runs the whole suite. Process-spawning tests must use
  `sys.executable` and no shebang, use `tmp_path` with no POSIX-only permission asserts unless
  marked, and fail within the 30 s test timeout.
- **History rule.** A worker that writes "D10", "p12", "Phase", "#846" or a section sign into
  code, tests or a workflow fails `test_code_no_history.py`. Use ADR numbers (0120-0126) or the
  issue's full URL.

## Implementation manifest

```yaml
plan_slug: plugin-framework
feature_branch: feature/plugin-framework
tracking_issue: 846
max_parallel: 3
manual_steps_artifact: https://claude.ai/artifact/HWFGVdnbDPW4kkE75kwLz5
manual_steps_source: docs/plugin-framework-plan-manual-steps.html
manual_before:
  - id: mb1-pypi-pending-publishers
    title: Register privacyfence-plugin-sdk as a pending trusted publisher on TestPyPI and PyPI
    why: The SDK publish jobs added by p10 use OIDC trusted publishing (ADR 0020), which cannot create a project on its own; without this the first stable tag after the merge fails the SDK publish jobs.
    done_when: Both test.pypi.org and pypi.org list a pending publisher for project privacyfence-plugin-sdk, owner privacyfence, repository privacyfence, workflow publish-pypi.yml, environment testpypi (TestPyPI) and pypi (PyPI).
manual_after:
  - id: ma1-smoke-test-today
    title: Run the ten-step smoke test of the today plugin on a packaged install
    why: CI cannot drive a packaged install with a real Calendar sign-in, a passkey prompt and a real administrator-only plugins directory.
  - id: ma2-plugin-dir-other-os
    title: Repeat smoke-test steps 1 and 2 on a second OS (macOS or Windows) packaged install
    why: The admin-only directory check and the plugins directory differ per OS; unit tests mock them.
verify_after_merge:
  - ruff check .
  - python3 -m pytest tests/unit/plugins tests/unit/plugin_sdk tests/unit/policy tests/unit/test_auto_accept.py tests/unit/test_code_no_history.py tests/unit/test_docs_references_exist.py -q
final_checks:
  - docs/plugin-framework-plan.md and docs/plugin-framework-plan-manual-steps.html are deleted and nothing links to them
  - docs/adr/0120-*.md to docs/adr/0126-*.md (or the next free numbers) exist with Status Accepted and are in docs/adr/README.md's index
  - CHANGELOG.md has an [Unreleased] entry for the plugin framework and no new version heading
  - The full /dod passes, including python3 scripts/check_coverage_floor.py coverage.json and python3 -m pytest tests/integration -v (test_mcp_daemon_contract.py, test_plugin_framework.py, test_plugin_refusals.py, test_sdk_testhost_conformance.py)
  - build.yml dispatched against feature/plugin-framework is green in all three platform jobs, including the today build and self-test steps (link the run in the PR)
  - python -m build plugin-sdk succeeds and python3 scripts/gen_plugin_sdk_types.py --check exits 0
phases:
  - id: p1-protocol-core
    title: Protocol constants, message types, JSON schema, test reset registry
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/plugins/__init__.py
      - src/privacyfence/plugins/constants.py
      - src/privacyfence/plugins/protocol.py
      - src/privacyfence/plugins/_testing.py
      - docs/plugin-protocol/protocol.schema.json
      - tests/conftest.py
      - tests/unit/plugins/__init__.py
      - tests/unit/plugins/test_constants.py
      - tests/unit/plugins/test_protocol.py
      - tests/unit/plugins/test_testing_registry.py
    brief: |
      1. Create src/privacyfence/plugins/__init__.py (docstring "Out-of-process plugins (ADR 0120)." and the
         __future__ import) and constants.py with exactly the code in the plan's Design D2 (the constants block
         and the three name helpers), plus a module docstring. Note the ADR numbers 0120-0126 are cited in
         docstrings even though the ADR files are written in the last phase; that is intended.
      2. Create _testing.py with register_reset and reset_all as in D2. Add one line to tests/conftest.py's
         _reset() function: `plugins._testing.reset_all()` (import `from privacyfence import plugins` and
         `import privacyfence.plugins._testing` the way the file imports other modules).
      3. Create protocol.py per D3: RpcError (code name, detail, retryable, extra; to_error()), the dataclasses
         PrincipalContext, ToolDef, InitializeResult, PrepareResult, ExecuteResult, SourceCallParams,
         ConfirmRequestParams, WebResponse with from_wire(obj, *, mode="local") and to_wire(), args_digest(args),
         and principal_context(principal, storage_dir, *, mode="local"). Validators are hand-written: types,
         required keys, D2 patterns and limits; unknown keys ignored; credential/roles in local mode raise
         RpcError("org_only_field"). Blocks inside preview/payload are only checked to be lists of dicts here;
         full block validation is blocks.py (next phase), so PrepareResult.from_wire accepts a
         `validate_blocks` callable parameter defaulting to a no-op.
      4. Write docs/plugin-protocol/protocol.schema.json (JSON Schema 2020-12) with every $defs entry listed in
         D3 and the field tables of D3/D4/D5, `additionalProperties: false` on Block variants and Manifest, and an
         `x-limits` object holding the D2 numbers the protocol exposes.
      5. Tests: test_constants.py (RESERVED_PLUGIN_NAMES ⊇ settings_controller.ALL_CONNECTORS; regexes accept/
         reject edge cases; name helpers); test_protocol.py (one class per dataclass with valid and invalid
         transcripts copied from D3's tables, args_digest stable under key order, org_only_field in local mode,
         and the schema check: json.load the schema and assert each listed $defs entry's property names equal
         the fields its dataclass handles); test_testing_registry.py (idempotent register, order, reset_all).
         Module docstrings name the invariant (e.g. "validators fail closed on malformed plugin output").
      6. No CHANGELOG line in this phase (the retire phase writes it).
      Stop condition: if tests/conftest.py's _reset() is not a plain function you can add a line to, stop.
    acceptance:
      - python3 -m pytest tests/unit/plugins -q passes
      - python3 -c "import json;json.load(open('docs/plugin-protocol/protocol.schema.json'))" exits 0
      - python3 -m pytest tests/unit/plugins --cov=src/privacyfence/plugins --cov-branch -q reports >= 95% for constants.py, protocol.py, _testing.py
      - ruff check . passes and python3 -m pytest tests/unit/test_code_no_history.py -q passes

  - id: p2-blocks-manifest
    title: Block validation and card conversion, manifest loader, audit decision docs
    depends_on: [p1-protocol-core]
    complexity: M
    touches:
      - src/privacyfence/plugins/blocks.py
      - src/privacyfence/plugins/manifest.py
      - src/privacyfence/audit_log.py
      - tests/unit/plugins/test_blocks.py
      - tests/unit/plugins/test_manifest.py
    brief: |
      1. blocks.py exactly per Design D4: BlockError, validate_blocks (sanitizing controls and the listed bidi
         characters, cell truncation, caps), to_card_blocks (mapping table in D4), fields_dict, flatten_text.
      2. manifest.py per D5: Manifest dataclass, ManifestError, MANIFEST_FILENAME, load_manifest (yaml.safe_load,
         unknown keys are errors, every field rule in D5's YAML comments, name must equal plugin_dir.name,
         service_credentials true rejected in mode "local"), resolve_command (inside-dir check after resolve(),
         ".exe" appended on Windows when command[0] has no suffix -- test by monkeypatching sys.platform).
      3. audit_log.py: comment-only change. In the decision-values comment block (around lines 139-310) document
         "plugin_source", "plugin_confirm" and "plugin_lifecycle" in the style of the existing entries, with what
         connector/tool/summary hold for each (D7, D11, D12, D13). No code change, no schema bump.
      4. Tests: test_blocks.py (TestValidate per type incl. unknown type/extra field rejected, TestSanitize with
         test_bidi_override_removed and test_html_in_text_is_kept_as_text (to_card_blocks keeps "<b>" as literal
         text), TestCaps, TestToCardBlocks, TestFieldsDict duplicate labels, TestFlatten); test_manifest.py (valid
         manifest, each invalid field, unknown key, reserved name, name/dir mismatch, command escaping via ".." and
         via a symlink (skip the symlink case where os.symlink is unavailable), Windows .exe).
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_blocks.py tests/unit/plugins/test_manifest.py tests/unit/test_audit_log.py -q passes
      - TestSanitize::test_bidi_override_removed and TestSanitize::test_html_in_text_is_kept_as_text pass
      - coverage of blocks.py and manifest.py >= 95% (python3 -m pytest tests/unit/plugins --cov=src/privacyfence/plugins --cov-branch)

  - id: p3-rpc-supervisor
    title: JSON-RPC peer and process supervisor
    depends_on: [p1-protocol-core]
    complexity: M
    touches:
      - src/privacyfence/plugins/rpc.py
      - src/privacyfence/plugins/supervisor.py
      - tests/unit/plugins/test_rpc.py
      - tests/unit/plugins/test_supervisor.py
      - tests/fixtures/plugins/stub/**
    brief: |
      1. rpc.py: RpcPeer exactly per Design D3 (framing, line cap via StreamReader limit and an explicit length
         check, independent id spaces, in-flight caps both directions, per-method timeouts from TIMEOUT_SECONDS,
         batch -> invalid_request, INVALID_LINES_LIMIT consecutive bad lines -> close with "invalid_output",
         handler RpcError vs other exceptions). RpcError is imported from protocol.py.
      2. supervisor.py: LaunchSpec, child_env(), Supervisor exactly per D8 (spawn flags per OS, log rotation and
         0600, handshake checks in order -- the tool-definition check calls a `validate_tools` callable passed in
         the constructor (add the parameter `validate_tools: Callable[[InitializeResult], None]`, default no-op;
         the host passes tools.validate_tool_defs later), crash window and backoff with injected clock/sleep,
         introspect(), stop sequence). The `initialize` params come from the injected callable.
      3. tests/fixtures/plugins/stub/stub_plugin.py: stdlib-only stub with the modes listed in D8, speaking the D3
         framing.
      4. Tests: test_rpc.py over an in-memory pipe pair (two asyncio StreamReader/Writer pairs or
         asyncio.open_connection on a socketpair): round trip both directions, timeout, error mapping, batch,
         junk lines closing after 3, in-flight cap, notifications ignored when unknown. test_supervisor.py with the
         stub plugin as a real child process ([sys.executable, stub_plugin.py, mode]): TestHandshake
         (ok, wrong-name), TestMajorMismatch::test_plugin_not_started, TestCrashLimit::test_disabled_after_five_in_ten_minutes
         (injected clock and a sleep that returns immediately), TestBackoffSequence, TestShutdown::test_grace_then_kill
         (slow-shutdown mode), TestEnvironment::test_daemon_env_not_inherited (set a sentinel env var in the test
         process; echo-env mode must not show it), TestJunkStdout (counts as a crash), TestLog (0600 on POSIX only,
         rotation).
      Every process test must finish well under the 30 s pytest timeout and must run on Windows (no shebangs, no
      signals other than terminate/kill).
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_rpc.py tests/unit/plugins/test_supervisor.py -q passes
      - TestCrashLimit::test_disabled_after_five_in_ten_minutes, TestShutdown::test_grace_then_kill, TestMajorMismatch::test_plugin_not_started and TestEnvironment::test_daemon_env_not_inherited pass
      - coverage of rpc.py and supervisor.py >= 95%

  - id: p4-policy-dynamic
    title: Dynamic tool registration and the plugin scope selector in the policy tables
    depends_on: [p1-protocol-core]
    complexity: M
    worker_model: opus
    worker_model_reason: It extends the policy engine's static tables and the "Always allow" proposal path; a proposal that matches more than the call it came from silently widens what auto-accepts.
    touches:
      - src/privacyfence/auto_accept.py
      - src/privacyfence/policy/registry.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/gate.py
      - src/privacyfence/write_effects.py
      - tests/unit/test_auto_accept.py
      - tests/unit/policy/test_plugin_scopes.py
      - tests/unit/policy/test_registry.py
      - tests/unit/test_write_effects.py
    brief: |
      1. Read Design D15 and the Current state "Tools, gate and policy" bullets. Implement in auto_accept.py:
         DynamicToolSpec, register_dynamic_tools, unregister_dynamic_tools, reset_dynamic_tools; register
         reset_dynamic_tools with privacyfence.plugins._testing.register_reset at import of auto_accept (a
         function-local import is fine if a cycle appears).
      2. policy/registry.py: register_dynamic(tool, operation, verb, gate) / unregister_dynamic(tool) adding to
         and removing from TOOL_TO_VERB and TOOL_REGISTRY using the same entry type _build_registry produces.
      3. gate.py: register_dynamic_layout(tool, layout) / unregister_dynamic_layout(tool) on _TOOL_LAYOUT. No
         other gate.py change.
      4. write_effects.py: register_dynamic_effect(tool, effect) / unregister_dynamic_effect(tool) on
         EFFECT_BY_TOOL. tests/unit/test_write_effects.py's coverage check must still pass for static tools and
         must ignore dynamic ones (they are removed by reset between tests).
      5. policy/scopes.py: register_plugin_selector / unregister_plugin_selector and _plugin_scope_matches exactly
         as D15.
      6. policy/propose.py: register_dynamic_scopes(owner, tool, predicates) / unregister_dynamic_scopes(owner),
         the dynamic table consulted by proposals_for after PROPOSABLE_SCOPES for "plugin." operations,
         _candidate_value accepting dynamic entries, rules_for_proposal finding their groups, and
         connector_of_operation returning the plugin name for "plugin.<p>.<t>". Do not change any static entry's
         behaviour. Do not touch policy/catalogue.py (D15 "Not in protocol 1").
      7. Tests: tests/unit/policy/test_plugin_scopes.py with TestPluginScope::test_matching_values_accepted,
         TestPluginScope::test_non_matching_value_not_accepted, TestPluginScope::test_empty_scope_never_matches,
         TestPluginScope::test_missing_scope_never_matches, TestProposals::test_scoped_tool_offers_scope_rule,
         TestProposals::test_unscoped_tool_offers_whole_tool_rule, TestProposals::test_proposal_covers_one_operation,
         and the hypothesis property test from D15 (TestProposalMatchesItsCall). Extend test_auto_accept.py with
         TestDynamicTools (register, unregister restores the exact prior tables, duplicate/static name refused,
         reset_all clears). Extend tests/unit/policy/test_registry.py for register_dynamic.
      Stop condition: if adding dynamic entries requires changing how any static PROPOSABLE_SCOPES entry is
      proposed or matched, stop with status=blocked and describe the conflict.
    acceptance:
      - python3 -m pytest tests/unit/test_auto_accept.py tests/unit/policy tests/unit/test_write_effects.py tests/unit/test_gate.py tests/unit/test_generate_always_allow_reference.py -q passes
      - TestPluginScope::test_non_matching_value_not_accepted, TestPluginScope::test_empty_scope_never_matches and TestProposalMatchesItsCall pass
      - git diff --stat origin/main -- src/privacyfence/policy/catalogue.py shows no change

  - id: p5-sdk-core
    title: privacyfence-plugin-sdk package (runtime, blocks, generated types)
    depends_on: [p1-protocol-core]
    complexity: M
    touches:
      - plugin-sdk/pyproject.toml
      - plugin-sdk/README.md
      - plugin-sdk/src/privacyfence_plugin_sdk/__init__.py
      - plugin-sdk/src/privacyfence_plugin_sdk/types.py
      - plugin-sdk/src/privacyfence_plugin_sdk/_rpc.py
      - plugin-sdk/src/privacyfence_plugin_sdk/plugin.py
      - plugin-sdk/src/privacyfence_plugin_sdk/blocks.py
      - plugin-sdk/src/privacyfence_plugin_sdk/responses.py
      - plugin-sdk/src/privacyfence_plugin_sdk/py.typed
      - scripts/gen_plugin_sdk_types.py
      - tests/unit/test_gen_plugin_sdk_types.py
      - tests/unit/plugin_sdk/__init__.py
      - tests/unit/plugin_sdk/conftest.py
      - tests/unit/plugin_sdk/test_plugin.py
      - tests/unit/plugin_sdk/test_blocks.py
      - tests/unit/plugin_sdk/test_rpc.py
    brief: |
      1. plugin-sdk/pyproject.toml and package layout exactly per Design D16 (no testing/ subpackage yet; the
         next SDK phase adds it). The SDK must not import privacyfence; it reimplements framing (D3), block rules
         (D4) and tool-definition floors (D9) in its own code. Copy the numeric limits it needs into a private
         _limits section of plugin.py, with a test asserting they equal privacyfence.plugins.constants.
      2. scripts/gen_plugin_sdk_types.py: reads docs/plugin-protocol/protocol.schema.json, writes
         plugin-sdk/src/privacyfence_plugin_sdk/types.py (TypedDict per $defs entry, Literal enums, header line from
         D16); --check exits 1 and prints the first differing line when stale. Run it to produce types.py.
      3. plugin.py: Plugin, ToolHandle (.execute decorator), Prepared, Context, the source/confirm helpers incl.
         ctx.source.download() exactly as D16 (cursor loop, one restart on revision_changed, writes to dest or
         data_dir/downloads), prepared-state store keyed by call_id with PREPARED_CALL_LIFETIME_SECONDS expiry and
         args_digest check (answer digest_mismatch / unknown_call), page routing (exact path; "" == "/"), event
         and purge handlers, tools_changed(), run() (asyncio stdio until shutdown notification or EOF; on
         Windows use a thread-based stdin reader because asyncio pipes on stdin are not supported there).
      4. README.md: install, a minimal plugin, the manifest, building with PyInstaller, protocol link
         (https://github.com/privacyfence/privacyfence/blob/main/docs/plugin-protocol.md). No history wording.
      5. Tests under tests/unit/plugin_sdk/ (conftest.py inserts <repo>/plugin-sdk/src at sys.path[0]): drive the
         runner over an in-memory stream pair by hand (the public test host comes next phase): initialize result
         shape, ToolDefinitionError on floor violations at registration, prepare/execute with state, digest
         mismatch, page routing, events, download loop with a revision change. tests/unit/test_gen_plugin_sdk_types.py
         runs the script with --check via subprocess and expects exit 0.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk tests/unit/test_gen_plugin_sdk_types.py -q passes
      - python3 scripts/gen_plugin_sdk_types.py --check exits 0
      - python3 -m build plugin-sdk --outdir /tmp/sdk-dist succeeds (pip install build if missing in the session)
      - ruff check . passes

  - id: p6-source-api
    title: source.call operation table, chunked Drive downloads, audit
    depends_on: [p1-protocol-core]
    complexity: M
    touches:
      - src/privacyfence/plugins/source_ops.py
      - src/privacyfence/plugins/spool.py
      - tests/unit/plugins/test_source_ops.py
      - tests/unit/plugins/test_spool.py
    brief: |
      1. source_ops.py exactly per Design D7: SourceAdapter, SOURCE_ADAPTERS for the six operations, the param
         validators, handle_source_call with the eight ordered checks, ConnectorState type alias, error mapping,
         size cap, and the one audit entry per call. The manifest parameter is typed loosely as an object with a
         `source_operations` attribute (manifest.py is written in a parallel phase; import it only under
         TYPE_CHECKING).
      2. spool.py: DownloadSpool per D7 (secure_mkdir root, per-plugin subdir, cursor encode/decode, revision
         check on every call, idle sweep with injected clock, clear()). Confirm the Drive client's metadata method
         name first (Risks: "DriveClient metadata method name").
      3. Do not modify any client or connector. Tests use fake connector objects with the private client
         attribute set to a stub client (MagicMock or a small class) returning shapes copied from the real
         dataclasses (import JiraIssue, CalendarEvent, ConfluencePage from the client modules to build them).
      4. Tests: TestAllowlist::test_operation_outside_manifest_refused, TestAllowlist::test_operation_outside_allowlist_refused,
         TestPrincipal::test_unknown_principal, TestOrgOnly::test_credential_rejected, TestIntrospection,
         TestConnectorUnavailable::test_reasons (disabled / not_authenticated / unavailable per D6),
         TestUpstreamError::test_message_not_leaked, TestPayloadCap, TestAuditNoContent::test_response_bytes_absent_from_audit
         (use init_audit_logger(str(tmp_path)), put a sentinel string in the stub's data and assert it is in no
         audit line), one TestAdapter<Op> per operation; test_spool.py: TestDriveChunks::test_reassembles_ten_mb_file
         (10 MiB of random bytes in two chunks), test_offset_and_cursor_together_rejected,
         test_revision_change_is_reported, test_spool_removed_after_idle, test_file_over_64_mib_refused.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_source_ops.py tests/unit/plugins/test_spool.py -q passes
      - TestAuditNoContent::test_response_bytes_absent_from_audit and TestDriveChunks::test_reassembles_ten_mb_file pass
      - git diff --stat origin/main -- 'src/privacyfence/*_client.py' src/privacyfence/connectors shows no change
      - coverage of source_ops.py and spool.py >= 95%

  - id: p7-tool-defs
    title: Tool definition validation and floors
    depends_on: [p1-protocol-core]
    complexity: S
    touches:
      - src/privacyfence/plugins/tools.py
      - tests/unit/plugins/test_tools.py
    brief: |
      1. tools.py per Design D9's first paragraph: ToolDefError, validate_scope_types, validate_tool_defs with every
         rule and the exact error texts given there. Takes the manifest as an object with max_gate_floor.
      2. Tests: TestFloors::test_destructive_must_be_popup, TestFloors::test_write_auto_needs_manifest_floor,
         TestFloors::test_read_auto_allowed, TestNames (pattern, 64-char MCP name, duplicates), TestScopes
         (undeclared scope type), TestParameters (reason property refused, non-object refused), TestLimits
         (65 tools, long description, effect, title), TestWholeListRejected (one bad tool rejects all).
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_tools.py -q passes
      - coverage of tools.py = 100%

  - id: p8-trust-state
    title: Plugins directory, admin-only check, hashes, enabled-plugin state
    depends_on: [p2-blocks-manifest]
    complexity: M
    worker_model: opus
    worker_model_reason: Cross-platform ownership and ACL logic extracted from the privilege-separation code; a mistake silently weakens the ADR 0058 boundary.
    touches:
      - src/privacyfence/privilege_separation.py
      - src/privacyfence/plugins/trust.py
      - src/privacyfence/plugins/state.py
      - tests/unit/test_privilege_separation.py
      - tests/unit/plugins/test_trust.py
      - tests/unit/plugins/test_state.py
      - tests/platform/test_plugin_dir_permissions.py
    brief: |
      1. privilege_separation.py: add public admin_only_write_problem(path) per Design D5 and make
         _posix_script_elevation_problem and _windows_script_elevation_problem call it. Every existing test in
         tests/unit/test_privilege_separation.py (TestElevationScriptProblem and the monkeypatched call sites) must
         pass unchanged; add tests for the new function on directories.
      2. trust.py per D5: plugins_dir() per OS (table), DiscoveredPlugin, discover(), admin_only_problem(plugin_dir,
         executable) (executable, dir, ancestors up to plugins_dir().parent), sha256_file(). Problems map to the D6
         reason strings; log the underlying detail at warning.
      3. state.py per D5: PluginRecord, PluginStateStore with load/enable/disable/forget/check_hashes, atomic writes
         with mode 0600, corrupt file -> empty and logged.
      4. Tests: test_trust.py (TestLocation per OS via monkeypatch of sys.platform and env, TestDiscovery,
         TestLocation::test_user_writable_executable_refused using a tmp dir and a monkeypatched
         admin_only_write_problem, TestHashes); test_state.py (TestHashDrift::test_changed_hash_disables, round trip,
         corrupt file fail-closed, mode 0600 on POSIX). tests/platform/test_plugin_dir_permissions.py
         (pytestmark = pytest.mark.platform): on POSIX, a file owned by the current non-root user is refused by
         admin_only_write_problem; on Windows, a file in tmp_path (user-writable ACL) is refused. Skip the POSIX
         case when running as root.
    acceptance:
      - python3 -m pytest tests/unit/test_privilege_separation.py tests/unit/plugins/test_trust.py tests/unit/plugins/test_state.py tests/platform/test_plugin_dir_permissions.py -q passes
      - TestHashDrift::test_changed_hash_disables and TestLocation::test_user_writable_executable_refused pass
      - coverage of trust.py and state.py >= 95%

  - id: p9-sdk-testhost
    title: Public SDK test host and operation samples
    depends_on: [p5-sdk-core]
    complexity: M
    touches:
      - plugin-sdk/src/privacyfence_plugin_sdk/testing/**
      - tests/unit/plugin_sdk/test_testhost.py
    brief: |
      1. Implement privacyfence_plugin_sdk.testing exactly per Design D16 "Test host": PluginTestHost, ToolOutcome,
         PageResponse, SourceFixtureMissing, ToolDefinitionError re-export, rules.allow_scope with D15 semantics,
         source fixtures, samples.get / samples.drive_download (chunking and cursor format identical to D7),
         pages with D14 headers and normalization, confirmations, emit/purge/shutdown, and testing/pytest.py's
         plugin_host fixture importing pytest lazily.
      2. testing/samples/<operation>.json for the six operations with the D7 data shapes. Hand-written and
         redacted: no real names, emails, ids or URLs (use example.com, "Jane Example", ids like "EXAMPLE-1").
      3. Do not edit plugin-sdk/README.md (the retire phase documents testing in docs/plugins.md). Put usage
         examples in the testing package's module docstring.
      4. Tests tests/unit/plugin_sdk/test_testhost.py with a small in-test plugin: TestPluginTestHost::test_review_read_released_equals_card_payload,
         test_scope_rule_skips_card, test_scope_rule_mismatch_shows_card, test_source_fixture_missing_raises,
         test_page_headers_match_daemon, test_post_returns_405, test_tool_floor_violation_raises,
         test_confirmation_round_trip, test_drive_download_sample_chunks.
    acceptance:
      - python3 -m pytest tests/unit/plugin_sdk -q passes
      - every file in plugin-sdk/src/privacyfence_plugin_sdk/testing/samples/ is valid JSON (python3 -c loop) and grep -rniE '@(gmail|privacyfence)\.|https?://(?!example)' on that folder returns nothing

  - id: p10-sdk-release
    title: Publish the SDK from the release workflow
    depends_on: [p5-sdk-core]
    complexity: S
    touches:
      - .github/workflows/publish-pypi.yml
      - docs/releasing.md
    brief: |
      1. publish-pypi.yml: add build-sdk, publish-sdk-testpypi and publish-sdk-pypi exactly per Design D16
         "Release", copying the existing jobs' needs/if/permissions/environment/pinned action SHA. The SDK version
         comes from setuptools_scm on the same tag (plugin-sdk/pyproject.toml's root = ".."), so checkout must
         fetch tags the same way the existing build job does. No R2 upload for the SDK.
      2. docs/releasing.md "Publishing to PyPI": add the second pending-publisher registration (project
         privacyfence-plugin-sdk) to the numbered steps and one sentence that the SDK is built and published by
         the same workflow from the same tag. Present tense, no history.
      3. Validate: python3 -c "import yaml;yaml.safe_load(open('.github/workflows/publish-pypi.yml'))" and, if
         actionlint is installable (pip install actionlint-py), run it on the file.
      Do not touch release.yml or build.yml.
    acceptance:
      - python3 -c "import yaml;d=yaml.safe_load(open('.github/workflows/publish-pypi.yml'));assert {'build-sdk','publish-sdk-testpypi','publish-sdk-pypi'} <= set(d['jobs'])" exits 0
      - python3 -m pytest tests/unit/test_code_no_history.py tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py -q passes

  - id: p11-confirm-cards
    title: Plugin confirmations and code/diff blocks on cards
    depends_on: [p2-blocks-manifest]
    complexity: M
    worker_model: opus
    worker_model_reason: Confirmations must never be auto-accepted and must keep step-up; card rendering of plugin-supplied text is an injection surface.
    touches:
      - src/privacyfence/plugins/confirm.py
      - src/privacyfence/dialog_window_html.py
      - src/privacyfence/approval_window_html.py
      - src/privacyfence/resources/approval_window/styles.css
      - tests/unit/plugins/test_confirm.py
      - tests/unit/test_dialog_window_html.py
      - tests/unit/test_approval_window_html.py
      - tests/integration/test_plugin_card_escaping_browser.py
    brief: |
      1. approval_window_html._render_block: add "code" and "diff" per Design D4's last paragraph (escaped text,
         the three pf-diff-* classes). styles.css: styles for pre.pf-code and the diff classes using existing
         design tokens only (no colour literals, no width @media; tests/unit/test_design_system.py must pass).
      2. dialog_window_html.build_confirmation_html: new keyword body_blocks per D11, rendered through
         build_preview_body_html(blocks=...). Existing callers and their tests unchanged.
      3. plugins/confirm.py: ConfirmationService exactly per D11 (request returns immediately; await_ waits on the
         card event in the injected executor; id ownership check; result mapping; deny note; plugin_confirm audit
         entries; unattended refusal; introspection refusal).
      4. Tests: test_confirm.py with a real PendingApprovalRegistry: TestConfirm::test_never_auto_accepted (add a
         rule that would match anything for every operation; reevaluate_all leaves the card pending),
         TestConfirm::test_refused_when_unattended, TestConfirm::test_step_up_marks_sensitive,
         TestConfirm::test_await_approved / denied with deny note / expired / timeout,
         TestConfirm::test_other_plugins_id_refused, TestConfirm::test_await_approval_meta_tool_sees_status
         (registry.await_status returns "pending" then "approved"). test_approval_window_html.py: code and diff
         escaping. test_dialog_window_html.py: body_blocks rendered and escaped.
         tests/integration/test_plugin_card_escaping_browser.py (markers integration, browser; reuse the Chromium
         launch helper used by tests/integration/test_browser_smoke.py): load a confirmation card built from
         hostile blocks ("<script>window.pwned=1</script>", "<img src=x onerror=...>", bidi overrides) and assert no
         element besides the expected ones exists and window.pwned is undefined.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_confirm.py tests/unit/test_dialog_window_html.py tests/unit/test_approval_window_html.py tests/unit/test_design_system.py -q passes
      - TestConfirm::test_never_auto_accepted and TestConfirm::test_refused_when_unattended pass
      - python3 -m pytest tests/integration/test_plugin_card_escaping_browser.py -q passes and reports PASSED, not SKIPPED (use PRIVACYFENCE_TEST_CHROMIUM if the session hook set it)

  - id: p12-plugin-connector
    title: PluginConnector, MCP exposure and the two-step gate
    depends_on: [p2-blocks-manifest, p3-rpc-supervisor, p4-policy-dynamic, p7-tool-defs]
    complexity: M
    worker_model: opus
    worker_model_reason: Prepare-to-release consistency, pending-call reuse and single-use writes are security invariants of the gate.
    touches:
      - src/privacyfence/plugins/connector.py
      - tests/unit/plugins/test_connector.py
    brief: |
      1. Read Design D9 (exposure, registration, rejected tools.changed) and D10 in full, and gate.py's gated_call
         signature and its pending/ledger path, before writing code.
      2. connector.py: PluginConnector(Connector) with constructor (plugin name, display_name, manifest-like
         object, peer_provider: Callable[[], RpcPeer | None], principal_context_provider: Callable[[], dict],
         on_audit_lifecycle: Callable[[str], None]); set_tools(defs) (unregister then register_dynamic_tools,
         build ToolSpecs per D9), clear_tools(), tool_specs(), call() per D10 steps 1-5 with the exact RuntimeError
         sentences, _pending_prepared with expiry, the auto-audit entry copied from connectors/tasks.py's
         _auto_audit, and handle_tools_changed(params) applying D9's whole-list rule (uses
         tools.validate_tool_defs).
      3. Tests (fake RpcPeer object whose request() is an AsyncMock-like stub; real gate with stubbed show_popup /
         show_read_popup per guidelines §2.4, or the gated_call_spy pattern where only the arguments matter):
         TestExposure (names, annotations, reason param on gated tools only), TestGateFlow::test_auto_read_no_card_and_audited,
         TestGateFlow::test_read_returns_prepared_payload_verbatim (execute returns different data; caller gets the
         prepared payload), TestGateFlow::test_write_executes_once, TestGateFlow::test_pending_reuses_prepared_call
         (first call raises ApprovalPending, second call does not call prepare again and releases the same payload),
         TestGateFlow::test_denied_never_executes, TestGateFlow::test_review_scans_payload_for_pii,
         TestGateFlow::test_scope_rule_auto_accepts_matching_call and test_scope_rule_does_not_accept_other_value
         (real policy rules via auto_accept.add_policy_v2_rules), TestCardShowsPayload (preview_blocks passed),
         TestErrors (prepare error codes -> sentences; unknown_call), TestToolsChanged::test_violation_keeps_previous_list,
         and assert_all_tools_leave_an_audit_trail-style check: every tool of a 3-tool plugin writes an audit entry.
      Stop condition: see the Risks bullets "Card wiring for preview_blocks" and "Telling auto-accepted from
      card-approved".
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_connector.py tests/unit/test_gate.py -q passes
      - TestGateFlow::test_read_returns_prepared_payload_verbatim, TestGateFlow::test_write_executes_once and TestGateFlow::test_pending_reuses_prepared_call pass
      - coverage of connector.py >= 95%

  - id: p13-host-wiring
    title: PluginHost, plugin storage, daemon wiring and the plugins.enabled setting
    depends_on: [p3-rpc-supervisor, p6-source-api, p8-trust-state, p11-confirm-cards, p12-plugin-connector]
    complexity: M
    touches:
      - src/privacyfence/plugins/host.py
      - src/privacyfence/plugins/storage.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/server.py
      - src/privacyfence/resources/settings.yaml.example
      - docs/configuration-reference.md
      - tests/unit/plugins/test_host.py
      - tests/unit/plugins/test_storage.py
      - tests/unit/test_daemon_main.py
      - tests/unit/web/test_server.py
    brief: |
      1. storage.py per Design D12 "Directories" (install_dir, principal_dir, ensure_dirs, remove_all).
      2. host.py per D13: constructor seams, start/stop_all/rescan/inspect/enable/disable/purge/connectors/rows,
         listeners, request handlers (source.call -> source_ops.handle_source_call with the running plugin's
         manifest and a shared DownloadSpool; confirm.request/await -> ConfirmationService), the tools.changed
         notification -> PluginConnector.handle_tools_changed then the tools-changed listener, initialize params
         (D3 table: daemon {name: "privacyfence", version}, plugin {name, manifest_version}, data_dir,
         principals [principal_context(LOCAL_PRINCIPAL, principal_dir)], limits), the supervisor's validate_tools
         wired to tools.validate_tool_defs, state transitions and plugin_lifecycle audit entries (D9, D12, D13), D6
         reasons for feature off / no privilege separation, the enable hash recheck with its exact error text, and
         purge per D12 steps 1-4. Event fan-out is the next settings phase; leave on_connectors_changed as a method
         that stores the latest rows (the next phase completes it).
      3. daemon_main.py per D13 "Daemon wiring" 1-6, local mode only. settings_controller.py: only the new
         constructor kwarg plugin_host=None stored as self._plugin_host. web/server.py: only the new plugin_host=None
         kwarg on WebServer and build_app, stored on app.state.plugin_host (or the module's equivalent); no route.
      4. settings.yaml.example: a `plugins:` section with `enabled: true` and a one-line comment; docs/configuration-reference.md:
         a `plugins.enabled` entry (what it does; plugins also need the packaged install's background service).
      5. Tests: test_storage.py; test_host.py driving the stub plugin and a minimal in-test SDK plugin (PYTHONPATH
         plugin-sdk/src) with plugins_dir=tmp_path, trust_check=lambda *_: None, separation_enabled=lambda: True:
         TestStartEnabled, TestFeatureOff, TestNoSeparation (reason string), TestEnable::test_hash_changed_since_review_refused,
         TestDisable, TestCrashLimitRecorded, TestHashDriftOnStart, TestInspectReturnsSummary,
         TestSourceCallRouted (stub connectors_provider), TestPurge::test_deletes_after_timeout,
         TestPurge::test_uninstall_deletes_directories, TestToolsChangedNotifiesListener. test_daemon_main.py:
         TestDaemonPluginHost::test_plugins_stop_before_audit_close, test_plugin_connectors_merged_for_local_principal,
         test_no_host_in_org_mode. test_server.py: plugin_host stored.
      Stop condition: see Risks "Event loop for the host".
    acceptance:
      - python3 -m pytest tests/unit/plugins tests/unit/test_daemon_main.py tests/unit/web/test_server.py tests/unit/test_settings_controller.py tests/unit/test_docs_configuration_reference.py -q passes
      - TestPurge::test_deletes_after_timeout, TestPurge::test_uninstall_deletes_directories and TestEnable::test_hash_changed_since_review_refused pass
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q passes
      - coverage of host.py and storage.py >= 95%

  - id: p14-plugin-pages
    title: Sandboxed plugin pages
    depends_on: [p13-host-wiring]
    complexity: M
    worker_model: opus
    worker_model_reason: The sandbox CSP and the route authentication are the whole isolation story for plugin HTML.
    touches:
      - src/privacyfence/plugins/pages.py
      - src/privacyfence/web/routes_plugins.py
      - src/privacyfence/web/server.py
      - tests/unit/plugins/test_pages.py
      - tests/unit/web/test_routes_plugins.py
      - tests/unit/web/test_server.py
      - tests/integration/test_plugin_pages_browser.py
    brief: |
      1. pages.py per Design D14: normalize_path, parse query (last value wins), filter_response, and
         render_plugin_page(host, name, path, query, principal) sending web.request with the 10 s timeout.
      2. web/routes_plugins.py: build_routes(plugin_host, <the local auth adapter pieces>) per D14 (GET only, 405
         with Allow: GET otherwise without reaching the plugin, owner-only human session like the Settings page,
         403 for bearer/no session, 404 when not running or pages false, 400 for a rejected path, redirect
         /plugins/<name> -> /plugins/<name>/). Read how build_app wraps settings routes with _owner_only_routes and
         reuse that wrapping.
      3. web/server.py: mount the routes in local mode only (not in _build_org_app), and add the /plugins/ branch to
         _SecurityHeadersMiddleware that sets D14's CSP instead of build_csp(...) plus the four extra headers.
         Every other path's headers must be byte-for-byte unchanged (TestSecurityHeaders and
         TestSecurityHeadersMiddlewareReplacesNotExtends keep passing).
      4. Tests: test_pages.py (normalization table, filter_response table, oversize body -> 502, plugin cookie and
         CSP dropped); test_routes_plugins.py (TestRoutes::test_post_returns_405_without_calling_plugin,
         test_bearer_gets_403, test_no_session_gets_403, test_not_running_404, test_traversal_400, test_redirect,
         test_headers_present_on_error); test_server.py (sandbox CSP only under /plugins/; org app has no
         /plugins route). tests/integration/test_plugin_pages_browser.py (integration, browser): serve a page whose
         script tries document.cookie and fetch('/api/settings/state') and writes the outcome into the DOM; assert
         the cookie is unreadable (opaque origin raises or returns "") and the fetch fails.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_pages.py tests/unit/web/test_routes_plugins.py tests/unit/web/test_server.py -q passes
      - python3 -m pytest tests/integration/test_plugin_pages_browser.py -q reports PASSED, not SKIPPED
      - TestRoutes::test_post_returns_405_without_calling_plugin passes

  - id: p15-settings-events
    title: Settings Plugins section, sensitive enable and purge, connector events
    depends_on: [p13-host-wiring]
    complexity: M
    touches:
      - src/privacyfence/plugins/events.py
      - src/privacyfence/plugins/host.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/plugins/test_events.py
      - tests/unit/plugins/test_host.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_org_settings_scope.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_plugin_settings_browser.py
    brief: |
      1. events.py: EventFanout per Design D12 "Events" (diff of (enabled, authed) per connector -> the four
         states; best effort). host.py: complete on_connectors_changed to forward to EventFanout and notify running
         plugins. settings_controller.py: call self._plugin_host.on_connectors_changed(<rows>) inside the existing
         refresh_connectors listener chain when a host is set, and include `plugins: plugin_host.rows()` in the
         snapshot that _push_snapshot sends (empty list when no host).
      2. Settings actions in web/org_settings_scope.ACTION_SCOPES (local mode only) and web/routes_settings.py:
         rescan_plugins (non-sensitive), inspect_plugin {name} (non-sensitive; returns host.inspect summary),
         enable_plugin {name, executable_sha256, manifest_sha256} (sensitive: human session + step-up, like
         enable_connector), disable_plugin {name} (non-sensitive), purge_plugin_data {name} (sensitive). Each
         calls the matching SettingsController method, which calls the host (through the loop bridge chosen in the
         host phase). Errors return the existing settings error JSON with the host's exact message.
      3. settings_window_html.py: a "Plugins" section (local mode only in _capabilities_for), listing each row:
         display name, version, state, reason, a link "Open page" when page_url is set, and buttons. "Review and
         enable" calls inspect_plugin, then shows a dialog listing every tool with its gate (auto / review / popup),
         read-only or write, destructive, the source operations, pages yes/no, the gate floor ("Writes may run
         without asking" when max_gate_floor is auto), and an "Enable" button that posts enable_plugin with the two
         hashes from the summary (the existing step-up flow handles the passkey). "Disable", "Delete this plugin's
         data" (with the existing confirm pattern), "Rescan". Use only design-system primitives and tokens.
      4. Tests: test_events.py (each transition; no event when unchanged; send failure swallowed); test_host.py
         additions for event forwarding; test_settings_controller.py (snapshot contains plugins; methods call the
         host); test_routes_settings.py (TestSensitiveActionsCoverAllAllowedActions still passes,
         TestEnablePluginSensitivity::test_requires_step_up_and_human_session, TestPurgePluginSensitivity, disable
         is not sensitive); test_org_settings_scope.py (actions local-only); test_settings_window_html.py (section
         present in local, absent in org; enable dialog lists gates). tests/integration/test_plugin_settings_browser.py
         (integration, browser): a TestPhoneLayout case for the Plugins section and the enable dialog with two fake
         rows, following the existing TestPhoneLayout pattern.
    acceptance:
      - python3 -m pytest tests/unit/plugins/test_events.py tests/unit/plugins/test_host.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_settings.py tests/unit/web/test_org_settings_scope.py tests/unit/test_settings_window_html.py tests/unit/test_design_system.py -q passes
      - TestEnablePluginSensitivity::test_requires_step_up_and_human_session passes
      - python3 -m pytest tests/integration/test_plugin_settings_browser.py -q reports PASSED, not SKIPPED

  - id: p16-echo-e2e
    title: Echo reference plugin, end-to-end tests, refusals, test-host conformance
    depends_on: [p9-sdk-testhost, p14-plugin-pages, p15-settings-events]
    complexity: M
    touches:
      - tests/fixtures/plugins/echo/**
      - tests/fixtures/plugins/echo-variants/**
      - tests/integration/test_plugin_framework.py
      - tests/integration/test_plugin_refusals.py
      - tests/integration/test_sdk_testhost_conformance.py
      - tests/unit/plugins/test_sdk_samples.py
    brief: |
      1. Build echo on the SDK per Design D17 (tools table, page, event recorder) and the refusal variants under
         echo-variants/<case>/ (bad manifest key, reserved name, write tool on auto without floor, destructive
         not popup, major "2", org-only service_credentials true).
      2. tests/integration/test_plugin_framework.py: an in-process daemon like test_mcp_daemon_contract.py's
         running_mcp_server, plus a PluginHost with plugins_dir=tmp copy of echo, trust_check=lambda *_: None,
         separation_enabled=lambda: True, command_resolver returning [sys.executable, echo_plugin.py] with
         PYTHONPATH including plugin-sdk/src, connectors_provider with a fake calendar connector whose _calendar
         stub returns CalendarEvent objects; approvals driven by stubbing the approval UI per guidelines §2.4.
         Classes: TestEchoAutoRead, TestEchoReviewRead::test_returns_exactly_the_approved_data,
         TestEchoPopupWrite::test_single_use, TestPluginScopeRule (matching and non-matching), TestConfirmRoundTrip
         (privacyfence_await_approval sees the decision), TestSourceCallAudit::test_no_content_in_audit,
         TestEchoPage::test_sandbox_csp (HTTP GET through the server with a human session cookie, as other
         integration tests obtain one), TestToolsListChanged, TestCrashRestart, TestDisabledAfterFive,
         TestCleanShutdown, TestPurgeOnUninstall, TestAuditTrail (every echo tool leaves an audit entry).
      3. tests/integration/test_plugin_refusals.py: one parametrized TestRefusals covering: operation outside the
         allowlist, operation outside the manifest, user-writable executable (real trust_check with a monkeypatched
         admin_only_write_problem returning a problem), changed hash, major mismatch, non-read-only auto without
         the floor, destructive not on popup, HTML in a block rendered escaped (card HTML contains &lt;script),
         POST to /plugins/echo/ -> 405, org-only field in local mode.
      4. tests/integration/test_sdk_testhost_conformance.py: run the same scenarios (auto read, review read, popup
         write, scope rule match and mismatch, floor violation, block validation, page headers, chunked download
         from samples.drive_download vs. the daemon's spool with a stub Drive client) through PluginTestHost and
         through the real daemon with echo; assert equal outcomes field by field.
      5. tests/unit/plugins/test_sdk_samples.py: every SDK sample's data passes the daemon's D7 shape check.
      If a test exposes a bug in src/privacyfence/plugins/**, do not fix it here: stop with status=blocked naming
      the failing test and module (the orchestrator sends it back to the owning phase).
    acceptance:
      - python3 -m pytest tests/integration/test_plugin_framework.py tests/integration/test_plugin_refusals.py tests/integration/test_sdk_testhost_conformance.py tests/unit/plugins/test_sdk_samples.py -q passes
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py -q passes
      - grep -rn "xfail\|skip(" tests/integration/test_plugin_*.py tests/integration/test_sdk_testhost_conformance.py returns nothing

  - id: p17-today-example
    title: The today example plugin, its build script and the build.yml steps
    depends_on: [p16-echo-e2e]
    complexity: M
    touches:
      - examples/plugins/today/**
      - scripts/build_example_plugin.py
      - .github/workflows/build.yml
      - tests/unit/examples/__init__.py
      - tests/unit/examples/conftest.py
      - tests/unit/examples/test_today_plugin.py
      - tests/unit/test_build_example_plugin.py
      - tests/fixtures/plugins/today/**
    brief: |
      1. examples/plugins/today/: today_plugin.py (the plugin, built only on the SDK), privacyfence-plugin.yaml
         (name today, display_name Today, version 1.0.0, protocol "1", command ["today-plugin"],
         source_operations [calendar.list_events], tools dynamic, max_gate_floor auto, pages true), README.md with
         the ten smoke-test steps from the issue (Manual after section of
         https://github.com/privacyfence/privacyfence/issues/846), adapted to the exact tool names in Design D17
         and the Settings flow in the settings phase ("Review and enable", passkey). Behaviour exactly per D17.
      2. scripts/build_example_plugin.py per D17 (argparse; PyInstaller via subprocess with --onefile, --name
         today-plugin, --paths plugin-sdk/src and --paths examples/plugins/today so the SDK is bundled without being
         installed; writes manifest and build-flags.json; --with-crash-tool; --out).
      3. .github/workflows/build.yml: in each of build, build-windows and build-deb add the two steps from D17
         after that job's own build steps (shell: bash on macOS/Linux, pwsh on Windows). Keep names short:
         "Build the today example plugin" and "Self-test the today example plugin".
      4. Tests: tests/unit/examples/conftest.py adds plugin-sdk/src and examples/plugins/today to sys.path;
         test_today_plugin.py uses PluginTestHost with a recorded Calendar fixture under
         tests/fixtures/plugins/today/ (hand-written, redacted, D7 normalized shape), one class per row of the
         issue's P12 table (initialize and scope type, source call, storage, each tool and gate, publish
         confirmation, page incl. manifest code block and cookie check script presence, the four events, crash
         tool present only with the build flag). test_build_example_plugin.py: argument parsing and the files
         written, with the PyInstaller subprocess call monkeypatched.
      5. Run python3 scripts/build_example_plugin.py today --out /tmp/pf-plugins locally (pyinstaller is in the dev
         extra) and /tmp/pf-plugins/today/today-plugin --self-test; paste the output in the PHASE-REPORT.
    acceptance:
      - python3 -m pytest tests/unit/examples tests/unit/test_build_example_plugin.py -q passes
      - python3 scripts/build_example_plugin.py --help exits 0
      - the local Linux build and --self-test print "today ok protocol 1.0.0"
      - python3 -c "import yaml;yaml.safe_load(open('.github/workflows/build.yml'))" exits 0

  - id: p18-retire
    title: ADRs, reference docs, changelog, retire the plan
    depends_on: [p1-protocol-core, p2-blocks-manifest, p3-rpc-supervisor, p4-policy-dynamic, p5-sdk-core, p6-source-api, p7-tool-defs, p8-trust-state, p9-sdk-testhost, p10-sdk-release, p11-confirm-cards, p12-plugin-connector, p13-host-wiring, p14-plugin-pages, p15-settings-events, p16-echo-e2e, p17-today-example]
    complexity: M
    touches:
      - docs/adr/**
      - docs/plugin-protocol.md
      - docs/plugins.md
      - docs/README.md
      - docs/approvals-and-policy.md
      - docs/security-and-compliance.md
      - docs/platform-support.md
      - CHANGELOG.md
      - docs/plugin-framework-plan.md
      - docs/plugin-framework-plan-manual-steps.html
    brief: |
      1. ADRs: write the seven ADRs in the plan's ADRs section with docs/adr/README.md's template, numbered from
         the next free number (check `ls docs/adr`), Status "Accepted — <today>. Implemented.", linking the issue
         https://github.com/privacyfence/privacyfence/issues/846 and source files, never the plan. Take the context,
         decision and rejected alternatives from the Design sections each ADR names. Add all seven to
         docs/adr/README.md's index. If the numbers differ from 0120-0126, grep src/ tests/ plugin-sdk/ examples/
         docs/ for "ADR 012" and fix every citation to the real numbers.
      2. docs/plugin-protocol.md: the protocol reference from the final schema and code (D3, D4, D5 manifest, D7
         operations with data shapes, D9 floors, D10 flow, D11, D12 events, D14 headers, limits and timeouts),
         stating current behaviour only; link the schema file and the ADRs.
      3. docs/plugins.md: for administrators and plugin authors: what a plugin is, the plugins directory per OS
         and its permission requirement, installing, "Review and enable" with the passkey, logs location
         (data_dir/logs/plugins/<name>.log), disabling, deleting data, removal, plugins.enabled, the reasons a
         plugin does not start (D6 table), and writing and testing a plugin with the SDK and PluginTestHost.
      4. List both new docs in docs/README.md. Update approvals-and-policy.md (plugin tools, gates, the
         plugin:<name>:<scope> rule selector, confirmations never auto-accepted), security-and-compliance.md
         (the plugin trust model and residual risk, the source API being ungated but audited, sandboxed pages) and
         platform-support.md (plugins directory per OS). tools-reference.md and always-allow-rules-reference.md are
         generated from static tables and do not change; confirm with their tests.
      5. CHANGELOG.md: one entry under ## [Unreleased] for the plugin framework, the SDK and the today example.
      6. Delete docs/plugin-framework-plan.md and docs/plugin-framework-plan-manual-steps.html. grep the repo for
         "plugin-framework-plan" and remove any reference.
    acceptance:
      - test ! -e docs/plugin-framework-plan.md && test ! -e docs/plugin-framework-plan-manual-steps.html
      - grep -rn "plugin-framework-plan" --include='*' . --exclude-dir=.git returns nothing
      - python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_website_docs_allowlist.py tests/unit/test_code_no_history.py tests/unit/test_docs_tools_reference.py tests/unit/test_generate_always_allow_reference.py tests/unit/test_changelog_section.py -q passes
      - seven new docs/adr/NNNN-*.md files with Status Accepted, all in docs/adr/README.md's index
```
