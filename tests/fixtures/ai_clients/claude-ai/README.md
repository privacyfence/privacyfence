# claude.ai (organization mode, custom connector)

**SEEDED FROM VENDOR DOCS, NOT YET CAPTURED.** WP 2.1 replaces this fixture with one captured
from a real claude.ai connection, following `ai-client-qa.md`.

- Expected agent_id, unpinned: `unknown:Claude`
- Expected agent_id, pinned: `claude`

## What each file holds, and where it came from

- `register.json`: the DCR `/register` body.
  - `redirect_uris`: `https://claude.ai/api/mcp/auth_callback`, the callback URL Anthropic
    documents for custom connectors. The same page says it may move to
    `https://claude.com/api/mcp/auth_callback`.
  - `client_name`: `Claude`. The same page gives "Claude" as claude.ai's OAuth client name.
  - `token_endpoint_auth_method` `none`, `grant_types` and `response_types`: a public client
    using PKCE, which is what the same page and the MCP authorization spec describe. Not
    documented field by field.
- `initialize.json`: the MCP `initialize` params. Anthropic does not document claude.ai's
  `clientInfo`; `claude-ai` is `agent_identity.py`'s registry entry for Claude, which that
  registry's ADR marks as a guess. The version is a placeholder.

## Why the unpinned agent_id is `unknown:Claude`

In organization mode the DCR `client_name` wins over the handshake's `clientInfo.name`. The
documented `client_name` is `Claude`, and the registry matches only `claude-ai`, so an unpinned
claude.ai registration is attributed as an unrecognised client named "Claude" today. If the
capture confirms that name, WP 2.1 adds it to the registry and changes the line above to `claude`.

## Sources

- Anthropic, "Building custom connectors via remote MCP servers":
  https://support.anthropic.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers
- MCP specification, Authorization:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
