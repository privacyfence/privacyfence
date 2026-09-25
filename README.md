# PrivacyFence

**Human control and policy enforcement for AI access to enterprise data.**

PrivacyFence is a local enterprise AI governance layer for macOS, Windows, and Linux. It sits between an MCP-compatible AI assistant and the business systems it can access, so data reads and actions are reviewed, governed, and logged before they are executed.

Instead of granting an AI assistant broad, persistent access and relying on the assistant to use it safely, PrivacyFence applies an independent control point:

- **Human approval** for sensitive reads and actions
- **Policy-based automation** for routine, low-risk requests
- **PII detection** before personal data enters the AI context
- **Audit logging** for accepted, denied, and automatically approved requests
- **Connector-level control** across common enterprise systems
- **Local credential ownership**: credentials remain in the PrivacyFence daemon, not in the AI-facing shim

> PrivacyFence runs on macOS, Windows, and Linux and integrates with Claude through MCP. Its governance model is designed around a broader problem: controlling how AI assistants access and act on enterprise information.

---

## Why PrivacyFence?

Organizations are rapidly adopting AI assistants, but conventional access-control models were designed for people and applications—not autonomous agents that can independently search, retrieve, combine, and modify information across multiple systems.

Granting an assistant access to Gmail, Drive, Slack, Salesforce, Jira, or other business tools can create a new gap between authorization and intent:

- A user may be authorized to access a record, but may not want that record sent to an AI.
- A connector may technically permit an action, but the action may still require human judgment.
- Static permissions cannot capture the context of a specific request.
- Native AI-client prompts may show technical tool names without enough business context.
- Broad access can reduce accountability unless every decision is traceable.

PrivacyFence introduces a governance layer between the assistant and enterprise systems. It allows AI to remain useful while keeping access decisions visible, contextual, and accountable.

---

## Governance principles

PrivacyFence is built around five principles:

1. **The AI assistant is not the authorization boundary.**  
   Policy and approval are enforced independently by PrivacyFence.

2. **Humans stay in control of sensitive data and consequential actions.**  
   Users can inspect the actual content or action before approving it.

3. **Routine work should remain efficient.**  
   Narrow auto-accept rules and temporary approvals reduce repetitive prompts without creating unrestricted access.

4. **Every decision should be auditable.**  
   Accepted, denied, and auto-accepted requests are recorded locally.

5. **Governance should enable AI adoption—not block it.**  
   The objective is safe, practical use of AI across real business workflows.

---

## What PrivacyFence governs

PrivacyFence applies policy and review to both directions of an AI workflow.

### Data flowing to the AI

Examples:

- Reading an email or email thread
- Opening a Drive file or Google Doc
- Reading spreadsheet values
- Searching Slack or Telegram
- Reading a Jira issue
- Fetching a Salesforce record or report
- Reading a Confluence page or downloading its attachments

Sensitive reads can be shown in full before release to the assistant. The optional PII detection gate scans read content locally and adds an explicit warning when likely personal data is found.

### Actions flowing from the AI

Examples:

- Creating a Gmail draft
- Sending a Slack or Telegram message
- Creating or updating calendar events
- Writing to a Drive file, Google Doc, or spreadsheet
- Creating or updating Jira issues
- Creating or updating Confluence pages
- Creating or updating contacts and tasks
- Moving or uploading files

Write dialogs explain the intended operation in business terms and require explicit approval before execution.

---

## Human-readable approvals

PrivacyFence translates MCP tool calls into operation-specific review dialogs. Users see the target object, relevant metadata, the full content or action, and the available decision—not just a raw tool name or JSON payload.

### PII-aware review

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/gmail-read-thread.png" alt="PrivacyFence review dialog for a Gmail thread, showing the AI-visibility checklist, a detected-PII warning, and Claude's stated reason for the request" width="611">

The PII gate runs locally before a read is approved. When likely personal data is detected, PrivacyFence highlights the categories found and requires an additional confirmation. Every review dialog also discloses exactly what the AI will receive, how often the item has come up recently, and the reason Claude gave for the request.

