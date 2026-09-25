# Install on Linux

Start with [Getting started](getting-started.md) if you haven't: it covers what you need and what
happens after the install. This page covers the `.deb` for Debian and Ubuntu desktops; for other
distributions and for arm64, see [Other Linux systems and arm64](#other-linux-systems-and-arm64).

## Requirements

- Ubuntu 24.04 or Debian 13 or newer (glibc 2.38 and systemd 242), on amd64
  ([Platform support](platform-support.md#support-matrix)). `apt` refuses the package on anything
  older, and there is no arm64 `.deb`.
- `sudo`.
- `zenity` or `kdialog` for the companion's dialogs. Ubuntu and Debian desktops ship one;
  see [Platform support](platform-support.md#linux-desktop-dialogs-need-zenity-or-kdialog).
- Claude Code or another Streamable HTTP MCP client. Claude Desktop has no Linux build, so there
  is no `.mcpb` step on Linux.

## Download and verify

Download `privacyfence_<version>_amd64.deb` from
[privacyfence.eu/download](https://privacyfence.eu/download/). The page lists each file's SHA-256;
compare it before installing:

```bash
sha256sum ~/Downloads/privacyfence_<version>_amd64.deb
```

## Install

1. Install the package with `sudo`, from your own account:

   ```bash
   sudo apt install ./privacyfence_<version>_amd64.deb
   ```

   The package puts the self-contained application in `/opt/privacyfence` (it needs no Python
   packages from the system) and sets up privilege separation while it installs: it creates the
   `privacyfence` system account, keeps the data in `/var/lib/privacyfence`, runs the daemon as the
   `privacyfence-daemon` **system** service, and adds you (the `sudo` user) to the `privacyfence`
   group. Nothing is stored under `~/.privacyfence`. If this setup fails, the package install fails.
2. **Log out and back in.** Group membership is only picked up at login; until then neither the
   companion nor your AI client can reach the daemon.

At each desktop login, an autostart entry starts the companion (`privacyfence-companion --serve`)
in your session. It is what opens connector sign-in pages in your browser. The daemon itself does
not autostart with your session: systemd runs it at boot, whether or not anyone is logged in.

**Installing over an existing install:** install the new `.deb` the same way. The package stops the
service, replaces the program files and starts it again. Your data is kept.

Installed without `sudo` (as root, or by `unattended-upgrades` or a management tool), the package
cannot tell who it is for; see
[Unattended `.deb` installs](platform-support.md#unattended-deb-installs).

## First start

Linux has no tray icon. The **PrivacyFence** entry in your Applications menu is the companion:

- Clicking it opens Approvals; you are asked to confirm first
  ([ADR 0031](adr/0031-clicking-privacyfence-opens-approvals-through-the-companion.md)).
- Right-clicking it (or your desktop's equivalent) offers **Settings**,
  **New PrivacyFence recovery code**, **PrivacyFence service status**, **Start PrivacyFence**,
  **Restart PrivacyFence**, **Stop PrivacyFence** and **Quit PrivacyFence**.

From a terminal, `privacyfence-companion --action=open-settings` opens Settings. After you log back
in, the companion opens the **Passkeys** page until you have added a passkey; follow
[the first approval](getting-started.md#your-first-approval) from there. The passkey has to come
from an authenticator your browser offers as built into the device; PrivacyFence does not ask
the browser for a USB security key.

## Connect Claude Code

From your own account, after logging back in:

```bash
PF_HANDOFF=/var/lib/privacyfence/handoff
claude mcp add --transport http --scope user privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
  --header "Authorization: Bearer $(privacyfence-app --print-mcp-token)"
```

`mcp_url` holds `http://127.0.0.1:8765/mcp` unless you changed `web.port`. `--print-mcp-token`
mints your account's token the first time and prints the same one afterwards. Any other
Streamable HTTP MCP client takes the same URL and header. To give Claude Code a local file, see
[How it works](how-it-works.md).

## Troubleshooting

| What you see | What to do |
|---|---|
| `--print-mcp-token` or the companion cannot reach the daemon | Log out and back in, then run `status` (below). |
| The daemon is stopped | Applications menu → PrivacyFence → **Start PrivacyFence** (asks for your password), or `sudo systemctl start privacyfence-daemon`. The companion also shows a desktop notification when the daemon has been down for more than 15 seconds. |
| The service keeps failing | `journalctl -u privacyfence-daemon -e` shows why. |
| A connector sign-in never opens a browser page | The `--serve` companion is not running in your session. Log out and back in, or run `privacyfence-companion --serve &`. |
| A first passkey enrollment is refused, naming `zenity`/`kdialog` | `sudo apt install zenity`. |
| Adding a passkey fails with "no built-in passkey authenticator ready to use" | Your browser offers no built-in passkey authenticator on this machine. Use a browser that does. |
| You cannot use the Applications-menu entry | `privacyfence-app --print-sign-in-link` prints a one-time link; the companion asks you to confirm it before it can approve anything. |

Check the whole install with `status`, and repair it with `enable`:

```bash
sudo privacyfence-privilege-separation status
sudo privacyfence-privilege-separation enable
```

Logs, data locations and start/stop commands are in [Platform support](platform-support.md#logs).
Problems common to all platforms are in
[Getting started](getting-started.md#troubleshooting-on-every-platform).

## Uninstall

```bash
sudo apt remove privacyfence
```

This runs `privacyfence-privilege-separation uninstall`: it stops and removes the service and the
program files and **keeps your data** in `/var/lib/privacyfence`, together with the `privacyfence`
account. Installing again picks it all back up
([ADR 0042](adr/0042-uninstall-replaces-disable.md)).

## Purge (delete your data)

```bash
sudo apt purge privacyfence
```

This also deletes `/var/lib/privacyfence` (connector sign-ins, policy, passkeys, audit log) and the
`privacyfence` account and group. It cannot be undone, and it works after a plain `apt remove` too.

## Other Linux systems and arm64

There is no `.deb` for arm64 and no package for other distributions. On those systems you can
install the Python package from PyPI (Python 3.11 or later):

```bash
pipx install privacyfence
privacyfence-app
```

PrivacyFence itself is pure Python, but only amd64 Linux is tested, so on arm64 you are on your
own. Know what this install is before you rely on it:

- **It is not privilege-separated.** The daemon runs as you, and so does your AI client, which can
  therefore read and change your policy and audit log. The data directory is `~/.privacyfence`.
- **Passkey step-up is off by default**, because a passkey store your AI client can write proves
  nothing. You can turn it on in `settings.yaml` ([Configuration reference](configuration-reference.md)).
- To start it at login, copy the repository's `privacyfence.service` to `~/.config/systemd/user/`
  and run `systemctl --user enable --now privacyfence`.

Connect Claude Code as above, reading `mcp_url` from `~/.privacyfence/mcp_url`. Uninstall with
`pipx uninstall privacyfence`; delete `~/.privacyfence` to remove your data.
