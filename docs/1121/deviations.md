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

- None.
- review: The approvals line is shown in the token list row, not inside the "Rules and tools" details (the worker read "token list line" that way); accepted.
- retry: tld plugin absent in the worker container; the worker ran the verify.dod commands directly and reported done. No roles are required for this phase. Orchestrator re-ran ruff, the full unit suite and all 29 browser tests (with the Chromium path override) on the merged tree: green.

## p5-end-to-end

- worker: The sample proposal text "contact: bob@example.org" is not detected as personal data by the PII scan (an email alone is not a category), so the review result passed. The reference server and its tests use an IBAN sample instead, which is detected; the still-denied test therefore proves the scan of the structured part.
- worker: tests/unit/test_upstream_mcp_client.py (not in touches) had one tool-count assertion updated for the new list_proposals tool; accepted.
- scope: scope: tests/unit/test_upstream_mcp_client.py (the one-line fix above; accepted)
- retry: tld plugin absent in the worker container; the worker ran the verify.dod commands directly and reported done. No roles are required for this phase. Orchestrator re-ran ruff, the full unit suite and the integration file on the merged tree: green.

## p6-retire-adr-docs-changelog

- None.
- scope: scope: docs/adr/0164-*.md, docs/adr/0236-*.md, docs/adr/0285-*.md (the one Amended-by line each that the brief names; accepted)
- retry: tld plugin absent in the worker container; the worker ran the verify.dod commands directly and reported done. No roles are required for this phase. Orchestrator re-ran ruff and the full unit suite on the merged tree: green.

## Final review

- None.

## Finishing

- scope: Scope disposition: the five out-of-scope paths (one test file each in p2 and p5, three ADR Amended-by lines in p6) are still in the final diff, so all are accepted.
- final: Definition of done run by the orchestrator on the feature branch: ruff, mypy, strict mypy, bandit, mcpb/shim tests and typecheck, the full pytest run with the coverage floor (22624 passed), and the 29 browser tests with the Chromium path override all pass. The tld roles and the dod playbook could not run in the worker containers (plugin absent).
