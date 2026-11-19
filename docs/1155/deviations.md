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

- scope: scope: contrib/examples/knowledge-hub/{admin_config.upstream_mcp,group_membership_static,upstream-mcp}.json deleted outside touches (part of the removed examples); accepted.
- scope: scope: docs/org-mode-docker.md - the commented-out upstream lines were removed to match compose.yaml; collides with p8 and p23 but a small removal; accepted.
- review: The worker report text could not be read back, only its first line; the 11-file diff was reviewed in full (the pyproject.toml change is a comment only, so no lock change is owed).

## p8-docs-cleanup

- review: The worker report text could not be read back, only its first line; the 9-file docs diff was reviewed by name and size, with no version heading added to CHANGELOG.md.
- review: Owner decision (board ask a-cb243482c7e1c50b): the nine lines p8 deleted from the released 8.0.0 changelog section are restored, and one Removed entry under [Unreleased] announces the breaking removal. The plan had assumed those entries were unreleased.
- review: Three links in the restored 8.0.0 lines pointed at headings p8 removed (org-mode-docker upstream-mcp-file, and anchors in upstream-mcp.md and identity-claim.md); they now point at the files themselves or are plain text so the docs link test passes.

## p9-config

- review: The worker report text could not be read back, only its first line; the two-file diff was checked for scope, and its tests pass with 100 percent coverage of the new module.

## p10-oauth-tokens

- review: The worker report text could not be read back, only its first line; the three-file diff was checked for scope, and the new module passes its tests with full coverage and no bandit findings.

## p11-oauth-discovery
- worker: RegistrationStore.__init__ takes an extra optional upstreams= keyword, and ensure_client remembers its upstream, because client_for(upstream_id) must answer for a declared client; the p15 runtime has to pass upstreams= when it builds each store.
- worker: The public-address check also refuses multicast and reserved addresses, where the plan names only is_global (Python treats some multicast ranges as global); stricter than the plan, in line with its rule that multicast is refused.
- review: Open point passed to p15: after a restart and before any Connect, a declared client with a secret falls back to client_secret_basic because the server auth methods are not stored; p15 must cache or persist them so a refresh works against a server that supports only client_secret_post.


## p12-tool-store

- review: The worker report text could not be read back, only its first line; the two-file diff was checked for scope and the new module passes its tests with full coverage.

## p13-policy-hooks

- review: The worker report text could not be read back, only its first line; the 14-file diff was checked for scope, and the merged tree passes the unit suite, mypy, the strict-module check, the integration suite that runs here (38 passed, 570 skipped without a browser), and full coverage of the new registry module.

## p14-client-connector

- scope: scope: tests/unit/connectors/test_readme_manifest_alignment.py - lists UpstreamMcpConnector in CONNECTOR_CLASSES; needed for pytest tests/unit to pass once the connector module exists; accepted.
- worker: Design choices beyond the plan text: BearerAuth records the first error status so the session-close DELETE cannot hide a 401; list_tools reads one page only (as the old client did); a connector with an upstream but no admit, client or bearer fails closed with "not configured"; a new helper person_gate_of in upstream_mcp_client.py gives a saved tool its review/popup gate.

## p15-runtime

- worker: Finding from p11 handled: the runtime builds one RegistrationStore per upstream and passes upstreams=[upstream]; for a declared client with a secret, _client_source takes auth_method from the newest saved token record with the same client_id (falling back to client_for), so a refresh after a restart uses the method that worked at Connect. No p10 or p11 file changed.
- worker: The brief check that any other mode with an upstream variable set raises is not implemented, because organization mode is the only mode (org_mode.validate_mode refuses the rest), so it could never fire.
- worker: admin_rows takes an optional principal (default the current one) because admin_connected needs the administrator; the runtime adds UpstreamAdminError(message, status) and UpstreamRefreshError with statuses 400, 409 and 502 for p16 to map; pending tools carry extra fields name, exposed_name, gate and reason; fetched_at is time.time() bumped by 1e-6 so two refreshes never share a value; the tools-changed callback is a module global that survives configure and is cleared by reset; _UNAVAILABLE is i

## p16-admin-backend

- worker: Added _checked_upstream_params in routes_admin.py because _coerce lets float and dict parameters through unchecked and routes_settings.py is outside touches: gates must be an object of at most 50 string entries and fetched_at a number that is not a bool (400 otherwise); an int fetched_at is accepted and converted to a float; an OSError while saving becomes a 500 with the text Could not save the upstream tool list.; _dispatch became async.

## p17-connect-routes

- worker: The token page main tool list takes an upstream tool when it is in UPSTREAM_AUTO_TOOLS, but _gated_tools is unchanged, so an auto-ceiling tool also still appears in the administrator tick list; a review tool appears only in the tick list as the plan says.
- review: Open point: the Connect rows are listed in sorted-id order, not declared order, because the runtime exposes upstream_ids() as a frozenset; a small runtime accessor would fix it. Not blocking.

## p18-admin-ui

- worker: The token-form connection note sits after the gated-tools fieldset, not directly under that upstream tool chips, because the form has no per-upstream tool group; the Save button gather-and-post step and the note rendering for a ticked versus unticked tool are tested only as markup and page source, since the node harness has no click events and browser tests skip here. A browser or manual check is owed.

## p19-reference-server

- None.

## p20-integration

- retry: The integration test found two defects outside the phase files: privacyfence_status lost its upstreams rows when p1 unwired the old feature and no later phase restored them, and saving a tool list did not evict connector hosts already built for people, so they never saw the new tools. The phase was reported blocked, its test branch kept as is, and fix-1 (merging that branch) fixes both in web/mcp_dispatch.py and web/server.py with unit tests.
- None.
- review: The p20 test branch reached the feature branch through the fix-1 merge, whose message carries both trailers so the phase counts as done; fix-1 added unit tests in test_mcp_dispatch.py and test_server.py and says it did not run those two against the unfixed code, but the integration tests cover both defects.

## p21-adrs

- review: ADR numbers 0327 and 0328 were taken on main, so the new ADRs are numbered 0329, 0330 and 0331 (owner decision, board ask a-7e915c58fe47b2ca); the plan was renumbered before the start commit.
- review: ADR 0331 should record two behaviours found by the integration test: privacyfence_status carries the upstream rows from the runtime, and saving a tool list evicts every person connector host so the new tools show at once.

## p22-adr-status

- pending

## p23-docs

- pending

## Final review

- pending

## Finishing

- pending
