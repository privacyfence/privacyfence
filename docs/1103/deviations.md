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

- pending

## p8-audience-rename

- worker: none from the brief; the browser selector test was edited but could not run here (no Chromium). Doorbell failed again, found by timer (no doorbell).

## p9-groups-config

- worker: The operator-file grants key is still parsed (UpstreamServer.grants) but ignored with a startup warning, and admin_config.seed_upstream_grants stays with no caller, because removing them touches files outside the phase (tests/integration/test_org_docker_deploy_files.py, tests/unit/test_admin_config.py, docs/org-mode-docker.md). To be settled before the final review.
- worker: claim.key_overlap_hours is parsed but unused until phase 10 builds the new signer; docs/approvals-and-policy.md and docs/connector-qa.md still describe grants.

## p10-claim-switch

- pending

## p11-forward-audit

- pending

## p12-reference-server-final

- pending

## p13-membership-tool

- pending

## p14-token-admin-only

- pending

## p15-admin-approvals

- pending

## p16-contract-and-examples

- pending

## p17-retire

- pending

## Final review

- pending

## Finishing

- pending
