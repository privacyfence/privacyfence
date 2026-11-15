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

- worker: tests/conftest.py (outside touches): the marker_path stub fixture now takes the optional root argument that marker_path gained.
- scope: scope: tests/conftest.py
- conflict: CHANGELOG.md conflicted with the p18b entry in [Unreleased] ### Changed; both entries kept.
- retry: No tld CLI in the worker container; the orchestrator ran the checks, including 100% line and branch coverage of data_format (owner choice orch_roles); no roles are required for this diff.

## p06-local-cli

- worker: _last_startup_error and its reset in tests/conftest.py stay, because the surviving TestMain tests read last_startup_error(); only comments were reworded.
- worker: Also deleted test_a_malformed_admin_config_makes_the_cli_exit_non_zero, which called main(["--slack-oauth"]); and the seven TestWindowsServiceHost tests with their _run_isolated helper in tests/unit/test_privilege_separation.py (the installer-script tests in that class stay).
- worker: src/_launcher_entry.py did not exist (p02 deleted it). Still referencing the deleted modules and left for later phases: the three PrivacyFenceApp spec files, scripts/pyinstaller_common.py, scripts/check_coverage_floor.py, scripts/windows_privilege_separation.ps1, .github/workflows/tests.yml and tests/unit/test_check_coverage_floor.py.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p07-server-local

- worker: Outside touches: tests/unit/test_privilege_separation.py (deleted test_mcp_token_moved_out_of_the_shared_handoff_dir, which called the removed load_or_create_mcp_token; dropped that one call from test_resolving_paths_does_not_retighten_the_handoff_dir), tests/unit/web/test_control_channel.py (deleted two tests that built the local app or used rotate_mcp_token), tests/unit/web/test_routes_approvals.py (TestPerPrincipalIsolation._app builds its resolver inline) and tests/unit/web/test_routes_setti
- worker: server.py went beyond the brief: build_app stays as a thin organization-only wrapper because the org tests call it; WebServer now takes only org, host, port, mcp_dispatcher, principal_resolver, the TLS arguments and trusted_proxies (daemon_main never passed allow_quit, notifications_*, step_up, agent_overrides, startup_admin or org_config); _state_stream_route and _state_stream_loop_lifespan were deleted with their tests; wait_until_ready is kept and always returns None.
- worker: mcp_auth.py: single_token_verifier is kept and returns a private _SingleTokenVerifier because PerUserTokenVerifier is gone; a small TestSingleTokenVerifier covers it. Two _scoped_rule_entry tests that sat in a deleted class were restored to keep the branch covered. Coverage after merge: server.py 98% (floor 95), mcp_auth.py 100% (floor 98).
- worker: tests/integration/test_shim_mcp_contract.py and test_settings_personal_transfer_browser.py still build a local WebServer; they were not run and are deleted by p11 and p09. Comments in routes_mcp.py, routes_file_bridge.py and control_channel.py still name PerUserTokenVerifier.
- scope: scope: tests/unit/test_privilege_separation.py
- scope: scope: tests/unit/web/test_control_channel.py
- scope: scope: tests/unit/web/test_routes_approvals.py
- scope: scope: tests/unit/web/test_routes_settings.py
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p08a-routes-settings-admin

