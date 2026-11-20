# ADR 0336: The n8n node is versioned and its arguments field is `toolArguments`

## Status

Accepted — 2026-11-20. Implemented: `n8n/nodes/PrivacyFence/PrivacyFence.node.ts`, `n8n/test/node.test.mts`.

## Context

The PrivacyFence n8n node stores the tool's arguments in a JSON parameter named `arguments`. The n8n MCP
rejects any parameter path with a segment `arguments` ("path '/arguments' is invalid or contains unsafe
segments") and silently drops the key from `updateNodeParameters`, so an agent can never set the field. It
saves a parameter named `toolArguments` without trouble ([#1175](https://github.com/privacyfence/privacyfence/issues/1175)).

## Decision

1. **The node has two versions.** `description.version` is `[1, 2]` and `defaultVersion` is `2`. The node's
   `name`, `defaults` and credential do not change.
2. **Version 1 keeps `arguments`.** The property is shown only for `@version` 1, so a saved workflow keeps its
   arguments and runs as before.
3. **Version 2 uses `toolArguments`.** It is a separate JSON property, labelled **Arguments**, shown for
   `@version` 2 and later. `execute` reads `toolArguments` when `typeVersion >= 2` and `arguments` otherwise;
   parsing, the `BAD_ARGUMENTS` message and the merge of `reason` are shared.

## Alternatives considered

- **Rename `arguments` in place.** A saved workflow would lose its arguments. Rejected.
- **A hidden second property with no version.** Both boxes would show in the editor. Rejected.

## Consequences

- New nodes, whether added in the editor or by an agent, are version 2; nothing in an existing workflow changes.
- Two properties stay in the node until version 1 is retired.

## Related

[#1175](https://github.com/privacyfence/privacyfence/issues/1175).
