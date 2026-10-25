# ADR 0198: Deleting an auto-accept rule removes it from scoped tokens

## Status

Accepted — 2026-10-17. Implemented.
Amends ADR 0164 and ADR 0172. A deleted rule no longer simply stops matching: its id is also dropped from every scoped token that carried it.

## Context

A scoped token names the auto-accept rules it may use by id (ADR 0164). A rule deleted in Settings
or through `privacyfence_propose_policy_change` stopped matching, but the token kept its id. A
rule's id is derived from its scope and value (ADR 0172), so adding the same scope and value again
gives the same id, and the token silently had its old access back. The person who deleted the rule
to take that access away did not expect a later rule to restore it for a job they had forgotten.

## Decision

When a rule row is deleted, its id is dropped from every scoped token of that principal, together
with the rule's limit (`rule_limits`). The token is not revoked: its secret, expiry, other rules,
tools and personal-data setting stay, and a token left with no rules and no tools stays too.

Three paths delete a row and all three do this: `auto_accept.remove_policy_v2_rule` (the
organization Settings action and the bridge `remove`), `SettingsController.remove_policy_rule`
(local Settings) and the `update` operation of `privacyfence_propose_policy_change`. An `update`
removes the row and adds one built from the new group and value, so it forgets the old id only when
that id is absent from the rows after the whole operation; a change of verbs on the same value keeps
the id and the token's access. Changes that keep the id (`update_policy_rule`,
`set_policy_rule_verbs`) are not deletions and do not touch tokens.

Tokens that already carry a deleted rule's id are not touched; they keep showing "a rule that no
longer exists".

## Alternatives considered

- **Revoke the whole token.** It would break a scheduled job that has other working rules and tools,
  to fix a problem with one rule.
- **Prune dead ids lazily, when a token is read.** The revival happens when the rule is added again,
  before any read.
- **Prune at startup.** It does not stop a delete followed by an add in one session.
- **Do nothing.** A token's access would depend on whether a rule with the same id exists later.

## Consequences

- Adding the same rule again after a delete does not restore any token's access; the person creates
  a new token or a duplicate with the rule.
- The token write happens after the rule delete is saved. If it fails, the failure is logged and the
  delete stands (fail open): the revival risk remains until the token file is fixed and the rule is
  deleted again or the token revoked.
- Deleting a row by editing `settings.yaml` by hand is not covered; nothing runs on that path.
- No audit schema change, no new setting and no UI change.

## Verification

- `tests/unit/test_scoped_tokens.py`: `TestForgetRules`.
- `tests/unit/test_auto_accept.py`: `test_remove_policy_v2_rule_drops_the_id_from_a_token_that_carried_it`,
  `test_removing_an_unknown_id_leaves_the_token_file_alone`,
  `test_forget_in_tokens_false_leaves_tokens_alone`,
  `test_a_failing_token_write_does_not_undo_the_delete`.
- `tests/unit/test_gate.py`: `TestProposePolicyChange` (`update` to another folder, `update` of only the verbs, `remove`).
- `tests/unit/test_settings_controller.py`: `test_remove_drops_the_id_from_a_token_that_carried_it`,
  `test_adding_the_same_scope_again_does_not_restore_the_token_access`.
- `tests/unit/web/test_routes_org_settings.py`: `TestRemoveRule.test_drops_the_rule_from_that_principals_tokens_only`.

## Related

- [`scoped_tokens.py`](../../src/privacyfence/scoped_tokens.py), [`auto_accept.py`](../../src/privacyfence/auto_accept.py), [`gate.py`](../../src/privacyfence/gate.py), [`settings_controller.py`](../../src/privacyfence/settings_controller.py)
- ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), ADR [0172](0172-one-stored-auto-accept-rule-per-scope.md)