- worker: The brief premise was wrong: handle_org_config_upload took a SettingsController and a LocalSessionStore and its only caller was the local routes_admin factory. It was deleted with the tests that covered only it (owner-visible orchestrator decision); tests of _gate_upload and read_bounded_form moved to tests/unit/web/test_routes_org_config_upload.py.
- worker: Also deleted with no remaining users: the _ALLOWED_ACTIONS, _NON_SENSITIVE_ACTIONS and _BESPOKE_* sets, AiClientConnect, ScopedTokenWiring, _call_action, settings_page_state, _snapshot, _is_confirmed, MAX_ORG_CONFIG_BYTES and the routes_admin state_extra argument. _SENSITIVE_ACTIONS is trimmed to the five organization actions.
- worker: The extend_scoped_token shim entry is KEPT, because the organization settings page and its route need it; only the dead _BESPOKE_* path sets were removed.
- worker: org_settings_scope.py: local-only entries now have modes=frozenset() (NOT_APPLICABLE_ACTIONS unchanged); the LOCAL_MODE constant stays because settings_window_html.py imports it (a later phase removes it).
- worker: Coverage after merge: org_settings_scope 100%, routes_admin 99% (floor 98), routes_settings 96% (no floor). The tip is an empty marker commit.
- retry: Sent back once after the worker stopped blocked on the handle_org_config_upload premise. No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p08b-routes-approvals-security

- worker: tests/unit/web/test_routes_org_approvals.py (outside touches): renamed the _org_bridge_shim helper to _org_decide_shim, because the acceptance grep matches _bridge_shim as a substring of the organization function name.
- worker: routes_approvals.py: also deleted the human_session_guard parameter, _no_human_session_guard and the sensitive_confirm variable (they only fed the local human-session check, and as dead code kept the file below its coverage floor). state_stream.py: removed push_settings, the receives_settings parameter and the settings_snapshot constructor argument, which leaves only the approvals event.
- worker: New organization tests for behaviour only deleted tests covered: no Recent sign-ins section or warning on /security, a register/verify CSRF refusal, an audit-write failure that does not fail the request, the plain page when nav_items is None, an unrepresentable credential creation time, a close that wakes a waiting stream subscription, a delivered broadcast event and a non-JSON batch body. Coverage: routes_approvals 100% (floor 98), routes_security 100% (floor 97), state_stream 100% (floor 99).
- worker: Stale comments naming _bridge_shim (routes_settings.py lines 481 and 504) and StateStream.push_settings (line 969) remain in a file p08a owned.
- scope: scope: tests/unit/web/test_routes_org_approvals.py
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p09-controller-channel

- worker: Outside touches (orchestrator-approved after the worker stopped blocked): tests/unit/web/test_agent_attestation.py (audit_rows now imported from settings_state) and tests/unit/test_privilege_separation.py (the control_channel import and three tests removed; the file is deleted whole by p13).
- worker: scripts/qa_web_smoke.py and scripts/qa_readme_screenshots.py were deleted whole, because they could only build a local WebServer(controller=...). docs/images/screenshots/README.md still names qa_readme_screenshots.py as the source of the README screenshots; the docs phases need to fix that row. tests/unit/test_code_no_history.py still lists qa_readme_screenshots.py in an allowlist.
- worker: tests/integration/test_settings_personal_transfer_browser.py was deleted whole: every test used the local server and SettingsController, and no organization browser equivalent was added (Chromium unavailable to the worker).
- worker: The ADR 0166 and 0198 edits only turn the settings_controller.py link into plain text.
- scope: scope: tests/unit/test_privilege_separation.py
- scope: scope: tests/unit/web/test_agent_attestation.py
- retry: Sent back once after the worker stopped blocked on the two outside-touches files. No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles), including session_auth at 100% coverage; no roles are required for this diff.

## p10a-google-loopback

- worker: google_oauth.py coverage after merge is 100% (no floor listed); the oauth_loopback module itself stays until p10b.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff. The worker reported no deviations.

## p10b-other-loopback

- worker: Outside touches: comment and docstring edits in confluence_client.py, jira_client.py, org_identity.py and web/routes_connect.py that named the deleted oauth_loopback module or atlassian_oauth.authorize_interactive.
- scope: scope: src/privacyfence/confluence_client.py
- scope: scope: src/privacyfence/jira_client.py
- scope: scope: src/privacyfence/org_identity.py
- scope: scope: src/privacyfence/web/routes_connect.py
- review: After this merge the coverage ratchet (scripts/check_coverage_floor.py on the unit suite) fails on one module: org_mode.py is at 99.23% against a 100% floor. It comes from the earlier mode-flip phase; p14b rewrites org_mode and p22 re-measures the floors.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p11-file-bridge

