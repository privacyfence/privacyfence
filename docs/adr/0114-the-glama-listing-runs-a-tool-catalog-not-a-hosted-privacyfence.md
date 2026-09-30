# ADR 0114: The Glama listing runs a credential-free tool catalog, not a hosted PrivacyFence

## Status

Accepted — 2026-09-30. Implemented: `src/privacyfence/catalog_server.py`,
`src/privacyfence/connector_catalog.py`.

## Context

Glama grades a listing "A" only after it can build the server, start it and introspect it
(`initialize`, `tools/list`) with no credentials, and the awesome-mcp-servers list asks for that
grade. PrivacyFence has no stdio server: the only MCP transport is Streamable HTTP at `/mcp` inside
the daemon ([ADR 0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md) covers the stdio route, a shim
that relays to a running daemon). Local mode listens only on localhost, requires a per-user token,
and approves only from a companion-attested session ([ADR 0062](0062-only-a-companion-attested-session-may-approve.md)).

[`docs/security-and-compliance.md`](../security-and-compliance.md) says there is no
PrivacyFence-operated infrastructure and no hosted service in the data path. Glama's hosted servers
put Glama's gateway, which logs payloads, and Glama-held secrets in that path.

## Decision

The catalog server lists every tool from the live definitions (each connector's `tool_specs()` plus
the `privacyfence_*` meta-tools) and refuses every call with a fixed message. It holds no
credentials and does no I/O beyond stdio. Glama installs it from PyPI with the build spec in
[`docs/packaging.md`](../packaging.md).

PrivacyFence is not offered, documented or advertised as a server hosted by Glama or any other
third party.

## Alternatives considered

- **Run the daemon plus the `mcpb` shim in Glama's container.** Rejected. It needs Node, a
  background daemon and a token handoff. It would present a working-looking gateway with Glama as
  operator in the data path, which contradicts `docs/security-and-compliance.md`. And local mode
  approves only from a companion-attested session (ADR 0062), so nothing could ever be approved
  there.
- **List only the eight meta-tools**, which is what an unconnected install shows. Rejected by the
  maintainer: the listing would not show what PrivacyFence does.
- **A committed `Dockerfile`, or build fields in `glama.json`.** Glama builds from its admin build
  spec and reportedly ignores a repo Dockerfile. The only confirmed `glama.json` field is
  `maintainers`, and an unknown field could invalidate the file. `glama.json` stays unchanged.
- **Install from the repo checkout.** Rejected by the maintainer in favour of installing the latest
  stable release from PyPI, so the listing runs what users install.

## Consequences

- Even if someone deploys the listing on Glama, the instance holds nothing and reaches nothing.
- The grade appears only after the next stable release that carries this module.
- The build spec lives in Glama's admin page, so the page and the doc can drift;
  `docs/packaging.md` is the reference.
- `uv` and `mcp-proxy` are Glama's environment, not guaranteed by this repo.

## Verification

`tests/unit/test_catalog_server.py`, `tests/unit/test_connector_catalog.py`, and the "The Glama
listing" section of `docs/packaging.md`.

## Related

- [ADR 0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md)
- [ADR 0013](0013-no-mcp-tool-mints-a-sign-in-credential.md)
- [ADR 0062](0062-only-a-companion-attested-session-may-approve.md)
- [ADR 0089](0089-tool-annotations-are-always-truthful.md)
- [ADR 0112](0112-stable-releases-are-listed-on-the-mcp-registry-with-the-mcpb.md)
