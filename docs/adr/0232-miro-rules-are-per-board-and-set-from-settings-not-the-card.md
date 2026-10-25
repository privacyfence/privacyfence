# ADR 0232: Miro auto-accept rules are per board and are set on Settings, never from the approval card

## Status

Accepted — 2026-10-25. Implemented: the `miro.board` scope.

## Context

A Miro rule has to name something stable. Miro boards have ids, names and links; names change and
links carry more than the id. Every rule value is validated when the rule is saved, and
`privacyfence_check_policy` suggests rules from the arguments of an uncovered call. The approval
card proposes "Always allow" only from a fixed set of scopes, and the extra scopes (Apps Script,
Grist, Peek) are not in it.

## Decision

- **The scope is `miro.board`,** an identity scope on the call's `board_id`, compared verbatim. It
  covers the read, add and update verbs.
- **Rules are made on Settings > Auto-accept,** through `privacyfence_propose_policy_change`, or
  from a `privacyfence_check_policy` suggestion. The approval card never offers "Always allow" for
  a Miro operation.
- **A rule value must be a board id.** A board link is refused when the rule is saved
  (`policy/value_format.py`, reusing `miro_client.BOARD_ID_RE`). The tools take ids only, so a
  suggestion's value is always one a rule can hold.
- **The scope is in the extra-scope tables,** not in `SCOPE_SELECTORS`' frozen v1 set.

## Alternatives considered

- **Accepting board links in the tools and rewriting them to ids** — the rule side refuses URLs, so
  `privacyfence_check_policy` would suggest rules that cannot be saved.
- **Adding the scope to the frozen v1 set** — that set is fixed
  ([ADR 0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md),
  [ADR 0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md) are the
  precedent).
- **Resolving a board's name on the Settings page** — a rule shows the board id, as Peek's rules
  show page ids; the resolver Grist has is not copied.

## Consequences

- A rule is made once on Settings, where its scope is visible, rather than by a click on a card.
- A rule for a board does not follow a board that is copied: the copy has another id.
- The tools refuse a link, so an assistant that has only a link must list the boards first.

## Verification

- `tests/unit/policy/test_value_format.py`: ids accepted, links and names refused, and the format
  is `miro_client.BOARD_ID_RE`.
- `tests/unit/policy/test_scopes.py`, `test_catalogue.py`, `test_suggest.py`, `test_propose.py` and
  `test_registry.py`: the scope, its verbs, the suggestion by `board_id`, and no card proposal.
- `tests/unit/test_systemic_gate_invariants.py`.

## Related

- ADR [0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md),
  ADR [0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md),
  ADR [0204](0204-check-policy-names-the-rules-that-would-cover-an-uncovered-call.md),
  ADR [0228](0228-an-extra-scope-can-give-each-verb-its-own-predicate.md)
- [`policy/scopes.py`](../../src/privacyfence/policy/scopes.py),
  [`policy/catalogue.py`](../../src/privacyfence/policy/catalogue.py),
  [`policy/value_format.py`](../../src/privacyfence/policy/value_format.py)
