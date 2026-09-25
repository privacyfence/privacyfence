# Coding and testing guidelines

These are the repository's standing expectations for implementation and test changes. It is
descriptive before it is prescriptive: every rule below reflects a pattern already established in
`src/privacyfence/` and `tests/` — the Python daemon — not a generic style guide imported wholesale.
`mcpb/shim/` (the Node/TypeScript stdio-to-`/mcp` transport proxy Claude Desktop's `.mcpb` installs)
follows its own, separate conventions; this document covers it only where the shim's checks are
part of the definition of done or a change on its side of a contract needs a matching Python-side
check.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for process (PRs, issues, license) and
[`security-and-compliance.md`](security-and-compliance.md) for the security model this code
implements. This document covers how to write, test and extend the code, not why it exists.

## 1. Coding guidelines

Keep policy/security decisions in shared layers rather than connector-specific shortcuts — a new or
changed connector tool must use the same gate, privacy-filter, audit, secure-file, and
principal-scoping mechanisms as equivalent existing tools. Prefer explicit, testable boundaries
over hidden global state: keep filesystem/network/provider behavior behind modules that can be
exercised independently.

### 1.1 Language baseline

- Python 3.11+. Every module starts with `from __future__ import annotations` (right after the
  module docstring, before other imports).
- Prefer the standard library over new dependencies (stated in `CONTRIBUTING.md`).
- Use modern union syntax, `X | None`, not `Optional[X]`. `src/privacyfence/` has no
  `Optional[...]` call sites; keep it that way.
- Type-hint function signatures, including return types (`-> None`, `-> Any`, etc.). Dataclass
  fields are always typed.

### 1.2 Module & docstring conventions

- Every module has a docstring as the first line of the file — even a one-liner
  (`"""Gmail connector."""`). For modules with non-obvious lifecycle or invariants (e.g.
  `daemon_main.py`, `gate.py`, `audit_log.py`), the docstring explains the *why*: threading model,
  ordering guarantees, what a caller must not assume.
- Default to no comments. Only add one when it captures a non-obvious *why* — a hidden constraint,
  a race that was fixed, a workaround for a specific API quirk — never a restatement of *what* the
  next line does.
- Comments, docstrings and user-visible strings carry no project history: no phase names, plan or
  review item IDs, finding IDs, bare issue or PR numbers, section numbers of a deleted plan, or
  "as of version N" phrasing. Say the reason in your own words, and when the reason is a decision,
  name its ADR (`ADR 0003`, or a section or decision of it). Work that is still open is cited by
  the issue's full URL, with the limitation described next to it. History belongs in
  `CHANGELOG.md` and the ADRs. `tests/unit/test_code_no_history.py` enforces this as a blocking
  test; see [ADR 0056](adr/0056-code-carries-no-project-history.md).
- Long files separate groups of methods with a three-line `# ----- #` banner comment (e.g.
  `connectors/gmail.py`'s "Auto (no gate)" group). Use this once a connector or client grows past
  ~5-6 methods; don't bother for short files.

### 1.3 Data modeling

- Use `@dataclass` for structured data crossing a boundary (API responses, tool specs, audit
  entries). Give collection fields `field(default_factory=...)`, never a mutable default.
- Dataclasses that normalize an external API's response document what they deliberately *don't*
  carry (e.g. `Attachment`: "Content is intentionally never carried here") when the omission is a
  privacy decision, not just an oversight.
- `Connector` subclasses (`connector.py`) are the unit of extension. A connector's tools reach
  `/mcp` through the shared dispatcher without any per-connector code there, but a new connector
  is still wired into several tables across the tree — see [§3](#3-adding-a-connector) for the
  full list. Don't special-case a connector's behavior outside those tables.

### 1.4 Error handling

- Every external-API client (`src/privacyfence/*_client.py`) defines its own
  `<Name>ClientError(Exception)` and raises only that (or lets it propagate) across its public
  methods. Code that only talks to the daemon's own local endpoints is the one accepted exception;
  an external cloud API always gets a dedicated error type.
- Connectors catch the client's specific error type at the boundary, log it, and re-raise as
  `RuntimeError(str(exc)) from exc` — never swallow it, never let the raw client exception or a
  bare `except Exception` leak past the connector into the tool-call response.
- Non-critical side effects (writing an audit entry) are wrapped in their own
  `try/except Exception: logger.warning(...)` so a logging failure never blocks the primary
  operation — see `gate.py::_audit` and every connector's `_auto_audit`.

### 1.5 The gate is load-bearing — treat it as a security boundary, not plumbing

This is the one area where "looks like a style rule" is actually a security invariant:

- Every tool call that touches real data must go through `gated_call()` (`gate.py`), **or** be a
  connector that deliberately auto-approves a whole tool and says so in its own docstring/comments
  (e.g. `contacts.py`'s unconditionally-auto-accepted tools, or the read-only listing tools every
  connector has). There is no third option — a tool that silently skips both the gate and an
  explicit auto-approve rationale is a bug.
- `gated_call()` must never return `raw_data` when `filtered_data` differs from it. This is stated
  verbatim in `tests/unit/test_gate.py`'s module docstring — it's the actual privacy boundary the
  whole gate exists to enforce.
- `preview` dicts (shown before approval) carry metadata only — sender, subject, size, destination
  path. Full body/content only ever goes into `details_text`, which the user must actively expand
  to see. Never put message bodies, file contents, or similar into `preview`.
- Every tool a connector exposes must leave an audit trail, one way or another — either through
  `gated_call` (which always audits, on every branch) or via a direct `_auto_audit`-style call for
  auto-approved tools. `tests/helpers.py::assert_all_tools_leave_an_audit_trail` enforces this
  mechanically; don't add a tool that this helper can't verify.
- Writes default to `gate="popup"`, reads of full content default to `gate="review"`; only
  low-sensitivity metadata listing calls (`gmail_list_messages`, `tasks_list_task_lists`, ...)
  default to `read_only=True` with no gate at all. The few ungated writes (`drive_create_blank_file`,
  `drive_sheets_create`) are deliberate, and say so in their own tool description. If a new tool
  doesn't fit one of these buckets cleanly, raise it as a design question rather than defaulting to
  whichever is least effort.

### 1.6 Untrusted input

- Any filename, path fragment, or identifier that originates from a remote party (an email
  attachment name, a message header) is untrusted. Before it touches the filesystem, sanitize it —
  the established pattern is `os.path.basename(filename) or "<fallback>"`
  (`gmail_client.py::resolve_attachment_destination`, `download_staging.py::stage`), which is what
  stops a crafted name like `../../.ssh/authorized_keys` from writing outside the intended
  directory. Compute the destination path once, in one function, and reuse it for both the
  pre-approval preview and the actual write — never compute it twice, or the preview and the real
  write can silently disagree.

### 1.7 Async & concurrency

- Blocking/sync calls (the Google/Slack/Salesforce/Atlassian SDKs, file I/O in a hot path) are
  wrapped in `asyncio.to_thread(...)`, never called directly from an `async def`.
- Approval dialogs (the popup itself, the PII confirmation, the "Always allow" rule confirmation)
  run on their own dedicated thread-pool executor (`gate._popup_executor`), not `asyncio.to_thread`'s
  shared default pool — a slow connector call (a Slack rate-limit retry sleeping out a
  `Retry-After` window, say) can otherwise occupy every worker in that shared pool and starve a
  popup that has nothing to do with it.
- `WebApprovalUI` can show several pending approvals at once — there is no single lock serializing
  dialogs. Instead, each gate interaction re-checks the current auto-accept state immediately before
  resolving (see each branch's own `_interact` closure), so a rule created by one
  concurrently-resolving approval is picked up by another still in flight, and
  `approvals.PendingApprovalRegistry.reevaluate_all()` re-evaluates anything already parked in the
  pending/registry state.

### 1.8 Logging vs. `print`

- Library/application code (clients, connectors, the daemon's request handling) logs via
  `logging.getLogger(__name__)`. Do not log secrets, bearer/session/bootstrap tokens, connector
  credentials, full message bodies, document contents, or protected provider content, unless the
  existing audit/data model explicitly requires a safe representation — log identifiers, subjects,
  counts instead.
- `print()` is reserved for the handful of argparse CLI flags in `daemon_main.py`
  (`--gmail-oauth`, `--telegram-setup`, `--print-mcp-token`, ...) that a human invokes directly at a
  terminal and that need to print a human-facing confirmation. It does not belong inside a `*_client.py` method — those are
  library code, called from multiple contexts, and should only log; the CLI entry point that calls
  them is responsible for any terminal-facing `print`.

### 1.9 Formatting and static analysis

Run Ruff on changed Python code:

```bash
ruff check .
python3 scripts/mypy_strict_modules.py
```

`pyproject.toml` is authoritative for Ruff, mypy, Bandit, pytest, and coverage configuration. Ruff
and Bandit are blocking CI checks. mypy runs twice in the same job: `mypy src/privacyfence` over
the whole tree is a visible informational check (`continue-on-error`), and
`scripts/mypy_strict_modules.py` re-runs it, blocking, over just the modules the ratchet has
promoted (`[tool.mypy]`'s `[[tool.mypy.overrides]]` entries — the script reads that list out of
`pyproject.toml`, so promoting a module needs no workflow change). Promoting the next module means
adding an overrides block once the module is clean; from then on a regression in it fails the
merge.

For Node/TypeScript changes under `mcpb/shim/`, run:

```bash
cd mcpb/shim
npm test
npm run typecheck
npm run build
```

For changes under `cloudflare/downloads/` (the downloads.privacyfence.eu Worker,
[`downloads-and-release-kpi.md`](downloads-and-release-kpi.md)), run:

```bash
cd cloudflare/downloads
npm test
npm run typecheck
npm run dry-run
```

`npm test` runs entirely against local Miniflare/workerd -- no Cloudflare credentials or network
access to the real R2/D1 resources involved.


## 2. Testing guidelines

### 2.1 Framework & layout

- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed).
  `pytest-timeout` caps every test at 30s (`[tool.pytest.ini_options]`'s `timeout`) so a hung
  fixture fails loudly instead of stalling CI. `freezegun` for time-dependent tests, `openpyxl` for
  asserting against the audit log's Excel export.
- `tests/unit/` mirrors `src/privacyfence/`: `src/privacyfence/<module>.py` is tested by
  `tests/unit/test_<module>.py`, `src/privacyfence/web/<module>.py` by
  `tests/unit/web/test_<module>.py`, and a connector `src/privacyfence/connectors/<name>.py` by
  `tests/unit/connectors/test_<name>_connector.py`. This is a convention, not a check — no test
  enforces it. These modules are deliberately tested elsewhere rather than in a module of their own:

  | Source module | Tested in |
  |---|---|
  | `connector_host.py` | its consumers' tests: `test_settings_controller.py`, `test_daemon_main.py`, `web/test_routes_settings.py`, `web/test_server.py` |
  | `std_streams.py` | `test_daemon_std_streams.py` |
  | `windows_service.py` | `test_daemon_main.py`, `test_privilege_separation.py` |
  | `agent_overrides.py`, `web/agent_pins.py` | `web/test_agent_attestation.py` |
  | `web/step_up_decide.py` | `web/test_approval_step_up.py` |
  | `web/routes_org_stepup.py` | `web/test_routes_org_approvals.py` |

  A new source module gets its own test module unless it belongs in this table for the same reason.
- `tests/integration/`, `tests/system/`, and `tests/platform/` hold cross-boundary, full-scenario,
  and OS-level tests respectively — see [`testing-policy.md`](testing-policy.md) for the full layer
  taxonomy and what runs where.
- New tests carry the marker that matches their layer: `unit`, `integration`, `system`, `platform`,
  `browser`, `packaged`, or `live` (declared in `pyproject.toml`'s `markers`).
- Run the smallest relevant test set while developing, then the affected unit/integration suite
  before opening/updating a PR. CI runs the full suite on Linux, Windows, and macOS runners (see
  `testing-policy.md`); a 100% pass rate is required to merge on all of them.
- Keep ordinary unit/integration tests offline and deterministic. Do not add real connector
  credentials or unmocked provider calls to GitHub-hosted CI.

### 2.2 Module & class organization

- Every test module opens with a docstring naming the module under test and, where relevant, the
  one invariant that matters most (see `test_gate.py`, `test_gmail_connector.py`). If a whole test
  file exists to prevent one specific class of bug, say so up top.
- Group tests into `class TestScenario:` blocks by behavior (`TestAutoAcceptPath`,
  `TestReviewGateDecisions`, `TestAcceptAll`, ...), not by method-under-test or in one flat list.
  Bare module-level `def test_...` functions are the exception, reserved for
  structural/cross-cutting checks that aren't about one component's behavior (e.g.
  `tests/unit/connectors/test_readme_manifest_alignment.py`, which checks the documented privacy
  matrix against the connectors' real tool specs).
- Regression tests carry a docstring explaining the bug they guard against, not just what they
  assert — see `TestGetMessagePreviewMinimization` in `test_gmail_connector.py`. A future reader
  should be able to tell *why* the test exists before deciding it's safe to delete or weaken.

### 2.3 Fixtures & isolation

- Module-level state is reset in the `autouse=True` fixture `_reset_singletons` in
  `tests/conftest.py`, before *and* after each test (its `_reset()` function). Two shapes are
  reset there:
  - **Per-principal registries** — `auto_accept`, `audit_log`, `pii_detector`, `privacy_filter`
    and `resource_names` each hold a `principal.PrincipalRegistry` named `_REGISTRY`, reset with
    `<module>._REGISTRY.reset()`, which drops every principal's instance.
  - **Process-wide singletons** — `approval_ui._INSTANCE`, `web_approval_ui._INSTANCE`,
    `download_staging._INSTANCE` and `upload_staging._INSTANCE` are set back to `None`; other
    module globals (`privilege_separation.reset_cache()`, `gate.configure_popup_executor(...)`,
    `daemon_main._shutdown_event`, ...) are reset by their own call.

  New per-user state belongs in a `PrincipalRegistry`, not a bare `_INSTANCE`. Any new module-level
  state needs a matching line in `_reset()`, or it leaks between tests silently.
- Use the `tmp_path` fixture with `init_audit_logger(str(tmp_path))` to get an isolated audit log
  directory per test — never point tests at the real `logs/audit/` directory.
- A lock or other primitive that binds itself to whichever asyncio event loop first contends on it
  (e.g. `asyncio.Lock`) needs its own `autouse` per-test reset if tests exercise real contention on
  it.

### 2.4 Faking the gate and the approval UI

- Never let a test spawn a real interactive dialog. Stub `show_popup` / `show_read_popup` /
  `show_rule_confirmation_popup` via `monkeypatch.setattr` on the module under test, not by mocking
  at `WebApprovalUI`'s own import site of every caller.
- Connector tests stub `gated_call` itself (the `gated_call_spy` pattern every
  `tests/unit/connectors/test_*_connector.py` uses) to capture exactly what a tool sends into the
  gate — `preview`, `details_text`, `raw_data`, `filtered_data`, `args`, `gate` — and assert on
  those kwargs, rather than trying to drive the real gate end-to-end from a connector test.
  `test_gate.py` owns proving the gate's own state machine; connector tests own proving each tool
  calls it correctly.

### 2.5 Reuse shared helpers before writing new ad hoc ones

- `tests/helpers.py` provides `make_ctx` (a `ReviewContext` with sane defaults),
  `build_stub_args` (a minimal-but-plausible args dict from a `ToolSpec`),
  `assert_all_tools_leave_an_audit_trail`, `assert_no_placeholder_fields` (fails loudly the
  moment a `_parse_*` field mapping silently degrades to a fallback value), and `policy_rules`.
  Check there before writing a new stub-args builder, a new per-connector audit-trail sweep, or a
  new placeholder-field check — duplicating these tends to drift out of sync with the real
  `Connector`/`ToolSpec` shape over time.

### 2.6 New-connector checklist

A new connector's test module should include, at minimum:

1. `TestDispatch` — unknown tool name raises `ValueError`.
2. One test per auto-approved tool proving it never touches the gate and does write its own audit
   entry.
3. One test per gated tool's `preview` dict, asserting it contains *only* metadata (data
   minimization) — never full body/content.
4. A call to `assert_all_tools_leave_an_audit_trail` covering every tool the connector declares,
   with `arg_overrides` for any tool that validates its args before reaching the client/gate.
5. For at least one gated read tool, an end-to-end test that runs a fully-populated raw API
   response through the real `_parse_*` method and the real connector method that builds the popup
   preview (not a hand-built dataclass, unlike the rest of this checklist), checked with
   `assert_no_placeholder_fields` — see `TestFieldCompleteness` in
   `tests/unit/connectors/test_confluence_connector.py` for the pattern this catches (a `_parse_*`
   field mapping silently degrading to a fallback value).

Its client's test module (`tests/unit/test_<name>_client.py`) also carries a
`TestLiveFixtureParsing` class that replays the committed fixture — see
[§2.9](#29-live-fixtures-and-provider-drift). The rest of what a new connector needs is in
[§3](#3-adding-a-connector).

### 2.7 Definition of done for a PR touching this repo

This section is the authoritative copy. The `/dod` command (`.claude/commands/dod.md`) runs its
commands and checks the conditional rows against the branch's diff, and
`.github/pull_request_template.md` repeats the checklist for the PR description.

**Every PR:**

- [ ] `pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing
      --cov-report=json:coverage.json` passes at 100%, and `python3 scripts/check_coverage_floor.py
      coverage.json` passes (the coverage ratchet — see `testing-policy.md`).
- [ ] `ruff check .`, `bandit -c pyproject.toml -r src` and `python3 scripts/mypy_strict_modules.py`
      all pass (CI's `static-analysis` job blocks on all three; the whole-tree `mypy src/privacyfence`
      run in that same job is informational only, while the modules with a
      `[[tool.mypy.overrides]]` entry are what the third command checks and CI blocks on — see
      `[tool.ruff.lint]`/`[tool.mypy]`/`[tool.bandit]` in `pyproject.toml`). A new Bandit finding
      that's a genuine false positive gets a `# nosec BXXX  # <reason>` comment at its call site,
      not a suppression in `pyproject.toml`.
- [ ] In `mcpb/shim/`: `npm test` and `npm run typecheck` pass (CI's `test` job runs both, and
      blocks on them, on every PR — whether or not the PR touches the shim).
- [ ] A user-visible change has a line under `CHANGELOG.md`'s `## [Unreleased]` heading (not under
      a concrete version heading — see this repo's CLAUDE.md, "Release notes come from
      CHANGELOG.md"). Internal-only changes don't need one.
- [ ] A decision that is hard to reverse, moves a trust boundary, changes the build/release/
      distribution path, or rejects a non-obvious alternative has an ADR in `docs/adr/`. A PR that
      deletes a plan document extracts that plan's decisions into ADRs first, or says in its
      description that it made none — see [`adr/README.md`](adr/README.md).
- [ ] Every new/changed tool call still resolves through `gated_call` or an explicit
      always-auto-approve connector, and leaves an audit trail either way.
- [ ] No preview dict carries full content; no log line carries a credential or a message/document
      body.
- [ ] New client code has a matching `<Name>ClientError`; new connector code catches it and
      re-raises as `RuntimeError`.
- [ ] New module-level state has a reset added to `tests/conftest.py` ([§2.3](#23-fixtures--isolation)).
- [ ] Comments only where the *why* is non-obvious; no restated-*what* comments.

**Only if the PR touches these files:**

- [ ] `src/privacyfence/*_client.py` or `src/privacyfence/connectors/**`: run
      `scripts/qa_fixture_recorder.py --check <connector>` against a dedicated QA account per
      [`connector-qa.md`](connector-qa.md), and paste its report into the PR description. Without
      local QA credentials, dispatch `connector-live-check.yml` against the branch and link the run
      instead — see [`testing-policy.md`, layer 5](testing-policy.md#layer-5-live-connector).
- [ ] `web/mcp_dispatch.py`, `web/routes_mcp.py`, `connector.py`'s `ToolSpec`/`ToolParam` shapes, or
      `web/server.py`'s socket-binding/lifecycle: run `pytest tests/integration -v` locally and
      confirm `test_mcp_daemon_contract.py` still passes — it drives a real, socket-bound daemon
      with the official `mcp` client, catching what the in-process `test_routes_mcp.py` transport
      can't.
- [ ] `web/mcp_auth.py`, `web/server.py`'s `mcp_url` discovery-file writing, or anything under
      `mcpb/shim/src/`: run `pytest tests/integration -v` locally (needs Node on `PATH`) and confirm
      `test_shim_mcp_contract.py` still passes — a change on one side of the shim<->`/mcp` contract
      without the other only fails there, not in either side's own unit tests.
- [ ] A dependency in `pyproject.toml` (a version bound, a new package, an extra): run
      `scripts/update_dependency_locks.sh` (needs `uv` on `PATH`, see the script's own comments for
      why) and commit the resulting `requirements/*.lock.txt` — `dependency-audit.yml`'s
      `lockfile-freshness` job fails the build otherwise.
- [ ] `cloudflare/downloads/`: `npm test`, `npm run typecheck` and `npm run dry-run` there pass
      ([§1.9](#19-formatting-and-static-analysis)).

### 2.8 Test design

Prefer tests that assert observable contracts rather than implementation trivia. For gated
connector operations, verify the complete relevant outcome:

- selected gate/policy path;
- whether the connector was called;
- returned result/error;
- approval state/decision;
- audit entry;
- auto-accept rule side effect when applicable.

Use parameterization where multiple connector/tool states share the same invariant.

Avoid fixed sleeps for synchronization when an event, condition poll, socket readiness check, or
explicit signal can make the test deterministic. Every async/process test must fail in bounded time
rather than hang indefinitely.

### 2.9 Live fixtures and provider drift

Parser/client unit tests replay committed, redacted live fixtures from
`tests/fixtures/live/<connector>/`; they must not require a network connection.

Real provider checks run through `scripts/qa_fixture_recorder.py` (`--check`, `--record`,
`--lifecycle`) against dedicated QA accounts, on a developer machine or on the self-hosted runner
through `connector-live-check.yml` (weekly, or dispatched) and `qa-record-fixture.yml` (records one
connector's fixture and commits it to the branch it was dispatched against). See
[`connector-qa.md`](connector-qa.md) for the accounts, the runner and both workflows, and
[`testing-policy.md`, layer 5](testing-policy.md#layer-5-live-connector) for where
this sits in the test policy.

Never commit real account identifiers, access tokens, tenant URLs, private content, or unredacted
provider payloads in fixtures.

### 2.10 Browser tests

Use the existing Playwright harness (`tests/integration/test_browser_smoke.py`, marker `browser`)
for behavior that only a real browser can prove: CSP enforcement, browser session behavior, JS/DOM
ordering, approval interactions, responsive structure, service-worker/notification behavior, and
org-mode WebAuthn UI.

Do not use pixel-perfect screenshots as the primary correctness assertion. Subjective visual
quality belongs in [`release-testing.md`](release-testing.md).

### 2.11 Security-sensitive changes

Changes to auth/session/CSRF/CSP, org identity/principal scoping, connector token storage,
approval/gate behavior, PII filtering, audit integrity/forwarding, configuration trust, staged
downloads, or MCP authentication require targeted negative tests in addition to the happy path.

Fail closed on malformed/unknown security configuration. A test should prove rejection rather than
only proving that valid configuration works.

Use the existing secure filesystem helpers (`secure_files.py`: `atomic_write_text`,
`secure_mkdir`, ...) for credential/security-sensitive files instead of open-coding permissions.
`tests/unit/test_systemic_gate_invariants.py`'s `TOKEN_WRITE_SITES` lists every token-file writer
and asserts each one uses the shared atomic-write helper.

### 2.12 Diagnostics for system and packaged-artifact tests

New `pytest.mark.system`/`pytest.mark.packaged` tests get CI-diagnostics capture
(`tests/diagnostics.py`, called from `tests/conftest.py`'s `pytest_runtest_makereport` hook) with no
per-test code, as long as:

- the test's own daemon home/install directory lives under its `tmp_path` (directly or via a
  fixture it depends on — see `test_org_ubuntu_release_smoke.py`'s `home = tmp_path / "home"`), and
- any subprocess log is named `daemon.log`, `install*.log` or `uninstall*.log`, or is a `*.jsonl`
  audit log.

A test that instead drives a real system-wide install (`dpkg -i`, not a `tmp_path`-scoped one)
needs its own small capture call into `tests.diagnostics.failure_dir()`/`suite_name_for()` — see
`test_deb_packaged_lifecycle.py`'s `_capture_installed_file_manifest` for the pattern.

## 3. Adding a connector

Use an existing connector of the same kind as the template — `apps_script` for a Google-backed
one, `telegram` for one outside the org bundle. The rows marked **enforced** fail a unit test (or,
for `qa_fixture_recorder.py`, an import-time assertion) when missed; the others fail only at
runtime or in review, so work through them deliberately.

**Client and connector**

| What | Where | Enforced by |
|---|---|---|
| API client with its own `<Name>ClientError` ([§1.4](#14-error-handling)); token writes through `secure_files` | `src/privacyfence/<name>_client.py` | **enforced** for token writes once added to `TOKEN_WRITE_SITES` in `tests/unit/test_systemic_gate_invariants.py` |
| `Connector` subclass: `tool_specs()`, dispatch, `gated_call` or `_auto_audit` per tool, a required `reason` `ToolParam` on every gated tool | `src/privacyfence/connectors/<name>.py` | **enforced**: `test_systemic_gate_invariants.py` (`reason` param, `pii_scan_text` on review-gated reads) |
| Tests per [§2.6](#26-new-connector-checklist) | `tests/unit/connectors/test_<name>_connector.py`, `tests/unit/test_<name>_client.py` | review |
| Add the class to `CONNECTOR_CLASSES` | `tests/unit/connectors/test_readme_manifest_alignment.py` and `tests/unit/test_systemic_gate_invariants.py` (two separate copies) | nothing — a connector missing here is silently skipped by every check built on these lists |

**Gate and policy tables**

| What | Where | Enforced by |
|---|---|---|
| Every tool's gate (`auto`/`review`/`popup`), matching the `gate=` its `gated_call` passes | `auto_accept.TOOL_TO_GATE` | **enforced**: `test_readme_manifest_alignment.py` (no missing or stale tools, matches the source) |
| Operation key for every `review`/`popup` tool | `auto_accept.TOOL_TO_OPERATION` | review — a missing key makes rules under the dotted name never match (see `tests/unit/test_auto_accept.py`) |
| Verb for every tool with an operation key | `policy/registry.py`'s `TOOL_TO_VERB` | **enforced**: `tests/unit/policy/test_registry.py` |
| "What approving does" sentence for every `popup` tool | `write_effects.EFFECT_BY_TOOL` | **enforced**: `tests/unit/test_write_effects.py` |
| Card layout for tools that show a body (`WIDE`); `NARROW` is the default | `gate._TOOL_LAYOUT` | optional |
| Rule scopes an "Always allow" can offer for the new operations | `policy/scopes.py` (`SCOPE_SELECTORS`/`NEW_SCOPE_SELECTORS`), `policy/catalogue.py` (`EXTRA_SCOPES`), `policy/propose.py` (`PROPOSABLE_SCOPES`), `policy/resource_registry.py` for grants | **enforced** once added: `tests/unit/policy/test_scopes.py` (each selector has a fixture), `test_catalogue.py`, `test_propose.py` |
| Regenerate the always-allow reference | `python3 scripts/generate_always_allow_reference.py` rewrites `docs/always-allow-rules-reference.md` | **enforced**: `tests/unit/test_generate_always_allow_reference.py` |

**Daemon, settings and packaging**

| What | Where | Enforced by |
|---|---|---|
| Import, credential path, build block honouring `connectors.<name>.enabled` | `daemon_main.py`: `TOKEN_FILES`, `build_connectors()` | review |
| Interactive auth: a `run_<name>_oauth()` and `--<name>-oauth` flag | `daemon_main.py` | review |
| Settings page: connector list, label, org-bundle section, client class | `settings_controller.py`: `ALL_CONNECTORS`, `_CONNECTOR_LABEL_OVERRIDES`, `ORG_CONFIG_SERVICE`, and for Google `GOOGLE_CONNECTORS`/`_GOOGLE_CLIENTS` | review |
| Org-mode per-user connect page: scopes, label, row | `web/routes_connect.py` | review |
| Explicit PyInstaller hidden import | `scripts/pyinstaller_common.py`'s `privacyfence.connectors.*` list | **enforced**: `tests/unit/test_pyinstaller_hidden_imports.py` (the list equals the modules in `connectors/`) |
| Approval-card icon (real brand asset only) | `src/privacyfence/resources/connector_icons/<name>.png` | optional; no icon renders cleanly |

**QA and docs**

| What | Where | Enforced by |
|---|---|---|
| Live check and recorded fixture | `scripts/qa_fixture_recorder.py`: `CONNECTOR_CHECKS` and `EXPECTED_FIXTURES` | **enforced**: import-time assertion that both have the same keys, plus `tests/unit/test_qa_fixture_recorder.py`'s `TestFixturePresence` (each listed fixture file exists and is valid JSON) |
| QA seed objects for the connector | `tests/fixtures/qa_environment.yaml.example` (the real, git-ignored `qa_environment.yaml` lives on the QA machine and runner) | review |
| QA account authentication step (OAuth connectors) | `scripts/qa_authenticate_connectors.py`'s `STEPS` | review |
| Record the fixture | dispatch `qa-record-fixture.yml` with `connector=<name>` against the feature branch; it commits `tests/fixtures/live/<name>/…` back to that branch — see [`connector-qa.md`](connector-qa.md#recording-one-connector) | the fixture-presence test above |
| Privacy-matrix rows (tool, direction, gate) | the doc `test_readme_manifest_alignment.py` parses (its `README_PATH`) | **enforced**: every tool documented, direction matches `read_only` |
| User-facing setup guide and a `CHANGELOG.md` line | `docs/<service>-setup.md` (or the shared Google guide), `## [Unreleased]` | review |

## 4. Packaging changes

When changing PyInstaller specs, installers, Debian metadata, startup registration, MCPB contents,
or release workflows, run the corresponding package/build checks and update
[`platform-support.md`](platform-support.md) if user-visible behavior changes. The packaged-artifact
smoke tests (`pytest.mark.packaged`) run only inside `build.yml`; dispatch it against the branch to
exercise them before merging.

Do not use a source checkout as proof that a packaged artifact works — see
[`dev-vs-live-setup.md`](dev-vs-live-setup.md).

## 5. Documentation

Update standing documentation in the same PR as behavior changes. Standing docs describe current
behavior, not implementation history: no phase narratives, issue numbers, version qualifiers or
"previously/after X" explanations. History belongs in `CHANGELOG.md` and the *why* in an ADR — see
[`adr/README.md`](adr/README.md) for when a plan document, an ADR or a reference doc is the right
home.

A new testing gap belongs in [`testing-policy.md`](testing-policy.md) if it changes current policy,
or in [`release-testing.md`](release-testing.md#what-stays-manual)'s "What stays manual" if it's a
standing open item — not in a plan document written to hold it.
