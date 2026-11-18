# Deviations: upstream-audiences

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-config-and-parser

- None.
- review: A key set to null (audiences_tool: null) is read as absent rather than refused; accepted, non-blocking.

## p2-reference-upstream

- None.
- retry: Worker sent back twice: it first skipped the dod table and report shape, then reported blocked on the one test the new tool broke; both fixed on the phase branch.
- scope: tests/unit/test_upstream_mcp_client.py: one-line expectation of the reference upstream tool list updated for list_audiences; accepted.

## p3-runtime

- worker: Also updated one expected status() row in tests/unit/web/test_mcp_dispatch.py for the new audiences_source key.
- scope: tests/unit/web/test_mcp_dispatch.py: one expected row gains audiences_source; accepted.
- retry: Sent back once: two exact status() assertions in test_mcp_dispatch.py failed in the full unit suite; fixed on the phase branch.

## p4-admin-page-and-integration

- pending

## p5-retire

- pending

## Final review

- pending

## Finishing

- pending
