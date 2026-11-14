# Deviations: upstream-identity-claim

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-claim-keys

- worker: Worker interpreted: revoking a pending key un-retires the signing key waiting to hand over; revoke refuses when no other key can sign now; the CLI uses the default 24 h overlap until phase 9/10 wires claim.key_overlap_hours.
- worker: Worker reported blocked only because the tld plugin is absent in its container; no roles are required for its files, so I accepted the head after scope check and the unit suite.

## p2-membership-store

- worker: Worker interpreted: parked counts new (email, group, restricted audience) triples; bootstrap additions are left out of the 24 h cap; an unmapped group is dropped like a removal; approve/decline return 0 for the all audience.
- worker: Worker reported blocked because the tld plugin is absent in its container; no roles are required for its files, so I accepted the head after my own review and the unit suite.

## p3-audit-fields

- review: Worker loosened two existing audit tests (schema version asserted >= 7 or CURRENT_SCHEMA_VERSION; Excel column slices bounded) because the schema bump and new columns broke their hard-coded values; inside touches.

## p4-client-id-context

- None.

## p5-claim-core

- worker: ClaimSigner accepts an injectable clock (not in the brief signature); no other deviation. Worker could not doorbell the parent, found by timer (no doorbell).

## p6-verifier

- review: Worker restricted the JWKS fetch to http(s) URLs (bandit); its unexamined bandit High note did not reproduce: bandit on src exits clean on the merged tree.

## p7-reference-server-both

- None.

## p8-audience-rename

- worker: none from the brief; the browser selector test was edited but could not run here (no Chromium). Doorbell failed again, found by timer (no doorbell).

## p9-groups-config

- worker: The operator-file grants key is still parsed (UpstreamServer.grants) but ignored with a startup warning, and admin_config.seed_upstream_grants stays with no caller, because removing them touches files outside the phase (tests/integration/test_org_docker_deploy_files.py, tests/unit/test_admin_config.py, docs/org-mode-docker.md). To be settled before the final review.
- worker: claim.key_overlap_hours is parsed but unused until phase 10 builds the new signer; docs/approvals-and-policy.md and docs/connector-qa.md still describe grants.
- gate: Owner decision (board ask a-be024333bc4d3bb4): remove the operator-file grants key and the dead seeding code; fix-1 does it and the key is then refused with the groups message.
- gate: Owner decision (board ask a-be024333bc4d3bb4): remove the operator-file grants key and the dead seeding code; follow-up fix-1 did it (also edited docs/upstream-mcp.md and tests/unit/test_daemon_main.py: grants-ignored test became start-refused, seeding-collision test deleted). A grants key now fails the load with the groups message.

## p10-claim-switch

- worker: tests/integration/test_upstream_mcp_org.py still builds the old signer and errors against the v1 client (2 errors) until phase 12, which owns that file; unit suite is green. The acceptance grep for upstream_claims in src also matches a docstring mention in identity_claim.py (not an import).
- worker: Signing-failure detection wraps the signer in a small recorder (_FailureRecorder) in upstream_mcp_client.py because the transport may swallow exceptions from auth_flow.

## p11-forward-audit

- worker: For an auto-gated call the upstream_forwarded entry is written before the auto_accepted entry; claim_jti and claim_req stay empty on error outcomes because the client returns no jti on failure; the token-leak test uses a sentinel token in a faked client call, not a real signed header.

## p12-reference-server-final

- scope: scope: src/privacyfence/identity_claim.py (phase 5 file): worker deleted one stale docstring sentence naming the removed module so the acceptance grep returns nothing; no code change.
- worker: Reference server build_app gained an optional clock parameter so the expired-claim test can move the verifier clock; the integration test now passes with the v1 signer.

## p13-membership-tool

- gate: Owner decision (board ask a-06bdee891f6f1a4a): the membership tool is not added to the website connector list; the website tests skip the PrivacyFence administration reference row so the site stays at fifteen connectors. Worker sent back to adjust the website test files.
- scope: scope: tests/unit/test_daemon_main.py (one assertion now expects the privacyfence connector in org mode) and tests/unit/test_website_connectors_page.py (skips the privacyfence-administration row per the owner decision); both accepted.

## p14-token-admin-only

- None.

## p15-admin-approvals

- scope: scope: src/privacyfence/upstream_mcp_runtime.py gained a public membership_source() so the routes can tell static from snapshot membership; accepted. Existing nav/capability assertions in test_settings_window_html.py were extended for the new membership section.

## p16-contract-and-examples

- None.

## p17-retire

- gate: ADR numbers: main took 0288-0295 after the plan was written, so the three new ADRs are numbered 0296, 0297 and 0298 (the plan and its final check name 0288-0290). Orchestrator decision under the ADR README next-free rule; code and doc references and the pointer on main ADR 0288 (which seeded grants from the operator file) follow.
- scope: scope: code comments citing the renumbered ADRs in audit_log.py, daemon_main.py, upstream_mcp_client.py, upstream_mcp_config.py, web/routes_upstream_mcp.py and tests/unit/test_audit_log.py; main ADR 0288 got a pointer-only Amended-by line and index row; one sentence of the existing CHANGELOG YAML/inline entry now says a grants key is ignored with a warning. All accepted.
- worker: Leftovers reported, not edited: docs/org-mode-docker.md lines 232, 393 and 404 still show the grants key and data domains; UpstreamServer.grants and admin_config.seed_upstream_grants remain as dead code; tests/unit/test_upstream_mcp_config.py:373 comment cites ADR 0289 for the YAML/inline declaration (should be 0288).
- conflict: Merging origin/main (ADRs 0294, 0295 and an n8n delivery change) conflicted in docs/adr/README.md (kept both: 0294, 0295, then 0296-0298) and docs/configuration-reference.md (kept main wording of the scoped_tokens and agent_runtimes rows plus this feature groups row in place of the grants row). Unit suite 22446 passed after the merge.
- conflict: Main took ADR 0296 again after phase 17, so the three new ADRs are finally 0297, 0298 and 0299 (files renamed, every reference in code comments, docs, the changelog and the ADR index follows); the plan text, the earlier deviation line and the manifest final check still say 0288-0290 / 0296-0298.

## Final review

- pending

## Finishing

- pending
