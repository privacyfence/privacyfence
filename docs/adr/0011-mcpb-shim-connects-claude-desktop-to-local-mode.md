# ADR 0011: a `.mcpb` stdio shim connects Claude Desktop to local mode with zero hand-configuration

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-08-28 in
`docs/https-connector-refactor-plan.md` §12 and §15 D11, deleted in `96cd5af4`). Implemented as
phase P4b. The shim was removed on 2026-09-02 (`8ea876c8`) in favour of a "Connect Claude" card
(P4c), and restored on 2026-09-03 (`beb445a2`); the plan's D12 records that reversal.
[ADR 0007](0007-local-file-bridge.md) builds on this decision.

## Context

Before the HTTP refactor, `PrivacyFence.mcpb` wrapped the Node bridge (`bridge/`): an stdio MCP
server that Claude Desktop installs on double-click and spawns per session, which found the daemon
through `~/.privacyfence/ipc_port`, read `ipc_token` itself, and forwarded calls over a loopback TCP
socket. No config file was edited and no secret copied by hand.

Phase P2 put MCP on the daemon's own `/mcp` endpoint, and P5 was to delete the bridge. Claude Code
connects to `/mcp` natively; Claude Desktop, in local mode, had no equivalent. The plan found none
of the obvious routes worked:

- Desktop's own config file did not reliably accept a Streamable HTTP entry, because of an open
  upstream bug (`anthropics/claude-code#37286`, up to silently clearing `mcpServers`);
- Settings → Connectors connects from Anthropic's cloud, so it needs a public HTTPS URL, which is
  org mode's shape, not local mode's;
- local mode's bearer-token-in-a-file posture is permanent (ADR 0009), so a manually configured
  secret would stay manual.

## Decision

PrivacyFence ships its own thin stdio-to-Streamable-HTTP shim inside `PrivacyFence.mcpb`
(`mcpb/shim/`, staged as `server/shim.js`). It reads the daemon's `/mcp` URL and bearer token from
discovery files the daemon writes, launches the daemon if it is not running, and pipes MCP frames
between its stdio transport and `/mcp`.

It is a transport proxy only: no `ToolSpec` knowledge, no manifest fetch, no tool registration, no
JSON-RPC framing of its own. The plan's review rule: if connector knowledge appears in it, it has
grown into the thing it replaced. It talks only to `/mcp`, never to `/approvals` or `/settings`.

## Alternatives considered

- **Wait for Desktop's own HTTP config support.** Rejected: it would make retiring the bridge
  depend on an external bug fix that cannot be scheduled.
- **Declare Desktop + local mode unsupported after P5.** Rejected: it contradicts local mode's own
  definition (used from Claude Code or Claude Desktop) and makes the shipped DMG + `.mcpb` path a
  dead end.
- **Recommend `mcp-remote`.** Rejected: a third-party dependency, a hand-edited config, and an
  `--allow-http` flag whose purpose is to switch off a safety check.
- **A "Connect Claude" card on `/settings` showing the URL and token (P4c).** Tried and reverted.
  D12's final reasoning: on a Team-plan claude.ai account a user cannot add a custom MCP connector
  themselves, so the card offers nothing there, and Desktop still needs a zero-config path. The plan
  marked P4c to be revisited once org mode shipped; no later record of that revisit exists.

## Consequences

- The zero-config install survives: Desktop writes the `mcpServers` entry from
  `mcpb/manifest.json.tmpl`, and the shim reads the token so the human never copies it.
- The daemon writes an `mcp_url` discovery file on bind and clears it on stop, the successor of
  `ipc_port`. Under privilege separation (ADR 0003) the two files the shim reads live in the handoff
  directory, the part of the daemon's data that stays reachable from the user's session.
- A Node process still runs per Desktop session; the plan did not claim a smaller runtime surface.
- Because the shim understands no schema, wire-format drift between shim and daemon is not a class
  of bug it can have. ADR 0007 later added one named, bounded exception (the local file bridge).
- Because the shim runs as the human, it is the component ADR 0007 uses to carry files across the
  privilege-separation boundary.

## Verification

- `mcpb/shim/src/index.ts` (module docstring: the shim's whole job), `proxy.ts`, `daemon.ts`,
  `protocol.ts` (discovery files, handoff directory); `mcpb/manifest.json.tmpl`.
- `src/privacyfence/web/server.py`: `_write_mcp_url_file` / `_clear_mcp_url_file`.
- `mcpb/shim/test/` (including `proxy.test.ts`); `tests/integration/test_shim_mcp_contract.py`.

## Related

- `git show 96cd5af4^:docs/https-connector-refactor-plan.md` §8.1, §12 ("Gap found while
  implementing P2"), §15 D11 and D12.
- Commits `ae7d6892` (D11 decided), `00b7f78d` (P4b implemented), `8ea876c8` (P4b reverted),
  `beb445a2` (P4c reverted, P4b restored).
- ADR 0007 (builds on this), ADR 0009 (the plain-HTTP posture), ADR 0003 (privilege separation).
