# Deviations: n8n-privacyfence-node

Written by the implement orchestrator after each phase merge; workers never edit this file.
Every deviation from the plan is listed under its phase. "None." means the phase followed the plan.

## p1-package

- worker: package license is MIT (owner decision, plan section 6 left it open), not Apache-2.0; n8n/LICENSE with MIT text is added by p3
- worker: owner approved three lint-driven changes: stubs throw NodeOperationError (privacyFenceApiTest uses a stand-in node object), the tool parameter is named Tool Name or ID with the standard dynamic-options description, and the unused credential parameter of the stub is removed
- retry: phase was blocked on lint once, resumed on the same branch after the owner answered; the branch carries a WIP commit before the fix
- review: worker could not run dod or roles (toolkit directory missing in its environment); no roles are required at implement_phase for these paths, and the orchestrator ran the n8n lint, test and unit suites after the merge

## p2-mcp-client

- worker: judgement fills: an isError result without text gets a fixed message; the client sends a best-effort DELETE before throwing on an unsupported version or a failed initialized notification; constant FILE_BRIDGE_KEY renamed FILE_BRIDGE_META for a lint rule
- review: worker had no toolkit so ran no dod or roles; no roles are required for these paths; orchestrator re-ran n8n test, lint, typecheck and the coverage gate after the merge

## p3-node-logic

- review: plain Error for the unreachable sender failure replaced by UnexpectedError (lint rule node-execute-block-wrong-error-thrown, as the brief allowed); credential test sender uses the legacy request helper with a documented eslint-disable because ICredentialTestFunctions declares only request
- worker: n8n/LICENSE carries MIT text per the owner decision, not the repository Apache-2.0 file; worker had no toolkit so ran no dod or roles, none are required for these paths; orchestrator re-ran n8n lint, typecheck, build, test and ruff after the merge

## p4-delivery-core

- worker: none reported; docs/tools-reference.md did not change because the generator output holds no status tool description
- review: worker had no toolkit so ran no dod or roles; no roles are required for these paths; orchestrator re-ran ruff, mypy and the unit suite after the merge

## p5-admin-setting

- review: worker had no toolkit so ran no dod or roles; none are required for these paths; orchestrator re-ran ruff, mypy and the unit suite after the merge

## p6-contract-test

- review: worker had no toolkit so ran no dod or roles; none are required for these paths; orchestrator ran the integration contract test and the field-name pin (5 passed, none skipped) and ruff after the merge

## p7-publish-workflow

- review: the stable-channel step exits 1 on pre-release tags as the plan specifies, so a pre-release tag shows this workflow as failed with a notice; worker had no toolkit so ran no dod or roles, none are required for these paths; orchestrator re-ran ruff and the unit suite after the merge

## p8-ci-dod

- retry: first merge was undone before pushing: four definition-of-done drift tests failed because the n8n item was missing from the pull request template; sent back and fixed on the same branch
- review: worker had no toolkit so ran no dod or roles, none are required for these paths; orchestrator re-ran profile validate, ruff and the unit suite after the merge
- scope: out of scope: .github/pull_request_template.md, added at the orchestrator instruction so the definition-of-done drift test passes

## p9-docs

- review: worker had no toolkit so ran no dod or roles, none are required for these paths; orchestrator re-ran ruff and the full unit suite after the merge (only the ADR 0283-0287 links of the plan text stay red until p10); docs read against the built code and match

## p10-adrs-changelog

- gate: owner chose on the board to widen the link-test plan-doc exemption to docs/<n>/plan.md instead of editing the confirmed plan; the worker changed tests/unit/test_docs_links.py and tests/unit/test_docs_references_exist.py accordingly
- scope: out of scope by touches, accepted: tests/unit/test_docs_links.py and tests/unit/test_docs_references_exist.py (the owner decision above)
- worker: ADR 0283 states MIT for the n8n/ package per the owner decision; amendment marks added to the 0278 and 0280 index rows as the ADR rules ask
- retry: first report was blocked on two plan-link tests; resumed on the same branch after the owner decision
- review: worker had no toolkit so ran no dod or roles, none are required for these paths; orchestrator re-ran ruff, the full unit suite (21653 passed) and the n8n tests after the merge
- conflict: merging main brought ADRs 0283 to 0287 of other work, so the five ADRs of this change are renumbered 0288 to 0292 (files, index rows, the amendment lines of 0278 and 0280 and the cross links); the plan text and its final_checks still name 0283 to 0287
- conflict: merge of main: status_for and the status tool description combine main expired state with the credential_type and node fields; docs, ADR index, changelog (one Changed list) keep both sides; tests/unit/test_docs_references_exist.py takes main version, which already exempts docs/<n>/plan.md

## Final review

- pending

## Finishing

- pending
