# ADR 0193: The Salesforce object-type rule also covers creating and updating records of that type

## Status

Accepted — 2026-10-16. Implemented: `src/privacyfence/policy/propose.py` (`approved_object_types`),
`src/privacyfence/auto_accept.py` (`TOOL_TO_OPERATION`).

## Context

Salesforce already has an "Always allow" rule per object type (`approved_object_types`) for reads
and searches. With four write tools, the question is whether the same rule can cover writes, and
how an existing rule behaves. [ADR 0166](0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md)
says a stored rule is widened only by an explicit, confirmed update, and
[ADR 0171](0171-auto-accept-rules-use-nine-verbs.md) gives rules nine verbs, among them `add` and
`update`.

## Decision

- **`approved_object_types` also takes the verbs `add` and `update`.** Its verbs are read, search,
  add and update; `salesforce.run_report` stays excluded. The scope matcher reads `object_type`
  from the call's args, which every write tool passes, and for a batch the one object type of the
  call must be in the rule.
- **The approval card offers this rule for a write**, as Jira's project scope and Confluence's
  space scope are offered. The scope is already one of the frozen `SCOPE_SELECTORS`.
- **Each write tool has its own operation key** (`salesforce.create_record`,
  `salesforce.create_records`, `salesforce.update_record`, `salesforce.update_records`), so a later
  rule can leave the batch tools out with one `excludes` entry without changing any key.
- **Existing rules are not widened.** A rule saved before this change keeps the verbs it has. Its
  owner adds `add` or `update` with **Edit verbs** on Settings → Auto-accept
  ([ADR 0174](0174-a-rules-verbs-are-edited-in-place.md)).

## Alternatives considered

- **Settings-only write rules, as for Grist ([ADR 0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md))** —
  Grist needed a new scope outside the frozen `SCOPE_SELECTORS`. `approved_object_types` is already
  in that set and is proposable for reads, so the card can offer it, as Jira and Confluence offer
  their write rules.
- **One operation key for all four tools** — a rule could not allow single-record writes and keep
  asking for batches.
- **Widening stored rules on upgrade** — it would turn a read-only trust decision into permission to
  change data, which ADR 0166 rules out.

## Consequences

- A user who trusts an object type such as Opportunity can let an assistant update its records
  without a card, with every call still audited.
- A rule that holds read and search only does not allow a write, however old it is.
- A batch create or update is allowed by a rule only when all of its records are of the one object
  type the rule names.
- The approval binding still ties an approval to the whole request ([ADR 0175](0175-an-approval-is-bound-to-the-whole-request-and-what-its-card-showed.md));
  a rule is the only way to skip the card.

## Verification

`tests/unit/policy/test_propose.py` (the Salesforce write tools in the per-tool context list),
`tests/unit/policy/test_registry.py` (`Verb.ADD` and `Verb.UPDATE` for the four tools),
`tests/unit/test_auto_accept.py` (operation keys),
`tests/unit/test_generate_always_allow_reference.py` for `docs/always-allow-rules-reference.md`.

## Related

[ADR 0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md),
[ADR 0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md),
[ADR 0166](0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md),
[ADR 0171](0171-auto-accept-rules-use-nine-verbs.md),
[ADR 0174](0174-a-rules-verbs-are-edited-in-place.md),
[ADR 0175](0175-an-approval-is-bound-to-the-whole-request-and-what-its-card-showed.md),
[ADR 0191](0191-salesforce-writes-create-and-update-one-record-or-a-batch-of-up-to-200-all-or-none-and-never-delete.md),
`src/privacyfence/policy/propose.py`, `src/privacyfence/auto_accept.py`.
