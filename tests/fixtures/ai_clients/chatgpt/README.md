# ChatGPT (organization mode, Developer Mode connector)

**Captured 2026-09-28, client version: ChatGPT web (no version exposed; handshake
`clientInfo.version` 1.0.0), against PrivacyFence 5.0.0** — M1.3 in `ai-client-qa.md`, evidence
at https://github.com/privacyfence/privacyfence/issues/391#issuecomment-5871022834.

- Expected agent_id, unpinned: `chatgpt`
- Expected agent_id, pinned: `chatgpt`

## What each file holds

- `register.json`: the fields ChatGPT sent to `/register`, from the deployment's
  `oauth_clients.json`. Scrubbed: the issued `client_id` and `client_secret` are dropped, and the
  per-connector callback id in the redirect URI is replaced with `dummy-callback-id`.
  - ChatGPT is a **confidential native client**: `application_type` `native` and
    `token_endpoint_auth_method` `client_secret_post`. The seeded fixture this replaced guessed
    `none`; the replay now checks that a secret is issued and presented at `/token`.
  - It registered the **per-connector** redirect URI, `https://chatgpt.com/connector/oauth/<id>`,
    not the stable `https://chatgpt.com/connector_platform_oauth_redirect` the seeded fixture
    guessed.
  - `client_name` is `ChatGPT`, which the registry's `chatgpt` entry matches.
- `initialize.json`: **only `clientInfo.version` (`1.0.0`) was observed**, through the pinned
  audit entry. ChatGPT's `clientInfo.name` could not be seen, because the DCR name decides
  attribution before the handshake name is read, so the file says `not-observed` rather than
  guessing. It is not `openai-mcp`: before the registry knew `ChatGPT`, the unpinned call was
  recorded as `unknown:ChatGPT`, and a recognised handshake name would have won over that.
  `openai-mcp/1.0.0` is ChatGPT's HTTP `User-Agent`. `protocolVersion` and `capabilities` were not
  captured; the replay uses only `clientInfo`.

## Request order

An unauthenticated `/mcp` probe first, then discovery. Three HTTP stacks: `Python/3.14
aiohttp/3.13.5` for discovery and DCR, `openai-connectors-oauth/1.0` for `/token`, and
`openai-mcp/1.0.0` for `/mcp`. Query strings are dropped.

```text
POST /mcp  401                                     Python/3.14 aiohttp/3.13.5
GET  /.well-known/oauth-protected-resource/mcp     Python/3.14 aiohttp/3.13.5
GET  /.well-known/oauth-authorization-server       Python/3.14 aiohttp/3.13.5
GET  /.well-known/openid-configuration  404        Python/3.14 aiohttp/3.13.5
POST /register  201                                Python/3.14 aiohttp/3.13.5
GET  /authorize                                    browser
GET  /oauth/idp/callback                           browser
POST /token                                        openai-connectors-oauth/1.0
POST /mcp  401, discovery again, then POST /mcp    openai-mcp/1.0.0
POST /mcp  (every call in the chat)                openai-mcp/1.0.0 (Codex)
GET  /mcp-files/fetch/<token>  200                 curl/8.5.0
PUT  /mcp-files/slots/<token>  204                 curl/8.5.0
```

The last two are ChatGPT's code sandbox using a download link and an upload slot.

## ChatGPT desktop

Not a fixture: ChatGPT desktop connects to a local install, which has no registration. Its
handshake name is `codex-mcp-client`, the Codex CLI's, which the registry does not match yet.
