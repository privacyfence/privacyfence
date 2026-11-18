# Deviations: upstream-audiences

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-config-and-parser

- None.
- review: A key set to null (audiences_tool: null) is read as absent rather than refused; accepted, non-blocking.

## p2-reference-upstream

- None.
- retry: Worker sent back twice: it first skipped the dod table and report shape, then reported blocked on the one test the new tool broke; both fixed on the phase branch.

## p3-runtime

- pending

## p4-admin-page-and-integration

- pending

## p5-retire

- pending

## Final review

- pending

## Finishing

- pending
