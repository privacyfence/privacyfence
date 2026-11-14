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

## p8-connector

- worker: the connector caller argument is a zero-argument callable returning a CallerClaim (the plan left the signature open); p9 passes functools.partial(caller_for, upstream)
- retry: worker stopped as blocked: tests/unit/test_website_connector_pages.py::test_every_connector_module_is_mapped requires a page, setup guide and README row for every connectors/ module; owner chose a documented exemption (ask a-3c3707bb21af5434) and the worker added NO_PAGE_MODULES for upstream_mcp
- scope: tests/unit/test_website_connector_pages.py is outside touches; changed by the owner-approved exemption for upstream_mcp; accepted

## p9-org-wiring

- pending

## p10-scoped-token-domains

- pending

## p11-docs-and-adrs

- pending

## Final review

- pending

## Finishing

- pending
