# Claude Code (organization mode, `claude mcp add --transport http`)

**Captured 2026-09-28, client version Claude Code 2.1.283, against PrivacyFence 5.0.0a2** — M1.2
in `ai-client-qa.md`, evidence at
https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865527889.

- Expected agent_id, unpinned: `claude-code`
- Expected agent_id, pinned: `claude-code`

## What each file holds

- `register.json`: the fields Claude Code sent to `/register`, from the deployment's
  `oauth_clients.json`, verbatim. Scrubbed: the issued `client_id` is dropped. There is no secret;
  Claude Code is a public client (`token_endpoint_auth_method` `none`).
  - `client_name` is `Claude Code (<server>)`, where `<server>` is the name the user gave
    `claude mcp add` (`privacyfence` here). The registry matches it as a name template, not a
    fixed string (ADR 0094).
  - `redirect_uris`: a loopback `http://localhost:<port>/callback` with a random port.
- `initialize.json`: `clientInfo` `claude-code` 2.1.283, observed in the audit log of the same
  client in local mode (M1.1) and as the pinned version here. `protocolVersion` and
  `capabilities` were not captured; the replay uses only `clientInfo`.

## Request order

Claude Code probes `/mcp` without a token first, and does DCR and `/token` with a separate HTTP
stack (`Bun/1.4.3`).

```text
POST /mcp  -> 401                                claude-code/2.1.283 (cli)
GET  /.well-known/oauth-protected-resource/mcp   claude-code/2.1.283 (cli)
GET  /.well-known/oauth-authorization-server     claude-code/2.1.283 (cli)
GET  both .well-known documents, twice each      Bun/1.4.3
POST /register                                   Bun/1.4.3
GET  /authorize                                  browser
GET  /oauth/idp/callback                         browser
POST /token                                      Bun/1.4.3
POST /mcp  (initialize, then every call)         claude-code/2.1.283 (cli)
```