### Review before an AI action

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/sheets-write.png" alt="PrivacyFence review dialog for writing a spreadsheet range, showing a detected content warning and Claude's stated reason for the request" width="611">

Write operations show exactly which object will change and what values will be written, including an informational warning when Claude's own drafted content appears to contain sensitive figures. Selected high-frequency operations can receive a narrowly scoped, in-memory **Accept for 5 min** approval.

### Local administration

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/settings-connectors.png" alt="PrivacyFence Settings, Connectors page, showing connector status, org-config requirements, and per-connector authenticate/enable controls" width="700">

An embedded, local-only web page (PrivacyFence Settings) provides access to:

- PII detection
- Organization configuration
- Auto-accept rules
- Connector authentication and status
- The local audit log

Every auto-accept rule, across every connector, is managed from a single filterable list -- each rule reads as a plain-language sentence, shows exactly which tools it unblocks, and can be added or removed without leaving the page:

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/settings-auto-accept-rules.png" alt="PrivacyFence Settings, Auto-accept page, showing a filterable list of rules rendered as plain-language sentences with verb chips, and an add-a-rule form" width="700">

---

## Policy-based automation

Not every request needs a popup.

PrivacyFence can automatically approve routine, low-risk operations when a narrowly defined rule matches. Rules can use context such as:

- sender domain or Gmail label
- file ownership or approved Drive folder
- spreadsheet and tab
- Slack channel
- calendar ownership and attendee scope
- Jira project
- Confluence space
- Salesforce object type or report
- Telegram chat
- Google Tasks list

PII detection takes precedence over read auto-accept rules. A request that would normally pass silently is returned to human review when likely personal data is found in the content.

Temporary approval is also available for selected repetitive write operations. These approvals are scoped to the same operation and file, held only in memory, and expire automatically.

