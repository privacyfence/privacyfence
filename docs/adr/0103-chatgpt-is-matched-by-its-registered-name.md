# ADR 0103: ChatGPT is matched by the name it registers with, and the `openai-mcp` guess is removed

## Status

Accepted — 2026-09-28. Implemented.
Amends [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)
(decision 2's registry) and
[ADR 0094](0094-claude-clients-are-matched-by-their-observed-names.md) (decision 1's rule that the
registry holds only observed names, applied to ChatGPT).
Part of [issue 391](https://github.com/privacyfence/privacyfence/issues/391) and
[issue 390](https://github.com/privacyfence/privacyfence/issues/390).

## Context

ADR 0035 decision 2 seeded `agent_identity.REGISTRY` with `chatgpt` matching the handshake name
`openai-mcp`, a guess. ADR 0094 corrected the Claude guesses from real handshakes; ChatGPT's had
not been observed yet. The maintainer's manual QA against 5.0.0 on 2026-09-28 is that handshake:
[M1.3](https://github.com/privacyfence/privacyfence/issues/391#issuecomment-5871022834) (ChatGPT
web, Developer Mode, organization mode) and
[M1.4](https://github.com/privacyfence/privacyfence/issues/390#issuecomment-5872180177) (ChatGPT
desktop for macOS, local mode). It found:

| Client | Mode | DCR `client_name` | Handshake `clientInfo` | Recorded before this ADR |
|---|---|---|---|---|
| ChatGPT web, Developer Mode | org | `ChatGPT` | not observable; version `1.0.0` | `unknown:ChatGPT` |
| ChatGPT desktop 26.924.22138 (macOS) | local | — | `codex-mcp-client` 0.158.0-alpha.2.1 | `unknown:codex-mcp-client` |

- Neither name embeds a part the user chose, so neither needs a `{server}` name template.
- ChatGPT web's handshake name is **not** `openai-mcp`. Under ADR 0094 decision 3 an unrecognised
  DCR name yields to a recognised handshake name, and the registry recognised `openai-mcp`, yet the
  unpinned call was recorded as `unknown:ChatGPT`. `openai-mcp/1.0.0` is ChatGPT's HTTP
  `User-Agent`, which is probably where the guess came from.
- ChatGPT desktop is built on Codex: its MCP configuration is shared with the Codex CLI, and its
  handshake name is the Codex CLI's. PrivacyFence cannot tell the two apart.

## Decision

### 1. `chatgpt` matches `ChatGPT`, the name ChatGPT registers with

The registry's `chatgpt` entry matches the exact name `ChatGPT`, ChatGPT's DCR `client_name` for a
Developer Mode connector. The guess `openai-mcp` is removed: no ChatGPT client was seen sending
it. ChatGPT web's handshake name stays unknown and is not guessed; it matters only if the DCR name
stops matching, exactly as for claude.ai under ADR 0094.

### 2. `codex-mcp-client` is recorded as observed, and deliberately gets no entry yet

ChatGPT desktop's handshake name, `codex-mcp-client`, is not added to `chatgpt` or to a new entry
in this change:

- It names the Codex CLI as much as ChatGPT desktop, so it is not ChatGPT's name, and matching it
  as `chatgpt` would misname Codex CLI calls.
- A new entry needs its own mark in `resources/agent_icons/`, which `test_approval_icons.py`
  requires for every entry, and its own name.
- ChatGPT desktop connects to a local install, where cards and the Audit Log show every requester
  as "Undetected" ([ADR 0088](0088-local-mode-shows-every-requester-as-undetected.md)). An entry
  would change only the audit entry's `agent_id`, which already records the exact name as
  `unknown:codex-mcp-client`.

The ChatGPT desktop work package that documents local mode decides whether it gets an entry.

## Consequences

- ChatGPT in organization mode is attributed to ChatGPT without a pin. Its cards still say
  "Says it is ChatGPT" and **Not verified** until an admin pins the registration on
  Settings → AI systems.
- A registration pinned to `chatgpt` before this change keeps its pin: pins name an `agent_id`,
  and `chatgpt` keeps its.
- A client whose handshake name is `openai-mcp` is now recorded as `unknown:openai-mcp`.
- The replay fixture `tests/fixtures/ai_clients/chatgpt/` is the captured one: `application_type`
  `native`, `client_secret_post`, and the per-connector redirect URI
  `https://chatgpt.com/connector/oauth/<id>`, where the seeded fixture had guessed `none` and the
  stable `connector_platform_oauth_redirect`.

## Alternatives considered

- **Keep `openai-mcp` in case some ChatGPT surface sends it.** Rejected: ADR 0035's rule is that a
  guess is corrected from a real handshake, and M1.3 showed ChatGPT web's handshake is not it.
- **Match `codex-mcp-client` as `chatgpt`.** Rejected: it is the Codex CLI's name too, and a
  registry name is a claim of which product is asking, so it would name every Codex CLI call
  ChatGPT.
- **Match the `User-Agent` `openai-mcp/…`.** Rejected: attribution reads the client's MCP and DCR
  claims only (ADR 0035), not HTTP headers, and a header would add a second, weaker kind of claim
  for no gain over the registered name.
