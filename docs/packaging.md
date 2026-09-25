# Packaging

How each release artifact is built, signed and installed, and which CI job proves it. For the
support matrix and what a user does to install, see [`platform-support.md`](platform-support.md);
for cutting a release, see [`CLAUDE.md`'s "Releasing"](../CLAUDE.md#releasing); for what
privilege separation protects, see
[`security-and-compliance.md`](security-and-compliance.md#privilege-separation-macos-linux-and-windows).

## What ships

| Platform | Built by | Artifact | Contents |
|---|---|---|---|
| macOS (arm64) | `scripts/build_dmg.sh` | `dist/PrivacyFence-<version>.dmg` | `PrivacyFence.pkg` and `PrivacyFence.mcpb`, nothing else |
| Windows (x64) | `scripts/build_installer.ps1` → `installer/privacyfence.iss` | `dist/PrivacyFence-<version>-setup.exe` | PyInstaller onedir, the `.mcpb`, the separation script and its task template |
| Debian/Ubuntu (amd64) | `scripts/build_deb.sh` | `dist/privacyfence_<debversion>_amd64.deb` | PyInstaller onedir under `/opt/privacyfence`, wrappers, separation tool and templates |
| Any (Python) | `publish-pypi.yml` | sdist + wheel | the `privacyfence` package |

Every build reads its version from the installed package metadata
(`importlib.metadata.version("privacyfence")`, resolved from git tags by `setuptools_scm`), so each
build script needs `pip install -e .` first and a checkout with full tag history. There is no
version string to edit; see [`CLAUDE.md`'s "Releasing"](../CLAUDE.md#releasing).

Shared build inputs:

- **PyInstaller specs**: `PrivacyFenceApp.spec` (macOS), `PrivacyFenceApp.win.spec`,
  `PrivacyFenceApp.linux.spec`, with common pieces in `scripts/pyinstaller_common.py`. Every build
  runs PyInstaller with `--clean`, and builds only for the host architecture.
- **Telegram app credentials** are written into a git-ignored module by
  `scripts/telegram_credentials.py write` from the `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` secrets
  before PyInstaller runs; a local build without them ships without Telegram
  ([ADR 0040](adr/0040-telegram-app-credentials-ship-in-every-distribution.md)).
- **The `.mcpb`** is built by `scripts/build_mcpb.sh`: esbuild bundles `mcpb/shim/` into one
  dependency-free `server/shim.js`, and the manifest template's `__VERSION__` is stamped with the
  resolved version. `mcpb/shim/package.json` stays at `0.0.0-dev`
  ([ADR 0012](adr/0012-mcpb-shim-connects-claude-desktop-to-local-mode.md)).

## Minimum OS and CPU enforcement

Each installer refuses a system below the support matrix instead of installing something that
cannot start ([ADR 0039](adr/0039-installers-refuse-an-os-below-the-support-matrix.md)).
`tests/unit/test_minimum_os_versions.py` keeps all of these in step with the matrix in
`platform-support.md`.

| Installer | Floor | Where it is declared |
|---|---|---|
| `.pkg` | macOS 13.0, arm64 | `build_pkg.sh` reads `LSMinimumSystemVersion` (set in `PrivacyFenceApp.spec`) back from the built bundle into `<allowed-os-versions>`, and sets `hostArchitectures="arm64"`; it refuses to package a bundle `lipo -archs` does not report as `arm64` |
| Inno Setup | Windows 10 / Server 2016, x64 | `MinVersion=10.0`, `ArchitecturesInstallIn64BitMode=x64compatible` in `installer/privacyfence.iss` |
| `.deb` | glibc 2.38, systemd 242, amd64 | `Depends: libc6 (>= 2.38), systemd (>= 242)` and `Architecture: amd64` in `debian/control` |

The `.deb`'s glibc floor is whatever the build host linked the bundled Python against;
`scripts/check_deb_glibc_floor.py` fails `build_deb.sh` if the bundle needs more than `debian/control`
declares. `build_deb.sh` also refuses to package on a host architecture `debian/control` does not
list. The `.deb` declares only what CI builds and tests: adding arm64 means an arm64 `build-deb` leg,
its lifecycle test, `debian/control` and the matrix, together
([ADR 0044](adr/0044-the-deb-declares-only-the-architectures-ci-builds.md)).

## macOS

### Build (`scripts/build_dmg.sh`)

1. Converts `src/privacyfence/resources/icon_512.png` to `.icns`; writes the Telegram credentials.
2. Runs PyInstaller on `PrivacyFenceApp.spec`, producing `dist/PrivacyFenceApp.app` with four
   executables in `Contents/MacOS/`: `PrivacyFence` (the launcher, the bundle's
   `CFBundleExecutable`, which opens Approvals through the companion —
   [ADR 0031](adr/0031-clicking-privacyfence-opens-approvals-through-the-companion.md)),
   `PrivacyFenceApp` (the daemon), `PrivacyFenceCompanion`, and the `privacyfence-app` symlink to
   the daemon that the launchd plists and the shim use.
3. Copies `scripts/macos_privilege_separation.sh` and the two launchd templates into
   `Contents/Resources/scripts/` and `Contents/Resources/installer/macos/`, the same relative
   layout as the checkout, so the script needs no packaged-vs-checkout branch.
4. Signs the `.app` (below), builds the `.mcpb`, then calls `scripts/build_pkg.sh`.
5. Stages the `.pkg` and `.mcpb` (renamed to the unversioned `PrivacyFence.pkg`/`PrivacyFence.mcpb`)
   into `build/dmg-root/` and runs `create-dmg` over it; notarizes and staples the DMG.

`scripts/build_pkg.sh` never runs PyInstaller; it packages the existing `.app`. It marks the bundle
non-relocatable (`BundleIsRelocatable false` in the component plist) so Installer.app always
installs to `/Applications` rather than over a copy it finds elsewhere, wraps the component package
with `productbuild` to add `installer/macos/pkg/resources/welcome.html`/`conclusion.html.tmpl`, and
restricts the install to the system domain (`enable_localSystem` only, `rootVolumeOnly`,
`require-scripts`). The `.pkg` is never released on its own; releasing the DMG releases all three.

### Signing and notarization

| What | Tool | Identity | CI secrets |
|---|---|---|---|
| `.app` | `codesign --deep --force --options runtime --entitlements scripts/entitlements.plist` | "Developer ID Application" (`--sign` / `SIGN_IDENTITY`) | `MACOS_CERTIFICATE`, `MACOS_CERTIFICATE_PWD`, `SIGN_IDENTITY` |
| `.pkg` | `productsign` | "Developer ID Installer" (`SIGN_IDENTITY_INSTALLER`) | `MACOS_INSTALLER_CERTIFICATE`, `MACOS_INSTALLER_CERTIFICATE_PWD`, `SIGN_IDENTITY_INSTALLER` |
| `.pkg`, then `.dmg` | `xcrun notarytool submit --wait` + `xcrun stapler staple` | keychain profile named by `NOTARIZE_PROFILE` | `APPLE_API_KEY_P8`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID` |

The two certificate types are distinct, which is why `build_pkg.sh` reads
`SIGN_IDENTITY_INSTALLER` and never `SIGN_IDENTITY`. Each is optional: without
`SIGN_IDENTITY_INSTALLER` the `.pkg` inside an otherwise signed DMG is unsigned, which is a valid
local build. Notarization runs only when both an identity and `NOTARIZE_PROFILE` are set
(`build.yml` uses the profile name `privacyfence-ci-notary`). The `.pkg` is notarized and stapled on
its own, because stapling the DMG does not staple what it carries.

`codesign` and `productsign` both go through `sign_with_timestamp_retry` in
`scripts/macos_sign_retry.sh`, which retries only when Apple's timestamp server or the network under
it was unreachable. The delays come from `SIGN_RETRY_DELAYS` (default `10 20 40`, so at most four
attempts); any other signing failure returns on the first attempt.

### Install (`installer/macos/pkg/postinstall`)

The `.pkg` postinstall runs as root with no login session. It resolves the human from the console
owner (`stat -f '%Su' /dev/console`; `root`/`loginwindow`/empty count as nobody), runs
`/Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh enable
--auto [--user <console user>] --app /Applications/PrivacyFenceApp.app`, then `daemon
ensure-running`. It always exits 0: a failed `enable` is logged (`/var/log/install.log`), and the
daemon's own admin-password prompt (`privilege_separation.maybe_auto_enable_macos()`) re-offers it
on every start that finds a packaged install unseparated. Re-running the `.pkg` over a running
install is the upgrade path: `enable` re-stages the image and bootouts/bootstraps the LaunchDaemon
unconditionally.

`enable` ([ADR 0003](adr/0003-separated-installs-only.md)):

1. creates the `_privacyfence` system user and group (`dscl`);
2. adds the owner to that group, or records the membership as pending when there is no owner
   (closed later by the companion or `enable --for-user <name>`);
3. lays out the data directory (below);
4. stages a root:wheel-owned copy of the app under `/Library/PrivacyFence/image`, because
   `/Applications` is admin-group-writable and nothing directly under it can be trusted
   (`stage_trusted_image()`);
5. writes the marker `privilege-separation.json` that `src/privacyfence/privilege_separation.py`
   and the shim (`mcpb/shim/src/protocol.ts`) read;
6. renders `/Library/LaunchDaemons/com.privacyfence.daemon.plist` and
   `/Library/LaunchAgents/com.privacyfence.companion.plist` from `installer/macos/*.plist.tmpl`,
   both pointing at the staged copy.

| Path | Owner | Mode | Holds |
|---|---|---|---|
| `/Library/Application Support/PrivacyFence` | `_privacyfence` | `0711` | everything; traversable, not listable |
| `…/authority` | `_privacyfence` | `0700` | `config/settings.yaml`, WebAuthn credentials, audit log and key, the owner's `mcp_token` |
| `…/handoff` | `_privacyfence:_privacyfence` | `3770` (files `0640`) | `mcp_url`, control-channel sockets |

The sticky bit on `handoff` and the rule that `enable` never re-owns a socket are
[ADR 0027](adr/0027-a-group-member-cannot-take-over-another-members-companion-socket.md) and
[ADR 0029](adr/0029-the-layout-step-never-re-owns-a-socket.md). Group membership takes effect at
the next login.

## Windows

### Build (`scripts/build_installer.ps1`)

1. Converts the icon to `.ico` with Pillow; writes the Telegram credentials.
2. Runs PyInstaller on `PrivacyFenceApp.win.spec` (onedir), and copies `PrivacyFenceApp.exe` to
   `privacyfence-app.exe` (a real copy; Windows has no bundle symlink).
3. Builds the `.mcpb` by running `scripts/build_mcpb.sh` under Git for Windows' `bash.exe`.
4. Signs `PrivacyFenceApp.exe`, `privacyfence-app.exe` and `PrivacyFenceCompanion.exe`, runs
   `iscc.exe` on `installer/privacyfence.iss`, then signs the setup `.exe`.

### Signing (eSigner CodeSignTool)

The code-signing key lives only in SSL.com's eSigner cloud HSM, so signing goes through SSL.com's
CodeSignTool CLI (`CodeSignTool.bat sign ... -override`, signing in place) rather than `signtool.exe`
and a local `.pfx`. `build_installer.ps1` signs only when `CODESIGNTOOL_DIR` is set, and then needs
`ES_USERNAME`, `ES_PASSWORD`, `ES_CREDENTIAL_ID` and `ES_TOTP_SECRET` (CI secrets `ESIGNER_USERNAME`,
`ESIGNER_PASSWORD`, `ESIGNER_CREDENTIAL_ID`, `ESIGNER_TOTP_SECRET`).

In CI, `build.yml`'s `build-windows` step **"Install eSigner CodeSignTool"** (skipped when
`ESIGNER_CREDENTIAL_ID` is unset) downloads one pinned release and checks its SHA-256 before
extracting it and exporting `CODESIGNTOOL_DIR`
([ADR 0046](adr/0046-release-ci-pins-codesigntool-by-version-and-sha256.md)). The pin is that step's
two `env:` lines, `CODESIGNTOOL_VERSION` (currently `v1.3.2`) and `CODESIGNTOOL_SHA256`. To bump it,
change those two lines and nothing else; keep the hash quoted; take the hash from the release page's
asset list or by hashing the `CodeSignTool-<version>-windows.zip` yourself, never from a CI log. On
a mismatch the step fails and prints the downloaded hash beside GitHub's recorded digest, for
diagnosis only.

### Install (`installer/privacyfence.iss`)

- `PrivilegesRequired=admin`; installs to `%ProgramFiles%\PrivacyFence` with the onedir output, the
  versioned `.mcpb`, `privilege-separation.ps1` (renamed from
  `scripts/windows_privilege_separation.ps1`) and `privacyfence-companion-task.xml.tmpl` beside it.
- Start Menu: **PrivacyFence** (`PrivacyFenceCompanion.exe --launch`, ADR 0031),
  **PrivacyFence Companion**, and the uninstaller.
- **Before copying files** (`PrepareToInstall`,
  [ADR 0045](adr/0045-the-windows-installer-ends-its-own-processes-and-force-closes-the-rest.md)):
  `sc.exe stop PrivacyFence` and wait up to 30 s for `STOPPED` (a `taskkill` alone loses to the
  service's crash-restart), then `taskkill /F` on `PrivacyFenceCompanion.exe`,
  `PrivacyFenceApp.exe` and `privacyfence-app.exe` and wait up to 30 s for them to exit. Both waits
  continue on timeout. `CloseApplications=force` makes RestartManager terminate anything still
  holding a file, since none of these processes has a window that answers a close request.
- **After copying files** (`CurStepChanged(ssPostInstall)`): runs `privilege-separation.ps1 enable`
  with Setup's elevated token. A failure raises an error and the install is reported as failed
  interactively; it does not change Setup's exit code (Inno cannot fail an install from
  `ssPostInstall`), so automation detects it from the install log or the absence of the marker and
  service.

`enable` provisions ([ADR 0003](adr/0003-separated-installs-only.md) decision 4):

- **Service account**: the virtual account `NT SERVICE\PrivacyFence`, created by
  `sc create PrivacyFence binPath= "\"<install>\privacyfence-app.exe\" --windows-service"
  obj= "NT SERVICE\PrivacyFence" start= auto` (no password). `--windows-service` runs
  `src/privacyfence/windows_service.py`, the SCM service host around `daemon_main.main()`.
  Crash restart is `sc failure PrivacyFence reset= 86400 actions= restart/5000/restart/10000/restart/30000`.
  An Event Log source for the service is registered from the bundled `servicemanager*.pyd`.
- **Companion autostart**: the only Scheduled Task, `PrivacyFenceCompanion`, rendered from
  `installer/windows/privacyfence-companion-task.xml.tmpl` (`__EXEC_PATH__` → the installed
  `PrivacyFenceCompanion.exe`) and registered with `schtasks /create /xml`. It is a `LogonTrigger`
  for any user, principal `GroupId` Users at `LeastPrivilege`, `MultipleInstancesPolicy`
  `IgnoreNew`, no battery restrictions, no repeating trigger. The template's header comment lists
  the details that must not be dropped (`version="1.2"`, the Principal `id`/Actions `Context`
  binding, `StartBoundary`, no `encoding` attribute).
- **Group**: local group `PrivacyFenceUsers`; the owner is added (pending when no owner resolves;
  `enable -ForUser <name>` or the companion closes it). The virtual account cannot hold group
  memberships, so every ACL names it directly.
- **Companion required**: `enable` refuses if `PrivacyFenceCompanion.exe` is missing. The service
  runs in session 0 and cannot open a browser, so the companion's control channel is what opens
  connector OAuth pages ([ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md)
  decision 5).
- **Image check**: `Assert-ImageProtected` refuses if anything other than `SYSTEM`/`Administrators`
  can write the install directory, since the service runs whatever its `binPath` names; the daemon
  re-checks its own image on every start.

ACLs on `%ProgramData%\PrivacyFence`, set by `Set-Layout` with well-known SIDs so they work in every
locale, and audited on every daemon start by `src/privacyfence/windows_acl.py`:

| Path | Grants (inheritance removed first) | POSIX equivalent | Holds |
|---|---|---|---|
| `%ProgramData%\PrivacyFence` | service account, `SYSTEM`, `Administrators` full; `Users` traverse (`X`) only | `0711` | everything |
| `…\authority` | service account, `SYSTEM`, `Administrators` full | `0700` | settings, WebAuthn credentials, audit log and key, `mcp_token` |
| `…\handoff` | as above, plus `PrivacyFenceUsers` read/execute | `3770` | `mcp_url`, discovery files |

The tree is owned by `Administrators` (`icacls /setowner`) so the signed-in user cannot hold
`WRITE_DAC` on a directory they pre-created, and files already in `handoff\` are `icacls /reset` to
inherit the new grants. `handoff\` is read-only to the group because both control channels are
named pipes, whose own DACLs (`web/control_channel.py`) grant the service account and the group.

## Debian/Ubuntu

### Build (`scripts/build_deb.sh`)

Builds the onedir with `PrivacyFenceApp.linux.spec`, stages a package tree by hand and runs
`dpkg-deb --build --root-owner-group` (not `dpkg-buildpackage`; `debian/install` documents the same
file mappings for `dh_install`). Along the way it strips build-host library search paths, sets
bundled `.so` files to `0644`, renders `DEBIAN/control` for the host architecture, runs
`check_deb_glibc_floor.py`, and finally `lintian --fail-on error` when `lintian` is installed. The
Debian version maps PEP 440 pre-releases with a tilde (`4.3.0a2` → `4.3.0~a2`, `.devN` →
`~devN`) so they sort before the release ([ADR 0018](adr/0018-linux-ships-a-self-contained-deb-built-with-pyinstaller.md)).

Installed files: `/opt/privacyfence/` (the bundle), `/usr/bin/privacyfence-app` and
`/usr/bin/privacyfence-companion` (wrappers from `resources/linux/`), the companion's
Applications-menu entry, `/usr/sbin/privacyfence-privilege-separation` (renamed
`scripts/linux_privilege_separation.sh`), its templates under
`/usr/share/privacyfence/installer/linux/`, the polkit action
`eu.privacyfence.daemon-control.policy` (lets the companion run `daemon start`/`stop`/`restart`
through `pkexec`), icons, and lintian overrides. No autostart entry ships in the package.

### Maintainer scripts

| Script | Case | Action |
|---|---|---|
| `postinst` | `configure` (every install and upgrade) | `privacyfence-privilege-separation enable --machine-only`, unguarded: a failure fails the install. Then, if `$SUDO_USER` is set and not `root`, `enable --auto --for-user "$SUDO_USER" \|\| true` |
| `prerm` | `remove` | `privacyfence-privilege-separation uninstall \|\| true` |
| `prerm` | `upgrade` | `systemctl stop privacyfence-daemon.service` |
| `postrm` | `purge` | deletes `/var/lib/privacyfence`, then `userdel`/`groupdel privacyfence` |

The machine half creates the `privacyfence` system account (`useradd --system`), the data directory,
the marker, `/etc/systemd/system/privacyfence-daemon.service` (from
`installer/linux/privacyfence-daemon.service.tmpl`, then `systemctl enable --now`) and the
companion's `/etc/xdg/autostart/privacyfence-companion.desktop` (runs
`privacyfence-companion --serve` in each user session). The daemon itself is a system unit only;
it has no XDG autostart entry. An install with no `$SUDO_USER` (root shell, unattended upgrade) ends
up separated with the group membership pending, which `status` reports as `PENDING USER`.

| Path | Owner | Mode | Holds |
|---|---|---|---|
| `/var/lib/privacyfence` | `privacyfence` | `0711` | everything |
| `…/authority` | `privacyfence` | `0700` | settings, WebAuthn credentials, audit log and key, `mcp_token` |
| `…/handoff` | `privacyfence:privacyfence` | `3770` (files `0640`) | `mcp_url`, control-channel sockets |

The repository-root `privacyfence.service` is a `systemd --user` unit for a `pip`/`pipx` install and
is not part of the `.deb`. A `pip`/`pipx` or source install is not separated unless someone runs
`sudo ./scripts/linux_privilege_separation.sh enable` by hand; the tool does not touch that
`--user` unit, so disable it first or a second daemon starts as the logged-in user and refuses to
run (`privilege_separation.check_runtime_identity()`). A packaged build that finds itself
unseparated refuses to serve (ADR 0003 decision 6); `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1` overrides
that for development only. An unseparated Windows run (source checkout, `pip`/`pipx`) keeps its
state under `%LOCALAPPDATA%\PrivacyFence` (`paths.py`), protected only by the profile's default
ACLs, with no `icacls` step.

**Further accounts.** On any platform, `enable --for-user <name>` (POSIX) or `enable -ForUser
<name>` (Windows) adds another account to the service group, or that account's companion offers the
same join at its first login. Each account becomes its own principal
([ADR 0008](adr/0008-one-principal-per-os-user.md)) and has to log out and back in once.

## Uninstall and purge

All three platforms split remove from purge the same way
([ADR 0042](adr/0042-uninstall-replaces-disable.md)); nothing ever moves data back into a home
directory or user profile ([ADR 0041](adr/0041-only-the-current-install-layout-is-supported.md)).

| Platform | Uninstall (keeps data, marker, account/group) | Purge (also deletes them) |
|---|---|---|
| macOS | `sudo …/macos_privilege_separation.sh uninstall`: boots out and removes both plists, deletes `/Library/PrivacyFence`, and deletes `/Applications/PrivacyFenceApp.app` and `pkgutil --forget`s `com.privacyfence.installer` when the `.pkg` installed it | `uninstall --purge`: also deletes `/Library/Application Support/PrivacyFence`, the `_privacyfence` group and account |
| Windows | Uninstaller runs `privilege-separation.ps1 uninstall`: removes the service (the virtual account goes with it and returns with the same SID on reinstall), the companion task and the Event Log source; `[UninstallRun]` then `schtasks /delete` / `sc stop` / `sc delete` as a floor | **Delete PrivacyFence data** checkbox (unchecked by default, never shown on a silent uninstall) adds `-Purge`: also deletes `%ProgramData%\PrivacyFence` and `PrivacyFenceUsers` |
| Debian/Ubuntu | `apt remove`: `prerm` runs `uninstall`, which stops and removes the unit and the autostart entry | `apt purge`: `postrm` deletes `/var/lib/privacyfence` and the `privacyfence` account and group |

Each script also has `status` (audits the layout) and `daemon status|start|stop|restart` (used by
the companion's service controls;
[ADR 0026](adr/0026-the-companion-manages-the-daemon-through-the-service-manager.md)).

## CI

`build.yml` runs on a `v*` tag push and on `workflow_dispatch` (every upload, publish and release
step is gated on a tag ref, so a dispatch against an untagged commit is the pre-flight described in
[`CLAUDE.md`](../CLAUDE.md#releasing)). Each job builds one artifact, runs its
`pytest.mark.packaged` tests against it, and only then uploads it; a failed test stops the job
before any upload.

| Job | Runner | Builds | Packaged tests (inline) |
|---|---|---|---|
| `build` | `macos-latest` (arm64) | DMG (`.app`, `.pkg`, `.mcpb`) | `test_macos_packaged_smoke.py` (mount, daemon→MCP→approval round trip, `codesign --verify`/`spctl`, upgrade), `test_macos_pkg_smoke.py` (structural, `pkgutil --expand-full`, no install) |
| `build-windows` | `windows-latest` | setup `.exe` | `test_windows_packaged_smoke.py` (silent install with separation, round trip, uninstall keep/purge, upgrade) |
| `build-deb` | `ubuntu-latest` (amd64) | `.deb` | `test_org_ubuntu_release_smoke.py`, then `test_deb_packaged_lifecycle.py` (`dpkg -i`, `desktop-file-validate`, round trip, `dpkg -r`/`-P`, upgrade) |
| `sbom` | `ubuntu-latest` | CycloneDX SBOMs for the Python lock file and the shim | — |
| `finalize-release` | `ubuntu-latest` | needs all four; attaches files, renders `CHANGELOG.md` notes, promotes R2 | runs `scripts/check_graphical_session_coverage.py` |

All tests live in `tests/integration/`. Each job first runs `scripts/r2_release.py check-tag`
([ADR 0022](adr/0022-one-release-tag-per-commit.md)).

The graphical-session tier needs a real login-equivalent session and is too slow for the release
path, so it runs in its own workflows, on pushes to `main`/`releases/**` that touch packaging paths,
weekly (Monday 07:00/08:00/09:00 UTC for Linux/Windows/macOS) and on dispatch:

| Workflow | Tests |
|---|---|
| `linux-graphical-session.yml` | `test_linux_graphical_session_autostart.py`: real `systemd --user` session and `xdg-desktop-autostart.target`, OAuth loopback under Xvfb |
| `windows-graphical-session.yml` | `test_windows_graphical_session_autostart.py`: the task definition Task Scheduler stored, checked against `tests/windows_task_contract.py`; companion started by Task Scheduler; service killed and restarted by the SCM |
| `macos-graphical-session.yml` | `test_macos_graphical_session_autostart.py` (`enable` via `sudo`, LaunchDaemon and LaunchAgent up as the right accounts), `test_macos_pkg_install.py` (real `sudo installer -pkg`, postinstall alone wires both) |

`finalize-release` never waits on these; it checks whether the latest reachable run of each passed,
warns on a gap for a pre-release, and fails a stable tag.

Per PR, `tests/unit/test_privilege_separation.py` holds the three separation scripts, the service
templates, `installer/privacyfence.iss`'s cleanup names, `privilege_separation.py` and the shim's
marker parser to one contract, and `tests/platform/test_windows_acls.py` runs real `icacls` on the
`platform-windows` job. What CI cannot reach (two real OS accounts, the interactive `LogonTrigger`,
Installer.app's GUI password dialog) is in [`release-testing.md`](release-testing.md). Layer
definitions are in [`testing-policy.md`](testing-policy.md#the-seven-layers).
