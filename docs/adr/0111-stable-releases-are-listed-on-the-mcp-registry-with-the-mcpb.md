# ADR 0111: Stable releases are listed on the official MCP registry, with the `.mcpb` attached to the GitHub Release

## Status

Accepted — 2026-09-29. Implemented: `.github/workflows/publish-mcp-registry.yml`,
`mcpb/server.json.tmpl`, `scripts/mcp_registry_server_json.py`, and the `.mcpb` upload in
`.github/workflows/build.yml`.

## Context

The official MCP registry (`registry.modelcontextprotocol.io`) is where MCP directories and
clients look servers up, so one entry there reaches many of them. PrivacyFence's local users
connect Claude Desktop through `PrivacyFence.mcpb`
([ADR 0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md)), which makes it the package
to list.

The registry accepts an MCPB package only as a GitHub or GitLab release asset: the `identifier`
must be a URL on `github.com` or `gitlab.com` (`internal/validators/registries/mcpb.go` in
`modelcontextprotocol/registry`), and it must carry a `fileSha256` that clients check before
installing. Until now the `.mcpb` shipped only inside the DMG and the Windows installer, and was
never uploaded or attached on its own
([ADR 0087](0087-two-claude-desktop-extensions-ship-in-the-dmg-and-the-windows-installer.md),
decision 3, carried into `CLAUDE.md` and `scripts/build_dmg.sh`). So v5.1.0's release has no
`.mcpb`, and because this repository's GitHub Releases are immutable once published, it can never
get one.

## Decision

1. **A stable GitHub Release also carries `PrivacyFence.mcpb` on its own.** `build.yml`'s `build`
   job uploads the same `.mcpb` it packs into the DMG, under the same unversioned name, and
   `finalize-release` attaches it with the rest of the set. Pre-release entries still carry no
   files, and R2 and the download site are unchanged: they keep offering the DMG.
2. **Every stable release is published to the registry automatically**, as
   `io.github.privacyfence/privacyfence`, by `publish-mcp-registry.yml`. It is triggered by the tag
   push and gated on `build.yml` succeeding for that commit, exactly like `publish-pypi.yml`.
3. **The committed file is a template** (`mcpb/server.json.tmpl`), filled per release, because no
   version string lives in the source tree.
4. **The hash is taken from the public release URL**, after the release exists, not from the
   build job's copy: it has to be the hash of the bytes a client will download.
5. **Authentication is GitHub OIDC** (`mcp-publisher login github-oidc`). The registry grants the
   `io.github.<repository_owner>/*` namespace to a token minted for this repository's workflow.

## Alternatives considered

- **Publish by hand from a maintainer's terminal** (`mcp-publisher login github`). Rejected: a
  step every release has to remember, and it needs the maintainer's organization membership to be
  public for the `io.github.privacyfence/` namespace. OIDC needs neither.
- **List the `.mcpb` from the download site.** Not possible: the registry allows only GitHub and
  GitLab release hosts.
- **Commit a filled-in `server.json` and bump it per release.** Rejected: it is the hand-bumped
  version file `setuptools_scm` replaced, with the same failure mode (CLAUDE.md, "Releasing").
- **A job in `build.yml`.** Rejected: `publish-pypi.yml` waits for `build.yml` to succeed, so a
  registry outage would hold PyPI back for a listing nothing else depends on.
- **Backfill v5.1.0.** Not possible: its release is immutable and has no `.mcpb`.

## Consequences

- The first release that can be listed is the first stable tag cut after this lands.
- The `.mcpb` alone does nothing without the PrivacyFence app: the shim connects to the local
  daemon. Someone who installs it from a registry client without the app gets an extension that
  cannot connect. So the registry description starts with "Needs the PrivacyFence app", its
  `websiteUrl` is the download page, the extension's own long description says the same, and
  the shim's "daemon did not start" error names the download page.
- The listing's version is immutable on the registry: a re-run for a version already listed does
  nothing, and a wrong entry is fixed by the next release (or by `mcp-publisher status` to
  deprecate it), not by republishing.
- `mcp-publisher` is pinned by version and SHA-256 in the workflow and has to be bumped by hand.

## Related

- [ADR 0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md),
  [ADR 0087](0087-two-claude-desktop-extensions-ship-in-the-dmg-and-the-windows-installer.md),
  [ADR 0089](0089-tool-annotations-are-always-truthful.md).
- [ADR 0021](0021-release-tag-push-never-uses-github-token.md): the tag push is what starts this
  workflow too.
