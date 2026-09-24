# ADR 0018: Linux local mode ships a self-contained `.deb` built with PyInstaller

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-09 in
`docs/linux-local-deb-packaging-plan.md` and `docs/windows-linux-support-plan.md`, both deleted in
`be78e7ee`). Implemented in `ebb7c8ee` (2026-09-09). The autostart wiring that plan also decided
has since changed under [ADR 0003](0003-separated-installs-only.md); see Consequences.
Amended by [ADR 0039](0039-installers-refuse-an-os-below-the-support-matrix.md): `debian/control` now
declares `libc6`/`systemd` floors.

## Context

Local mode needed a desktop install path on Linux equivalent to the macOS DMG. Before this
decision, a Linux desktop user had `pip`/`pipx install privacyfence` and the repo-root
`privacyfence.service` `--user` unit: a developer-tool install with no autostart wiring, no icon
and no uninstall-by-package-manager.

Two facts shaped the choice. First, the runtime dependency list in `pyproject.toml` includes
packages the deleted plan judged "unlikely to exist as Debian archive packages at compatible
versions" (it names `slack-sdk`, `telethon`, `atlassian-python-api`, `simple-salesforce`, `mcp`,
`webauthn`); a package built against system `python3-*` packages would have needed a private APT
repository of every dependency. Second, the repo already built the macOS `.app` with PyInstaller
(`PrivacyFenceApp.spec`) and planned the same for Windows.

The earlier plan compared four formats and itself recommended the unpackaged `pip`/`pipx` path
for v1, with a `.deb` as a stretch goal. The `.deb` was then planned and built two days later.

## Decision

Linux local mode ships as a `.deb` wrapping a PyInstaller onedir bundle:

- The bundle carries its own Python interpreter and every dependency, so the package declares no
  `python3-*` dependencies (`debian/control`: `Depends: ${misc:Depends}` only).
- The app installs under `/opt/privacyfence/`, with thin wrappers on `PATH` in `/usr/bin/`; the
  plan cites Debian policy §9.1.2 for `/opt`.
- One build tool across all three platforms: `PrivacyFenceApp.linux.spec` shares its data and
  hidden-import lists with the macOS and Windows specs through `scripts/pyinstaller_common.py`.
- Distribution is a downloaded file installed with `dpkg -i` / `apt install ./…`. There is no
  hosted APT repository.

## Alternatives considered

- **`pip`/`pipx` plus the `--user` systemd unit only** (the earlier plan's own v1 recommendation) —
  kept as the documented path for a bare Python install, but not the desktop product: no autostart
  wiring, no icon, no uninstall by package manager.
- **A "proper" Debian package against system `python3-*` packages** — rejected: not realistic
  without maintaining a private APT repo of every dependency.
- **`dh-virtualenv`** — rejected: it "still needs every dependency to build from source or a wheel
  at package-build time with no obvious win over PyInstaller here", and adds Debian-specific
  tooling to keep working in CI instead of reusing the PyInstaller pipeline proven on macOS.
- **AppImage** — rejected: "a background daemon with autostart-at-login wants to be *installed*,
  not run ad hoc from a downloaded file; AppImage has no native autostart/systemd integration
  story."
- **Flatpak / Snap** — rejected: the heaviest lift, and "its sandbox model fights a background
  daemon that manages its own credentials directory more than it helps."
- **A hosted APT repository** — deferred, not rejected outright: GPG key rotation, hosting and
  freshness were judged "disproportionate to current demand", to be revisited if adoption warrants.

## Consequences

- The package is "a Debian package that happens to install a bundled binary", not one that
  integrates with the system Python. Security fixes in bundled libraries reach users only through
  a new PrivacyFence release, not through the distribution's updates.
- Only Debian/Ubuntu-family distributions are covered; an RPM or any other format would be a
  separate package. [ADR 0003](0003-separated-installs-only.md) (Out of scope) adds that any such
  future format must be able to separate itself.
- PyInstaller does not cross-compile: `debian/control` lists `amd64 arm64`, but CI builds only on
  its `ubuntu-latest` (amd64) runner, and arm64 needs a native arm64 build host.
- No package signing: `dpkg` does not gate on publisher identity the way Gatekeeper or SmartScreen
  do. Signing becomes relevant only if an APT repository is ever stood up.
- The PEP 440 version from `setuptools_scm` is translated at build time into a valid Debian
  version (`a`/`b`/`rc` become `~a`/`~b`/`~rc`), see `scripts/build_deb.sh`.
- The plan also chose an XDG autostart entry, not a systemd `--user` unit, to start the daemon at
  login. Since [ADR 0003](0003-separated-installs-only.md) decision 5, `debian/postinst` separates
  every install: a system systemd unit under a dedicated account runs the daemon, the original
  daemon autostart entry is moved aside, and a separate XDG autostart entry runs the companion
  (`docs/platform-support.md`, Linux section). That change is recorded there, not here.

## Verification

- `PrivacyFenceApp.linux.spec`, `scripts/pyinstaller_common.py`, `scripts/build_deb.sh` (its
  header restates the PyInstaller, `/opt` and autostart choices), `debian/`.
- `debian/control`: no `python3-*` in `Depends:`; the long description says the build is
  self-contained.
- `.github/workflows/build.yml` job `build-deb` on `ubuntu-latest`: runs `scripts/build_deb.sh`,
  gates on `tests/integration/test_deb_packaged_lifecycle.py` (install, remove, purge, upgrade),
  then uploads to R2 and as a workflow artifact for `finalize-release`.
- `docs/platform-support.md`: the platform table's Debian/Ubuntu row and the Linux `.deb` section.

## Related

- `git show be78e7ee^:docs/linux-local-deb-packaging-plan.md`, "Key decision: how the package
  carries its dependencies", Phase 3 (autostart) and P6.1 (no APT repository).
- `git show be78e7ee^:docs/windows-linux-support-plan.md`, "A2. Local mode (desktop)": the
  packaging-format comparison table.
- `ebb7c8ee` (implementation), `abbfcdac` (automated `.deb` lifecycle test).
- [ADR 0003](0003-separated-installs-only.md) decision 5: the `.deb` postinst's separation step.
