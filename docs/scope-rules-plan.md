# Plan: nine verbs, one auto-accept rule per scope, editable verbs, personal data on tokens

## 1. Goal

Today the Auto-accept page can show the same scope twice. In the reported case, two Drive rules
on folder "2 Rolling 12M Cashflow": one "allow download", and one "allow read, download - personal
data allowed in exports, file downloads and page uploads". This happens because a rule's identity
is its predicate, value, conditions and personal-data flag, and one Settings scope compiles to up
to four predicates. The verb vocabulary has also grown to 19 overlapping verbs, Grist and Peek
show verbs with bracketed labels, and the only way to change a rule's verbs is to remove it and
add it again.

After this change:

1. **Nine verbs** (read, download, search, add, update, restructure, move, send, delete), mapped to
   tools exactly as in the reviewed verb matrix
   (https://claude.ai/artifact/6w2uDnXbvVgD1c2CSzT3QT). No verb has a bracketed label any more.
2. **One rule per scope.** `settings.yaml` stores one record per (scope, value), which compiles to
   whatever predicates the engine needs. The Settings page shows one row per scope, and adding
   verbs to a scope that already has a row extends that row.
3. **Editable verbs.** The Settings page can add or remove verbs on an existing rule, behind the
   same step-up as adding a rule.
4. **Personal data is a token property.** "Personal data allowed" moves from rules to scoped
   tokens (ADR 0164). It applies only to calls made with a token that has it ticked, so a plain
   Claude session always gets the PII card. This also removes the cause of the reported duplicate.
5. **Automatic upgrade.** On first start after the upgrade, the existing `auto_accept:` section is
   converted, existing scoped tokens are re-pointed at the new rule ids, and tokens that used a
   flagged rule get the flag. The conversion never widens what an unscoped call can auto-accept.
   Rule usage history ("Matched 11x") is kept.

Decisions taken with the maintainer while planning:

- An existing rule that covers only part of a merged verb (for example an "update" rule without the
  former "format" tools) is **not** widened on upgrade (ADR 0166 stands). The row shows the
  partial-coverage notice and the existing "Update rule" button.
- The personal-data allowance is **one checkbox per token**, covering all of the token's rules.

## 2. Current state

- **Verbs.** `src/privacyfence/policy/registry.py:35-56` defines 19 `Verb` members,
  `VERB_FAMILY` (:70-90), `VERB_SCOPE_SUBJECT` (:92-112) and `TOOL_TO_VERB` (:145-236), with 93
  governed tools. Verbs are **not stored**: a stored rule holds operation keys
  (`policy/store.py:5-22`), and `describe.rule_verbs` (`policy/describe.py:79`) derives verbs from
  them. Collapsing verbs therefore changes no stored data on its own.
- **Catalogue.** `policy/propose.py:279-392` (`PROPOSABLE_SCOPES`) declares which verbs each
  predicate governs. `policy/catalogue.py:49-77` (`EXTRA_SCOPES`) holds Apps Script, Grist, Peek,
  `gmail.configure` and `slack.share_anything`. `verb_labels` (:46, :59, :69) produces the
  bracketed Grist and Peek labels, and the JS shows them at `settings_window_html.py:928`.
  `extra_operations_for(connector, verb)` (:119) derives an extra scope's operations from the
  registry. `PERSONAL_DATA_GROUPS` is at :110.
- **Shared operation keys.** Four operation keys are reached by two verbs:
  - `calendar.create_modify_event` (create, update);
  - `slack.read_messages` (read through `approved_channel`, search through
    `approved_channel_all_results`);
  - `telegram.read_chat_messages` (the same split);
  - `jira.read_issue` (read through `approved_project_keys`, search through
    `approved_project_keys_all_results`).
- **Seeded config.** `src/privacyfence/resources/settings.yaml.example:178-195` seeds a v2
  `auto_accept:` section with one rule: `always_allow` on `contacts.edit` with the condition
  `no_contact_info_change`. No catalogue entry carries that condition.
  `daemon_main._bootstrap_config` (:336-345) copies the file on first run, and
  `docs/configuration-reference.md:125-136` documents it. `tests/unit/test_docs_configuration_reference.py`
  pins that doc against the example.
- **Pinned v2 shape in tests.**
  - `tests/unit/test_gate_real_evaluator.py:810-891` asserts predicates and `store.rule_id_for`
    ids on disk after "Always allow".
  - `tests/packaged_policy_probe.py:104-112` asserts `version == 2` and a `trusted_sender_domain`
    predicate. Only the packaged smoke tests in `build.yml` run it: `test_macos_packaged_smoke.py`,
    `test_deb_packaged_lifecycle.py` and `test_org_ubuntu_release_smoke.py`.
- **Org-mode load order.** In org mode a principal's settings load lazily, on the first request
  that needs their connectors (`daemon_main._connectors_for_principal`, :1126-1141). A scoped MCP
  request resolves its token (`web/routes_mcp.py:432-436`) before that happens. The
  `ScopedTokenStore` is built at `daemon_main.py:2201-2210` (and `web/server.py:1476`) over
  `web/mcp_auth.scoped_token_principals()` (`mcp_auth.py:208`), which yields the local principal
  and every `users/<id>/` directory.
- **One scope, several stored rules.** `propose.rules_for_scope_group` (:617) and
  `_rules_from_pairs` (:576) create one `PolicyRule` per predicate. For example, `drive.folder`
  becomes `approved_folder` (read, download), `approved_sandbox_folder` (update, format,
  restructure, comment, delete), `parent_folder_allowlist` (create) and
  `move_within_approved_folders` (move, copy). `store.merge_rules` (:127) merges only rules with
  the same (predicate, value, conditions, allow_personal_data).
- **Rule id.** `store.rule_id_for` (:93) hashes (predicate, value, conditions), plus the
  personal-data flag when it is set (ADR 0074, ADR 0157). The id is used by:
  - audit attribution: `gate.py:705`, then `AuditLogger.rule_usage()` (`audit_log.py:964`), then
    the "Matched Nx" line (`settings_controller.py:506-508`);
  - scoped tokens: `ScopedToken.rule_ids` (`scoped_tokens.py:55`), filtered at
    `gate.py:712-719` (`_rules_in_scope`);
  - Remove and Update rule on the Settings page.
- **Personal data.** `PolicyRule.allow_personal_data` (`policy/engine.py:59`).
  `gate._personal_data_rule` (`gate.py:723-736`) lets the six tools in
  `auto_accept.PERSONAL_DATA_RULE_TOOLS` (`auto_accept.py:360`) auto-accept despite a PII finding
  when a flagged rule matches. This works for any client, scoped or not. The flag is set from the
  "Add a rule" form for `catalogue.PERSONAL_DATA_GROUPS` (`catalogue.py:353`).
- **Writers of the `auto_accept:` section.**
  - `auto_accept.add_policy_v2_rules` (:555), `remove_policy_v2_rule` (:581),
    `update_policy_v2_rule` (:603) and `get_policy_v2_rules` (:533). Callers are the popup's
    "Always allow" (`gate.py:1191`, `:1323`), the bridge `propose_policy_change`
    (`gate.py:1422-1552`) and org-mode Settings (`web/routes_settings.py:1851-1882`).
  - The local Settings controller writes the section itself: `settings_controller.py:1719-1781`
    (`add_policy_rule`, `remove_policy_rule`, `update_policy_rule`), then
    `_save_and_reload_policy_v2` (:1783).
- **Readers.**
  - Settings rows: `settings_controller._rule_row` (:484) and `_auto_accept_state_from_rules`
    (:513), with org mode at `web/routes_settings.py:1512`, `:1542`.
  - Bridge `privacyfence_list_policy`: `web/mcp_dispatch.py:43-63` (`_policy_rule_row`) and
    `:376-405`.
  - Scoped token form: `web/server.py:289` (`_scoped_rule_entry`) and `:308`
    (`scoped_token_page_data`).
  - JS: `settings_window_html.py:830-950` (`renderAutoAccept`), the notice at :887-894 ("Newer
    tools aren't covered yet: …"), the add form at :908-946, `submitAddPolicyRule` at :1878-1891,
    and the token form starting at :1212 (`window.__pfScopedTokens`).
- **Load path.** `daemon_main.load_config` (:382) runs `_drop_converted_v1_sections` (:359, the
  precedent for a persisted startup rewrite) and `reject_v1_sections`. It has two callers:
  - `main` (:2374), for the local install;
  - `_load_principal_settings` (:1027), for each org principal. Org Settings reaches it through
    `web/routes_settings._ensure_principal_settings_loaded` (:1467-1475).

  The rules are compiled at `run_app` (:1936-1938) for local mode and at
  `_load_principal_settings` (:1029-1030) for each org principal. Org mode has no admin-managed rules: each principal's rules live in their own
  `settings.yaml` and go through the same store.
- **Scoped token store.** `scoped_tokens.py`: `_to_json`/`_from_json` (:113-139),
  `ScopedTokenStore._path` (:156, `paths.authority_dir(principal) / "scoped_tokens.json"`),
  `create` (:212), `REVOKED_SCOPE` (:68). The create route's step-up fingerprint is
  `web/routes_settings.py:369` (`_scoped_create_fingerprint_body`), and its wiring is
  `ScopedTokenWiring.store_create` (:360).

## 3. Design

### 3.1 The nine verbs

`registry.Verb`, in this declaration order (which is also `propose.verb_sort_key`'s order within a
family):

| Verb | value | Family | Scope subject |
|---|---|---|---|
| `READ` | `read` | read | `ITEM` |
| `DOWNLOAD` | `download` | read | `ITEM` |
| `SEARCH` | `search` | read | `EVERY_RESULT` |
| `ADD` | `add` | write | `CONTAINER` |
| `UPDATE` | `update` | write | `ITEM` |
| `RESTRUCTURE` | `restructure` | write | `ITEM` |
| `MOVE` | `move` | write | `SOURCE_AND_DESTINATION` |
| `SEND` | `send` | send | `DESTINATION` |
| `DELETE` | `delete` | destructive | `ITEM` |

`ScopeSubject` keeps only `ITEM`, `CONTAINER`, `EVERY_RESULT`, `SOURCE_AND_DESTINATION` and
`DESTINATION`, because nothing outside `registry.py` and its tests uses the others.

**`TOOL_TO_VERB` changes.** Every tool not listed here keeps its current verb.

| Old verb | Tools | New verb |
|---|---|---|
| CREATE | every tool now on `Verb.CREATE` | ADD |
| DRAFT | all six `gmail_*draft*` tools | ADD |
| FORMAT | `drive_sheets_format_range`, `drive_docs_format_content` | UPDATE |
| COMMENT | `drive_add_comment`, `jira_add_comment`, `peek_add_comment` | UPDATE |
| LABEL | `gmail_add_label`, `gmail_remove_label`, `contacts_add_label`, `contacts_remove_label` | UPDATE |
| COMPLETE | `tasks_complete_task`, `tasks_uncomplete_task` | UPDATE |
| TRANSITION | `jira_transition_issue` | UPDATE |
| SHARE | `calendar_set_event_visibility`, `peek_set_visibility` | UPDATE |
| SHARE | `slack_create_group_chat` | SEND |
| COPY | `drive_copy_file` | MOVE |
| ARCHIVE | `gmail_archive_message` | MOVE |
| CONFIGURE | `gmail_create_filter`, `gmail_update_filter` | RESTRUCTURE |

**Legacy names.** `registry.LEGACY_VERB_ALIASES: dict[str, Verb]` maps:

- `create`, `draft` → ADD
- `format`, `comment`, `label`, `complete`, `transition` → UPDATE
- `copy`, `archive` → MOVE
- `configure` → RESTRUCTURE
- `share` → SEND

`catalogue.parse_verbs` accepts a current verb name or a legacy alias, maps it, and drops
duplicates while keeping first-seen order. Anything else is still dropped silently. This keeps
`privacyfence_propose_policy_change` calls and sync recipes written with the old names working.

**`PROPOSABLE_SCOPES` verbs (`propose.py`).** Predicates, ids, groups and value builders do not
change.

| Predicate (scope type) | New verbs | `excludes` |
|---|---|---|
| `approved_folder` (drive.folder) | READ, DOWNLOAD | — |
| `approved_sandbox_folder` (drive.folder) | UPDATE, RESTRUCTURE, DELETE | — |
| `parent_folder_allowlist` (drive.folder) | ADD | — |
| `move_within_approved_folders` (drive.folder) | MOVE | — |
| `i_am_owner` | READ, DOWNLOAD | — |
| `i_am_sender`, `trusted_sender_domain` | READ, DOWNLOAD, MOVE | — |
| `label_name_allowlist` (gmail.label) | UPDATE | — |
| `always_allow` (gmail.anything) | ADD | `gmail.create_label` (new) |
| `personal_calendar` | READ, ADD, UPDATE, DELETE | unchanged (`calendar.out_of_office`, `calendar.working_location`) |
| `i_am_organizer` | READ | — |
| `approved_channel` / `approved_channel_all_results` / `approved_recipient` | READ / SEARCH / SEND | `approved_recipient` gets `slack.create_group_chat` (new) |
| `dm_with_myself`, `group_dm` | READ | — |
| `approved_chats` / `approved_chats_all_results` | READ, SEND / SEARCH | — |
| `approved_project_keys` | READ, ADD, UPDATE | — |
| `approved_project_keys_all_results` | SEARCH | — |
| `i_am_reporter`, `i_am_assignee` | READ | — |
| `approved_space_keys` | READ, DOWNLOAD, ADD, UPDATE | — |
| `i_am_author` | READ, DOWNLOAD | — |
| `approved_object_types`, `approved_report_ids` | unchanged | unchanged |
| `approved_task_list` | ADD, UPDATE, MOVE | — |
| `label_name_allowlist` (contacts.label) | UPDATE | `contacts.edit` (new) |
| the two calendar condition scopes | READ | — |

The new `excludes` stop a merged verb from reaching more tools:

- Gmail's unconditional drafting scope would otherwise also cover creating labels.
- A Slack channel's send rule would otherwise also cover creating group chats. `approved_recipient`
  could never match one, so the rule would only be mislabelled.
- A contacts label rule would otherwise claim `contacts_update`, which has no `label_name`.

**`EXTRA_SCOPES` (`catalogue.py`).** `PolicyExtraScope` loses `verb_labels` and gains
`excludes: frozenset[str] = frozenset()`.
`extra_operations_for(extra: PolicyExtraScope, verb: Verb) -> frozenset[str]` now takes the extra
scope instead of a connector, and removes `extra.excludes` from its result. Update its two callers:
`catalogue._compile_catalogue_rules` and `outdated.missing_operations`.

| id | predicate | verbs | label | excludes |
|---|---|---|---|---|
| `apps_script.project` | `apps_script.project` | READ, UPDATE | unchanged | — |
| `grist.document` | `grist.document` | READ, ADD, UPDATE, RESTRUCTURE | unchanged | — |
| `peek.page` | `peek.page` | READ, ADD | unchanged | — |
| `gmail.filters` (was `gmail.configure`) | `gmail.anything` | RESTRUCTURE | `Gmail — filters (unconditional)` | — |
| `slack.group_chats` (was `slack.share_anything`) | `slack.anything` | SEND | `Slack — group chats (unconditional)` | `slack.send_message` |

`catalogue.GROUP_ALIASES = {"gmail.configure": "gmail.filters", "slack.share_anything":
"slack.group_chats"}`. `rules_for_catalogue_entry` resolves an alias before anything else, so old
bridge calls keep working. `scope_catalogue()` no longer emits `verb_labels`, and the JS at
`settings_window_html.py:928` shows the bare verb.

Note: `gmail.anything` with RESTRUCTURE reaches exactly the two filter operations, because Gmail
has no other RESTRUCTURE tool. Leave the comment at `scopes.py:570-582` in place, but rewrite its
verb names: `configure`/`share` become `restructure`/`send`, and `draft` becomes `add`.

### 3.2 Personal data moves from rules to scoped tokens

- `ScopedToken` gains `allow_personal_data: bool = False`.
  - `_to_json` writes `"allow_personal_data": true` only when it is true.
  - `_from_json` reads it with `raw.get("allow_personal_data", False) is True`, so older files
    load as false.
  - `ScopedTokenStore.create(...)` gains a keyword `allow_personal_data: bool = False`.
  - `REVOKED_SCOPE` keeps the default.
- `gate._personal_data_rule`:
  - returns `None` unless `current_token_scope()` is a `ScopedToken` other than `REVOKED_SCOPE`
    whose `allow_personal_data` is true;
  - otherwise uses `_rules_in_scope(scope)` (no `rule.allow_personal_data` filter) with
    `find_matching_rule`;
  - keeps the existing tool and `content_reaches_result` conditions.
- `PolicyRule.allow_personal_data` is removed.
  - `store.rule_id_for` loses its `allow_personal_data` parameter.
  - `store.merge_rules`'s key, `rule_to_dict`, `rule_from_dict` and `describe.rule_sentence` drop
    it too.
  - `rule_from_dict` ignores an `allow_personal_data` key instead of validating it. The upgrade
    (3.4) reads it from the raw dict before anything compiles.
- `catalogue.PERSONAL_DATA_GROUPS` is removed, along with `rules_for_catalogue_entry`'s
  `personal_data` parameter and the `personal_data_label` key in `scope_catalogue()`.
  - `SettingsController.add_policy_rule` and the org route `add_policy_rule` lose `personal_data`.
  - The JS rule form loses its checkbox (`settings_window_html.py:937-941`) and its
    `personal_data` post field (:1887).
- The token form (both modes) gets one checkbox under the rule and tool pickers.
  - Label: **`Let exports, downloads and page uploads carry personal data`**.
  - Help text below it: **`Only for calls made with this token, and only for Drive downloads
    delivered as a file, Grist, Salesforce and Jira exports, and Peek page uploads. Other calls
    still stop for review when personal data is found.`**
  - The create request body gains `allow_personal_data` (bool, default false). It is part of
    `_scoped_create_fingerprint_body`, as `bool(payload.get("allow_personal_data", False))`, and a
    non-bool value makes the body malformed (`None`).
  - `ScopedTokenWiring.store_create` gains a fifth positional `bool` argument.
  - Each token in the token list shows the badge **`personal data allowed`** when the flag is on.
    `scoped_token_page_data`'s token dict gains `"allow_personal_data"`.
- Token creation is already a sensitive, step-up-gated action in both modes (ADR 0164, ADR 0165),
  so nothing new is classified.

### 3.3 One rule per scope: schema version 3

New module `src/privacyfence/policy/scope_rows.py`. It is pure: it does not read or write files.

```python
@dataclass(frozen=True)
class ScopeRow:
    id: str
    scope: str | None              # a scope_catalogue() id (a propose widening group or an EXTRA_SCOPES id); None = custom
    value: Any                     # list[str] or None
    verbs: frozenset[Verb]
    operations: frozenset[str]
    predicate: str = ""            # custom rows only
    conditions: tuple[tuple[str, Any], ...] = ()   # custom rows only
    former_ids: tuple[str, ...] = ()               # ids of the v2 rules merged into this row, sorted
```

On disk (`settings.yaml`):

```yaml
auto_accept:
  version: 3
  rules:
    - id: r-1a2b3c4d5e
      scope: drive.folder
      value: ["1CdeF..."]
      verbs: [download, read]
      operations: [drive.download_file, drive.read_file]
      former_ids: [r-0f9e8d7c6b, r-aa11bb22cc]   # omitted when empty
    - id: r-77de0a1b2c                           # a custom row: anything the upgrade could not map
      predicate: some_predicate
      value: [...]
      verbs: [read]
      operations: [x.read]
      conditions: [[some_condition, null]]
```

Functions, all in `scope_rows.py`:

- `SCHEMA_VERSION = 3`.
- `row_id_for(scope: str, value: Any) -> str`:
  `"r-" + sha256(repr(("scope", scope, store._sortable(value))).encode()).hexdigest()[:10]`.
  Adding or removing verbs never changes it.
- `group_entries(scope) -> tuple[ProposableScope, ...] | None` and
  `extra_scope(scope) -> PolicyExtraScope | None`: the catalogue lookups. `scope_verbs(scope) ->
  tuple[Verb, ...]` lists the verbs the scope offers, in `verb_sort_key` order. It is empty for an
  unknown scope.
- `implied_operations(scope, verbs) -> frozenset[str]`: every operation the scope's catalogue
  entries derive for those verbs. That is `propose.operations_for(entry, v)` over the group's
  entries, or `catalogue.extra_operations_for(extra, v)`.
- `compile_row(row) -> list[PolicyRule]`:
  - A custom row compiles to one rule:
    `[PolicyRule(id=row.id, predicate=row.predicate, value=row.value, operations=row.operations,
    conditions=row.conditions)]`.
  - A propose group compiles to one rule per entry `e` in the group. Its operations are
    `row.operations ∩ ⋃_{v ∈ row.verbs ∩ e.verbs} operations_for(e, v)`. Its other fields are
    `id=row.id`, `predicate=e.predicate`, `value=row.value`, and
    `conditions=(e.condition,) if e.condition else ()`. An entry whose operations come out empty
    is skipped.
  - An extra scope compiles to one rule with `predicate=extra.predicate` and operations
    `row.operations ∩ ⋃ extra_operations_for(extra, v)`.
  - Storing `verbs` is what keeps a shared operation key honest: `slack.read_messages` is reached
    by both `approved_channel` (read) and `approved_channel_all_results` (search), and only the
    stored verbs say which.
- `compile_rows(rows) -> list[PolicyRule]` concatenates `compile_row` in row order.
- `row_to_dict(row)` / `row_from_dict(data) -> ScopeRow | None`:
  - Lists are sorted.
  - Verbs are stored by their values, read back with `Verb(v)` and legacy aliases, and unknown
    verbs are dropped.
  - Fails closed, returning `None`, for:
    - a missing id;
    - empty operations;
    - no `scope` and no `predicate`;
    - a `scope` not in the catalogue;
    - a **scoped** row whose verbs are empty after parsing.
  - A **custom** row may have `verbs: []`. Its verbs are display only and never decide what it
    compiles to. The seeded `always_allow`/`contacts.edit` rule is such a row: no catalogue entry
    credits `always_allow` with UPDATE.
- **Custom row verbs.** A custom row's verbs are always
  `frozenset(v for op in row.operations for v in registry.operation_verbs(op))`. This is not
  `describe.rule_verbs`, which filters by the predicate's catalogue verbs and can come out empty.
- `rows_from_section(section) -> list[ScopeRow]` and `rows_to_config(rows) -> dict`, with
  `{"version": 3, "rules": [...]}`.
- `rows_from_config(cfg) -> list[ScopeRow]`: reads the `auto_accept:` section, and is read-only.
  - Version 3: `rows_from_section`.
  - Version 2, or no version with a `rules` list: `migrate_v2(section["rules"]).rows`. This keeps
    a v2 config readable for display and compile.
  - Anything else: `[]`.

  Writers never call it on a v2 section without first running the token-aware upgrade (3.4,
  `policy_upgrade.ensure_scope_rows`).
- `scope_of_rule(rule: PolicyRule) -> tuple[str, Any] | None`: the scope a v2 rule belongs to.
  1. If the rule has exactly one condition, find the single `PROPOSABLE_SCOPES` entry whose
     predicate equals the rule's predicate and whose `condition` equals that condition. Its
     `widening_group` is the candidate scope.
  2. If the rule has no conditions, collect the candidates whose predicate equals the rule's
     predicate and whose connector is in `{propose.connector_of_operation(op) for op in
     rule.operations}`. Candidates are the `PROPOSABLE_SCOPES` entries with `condition is None`
     (contributing their `widening_group`) and the `EXTRA_SCOPES` entries (contributing their id).
     There must be exactly one candidate scope.
  3. In both cases, the rule maps only if every one of its operations is in
     `implied_operations(scope, scope_verbs(scope))`. Anything else returns `None`, which means a
     custom row.

  The scope's value is `rule.value`.
- `add_rules(rows, rules: list[PolicyRule]) -> tuple[list[ScopeRow], list[str]]`. This is the one
  merge function every writer uses. For each rule:
  - **Mapped** to `(scope, value)`: find the row with the same `scope` and the same
    `store._sortable(value)`. Union `describe.rule_verbs(rule)` into its verbs and
    `rule.operations` into its operations. If there is no such row, append
    `ScopeRow(id=row_id_for(scope, value), ...)`.
  - **Unmapped**: a custom row with `id=store.rule_id_for(rule.predicate, rule.value,
    rule.conditions)`. If a custom row with that id already exists, union its operations, then
    recompute its verbs as above.

  Returns the new row list and the ids of the rows touched, deduplicated and in order.
- `remove_row(rows, row_id) -> list[ScopeRow]`.
- `missing_operations(row) -> frozenset[str]`: what the row's verbs imply but its operations
  lack.
  - A scoped row returns `implied_operations(row.scope, row.verbs) - row.operations`.
  - A custom row returns `outdated.missing_operations(compile_row(row)[0])`.
- `missing_tools(row) -> tuple[str, ...]`: the tools of `missing_operations(row)` whose verb is in
  `row.verbs`, sorted.
- `fill_missing(rows, row_id) -> list[ScopeRow]`: adds `missing_operations` to one row (the
  "Update rule" button).
- `set_verbs(rows, row_id, verbs: Iterable[Verb]) -> list[ScopeRow] | None`: the verb edit (3.5).
- `pairs_of(rows, row_id) -> frozenset[tuple[str, str]]`: the `(predicate, operation)` pairs that
  `compile_row` gives for that row today. Empty for an unknown id. Token creation and the upgrade
  use it for `rule_limits` (3.4).
- `migrate_v2(raw_rules: Any) -> V2Migration`, described in 3.4.

`store.compile_rules_from_config(cfg)` dispatches on version. For a version-3 section it returns
`scope_rows.compile_rows(scope_rows.rows_from_section(section))`, imported inside the function
with a one-line comment: `scope_rows` imports `catalogue`, which imports `store`. The v2 path is
unchanged.

The engine (`policy/engine.py`) does not change. Several compiled rules share one row id, which
ADR 0074 already allows: `find_matching_rule` returns the object, and attribution uses its `.id`.

**Downgrade.** An older daemon's `rule_from_dict` compiles the custom rows (they carry predicate,
operations and conditions) and drops the scoped rows, because they have no predicate. Any rule
write from an older daemon then rewrites the section as v2 and loses the scoped rows. The backup
file (3.4) is the way back. ADR 0172 and the CHANGELOG say so.

### 3.4 The upgrade

#### Pure part: `scope_rows.migrate_v2(raw_rules) -> V2Migration`

`V2Migration` is a frozen dataclass with these fields:

- `rows: list[ScopeRow]`
- `id_map: dict[str, str]`: old v2 id → new row id, for every v2 rule that went into a scoped row
- `members: dict[str, frozenset[str]]`: row id → the old ids merged into it
- `member_pairs: dict[str, frozenset[tuple[str, str]]]`: old id → its `(predicate, operation)`
  pairs
- `personal_data_ids: frozenset[str]`: old ids whose raw dict had `allow_personal_data: true`

Algorithm:

1. **Parse.** For each raw entry that is a dict, record its `id` when `allow_personal_data is True`,
   then parse it with `store.rule_from_dict`. Drop entries that fail to parse, as today.
2. **Group.** Map each rule with `scope_of_rule` and group mapped rules by
   `(scope, _sortable(value))`.
3. **Build a candidate row per group.**
   - `verbs` is the union of `describe.rule_verbs(r)`.
   - `operations` is the union of `r.operations`.
   - `id` is `row_id_for(scope, value)`.
   - `former_ids` is the sorted old ids.
4. **Check the candidate is lossless.** For each predicate, the operations and conditions of
   `compile_row(candidate)` must equal the union of the group's rules for that predicate. A
   predicate with no operations counts as absent.
   - If the check fails, none of that group's rules merge. Each becomes its own custom row,
     keeping its old id unchanged, with `former_ids=()`.
   - The check guarantees the upgrade changes no unscoped decision except dropping the
     personal-data flag.
5. **Unmapped rules** become custom rows with their old id unchanged.
6. **Order.** Rows come out in the order their first member appeared.

Tests must check this guarantee **after a round trip through the written form**:
`migrate_v2` → `rows_to_config` → `rows_from_section` → `compile_rows`, compared with the v2
compile.

#### Token part: `ScopedToken.rule_limits`

```python
rule_limits: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict, hash=False)
```

A limit lists, for one rule id the token carries, the `(predicate, operation)` pairs that may
auto-accept under it for this token.

- In `gate._rules_in_scope`, a rule whose id is in `scope.rule_limits` is replaced by
  `dataclasses.replace(rule, operations=frozenset(op for op in rule.operations if (rule.predicate,
  op) in limits))`, and dropped if that leaves it empty. A rule id with no limit is used as is:
  that only happens for a token written before this change and never upgraded.
- **Every token gets a limit for every rule it carries, so a token can only narrow.** A row keeps
  its id when verbs are added (`add_rules`, `set_verbs`, `fill_missing`). Without a limit, a
  recipe's token would silently gain whatever the person later adds to that row, which ADR 0164
  rules out.
- **At creation.** `ScopedTokenStore.create` sets
  `rule_limits = {rid: scope_rows.pairs_of(rows, rid) for rid in rule_ids}`, with rows from
  `auto_accept.get_policy_rows()`. This covers both modes, because both create through the store.
- `_to_json` writes `"rule_limits": {row_id: [[predicate, operation], ...]}`, sorted, when the
  dict is not empty. `_from_json` reads it with `.get("rule_limits", {})`, and a malformed value
  makes the entry malformed (skipped, fail closed).
- **Display.** The token page shows `(limited to: <tools>)` after a rule only when the limit is
  narrower than `pairs_of` for that row today (3.6).

`scoped_tokens.migrate_token_file(path: Path, migration: V2Migration) -> list[str]` changes only
what needs changing, and returns the labels of tokens whose `allow_personal_data` it newly set. It
is idempotent. For each token:

1. If `token.rule_ids ∩ migration.personal_data_ids` is not empty, set `allow_personal_data` to
   true.
2. For each row id `R` with `members[R] ∩ token.rule_ids` non-empty:
   - **replace** those old ids in `rule_ids` with `R`, so they don't show as "a rule that no
     longer exists";
   - set `rule_limits[R] = ⋃ member_pairs[m] for m in members[R] ∩ token.rule_ids`.
3. For each rule id left in `rule_ids` that is a custom row id kept unchanged and has no limit,
   set its limit to its own `member_pairs`.
4. A second run finds no old ids, so it changes nothing.
5. A token file that is missing or unreadable is left alone, and the function returns `[]`.

The write happens under the store's lock when a store is live:

- `ScopedTokenStore.apply_migration(principal, migration) -> list[str]` holds `self._lock`,
  refreshes, applies the same transformation in memory, and writes with `_write`.
- `scoped_tokens.migrate_tokens(principal, migration) -> list[str]` calls
  `apply_migration` on the store `get_store()` would return when one is set. Otherwise it calls
  `migrate_token_file(paths.authority_dir(principal) / STORE_FILE_NAME, migration)`.
- Add `scoped_tokens.current_store() -> ScopedTokenStore | None` (the set store, or `None`) for
  this check, without changing `get_store()`.

#### Non-pure part: `src/privacyfence/policy_upgrade.py`

`ensure_scope_rows(cfg: dict, config_path: str, principal: Principal) -> bool` is the one
token-aware upgrade. It returns `True` when `cfg`'s section is version 3 on return, and `False`
when it had to stay v2.

1. Do nothing (return `True`) unless the `auto_accept:` section is a dict whose `version` is 2, or
   has no version and has a `rules` list.
2. Run `migration = scope_rows.migrate_v2(section["rules"])`.
3. Copy `config_path` to `f"{config_path}.pre-scope-rules.bak"` with `shutil.copy2`, unless that
   file already exists or `config_path` does not exist.
4. Run `labels = scoped_tokens.migrate_tokens(principal, migration)`. On `OSError`:
   - log the warning `f"{principal.id}: could not update scoped tokens for the new rule format:
     {exc}"`;
   - leave `cfg` untouched, so it stays v2 and keeps working;
   - return `False`.
5. Set `cfg["auto_accept"] = scope_rows.rows_to_config(migration.rows)` and write it with
   `secure_files.atomic_write_text(config_path, yaml.safe_dump(cfg, default_flow_style=False,
   allow_unicode=True))`. On `OSError`, log a warning and still return `True`: the in-memory config
   is v3, the next start converts again from v2, and the token step is idempotent.
6. On success, log `f"{config_path}: auto-accept rules converted to one rule per scope (version
   3); the previous file is kept at {config_path}.pre-scope-rules.bak"`.
7. If `labels` is not empty, also log `f"Scoped tokens now allowed to carry personal data,
   because they used a rule that allowed it: {', '.join(labels)}"`.

**Logging.** `ensure_scope_rows` takes `log: Callable[[str], None]`, which defaults to
`logger.warning`. `daemon_main` passes `_deferred_warnings.append` when it runs before logging is
set up.

**Callers:**

- `daemon_main.load_config(config_path, *, principal: Principal | None = None)`. Right after
  `_drop_converted_v1_sections`, when `principal` is not `None`, it calls
  `policy_upgrade.ensure_scope_rows(config, resolved, principal,
  log=_deferred_warnings.append)`. The two existing callers pass:
  - `main` (:2374): `principal=LOCAL_PRINCIPAL`;
  - `_load_principal_settings` (:1027): `principal=current_principal()`.
- **Org-mode startup.** Before the line that constructs `ScopedTokenStore` at
  `daemon_main.py:2210`, call the new `daemon_main._upgrade_all_principals()`. For each principal
  in `web.mcp_auth.scoped_token_principals()`, under `principal_scope(p)`, it:
  1. resolves `_resolve_authority_path("config/settings.yaml")`, and skips the principal if that
     file does not exist;
  2. loads it with `yaml.safe_load`;
  3. calls `ensure_scope_rows`.

  This way no scoped request is served with old token ids (`routes_mcp.py:432-436` resolves the
  token before the lazy per-principal load). The local principal is covered too; for it this is a
  no-op after `main`'s load.
- **Every writer** (3.7) calls `ensure_scope_rows` on the config it re-read, before mutating.
  If it returns `False`, the writer writes nothing and reports no change. `SettingsController`
  methods then set `self.error = "Auto-accept rules could not be upgraded to the new format;
  restart PrivacyFence and try again."`, so a write can never convert v2 without re-pointing
  tokens.
- **Import cycle.** `policy_upgrade` imports `scoped_tokens`, which imports `auto_accept`. So
  `auto_accept` imports `policy_upgrade` inside the writer functions, as it already does for
  `policy` imports (`auto_accept.py:540-544`).

**The seeded config ships as version 3.** `src/privacyfence/resources/settings.yaml.example` gets
`version: 3` and its one rule as a custom row. Generate the exact row with
`scope_rows.row_to_dict` from the current v2 rule, so its id stays `r-d76028ec0b`; its verbs are
`[update]`. The comment above it is rewritten to say:

- rules are stored one per scope;
- a scoped row's id comes from (scope, value);
- a custom row keeps the id it was written with.

`docs/configuration-reference.md:125-136` is updated in the same phase: the keys table for
version 3 (`id`, `scope`, `predicate`, `value`, `verbs`, `operations`, `conditions`, `former_ids`).
This keeps `tests/unit/test_docs_configuration_reference.py` passing.

### 3.5 Editing a rule's verbs

`scope_rows.set_verbs(rows, row_id, verbs)` returns the new row list, or `None` to refuse. It
refuses when:

- the row is unknown;
- the row is custom;
- `verbs` is empty;
- any verb is outside `scope_verbs(row.scope)`.

Otherwise:

- `kept = row.verbs ∩ verbs` and `added = verbs - row.verbs`.
- `operations = {op ∈ row.operations : op ∈ implied_operations(scope, kept)} ∪
  implied_operations(scope, added)`.

So:

- a verb that was already there keeps exactly the operations it had (no silent completion; that
  is still the "Update rule" button);
- a new verb gets all of its tools;
- a removed verb's operations go, unless a kept verb also implies them
  (`calendar.create_modify_event` is implied by both add and update).

The id and `former_ids` are kept. An unchanged result is still returned, and the caller treats
equal rows as a no-op. A token that carries the row is unaffected by an added verb (its
`rule_limits`), and loses what is removed.

Callers:

- `auto_accept.set_policy_rule_verbs(rule_id: str, verbs: list[Verb]) -> bool`: the shared writer.
- `SettingsController.set_policy_rule_verbs(self, rule_id: str, verbs: list) -> dict`: parses with
  `_parse_verbs`, writes, reloads, returns `snapshot()`.
- An org-mode action of the same name in `web/routes_settings.py._apply_org_action`. It audits
  `f"Changed auto-accept rule {rule_id!r} to allow {verbs!r}"`.
- Action classification, the same as `update_policy_rule` everywhere:
  - `_SENSITIVE_ACTIONS`;
  - `ACTION_SCOPES` in `web/org_settings_scope.py`, with LOCAL+ORG and the same scope as
    `add_policy_rule`;
  - `_ORG_ALLOWED_ACTIONS`;
  - the parametrize list of `test_a_rule_write_is_per_principal_and_routed_in_both_modes`
    (`tests/unit/web/test_org_settings_scope.py:139`).

UI, in each scoped row card of `renderAutoAccept`:

- An **`Edit verbs`** link (`data-aa-edit="<row id>"`) sits next to "✕ Remove".
- Opening it shows the scope's verb chips as toggles (`data-aa-edit-verb="<verb>"`), pre-selected
  with the row's verbs, then **`Save verbs`** (`data-aa-edit-save`) and **`Cancel`**
  (`data-aa-edit-cancel`).
- Save posts `set_policy_rule_verbs {rule_id, verbs}` with `data-confirm` text **`Change this rule
  to allow <verbs, comma-separated>? Added verbs cover all of their tools. Tools that only removed
  verbs cover stop being auto-accepted.`**
- Save is disabled while no verb is selected.
- On a row whose `value_ids` has fewer than two ids, the `move` toggle is disabled, with the same
  hint `aaPairHint` shows on the add form: a move needs both folders or lists.
- Custom rows (`scope` null) show no Edit link.

### 3.6 Settings rows, bridge listing and token form read rows

- `settings_controller._rule_row(row: ScopeRow, usage: dict, *, resolved_value: str)` returns the
  keys it has today, plus:
  - `"scope"`: the row's scope, or `null`;
  - `"scope_verbs"`: `[v.value for v in scope_verbs(row.scope)]`, or `[]`.

  Field changes:
  - `verbs` comes from `row.verbs` (sorted with `verb_sort_key`, each with its family).
  - `covered_tools` is `⋃ describe.covered_tools(r) for r in compile_row(row)`, sorted.
  - `missing_tools` is `scope_rows.missing_tools(row)`.
  - `scope_type` is `describe.scope_type_of(compile_row(row)[0])`, or `""` when the row compiles
    to nothing.
  - Usage sums `count` over `row.id` and every `former_ids` entry, and takes the latest
    `last_matched` of them.

  `_auto_accept_state_from_rules` becomes `_auto_accept_state_from_rows(rows, usage, *,
  resolve_value: Callable[[ScopeRow], str])`. `cached_rule_value` is applied to
  `compile_row(row)[0]`, and a row that compiles to nothing shows its raw value.
- `describe.row_sentence(row: ScopeRow, value_display: str | None = None) -> str`:
  - A custom row returns `rule_sentence(compile_row(row)[0], value_display)`.
  - A scoped row returns `f"{connector_label} - {noun} {value}: allow {verbs}"`, or
    `f"{connector_label} - {noun}: allow {verbs}"` when it has no value.
    - `noun` is `scope_type_label(scope_type)`, where the scope type is that of the group's first
      entry (propose groups) or of the extra's selector
      (`scopes.NEW_SCOPE_SELECTORS[extra.predicate].scope_type`).
    - The verbs are comma-separated in `verb_sort_key` order.
  - A condition scope appends `" - when " + condition name with underscores as spaces`, as
    `rule_sentence` does.
  - The personal-data suffix no longer exists.
- The partial-coverage notice text (`settings_window_html.py:889`) becomes **`These verbs also
  cover tools this rule doesn't allow yet: `** followed by the tool list. The "Update rule" button
  and its confirm text are unchanged.
- `web/mcp_dispatch.py._policy_rule_row` takes a `ScopeRow` and returns:

  ```
  {id, sentence, connector, scope, scope_type, value, operations, verbs: [{verb, family}],
   conditions, covered_tools}
  ```

  `conditions` is the custom row's conditions, or `[]`. `privacyfence_list_policy` lists
  `auto_accept.get_policy_rows()`.
- `web/server.py._scoped_rule_entry(row, value_display)` builds one entry per row, with the same
  keys as today. In `scoped_token_page_data`, a token rule whose `rule_limits` entry is narrower
  than `scope_rows.pairs_of(rows, rid)` gets `"limited_to": [tools]`: the tools whose operation and
  predicate pair is in the limit. The JS token list then renders `<sentence> (limited to:
  <tools>)`.
- The org create-token audit text (`web/routes_settings.py:1723-1727`) appends `", personal data
  allowed"` when the flag is set.

### 3.7 Writers

All writers go through `scope_rows`, and first call `policy_upgrade.ensure_scope_rows` (3.4) on the
config they re-read.

- **`auto_accept.py`.**
  - `get_policy_rows() -> list[ScopeRow]` reads disk via `rows_from_config`.
    `get_policy_v2_rules()` returns `compile_rows(get_policy_rows())`.
  - `add_policy_v2_rules(rules)` keeps its name and `bool` return. It now applies
    `scope_rows.add_rules`.
  - New `policy_row_ids_for(rules) -> list[str]`: the ids `add_rules` reports, computed against
    the current rows without writing.
  - `remove_policy_v2_rule(rule_id)` applies `remove_row`, and `update_policy_v2_rule(rule_id)`
    applies `fill_missing`.
  - `set_policy_rule_verbs` (3.5).
  - Each one re-reads under `write_lock`, calls `ensure_scope_rows(cfg, path,
    current_principal())`, writes `rows_to_config`, then calls
    `set_policy_v2_store_rules(compile_rows(rows))` and `notify_rules_changed`, as today.
  - `scope_rows` and `policy_upgrade` are imported inside the functions (see the import cycle note
    in 3.4).
- **`settings_controller.py`.** `add_policy_rule`, `remove_policy_rule`, `update_policy_rule` and
  the new `set_policy_rule_verbs`:
  1. call `ensure_scope_rows` (with the controller's config path and `current_principal()`);
  2. read `scope_rows.rows_from_config(cfg)` and apply the same `scope_rows` function;
  3. set `cfg["auto_accept"] = rows_to_config(rows)`;
  4. call `_save_and_reload_policy_v2(cfg)`, which compiles through
     `store.compile_rules_from_config`.
- **`gate.propose_policy_change`.** The confirmation text uses `describe.row_sentence` of the row
  being removed, or of the row the addition lands in after `add_rules`. `affected_ids` is
  `auto_accept.policy_row_ids_for(new_rules)`, computed before writing. Removal still looks the id
  up among the rows.
- **The popup's "Always allow"** (`gate.py:1191`, `:1323`) is unchanged: it still calls
  `add_policy_v2_rules(rules_for_proposal(chosen))`.

## 4. ADRs

Written in the last phase. Each one is `Accepted`, `Implemented`, and dated the day of the phase.

- **ADR 0171: Auto-accept rules use nine verbs.** It records 3.1 and the legacy aliases, and
  amends ADR 0170: copy is folded into move, still governed by `move_within_approved_folders`, and
  a move rule now shows copy as a missing tool until updated. Rejected alternatives: keeping
  19 verbs with grouped chips; separate verbs for share/send and copy/move.
- **ADR 0172: One stored auto-accept rule per scope (settings schema version 3).** It records 3.3
  and 3.4: row id, stored verbs, compile-to-predicates, the lossless check, custom rows,
  `former_ids`, the backup file, the token-aware upgrade (`policy_upgrade.ensure_scope_rows`, run
  at load, at org startup and before every write), token re-pointing, and `rule_limits` set at
  token creation so a token never gains what is later added to its row. It amends ADR 0074: a
  scoped row's id is derived from (scope, value). Rejected alternatives:
  - grouping v2 rules only in the UI, which leaves several ids per row and makes Remove, tokens
    and usage ambiguous;
  - storing verbs only, which would widen rules whenever a release adds a tool, contrary to
    ADR 0166;
  - letting a token follow its row's growth, contrary to ADR 0164;
  - widening tokens to whole rows on upgrade.

  Consequence, on a downgrade:
  - an older daemon compiles the custom rows and drops the scoped rows;
  - any rule write from it rewrites the section as v2 and loses the scoped rows;
  - `settings.yaml.pre-scope-rules.bak` restores the old section by hand.
- **ADR 0173: Personal data is allowed per scoped token, not per rule.** It records 3.2 and the
  upgrade's flag transfer, and amends ADRs 0157 and 0164. Rejected alternatives:
  - a per-rule flag, which applied to any client, including interactive ones;
  - a per-rule flag inside a token, which means more UI for a distinction the maintainer did not
    want.

  Consequences:
  - An unscoped session that used to auto-accept a flagged export now gets the card.
  - A token that carried one flagged rule now allows personal data on all of its rules' exports,
    downloads and uploads. The upgrade logs the labels of the tokens this happened to.
- **ADR 0174: A rule's verbs are edited in place.** It records 3.5 and amends ADR 0166: an explicit
  verb edit adds a new verb's full tool set, and editing never silently completes a verb that was
  already present. Rejected alternative: recomputing all operations on every edit, which would
  widen kept verbs the user did not touch.

## 5. Manual steps

There are no `manual_before` steps. One `manual_after` step: upgrade a real install with real
rules and tokens and check the result. CI cannot see a real `settings.yaml` or a sync recipe.
Step-by-step page: https://claude.ai/artifact/Rgcogc68rCaw6QfyCowQWy (source:
`docs/scope-rules-plan-manual-steps.html`).

## 6. Risks and open questions

- **Line numbers drift.** Every `path:line` above is from `406f1f5a`. A worker that finds a
  symbol missing at its named place should find it by name. If the symbol is gone or its role
  has changed, stop with `status=blocked`.
- **The registry join test.** `tests/unit/policy/test_registry.py` asserts the registry is an
  exact join with `auto_accept.TOOL_TO_GATE` / `TOOL_TO_OPERATION`. If p1 makes that test fail
  for anything other than the verb names, the matrix in 3.1 disagrees with the code: stop.
- **The lossless check (3.4) rejecting common rules.** If, in p4's tests, a rule set produced by
  today's `rules_for_scope_group` for any catalogue group does not migrate into a single row,
  `compile_row` or `scope_of_rule` disagrees with `propose`. Fix the function, never the check;
  if it cannot be fixed, stop with `status=blocked`.
- **Settings page tests are many and brittle.** Tests that pin exact sentences, verb lists or the
  notice text must be updated to the new strings in this plan, never loosened.
- **Coverage floor.** `scripts/check_coverage_floor.py` is ratcheted. Every new function needs
  direct tests.
- **Packaged smoke tests run only in `build.yml`.** `tests/packaged_policy_probe.py` is exercised
  by `test_macos_packaged_smoke.py`, `test_deb_packaged_lifecycle.py` and
  `test_org_ubuntu_release_smoke.py`, which unit runs never execute. The feature PR must dispatch
  `build.yml` (it is in `ci.dispatchable`) on its head and link a green run (`final_checks`).

## Implementation manifest

```yaml
plan_slug: scope-rules
feature_branch: feature/scope-rules
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/Rgcogc68rCaw6QfyCowQWy
manual_steps_source: docs/scope-rules-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-upgrade-real-install
    title: Upgrade a real install and check its Auto-accept page, tokens and a sync run
    why: CI has no real settings.yaml, real scoped tokens or real recipe; this confirms the version-3 conversion, the single row per scope, the token flag transfer and an unattended export with personal data on the user's own data
verify_after_merge:
  - python3 -m pytest tests/unit/policy -q
  - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_gate.py tests/unit/test_auto_accept.py tests/unit/test_daemon_main.py tests/unit/test_settings_controller.py tests/unit/test_settings_window_html.py tests/unit/web -q
final_checks:
  - docs/scope-rules-plan.md and docs/scope-rules-plan-manual-steps.html are deleted and nothing links to them
  - ADRs 0171, 0172, 0173 and 0174 exist, are Accepted, and are listed in docs/adr/README.md; ADRs 0074, 0157, 0164, 0166 and 0170 carry a one-line Status pointer to the ADR that amends them
  - CHANGELOG.md has the four entries under "## [Unreleased]" and no new version heading
  - build.yml was dispatched on the feature branch head (it runs tests/packaged_policy_probe.py through the packaged smoke tests) and the green run is linked in the PR
phases:
  - id: p1-nine-verbs
    title: Collapse the 19 verbs to nine and drop bracketed verb labels
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/policy/registry.py
      - src/privacyfence/policy/propose.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/outdated.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/describe.py
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/web/mcp_tools.py
      - docs/always-allow-rules-reference.md
      - tests/unit/policy/**
      - tests/unit/test_settings_controller.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_gate.py
      - tests/unit/test_auto_accept.py
      - tests/unit/web/**
      - tests/packaged_policy_probe.py
      - tests/unit/test_generate_always_allow_reference.py
      - tests/integration/test_settings_update_rule_browser.py
    brief: |
      Read docs/scope-rules-plan.md sections 1, 2 and 3.1 first. Section 3.1 is the spec; every
      verb, tool mapping, exclude, id and label you need is there.
      1. src/privacyfence/policy/registry.py:
         - Replace `Verb` with the nine members in 3.1's order, and fix the docstring ("The nine
           verbs ...").
         - Rewrite VERB_FAMILY and VERB_SCOPE_SUBJECT per the 3.1 table, and trim ScopeSubject
           to the five members 3.1 names.
         - Apply the TOOL_TO_VERB changes table exactly.
         - Add LEGACY_VERB_ALIASES (3.1) and export it.
      2. src/privacyfence/policy/propose.py: set each PROPOSABLE_SCOPES entry's verbs and the
         three new excludes exactly per 3.1's table. Rewrite comments that name removed verbs:
         ":304 draft is a send-family verb" becomes "add is a write-family verb, and this scope
         is still never widenable".
      3. src/privacyfence/policy/catalogue.py:
         - PolicyExtraScope: remove verb_labels and add `excludes`.
         - Change extra_operations_for to `(extra: PolicyExtraScope, verb: Verb)`, removing
           extra.excludes from its result.
         - Re-key gmail.configure and slack.share_anything to gmail.filters and
           slack.group_chats, with the verbs, labels and excludes from 3.1.
         - Add GROUP_ALIASES, and resolve it at the top of rules_for_catalogue_entry.
         - parse_verbs accepts LEGACY_VERB_ALIASES and de-duplicates, keeping first-seen order.
         - scope_catalogue no longer emits verb_labels.
         - Update the module docstring's verb names.
      4. src/privacyfence/policy/outdated.py: call `catalogue.extra_operations_for(extra, verb)`.
      5. src/privacyfence/policy/scopes.py: rewrite only the verb names in the comment at
         :570-582, as 3.1's note says.
      6. src/privacyfence/policy/describe.py: change the docstring examples to the new verbs
         ("allow read, update"; "also allow update (2 tools)").
      7. src/privacyfence/settings_window_html.py:
         - :928: chip text becomes `esc(verb)`, with no verb_labels lookup.
         - :1862-1876 (aaPairHint): `['move', 'copy']` becomes `['move']`. Keep the two-id hint.
      8. src/privacyfence/web/mcp_tools.py: in the propose tool's description, say verbs are
         one of read, download, search, add, update, restructure, move, send, delete, and that
         older names are mapped. Keep the `['read', 'update']` example.
      9. Regenerate docs/always-allow-rules-reference.md by running
         `python3 scripts/generate_always_allow_reference.py` as its docstring says, and commit
         the result.
      10. Tests: update every test that pins verb names, labels or counts, at least (test_registry, test_describe,
          test_propose, test_catalogue, test_outdated, test_settings_controller,
          test_routes_settings, test_routes_org_settings, test_gate, test_auto_accept,
          test_mcp_tools, test_mcp_dispatch, test_server, test_settings_window_html,
          test_routes_approvals, tests/packaged_policy_probe.py,
          tests/integration/test_settings_update_rule_browser.py) to the new names and counts.
          Never weaken an assertion. Rewrite test_outdated's "move rule is not widened by the
          copy verb" as "a move rule without drive.copy_file reports it missing".
          Add these tests:
          - tests/unit/policy/test_registry.py::test_there_are_nine_verbs
          - ::test_tool_to_verb_matches_the_verb_matrix: a literal dict of the 93 tools from 3.1
          - tests/unit/policy/test_catalogue.py::test_parse_verbs_maps_legacy_names
          - ::test_group_aliases_resolve
          - ::test_slack_group_chats_never_reaches_send_message
          - ::test_scope_catalogue_has_no_verb_labels
          - tests/unit/policy/test_propose.py::test_gmail_drafting_scope_excludes_create_label
          - ::test_contacts_label_scope_excludes_contacts_edit
          - ::test_slack_recipient_scope_excludes_group_chat
      Stop condition: if test_registry's join test fails for any reason other than verb names,
      stop with status=blocked (plan section 6).
      Run `ruff check .` and `python3 -m pytest tests/unit -q` before finishing.
    acceptance:
      - python3 -m pytest tests/unit/policy -q passes
      - python3 -m pytest tests/unit/policy/test_registry.py::test_there_are_nine_verbs tests/unit/policy/test_registry.py::test_tool_to_verb_matches_the_verb_matrix -q passes
      - python3 -m pytest tests/unit/test_generate_always_allow_reference.py -q passes
      - grep -rn "Verb\.\(CREATE\|FORMAT\|COMMENT\|LABEL\|COPY\|ARCHIVE\|COMPLETE\|TRANSITION\|CONFIGURE\|DRAFT\|SHARE\)" src tests matches nothing
      - grep -rn "verb_labels" src matches nothing
      - ruff check . passes and python3 -m pytest tests/unit -q passes

  - id: p2-token-personal-data
    title: Personal data is allowed by a scoped token flag, not by a rule
    depends_on: [p1-nine-verbs]
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/gate.py
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_gate.py
      - tests/unit/web/**
      - tests/unit/test_settings_window_html.py
    brief: |
      Read docs/scope-rules-plan.md section 3.2 (the token half). Do not touch PolicyRule or the
      rule form yet; p3 does that.
      1. scoped_tokens.py:
         - ScopedToken gains `allow_personal_data: bool = False`.
         - _to_json writes it only when true; _from_json reads
           `raw.get("allow_personal_data", False) is True`.
         - create() gains the keyword `allow_personal_data: bool = False` and stores it.
      2. gate.py _personal_data_rule (:723): return None unless `scope = current_token_scope()`
         is not None, is not scoped_tokens.REVOKED_SCOPE, and has allow_personal_data. Then use
         `_rules_in_scope(scope)` without filtering on rule.allow_personal_data, keeping the
         existing tool and content_reaches_result checks. Update its docstring to cite the token
         flag (ADR 0173).
      3. web/routes_settings.py:
         - _scoped_create_fingerprint_body includes `"allow_personal_data": <bool>`, and returns
           None if the key is present and not a bool.
         - ScopedTokenWiring.store_create gets a fifth bool argument.
         - Pass it through in both modes' create routes (find every `store_create(` call and
           the org create path).
      4. web/server.py:
         - The store_create wiring passes allow_personal_data to ScopedTokenStore.create.
         - scoped_token_page_data adds `"allow_personal_data": token.allow_personal_data` to
           each token dict.
      5. settings_window_html.py, in the scoped token form (search `__pfScopedTokens` and the
         create submit):
         - Add the checkbox with the exact label and help text from 3.2, sending
           `allow_personal_data`.
         - In the token list, show the badge `personal data allowed` for tokens that have it.
      6. Tests:
         - test_scoped_tokens.py: round trip, an old file without the key loads as false,
           create() stores it.
         - test_gate.py TestPersonalDataRules, rewritten:
           - test_flagged_token_lets_personal_data_through_export
           - test_unscoped_call_never_lets_personal_data_through: even when a rule still has
             allow_personal_data=True
           - test_token_without_flag_keeps_the_scan
           - test_revoked_scope_keeps_the_scan
           - test_inline_download_keeps_the_scan_even_with_flag
         - tests/unit/web/test_routes_settings.py and the org route tests: the fingerprint
           covers the flag, a non-bool flag is a 400, and the flag reaches the store.
         - test_settings_window_html.py: the checkbox label text and the badge text are present.
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_gate.py -q passes
      - python3 -m pytest tests/unit/test_gate.py -q -k "personal_data" passes and includes test_unscoped_call_never_lets_personal_data_through
      - grep -n "Let exports, downloads and page uploads carry personal data" src/privacyfence/settings_window_html.py matches
      - ruff check . passes and python3 -m pytest tests/unit -q passes

  - id: p3-rules-drop-personal-data
    title: Remove the personal-data flag from rules and the rule form
    depends_on: [p2-token-personal-data]
    complexity: M
    touches:
      - src/privacyfence/policy/engine.py
      - src/privacyfence/policy/store.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/describe.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/settings_window_html.py
      - src/privacyfence/auto_accept.py
      - tests/unit/policy/test_store.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_describe.py
      - tests/unit/policy/test_engine.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_auto_accept.py
      - tests/unit/test_gate.py
      - tests/unit/web/**
    brief: |
      Read docs/scope-rules-plan.md section 3.2 (the rule half).
      1. policy/engine.py: remove PolicyRule.allow_personal_data and its docstring paragraph.
      2. policy/store.py:
         - rule_id_for loses `allow_personal_data`, and its docstring drops the ADR 0157
           sentence.
         - merge_rules keys on (predicate, sortable value, conditions).
         - rule_to_dict no longer writes the flag.
         - rule_from_dict ignores an "allow_personal_data" key (no validation, no drop).
         - rule_id_for_rule follows.
      3. policy/catalogue.py: remove PERSONAL_DATA_GROUPS, rules_for_catalogue_entry's
         `personal_data` parameter, and the personal_data_label emission in scope_catalogue.
      4. policy/describe.py: remove the personal-data suffix from rule_sentence.
      5. settings_controller.py add_policy_rule: remove `personal_data` and its docstring
         sentence. web/routes_settings.py org add_policy_rule: remove its personal_data
         handling.
      6. settings_window_html.py: remove the rule form's personal-data checkbox (:937-941), the
         aaPersonalData state and the `personal_data` post field (:1887).
      7. auto_accept.py: fix any docstring that mentions the rule flag (:607). Keep
         PERSONAL_DATA_RULE_TOOLS and fix its comment to say the token flag (ADR 0173).
      8. Tests:
         - Delete the tests of the removed parameter and flag.
         - Add tests/unit/policy/test_store.py::test_rule_from_dict_ignores_allow_personal_data
           and ::test_rule_id_for_has_no_personal_data_variant.
         - test_settings_window_html asserts the rule form has no data-aa-personal-data
           attribute.
      Stop condition: `grep -rn "allow_personal_data" src` may still match the token feature
      from p2: scoped_tokens.py, gate.py (the token check), web/server.py, the
      create-token fingerprint and wiring in web/routes_settings.py, and the token form in
      settings_window_html.py. Leave those alone. Remove any other rule-flag use you find in
      this phase's files. If one remains in a file outside this phase's touches, stop with
      status=blocked.
    acceptance:
      - grep -rn "allow_personal_data" src/privacyfence/policy src/privacyfence/settings_controller.py matches nothing
      - grep -rn "PERSONAL_DATA_GROUPS" src tests matches nothing
      - python3 -m pytest tests/unit/policy/test_store.py::test_rule_from_dict_ignores_allow_personal_data -q passes
      - ruff check . passes and python3 -m pytest tests/unit -q passes

  - id: p4-scope-rows-module
    title: The scope-row model, compile, merge, edit and v2 migration (pure)
    depends_on: [p1-nine-verbs]
    complexity: M
    touches:
      - src/privacyfence/policy/scope_rows.py
      - tests/unit/policy/test_scope_rows.py
    brief: |
      Read docs/scope-rules-plan.md sections 3.3, 3.4 ("Pure part" only) and 3.5 (set_verbs only).
      Create src/privacyfence/policy/scope_rows.py with exactly the names, signatures and
      semantics listed there:
      SCHEMA_VERSION, ScopeRow, row_id_for, group_entries, extra_scope, scope_verbs,
      implied_operations, compile_row, compile_rows, row_to_dict, row_from_dict,
      rows_from_section, rows_to_config, rows_from_config, scope_of_rule, add_rules, remove_row,
      missing_operations, missing_tools, fill_missing, set_verbs, pairs_of, V2Migration,
      migrate_v2.
      - Custom rows take their verbs from registry.operation_verbs and may have empty verbs (3.3).
      - Give the module a docstring in the repo's style: what it is, that it is pure, and that it
        cites ADR 0172.
      - It must not import daemon_main, auto_accept writers, scoped_tokens, settings_controller
        or web.
      - This phase runs in parallel with p2/p3, whose base still has the personal-data flag on
        PolicyRule. So:
        - construct PolicyRule without allow_personal_data;
        - build flagged v2 entries in tests only as raw dicts;
        - read a raw entry's "allow_personal_data" yourself in migrate_v2, before calling
          store.rule_from_dict;
        - do not test a non-bool flag;
        - do not call rules_for_catalogue_entry with personal_data.
      Tests (tests/unit/policy/test_scope_rows.py), at least:
      - test_each_catalogue_group_round_trips_through_migration: parametrized over every
        propose.SCOPES_BY_GROUP key and every catalogue.EXTRA_SCOPES key.
        1. Build v2 rules with propose.rules_for_scope_group / catalogue.rules_for_catalogue_entry
           for all of the group's verbs and a sample value.
        2. Run migrate_v2 on their store.rule_to_dict forms, then rows_to_config, then
           rows_from_section, then compile_rows.
        3. Assert there is exactly one row and the compiled (predicate, operations, conditions)
           set equals the v2 one.
      - test_upgrade_round_trip_preserves_every_rule: a fixture with:
        - the seeded rule from src/privacyfence/resources/settings.yaml.example (always_allow,
          contacts.edit, no_contact_info_change);
        - a hand-written rule with a non-canonical id;
        - an unknown predicate;
        - a drive.file rule;
        - a rule with an operation its predicate's entry does not derive.

        Through the same write-and-read round trip, every rule compiles back identically, and
        custom rows keep their old ids.
      - test_screenshot_case_merges_to_one_row: approved_folder [F] {drive.download_file}, plus a
        raw dict of the same rule with allow_personal_data true and
        {drive.read_file, drive.download_file}. Expect one row with verbs {read, download},
        former_ids = both ids, and the flagged id in personal_data_ids.
      - test_slack_read_does_not_become_search, test_telegram_read_does_not_become_search,
        test_jira_read_does_not_become_search: the shared-key cases from section 2.
      - test_partial_verb_is_not_widened: a v2 sandbox rule with only sheets.format_range migrates
        with its operations unchanged, and missing_operations reports the other update operations.
      - test_condition_rule_outside_its_entry_stays_custom: a scope_of_rule step-3 check for a
        condition scope.
      - test_row_id_ignores_verbs and test_row_id_is_value_order_independent.
      - test_add_rules_extends_existing_row and test_add_rules_returns_touched_ids.
      - test_set_verbs_keeps_existing_operations_of_kept_verbs,
        test_set_verbs_adds_full_operations_for_new_verbs,
        test_set_verbs_keeps_shared_operation_implied_by_kept_verb (calendar add/update), and
        test_set_verbs_refuses_empty_unknown_custom_or_foreign_verbs.
      - test_row_from_dict_fails_closed: one case per refusal in 3.3, plus
        test_custom_row_with_empty_verbs_loads.
      - test_rows_from_config_converts_v2_in_memory and test_pairs_of_matches_compile_row.
      Stop condition: if a catalogue group fails the round-trip test and the cause is in
      propose/catalogue rather than in scope_rows, stop with status=blocked (plan section 6).
    acceptance:
      - python3 -m pytest tests/unit/policy/test_scope_rows.py -q passes
      - python3 -m pytest tests/unit/policy/test_scope_rows.py::test_upgrade_round_trip_preserves_every_rule -q passes
      - python3 -m pytest tests/unit/policy/test_scope_rows.py -q --cov=privacyfence.policy.scope_rows --cov-branch --cov-report=term-missing reports 100%
      - ruff check . passes and python3 -m mypy src/privacyfence/policy/scope_rows.py passes

  - id: p5-token-limits-and-migration
    title: Scoped tokens carry per-rule limits and can be re-pointed by the upgrade
    depends_on: [p3-rules-drop-personal-data, p4-scope-rows-module]
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/gate.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_gate.py
    brief: |
      Read docs/scope-rules-plan.md section 3.4, "Token part".
      1. scoped_tokens.py:
         - Add `rule_limits` to ScopedToken, with its JSON form and the fail-closed read.
         - ScopedTokenStore.create sets `rule_limits[rid]` for every chosen rule id to
           {(r.predicate, op) for r in auto_accept.get_policy_v2_store_rules() if r.id == rid
           for op in r.operations}. These are the same pairs scope_rows.pairs_of gives, taken
           from the compiled cache create() already validates against (:247).
         - Add migrate_token_file(path, migration) -> list[str] exactly as 3.4 specifies (replace
           old ids, limits, flag, idempotent), writing with secure_files.atomic_write_json in the
           same {"version": 1, "tokens": [...]} shape as _write. Factor the per-token
           transformation into one private function that apply_migration shares.
         - Add ScopedTokenStore.apply_migration(principal, migration), current_store() and
           migrate_tokens(principal, migration).
      2. gate.py _rules_in_scope: apply rule_limits as 3.4 says.
      3. Tests:
         - tests/unit/test_scoped_tokens.py:
           - test_create_records_limits_for_every_rule
           - test_rule_limits_round_trip
           - test_malformed_rule_limits_skips_the_token
           - test_migrate_token_file_replaces_old_ids
           - test_migrate_token_file_is_idempotent
           - test_partial_reference_records_narrow_limits
           - test_custom_row_reference_gets_its_own_limit
           - test_flag_transfer_returns_labels
           - test_missing_file_is_left_alone
           - test_apply_migration_holds_the_lock_and_refreshes: two migrations and a create,
             interleaved, lose nothing
         - tests/unit/test_gate.py:
           - test_rule_limits_narrow_a_token_to_its_former_rules
           - test_rule_limits_drop_a_rule_left_empty
           - test_adding_a_verb_to_a_row_does_not_widen_an_existing_token: build the compiled
             rules for a row before and after scope_rows.set_verbs adds a verb, and check the
             token created before still auto-accepts only its original pairs
    acceptance:
      - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_gate.py -q passes
      - python3 -m pytest tests/unit/test_gate.py -q -k test_adding_a_verb_to_a_row_does_not_widen_an_existing_token passes
      - ruff check . passes and python3 -m pytest tests/unit -q passes

  - id: p6-settings-upgrade
    title: Read version 3 and run the token-aware upgrade at load and at org startup
    depends_on: [p5-token-limits-and-migration]
    complexity: M
    touches:
      - src/privacyfence/policy/store.py
      - src/privacyfence/policy_upgrade.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/resources/settings.yaml.example
      - docs/configuration-reference.md
      - tests/unit/policy/test_store.py
      - tests/unit/test_policy_upgrade.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_docs_configuration_reference.py
    brief: |
      Read docs/scope-rules-plan.md section 3.4 ("Non-pure part" and the seeded-config
      paragraph), and 3.3's note on store.compile_rules_from_config.
      1. policy/store.py: compile_rules_from_config dispatches a version-3 section to scope_rows
         (a local import, with the cycle comment from 3.3).
      2. Create src/privacyfence/policy_upgrade.py with ensure_scope_rows exactly as 3.4 says,
         log strings verbatim.
      3. daemon_main.py:
         - load_config gains `principal`, and the two callers pass it as 3.4 lists.
         - Add _upgrade_all_principals and call it immediately before the
           `scoped_tokens.ScopedTokenStore(scoped_token_principals)` line (:2210).
      4. settings.yaml.example: version 3 with the seeded custom row generated by
         scope_rows.row_to_dict (id r-d76028ec0b, verbs [update]), and the rewritten comment.
         docs/configuration-reference.md :125-136: the version-3 keys table. Make
         tests/unit/test_docs_configuration_reference.py pass, extending it only where it pins
         v2 keys.
      5. Tests:
         - tests/unit/test_policy_upgrade.py (new):
           - test_v2_is_converted_and_backed_up
           - test_backup_is_not_overwritten
           - test_v3_is_left_alone
           - test_missing_config_file_makes_no_backup
           - test_token_failure_leaves_v2_and_returns_false (monkeypatch migrate_tokens to
             raise OSError)
           - test_settings_write_failure_keeps_v3_in_memory
           - test_logs_flagged_token_labels
         - tests/unit/test_daemon_main.py, new class TestScopeRowsUpgrade:
           - test_load_config_upgrades_with_principal
           - test_load_config_without_principal_does_not_upgrade
           - test_upgrade_all_principals_runs_before_the_token_store_loads
           - test_fresh_install_seed_is_already_version_3: no backup, no warning
           - test_unscoped_decisions_unchanged_by_upgrade: a v2 fixture covering every catalogue
             group plus the custom cases from p4's round-trip test. Load it through load_config
             (so through the written file), then compare policy.engine.evaluate's bool for every
             (operation, sample ctx) pair before and after.
         - tests/unit/policy/test_store.py: test_compile_rules_from_config_reads_version_3.
      Stop condition: if _load_principal_settings runs outside a principal_scope (so
      current_principal() is not the principal being loaded), or the ScopedTokenStore
      construction at :2210 is not on the org startup path that precedes serving requests, stop
      with status=blocked rather than guessing.
    acceptance:
      - python3 -m pytest tests/unit/test_policy_upgrade.py -q passes
      - python3 -m pytest tests/unit/test_daemon_main.py -q -k TestScopeRowsUpgrade passes
      - python3 -m pytest tests/unit/test_daemon_main.py -q -k test_unscoped_decisions_unchanged_by_upgrade passes
      - python3 -m pytest tests/unit/test_docs_configuration_reference.py -q passes
      - grep -n "version: 3" src/privacyfence/resources/settings.yaml.example matches
      - ruff check . passes and python3 -m pytest tests/unit -q passes

  - id: p7-row-writers
    title: Every writer stores scope rows, after the token-aware upgrade
    depends_on: [p6-settings-upgrade]
    complexity: M
    touches:
      - src/privacyfence/auto_accept.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/gate.py
      - tests/unit/test_auto_accept.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_gate.py
      - tests/unit/test_gate_real_evaluator.py
      - tests/packaged_policy_probe.py
      - tests/unit/web/**
    brief: |
      Read docs/scope-rules-plan.md section 3.7. Do not add set_policy_rule_verbs yet (p9).
      1. auto_accept.py:
         - Add get_policy_rows and policy_row_ids_for.
         - Re-implement get_policy_v2_rules, add_policy_v2_rules, remove_policy_v2_rule and
           update_policy_v2_rule on scope_rows, each calling policy_upgrade.ensure_scope_rows
           first. Keep their names, return types, locking and notifications.
         - Import scope_rows and policy_upgrade inside the functions (cycle; see
           auto_accept.py:540-544).
      2. settings_controller.py: add_policy_rule, remove_policy_rule and update_policy_rule work
         on rows as 3.7 says, setting the self.error text from 3.4 when ensure_scope_rows returns
         False. Their docstrings describe rows ("adds to the scope's row").
      3. web/routes_settings.py _apply_org_action: the add, remove and update branches keep
         calling the auto_accept writers. The add audit text names the row id(s) from
         policy_row_ids_for.
      4. gate.py propose_policy_change: keep rule_sentence for the description (p8 switches it).
         affected_ids comes from auto_accept.policy_row_ids_for(new_rules).
      5. tests/unit/test_gate_real_evaluator.py :810-891 (TestAcceptAllPersistsARealRule and
         ...ForWrites): assert on disk `scope: gmail.sender_domain` / `scope: gmail.label`
         instead of the predicate, and compare auto_accept_rule with
         scope_rows.row_id_for(scope, value).
      6. tests/packaged_policy_probe.py (probe_rule_on_disk / assert_probe_rule_on_disk, :104-112):
         expect version 3, `scope == "gmail.sender_domain"`, the same value, `verbs == ["read"]`
         and the same operations.
      7. Tests:
         - test_auto_accept.py:
           - test_add_policy_v2_rules_writes_version_3
           - test_adding_a_verb_to_a_scope_extends_its_row: the reported duplicate, from the add
             side
           - test_remove_by_row_id
           - test_update_fills_missing_on_row
           - test_writer_refuses_when_upgrade_fails
         - test_settings_controller.py: the same, plus test_two_adds_on_one_scope_leave_one_row,
           which asserts on scope_rows.rows_from_config(cfg), not on the snapshot (p8 changes the
           snapshot).
         - test_gate.py: the bridge add returns the row id; the popup "Always allow" lands in an
           existing row.
         Update the existing tests that read the section as version 2.
    acceptance:
      - python3 -m pytest tests/unit/test_auto_accept.py tests/unit/test_settings_controller.py tests/unit/test_gate_real_evaluator.py -q passes
      - python3 -m pytest tests/unit/test_auto_accept.py::test_adding_a_verb_to_a_scope_extends_its_row tests/unit/test_settings_controller.py -q -k "test_two_adds_on_one_scope_leave_one_row" passes
      - grep -n "rules_to_config" src/privacyfence/auto_accept.py src/privacyfence/settings_controller.py matches nothing
      - ruff check . passes and python3 -m pytest tests/unit -q passes

  - id: p8-row-readers
    title: Settings page, bridge listing and token form read scope rows
    depends_on: [p7-row-writers]
    complexity: M
    touches:
      - src/privacyfence/policy/describe.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/web/server.py
      - src/privacyfence/gate.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/policy/test_describe.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/test_gate.py
      - tests/unit/web/**
      - tests/integration/test_settings_update_rule_browser.py
    brief: |
      Read docs/scope-rules-plan.md section 3.6.
      1. describe.py: add row_sentence exactly as specified, and export it.
      2. settings_controller.py:
         - _rule_row takes a ScopeRow and adds `scope` and `scope_verbs`.
         - Rename _auto_accept_state_from_rules to _auto_accept_state_from_rows.
         - Usage is summed over id and former_ids.
         - SettingsController._auto_accept_state reads scope_rows.rows_from_config(cfg).
         - Keep the sort by sentence.
      3. web/routes_settings.py:
         - Org state builder (:1512-1542): use auto_accept.get_policy_rows() and the renamed
           function. _await_rule_names and cached_rule_value apply to compile_row(row)[0].
         - The org create-token audit text (:1723-1727) appends ", personal data allowed" when
           the flag is set.
      4. web/mcp_dispatch.py: _policy_rule_row(row), with the output keys from 3.6.
         list_policy lists get_policy_rows().
      5. web/server.py: _scoped_rule_entry per row; scoped_token_page_data iterates rows and adds
         `limited_to` per 3.6.
      6. gate.py propose_policy_change: descriptions use describe.row_sentence of the affected
         row(s).
      7. settings_window_html.py:
         - The notice text (:889) becomes exactly the string in 3.6.
         - The token list renders "(limited to: …)" when `limited_to` is present.
      8. Tests:
         - test_describe.py: test_row_sentence_for_a_folder_row (exact string
           "Drive - folder F: allow read, download"), plus the custom-row and condition-row
           cases.
         - test_settings_controller.py:
           - test_screenshot_case_renders_one_row: a v2 config with the two reported rules gives
             one row with verbs [read, download]
           - test_usage_sums_former_ids
         - test_mcp_dispatch.py: the list_policy keys.
         - test_server.py: limited_to appears only for a narrowed limit.
         - The org route test for the create-token audit text.
         - Update the browser test's expected notice text.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_controller.py -q -k "screenshot_case_renders_one_row or usage_sums_former_ids" passes
      - grep -n "These verbs also cover tools this rule doesn't allow yet" src/privacyfence/settings_window_html.py matches
      - grep -rn "_auto_accept_state_from_rules" src tests matches nothing
      - ruff check . passes and python3 -m pytest tests/unit -q passes
      - python3 -m pytest tests/integration/test_settings_update_rule_browser.py -v passes and no test reports SKIPPED (use PRIVACYFENCE_TEST_CHROMIUM if the session hook exported it)

  - id: p9-edit-verbs
    title: Edit a rule's verbs on the Settings page
    depends_on: [p8-row-readers]
    complexity: M
    touches:
      - src/privacyfence/auto_accept.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/org_settings_scope.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_auto_accept.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/web/**
      - tests/integration/test_settings_edit_verbs_browser.py
    brief: |
      Read docs/scope-rules-plan.md section 3.5. Copy the shape of the `update_policy_rule`
      action across every file it appears in. It is the closest precedent: list them with
      `grep -rn "update_policy_rule" src tests`.
      1. auto_accept.py: set_policy_rule_verbs(rule_id, verbs) -> bool on scope_rows.set_verbs,
         after ensure_scope_rows. It returns False on refusal or no change.
      2. settings_controller.py: set_policy_rule_verbs(self, rule_id: str, verbs: list) -> dict.
      3. web/routes_settings.py:
         - Add the action to _SENSITIVE_ACTIONS and _ORG_ALLOWED_ACTIONS.
         - Add an _apply_org_action branch, with the audit text from 3.5.
      4. web/org_settings_scope.py: add an ACTION_SCOPES entry, the same as update_policy_rule's.
      5. settings_window_html.py: the Edit verbs UI exactly as in 3.5, including the data-aa-*
         attributes, strings, the disabled Save and the disabled move toggle for fewer than two
         ids. Wire it into the existing handler block (:1707-1745) the way data-aa-expand and
         update_policy_rule are wired.
      6. Tests:
         - Unit tests for the controller and auto_accept: add a verb; remove a verb; a refusal
           is a no-op that writes nothing.
         - tests/unit/web/test_org_settings_scope.py: add set_policy_rule_verbs to the parametrize
           list of test_a_rule_write_is_per_principal_and_routed_in_both_modes (:139).
           test_every_action_names_local_mode_or_is_a_classified_org_only_action and
           test_org_allowed_actions_is_exactly_the_org_routed_projection must pass.
         - tests/unit/web/test_routes_settings.py: TestSensitiveActionsCoverAllAllowedActions
           (:452) passes, and step-up is required when require_passkey is on, in both modes.
         - test_settings_window_html: the Edit verbs hooks and confirm text are present.
         - A new tests/integration/test_settings_edit_verbs_browser.py, modelled on
           test_settings_update_rule_browser.py: open Edit verbs on a Drive folder row, add
           update, confirm, and see the row's chips change.
    acceptance:
      - python3 -m pytest tests/unit/test_settings_controller.py tests/unit/test_auto_accept.py -q -k set_policy_rule_verbs passes
      - python3 -m pytest tests/unit/web/test_org_settings_scope.py tests/unit/web/test_routes_settings.py -q passes
      - python3 -m pytest tests/integration/test_settings_edit_verbs_browser.py -v passes and no test reports SKIPPED
      - ruff check . passes and python3 -m pytest tests/unit -q passes

  - id: p10-retire
    title: ADRs, reference docs, changelog, delete the plan
    depends_on: [p9-edit-verbs]
    complexity: S
    touches:
      - docs/adr/**
      - docs/approvals-and-policy.md
      - docs/grist-setup.md
      - docs/configuration-reference.md
      - docs/how-it-works.md
      - docs/org-mode-setup-guide.md
      - docs/connect-claude-code.md
      - docs/privacyfence-sync.md
      - plugins/privacyfence-sync/skills/privacyfence-sync-recipes/**
      - CHANGELOG.md
      - docs/scope-rules-plan.md
      - docs/scope-rules-plan-manual-steps.html
    brief: |
      1. Write ADRs 0171, 0172, 0173 and 0174 from plan section 4, in the repo's ADR format (see
         docs/adr/0166-*.md). Each Verification section names the tests the phases added.
         Add them to docs/adr/README.md's index.
      2. Add one Status line to each amended ADR, and change nothing else in them (ADR rule 2):
         - 0074: "Amended by ADR 0172."
         - 0157: "Amended by ADR 0173."
         - 0164: "Amended by ADR 0173."
         - 0166: "Amended by ADR 0174."
         - 0170: "Amended by ADR 0171."
         Update the index's status column to match.
      3. docs/approvals-and-policy.md:
         - Replace the "### Verbs" section (:441-450) with the nine verbs and their families.
         - Rewrite :301-314 to describe one row per scope, Edit verbs, the new notice text, and
           "nothing widens a rule at startup or upgrade".
         - Fix the Peek sentence at :404: add covers peek_upload_page.
         - Fix the "create" example at :310.
         - Add a short "Personal data and scoped tokens" paragraph from 3.2.
      4. docs/grist-setup.md :170-180: the verb table becomes read/add/update/restructure, and
         "grant add and update".
      5. plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md
         :103-112:
         - The "Verbs to tick" table uses the new verbs (Peek "add", not "update (new
           versions)").
         - Tell the user to tick "Let exports, downloads and page uploads carry personal data"
           on the recipe's token instead of on the rule.
         - Fix any matching line in its SKILL.md and docs/privacyfence-sync.md.
      6. Search the docs that mention scoped tokens (docs/configuration-reference.md,
         how-it-works.md, org-mode-setup-guide.md, connect-claude-code.md) for the personal-data
         rule flag and verb names, and fix what you find.
      7. CHANGELOG.md, under "## [Unreleased]", in its existing subsection style:
         - "Auto-accept rules now use nine verbs: read, download, search, add, update,
           restructure, move, send and delete. Grist and Peek verbs no longer carry bracketed
           labels."
         - "Each scope now has one auto-accept rule. Adding verbs to a scope that already has a
           rule extends it, and existing rules are merged automatically on upgrade (the old
           settings file is kept as settings.yaml.pre-scope-rules.bak). Scoped tokens keep
           exactly the access they had. Downgrading is not supported: an older version ignores
           the merged rules and drops them if it saves rules; restore the backup file instead."
         - "A rule's verbs can now be edited on the Auto-accept page."
         - "Personal data in exports, file downloads and Peek uploads is now allowed per scoped
           token instead of per rule. Tokens that used such a rule keep it; calls made without
           a token always stop for review when personal data is found."
      8. Delete docs/scope-rules-plan.md and docs/scope-rules-plan-manual-steps.html.
         `grep -rn "scope-rules-plan" .` must match nothing.
    acceptance:
      - test -e docs/scope-rules-plan.md fails and grep -rn "scope-rules-plan" --include=*.md --include=*.py --include=*.html . matches nothing
      - ls docs/adr/0171-*.md docs/adr/0172-*.md docs/adr/0173-*.md docs/adr/0174-*.md lists four files and each is in docs/adr/README.md
      - grep -n "nine verbs" CHANGELOG.md matches a line under "## [Unreleased]"
      - ruff check . passes and python3 -m pytest tests/unit -q passes
```
