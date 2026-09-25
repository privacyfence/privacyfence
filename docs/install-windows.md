# Install on Windows

Start with [Getting started](getting-started.md) if you haven't: it covers what you need and what
happens after the install.

## Requirements

- Windows 10 or later, or Windows Server 2016 or later, on an x64 PC
  ([Platform support](platform-support.md#support-matrix)). The installer refuses older Windows.
- Administrator rights. The installer is per-machine only: it installs under
  `%ProgramFiles%\PrivacyFence\` and cannot be installed for a single user.
- Windows Hello with a PIN (or fingerprint or face) set up **before** you add your first passkey:
  **Settings → Accounts → Sign-in options**. PrivacyFence only accepts the device's built-in
  authenticator.
- Claude Desktop, Claude Code, or another MCP client.

## Download and verify

Download `PrivacyFence-<version>-setup.exe` from
[privacyfence.eu/download](https://privacyfence.eu/download/). The page lists each file's SHA-256;
compare it before running the file:

```powershell
Get-FileHash "$env:USERPROFILE\Downloads\PrivacyFence-<version>-setup.exe" -Algorithm SHA256
```

Stable releases are Authenticode-signed. Pre-release builds may not be.

## Install

1. Run `PrivacyFence-<version>-setup.exe` and accept the administrator prompt.
2. The installer copies PrivacyFence to `%ProgramFiles%\PrivacyFence\`, then sets up privilege
   separation as part of the install: it creates the `PrivacyFence` Windows service running as the
   `NT SERVICE\PrivacyFence` virtual account, keeps your data in `%ProgramData%\PrivacyFence\`,
   adds you to the `PrivacyFenceUsers` group, registers the `PrivacyFenceCompanion` task that starts
   the tray icon at each sign-in, and starts the service. If this step fails, Setup ends with an
   error showing what the step reported instead of a success screen, and PrivacyFence is not set
   up. The usual cause is an install folder your own account can write to; install to the default
   folder under Program Files.
3. On the last page, leave the Claude Desktop checkbox checked and click **Finish** (see
   [Connect Claude Desktop](#connect-claude-desktop)).
4. **Sign out and back in.** Windows puts group membership into your sign-in session, so until you
   sign out your session is not in `PrivacyFenceUsers` and cannot reach the daemon.

**Installing over an existing install:** run the new installer. It stops PrivacyFence, replaces the
program files and starts it again. Your data is kept.

For a silent or management-tool install run as `SYSTEM`, no person is added to `PrivacyFenceUsers`;
the companion completes that at the first person's sign-in (see `PENDING USER` in
[Getting started](getting-started.md#troubleshooting-on-every-platform)).

## First start

After you sign back in, the PrivacyFence tray icon is running. The companion opens the **Passkeys**
page for you until you have added a passkey; follow
[the first approval](getting-started.md#your-first-approval) from there.

The Start Menu has three entries:

- **PrivacyFence** opens Approvals through the companion, starting the tray icon first if it is not
  running; if it is, you are asked to confirm first
  ([ADR 0031](adr/0031-clicking-privacyfence-opens-approvals-through-the-companion.md)).
- **PrivacyFence Companion** starts the tray icon.
- **Uninstall PrivacyFence**.

## Connect Claude Desktop

The checkbox on the installer's last page reads one of two things:

- **Install PrivacyFence into Claude Desktop**: Claude Desktop opens and offers to install the
  extension. Accept it.
- **Show the PrivacyFence Claude Desktop extension in File Explorer**: Windows has no working
  `.mcpb` file association, which can happen even with Claude Desktop installed. In the File
  Explorer window, drag `PrivacyFence-<version>.mcpb` onto Claude Desktop's
  **Settings → Extensions** page, or right-click it → **Open with** → **Claude** and tick
  *Always use this app*.

To do this later, paste `%ProgramFiles%\PrivacyFence\` into File Explorer's address bar and use the
`.mcpb` file there. The extension finds the running daemon by itself and never starts it: the daemon
is a Windows service. If the service is stopped, the extension waits and logs that you should choose
**Start PrivacyFence…** from the tray icon.

## Connect Claude Code

From PowerShell, signed in as yourself (not elevated):

```powershell
$url = Get-Content "$env:ProgramData\PrivacyFence\handoff\mcp_url"
$token = & "$env:ProgramFiles\PrivacyFence\privacyfence-app.exe" --print-mcp-token
claude mcp add --transport http --scope user privacyfence $url --header "Authorization: Bearer $token"
```

`mcp_url` holds `http://127.0.0.1:8765/mcp` unless you changed `web.port`. `--print-mcp-token`
mints your account's token the first time and prints the same one afterwards. Capture its output as
above: `privacyfence-app.exe` has no console window of its own, so run bare it prints nothing
visible. Any other Streamable HTTP MCP client takes the same URL and header. Claude Code cannot read
local files through PrivacyFence the way the Claude Desktop extension does; see
[How it works](how-it-works.md) for how it uploads a file instead.

## Troubleshooting

| What you see | What to do |
|---|---|
| No tray icon | Start Menu → **PrivacyFence Companion**. |
| The tray icon or `--print-mcp-token` cannot reach the daemon | Sign out and back in. `status` (below) shows `PENDING SIGNOUT` until you do. |
| The service stops within seconds of starting | Read why in Event Viewer → **Windows Logs → Application**, source **PrivacyFence**, or run `Get-WinEvent -FilterHashtable @{ProviderName='PrivacyFence'}`. A refused start reads `PrivacyFence exited with status 1: <reason>`. |
| The daemon is stopped | Tray icon → **Start PrivacyFence…**, or `sc.exe start PrivacyFence` from an elevated prompt. |
| Adding a passkey fails with "no built-in passkey authenticator ready to use (NotAllowedError)" | Windows Hello is not set up. Add a PIN under **Settings → Accounts → Sign-in options**, then try again. |
| Adding a passkey fails with "the passkey prompt was cancelled, timed out, or was blocked (NotAllowedError)" | The Windows Hello prompt was dismissed or timed out. It can open behind the browser window; try again and finish it. |
| "this device already has a passkey enrolled here" | This device's Windows Hello already holds a passkey for PrivacyFence; use it. |
| You cannot reach the tray menu at all | Run `& "$env:ProgramFiles\PrivacyFence\privacyfence-app.exe" --print-sign-in-link > "$env:TEMP\pf-link.txt"` and open the link in that file. The companion asks you to confirm it before it can approve anything. |

Check the whole install from an **elevated** PowerShell with `status`, and repair it with `enable`:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" status
powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" enable
```

Logs, data locations and start/stop commands are in [Platform support](platform-support.md#logs).
Problems common to all platforms are in
[Getting started](getting-started.md#troubleshooting-on-every-platform).

## Uninstall

Uninstall PrivacyFence from **Settings → Apps → Installed apps**, or with the Start Menu's
**Uninstall PrivacyFence**. The uninstaller removes the service, the companion task, the
`PrivacyFence` event log source and the program files, and **keeps your data** in
`%ProgramData%\PrivacyFence\` together with the `PrivacyFenceUsers` group. Installing again picks
it all back up ([ADR 0042](adr/0042-uninstall-replaces-disable.md)).

## Purge (delete your data)

Tick **Delete PrivacyFence data** in the uninstaller (unchecked by default). It also deletes
`%ProgramData%\PrivacyFence\` (connector sign-ins, policy, passkeys, audit log) and the
`PrivacyFenceUsers` group. It cannot be undone. A silent uninstall never deletes data.

After a plain uninstall, an administrator can delete the data by hand: remove
`%ProgramData%\PrivacyFence\` and run `Remove-LocalGroup PrivacyFenceUsers` from an elevated
PowerShell.
