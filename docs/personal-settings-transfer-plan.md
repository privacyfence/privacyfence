# Personal settings export and import (desktop to organization server, and back)

## 1. Goal

A person who has used PrivacyFence on their own computer (local mode) and then moves to the
organization server (org mode) has to rebuild their always-allow rules by hand today: nothing
carries over, and the rules are the part of a setup that took weeks to accumulate. This plan adds
**Export my settings** and **Import my settings** to **Settings > Auto-accept**, in both modes, so a
person can take their personal settings out of one install and bring them into another.

Decisions already taken with the maintainer (not to be re-opened by a worker):

- The file carries **personal settings only**: the person's always-allow rules and their own
  step-up scope. No install-wide policy (privacy filter, PII detection), no credentials, no passkeys,
  no scoped tokens, no audit log.
- Import is **merge-only with a preview**: it never removes or narrows anything, and the person sees
  every row, and why a row would be skipped, before confirming.
- It works in **both directions** (local to org, org to local, org to org, local restore) with one file
  format.
- Delivery is **a file**: export downloads it, import uploads it in the browser. No MCP tool, no
  network path between the desktop app and the server.

Issue: none (requested directly). Tracking issue: none.

## 2. Current state

- Personal settings live in each person's own `settings.yaml`. In local mode that is `config/settings.yaml`
  under `paths.authority_dir()`; in org mode `users/<principal>/authority/config/settings.yaml`. In org
  mode only the `auto_accept` section of it is read (`docs/configuration-reference.md`, "Organization
  mode"). Everything else in a local `settings.yaml` is either install-wide (`privacy`, `*_privacy`,
  `pii_detection`, which in org mode are the administrator's) or local-only (`connectors.<name>.enabled`,
  `web.notifications.detail`, `agent_overrides`, `update_check`, `logging`).
- The rules are `ScopeRow`s (`src/privacyfence/policy/scope_rows.py:36`), stored as
  `auto_accept: {version: 3, rules: [...]}`. `scope_rows.row_to_dict` / `row_from_dict` /
  `rows_to_config` / `rows_from_config` already define the on-disk row shape and fail closed on a
  malformed row. A row id is content-derived (`scope_rows.row_id_for`), so the same rule has the same id
  in every install.
- The one merge every writer shares is `auto_accept.add_policy_v2_rules(rules)`
  (`src/privacyfence/auto_accept.py:624`): it takes `PolicyRule`s, merges them into rows via
  `scope_rows.add_rules`, writes the file, hot-reloads the store and notifies listeners. Its inputs
  come from `scope_rows.compile_rows(rows)`. It reads and writes the **current principal's** file
  (`current_principal()`), so a caller must be inside `principal_scope(principal)`.
  `auto_accept.get_policy_rows()` (`auto_accept.py:590`) reads the current principal's rows from disk.
- A person's own step-up scope is `step_up_config.personal_scope(principal)` and is written with
  `step_up_config.set_personal_scope(principal, scope, floor=...)` (`src/privacyfence/step_up_config.py:119,135`);
  the stored value can only widen the configured minimum (ADR 0091).
- `describe.row_sentence(row, value_display)` (`src/privacyfence/policy/describe.py:144`) renders a row as
  a human sentence; the Auto-accept page already uses it.
- The settings routes live in `src/privacyfence/web/routes_settings.py`: `build_routes` (local mode,
  ends at line ~1325) and `build_org_routes` (org mode, ends at line ~2076). Bespoke routes must be
  classified in `_BESPOKE_SENSITIVE_ROUTE_PATHS` (step-up gated when step-up is in force) or
  `_BESPOKE_EXEMPT_ROUTE_PATHS` (with a reason), or `build_routes` raises (`routes_settings.py:1342`);
  `TestBespokeRoutesAreClassified` (`tests/unit/web/test_routes_settings.py:1971`) re-asserts it.
  `org_config_upload` (`routes_settings.py:1018`) is the pattern for a multipart upload with CSRF,
  origin, human-session and step-up gates; `audit_log_download` (`routes_settings.py:1293`) is the pattern
  for a download. The org scoped-token routes (`_org_scoped_token_request`, `routes_settings.py:1706`)
  are the pattern for an org route that runs `_verify_or_challenge` with `principal`.
- The browser shim `_settings_bridge_shim` (`routes_settings.py:493`) is injected into both modes' pages
  after `PF_WEBAUTHN_JS` (`routes_settings.py:894-895` local, `1860-1862` org). Its multipart upload
  (`attemptOrgUpload`) shows how a 428 step-up answer is completed with `pfWebauthnGet` and the file
  re-posted.
- `settings_window_html.py` renders the page; `renderAutoAccept` is at line 881, capabilities are
  emitted by `build_html` (line 2324, `window.__pfCapabilities` at ~2404).
- Nothing today exports or imports personal settings. The org bundle is the only thing a person
  uploads (`org_config_upload`); it is admin-built and signed, and is unrelated.

## 3. Design

Every name below is final. A worker implements these, it does not rename them.

### 3.1 The file

`privacyfence-personal-settings.json`, UTF-8 JSON, at most `MAX_IMPORT_BYTES = 262144` bytes:

