# Deviations: scoped-token-gated-upstream-tools

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-token-only-approval-rule

- retry: Worker reported status=blocked only because the tld plugin was absent in its container (guard, dod and roles verify could not run); no roles are required for this phase. Orchestrator reviewed the diff and ran ruff, the full unit suite, mypy, strict mypy and bandit on the merged tree: all green. The coverage-floor row is left to CI.

## p2-create-with-approval

- worker: tests/unit/test_upstream_mcp_registry.py (not in touches; p1 owned it) had one test changed from the review tool kh_search to the auto tool kh_list_topics, because creating a token for a gated tool with no approval is now refused. Same intent, no collision.
- scope: scope: tests/unit/test_upstream_mcp_registry.py (the test fix above; accepted)
- retry: tld plugin absent in the worker container; the worker ran the verify.dod commands directly and reported done. No roles are required for this phase. Orchestrator re-ran ruff and the full unit suite on the merged tree: green.

## p3-refuse-before-forwarding

- worker: Three existing connector tests encoded the old scan and metadata behaviour and were updated (one asserting text-only scanning when structured content also exists was removed, one asserting indented JSON, one asserting metadata without gate). All are inside touches and follow plan 3.6.
- retry: tld plugin absent in the worker container; the worker ran the verify.dod commands directly and reported done. No roles are required for this phase. Orchestrator re-ran ruff and the full unit suite on the merged tree: green.

## p4-token-form

- pending

## p5-end-to-end

- pending

## p6-retire-adr-docs-changelog

- pending

## Final review

- pending

## Finishing

- pending
