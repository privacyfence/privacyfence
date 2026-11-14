# Deviations: drop-desktop-mode

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p00-adrs-new

- worker: First worker found no tld CLI in its container, so it ran no dod or roles; the orchestrator ran them (owner choice orch_roles).
- retry: Two gate-rerun workers stopped at once because tld was still missing; no code changed.

## p01-org-test-harness

- worker: Deleted the local-only browser tests for the bootstrap link, local /settings pages and local-mode links, and the local_server_with_settings fixture: the org WebServer ignores controller=, so they cannot be mounted. The brief said to delete nothing else; the later phase would have deleted them.
- worker: Dropped the bridge_download_served audit-row assertions (a file-bridge row) from the pending-approval test.
- worker: Browser smoke signs in with an org session cookie for LOCAL_PRINCIPAL, not the single-user principal, so cards registered on helper threads stay visible.
- worker: The brief names tests/integration/test_org_mode_smoke.py, which does not exist; test_upstream_mcp_org.py and the org fixture of test_browser_smoke.py were the templates.
- retry: First worker had no tld CLI and no Plan-Phase trailer; one rerun stopped for the same reason; a finish worker re-ran the full browser smoke (230 passed) and added the trailer commit. The orchestrator ran the checks (owner choice orch_roles); no roles are required for test-only files.

## p02-orphans

- worker: Deleted three tests in tests/unit/test_privilege_separation.py that read files this phase deletes (outside touches; orchestrator-approved; they collide with phases p06 and p13, which delete or trim the same file).
- scope: scope: tests/unit/test_privilege_separation.py
- retry: The worker first stopped blocked on those three failing tests; sent back with the approval above. The first two commits had no Plan-Phase trailer, so a marker commit was added; no tld CLI in the worker container, the orchestrator ran the checks (owner choice orch_roles).

## p03-settings-state

- worker: Repointed one patch target or import in tests/unit/policy/test_resource_registry.py and tests/unit/web/test_server.py (outside touches, not owned by another phase; the second collides with p07-server-local) because they referenced moved names.
- worker: settings_controller.py imports only the moved names its class uses; ruff flags the others as unused. All importers and tests use settings_state.
- worker: tests/unit/test_settings_state.py has extra direct tests beyond the moved ones so the moved code reaches 100% statement and branch coverage on its own; tests/unit/test_settings_controller.py also patches data_dir on settings_state.
- scope: scope: tests/unit/policy/test_resource_registry.py
- scope: scope: tests/unit/web/test_server.py
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p04-mode-flip

- pending

## p05-startup-data-check

- pending

## p06-local-cli

- pending

## p07-server-local

- pending

## p08a-routes-settings-admin

- pending

## p08b-routes-approvals-security

- pending

## p09-controller-channel

- pending

## p10a-google-loopback

- pending

## p10b-other-loopback

- pending

## p11-file-bridge

- pending

## p12a-google-connectors

- pending

## p12b-other-connectors

- pending

## p12c-remaining-connectors

- pending

## p12d-live-qa

- pending

## p13-privilege-separation

- pending

## p14a-ui-renderers

- pending

## p14b-mode-validation

- pending

## p14c-mode-parameters

- pending

## p14d-mode-leftovers

- pending

## p15a-packaging

- pending

## p15b-build-workflow

- pending

## p15c-retire-endpoints

- pending

## p16-ci-legs

- pending

## p17-deps

- pending

## p18a-website-build

- pending

## p18b-website-copy

- pending

## p18c-website-agents

- pending

## p19-docs-platform

- pending

## p20-docs-user

- pending

## p21-docs-contributor

- pending

## p20b-docs-org-guide

- pending

## p24a-pypi-workflow

- pending

## p22-ratchet

- pending

## p23-retirement

- pending

## Final review

- pending

## Finishing

- pending
