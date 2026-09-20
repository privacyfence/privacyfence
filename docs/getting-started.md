# Getting started

A step-by-step walkthrough of a first PrivacyFence install, on each platform PrivacyFence ships
for: download, install, connect your AI client, and finish setup so approvals actually work.

Follow one platform section, then the shared
[Finish setup](#finish-setup-every-platform) section below it. Everything here is **local mode** —
PrivacyFence running on your own machine. If your organization handed you a URL instead of an
installer, skip to [Organization mode](#organization-mode-nothing-to-install).

## The shape of the install, on every platform

Different installers, same five moves:

1. **Install the package.** It sets up privilege separation during the install, using the
   administrator password it already asked you for.
2. **Don't open anything yet.** PrivacyFence is a background daemon plus a small companion app;
   both start on their own. There is no main window, and double-clicking the application does not
   open one.
3. **Connect your AI client** — the bundled `.mcpb` extension for Claude Desktop (macOS, Windows),
   or the daemon's local `/mcp` endpoint for an HTTP-capable client such as Claude Code (any
   platform, and the only route on Linux).
4. **Open Settings from the companion** — the menu-bar icon (macOS), the tray icon (Windows), or
   the Applications-menu entry (Linux). This is the only way in: PrivacyFence never hands a
   sign-in link to the AI client it governs, so asking Claude for one gets you pointed back here.
5. **Add a passkey, then connect connectors.** A packaged install approves nothing until a passkey
   is enrolled, and has no tools to offer until at least one connector is authenticated.

## Before you start

| | macOS | Windows | Debian/Ubuntu desktop |
|---|---|---|---|
| Download | `PrivacyFence-<version>.dmg` | `PrivacyFence-<version>-setup.exe` | `privacyfence_<version>_amd64.deb` |
| Needs | an administrator password | administrator rights | `sudo` |
| AI client | Claude Desktop (via `.mcpb`), or Claude Code | Claude Desktop (via `.mcpb`), or Claude Code | Claude Code or another HTTP MCP client — Claude Desktop has no Linux build |
| Sign-in surface | menu-bar icon | tray icon | Applications-menu entry (no tray on Linux) |

All three downloads are at [privacyfence.eu/download](https://privacyfence.eu/download/). Stable
builds are signed (Apple notarization on macOS, Authenticode on Windows); pre-release builds may
not be, depending on what signing credentials were available at build time.

You will also want, before step 5: the accounts you want to govern (Google, Slack, Salesforce,
Atlassian, Telegram), a passkey authenticator (Touch ID, Windows Hello, a phone or a security
key), and — if your IT administrator gave you one — the organization configuration bundle file.

---

## macOS, step by step

### 1. Download and open the disk image

Download `PrivacyFence-<version>.dmg` and double-click it. The image holds exactly two files:
**PrivacyFence.pkg** and **PrivacyFence.mcpb**.

### 2. Run the installer

Double-click **PrivacyFence.pkg** and follow the installer. It asks for an administrator password
as an ordinary install step; that password is what lets it set up privilege separation right then,
so nothing has to ask you again later.

When it finishes it installs `/Applications/PrivacyFenceApp.app`, runs the daemon under a dedicated
`_privacyfence` service account, and starts the companion app in your session.

### 3. Log out and back in (first install on this account only)

macOS only checks group membership at login. Until you log out and back in once, your session
isn't yet in the `_privacyfence` group, and neither the companion nor your AI client can reach the
daemon. The installer's last screen says the same thing.

### 4. Install the extension into Claude Desktop

Back on the mounted disk image, double-click **PrivacyFence.mcpb**. Claude Desktop opens and
offers to install the extension; accept it. The extension is a thin shim — it finds the running
daemon by itself and starts it if it isn't running, with no config file to edit.

If you use Claude Code rather than Claude Desktop, skip this and see
[Connect Claude Code](#connect-claude-code-or-another-http-mcp-client) instead.

### 5. Open Settings from the menu bar

Click the PrivacyFence icon in the menu bar and choose **Open Settings**. Settings opens in your
browser on its Connectors page, already signed in.

Now continue with [Finish setup](#finish-setup-every-platform).

> **If the menu-bar icon isn't there**, start the companion by hand:
> `/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceCompanion`. If you still can't
> reach its menu, `/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp
> --print-sign-in-link` prints a one-time link — the companion asks you to confirm it before that
> link may approve anything. Check the install with
> `sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh status`.

---

## Windows, step by step

### 1. Run the installer

Download `PrivacyFence-<version>-setup.exe` and run it. It asks for administrator rights — it needs
them to register the autostart task and to set up privilege separation, which it does as part of
the install.

It installs PrivacyFence to `%ProgramFiles%\PrivacyFence\`, registers the daemon as a Windows
service under a virtual account, starts it immediately, and starts a tray companion in your
session. On a privilege-separated install your PrivacyFence data lives under
`%ProgramData%\PrivacyFence\`.

### 2. Install the extension into Claude Desktop, from the installer's last page

Leave the checkbox on the final page checked and click **Finish**. What it does depends on your
machine:

- **"Install PrivacyFence into Claude Desktop"** — Claude Desktop opens and offers to install the
  extension. Accept it.
- **"Show the PrivacyFence Claude Desktop extension in File Explorer"** — Windows has no working
  `.mcpb` file association. This happens even with Claude Desktop installed, because its own
  installer doesn't always register that association cleanly. From the File Explorer window that
  opens, either double-click `PrivacyFence-<version>.mcpb` (after installing Claude Desktop if you
  haven't), or drag it onto Claude Desktop's **Settings → Extensions** page — that works with or
  without the association — or right-click it → **Open With** → **Claude** → check *Always use this
  app* to fix the association for next time.

To do this later, or if you unchecked the box: paste `%ProgramFiles%\PrivacyFence\` into File
Explorer's address bar, press Enter, and act on the `.mcpb` file there.

### 3. Sign out and back in

Like macOS, Windows evaluates group membership when you sign in. Sign out and back in once so your
session is in the `PrivacyFenceUsers` group and can reach the daemon's handoff files.

### 4. Open Settings from the tray

Click the PrivacyFence tray icon and choose **Open Settings**. Settings opens in your browser on
its Connectors page, already signed in.

The Start Menu **PrivacyFence** shortcut points at the plain settings URL, which only works once
you are already signed in — it is not the first way in. The tray icon is.

Now continue with [Finish setup](#finish-setup-every-platform).

> **If the tray icon isn't there**, start it from the Start Menu's **PrivacyFence Companion**
> entry. If you still can't reach its menu, the break-glass command prints a one-time sign-in link
> — redirect it to a file, because the Windows build is a windowed process with no console of its
> own:
>
> ```powershell
> & "$env:ProgramFiles\PrivacyFence\PrivacyFenceApp.exe" --print-sign-in-link > "$env:TEMP\pf-link.txt"
> ```
>
> Check the install from an elevated PowerShell with
> `powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" status`.

---

## Debian/Ubuntu, step by step

### 1. Install the package

```bash
sudo apt install ./privacyfence_<version>_amd64.deb
```

`sudo dpkg -i privacyfence_<version>_amd64.deb` works too — the package declares no dependencies
today, and `apt` simply resolves any future ones. Installing it separates the install itself: the
daemon becomes a system systemd unit running as a dedicated `privacyfence` account, with your data
directory at `/var/lib/privacyfence`.

### 2. Log out and back in

Group membership is evaluated at login here too. The companion also closes a still-pending group
membership the first time you log in, so this step is not optional.

### 3. Connect your AI client

Claude Desktop has no Linux build, so there is no `.mcpb` step on this platform. Point Claude Code
(or another Streamable HTTP MCP client) at the daemon's local `/mcp` endpoint — see
[Connect Claude Code](#connect-claude-code-or-another-http-mcp-client) below.

### 4. Open Settings from the Applications menu

Linux has no tray icon. Open **PrivacyFence** from your applications menu — clicking the entry
opens Approvals; its **Settings** action (right-click the entry, or your desktop's equivalent)
opens Settings. From a terminal, the same thing is:

```bash
privacyfence-companion --action=open-settings
```

Now continue with [Finish setup](#finish-setup-every-platform).

> **Desktop dialogs need `zenity` or `kdialog`.** The companion uses whichever is present to ask
> you to confirm a first passkey enrollment. Every desktop this package targets ships one; a
> stripped-down install may ship neither, and enrollment is then refused with a message naming
> them. `sudo apt install zenity` fixes that.
>
> **Check the install** with `sudo privacyfence-privilege-separation status` and
> `systemctl status privacyfence-daemon`.

---

## Connect Claude Code (or another HTTP MCP client)

This works on every platform, and is the only route on Linux. The daemon writes the URL and the
bearer token your client needs into its handoff directory when it starts.

Where that directory is depends on the platform (all three packaged installs are
privilege-separated):

| Platform | Handoff directory |
|---|---|
| macOS (packaged) | `/Library/Application Support/PrivacyFence/handoff/` |
| Windows (packaged) | `%ProgramData%\PrivacyFence\handoff\` |
| Debian/Ubuntu (packaged) | `/var/lib/privacyfence/handoff/` |
| Source or `pip`/`pipx` run (not separated) | `~/.privacyfence/` (`%LOCALAPPDATA%\PrivacyFence\` on Windows) |

It holds `mcp_url` (e.g. `http://127.0.0.1:8765/mcp`) and `mcp_token`. On macOS or Linux:

```bash
PF_HANDOFF=/var/lib/privacyfence/handoff        # macOS: "/Library/Application Support/PrivacyFence/handoff"
claude mcp add --transport http privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
  --header "Authorization: Bearer $(cat "$PF_HANDOFF/mcp_token")"
```

You must be logged in as the user the install was provisioned for — that group membership is what
makes these two files readable at all. If `cat` reports *Permission denied*, you haven't logged out
and back in since installing.

---

## Finish setup (every platform)

You are now on the Settings **Connectors** page, opened from the companion.

### 1. Add a passkey

A packaged install requires a passkey before it releases **any** approval, and a fresh install has
none — so until you add one, PrivacyFence approves nothing and every page says so. The companion
opens the Passkeys page for you at each start until one is enrolled; you can also reach it at
`/security` from that banner.

**Write down the one-time recovery code it shows you.** It is the only way back from a lost
authenticator — local mode has no identity provider to fall back on. The companion can issue a
fresh code later (its **New Recovery Code…** menu item), but it can never re-show that one.

### 2. Install your organization configuration, if you were given one

On the Connectors page, upload the bundle your IT administrator gave you. It carries the OAuth
application configuration some connectors need, plus any PII policy and auto-accept rules your
organization sets. Skip this if nobody gave you one.

### 3. Connect the connectors you want

Each connector on that page has its own sign-in flow; the companion opens the provider's sign-in
page in your browser and the resulting credentials stay in the daemon, never in the AI-facing
shim. Per-provider prerequisites (OAuth clients, scopes, workspace approvals) are in the connector
guides:
[Google](google-cloud-setup.md) ·
[Slack](slack-setup.md) ·
[Salesforce](salesforce-setup.md) ·
[Atlassian](atlassian-setup.md) ·
[Telegram](telegram-setup.md).

Your AI client is told the tool list changed as each connector comes online — no reconnect needed.

### 4. Make your first governed request

Ask your AI client for something that goes through a connector ("summarize my last email", say).
What happens next:

- If policy allows it outright, it just runs, and the decision is written to the audit log.
- Otherwise the call comes back pending and PrivacyFence relays a link to the approval. Open that
  link, or open **Approvals** from the companion, and you'll see the full card: what is being
  asked for, what the AI would receive, any detected PII, and the reason the AI gave.
- Decide. The list gives you **Deny** on the row and **Review →** for the full card; there is
  deliberately no one-click Allow on the list.

Keeping the Approvals tab open gets you browser notifications for new requests.

---

## Check the install is working

1. **The companion is there** — menu-bar icon (macOS), tray icon (Windows), Applications-menu entry
   (Linux).
2. **Settings opens from it**, signed in, without you pasting a link.
3. **Your AI client sees PrivacyFence.** Ask it to check PrivacyFence's status; it calls the
   `privacyfence_status` tool and reports the mode, whether setup is complete, and which connectors
   are authenticated or blocked. An empty or partial tool list means this install isn't set up yet
   — not that there is nothing to do.
4. **Privilege separation reports as on** — the `status` command for your platform, in the platform
   section above.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| Claude offers no PrivacyFence tools at all | The extension isn't installed, or the daemon isn't running. Re-check step 4 (macOS) / 2 (Windows), or ask the client for `privacyfence_status`. |
| Claude lists only `privacyfence_*` tools | No connector is authenticated yet. Finish setup, step 3. |
| Claude says it can't give you a sign-in link | Working as designed. PrivacyFence never issues a session to the program it governs — use the companion. |
| Nothing gets approved; every page warns about it | No passkey is enrolled. Finish setup, step 1. |
| *Permission denied* reading `mcp_token`, or the companion can't reach the daemon | You haven't logged out and back in since installing. |
| The companion menu is out of reach | Print a one-time sign-in link with the break-glass command for your platform (see the note at the end of each platform section; on Debian/Ubuntu it is `privacyfence-app --print-sign-in-link`) and confirm it at the companion's dialog. |
| Connector sign-in never opens a browser page (Linux) | The `--serve` companion isn't running in your session. Log out and back in, or start it by hand with `privacyfence-companion --serve &`. |
| A first passkey enrollment is refused, naming `zenity`/`kdialog` (Linux) | Install either one. |

---

## Other ways to run PrivacyFence

### From source (development)

Needs **Python 3.11+** and **pip 21.3+**. Point `venv` at a 3.11+ interpreter explicitly rather
than a bare `python3`, which on macOS in particular is often still 3.9:

```bash
git clone https://github.com/privacyfence/privacyfence
cd privacyfence
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

A source install is **not** privilege-separated: the daemon runs as you, and so does the AI client
it governs, which means your policy and audit log are readable and writable by that client. That is
the right trade for development and the wrong one for a laptop — use your platform's package above
for a real install. `pip`/`pipx install privacyfence` plus the repo-root `privacyfence.service`
(`--user` systemd unit) is the same trade. See
[`dev-vs-live-setup.md`](dev-vs-live-setup.md) for keeping a source run and a packaged install out
of each other's way.

### Organization mode (nothing to install)

If you were given a URL like `https://pf.your-org.example.com` rather than an installer, your
organization runs PrivacyFence centrally and there is nothing to install on your machine:

1. Point Claude (Desktop, Cowork, or any Streamable HTTP MCP client) at
   `https://pf.your-org.example.com/mcp` as an MCP server — no `.mcpb`, no token to copy.
2. The first time it connects, Claude's OAuth sign-in redirects you to your organization's identity
   provider. Sign in there.
3. From `https://pf.your-org.example.com/connect`, reached through that same sign-in, authenticate
   the connectors you want.

Org mode has no local Settings surface, no passkey enrollment of its own, and no sign-in link
concept — every session is an identity-provider authentication. Deploying it is
[`org-mode-setup-guide.md`](org-mode-setup-guide.md).

---

## Where to go next

- [Technical reference](TECHNICAL_REFERENCE.md) — the MCP endpoint, meta-tools, approval model,
  configuration, packaging details.
- [Platform support](platform-support.md) — what each platform's packaging does, and its known open
  items.
- [Security and compliance](security-and-compliance.md) — the trust boundary, privilege separation,
  what a passkey buys.
- [Approval window content reference](approval-window-content-reference.md) — what each approval
  dialog shows.
- ["Always allow" reference](always-allow-rules-reference.md) — what clicking **Always allow**
  actually grants.
