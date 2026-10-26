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
  (`daemon_main.py:412`), `daemon_main._upgrade_all_principals` (`:427`), every Auto-accept writer
  (`auto_accept._read_rows_locked`, `auto_accept.py:595`) and
  `SettingsController._load_rows` (`settings_controller.py:1947`). `load_config` runs before
  logging is set up, which is why it collects messages in `_deferred_warnings`
  (`daemon_main.py:378`).
- **ADR 0198 pruning.** `_without_rules` (`:308`), `forget_rules_in_file` (`:320`),
  `ScopedTokenStore.forget_rules` (`:558`) and the module `forget_rules` (`:688`) drop a deleted
  rule's id from tokens. They are called through `auto_accept.forget_removed_rules`
  (`auto_accept.py:682`) from `auto_accept.remove_policy_v2_rule` (`:698`, with its
  `forget_in_tokens` parameter), `SettingsController.remove_policy_rule`
  (`settings_controller.py:1908`) and `gate.propose_policy_change` (`gate.py:1649`–`1659`).
- **Gate.** `gate._rules_in_scope` (`gate.py:728`) filters `get_policy_v2_store_rules()` to the
  token's ids and narrows each one by `rule_limits`. `_evaluate_auto_accept` (`:701`),
  `rule_values` (`:750`), `_personal_data_rule` (`:765`), `preflight_auto_accept` (`:795`, which
  backs `privacyfence_check_policy`) and `_assets_schema_detail` (`:1714`) all read through it.
- **Tool grouping bug.** `scoped_token_page_data` groups tools by `name.split("_", 1)[0]`
  (`server.py:376`–`379`), so `apps_script_*` tools fall under "apps", which is not a connector
  name, and are never drawn on the form.
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
  Its next create, rename or revoke then rewrites the file without the skipped entries
  (`_write`, `scoped_tokens.py:407`), so after a downgrade those tokens stop working and are
  deleted by the next token change. The ADR and the changelog say so.

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
1. `rules = [r for r in rules if r.operations]`, then `if not rules: return []`. A row with no
   operations would not survive `row_from_dict`.
2. `merged, _ = add_rules([], rules)`, then
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

The conversion runs only at start-up, and for a version-2 section inside the conversion that
section already does. It never runs on an ordinary Auto-accept write: a write on a version-3 section
must not depend on, or touch, the token file.

In `scoped_tokens.py`, every function below takes `compiled` as a **callable**
(`Callable[[], Sequence[PolicyRule]]`), called only when a legacy token exists. It also takes a
keyword `log: Callable[[str], None] = logger.warning`, which every message goes through, so
`daemon_main` can collect the messages before logging is set up. The two messages are, exactly:
- `f"Keeping an unreadable scoped token entry as it is ({principal_or_path})"`
- `f"Scoped token {label!r} now holds its own copy of {copied} rule(s); {missing} rule(s) it named no longer existed"`

The functions:

- `own_rules_in_file(path: Path, compiled, personal_data_ids=frozenset(), *, log=logger.warning) -> list[str]`.
  It reads the file. A missing or unreadable file returns `[]` with nothing logged. When no entry
  lacks `"rules"`, it returns `[]` without calling `compiled`. Otherwise it calls `compiled()` once
  and passes each entry through `owned_entry`:
  - An entry that raises `KeyError`, `TypeError` or `ValueError` is kept as it is, with the first
    message.
  - `None` is kept as it is.
  - A converted entry replaces it, with the second message.

  It writes `{"version": 2, "tokens": out}` only when something changed, and returns the labels of
  the newly flagged entries. An `OSError` on write propagates.
- `ScopedTokenStore.own_legacy_rules(principal, compiled, personal_data_ids=frozenset(), *, log=logger.warning) -> list[str]`.
  It takes the store lock and calls `_refresh(principal)`. With no token having `legacy_raw`, it
  returns `[]`, writes nothing and does not call `compiled`. Otherwise it calls `compiled()` once
  and applies `owned_entry({**t.legacy_raw, "label": t.label}, ...)` to each such token. Each token
  is rebuilt with `_from_json(principal.id, owned.entry)`. A token that raises is kept, with the
  first message. Then it replaces the list, calls `_write`, sends the second message per converted
  token, and returns the newly flagged labels.
- The module-level `own_legacy_rules(principal, compiled, personal_data_ids=frozenset(), *, log=logger.warning)`
  goes through the live store when `current_store()` is set, else through `own_rules_in_file` on
  `paths.authority_dir(principal) / STORE_FILE_NAME`. This mirrors today's `migrate_tokens`.
  `paths.authority_dir` raises `ValueError` for an unsafe principal id. Callers catch it (below).
- Remove `_migrated`, `migrate_token_file`, `ScopedTokenStore.apply_migration`, `migrate_tokens`
  and the `V2Migration` `TYPE_CHECKING` import. The fields of `scope_rows.V2Migration` that only
  the token re-pointing used (`id_map`, `members`, `member_pairs`) are removed in p3b;
  `personal_data_ids` stays.

In `policy_upgrade.py`:

- **`ensure_scope_rows`, version-2 branch:** replace the
  `scoped_tokens.migrate_tokens(principal, migration)` call with
  `scoped_tokens.own_legacy_rules(principal, lambda: v2_rules, migration.personal_data_ids, log=log)`,
  where `v2_rules = [r for r in (policy_store.rule_from_dict(raw) for raw in raw_rules) if r is not None]`.
  Catch `(OSError, ValueError)` (today only `OSError`), with the same failure message and
  `return False`. The rest of the function stays, including the personal-data label log line. The
  non-version-2 path of `ensure_scope_rows` is unchanged: it still returns `True` at once.
- **New `ensure_token_rules(cfg: dict[str, Any], principal: Principal, *, log: Callable[[str], None] = logger.warning) -> None`**:

  ```python
  def ensure_token_rules(cfg, principal, *, log=logger.warning):
      """Give ``principal``'s scoped tokens written before tokens held their own rules a copy of
      the rules they name, from ``cfg``'s version-3 rows (ADR 0234). Start-up only
      (``daemon_main``), after ``ensure_scope_rows``; nothing is done while the section is still
      version 2, whose own conversion handles the tokens."""
      if _v2_rules(cfg.get("auto_accept")) is not None:
          return
      try:
          scoped_tokens.own_legacy_rules(
              principal, lambda: scope_rows.compile_rows(scope_rows.rows_from_config(cfg)), log=log,
          )
      except (OSError, ValueError) as exc:
          log(f"{principal.id}: could not give scoped tokens their own copy of their rules: {exc}")
  ```
