# ADR 0172: One stored auto-accept rule per scope (settings schema version 3)

## Status

Accepted — 2026-10-13. Implemented.
Amends ADR 0074. A scoped rule's id is now derived from its scope and value, not from a predicate, value and conditions.

## Context

A rule's identity was its predicate, value, conditions and personal-data flag (ADR 0074), and one
Settings scope compiles to up to four predicates. A Drive folder, for example, becomes
`approved_folder` (read, download), `approved_sandbox_folder`, `parent_folder_allowlist` and
`move_within_approved_folders`. So the Auto-accept page could show the same folder twice: one rule
that allowed download, and another that allowed read and download with the personal-data box ticked.
Adding verbs to a scope never extended the rule that was already there, and Remove, scoped tokens
(ADR 0164) and the "Matched Nx" counts each had several ids to choose from.

## Decision

- **`settings.yaml` stores one record per (scope, value), version 3.** A row holds `id`, `scope`,
  `value`, `verbs`, `operations` and, when it has merged older rules, `former_ids`. The engine is
  unchanged: `policy.scope_rows.compile_row` compiles a row to the predicate rules the engine needs,
  all carrying the row's id, which ADR 0074 already allows.
- **The row id is `r-` plus ten hex digits of a hash of (scope, value).** Verbs and operations are
  not in it, so adding or removing verbs never changes the id.
- **Verbs are stored.** They are what keeps a shared operation key honest: `slack.read_messages`,
  `telegram.read_chat_messages` and `jira.read_issue` are reached by both a *read* and a *search*
  predicate, and only the stored verbs say which one a row means. Storing verbs and also operations
  keeps ADR 0166: a release that adds a tool widens no stored row.
- **Custom rows hold what the upgrade could not map.** An unknown predicate, a condition no
  catalogue entry has, or an operation a scope does not derive stays a custom row with its old id,
  predicate, operations and conditions. The seeded `contacts.edit` rule is one. Custom rows show no
  Edit verbs link (ADR 0174).
- **A row is stored only if it loads exactly.** `row_from_dict` fails closed for a missing id, empty
  operations, no scope and no predicate, a scope outside the catalogue, or a scoped row with no
  verbs.
- **The upgrade is lossless for an unscoped call.** `scope_rows.migrate_v2` groups v2 rules by
  scope and value, builds a candidate row, and merges the group only if compiling the row gives back
  exactly the group's predicates, operations and conditions. Otherwise each rule of the group stays
  its own custom row. The unscoped decision of every operation is unchanged by the upgrade (the one
  deliberate change is that the personal-data flag stops mattering, ADR 0173).
- **Two choices are stricter than the minimum.** `scope_rows.add_rules` turns a rule its scope
  cannot reproduce exactly into a custom row instead of narrowing it to what the scope offers, and
  `compile_row` compiles only the verbs an extra scope (Grist, Peek, Apps Script, the unconditional
  Gmail and Slack scopes) offers.
- **Old ids stay readable.** `former_ids` lists the v2 ids merged into a row, and the Settings
  "Matched Nx" count sums the row's id and its former ids.
- **The upgrade is token-aware and runs before anything can use the old ids.**
  `policy_upgrade.ensure_scope_rows(cfg, config_path, principal)` runs:
  - at `daemon_main.load_config`, for the local install and for each organization principal's lazy
    settings load;
  - at organization startup, in `_start_org_web_server`, through
    `daemon_main._upgrade_all_principals`, immediately before `WebServer(...)` is constructed. That
    is before the scoped token store that serves requests is built in `web/server.py`, so no scoped
    request is resolved against a token that still names old ids;
  - before every writer (`auto_accept`, `SettingsController`, the organization Settings route),
    on the config it re-read.

  If the tokens cannot be updated, the file stays version 2 and the writer reports that the rules
  could not be upgraded, so a write can never convert the rules without re-pointing the tokens.
- **Before the first conversion, the old file is copied to `settings.yaml.pre-scope-rules.bak`** and
  never overwritten.