```json
{
  "format": "privacyfence-personal-settings",
  "format_version": 1,
  "exported_at": "2026-10-16T09:30:00+00:00",
  "source": {"mode": "local", "version": "6.3.0"},
  "auto_accept": {"version": 3, "rules": [ /* scope_rows.row_to_dict output */ ]},
  "step_up": {"scope": "writes_and_reads"},
  "not_portable": [{"item": "Connector sign-ins", "note": "Connect your accounts again on the new install."}]
}
```

- `auto_accept` is exactly `scope_rows.rows_to_config(rows)`; it is the `ScopeRow`s on disk, version 3.
- `step_up.scope` is `step_up_config.personal_scope(principal)` (a value on `STEP_UP_SCOPES`) or `null`
  when the person never chose one.
- `exported_at` is UTC ISO-8601 with offset; `source.mode` is `"local"` or `"org"`; `source.version` is
  `privacyfence.__version__`.
- `not_portable` is informational, identical in every export, ignored by import. Its entries are the
  constant `NOT_PORTABLE` in `personal_settings.py`, in this order (item / note):
  1. `Connector sign-ins and tokens` / `Connect your accounts again on the new install; resource ids in rules belong to the account they were made with.`
  2. `Passkeys` / `Enroll a passkey on the new install at /security.`
  3. `Scoped tokens` / `Create new ones on the new install if your administrator allows them.`
  4. `Privacy filter and PII detection` / `Install-wide: a desktop install's own, an administrator's on an organization server.`
  5. `Connector on/off switches, notification detail, AI system names, update checks, log level` / `Desktop-only settings; set them again if you move back to a desktop install.`
  6. `Audit log` / `Stays on the install that wrote it.`
- Nothing secret or identifying is written: no account addresses, no token, no passkey, no principal id.
- Unsigned, deliberately (see ADR 0194): a personal file has no key to sign with, and the import's
  safety rests on the preview, the file hash bound into the step-up, and the merge-only rule.

### 3.2 Module `src/privacyfence/personal_settings.py` (pure, no I/O, no Starlette)

```python
FORMAT = "privacyfence-personal-settings"
FORMAT_VERSION = 1
FILE_NAME = "privacyfence-personal-settings.json"
MAX_IMPORT_BYTES = 262_144
MAX_IMPORT_ROWS = 500

class PersonalSettingsError(ValueError): ...   # message is safe to show to the person

@dataclass(frozen=True)
class PersonalSettingsDocument:
    rows: tuple[ScopeRow, ...]
    dropped_rows: int          # entries scope_rows.row_from_dict refused
    step_up_scope: StepUpScope | None
    source_mode: str           # "local" | "org" | "" when absent
    exported_at: str           # as written, "" when absent

class ItemStatus(str, Enum): ADD="add"; EXTEND="extend"; PRESENT="present"; SKIP="skip"

@dataclass(frozen=True)
class ImportItem:
    row: ScopeRow
    status: ItemStatus
    reason: str                # "" unless SKIP (or EXTEND: what is added)
    sentence: str              # describe.row_sentence(row)

class StepUpStatus(str, Enum): APPLY="apply"; UNCHANGED="unchanged"; SKIP="skip"

@dataclass(frozen=True)
class StepUpItem:
    status: StepUpStatus
    file_scope: StepUpScope | None
    reason: str

@dataclass(frozen=True)
class ImportPlan:
    items: tuple[ImportItem, ...]
    dropped_rows: int
    step_up: StepUpItem

    def counts(self) -> dict[str, int]       # keys "add","extend","present","skip"
    def to_preview(self, *, sha256: str, document: PersonalSettingsDocument) -> dict[str, Any]

def build_export(rows, personal_step_up_scope, *, mode, version, now) -> dict[str, Any]
def export_bytes(document: dict[str, Any]) -> bytes        # json.dumps(indent=2, sort_keys=False) + "\n", utf-8
def parse_document(raw: bytes) -> PersonalSettingsDocument  # raises PersonalSettingsError
def plan_import(document, existing_rows, *, step_up_active, step_up_floor, current_personal_scope) -> ImportPlan
def apply_import(plan: ImportPlan, *, principal, step_up_floor) -> dict[str, Any]  # needs principal_scope entered by caller
```

`parse_document` errors, exact messages:

- larger than `MAX_IMPORT_BYTES`: `"This file is too large to be a PrivacyFence settings export."`
- not UTF-8 or not JSON or not an object: `"This is not a PrivacyFence settings export."`
- `format` is not `FORMAT`: `"This is not a PrivacyFence settings export."`
- `format_version` is an int greater than 1: `"This file was made by a newer PrivacyFence. Update this install and try again."`; any other value not equal to 1: `"This is not a PrivacyFence settings export."`
- `auto_accept.rules` missing or not a list: `"This settings export has no rules section."` (an *empty* list is valid).
- more than `MAX_IMPORT_ROWS` entries: `"This settings export has more than 500 rules."`

Rows go through `scope_rows.row_from_dict`; each `None` increments `dropped_rows`. An unknown top-level key is
ignored. `step_up.scope` that is not on `STEP_UP_SCOPES` reads as `None`.

