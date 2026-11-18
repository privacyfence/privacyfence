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

- None.
- review: The Source row uses a typographic apostrophe (server’s) where the plan text has a straight one; accepted, display only.

## p5-retire

- None.

## Final review

- None.

## Finishing

- scope: Scope drift disposition: tests/unit/test_upstream_mcp_client.py (p2) and tests/unit/web/test_mcp_dispatch.py (p3) stay in the final diff, accepted.
- final: Non-blocking findings fixed after the final review: ADR 0326 now cites the p2 test; a failed audiences refresh logs one warning (upstream id and kind); audiences_tool null is refused; the Admin Source row uses a straight apostrophe; the reference-upstream docs and docstring say audience_members is kept by reference. The loop tests stay in TestAudiencesLoop (named by ADR 0325).
