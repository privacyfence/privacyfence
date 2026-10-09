# ADR 0141: Plugin tools take files by reference, and PrivacyFence passes the bytes

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/plugins/files.py`,
`src/privacyfence/plugins/connector.py`, `src/privacyfence/plugins/tools.py`,
`src/privacyfence/plugins/protocol.py`, `src/privacyfence/plugins/constants.py`,
`src/privacyfence/local_files.py` (`resolved_name`), `src/privacyfence/upload_staging.py`,
`plugin-sdk/src/privacyfence_plugin_sdk/_files.py`, `docs/plugin-protocol/protocol.schema.json`.
Amends [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md).

## Context

A plugin tool could take a file only as a string the AI wrote out, a base64 or HTML body passed
through the model's output: slow, costly, and impossible past a few hundred kilobytes. A plugin
that publishes AI-built dashboards needs the AI to point at a file and PrivacyFence to deliver it.
The delivery has to keep ADR 0122's rule that a human sees what a call does before it happens, and
the file bridge's rules ([ADR 0007](0007-local-file-bridge.md),
[ADR 0028](0028-clients-without-the-shim-get-capability-urls.md)): a file is read only where the
user's account can read it, and a capability slot is single-use.

## Decision

Plugin protocol 1.3 adds file parameters.

- **Declaration.** A tool-definition property whose schema carries `x-privacyfence-file`
  (`max_bytes`, `media_types`) is a file parameter. A tool has at most one, it is not read-only and it
  is not on the `auto` gate. The daemon and the SDK apply the same seven rules with the same messages.
- **Reference.** The AI passes a local path or `upload:<upload_id>` from
  `privacyfence_create_upload_slot`. The daemon reads it through `local_files` as every other
  connector does, up to the parameter's `max_bytes` (at most 8 MiB), and the parameter's description
  tells the AI how.
- **Description.** The daemon works out the declared type (from the name's extension, a fixed map) and
  the detected type (from the bytes), refuses a detected type the parameter does not accept, hashes the
  bytes, and shows a block on the approval card with the file's name, source, size, both types and
  SHA-256.
- **Delivery.** `tool.prepare` carries the metadata only. `tool.execute` carries the bytes
  (`content_base64`), after the card or a saved rule accepted the call. The file parameter is removed from
  `args`, so the plugin never sees the path or the upload token, and a plugin refuses an execute whose
  files' SHA-256 differ from the prepared ones.
- **Slots and audit.** The slot is used up after the gate and before the plugin runs
  ([ADR 0102](0102-an-upload-slot-is-consumed-after-the-gate-not-before.md)). One `plugin_file` audit
  entry per file records name, size, SHA-256 and detected type, never the content, and only after the gate
  passed.
- **No held bytes.** The daemon keeps only the SHA-256 across calls. A file whose bytes changed between
  two calls gets a fresh prepare and its own card, never the earlier approval.
- The SDK adds `file_param`, `ctx.files` and `PluginTestHost.call_tool(files=…)`, and leaves a tool with a
  file parameter out of the list it offers a daemon older than 1.3.

## Alternatives considered

- **Base64 in a string argument (as before).** Every byte passes through the AI's output.
- **A read-only path for the plugin.** The plugin would hold a path into a daemon-written file, and every
  plugin would need a cleanup rule for it. Inline bytes fit the 16 MiB line at 8 MiB per file and need no
  new storage.
- **Bytes in `tool.prepare` too.** The plugin could check the file before the card. But
  [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)
  trusts a plugin with what its account can read, and on a privilege-separated install the user's own
  files are not that: sending the bytes before the card would disclose a file the owner has not approved.
- **File parameters on read-only or `auto` tools.** A read could return a local file's content to the AI
  through its payload, and an `auto` tool would hand a file to a plugin with no card. Both refused.
- **A new slot kind.** The existing slots are already single-use, principal-bound, expiring and consumed
  after the gate; the per-tool limit is checked when the slot is read.
- **The file's SHA-256 in the reviewed tool signature.** Parameters were never in the signature, and
  changing its persisted shape would disable every enabled plugin. The per-call card gates every file.
- **More than one file per tool.** Two 8 MiB files would not fit one 16 MiB line. A later minor version may
  raise the limit with a total cap.

## Consequences

- One file per tool call, at most 8 MiB.
- Every file passes a card or a saved rule; no file reaches a plugin from a read or an `auto` tool.
- No bytes are held across calls, and a re-issued call reads the file again.
- The plugin never sees a path or a token, and what it stores is its own data, deleted with the plugin's
  data.
- A plugin written for 1.0 to 1.2 is unchanged: it declares no file parameter, so it is never sent `files`.
- The card puts the daemon's file block under a heading saying PrivacyFence read and checked it, and the
  plugin's preview under a heading saying it comes from the plugin; a preview or payload `fields` row
  labelled like one of the daemon's six rows (case and surrounding spaces ignored) fails the prepare as an
  invalid preview, so no plugin row can pass for a checked one.
- Plugins run in local mode only, so the file is read under the local-mode rules; a future organization-mode
  plugin host must revisit that.

## Verification

- `tests/unit/plugins/test_tools.py`, `test_protocol.py`, `test_constants.py` (the rules, the types, the
  limits) and `docs/plugin-protocol/protocol.schema.json` with `scripts/gen_plugin_sdk_types.py --check`.
- `tests/unit/plugins/test_files.py` (type detection, references, the card block) and `test_connector.py`
  (the wire shapes, the audit entry, the changed-bytes key, the slot consumed after the gate).
- `tests/unit/test_local_files.py` and `tests/unit/test_upload_staging.py` (`resolved_name`,
  `declared_path`).
- `tests/unit/plugin_sdk/test_sdk_files.py`, `test_sdk_files_parity.py` and `test_testhost.py`.
- `tests/integration/test_plugin_files.py` and `test_sdk_testhost_conformance.py`, end to end through
  MCP with the echo fixture plugin.

## Related

- [Issue 846](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0007](0007-local-file-bridge.md)
- [ADR 0028](0028-clients-without-the-shim-get-capability-urls.md)
- [ADR 0102](0102-an-upload-slot-is-consumed-after-the-gate-not-before.md)
- [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)
- [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md)
- [ADR 0126](0126-the-plugin-sdk-lives-in-this-repository-and-is-published-from-the-same-tag.md)
