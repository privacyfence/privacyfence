# ADR 0144: Grist rules are per document and set from Settings, not from the approval card

## Status

Accepted — 2026-10-10. Implemented.

## Context

Auto-accept rules need a scope that says which Grist data they cover. A document is the natural
unit: it is what a person shares and what a Grist access rule protects. The approval popup can
offer "Always allow", but only for the scopes in `policy/scopes.SCOPE_SELECTORS`, which is the
frozen set checked against the v1 reference (`tests/unit/policy/_v1_reference.py`).

## Decision

- Grist rules are scoped by `grist.document`, an identity scope resolved from the call's `doc_id`
  argument. It is in `NEW_SCOPE_SELECTORS` and offered through `policy/catalogue.EXTRA_SCOPES`, with
  the verbs read, create, update and restructure.
- It has no `policy/propose.PROPOSABLE_SCOPES` entry, so the popup never offers "Always allow" for a
  Grist operation. Rules are made on **Settings > Auto-accept** or through
  `privacyfence_propose_policy_change`.
- The rule value is the full Document ID, the same id the tools take everywhere.

## Alternatives considered

- **Adding the scope to `SCOPE_SELECTORS`** — that set is frozen for v1 equivalence; a new scope
  would break the check that guards it.
- **Proposing from the popup anyway** — the popup proposes only from `SCOPE_SELECTORS`; ADR 0077 is
  the precedent for leaving extra-scope operations out.

## Consequences

Making a Grist rule takes a trip to Settings. A rule never widens by accident from a single approval.

## Verification

- `tests/unit/policy/test_scopes.py`: `test_grist_document_matches_doc_id_from_args`.
- `tests/unit/policy/test_catalogue.py`: the `grist.document` entry and its compiled rules.
- `tests/unit/policy/test_propose.py`: `test_the_extra_scope_operations_stay_unproposed` includes
  every Grist write and review tool.
- `tests/unit/test_gate.py`: a stored `grist.document` rule auto-accepts only that document.

## Related

- [`policy/scopes.py`](../../src/privacyfence/policy/scopes.py), [`policy/catalogue.py`](../../src/privacyfence/policy/catalogue.py)
- ADR [0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md)
