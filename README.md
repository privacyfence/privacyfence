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
   Narrow always-allow rules and short-lived approvals reduce repetitive prompts without creating unrestricted access.

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
- Searching Slack, Telegram, or Confluence
- Reading a Jira issue
- Fetching a Salesforce record or report
- Reading a Confluence page or downloading its attachments

Sensitive reads can be shown in full before release to the assistant. PII detection (on by default) scans read content locally, highlights what it found, and requires an extra confirmation when likely personal data is present.

### Actions flowing from the AI

Examples:

- Creating a Gmail draft
- Sending a Slack or Telegram message
- Creating, updating, or deleting calendar events
- Writing to a Drive file, Google Doc, or spreadsheet
- Creating or updating Jira issues
- Creating or updating Confluence pages
- Creating or updating contacts and tasks
- Moving or uploading files

Write cards explain the intended operation and its effect in business terms and require explicit approval before execution.

---

## Human-readable approvals

PrivacyFence translates MCP tool calls into operation-specific approval cards, collected in one approvals list. Users see the target object, relevant metadata, the full content or action, the AI system that asked, and the available decision—not just a raw tool name or JSON payload.

### PII-aware review

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/gmail-read-thread.png" alt="PrivacyFence approval card for reading a Gmail thread, showing the requesting AI system, its stated reason, a possible-PII warning with the matches highlighted in the message text, and what the AI system will receive" width="700">

The PII check runs locally before a read is approved. When likely personal data is detected, PrivacyFence lists the categories found, highlights the matches in the content, and requires an additional confirmation. Every read card also discloses exactly what the AI system will receive, how often the same request has come up this week, which AI system is asking, and the reason it gave (shown as unverified, because it is self-reported).

### Review before an AI action

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/sheets-write.png" alt="PrivacyFence approval card for writing a spreadsheet range, showing the target spreadsheet and range, the effect of the write, the values to be written, the AI system's stated reason, and an informational financial-figures warning" width="700">

Write operations show exactly which object will change, what the change does, and what values will be written, including an informational warning when the AI system's own drafted content appears to contain sensitive figures. For selected high-frequency writes, approving one also allows further calls of the same kind to the same file for 5 minutes, held in memory only.

### Local administration

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/settings-connectors.png" alt="PrivacyFence Settings, Connectors page, showing connector status, org-config requirements, and per-connector authenticate/enable controls" width="700">

PrivacyFence's local web settings page, opened from the companion app, provides access to:

- General settings, including PII detection and passkey step-up
- Connector authentication and status, and the organization configuration
- Always-allow (auto-accept) rules and the privacy filter
- The local audit log
- The AI systems that have connected

Every always-allow rule, across every connector, is managed from a single filterable list -- each rule reads as a plain-language sentence, shows exactly which tools it unblocks, and can be added or removed without leaving the page:

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/settings-auto-accept-rules.png" alt="PrivacyFence Settings, Auto-accept page, showing a filterable list of rules rendered as plain-language sentences with verb chips, and an add-a-rule form" width="700">

---

## Policy-based automation

Not every request needs a popup.

PrivacyFence can automatically approve routine, low-risk operations when a narrowly defined always-allow rule matches. Rules are created from an approval card's **Always allow** button, from the settings page, or proposed by the AI system itself for you to confirm. Rules can use context such as:

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

PII detection takes precedence over read rules. A request that would normally pass silently is returned to human review when likely personal data is found in the content.

Short-lived approval is also available for selected repetitive write operations. These approvals are scoped to the same operation and file, held only in memory, and expire after 5 minutes.

