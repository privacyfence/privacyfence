# ADR 0166: A stored rule is widened to its verbs' current operations only by an explicit, confirmed update

## Status

Accepted — 2026-10-11. Implemented.
Amends ADR 0148. Its consequence "stored rules hold operation keys, so existing rules are not widened" still holds for every automatic path; this ADR adds the one explicit way to widen a rule.

## Context

A rule's id is derived from its predicate, value and conditions, and leaves the operations out
(ADR 0074); `policy.store.merge_rules` unions the operations of rules that share an id. The verbs a
rule shows are derived from the operations it already carries (`describe.rule_verbs`). So when a
release adds a tool under a verb a rule already has — `grist_import_csv` under a Grist document
rule saved before the CSV tools existed, which still shows *create* — the rule lacks the new
operation key and the tool is denied. The rule looks complete and is not. The only remedy was to
add the same rule again from the form, which nobody would guess; a scheduled `privacyfence-sync`
recipe was denied at a CSV import step this way.

ADR 0148 chose not to widen stored rules automatically, and that is right: a rule is the user's
consent, and a release must not enlarge it.

## Decision

- **Detection is pure and derived from the catalogues.** `policy.outdated.missing_operations(rule)`
  returns the operations the rule's own verbs imply today and it lacks. It uses the same sources the
  "add a rule" form compiles from (`propose.PROPOSABLE_SCOPES`, `catalogue.EXTRA_SCOPES`) and counts
  an entry only when its predicate, connector and conditions are the rule's. A predicate no catalogue
  knows, or a condition combination no entry has, reports nothing: it never guesses. It offers
  nothing for a verb the rule does not have, so a read-only rule is never offered a write tool.
  `missing_tools` names the tools those operations unblock.
- **The Auto-accept page shows the gap and offers one action.** A rule row with missing tools says
  "Newer tools aren't covered yet: …" and has an **Update rule** button whose confirmation names the
  tools. Nothing else changes the row.
- **The update adds only what detection computes.** `update_policy_rule(rule_id)` takes the id and
  nothing else from the client, computes the missing operations on the server, and adds them. The
  id, value, conditions and `allow_personal_data` are kept, so a scoped token that lists the id (ADR 0164)
  and the audit history keep working.
- **It needs the same step-up as adding a rule.** `update_policy_rule` is a sensitive action, so
  with `step_up.require_passkey` it asks for a fresh passkey assertion. It is routed in both modes
  with the scope `add_policy_rule` has (the signed-in principal's own rules); in organization mode
  the audit entry names the rule and the operations added.
- **No automatic widening.** Loading the config, starting the daemon, upgrading, and rendering the
  page never write a rule. Only the confirmed action does.

## Alternatives considered

- **Add the new operations to stored rules at load or upgrade.** It would enlarge consent without
  the user knowing, which ADR 0148 rules out.
- **Put the operations in the rule id.** It would change every stored id and break scoped tokens and
  audit attribution (ADR 0074, ADR 0164).
- **Let the client send the operations to add.** A forged request could then widen a rule beyond its
  verbs; computing them on the server removes that.
- **Do nothing and document "add the rule again".** The failure shows up in an unattended run, far
  from the cause.

## Consequences

- A rule saved before a tool existed shows why the tool is denied and how to fix it in one click.
- Widening stays a deliberate, passkey-protected act, and a rule's id never changes by it.
- A new tool whose operation no catalogue entry derives is not offered; that is a catalogue gap, not
  something this update papers over.

## Verification

- `tests/unit/policy/test_outdated.py` (detection, including the shared-predicate and condition cases).
- `tests/unit/test_settings_controller.py` (`TestUpdatePolicyRule`: exact additions, id and flag kept,
  no-ops, no widening on load, scoped token still matches).
- `tests/unit/web/test_routes_settings.py`, `tests/unit/web/test_routes_org_settings.py`,
  `tests/unit/web/test_org_settings_scope.py` (step-up, scope, audit, other principals).
- `tests/integration/test_settings_update_rule_browser.py` (notice, button, confirm).

## Related

- [`policy/outdated.py`](../../src/privacyfence/policy/outdated.py), [`settings_controller.py`](../../src/privacyfence/settings_controller.py)
- ADR [0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md), ADR [0148](0148-grist-bulk-writes-take-a-csv-file-and-are-reviewed-as-counts-and-a-sample.md), ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)
