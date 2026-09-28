# AI-client handshake fixtures

What real AI clients send when they connect to an organization-mode PrivacyFence, replayed
in-process by `tests/unit/web/test_ai_client_replay.py` on every PR. The test is parametrized over
every directory here, so adding a client means adding a directory; no test code changes.

## Layout

```text
ai_clients/
  <client>/            one directory per client, named by its slug (claude-ai, claude-code, ...)
    register.json      the DCR POST /register body, exactly as the client sent it
    initialize.json    the MCP initialize params: protocolVersion, capabilities, clientInfo
    README.md          where the files came from, and the expected attribution
```

Each `README.md` carries two lines the replay test reads:

```text
- Expected agent_id, unpinned: `<agent_id>`
- Expected agent_id, pinned: `<registry agent_id>`
```

*Unpinned* is what the call is attributed to from the client's own claims: in organization mode,
a DCR `client_name` the registry matches wins over `clientInfo.name`; one it doesn't match yields
to a `clientInfo.name` it does (ADR 0092); and when neither matches, the DCR name is recorded as
`unknown:<name>`. *Pinned* is the registry entry an admin pins the registration to on
**Settings → AI systems**.

The first line of a fixture's `README.md` says when it was captured and from which client version.
A field that could not be observed is named as such there, never filled with a guess.

## Capturing a fixture

Follow the per-client script in `ai-client-qa.md`, against the test organization deployment it
describes. Its last step saves the fixture: the client's registration from the deployment's
`oauth_clients.json`, reduced to the fields the client sent to `/register`, and the `initialize`
params it sent to `/mcp`.

## Scrubbing

A fixture is committed, so scrub it before it enters the repository:

- Replace the deployment's host with `pf.example.com`.
- Replace every issued or client-chosen identifier (client ids, user ids, tenant ids, session ids)
  with an obvious dummy.
- Remove every secret: client secrets, tokens, codes, PKCE verifiers.
- Keep everything the client decides: `client_name`, `redirect_uris`,
  `token_endpoint_auth_method`, `grant_types`, `response_types`, scopes and `clientInfo`. Those
  are what the replay tests. A redirect URI on the vendor's own domain stays as it is.

Read the diff before committing it, as `connector-qa.md` asks for connector fixtures.
