# ADR 0085: Tool annotations are truthful by default; an organization bundle or a local connection can ask for every tool read-only

## Status

Accepted — 2026-09-27. Implemented. Supersedes
[ADR 0076](0076-every-connector-tool-is-advertised-read-only.md). Resolves
[issue 46](https://github.com/privacyfence/privacyfence/issues/46).

## Context

ADR 0076 advertised every connector tool to MCP clients as read-only, non-destructive and
idempotent, whatever the tool really does. The reason was claude.ai on a Team plan: a tool marked
`destructiveHint = true` gets a confirmation prompt on every call, "Allow all for this task" is
greyed out, and no organization-level pre-approval exists
([anthropics/claude-ai-mcp#491](https://github.com/anthropics/claude-ai-mcp/issues/491)). The user
would confirm the same write twice. ADR 0076 called itself a workaround, and
[issue 46](https://github.com/privacyfence/privacyfence/issues/46) tracked undoing it.

The workaround has a cost for everyone else. Every client, and anyone reviewing the tool list, is
told that `gmail_create_draft` and `calendar_delete_event` are read-only. Clients that do not prompt
annoyingly (Claude Code, Claude Desktop with its own per-tool settings) lose information they could
use, and a reviewer sees write tools labelled read-only. MCP annotations are hints and not a
security boundary either way: `gate.py` decides, in the daemon.

PrivacyFence is also about to support clients other than Claude, each with its own prompting
behaviour. A uniform lie tuned to one client's plan is the wrong default for all of them.

## Decision

1. **Truthful by default.** `web/mcp_tools.py`'s `to_mcp_tool(spec, *, annotations_mode)` derives
   each connector tool's hints from its `ToolSpec`:
   - a read (`read_only=True`): `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`;
   - a write: `readOnlyHint=false`, `destructiveHint=spec.destructive`, `idempotentHint=false`.

   PrivacyFence's own `privacyfence_*` meta-tools keep their individually declared annotations in
   every mode.

2. **Writes are not destructive, except the two that delete.** `ToolSpec` gains
   `destructive: bool = False`. It is set on exactly two tools, the only ones that delete something:
   `calendar_delete_event` (an event) and `drive_sheets_delete_dimensions` (rows or columns of a
   spreadsheet). Overwrites such as `drive_write_file_content` and `drive_sheets_write_range` stay
   non-destructive. The maintainer's reasoning: `destructiveHint` should mean what a person means
   by "destructive", that something is gone, so the clients that honour it prompt hardest on the
   calls that deserve it. Marking every write destructive would make the hint carry no information
   beyond `readOnlyHint=false`, and would put the heaviest client prompt on sending a draft. A test
   (`tests/unit/test_connector_tool_annotations.py`) pins the destructive set to exactly these two,
   so a new deleting tool fails CI until someone classifies it on purpose.

3. **An organization bundle can ask for every tool read-only.** `org_config.json`'s
   `mcp.tool_annotations` is `"truthful"` or `"all_read_only"`; absent means truthful. It applies
   in both modes, like `unattended_sessions`. `all_read_only` is ADR 0076's uniform triple for every
   connector tool, for an organization whose client would otherwise prompt on every write.
   `scripts/build_org_bundle.py --tool-annotations {truthful,all-read-only}` writes it, only when
   given. Any other value makes the daemon refuse to start rather than guess.

4. **In local mode, one connection can choose for itself.** A client may send
   `X-PrivacyFence-Tool-Annotations: truthful | all-read-only` on its `/mcp` requests. Precedence:
   the connection's header, then the bundle's `mcp.tool_annotations`, then `truthful`. Any other
   header value is answered `400 Bad Request`, so a typo fails loudly instead of silently falling
   back. This is how a Claude Code user (`claude mcp add … --header …`) or a second Claude Desktop
   extension that sends the header can opt out of client prompts on one machine without an
   organization bundle.

5. **In organization mode the header is ignored.** The administrator's bundle decides what every
   member's client is told. A connection's choice is a claim made by the client, and on an
   organization server the administrator, not whichever client connects, owns what the members'
   clients are told about each tool. Ignoring it (rather than refusing it) keeps a client that sends
   the header, such as the local-mode extension pointed at an organization server, working.

## Alternatives considered

- **Keep every tool uniformly read-only (ADR 0076).** Rejected: it tells every client and every
  reviewer something false in order to suit one client's plan, and it is the wrong default for the
  non-Claude clients being added. The organization switch and the per-connection header keep it
  available for those who want it.
- **Per-client annotations keyed on agent identity** (read-only for claude.ai, truthful for
  others). Rejected: the identity is a claim. `clientInfo.name` and an unpinned DCR `client_name`
  are strings the client chose ([ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)),
  and ADR 0006 keeps claimed identity from changing an outcome. It would also need a table of
  client behaviours kept up to date by hand.
- **A `settings.yaml` key for local mode.** Rejected in favour of the per-connection header: the
  choice belongs to the client that prompts, not to the whole install, and a person running Claude
  Code and Claude Desktop side by side may want different answers for each.
- **`destructiveHint=true` for every write** (MCP's default when the hint is absent). Rejected, see
  decision 2: it makes the hint carry no information and puts the heaviest prompt on ordinary
  writes.

## Consequences

- Clients that honour the hints may now ask for their own confirmation before a write, in front of
  PrivacyFence's approval. This changes behaviour for every existing install on upgrade. The
  changelog says so and names both switches.
- A client told the truth still gets no more than a hint: every call still goes through `gate.py`,
  and the gate tables (`auto_accept.TOOL_TO_GATE`) remain the control.
- A new deleting tool must set `destructive=True` and be added to the pinned set; a new tool that
  only overwrites must not.
- The header is read on every `tools/list` request, not frozen at `initialize`: a client that
  sends it must send it on every request (the bundled shim and Claude Code's `--header` both do).

## Verification

- `tests/unit/web/test_mcp_tools.py`'s `TestToMcpTool.test_annotations_per_mode` and
  `TestAnnotationsModeFromHeader`.
- `tests/unit/web/test_routes_mcp.py`'s `TestToolAnnotationsOverTheWire`: a live `/mcp`
  `list_tools` for every mode, header precedence in local mode, the header ignored in org mode, a
  bad header answered 400, and the meta-tools unchanged in every mode.
- `tests/unit/test_connector_tool_annotations.py`: the destructive set, and every destructive tool
  is a write.
- `tests/unit/test_org_mode.py`'s `TestResolveToolAnnotations` and `tests/unit/test_daemon_main.py`
  (the daemon refuses an unknown value).
- `tests/unit/test_build_org_bundle.py`'s `TestToolAnnotationsFlag`.

## Related

- [ADR 0076](0076-every-connector-tool-is-advertised-read-only.md), superseded by this ADR.
- [Issue 46](https://github.com/privacyfence/privacyfence/issues/46).
- [ADR 0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md): why
  a client's name is a claim.
