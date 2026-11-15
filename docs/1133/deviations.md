# Deviations: email-visibility-oidc-groups

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-email-visibility

- worker: Worker could not run tld guard, the dod gate or roles: the toolkit directory was empty in its session; code and tests were pushed and the report said blocked for that reason only.
- review: Orchestrator ran the dod commands itself after the merge as the owner chose (ask gate-handling); required roles at implement_phase were empty. Unit suite, the manifest pytest line, mypy, strict mypy, bandit and ruff pass.
- final: Worker added test_opted_in_a_non_ascii_email_gives_none in tests/unit/test_upstream_mcp_runtime.py, which the brief did not list; it covers the opted-in non-ASCII case of Design 3.1 and is inside the phase touches.

## p2-oidc-groups-provider

- worker: Worker added a small TestCoverageGaps class in tests/unit/test_oidc_provider.py covering three branches that were already untested, so the 100 percent coverage acceptance item holds.
- worker: Worker could not run tld guard, the dod gate or roles: the toolkit directory was empty in its session.
- review: Orchestrator ran the dod commands itself after the merge as the owner chose (ask gate-handling); required roles were empty. Unit suite, the manifest pytest line, mypy, strict mypy, bandit and ruff pass.
- final: Worker put the new client-variable tests in new classes TestClientGroupsEnv and TestAllowsGroup in tests/unit/test_oidc_provider.py instead of under TestClientsFromEnv as the brief said; same tests, inside the phase touches.

## p3-approved-groups

- review: Worker reported no deviations and ran ruff, mypy and pytest by hand because the toolkit was unavailable. Orchestrator ran the dod commands after the merge as the owner chose (ask gate-handling); required roles were empty. Unit suite, the manifest pytest line, mypy, strict mypy, bandit and ruff pass.

## p4-wire-groups

- review: Worker reported no deviations and ran ruff, mypy and pytest by hand because the toolkit was unavailable. Orchestrator ran the dod commands after the merge as the owner chose (ask gate-handling); required roles were empty. Unit suite, daemon tests, the manifest pytest line, mypy, strict mypy, bandit and ruff pass.

## p5-reference-docs

- review: Worker reported no deviations and ran ruff and the docs tests by hand because the toolkit was unavailable. Orchestrator ran the dod commands after the merge as the owner chose (ask gate-handling). Docs tests, the manifest pytest line, the full unit suite and ruff pass. No docs added, moved or deleted.

## p6-retire

- review: Worker reported no deviations. It ran ruff and the changelog and docs tests by hand because the toolkit was unavailable. Orchestrator ran the dod commands after the merge as the owner chose (ask gate-handling) and checked that every test name cited in ADRs 0310 and 0311 exists. The plan document stays in the branch as the plan itself says; CLAUDE.md still describes the older rule that the last phase deletes it.

## Final review

- final: Final review blocking finding 1 (p1): the extra non-ASCII caller_for test was missing from the file; recorded under p1 by the orchestrator.
- final: Final review blocking finding 2 (p2): the renamed test classes were missing from the file; recorded under p2 by the orchestrator. No code changed after the review, so no second review was run.

## Finishing

- pending