- worker: Outside touches in src: connectors/gmail.py, connectors/drive.py and connectors/confluence.py got local_files.mark_staged_download() on the organization staging paths (the plan section on staged_download), and a drive.py comment was reworded; these files are otherwise removed or edited by the connector phases p12a and p12c.
- worker: Outside touches in tests (one-line or helper repoints, each because a test failed on the deletion): tests/helpers.py, tests/unit/test_gate.py, tests/unit/test_gmail_client.py, tests/unit/test_upload_staging.py, tests/unit/abuse/test_abuse_mcp_error_taxonomy.py, tests/unit/connectors/test_assets_connector.py, test_grist_connector.py, test_jira_connector.py, test_salesforce_connector.py, tests/unit/web/test_routes_mcp_agent_identity.py, test_routes_mcp_principal.py, test_tool_schema_portability.py
- review: The dispatcher filter drops every argument the tool does not declare (tools with a verbatim input_schema are left alone); its test came first. The status tool lost deprecation and next_step; the server instructions no longer mention the companion; docs/tools-reference.md was regenerated. After merge the five stripped modules meet their coverage floors; the only ratchet failure is org_mode.py (99.23% vs 100%), left for p14b.
- scope: scope: src/privacyfence/connectors/confluence.py
- scope: scope: src/privacyfence/connectors/drive.py
- scope: scope: src/privacyfence/connectors/gmail.py
- scope: scope: tests/helpers.py
- scope: scope: tests/integration/test_pending_approval_releases_nothing.py
- scope: scope: tests/unit/abuse/test_abuse_mcp_error_taxonomy.py
- scope: scope: tests/unit/connectors/test_assets_connector.py
- scope: scope: tests/unit/connectors/test_grist_connector.py
- scope: scope: tests/unit/connectors/test_jira_connector.py
- scope: scope: tests/unit/connectors/test_salesforce_connector.py
- scope: scope: tests/unit/test_gate.py
- scope: scope: tests/unit/test_gmail_client.py
- scope: scope: tests/unit/test_upload_staging.py
- scope: scope: tests/unit/web/test_routes_mcp_agent_identity.py
- scope: scope: tests/unit/web/test_routes_mcp_principal.py
- scope: scope: tests/unit/web/test_tool_schema_portability.py
- conflict: tests/unit/test_gmail_client.py auto-merged with the earlier loopback phase; no manual resolution needed.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p12a-google-connectors

- worker: src/privacyfence/daemon_main.py (outside touches, two lines): the connector.download_mode assignments for Drive and Gmail were removed because the attribute no longer exists; p12c removes the rest.
- scope: scope: src/privacyfence/daemon_main.py
- review: Drive and Gmail now take upload: references only; the dispatcher filter handles agents that still send destination_dir or local_path. After the merge the coverage ratchet fails only on org_mode.py (known, p14b).
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p12b-other-connectors

- worker: src/privacyfence/daemon_main.py (outside touches, three lines): the connector.download_mode assignments for Salesforce, Jira and Assets were removed because the attribute no longer exists; p12c removes the rest.
- scope: scope: src/privacyfence/daemon_main.py
- review: After merge the coverage ratchet fails only on org_mode.py (known, p14b). The worker reported no deviations of its own.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p12c-remaining-connectors

