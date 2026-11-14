# Deviations: upstream-mcp-connector

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-tool-input-schema

- worker: none reported
- review: worker also updated the existing test_to_dict_contains_all_fields (inside the touched test file) to expect the new input_schema key; accepted
- retry: worker stopped as blocked because the tld toolkit was missing in worker sessions; owner chose orchestrator-run roles (ask a-6883f76fcafb9108); no roles are required for this phase; code merged unchanged
- review: post-merge checks deselect tests/unit/test_docs_references_exist.py::test_every_current_docs_reference_exists until p11 creates the docs the plan cites (owner decision, ask a-6883f76fcafb9108)

## p2-upstream-config

- worker: none from the design; parse_upstreams takes a source argument used as the error-message prefix and load_upstreams passes ENV_VAR
- retry: worker stopped as blocked because the tld toolkit was missing in worker sessions; no roles are required for this phase; code merged unchanged after orchestrator checks (unit suite, daemon contract, 100 percent coverage of the new module)

## p3-verified-email

- worker: none from the brief
- retry: worker stopped as blocked because the tld toolkit was missing in worker sessions; no roles are required for this phase; code merged unchanged after orchestrator checks (unit suite, daemon and shim contract tests)

## p4-upstream-claims

- None.

## p5-tool-registration

- worker: register also refuses an exposed tool name repeated within one call (collision check covers duplicates), which the brief did not list; no other deviation

## p6-reference-upstream

- worker: the brief left open how claims reach tool handlers across sessions; the worker built the server with a stateless StreamableHTTPSessionManager so every request is verified and handled with its own claim; --jwks-url and --public-key-file are not exercised end to end by the tests

## p7-client-catalog

- worker: structured results and non-text blocks are tested by patching ClientSession.call_tool because the reference server never returns them; a schema whose properties is not a dict or whose required is not a list is skipped with the non-object reason
- review: the lock script re-resolved all four lock files: no package added or removed in runtime, but 16 runtime pins moved (including pypdf 6.21.4 to 7.1.0, soupsieve 3.0.4 to 4.1, cbor2 6.1.5 to 6.3.0) and dev swapped pyee for lazy-importer; unit and integration suites pass; owner should look at the pin movement in review
- review: CI on the pull request found two defects of the p7 dependency change, fixed on the branch: the new direct floor httpx2>=2.5 let the floor-audit job resolve httpx2 2.5.0 with five advisories, so the floor is now 2.12; and dev.lock.txt carried a colorama marker written by an older uv than the one CI installs, now regenerated with the current uv (one line). main itself fails the same floor-audit job on pypdf 6.21.2 and soupsieve 2.10, which this change does not touch

## p8-connector

- worker: the connector caller argument is a zero-argument callable returning a CallerClaim (the plan left the signature open); p9 passes functools.partial(caller_for, upstream)
- retry: worker stopped as blocked: tests/unit/test_website_connector_pages.py::test_every_connector_module_is_mapped requires a page, setup guide and README row for every connectors/ module; owner chose a documented exemption (ask a-3c3707bb21af5434) and the worker added NO_PAGE_MODULES for upstream_mcp
- scope: tests/unit/test_website_connector_pages.py is outside touches; changed by the owner-approved exemption for upstream_mcp; accepted
- review: a code-quality bot flagged lambda: MagicMock() in tests/unit/test_systemic_gate_invariants.py (optional style nit); verified real and replaced by MagicMock, no behaviour change

## p9-org-wiring

- worker: settings_controller.py left unchanged because none of its connector-keyed lookups raise for an upstream id; added a signer() accessor to the runtime for the JWKS route; the org_mode skip test lives in tests/unit/test_upstream_mcp_runtime.py instead of test_org_mode.py (outside touches)

## p10-scoped-token-domains

- worker: beyond the brief: narrowing_error also refuses a reviewed proposal that adds data domains the proposal lacked, and the token list payload carries each token upstream_domains so Duplicate can prefill them; the proposal refusal matches tool names by the upstream id plus underscore prefix instead of an exact tool set
- worker: org create-route tests were added to tests/unit/web/test_routes_org_settings.py because test_routes_settings.py covers only local mode; an upstream tool gated review or popup has no scope-catalogue entry yet, so only auto-gated upstream tools can be ticked in the New token form (the plan defers rules for them)
- scope: tests/unit/web/test_routes_org_settings.py is outside touches; it holds the org create-route tests; accepted
- review: main added a delivery path for AI-proposed tokens (runtime_credentials.deliver); fix-1 made it pass upstream_domains, held_domains and principal_email_verified like the Settings create route, with a test
- review: fix-3 added a unit test that extending a scoped token (main extend feature) keeps its upstream_domains and principal_email_verified; the extend operation already used dataclasses.replace, so no source change was needed

## p11-docs-and-adrs

- worker: the docs follow the merged code where the plan differs: GrantStore takes (path, upstream_ids); the reference server also requires nbf, sub and jti and checks iss only when --issuer is given
- review: open from the worker report: ADRs 0036, 0164 and 0236 and their index rows do not yet carry an amended-by pointer to 0279/0280 (outside touches); the docs disagree on editing admin_config.json while the daemon runs (configuration-reference says not to, upstream-mcp.md says grants may be edited live) -- left for the final review
- conflict: merging main (the n8n scoped-token delivery feature, PR 1094) conflicted in CHANGELOG.md, docs/adr/README.md, docs/configuration-reference.md, scoped_tokens.py, routes_settings.py and test_server.py; fix-1 kept both features
- review: main took ADRs 0278-0282, so this plan ADRs 0278-0280 were renumbered to 0283-0285 everywhere outside docs/1091 on the owner decision (ask a-0213d8fce9cc9789); the plan text still names 0278-0280, so its final check for those ADRs is met by 0283-0285
- review: tests/unit/test_docs_references_exist.py now exempts docs/adr/ citations in docs/<n>/plan.md as it already did for old-layout plans, because the frozen plan still names the old ADR filenames; owner decision (ask a-adfde717d85da958); the plan file is unchanged
- review: fix-2 (final review): ADRs 0036, 0164 and 0236 and their index rows now point forward to ADRs 0284 and 0285; configuration-reference.md now says grant edits apply live; the docker exec snippets write the file atomically; docs only
- conflict: main moved again after the pull request opened (the extend-n8n-credentials feature, PR 1099): conflicts in docs/adr/README.md and docs/configuration-reference.md; fix-3 merged main and kept both features
- review: main took ADRs 0283 and 0284 as well, so the upstream MCP ADRs were renumbered a second time under the same owner decision: now 0285 (declared connectors), 0286 (signed caller claim), 0287 (data domains); the plan text and the earlier entries above still use 0278-0280 and 0283-0285

## Final review

- final: ADRs 0036, 0164 and 0236 and their index rows lacked the forward pointer to ADRs 0284 and 0285 that docs/adr/README.md rule 3 requires; fixed in fix-2

## Finishing

- scope: two phases changed one file each outside their touches, both still in the final diff and accepted: tests/unit/test_website_connector_pages.py (p8, the owner-approved upstream_mcp exemption) and tests/unit/web/test_routes_org_settings.py (p10, the org create-route tests)
- review: the definition-of-done gate ran on the feature branch: full pytest with coverage (22578 passed, 86 skipped, browser tests run with PRIVACYFENCE_TEST_CHROMIUM), coverage ratchet, ruff, bandit, mypy, strict mypy, mcpb/shim tests and typecheck all pass; no role is required at the dod point; the live QA fixture row does not apply because the upstream connector has no external provider
