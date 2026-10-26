# Plan: a scoped token holds its own rules

## Goal

Today a scoped token names a subset of the person's Auto-accept rules by id
([ADR 0164](adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)).
To give a scheduled job (a `privacyfence-sync` recipe, or a Claude Code routine whose own
environment reaches `/mcp` with a Bearer token) an auto-accept, the person has to add an
Auto-accept rule that then also auto-accepts the same calls in every interactive chat. After this
change a token **holds its own copy of its rules**. On the New token form the person adds rules for
that token only, with the same scope, value and verb controls as the Auto-accept page, or copies
one of their Auto-accept rules (a copy, not a reference). A scoped call never consults the person's
Auto-accept rules, and changing or deleting an Auto-accept rule no longer changes any token.
Existing tokens are converted once, at start-up: each gets its own copy of the rules it named, with
its recorded limits applied. Rules named by an id that no longer exists are left out. Everything
else about tokens stays: deny-only, no cards, `denied_scoped_token`, expiry, the tool list, both
personal-data flags, the audit identity and the step-up check, whose fingerprint now covers the
rules.

## Current state

- **Store.** `src/privacyfence/scoped_tokens.py:64` `ScopedToken` carries `rule_ids`,
  `rule_limits` (per rule id, the `(predicate, operation)` pairs it may use) and
  `limits_split_verbs`. `_from_json` (`:230`) applies `_LIMIT_PAIR_SUCCESSORS` (`:57`, the ADR 0228
  predicate split) to tokens without `limits_split_verbs`. `create` (`:424`) checks the ids
  against `auto_accept.get_policy_v2_store_rules()` and records their pairs as limits. The file is
  `{"version": 1, "tokens": [...]}` under `authority_dir(principal)/scoped_tokens.json`.
- **v2→v3 re-pointing.** `_migrated` (`:251`), `migrate_token_file` (`:277`),
  `ScopedTokenStore.apply_migration` (`:540`) and `migrate_tokens` (`:679`) re-point tokens from
  v2 rule ids to v3 row ids. They are called from `policy_upgrade.ensure_scope_rows`
  (`src/privacyfence/policy_upgrade.py:39`, `:70`), which runs from `daemon_main.load_config`
  (`daemon_main.py:411`), `daemon_main._upgrade_all_principals` (`:427`), every Auto-accept writer
  (`auto_accept._read_rows_locked`, `auto_accept.py:594`) and
  `SettingsController._load_rows` (`settings_controller.py:1949`).
- **ADR 0198 pruning.** `_without_rules` (`:308`), `forget_rules_in_file` (`:320`),
  `ScopedTokenStore.forget_rules` (`:558`) and the module `forget_rules` (`:688`) drop a deleted
  rule's id from tokens. They are called through `auto_accept.forget_removed_rules`
  (`auto_accept.py:682`) from `auto_accept.remove_policy_v2_rule` (`:698`, with its
  `forget_in_tokens` parameter), `SettingsController.remove_policy_rule`
  (`settings_controller.py:1908`) and `gate.propose_policy_change` (`gate.py:1643`–`1647`).
- **Gate.** `gate._rules_in_scope` (`gate.py:728`) filters `get_policy_v2_store_rules()` to the
  token's ids and narrows each one by `rule_limits`. `_evaluate_auto_accept` (`:700`),
  `rule_values` (`:750`), `_personal_data_rule` (`:767`), `preflight_auto_accept` (`:796`, which
  backs `privacyfence_check_policy`) and `_assets_schema_detail` (`:1703`) all read through it.
- **Settings.** `web/server.py:353` `scoped_token_page_data` builds `window.__pfScopedTokens`. Each
  token's rules come from `_token_rule_entry` (`:337`), with `missing` for an id that is gone and
  `limited_to` (`_limited_tools`, `:326`) for a narrowed rule. `rules` lists the person's
  Auto-accept rows as chips. The create request is `{label, days, rule_ids, tools,
  allow_personal_data, allow_personal_data_reads}`. Its fingerprint is
  `routes_settings._scoped_create_fingerprint_body` (`routes_settings.py:368`), shared by the local
  route (`:1208`) and the org route (`:1828`). `ScopedTokenWiring.store_create` (`:348`) is the
  local seam, and `server.build_scoped_token_wiring` (`server.py:409`) implements it. The form
  itself is `renderScopedTokens` / `submitScopedToken` in `settings_window_html.py:1438`–`1715`.
  Both modes use that page.
- **Status.** `privacyfence_status` under a token reports `"rules": len(scope.rule_ids)`
  (`web/routes_mcp.py:645`). The create audit summaries count `len(token.rule_ids)`
  (`routes_settings.py:1238`, `:1854`).
- **Usage stats.** `AuditLogger.rule_usage` (`audit_log.py:982`) counts every `auto_accepted`
  entry by `rule_id`, including calls made with a scoped token.
- **Import cycle.** `scope_rows` → `catalogue` → `store` → `engine` → `auto_accept`, and
  `policy_upgrade` → `scoped_tokens` → `auto_accept`. That is why `auto_accept` imports
  `scope_rows` inside its functions (`auto_accept.py:599`–`602`). `scoped_tokens` must do the same.
- Tests that build a `ScopedToken` or call `create`/the forget/migrate functions: about 190
  references across `tests/unit/test_scoped_tokens.py` (111), `test_gate.py` (28),
  `test_auto_accept.py` (10), `web/test_routes_org_settings.py` (9), `web/test_server.py` (8),
  `web/test_routes_settings.py` (7), `test_policy_upgrade.py` (6), `test_settings_controller.py`
  (5), and one or two each in `web/test_mcp_dispatch.py`, `web/test_mcp_auth.py`,
  `web/test_routes_mcp.py`, `web/test_routes_mcp_agent_identity.py`, `web/test_oauth_provider.py`,
  `connectors/test_assets_connector.py` and `test_daemon_main.py`.

## Design

### D1. Token rules are `ScopeRow`s

