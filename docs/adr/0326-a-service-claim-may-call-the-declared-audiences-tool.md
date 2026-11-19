# ADR 0326: A service claim may call the declared audiences tool

## Status

Accepted — 2026-11-18. Amends [ADR 0297](0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md).
Relates to [ADR 0286](0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md),
[ADR 0310](0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md),
[ADR 0325](0325-an-upstream-can-be-the-source-of-its-own-audiences.md).
Implemented: `docs/identity-claim.md`, `src/privacyfence/upstream_mcp_runtime.py`,
`scripts/upstream_mcp_reference_server.py`. Issue
[#1153](https://github.com/privacyfence/privacyfence/issues/1153); the record of the change is
[`docs/1153/plan.md`](../1153/plan.md).

**Superseded by [ADR 0329](0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md), 2026-11-19.**

## Context

ADR 0297 defined the `service` principal as PrivacyFence's own startup tool listing: `sub` `privacyfence`,
no audiences, and an upstream returns the tool list and no data for it. ADR 0325 makes PrivacyFence call an
upstream tool to read audiences, and that call carries a service claim. The contract must say when an
upstream may answer a service claim with data, or each upstream would guess, and a verifier that is too
generous would hand the table to any service claim.

## Decision

1. **When an upstream may answer a service claim with data.** Only when all of these hold: the claim
   verified; `principal_type` is `service` and `sub` is `privacyfence`; `req` matched the request body;
   and `params.name` is the audiences tool the operator declared. Every other request with a service claim
   still gets no data.
2. **`tools` is never a reason.** The claim's `tools` is `[<name>]` for this call, for information only, as
   everywhere in the contract; an upstream must not decide on it.
3. **What it returns.** `{"version": 1, "audiences": {<name>: [<address>, …]}}`, audience names and
   addresses only, never documents, with the limits of ADR 0325.
4. **`claim_version` stays 1.** The opt-in is declared by the operator per upstream and changes nothing for
   an upstream that does not opt in. A later change to the shape of the result bumps the result's own
   `version`, not `claim_version`.
5. **The verifier does not change.** `contrib/verifier/pf_claim_verify.py` already verifies a service
   claim and binds `req`; the decision of what to return is the upstream's.

## Alternatives considered

- **A separate claim type or `principal_type` for the audiences call**: a verifier would have to learn a
  new type, which is a `claim_version` bump for no gain; the service claim with a `req` bound to the tool
  name already says what is being asked.
- **A new endpoint instead of a tool**: another surface to authenticate, and the claim format and the
  `req` binding exist for `tools/call` only.
- **Letting an upstream decide on `tools`**: it is information only and would let a change to a listing
  change what data is returned.
- **Returning the table for any service claim**: the startup tool listing would then carry data.

## Consequences

- An upstream that opts in adds one branch: the verified service claim on its own audiences tool.
- The reference upstream implements it as `list_audiences` and refuses every other caller.
- An upstream that does not opt in is unchanged: a service claim still gets no data.

## Verification

- `tests/unit/test_upstream_mcp_reference_server.py`:
  `test_list_audiences_answers_a_service_claim_with_the_table`, `test_list_audiences_refuses_a_person`,
  `test_list_audiences_refuses_a_scoped_token`, `test_list_audiences_reads_the_table_on_every_call`,
  `test_a_service_claim_still_gets_no_documents_from_search`.
- `tests/unit/test_upstream_mcp_runtime.py::TestAudiencesFromUpstream::test_the_service_claim_names_only_the_audiences_tool`.

## Related

[ADR 0286](0286-privacyfence-sends-the-upstream-a-signed-caller-claim.md),
[ADR 0297](0297-the-upstream-claim-is-an-eddsa-identity-claim-in-its-own-header-bound-to-one-call.md),
[ADR 0310](0310-email-is-an-opt-in-visibility-input-of-the-identity-claim.md),
[ADR 0325](0325-an-upstream-can-be-the-source-of-its-own-audiences.md).
