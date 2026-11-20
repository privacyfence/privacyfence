# Deviations: org-auto-accept-rules

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-audit-allowed-by

- None.
- review: Both phase merges were verified together (tests/unit and ruff green after the second merge) and the scope check ran after merging, so it recorded skipped; the diffs were checked by hand to stay inside touches.
- final: fix-1: two bandit B105 false positives (label and enum value) suppressed at the line with a reason.

## p2-org-rules-store

- None.
- final: fix-1: added admin_config tests (malformed value, ungovernable verb, row not storable as an org rule) to bring admin_config.py back to its 100 percent floor.

## p3-approvals-principal-and-source

- retry: Merge turned tests/unit/test_gate.py TestRulesChangedReevaluation red because gate.py (phase p4) still passes a 2-tuple callback; merge undone and p3 sent back to accept a 2-tuple callback until p4 lands.
- worker: Re-evaluation helper accepts both 2-tuple and 3-tuple callbacks until the gate (p4) moves to 3-tuples.
- final: fix-1: the 2-tuple callback shim in the re-evaluation helper is removed; the callback is now typed (ok, rule_id, source).

## p4-gate-org-rules

- None.
- final: fix-1: the org-rule pending-card test now asserts the late audit row (allowed_by org_rule, rule_id); added tests for held review and popup calls released by a rule change to bring gate.py back to its coverage floor.

## p5-person-and-ai-surfaces

- None.

## p6-admin-backend

- None.
- final: fix-1: the usage-field stripping is one shared helper, settings_state.strip_usage_fields, used by org_rule_rows and the Admin state.

## p7-admin-and-person-ui

- worker: Four existing Admin nav/section tests in test_settings_window_html.py now list org_rules after tokens; four source-string checks were renamed for the shared (list-parameterised) add-rule code.
- worker: No existing browser test stubs the Admin passkey step; the new browser test replaces the page bridge with a recorder, as test_settings_edit_verbs_browser.py does.
- review: Worker noted _ORG_ADMIN_PAGE_SECTIONS in tests/integration/test_browser_smoke.py could gain org_rules so the shared phone-layout check covers the new section; left out of scope (not in touches).
- final: fix-1: org_rules added to _ORG_ADMIN_PAGE_SECTIONS in the browser smoke test (outside the original touches).

## p8-token-copy

- worker: Added a shared copyable_rows() helper in scoped_tokens.py used by the copy check and both proposal call sites; the "(set by your organization)" label sits after the rule title in the chip, not after a sentence.
- scope: tests/unit/test_scoped_token_proposals.py: one proposal test added outside touches; the brief allowed it and the scope check accepted it.

## p9-retire-docs-adrs

- worker: The brief names four allowed_by values; the code also infers a fifth, no_approval_needed (a connector own auto rows), and the docs and ADR 0334 name it.

## Final review

- None.
- scope: src/privacyfence/approvals.py: changed by fix-1 for the non-blocking findings and definition-of-done gaps, outside the declared touches of the fix's nearest phase.
- scope: src/privacyfence/audit_log.py: changed by fix-1 for the non-blocking findings and definition-of-done gaps, outside the declared touches of the fix's nearest phase.
- scope: src/privacyfence/settings_state.py: changed by fix-1 for the non-blocking findings and definition-of-done gaps, outside the declared touches of the fix's nearest phase.
- scope: src/privacyfence/web/routes_admin.py: changed by fix-1 for the non-blocking findings and definition-of-done gaps, outside the declared touches of the fix's nearest phase.
- scope: tests/integration/test_browser_smoke.py: changed by fix-1 for the non-blocking findings and definition-of-done gaps, outside the declared touches of the fix's nearest phase.
- scope: tests/unit/test_admin_config.py: changed by fix-1 for the non-blocking findings and definition-of-done gaps, outside the declared touches of the fix's nearest phase.
- scope: tests/unit/test_approvals.py: changed by fix-1 for the non-blocking findings and definition-of-done gaps, outside the declared touches of the fix's nearest phase.

## Finishing

- review: Scope: p8 added one test outside touches and fix-1 touched files of p1, p2, p3, p5, p6 and the browser smoke test by design (its findings sit in those phases); every path is still in the final diff and accepted.
- review: The owner asked for the non-blocking final-review findings to be fixed; fix-1 also closed two bandit B105 false positives and two coverage-floor gaps (gate.py, admin_config.py) found by the wider definition-of-done run. The full suite, ruff, mypy, bandit and the coverage ratchet pass; browser tests skip here and are left to CI.
