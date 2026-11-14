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

- worker: Outside touches: removed the agent_label._local_mode patches and the two "Undetected" rendering tests in tests/unit/test_card_builder.py, tests/unit/test_approval_list_html.py and tests/unit/web/test_routes_mcp_agent_identity.py, because the switch no longer exists.
- worker: Added an autouse fixture pinning org_mode.resolve_mode to "local" in tests/unit/test_settings_controller.py and tests/unit/web/test_routes_settings.py, because the desktop controller still exists; phase p09 deletes the controller and these tests.
- worker: main() one-shot sign-in branch (--gmail-oauth and the other sign-in flags) now calls _release_org_config() instead of the deleted _local_effective_org_config; phase p06 removes those flags.
- worker: Removed the unreachable mode != "org" check from daemon_main._first_bundle_difference and the PRIVACYFENCE_MODE case of TestRetireBundle.
- worker: Kept four tests of TestLocalOverrideNeverAttests that start no local server as TestOverrideResolution; deleted the rest of the class and the _started_overrides helper.
- worker: Deleted 8 build_connectors tests that relied on the old local default (Grist, Peek, Salesforce), test_enabled_but_local_mode_no_forwarder_built and two main() runtime-identity-check tests; TestLoadOrgConfigAdoption now pins resolve_mode; the no-mode-key bundle test expects the signed-bundle refusal.
- worker: TIER_UNDETECTED and UNDETECTED_LABEL stay in agent_label.py because the card and list renderers still import them; request_shutdown stays for settings_controller until p09; Mode stays Literal["local","org"] until p14b.
- scope: scope: tests/unit/test_approval_list_html.py
- scope: scope: tests/unit/test_card_builder.py
- scope: scope: tests/unit/test_settings_controller.py
- scope: scope: tests/unit/web/test_routes_mcp_agent_identity.py
- scope: scope: tests/unit/web/test_routes_settings.py
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

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

- worker: Deleted test_the_download_page_offers_every_installer_platform in tests/unit/test_docs_platform_pages.py (outside touches; orchestrator-approved; it read the deleted download.js; the file is deleted whole by the docs phase p19).
- worker: The /download/ stub title is "Download PrivacyFence" (h1 "Download") because test_every_page_has_title_and_description needs a title of at least 10 characters.
- worker: Removed --offline from scripts/build_site.py and from the two website-build.yml build commands, since nothing is left to fetch.
- worker: tests/unit/test_website_pages.py: /faq/ now needs 2 content links instead of 3, because the download and releases pages were two of its linkers.
- worker: Outside touches: tests/integration/test_website_layout.py and test_website_csp.py lost their Worker stubs and the API_ORIGIN and STABLE_MANIFEST imports, both removed from tests/website_site.py.
- worker: Removed the whole stats block of website/index.html, GitHub-stars counter included, since stats.js is gone.
- worker: The changelog entry sits under a new "### Removed" heading in [Unreleased].
- scope: scope: tests/integration/test_website_csp.py
- scope: scope: tests/integration/test_website_layout.py
- scope: scope: tests/unit/test_docs_platform_pages.py
- retry: Sent back twice: once for the failing docs test above, once because the tip commit lacked the Plan-Phase trailer. No tld CLI in the worker container; the orchestrator ran the checks, including the website build with the docs extra installed (owner choice orch_roles).

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
