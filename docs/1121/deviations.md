# Deviations: scoped-token-gated-upstream-tools

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-token-only-approval-rule

- retry: Worker reported status=blocked only because the tld plugin was absent in its container (guard, dod and roles verify could not run); no roles are required for this phase. Orchestrator reviewed the diff and ran ruff, the full unit suite, mypy, strict mypy and bandit on the merged tree: all green. The coverage-floor row is left to CI.

## p2-create-with-approval

- pending

## p3-refuse-before-forwarding

- pending

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
