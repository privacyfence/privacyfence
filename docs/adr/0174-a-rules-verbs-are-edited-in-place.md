# ADR 0174: A rule's verbs are edited in place

## Status

Accepted — 2026-10-13. Implemented.
Amends ADR 0166. An explicit verb edit adds a new verb's full tool set; it never completes a verb the rule already had.

## Context

The only way to change a rule's verbs was to remove the rule and add it again, which for a rule
that scoped tokens and the audit history refer to meant a new id. With one rule per scope (ADR
0172) the id no longer depends on the verbs, so a rule can be changed without losing what points at
it. ADR 0166 allows widening a stored rule only through an explicit, confirmed action; an edit is
another such action and has to say what it adds.

## Decision

- **The Auto-accept page has an "Edit verbs" link on each scoped rule row.** It shows the scope's
  verbs as toggles, pre-selected with the row's verbs, and Save is disabled while none is selected.
  Saving asks for confirmation, naming the verbs, and then goes through the same step-up as adding a
  rule. The action is `set_policy_rule_verbs`, routed in both modes with the scope
  `update_policy_rule` has, and in organization mode it is audited.
- **`scope_rows.set_verbs` decides the new operations.** A verb that was already there keeps exactly
  the operations it had: no silent completion, that is still **Update rule**. A new verb gets all of
  its tools. A removed verb's operations go, unless a kept verb also implies them
  (`calendar.create_modify_event` is implied by both *add* and *update*). The row's id and
  `former_ids` are kept. It refuses an unknown row, a custom row, an empty verb list and a verb the
  scope does not offer.
- **Tokens are unaffected by an added verb and lose what is removed**, because of their
  `rule_limits` (ADR 0172).
- **A move needs both folders or lists, but only where the move has a destination.** The "needs two
  ids" requirement applies only to `drive.folder` and `tasks.list` (one shared JavaScript constant,
  `AA_MOVE_PAIR_SCOPES`), whose moves have a source and a destination. Gmail sender and
  sender-domain rules include `move` (the former *archive*), which has no destination, so they
  toggle it freely with one value. On a `drive.folder` or `tasks.list` row with fewer than two ids,
  the move toggle is disabled only for turning move on; a row that already has move can still switch
  it off, because removing a verb only narrows.

## Alternatives considered

- **Recompute all operations on every edit.** It would widen verbs the user did not touch, the
  silent completion ADR 0166 rules out.
- **Apply the two-id rule to every valued scope.** It would block a Gmail sender rule from gaining
  *move* for no reason, since archiving has no destination to check.

## Consequences

- Narrowing or extending a rule no longer means removing and re-adding it, so its id, its usage
  history and the tokens that name it survive.
- Adding a verb is a deliberate, confirmed, passkey-protected act, as widening already is.
- A rule that covers only part of a kept verb stays partial after an edit.

## Verification

- `tests/unit/policy/test_scope_rows.py` (`test_set_verbs_keeps_existing_operations_of_kept_verbs`,
  `test_set_verbs_adds_full_operations_for_new_verbs`,
  `test_set_verbs_keeps_shared_operation_implied_by_kept_verb`,
  `test_set_verbs_refuses_empty_unknown_custom_or_foreign_verbs`).
- `tests/unit/test_auto_accept.py` and `tests/unit/test_settings_controller.py` (the
  `set_policy_rule_verbs` cases: add, remove, refusal writes nothing, no-op).
- `tests/unit/web/test_org_settings_scope.py`, `tests/unit/web/test_routes_settings.py` (routed in
  both modes, step-up, audit).
- `tests/unit/test_settings_window_html.py` (`test_edit_verbs_hooks_and_confirm_text_are_present`,
  `test_move_needs_two_ids_hint_and_block`,
  `test_the_move_pair_rule_names_exactly_drive_folder_and_tasks_list`).
- `tests/integration/test_settings_edit_verbs_browser.py` (adding *update* changes the chips; Save
  disabled with no verb; a Drive folder row with one id cannot turn move on; a Gmail sender-domain
  row with one value can).

## Related

- [`policy/scope_rows.py`](../../src/privacyfence/policy/scope_rows.py), [`settings_window_html.py`](../../src/privacyfence/settings_window_html.py)
- ADR [0166](0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md), ADR [0172](0172-one-stored-auto-accept-rule-per-scope.md)
