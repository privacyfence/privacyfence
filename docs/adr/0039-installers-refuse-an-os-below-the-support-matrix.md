# ADR 0039: every installer refuses an OS below the support matrix's floor

## Status

Accepted — 2026-09-24.
Amends [0018](0018-linux-ships-a-self-contained-deb-built-with-pyinstaller.md) (the `.deb` now declares dependency floors).

## Context

The three installers built by `build.yml` declared no minimum OS. The macOS app bundle carried
`LSMinimumSystemVersion` 13.0, but the `.pkg` that installs it had no check of its own, so an older
Mac got a completed install of an app Launch Services then refused to open. The Windows installer
ran on anything Inno Setup runs on. The `.deb` declared no dependencies at all (ADR 0018), although
the PyInstaller bundle inside it is linked against the build runner's glibc — a floor nobody chose,
which rises whenever `ubuntu-latest` moves to a newer image.

## Decision

1. `docs/platform-support.md`'s support matrix has a **Minimum OS** column, and each installer
   refuses a system below its row, in its own native mechanism:
   - macOS `.pkg`: `allowed-os-versions` in the distribution XML, read back from the built app's
     `LSMinimumSystemVersion` (`scripts/build_pkg.sh`) rather than restated.
     It also sets `hostArchitectures="arm64"`: the app is built for Apple silicon only (PyInstaller
     builds for the runner's architecture), so the `.pkg` refuses an Intel Mac, and
     `scripts/build_pkg.sh` refuses to package a bundle built for any other architecture.
   - Windows: `MinVersion=10.0` in `installer/privacyfence.iss` (Windows 10 / Server 2016).
   - `.deb`: dependency versions, not a distribution-name check — `libc6 (>= X)` for the glibc the
     bundle is linked against and `systemd (>= 242)` for the unit's newest directive.
2. The `.deb`'s glibc floor is **measured, not assumed**: `scripts/check_deb_glibc_floor.py` runs
   in `scripts/build_deb.sh` on every build, reads every bundled ELF file's `GLIBC_` symbol versions
   and fails the build if any is newer than the declared floor. A runner-image bump that raises the
   real floor therefore breaks the build instead of shipping a package that installs and won't start.

## Alternatives considered

- **Check `/etc/os-release` in `preinst`** — refuses by distribution name, so it rejects derivatives
  that have the right libraries and accepts nothing dpkg/apt can reason about; dependency versions
  are what apt already resolves and reports.
- **Let `dh_shlibdeps` compute `${shlibs:Depends}`** — ADR 0018 / `debian/rules` skip it on purpose:
  it would emit bounds for every library PyInstaller bundles, tied to the build host's package
  versions. Only glibc is really taken from the system.
- **Pin the build runner to an older Ubuntu to lower the floor** — possible later, and the check
  above is what would prove it worked; out of scope for declaring the floor we ship today.

## Consequences

- The `.deb` no longer installs on distributions whose glibc is older than the build runner's
  (e.g. Debian 12, Ubuntu 22.04). That was already true at runtime; now it is refused up front and
  written down.
- ADR 0018's "`Depends: ${misc:Depends}` only" no longer describes `debian/control`; its decision —
  no `python3-*` dependencies, a self-contained bundle — is unchanged.
- Raising any floor is a deliberate edit to the installer declaration and the matrix together.

## Verification

- `tests/unit/test_minimum_os_versions.py` reads `PrivacyFenceApp.spec`, `scripts/build_pkg.sh`,
  `installer/privacyfence.iss`, `debian/control` and the matrix and fails if they disagree.
- `tests/integration/test_macos_pkg_smoke.py` checks the built `.pkg`'s `allowed-os-versions`
  against its own payload's `LSMinimumSystemVersion`, and its `hostArchitectures` against the
  payload's executables (`build.yml`'s `build` job).
- `scripts/check_deb_glibc_floor.py` gates `build.yml`'s `build-deb` job.

## Related

ADR 0018 (the self-contained `.deb`), ADR 0003 (the separated install the `.deb`'s systemd unit
provides).
