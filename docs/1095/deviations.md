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

- pending

## p4-delivery-core

- pending

## p5-admin-setting

- pending

## p6-contract-test

- pending

## p7-publish-workflow

- pending

## p8-ci-dod

- pending

## p9-docs

- pending

## p10-adrs-changelog

- pending

## Final review

- pending

## Finishing

- pending