`plan_import` classifies each row, in file order, first match wins:

1. operations not all in `registry.TOOL_REGISTRY`'s operation keys (use the same key set `scope_rows.missing_operations` /
   `registry` expose; the worker finds the helper that lists known operation keys and uses it) → `SKIP`,
   reason `"Needs a newer PrivacyFence (unknown tool)."`
2. a custom row (`row.scope is None`) whose `predicate` is not known to `policy.engine._selector_for` (use a
   public wrapper if one exists, else add `engine.is_known_predicate(predicate) -> bool` returning
   `_selector_for(predicate) is not None`) or one of whose condition names is not in
   `conditions.CONDITION_SELECTORS` → `SKIP`, reason `"Needs a newer PrivacyFence (unknown rule type)."`
3. `scope_rows.compile_row(row)` returns `[]` → `SKIP`, reason `"This rule cannot be applied by this version."`
4. an existing row with the same id covers every operation and verb of the file's row → `PRESENT`
5. an existing row with the same id lacks some operations or verbs → `EXTEND`, reason
   `"Adds <n> tool(s) to a rule you already have."` (n = operations added)
6. otherwise `ADD`.

Rows with the same id twice in the file: the second is `PRESENT` (it is covered by the first).

The step-up item: `file_scope is None` → `UNCHANGED`, reason `"The file sets no personal step-up level."`;
`step_up_active` is false → `SKIP`, reason `"Passkey step-up is not turned on on this install."`;
`widest_scope(step_up_floor, current_personal_scope or step_up_floor) ` already covers `file_scope`
(compare with `step_up_config.widest_scope`) → `UNCHANGED`, reason `"Already at this level or wider."`; otherwise `APPLY`.
`step_up_floor` is `None` when step-up is not active.

`apply_import` (caller is inside `principal_scope(principal)`): collect the rows whose status is `ADD` or `EXTEND`,
call `auto_accept.add_policy_v2_rules(scope_rows.compile_rows(those_rows))` once, then if the step-up item is `APPLY` call
`step_up_config.set_personal_scope(principal, file_scope, floor=step_up_floor)`. Returns
`{"added": n_add, "extended": n_extend, "present": n_present, "skipped": n_skip, "step_up_applied": bool}`. It never calls
`remove_policy_v2_rule`, `set_policy_rule_verbs` or anything that narrows.

`to_preview` returns (this is the JSON the preview route answers; keys exact):

```json
{"sha256": "<hex>", "source_mode": "local", "exported_at": "...",
 "counts": {"add": 3, "extend": 1, "present": 2, "skip": 1}, "dropped_rows": 0,
 "items": [{"id": "r-…", "sentence": "…", "status": "add", "reason": ""}],
 "step_up": {"status": "apply", "scope": "writes_and_reads", "reason": ""},
 "warnings": ["Rules name resources (folders, channels, projects) by id. They only match if you connect the same accounts here."]}
```

`warnings` always contains the sentence above when at least one item is `ADD` or `EXTEND`, and, when `dropped_rows > 0`,
`"<n> entr(y|ies) in the file could not be read and were left out."` (`entry`/`entries` by count).

### 3.3 Routes `src/privacyfence/web/personal_settings_routes.py`

One factory used by both modes, so the two cannot drift:

```python
@dataclass(frozen=True)
class PersonalSettingsHooks:
    authenticate: Callable[[Request], Principal | None]            # None → 401
    check_mutation: Callable[[Request, str | None], Response | None]  # csrf, origin, human session; a Response refuses
    open_scope: Callable[[Principal], ContextManager[None]]        # enters principal_scope and loads that principal's settings
    step_up_state: Callable[[], tuple[bool, str | None]]           # (active, configured floor scope or None)
    step_up_gate: Callable[[Principal, dict[str, Any], object], Response | None]  # fingerprint body, assertion
    audit: Callable[[Principal, str], None]
    mode: str                                                      # "local" | "org"

def build_personal_routes(hooks: PersonalSettingsHooks) -> list[Route]
```

Routes (all under the settings prefix, none match `/api/settings/{action}` because they have two path segments):

- `GET /api/settings/personal/export` → `200`, `Content-Type: application/json`,
  `Content-Disposition: attachment; filename="privacyfence-personal-settings.json"`, `Cache-Control: no-store`.
  Gate: `authenticate`. Reads rows and the personal scope inside `open_scope`, audits
  `"Exported personal settings (<n> rule(s)) (principal=<id>)"`.
- `POST /api/settings/personal/preview` (multipart: `file`, `csrf`) → JSON of `to_preview`. Gates: authenticate, `check_mutation`.
  Changes nothing. `PersonalSettingsError` → `400 {"error": <message>}`. Missing file → `400 {"error": "missing file"}`.