- Add one paragraph to the module docstring: "A scoped token holds its own copy of its rules in
  the same row format (ADR 0234). A later change to that format that needs a rewrite on disk must
  rewrite each token's `rules` too, the way `own_legacy_rules` converts a token's old rule ids."

In `daemon_main.py`:

- `load_config` (`:412`): right after the `ensure_scope_rows` call, inside the same
  `if principal is not None:`, call
  `policy_upgrade.ensure_token_rules(config, principal, log=_deferred_warnings.append)`.
- `_upgrade_all_principals` (`:427`): after its `ensure_scope_rows` call, call
  `policy_upgrade.ensure_token_rules(config, principal, log=_deferred_warnings.append)`. For a
  principal without a settings file, call
  `policy_upgrade.ensure_token_rules({}, principal, log=_deferred_warnings.append)` before the
  `continue`. That principal has no rules, so its legacy tokens get none and the start-up warning
  stops repeating. For a principal whose file is not a mapping, keep the `continue` and convert
  nothing.

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

### D9. Usage stats leave scoped-token calls out (p3b)

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
   These errors (a bad value, an incomplete new rule, and a `RuntimeError` from
   `get_policy_rows` when no config is loaded) therefore come before the duplicate-label, days and
   tool checks. p3's tests assert that order.
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

- **Tool grouping (p3):** group each tool under the connector in `connector_names` that it
  starts with plus `"_"`, so `apps_script_*` tools land under `apps_script`. Pick the longest
  matching name when several match. Replace the `name.split("_", 1)[0]` lookups for `tools` and
  for `auto_tools` with one helper:

  ```python
  def _tool_connector(name: str, connector_names: set[str]) -> str | None:
      matches = [c for c in connector_names if name.startswith(c + "_")]
      return max(matches, key=len) if matches else None
  ```

- **(p2)** Each token's `"rules"` is
  `[_scoped_rule_entry(row, cached_row_value(row, resolver), row_value_items(row, resolver)) for row in token.rules]`,
  built inside the `with principal_scope(principal):` block. `_token_rule_entry` and
  `_limited_tools` are removed, and with them the `missing` and `limited_to` keys.
