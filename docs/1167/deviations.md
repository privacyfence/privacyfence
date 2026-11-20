# Deviations: org-auto-accept-rules

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-audit-allowed-by

- None.
- review: Both phase merges were verified together (tests/unit and ruff green after the second merge) and the scope check ran after merging, so it recorded skipped; the diffs were checked by hand to stay inside touches.

## p2-org-rules-store

- None.

## p3-approvals-principal-and-source

- retry: Merge turned tests/unit/test_gate.py TestRulesChangedReevaluation red because gate.py (phase p4) still passes a 2-tuple callback; merge undone and p3 sent back to accept a 2-tuple callback until p4 lands.
- worker: Re-evaluation helper accepts both 2-tuple and 3-tuple callbacks until the gate (p4) moves to 3-tuples.

## p4-gate-org-rules

- None.

## p5-person-and-ai-surfaces

- None.

## p6-admin-backend

- None.

## p7-admin-and-person-ui

- pending

## p8-token-copy

- pending

## p9-retire-docs-adrs

- pending

## Final review

- pending

## Finishing

- pending
