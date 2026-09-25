# Platform support

PrivacyFence runs on your own machine (local mode) on macOS, Windows and Debian/Ubuntu Linux, or
centrally on a Linux server for a whole organization (organization mode). This page is the
reference for what each platform supports and where things live. To install, follow your platform's
guide: [macOS](install-macos.md), [Windows](install-windows.md), [Linux](install-linux.md).

## Support matrix

| Platform | Minimum OS | Download | Daemon runs as | Guide |
|---|---|---|---|---|
| macOS | macOS 13, Apple silicon | `PrivacyFence-<version>.dmg` (holds `PrivacyFence.pkg` and `PrivacyFence.mcpb`) | a LaunchDaemon, as the `_privacyfence` account | [Install on macOS](install-macos.md) |
| Windows | Windows 10 / Windows Server 2016 (x64) | `PrivacyFence-<version>-setup.exe` | the `PrivacyFence` Windows service, as `NT SERVICE\PrivacyFence` | [Install on Windows](install-windows.md) |
| Debian/Ubuntu local mode | glibc 2.38 and systemd 242 (Ubuntu 24.04, Debian 13 or newer, amd64) | `privacyfence_<version>_amd64.deb` | the `privacyfence-daemon` system service, as the `privacyfence` account | [Install on Linux](install-linux.md) |
| Linux Python install | Python 3.11 | `pip install privacyfence` / `pipx install privacyfence` | your own account (not separated) | [Install on Linux](install-linux.md#other-linux-systems-and-arm64) |
| Linux org mode | Python 3.11 | `pip install privacyfence` on a server | a system service you define | [Organization deployment](org-mode-setup-guide.md) |

Each installer refuses a system below its row instead of installing something that cannot start
([ADR 0039](adr/0039-installers-refuse-an-os-below-the-support-matrix.md)): the `.pkg` refuses a
macOS older than 13 and an Intel Mac, the Windows installer refuses anything older than Windows 10,
and `apt` refuses the `.deb` without glibc 2.38 and systemd 242. The `.deb` is built for amd64 only
([ADR 0044](adr/0044-the-deb-declares-only-the-architectures-ci-builds.md)); see
[Install on Linux](install-linux.md#other-linux-systems-and-arm64) for arm64.

Every packaged install is privilege-separated: the daemon runs under its own service account, and
your AI client, which runs as you, cannot read or change your policy, passkeys, audit log or
connector credentials ([ADR 0003](adr/0003-separated-installs-only.md)). A packaged install that
finds itself unseparated refuses to serve. A `pip`/`pipx` or source install is not separated.
[Security and compliance](security-and-compliance.md) explains what separation does and does not
protect.

## Data locations

Your installed PrivacyFence keeps all of its data in one directory:

| Install | Data directory | Policy (`settings.yaml`) | Audit log | Daemon log | `mcp_url` |
|---|---|---|---|---|---|
| macOS | `/Library/Application Support/PrivacyFence` | `authority/config/` | `authority/logs/audit/` | `logs/privacyfence.log`, `logs/launchd.log` | `handoff/` |
| Windows | `%ProgramData%\PrivacyFence` | `authority\config\` | `authority\logs\audit\` | `logs\privacyfence.log` | `handoff\` |
| Debian/Ubuntu `.deb` | `/var/lib/privacyfence` | `authority/config/` | `authority/logs/audit/` | `logs/privacyfence.log` | `handoff/` |
| `pip`/`pipx` install | `~/.privacyfence` (Windows: `%LOCALAPPDATA%\PrivacyFence`) | `authority/config/` | `authority/logs/audit/` | `logs/privacyfence.log` | the data directory itself |
| Source checkout | the repository root | `authority/config/` | `authority/logs/audit/` | `logs/privacyfence.log` | the data directory itself |

Paths in the last four columns are relative to the data directory. On a separated install only
`handoff/` is readable from your own account (and only by members of the service group); everything
else needs `sudo` or an elevated PowerShell. The program files live elsewhere:
`/Applications/PrivacyFenceApp.app` on macOS, `%ProgramFiles%\PrivacyFence\` on Windows,
`/opt/privacyfence` on Linux.

Each additional OS account added to an install ([below](#adding-a-second-account)) gets its own
policy, audit log, passkeys and credentials under `users/os-<id>/` in the same data directory.

## Logs

| Platform | Where to look |
|---|---|
| macOS | `sudo tail -f "/Library/Application Support/PrivacyFence/logs/privacyfence.log"`; startup failures before logging starts are in `logs/launchd.log` next to it |
| Windows | `%ProgramData%\PrivacyFence\logs\privacyfence.log` (elevated) |
| Windows event log | Event Viewer → **Windows Logs → Application**, source **PrivacyFence**; or `Get-WinEvent -FilterHashtable @{ProviderName='PrivacyFence'}`. A start the daemon refused is logged there as `PrivacyFence exited with status 1: <reason>` |
| Debian/Ubuntu | `journalctl -u privacyfence-daemon -f`, or `sudo tail -f /var/lib/privacyfence/logs/privacyfence.log` |

The audit log (who asked for what, what was decided) is a separate record; read it from the
**Audit** page in Settings rather than from disk.

## Start, stop and status

The companion's menu has **Start PrivacyFence…**, **Restart PrivacyFence…** and
**Stop PrivacyFence…** (on Linux, the matching actions of the PrivacyFence Applications-menu
entry). Each asks for an administrator password, then runs the platform command below.

| Platform | Status | Start / stop |
|---|---|---|
| macOS | `sudo launchctl print system/com.privacyfence.daemon` | `sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh daemon start` (or `stop`, `restart`) |
| Windows | `sc.exe query PrivacyFence` | `sc.exe start PrivacyFence` / `sc.exe stop PrivacyFence` (elevated) |
| Debian/Ubuntu | `systemctl status privacyfence-daemon` | `sudo systemctl start privacyfence-daemon` / `sudo systemctl stop privacyfence-daemon` |

To check the whole install, not just the daemon, run the platform's `status` command:

- macOS: `sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh status`
- Windows (elevated PowerShell):
  `powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" status`
- Debian/Ubuntu: `sudo privacyfence-privilege-separation status`

It reports `privilege separation: ON` followed by one line per check. `PENDING USER` and (Windows
only) `PENDING SIGNOUT` are explained in
[Getting started](getting-started.md#troubleshooting-on-every-platform).

On every platform the daemon restarts itself after a crash (launchd `KeepAlive`, the service's
failure actions: restart after 5, 10 and 30 seconds, systemd `Restart=on-failure` after 5 seconds)
but stays stopped once you stop it.

## Linux: desktop dialogs need zenity or kdialog

The companion asks you to confirm some things on your desktop: a first passkey enrollment, a new
recovery code, a sign-in link. On Linux it uses `zenity` or `kdialog`, whichever is installed.
Ubuntu and Debian desktops ship one of them. On a system with neither, a first passkey enrollment is
refused with a message naming them and no recovery code is issued; `sudo apt install zenity` fixes
it. macOS and Windows use built-in dialogs.

## Adding a second account

Each OS account that uses PrivacyFence on a machine gets its own fully separate identity: its own
MCP token, approvals, policy, audit log, passkeys and connector sign-ins
([ADR 0008](adr/0008-one-principal-per-os-user.md)). To add an account, an administrator runs:

- macOS: `sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh enable --for-user <name>`
- Windows: `powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" enable -ForUser <name>`
- Debian/Ubuntu: `sudo privacyfence-privilege-separation enable --for-user <name>`

That account then logs out and back in once, because group membership is only picked up at login.
Adding an account never changes the install's recorded owner
([ADR 0043](adr/0043-the-recorded-owner-is-never-rewritten.md)).

## Unattended `.deb` installs

`apt` installs run through `unattended-upgrades`, a configuration-management tool or a root shell
have no `$SUDO_USER`, so the package cannot tell which person the install is for. It still installs
and separates fully: the service account, the data directory and the system service are set up,
and only adding a person to the `privacyfence` group is left pending (`status` shows
`PENDING USER`). The companion completes that the first time someone logs in to a desktop session,
after an administrator password prompt, or an administrator runs
`sudo privacyfence-privilege-separation enable --for-user <name>` ahead of time. Either way the
person logs out and back in once afterwards.

If the machine half of the setup fails, the package install fails, so `dpkg` never reports an
installed PrivacyFence that is not separated. Upgrades stop the service, install the new files,
and start it again; your data is kept.
