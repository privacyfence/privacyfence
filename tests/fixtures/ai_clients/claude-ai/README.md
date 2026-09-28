# claude.ai (organization mode, custom connector)

**Captured 2026-09-28, client version: claude.ai web (no version exposed; handshake
`clientInfo.version` 1.0.0), against PrivacyFence 5.0.0a2** — M1.3 in `ai-client-qa.md`, evidence
at https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865753049.

- Expected agent_id, unpinned: `claude`
- Expected agent_id, pinned: `claude`

## What each file holds

- `register.json`: the fields claude.ai sent to `/register`, from the deployment's
  `oauth_clients.json`, verbatim. Scrubbed: the issued `client_id` and `client_secret` are
  dropped, and so is `client_secret_expires_at` (`0`, never expires), which the server issues
  rather than the client sending it.
  - claude.ai is a **confidential web client**: `application_type` `web` and
    `token_endpoint_auth_method` `client_secret_post`. The seeded fixture this replaced guessed
    `none`; the replay now checks that a secret is issued and presented at `/token`.
  - `client_name` is `Claude`, which the registry's `claude` entry matches (ADR 0094).
- `initialize.json`: **only `clientInfo.version` (`1.0.0`) was observed**, through the pinned
  audit entry. claude.ai's `clientInfo.name` could not be seen, because the DCR name decides
  attribution before the handshake name is read, so the file says `not-observed` rather than
  guessing. `protocolVersion` and `capabilities` were not captured; the replay uses only
  `clientInfo`.

## Request order

No unauthenticated `/mcp` probe first. Two HTTP stacks: `python-httpx/0.28.1` for discovery, DCR
and `/token`, and `Claude-User` for `/mcp`.

```text
GET  /.well-known/oauth-protected-resource/mcp   python-httpx/0.28.1
GET  /.well-known/oauth-authorization-server     python-httpx/0.28.1
POST /register                                   python-httpx/0.28.1
GET  /authorize                                  browser
GET  /oauth/idp/callback                         browser
POST /token                                      python-httpx/0.28.1
POST /mcp  (initialize, then every call)         Claude-User
```

## Claude Desktop's custom connector

Not captured: M1.5 was not run, because no account available allowed custom connectors. Claude
Desktop's remote connectors most likely go through the same claude.ai account and register exactly
as above, but that is unverified, so there is no `claude-desktop` fixture here.
