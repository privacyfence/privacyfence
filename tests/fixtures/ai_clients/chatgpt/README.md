# ChatGPT (organization mode, Developer Mode connector)

**SEEDED FROM VENDOR DOCS, NOT YET CAPTURED (2026-09-28).** Nothing here was observed from a real
ChatGPT handshake. It stands in until M1.3 in `ai-client-qa.md` captures one, and is then replaced
wholesale, as claude.ai's seeded fixture was. Until then the replay proves only that a client
shaped like this one gets through; it pins nothing about ChatGPT itself.

- Expected agent_id, unpinned: `chatgpt`
- Expected agent_id, pinned: `chatgpt`

## Sources, and how far to trust each field

OpenAI's own pages could not be opened from the session that wrote this (every fetch was refused
by its network egress policy), so each value below comes from a search engine's summary of the
named page, not from reading it. Re-check each one against the page, or better the capture,
before relying on it.

- `register.json`:
  - `redirect_uris`: `https://chatgpt.com/connector_platform_oauth_redirect`, the stable redirect
    URI ChatGPT uses when the authorization server meets its issuer-identification requirements,
    per <https://developers.openai.com/plugins/build/auth>. The same page says ChatGPT otherwise
    uses a per-connector `https://chatgpt.com/connector/oauth/{callback_id}`, and that it prefers
    Client ID Metadata Documents over DCR where the server offers them; PrivacyFence offers DCR.
  - `token_endpoint_auth_method`: `none` (a public client using PKCE). No OpenAI page gave the
    DCR value; `none` is from a community.openai.com thread, not OpenAI's documentation.
  - `client_name`: **not observed.** No source names what ChatGPT sends to `/register`, so the
    file says `not-observed` rather than guessing. The registry does not match it, so attribution
    falls through to `clientInfo.name` (ADR 0094).
  - `grant_types` and `response_types`: the authorization-code defaults every DCR client here
    sends; not from a ChatGPT source.
- `initialize.json`: `clientInfo` `openai-mcp` / `1.0.0` and `protocolVersion` `2025-03-26` are
  from a community.openai.com thread, not OpenAI's documentation. `openai-mcp` is the name the
  registry's `chatgpt` entry has matched since ADR 0035 marked it a guess, which is why the
  expected `agent_id` is `chatgpt` in both cases. A capture that shows another name changes the
  registry (ADR 0094), not this expectation.
