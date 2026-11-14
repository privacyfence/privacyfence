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

- pending

## p4-upstream-claims

- pending

## p5-tool-registration

- pending

## p6-reference-upstream

- pending

## p7-client-catalog

- pending

## p8-connector

- pending

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
