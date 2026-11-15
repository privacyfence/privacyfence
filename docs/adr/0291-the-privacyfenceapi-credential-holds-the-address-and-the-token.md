# ADR 0291: The privacyFenceApi credential holds the address and the token, and has no authenticate block

## Status

Accepted — 2026-11-14. Implemented.
Amends [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md).

## Context

The PrivacyFence node needs to know where PrivacyFence is as well as the token. n8n credentials can carry an
`authenticate` block, which lets n8n's HTTP Request node apply the credential to any URL. ADR 0278 already accepts that a
bearer credential's secret is readable by whoever can edit workflows in that n8n; the new type must not widen that.
Tracking issue: https://github.com/privacyfence/privacyfence/issues/1095.

## Decision

- The `privacyFenceApi` credential has exactly two properties: `mcpUrl` (the server's `/mcp` address) and `token`
  (a password field). n8n rejects extra data keys, so nothing else is added.
- It has **no `authenticate` block and no `test` property**. The node sets the `Authorization` header itself and the credential
  is tested by the node's own `testedBy` method, which lists the tools through a session and closes it.
- The node has no URL parameter and no way to override the credential's address.
- **Who can change the address.** Anyone who can edit the credential in n8n can change `mcpUrl` and so receive the token
  at their own host. For `httpBearerAuth` the same person could attach the token to an HTTP Request node. The reach is the
  same, namely whoever has edit access to workflows and credentials there; the review page, the dialog and the docs
  keep saying so. The exposure is documented, not reduced, until n8n's credential domain allow-list is researched
  (open since ADR 0278).

## Alternatives considered

- **An `authenticate` block, or `httpRequestWithAuthentication`.** It would let the HTTP Request node apply the credential
  anywhere and read the token back.
- **A token-only credential with the address as a node parameter.** Every workflow author would then choose where the
  token goes, which is a wider reach than a credential that fixes it.
- **Keeping the address server-side only.** The node runs inside n8n and needs the address there.

## Consequences

- The credential is usable only through the PrivacyFence node, so an HTTP Request node cannot use it by selecting it.
- Whoever edits the credential can redirect the token; this is the same trust as ADR 0278 and is stated, not mitigated.
- Without a `test` property, the credential's "Test" button runs the node's own check.

## Verification

- `n8n/credentials/PrivacyFenceApi.credentials.ts`; `n8n/test/credential.test.mts`.
- `n8n/eslint.config.mjs` (the community-node lint rules, `credential-test-required` among them).

## Related

- Issue: [issue 1095](https://github.com/privacyfence/privacyfence/issues/1095); sources `n8n/credentials/PrivacyFenceApi.credentials.ts`, `n8n/nodes/PrivacyFence/PrivacyFence.node.ts`.
- [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md), [ADR 0290](0290-the-delivered-credential-type-is-an-administrators-choice.md).
