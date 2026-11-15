# Deviations: retire-release-yml

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-build-permissions

- retry: First attempt stopped at rule 0 because the tld plugin is not installed in worker containers; the orchestrator told the worker to skip rule 0 and run ruff and pytest directly. The orchestrator ran tld roles required: none required.
- review: Diff is the four planned lines only.

## p2-release-check-script

- retry: Worker reported blocked first because the tld plugin is missing in worker containers; the orchestrator ruled roles are not required (tld roles required: none) and the worker reissued status=done at the same head.
- review: Diff matches the design of sections 3.2 and 3.3; the one failing unit test in the worker run was the plan line 32 reference, since fixed on the feature branch.

## p3-release-workflows

- pending

## p4-release-docs

- pending

## p5-rulesets-in-policy

- pending

## p6-tld-lifecycle-docs

- pending

## p7-release-branch-docs

- pending

## p8-release-branch-workflows

- pending

## p9-adrs

- pending

## p10-retirement

- pending

## Final review

- pending

## Finishing

- pending
