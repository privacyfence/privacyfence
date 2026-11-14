# ADR 0301: Releases build the org image, privacyfence-sync and the SBOM only

## Status

Accepted — 2026-11-14. Supersedes [ADR 0057](0057-stable-tags-are-gated-on-graphical-session-coverage.md); amends [ADR 0087](0087-two-claude-desktop-extensions-ship-in-the-dmg-and-the-windows-installer.md).

## Context

[ADR 0057](0057-stable-tags-are-gated-on-graphical-session-coverage.md) gated stable tags on graphical-session coverage of the installers, and [ADR 0087](0087-two-claude-desktop-extensions-ship-in-the-dmg-and-the-windows-installer.md) put two Claude Desktop extensions in the DMG and the Windows installer. With no installers there is nothing to gate or bundle.

## Decision

- The build workflow keeps `dedupe`, `build-org-image`, `sbom` and `finalize-release`, and drops the macOS, Windows and Debian builds. The organization release smoke test moves into `build-org-image`.
- `finalize-release` depends on the SBOM and the image. It drops the graphical-coverage gate, the installer assets, the release manifest, the promotion to `latest` and the download-route smoke test, and keeps the sync plugin zip, the connector-config page, the pre-release notes and, last, the move of the `stable` branch.
- R2 keeps uploads, the channel and tag checks and the immutability guard, without a manifest or `latest`. The release-script functions for installers, the manifest and promotion are deleted.
- Scripts that were uploaded with the installers (the room directory sync, the connector-config signer) move to `finalize-release`; the desktop-only bundle builder is deleted.

## Alternatives considered

- **Keeping installer builds without publishing them.** Rejected: they would still need maintenance and platform runners.
- **Keeping the manifest and `latest` for the remaining artifacts.** Rejected: only the download page read them.

## Consequences

- A release is smaller and faster; the stable gate does not need a graphical session.
- Stable tags are gated on the checks that remain.

## Verification

`tests/unit/test_release_assets.py` and `tests/unit/test_r2_release.py`.

## Related

ADR 0057, 0087, 0169, 0296, 0300, 0302, 0304.
