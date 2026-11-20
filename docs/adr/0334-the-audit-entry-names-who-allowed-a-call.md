# ADR 0334: The audit entry names who allowed a call

## Status

Accepted — 2026-11-20. Implemented.

## Context

An audit entry said `auto_accepted` and, since ADR [0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md), the id of the rule. It did not say
whose rule it was, and once an administrator can set rules for everyone
([ADR 0333](0333-organization-auto-accept-rules-are-set-by-an-administrator.md)) the audit file has
to show clearly whether a person or a rule allowed each call, and whose rule. Rule ids hash scope
and value only, so a person's rule and an organization rule can share an id, and an organization
match would otherwise raise the "Matched Nx" count of the person's rule.

## Decision

- **A new field `allowed_by`** on every audit entry (schema version 9). Stored values: `person`
  (a click, the "allow all" choices and the temp-accept window), `person_rule`, `org_rule`,
  `token_rule` and the empty string for decisions nobody allowed. `audit_log.allowed_by_for` derives
  it at write time from the decision and the source of the matching rule list.
- **Old rows are inferred at read time** by `audit_log.effective_allowed_by`: the stored value when
  set; `person` for the person decisions; for `auto_accepted`, `token_rule` for a scoped-token agent,
  `person` for the temp-accept window, `no_approval_needed` for the tools that never ask (the
  connector's own `auto` rows), else `person_rule` when a rule id or name is present. The file on
  disk is never rewritten and the hash chain is unchanged: a schema 8 line verifies as before.
- **The Excel export gains the column `Allowed By`**, last, shown as `Person`, `Person's own
  auto-accept rule`, `Organization auto-accept rule`, `Scoped token's rule` or `No approval needed`.
  Rows allowed by an organization rule have their own fill.
- **`rule_usage` is split by source.** It counts `person_rule` entries by default, so an
  organization match never counts for a person's rule with the same id.
- **Changes to organization rules** are audited with the decisions `org_rule_added`,
  `org_rule_removed` and `org_rule_verbs_changed`.

## Alternatives considered

- **A new decision string `auto_accepted_org`** — it breaks every consumer that matches
  `auto_accepted`: usage counts, the approved-like decision set and forwarding filters.
- **Inferring the source only at read time** — a rule that is removed or renamed later cannot be
  attributed, and the temp-accept window is indistinguishable from a person rule.
- **Rewriting old files** — it would break the hash chain.

## Consequences

- Consumers that ignore unknown fields keep working; consumers can group by `allowed_by`.
- An old log reads clearly in the export, with the inference stated by the code, not the file.
- A forwarded entry carries the field.

## Verification

- `tests/unit/test_audit_log.py`: `TestAllowedBy`, `TestAllowedByExport`, `TestRuleUsageBySource`.
- `tests/unit/test_gate.py`: `TestOrgRules` (the source on each audit row).
- `tests/unit/web/test_routes_admin.py`: `TestOrgRuleActions` (the three decisions).

## Related

- ADR [0071](0071-audit-log-integrity-is-a-keyed-hash-chain-plus-off-host-forwarding.md),
  [0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md),
  [0333](0333-organization-auto-accept-rules-are-set-by-an-administrator.md)
- Issue [#1167](https://github.com/privacyfence/privacyfence/issues/1167), plan
  [`docs/1167/plan.md`](../1167/plan.md)
- [`audit_log.py`](../../src/privacyfence/audit_log.py)
