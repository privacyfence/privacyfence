# ADR 0074: Auto-accept rules are identified by a content-derived id, and decisions are attributed by it

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-18 in the policy v2
redesign's rule-attribution phase, implemented in `ee93518a` and merged in
[#541](https://github.com/privacyfence/privacyfence/pull/541); simplified in `081c1fe4` when the v1
engine was retired, ADR 0004). Implemented.

## Context

An auto-accepted decision in the audit log should answer the question "which rule let this
through?" The Auto-accept Settings page needs the same answer for each rule's match count, last
match and never-matched flag (`AuditLogger.rule_usage()`), and Remove needs it to name one row.

The only identifier an audit entry had was `auto_accept_rule`, a rule *name*. Names repeat. Under
the v1 model, every rule built from one predicate carried that predicate as its name, so
`approved_sandbox_folder` for folder F1 and `approved_sandbox_folder` for folder F2 were the same
string in the log. A per-rule count keyed on that string merges different rules, and removing a
rule found that way could remove the wrong one.

## Decision

- **A rule's id is derived from what it means.** `policy.store.rule_id_for(predicate, value,
  conditions)` is `r-` followed by the first ten hex digits of the SHA-256 of
  `repr((predicate, sortable(value), conditions))`. A list value is order-independent, and the
  operations the rule covers are not part of the id. `merge_rules` gives every rule PrivacyFence
  writes this id (Settings, the MCP bridge, the popup's "Always allow", and migration), so two
  rules with the same meaning are one row. `rule_id_for_rule(rule)` recomputes it for a rule whose
  own `.id` cannot be trusted.
- **Decisions are attributed by that id.** `gate.py`'s `_evaluate_auto_accept` returns the matched
  row's `.id`, which is the id Settings lists and `remove_policy_rule` looks up, and `_audit`
  writes it to `AuditEntry.rule_id` (audit schema version 4). `rule_usage()` groups by `rule_id`.
- **Attribution never guesses.** `rule_id` is `""` for every decision other than
  `auto_accepted`, for entries recorded before the field existed, and for the
  `session_temp_accept` grace window, which is not a stored rule. `policy.engine.find_matching_rule`
  returns the matching rule object, not an id looked up again, so two rules that share an `.id`
  cannot be confused.

Only one rule engine exists (ADR 0004), so there is no second engine whose match could disagree.

## Alternatives considered

- **Attribute by rule name.** This is what `auto_accept_rule` held. Names repeat, as shown above, so
  a count or a Remove keyed on a name reaches every rule that shares it.
- **A random id minted when a rule is created.** It would be unique, but the same rule added twice,
  or re-created after removal, would get a new id and lose its usage history. `merge_rules` could no
  longer merge two rules with the same meaning into one row.

## Consequences

- A rule keeps its id, and its usage history, across edits that only add or remove operations.
  Changing its predicate, value or conditions makes it a different rule with a new id.
- `settings.yaml` is trusted as it is: a hand-edited `id` that is not the content hash is recorded
  and listed as written. Recomputing the id at match time would stop the audit log agreeing with
  the row Settings shows.
- A usage count cannot be carried back to entries recorded before `rule_id` existed.

## Verification

- `tests/unit/test_gate.py`'s `TestRuleIdAttribution`: a store-rule match records the canonical id,
  the temp-accept window records none, and the review gate's race recheck carries the id.
- `tests/unit/policy/test_store.py`: `rule_id_for` is stable and order-independent and depends
  only on predicate, value and conditions (`TestRuleIdForRule` included).
- `tests/unit/policy/test_engine.py`'s
  `test_picks_the_same_rule_evaluate_reports_by_id_when_ids_collide`.
- `tests/unit/test_daemon_main.py`: migrated rules carry canonical ids.

## Related

- [ADR 0004](0004-retire-the-v1-auto-accept-config-model.md): the retired v1 model whose shared
  names made name-based attribution ambiguous.
- [ADR 0071](0071-audit-log-integrity-is-a-keyed-hash-chain-plus-off-host-forwarding.md): the audit
  log this field is written to.
