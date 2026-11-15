# ADR 0303: The MCP registry listing of the extension is no longer published

## Status

Accepted — 2026-11-14. Supersedes [ADR 0112](0112-stable-releases-are-listed-on-the-mcp-registry-with-the-mcpb.md). Implemented.

## Context

[ADR 0112](0112-stable-releases-are-listed-on-the-mcp-registry-with-the-mcpb.md) listed stable releases on the MCP registry with the `.mcpb` extension. The extension is removed, so a new listing would point at a file that is not built.

## Decision

- The workflow that published the registry entry, its server-description script and test, and the template are removed.
- The entry that is already published is left as it is; changing it is a manual step on the registry.

## Alternatives considered

- **Keeping the entry updated with a Docker or remote listing.** Rejected: that is a different listing and a separate decision.
- **Deleting the published entry.** Rejected: a registry entry for a released version is history, and the registry decides its own lifecycle.

## Consequences

- The registry shows the last published version.
- The Glama listing is separate and is covered by the decision on the organization image.

## Verification

No test covers this decision: its subject is a deleted workflow file.

## Related

ADR 0112, 0114, 0296.
