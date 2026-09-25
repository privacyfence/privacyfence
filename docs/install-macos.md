# Install on macOS

Start with [Getting started](getting-started.md) if you haven't: it covers what you need and what
happens after the install.

## Requirements

- macOS 13 or later on an Apple silicon Mac. The installer refuses an older macOS and Intel Macs
  ([Platform support](platform-support.md#support-matrix)).
- An administrator password.
- Claude Desktop, Claude Code, or another MCP client.

## Download and verify

Download `PrivacyFence-<version>.dmg` from
[privacyfence.eu/download](https://privacyfence.eu/download/). The page lists each file's SHA-256;
compare it before opening the file:

```bash
shasum -a 256 ~/Downloads/PrivacyFence-<version>.dmg
```

Stable releases are signed and notarized by Apple, so Gatekeeper opens them without warnings.
Pre-release builds may not be.

## Install

1. Double-click the DMG. It holds two files: **PrivacyFence.pkg** and **PrivacyFence.mcpb**.
2. Double-click **PrivacyFence.pkg** and follow the installer. It asks for your administrator
   password as part of the install and uses it to set everything up at once: it installs
   `/Applications/PrivacyFenceApp.app`, creates the `_privacyfence` service account, moves the
   daemon under that account as a LaunchDaemon, and registers the companion (the menu-bar icon) to
   start in each login session.
3. **Log out and back in once.** The installer adds you to the `_privacyfence` group, and macOS only
   picks up group membership at login. Until then neither the companion nor your AI client can reach
   the daemon.

The installer never fails because of the privilege-separation step. If that step could not finish,
the app is still installed but not yet separated. The next time the daemon starts (the Claude
Desktop extension starts it on such an install), it asks for your administrator password to finish
the setup, and asks again at every start until it is done; until then it does not serve. You can
also finish it by hand with the `enable` command in [Troubleshooting](#troubleshooting).

**Installing over an existing install:** download the newer DMG and run its `PrivacyFence.pkg`.
Your data is kept, and the installer restarts the daemon on the new version. There is nothing to
quit first.

## First start

After you log back in, the PrivacyFence icon appears in the menu bar. The companion opens the
**Passkeys** page for you until you have added a passkey; follow
[the first approval](getting-started.md#your-first-approval) from there.

Double-clicking **PrivacyFence** in `/Applications` opens Approvals through the companion
([ADR 0031](adr/0031-clicking-privacyfence-opens-approvals-through-the-companion.md)). If the
menu-bar icon is not running, that starts it; if it is, you are asked to confirm first.

## Connect Claude Desktop

Double-click **PrivacyFence.mcpb** on the DMG. Claude Desktop opens and offers to install the
extension; accept. There is nothing to configure: the extension finds the running daemon by
itself. On a separated install it never starts the daemon: the daemon belongs to launchd and its
own account. If the daemon is stopped, the extension waits for it and logs that you should choose
**Start PrivacyFence…** from the menu-bar icon.

The first time a tool reads or saves a file outside Claude's own folders, macOS may ask
"Claude would like to access files in your Downloads folder". The extension runs inside Claude, so
macOS attributes the access to Claude. Allow it once per folder.

## Connect Claude Code

Claude Code talks to the daemon's local `/mcp` endpoint with your own bearer token. From your own
account, after logging back in:

```bash
PF_HANDOFF="/Library/Application Support/PrivacyFence/handoff"
claude mcp add --transport http --scope user privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
  --header "Authorization: Bearer $(/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp --print-mcp-token)"
```

`mcp_url` holds `http://127.0.0.1:8765/mcp` unless you changed `web.port`. `--print-mcp-token`
mints your account's token the first time and prints the same one afterwards. Any other
Streamable HTTP MCP client takes the same URL and header. Claude Code cannot read local files
through PrivacyFence the way the Claude Desktop extension does; see
[How it works](how-it-works.md) for how it uploads a file instead.

## Troubleshooting

| What you see | What to do |
|---|---|
| No menu-bar icon after logging back in | Double-click PrivacyFence in `/Applications`. |
| `--print-mcp-token` or the companion cannot reach the daemon | Log out and back in, then run the `status` command below. |
| The daemon is stopped | Menu-bar icon → **Start PrivacyFence…**. |
| You cannot reach the companion's menu at all | `/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp --print-sign-in-link` prints a one-time link; the companion asks you to confirm it before it can approve anything. |

Check the whole install with `status`, and finish or repair the setup with `enable`:

```bash
sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh status
sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh enable
```

Logs, data locations and start/stop commands are in [Platform support](platform-support.md#logs).
Problems common to all platforms are in
[Getting started](getting-started.md#troubleshooting-on-every-platform).

## Uninstall

```bash
sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh uninstall
```

This stops PrivacyFence, removes the LaunchDaemon, the companion and
`/Applications/PrivacyFenceApp.app`, and **keeps your data** in
`/Library/Application Support/PrivacyFence` together with the `_privacyfence` account. Installing
again picks it all back up ([ADR 0042](adr/0042-uninstall-replaces-disable.md)).

## Purge (delete your data)

```bash
sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh uninstall --purge
```

This does everything `uninstall` does and also deletes the data directory (connector sign-ins,
policy, passkeys, audit log) and the `_privacyfence` account and group. It cannot be undone.

The script lives inside the app, and a plain `uninstall` removes the app. To purge after a plain
uninstall, install PrivacyFence again and then run `uninstall --purge`.
