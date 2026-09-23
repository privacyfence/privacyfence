# Platform support

PrivacyFence local mode is packaged for macOS, Windows, and Debian/Ubuntu Linux. Org mode is a Linux/server deployment path.

## Support matrix

| Platform | Distribution | Startup model | Release automation |
|---|---|---|---|
| macOS | one signed/notarized DMG, carrying the `.pkg` installer (which holds the PyInstaller app bundle) and the MCPB side by side | installed by the `.pkg`, which provisions a LaunchDaemon under a dedicated account at install time, mandatorily (see below); D1's runtime prompt is the fallback for an install that reached a running state some other way, not a second shipped path | `.github/workflows/build.yml` on `macos-latest` |
| Windows | Inno Setup installer containing the PyInstaller executable and MCPB | the installer separates the install as part of installing (see below): a Windows service under a virtual service account runs the daemon, a Task Scheduler entry runs the companion | `.github/workflows/build.yml` on `windows-latest` |
| Debian/Ubuntu local mode | self-contained `.deb` built from the PyInstaller onedir output | `postinst` separates the install unconditionally on every install and upgrade (see below): a system systemd unit under a dedicated account runs the daemon, an XDG autostart desktop entry runs the companion | `.github/workflows/build.yml` on `ubuntu-latest` |
| Linux Python install | wheel/sdist with `privacyfence-app` console script | operator-managed process or `privacyfence.service` | PyPI publishing workflow |
| Linux org mode | Python/system service behind the configured reverse proxy and identity provider | operator-managed service | release smoke coverage in the build/test suite |

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
called from `daemon_main.main()`) is the fallback for an install that reached a running state some
other way — a copied app bundle, an in-place upgrade from before this ADR — and is asked again on
every start it finds itself still unseparated, not just once: ADR 0003 decision 6 retired the
one-shot marker that used to make a decline permanent, because under that ADR a decline is not a
configuration, it is an unfinished install. `enable`/`disable`/`status` remain available by hand for
inspecting or reversing an install either way — but see ADR 0003 decision 6 below: a **packaged**
build that ends up unseparated anyway refuses to serve, so `disable` stops being a way to keep
running PrivacyFence and becomes only the way to get your data back out from under the service
account first.

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
Desktop keeps finding the daemon. `… status` audits the result; `… disable` reverses it.

Mandatory on every packaged install as of [ADR 0003](adr/0003-separated-installs-only.md) — the
manual `enable`/`disable`/`status` subcommands above still exist for inspecting or reversing an
install by hand; the migration moves live connector OAuth tokens. Linux and Windows separate too
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
- creates a Start Menu entry for the settings UI;
- creates a Task Scheduler entry for user-session startup;
- creates a Start Menu entry for the companion app;
- installs the privilege-separation tool as `privilege-separation.ps1` next to the
  application, with the companion autostart task template it renders (see below);
- **runs `privilege-separation.ps1 enable` itself**, with its own elevated token, as a post-install
  step — ADR 0003 decision 4. That is what starts the daemon (as its service) and the companion,
  and it is also why the Task Scheduler entry above is registered and then left *disabled*: it is
  the unseparated install's autostart, which `privilege-separation.ps1 disable` hands back. A
  failure of this step fails the install;
- removes the scheduled task, the companion task and the privilege-separation service on uninstall;
- preserves the user's PrivacyFence state directory on uninstall.

Optional signing is configured through `CODESIGNTOOL_DIR`, `ES_USERNAME`, `ES_PASSWORD`, `ES_CREDENTIAL_ID`, and `ES_TOTP_SECRET` — see `scripts/build_installer.ps1`'s header comment. Signing goes through SSL.com's eSigner CodeSignTool rather than a local Authenticode `.pfx`, since CA/B Forum's 2023 key-storage rules mean code-signing private keys can no longer be exported to a portable `.pfx` at all.

### Privilege separation (mandatory)

