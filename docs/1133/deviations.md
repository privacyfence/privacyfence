# Deviations: email-visibility-oidc-groups

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-email-visibility

- worker: Worker could not run tld guard, the dod gate or roles: the toolkit directory was empty in its session; code and tests were pushed and the report said blocked for that reason only.
- review: Orchestrator ran the dod commands itself after the merge as the owner chose (ask gate-handling); required roles at implement_phase were empty. Unit suite, the manifest pytest line, mypy, strict mypy, bandit and ruff pass.

## p2-oidc-groups-provider

- worker: Worker added a small TestCoverageGaps class in tests/unit/test_oidc_provider.py covering three branches that were already untested, so the 100 percent coverage acceptance item holds.
- worker: Worker could not run tld guard, the dod gate or roles: the toolkit directory was empty in its session.
- review: Orchestrator ran the dod commands itself after the merge as the owner chose (ask gate-handling); required roles were empty. Unit suite, the manifest pytest line, mypy, strict mypy, bandit and ruff pass.

## p3-approved-groups

- review: Worker reported no deviations and ran ruff, mypy and pytest by hand because the toolkit was unavailable. Orchestrator ran the dod commands after the merge as the owner chose (ask gate-handling); required roles were empty. Unit suite, the manifest pytest line, mypy, strict mypy, bandit and ruff pass.

## p4-wire-groups

- pending

## p5-reference-docs

- pending

## p6-retire

- pending

## Final review

- pending

## Finishing

- pending
