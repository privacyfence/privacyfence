# ADR 0130: Plugin outputs are a folder that PrivacyFence reads through its own tools

## Status

Accepted — 2026-10-08. Implemented: `src/privacyfence/plugins/outputs.py`,
`src/privacyfence/plugins/host.py`, `src/privacyfence/plugins/storage.py`,
`src/privacyfence/auto_accept.py` (`register_internal_dynamic_tools`),
`src/privacyfence/policy/scopes.py` (`register_plugin_output_selector`),
`src/privacyfence/policy/propose.py`.

## Context

A plugin's results can be far larger than the 100 KB a prepared payload may carry
([ADR 0092](0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)), and an AI
client has no other way to read a file a plugin made: plugin folders are private to the service
account. The result still has to reach the AI only after a human, or a rule the human wrote, allows
it, and a rule must be able to cover "the reports folder of this plugin" and nothing wider.
Requested in [the design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618).

## Decision

**A folder, not a channel.** A plugin whose manifest sets `outputs: true` receives a per-principal
output directory (`PrincipalContext.output_dir`), created `0700` before `initialize`, and writes files
there. A file is published when it is a regular file (never a symlink) at most eight segments deep,
with no dot-prefixed segment, and an extension of a type the manifest allows (JSON and CSV by
default; text, Markdown and HTML on request). The SDK writes `.name.tmp` and renames, so a half-written
file is never published.

**Two PrivacyFence tools read it.** `plugin_outputs_list` is on the `auto` gate: it shows paths,
sizes and times, never content. `plugin_outputs_read` is on the `review` gate, with a PII scan, and
returns about 90 KB per call with an offset to continue; the file is read once before the card, and
the released text is those bytes. Each read is also audited against the plugin's own output source.
The tools exist while any enabled plugin has `outputs: true`.

**Paths are canonical.** A requested path is accepted only when it contains no backslash, colon or
empty segment, no segment starts with a dot, and resolves, with no symlink on the way, to a regular
file whose relative path equals the request exactly. The canonical path is what a rule is checked
against, so no spelling reaches a folder rule it does not belong to.

**The scope is a prefix.** The rule scope `plugin:<name>:output` takes values that end in `/`, which
cover that folder and everything under it, or a path, which is one exact file. An empty value
matches nothing. "Always allow" proposes the file's folder (or the file, at the root). `output` is a
reserved scope type: a plugin tool cannot declare it.

Large results reach the AI through outputs, and the 100 KB inline limit stays.

## Alternatives considered

- **Raise the inline limit.** Rejected. It is the cap on what one tool result may put in the AI's
  context (ADR 0092), and a larger one is a worse default for everything else.
- **Let the plugin return a file path or URL.** Rejected. The AI client cannot read the service
  account's files, and a URL would be a read with no gate.
- **Match scope values as a set, like other plugin scopes.** Rejected. A folder rule has to cover
  files that do not exist yet.

## Consequences

- A plugin's output is only as private as its folder rules: a rule on `reports/` releases every
  file under it without a card.
- A denied read leaves no output-source attribution entry, since that entry is written after the
  gate returns; the gate's own audit entry records the denial.
- Removing a plugin removes its outputs with the rest of its data and its rules.

## Verification

`tests/unit/plugins/test_outputs.py` (listing, paths, reading), `tests/integration/test_plugin_outputs.py`
(a real plugin publishes, the AI lists and reads, a folder rule applies), the policy tests for the
scope, and `tests/unit/plugin_sdk/test_plugin.py` (`ctx.outputs`).

## Related

- [The design comment](https://github.com/privacyfence/privacyfence/issues/846#issuecomment-6039094618)
- [ADR 0092](0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)
- [ADR 0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md)
- [ADR 0122](0122-plugin-tools-are-gated-in-two-steps-and-a-read-releases-the-prepared-payload.md)