- `POST /api/settings/personal/import` (multipart: `file`, `csrf`, `sha256`, `confirmed`, optional `webauthn_assertion`
  as a JSON-encoded string, parsed the way `org_config_upload` parses it with `_parse_form_assertion`) → `200` with
  `apply_import`'s dict. Gates in order: authenticate, `check_mutation`, `confirmed == "true"` else `400 {"error": "confirmation required"}`,
  size, `parse_document`, `sha256` of the uploaded bytes equals the `sha256` field else `400 {"error": "file_changed"}`,
  then `step_up_gate(principal, {"sha256": <hex>}, assertion)` (subject key `import_personal_settings`), then — inside
  `open_scope` — re-read the current rows, recompute `plan_import` (never trust the earlier preview), `apply_import`,
  audit `"Imported personal settings: <a> added, <e> extended, <p> already present, <s> skipped (principal=<id>)"`.
  An audit line never carries a rule value or a resource id.

Local wiring (`build_routes`): `authenticate` = `_authenticated(request)` → `LOCAL_PRINCIPAL`; `check_mutation` = CSRF + `_origin_ok` +
`require_human_session`/`_is_human_session` (the checks `org_config_upload` makes at `routes_settings.py:1022-1034`);
`open_scope` = `principal_scope(LOCAL_PRINCIPAL)`; `step_up_state` from the `step_up` argument (`enabled`/`require_passkey`, `scope`);
`step_up_gate` = `_verify_or_challenge(...)` exactly as `org_config_upload` calls it, with subject key `import_personal_settings`.
Org wiring (`build_org_routes`): `authenticate` = `_current_principal(request)`; `check_mutation` = `org_session.check_csrf` + `org_session.check_origin`;
`open_scope(principal)` = `principal_scope(principal)` + `_ensure_principal_settings_loaded()`; `step_up_gate` as in `_org_scoped_token_request`.
Step-up is "in force" when `step_up is not None and step_up.enabled and step_up.require_passkey`, the same test both modes already use.

Classification: `/api/settings/personal/import` goes in `_BESPOKE_SENSITIVE_ROUTE_PATHS` (it widens what is auto-accepted, exactly as adding
a rule does); `/api/settings/personal/preview` and `/api/settings/personal/export` go in `_BESPOKE_EXEMPT_ROUTE_PATHS` with reasons
`"parses an uploaded file and returns a preview; changes nothing"` and `"a GET -- read-only export, no mutation"`.

### 3.4 Page

In `settings_window_html.py`, `renderAutoAccept` gets a card **"Move your settings"** between the page subtitle and the filter bar,
drawn only when `CAPS.personalTransfer` is true. Text: title `Move your settings`; body `Export your always-allow rules and your step-up level to a file, or bring them in from another PrivacyFence install. Importing only adds; it never removes or narrows anything.`;
buttons `Export…` (`data-pf-personal-export="1"`) and `Import…` (`data-pf-personal-import="1"`). `build_html` gets a keyword
`personal_transfer: bool = False`, emitted as `personalTransfer` in `window.__pfCapabilities`; both web page renderers pass `True`.

A new module `src/privacyfence/web/personal_settings_shim.py` exposes `personal_transfer_shim(*, csrf: str, nonce: str) -> str`, a
`<script nonce>` injected right after `_settings_bridge_shim(...)` in both pages. It: on a `[data-pf-personal-export]` click sets
`window.location = '/api/settings/personal/export'`; on `[data-pf-personal-import]` clicks a hidden file input; on file chosen POSTs it to
`/api/settings/personal/preview`; renders a modal (`role="dialog"`, `aria-modal="true"`, focus moved in, Escape and a **Cancel** button close it)
whose rows are built with `textContent` only (never `innerHTML`), grouped *Will be added*, *Will be extended*, *Already there*, *Skipped (with reason)*, then
the step-up line and the warnings; its confirm button reads `Import <add+extend> rule(s)` and is disabled when that is 0 and the step-up status is not `apply`;
confirm POSTs the same file with `sha256`, `confirmed=true`, and handles `428` (complete with `pfWebauthnGet`, re-post with `webauthn_assertion`) and `403`
(`enroll_url`) exactly as `attemptOrgUpload` does; on success it alerts nothing and reloads the page.

### 3.5 Rejected alternatives (they become the ADRs)

- **Copy `settings.yaml` / export everything.** Rejected: most keys are install-wide or desktop-only and meaningless or dangerous in org mode; import would have to filter, and the filter is the feature.
- **A signed export, like the org bundle.** Rejected: there is no key for a person to sign with, and a signature would not tell the person whether the *contents* are what they want; the preview, the hash bound into the step-up and merge-only import do.
- **Replace mode ("make my rules equal the file").** Rejected: it can delete or narrow rules, which makes a hostile or stale file more powerful than any single Settings action.
- **An MCP tool or a server-to-desktop sync.** Rejected: an agent submitting a rule set is a different trust question; a file the person moves themselves keeps the human in the loop and adds no network path.
- **Keeping the uploaded file on the server between preview and confirm.** Rejected: stateless two-request flow, the file is re-uploaded and re-planned against the current rows, so a stale preview cannot be applied.

## 4. ADRs

Next free number at the time of writing is 0194; **re-check `docs/adr/` in phase p5 and renumber if a parallel branch took them.**

- **0194** — Personal settings are exported as a file of the person's auto-accept rows and step-up level, and nothing else.
- **0195** — Importing personal settings is merge-only, previewed, and step-up gated on the hash of the file previewed.

