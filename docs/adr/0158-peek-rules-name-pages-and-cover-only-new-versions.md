# ADR 0158: Peek rules name pages and cover only new versions

## Status

Accepted — 2026-10-11. Implemented.

## Context

No auto-accept scope covered a Peek tool, so a scheduled run that refreshes a dashboard needed a
person for every upload. Publishing is egress to people, so the rule has to be narrow and
deliberate.

## Decision

A `peek.page` scope ("Peek — page") is keyed by page slug. *Read* covers `peek_get_comments` and
`peek_get_stats` for the named pages; *update* covers `peek_upload_page` with a `slug` that is
named, which uploads a new version. A call without a slug creates a new page and never matches, so
no rule covers a new page, a visibility change, a rename or a delete. The rule is set from Settings
only; the popup offers no Peek "Always allow" button. The personal-data checkbox (ADR 0157) is
offered here as "New versions may include personal data".

## Alternatives considered

- **A slug rule that also covers new pages.** A new page has no slug until it is created, so there is
  nothing to name; covering every new page would be an open-ended publish rule.
- **A Peek "Always allow" button on the card.** Publishing is egress to people; the rule is set
  deliberately in Settings, as for Grist documents (ADR 0144).

## Consequences

- A dashboard can be republished unattended; creating it the first time stays an approval.
- `peek_upload_page` with an unknown slug still fails before the gate, as before.
- `privacyfence_propose_policy_change` can propose a `peek.page` rule, without the flag.

## Verification

- `tests/unit/policy/test_scopes.py` (`test_peek_page_*`), `tests/unit/policy/test_catalogue.py`
  (`test_peek_page_*`), `tests/unit/test_gate.py` (`test_stored_peek_page_rule_covers_a_new_version_only`,
  `TestPersonalDataRules`).

## Related

- [`policy/scopes.py`](../../src/privacyfence/policy/scopes.py), [`policy/catalogue.py`](../../src/privacyfence/policy/catalogue.py),
  [`connectors/peek.py`](../../src/privacyfence/connectors/peek.py)
- ADR [0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md), ADR [0157](0157-a-rule-can-let-personal-data-through-tools-whose-result-holds-no-row.md)
