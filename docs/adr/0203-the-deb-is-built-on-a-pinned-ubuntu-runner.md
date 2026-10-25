# ADR 0203: The `.deb` is built on a pinned Ubuntu runner

## Status

Accepted — 2026-10-19. Implemented: `.github/workflows/build.yml` (`build-deb`). Acts on the
"pin the build runner" alternative [ADR 0039](0039-installers-refuse-an-os-below-the-support-matrix.md)
left for later.

## Context

The `.deb` ships a PyInstaller bundle linked against the build host's glibc, so the runner image
sets the package's real glibc floor. ADR 0039 makes that floor measured: `scripts/check_deb_glibc_floor.py`
fails the build when a bundled binary needs a newer glibc than `debian/control` declares
(`libc6 (>= 2.38)`).

`build-deb` ran on `ubuntu-latest`. When that label moved to an image with glibc 2.42, the
`build.yml` pre-flight for 6.5.0 failed at "Build .deb" (`libtinfo.so.6` references `GLIBC_2.42`),
with no change in the repository. Following the check's advice and raising the floor to 2.42 would
make the package uninstallable on Ubuntu 24.04, which the support matrix lists.

## Decision

`build-deb` runs on `ubuntu-24.04`, the oldest supported distribution, so the floor stays at the
declared 2.38 and the support matrix is unchanged. The glibc floor check stays in place and is
what proves the pin still holds. Raising the floor remains a deliberate edit to `debian/control`
and `docs/platform-support.md` together.

## Consequences

- A runner label change no longer alters the `.deb`'s floor or fails a release pre-flight.
- `ubuntu-24.04` is retired by GitHub eventually; moving off it is then a support-matrix decision,
  surfaced by the same check.
- Other jobs stay on `ubuntu-latest`; they do not build the shipped package.
