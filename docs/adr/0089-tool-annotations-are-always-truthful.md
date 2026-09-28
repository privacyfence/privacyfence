# ADR 0089: Tool annotations are always truthful; the all-read-only mode and the no-prompts extension are removed

## Status

Accepted — 2026-09-28. Implemented.
Supersedes in part [ADR 0086](0086-tool-annotations-are-truthful-by-default.md) (its decisions 3,
4 and 5: the organization bundle's `mcp.tool_annotations` and the per-connection
`X-PrivacyFence-Tool-Annotations` header).
Supersedes [ADR 0087](0087-two-claude-desktop-extensions-ship-in-the-dmg-and-the-windows-installer.md).
Part of [issue 46](https://github.com/privacyfence/privacyfence/issues/46).

## Context

[ADR 0076](0076-every-connector-tool-is-advertised-read-only.md) advertised every connector tool
as read-only, non-destructive and idempotent, whatever it really does, so that Claude clients would
not put their own confirmation in front of PrivacyFence's approval card: at the time they prompted
on every write and had no way to always allow a write tool. ADR 0086 made truthful annotations the
default and kept ADR 0076's uniform triple as an opt-in, through an organization bundle key and,
in local mode, a request header. ADR 0087 shipped that header to Claude Desktop users as a second
extension, `PrivacyFence-no-prompts.mcpb`. All three were workarounds for the client side.

Two things have changed since:

- **The clients.** The maintainer's manual QA on 2026-09-27, against 5.0.0a1 with Claude Code
  2.1.283 and Claude Desktop 2.9939.2 on macOS, showed both clients now let the user always allow
  a write tool. A user who does not want the client's own prompt in front of PrivacyFence's card
  can turn it off in the client, per tool, without PrivacyFence saying anything untrue. The
  evidence is on [issue 46](https://github.com/privacyfence/privacyfence/issues/46).
- **The principle, stated by the maintainer on 2026-09-28.** PrivacyFence is a privacy tool and
  must not tell AI clients anything false. An opt-in lie is still a lie told on the user's behalf
  to a client and to anyone who reviews its tool list, and every option that exists has to be
  documented, tested, shipped and explained.

The opt-in only ever shipped in the 5.0.0a1 pre-release; no stable release carried it.

## Decision

1. **Truthful annotations are the only behaviour, in both modes.** `web/mcp_tools.py`'s
   `to_mcp_tool(spec)` derives each connector tool's hints from its `ToolSpec` and nothing else:
   a read is `readOnlyHint=true, destructiveHint=false, idempotentHint=true`; a write is
   `readOnlyHint=false, idempotentHint=false`, with `destructiveHint=true` only when
   `ToolSpec.destructive` is set (`calendar_delete_event`, `drive_sheets_delete_dimensions`; ADR
   0086's decision 2 stands). The meta-tools keep their declared annotations. There is no mode
   parameter, no bundle key, no header and no second extension.

2. **A bundle still carrying `mcp.tool_annotations` stops the daemon from starting.** Only a
   bundle built by 5.0.0a1's `build_org_bundle.py --tool-annotations` can carry it.
   `org_mode.reject_removed_tool_annotations` raises `ConfigurationError` for the key whatever its
   value, with a message naming the key, ADR 0089 and the fix (rebuild without the flag), and
   `daemon_main.run_app` calls it where the bundle is loaded, so the daemon takes the same "print
   and refuse to start" path as any other broken bundle. The reason for refusing rather than
   ignoring: an administrator who configured `all_read_only` would otherwise silently get
   different behaviour from what their bundle says, and find out only when members report prompts.
   A refusal is loud once, at upgrade time, on a pre-release install. Refusing the value `truthful`
   too keeps the rule to one sentence ("the key is removed") and costs nothing: it already means
   today's behaviour, and a rebuild drops it. `build_org_bundle.py --merge` drops the key from the
   bundle it rewrites, so the rebuild the message asks for is one command.

3. **A request still sending `X-PrivacyFence-Tool-Annotations` is ignored, not refused.** The
   header is not read at all, and the `400 Bad Request` ADR 0086 gave a bad value is gone with
   it. The only sender is a 5.0.0a1 no-prompts extension still installed in someone's Claude
   Desktop. Refusing its requests would break a working connection for a header that can no
   longer change anything; ignoring it gives that user truthful annotations and a working tool
   list. Unlike the bundle, the header was never an administrator's configuration, so nobody is
   silently overridden.

4. **The DMG carries the `.pkg` and one `.mcpb` again, and the Windows installer one `.mcpb`.**
   `scripts/build_mcpb.sh` builds one extension from `mcpb/manifest.json.tmpl`;
   `scripts/mcpb_manifest.py` and the second manifest are deleted, and so are the shim's
   `--tool-annotations` flag and header. The shim again passes no annotation choice of any kind:
   what reaches Claude Desktop is what the daemon says.

5. **Users who do not want the client's own prompt always-allow PrivacyFence's tools in the
   client.** The docs for each client say how. PrivacyFence's approval card remains the
   confirmation that decides; every call still goes through `gate.py`.

## Alternatives considered

- **Keep the opt-in (bundle key, header and second extension).** Rejected: it tells clients
  something false, which a privacy tool must not do even when asked to, and the clients it was for
  no longer need it (see Context).
- **Keep only the organization bundle switch.** Rejected for the same reason: an administrator's
  choice is still a false statement to every member's client, and it would keep a code path,
  tests and documentation alive for a workaround no longer needed by the clients it was built for.
- **Ignore a bundle's `mcp.tool_annotations` with a warning in the log.** Rejected, see decision
  2: an administrator who asked for all-read-only would silently get something else, and a log
  line on a server is easy to miss.
- **Keep answering a bad header value 400.** Rejected, see decision 3: the header has no effect
  left to protect, and a refusal would only break the one client that still sends it.

## Consequences

- A client that honours the hints may ask for its own confirmation before a write, in front of
  PrivacyFence's card, and PrivacyFence offers no way to prevent that; the user turns it off in
  the client. Claude Code and Claude Desktop support that (Context). claude.ai was not part of the
  2026-09-27 QA: if its Team plan still cannot always-allow a write tool
  ([anthropics/claude-ai-mcp#491](https://github.com/anthropics/claude-ai-mcp/issues/491), ADR
  0086's context), its users confirm such a write twice. That cost is accepted rather than
  answered with a false annotation.
- A 5.0.0a1 organization install whose bundle was built with `--tool-annotations` does not start
  after upgrading until the bundle is rebuilt. The changelog's Removed entry says so.
- A 5.0.0a1 user who installed "PrivacyFence (no Claude prompts)" keeps a working connection, with
  truthful annotations. Installing `PrivacyFence.mcpb` next to it lists every tool twice, because
  the two manifests have different names; the changelog tells them to remove the old one.
- Nothing about gating changes: annotations were never a security boundary.
- The next time a client prompts in a way users dislike, the answer is the client's own
  always-allow setting or a client-side fix, not a PrivacyFence annotation mode.

## Verification

- `tests/unit/web/test_mcp_tools.py`'s `TestToMcpTool.test_annotations_are_truthful`.
- `tests/unit/test_connector_tool_annotations.py`: the pinned destructive set, and every real
  connector tool advertised exactly as its `ToolSpec` says.
- `tests/unit/web/test_routes_mcp.py`'s `TestToolAnnotationsOverTheWire`: a live `/mcp`
  `list_tools` is truthful in both modes whatever `X-PrivacyFence-Tool-Annotations` says, the
  meta-tools are unchanged, and a request carrying the header is not refused.
- `tests/unit/web/test_tool_schema_portability.py`: one advertised list, which the live
  `tools/list` matches, annotations included.
- `tests/unit/test_org_mode.py`'s `TestRejectRemovedToolAnnotations`, `tests/unit/test_daemon_main.py`
  (the daemon refuses a bundle carrying the key) and `tests/unit/test_build_org_bundle.py`'s
  `TestRemovedToolAnnotations` (the flag is gone; `--merge` drops the key).
- `tests/integration/test_shim_mcp_contract.py`: the real shim passes the daemon's truthful
  annotations through.
- `tests/integration/test_macos_packaged_smoke.py` and `test_windows_packaged_smoke.py`: exactly
  one `.mcpb` on the DMG and in the install folder.

## Related

- [ADR 0076](0076-every-connector-tool-is-advertised-read-only.md), the original workaround.
- [ADR 0086](0086-tool-annotations-are-truthful-by-default.md), superseded in part: its decisions
  1 and 2 (truthful hints, the destructive set) stand.
- [ADR 0087](0087-two-claude-desktop-extensions-ship-in-the-dmg-and-the-windows-installer.md),
  superseded.
- [Issue 46](https://github.com/privacyfence/privacyfence/issues/46), with the 2026-09-27 QA
  evidence.
