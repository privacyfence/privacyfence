# Deviations: extend-n8n-credentials

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-store-extend-and-keep-expired

- worker: Worker ended blocked: its tld CLI was missing so roles could not run; none are required for this diff.
- review: tests/unit docs-link tests (5) fail on the feature branch before and after this merge because the plan links ADRs 0283/0284 that p5 writes; verify_after_merge stays red on these until p5.
- retry: Worker sent back once to finish dod and roles.

## p2-status-expired

- None.
- review: The 5 docs-link tests stay red until p5 writes ADRs 0283/0284 (same as p1). Worker had no tld CLI; no roles required.

## p3-extend-routes

- None.
- review: The 5 docs-link tests stay red until p5 (as p1, p2). Worker had no tld CLI; no roles required.

## p4-extend-ui

- worker: The dialog also rejects non-digit input such as 1e2 or 2.5 in addition to the 1..max_days range check.
- review: The two new browser tests did not run: no Chromium launches here, so the browser tests skip. A skip is not a pass. The 5 docs-link tests stay red until p5. Worker had no tld CLI; no roles required.

## p5-retire-adr-docs-changelog

- worker: Worker ended blocked: 4 docs-link tests still fail on links inside docs/1098/plan.md; it also found docs/connect-claude-code.md and ADR 0278 outside touches.
- review: Docs diff reviewed inside touches; tld CLI absent in the worker, no roles required. Owner asked on the board how to fix the plan links and whether to touch the two extra docs.
- review: Follow-up fix-1, approved by the owner on the board (ask a-754acc02fc8e5f40): docs/connect-claude-code.md now lists Extend and no longer says expiry cannot be edited; ADR 0278 needed no change.

## Final review

- None.

## Finishing

- final: Owner chose edit_plan on the board (ask a-754acc02fc8e5f40): four markdown links to ADRs 0283/0284 in docs/1098/plan.md became plain text so the docs-link tests pass; no other plan text changed.
- final: Scope disposition: no out-of-scope paths in any phase; DoD gate green (21666 passed, coverage floor ok, ruff, bandit, mypy, mypy strict, shim tests and typecheck).