## 5. Manual steps

No `manual_before`. One `manual_after`, `ma1-real-migration`: move a real desktop install's rules to a real organization server and back, which CI cannot do (needs a real
desktop install, an IdP and a passkey). Step-by-step page: the manual-steps artifact (linked in the manifest).

## 6. Risks and open questions

- **Round trip through `add_policy_v2_rules` may not reproduce a row exactly** (verbs are re-derived from the compiled rule in `scope_rows.add_rules`). Phase p1 writes the round-trip test first
  (export rows → `compile_rows` → `add_rules` into empty rows → `row_to_dict` equal to the exported rows, for a scoped row, an extra-scope row and a custom row). If any
  differs, the worker stops with `status=blocked` and reports the row; it must not change `scope_rows.add_rules`.
- **`registry`'s operation-key helper may not be public.** The brief says to use the existing key set; if there is none that is importable without a cycle (`registry` ← `scope_rows` already imports it, so there should not be), stop with `status=blocked`.
- **Org page loads a principal's settings lazily** (`_ensure_principal_settings_loaded`). If calling `auto_accept.get_policy_rows()` inside `open_scope` raises `RuntimeError("auto_accept config path not initialized")` in the org tests, the hook is wrong; stop, do not catch the error.
- **Rules name resource ids by account.** The file cannot know whether the target is connected to the same accounts. This is surfaced as a preview warning and in the docs, not solved.
- **The browser shim is JS in a Python string.** The browser test in p4 is the only thing that proves it runs; if the browser test cannot run in the container (see `PRIVACYFENCE_TEST_CHROMIUM` in the session hook output), say so in the phase result rather than skipping it silently.
- **Parallel ADR numbering** (see §4).

## Implementation manifest

