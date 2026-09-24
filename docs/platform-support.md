# Platform support

PrivacyFence local mode is packaged for macOS, Windows, and Debian/Ubuntu Linux. Org mode is a Linux/server deployment path.

## Support matrix

| Platform | Minimum OS | Distribution | Startup model | Release automation |
|---|---|---|---|---|
| macOS | macOS 13, Apple silicon | one signed/notarized DMG, carrying the `.pkg` installer (which holds the PyInstaller app bundle) and the MCPB side by side | installed by the `.pkg`, which provisions a LaunchDaemon under a dedicated account at install time, mandatorily (see below); D1's runtime prompt is the fallback for an install that reached a running state some other way, not a second shipped path | `.github/workflows/build.yml` on `macos-latest` |
| Windows | Windows 10 / Windows Server 2016 (x64) | Inno Setup installer containing the PyInstaller executable and MCPB | the installer separates the install as part of installing (see below): a Windows service under a virtual service account runs the daemon, a Task Scheduler entry runs the companion | `.github/workflows/build.yml` on `windows-latest` |
| Debian/Ubuntu local mode | glibc 2.38 and systemd 242 (Ubuntu 24.04, Debian 13 or newer) | self-contained `.deb` built from the PyInstaller onedir output | `postinst` separates the install unconditionally on every install and upgrade (see below): a system systemd unit under a dedicated account runs the daemon, an XDG autostart desktop entry runs the companion | `.github/workflows/build.yml` on `ubuntu-latest` |
| Linux Python install | Python 3.11 | wheel/sdist with `privacyfence-app` console script | operator-managed process or `privacyfence.service` | PyPI publishing workflow |
| Linux org mode | Python 3.11 | Python/system service behind the configured reverse proxy and identity provider | operator-managed service | release smoke coverage in the build/test suite |

Each installer enforces its own row's floor and refuses an older system instead of installing
something that cannot start: the `.pkg` through its `allowed-os-versions` (the app's
`LSMinimumSystemVersion`) and `hostArchitectures="arm64"` (the app is built for Apple silicon
only, so an Intel Mac is refused), the Windows installer through `MinVersion`, and the `.deb` through its
`libc6`/`systemd` dependency versions. The `.deb`'s glibc floor is whatever the build runner linked
the bundled Python against; `scripts/check_deb_glibc_floor.py` fails the build if that ever rises
above the declared floor. `tests/unit/test_minimum_os_versions.py` keeps all three in step with
this table.

## Shared runtime architecture

All desktop platforms run the same Python daemon and embedded web UI. MCP clients connect to the daemon's `/mcp` Streamable HTTP endpoint. Claude Desktop uses the bundled Node/TypeScript stdio shim in `mcpb/shim/` to discover and proxy to that endpoint.

The daemon uses `portalocker` for the single-instance lock, so the locking abstraction is shared across POSIX and Windows. Platform-specific behavior is concentrated in packaging, process discovery/startup, filesystem locations, browser launching, and installer integration.

### What the companion does on every platform

The companion app is the only PrivacyFence process that runs inside a human's own login session
(the daemon runs as a service account on every packaged install — see each platform's *Privilege
separation* section below), which is what makes it the place for anything that needs a person
rather than a process. Four jobs, in the order the first three happen (the fourth runs continuously
alongside them):

1. **Close the pending per-user half of privilege separation** ([ADR
   0003](adr/0003-separated-installs-only.md) decision 3) — the group membership an installer with
   nobody at the console could not add.
2. **Offer the first passkey enrollment.** A packaged install defaults `step_up.enabled` and
   `step_up.require_passkey` on (see [`security-and-compliance.md`](security-and-compliance.md)),
   so a fresh one comes up requiring a passkey it does not have yet and releasing nothing until it
   does. The companion asks the daemon whether that is the case at each start, and if it is, opens
   `/security` with a session already minted. It re-offers at every start until something is
   enrolled, and does nothing at all once one is. (Skipped while the membership from step 1 is
   still pending: group membership is evaluated when a session is created, so until the human has
   logged out and back in there is no page to open.)
3. **Show the one-time recovery code**, and issue a replacement on request — "New Recovery Code…"
   on the macOS/Windows menu bar, the matching Applications-menu entry on Linux
   (`--action=recovery-code`). The code is never in an HTTP response body on a packaged install.
