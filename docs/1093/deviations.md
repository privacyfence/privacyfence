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

- review: No doorbell arrived from this worker; found it finished by its session state and branch head. Diff matches section 3.7; the unit suite and ruff passed after the merge.

## p8-release-branch-workflows

- review: Diff matches section 3.7: the releases/** triggers and their comments are gone; the qa-record-fixture guard is unchanged.

## p9-adrs

- level: Renumbered by the orchestrator: ADRs 0310 and 0311 are taken on main, so the five ADRs are 0312 to 0316 (plan 0310 to 0312, 0311 to 0313, 0312 to 0314, 0313 to 0315, 0314 to 0316). The ADR mentions in scripts/release_check.py and scripts/tag_release.py were changed from 0310 to 0312 by this phase; CLAUDE.md and docs/releasing.md follow in the retirement phase.
- scope: scope: the five ADR files are renumbered 0312 to 0316 and the two script docstring edits were authorised by the orchestrator (see the renumbering entry above).
- review: ADRs are 48 to 70 lines each, carry the Status lines of the design, and the index rows and old-ADR status cells use the renumbered numbers. Old ADR files 0021, 0132 and 0185 are untouched here.
- scope: scope: docs/adr/0312-a-release-tag-is-created-only-by-tld-release-yml.md (renumbered ADR file, orchestrator decision)
- scope: scope: docs/adr/0313-workflow-commands-come-from-the-tld-plugin.md (renumbered ADR file, orchestrator decision)
- scope: scope: docs/adr/0314-plans-stay-on-main-as-the-record-of-their-change.md (renumbered ADR file, orchestrator decision)
- scope: scope: docs/adr/0315-rulesets-and-required-checks-live-in-the-admin-policy.md (renumbered ADR file, orchestrator decision)
- scope: scope: docs/adr/0316-the-releases-integration-branch-convention-is-retired.md (renumbered ADR file, orchestrator decision)

## p10-retirement

- worker: The worker used the renumbered ADR numbers 0312, 0313 and 0314 in the three old-ADR status lines and the links in CLAUDE.md and docs/releasing.md. tld profile validate was run by the orchestrator and passed.
- review: Diff matches sections 3.4, 3.8 and 3.9; seven protected-path entries removed, six kept.

## Final review

- final: Blocking finding 1: tests/unit/test_required_checks.py cited ADR 0313 where the renumbered rulesets ADR is 0315; fixed on the fix-1 branch together with three non-blocking text fixes (ADR 0316 verification command, ADR 0314 decision 4 wording, the blank line after the Cutting a release heading).
- final: fix-1 merged: one fix worker covered the blocking finding and the three small non-blocking text fixes; its files belong to p4, p5 and p9, so the scope check lists them as outside p5.
- final: Second review: approve, no findings.
- scope: scope: docs/adr/0314-plans-stay-on-main-as-the-record-of-their-change.md (fix-1 text fix)
- scope: scope: docs/adr/0316-the-releases-integration-branch-convention-is-retired.md (fix-1 text fix)
- scope: scope: docs/releasing.md (fix-1 text fix)

## Finishing

- scope: Scope disposition: the p9 and fix-1 out-of-scope paths are accepted (renumbered ADR files, two script docstrings, three small text fixes) and still in the final diff. The dod gate passed in full on the feature branch: coverage run and floor, ruff, bandit, mypy, strict mypy.