```yaml
plan_slug: personal-settings-transfer
feature_branch: feature/personal-settings-transfer
max_parallel: 1
manual_steps_artifact: https://claude.ai/artifact/AjiWXxy7a7BgrJehCKEzNo
manual_steps_source: docs/personal-settings-transfer-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-real-migration
    title: Move a real desktop install's rules to a real organization server and back
    why: CI cannot exercise a real desktop Settings page download, a real identity-provider sign-in or a real passkey prompt on the import.
verify_after_merge:
  - python3 -m pytest tests/unit/test_personal_settings.py tests/unit/web/test_personal_settings_routes.py -q
final_checks:
  - docs/personal-settings-transfer-plan.md and docs/personal-settings-transfer-plan-manual-steps.html are deleted and nothing links to them
  - docs/adr/ has the two ADRs from the ADRs section, each Accepted, and both are in docs/adr/README.md's index
  - CHANGELOG.md has an entry under "## [Unreleased]" and no version heading
phases:
  - id: p1-core-module
    title: Pure export/import module and the row round trip
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/personal_settings.py
      - src/privacyfence/policy/engine.py
      - tests/unit/test_personal_settings.py
    brief: |
      Context: design is section 3.1 and 3.2 of docs/personal-settings-transfer-plan.md; read them and
      src/privacyfence/policy/scope_rows.py, src/privacyfence/auto_accept.py (lines 540-720),
      src/privacyfence/step_up_config.py (lines 80-170), src/privacyfence/policy/describe.py (row_sentence)
      and docs/coding-and-testing-guidelines.md section 2.7 first.
      1. Create tests/unit/test_personal_settings.py with the round-trip test FIRST: build three ScopeRows
         (one scoped row from propose.SCOPES_BY_GROUP, one from catalogue.EXTRA_SCOPES, one custom row with a
         predicate and a condition), export them with build_export, parse_document the bytes, compile_rows,
         scope_rows.add_rules into [] and assert [row_to_dict(r) for r in result] equals the exported rows.
         If it fails for any row, stop with status=blocked and report which row and how it differs.
      2. Add engine.is_known_predicate(predicate: str) -> bool in src/privacyfence/policy/engine.py returning
         _selector_for(predicate) is not None. Nothing else in engine.py changes.
      3. Create src/privacyfence/personal_settings.py with exactly the names, signatures, messages and status
         rules of section 3.2 (FORMAT, FORMAT_VERSION, FILE_NAME, MAX_IMPORT_BYTES, MAX_IMPORT_ROWS,
         NOT_PORTABLE, PersonalSettingsError, PersonalSettingsDocument, ItemStatus, ImportItem, StepUpStatus,
         StepUpItem, ImportPlan, build_export, export_bytes, parse_document, plan_import, apply_import).
         Imports are limited to the standard library, privacyfence.__version__ is NOT imported here (the
         caller passes version), and policy.scope_rows, policy.registry, policy.describe, policy.engine,
         policy.conditions, step_up_config, auto_accept. apply_import imports auto_accept lazily if a cycle appears.
      4. Extend tests/unit/test_personal_settings.py to cover, one test each: every parse_document error
         message; a newer format_version; dropped_rows counting; the ten plan_import outcomes (unknown tool SKIP,
         unknown predicate SKIP, unknown condition SKIP, uncompilable SKIP, PRESENT, EXTEND with the right
         count in reason, ADD, duplicate id in file, step-up APPLY/UNCHANGED/SKIP in each of its four
         branches); to_preview's exact keys and both warnings; apply_import adds and extends, never removes
         (existing unrelated row survives) and sets the personal scope only on APPLY, using the fixtures in
         tests/unit/test_auto_accept.py for the registry/config path; that export_bytes of a document contains none of
         the strings "token", "secret", "credential" outside NOT_PORTABLE's own notes (assert on the keys of
         the top-level object being exactly format, format_version, exported_at, source, auto_accept, step_up, not_portable).
      5. New module-level state: none. If any appears, add its reset to tests/conftest.py.
      6. Run: ruff check ., python3 -m pytest tests/unit/test_personal_settings.py tests/unit/policy -q,
         python3 -m mypy src/privacyfence, and scripts/check_coverage_floor.py is satisfied by 100% of the new
         module being covered (pytest --cov=src/privacyfence/personal_settings --cov-branch --cov-report=term-missing).
      Stop with status=blocked if: the round trip in step 1 fails; there is no importable list of known
      operation keys; compile_row returns a different number of rules than add_rules consumes for a scoped row.
    acceptance:
      - "python3 -m pytest tests/unit/test_personal_settings.py -q passes, including the round-trip test"
      - "pytest tests/unit/test_personal_settings.py --cov=src/privacyfence/personal_settings --cov-branch --cov-report=term-missing shows 100% for src/privacyfence/personal_settings.py"
      - "grep -n 'def is_known_predicate' src/privacyfence/policy/engine.py prints one line"
      - "grep -rn 'remove_policy_v2_rule\\|set_policy_rule_verbs' src/privacyfence/personal_settings.py prints nothing"
      - "ruff check . and python3 -m mypy src/privacyfence pass"
  - id: p2-local-routes
    title: Shared route factory, mounted in local mode
    depends_on: [p1-core-module]
    complexity: M
    touches:
      - src/privacyfence/web/personal_settings_routes.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/web/test_personal_settings_routes.py
      - tests/unit/web/test_routes_settings.py
    brief: |
      Context: design is section 3.3. Read src/privacyfence/web/routes_settings.py lines 220-335 (action
      classification), 1004-1090 (org_config_upload), 1293-1330 (audit_log_download and the routes list),
      and ADR docs/adr/0034-sensitive-settings-writes-require-step-up-in-both-modes.md first. Copy the
      testing style of tests/unit/web/test_routes_settings.py (its TestClient setup and step-up fixtures).
      1. Create src/privacyfence/web/personal_settings_routes.py with PersonalSettingsHooks and
         build_personal_routes(hooks) exactly as section 3.3 states: the three routes, their gate order,
         status codes, JSON keys, headers, the audit strings, and subject key "import_personal_settings".
         Read the upload with `await upload.read()` and refuse more than MAX_IMPORT_BYTES with
         400 {"error": "file too large"} (mirror org_config_upload's size check).
      2. In routes_settings.py add "/api/settings/personal/import" to _BESPOKE_SENSITIVE_ROUTE_PATHS and the
         two other paths to _BESPOKE_EXEMPT_ROUTE_PATHS with the reasons in section 3.3.
      3. In build_routes (local mode) build a PersonalSettingsHooks as section 3.3 describes for local wiring
         and append build_personal_routes(hooks) to `routes` BEFORE the final generic Route("/api/settings/{action}"...).
         step_up_state returns (step_up.enabled, step_up.scope) from the existing `step_up` argument, or (False, None) when it is None.
         The step-up gate itself runs only when `step_up.enabled and step_up.require_passkey`, as _step_up_in_force() does.
      4. Tests in tests/unit/web/test_personal_settings_routes.py (local mode app, same fixtures as test_routes_settings.py):
         export is a download with the exact headers and a body parse_document accepts; export audits; preview of a good file
         returns the exact keys and writes nothing (rows unchanged); preview of each bad file returns 400 with the exact message;
         import without csrf 401, wrong origin 403, without confirmed 400, with a sha256 that does not match 400 "file_changed",
         oversized 400; import with step-up in force and no assertion returns 428 with webauthn_options, with a valid assertion
         (reuse the software authenticator helpers from test_routes_settings.py) applies; import merges (an existing unrelated
         rule survives, a duplicate is PRESENT); applying twice is a no-op the second time; the audit line has counts and no rule value;
         a stale preview is re-planned (change the rows between preview and import and assert the import reports against the new rows).
      5. TestBespokeRoutesAreClassified in tests/unit/web/test_routes_settings.py must still pass; extend it only if it enumerates paths by hand.
      6. Run: ruff check ., python3 -m pytest tests/unit/web -q, python3 -m mypy src/privacyfence, python3 scripts/mypy_strict_modules.py.
      Stop with status=blocked if: build_routes raises for an unclassified route after step 2; the generic /api/settings/{action}
      route swallows a personal path (it must not: it is registered last and the paths have two segments).
    acceptance:
      - "python3 -m pytest tests/unit/web/test_personal_settings_routes.py tests/unit/web/test_routes_settings.py -q passes"
      - "grep -c 'api/settings/personal' src/privacyfence/web/routes_settings.py is at least 3"
      - "the personal_settings_routes.py module is 100% covered: pytest tests/unit/web/test_personal_settings_routes.py --cov=src/privacyfence/web/personal_settings_routes --cov-branch shows no missing lines"
      - "ruff check ., python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass"
  - id: p3-org-routes
    title: Mount the same routes in organization mode
    depends_on: [p2-local-routes]
    complexity: S
    touches:
      - src/privacyfence/web/routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Context: design is section 3.3 "Org wiring". Read src/privacyfence/web/routes_settings.py lines 1390-1440 and
      1649-1830 (org helpers: _current_principal, _ensure_principal_settings_loaded, _org_scoped_token_request,
      _guard_step_up) and tests/unit/web/test_routes_org_settings.py (fixtures, TestScopedTokenRoutes).
      1. In build_org_routes build a PersonalSettingsHooks for org mode and append build_personal_routes(hooks) to the returned
         list BEFORE Route("/api/settings/{action}", ...). open_scope is a small contextmanager defined next to the other
         org helpers: it enters principal_scope(principal) and calls _ensure_principal_settings_loaded(), and lets a
         policy_store.V1PolicyConfigError propagate (the route then answers 500 with _v1_settings_message(principal, exc), like
         scoped_token_create). check_mutation is org_session.check_csrf + org_session.check_origin. step_up_gate is
         _verify_or_challenge(...) with the principal, as in _org_scoped_token_request. Step-up active = the org's `step_up`
         argument enabled; floor = its scope.
      2. Tests in tests/unit/web/test_routes_org_settings.py (new class TestPersonalSettingsRoutes): a signed-in user exports and
         the file parses; two principals are isolated (principal A's export has none of principal B's rows, and A's import does not
         touch B's file); import by a non-admin works (it is per-person, not admin-only); an unauthenticated request is 401 on all
         three routes; import with require_passkey on needs the assertion (428) and applies with a valid one; the step-up scope
         from the file widens the person's own scope and never goes below the org floor (set_personal_scope floor);
         local-to-org: a file produced by build_export in local shape imports into an org principal and the rows land in
         that principal's settings.yaml (read it back with auto_accept.get_policy_rows() inside principal_scope).
      3. Run: ruff check ., python3 -m pytest tests/unit/web -q, python3 -m mypy src/privacyfence.
      Stop with status=blocked if: auto_accept.get_policy_rows() cannot be read for a second principal in the org test harness
      (do not work around it by touching another principal's file by hand).
    acceptance:
      - "python3 -m pytest tests/unit/web/test_routes_org_settings.py -k PersonalSettings -q passes"
      - "python3 -m pytest tests/unit/web -q passes"
      - "ruff check . and python3 -m mypy src/privacyfence pass"
  - id: p4-page
    title: The Move your settings card, preview dialog and browser test
    depends_on: [p3-org-routes]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/web/personal_settings_shim.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/web/test_personal_settings_shim.py
      - tests/integration/test_settings_personal_transfer_browser.py
    brief: |
      Context: design is section 3.4. Read src/privacyfence/settings_window_html.py lines 300-340 (CAPS), 881-960
      (renderAutoAccept) and 2324-2410 (build_html), src/privacyfence/web/routes_settings.py lines 493-640
      (_settings_bridge_shim) and the existing browser tests tests/integration/test_settings_update_rule_browser.py and
      tests/integration/test_settings_scoped_token_form_browser.py (copy their harness).
      1. settings_window_html.py: add the keyword `personal_transfer: bool = False` to build_html and emit
         `personalTransfer` in window.__pfCapabilities; add `personalTransfer: false` to the CAPS default object; add the
         "Move your settings" card to renderAutoAccept with the exact text and attributes in section 3.4, drawn only when
         CAPS.personalTransfer. Use the existing esc() helper for every string.
      2. Create src/privacyfence/web/personal_settings_shim.py with personal_transfer_shim(*, csrf, nonce) -> str as in
         section 3.4. All DOM text through textContent; the modal has role="dialog", aria-modal="true", an accessible name,
         focus moved to its first button on open and back on close, Escape closes. The same file is re-posted for the import
         (keep the File object; do not read it into a string).
      3. routes_settings.py: pass personal_transfer=True to build_html in _render_settings_page and in _wrap_org_settings,
         and append `personal_transfer_shim(csrf=csrf, nonce=nonce)` right after each `_settings_bridge_shim(...)` call.
      4. Unit tests: tests/unit/test_settings_window_html.py (card present with personal_transfer=True in both modes, absent
         by default); tests/unit/web/test_personal_settings_shim.py (the shim string contains the three URLs, the nonce, no
         innerHTML, and escapes a hostile csrf value the way _settings_bridge_shim does). Both the local and the org page
         responses (existing fixtures) contain the shim.
      5. Browser test tests/integration/test_settings_personal_transfer_browser.py with the harness of the existing browser tests:
         open Auto-accept, click Import…, choose a fixture file with one add, one present and one skip row, assert the dialog lists
         the three groups and the confirm button reads `Import 1 rule(s)`, Cancel changes nothing, confirm applies and the new rule
         appears on the reloaded page; a hostile sentence/reason containing `<img src=x onerror=...>` in a skipped row renders as text.
         Click Export… and assert the download's file name and that it parses. Use PRIVACYFENCE_TEST_CHROMIUM as the other browser tests do.
      6. Run: ruff check ., python3 -m pytest tests/unit -q, python3 -m pytest tests/integration/test_settings_personal_transfer_browser.py -q,
         python3 -m mypy src/privacyfence.
      Stop with status=blocked if: the browser test cannot launch Chromium in the container (report the exact error; do not skip it
      silently); build_html's existing callers outside routes_settings.py need the new keyword (it is optional, none should).
    acceptance:
      - "python3 -m pytest tests/unit/test_settings_window_html.py tests/unit/web/test_personal_settings_shim.py -q passes"
      - "python3 -m pytest tests/integration/test_settings_personal_transfer_browser.py -q passes"
      - "grep -c 'innerHTML' src/privacyfence/web/personal_settings_shim.py prints 0"
      - "python3 -m pytest tests/unit -q passes and ruff check . passes"
  - id: p5-retire
    title: Docs, ADRs, changelog; delete the plan
    depends_on: [p4-page]
    complexity: S
    touches:
      - docs/org-mode-setup-guide.md
      - docs/approvals-and-policy.md
      - docs/configuration-reference.md
      - docs/getting-started.md
      - docs/adr/README.md
      - docs/adr/0194-personal-settings-are-exported-as-a-file-of-auto-accept-rows-and-the-step-up-level.md
      - docs/adr/0195-importing-personal-settings-is-merge-only-previewed-and-step-up-gated-on-the-file-hash.md
      - CHANGELOG.md
      - docs/personal-settings-transfer-plan.md
      - docs/personal-settings-transfer-plan-manual-steps.html
    brief: |
      Context: docs/adr/README.md (template, rules, index), CONTRIBUTING.md "Decisions, plans and ADRs", sections 3 and 4 of this plan.
      1. Re-check docs/adr/ for the next two free numbers. Use 0194 and 0195 if free; otherwise renumber both file names, the
         titles and every cross-reference in this phase.
      2. Write the two ADRs from the template in docs/adr/README.md, Status "Accepted — 2026-10-16. Implemented.", one decision each,
         Context/Decision/Alternatives considered from section 3.5 and the Decision text of section 4, Consequences (resource ids belong
         to an account; a hostile file can only add, and only after a preview and a passkey), Verification naming
         tests/unit/test_personal_settings.py, tests/unit/web/test_personal_settings_routes.py,
         tests/unit/web/test_routes_org_settings.py::TestPersonalSettingsRoutes and the browser test. ADR 0194 links ADR 0091 and ADR 0034;
         ADR 0195 links ADR 0034, ADR 0032 and ADR 0172. Add both to the index table in docs/adr/README.md in the same format as row 0193.
         Never link the plan document.
      3. docs/org-mode-setup-guide.md, section 10: add "### Moving a person from a desktop install" after "Per-person rules." with
         the steps: export from the desktop (Settings > Auto-accept > Export…), sign in to the organization server, connect the same
         accounts at /connect, Settings > Auto-accept > Import…, read the preview, confirm with the passkey; what does not move
         (copy NOT_PORTABLE's six items as a list); that an administrator cannot import for someone else. Update the Contents list only if
         it lists subsections (it does not).
      4. docs/approvals-and-policy.md, section "Where rules are stored": one paragraph after the list pointing at the export/import and
         at the new org guide subsection. docs/configuration-reference.md: a short "Exporting and importing personal settings" subsection
         at the end of "How settings.yaml is read" with the file format table (the keys of section 3.1) and the statuses
         add/extend/present/skip. docs/getting-started.md: one sentence in the section that mentions the organization upload
         (line ~48 area) linking the org guide subsection.
      5. CHANGELOG.md under "## [Unreleased]": one line in the file's existing style: "Export and import your personal settings (always-allow rules and step-up level) from Settings > Auto-accept, to move from a desktop install to an organization server or back."
      6. Delete docs/personal-settings-transfer-plan.md and docs/personal-settings-transfer-plan-manual-steps.html with git rm. grep the repo
         for both file names and for "personal-settings-transfer-plan"; nothing may link them.
      7. Run: python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_configuration_reference.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_changelog_section.py -q, then /dod.
      Stop with status=blocked if: a docs test fails because a new link or section name is not accepted and the fix would change the test.
    acceptance:
      - "ls docs/adr/0194-*.md docs/adr/0195-*.md lists two files (or the renumbered pair), and grep -c '0194\\|0195' docs/adr/README.md is at least 2"
      - "grep -rn 'personal-settings-transfer-plan' . --include='*.md' --include='*.html' --include='*.py' --include='*.yml' prints nothing"
      - "git ls-files docs/personal-settings-transfer-plan.md prints nothing"
      - "python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_configuration_reference.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_no_history.py tests/unit/test_changelog_section.py -q passes"
      - "CHANGELOG.md has a line mentioning 'personal settings' under '## [Unreleased]' and no new '## [' heading"
```
