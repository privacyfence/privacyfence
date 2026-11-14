# ADR 0289: The delivered credential type is an administrator's choice, and the new type is checked against n8n before a token is minted

## Status

Accepted — 2026-11-14. Implemented.
Amends [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md) and [ADR 0280](0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md).

## Context

ADR 0278 delivers an n8n `httpBearerAuth` credential only. The PrivacyFence node needs a credential of its own type,
`privacyFenceApi`, but it can be used only in an n8n where the community node is installed, and that is the
n8n administrator's decision, not the AI client's. Tracking issue: https://github.com/privacyfence/privacyfence/issues/1095.

## Decision

- `agent_runtimes.n8n.credential_type` is a server-wide setting, `httpBearerAuth` (default, unchanged behaviour) or
  `privacyFenceApi`, set in Admin → Agent runtimes. Another value is a configuration error, and `privacyFenceApi`
  needs an `mcp_url`, because the credential keeps the server's address. Changing it needs the restart the other
  runtime fields need.
- For `privacyFenceApi`, delivery first calls `GET /api/v1/credentials/schema/privacyFenceApi`, before a record or a token
  exists. Only a **404** means "node not installed" and answers 503 with a text that names the community node; every other
  error (n8n unreachable, a 401 from a revoked API key, a redirect, a 5xx) is the ordinary delivery failure, 502. Neither
  leaves a record or a token; both write the audit entry `runtime_credential_delivery_failed`.
- A record stores its `credential_type`. Records written before this change have none and read as `httpBearerAuth`, so
  status, revocation and the sweeper keep working after the setting changes; the status tool answers from the
  **record's** type. The sweeper's start-up check uses the configured type.
- The delivery message and the status answer name the node for `privacyFenceApi`, and `node_authentication` for the bearer type.

## Alternatives considered

- **Always delivering `privacyFenceApi`.** It would make the community node required in every n8n and drop the MCP Client
  path; the default stays `httpBearerAuth`.
- **Checking the node at propose time.** It would make an MCP call depend on n8n; the check is made at delivery, where
  failure is already fail-closed.
- **Treating any check failure as "node not installed".** An unreachable n8n or a revoked key would then send the person
  to install a node that is already there.
- **A per-proposal choice by the AI client.** Which node an n8n has installed is not the client's to decide.

## Consequences

- Existing installs change nothing. Switching the type affects new deliveries only; tokens already delivered keep their type.
- With `privacyFenceApi`, an AI Agent in n8n uses one PrivacyFence node per call instead of the MCP Client Tool.
- A delivery to an n8n without the node fails before any token exists, with a text the person can act on.

## Verification

- `tests/unit/test_runtime_credentials.py`: `TestDeliverNodeCheck`, `TestOldRecords`, `TestStatusFor`.
- `tests/unit/test_n8n_api.py`; `tests/unit/test_n8n_credential_fields.py`.

## Related

- Issue: [issue 1095](https://github.com/privacyfence/privacyfence/issues/1095); sources `src/privacyfence/agent_runtimes.py`, `src/privacyfence/runtime_credentials.py`, `src/privacyfence/n8n_api.py`.
- [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md),
  [ADR 0280](0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md),
  [ADR 0281](0281-a-sweeper-deletes-the-credential-of-a-revoked-expired-or-undelivered-token.md).
