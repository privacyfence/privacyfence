# ADR 0054: The Windows installer refuses anything but a native x64 Windows

## Status

Accepted — 2026-09-25. Implemented in `installer/privacyfence.iss` (`ArchitecturesAllowed=x64os`).

Extends [ADR 0039](0039-installers-refuse-an-os-below-the-support-matrix.md) to the Windows
installer's CPU architecture, the way that ADR already covers the `.pkg`'s.

## Context

The support matrix (`docs/platform-support.md`) and `docs/install-windows.md` promise Windows on
x64 only, and `build.yml` builds and tests only an x64 installer. `installer/privacyfence.iss` set
`ArchitecturesInstallIn64BitMode=x64compatible` and no `ArchitecturesAllowed`. The first only picks
64-bit or 32-bit install mode; with the second blank, Inno Setup runs on every architecture:

- Windows 11 on arm64 counts as x64-compatible, so Setup installed in 64-bit mode and the x64
  binaries ran under emulation, which nothing tests.
- Windows 10 on arm64 and 32-bit Windows are not x64-compatible, so Setup installed in **32-bit
  mode**: `{autopf}` became `Program Files (x86)`, the x64 service could not start, and the fix-it
  text `privilege_separation.py` prints (which quotes `$env:ProgramFiles\PrivacyFence\…`) named
  the wrong directory.

Filed as privacyfence/privacyfence#718.

## Decision

1. `ArchitecturesAllowed=x64os`: Setup runs only on a native x64 Windows. It refuses 32-bit
   Windows and every arm64 Windows, Windows 11 on arm64 included.
2. `ArchitecturesInstallIn64BitMode=x64os` to match, so the two directives name one architecture.
3. arm64 Windows becomes supported the way ADR 0044 describes for the `.deb`: as one change that
   adds a build and packaged-test leg for it, the installer directive and the matrix row together.

## Alternatives considered

- **`ArchitecturesAllowed=x64compatible`.** Refuses 32-bit Windows and Windows 10 on arm64, but
  still accepts Windows 11 on arm64 under emulation. Rejected: the matrix says x64, and an emulated
  service with a kernel-enforced ACL layout is exactly the kind of untested combination ADR 0039
  exists to refuse up front rather than let fail at runtime.
- **Leave it to the docs.** Rejected: the installer is the only place the promise can be enforced,
  and the failure on 32-bit mode is silent until the service will not start.

## Consequences

- Windows 11 on arm64 users get a refusal from Setup instead of an emulated install. They were
  never supported; now they are told so before anything is installed.
- The installer's `x64os` requires Inno Setup 6.3 or later; `build.yml` installs the current one.

## Verification

- `tests/unit/test_minimum_os_versions.py`'s `test_windows_installer_floor_matches_matrix` asserts
  both directives alongside `MinVersion` and the matrix row.

## Related

- privacyfence/privacyfence#718.
- [ADR 0039](0039-installers-refuse-an-os-below-the-support-matrix.md),
  [ADR 0044](0044-the-deb-declares-only-the-architectures-ci-builds.md).
