# Gemini CLI (organization mode, `gemini mcp add --transport http`)

**SEEDED FROM VENDOR DOCS, NOT YET CAPTURED.** Written 2026-09-28 from the source of Gemini CLI
0.61.0 (tag `v0.61.0`, commit `bb523741c7429a44d03e964bc124c7c92df59d5f`), not from a handshake
against a PrivacyFence deployment. Replace it with a captured fixture after the Gemini CLI
organization-mode run in `ai-client-qa.md`
(https://github.com/privacyfence/privacyfence/issues/392).

- Expected agent_id, unpinned: `gemini-cli`
- Expected agent_id, pinned: `gemini-cli`

## Sources

- [`packages/core/src/mcp/oauth-provider.ts`](https://github.com/google-gemini/gemini-cli/blob/bb523741c7429a44d03e964bc124c7c92df59d5f/packages/core/src/mcp/oauth-provider.ts):
  `registerClient()` builds the `/register` body.
- [`packages/core/src/utils/oauth-flow.ts`](https://github.com/google-gemini/gemini-cli/blob/bb523741c7429a44d03e964bc124c7c92df59d5f/packages/core/src/utils/oauth-flow.ts):
  `REDIRECT_PATH` and `getRedirectUri()`.
- [`packages/core/src/tools/mcp-client.ts`](https://github.com/google-gemini/gemini-cli/blob/bb523741c7429a44d03e964bc124c7c92df59d5f/packages/core/src/tools/mcp-client.ts):
  `connectToMcpServer()` names the MCP client and registers its capabilities.
- [`docs/tools/mcp-server.md`](https://github.com/google-gemini/gemini-cli/blob/bb523741c7429a44d03e964bc124c7c92df59d5f/docs/tools/mcp-server.md),
  "OAuth support for remote MCP servers": the redirect goes to
  `http://localhost:<random-port>/oauth/callback`.

## What each file holds

- `register.json`: the body `registerClient()` sends, field for field.
  - `client_name` is the fixed string `Gemini CLI MCP Client`; it does not embed the server name
    given to `gemini mcp add`. The registry does not match it, so attribution falls through to the
    recognised `clientInfo.name` (ADR 0094).
  - `redirect_uris`: `http://localhost:<port>/oauth/callback`. The port is OS-assigned unless the
    server's `oauth.redirectUri` or `OAUTH_CALLBACK_PORT` sets one; `7777` stands in for it.
  - `token_endpoint_auth_method` is `none`: a public client, so no secret is issued.
  - `scope` is `""`: Gemini CLI sends the configured `oauth.scopes` joined by spaces, and an empty
    string when none are configured.
- `initialize.json`: `clientInfo.name` `gemini-cli-mcp-client`, the name ADR 0035 already
  verified from the same file; `clientInfo.version` is the CLI's own version, so `0.61.0`.
  `capabilities` is the `roots` capability `connectToMcpServer()` registers. `protocolVersion` is
  `LATEST_PROTOCOL_VERSION` of the MCP SDK Gemini CLI 0.61.0 pins (`@modelcontextprotocol/sdk`
  1.23.0). The replay uses only `clientInfo`.

## Request order

Not observed. The source says Gemini CLI connects to `/mcp`, gets a 401, discovers the
`.well-known` documents, registers, then opens the browser for `/authorize`; its local callback
server rejects a redirect without an `iss` parameter matching the discovered issuer (RFC 9207).
