# Plan: organization auto-accept rules, and an audit trail that names who allowed a call (issue 1167)

> Historical record once merged: read it only to understand why a change was made.

## Summary

**Use it:** An administrator opens **Admin → Organization rules**, picks a scope (for example a Drive folder, a Drive tag or a Grist document), its value and verbs, and saves it with a passkey. From then on every person's own AI calls that the rule covers go through without a card, and each person sees the rule, read-only, under "Set by your organization" on their Auto-accept page and in `privacyfence_list_policy`. The audit log and its Excel export gain an **Allowed By** column that says `Person` (a click), `Person's own auto-accept rule`, `Organization auto-accept rule` or `Scoped token's rule`, for old rows as well.

**Scope:** New process-wide store `org_rules` kept in `org/admin_config.json`, three Admin actions, one more rule list in the gate (after the person's own rules, never under a scoped token), `allowed_by` on every audit entry (schema 9), read-only display for people and for AI clients, and a copy of an org rule into a new scoped token. Not in scope: a per-person opt-out, org rules inside scoped tokens, any change to PII confirmation or to the exempt tag-writing tools.

**Security:** This is a new way for a call to skip the approval card, set by an administrator for everyone. Only an administrator can create, change or remove one (passkey step-up, always); an AI client cannot propose, change or remove one. Rules stay fully general as the owner decided, so a write rule on a Drive tag can be forged by any editor of a tagged file (ADR 0321); the Admin form says so. PII confirmation still overrides every rule.

**Review focus:** the evaluation order and source attribution in the gate (3.3); the audit field and the `rule_usage` split so org matches never count for a person's rule (3.4); re-evaluating other people's pending cards when an org rule changes (3.3, p3).

## 1. Goal

Today only a person can create an auto-accept rule, and only for their own calls. An organization wants to say once, centrally, "reads of this Drive folder or this Grist document need no card for anyone". The audit file must show clearly whether a person or a rule allowed each call, and which rule's author (person, organization or token).

Issue: https://github.com/privacyfence/privacyfence/issues/1167. Owner decisions (board ask `a-30767df0a7ec2d7b`, 2026-11-20): rules are fully general (every scope and verb, the Drive tag forgery risk documented, no restriction); scoped-token calls do not see org rules (a token's creator can copy an org rule into the token); a person cannot switch an org rule off.

The "read-only simplification" does not matter for the design: org rules reuse the person-rule scope catalogue, verbs and row format unchanged, so being general costs nothing extra.

## 2. Current state

- **Rules.** One stored row per scope and value (`policy/scope_rows.py:37` `ScopeRow`, id `row_id_for` `:63`), compiled by `compile_rows` (`:153`) to `PolicyRule`s; the person's rows live in the person's `settings.yaml` and are cached per principal by `auto_accept._REGISTRY` (`auto_accept.py:564`, `get_policy_v2_store_rules` `:608`). The writer is `add_policy_v2_rules` (`:674`).
- **Gate.** `gate._rules_in_scope(scope)` (`gate.py:729`) returns the person's rules for an unscoped call and the token's own rules under a token (ADR 0236). `_evaluate_auto_accept` (`:702`) returns `(ok, rule_id, rule_id)`; `find_matching_rule` (`policy/engine.py:63`) is first-match; the temp-accept window comes last and only unscoped. `rule_values` (`:741`) feeds connectors the values of the call's rules (Gmail queries). `preflight_auto_accept` (`:786`) backs `privacyfence_check_policy`.
- **Where a call is audited as auto-accepted:** three immediate sites (`gate.py:1228`, `:1257`, `:1396`, with `rule_id=`) and two late ones for a card a rule change resolved while it was pending (`:1343`, `:1475`; they set only `auto_accept_rule=rule_name` and no `rule_id`). The late path is driven by `approvals.PendingApprovalRegistry.reevaluate_all` (`approvals.py:997`), which handles only the current principal (`:1016`) and finalizes with `finalize(approval_id, decision, rule_name)` (`:780`). `_resolve_decision` (`gate.py:450`) returns an 8-tuple to three call sites (review, popup, upload branches).
- **Audit.** `AuditEntry` (`audit_log.py:132`), `CURRENT_SCHEMA_VERSION = 8` (`:67`), `decision` values (`approved`, `auto_accepted`, `accepted_via_accept_all`, `accepted_via_temp_session`, ...), `rule_id` (schema 4), 35 Excel columns appended in `export_week_to_excel` (`:763`; new columns always last), `rule_usage()` (`:1010`; skips `agent_id` starting `scoped-token:`). The pattern commit for a new field is `96a61785d` (schema 8).
- **Admin.** `POST /api/admin/{action}` (`web/routes_admin.py:728`); actions are declared in `ADMIN_ACTIONS` (`:101`), `ADMIN_PARAMS` (`:137`) and `admin_config._ACTION_MODES` (`admin_config.py:453`), applied in `admin_config.apply_change` (`:936`), audited as `settings_change` by `_record_settings_audit` (`web/routes_settings.py:375`). `applies` is `live` (refreshes services and evicts every connector host), `restart` or `self`. Parameter types are `str`, `int`, `bool`, `float`, `dict` only. The state is built by `_admin_state` (`:324`); the page is rendered by `settings_window_html.py` (`ADMIN_NAV_ITEMS` `:2002`, `adminSection` `:2647`, `_admin_capabilities` `:3163`).
- **Person page.** `_org_state` (`web/routes_settings.py:654`) builds `auto_accept` with `_auto_accept_state_from_rows` (`settings_state.py:432`, `_rule_row` `:385`); rendered by `renderAutoAccept` (`settings_window_html.py:786`).
- **MCP tools.** `list_policy` (`web/mcp_dispatch.py:457`, row via `_policy_rule_row` `:59`), `check_policy` (`:389`), `propose_policy_change` (`:488` → `gate.propose_policy_change` `gate.py:1546`, which looks up ids in the person's rows only).
- **Startup.** `daemon_main.load_admin_config(bundle)` (`daemon_main.py:579`) reads the admin config once at start.
- **Scoped-token copy.** `ScopedTokenStore._prepare` (`scoped_tokens.py:~809`) resolves `copy` entries from `auto_accept.get_policy_rows()`; the token form lists those rows (`web/server.py:264`) and posts `{kind: 'copy', rule_id}` (`settings_window_html.py:1950`).

## 3. Design

### 3.1 Storage and module `org_rules`

- `org/admin_config.json` gets a top-level key `org_rules`: a list of rows in `scope_rows.row_to_dict` format. `admin_config._default_config()` adds `"org_rules": []`. At most `MAX_ORG_RULES = 100` rows. Only catalogue-scope rows are accepted: if `scope_rows.add_rules` yields a custom row (`scope is None`) the action fails with `That rule cannot be stored as an organization rule.`
- New module `src/privacyfence/org_rules.py` (docstring: install-wide, not per principal, read by every request). Process-wide state (a list of `ScopeRow`, its compiled `PolicyRule`s, a `threading.Lock`, a list of change listeners). Public functions: `rows() -> list[ScopeRow]`, `rules() -> list[PolicyRule]` (compiled once per change, so the same objects are returned until the next change), `is_org_rule(rule: PolicyRule) -> bool` (identity: `any(r is rule for r in rules())`), `load_from_config(admin: dict) -> None` (set silently, used at startup), `reload_from_admin_config() -> None` (read `admin_config.load()`, set, then notify), `add_changed_listener(cb)`, `remove_changed_listener(cb)`, `notify_changed()`, `reset()`. A row that does not parse (`row_from_dict` returns `None`) is dropped with a warning, which only narrows.
- Module state needs the reset line in `tests/conftest.py` `_reset()`.

### 3.2 Admin actions (all org-only, sensitive, `applies="self"`)

| Action | Params | Effect and audit summary |
|---|---|---|
| `add_org_rule` | `group` str, `value` str, `verbs` str (comma-separated) | builds rules like the Auto-accept page's `add_policy_rule` (`catalogue.parse_value_list`, `catalogue.parse_verbs`, `catalogue.rules_for_catalogue_entry`, `scope_rows.add_rules`); summary `Added organization rule: <sentence>` |
| `remove_org_rule` | `rule_id` str | `scope_rows.remove_row`; summary `Removed organization rule: <sentence>`; unknown id `Unknown organization rule.` |
| `set_org_rule_verbs` | `rule_id` str, `verbs` str | `scope_rows.set_verbs`; summary `Changed the verbs of organization rule: <sentence>`; `None` result `Those verbs cannot be set on that rule.` |

`<sentence>` is `policy_describe.row_sentence(row)`. `verbs` is one comma-separated string because `ADMIN_PARAMS` has no list type. An empty rule (no verbs, a missing value) fails with `An organization rule needs a scope, at least one of its verbs, and a value if the scope takes one.` A `RuleValueError` becomes an `AdminConfigError` with its text. A no-op returns `("", [])`. Changed section name: `org_rules`. After a change `routes_admin._dispatch` calls `org_rules.reload_from_admin_config()` (outside `admin_config._write_lock`), and writes the audit row with decision `org_rule_added`, `org_rule_removed` or `org_rule_verbs_changed` (new optional `decision` argument of `_record_settings_audit`, default `settings_change`). `applies="self"`: no `refresh_live`, no connector eviction, no restart label.

### 3.3 Evaluation

- **Order.** An unscoped call checks the person's rules first, then org rules, then the temp-accept window. A scoped-token call checks only the token's rules (ADR 0236 unchanged; owner decision). Rejected: org first (the person's `Matched Nx` counts would drift) and merging org rows into the person's file (the person could delete them; the owner wants no opt-out).
- **Source.** `_rules_in_scope(None)` returns `person_rules + org_rules.rules()` (so `rule_values` and `preflight_auto_accept` see both). `gate._rule_source(rule, scope)` returns `"token"` under a scope, else `"organization"` when `org_rules.is_org_rule(rule)` (identity), else `"person"`. `_evaluate_auto_accept` returns a 4-tuple `(ok, rule_id, rule_id, source)`; `source` is `""` for the temp-accept window. `_should_auto_accept` returns `(ok, rule_id, source)`.
- **PII, exempt operations, personal-data flags** are untouched: `pii_forces_confirmation` still wins over every rule; `RULE_EXEMPT_OPERATIONS` (the tag-writing tools) still match nothing.
- **Pending cards.** `PendingApproval` gets `principal: Principal` (default `current_principal()`) and `final_rule_source: str`. `finalize(approval_id, decision, rule_name="", rule_source="")` stores it; `LedgerHit` carries `rule_source`. `reevaluate_all` keeps its current-principal behaviour; a new `reevaluate_every_principal(should_auto_accept)` evaluates each candidate inside `principal_scope(approval.principal)` (the person's rules are per principal) and passes the source on. The callback shape becomes `(ok, rule_id, source)` for both. `gate._on_org_rules_changed` calls `reevaluate_every_principal`; it is registered with `org_rules.add_changed_listener` next to the existing `add_rules_changed_listener(_on_rules_changed)` (`gate.py:429`).
- **`_resolve_decision`** returns a 9-tuple, `rule_source` last; the three call sites and the registry-less path (`""`) are updated. The late auto-accept audit rows set `rule_id=rule_name` and `rule_source` when the source is not `""`.
- **`preflight_auto_accept`** returns a 5-tuple, `rule_source` last (`""` when nothing matched), derived by looking the matched id up in the rules it evaluated, person first.
- **AI clients.** `propose_policy_change` with a `rule_id` that is an org rule fails before any popup with `That rule is set by your organization; only an administrator can change it on the Admin page.` The model cannot add an org rule at all: `add` only writes the person's rows.

### 3.4 Audit: who allowed it

- `AuditEntry.allowed_by: str = ""`, schema version 9. Stored values: `person`, `person_rule`, `org_rule`, `token_rule`, `""`. `audit_log.allowed_by_for(decision: str, rule_source: str) -> str`: decisions `approved`, `accepted_via_accept_all`, `accepted_via_temp_session` give `person`; `auto_accepted` gives `org_rule` for source `organization`, `token_rule` for `token`, `person_rule` for `person`, and `person` for `""` (the temp-accept window is a person's click); every other decision gives `""`. `gate._audit` takes `rule_source=""` and stores `allowed_by_for(decision, rule_source)`.
- `audit_log.effective_allowed_by(data: dict) -> str` for rows written before schema 9: the stored value when set; else for `auto_accepted` `token_rule` when `agent_id` starts with `scoped-token:`, `person_rule` when `rule_id` is set, `person` otherwise; for the three person decisions `person`; else `""`. The export and `rule_usage` use it, so old logs read clearly too.
- Excel: column 36 `Allowed By`, shown as `Person`, `Person's own auto-accept rule`, `Organization auto-accept rule`, `Scoped token's rule` (empty for `""`); a row allowed by an organization rule gets fill `EDE7F6`; decisions `org_rule_added`, `org_rule_removed`, `org_rule_verbs_changed` get fill `FFF3CD` (like the other rule changes) and are listed in the `decision` comment.
- `rule_usage(source: str = "person_rule")` counts entries whose `effective_allowed_by` equals `source` (and, as today, never `scoped-token:` agents for `person_rule`); `source="org_rule"` backs the Admin page's `Matched Nx`. Without this an org match would raise the count of a person's rule with the same id (ids hash scope and value only).

### 3.5 Surfaces

- **Person page.** `_org_state` adds `org_rules`: the org rows through `_rule_row` with no usage. The Auto-accept section shows them under the heading `Set by your organization`, as sentences with their verbs, no links or buttons, and the line `Only an administrator can change these.`
- **MCP.** `list_policy` returns `organization_rules` (the same row shape, plus `"source": "organization"`); `check_policy` returns `rule_source` (`person`, `organization` or `null`).
- **Admin page.** Nav key `org_rules`, label `Organization rules`, after `tokens`. State key `org_rules` shaped like `auto_accept` (`rules` with `match_count` and `last_matched` from `rule_usage(source="org_rule")`, `scope_groups`, `connectors`). The section lists rules (sentence, verbs, `Matched Nx`, Edit verbs, Remove) and the Add-rule controls the Auto-accept page uses. Warning text above the list: `These rules let every person's AI use the matching resources without asking. A rule on a Drive tag can be forged by anyone who can edit a tagged file, so prefer read verbs there.`
- **Scoped tokens.** `ScopedTokenStore._prepare` resolves a `copy` entry from the person's rows, else from the org rows (the person's row wins on an id collision). The token form lists org rows not already listed, labelled `(set by your organization)`.

### 3.6 Alternatives rejected

- Org rules stored in each person's `settings.yaml` (copied in): the person could delete them and they would drift.
- Org rules covering scoped tokens: widens every existing token silently (ADR 0236).
- A person opt-out: not wanted.
- A new `decision` string `auto_accepted_org` instead of a field: it breaks every consumer that matches `auto_accepted` (usage counts, `APPROVED_LIKE_DECISIONS`, forwarding filters).
- Restricting org rules to read verbs or excluding Drive tags: the owner chose fully general.

## 4. ADRs

- **0333**: Organization auto-accept rules are set by an administrator, stored in the admin config, checked after a person's own rules, never apply under a scoped token and cannot be changed by an AI client or switched off by a person; rules are fully general and the tag forgery risk of ADR 0321 is accepted for them. Relates to 0164, 0172, 0236, 0257, 0320, 0321.
- **0334**: The audit entry names who allowed a call in an `allowed_by` field (schema 9), derived at write time and for old rows at read time. Relates to 0071, 0074.

## 5. Manual steps

**After implementation.** One check CI cannot do: a real passkey and a real server (about 10 minutes).

1. Open the Admin page of a QA or test organization server (`/admin`), sign in as an administrator.
2. Open **Organization rules**, choose a scope you can safely read (for example Grist document, or Drive folder with the verb `read`), enter its value, and save with your passkey. You should see the rule listed with `Matched 0x`.
3. Sign in as an ordinary person, open **Settings → Auto-accept** and confirm the rule appears under **Set by your organization** with no buttons.
4. Have an AI client make one read call the rule covers; it should return with no approval card.
5. Open the weekly Excel export under `logs/audit/` (or the Admin audit view) and confirm the row has `Allowed By` = `Organization auto-accept rule` and the rule id; then click Allow on one other card and confirm `Allowed By` = `Person`.
6. Remove the rule on the Admin page and confirm the next call asks for a card again.
7. Report back one line for the pull request: `org rules: pass` or `org rules: fail - <step>`.

## 6. Risks and open questions

- **Gate regression.** `_evaluate_auto_accept` and `_resolve_decision` change shape and every unpack site must follow. A worker that finds an unpack site not named in its brief stops with `status=blocked`.
- **Pending cards of other people.** `reevaluate_every_principal` runs the evaluator in another principal's scope. If a test shows a card finalized under the wrong person's rules, p3/p4 stop and report.
- **Single-user installs** are organization mode with one administrator; org rules there duplicate personal rules and do no harm.
- **Value-less "mine" scopes** (for example Drive files I own) as org rules mean each person's own files: they evaluate against each caller. The sentence shown says so already.
- **Hash chain.** A new field must not break verification of schema 8 logs; p1's test replays a stored schema 8 line.
- **Org rules do not follow the admin who left.** They persist until removed; the Admin page lists them.
- **JS sharing.** p7 shares the Auto-accept add-rule controls with the Admin section; if they cannot be shared in a small change, p7 stops (it is a `strong` phase for that reason).
- **Browser tests cannot run in this container** (no Chromium for the pinned Playwright); they run in CI on the pull request.

## Implementation manifest

```yaml
plan_slug: org-auto-accept-rules
feature_branch: feature/org-auto-accept-rules
tracking_issue: 1167
max_parallel: 2
manual_before: []
manual_after:
  - id: ma1-org-rule-on-a-real-server
    title: Create an organization rule with a passkey and check the audit export
    why: A passkey step-up, a real second person and the Excel export cannot be driven by CI.
verify_after_merge:
  - python3 -m pytest tests/unit -q
  - ruff check .
final_checks:
  - "`tld plan deviations check --file docs/1167/deviations.md --plan docs/1167/plan.md --all` exits 0"
  - "ADRs 0333 and 0334 exist, are Accepted, and are in docs/adr/README.md"
  - "CHANGELOG.md has entries under `## [Unreleased]` and no version heading"
phases:
  - id: p1-audit-allowed-by
    title: The audit entry says who allowed a call
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/audit_log.py
      - tests/unit/test_audit_log.py
    brief: |
      Read first: plan section 3.4; src/privacyfence/audit_log.py; commit 96a61785d (`git show 96a61785d`) is the pattern for adding fields, a schema bump and appended Excel columns.
      1. In `audit_log.py` add `allowed_by: str = ""` to `AuditEntry` (after `metadata`, before the hash-chain fields), with a comment block like the neighbouring fields; bump `CURRENT_SCHEMA_VERSION` to 9 and add the history line `#   9 -- who allowed the call: + allowed_by`.
      2. Add module-level `ALLOWED_BY_LABELS = {"person": "Person", "person_rule": "Person's own auto-accept rule", "org_rule": "Organization auto-accept rule", "token_rule": "Scoped token's rule"}`.
      3. Add `def allowed_by_for(decision: str, rule_source: str) -> str` and `def effective_allowed_by(data: dict[str, Any]) -> str` exactly as plan 3.4 defines them (`data` is an entry dict as read from a `.jsonl` line, keys `allowed_by`, `decision`, `agent_id`, `rule_id`).
      4. In `export_week_to_excel` append the header `Allowed By` as column 36 (add its width to `COL_WIDTHS`), append `ALLOWED_BY_LABELS.get(effective_allowed_by(asdict(entry)), "")` to each row, and in `decision_fills` give `org_rule_added`, `org_rule_removed` and `org_rule_verbs_changed` the fill `FFF3CD`; for a row whose `effective_allowed_by` is `org_rule` use fill `EDE7F6` instead of the decision's. List the three new decisions in the `AuditEntry.decision` comment.
      5. Change `rule_usage(self)` to `rule_usage(self, source: str = "person_rule")`: count an entry only when `decision == "auto_accepted"`, `rule_id` is non-empty and `effective_allowed_by(data) == source`. Keep the existing `scoped-token:` skip via `effective_allowed_by` (a token row is `token_rule`, so it no longer matches `person_rule`).
      6. Tests in `tests/unit/test_audit_log.py`, new classes `TestAllowedBy` (the mapping table of `allowed_by_for`, every row of plan 3.4, parametrized; `effective_allowed_by` on legacy dicts: token agent, rule id, no rule id, `approved`, `rejected`), `TestAllowedByExport` (header index 35 is `Allowed By`, labels, fills; an `org_rule_added` row has fill `FFF3CD`) and `TestRuleUsageBySource` (`person_rule` ignores an `org_rule` row with the same `rule_id`; `org_rule` counts only it; legacy rows with no `allowed_by` and a `rule_id` count for `person_rule`). Extend `TestAgentFields`'s `headers.index("Rule ID") == 21` style checks only if they break. Add one test that `verify_chain` still passes on a hand-written schema 8 line (no `allowed_by` key) followed by a schema 9 line.
      Stop condition: if `verify_chain` fails on the schema 8 line, stop with status=blocked and report; do not change the hash input.
    acceptance:
      - "`python3 -m pytest tests/unit/test_audit_log.py -q` passes, including `TestAllowedBy`, `TestAllowedByExport`, `TestRuleUsageBySource`"
      - "`grep -n 'CURRENT_SCHEMA_VERSION = 9' src/privacyfence/audit_log.py` prints one line"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` pass"
  - id: p2-org-rules-store
    title: The org rule store and its Admin config actions
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/org_rules.py
      - src/privacyfence/admin_config.py
      - tests/unit/test_org_rules.py
      - tests/unit/test_admin_config.py
      - tests/conftest.py
    brief: |
      Read first: plan sections 3.1 and 3.2; src/privacyfence/admin_config.py (`_default_config`, `_ACTION_MODES`, `apply_change`, `_replace_section`); src/privacyfence/policy/scope_rows.py; src/privacyfence/policy/catalogue.py; the `add_policy_rule` handling in src/privacyfence/web/routes_settings.py (`_apply_org_action`) as the pattern for building rules from `group`, `value` and `verbs`.
      1. Create `src/privacyfence/org_rules.py` with the public functions and state of plan 3.1. `rules()` caches `scope_rows.compile_rows(rows())`; `set` replaces rows and invalidates it. Listeners are called outside the lock, each in `try/except Exception: logger.exception(...)`.
      2. In `admin_config.py`: add `"org_rules": []` to `_default_config()` (a list default: `_fill` leaves lists alone, so no code change there); add `MAX_ORG_RULES = 100`; add the three actions to `_ACTION_MODES` as `_ORG_ONLY`; implement `_apply_add_org_rule`, `_apply_remove_org_rule`, `_apply_set_org_rule_verbs` as plan 3.2 says and dispatch them in `apply_change` before the final `else`. Rows are read with `scope_rows.row_from_dict` and written with `scope_rows.row_to_dict`; a stored row that does not parse is dropped from the list on the next write. Refuse more than `MAX_ORG_RULES` rows with `An organization can have at most 100 rules.` Refuse a custom row as plan 3.1 says. The summary text is exactly the one in the 3.2 table; changed sections `["org_rules"]`.
      3. `tests/conftest.py`: import `org_rules` and add `org_rules.reset()` to `_reset()` next to `auto_accept._REGISTRY.reset()`.
      4. Tests. `tests/unit/test_org_rules.py` (`TestStore`: set/rows/rules, `is_org_rule` identity, a non-parsing row dropped, listeners called once per change and an exception in one does not stop the next, `load_from_config` does not notify, `reload_from_admin_config` reads the admin config file). `tests/unit/test_admin_config.py` (`TestOrgRules`: add a Drive folder read rule, add the same rule again with another verb merges into one row, remove, unknown id, verbs change, no verbs refused, missing value refused, the 101st rule refused, an unknown group refused, a no-op returns `("", [])` and writes nothing, the written file holds `org_rules` rows).
      Stop condition: if `scope_rows.add_rules` returns a custom row for a catalogue scope you try, stop with status=blocked and report which scope.
    acceptance:
      - "`python3 -m pytest tests/unit/test_org_rules.py tests/unit/test_admin_config.py -q` passes"
      - "`grep -n 'org_rules' tests/conftest.py` shows the reset line"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` pass"
  - id: p3-approvals-principal-and-source
    title: Pending cards remember their principal and the source of the rule that resolved them
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/approvals.py
      - tests/unit/test_approvals.py
    brief: |
      Read first: plan section 3.3 (Pending cards); src/privacyfence/approvals.py (`PendingApproval` :275, `finalize` :780, `reevaluate_all` :997, `LedgerHit` :478, `consume_ledger` :814).
      1. Add `principal: Principal = field(default_factory=current_principal)` to `PendingApproval` (import `Principal` from `.principal`) and `final_rule_source: str = ""`. Keep `principal_id` as it is.
      2. `finalize(self, approval_id, decision, rule_name="", rule_source="")` stores `approval.final_rule_source = rule_source`. Add `rule_source: str = ""` to `LedgerHit` and fill it in `consume_ledger` from `approval.final_rule_source`.
      3. `reevaluate_all(should_auto_accept)` now expects a callback returning `(ok, rule_id, source)`; pass `source` to `finalize`. Factor its candidate loop into a private helper that takes the candidate list, and add `reevaluate_every_principal(self, should_auto_accept) -> list[PendingApproval]`: candidates are every non-finalized `kind == "card"` approval with an operation key and no forced PII confirmation, regardless of principal; each is evaluated inside `with principal_scope(approval.principal):`. `reevaluate_all` keeps selecting only `a.principal_id == current_principal().id`.
      4. Tests in `tests/unit/test_approvals.py`: new class `TestReevaluateEveryPrincipal` (two principals each with a pending card; a callback that accepts only inside principal A's scope finalizes only A's card, proving `principal_scope` is entered; the source reaches `final_rule_source` and the `LedgerHit`; `reevaluate_all` still skips the other principal's card). Update the existing `reevaluate_all` tests for the 3-tuple callback.
      Stop condition: if `principal_scope` cannot be imported from `.principal` without a cycle, stop with status=blocked.
    acceptance:
      - "`python3 -m pytest tests/unit/test_approvals.py -q` passes, including `TestReevaluateEveryPrincipal`"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` pass"
  - id: p4-gate-org-rules
    title: The gate checks org rules, attributes the source, and re-evaluates pending cards
    depends_on: [p1-audit-allowed-by, p2-org-rules-store, p3-approvals-principal-and-source]
    complexity: M
    touches:
      - src/privacyfence/gate.py
      - src/privacyfence/daemon_main.py
      - tests/unit/test_gate.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read first: plan sections 3.3 and 3.4; src/privacyfence/gate.py (`_resolve_decision` :450, `_on_rules_changed` :674, `_evaluate_auto_accept` :702, `_rules_in_scope` :729, `preflight_auto_accept` :786, the audit call sites listed in plan section 2, `propose_policy_change` :1546, `_audit` :1860); src/privacyfence/daemon_main.py `load_admin_config` :579; tests/unit/test_gate.py `TestRuleIdAttribution` :352 and `TestScopedTokenGate` :4274.
      1. `_rules_in_scope(scope)`: for `scope is None` return the person's non-token-only rules followed by `org_rules.rules()`; the scoped branch is unchanged.
      2. Add `_rule_source(rule, scope) -> str` (plan 3.3). `_evaluate_auto_accept` returns `(ok, rule_id, rule_id, source)` with source `""` for the temp-accept match. `_should_auto_accept` returns `(ok, rule_id, source)`.
      3. `_audit(..., rule_source="")` computes `allowed_by=audit_log.allowed_by_for(decision, rule_source)` and sets it on the `AuditEntry`. Thread `rule_source` through the inner `audit()` closure of `gated_call` (new keyword `rule_source=""`). Pass the source at the three immediate sites. `_personal_data_rule` sites pass `rule_source="token"` (that path is token-only).
      4. `_resolve_decision` returns the 9-tuple with `rule_source` last (`""` on the registry-less path, `ledger_hit.rule_source` on a ledger hit, `approval.final_rule_source` after a wait). Update its three call sites; at the two late `auto_accepted` audit rows pass `rule_source=rule_source` and `rule_id=rule_name` when `rule_source != ""`.
      5. `preflight_auto_accept` returns `(verdict, matched_rule, matched_rule_id, reason, rule_source)`; the source is looked up with `_rule_source` over the rules actually evaluated, taking the first rule whose `.id` equals the matched id (person rules come first); `""` when nothing matched or the match was the temp-accept window. The error path returns `("requires_review", "", "", "", "")`. Update the one caller in `web/mcp_dispatch.py` minimally: unpack the fifth value and ignore it (p5 uses it). If a second caller exists, update it the same way.
      6. `_on_org_rules_changed()` calls `registry.reevaluate_every_principal(_should_auto_accept)` and logs like `_on_rules_changed`; register it with `org_rules.add_changed_listener` where `add_rules_changed_listener(_on_rules_changed)` is registered (`gate.py:429`).
      7. `propose_policy_change`: before the unknown-id check, when `rule_id` is non-empty and equals the id of a row in `org_rules.rows()` and not of a person row, raise `ValueError("That rule is set by your organization; only an administrator can change it on the Admin page.")`.
      8. `daemon_main.load_admin_config`: after `admin_config.ensure(...)` succeeds call `org_rules.load_from_config(admin)`.
      9. Tests in `tests/unit/test_gate.py`: new class `TestOrgRules` (an org rule auto-accepts an unscoped call with `allowed_by == "org_rule"` and its `rule_id`; a person rule with the same scope and value wins and is `person_rule`; a call under a scoped token with no token rule ignores the org rule and is denied; the temp-accept window audits `allowed_by == "person"`; `pii_forces_confirmation` still forces the card despite an org rule; a tag-writing operation is not covered; `rule_values` includes an org rule's values; `preflight_auto_accept` returns the source; `propose_policy_change` refuses an org rule id with the exact message; a pending card of person B resolves as `org_rule` when `org_rules.set` + `notify_changed` runs and A's pending card is untouched by B's person rules). Extend `TestRuleIdAttribution` for the late path now carrying `rule_id`. In `tests/unit/test_daemon_main.py` one test that `load_admin_config` loads the rows from a written `admin_config.json`.
      Stop condition: if any caller of `_evaluate_auto_accept`, `_resolve_decision` or `preflight_auto_accept` exists that this brief does not name (`grep -rn` for each), stop with status=blocked and list it.
    acceptance:
      - "`python3 -m pytest tests/unit/test_gate.py tests/unit/test_daemon_main.py tests/unit/test_approvals.py tests/unit/web/test_mcp_dispatch.py -q` passes, including `TestOrgRules`"
      - "`python3 -m pytest tests/unit/test_systemic_gate_invariants.py -q` passes"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` pass"
  - id: p5-person-and-ai-surfaces
    title: People and AI clients see org rules read-only
    depends_on: [p2-org-rules-store, p4-gate-org-rules]
    complexity: S
    touches:
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/settings_state.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/test_settings_state.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Read first: plan section 3.5; web/mcp_dispatch.py (`_policy_rule_row` :59, `check_policy` :389, `list_policy` :457); settings_state.py (`_rule_row` :385, `_auto_accept_state_from_rows` :432); web/routes_settings.py (`_org_state` :654).
      1. `mcp_dispatch.list_policy`: add `"organization_rules": [dict(_policy_rule_row(row), source="organization") for row in org_rules.rows()]` sorted by `sentence`, next to `rules`. The audit row and the existing keys do not change.
      2. `mcp_dispatch.check_policy`: use the fifth value of `preflight_auto_accept` as `rule_source` in the result: `"person"`, `"organization"` or `None` when it is `""`; the `gate == "auto"` branch returns `rule_source: None`.
      3. `settings_state.py`: add `org_rule_rows(rows, *, resolve_value=None, resolver=None) -> list[dict]` that returns `_rule_row(row, ...)` for each org row with `match_count`, `last_matched` and `never_matched` removed.
      4. `routes_settings._org_state`: add the key `"org_rules": settings_state.org_rule_rows(org_rules.rows(), resolve_value=..., resolver=...)` using the same `resolve_value` and `resolver` it passes to `_auto_accept_state_from_rows`.
      5. Tests: `TestListPolicy` (`organization_rules` present, empty by default, source field), `TestCheckPolicy` (`rule_source` for a person rule, an org rule, no match, the `auto` gate), `TestOrgRuleRows` in test_settings_state.py, and one test in `TestPrincipalScopedRules` that two principals both see the same `org_rules` while their own `auto_accept.rules` differ.
    acceptance:
      - "`python3 -m pytest tests/unit/web/test_mcp_dispatch.py tests/unit/test_settings_state.py tests/unit/web/test_routes_org_settings.py -q` passes"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` pass"
  - id: p6-admin-backend
    title: The Admin actions, state and audit rows for org rules
    depends_on: [p2-org-rules-store]
    complexity: S
    touches:
      - src/privacyfence/web/routes_admin.py
      - tests/unit/web/test_routes_admin.py
    brief: |
      Read first: plan sections 3.2 and 3.5; web/routes_admin.py (`ADMIN_ACTIONS`, `ADMIN_PARAMS`, `_dispatch` :386, `_admin_state` :324); web/routes_settings.py `_record_settings_audit` :375 (read only: it is edited below through a keyword you pass, see step 3); tests/unit/web/test_routes_admin.py `TestApplying`, `TestAudit`, `TestRouteClassification`.
      1. `ADMIN_ACTIONS`: add `add_org_rule`, `remove_org_rule`, `set_org_rule_verbs` as `AdminAction(_ORG_ONLY, True, "self")`. `ADMIN_PARAMS`: `add_org_rule`: `(("group", str, True), ("value", str, True), ("verbs", str, True))`; `remove_org_rule`: `(("rule_id", str, True),)`; `set_org_rule_verbs`: `(("rule_id", str, True), ("verbs", str, True))`.
      2. In `_dispatch`, after `apply_change` succeeds with `"org_rules" in changed`, call `org_rules.reload_from_admin_config()`.
      3. The audit decision: `_record_settings_audit` lives in `web/routes_settings.py`, which this phase may not edit; instead write the row in `routes_admin` with a small local helper `_record_org_rule_audit(principal, decision, summary)` that builds the same `AuditEntry` shape `_record_settings_audit` builds (copy its fields) but with the given decision; use it, instead of `_record_settings_audit`, when the action is one of the three, with decision `org_rule_added`, `org_rule_removed` or `org_rule_verbs_changed`.
      4. `_admin_state`: add `"org_rules"`: `{"rules": ..., "scope_groups": policy_catalogue.scope_catalogue(), "connectors": ...}` built by `settings_state._auto_accept_state_from_rows(org_rules.rows(), usage, resolve_value=None, resolver=None)` with `usage = AuditLogger(<audit dir>).rule_usage(source="org_rule")`, where the audit directory is built exactly as `settings_state._rule_usage_map()` builds it (read that function, copy its path expression into a private helper in `routes_admin.py`; do not edit `settings_state.py`).
      5. Tests: each action applies, writes the config, reloads `org_rules`, writes an audit row with the right decision and the sentence; a non-admin gets 403; a missing or unknown field is refused; the passkey is required (`TestStepUp` pattern); `TestRouteClassification` still passes; `_admin_state` has the `org_rules` key with `match_count` from `org_rule` audit rows.
      Stop condition: if `_record_settings_audit`'s `AuditEntry` fields cannot be reproduced from this module's imports, stop with status=blocked.
    acceptance:
      - "`python3 -m pytest tests/unit/web/test_routes_admin.py -q` passes"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` pass"
  - id: p7-admin-and-person-ui
    title: The Organization rules section and the read-only list on the Auto-accept page
    depends_on: [p5-person-and-ai-surfaces, p6-admin-backend]
    complexity: M
    worker_level: strong
    worker_level_reason: The Admin section must share the Auto-accept page's add-rule controls inside one large inline JS string; deciding how to share them without breaking either page needs judgement.
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_settings_org_rules_browser.py
    brief: |
      Read first: plan section 3.5; settings_window_html.py (`renderAutoAccept` :786, `editVerbsHtml`, `submitAddPolicyRule` :2988, `ADMIN_NAV_ITEMS` :2002, `adminSection` :2647, `renderUpstreams` :2587 as the pattern for an Admin section, `_admin_capabilities` :3163, `_admin_section_defaults` :3138); the design rules in docs/coding-and-testing-guidelines.md section 1.10 (tokens only, no width media queries, no colour literals); tests/integration/test_settings_edit_verbs_browser.py as the browser test pattern.
      1. Person page: in `renderAutoAccept(state)`, after the person's rules, when `state.org_rules` is non-empty render a block with the heading `Set by your organization`, one line per rule (its `sentence`), and the text `Only an administrator can change these.`; no buttons or links. Use existing CSS classes of the rule list; a new colour is not needed.
      2. Admin page: add `{key: 'org_rules', label: 'Organization rules'}` to `ADMIN_NAV_ITEMS` after `tokens`; add `org_rules` to `_admin_capabilities` and `_admin_section_defaults` the way `upstreams` is; add `renderOrgRules(state)` to the `adminSection` switch. It shows the warning text of plan 3.5, the list from `state.org_rules.rules` (sentence, verbs, `Matched Nx` and the never-matched line as the Auto-accept page shows them, `Edit verbs`, `Remove`) and the add-rule controls.
      3. Reuse, do not copy, the Auto-accept page's add-rule and edit-verbs markup and handlers: parameterize them by a state key prefix and an action-name triple (`add_policy_rule`/`remove_policy_rule`/`set_policy_rule_verbs` for the person page, `add_org_rule`/`remove_org_rule`/`set_org_rule_verbs` for the admin section). The Admin actions take `verbs` as one comma-separated string; the person actions take a list: build the string with `.join(',')` in the org branch only. The Admin posts go through the existing admin `post(...)` path that carries the passkey assertion.
      4. Tests: `tests/unit/test_settings_window_html.py` new class `TestOrgRulesUi` (the nav item exists in the admin surface only, the person surface shows the block only when `org_rules` is non-empty, the admin section renders the warning text and the three action names). Browser test `tests/integration/test_settings_org_rules_browser.py` (marker `browser`): on the admin surface add a Drive-folder read rule through the form with the passkey step stubbed the way the existing admin browser tests stub it, see the rule listed; on the person surface see the read-only block with no Remove button; add a phone-width case per section 2.10 of the guidelines.
      Stop condition: if the add-rule and edit-verbs markup cannot be shared with fewer than about 60 changed lines in `renderAutoAccept`, stop with status=blocked and describe what blocks sharing. No browser is available in the worker's container: run the unit tests, and leave the browser test to CI (say so in the report).
    acceptance:
      - "`python3 -m pytest tests/unit/test_settings_window_html.py -q` passes, including `TestOrgRulesUi`"
      - "`python3 -m pytest tests/unit/test_design_system.py -q` passes (no colour literals, no width media queries)"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` pass"
  - id: p8-token-copy
    title: A new scoped token can copy an organization rule
    depends_on: [p7-admin-and-person-ui]
    complexity: S
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/web/server.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/web/test_server.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read first: plan section 3.5 (Scoped tokens); scoped_tokens.py (`_prepare` :~790-830, `_check_locked` :~840-870); web/server.py :~264 (the token page data that lists the person's rows); settings_window_html.py :~1925-1960 (`submitScopedToken`, the copy checkboxes `ui.stRules`).
      1. `_prepare`: when any entry is `copy`, build `own_rows` from `org_rules.rows()` first and then overwrite with `auto_accept.get_policy_rows()`, so the person's row wins on an id collision. The refusal text for a missing id stays.
      2. `web/server.py`: where the token page data lists the person's rows for copying, append each org row whose id is not already listed, with an added key `"organization": True`.
      3. `settings_window_html.py`: in the token form's copy list show the text `(set by your organization)` after the sentence of a row with `organization` true. The post body does not change.
      4. Tests: `tests/unit/test_scoped_tokens.py` new `TestCopyOrgRule` (an org row copies into the token's own rules as an exact copy; a person row with the same id wins; an unknown id is refused; a token holding the copy never gains later org rule changes: remove the org row and the token's rule stays); `tests/unit/web/test_server.py` (`TestScopedTokenPageData`: org rows listed once with the flag); one render test in `test_settings_window_html.py`.
    acceptance:
      - "`python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/web/test_server.py tests/unit/test_settings_window_html.py -q` passes"
      - "`ruff check .` and `python3 -m mypy src/privacyfence` pass"
  - id: p9-retire-docs-adrs
    title: ADRs, reference docs and the changelog
    depends_on: [p8-token-copy]
    complexity: S
    touches:
      - docs/adr/0333-organization-auto-accept-rules-are-set-by-an-administrator.md
      - docs/adr/0334-the-audit-entry-names-who-allowed-a-call.md
      - docs/adr/README.md
      - docs/approvals-and-policy.md
      - docs/security-and-compliance.md
      - docs/org-mode-setup-guide.md
      - docs/configuration-reference.md
      - docs/how-it-works.md
      - CHANGELOG.md
    brief: |
      Read first: docs/adr/README.md (template, rules 1-7); docs/adr/0236-a-scoped-token-holds-its-own-rules.md as a style example; plan sections 3 and 4; docs/coding-and-testing-guidelines.md section 4 (standing docs carry no history).
      1. Write ADR 0333 and ADR 0334 from plan sections 3.1-3.6 and 4, following the template (Status `Accepted — 2026-11-20. Implemented.`, Context, Decision, Alternatives considered, Consequences, Verification naming the test classes of p1 to p8, Related); link issue https://github.com/privacyfence/privacyfence/issues/1167 and `docs/1167/plan.md`. ADR 0333 states the owner decisions of plan section 1 and the Drive tag residual of ADR 0321.
      2. `docs/adr/README.md`: add rows 0333 and 0334 at the end of the index (format of the neighbouring rows), and append `; amended by 0333` is NOT needed to older ADRs (they are frozen).
      3. `docs/approvals-and-policy.md`: in `## How requests are gated` item 1 mention that an always-allow rule can be a person's own or an organization's; in `### Where rules are stored` add a paragraph on organization rules (stored in `org/admin_config.json`, set on Admin, checked after the person's rules, not under scoped tokens, read-only to people and AI clients); add `Allowed By` to the audit description there if it names audit fields.
      4. `docs/security-and-compliance.md`: in `## Audit log integrity` add the decisions `org_rule_added`, `org_rule_removed`, `org_rule_verbs_changed` to the `decision` list and describe the `allowed_by` field and its four values and the `Allowed By` column.
      5. `docs/org-mode-setup-guide.md`: in `### The Admin page` add the `Organization rules` row to the sections table and one paragraph (what it does, passkey, the Drive tag warning); in `### Install-wide and per-user policy` add one line that org rules apply to every person's own calls and not to scoped tokens.
      6. `docs/configuration-reference.md`: in `### Admin config (org/admin_config.json)` add the key `org_rules` (list of rule rows, written by the Admin page, at most 100).
      7. `docs/how-it-works.md`: where the audit log fields are described, add `allowed_by` in one sentence.
      8. `CHANGELOG.md` under `## [Unreleased]`: one line `Administrators can set organization auto-accept rules on the Admin page; the audit log and its Excel export now say whether a person, their own rule, an organization rule or a scoped token's rule allowed each call (new Allowed By column).` Do not open a version heading.
      9. Run `python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py -q`; fix wording until they pass. Do not rename any heading that other docs link to.
    acceptance:
      - "`python3 -m pytest tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py tests/unit/test_docs_references_exist.py tests/unit/test_website_docs_allowlist.py -q` passes"
      - "`ls docs/adr/0333-*.md docs/adr/0334-*.md` lists both and `grep -c '0333\\|0334' docs/adr/README.md` prints 2 or more"
      - "`grep -n 'organization auto-accept rules' CHANGELOG.md` prints a line under `## [Unreleased]`"
```