4. **Manage the daemon itself** ([ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md)'s
   Amendment, "the companion becomes the daemon manager"). A live status line on the macOS/Windows
   menu bar (`● PrivacyFence is running (v4.2.0)`, `○ ... is not running`, `⚠ ... has failed` /
   `is not responding`), polled every five seconds and backed by `daemon_status.py`'s `probe()` —
   the daemon's own control channel first, the platform's service manager
   (`launchctl print`/`systemctl show`/`sc.exe query`) if that doesn't answer. Start/Restart/Stop
   run the platform script's own `daemon start`/`stop`/`restart` subcommand with a fresh,
   platform-native elevation prompt each time (`service_control.py`'s `run_elevated()`) — no
   standing grant, same as every other elevation this doc's *Privilege separation* sections
   describe. Linux, which has no tray (decision 4's dependency budget), gets the same four actions
   as `.desktop` Applications-menu entries (`--action=service-status`/`service-start`/
   `service-restart`/`service-stop`) plus a `notify-send` notification from the `--serve` process's
   own background poll when the daemon has been down for more than fifteen seconds.

The first three need a dialog on the human's desktop. macOS uses `osascript`, Windows `MessageBoxW`,
and Linux whichever of `zenity` or `kdialog` is present — none of them a PrivacyFence dependency
(ADR 0002 decision 4's Linux budget). **On Linux, a desktop with neither program installed cannot
show these**, and PrivacyFence says so rather than failing quietly: a first enrollment is refused
with that reason, and a recovery code is not issued rather than issued to nobody. The fourth
(status/start/stop/restart) needs no dialog to *read* — only the elevation prompt itself, which is
the OS's own (`osascript`/`pkexec`/UAC), not one of these two.

### Opening PrivacyFence itself opens Approvals

Every platform's visible entry point runs the companion, never the daemon ([ADR
0031](adr/0031-clicking-privacyfence-opens-approvals-through-the-companion.md)). On macOS that is
the app in `/Applications`, whose bundle's main executable is a launcher
(`Contents/MacOS/PrivacyFence`, not the daemon's `PrivacyFenceApp`). On Windows it is the main
Start Menu entry, and on Linux the Applications-menu entry. What happens depends on whether a
companion is already running:

- **A companion is running:** the click asks it to open Approvals, and it asks the human to
  confirm first. The request comes from another process running as the same user, so the
  confirmation cannot be skipped. On a separated POSIX install, this page request is the one
  thing the companion's own user may send the companion's channel.
- **No companion is running (macOS, Windows):** the click becomes the tray/menu-bar companion
  itself and opens Approvals with no dialog.
- **No companion is running (Linux):** the click falls back to the view-only link, as
  `--action=open-approvals` always has.

launchd, the Scheduled Task and systemd still start the daemon and the companion by their own
explicit paths, so none of this touches the service.

## macOS

The macOS app is defined by `PrivacyFenceApp.spec`. Release builds are produced by `scripts/build_dmg.sh` and the macOS job in `.github/workflows/build.yml`.

**One macOS download.** `scripts/build_dmg.sh` builds the app bundle, the `.mcpb`, and (by calling
`scripts/build_pkg.sh`) the `.pkg`, then puts the `.pkg` and the `.mcpb` on the DMG — and nothing
else: no app bundle to drag, no `/Applications` symlink. Mount it, double-click
`PrivacyFence.pkg`, then double-click `PrivacyFence.mcpb`. The `.pkg` is not published on its own
(not to GitHub Releases, not to the R2 archive, not as a second card on the download page); the
DMG that carries it is the macOS release artifact. See the `.pkg` section below for what that
buys, and `scripts/build_dmg.sh`'s own header for the two problems the old layout had.

The packaged application keeps user state outside the application bundle. The release workflow signs and notarizes the app/DMG when the required signing credentials are configured.

### Privilege separation (mandatory)

Without this, the daemon starts in the logged-in user's session — the LaunchAgent path above —
which is also the session the AI client it governs runs in. `scripts/macos_privilege_separation.sh
enable` changes that: it creates a dedicated `_privacyfence` system account, moves the data
directory from `~/.privacyfence` to `/Library/Application Support/PrivacyFence` owned by that
account, and inverts the startup wiring — a **LaunchDaemon**
(`installer/macos/com.privacyfence.daemon.plist.tmpl`) runs the daemon with no login session at
all, while a **LaunchAgent** (`installer/macos/com.privacyfence.companion.plist.tmpl`) runs the
companion app in each user session so a human still has a way in.

The `.pkg` below runs this itself, as root, during the install — the ordinary macOS path, since
[ADR 0003](adr/0003-separated-installs-only.md) decision 2 left nothing else to install macOS
PrivacyFence from. The admin-password dialog (`privilege_separation.maybe_auto_enable_macos()`,
called from `daemon_main.main()`) is the fallback for a `.pkg` install whose postinstall did not
finish separating — the postinstall never fails the install, so the app can land in `/Applications`
with no marker, and the MCPB shim then starts the packaged daemon directly. It is asked again on
every start it finds itself still unseparated, not just once: ADR 0003 decision 6 retired the
one-shot marker that used to make a decline permanent, because under that ADR a decline is not a
configuration, it is an unfinished install. `enable`/`uninstall`/`status` remain available by hand
([ADR 0042](adr/0042-uninstall-replaces-disable.md)): `uninstall` stops and unregisters the
LaunchDaemon and companion and keeps the data under the system root; `uninstall --purge` also
deletes it. Nothing moves data back into `~/Library` — see ADR 0003 decision 6 below: a
**packaged** build that ends up unseparated refuses to serve.

Three parts of the layout matter to anything that has to find PrivacyFence's files:

| Path | Owner | Mode | Holds |
|---|---|---|---|
| `/Library/Application Support/PrivacyFence` | `_privacyfence` | `0711` | everything; traversable but not listable |
| `…/authority` | `_privacyfence` | `0700` | `config/settings.yaml`, WebAuthn credentials, audit log + key, the owner's own `mcp_token` (ADR 0008 D3) |
| `…/handoff` | `_privacyfence:_privacyfence` | `3770` | `mcp_url`, the control-channel sockets |

The installing user is added to the `_privacyfence` group, which is what keeps `handoff` reachable
from their session — macOS evaluates group membership at login, so this needs a logout/login to take
effect. `src/privacyfence/privilege_separation.py` resolves all of it from a marker file the
installer writes, and the MCPB shim (`mcpb/shim/src/protocol.ts`) reads the same marker so Claude
Desktop keeps finding the daemon. `… status` audits the result; `… uninstall [--purge]` removes it.

Mandatory on every packaged install as of [ADR 0003](adr/0003-separated-installs-only.md) — the
manual `enable`/`uninstall`/`status` subcommands above still exist for inspecting or removing an
install by hand. Linux and Windows separate too
(below), the same ADR making all three mandatory rather than leaving any of them opt-in.
See [`security-and-compliance.md`](security-and-compliance.md#privilege-separation-macos-linux-and-windows) for
what the separation does and does not buy.

### `.pkg` installer (#428 D2)

`scripts/build_pkg.sh` packages the `.app` `scripts/build_dmg.sh` builds into a signed installer
package (`installer -pkg PrivacyFence-<version>.pkg -target /`, or the ordinary double-click
Installer.app flow from the mounted DMG) whose own `postinstall` script
(`installer/macos/pkg/postinstall`) runs `macos_privilege_separation.sh enable --auto` while the
package install is still running, as root. This exists because D1's own runtime admin-password
prompt (`maybe_auto_enable_macos()`) was the *only* automatic path a drag install had — dragging
an app bundle runs nothing as root, so D1 could only ask the daemon's own first start to pop a
dialog, with no PrivacyFence-specific explanation, that can appear disconnected from anything the
person just did, and that a decline or a failed safety check silently leaves unresolved. That
prompt still exists, for an install that never went through the installer (a `pip`/source run, a
bundle copied off another machine), but it is no longer what a download leads to. A `.pkg` install
already runs as root and already asks for an administrator password as the ordinary "Install
PrivacyFence" step non-technical users already expect, so the one elevation macOS requires for this
happens there instead — once, with `installer/macos/pkg/resources/welcome.html`/`conclusion.html.tmpl`
explaining what it does, rather than a bare system dialog.

Since Apple's installer runs package scripts with no login session and no `$SUDO_USER`, the
postinstall script resolves the human to provision this for from the logged-in console account
(`stat -f '%Su' /dev/console`) instead — the same thing Finder/`who` would show. Since
[ADR 0003](adr/0003-separated-installs-only.md) decision 3, "nobody logged in" no longer means "the
install stays unseparated": `enable`'s machine half — the service account, the data directory, the
marker, the LaunchDaemon — runs regardless, and only the one per-person step (the group membership)
is left pending, closed by the companion the first time a real login session starts one. If the
daemon's bundled `macos_privilege_separation.sh` can't be found at all, the postinstall logs why and
leaves the daemon's own runtime prompt to offer this later — it never fails the package install
itself over this.

A pkg-installed `.app` lands root:wheel-owned by `pkgbuild`'s own default ownership -- but
`/Applications` itself is always `root:admin`, so that alone was found not to satisfy
`require_trusted_image()` (B1), which walks every ancestor directory including `/Applications`
itself. `enable` now closes that itself, for every caller (this `.pkg`'s postinstall, D1's own
runtime prompt, and a human running it by hand) rather than the `.pkg` alone: it stages its own
root:wheel-owned copy of whatever `--app` points at before trusting anything. See
`macos_privilege_separation.sh`'s `stage_trusted_image()` and `CHANGELOG.md`'s `#428 D2` B1
follow-up entry for the full story.

The `.pkg` *is* the macOS install now — it ships inside the DMG rather than beside it, so there
is no longer a drag-install path alongside it to be "additional" to. That also made the installer's
own conclusion screen honest: it tells the user to open `PrivacyFence.mcpb` next to the installer,
which was simply false for anyone who downloaded a standalone `.pkg`. Signing a `.pkg` needs a
"Developer ID **Installer**" certificate -- a different type from the "Developer ID **Application**"
one `scripts/build_dmg.sh --sign` uses -- so it is passed separately, as `SIGN_IDENTITY_INSTALLER`
(with `MACOS_INSTALLER_CERTIFICATE*`), and stays optional; an unsigned `.pkg` inside an otherwise
signed DMG is still a valid local dev build. Covered by the same
two-tier split as the DMG: `test_macos_pkg_smoke.py` (structural only -- `pkgutil --expand-full`,
no install, no root) runs inline in `build.yml`'s release-critical `build` job; `test_macos_pkg_install.py`
(a real `sudo installer -pkg ... -target /`, with no separate `enable` call, proving the postinstall
script alone wires up both the LaunchDaemon and the companion LaunchAgent) runs in the weekly
`macos-graphical-session.yml`, alongside `test_macos_graphical_session_autostart.py`'s own coverage
of the manual-script path.

## Windows

The Windows executable is defined by `PrivacyFenceApp.win.spec`. `scripts/build_installer.ps1` builds the application and invokes `installer/privacyfence.iss` to produce the installer.

The installer:

- installs PrivacyFence under Program Files;
- installs the bundled MCPB/shim assets;
- creates a Start Menu entry that opens Approvals through the companion (`--launch`, starting
  the tray icon first if it isn't running; [ADR
  0031](adr/0031-clicking-privacyfence-opens-approvals-through-the-companion.md));
- creates a Start Menu entry for the companion app;
- installs the privilege-separation tool as `privilege-separation.ps1` next to the
  application, with the companion autostart task template it renders (see below);
- **runs `privilege-separation.ps1 enable` itself**, with its own elevated token, as a post-install
  step — ADR 0003 decision 4. That is what installs and starts the daemon (as its service) and
  registers the companion's sign-in task; there is no daemon sign-in task. A failure of this step
  fails the install;
- on uninstall, runs `privilege-separation.ps1 uninstall`, which stops and removes the service and
  the companion task and keeps `%ProgramData%\PrivacyFence` — data and marker — and the
  `PrivacyFenceUsers` group, so a reinstall picks the data up again
  ([ADR 0042](adr/0042-uninstall-replaces-disable.md)). The uninstaller offers a
  **Delete PrivacyFence data** checkbox, unchecked by default, which adds `-Purge` and deletes those
  too; a silent uninstall never purges.

Optional signing is configured through `CODESIGNTOOL_DIR`, `ES_USERNAME`, `ES_PASSWORD`, `ES_CREDENTIAL_ID`, and `ES_TOTP_SECRET` — see `scripts/build_installer.ps1`'s header comment. Signing goes through SSL.com's eSigner CodeSignTool rather than a local Authenticode `.pfx`, since CA/B Forum's 2023 key-storage rules mean code-signing private keys can no longer be exported to a portable `.pfx` at all.

### Privilege separation (mandatory)

The same change as macOS's and Linux's, in Windows' own primitives, and the one platform where
those primitives are genuinely different rather than differently spelled. Without it, the daemon
and the AI client it governs would run in the same session as the same account; per
[ADR 0003](adr/0003-separated-installs-only.md) decision 4 the installer runs this itself, elevated,
as an install step, so an ordinary install never ends up in that state. The same command remains available for inspecting or re-running it by hand, from an
**elevated** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" enable
```

(A source checkout runs the same file as `scripts/windows_privilege_separation.ps1`; `... status`
audits the result, `... uninstall [-Purge]` takes the service and the companion task down — see
"Uninstall" below. There is no `disable`: nothing moves the data back into a user profile
([ADR 0041](adr/0041-only-the-current-install-layout-is-supported.md)).)

That creates a **virtual service account** (`NT SERVICE\PrivacyFence` — materialized by the Service
Control Manager along with the service, with its own SID and no password anyone has to manage, in
preference to the shared `LocalService` #428 mentions), lays out `%ProgramData%\PrivacyFence` for
it, and wires startup the separated way — a **Windows service** (`PrivacyFence`,
`sc.exe`-registered, with its own `sc failure` crash-restart) runs the daemon with no desktop
session at all, while a **Scheduled Task** (`PrivacyFenceCompanion`, from
`installer/windows/privacyfence-companion-task.xml.tmpl`) runs the companion tray app in each user
session. Nothing is migrated in from `%LOCALAPPDATA%`.

Three things carry over unchanged from the POSIX layouts — the marker file every PrivacyFence
process reads, the three directories, and which files move into `handoff\`. What does not carry
over is the permission model:

| Path | Trustees | POSIX equivalent | Holds |
|---|---|---|---|
| `%ProgramData%\PrivacyFence` | service account, `SYSTEM`, `Administrators` full; `Users` traverse-only | `0711` | everything; traversable but not listable |
| `…\authority` | service account, `SYSTEM`, `Administrators` | `0700` | `config/settings.yaml`, WebAuthn credentials, audit log + key, the owner's own `mcp_token` (ADR 0008 D3) |
| `…\handoff` | the above, plus the `PrivacyFenceUsers` local group, read-only | `3770` | `mcp_url`, the discovery files |

Two steps before any grant are load-bearing, and both are easy to leave out. `icacls
/inheritance:r` on each directory: `%ProgramData%` grants `Users` read-and-execute by inheritance,
so a directory created under it is readable by every account on the machine until that inheritance
is cut. And `icacls /setowner` on the tree to `Administrators`: an object's owner holds `WRITE_DAC`
implicitly whatever its ACL says, and any user may create a directory under `%ProgramData%` — one
the signed-in account created there first would otherwise stay owned by the very account being
excluded, wearing an ACL that account could rewrite with one command and no elevation.
Administrators rather than the service account, deliberately: it needs no privilege juggling, and
it denies the daemon `WRITE_DAC` on its own boundary. Ownership is checked by `… status` and
re-checked on every daemon start, alongside the grants. The installing user is added
to `PrivacyFenceUsers`, which is what keeps `handoff\` reachable from their session — Windows puts
group memberships in the logon token, so this needs a sign-out/sign-in to take effect, exactly like
macOS and Linux. `src/privacyfence/windows_acl.py` is both the translation table above and the audit
that reads it back on every daemon start.

`handoff\` is read-only to the group here, where POSIX has to grant `rwx`: on this platform both
control channels are named pipes rather than socket files, so nothing in the user's session ever
creates anything in that directory. The pipes carry the equivalent grant in their own DACLs instead
(`web/control_channel.py`), naming the service account and the group explicitly rather than relying
on membership — a virtual service account cannot hold one.

Two Windows-only requirements, both enforced rather than documented:

- **A per-machine install.** A service runs whatever its `binPath` names, so an install the
  logged-in user can rewrite would let the agent run its own code *as the service account*. `enable`
  reads the install directory's ACL and refuses if anything but `SYSTEM`/`Administrators` can write
  it, which rules out a non-elevated per-user install outright — [#407](https://github.com/privacyfence/privacyfence/issues/407)
  added that path and [ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) decision
  5a preserved it as a second, unseparated tier; [ADR 0003](adr/0003-separated-installs-only.md)
  decision 4 withdrew it outright rather than leaving it opt-in. There is one Windows install tier
  now: the elevated, per-machine install under `%ProgramFiles%` the installer above already
  requires (`PrivilegesRequired=admin`, set since [#410](https://github.com/privacyfence/privacyfence/issues/410)
  for the unrelated reason that a non-elevated install could never register its own autostart task).
  The daemon re-checks its own image on every start.
- **The companion.** A service runs in session 0 and cannot reach the desktop, so
  `oauth_loopback.run_browser_oauth()` has no browser to open for Slack/Salesforce/Atlassian. The
  companion's control channel is what opens those pages (ADR 0002 decision 5), so `enable` refuses
  to install the daemon half alone.

The daemon is started by the SCM as `privacyfence-app.exe --windows-service`, not as the plain
executable: Windows' service manager waits for a started process to call
`StartServiceCtrlDispatcher` and kills one that never does (error 1053), so
`src/privacyfence/windows_service.py` is a service host wrapping the same `daemon_main.main()` the
console entry point calls. Stopping the service takes the same shutdown path the web UI's own Quit
button does.

**Uninstall** follows [ADR 0042](adr/0042-uninstall-replaces-disable.md), the same split
as the `.deb`'s `apt remove`/`apt purge`. Uninstalling PrivacyFence runs
`privilege-separation.ps1 uninstall`: the service and the companion task are removed (the
`NT SERVICE\PrivacyFence` virtual account goes with its service, and comes back with the same SID
on reinstall), and `%ProgramData%\PrivacyFence` — data and marker — and `PrivacyFenceUsers` stay
where they are, so reinstalling picks them straight back up. Ticking **Delete PrivacyFence data**
in the uninstaller (unchecked by default; a silent uninstall never asks) runs `uninstall -Purge`,
which deletes the directory and the group as well. After a plain uninstall, an administrator can
still delete `%ProgramData%\PrivacyFence` by hand.

The `platform-windows` job in `.github/workflows/tests.yml` runs the full core Python suite on `windows-latest` on every PR, alongside the normal Ubuntu suite. Windows packaging itself (the installer build, silent install/autostart/uninstall) is exercised only by the release build workflow (`build.yml`'s `build-windows` job, tag/`workflow_dispatch`-triggered), not per PR — see "Known open items" below for its current live status.

## Debian/Ubuntu local mode

The local desktop package is defined by `PrivacyFenceApp.linux.spec`, `scripts/build_deb.sh`, `debian/`, and `resources/linux/`.

The `.deb` installs the self-contained application under `/opt/privacyfence`, exposes `/usr/bin/privacyfence-app` and `/usr/bin/privacyfence-companion`, installs application icons and an application-menu entry, and installs the privilege-separation tool as `/usr/sbin/privacyfence-privilege-separation` with its templates under `/usr/share/privacyfence/` (see below — as of #428 D1, `debian/postinst` runs it automatically, `configure)` case, on every install and upgrade).

The repository's `privacyfence.service` is the `--user` unit for a pip/pipx install, not the `.deb`'s startup mechanism.

`apt remove` runs `privacyfence-privilege-separation uninstall` from `debian/prerm`: it stops and unregisters the service and keeps `/var/lib/privacyfence`, the marker and the `privacyfence` account, so a reinstall picks the data up. `apt purge` deletes those too, from `debian/postrm`. Nothing is moved into a home directory ([ADR 0042](adr/0042-uninstall-replaces-disable.md)).

### Privilege separation (mandatory)

The same change as macOS's, above, in Linux's own idioms. Without it, the daemon starts in the
logged-in user's session — a pip/pipx install's `--user` unit — which is also the session the AI
client it governs runs in. `sudo privacyfence-privilege-separation enable` turns that off — the
`.deb` installs it under that name in `/usr/sbin`, and a source checkout runs the same file as
`sudo ./scripts/linux_privilege_separation.sh enable`. It
creates a dedicated `privacyfence` system account (`useradd --system`), creates the data directory
`/var/lib/privacyfence` owned by that account, and inverts the startup
wiring — a **system systemd unit**
(`installer/linux/privacyfence-daemon.service.tmpl` → `/etc/systemd/system/privacyfence-daemon.service`)
runs the daemon with no desktop session at all, while an **XDG autostart entry**
(`installer/linux/privacyfence-companion.desktop.tmpl` → `/etc/xdg/autostart/`) runs
`privacyfence-companion --serve` in each user session.

#428 D1 (4.1): `debian/postinst` runs the separation tool itself on every install and upgrade — it
already runs as root at that point, which is exactly what this needs. ADR 0003 decisions 3 and 5
made that two calls with two different failure policies, because the work splits into two halves
that do not need the same things:

- **The machine half** (`enable --machine-only`) — the system account, the data directory, the
  marker, the systemd unit — needs to know nothing about which human this install is for, so it
  runs unconditionally and *without* `|| true`. A failure of it fails the package install, loudly,
  leaving dpkg with a half-configured package rather than an installed-looking one whose central
  claim does not hold.
- **The per-user half** (`enable --auto --for-user "$SUDO_USER"`) — the group membership — does
  need to know, and `$SUDO_USER` is the
  only thing a postinst has that can say. It names a resolvable, non-root account when the `.deb`
  was installed via `sudo apt install`/`sudo dpkg -i`, and nothing when root installed it directly
  or an unattended upgrade did. So it stays conditional and stays non-fatal: an unattended install
  still ends up separated, with this one re-runnable step pending, which `... status` reports as
  `PENDING USER` (distinct from `OFF`) and which the companion app closes at the first real login
  session.

A pip/pipx source install has no such postinst hook and is not a packaged build in [ADR
0003](adr/0003-separated-installs-only.md) decision 6's sense (that decision's own "Out of scope" —
it is how the project is developed and how org mode is deployed), so it stays opt-in via the manual
command above. `... uninstall [--purge]` takes it down again (see above). A **packaged** daemon
whose separation was purged refuses to serve on its next start (ADR 0003 decision 6).

The tool does not touch a pip/pipx install's `--user` unit: disable it before separating, or it
starts a second daemon as the logged-in user — which on a separated install refuses to start (see
`privilege_separation.check_runtime_identity()`) rather than silently seeding a default policy.

The layout matches macOS exactly apart from the root and the account name:

| Path | Owner | Mode | Holds |
|---|---|---|---|
| `/var/lib/privacyfence` | `privacyfence` | `0711` | everything; traversable but not listable |
| `…/authority` | `privacyfence` | `0700` | `config/settings.yaml`, WebAuthn credentials, audit log + key, the owner's own `mcp_token` (ADR 0008 D3) |
| `…/handoff` | `privacyfence:privacyfence` | `3770` | `mcp_url`, the control-channel sockets |

`/var/lib` rather than `/opt` for the same reason macOS uses `/Library/Application Support`: this is
variable state the daemon rewrites (FHS 3.0 §5.8), while `/opt/privacyfence` holds the read-only,
dpkg-owned application bundle. The installing user is added to the `privacyfence` group, which is
what keeps `handoff` reachable from their session — group membership is evaluated at login, so this
needs a logout/login to take effect. `src/privacyfence/privilege_separation.py` resolves all of it
from a marker file the installer writes, and the MCPB shim (`mcpb/shim/src/protocol.ts`) reads the
same marker. `… status` audits the result; `… uninstall [--purge]` takes it down.

The `--serve` companion is the one piece with no macOS counterpart, and it is not optional: Linux
has no tray (ADR 0002 decision 4), so without a persistent process in the user's session a
separated daemon's `webbrowser.open()` has no display to reach and connector OAuth for
Slack/Salesforce/Atlassian cannot show a sign-in page. It runs the companion's control channel and
nothing else — no `pystray`, no new dependency.

That channel carries one soft external requirement on Linux specifically. Enrolling a *first* passkey
asks the companion to confirm with the human at the login session (see
[`security-and-compliance.md`](security-and-compliance.md#enrolling-a-passkey-is-itself-gated)), and
the companion puts that question up using **`zenity` or `kdialog`**, whichever is present — still no
new PrivacyFence dependency, which is what decision 4's budget actually constrains, but not something
the `.deb` can guarantee either. Every desktop environment this package targets ships one of the two;
a headless or stripped-down install may ship neither, in which case a first enrollment is refused with
a message naming them. macOS (`osascript`) and Windows (`MessageBoxW`) have no equivalent gap — both
are part of the OS.

Installing the `.deb` turns all of this on, and that is the whole of ADR 0003 decision 5.
`debian/postinst` runs `enable --machine-only` on every `configure` — every install and every
upgrade — unconditionally, and **a failure of that step fails the package install**, leaving dpkg
with a half-configured package rather than a silently unseparated PrivacyFence. So the system
systemd unit and the companion autostart entry are written by the package itself, the data
directory is `/var/lib/privacyfence` under the service account, all without anybody having to know
this tool exists.

The per-user half — adding the installing user to the `privacyfence` group — runs second, still
gated on `$SUDO_USER` resolving to a real account and still allowed to defer: an unattended `apt`
upgrade or a root shell has nobody to add, and that leaves the install separated with one group
membership outstanding (`status` reports `PENDING USER`, not "not separated"). The companion closes
it at the next login, or `enable --for-user <name>` does by hand.

**Adding a second account to an already-separated install** works the same way, on any platform:
an administrator runs `enable --for-user <name>` (POSIX) or `-ForUser <name>` (Windows) for that
account, or that account's own companion offers to complete the same pending join at its first
login. Since [ADR 0008](adr/0008-one-principal-per-os-user.md), the result is a second, fully
isolated principal — its own MCP token, approvals, audit log, passkeys and companion-channel
address — never the first account's own data. The one manual step no command can take: the new
account has to log out and back in, because group membership is evaluated when a login session is
created.

## Architecture and CPU constraints

PyInstaller builds are native to the runner architecture. The current Debian release job produces the architecture supported by its Ubuntu runner rather than cross-compiling another CPU target.

## Verification boundaries

The repository distinguishes build automation from target-environment validation. Packaging workflows prove that release artifacts can be built and exercise their automated smoke tests; OS-native presentation and login-session behavior still require the relevant platform environment where automation does not cover it.

What automation deliberately does not cover, and why, is in [`testing-policy.md`](testing-policy.md)'s "What deliberately remains manual"; the platform-specific instances are the "Known open items" immediately below.

## Known open items

- **Windows sign-in and crash-restart.** The daemon is a Windows service (crash-restart is its
  `sc failure ... actions= restart/5000/restart/10000/restart/30000`), and the only Scheduled Task is
  the companion's (`PrivacyFenceCompanion`). `windows-graphical-session.yml` verifies the companion
  task definition **Task Scheduler itself stored** (`schtasks /query /xml`) against
  `tests/windows_task_contract.py`, has Task Scheduler really start the packaged companion as the
  signed-in account, and kills the service's process to watch the SCM restart it; the same contract
  is held against the shipped template on every PR by `tests/unit/test_privilege_separation.py`.
  What CI cannot cover is the `LogonTrigger`'s own firing on a real interactive sign-in — a hosted
  runner cannot produce the Terminal Services session logon it subscribes to — so that is a Windows
  human check in [`release-testing.md`](release-testing.md), tracked on
  [privacyfence/privacyfence#121](https://github.com/privacyfence/privacyfence/issues/121). The
  daemon's own sign-in task that preceded all this, and the chain of real bugs it went through
  (a UTF-8 encoding declaration `schtasks` rejected, a missing `version="1.2"`, an unbound
  `Principal`/`Actions` pair, inverted battery defaults, `<RestartOnFailure>` measured not to restart
  a crashed action, `SeCreateGlobalPrivilege` for a `LogonTrigger` making `PrivilegesRequired=admin`
  necessary — #410), is in this file's git history; the lessons that still apply are in the companion
  template's header comment.
- **Linux org mode's real end-to-end deployment path has now been run once for real**: a fresh Ubuntu
  host following `org-mode-setup-guide.md` verbatim, a real OIDC round trip against a real identity
  provider, and at least one live connector (Gmail) exercised through a real MCP client hitting the
  public `/mcp` URL. The `org-mode-smoke` CI job continues to cover the same daemon/MCP/approval/audit
  contract on every PR, against a synthetic, mocked identity provider — a narrower, faster guarantee
  than a real deployment run, kept for regression coverage rather than as the only proof of the path.
- **Privilege separation, on any of the three platforms, has no automated end-to-end coverage, and
  cannot have any from this repo's CI**: provisioning it needs root (or Administrator), creates a
  real system account, and the property it buys only exists once two real OS accounts are involved —
  a hosted runner can build the artifact but not prove that
  `_privacyfence`/`privacyfence`/`NT SERVICE\PrivacyFence` actually owns `authority/` and that the
  logged-in user actually cannot read it. What CI does prove, per PR, is the contract between
  the four artifacts involved: `tests/unit/test_privilege_separation.py` asserts that each
  platform's installer (`scripts/{macos,linux}_privilege_separation.sh`,
  `scripts/windows_privilege_separation.ps1`), its service definitions (`installer/macos/`'s two
  launchd plists, `installer/linux/`'s systemd unit and autostart entry, `installer/windows/`'s
  companion Scheduled Task plus the service and task names `installer/privacyfence.iss` has to clean
  up), `src/privacyfence/privilege_separation.py` and the MCPB shim's own port of its
  marker discovery (`mcpb/shim/src/protocol.ts`) still agree on every account name, directory, mode
  and marker field, and that every path the module resolves from a marker is the one the installer
  provisions.
  **Windows' own layout is ACLs rather than modes, and that half has more coverage than the rest
  rather than less**, because it is the one part a hosted runner really can exercise: an ACL set on
  a directory the test process just created needs no elevation at all. So
  `tests/unit/test_windows_acl.py` covers the access-mask arithmetic every layout decision rests on,
  `TestWindowsLayoutAudit` drives `audit_layout()`'s Windows branch through a synthetic DACL on
  every platform, and `tests/platform/test_windows_acls.py` proves on the `platform-windows` job
  that real `icacls` output reads back the way all of that assumes — including the two behaviors the
  installer's own structure depends on: that a file created in the handoff directory inherits its
  grants, and that a file *moved* there does not (which is why `enable` runs `icacls /reset` over
  whatever a previous install left there). What none of that reaches is the service, the virtual account, or the
  logon-token group membership. The remaining half — enable on a real machine, confirm the daemon comes up under the
  service account, confirm the companion and the MCPB shim still reach it after a logout/login,
  confirm uninstall keeps the data and a reinstall picks connector tokens back up — is a manual check,
  and belongs with the other per-platform human checks in
  [`release-testing.md`](release-testing.md). **This manual real-machine verification still has not
  run against a release build.** #428 D1 (4.1) turned privilege separation on by default on macOS
  and Linux (Windows stayed opt-in at the time), ahead of the soak-through-a-release-cycle criterion
  this section originally argued for — an explicit override of that plan, not a claim that the gap
  above had closed. [ADR 0003](adr/0003-separated-installs-only.md) has since removed the "opt-in"
  half of that sentence entirely: every packaged install on all three platforms separates now, and a
  packaged build that ends up unseparated anyway refuses to serve (decision 6) rather than running
  degraded. The automated coverage this bullet describes is unchanged either way; running the manual
  checks against a real release build is now more urgent than when this was written, precisely
  because there is no platform left where an unseparated install is the documented default a person
  might have knowingly chosen.
  **The `.pkg` installer (#428 D2, above) is what a macOS download now installs through -- the
  daemon's own runtime prompt is the fallback for an install that bypassed it -- and it has its own
  real-CI coverage** (`test_macos_pkg_install.py`, in this same `macos-graphical-
  session.yml`): a real `sudo installer -pkg ... -target /` with no separate `enable` call,
  confirming the postinstall script alone -- not this test -- wires up the LaunchDaemon and
  companion LaunchAgent. This coverage found a real bug the first time it ran, not just a gap: `enable`
  refused *every* real `/Applications` install outright (B1's `require_trusted_image()` walk always
  failed on `/Applications` itself, `root:admin` on every real Mac) -- true for D1's own runtime
  prompt and a hand-run `enable` too, not only the `.pkg`, since all three share the same check. Fixed
  by having `enable` stage its own root:wheel-owned copy before trusting anything (see `CHANGELOG.md`'s
  `#428 D2 follow-up (B1)` entry and ADR 0002's matching amendment); `test_macos_pkg_install.py` now
  passes against the fix. That closes the "does the automatic path even run to completion, and
  actually separate anything" half of this gap for the `.pkg`, the same way `test_macos_graphical_
  session_autostart.py` -- no longer pre-staging a workaround copy itself -- now does for `enable`
  run by hand. It does **not** close the manual-check gap above for the DMG's own path: `installer
  -pkg` from the command line never invokes Installer.app's GUI or a real admin-password dialog, so a
  human double-clicking the `.pkg` (or, on the DMG side, the daemon's own `osascript` prompt actually
  appearing and being answered) against a real signed release build is still the same still-open
  manual check this bullet has always described -- now at least backed by a mechanism proven to work
  once it runs, rather than one that was silently a dead end.
  **macOS's own launchd wiring is now covered too** (B19,
  [privacyfence/privacyfence#374](https://github.com/privacyfence/privacyfence/issues/374)):
  `macos-graphical-session.yml`/`test_macos_graphical_session_autostart.py` drives
  `scripts/macos_privilege_separation.sh enable` directly via passwordless `sudo` (the only path
  a hosted runner can take at all — the daemon's own unattended trigger,
  `maybe_auto_enable_macos()`, pops a real GUI admin-password dialog nothing in CI can answer) and
  confirms both the daemon's `system/` LaunchDaemon and the companion's `gui/<uid>` LaunchAgent
  actually come up, as the right account, with real control-channel sockets underneath — on the one
  already-logged-in Aqua session a GitHub-hosted `macos-latest` runner gives it, not a second real
  login the way a full soak would need.
  **Linux carries one thing macOS does not**: the companion is a `--serve` process autostarted by
  an XDG entry rather than a tray app, and XDG autostart is a desktop-environment behavior, not a
  systemd one — `linux-graphical-session.yml` covers the daemon's own autostart entry on a real
  session, but the separated install's replacement of it is outside what that workflow provisions.
  **Windows carries two more.** The service host itself (`src/privacyfence/windows_service.py`) is
  only ever exercised by the SCM, so "the daemon comes up as `NT SERVICE\PrivacyFence` rather than
  dying with error 1053" is a manual check and nothing else; and the refusal to separate a
  user-writable install — the check that used to matter for the non-elevated per-user tier
  [#407](https://github.com/privacyfence/privacyfence/issues/407) added and [ADR
  0003](adr/0003-separated-installs-only.md) decision 4 later withdrew — is asserted as a rule
  (`windows_acl.image_problems`, and the `.ps1`'s own `Assert-ImageProtected`) but never run against
  a real install directed at a user-writable location (a manual `/DIR=` override), since CI only
  ever builds and installs to the one supported, per-machine location.
  A connector OAuth flow completing on a separated Linux install is therefore the specific thing
  the manual check has to exercise, not just the daemon coming up.
