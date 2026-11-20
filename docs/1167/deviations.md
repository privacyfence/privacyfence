# Deviations: org-auto-accept-rules

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-audit-allowed-by

- None.
- review: Both phase merges were verified together (tests/unit and ruff green after the second merge) and the scope check ran after merging, so it recorded skipped; the diffs were checked by hand to stay inside touches.

## p2-org-rules-store

- None.

## p3-approvals-principal-and-source

- retry: Merge turned tests/unit/test_gate.py TestRulesChangedReevaluation red because gate.py (phase p4) still passes a 2-tuple callback; merge undone and p3 sent back to accept a 2-tuple callback until p4 lands.
- worker: Re-evaluation helper accepts both 2-tuple and 3-tuple callbacks until the gate (p4) moves to 3-tuples.

## p4-gate-org-rules

- None.

## p5-person-and-ai-surfaces

- None.

## p6-admin-backend

- None.

## p7-admin-and-person-ui

- worker: Four existing Admin nav/section tests in test_settings_window_html.py now list org_rules after tokens; four source-string checks were renamed for the shared (list-parameterised) add-rule code.
- worker: No existing browser test stubs the Admin passkey step; the new browser test replaces the page bridge with a recorder, as test_settings_edit_verbs_browser.py does.
- review: Worker noted _ORG_ADMIN_PAGE_SECTIONS in tests/integration/test_browser_smoke.py could gain org_rules so the shared phone-layout check covers the new section; left out of scope (not in touches).

## p8-token-copy

- worker: Added a shared copyable_rows() helper in scoped_tokens.py used by the copy check and both proposal call sites; the "(set by your organization)" label sits after the rule title in the chip, not after a sentence.
- scope: tests/unit/test_scoped_token_proposals.py: one proposal test added outside touches; the brief allowed it and the scope check accepted it.

## p9-retire-docs-adrs

- worker: The brief names four allowed_by values; the code also infers a fifth, no_approval_needed (a connector own auto rows), and the docs and ADR 0334 name it.

## Final review

- None.

## Finishing

- pending