Scheduled tasks can run unattended: the `privacyfence_check_policy` tool lets the AI system check
ahead of time whether a call would be allowed automatically or need a human, and an opt-in
unattended-session mode denies unmatched requests immediately instead of leaving an approval open
for nobody to answer. See [How it works](https://github.com/privacyfence/privacyfence/blob/main/docs/how-it-works.md) and
[Approvals and policy](https://github.com/privacyfence/privacyfence/blob/main/docs/approvals-and-policy.md).

---

## Supported connectors

| Connector | Examples of governed capabilities |
|---|---|
| Gmail | Read messages and threads, download attachments, create drafts and replies (plain text or rich text/HTML, optionally with your signature), create and apply labels, archive messages, create and update filters |
| Google Drive, Docs & Sheets | Read, download, upload, create, move, and write files; write, partially edit, and format (including highlight) Google Docs; add comments; create spreadsheets; read, write, and format Sheets ranges; add and rename tabs; insert and delete rows/columns |
| Google Calendar | Read events, free/busy and rooms, get/set event visibility and color, create, update, and delete events, create out-of-office entries, set working location |
| Google Contacts | Read, search, create, update, and label contacts |
| Slack | List channels, DMs, and group chats (filterable by participant), read channels and threads, search messages, start group chats, send messages |
| Salesforce | Read records, search by name or id, and run reports |
| Jira | Read, create, update, comment on, and transition issues |
| Confluence | Search and read pages, create and update pages; list and download page attachments |
| Google Tasks | Read, create, update, complete, uncomplete, and move tasks |
| Google Apps Script | List projects, read and write script project source; read the result of a run you triggered yourself (PrivacyFence never runs scripts) |
| Telegram | Read chats, search messages, and send messages |

Every tool and the gate it goes through is listed in the [Tools reference](https://github.com/privacyfence/privacyfence/blob/main/docs/tools-reference.md).

---

## Architecture

![PrivacyFence architecture](https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/architecture.svg)

PrivacyFence is one long-running daemon, **`privacyfence-app`**, which owns credentials,
connector clients, policy evaluation, approval cards, PII detection, short-lived approvals, and the
audit log. On a packaged install it runs under a service account of its own (a system daemon on
macOS, a Windows service, a systemd system service on Linux), so the AI client — which runs as you —
cannot read or rewrite its policy, audit key, or connector credentials. A small companion app in
your own session (menu-bar icon on macOS, tray icon on Windows, application-menu entry on Linux)
opens the approvals list and settings in your browser.

The daemon exposes a local, token-authenticated `/mcp` Streamable HTTP endpoint that an MCP client
talks to directly (Claude Code, for example) or through the thin stdio-to-HTTP shim that
`PrivacyFence.mcpb` installs into Claude Desktop. The shim carries no service credentials and no
tool-schema knowledge of its own; it only forwards bytes. In an organization deployment the same
daemon runs once on a server behind HTTPS, and clients (including claude.ai) connect to its `/mcp`
endpoint with OAuth sign-in through the organization's identity provider.

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
                ├── policy and always-allow rules
                ├── PII detection for reads
                ├── human review gate
                ├── short-lived approvals
                ├── audit log
                └── enterprise connectors
```

See [How it works](https://github.com/privacyfence/privacyfence/blob/main/docs/how-it-works.md) for the architecture, the MCP endpoint, the `privacyfence_*` meta-tools, and how the calling AI system is identified.

---

## Who is PrivacyFence for?

PrivacyFence is relevant to:

- CIOs and CTOs introducing AI assistants into business workflows
- CISOs and information-security teams
- AI governance, privacy, risk, and compliance leaders
- Enterprise IT administrators
- Developers building MCP-enabled workflows
- Organizations assessing AI use under GDPR, the EU AI Act, and internal information-handling policies

PrivacyFence is an open-source implementation for macOS, Windows, and Linux rather than a certified compliance product. It can support governance and evidence collection, but it does not by itself make an organization compliant with any regulation.

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

PrivacyFence runs in one of two ways:

- **Local mode** — installed on your own computer, used by an MCP client on that same computer
  (Claude Desktop or Claude Code). Start with [Getting started](https://github.com/privacyfence/privacyfence/blob/main/docs/getting-started.md), then
  follow the page for your platform:
  - [Install on macOS](https://github.com/privacyfence/privacyfence/blob/main/docs/install-macos.md) — `PrivacyFence-<version>.dmg`
  - [Install on Windows](https://github.com/privacyfence/privacyfence/blob/main/docs/install-windows.md) — `PrivacyFence-<version>-setup.exe`
  - [Install on Linux](https://github.com/privacyfence/privacyfence/blob/main/docs/install-linux.md) — `privacyfence_<version>_amd64.deb` (Debian/Ubuntu)
- **Organization mode** — deployed once on a server by an administrator and shared by everyone in the
  organization, with sign-in through the organization's identity provider. This is also the only
  way claude.ai can reach PrivacyFence, because local mode listens on your own machine only. See
  [Organization deployment](https://github.com/privacyfence/privacyfence/blob/main/docs/org-mode-setup-guide.md).

Installers are on [privacyfence.eu/download](https://privacyfence.eu/download/). On every platform
a local install has the same shape:

1. **Run the installer.** It needs administrator rights, and it sets up privilege separation
   during the install: the daemon moves to a service account of its own, and the companion app
   starts in your session. Installing a newer version over an existing one keeps your data.
2. **Sign out and back in once** if this is the first install on your account. The installer adds
   you to PrivacyFence's group, and the operating system only picks that up at the next sign-in.
3. **Connect an MCP client.** On macOS and Windows, install `PrivacyFence.mcpb` into Claude Desktop
   (it ships with the installer). On any platform, an MCP client that speaks Streamable HTTP, such
   as Claude Code, can use the local `/mcp` endpoint directly.
4. **Open PrivacyFence** from the companion app (menu-bar icon on macOS, tray icon on Windows, the
   **PrivacyFence** application-menu entry on Linux) and open Settings. Your AI client cannot do this
   for you: PrivacyFence never gives the program it governs a sign-in link, and when asked it tells
   you to open the companion app.
5. **Connect your services** on the Settings **Connectors** page, after installing the organization
   configuration your IT administrator gave you, if any. See
   [Connecting a service](https://github.com/privacyfence/privacyfence/blob/main/docs/connecting-a-service.md).
6. **Add a passkey when asked.** A packaged install releases no approval until one is enrolled.
   Keep the one-time recovery code it shows you.

Uninstalling keeps PrivacyFence's data so a reinstall picks it up; the purge form deletes it too:

| Platform | Uninstall |
|---|---|
| macOS | `sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh uninstall [--purge]` |
| Windows | **Settings → Apps → PrivacyFence → Uninstall**; tick **Delete PrivacyFence data** to purge. The uninstaller runs `privilege-separation.ps1 uninstall [-Purge]` from the install folder under `%ProgramFiles%\PrivacyFence\`. |
| Linux | `sudo apt remove privacyfence`, or `sudo apt purge privacyfence` to purge. These do what `sudo privacyfence-privilege-separation uninstall [--purge]` does, and remove the package. |

The install pages cover each step in detail, including where data lives, what uninstalling removes,
and what to do when a step does not work.

### Run from source

Needs Python 3.11 or newer and pip 21.3 or newer (editable installs of a `pyproject.toml`-only
project need it).

```bash
git clone https://github.com/privacyfence/privacyfence
cd privacyfence
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

A source install is not privilege-separated: the daemon runs as you, so your policy, audit log and
connector credentials are readable and writable by the same account your AI client runs as. That is
fine for development and for an organization server, and the wrong choice for a laptop — use the
packaged installer there. `pip install privacyfence` from PyPI is the same kind of install.

---

## Documentation

**Using PrivacyFence**

- [Getting started](https://github.com/privacyfence/privacyfence/blob/main/docs/getting-started.md) — what you need, local vs. organization mode, your first approval
- [Install on macOS](https://github.com/privacyfence/privacyfence/blob/main/docs/install-macos.md), [Windows](https://github.com/privacyfence/privacyfence/blob/main/docs/install-windows.md), [Linux](https://github.com/privacyfence/privacyfence/blob/main/docs/install-linux.md)
- [Platform support](https://github.com/privacyfence/privacyfence/blob/main/docs/platform-support.md) — supported OS versions, where data and logs live, starting and stopping
- [Connecting a service](https://github.com/privacyfence/privacyfence/blob/main/docs/connecting-a-service.md) — authenticate, reconnect, and check connector status
- Service setup: [Google](https://github.com/privacyfence/privacyfence/blob/main/docs/google-cloud-setup.md), [Slack](https://github.com/privacyfence/privacyfence/blob/main/docs/slack-setup.md), [Salesforce](https://github.com/privacyfence/privacyfence/blob/main/docs/salesforce-setup.md), [Atlassian](https://github.com/privacyfence/privacyfence/blob/main/docs/atlassian-setup.md), [Telegram](https://github.com/privacyfence/privacyfence/blob/main/docs/telegram-setup.md)
- [Approvals and policy](https://github.com/privacyfence/privacyfence/blob/main/docs/approvals-and-policy.md) — approval cards, the PII check, always-allow rules, the privacy filter, notifications

**Reference**

- [How it works](https://github.com/privacyfence/privacyfence/blob/main/docs/how-it-works.md) — architecture, the MCP endpoint, meta-tools, AI-system attribution, unattended sessions
- [Tools reference](https://github.com/privacyfence/privacyfence/blob/main/docs/tools-reference.md) — every connector tool and its gate
- ["Always allow" per-tool reference](https://github.com/privacyfence/privacyfence/blob/main/docs/always-allow-rules-reference.md) — what clicking Always allow does, tool by tool
- [PII detection keywords](https://github.com/privacyfence/privacyfence/blob/main/docs/pii-detection-keywords.md)
- [Configuration reference](https://github.com/privacyfence/privacyfence/blob/main/docs/configuration-reference.md) — every `settings.yaml` and organization-bundle key

**Operating and reviewing**

- [Organization deployment](https://github.com/privacyfence/privacyfence/blob/main/docs/org-mode-setup-guide.md) — running PrivacyFence centrally for an organization
- [Security and compliance](https://github.com/privacyfence/privacyfence/blob/main/docs/security-and-compliance.md) — deployment model, privilege separation, data handling, GDPR and EU AI Act positioning
- [Security policy](https://github.com/privacyfence/privacyfence/blob/main/SECURITY.md) — reporting a vulnerability
- [Changelog](https://github.com/privacyfence/privacyfence/blob/main/CHANGELOG.md) — what changed in each release
- [Contributing](https://github.com/privacyfence/privacyfence/blob/main/CONTRIBUTING.md)

---

## Status and scope

PrivacyFence has a stable connector and policy interface. It remains under active development, and
you should review the limitations and security model before using it with production or regulated
data.

Tested with Claude Desktop and Claude Code on every install, and with claude.ai through an
organization deployment; any MCP-compatible client can connect. Connector sign-in is per user, and
some connectors need an OAuth application configured by your organization.

---

## License

Apache License 2.0. See [LICENSE](https://github.com/privacyfence/privacyfence/blob/main/LICENSE) and [NOTICE](https://github.com/privacyfence/privacyfence/blob/main/NOTICE).
