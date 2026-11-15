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

- worker: Edited tld-release.yml directly, without tld guard allow-workflow-install, because the tld plugin is not installed in worker containers.
- review: Diff is the deletion of release.yml and the one CHECK_SCRIPT line.

## p4-release-docs

- review: Docs match section 3.5. The blank line after the Cutting a release heading in docs/releasing.md was dropped; cosmetic, left as is.

## p5-rulesets-in-policy

- worker: tld ops repo-admin validate-policy was run by the orchestrator, not the worker, because the tld plugin is not installed in worker containers; it passed.
- review: Diff matches section 3.6; ten required checks, merge-method settings and check_script added.

## p6-tld-lifecycle-docs

- review: Diff matches section 3.8 for CLAUDE.md, CONTRIBUTING.md, the steward skill, the PR template and section 2.7; ADR 0311 and 0312 stay plain text until the ADR phases.

## p7-release-branch-docs

- pending

## p8-release-branch-workflows

- review: Diff matches section 3.7: the releases/** triggers and their comments are gone; the qa-record-fixture guard is unchanged.

## p9-adrs

- pending

## p10-retirement

- pending

## Final review

- pending

## Finishing

- pending