The same change as macOS's and Linux's, in Windows' own primitives, and the one platform where
those primitives are genuinely different rather than differently spelled. Without it, the daemon
starts in the logged-in user's session — the Scheduled Task above — which is also the session the AI
client it governs runs in; per [ADR 0003](adr/0003-separated-installs-only.md) decision 4 the
installer now runs this itself, elevated, as an install step, so an ordinary install never ends up
in that state. The same command remains available for inspecting or re-running it by hand, from an
**elevated** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" enable
```

(A source checkout runs the same file as `scripts/windows_privilege_separation.ps1`; `... status`
audits the result, `... disable` reverses it — and, per ADR 0003 decision 6, stops being a way to
keep the daemon running on a packaged build: it refuses to serve once it finds no marker.)

That creates a **virtual service account** (`NT SERVICE\PrivacyFence` — materialized by the Service
Control Manager along with the service, with its own SID and no password anyone has to manage, in
preference to the shared `LocalService` #428 mentions), moves the data directory from
`%LOCALAPPDATA%\PrivacyFence` to `%ProgramData%\PrivacyFence` owned by it, and inverts the startup
wiring — a **Windows service** (`PrivacyFence`, `sc.exe`-registered, with its own `sc failure`
crash-restart) runs the daemon with no desktop session at all, while a **Scheduled Task**
(`PrivacyFenceCompanion`, from `installer/windows/privacyfence-companion-task.xml.tmpl`) runs the
companion tray app in each user session. The installer's own `PrivacyFence` task is *disabled*
rather than deleted, so `disable` can put it back and uninstall still finds it; left enabled it
would start a second daemon as the logged-in user, which on a separated install refuses to start
(`privilege_separation.check_runtime_identity()`) rather than silently seeding a default policy.

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
implicitly whatever its ACL says, and `enable` *moves* the data directory out of `%LOCALAPPDATA%` —
a move preserves ownership, so without this the separated root would be owned by the very account
being excluded, wearing an ACL that account could rewrite with one command and no elevation.
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

**Uninstall order matters here in a way it does not on the other two platforms.** Uninstalling
PrivacyFence removes the service and both tasks but, like `%LOCALAPPDATA%\PrivacyFence` before it,
deliberately leaves `%ProgramData%\PrivacyFence` in place — which on a separated install is a
directory only the (now deleted) service account and `Administrators` could read. Run
`... disable` *before* uninstalling to move the data back under your own account; an administrator
can still recover it afterwards by taking ownership.

The `platform-windows` job in `.github/workflows/tests.yml` runs the full core Python suite on `windows-latest` on every PR, alongside the normal Ubuntu suite. Windows packaging itself (the installer build, silent install/autostart/uninstall) is exercised only by the release build workflow (`build.yml`'s `build-windows` job, tag/`workflow_dispatch`-triggered), not per PR — see "Known open items" below for its current live status.

## Debian/Ubuntu local mode

The local desktop package is defined by `PrivacyFenceApp.linux.spec`, `scripts/build_deb.sh`, `debian/`, and `resources/linux/privacyfence.desktop`.

The `.deb` installs the self-contained application under `/opt/privacyfence`, exposes `/usr/bin/privacyfence-app` and `/usr/bin/privacyfence-companion`, installs application icons, installs an XDG autostart desktop entry under `/etc/xdg/autostart/`, and installs the privilege-separation tool as `/usr/sbin/privacyfence-privilege-separation` with its templates under `/usr/share/privacyfence/` (see below — as of #428 D1, `debian/postinst` runs it automatically, `configure)` case, on every install and upgrade).

The XDG desktop autostart path is separate from the repository's `privacyfence.service`, which is the Python/system-service template rather than the desktop `.deb` startup mechanism.

Package removal does not delete per-user PrivacyFence state from the user's home directory.

### Privilege separation (mandatory)

The same change as macOS's, above, in Linux's own idioms. Without it, the daemon starts in the
logged-in user's session — either of the two startup paths above — which is also the session the AI
client it governs runs in. `sudo privacyfence-privilege-separation enable` turns that off — the
`.deb` installs it under that name in `/usr/sbin`, and a source checkout runs the same file as
`sudo ./scripts/linux_privilege_separation.sh enable`. It
creates a dedicated `privacyfence` system account (`useradd --system`), moves the data directory
from `~/.privacyfence` to `/var/lib/privacyfence` owned by that account, and inverts the startup
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
- **The per-user half** (`enable --auto --for-user "$SUDO_USER"`) — the group membership, and
  migrating that person's existing `~/.privacyfence` — does need to know, and `$SUDO_USER` is the
  only thing a postinst has that can say. It names a resolvable, non-root account when the `.deb`
  was installed via `sudo apt install`/`sudo dpkg -i`, and nothing when root installed it directly
  or an unattended upgrade did. So it stays conditional and stays non-fatal: an unattended install
  still ends up separated, with this one re-runnable step pending, which `... status` reports as
  `PENDING USER` (distinct from `OFF`) and which the companion app closes at the first real login
  session.

A pip/pipx source install has no such postinst hook and is not a packaged build in [ADR
0003](adr/0003-separated-installs-only.md) decision 6's sense (that decision's own "Out of scope" —
it is how the project is developed and how org mode is deployed), so it stays opt-in via the manual
command above. `... disable` remains how to turn it back off on any install — and, per ADR 0003
decision 6, stops being a way to keep a **packaged** daemon running: it refuses to serve once it
finds no marker.

Both pre-Phase-4 startup paths are moved aside rather than left in place: `/etc/xdg/autostart/
privacyfence.desktop` and the `--user` unit each become `.disabled`, because either would start a
second daemon as the logged-in user — which on a separated install refuses to start (see
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
same marker. `… status` audits the result; `… disable` reverses it, restoring both startup paths it
moved aside.

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
directory is `/var/lib/privacyfence` under the service account, and the two pre-separation startup
paths are moved aside, all without anybody having to know this tool exists.

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

- **Windows autostart — now actually works, including real crash-restart, verified end to end by real
  `workflow_dispatch` runs, after a chain of independent bugs, the most recent of which was that the
  installer never actually needed the elevated token its own registration step required (see "a
  related wrinkle" below). The one thing CI structurally cannot cover — the `LogonTrigger`'s own
  firing on a real interactive sign-in — has now been confirmed by hand on a real Windows machine;
  future Windows-autostart-affecting changes should re-confirm it via the Windows human checks
  (below), since no hosted runner can ever produce that coverage itself.** This
  mechanism went through several real, independently-found-and-fixed bugs before
  landing where it is now — see `installer/privacyfence-task.xml.tmpl`'s own header comment and
  `installer/privacyfence.iss`'s `[Code]` section for the full detail — and the early ones are worth
  naming here only because this bullet itself carried wrong theories about them at the time:
  non-elevation was never the cause of *this specific* early chain of bugs (it turned out, much
  later, to be the cause of a different one — see "a related wrinkle" below); nor, in the end, was
  the `/ri`/`/du` and `/RU`-scoping pair of `schtasks /create` CLI-flag bugs this bullet previously
  described as the fix — those flags were superseded entirely once the mechanism moved to a real Task
  Scheduler XML task definition (`schtasks /create /xml`), which is what actually ships today.
  **The real, final blocker in that XML approach** was an `encoding="UTF-8"` declaration in the XML
  prolog: `schtasks.exe` hands the file to MSXML as a Unicode stream already, so a declaration
  claiming UTF-8 contradicted the stream the parser was already on and MSXML rejected the whole
  registration outright (`ERROR: The task XML is malformed. (1,40)::ERROR: unable to switch the
  encoding`) — on every install, silently, until `[Code]` was changed to actually capture and log
  `schtasks`'s own output. Fixed by dropping the encoding declaration entirely. Alongside it, the
  task definition also regained three elements an earlier simplification pass had dropped and that
  turned out to be load-bearing once registration itself started succeeding: `version="1.2"` on the
  root `<Task>` element (the schema version `<RestartOnFailure>` and `<MultipleInstancesPolicy>`
  actually need), `id` on `<Principal>`, and the matching `Context` on `<Actions>` — without that
  id/Context pair, the registered `GroupId` principal is never actually bound to anything that runs.
  **The defect that actually kept Windows autostart from ever working was not in the task at all —
  it was in the daemon, and only a test that let Task Scheduler do the launching could see it.** The
  Windows build is a windowed executable (`PrivacyFenceApp.win.spec`'s `console=False`, since a
  console window flashing up at every sign-in would be a bug of its own), and a windowed process
  started with no console has no standard handles, so CPython sets `sys.stdout`/`sys.stderr` to
  `None`. uvicorn's default log formatter calls `sys.stdout.isatty()` while `uvicorn.Config(...)` is
  being built, so the daemon raised `AttributeError: 'NoneType' object has no attribute 'isatty'`,
  logged `Fatal error`, and exited 1 — before binding its port. Task Scheduler was starting the
  daemon correctly and the daemon was killing itself; the same would have happened to the installer's
  own "launch PrivacyFence now" step and to double-clicking the executable. Nothing caught it because
  every automated start of this app until now — the packaged smoke tests included — ran it from a
  shell with stdout redirected to a file, which is a perfectly valid stream.
  `privacyfence/std_streams.py` now repairs the frozen process's streams before anything else runs
  (the same move as `_daemon_entry.py`'s `SSL_CERT_FILE` fix-up), with
  `tests/unit/test_daemon_std_streams.py` as a per-PR regression test built around the real failing
  call.
  **One more real defect came out of asserting the definition Task Scheduler stored rather than the
  one this repo ships**: `<DisallowStartIfOnBatteries>` and `<StopIfGoingOnBatteries>` both default
  to `true` and the template had never mentioned either, so on a laptop the shipped task would not
  start PrivacyFence at sign-in while on battery, and would stop it the moment the machine was
  unplugged — a privacy gate quietly not running, with the MCP client simply finding no daemon. Both
  are now explicitly `false`, and the contract asserts them with no default fallback.
  **Crash-restart now works, via a different mechanism than the one first shipped — and that first
  attempt's failure is worth keeping here precisely because it looked like a fix and was not one.**
  The task shipped carrying `<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>`,
  added as the Windows analogue of the macOS LaunchAgent's `KeepAlive`/`SuccessfulExit=false` and the
  Linux `.deb`'s systemd `Restart=on-failure`. It was not one, and the first test ever to kill a
  Scheduler-started daemon measured that directly: nothing came back, and Task Scheduler's own
  operational log said why — it logged the dead action as a *success*:

  ```
  Event ID 201: Task Scheduler successfully completed task "\PrivacyFence", instance "{63cf2afb-…}",
                action "…\privacyfence-app.exe" with return code 2147942401.
  ```

  `2147942401` is `0x80070001`, the action's own non-zero exit surfaced as an HRESULT. The setting
  answers a task that fails to *run*, not an action that ran and then died, so it never engaged. That
  measurement was pinned by a deliberately negative test, so the setting could not be re-added and
  re-declared a fix without measuring it again — until a real fix replaced it.
  **Real crash-restart is a repeating `<TimeTrigger>`, and it now ships.** `installer/privacyfence-
  task.xml.tmpl` carries `<TimeTrigger><StartBoundary>2020-01-01T00:00:00</StartBoundary>
  <Enabled>true</Enabled><Repetition><Interval>PT5M</Interval></Repetition></TimeTrigger>` alongside
  the existing `<LogonTrigger>` — a past `StartBoundary` and an indefinite `<Repetition>` so it is
  live without waiting for a sign-in, relaunching the daemon on the next tick after it dies.
  `<RestartOnFailure>` stays in the definition too, for the narrower thing it still does: a faster
  (`PT1M`) retry of a launch failure right at logon, ahead of the `TimeTrigger`'s own next tick.
  `MultipleInstancesPolicy` stays `Parallel`, unchanged: `IgnoreNew` would suppress a redundant tick's
  spawn but also stop a second user's sign-in from ever getting a daemon.
  `privacyfence.daemon_main.run_app()`'s "another instance is already running" path — the normal
  outcome on every tick but the one that actually needed a relaunch, under this design — now logs at
  INFO and exits `0` instead of ERROR/`1`, so Task Scheduler logs a clean success on every ordinary
  tick instead of a failed run forever.

  **ADR 0003 moved crash-restart again, to the service.** Every packaged install is
  privilege-separated now, and on a separated install the daemon is a Windows service rather than
  anything Task Scheduler starts: `scripts/windows_privilege_separation.ps1`'s
  `Install-DaemonService` configures `sc failure PrivacyFence reset= 86400 actions=
  restart/5000/restart/10000/restart/30000`, and that is what answers a crashed daemon. The
  `<TimeTrigger>` above is not dead — it still covers an install that has not been separated yet,
  and it stays in the shipped template for that — but it cannot be what answers on a separated one,
  because `enable` leaves the daemon's own task `Disabled`.
  `test_windows_graphical_session_autostart.py`'s crash-restart test therefore kills the *service's*
  process (`taskkill /f`, so its control handler never runs and the SCM sees a failure rather than
  an orderly stop) and waits for a new service pid. The same module's first test asserts the task is
  `Disabled` by then, which is what rules the `<TimeTrigger>` out as the thing that answered.

  **Measured on a real `windows-latest` runner, not assumed — including the three specific things this
  design could not have gotten right by reading documentation alone**: omitting `<Duration>` inside
  `<Repetition>` really does mean "repeat indefinitely" as Task Scheduler stores it (no `<Duration>` or
  `<StopAtDurationEnd>` appeared in the registered document); a `<TimeTrigger>` does fire for the
  `GroupId` principal, and fires effectively immediately given a `StartBoundary` far in the past — the
  crash-restart test killed the Scheduler-started daemon and saw a new pid, under the same signed-in
  account, well inside its wait window; and a past `StartBoundary` behaves as intended rather than
  being normalized or rejected. The one real bug that first `workflow_dispatch` run found was in the
  test, not the task: the contract required `<TimeTrigger><Enabled>true</Enabled>` verbatim, but Task
  Scheduler normalizes away `<Enabled>` on either trigger when it is `true` (the schema default) — the
  same thing it already does for `<LogonTrigger>`, which the contract already tolerated. Fixed to use
  the same fallback for `<TimeTrigger>`, confirmed by a second, fully green run.
  **One thing stays outside what CI can observe, not the installer: the `LogonTrigger`'s own
  firing** — now confirmed once by hand on a real machine (see above), with the Windows human checks
  in `release-testing.md` as the standing, per-release coverage for it going forward. The test used to claim it drove that, via PowerShell's `Start-Process -Credential`
  (`CreateProcessWithLogonW`) as a stand-in for signing in, and was red on every run because of it:
  `schtasks /query /v` reported the task `Enabled`/`Ready`, scoped to the right group, pointing at the
  right exe, and simply never fired (`Last Result: 267011` / `SCHED_S_TASK_HAS_NOT_RUN`).
  `CreateProcessWithLogonW` creates a logon session but not the Terminal Services *session* logon a
  `LogonTrigger` subscribes to, so the trigger was never evaluated — a limitation of the substitution,
  not a defect in the shipped task definition, and one no task-XML or `[Code]` change could fix.
  Of the three ways out this note used to list unchosen, the first is now taken: the automated
  assertions are narrowed to what a hosted runner can actually prove, and the trigger's own firing is
  covered by the Windows human checks in [`release-testing.md`](release-testing.md) on a machine with
  a real sign-in. RDP loopback would create a genuine session logon but needs an RDP client that can
  run without a desktop of its own, which a hosted runner does not have; retiring the workflow would
  have given up the Scheduler-driven coverage below as well. What CI now proves, every run: the
  definition **Task Scheduler itself stored** (`schtasks /query /xml`, not this repo's template)
  matches the autostart contract element by element; Task Scheduler itself starts the daemon, into the
  real signed-in account's own profile with no injected environment, running as that account, serving
  the full daemon/MCP/approval/audit round trip and ending on "Quit PrivacyFence"; and the
  crash-restart above. The one substitution left is asking Task Scheduler to run the task on demand
  instead of the trigger asking it — everything after that decision (resolving the `Builtin\Users`
  principal to a signed-in member, its `LeastPrivilege` token, its profile, the action launch) is the
  same code path. A group principal runs as a member who is *signed in*, so the signed-in account is
  the only one a hosted runner can have it run for — an attempt with a throwaway account returned
  `ERROR: Access is denied.` The cheap half of the same coverage also runs on every PR, on any OS:
  `tests/unit/test_windows_autostart_task_template.py` holds the shipped template to the same
  contract (`tests/windows_task_contract.py`), so a regression in it no longer waits for a scheduled
  Windows-only workflow to notice. Check `windows-graphical-session.yml`'s own run history for the
  current result rather than trusting this note alone — as of this writing it is green for the first
  time since it was written, with the autostart path exercised end to end.
  **A related wrinkle, previously written up here as "not currently a defect," was in fact a
  defect, and the installer no longer allows it to occur.** `installer/privacyfence.iss` used to be
  `PrivilegesRequired=lowest`, so a silent install with no explicit "Run as administrator" resolved
  `{autopf}` to `{userpf}` — `%LOCALAPPDATA%\Programs\PrivacyFence`, inside the installing account's
  own profile — and launched Setup with an ordinary, non-elevated token. This note used to reason
  that the task's `Builtin\Users` group principal "only composes with a per-machine install" and that
  nothing was wrong on the single-user desktop this product targets, because the installing and
  signing-in accounts are the same one. That reasoning addressed the wrong question: it is about
  which account the `LogonTrigger` fires *for* once the task exists, not about whether registering a
  `LogonTrigger` task at all requires an elevated token in the first place — it does, unconditionally.
  `schtasks /create /xml` registering a task with a `LogonTrigger` needs the `SeCreateGlobalPrivilege`
  user right, which Windows grants by default only to Administrators, `SERVICE`, `LOCAL SERVICE` and
  `NETWORK SERVICE`; a UAC-filtered admin token — the ordinary, non-elevated token an admin account's
  own processes run with by default, exactly what a non-elevated `lowest` install launches Setup
  with — does not carry it, regardless of whether the task's `Principal` names a `GroupId` or the
  calling user's own `UserId`. So `RegisterAutostartTask()` failed with "Access is denied" on every
  non-elevated install, deterministically, not occasionally, and the CI test installing to a
  machine-wide directory (see above) never exercised the failing path at all: `windows-graphical-
  session.yml`'s own module requires `_is_admin()` before it will even run, and `build.yml`'s
  packaged-artifact smoke test happens to run on a hosted runner whose account already carries a full
  admin token with no UAC filtering, so neither ever saw the "Access is denied" a real client-Windows
  non-admin install gets every time.
  This wrinkle first stopped being harmless the day a real non-admin user hit two more bugs stacked
  on top of it (privacyfence/privacyfence#410): `RegisterAutostartTask()`'s own failure was logged to
  the Inno Setup install log only, with the install still reporting success, so a schtasks failure at
  install time was invisible until the next reboot silently left no daemon running; and separately,
  the mcpb shim's own `findDaemonCmd()` self-heal fallback (`daemon.ts`) hardcoded the *admin*
  `%ProgramFiles%\PrivacyFence\` path only, so on the common non-admin install it couldn't find the
  daemon at `%LOCALAPPDATA%\Programs\PrivacyFence\` either, whatever autostart did. Those two were
  fixed at the time: a failed `RegisterAutostartTask()` also raises a dialog (guarded by
  `WizardSilent` so a scripted/silent install never blocks on it), and `findDaemonCmd()` checks both
  Windows install locations, preferring `%ProgramFiles%` but falling back to
  `%LOCALAPPDATA%\Programs`. Neither fix touched the registration failure itself, so the dialog kept
  firing on every non-admin install — including, later, a second real user hitting exactly the same
  dialog, screenshots and all. **The actual fix is `PrivilegesRequired=admin`**: Setup's own manifest
  now requires an elevated token before it runs at all, so `RegisterAutostartTask()` never runs
  without the privilege `schtasks /create /xml` needs for a `LogonTrigger` task, on any account.
  This has not yet been measured on a real non-admin client-Windows machine the way the rest of this
  bullet's history has — check `windows-graphical-session.yml` and `release-testing.md`'s Windows
  human checks for whether that measurement has happened by the time this is read.
  None of this needed a dedicated bullet on its own here for the manual-QA/issue-closure part of it:
  that content now lives in [`release-testing.md`](release-testing.md)'s human-checks list
  (Windows-specific bullets — a real installer run on a clean Windows VM, OAuth loopback, the
  sign-out/sign-in check that covers the `LogonTrigger` above, and a clean Add/Remove Programs
  uninstall) and as a standing comment on
  [privacyfence/privacyfence#121](https://github.com/privacyfence/privacyfence/issues/121) itself
  recording that it stays open until a real tagged release ships the signed installer and that QA
  has run against it — not duplicated here as well.
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
  grants, and that a file *moved* there does not (which is why `enable` runs `icacls /reset` over it
  after the migration). What none of that reaches is the service, the virtual account, or the
  logon-token group membership. The remaining half — enable on a real machine, confirm the daemon comes up under the
  service account, confirm the companion and the MCPB shim still reach it after a logout/login,
  confirm `disable` restores the previous layout with connector tokens intact — is a manual check,
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