A token stores its rules as `policy.scope_rows.ScopeRow`s, written with `scope_rows.row_to_dict`
and read with `scope_rows.row_from_dict`, the same format as the `auto_accept:` section's rows.
A scoped row's predicates come from the catalogue when it is compiled, so a later catalogue change
that `compile_row` already handles for Auto-accept rows (such as ADR 0228's per-verb predicates)
reaches token rows the same way. The stored `operations` of a row bound it, so a verb that later
gains a tool does not widen a token (ADR 0166). A row whose dict no longer parses (a retired scope
id) is dropped from that token with a warning. Dropping is fail-closed, because it only ever
narrows the token. A change to the row format that needs a rewrite on disk must rewrite token rows
too. The `policy_upgrade` module docstring and the ADR say so.

Rejected: storing compiled `PolicyRule`s (`store.rule_to_dict`). A predicate rename would then need
its own token rewrite, as `_LIMIT_PAIR_SUCCESSORS` did. Also rejected: keeping ids plus a pinned
copy, which brings back two sources of truth.

### D2. `ScopedToken` fields

Remove `rule_ids`, `rule_limits` and `limits_split_verbs`. After `tools`, the fields are:

```python
allow_personal_data: bool = False
allow_personal_data_reads: bool = False
# The token's own rules (ADR 0234), in the auto_accept: row format.
rules: tuple["ScopeRow", ...] = field(default=(), hash=False)
# The stored entry of a token written before tokens held their own rules, kept as read until it
# is converted (``own_legacy_rules``); such a token has no rules meanwhile.
legacy_raw: dict[str, Any] | None = field(default=None, compare=False, hash=False, repr=False)
```

Add a method:

```python
def compiled_rules(self) -> list["PolicyRule"]:
    """The engine rules of the token's own rows (``scope_rows.compile_rows``)."""
    from .policy import scope_rows
    return scope_rows.compile_rows(self.rules)
```

`ScopeRow` and `PolicyRule` are imported under `TYPE_CHECKING` only. Every runtime use of
`scope_rows` inside `scoped_tokens.py` is a function-local import (see "Import cycle" above).

### D3. Store format

- The file becomes `{"version": 2, "tokens": [...]}`. `_write` and every file-level writer write
  version 2. Reading ignores `version`, as today.
- A current entry has `"rules": [row_to_dict(row), ...]` and no `rule_ids`, `rule_limits` or
  `limits_split_verbs`. `_to_json` writes the other keys exactly as today.
- `_from_json`:
  - With a `"rules"` key: it must be a list, otherwise `TypeError` and the entry is skipped. Each
    item goes through `scope_rows.row_from_dict`. A `None` result logs
    `logger.warning("Scoped token %s: skipping a rule that cannot be read", raw["id"])` and is left
    out. The result has `legacy_raw=None`.
  - Without `"rules"` (a legacy entry): `raw["rule_ids"]` must be a list (a `KeyError` or
    `TypeError` skips the entry, as today). The token gets `rules=()` and `legacy_raw=dict(raw)`.
    Its other fields are read as today.
- `_to_json` of a token with `legacy_raw` returns `{**token.legacy_raw, "label": token.label}`, so
  a rename, or a write caused by another token, keeps the legacy entry convertible.
- In `ScopedTokenStore._reload`, each token with `legacy_raw` logs once per reload:
  `logger.warning("Scoped token %s was created before tokens held their own rules; it allows no
  rule-covered call until PrivacyFence converts it at the next start", token.id)`.
- An older PrivacyFence reading a version-2 entry gets a `KeyError` on `rule_ids` and skips it.
  Such a token fails closed on a downgrade. The ADR and the changelog say so.

### D4. Exact copies: `scope_rows.rows_for_rules`

New public pure function in `src/privacyfence/policy/scope_rows.py` (add it to `__all__`):

```python
def rows_for_rules(rules: Sequence[PolicyRule], row_id: str) -> list[ScopeRow]:
    """Rows that compile to exactly ``rules`` -- the same predicates, conditions, values and
    operations -- all carrying ``row_id``: the rows ``add_rules`` builds when they compile back
    to exactly that, else one custom row per rule. ``[]`` for no rules. A scoped token's own copy
    of a rule is built with it (ADR 0234)."""
```

Implementation:
1. `if not rules: return []`.
2. `merged, _ = add_rules([], list(rules))`, then
   `candidate = [dataclasses.replace(row, id=row_id, former_ids=()) for row in merged]`.
3. Return `candidate` when `_quads(compile_rows(candidate)) == _quads(rules)`. Otherwise return
   `[_custom_row(rule, row_id) for rule in rules]`.
4. Use this private helper:

   ```python
   def _quads(rules: Iterable[PolicyRule]) -> frozenset[tuple[str, str, str, str]]:
       return frozenset(
           (r.predicate, repr(r.conditions), repr(store._sortable(r.value)), op)
           for r in rules for op in r.operations
       )
   ```

### D5. Converting a legacy entry: `scoped_tokens.owned_entry`

Pure, in `scoped_tokens.py`:

```python
_LEGACY_KEYS = ("rule_ids", "rule_limits", "limits_split_verbs")


@dataclass(frozen=True)
class OwnedEntry:
    entry: dict[str, Any]
    newly_flagged: bool  # allow_personal_data newly set (ADR 0173's transfer from a v2 rule)
    copied: int  # rule ids that named a rule and were copied
    missing: int  # rule ids that named no rule any more, or whose limit left nothing


def owned_entry(
    raw: Any, compiled: Sequence["PolicyRule"], personal_data_ids: frozenset[str] = frozenset(),
) -> OwnedEntry | None:
    """A legacy token entry (``rule_ids``, no ``rules``) with its own copy of each rule it named,
    narrowed to its ``rule_limits``; ``None`` for an entry that already holds its rules. Raises
    ``KeyError``, ``TypeError`` or ``ValueError`` for an entry that cannot be read."""
```

Steps:
1. `raw` not a dict raises `TypeError("not a token entry")`. `"rules" in raw` returns `None`.
2. `ids = raw["rule_ids"]` must be a list of `str`, otherwise `TypeError`.
3. `limits = _limits_from_json(raw.get("rule_limits", {}))`. Unless
   `raw.get("limits_split_verbs") is True`, apply `_with_limit_successors(limits)`. Both helpers
   stay as they are, used only here now.
4. `rows = []`, `copied = missing = 0`. For each `rid` in `sorted(set(ids))`:
   - `matching = [r for r in compiled if r.id == rid]`.
   - If `limits.get(rid)` is a set `limit`:
     `matching = [replace(r, operations=frozenset(op for op in r.operations if (r.predicate, op) in limit)) for r in matching]`,
     then keep only rules with operations.
   - If nothing is left: `missing += 1`. Otherwise
     `rows.extend(scope_rows.rows_for_rules(matching, rid))` and `copied += 1`.
5. `entry = {k: v for k, v in raw.items() if k not in _LEGACY_KEYS}` and
   `entry["rules"] = [scope_rows.row_to_dict(row) for row in rows]`.
6. `newly_flagged = raw.get("allow_personal_data") is not True and bool(set(ids) & personal_data_ids)`.
   When it is true, set `entry["allow_personal_data"] = True`.
7. Return `OwnedEntry(entry, newly_flagged, copied, missing)`.

`compiled` holds the person's rules at upgrade time. For a version-3 section that is
`scope_rows.compile_rows(rows)`, whose rules carry row ids. For a version-2 section it is the v2
rules (`store.rule_from_dict` of each raw rule), whose ids are the v2 ids a pre-v3 token names.
Either way the token's ids match `compiled`'s ids, so the v2→v3 re-pointing
(`_migrated` and the rest) is no longer needed.

### D6. Running the conversion

In `scoped_tokens.py`:

- `own_rules_in_file(path: Path, compiled, personal_data_ids=frozenset()) -> list[str]`. It reads
  the file. A missing or unreadable file returns `[]`. Each entry goes through `owned_entry`:
  - An entry that raises `KeyError`, `TypeError` or `ValueError` is kept as it is, with
    `logger.warning("Keeping an unreadable scoped token entry as it is")`.
  - `None` is kept as it is.
  - Otherwise the new entry replaces it, and
    `logger.info("Scoped token %r now holds its own copy of %d rule(s); %d rule(s) it named no longer existed", label, copied, missing)`
    is logged.

  It writes `{"version": 2, "tokens": out}` only when something changed, and returns the labels of
  the newly flagged entries. An `OSError` on write propagates.
- `ScopedTokenStore.own_legacy_rules(principal, compiled, personal_data_ids=frozenset()) -> list[str]`.
  It takes the store lock and calls `_refresh(principal)`. With no token having `legacy_raw`, it
  returns `[]` and writes nothing. Otherwise it applies `owned_entry({**t.legacy_raw, "label": t.label}, ...)`
  to each such token and rebuilds the token with `_from_json(principal.id, owned.entry)`. A token
  that raises is kept as it is, with the warning above. Then it replaces the list, calls `_write`,
  logs the same info line per token, and returns the newly flagged labels.
- The module-level `own_legacy_rules(principal, compiled, personal_data_ids=frozenset())` goes
  through the live store when `current_store()` is set, else through `own_rules_in_file` on
  `paths.authority_dir(principal) / STORE_FILE_NAME`. This mirrors today's `migrate_tokens`.
- Remove `_migrated`, `migrate_token_file`, `ScopedTokenStore.apply_migration`, `migrate_tokens`
  and the `V2Migration` `TYPE_CHECKING` import. `scope_rows.V2Migration` keeps its fields;
  `personal_data_ids` is still used.

In `policy_upgrade.ensure_scope_rows`:

- **Not version 2** (`raw_rules is None`): call
  `scoped_tokens.own_legacy_rules(principal, scope_rows.compile_rows(scope_rows.rows_from_config(cfg)))`
  inside `try`. On `OSError` call
  `log(f"{principal.id}: could not give scoped tokens their own copy of their rules: {exc}")`.
  Return `True` either way. A failed conversion leaves legacy tokens without rules, which is
  fail-closed, and must not block rule writes.
- **Version 2:** replace the `scoped_tokens.migrate_tokens(principal, migration)` call with
  `scoped_tokens.own_legacy_rules(principal, v2_rules, migration.personal_data_ids)`, where
  `v2_rules = [r for r in (policy_store.rule_from_dict(raw) for raw in raw_rules) if r is not None]`.
  It goes in the same `try`, with the same failure message and `return False`. The rest of the
  function stays, including the personal-data label log line.
- Add one paragraph to the module docstring: "A scoped token holds its own copy of its rules in
  the same row format (ADR 0234). A later change to that format that needs a rewrite on disk must
  rewrite each token's `rules` too, the way `own_legacy_rules` converts a token's old rule ids."

In `daemon_main._upgrade_all_principals` (`daemon_main.py:427`): for a principal without a
settings file, call `scoped_tokens.own_legacy_rules(principal, [])` instead of `continue`. On
`OSError` append
`f"{principal.id}: could not give scoped tokens their own copy of their rules: {exc}"` to
`_deferred_warnings`, then `continue`. That principal has no rules, so its legacy tokens get none,
and the warning stops repeating.

### D7. Gate

```python
def _rules_in_scope(scope: ScopedToken | None) -> list[policy_engine.PolicyRule]:
    """Every cached rule for an unscoped call; under a scoped token only the token's own rules
    (ADR 0164, ADR 0234), never the person's Auto-accept rules."""
    if scope is None:
        return get_policy_v2_store_rules()
    return scope.compiled_rules()
```

Nothing else in `gate.py` changes for this. `REVOKED_SCOPE.compiled_rules()` is `[]`.

### D8. ADR 0198 pruning is removed

Remove `scoped_tokens._without_rules`, `forget_rules_in_file`, `ScopedTokenStore.forget_rules`,
the module `forget_rules`, `auto_accept.forget_removed_rules` (and its `__all__` entry), and the
`forget_in_tokens` parameter of `auto_accept.remove_policy_v2_rule`. Remove the call in
`SettingsController.remove_policy_rule` and the import. In `gate.propose_policy_change` the
`remove`/`update` branch becomes:

```python
    if operation == "remove":
        changed = remove_policy_v2_rule(rule_id)
        affected_ids = [rule_id]
    else:
        removed = remove_policy_v2_rule(rule_id) if operation == "update" else False
        row_ids = policy_row_ids_for(new_rules)
        added = add_policy_v2_rules(new_rules)
        changed = removed or added
        affected_ids = row_ids
```

Remove the `forget_removed_rules` import from `gate.py`. Deleting an Auto-accept rule writes
nothing to `scoped_tokens.json`.

### D9. Usage stats leave scoped-token calls out

In `AuditLogger.rule_usage`, skip an entry whose
`str(data.get("agent_id") or "").startswith("scoped-token:")`. The Auto-accept page's "Matched Nx"
then counts only the calls its rules really let through. A copied token rule keeps the row's id,
and its calls would otherwise count for the person's rule. Add one sentence to the docstring:
"Calls made with a scoped token are left out: they match the token's own rules, never the
person's (ADR 0234)."

### D10. Creating a token from rule requests

**Request.** The create body's `rule_ids` is replaced by `rules`, a list of entries of exactly one
of these shapes:

| `kind` | Other keys | Meaning |
|---|---|---|
| `"new"` | `group: str`, `value: str`, `verbs: [str]` | A rule built like the Auto-accept page's Add rule |
| `"copy"` | `rule_id: str` | An exact copy of one of the person's Auto-accept rows |
| `"duplicate"` | `token_id: str`, `rule_id: str` | An exact copy of a row of one of the person's tokens (the Duplicate button) |

**Normalising.** `scoped_tokens.normalise_rule_requests(raw: Any) -> list[dict[str, Any]] | None`:
- Return `None` unless `raw` is a list and every item is a dict whose key set is exactly that
  kind's keys plus `kind`, with each value of the type in the table.
- Each `"new"` entry's `verbs` become `sorted(set(verbs))`.
- Duplicates (same `json.dumps(entry, sort_keys=True)`) are dropped.
- The result is sorted by that JSON string.

`routes_settings._scoped_create_fingerprint_body` uses it. Its body is
`{"label", "days", "rules", "tools", "allow_personal_data", "allow_personal_data_reads"}`, and a
`None` from `normalise_rule_requests` returns `None` (400 `invalid request`). A body that still
sends `rule_ids` and no `rules` is therefore refused. The fingerprint covers every rule request,
which is what the passkey confirms.

**`ScopedTokenStore.create`** takes `rules: Sequence[dict[str, Any]]` (normalised requests) in
place of `rule_ids`. Steps, in this order:

1. The checks that need no lock stay as today: `policy.enabled`, then `LABEL_RE`.
2. `if len(rules) > MAX_RULES_PER_TOKEN: raise ScopedTokenError(f"A token has at most {MAX_RULES_PER_TOKEN} rules.")`,
   with the new constant `MAX_RULES_PER_TOKEN = 50`.
3. **Before taking the store lock**: when any entry is `"copy"`, read
   `own_rows = {row.id: row for row in auto_accept.get_policy_rows()}`, otherwise `{}`. Build the
   `"new"` entries with function-local imports of `policy.catalogue` and `policy.scope_rows`:
   - `verbs = catalogue.parse_verbs(entry["verbs"])` and
     `value = catalogue.parse_value_list(entry["value"], entry["group"])`.
   - `built = catalogue.rules_for_catalogue_entry(entry["group"], value, verbs)`. A
     `value_format.RuleValueError` becomes `ScopedTokenError(str(exc))`.
   - An empty `built` raises
     `ScopedTokenError("A new rule needs a scope, at least one of its verbs, and a value if the scope takes one.")`.
   - Collect every `built` list, in request order, into `new_rules`.

   `get_policy_rows` takes the Auto-accept write lock. `ensure_scope_rows` takes that lock and
   then the store lock, so taking them in the other order could deadlock.
4. Inside the lock, the checks run in today's order: duplicate label, days, then
   "Choose at least one tool for the token." (now `if not rules and not tool_set`), then the tool
   names. Then resolve the base rows, in request order:
   - A `"copy"` takes `own_rows[rule_id]`.
   - A `"duplicate"` takes the row with that id from the principal's token with `token_id`.
   - A missing one raises `ScopedTokenError("One of the chosen rules no longer exists; reload the page.")`.
   - Each base row gets `dataclasses.replace(row, former_ids=())`. Rows with an id already taken
     are skipped (the first one wins).

   Then the `MAX_TOKENS_PER_PRINCIPAL` check, as today.
5. `token_rows, _ = scope_rows.add_rules(base_rows, new_rules)`. The token gets
   `rules=tuple(token_rows)`.

**Value parsing.** `settings_controller._parse_value_list` (`settings_controller.py:356`) moves
unchanged to `policy/catalogue.py` as `parse_value_list(raw_text: str, group: str = "") -> list[str] | None`.
`settings_controller.py` keeps `_parse_value_list = policy_catalogue.parse_value_list` in its
alias block (`:266`–`269`), so `routes_settings.py:161` and the existing tests keep working.

**Wiring.** `ScopedTokenWiring.store_create` becomes
`Callable[[str, int, list[dict[str, Any]], list[str], bool, bool], tuple[ScopedToken, str]]`, and
the local route passes `bound["rules"]`. The org route passes `rules=bound["rules"]`.
`server.build_scoped_token_wiring._create` takes `rules: list[dict[str, Any]]`.

### D11. Settings page data

In `server.scoped_token_page_data`:

- Each token's `"rules"` is
  `[_scoped_rule_entry(row, cached_row_value(row, resolver), row_value_items(row, resolver)) for row in token.rules]`,
  built inside the `with principal_scope(principal):` block. `_token_rule_entry` and
  `_limited_tools` are removed, and with them the `missing` and `limited_to` keys.
- `"rules"` (top level) stays: the person's Auto-accept rows, now offered as copies.
- New `"scope_groups"`: `policy.catalogue.scope_catalogue()` entries whose `"connector"` is in
  `connector_names`. Each gets one more key, `"verb_tools": {verb: [tool, ...]}`, from:

  ```python
  def _verb_tools(group_id: str, verbs: list[str]) -> dict[str, list[str]]:
      tools: dict[str, list[str]] = {}
      for name in verbs:
          verb = Verb(name)
          row = scope_rows.ScopeRow(
              id="", scope=group_id, value=None, verbs=frozenset({verb}),
              operations=scope_rows.implied_operations(group_id, [verb]),
          )
          tools[name] = sorted({t for rule in scope_rows.compile_row(row) for t in describe.covered_tools(rule)})
      return tools
  ```

### D12. The New token form (`settings_window_html.py`, both modes)

New `ui` state, next to the existing `st*` keys:
`stNewGroup: null, stNewValue: '', stNewVerbs: {}, stNewRules: [], stDupRules: {}`.
`ui.stRules` keeps meaning "Auto-accept rows to copy".

The **Rules** block of the form, in this order:

1. Hint (replaces the current one): `Rules belong to this token only: they never auto-accept anything for your other AI clients, and changing or deleting an Auto-accept rule later does not change the token. A tool that needs approval works only where one of the token's rules covers it.`
2. Sub-title `New rule for this token` (class `pf-hint`):
   - A `<select class="field" data-st-new-group="1">` of `SCOPED.scope_groups` (label text = `g.label`).
   - The value input, shown when `group.needs_value || group.value_optional`, with the same
     placeholder (`group.value_hint`) and aria-label as the Auto-accept page's value field, and
     `data-st-new-value="1"`.
   - One verb chip per `group.verbs` (`role="checkbox"`, `data-st-new-verb="<verb>"`).
   - The pair hint, from the shared `pairHint` below.
   - The button `Add rule to token` (`data-st-new-add="1"`). It has `aria-disabled="true"`, with no
     `tabindex`, while no verb is ticked, while the pair hint is invalid, or while
     `group.needs_value` and the value is blank.

   When `SCOPED.scope_groups` is empty, show only the hint
   `No connector is enabled, so there is no scope to make a rule for.`
3. Every added new rule, as `<ul class="pf-st-list" data-st-new-list="1">`. Each `<li>` shows
   `<group label>: <value, or "anything"> · allows <verbs joined by ", ">`, then a link
   `Remove` (`data-st-new-remove="<index>"`).
4. When duplicating (`ui.stDuplicateOf` set): sub-title `From <token label>`, then one chip per
   rule of that token (`data-st-dup-rule="<row id>"`, checked from `ui.stDupRules`, text
   `withValuesHtml(r.sentence_parts, r.value_items, r.sentence)`). `duplicateScopedToken` ticks
   all of them. It no longer touches `ui.stRules`.
5. Sub-title `Copy from my Auto-accept rules`. Then the existing rule chips (`data-st-rule`) and
   one hint: `A copy: later changes to the Auto-accept rule do not reach the token.` When
   `SCOPED.rules` is empty, the hint is `You have no Auto-accept rules to copy.`

Remove the `data-st-dup-missing` and `data-st-dup-limited` notices. The token list's detail
renders each rule as `withValuesHtml(r.sentence_parts, r.value_items, r.sentence)`, without the
"a rule that no longer exists" and "(limited to: …)" branches.

**Pair hint.** Refactor `aaPairHint(group)` into `pairHint(group, checkedVerbs, value)` with the
same body (reading the arguments in place of `ui.aaCheckedVerbs`/`ui.aaValue`).
`aaPairHint(group)` becomes `return pairHint(group, ui.aaCheckedVerbs, ui.aaValue);`.

**Add rule to token** pushes
`{group: ui.stNewGroup, label: <group label>, value: <value field text>, verbs: <ticked verbs in group.verbs order>}`
onto `ui.stNewRules`, then clears `stNewValue` and `stNewVerbs`. Changing the select sets
`stNewGroup` and clears `stNewValue` and `stNewVerbs`.

**Coverage.** `scopedToolCoverage()` adds, per tool:
- the titles of ticked copy chips (as today);
- for each ticked `ui.stDupRules` row of the duplicated token, its `title` (falling back to
  `sentence`) over its `tools`;
- for each `ui.stNewRules` entry, its `label` over the union of `group.verb_tools[verb]` for its
  verbs.

`anyRule` is true when any of the three is non-empty. **Add the tools these rules cover** uses the
same three sources.

**Submit.** `submitScopedToken` posts
`rules: [...new.map({kind: 'new', group, value, verbs}), ...ticked copy ids.map({kind: 'copy', rule_id}), ...ticked dup ids.map({kind: 'duplicate', token_id: ui.stDuplicateOf, rule_id})]`
in place of `rule_ids`. `clearScopedForm` also resets the five new `ui` keys.

### D13. Strings outside Settings

- `privacyfence-sync` `permissions.py`: `_NO_SUGGESTION` becomes
  `"NEEDS A RULE: PrivacyFence suggests no rule for this call; add one to the recipe's scoped token in Settings -> Scoped tokens (or in Settings -> Auto-accept for an attended run), or run once with --attended"`.
  `_NO_SUGGESTION_KEY` becomes
  `"NEEDS A RULE: this PrivacyFence does not suggest one; update it, or run once with --attended and use the card's Always allow (a scoped token needs the rule on the token itself)"`.
- Gate denial texts stay unchanged
  (`"Request denied: this call uses a scoped token, and none of the token's rules covers it."`).

### What does not change

The deny-only path (`_deny_scoped`, `audit_scoped_refusal`), the tool list and its check in
`routes_mcp`, expiry, revoke/rename/revoke-all, `find`, activity (ADR 0206), both personal-data
flags and `_pii_gate`, the agent identity `scoped-token:<id>`, org opt-in and `max_days`, and
`privacyfence_check_policy`. That tool already predicts through `_rules_in_scope`, so under a token
it now predicts from the token's rows alone.

## ADRs

- **ADR 0234: a scoped token holds its own rules.** A token stores its own copy of its rules in
  the `auto_accept:` row format and never consults the person's Auto-accept rules. Rules are
  authored on the New token form or copied from an Auto-accept rule. Existing tokens are converted
  once at start-up with their limits applied. Deleting an Auto-accept rule no longer touches
  tokens. Scoped-token calls leave the Auto-accept usage counts alone. A version-2 token file is
  unreadable to older versions. It amends ADR 0164, ADR 0220 and ADR 0173 (where the flag transfer
  now runs), and supersedes ADR 0198.
  (Use the next free number in `docs/adr/` when the last phase runs. If 0234 is taken by then, use
  the next one and use it consistently.)

## Manual steps

None before implementation. After the last phase, two checks on the maintainer's own install (see
the [step-by-step page](https://claude.ai/artifact/LTb2w5jrVYFQ7vaBp1sLX1)):

- `ma1-existing-token-survives-upgrade`: an existing token in real use (a `privacyfence-sync`
  recipe or a Claude Code routine) still works after installing the build, and Settings lists its
  rules.
- `ma2-token-only-rule-stays-in-the-token`: a token with a rule added on the New token form
  auto-accepts that call, while an ordinary Claude session making the same call still gets an
  approval card.

## Risks and open questions

- **Lock order (D10 step 3).** If a test or a reviewer finds `create` calling
  `auto_accept.get_policy_rows()` while holding `self._lock`, that is the deadlock D10 describes.
  A worker who cannot keep it outside the lock stops with `status=blocked`.
- **Import cycle.** A top-level `from .policy import scope_rows` in `scoped_tokens.py` can fail at
  import time (`ImportError: cannot import name 'ReviewContext' from partially initialized module`).
  If a function-local import does not fix it, stop with `status=blocked`.
- **`rows_for_rules` widening.** The catalogue parametrised test in p1 must show
  `_quads(compile_rows(rows_for_rules(rules, id))) == _quads(rules)` for every group. A group where
  it fails without falling back to custom rows means D4 is wrong: stop with `status=blocked`.
- **Catalogue connector names (D11).** `scope_groups` filters `scope_catalogue()` entries by
  `entry["connector"] in connector_names`. If a connector's catalogue id differs from its
  dispatcher key (the Drive group missing while `drive` is enabled), the filter is wrong: stop with
  `status=blocked` rather than invent a mapping.
- **Browser tests skip in the container.** The session hook warns when Chromium cannot launch
  through the locked Playwright. `tests/integration/test_settings_scoped_token_form_browser.py`
  then reports SKIPPED, which is not a pass. p4's worker runs it with
  `PRIVACYFENCE_TEST_CHROMIUM` set when the hook exported it. Otherwise it reports the skip, and
  CI's `tests.yml` run on the PR is the check.
- **Intermediate state between p1 and p2.** After p1, deleting an Auto-accept rule no longer
  prunes tokens that still name it by id, so re-adding the rule would restore the access
  (ADR 0198's problem) until p2 lands. The phases merge into one feature branch and ship together,
  so this never reaches a release.
- **A legacy token with no settings file and no tokens folder.** `own_legacy_rules` on a principal
  with no `scoped_tokens.json` returns `[]` and writes nothing. A test covers it (p2).

## Implementation manifest

```yaml
plan_slug: token-owned-rules
feature_branch: feature/token-owned-rules
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/LTb2w5jrVYFQ7vaBp1sLX1
manual_steps_source: docs/token-owned-rules-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-existing-token-survives-upgrade
    title: An existing scoped token in real use keeps working after the upgrade, and Settings lists its rules
    why: The conversion of real tokens (with real rule ids, limits and personal-data flags) on a real install is what unit tests can only imitate
  - id: ma2-token-only-rule-stays-in-the-token
    title: A rule added on the New token form auto-accepts for that token only, never for an ordinary Claude session
    why: Confirms end to end, through a real AI client and the real Settings page, that a token rule does not leak into the person's own access
verify_after_merge:
  - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_policy_upgrade.py tests/unit/policy/test_scope_rows.py tests/unit/policy/test_catalogue.py tests/unit/test_gate.py tests/unit/test_auto_accept.py tests/unit/test_settings_controller.py tests/unit/test_audit_log.py tests/unit/test_daemon_main.py -q
  - python3 -m pytest tests/unit/web/test_server.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/web/test_routes_mcp.py tests/unit/web/test_mcp_dispatch.py tests/unit/test_settings_window_html.py -q
  - python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q
  - python3 -m mypy src/privacyfence
  - python3 scripts/mypy_strict_modules.py
final_checks:
  - docs/token-owned-rules-plan.md and docs/token-owned-rules-plan-manual-steps.html are deleted and `grep -rn "token-owned-rules-plan" --include=*.md --include=*.html --include=*.py .` prints nothing
  - docs/adr/0234-a-scoped-token-holds-its-own-rules.md (or the number the last phase used) exists with Status Accepted, and docs/adr/README.md's index lists it; 0164, 0220 and 0173 say "Amended by" it and 0198 says "Superseded by" it, in both the ADR Status lines and the index
  - CHANGELOG.md has the entry under "## [Unreleased]" and no new version heading
  - grep -rn "rule_ids\|rule_limits\|forget_rules\|forget_removed_rules\|migrate_tokens" src/privacyfence prints only the propose result's rule_ids (gate.py's return value and web/mcp_tools.py's description) and scoped_tokens.py's _LEGACY_KEYS / owned_entry
phases:
  - id: p1-exact-copies-and-no-pruning
    title: rows_for_rules, the pure legacy-entry conversion, and remove ADR 0198's pruning
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/policy/scope_rows.py
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/gate.py
      - tests/unit/policy/test_scope_rows.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_auto_accept.py
      - tests/unit/test_settings_controller.py
      - tests/unit/test_gate.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Read docs/token-owned-rules-plan.md, sections Design D4, D5 and D8, before changing anything.
      Read docs/coding-and-testing-guidelines.md and docs/testing-policy.md. Set up with
      `python3 -m venv --upgrade-deps .venv` only if `python3 -m pytest` is not already usable.

      1. src/privacyfence/policy/scope_rows.py: add `_quads` and the public `rows_for_rules`
         exactly as Design D4 says (signature, docstring, steps). Add "rows_for_rules" to `__all__`
         in alphabetical position. Import `Sequence` from `typing` next to the existing imports.
      2. tests/unit/policy/test_scope_rows.py: add `class TestRowsForRules` with:
         - `test_no_rules_gives_no_rows`: `rows_for_rules([], "r-1") == []`.
         - `test_a_scoped_row_round_trips`: build a row with `add_rules([], rules_for_catalogue_entry("drive.folder", ["f1"], [Verb.READ]))`,
           compile it, call `rows_for_rules(compiled, row.id)`. It returns one row equal to the
           original except `former_ids == ()`.
         - `test_narrowed_rules_keep_only_their_operations`: drop one operation from a compiled
           rule. The result compiles (`compile_rows`) to exactly the narrowed rules (compare with
           `_quads`).
         - `test_an_unknown_predicate_becomes_a_custom_row_with_the_given_id`:
           `PolicyRule(id="r-in", predicate="gmail.anything", value=None, operations=frozenset({"gmail.read_message"}))`
           gives one row with `scope is None`, `id == "r-in"`, compiling back to that rule.
         - `test_every_catalogue_group_round_trips`: parametrised over every
           `catalogue.scope_catalogue()` entry for which
           `catalogue.rules_for_catalogue_entry(e["id"], ["x"] if e["needs_value"] else None, catalogue.parse_verbs(e["verbs"]))`
           returns a non-empty list without raising. Build that list at module level, outside the
           test, so no case is skipped. Assert `_quads(compile_rows(rows_for_rules(rules, "r-1"))) == _quads(rules)`
           and that every row's id is "r-1". Then remove the first operation of the first rule
           (keep the rule only if operations remain) and assert the same.
         Stop condition: if any group fails the `_quads` equality, stop with `status=blocked` and
         name the group.
      3. src/privacyfence/scoped_tokens.py: add `_LEGACY_KEYS`, the `OwnedEntry` dataclass and
         `owned_entry` exactly as Design D5 says. Import `scope_rows` inside the function (see the
         plan's "Import cycle" note). `PolicyRule` goes under `TYPE_CHECKING`. Do not change
         `ScopedToken` or any existing function yet, except as step 4 says.
      4. Remove ADR 0198's pruning exactly as Design D8 says:
         - scoped_tokens.py: delete `_without_rules`, `forget_rules_in_file`,
           `ScopedTokenStore.forget_rules` and the module `forget_rules`.
         - auto_accept.py: delete `forget_removed_rules` and its `__all__` entry. Drop the
           `forget_in_tokens` parameter of `remove_policy_v2_rule` and the call under it, and update
           its docstring to "Remove the row with this stable id from the on-disk `auto_accept:`
           section entirely. Returns whether anything was actually removed."
         - settings_controller.py: delete the import (line ~42) and the call (line ~1908).
         - gate.py: delete the import (line ~167) and rewrite the remove/update branch of
           `propose_policy_change` to the code block in D8.
      5. Tests for step 3, in tests/unit/test_scoped_tokens.py, new `class TestOwnedEntry`:
         - an entry with "rules" gives None;
         - rule ids are copied as rows with their own ids (compiled rules from a v3 row);
         - a `rule_limits` entry narrows the copy to its pairs;
         - an id with no compiled rule counts in `missing` and adds no row;
         - a limit that leaves nothing counts as missing;
         - a token without `limits_split_verbs` gets the `_LIMIT_PAIR_SUCCESSORS` pair: copy the
           setup of the existing TestLimitSuccessors cases;
         - `personal_data_ids` sets `allow_personal_data` and `newly_flagged`, and an entry that
           already has the flag gives `newly_flagged == False`;
         - the output entry has none of `_LEGACY_KEYS`;
         - a non-dict entry, a missing `rule_ids` and a non-list `rule_ids` raise.
      6. Tests for step 4: delete `TestForgetRules` (tests/unit/test_scoped_tokens.py) and the
         ADR 0198 tests named in docs/adr/0198-*.md's Verification section. Add these instead, one
         behaviour each:
         - tests/unit/test_auto_accept.py `test_remove_policy_v2_rule_leaves_scoped_tokens_alone`:
           the token file's bytes are unchanged after the delete.
         - tests/unit/test_settings_controller.py `test_remove_leaves_scoped_tokens_alone`.
         - tests/unit/test_gate.py, in TestProposePolicyChange: `test_remove_leaves_scoped_tokens_alone`
           and `test_update_leaves_scoped_tokens_alone`. Reuse that class's `_seed_token`/`_carried`
           helpers; the token still carries the old id.
         - tests/unit/web/test_routes_org_settings.py, in TestRemoveRule: replace
           `test_drops_the_rule_from_that_principals_tokens_only` with
           `test_leaves_scoped_tokens_alone`.
         Change no other assertion.
      7. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`
         and `python3 scripts/mypy_strict_modules.py`. No docs or changelog in this phase; the
         last phase writes them.
    acceptance:
      - python3 -m pytest tests/unit/policy/test_scope_rows.py -q -k TestRowsForRules passes
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q -k TestOwnedEntry passes
      - python3 -m pytest tests/unit/test_auto_accept.py tests/unit/test_settings_controller.py tests/unit/test_gate.py tests/unit/web/test_routes_org_settings.py -q -k leaves_scoped_tokens_alone passes and selects at least 5 tests
      - grep -rn "forget_rules\|forget_removed_rules\|forget_in_tokens\|_without_rules" src/privacyfence prints nothing
      - python3 -m pytest tests/unit -q passes; ruff check . passes; python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass

  - id: p2-tokens-hold-rows
    title: Tokens store and use their own rows; legacy tokens are converted at start-up
    depends_on: [p1-exact-copies-and-no-pruning]
    complexity: M
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/policy_upgrade.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/gate.py
      - src/privacyfence/audit_log.py
      - src/privacyfence/web/routes_mcp.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_policy_upgrade.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_gate.py
      - tests/unit/test_audit_log.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/web/test_routes_mcp.py
      - tests/unit/web/test_routes_mcp_agent_identity.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/web/test_mcp_auth.py
      - tests/unit/web/test_oauth_provider.py
      - tests/unit/connectors/test_assets_connector.py
      - tests/integration/test_settings_scoped_token_form_browser.py
    brief: |
      Read docs/token-owned-rules-plan.md, sections Design D1, D2, D3, D6, D7, D9 and D11's first
      bullet, before changing anything.

      1. src/privacyfence/scoped_tokens.py:
         a. `ScopedToken`: remove `rule_ids`, `rule_limits` and `limits_split_verbs`. Add `rules`,
            `legacy_raw` and `compiled_rules()` exactly as D2 says. Update the module docstring's
            first sentence to "Scoped AI client tokens (ADR 0164, ADR 0234): named, expiring
            credentials that carry their own auto-accept rules and a set of tools."
         b. `_to_json` and `_from_json` per D3. `_write` writes `"version": 2`. Add the
            legacy-token warning to `_reload` (D3).
         c. `create` keeps its `rule_ids` parameter for this phase, now meaning "copy these
            Auto-accept rows" (p3 replaces it). Before `with self._lock:`, compute
            `own_rows = {row.id: row for row in auto_accept.get_policy_rows()} if rules else {}`.
            Inside the lock, at the position of today's "no longer exists" check, raise the same
            error when `not rules <= own_rows.keys()`. Build
            `rules=tuple(dataclasses.replace(own_rows[rid], former_ids=()) for rid in sorted(rules))`.
            Delete the `limits` computation. Keep every other check and its order.
         d. Add `own_rules_in_file`, `ScopedTokenStore.own_legacy_rules` and the module
            `own_legacy_rules` per D6. Delete `_migrated`, `migrate_token_file`,
            `ScopedTokenStore.apply_migration` and `migrate_tokens`, and the `V2Migration`
            TYPE_CHECKING import.
      2. src/privacyfence/policy_upgrade.py: the two `ensure_scope_rows` branches and the
         docstring paragraph per D6. Import `store as policy_store` from `.policy` for
         `rule_from_dict`.
      3. src/privacyfence/daemon_main.py `_upgrade_all_principals`: the no-settings-file branch
         per D6.
      4. src/privacyfence/gate.py: `_rules_in_scope` per D7. Remove the `dataclasses` import
         only if ruff reports it unused.
      5. src/privacyfence/audit_log.py `rule_usage`: skip scoped-token entries per D9.
      6. src/privacyfence/web/routes_mcp.py: `"rules": len(scope.rules)`.
         src/privacyfence/web/routes_settings.py: both create audit summaries use
         `len(token.rules)`.
      7. src/privacyfence/web/server.py: D11's first bullet (token rules from the token's own rows,
         with `_token_rule_entry` and `_limited_tools` deleted). Remove imports ruff reports as
         unused.
      8. Tests. Every `ScopedToken(..., rule_ids=...)` and `rule_limits=` construction changes
         mechanically. The token's rules become
         `rules=tuple(row for rule in R for row in scope_rows.rows_for_rules([rule], rule.id))`,
         where R are the rules the test meant the token to use. In tests/unit/test_gate.py, change
         the `_scoped_token` helper (line ~4144) to take `rules: Sequence[PolicyRule] = (_gmail_read_rule("r-in"),)`
         and build rows that way. Change its callers that pass `rule_ids=`.
         A test that installs global rules with `set_policy_v2_store_rules` keeps them wherever it
         also makes an unscoped call. A test asserting that only the token's rules count keeps a
         global rule that the token does not hold.
         `create(..., rule_ids=[...])` calls with a non-empty list need `auto_accept.get_policy_rows`
         monkeypatched to return the rows (replace the `_known` helper in test_scoped_tokens.py).
         Delete `TestRuleLimits`, `TestLimitSuccessors` and `TestMigrateTokenFile` from
         test_scoped_tokens.py. Their cases are covered by p1's TestOwnedEntry and by step 9.
         Do not weaken any other assertion.
      9. New tests:
         - test_scoped_tokens.py `class TestTokenRows`:
           - a created token's file entry has "rules" (row dicts) and no rule_ids;
           - the file has version 2;
           - a row that cannot be read is dropped and the token still loads;
           - a legacy entry loads with no rules, and `_to_json` round-trips it unchanged except
             for a renamed label;
           - `compiled_rules()` of a copied row equals that row's `compile_row`.
         - test_scoped_tokens.py `class TestOwnLegacyRules`:
           - the file variant converts a legacy entry and is idempotent (a second run writes
             nothing: same mtime/inode);
           - unreadable entries are kept;
           - a missing file returns [];
           - the store variant converts under the lock and the token then auto-accepts through
             its own row;
           - newly flagged labels are returned.
         - tests/unit/test_policy_upgrade.py:
           - a v3 section plus a legacy token file converts the token from the v3 rows;
           - a v2 section plus a legacy token naming v2 ids converts from the v2 rules, and keeps
             the personal-data transfer and its log line;
           - an OSError from `own_legacy_rules` on a v3 section logs the D6 message and returns
             True;
           - on a v2 section it returns False and the section stays v2.
         - tests/unit/test_daemon_main.py: `_upgrade_all_principals` converts a principal's
           legacy token even when it has no settings file (the token ends up with no rules).
         - tests/unit/test_gate.py, in TestScopedTokenGate: `test_a_global_rule_never_covers_a_scoped_call`
           (a global rule covering the call, a token holding no rule for it: denied as
           denied_scoped_token, no card), and
           `test_deleting_the_global_rule_leaves_the_token_rule_working` (a token holding a copy
           of a row; the global rules emptied; the call auto-accepts with that row id).
         - tests/unit/web/test_mcp_dispatch.py, in TestCheckPolicy:
           `test_check_policy_under_a_token_predicts_from_the_token_rows` (verdict auto_accept,
           matched_rule = the token row id, with no global rules installed).
         - tests/unit/test_audit_log.py: `test_rule_usage_leaves_out_scoped_token_calls`.
         - tests/unit/web/test_server.py: a token's page-data rules come from its own rows (title
           and sentence) and have no "missing" or "limited_to" key.
      10. tests/integration/test_settings_scoped_token_form_browser.py: in DATA, remove the
          `r-gone` (missing) and `limited_to` entries. Delete `test_missing_rule_is_left_out_with_notice`.
          p4 rewrites the rest of this file; keep it passing for now.
      11. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`
          and `python3 scripts/mypy_strict_modules.py`. Run
          `python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -q`,
          because routes_mcp.py and server.py changed (the definition of done's conditional rows).
      Stop conditions: a top-level import of `policy.scope_rows` in scoped_tokens.py raising an
      ImportError that a function-local import does not fix; or a test whose only way to pass is
      reading the person's global rules under a token. Stop with status=blocked and say which.
    acceptance:
      - grep -n "rule_ids\|rule_limits\|limits_split_verbs" src/privacyfence/scoped_tokens.py matches only `_LEGACY_KEYS`, `owned_entry` and the `rule_ids` parameter of `create`
      - grep -rn "migrate_tokens\|apply_migration\|migrate_token_file\|_migrated\|_token_rule_entry\|_limited_tools" src/privacyfence prints nothing
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q -k "TestTokenRows or TestOwnLegacyRules or TestOwnedEntry" passes
      - python3 -m pytest tests/unit/test_gate.py -q -k "a_global_rule_never_covers_a_scoped_call or deleting_the_global_rule_leaves_the_token_rule_working" passes and selects 2 tests
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py -q -k check_policy_under_a_token_predicts_from_the_token_rows passes
      - python3 -m pytest tests/unit/test_audit_log.py -q -k rule_usage_leaves_out_scoped_token_calls passes
      - python3 -m pytest tests/unit/test_policy_upgrade.py tests/unit/test_daemon_main.py -q passes
      - python3 -m pytest tests/unit -q passes; ruff check . passes; python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass
      - python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -q passes

  - id: p3-create-from-rule-requests
    title: Create a token from new, copied and duplicated rule requests; page data gains scope_groups
    depends_on: [p2-tokens-hold-rows]
    complexity: M
    touches:
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/web/test_server.py
      - tests/unit/web/test_mcp_auth.py
      - tests/unit/web/test_oauth_provider.py
      - tests/unit/test_daemon_main.py
    brief: |
      Read docs/token-owned-rules-plan.md, sections Design D10 and D11, before changing anything.

      1. src/privacyfence/policy/catalogue.py: add `parse_value_list`, moved unchanged from
         settings_controller.py `_parse_value_list` (line ~356, body and docstring). In
         settings_controller.py, delete the function and add
         `_parse_value_list = policy_catalogue.parse_value_list` to the alias block (line ~266).
         Move its three existing tests in tests/unit/test_settings_controller.py only if they
         break. They should keep passing through the alias; leave them.
         Add tests/unit/policy/test_catalogue.py `test_parse_value_list_splits_on_commas` and
         `test_parse_value_list_keeps_a_single_value_group_whole` (pick one id from
         `catalogue.SINGLE_VALUE_GROUPS`).
      2. src/privacyfence/scoped_tokens.py: add `MAX_RULES_PER_TOKEN = 50`,
         `normalise_rule_requests` and the new `create` exactly as D10 says (parameter `rules`
         replaces `rule_ids`; steps 1–5 in that order, with the auto_accept read and the new-rule
         building before the lock). Error strings exactly as in D10.
      3. src/privacyfence/web/routes_settings.py: `_scoped_create_fingerprint_body` per D10.
         Change `ScopedTokenWiring.store_create`'s type and docstring, and both create routes,
         to pass the normalised `rules`.
      4. src/privacyfence/web/server.py: `build_scoped_token_wiring._create` takes `rules`, and
         `scoped_token_page_data` adds `"scope_groups"` with `_verb_tools` per D11.
      5. Tests:
         - test_scoped_tokens.py, replacing every create call's `rule_ids=` with `rules=`
           requests, and new `class TestRuleRequests`:
           - normalise: sorting, dedupe, verbs sorted, each malformed shape → None (unknown kind,
             extra key, missing key, wrong type, not a list);
           - create:
             - a "new" request builds the row `add_rules([], rules_for_catalogue_entry(...))`
               would build;
             - a bad value raises the RuleValueError text;
             - an incomplete "new" raises the D10 text;
             - a "copy" copies the row exactly (operations narrower than its verbs imply stay
               narrow);
             - a "duplicate" copies another token's row exactly;
             - a missing copy or duplicate raises "One of the chosen rules no longer exists;
               reload the page.";
             - 51 requests raise "A token has at most 50 rules.";
             - a "new" for the same scope and value as a copy merges into one row;
             - `auto_accept.get_policy_rows` is not called while the store lock is held. Assert
               with a fake `get_policy_rows` that checks `store._lock.locked()` is False.
         - tests/unit/web/test_routes_settings.py and test_routes_org_settings.py
           (TestScopedTokenRoutes):
           - the fingerprint covers `rules` (a changed rule request changes the fingerprint);
           - a body with `rule_ids` and no `rules` gets 400;
           - a malformed `rules` gets 400;
           - a created token's audit summary still counts rules;
           - a "new" request reaches the store.
         - tests/unit/web/test_server.py: `scope_groups` lists only enabled connectors' groups
           (with connector_names={"drive"} a "drive.folder" entry is present and no gmail entry);
           `verb_tools["read"]` of "drive.folder" contains "drive_get_file_content".
           Stop condition: if that entry is missing while "drive" is enabled, stop with
           status=blocked (see the plan's Risks).
         - test_mcp_auth.py, test_oauth_provider.py and test_daemon_main.py: their
           `create(..., rule_ids=[], ...)` calls become `rules=[]`.
      6. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`
         and `python3 scripts/mypy_strict_modules.py`. The HTML form still posts `rule_ids`, and
         it gets 400 until p4. That is expected between these phases. Do not touch
         settings_window_html.py.
    acceptance:
      - grep -rn "rule_ids" src/privacyfence/scoped_tokens.py src/privacyfence/web/routes_settings.py src/privacyfence/web/server.py matches only `_LEGACY_KEYS` and `owned_entry` in scoped_tokens.py
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q -k TestRuleRequests passes
      - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/web/test_server.py tests/unit/policy/test_catalogue.py -q passes
      - python3 -m pytest tests/unit -q passes; ruff check . passes; python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass

  - id: p4-new-token-form
    title: The New token form adds rules for the token, copies Auto-accept rules, and duplicates token rules
    depends_on: [p3-create-from-rule-requests]
    complexity: M
    touches:
      - src/privacyfence/settings_window_html.py
      - tests/unit/test_settings_window_html.py
      - tests/integration/test_settings_scoped_token_form_browser.py
    brief: |
      Read docs/token-owned-rules-plan.md, section Design D12 (and D10's request shapes, D11's
      page data), before changing anything. Every string, data attribute and ui key you need is
      in D12. Use them exactly.

      1. src/privacyfence/settings_window_html.py:
         a. Add the five ui keys to the ui object (line ~351).
         b. Refactor `aaPairHint` into `pairHint(group, checkedVerbs, value)` (line ~2215) and
            keep `aaPairHint` as the one-line wrapper. The Auto-accept page's behaviour must not
            change.
         c. `renderScopedTokens`: the Rules block per D12, items 1–5, and remove the
            dup-missing/dup-limited notices.
         d. `scopedTokenRow`'s detail list per D12.
         e. `duplicateScopedToken`, `clearScopedForm`, `scopedToolCoverage`, `scopedToolStatus`
            (if it reads coverage only through `scopedToolCoverage`, leave it),
            the add-covered handler and `submitScopedToken` per D12.
         f. Event wiring next to the existing `data-st-*` handlers (click ~2145, input/change
            ~2254–2290):
            - `data-st-new-group` (change);
            - `data-st-new-value` (input: update `ui.stNewValue` without a re-render, the way
              `data-aa-value` does, so typing keeps focus);
            - `data-st-new-verb`, `data-st-new-add`, `data-st-new-remove` and `data-st-dup-rule`
              (click, re-render).
            Keyboard: the chips and links use the same Enter/Space handling as the existing
            `data-st-rule` chips.
      2. tests/unit/test_settings_window_html.py TestScopedTokensSection: update DATA to the p2/p3
         shape (token rules with title/sentence/tools, top-level `scope_groups` with `verb_tools`).
         Add string-level tests for:
         - the hint text;
         - the three sub-titles;
         - `Add rule to token`;
         - the empty-scope-groups hint;
         - the absence of "a rule that no longer exists" and "limited to".
         Keep the escaping tests and extend them to a scope group label and a value hint.
      3. tests/integration/test_settings_scoped_token_form_browser.py: update DATA the same way
         and rewrite `test_duplicate_prefills_the_form` so the duplicated token's rules are ticked
         `data-st-dup-rule` chips. Add:
         - `test_new_rule_is_sent_with_the_create_request`: pick a group, type a value, tick a
           verb, Add rule to token, create. The posted `rules` holds
           `{kind: "new", group, value, verbs}`.
         - `test_copied_rule_is_sent_as_copy`.
         - `test_duplicated_rule_is_sent_as_duplicate` (with `token_id`).
         - `test_new_rule_tools_are_listed`: the tools from `verb_tools` show up as tool chips.
         - `test_removed_new_rule_is_not_sent`.
         - `test_add_is_disabled_without_a_verb`.
         Use the existing `_capture_posts` helper.
      4. Run `ruff check .`, `python3 -m pytest tests/unit/test_settings_window_html.py -q` and
         `python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q`. Set
         PRIVACYFENCE_TEST_CHROMIUM if the session hook exported it. If the browser tests report
         SKIPPED, say so in your report: a skip is not a pass, and CI's run on the PR is then the
         check. Run `python3 -m pytest tests/unit -q`.
      Stop condition: if `SCOPED.scope_groups` is absent from the page data (p3 not merged), stop
      with status=blocked.
    acceptance:
      - grep -n "rule_ids" src/privacyfence/settings_window_html.py prints nothing
      - grep -c "data-st-new-add\|data-st-new-remove\|data-st-dup-rule\|data-st-new-group\|data-st-new-verb\|data-st-new-value" src/privacyfence/settings_window_html.py prints at least 6
      - grep -n "a rule that no longer exists\|data-st-dup-missing\|data-st-dup-limited" src/privacyfence/settings_window_html.py prints nothing
      - python3 -m pytest tests/unit/test_settings_window_html.py -q passes
      - python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q passes, with no test skipped when PRIVACYFENCE_TEST_CHROMIUM is set (otherwise the skip is reported and the tests.yml run on the PR is the check)
      - python3 -m pytest tests/unit -q passes; ruff check . passes

  - id: p5-sync-wording
    title: privacyfence-sync says where a scoped token's rules now go
    depends_on: []
    complexity: S
    touches:
      - sync/src/privacyfence_sync/permissions.py
      - sync/tests/test_permissions.py
      - sync/README.md
      - plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md
      - docs/privacyfence-sync.md
    brief: |
      Read docs/token-owned-rules-plan.md's Goal and Design D10, D12 and D13 for what the New token
      form now offers.

      1. sync/src/privacyfence_sync/permissions.py: set `_NO_SUGGESTION` and `_NO_SUGGESTION_KEY`
         to the exact D13 texts.
      2. sync/tests/test_permissions.py: update the assertion at line ~128 to the new text, and the
         test for `_NO_SUGGESTION_KEY` if one asserts its text (grep "Always allow").
      3. sync/README.md. Each of these says rules go in "Settings → Auto-accept → Add a rule".
         Rewrite them so the rules a scheduled run uses go on the recipe's scoped token
         (**Settings → Scoped tokens → New token**, "New rule for this token", or "Copy from my
         Auto-accept rules"), and Auto-accept rules serve only attended runs without a token:
         - "## The rules a recipe needs" (line ~100–107);
         - the paragraph after the permissions sample output (line ~820–825);
         - step 2 of "On a server, against organization mode" (line ~1019–1021: "Create each rule
           first, then the token that uses them" becomes "Add each rule on the token's form").
         Add one sentence to "## The rules a recipe needs": "A token's rules belong to the token:
         an Auto-accept rule, including one the card's Always allow adds, does not reach it, and
         deleting an Auto-accept rule does not change it."
      4. Copy sync/README.md to
         plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md (the two
         must be identical).
      5. docs/privacyfence-sync.md "## The rules a recipe needs" (line ~55–62): the same change in
         two sentences, linking connect-claude-code.md#scoped-tokens.
      6. In sync/: `pip install -e "./sync[test]"`, then
         `pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-report=term-missing --cov-fail-under=100`,
         `mypy sync/src` and `bandit -r sync/src` must pass.
    acceptance:
      - pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes
      - mypy sync/src passes; bandit -r sync/src passes
      - cmp sync/README.md plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md exits 0
      - grep -n "Create each rule first, then the token" sync/README.md prints nothing
      - grep -n "add one to the recipe's scoped token in Settings -> Scoped tokens" sync/src/privacyfence_sync/permissions.py matches

  - id: p6-retire-plan
    title: ADR 0234, reference docs, changelog, delete the plan, dispatch a build
    depends_on: [p1-exact-copies-and-no-pruning, p2-tokens-hold-rows, p3-create-from-rule-requests, p4-new-token-form, p5-sync-wording]
    complexity: M
    touches:
      - docs/adr/0234-a-scoped-token-holds-its-own-rules.md
      - docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md
      - docs/adr/0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md
      - docs/adr/0198-deleting-an-auto-accept-rule-removes-it-from-scoped-tokens.md
      - docs/adr/0220-a-routines-rule-profile-is-a-scoped-token.md
      - docs/adr/README.md
      - docs/connect-claude-code.md
      - docs/security-and-compliance.md
      - docs/how-it-works.md
      - docs/approvals-and-policy.md
      - docs/org-mode-setup-guide.md
      - CHANGELOG.md
      - docs/token-owned-rules-plan.md
      - docs/token-owned-rules-plan-manual-steps.html
    brief: |
      Read docs/adr/README.md (rules and template), CONTRIBUTING.md "Decisions, plans and ADRs",
      and this plan's Design and ADRs sections. The ADR is written from this plan. It never links
      the plan document.

      1. `ls docs/adr` and take the next free number (0234 unless another branch took it; then use
         that number everywhere below and rename the file).
         Write docs/adr/0234-a-scoped-token-holds-its-own-rules.md from the template, with:
         - Status `Accepted — <today>. Implemented.` plus the lines "Amends ADR 0164, ADR 0173
           and ADR 0220. Supersedes ADR 0198."
         - Context: the coupling, the user's two cases (a `privacyfence-sync` job with no AI
           model, and a Claude Code routine whose own environment reaches /mcp with a Bearer
           token and a narrow tool list).
         - Decision: D1, D2–D3 in prose, D5–D6 (conversion at start-up, limits applied, missing
           ids left out, the personal-data transfer kept), D7 (no fallback), D8, D9, D10 (request
           kinds; the fingerprint covers them), D12 in one paragraph.
         - Alternatives considered: compiled rules on disk; ids plus a pinned copy; keeping the
           global rules as a fallback; a token-only flag on Auto-accept rules (still one list and
           one id space); editing a token's rules in place (tokens stay immutable; Duplicate).
         - Consequences:
           - the person's other clients never gain access from a token rule;
           - deleting an Auto-accept rule no longer narrows a token, so revoking is the way;
           - "Matched Nx" no longer counts token calls;
           - a version-2 token file is unreadable to an older PrivacyFence, whose tokens then
             fail closed;
           - a future row-format rewrite must rewrite token rows.
         - Verification: the test classes and names p1–p4 added.
         - Related: the ADRs and the source files.
      2. Status lines only (never the body) of 0164, 0173 and 0220: add "Amended by
         [ADR 0234](0234-a-scoped-token-holds-its-own-rules.md): a token holds its own copy of
         its rules." For 0198, change the status to "Superseded by
         [ADR 0234](0234-a-scoped-token-holds-its-own-rules.md)." In docs/adr/README.md, add the
         0234 index row and update the status column of 0164, 0173, 0198 and 0220 the same way.
      3. Reference docs. Describe today's behaviour and link ADR 0234 for the why:
         - docs/connect-claude-code.md: both "### Scoped tokens" sections (local and organization
           mode) — the rules are the token's own (new or copied), the deletion sentence becomes
           "Deleting or changing an Auto-accept rule does not change a token", drop "edit a rule
           first and then create the token", keep Duplicate (it copies the token's rules).
         - docs/security-and-compliance.md "## Scoped tokens": the "What a scope limits", "One
           token per routine" and "Deleting a rule" bullets; add that a token file written by this
           version cannot be read by an older one (tokens then stop working).
         - docs/how-it-works.md "### Scoped tokens": "a list of the rules and tools" becomes "its
           own rules and a list of tools".
         - docs/approvals-and-policy.md line ~323 "(scoped tokens, the audit log)" becomes "(the
           audit log)", and add one sentence there: "A scoped token's own rules have no Update
           rule button: create a new token, or Duplicate it."
         - docs/org-mode-setup-guide.md lines ~349 and ~1033: "limited to chosen rules and tools"
           becomes "with its own rules and a chosen list of tools".
      4. CHANGELOG.md, under `## [Unreleased]`, a `### Changed` heading (create it there if it
         is missing) with:
         "- **A scoped token holds its own rules.** On the New token form you add rules for that
         token, or copy one of your Auto-accept rules. They never auto-accept anything for your
         other AI clients, and changing or deleting an Auto-accept rule no longer changes a token.
         Existing tokens get their own copy of the rules they used the first time PrivacyFence
         starts after the upgrade. A token saved by this version cannot be read by an older one."
         Open no version heading.
      5. `git rm docs/token-owned-rules-plan.md docs/token-owned-rules-plan-manual-steps.html`.
         Then `grep -rn "token-owned-rules-plan" .` must print nothing.
      6. Commit and push, then dispatch `build.yml` (workflow_dispatch, no inputs) on your pushed
         branch with the GitHub MCP `actions_run_trigger` tool. Put the run URL in your final
         report as `build_run: <url>`; the maintainer installs from it for the manual checks.
    acceptance:
      - test -f docs/adr/0234-a-scoped-token-holds-its-own-rules.md (or the number used) and grep -n "^Accepted" in it matches
      - grep -n "0234" docs/adr/README.md matches the index row and the 0164, 0173, 0198 and 0220 rows
      - grep -n "Superseded by \[ADR 0234\]" docs/adr/0198-deleting-an-auto-accept-rule-removes-it-from-scoped-tokens.md matches
      - grep -rn "edit a rule first and then create the token\|removes it from every scoped token" docs prints nothing outside docs/adr/
      - awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -n "A scoped token holds its own rules" matches
      - grep -rn "token-owned-rules-plan" . prints nothing
      - the final report has a build_run URL
```
