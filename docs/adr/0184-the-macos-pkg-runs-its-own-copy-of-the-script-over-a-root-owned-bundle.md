# ADR 0184: the macOS pkg runs its own copy of the privilege-separation script, and only over a root-owned app bundle

## Status

Accepted — 2026-10-15. Implemented.

## Context

The `.pkg` postinstall ran as root and executed
`/Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh`. The
bundle is copied into `/Applications`, where it could be owned by, or have been replaced by, a
non-root account before the postinstall ran; root would then run that account's script and stage
its files as the daemon image.

## Decision

1. **The pkg carries the script and the two LaunchDaemon/LaunchAgent templates in its Scripts
   directory**, and the postinstall runs that copy. Installer unpacks the Scripts directory to a
   root-only temporary location, so the script's own `REPO_ROOT` finds the templates there.
2. **The postinstall skips set-up unless every file in the installed bundle is owned by root.** It
   logs why and still exits 0, so the package install itself is not failed. The check uses
   `/usr/bin/find` by a fixed path.
3. The commands the postinstall prints for a person to run still name the in-bundle script path.

## Alternatives considered

- **A `codesign` team-identifier check on the bundle.** Rejected: pre-release and dispatched
  builds may be unsigned and would then fail to install.
- **Keep running the in-bundle script after the ownership check alone.** Rejected: the script
  would still be read from a location a non-root account may have changed after the check.

## Consequences

- The script that runs as root during an install is the one the package was built with.
- Accepted risk: `/Applications` is writable by administrators, so an administrator account could
  still swap the bundle between the ownership check and the `ditto` that stages the image. An
  administrator can already run anything as root, so this adds no new reach.
- A bundle someone has changed ownership of installs without the background service, with a log
  line saying so.

## Verification

- `installer/macos/pkg/postinstall` (`bundle_is_root_owned`), `scripts/build_pkg.sh`.
- `scripts/macos_privilege_separation.sh` (`stage_trusted_image`).

## Related

- [ADR 0058](0058-nothing-runs-elevated-unless-only-an-administrator-can-rewrite-it.md)
- [ADR 0003](0003-separated-installs-only.md)
