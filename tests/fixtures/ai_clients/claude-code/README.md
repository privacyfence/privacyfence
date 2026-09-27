# Claude Code (organization mode, `claude mcp add --transport http`)

**SEEDED FROM VENDOR DOCS, NOT YET CAPTURED.** WP 2.1 replaces this fixture with one captured
from a real Claude Code connection, following `ai-client-qa.md`.

- Expected agent_id, unpinned: `claude-code`
- Expected agent_id, pinned: `claude-code`

## What each file holds, and where it came from

- `register.json`: the DCR `/register` body.
  - `redirect_uris`: a loopback `http://localhost:<port>/callback`. Claude Code's MCP docs say it
    picks a random free port for the OAuth callback unless `--callback-port` fixes it, and give
    the redirect URI's form as `http://localhost:PORT/callback`. The port here is arbitrary.
  - `client_name`: Claude Code's docs do not say what it registers as. `claude-code` is
    `agent_identity.py`'s registry entry for Claude Code, used until a capture shows the real
    name.
  - `token_endpoint_auth_method` `none`, `grant_types` and `response_types`: a public client
    using PKCE, as the MCP authorization spec describes. Not documented field by field.
- `initialize.json`: the MCP `initialize` params. `clientInfo.name` `claude-code` is the same
  registry entry; the version is a placeholder.

## Sources

- Claude Code docs, "Connect Claude Code to tools via MCP" (OAuth, `--callback-port`):
  https://code.claude.com/docs/en/mcp
- MCP specification, Authorization:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