- **Tokens only ever narrow.** `ScopedToken.rule_limits` lists, for each rule id a token carries,
  the `(predicate, operation)` pairs it may use. Token creation records a limit for every rule it
  chooses, from the row as it is then. The upgrade re-points a token's old ids at the merged row id
  and sets the limit to the union of the pairs the token's own old rules had. Because a row keeps
  its id when verbs are added, without a limit a recipe's token would silently gain whatever the
  person later adds to that row, which ADR 0164 rules out. The token page shows "(limited to: …)"
  only where the limit is narrower than the row is today.
- **Nothing widens a rule at startup or upgrade.** Merged rows carry exactly the operations the old
  rules had; a row that covers only part of a merged verb shows the notice and the **Update rule**
  button (ADR 0166).
- **Downgrading is not supported.** An older daemon compiles the custom rows and drops the scoped
  rows, which have no predicate; any rule write from it then rewrites the section as version 2 and
  loses them. The backup file is the way back: restore it by hand.

## Alternatives considered

- **Group v2 rules in the UI only.** Several ids would remain behind each row, and Remove, tokens and
  usage counts would stay ambiguous.
- **Store verbs only and derive the operations at load.** A release that adds a tool would widen
  every rule that has the verb, contrary to ADR 0166.
- **Let a token follow its row's growth.** A token would gain access without anyone creating or
  approving a new token, contrary to ADR 0164.
- **Widen tokens to the whole merged row on upgrade.** The same objection, applied at the moment of
  upgrade.
- **Merge groups that fail the lossless check by narrowing or widening them.** It could change an
  unscoped decision; keeping them custom costs only a second row.

## Consequences

- The Auto-accept page shows one row per scope, and adding verbs to a scope extends its row.
- The first start after the upgrade rewrites `settings.yaml` and may rewrite `scoped_tokens.json`;
  the backup file records the previous rules. The upgrade logs what it did and which tokens gained
  the personal-data allowance (ADR 0173).
- A token file that cannot be written leaves the rules at version 2 until the next start.
- Rules written by a downgraded daemon are lost on its first save; this is documented, not handled.

## Verification

- `tests/unit/policy/test_scope_rows.py` (`test_each_catalogue_group_round_trips_through_migration`,
  `test_upgrade_round_trip_preserves_every_rule`, `test_screenshot_case_merges_to_one_row`,
  `test_partial_verb_is_not_widened`, `test_row_from_dict_fails_closed`, the shared-key cases,
  `test_pairs_of_matches_compile_row`).
- `tests/unit/test_policy_upgrade.py` (conversion and backup, backup not overwritten, token failure
  leaves version 2, logging of flagged token labels).
- `tests/unit/test_daemon_main.py` (`TestScopeRowsUpgrade`:
  `test_upgrade_all_principals_runs_before_the_token_store_loads`,
  `test_unscoped_decisions_unchanged_by_upgrade`, `test_fresh_install_seed_is_already_version_3`).
- `tests/unit/test_scoped_tokens.py` (limits recorded at creation, `migrate_token_file` replacing old
  ids, idempotence, partial references) and `tests/unit/test_gate.py`
  (`test_adding_a_verb_to_a_row_does_not_widen_an_existing_token`).
- `tests/unit/test_auto_accept.py` (`test_adding_a_verb_to_a_scope_extends_its_row`) and
  `tests/unit/test_settings_controller.py` (`test_two_adds_on_one_scope_leave_one_row`,
  `test_screenshot_case_renders_one_row`, `test_usage_sums_former_ids`).
- `tests/unit/test_docs_configuration_reference.py`.

## Related

- [`policy/scope_rows.py`](../../src/privacyfence/policy/scope_rows.py), [`policy_upgrade.py`](../../src/privacyfence/policy_upgrade.py), [`scoped_tokens.py`](../../src/privacyfence/scoped_tokens.py)
- ADR [0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md), ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), ADR [0166](0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md), ADR [0171](0171-auto-accept-rules-use-nine-verbs.md), ADR [0173](0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md)
