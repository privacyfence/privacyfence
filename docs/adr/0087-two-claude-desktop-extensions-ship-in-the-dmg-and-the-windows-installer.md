# ADR 0087: Two Claude Desktop extensions ship in the DMG and the Windows installer

## Status

Accepted — 2026-09-27. Implemented. Builds on
[ADR 0086](0086-tool-annotations-are-truthful-by-default.md), whose decision 4 (the per-connection
`X-PrivacyFence-Tool-Annotations` header) this puts in front of Claude Desktop users. Part of
[issue 46](https://github.com/privacyfence/privacyfence/issues/46).

## Context

ADR 0086 made tool annotations truthful by default, so Claude Desktop may now ask for its own
confirmation before a write, in front of PrivacyFence's approval card. It gave two ways back to
ADR 0076's uniform read-only triple: an organization bundle key, and, in local mode, a header one
connection sends. A Claude Code user can send the header with `claude mcp add … --header`. A Claude
Desktop user cannot: Claude Desktop starts PrivacyFence's `.mcpb` with the arguments in its
manifest, and there was one manifest.

Until now the macOS DMG carried exactly two files, the `.pkg` and `PrivacyFence.mcpb`; the Windows
installer carried one `.mcpb` next to the daemon; the `.deb` carried none. `CLAUDE.md`'s
"macOS ships one file" and the packaged smoke tests were written against that set, so adding an
extension is a change to the release artifact set, not only to the shim.

## Decision

1. **Two extensions, one shim.** `scripts/build_mcpb.sh` builds both from the same bundled
   `shim.js` and the same `mcpb/manifest.json.tmpl`; `scripts/mcpb_manifest.py` renders the two
   manifests.
   - `PrivacyFence-<version>.mcpb`: unchanged. Manifest `name` `privacyfence`; the shim runs with
     no flag, so the daemon's own mode applies (truthful unless a bundle says otherwise).
   - `PrivacyFence-no-prompts-<version>.mcpb`: manifest `name` `privacyfence-read-only`,
     `display_name` "PrivacyFence (no Claude prompts)", a description that says Claude Desktop will
     not ask before PrivacyFence's own approval and that only one of the two should be installed,
     and `args` `["${__dirname}/server/shim.js", "--tool-annotations=all-read-only"]`.

   Nothing else differs between them.

2. **The shim passes the choice through and learns nothing else.** `--tool-annotations=<truthful|
   all-read-only>` makes the shim send `X-PrivacyFence-Tool-Annotations` with that value on every
   `/mcp` request. The shim still has no tool-schema knowledge
   ([ADR 0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md)): the daemon applies the
   mode to that connection's `tools/list`. A value the shim does not know is logged and dropped
   rather than refused, for the same reason `parseArgs` ignores unknown flags: a shim that exits
   before reading stdin is a failure neither Claude Desktop nor the daemon's log can show.

3. **The artifact set.** The DMG carries `PrivacyFence.pkg`, `PrivacyFence.mcpb` and
   `PrivacyFence-no-prompts.mcpb` (stable, unversioned names) and nothing else; the `.pkg`'s
   conclusion screen names both extensions and says to open one. The Windows installer puts both
   versioned files into `{app}`; its Finish-page step offers the default one and its label names
   the other. The `.deb` carries neither, because Claude Desktop does not run on Linux. Neither
   `.mcpb` is uploaded or attached on its own, on any platform.

4. **Install one.** With both installed, Claude Desktop lists every tool twice. Every place that
   offers them (the manifests' descriptions, the `.pkg` conclusion screen, the Windows Finish page,
   the install guides and `connect-claude-desktop.md`) says so.

## Alternatives considered

- **One extension with a user-configurable option** (an MCPB `user_config` field Claude Desktop
  asks for at install time). Rejected: the choice would be buried in an install dialog and in
  Claude Desktop's extension settings rather than visible in which file you opened, and it would
  need the manifest's `user_config` substitution to carry the flag, which the shim would then have
  to parse from a value that may be empty. Two files make the choice a name.
- **A `settings.yaml` key or a companion menu toggle for local mode.** Rejected in ADR 0086 for the
  same reason it applies here: the choice belongs to the client that prompts, and a person running
  Claude Code and Claude Desktop side by side may want different answers for each.
- **Only document the Claude Code `--header` route and leave Claude Desktop truthful.** Rejected:
  Claude Desktop is the local-mode client most people use, and it is the one whose prompts
  duplicate PrivacyFence's card.
- **Ship the second extension as a separate download.** Rejected: it would reopen the "one macOS
  download" rule `CLAUDE.md` gives for the DMG, and the `.pkg`'s "next to this installer" sentence
  would again be untrue for one of the two.

## Consequences

- A release carries one more file inside the DMG and the Windows installer, and no new top-level
  artifact. `scripts/build_dmg.sh` passes both in-DMG names to `scripts/build_pkg.sh`.
- The two manifests have different `name`s, so Claude Desktop treats them as two extensions; a user
  who switches removes one and installs the other.
- A user of the no-prompts extension overrides, on their own computer, an organization bundle's
  `mcp.tool_annotations` (ADR 0086's precedence). In organization mode neither extension applies.
- The packaged smoke tests (`build.yml`'s `build` and `build-windows` jobs) fail a release whose
  DMG or installer lacks either extension, or whose daemon does not honour the no-prompts one's
  header.

## Verification

- `mcpb/shim/test/index.test.ts`: `parseArgs` and `daemonHeaders`, and a real `main()` run against
  a fake `/mcp` that asserts the header on every request with the flag and on none without it.
- `tests/unit/test_mcpb_manifest.py`: the two manifests differ in exactly the four decided fields,
  and the flag's value is one the daemon accepts.
- `tests/integration/test_shim_mcp_contract.py`'s
  `test_each_extensions_manifest_args_get_the_annotations_it_promises`: the real shim, started with
  each manifest's own arguments, against the real Python `/mcp`.
- `tests/integration/test_macos_packaged_smoke.py` and `tests/integration/test_windows_packaged_smoke.py`,
  through `tests/packaged_extensions.py`: both files ship, and the no-prompts extension's header
  gets a read-only `tools/list` from the real packaged daemon.

## Related

- [ADR 0086](0086-tool-annotations-are-truthful-by-default.md): the header and its precedence.
- [ADR 0076](0076-every-connector-tool-is-advertised-read-only.md): the uniform triple the second
  extension asks for.
- [ADR 0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md): why the shim knows no tool
  schemas.
- [Issue 46](https://github.com/privacyfence/privacyfence/issues/46).