Scheduled Claude Cowork tasks can run unattended: a `privacyfence_check_policy` tool lets Claude
check ahead of time whether a call would auto-accept or need a human, and an opt-in unattended-session
mode denies unmatched requests immediately instead of leaving a popup open for nobody to answer. See
[Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md#scheduled--unattended-cowork-tasks).

---

## Supported connectors

| Connector | Examples of governed capabilities |
|---|---|
| Gmail | Read messages and threads, download attachments, create drafts and replies (plain text or rich text/HTML), manage labels, archive messages, create and update filters |
| Google Drive, Docs & Sheets | Read, download, upload, move, and write files; write, partially edit, and format (including highlight) Google Docs; add comments; read, write, and format Sheets ranges; add and rename tabs; insert and delete rows/columns |
| Google Calendar | Read event details, get/set event visibility, create and update events, create out-of-office entries, set working location |
| Google Contacts | Read, create, update, and label contacts |
| Slack | List channels, DMs, and group chats (filterable by participant), read channels and threads, search messages, send messages |
| Salesforce | Read records, search by name or id, and run reports |
| Jira | Read, create, update, comment on, and transition issues |
| Confluence | Read, create, and update pages; list and download page attachments |
| Google Tasks | Read, create, update, complete, uncomplete, and move tasks |
| Google Apps Script | Read and write script project source; read the result of a run you triggered yourself (PrivacyFence never runs scripts) |
| Telegram | Read chats, search messages, and send messages |

The detailed tool-by-tool privacy matrix is maintained in [Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md#connectors--privacy-matrix).

---

## Architecture

![PrivacyFence architecture](https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/architecture.svg)

PrivacyFence is one persistent macOS daemon, **`privacyfence-app`**, which owns credentials,
connector clients, policy evaluation, approval dialogs, PII detection, temporary approvals, and the
audit log. It exposes a local, token-authenticated `/mcp` Streamable HTTP endpoint that Claude talks
to directly (Claude Code) or through a thin stdio-to-HTTP shim `PrivacyFence.mcpb` installs
(Claude Desktop, which has no built-in way to reach an HTTP MCP endpoint directly). The shim carries
no service credentials and no tool-schema knowledge of its own; it only forwards bytes.

This replaced an earlier design where a small, ephemeral `privacyfence-bridge` Node process, started
by the AI client, forwarded requests over a local TCP loopback socket instead of HTTP — retired once
both transports had shipped for a full release cycle each.

```text
Claude Code          Claude Desktop
     │                     │
     │ MCP over HTTP       │ MCP over stdio
     │                     ▼
     │              stdio<->/mcp shim
     │                     │
     │  local, token-authenticated /mcp
     ▼                     │
     └──────────┬──────────┘
                ▼
         privacyfence-app
                │
                ├── policy and auto-accept rules
                ├── PII detection for reads
                ├── human review gate
                ├── temporary approvals
                ├── audit log
                └── enterprise connectors
```

See [Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md) for the review model, connector matrix, rule catalogue, installation, configuration, `/mcp` design, and MCP annotation rationale.

---

## Who is PrivacyFence for?

PrivacyFence is relevant to:

- CIOs and CTOs introducing AI assistants into business workflows
- CISOs and information-security teams
- AI governance, privacy, risk, and compliance leaders
- Enterprise IT administrators
- Developers building MCP-enabled workflows
- Organizations assessing AI use under GDPR, the EU AI Act, and internal information-handling policies

PrivacyFence is currently an open-source macOS/Linux implementation rather than a certified compliance product. It can support governance and evidence collection, but it does not by itself make an organization compliant with any regulation.

---

## Why existing approaches fall short

| Existing approach | PrivacyFence |
|---|---|
| Block AI access entirely | Enable AI through governed access |
| Trust the AI client as the final control point | Enforce policy in an independent local daemon |
| Use static, broad permissions | Evaluate each operation in context |
| Show generic technical prompts | Present business-aware previews |
| Provide limited decision history | Maintain a local audit trail |
| Reapprove every routine request | Allow narrow policy rules and expiring approvals |
| Ignore content sensitivity | Detect likely PII before read content reaches the AI |

---

## Quick start

Every local install (macOS/Windows/Linux) follows the same shape: **install the app (you don't
need to open it yourself), connect an MCP client, ask that client to open PrivacyFence for you,
then authenticate from there.** The platform-specific steps below are that shape applied to each
installer. Centrally managed ("organization mode") deployments follow a different shape — see
[Organization mode](#organization-mode-centrally-managed-deployment) below.

For a fuller walkthrough of the same installs — every click and command, what you should see after
each one, how to connect Claude Code instead of Claude Desktop, and what to do when a step doesn't
work — see [Getting started](https://github.com/privacyfence/privacyfence/blob/main/docs/getting-started.md).

### Install on macOS

1. Download the latest `PrivacyFence-<version>.dmg` from [privacyfence.eu/download](https://privacyfence.eu/download/).
2. Open it and double-click **PrivacyFence.pkg**. The installer puts PrivacyFence in
   `/Applications` and, using the administrator password it asks for as part of the install, sets
   up privilege separation right then (see below) — so nothing has to ask you again later. If this
   is the first PrivacyFence install on this account, log out and back in once when it finishes:
   macOS only checks group membership at login.
3. Install **PrivacyFence.mcpb** into Claude Desktop — it's the other file on the disk image, next
   to the installer. The next time Claude Desktop loads the extension, its shim starts the daemon
   automatically if it isn't already running.
4. Open PrivacyFence's menu-bar icon and choose **Open Settings**. (Ask Claude to set up
   PrivacyFence and it will tell you the same thing: its `initialize` response already tells
   Claude to check `privacyfence_status` before its first governed action, and an un-onboarded
   install answers that with "send the human to the companion". Claude cannot hand you a sign-in
   link — PrivacyFence does not issue one to the program it governs.)
5. Settings opens on its Connectors page: install the organization configuration provided
   by your IT administrator, if any, and authenticate the connectors you want.
6. **Add a passkey when the companion asks.** A packaged install requires one before it will
   release any approval, and a fresh one has none yet — so until you add it, PrivacyFence approves
   nothing and every page says so. The companion opens the Passkeys page for you at each start
   until one is enrolled; you can also reach it at `/security` from that banner. Write down the
   one-time recovery code it shows you: it is the only way back from a lost authenticator, local
   mode has no IdP to fall back on, and the companion can issue a fresh one later but never
   re-show that one.

Stable releases are code-signed and notarized by Apple, so this just works — no Gatekeeper
warnings, no manual quarantine step. Pre-release (alpha/beta/rc) builds might not be, depending on
signing/notarization credential availability at build time. Full installation details are in
[Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md#installation-and-packaging).

**Upgrading is the same as installing:** download the newer DMG and re-run `PrivacyFence.pkg`.
The installer restarts the daemon on the new build as part of the install — there's nothing to
quit first, and no separate upgrade procedure.

**Privilege separation is mandatory on a packaged install:** without it, PrivacyFence's daemon runs
as you — and so does the AI client it governs, which is why that client could otherwise read and
rewrite the policy deciding what it's allowed to do. The installer in step 2 moves the daemon to an
account of its own — which takes the policy, the audit key and the connector credentials out of its
reach — and starts a menu-bar companion so you still have a way in, all while it already has the
administrator password you gave it. (If you install some other way, by copying the app bundle from
another machine say, the daemon asks for it itself, via the standard admin-password dialog, every
time it starts and finds the install unseparated — a decline is an unfinished install, not a
setting, and a packaged build that stays unseparated refuses to serve at all rather than serve a
guarantee it cannot keep.) Run
`sudo ./scripts/macos_privilege_separation.sh enable` any time afterward if you need to re-run it
by hand. To uninstall, run
`sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh uninstall`:
it stops PrivacyFence, removes the app and keeps the data under
`/Library/Application Support/PrivacyFence`; `... uninstall --purge` also deletes the data and the
`_privacyfence` account. Neither moves anything into your home directory. Worth reading
[Security and compliance](https://github.com/privacyfence/privacyfence/blob/main/docs/security-and-compliance.md#privilege-separation-macos-linux-and-windows)
before you run it by hand.

**Downloading or uploading a file may prompt "Claude would like to access files in your
Downloads folder"** the first time a tool saves or reads something outside its own working
directory. This is macOS's normal per-app file-access permission (TCC), attributed to Claude
because the `.mcpb` extension that actually reads/writes the file runs as a child process of
Claude.app — see [Local file bridge](https://github.com/privacyfence/privacyfence/blob/main/docs/security-and-compliance.md#local-file-bridge).
Approve it once and it won't ask again for that folder.

### Install on Windows

1. Download the latest `PrivacyFence-<version>-setup.exe` from [privacyfence.eu/download](https://privacyfence.eu/download/).
2. Run the installer — it asks for administrator rights (it needs them regardless of privilege
   separation, to register the autostart task) and installs PrivacyFence to
   `%ProgramFiles%\PrivacyFence\`. Using that same elevated token, it also sets up privilege
   separation right then (see below) — so nothing has to ask you again later. It registers a Task
   Scheduler task so the companion starts at login, and starts the daemon immediately — no separate
   "install the mcpb first" step is needed to get it running (unlike macOS, above), though you
   still need it installed to talk to Claude Desktop.
3. Install **PrivacyFence.mcpb** into Claude Desktop: on the installer's last page, leave the
   checked box for it checked and click Finish. If Windows already has a working `.mcpb` file
   association (normally set up by Claude Desktop's own installer), that box reads "Install
   PrivacyFence into Claude Desktop" and Claude Desktop opens on its own and offers to install it.
   Otherwise the box instead reads "Show the PrivacyFence Claude Desktop extension in File
   Explorer" and does exactly that. That second case isn't only "Claude Desktop isn't installed
   yet" — Claude Desktop's own installer doesn't always register the `.mcpb` association cleanly
   on Windows the first time, so it can happen even with Claude Desktop already installed. From
   File Explorer, either install Claude Desktop first if you haven't, then double-click the file;
   or, if Claude Desktop is already there but the file still won't open, drag it onto Claude
   Desktop's Settings → Extensions page instead (that always works, association or not), or
   right-click the file → Open With → Claude → check "Always use this app" to fix the association
   for next time. Either way, if you unchecked the box, or need to do this again later, open File
   Explorer yourself: paste the install path from step 2 into the address bar
   (`%ProgramFiles%\PrivacyFence\` or `%LOCALAPPDATA%\Programs\PrivacyFence\`, whichever applies to
   you) and press Enter, then act on the `.mcpb` file there as above.
4. Open PrivacyFence's tray icon and choose **Open Settings**. (Ask Claude to set up PrivacyFence
   and it will tell you the same thing — see the macOS steps above for why it cannot hand you a
   link itself. The Start Menu **PrivacyFence** entry opens Approvals through the same tray
   icon, starting it first if it isn't running.)
5. Settings opens on its Connectors page: install the organization configuration provided
   by your IT administrator, if any, and authenticate the connectors you want.
6. **Add a passkey when the companion asks.** A packaged install requires one before it will
   release any approval, and a fresh one has none yet — so until you add it, PrivacyFence approves
   nothing and every page says so. The companion opens the Passkeys page for you at each start
   until one is enrolled; you can also reach it at `/security` from that banner. Write down the
   one-time recovery code it shows you: it is the only way back from a lost authenticator, local
   mode has no IdP to fall back on, and the companion can issue a fresh one later but never
   re-show that one.

Stable releases are Authenticode-signed; pre-release (alpha/beta/rc) builds might not be, depending
on signing certificate availability at build time. Full installation details, including what
uninstalling does and doesn't remove, are in
[Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md#installation-and-packaging).

**Privilege separation is mandatory on a packaged install:** without it, PrivacyFence's daemon runs
as you — and so does the AI client it governs, which is why that client could otherwise read and
rewrite the policy deciding what it's allowed to do. The installer in step 2 moves the daemon to a
Windows service
running under a virtual account of its own — which takes the policy, the audit key and the
connector credentials out of that client's reach — and starts a tray companion so you still have a
way in, all while it already has the administrator rights it needs to do that. Your data lives
in `%ProgramData%\PrivacyFence\`. Uninstalling keeps it there, so a reinstall picks it up again;
tick **Delete PrivacyFence data** in the uninstaller to remove it as well. The same tool,
`powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" status`
from an elevated PowerShell, is how to inspect the separation by hand (`enable` re-runs it). Worth reading
[Security and compliance](https://github.com/privacyfence/privacyfence/blob/main/docs/security-and-compliance.md#privilege-separation-macos-linux-and-windows)
either way — the migration moves live connector tokens.

### Install from the `.deb` (Debian/Ubuntu desktop)

1. Download the latest `privacyfence_<version>_amd64.deb` from [privacyfence.eu/download](https://privacyfence.eu/download/).
2. `sudo apt install ./privacyfence_<version>_amd64.deb` (a plain
   `sudo dpkg -i privacyfence_<version>_amd64.deb` works too — its only dependencies are the
   minimum glibc and systemd versions, Ubuntu 24.04 or Debian 13 and newer).
3. Log out and back in — PrivacyFence starts automatically at the next graphical login (an XDG
   autostart entry, not a menu icon; there's no window to open, all interaction is through the web
   UI). To start it immediately instead of waiting for that, run `privacyfence-app &`.
4. Connect an MCP client. Claude Desktop has no Linux build, so the `.mcpb`/Claude Desktop route
   used on macOS and Windows doesn't apply here — instead, point an HTTP-capable client (Claude
   Code, for example) directly at the daemon's local, token-authenticated `/mcp` endpoint. See
   [Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md#mcp-endpoint) for connection details.
5. Open **PrivacyFence** from your applications menu and choose its **Open Settings** action.
   (Linux has no tray icon — see ADR 0002 decision 4 — so the menu entry is the way in. Asking
   your MCP client instead gets you pointed back here: PrivacyFence does not issue a sign-in link
   to the program it governs.)
6. Settings opens on its Connectors page: install the organization configuration provided
   by your IT administrator, if any, and authenticate the connectors you want.
7. **Add a passkey when the companion asks** — same as the macOS steps above, and for the same
   reason: a packaged install releases no approval until one is enrolled. Keep the one-time
   recovery code it shows you.

The package ships a self-contained PyInstaller build of the daemon — no `python3-*` packages
required beyond what a normal Debian/Ubuntu desktop already has. `apt remove`/`dpkg -r` stops
PrivacyFence and leaves its config, credentials and audit log in `/var/lib/privacyfence`, so a
reinstall picks them up; `apt purge` deletes them and the `privacyfence` service account. See
[Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md#installation-and-packaging) for the full details, and
[Platform support](https://github.com/privacyfence/privacyfence/blob/main/docs/platform-support.md#debianubuntu-local-mode) for how the
package is built.

`pip`/`pipx install privacyfence` plus the repo-root `privacyfence.service` (`--user` systemd unit)
also works, but is not an answer to "how do I install PrivacyFence on my Linux desktop": it is a
source install, not a packaged one, so it is not privilege-separated and
[ADR 0003](https://github.com/privacyfence/privacyfence/blob/main/docs/adr/0003-separated-installs-only.md)
decision 6's "a packaged install that finds itself unseparated does not serve" backstop does not
apply to it either — it runs, unprotected, with your policy and audit log readable and writable by
the same account the AI client runs as. That is the right trade for local development and for how
org mode is deployed, and the wrong one for a laptop; use the `.deb` above for that. See the same
[Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md#installation-and-packaging) section.

**Privilege separation is mandatory for the `.deb`:** `debian/postinst` separates the install
itself, on every install and upgrade, moving the daemon to a `privacyfence` system account of its
own — a system systemd unit — which takes the policy, the audit key
and the connector credentials out of the AI client's reach. That machine-level move always runs and
a failure of it fails the package install; the one piece that can be left pending is adding *you* to
the `privacyfence` group, when the install can't safely tell who owns it (an unattended upgrade with
no `sudo` session behind it) — the companion app closes that the first time you actually log in.
A source checkout has no such postinst hook and stays opt-in via
`sudo privacyfence-privilege-separation enable` (installed by the `.deb`; from a source checkout
it's `sudo ./scripts/linux_privilege_separation.sh enable`). `... uninstall` stops and unregisters
the service and keeps the data; `... uninstall --purge` also deletes the data and the service
account. Neither moves anything into your home directory. Worth reading
[Security and compliance](https://github.com/privacyfence/privacyfence/blob/main/docs/security-and-compliance.md#privilege-separation-macos-linux-and-windows)
before you run it by hand.

### Run from source

Needs **Python 3.11+** — the plain `python`/`python3` on PATH is often an older system Python
(macOS in particular still ships a 3.9 `python3` on many machines), which builds a venv that then
fails install with *"Package 'privacyfence' requires a different Python: 3.9.6 not in '>=3.11'"*.
Point `venv` at a 3.11+ interpreter explicitly (`python3.11`, `python3.12`, etc. — whichever you
have installed) rather than the bare `python3`.

Also needs pip 21.3+ — this is a `pyproject.toml`-only project (no `setup.py`), and editable
installs of those need pip's PEP 660 support, added in 21.3. An older pip fails with *"File
'setup.py' or 'setup.cfg' not found... editable mode currently requires a setuptools-based
build"*; if you hit that, `pip install --upgrade pip` first.

```bash
git clone https://github.com/privacyfence/privacyfence
cd privacyfence
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

Continue with the organization configuration and connector authentication steps in the [Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md#installation-and-packaging).

### Organization mode (centrally managed deployment)

Everything above is **local mode**: PrivacyFence runs on your own machine, and "the organization
configuration" is an optional config bundle your IT administrator hands you to install yourself.
**Organization mode** is a different deployment shape entirely — PrivacyFence runs once, centrally,
as a service your whole org's users share, with sign-in through your organization's own identity
provider instead of a one-time local link. If you were handed a URL like
`https://pf.your-org.example.com` rather than an installer, this is what you're using.

There's nothing to install on your own machine:

1. An administrator deploys PrivacyFence centrally (one Linux host, reachable at your org's own
   HTTPS hostname) — see the [org mode setup guide](https://github.com/privacyfence/privacyfence/blob/main/docs/org-mode-setup-guide.md) if you're setting
   this up yourself.
2. Point Claude (Desktop, Cowork, or any Streamable HTTP MCP client) at
   `https://pf.your-org.example.com/mcp` as an MCP server — no `.mcpb`, no token to copy.
3. The first time it connects, Claude's own OAuth sign-in redirects you to your organization's
   identity provider. Sign in there the same way you sign in to everything else at your org.
4. From `https://pf.your-org.example.com/connect` (reached via that same sign-in — there is no
   one-time link in this mode to begin with), authenticate the connectors you want.

Organization mode has no local `/settings` surface and no bootstrap-link concept at all — the
companion app's own Open Settings item (above) has nothing to open here, since sign-in is always
through your IdP. See the [org mode setup guide](https://github.com/privacyfence/privacyfence/blob/main/docs/org-mode-setup-guide.md) for the full deployment
walkthrough.

---

## Documentation

- [Getting started](https://github.com/privacyfence/privacyfence/blob/main/docs/getting-started.md) — step-by-step install, AI-client setup, and first-run checks for macOS, Windows, and Debian/Ubuntu
- [Changelog](https://github.com/privacyfence/privacyfence/blob/main/CHANGELOG.md) — what changed in each release, newest first, including how to upgrade from 3.x
- [Technical Reference](https://github.com/privacyfence/privacyfence/blob/main/docs/TECHNICAL_REFERENCE.md) — review model, connectors, policies, installation, configuration, and implementation notes
- [Security, Privacy & Compliance](https://github.com/privacyfence/privacyfence/blob/main/docs/security-and-compliance.md) — deployment model, data handling, organizational controls, GDPR, and EU AI Act positioning
- [Google setup](https://github.com/privacyfence/privacyfence/blob/main/docs/google-cloud-setup.md)
- [Slack setup](https://github.com/privacyfence/privacyfence/blob/main/docs/slack-setup.md)
- [Salesforce setup](https://github.com/privacyfence/privacyfence/blob/main/docs/salesforce-setup.md)
- [Atlassian setup](https://github.com/privacyfence/privacyfence/blob/main/docs/atlassian-setup.md)
- [Telegram setup](https://github.com/privacyfence/privacyfence/blob/main/docs/telegram-setup.md)
- [Org mode setup guide](https://github.com/privacyfence/privacyfence/blob/main/docs/org-mode-setup-guide.md) — Ubuntu server, Caddy, Google identity
- [Org mode operational readiness](https://github.com/privacyfence/privacyfence/blob/main/docs/org-mode-operational-readiness.md) — support/readiness level, backup/restore, upgrade/rollback, persisted-state compatibility, restart and single-daemon availability behaviour
- [Connector QA testing](https://github.com/privacyfence/privacyfence/blob/main/docs/connector-qa.md)
- [Release testing](https://github.com/privacyfence/privacyfence/blob/main/docs/release-testing.md)
- [Approval window content reference](https://github.com/privacyfence/privacyfence/blob/main/docs/approval-window-content-reference.md) — what each approval dialog shows, grouped by dialog shape
- ["Always allow" per-tool reference](https://github.com/privacyfence/privacyfence/blob/main/docs/always-allow-rules-reference.md) — what clicking Always allow does, tool by tool
- [What Claude knows before an approval prompt](https://github.com/privacyfence/privacyfence/blob/main/docs/claude-knowledge-boundary.md)
- [File type support](https://github.com/privacyfence/privacyfence/blob/main/docs/file-type-support.md) — attachment previews and PII scanning
- [PII detection keywords](https://github.com/privacyfence/privacyfence/blob/main/docs/pii-detection-keywords.md)
- [Development vs. installed configuration](https://github.com/privacyfence/privacyfence/blob/main/docs/dev-vs-live-setup.md)
- [Contributing](https://github.com/privacyfence/privacyfence/blob/main/CONTRIBUTING.md)

---

## Status and scope

PrivacyFence has a stable connector and policy interface. It remains under active development, and
you should review the limitations and security model before using it with production or regulated
data.

Current implementation assumptions:

- local daemon and local approval UI
- MCP-compatible AI client
- per-user connector authentication
- organization-provided OAuth application configuration where required

---

## License

Apache License 2.0. See [LICENSE](https://github.com/privacyfence/privacyfence/blob/main/LICENSE) and [NOTICE](https://github.com/privacyfence/privacyfence/blob/main/NOTICE).
