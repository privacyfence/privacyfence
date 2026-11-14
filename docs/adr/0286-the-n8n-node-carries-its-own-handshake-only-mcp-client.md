# ADR 0286: The n8n node carries its own handshake-only MCP client with no dependencies

## Status

Accepted — 2026-11-14. Implemented.
Relates to [ADR 0283](0283-the-n8n-node-ships-from-this-repository-and-is-published-with-provenance.md).

## Context

The node must speak MCP to PrivacyFence's `/mcp`. n8n's verification for community nodes forbids runtime dependencies,
and the daemon's routing of a session-less ordinary POST ends in a 400. Tracking issue: https://github.com/privacyfence/privacyfence/issues/1095.

## Decision

- The client is about three small files in `n8n/nodes/PrivacyFence/transport/` (`mcpClient.ts`, `sse.ts`, `result.ts`), plain
  TypeScript with no n8n runtime imports and no dependencies. It is driven through an injected sender, so the n8n adapter
  and the contract test share it.
- It speaks the **handshake revision only**: `initialize` (protocol `2025-11-25`; `2025-06-18` and `2025-03-26` are accepted
  in the answer), `notifications/initialized`, then `tools/list` and `tools/call` with the `Mcp-Session-Id` and
  `MCP-Protocol-Version` headers, and `DELETE` to close. It never sends `2026-07-28`.
- Replies are JSON or an event stream; the first message with the request's id is the answer. A body over 10,000,000
  characters is an error. One session lives for one execution.

## Alternatives considered

- **Depending on `@modelcontextprotocol/sdk`.** n8n's verification forbids runtime dependencies, and the SDK would be
  bundled code to audit.
- **The 2026-07-28 single-exchange revision.** It needs the `_meta` envelope and `resultType` handling, and the daemon's
  routing for it is new and marked temporary in the SDK.
- **Trying 2026-07-28 and falling back to the handshake.** A fallback doubles the surface and the tests.

## Consequences

- The package audits as a few hundred lines of its own code.
- A later MCP revision that PrivacyFence requires means a new client version, shipped with the next stable tag.
- The client supports what this node needs (tools only), not resources, prompts or server notifications.

## Verification

- `n8n/test/mcpClient.test.mts`, `n8n/test/sse.test.mts`, `n8n/test/result.test.mts`.
- `tests/integration/test_n8n_node_contract.py` runs the built node against the real daemon.

## Related

- Issue: [issue 1095](https://github.com/privacyfence/privacyfence/issues/1095); sources `n8n/nodes/PrivacyFence/transport/mcpClient.ts`, `n8n/nodes/PrivacyFence/transport/sse.ts`.
- [ADR 0283](0283-the-n8n-node-ships-from-this-repository-and-is-published-with-provenance.md).
