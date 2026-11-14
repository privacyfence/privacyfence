# ADR 0280: `privacyfence_runtime_credential_status` reports a delivered proposal's credential to the client that proposed it

## Status

Accepted — 2026-11-13. Implemented.
Amends [ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md) for delivered proposals only.
Amended by [0284](0284-the-delivered-credential-type-is-an-administrators-choice.md).

## Context

ADR 0250 refused a status tool: the proposing client never learns whether the person created the token. A delivered
token needs the opposite. The client must put the n8n credential's id on the MCP Client node, so it has to learn that
the person confirmed and which credential PrivacyFence created.

## Decision

- A new meta tool, `privacyfence_runtime_credential_status`, read-only and idempotent, takes `proposal_id` and
  `reason`. It answers only for `(current principal, proposal_id)`:
  - `delivered` with `runtime`, `credential_id`, `credential_name`, `credential_type`, `node_authentication`,
    `mcp_url` and `token_expires_at`, while the token is live;
  - `pending` while the person has not decided or while the credential is being created (no `expires_at` is
    given during creation, because confirming removes the proposal from the registry first);
  - `not_delivered` for everything else, with the same shape every time: another person's proposal, a declined or
    expired proposal, a revoked or expired token, an abandoned record and an unknown id.
- It never returns a token id, a digest, the secret or the n8n host beyond `mcp_url`.
- What it reveals: whether the person accepted, a later revoke or expiry (`delivered` turns into `not_delivered`),
  the chosen lifetime, and the label through `credential_name`. All of it is visible in n8n anyway.
- It is listed only on an organization server with proposals enabled and delivery set up, and is checked again at call
  time. A scoped token neither sees nor can call it; it is not in `SCOPED_META_TOOLS`. Each call is audited as
  `runtime_credential_status_read` with the reason.
- A proposal without `deliver_to` keeps ADR 0250's rule: the client never learns whether it was created. The propose
  tool's description says both.

## Alternatives considered

- **Claude finding the credential with n8n's `list_credentials`.** It couples Claude to the n8n MCP and to a naming
  rule.
- **Returning the credential id from the propose call.** The credential does not exist until the person confirms.
- **A status tool for every proposal.** It would reverse ADR 0250 for local mode and for tokens the person copies.

## Consequences

- The propose tool's description is longer, and was shortened elsewhere to stay under the 1024-character limit.
- The tool list differs by install: the tool appears only where delivery is set up.

## Verification

- `tests/unit/test_runtime_credentials.py`: `TestStatusFor`.
- `tests/unit/web/test_mcp_dispatch.py`: `TestProposeWithDelivery`, `TestRuntimeCredentialStatus`.
- `tests/unit/web/test_routes_mcp.py`: `TestRuntimeCredentialStatusTool` (a scoped token never sees or calls it).
- `tests/unit/web/test_tool_schema_portability.py`, `tests/integration/test_shim_mcp_contract.py`.

## Related

- Issue: [issue 1090](https://github.com/privacyfence/privacyfence/issues/1090); sources `src/privacyfence/web/mcp_tools.py`, `src/privacyfence/web/mcp_dispatch.py`, `src/privacyfence/runtime_credentials.py`.
- [ADR 0250](0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md), [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md).