- `"rules"` (top level) stays: the person's Auto-accept rows, now offered as copies.
- **(p3)** New `"scope_groups"`: `policy.catalogue.scope_catalogue()` entries whose `"connector"` is in
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
     `data-st-new-value="1"`. On `input` it sets `ui.stNewValue`, calls `render(pyState)`, then
     re-focuses `[data-st-new-value]` and restores the caret, exactly as the `data-aa-value` branch
     does (`settings_window_html.py:2287`–`2292`). A re-render is required, because the button's
     `aria-disabled` state depends on the value, and `.button[aria-disabled="true"]` has
     `pointer-events: none` (`:162`).
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
  unreadable to older versions, which drop its tokens at their next token change. It amends:
  - ADR 0164 (rules are no longer a subset of the person's);
  - ADR 0166 (an Update rule no longer reaches a token);
  - ADR 0172 (no token re-pointing, `rule_limits` or "limited to" display any more);
  - ADR 0173 (where the flag transfer now runs);
  - ADR 0220 (create the token with its rules, not after editing a rule);
  - ADR 0228 (`_LIMIT_PAIR_SUCCESSORS` is used only to convert old tokens; a later predicate split
    needs no entry for tokens).

  It supersedes ADR 0198.
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
- **`rows_for_rules` rows that do not survive storage.** The custom-row fallback makes the
  `_quads` equality hold by construction, so p1's catalogue test also round-trips every produced row
  through `row_to_dict`, JSON and `row_from_dict`. A group whose row comes back `None`, or compiles
  to other quads, means D4 is wrong: stop with `status=blocked` and name the group.
- **The form between p3 and p4.** After p3 the Settings form still posts `rule_ids` and gets a 400,
  until p4 lands. Both ship in the same PR.
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
  - ruff check .
  - python3 -m pytest tests/unit/test_scoped_tokens.py tests/unit/test_policy_upgrade.py tests/unit/policy/test_scope_rows.py tests/unit/policy/test_catalogue.py tests/unit/test_gate.py tests/unit/test_auto_accept.py tests/unit/test_settings_controller.py tests/unit/test_audit_log.py tests/unit/test_daemon_main.py -q
  - python3 -m pytest tests/unit/web/test_server.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/web/test_routes_mcp.py tests/unit/web/test_mcp_dispatch.py tests/unit/test_settings_window_html.py -q
  - python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q
  - python3 -m pytest tests -q --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json
  - python3 scripts/check_coverage_floor.py coverage.json
  - python3 -m mypy src/privacyfence
  - python3 scripts/mypy_strict_modules.py
final_checks:
  - docs/token-owned-rules-plan.md and docs/token-owned-rules-plan-manual-steps.html are deleted and `grep -rn "token-owned-rules-plan" --include=*.md --include=*.html --include=*.py .` prints nothing
  - docs/adr/0234-a-scoped-token-holds-its-own-rules.md (or the number the last phase used) exists with Status Accepted, and docs/adr/README.md's index lists it; 0164, 0166, 0172, 0173, 0220 and 0228 say "Amended by" it and 0198 says "Superseded by" it, in both the ADR Status lines and the index
  - CHANGELOG.md has the entry under "## [Unreleased]" and no new version heading
  - grep -rn "rule_limits\|forget_rules\|forget_removed_rules\|migrate_tokens\|pairs_of\|member_pairs" src/privacyfence matches only scoped_tokens.py's _LEGACY_KEYS, _limits_from_json and owned_entry; grep -rn "rule_ids" src/privacyfence matches only those, scoped_tokens.py's _from_json legacy branch, and the propose result's rule_ids (gate.py's return value and web/mcp_tools.py's description)
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
         exactly as Design D4 says (signature, docstring, steps 1-4). Add "rows_for_rules" to
         `__all__` in alphabetical position. Import `Sequence` from `typing` next to the existing
         imports.
      2. tests/unit/policy/test_scope_rows.py: add `class TestRowsForRules` with:
         - `test_no_rules_gives_no_rows`: `rows_for_rules([], "r-1") == []`, and the same for a
           list holding only a rule with empty operations.
         - `test_a_scoped_row_round_trips`: build a row with
           `add_rules([], rules_for_catalogue_entry("drive.folder", ["f1"], [Verb.READ]))`, compile
           it, and call `rows_for_rules(compiled, row.id)`. It returns one row equal to the original
           except `former_ids == ()`.
         - `test_narrowed_rules_keep_only_their_operations`: drop one operation from a compiled
           rule. The result compiles (`compile_rows`) to exactly the narrowed rules (compare with
           `_quads`).
         - `test_an_unknown_predicate_becomes_a_custom_row_with_the_given_id`:
           `PolicyRule(id="r-in", predicate="gmail.anything", value=None, operations=frozenset({"gmail.read_message"}))`
           gives one row with `scope is None`, `id == "r-in"`, compiling back to that rule.
         - `test_every_catalogue_group_round_trips`, parametrised over every
           `catalogue.scope_catalogue()` entry:
           - Use a module-level dict `SAMPLE_VALUES` with one valid value per group id. Use
             `None` for a group whose `needs_value` is false. For a valued group, use a value
             `value_format` accepts: an email domain such as `["example.com"]` for
             gmail.sender_domain, and an id of the shape the group's `value_hint` shows for the
             others. Find the shape by calling
             `catalogue.rules_for_catalogue_entry(id, value, all verbs)` in a scratch session
             until it returns rules without raising.
           - Assert `len(SAMPLE_VALUES) == len(catalogue.scope_catalogue())`, so a new group
             fails the test until it gets a sample.
           - For each group, `rules = rules_for_catalogue_entry(id, SAMPLE_VALUES[id], parse_verbs(entry["verbs"]))`
             is non-empty.
           - `rows = rows_for_rules(rules, "r-1")`: every row has id "r-1", and
             `_quads(compile_rows(rows)) == _quads(rules)`.
           - Every row survives storage:
             `row_from_dict(json.loads(json.dumps(row_to_dict(row)))) == row`.
           - Then remove the first operation of the first rule (keep that rule only if operations
             remain) and assert the same three things.

         Stop condition: if any group fails the round trip, stop with `status=blocked` and name the
         group.
      3. src/privacyfence/scoped_tokens.py: add `_LEGACY_KEYS`, the `OwnedEntry` dataclass and
         `owned_entry` exactly as Design D5 says. `owned_entry` takes `compiled` as a plain
         sequence. Import `scope_rows` inside the function (see the plan's "Import cycle" note).
         `PolicyRule` goes under `TYPE_CHECKING`. Do not change `ScopedToken` or any existing
         function, except as step 4 says.
      4. Remove ADR 0198's pruning exactly as Design D8 says:
         - scoped_tokens.py: delete `_without_rules`, `forget_rules_in_file`,
           `ScopedTokenStore.forget_rules` and the module `forget_rules`.
         - auto_accept.py: delete `forget_removed_rules` and its `__all__` entry. Drop the
           `forget_in_tokens` parameter of `remove_policy_v2_rule` and the call under it, and change
           its docstring to "Remove the row with this stable id from the on-disk `auto_accept:`
           section entirely. Returns whether anything was actually removed."
         - settings_controller.py: delete the import (line ~42) and the call (line ~1908).
         - gate.py: delete the import (line ~167) and rewrite the remove/update branch of
           `propose_policy_change` (lines ~1649-1659) to the code block in D8.
      5. Tests for step 3, in tests/unit/test_scoped_tokens.py, new `class TestOwnedEntry`:
         - an entry with "rules" gives None;
         - rule ids are copied as rows with their own ids (compiled rules from a v3 row);
         - a `rule_limits` entry narrows the copy to its pairs;
         - an id with no compiled rule counts in `missing` and adds no row;
         - a limit that leaves nothing counts as missing;
         - a token without `limits_split_verbs` gets the `_LIMIT_PAIR_SUCCESSORS` pair (copy the
           setup of the existing TestLimitSuccessors cases);
         - `personal_data_ids` sets `allow_personal_data` and `newly_flagged`, while an entry that
           already has the flag gives `newly_flagged == False`;
         - the output entry has none of `_LEGACY_KEYS`;
         - a non-dict entry, a missing `rule_ids` and a non-list `rule_ids` raise.
      6. Tests for step 4. Delete exactly these tests of the removed behaviour:
         - tests/unit/test_scoped_tokens.py: `TestForgetRules` (the whole class).
         - tests/unit/test_auto_accept.py:
           - `test_remove_policy_v2_rule_drops_the_id_from_a_token_that_carried_it`
           - `test_removing_an_unknown_id_leaves_the_token_file_alone`
           - `test_forget_in_tokens_false_leaves_tokens_alone`
           - `test_a_failing_token_write_does_not_undo_the_delete`
           - `test_forget_removed_rules_survives_an_unsafe_principal_id` (line ~438)
           - `test_forget_removed_rules_logs_the_changed_labels` (line ~446)
         - tests/unit/test_settings_controller.py:
           - `test_remove_drops_the_id_from_a_token_that_carried_it`
           - `test_adding_the_same_scope_again_does_not_restore_the_token_access`
         - tests/unit/test_gate.py:
           - `test_update_to_another_folder_drops_the_old_id_from_a_token` (line ~1350)
           - `test_update_of_only_the_verbs_keeps_the_id_in_a_token` (line ~1364)
           - `test_remove_drops_the_id_from_a_token` (line ~1379)
         - tests/unit/web/test_routes_org_settings.py:
           `TestRemoveRule.test_drops_the_rule_from_that_principals_tokens_only`.

         Add these, one behaviour each. Keep the existing `_seed_token` helpers of those files
         unchanged in this phase.
         - test_auto_accept.py `test_remove_policy_v2_rule_leaves_scoped_tokens_alone`: the token
           file's bytes are unchanged after the delete.
         - test_settings_controller.py `test_remove_leaves_scoped_tokens_alone`.
         - test_gate.py TestProposePolicyChange: `test_remove_leaves_scoped_tokens_alone` and
           `test_update_leaves_scoped_tokens_alone`. The token still carries the old id.
         - test_routes_org_settings.py TestRemoveRule: `test_leaves_scoped_tokens_alone`.

         Change no other assertion. A test that fails for another reason after step 4 is a sign
         the brief is wrong: stop with `status=blocked` and name it.
      7. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`
         and `python3 scripts/mypy_strict_modules.py`. No docs or changelog in this phase; the
         last phase writes them.
    acceptance:
      - python3 -m pytest tests/unit/policy/test_scope_rows.py -q -k TestRowsForRules passes, and the round-trip test runs one case per scope_catalogue() entry
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q -k TestOwnedEntry passes
      - python3 -m pytest tests/unit/test_auto_accept.py tests/unit/test_settings_controller.py tests/unit/test_gate.py tests/unit/web/test_routes_org_settings.py -q -k leaves_scoped_tokens_alone passes and selects 5 tests
      - grep -rn "forget_rules\|forget_removed_rules\|forget_in_tokens\|_without_rules" src/privacyfence tests prints nothing
      - python3 -m pytest tests/unit -q passes; ruff check . passes; python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass

  - id: p2-tokens-hold-rows
    title: Tokens store and use their own rows; legacy tokens are converted at start-up
    depends_on: [p1-exact-copies-and-no-pruning]
    complexity: M
    worker_model: opus
    worker_model_reason: Removing rule_ids and rule_limits from ScopedToken has to happen in one commit across 7 source files and about 190 references in 20 test files, several of which need judgement (tests that relied on a token reaching the person's rules must be inverted, not loosened). It cannot be split without an intermediate commit where scoped calls either fail to construct or fall back to the person's own rules.
    touches:
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/policy_upgrade.py
      - src/privacyfence/daemon_main.py
      - src/privacyfence/gate.py
      - src/privacyfence/web/routes_mcp.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/web/server.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_policy_upgrade.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_gate.py
      - tests/unit/test_auto_accept.py
      - tests/unit/test_settings_controller.py
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
      Read docs/token-owned-rules-plan.md, sections Design D1, D2, D3, D6, D7 and D11's "(p2)"
      bullet, before changing anything. D9 and the rest of D11 are other phases.

      1. src/privacyfence/scoped_tokens.py:
         a. `ScopedToken`: remove `rule_ids`, `rule_limits` and `limits_split_verbs`, and add
            `rules`, `legacy_raw` and `compiled_rules()`, exactly as D2 says. Change the module
            docstring's first sentence to "Scoped AI client tokens (ADR 0164, ADR 0234): named,
            expiring credentials that carry their own auto-accept rules and a set of tools."
         b. `_to_json` and `_from_json` as D3 says. `_write` writes `"version": 2`. Add the
            legacy-token warning to `_reload` (D3).
         c. `create` keeps its `rule_ids` parameter for this phase, now meaning "copy these
            Auto-accept rows" (p3 replaces it).
            - Before `with self._lock:`, compute
              `own_rows = {row.id: row for row in auto_accept.get_policy_rows()} if rules else {}`.
            - Inside the lock, at the position of today's "no longer exists" check, raise the same
              error when `not rules <= own_rows.keys()`.
            - Build
              `rules=tuple(dataclasses.replace(own_rows[rid], former_ids=()) for rid in sorted(rules))`.
            - Delete the `limits` computation. Keep every other check and its order.
         d. Add `own_rules_in_file`, `ScopedTokenStore.own_legacy_rules` and the module
            `own_legacy_rules` exactly as D6 says (`compiled` is a callable; the `log` keyword; the
            two exact messages). Delete `_migrated`, `migrate_token_file`,
            `ScopedTokenStore.apply_migration` and `migrate_tokens`, and the `V2Migration`
            TYPE_CHECKING import.
      2. src/privacyfence/policy_upgrade.py: the version-2 branch change, the new
         `ensure_token_rules` and the docstring paragraph, as D6 says. Import `store as
         policy_store` from `.policy` for `rule_from_dict`.
      3. src/privacyfence/daemon_main.py: the `load_config` and `_upgrade_all_principals` calls,
         as D6 says.
      4. src/privacyfence/gate.py: `_rules_in_scope` as D7 says. Remove the `dataclasses` import
         only if ruff reports it unused.
      5. src/privacyfence/web/routes_mcp.py: `"rules": len(scope.rules)`.
         src/privacyfence/web/routes_settings.py: both create audit summaries use
         `len(token.rules)`.
      6. src/privacyfence/web/server.py: D11's "(p2)" bullet. Token rules come from the token's own
         rows; delete `_token_rule_entry` and `_limited_tools`. Remove imports ruff reports as
         unused. Leave `scope_rows.pairs_of` in place; p3b removes it.
      7. Tests: the mechanical part.
         - Every `ScopedToken(..., rule_ids=...)` and `rule_limits=` construction changes. The
           token's rules become
           `rules=tuple(row for rule in R for row in scope_rows.rows_for_rules([rule], rule.id))`,
           where R are the rules the test meant the token to use.
         - In tests/unit/test_gate.py, change the `_scoped_token` helper (line ~4180) to take
           `rules: Sequence[PolicyRule] | None = None`. `None` means `(_gmail_read_rule("r-in"),)`.
           That helper is defined after `_scoped_token`, so it cannot be the default value. Build
           the rows as above, and change the callers that pass `rule_ids=`.
         - Rewrite the `_seed_token` helpers to seed `rules`: tests/unit/test_gate.py
           (TestProposePolicyChange, line ~1167, and its `_carried`, which now returns the row ids
           of `rules`), tests/unit/test_auto_accept.py (line ~63) and
           tests/unit/test_settings_controller.py (line ~2356). p1's `leaves_scoped_tokens_alone`
           tests then assert the token's rows are unchanged.
         - A test that installs global rules with `set_policy_v2_store_rules` keeps them wherever
           it also makes an unscoped call. A test asserting that only the token's rules count
           keeps a global rule that the token does not hold.
         - `create(..., rule_ids=[...])` calls with a non-empty list need
           `auto_accept.get_policy_rows` monkeypatched to return the rows. Replace the `_known`
           helper in test_scoped_tokens.py.
      8. Tests: the parts that change meaning. Do exactly this; do not weaken any other assertion.
         - tests/unit/test_settings_controller.py
           `test_a_token_scoped_to_the_rule_still_has_it_after_the_update` (line ~2556) becomes
           `test_update_rule_does_not_reach_a_token_copy`. The token holds a copy of the stale
           row. After Update rule, the global row gains the missing operations, while the token's
           `rules` (and `_rules_in_scope(token)`) are unchanged.
         - tests/unit/test_gate.py `class TestRuleLimits` (line ~5063): delete the class. Add
           `TestTokenRows.test_adding_a_verb_to_the_global_row_does_not_widen_the_token`: the token
           holds a copy of a row; the global row gets another verb via `scope_rows.set_verbs` and
           `set_policy_v2_store_rules(compile_rows(...))`; `gate._rules_in_scope(token)` is
           unchanged.
         - Delete `TestRuleLimits`, `TestLimitSuccessors` and `TestMigrateTokenFile` from
           tests/unit/test_scoped_tokens.py. p1's TestOwnedEntry and step 9 cover their cases.
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
           - unreadable entries are kept and reported through `log`;
           - a missing file returns [];
           - `compiled` is not called when no token is legacy;
           - the store variant converts under the lock, and the token then auto-accepts through
             its own row;
           - newly flagged labels are returned;
           - the conversion message reaches the `log` callable.
         - tests/unit/test_policy_upgrade.py:
           - `ensure_token_rules` converts a legacy token from a v3 section's rows;
           - it does nothing while the section is v2;
           - an OSError or ValueError from `own_legacy_rules` goes to `log` and is not raised;
           - `ensure_scope_rows` on a v2 section plus a legacy token naming v2 ids converts from
             the v2 rules, keeping the personal-data transfer and its log line;
           - `ensure_scope_rows` on a v3 section writes nothing to the token file;
           - an OSError from the v2 conversion returns False and the section stays v2.
         - tests/unit/test_daemon_main.py:
           - `load_config` with a principal converts a legacy token, and the message lands in
             `_deferred_warnings`;
           - `_upgrade_all_principals` converts a principal's legacy token even when it has no
             settings file (the token ends up with no rules).
         - tests/unit/test_gate.py TestScopedTokenGate:
           - `test_a_global_rule_never_covers_a_scoped_call`: a global rule covers the call, the
             token holds no rule for it; the call is denied as denied_scoped_token, with no card.
           - `test_deleting_the_global_rule_leaves_the_token_rule_working`: the token holds a copy
             of a row and the global rules are emptied; the call auto-accepts with that row id.
         - tests/unit/web/test_mcp_dispatch.py TestCheckPolicy:
           `test_check_policy_under_a_token_predicts_from_the_token_rows` (verdict auto_accept,
           matched_rule = the token row id, with no global rules installed).
         - tests/unit/web/test_server.py: a token's page-data rules come from its own rows (title
           and sentence), with no "missing" or "limited_to" key.
      10. tests/integration/test_settings_scoped_token_form_browser.py: in DATA, remove the
          `r-gone` (missing) and `limited_to` entries, and delete
          `test_missing_rule_is_left_out_with_notice`. p4 rewrites the rest of this file; keep it
          passing for now.
      11. Run these:
          - `ruff check .`
          - `python3 -m pytest tests/unit -q`
          - `python3 -m mypy src/privacyfence`
          - `python3 scripts/mypy_strict_modules.py`
          - `python3 -m pytest tests -q --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json`
            then `python3 scripts/check_coverage_floor.py coverage.json`. scoped_tokens.py, gate.py
            and server.py have per-file floors; the new OSError, ValueError and unreadable-entry
            branches need tests, never a lower floor.
          - `python3 -m pytest tests/integration/test_mcp_daemon_contract.py tests/integration/test_shim_mcp_contract.py -q`,
            because routes_mcp.py and server.py changed.
      Stop conditions: stop with status=blocked and say which, if any of these happens:
      - a top-level import of `policy.scope_rows` in scoped_tokens.py raises an ImportError that a
        function-local import does not fix;
      - a test can only pass by reading the person's global rules under a token;
      - the coverage floor cannot be met without lowering it.
    acceptance:
      - grep -c "rule_ids\|rule_limits\|limits_split_verbs" src/privacyfence/scoped_tokens.py counts only lines in `_LEGACY_KEYS`, `_limits_from_json`, `owned_entry`, the `_from_json` legacy branch and `create`'s `rule_ids` parameter (the worker lists them in its report)
      - grep -rn "migrate_tokens\|apply_migration\|migrate_token_file\|_migrated\|_token_rule_entry\|_limited_tools" src/privacyfence prints nothing
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q -k "TestTokenRows or TestOwnLegacyRules or TestOwnedEntry" passes
      - python3 -m pytest tests/unit/test_gate.py -q -k "a_global_rule_never_covers_a_scoped_call or deleting_the_global_rule_leaves_the_token_rule_working or adding_a_verb_to_the_global_row_does_not_widen_the_token" passes and selects 3 tests
      - python3 -m pytest tests/unit/test_settings_controller.py -q -k update_rule_does_not_reach_a_token_copy passes
      - python3 -m pytest tests/unit/web/test_mcp_dispatch.py -q -k check_policy_under_a_token_predicts_from_the_token_rows passes
      - python3 -m pytest tests/unit/test_policy_upgrade.py tests/unit/test_daemon_main.py -q passes
      - python3 -m pytest tests/unit -q passes; ruff check . passes; python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass
      - python3 scripts/check_coverage_floor.py coverage.json passes after the full coverage run
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
      Read docs/token-owned-rules-plan.md, sections Design D10 and D11 (the tool-grouping and
      "(p3)" bullets), before changing anything.

      1. src/privacyfence/policy/catalogue.py: add `parse_value_list`, moved unchanged (body and
         docstring) from settings_controller.py `_parse_value_list` (line ~356). In
         settings_controller.py, delete the function and add
         `_parse_value_list = policy_catalogue.parse_value_list` to the alias block (line ~266).
         Its existing tests in tests/unit/test_settings_controller.py keep passing through the
         alias; leave them. Add to tests/unit/policy/test_catalogue.py:
         - `test_parse_value_list_splits_on_commas`;
         - `test_parse_value_list_keeps_a_single_value_group_whole`, with group "gmail.query" (the
           only id in `SINGLE_VALUE_GROUPS`).
      2. src/privacyfence/scoped_tokens.py: add `MAX_RULES_PER_TOKEN = 50`,
         `normalise_rule_requests` and the new `create` exactly as D10 says. The parameter `rules`
         replaces `rule_ids`. Follow steps 1-5 in that order, with the auto_accept read and the
         new-rule building before the lock. Use the error strings exactly as in D10.
      3. src/privacyfence/web/routes_settings.py: `_scoped_create_fingerprint_body` as D10 says.
         Change `ScopedTokenWiring.store_create`'s type and docstring, and make both create routes
         pass the normalised `rules`.
      4. src/privacyfence/web/server.py:
         - `build_scoped_token_wiring._create` takes `rules`;
         - `scoped_token_page_data` adds `"scope_groups"` with `_verb_tools`;
         - its `tools` and `auto_tools` use `_tool_connector`, both as D11 says.
      5. Tests:
         - test_scoped_tokens.py: replace every create call's `rule_ids=` with `rules=` requests,
           and add `class TestRuleRequests`.
           - normalise: sorting, dedupe, verbs sorted, and each malformed shape gives None
             (unknown kind, extra key, missing key, wrong type, not a list);
           - create:
             - a "new" request builds the row that `add_rules([], rules_for_catalogue_entry(...))`
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
             - a bad value is reported even when the label is also a duplicate (the D10 order);
             - `auto_accept.get_policy_rows` is not called while the store lock is held. Assert it
               with a fake `get_policy_rows` that checks `store._lock.locked()` is False.
         - tests/unit/web/test_routes_settings.py and test_routes_org_settings.py
           (TestScopedTokenRoutes):
           - the fingerprint covers `rules` (a changed rule request changes the fingerprint);
           - a body with `rule_ids` and no `rules` gets 400;
           - a malformed `rules` gets 400;
           - a created token's audit summary still counts rules;
           - a "new" request reaches the store.
         - tests/unit/web/test_server.py:
           - `scope_groups` lists only enabled connectors' groups: with connector_names={"drive"}
             a "drive.folder" entry is present and no gmail entry;
           - `verb_tools["read"]` of "drive.folder" contains "drive_get_file_content";
           - with connector_names={"apps_script"}, the `apps_script_*` tools are listed under
             "apps_script".

           Stop condition: if the "drive.folder" entry is missing while "drive" is enabled, stop
           with status=blocked (see the plan's Risks).
         - test_mcp_auth.py, test_oauth_provider.py and test_daemon_main.py: their
           `create(..., rule_ids=[], ...)` calls become `rules=[]`.
      6. Run:
         - `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`
           and `python3 scripts/mypy_strict_modules.py`;
         - the coverage run and `python3 scripts/check_coverage_floor.py coverage.json`.

         The HTML form still posts `rule_ids` and gets 400 until p4. That is expected between
         these phases. Do not touch settings_window_html.py.
    acceptance:
      - grep -rn "rule_ids" src/privacyfence/web/routes_settings.py src/privacyfence/web/server.py prints nothing
      - grep -n "rule_ids" src/privacyfence/scoped_tokens.py matches only `_LEGACY_KEYS`, `owned_entry` and the `_from_json` legacy branch
      - python3 -m pytest tests/unit/test_scoped_tokens.py -q -k TestRuleRequests passes
      - python3 -m pytest tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/web/test_server.py tests/unit/policy/test_catalogue.py -q passes
      - python3 -m pytest tests/unit -q passes; ruff check . passes; python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass
      - python3 scripts/check_coverage_floor.py coverage.json passes after the full coverage run

  - id: p3b-usage-stats-and-dead-code
    title: Usage counts leave scoped-token calls out; remove pairs_of and V2Migration's token-only fields
    depends_on: [p2-tokens-hold-rows]
    complexity: S
    touches:
      - src/privacyfence/audit_log.py
      - src/privacyfence/policy/scope_rows.py
      - tests/unit/test_audit_log.py
      - tests/unit/policy/test_scope_rows.py
    brief: |
      Read docs/token-owned-rules-plan.md, Design D9, and the last bullet of D6.

      1. src/privacyfence/audit_log.py `AuditLogger.rule_usage` (line ~982): skip an entry whose
         `str(data.get("agent_id") or "").startswith("scoped-token:")`, and add D9's sentence to
         the docstring.
      2. tests/unit/test_audit_log.py: add `test_rule_usage_leaves_out_scoped_token_calls`. Write
         two auto_accepted entries for the same rule id, one with agent_id "scoped-token:st-1" and
         one with agent_id "claude-code". The count is 1 and last_matched is the second entry's
         timestamp. Copy the pattern of the existing rule_usage tests in that file.
      3. src/privacyfence/policy/scope_rows.py:
         - Remove `pairs_of`, which nothing uses after p2 (check with
           `grep -rn "pairs_of" src tests`).
         - Remove the `V2Migration` fields `id_map`, `members` and `member_pairs`, and the code in
           `migrate_v2` that only builds them; keep `rows` and `personal_data_ids`. Remove them
           from `__all__`, and update the class docstring to "The result of `migrate_v2`: the rows,
           and the v2 rule ids that allowed personal data."
      4. tests/unit/policy/test_scope_rows.py: delete `test_pairs_of_matches_compile_row` and
         `test_pairs_of_an_unknown_row_is_empty`. Delete only the assertions on `id_map`,
         `members` and `member_pairs` (lines ~111, ~112, ~129, ~130, ~184), keeping each test's
         other assertions.
         Stop condition: if `grep -rn "id_map\|\.members\b\|member_pairs\|pairs_of" src/privacyfence`
         finds a user outside scope_rows.py, stop with status=blocked.
      5. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`
         and `python3 scripts/mypy_strict_modules.py`.
    acceptance:
      - python3 -m pytest tests/unit/test_audit_log.py -q -k rule_usage_leaves_out_scoped_token_calls passes
      - grep -rn "pairs_of\|member_pairs\|id_map" src/privacyfence/policy/scope_rows.py prints nothing
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
      Read docs/token-owned-rules-plan.md, section Design D12, plus D10's request shapes and D11's
      page data, before changing anything. Every string, data attribute and ui key you need is in
      D12. Use them exactly.

      1. src/privacyfence/settings_window_html.py:
         a. Add the five ui keys to the ui object (line ~351).
         b. Refactor `aaPairHint` into `pairHint(group, checkedVerbs, value)` (line ~2215), keeping
            `aaPairHint` as the one-line wrapper. The Auto-accept page's behaviour must not change.
         c. `renderScopedTokens`: build the Rules block from D12 items 1-5, and remove the
            dup-missing and dup-limited notices.
         d. `scopedTokenRow`: the detail list, as D12 says.
         e. Change these as D12 says: `duplicateScopedToken`, `clearScopedForm`,
            `scopedToolCoverage`, the add-covered handler and `submitScopedToken`. Leave
            `scopedToolStatus` alone if it reads coverage only through `scopedToolCoverage`.
         f. Event wiring, next to the existing `data-st-*` handlers (click ~2145, input/change
            ~2254-2290):
            - `data-st-new-group`: on change.
            - `data-st-new-value`: on input, as D12 says (set, re-render, re-focus, restore the
              caret, exactly like the `data-aa-value` branch at ~2287-2292).
            - `data-st-new-verb`, `data-st-new-add`, `data-st-new-remove` and `data-st-dup-rule`:
              on click, then re-render.
            - Keyboard: the chips and links use the same Enter/Space handling as the existing
              `data-st-rule` chips.
      2. tests/unit/test_settings_window_html.py TestScopedTokensSection:
         - Update DATA to the p2/p3 shape: token rules with title, sentence and tools, and a
           top-level `scope_groups` with `verb_tools`.
         - Add string-level tests for the hint text, the three sub-titles, `Add rule to token`,
           the empty-scope-groups hint, and the absence of "a rule that no longer exists" and
           "limited to".
         - Keep the escaping tests, and extend them to a scope group label and a value hint.
      3. tests/integration/test_settings_scoped_token_form_browser.py:
         - Update DATA the same way.
         - Rewrite `test_duplicate_prefills_the_form` so the duplicated token's rules are ticked
           `data-st-dup-rule` chips.
         - Add, using the existing `_capture_posts` helper:
           - `test_new_rule_is_sent_with_the_create_request`: pick a group, type a value, tick a
             verb, click Add rule to token, then create. The posted `rules` holds
             `{kind: "new", group, value, verbs}`.
           - `test_add_works_when_the_verb_is_ticked_before_the_value_is_typed`, for a
             needs-value group: the button enables as you type, and the click adds the rule.
           - `test_copied_rule_is_sent_as_copy`
           - `test_duplicated_rule_is_sent_as_duplicate` (with `token_id`)
           - `test_new_rule_tools_are_listed`: the tools from `verb_tools` show as tool chips.
           - `test_removed_new_rule_is_not_sent`
           - `test_add_is_disabled_without_a_verb`
      4. Run these, then `python3 -m pytest tests/unit -q`:
         - `ruff check .`
         - `python3 -m pytest tests/unit/test_settings_window_html.py -q`
         - `python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q`,
           with PRIVACYFENCE_TEST_CHROMIUM set if the session hook exported it. If the browser
           tests report SKIPPED, say so in your report: a skip is not a pass, and CI's run on the
           PR is then the check.
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
      2. sync/tests/test_permissions.py: update the assertion at line ~128 to the new text. If a
         test asserts `_NO_SUGGESTION_KEY`'s text (grep "Always allow"), update it too.
      3. sync/README.md: rewrite these passages, which say rules go in "Settings → Auto-accept →
         Add a rule":
         - "## The rules a recipe needs" (line ~100-107);
         - the paragraph after the permissions sample output (line ~820-825);
         - step 2 of "On a server, against organization mode" (line ~1019-1021): "Create each
           rule first, then the token that uses them" becomes "Add each rule on the token's form".

         They should say that the rules a scheduled run uses go on the recipe's scoped token
         (**Settings → Scoped tokens → New token**, under "New rule for this token" or "Copy from
         my Auto-accept rules"), and that Auto-accept rules serve only attended runs without a
         token. Add one sentence to "## The rules a recipe needs": "A token's rules belong to the
         token: an Auto-accept rule, including one the card's Always allow adds, does not reach
         it, and deleting an Auto-accept rule does not change it."
      4. Copy sync/README.md to
         plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md; the two
         must be identical.
      5. docs/privacyfence-sync.md "## The rules a recipe needs" (line ~55-62): make the same
         change in two sentences, linking connect-claude-code.md#scoped-tokens.
      6. In sync/, run `pip install -e "./sync[test]"`, then these must pass:
         - `pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-report=term-missing --cov-fail-under=100`
         - `mypy sync/src`
         - `bandit -r sync/src`
    acceptance:
      - pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-fail-under=100 passes
      - mypy sync/src passes; bandit -r sync/src passes
      - cmp sync/README.md plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md exits 0
      - grep -n "Create each rule first, then the token" sync/README.md prints nothing
      - grep -n "add one to the recipe's scoped token in Settings -> Scoped tokens" sync/src/privacyfence_sync/permissions.py matches

  - id: p6-retire-plan
    title: ADR 0234, reference docs, changelog, delete the plan, dispatch a build
    depends_on: [p1-exact-copies-and-no-pruning, p2-tokens-hold-rows, p3-create-from-rule-requests, p3b-usage-stats-and-dead-code, p4-new-token-form, p5-sync-wording]
    complexity: M
    touches:
      - docs/adr/0234-a-scoped-token-holds-its-own-rules.md
      - docs/adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md
      - docs/adr/0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md
      - docs/adr/0172-one-stored-auto-accept-rule-per-scope.md
      - docs/adr/0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md
      - docs/adr/0198-deleting-an-auto-accept-rule-removes-it-from-scoped-tokens.md
      - docs/adr/0220-a-routines-rule-profile-is-a-scoped-token.md
      - docs/adr/0228-an-extra-scope-can-give-each-verb-its-own-predicate.md
      - docs/adr/README.md
      - docs/connect-claude-code.md
      - docs/security-and-compliance.md
      - docs/how-it-works.md
      - docs/approvals-and-policy.md
      - docs/org-mode-setup-guide.md
      - docs/configuration-reference.md
      - CHANGELOG.md
      - docs/token-owned-rules-plan.md
      - docs/token-owned-rules-plan-manual-steps.html
    brief: |
      Read these first: docs/adr/README.md (rules and template), CONTRIBUTING.md "Decisions, plans
      and ADRs", and this plan's Design and ADRs sections. Write the ADR from this plan, and never
      link the plan document from it.

      1. Run `ls docs/adr` and take the next free number: 0234 unless another branch took it, in
         which case use that number everywhere below and in the file name.
         Write docs/adr/0234-a-scoped-token-holds-its-own-rules.md from the template:
         - **Status:** `Accepted — <today>. Implemented.`, then "Amends ADR 0164, ADR 0166, ADR
           0172, ADR 0173, ADR 0220 and ADR 0228. Supersedes ADR 0198."
         - **Context:** the coupling, and the user's two cases: a `privacyfence-sync` job with no
           AI model, and a Claude Code routine whose own environment reaches /mcp with a Bearer
           token and a narrow tool list.
         - **Decision:**
           - D1, plus D2-D3 in prose;
           - D5-D6: conversion at start-up only, limits applied, missing ids left out, the
             personal-data transfer kept;
           - D7: no fallback to the person's rules;
           - D8 and D9;
           - D10: the request kinds, and that the fingerprint covers them;
           - D12, in one paragraph.
         - **Alternatives considered:**
           - storing compiled rules on disk;
           - ids plus a pinned copy;
           - keeping the person's rules as a fallback;
           - a token-only flag on Auto-accept rules (still one list and one id space);
           - editing a token's rules in place (tokens stay immutable, and Duplicate covers
             changes);
           - converting on every Auto-accept write.
         - **Consequences:**
           - the person's other clients never gain access from a token rule;
           - deleting an Auto-accept rule no longer narrows a token, so revoking is the way;
           - an Auto-accept rule's Update rule does not reach a token;
           - "Matched Nx" no longer counts token calls;
           - an older PrivacyFence cannot read a version-2 token file: its tokens stop working
             there, and that version's next token change deletes them;
           - a future row-format rewrite must rewrite token rows.
         - **Verification:** the test classes and names that p1-p4 added.
         - **Related:** the ADRs and the source files.
      2. Change the Status lines only, never an ADR's body:
         - For 0164, 0166, 0172, 0173, 0220 and 0228, add "Amended by
           [ADR 0234](0234-a-scoped-token-holds-its-own-rules.md): a token holds its own copy of
           its rules."
         - For 0198, change the status to "Superseded by
           [ADR 0234](0234-a-scoped-token-holds-its-own-rules.md)."
         - In docs/adr/README.md, add the 0234 index row, and update the status column of those
           seven rows the same way.
      3. Reference docs. Describe today's behaviour, and link ADR 0234 for the why:
         - docs/connect-claude-code.md, both "### Scoped tokens" sections (local and organization
           mode):
           - say that the rules are the token's own (new or copied);
           - change the deletion sentence to "Deleting or changing an Auto-accept rule does not
             change a token";
           - drop "edit a rule first and then create the token";
           - keep Duplicate, which copies the token's rules.
         - docs/security-and-compliance.md "## Scoped tokens": change the "What a scope limits",
           "One token per routine" and "Deleting a rule" bullets. Add that an older version cannot
           read a token file written by this one: the tokens stop working there, and its next
           token change deletes them.
         - docs/how-it-works.md "### Scoped tokens": change "a list of the rules and tools" to
           "its own rules and a list of tools".
         - docs/approvals-and-policy.md:
           - line ~323: change "(scoped tokens, the audit log)" to "(the audit log)", and add
             "A scoped token's own rules have no Update rule button: create a new token, or
             Duplicate it.";
           - near the "Matched 42x" text (line ~305): add "Calls made with a scoped token are not
             counted here: they match the token's own rules."
         - docs/org-mode-setup-guide.md (lines ~349 and ~1033) and
           docs/configuration-reference.md (line ~292): change "limited to chosen rules and tools"
           (or that row's wording) to "with its own rules and a chosen list of tools".
      4. CHANGELOG.md: under `## [Unreleased]`, add this under a `### Changed` heading (create the
         heading there if it is missing):
         "- **A scoped token holds its own rules.** On the New token form you add rules for that
         token, or copy one of your Auto-accept rules. They never auto-accept anything for your
         other AI clients, and changing or deleting an Auto-accept rule no longer changes a token.
         Existing tokens get their own copy of the rules they used the first time PrivacyFence
         starts after the upgrade. An older version cannot read tokens saved by this one: they stop
         working there, and that version's next token change deletes them."
         Open no version heading.
      5. Run `git rm docs/token-owned-rules-plan.md docs/token-owned-rules-plan-manual-steps.html`.
         Then `grep -rn "token-owned-rules-plan" .` must print nothing.
      6. Commit and push. Then, with the GitHub MCP `actions_run_trigger` tool, dispatch
         `build.yml` (workflow_dispatch, no inputs) on your pushed branch. Put the run URL in your
         final report as `build_run: <url>`; the maintainer installs from it for the manual checks.
    acceptance:
      - test -f docs/adr/0234-a-scoped-token-holds-its-own-rules.md (or the number used), and grep -n "^Accepted" in it matches
      - grep -n "0234" docs/adr/README.md matches the index row and the 0164, 0166, 0172, 0173, 0198, 0220 and 0228 rows
      - grep -n "Superseded by \[ADR 0234\]" docs/adr/0198-deleting-an-auto-accept-rule-removes-it-from-scoped-tokens.md matches
      - grep -rn "edit a rule first and then create the token\|removes it from every scoped token" docs prints nothing outside docs/adr/
      - awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md | grep -n "A scoped token holds its own rules" matches
      - grep -rn "token-owned-rules-plan" . prints nothing
      - the final report has a build_run URL
```