- worker: Outside touches: org_mode.py (wording only: LINK_RESULT_NOTE and comments no longer say destination_dir is ignored; one docstring was left with an odd closing line) and src/privacyfence/resources/settings.yaml.example (a comment); tests: test_assets_connector.py, test_jira_connector.py, test_salesforce_connector.py (one-line repoints), test_confluence_client.py, test_gate.py, test_org_mode.py (two tests that pin the Peek and Grist auto-provision "needs mode org" guards, which closed the org_mode
- worker: tests/integration/test_pending_approval_releases_nothing.py (outside touches): the fake Drive client was repointed to what the organization download path reads, and the stale destination_dir argument was dropped; the test failed on the merged result and the phase was sent back once to fix it.
- scope: scope: src/privacyfence/connectors/assets.py
- scope: scope: src/privacyfence/connectors/jira.py
- scope: scope: src/privacyfence/connectors/salesforce.py
- scope: scope: src/privacyfence/org_mode.py
- scope: scope: src/privacyfence/resources/settings.yaml.example
- scope: scope: tests/integration/test_pending_approval_releases_nothing.py
- scope: scope: tests/unit/test_org_mode.py
- retry: Sent back once: the first merge was undone locally (nothing pushed) because test_approved_download_is_audited_as_approved_under_one_request_id went red on the merged result. No tld CLI in the worker container; the orchestrator ran the checks, including the integration set and the coverage ratchet, which now passes (owner choice orch_roles); no roles are required for this diff.

## p12d-live-qa

- worker: The orchestrator told the worker to redispatch the Assets live QA once after its first failure (run 43668237309); the retry (run 43668856231) failed the same way, with a 400 from the Atlassian Assets objectschema/list call during check_connection. assets_client.py is identical to main and the plan changed no call on that path, so the failure looks QA-side (token scope, workspace id or an Atlassian API change).
- worker: Seven connectors recorded and succeeded (gmail 43667657275, drive 43667797773, confluence 43667952062, jira 43668092442, salesforce 43668387216, grist 43668526748, peek 43668660171); six fixture files changed (drive/get_file_metadata, gmail/get_message, grist/list_documents, peek/get_comments, peek/list_pages, salesforce/get_record). The orchestrator decoded the base64 bodies and checked every address and URL: only example.com placeholders and synthetic QA text.
- retry: The owner answered merge_without_assets: the phase merged without a successful Assets live run; the Assets live check is a manual item for the pull request (re-run qa-record-fixture.yml for assets once the QA workspace is fixed).
- gate: Owner decision on the board (ask a-de4f90bded6a0e1a): merge_without_assets.

## p13-privilege-separation

- worker: Outside touches: tests/unit/test_macos_uninstall_message.py was deleted (it read the deleted macOS privilege-separation script; p15a also lists packaging tests) and tests/unit/web/test_server.py lost its dead privilege_separation imports and monkeypatches.
- worker: paths.data_dir() and authority_dir() were compared before and after under three environment settings on Linux: identical, so the start-up data check is unaffected. scripts/build_deb.sh, build_dmg.sh, build_pkg.sh, debian/* and installer/* still mention the deleted scripts (p15a deletes them).
- worker: The worker added one CHANGELOG.md line under [Unreleased].
- scope: scope: tests/unit/test_macos_uninstall_message.py
- scope: scope: tests/unit/web/test_server.py
- review: After merge: unit suite 20044 passed, 3 skipped; integration contract set 12 passed, 1 skipped; test_org_guide_configs 23 passed, 1 skipped; the coverage ratchet passes.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p14a-ui-renderers

- worker: Outside touches: settings_state.py (deleted _pick_resource_index, which used the deleted build_choice_html and block_on_choice, with its test class), web/org_settings_scope.py (removed the LOCAL_MODE constant), tests/integration/test_browser_smoke.py and tests/integration/ui_review_capture.py (removed the choice-dialog cases and test_settings_local_connectors).
- worker: tests/integration/test_settings_grist_modal_browser.py was deleted (it tested the Grist sign-in modal that only the removed Connectors page could open); the other five test_settings_*_browser.py files now use mode="org" (48 passed with Chromium). This departs from the brief, which said to keep all six.
- worker: Beyond the brief: also deleted the browser-side sign-in modals for Telegram, Peek and Grist and the retired "undetected" agent tier from the audit list, the card and the approval list. A before/after DOM comparison of 18 organization pages rendered with build_html(mode="org") was identical with no JavaScript errors.
- worker: Left: comments in approvals.py (lines 157 and 170) still name block_on_choice; resources/approval_window/styles.css still has the agent-undetected rule; test_fallback_page in ui_review_capture.py still uses the local-server fixtures; approval_list_html.py is at 91.9% coverage (no own floor).
- scope: scope: src/privacyfence/settings_state.py
- scope: scope: src/privacyfence/web/org_settings_scope.py
- scope: scope: tests/integration/test_browser_smoke.py
- scope: scope: tests/integration/ui_review_capture.py
- scope: scope: tests/unit/test_settings_state.py
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p14b-mode-validation

- worker: The brief said to delete Mode; the worker stopped blocked because admin_config.py, oauth_apps.py and web/routes_admin.py (p14c files) still import it in about 20 signatures. Orchestrator decision: Mode = Literal["org"] stays in org_mode.py and its __all__ until p14c deletes it with the mode parameters.
- worker: The two Peek and Grist auto-provision "needs mode org" guards were deleted with their tests; the adoption fixtures that pinned resolve_mode were replaced by a fixture that marks the bundle signed (the signing check no longer depends on the mode); run_app_migrates lost its org parameter; mode= call sites in daemon_main.py pass the literal "org" until p14c.
- worker: validate_mode is called when the bundle is loaded (with the bundle) and at the start of run_app (with an empty dict, so only PRIVACYFENCE_MODE is checked there).
- review: After merge: unit suite 20041 passed, 3 skipped; integration set 12 passed, 1 skipped; coverage ratchet passes (org_mode 100%; the worker noted the daemon_main floor of 96% is stale against 97.34%, left for p22).
- retry: Sent back once for the Mode decision. No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p14c-mode-parameters

- worker: src/privacyfence/org_mode.py (outside touches, 4 lines): the Mode type was deleted as the last step, as the orchestrator told the worker (deferred from p14b).
- worker: Outside touches: tests/unit/test_daemon_main.py, tests/unit/web/test_mcp_dispatch.py, test_routes_admin.py, test_routes_mcp.py and test_server_org_mode.py got one-line repoints to the new signatures.
- scope: scope: src/privacyfence/org_mode.py
- scope: scope: tests/unit/test_daemon_main.py
- scope: scope: tests/unit/web/test_mcp_dispatch.py
- scope: scope: tests/unit/web/test_routes_admin.py
- scope: scope: tests/unit/web/test_routes_mcp.py
- scope: scope: tests/unit/web/test_server_org_mode.py
- review: After merge: unit suite 19789 passed, 3 skipped; integration set 12 passed, 1 skipped; coverage ratchet passes; scripts/qa_fixture_recorder.py compiles and answers --help.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p14d-mode-leftovers

- worker: Outside touches: the five tests/integration/test_settings_*_browser.py files, tests/unit/web/test_routes_admin.py (four "unknown in local mode" tests deleted) and tests/unit/test_web_shell.py (a docstring) changed because build_html and the admin helpers lost their mode argument or a function was deleted.
- worker: connector.py, catalog_server.py logic and tests/conftest.py needed no change beyond the /install/ URL. is_action_permitted(..., mode=) and the modes sets of ACTION_SCOPES and ADMIN_ACTIONS in org_settings_scope.py stay: they are the scope table key, not a local-mode switch. mint_recovery_code and store_recovery_code stay because routes_security.py uses them. DEFAULT_LOCAL_RP_ID was deleted (unused once from_local_config went); the accepted ADR 0010 still mentions it and was not touched.
- worker: Two tests were added (LiveStepUpConfig.off_notice and organization defaults) to keep step_up_config.py above its 99% floor after the local tests went.
- worker: Left for later: local-mode wording in the LiveStepUpConfig docstring and two "both modes" comments in step_up_config.py; routes_settings.py still passes mode="org" to the personal-settings builder (about line 851); a test named test_local_mode_has_no_relay_line and some "local mode" comments in tests/unit/test_settings_window_html.py.
- scope: scope: tests/integration/test_settings_edit_verbs_browser.py
- scope: scope: tests/integration/test_settings_scoped_token_form_browser.py
- scope: scope: tests/integration/test_settings_scoped_token_proposal_browser.py
- scope: scope: tests/integration/test_settings_show_ids_browser.py
- scope: scope: tests/integration/test_settings_update_rule_browser.py
- scope: scope: tests/unit/test_web_shell.py
- scope: scope: tests/unit/web/test_routes_admin.py
- review: paths.py changed only in docstrings (checked by the orchestrator in the diff), so data_dir() and authority_dir() behave exactly as before; the worker compared their values in a source checkout and two faked site-packages and dist-packages paths, but its bundled-install variants did not trigger is_bundled(), so that branch was not exercised by the comparison.
- retry: No tld CLI in the worker container; the orchestrator ran the checks, including the five settings browser tests with Chromium (48 passed), the integration set and the coverage ratchet (owner choice orch_roles); no roles are required for this diff.

## p15a-packaging

- worker: tests/unit/test_mcp_registry_server_json.py and tests/unit/test_minimum_os_versions.py were deleted here (orchestrator decision: they only tested the deleted mcpb registry template and the deleted specs and build scripts; p15c and p19 would have deleted them).
- worker: The mcpb/shim/package-lock.json entry was removed from hooks.protect_paths_allow in .tld/toolkit.yaml (orchestrator decision, so the acceptance grep for mcpb prints nothing); the cloudflare/downloads/package-lock.json entry stays for p15c.
- worker: A cloudflare/oauth-relay checkbox was added to .github/pull_request_template.md so the definition-of-done drift test still finds the `npm run dry-run` line; scripts/mcp_registry_server_json.py is orphaned and left for p15b; docs, build.yml, publish-pypi.yml, ADRs and several scripts still mention the deleted scripts and belong to later phases.
- scope: scope: tests/unit/test_mcp_registry_server_json.py
- scope: scope: tests/unit/test_minimum_os_versions.py
- retry: Sent back once for the two decisions above. No tld CLI in the worker container; the orchestrator ran the checks, including profile validation of the edited .tld/toolkit.yaml and the coverage ratchet (owner choice orch_roles); no roles are required for this diff.

## p15b-build-workflow

- worker: docs/org-mode-setup-guide.md (outside touches, two lines): the sentence saying build_org_bundle.py builds desktop bundles only and refuses organization options was deleted with the script; p20b rewrites that guide.
- scope: scope: docs/org-mode-setup-guide.md
- review: The orchestrator checked build.yml after the merge with a YAML job-graph script: jobs are dedupe, build-org-image, sbom and finalize-release; finalize-release needs [sbom, build-org-image] with permissions contents: write only; every needs reference names an existing job; the one artifact download (PrivacyFence-*) matches the remaining sbom upload (PrivacyFence-sbom) and the org image artifacts have their own names; the Ubuntu release smoke test now runs in build-org-image and the R2 upload of t
- retry: No tld CLI in the worker container; the orchestrator ran the checks, including the coverage ratchet (owner choice orch_roles); no roles are required for this diff.

## p15c-retire-endpoints

- worker: The cloudflare/downloads/package-lock.json entry was removed from hooks.protect_paths_allow in .tld/toolkit.yaml (orchestrator instruction, so no reference to the deleted Worker remains in .github or .tld); the deleted tests/unit/test_mcp_registry_server_json.py and the mcpb/shim allowlist entry had already gone in p15a.
- review: After merge: dependabot.yml has six update blocks and its header says six; dependency-audit.yml keeps lockfile-freshness, pip-audit, floor-audit, npm-audit (n8n) and npm-audit-oauth-relay; all 16 workflow files parse and every needs reference resolves; .tld/toolkit.yaml passes tld profile validate; unit suite 19789 passed, 3 skipped; coverage ratchet passes. floor-audit is deleted by p24a.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff. The Cloudflare Worker, its route, D1 database and the downloads-worker environment still exist and need the owner step ma2 after the pull request merges.

## p16-ci-legs

- review: After merge: tests.yml jobs are test, test-python-compat, org-mode-smoke, org-docker-image, static-analysis, n8n-node and sync with every needs reference resolving; REQUIRED_STATUS_CHECKS no longer names platform-windows or platform-macos; all 59 coverage floors name existing modules; the coverage ratchet passes. The live branch-protection ruleset is NOT changed (owner step ma1 before merging the pull request, otherwise the two removed checks stay required and block the merge).
- review: The worker reported no deviations.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p17-deps

- worker: Locks regenerated by the script: only removals, plus one re-resolved marker (colorama, now 'os_name == nt or sys_platform == win32') in dev.lock.txt; no version changes. Comments in pyproject.toml that cited the deleted floor-audit were reworded.

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

- worker: Outside touches: deleted test_desktop_mode_deprecation_notice in tests/unit/test_website_pages.py (it asserted the removed FAQ sentence), and dropped the "local mode" needle from test_security_has_the_limitations_section in tests/unit/test_website_claims.py (the limitation item is reworded).
- worker: Removed the "Privilege separation" card from the security page and the "How do I verify a download?" entry from the FAQ; nothing linked to the card anchor. Kept .verify-commands in styles.css because the install page uses it.
- worker: website/privacy/index.html still has a "Downloads and download counts" section and a mention of the download service at downloads.privacyfence.eu; the grep terms do not match it so it was left. It describes live infrastructure and needs rewriting once the Worker is retired.
- worker: The worker did not run the stale-tag and v999.0.0 website builds nor the final website guardrail pytest step; the orchestrator ran the first, the stale-tag build and the guardrail tests, but could not run the v999.0.0 build because session guards forbid creating a git tag, even a local one.
- scope: scope: tests/unit/test_website_claims.py
- scope: scope: tests/unit/test_website_pages.py
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.
- review: tests/integration/test_website_layout.py::test_docs_navigation_drawer_opens_on_screen[320] fails with Chromium 1243 in the orchestrator container on the commit before this phase as well; the other 316 website guardrail browser tests pass. Check it in CI at the final review.

## p18c-website-agents

- worker: website/ai-agents/claude-ai/index.html was also rewritten (inside website/ai-agents/**). The ChatGPT page is now organization-mode only (a Developer Mode app), because its only other path was the desktop app. clients.json: only the connects text of Claude Desktop and ChatGPT changed.
- worker: The worker ran only the first website-build step and the targeted tests; the orchestrator re-ran the build, the full unit suite and the docs tests after the merge.
- retry: No tld CLI in the worker container; the orchestrator ran the checks (owner choice orch_roles); no roles are required for this diff.

## p19-docs-platform

- worker: README.md (collides with p20-docs-user) and tests/unit/test_catalog_server.py were edited outside touches: links to deleted docs removed in README.md, and test_packaging_doc_names_the_catalog_command repointed to releasing.md and renamed; both fail otherwise.
- worker: release-archive.md and the releasing.md R2 section describe what r2_release.py does now (manifest, latest.json, finalize/verify/promote already removed by p15b); 'Who can download a pre-release' removed; the DMG wording in releasing.md's 'macOS ships one file' and gating sections is left for the docs phase that owns it; build_site.py content_group still returns 'platform' for install-* stems (code, left alone).

## p20-docs-user

- pending

## p21-docs-contributor

- pending

## p20b-docs-org-guide

- pending

## p24a-pypi-workflow

- pending

## p22-ratchet

- None.

## p23-retirement

- pending

## Final review

- pending

## Finishing

- pending
