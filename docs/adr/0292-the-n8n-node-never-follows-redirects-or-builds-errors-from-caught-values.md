# ADR 0292: The n8n node never follows a redirect and never builds an error or an item from a caught error, a request or a credential value

## Status

Accepted — 2026-11-14. Implemented.
Relates to [ADR 0290](0290-the-privacyfenceapi-credential-holds-the-address-and-the-token.md).

## Context

The node holds a bearer token. An HTTP client that follows redirects can send it to another host, and the usual way
a token leaks from a workflow is an error message or an output item that quotes the request, its headers or the
exception that carried them. n8n shows errors and items to anyone who can open the execution. Tracking issue: https://github.com/privacyfence/privacyfence/issues/1095.

## Decision

- Every request sets `disableFollowRedirect: true`. A 3xx answer is the fixed error that tells the person to check the
  address in the credential.
- Every error the node builds has a **fixed text** (it may contain the URL's host, a status code, and a JSON-RPC error's
  message with control characters removed and cut to 300 characters; a tool's own error text is cut to 1000).
- No caught error, request, header or credential value reaches an error or an item. The sender sets a flag inside its
  `try` and throws a fixed error after it, with no throw inside a `catch`; any other caught error becomes
  "The PrivacyFence node failed unexpectedly." built inside the `catch` as a `NodeOperationError`; the cause is never kept.
- The credential is read in one function (`readConnection`) and requests are sent in another (`makeSend`), so no function
  holds both.
- The lint rules of `@n8n/node-cli` enforce what a linter can: `require-node-api-error` (only `NodeApiError` and
  `NodeOperationError` inside a `catch`, no rethrow of the caught value), `no-http-request-with-manual-auth`,
  `missing-paired-item`, `credential-test-required`. The tests cover the rest.

## Alternatives considered

- **Following redirects within the same host.** A same-host rule needs its own parser and is one more thing to get wrong;
  a redirected MCP address is a configuration error to report.
- **Passing the caught error through for easier debugging.** It would carry the request, with its `Authorization` header,
  into the execution data.
- **Redacting the token from error text.** Redaction is a deny-list; fixed texts are an allow-list.

## Consequences

- Error texts are less detailed than the underlying failure; the person sees the host and the status, not the cause.
- A misconfigured address that redirects must be fixed in the credential.
- A new error path needs a new fixed text, which a reviewer can check at a glance.

## Verification

- `n8n/test/node.test.mts` and `n8n/test/mcpClient.test.mts` (no token, header or caught message in any error or item; redirects refused).
- `n8n/eslint.config.mjs` and `npm run lint` in the `n8n-node` job of `tests.yml`.

## Related

- Issue: [issue 1095](https://github.com/privacyfence/privacyfence/issues/1095); sources `n8n/nodes/PrivacyFence/PrivacyFence.node.ts`, `n8n/nodes/PrivacyFence/transport/mcpClient.ts`.
- [ADR 0290](0290-the-privacyfenceapi-credential-holds-the-address-and-the-token.md).
