# ADR 0044: The `.deb` declares only the architectures CI builds and tests

## Status

Accepted — 2026-09-24. Implemented in `debian/control` (`Architecture: amd64`) and
`scripts/build_deb.sh`'s control-file renderer.

Amends [ADR 0018](0018-linux-ships-a-self-contained-deb-built-with-pyinstaller.md): its
Consequences describe `debian/control` listing `amd64 arm64`; it now lists `amd64` only.

## Context

`debian/control` declared `Architecture: amd64 arm64`, but `scripts/build_deb.sh` builds only for
the host it runs on (PyInstaller does not cross-compile), `build.yml`'s `build-deb` job has only an
amd64 leg, and `docs/platform-support.md`'s support matrix promised no arm64 `.deb`. The package
metadata claimed an architecture nothing produced or tested. Found by P5 (#665) and filed as
privacyfence/privacyfence#679.

## Decision

1. `debian/control` declares exactly the architectures a `build-deb` leg builds and runs the
   packaged lifecycle test for. Today that is `amd64`.
2. The support matrix's Debian/Ubuntu row names the same architectures, and a unit test keeps the
   two equal.
3. `scripts/build_deb.sh` refuses to package on a host whose architecture `debian/control` does
   not declare, so an arm64 host cannot produce an untested arm64 `.deb` by accident.
4. arm64 comes back as one change: an arm64 `build-deb` leg with its packaged lifecycle test, the
   `Architecture:` line, and the matrix row.

## Alternatives considered

- **Add an arm64 build leg now.** Rejected for this change: nobody has asked for an arm64 `.deb`,
  and it costs an arm64 runner plus a second packaged lifecycle run on every release. Decision 4
  keeps the door open.
- **Keep `amd64 arm64` as an aspiration.** Rejected: the field is what `apt` and the build script
  act on, not documentation, and nothing would stop the two from drifting.

## Consequences

- The package metadata, the support matrix and CI agree. An arm64 `.deb` is not shipped and
  cannot be built by `scripts/build_deb.sh` without editing `debian/control` first.

## Verification

- `tests/unit/test_minimum_os_versions.py`'s `test_deb_architectures_match_matrix` and
  `test_deb_build_refuses_an_undeclared_host_architecture`.

## Related

- privacyfence/privacyfence#679, privacyfence/privacyfence#665.
- [ADR 0018](0018-linux-ships-a-self-contained-deb-built-with-pyinstaller.md),
  [ADR 0039](0039-installers-refuse-an-os-below-the-support-matrix.md).
