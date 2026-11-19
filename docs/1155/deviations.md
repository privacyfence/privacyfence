# Deviations: oauth-upstream-mcp

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-unwire

- None.

## p2-ui-membership

- worker: The brief said to keep the token form audiences parts but its acceptance required them gone; the worker followed the acceptance, so the form posts no upstream_audiences and the server-side fields stay for p4.

## p3-delete-core

- worker: The worker also edited tests/unit/web/test_routes_org_settings.py, test_gate.py and test_org_docker_deploy_files.py (outside touches), deleting only tests that built on the removed modules.
- scope: scope: tests/unit/web/test_routes_org_settings.py - outside touches, collides with p2-ui-membership; a small explained test deletion, accepted.

## p4-token-fields

- worker: Also removed held_audiences (only the audience check used it) and renamed the test class TestUpstreamDomains to TestEmailVerified; small and in the spirit of the brief.

## p5-delete-claim-modules

- scope: scope: contrib/verifier/pf_claim_verify.py and contrib/verifier/req_test_vectors.json - deleted outside touches; the phase title names the verifier, so accepted.
- review: The worker report text could not be read back, only its first line; the diff was reviewed in full by name and status and the unit suite passes.

## p6-claim-helpers

- scope: scope: tests/unit/web/test_routes_mcp.py - 17 lines of tests of the removed helper deleted outside touches; accepted.
- review: The worker report text could not be read back, only its first line; the 4-file diff was reviewed in full.

## p7-delete-assets

- pending

## p8-docs-cleanup

- pending

## p9-config

- pending

## p10-oauth-tokens

- pending

## p11-oauth-discovery

- pending

## p12-tool-store

- pending

## p13-policy-hooks

- pending

## p14-client-connector

- pending

## p15-runtime

- pending

## p16-admin-backend

- pending

## p17-connect-routes

- pending

## p18-admin-ui

- pending

## p19-reference-server

- None.

## p20-integration

- pending

## p21-adrs

- review: ADR numbers 0327 and 0328 were taken on main, so the new ADRs are numbered 0329, 0330 and 0331 (owner decision, board ask a-7e915c58fe47b2ca); the plan was renumbered before the start commit.

## p22-adr-status

- pending

## p23-docs

- pending

## Final review

- pending

## Finishing

- pending
