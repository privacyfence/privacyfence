# PrivacyFence

**AI access without giving AI the keys.** Approve the sensitive. Automate the routine.

PrivacyFence is an open-source privacy and approval gateway between AI assistants and your business systems. It connects MCP-compatible assistants such as Claude Desktop and Claude Code to Gmail, Google Drive, Calendar, Slack, Salesforce, Jira, Confluence, Telegram, and more.

PrivacyFence enforces, independently of the AI, what an assistant may see and do. Sensitive reads and consequential actions require human approval, while routine requests can be automated by policy. Optional PII detection runs locally before personal data reaches the AI, and every decision is audited.

PrivacyFence runs on an employee’s own computer (macOS, Windows, or Linux) or as a central deployment on infrastructure the organization controls, allowing web clients such as claude.ai to connect as well. Connector credentials stay with PrivacyFence, never with the AI client, and no data passes through PrivacyFence-operated servers — there are none.

**Website:** [privacyfence.eu](https://privacyfence.eu/) · **Download:** [privacyfence.eu/download](https://privacyfence.eu/download/) · **Docs:** [privacyfence.eu/docs](https://privacyfence.eu/docs/)

## Why

Giving an AI assistant access to Gmail, Drive, Slack or Salesforce usually means giving it a
standing permission and trusting it to use that permission well. Being allowed to read a record is
not the same as wanting it sent to an AI, and a tool name in a client's prompt says little about
what is about to change. PrivacyFence puts an independent control point between the assistant and
your systems: the AI asks, PrivacyFence decides, and you see what is at stake before it happens.

## What it does

- **Human approval** for sensitive reads and for writes, on cards that show the actual content or
  change, who is asking and why — not a raw tool name or JSON payload.
- **Local PII detection** before a read reaches the AI: likely personal data is highlighted, and it
  sends the request to a card even when a rule would have let it through.
- **Policy-based automation**: narrow always-allow rules (a sender domain, a Drive folder, a Slack
  channel, a Jira project) let routine requests run without a card.
- **An audit log** of every accepted, denied and automatically approved request, chained so that
  an edit made without its key is detected.
- **Credentials stay with PrivacyFence.** On a packaged install it runs under its own service
  account, so the AI client, which runs as you, cannot read them or approve its own request.
- **A defined set of connectors**, each tool with a gate fixed in code. PrivacyFence is not a
  generic proxy for arbitrary MCP tools.

<img src="https://raw.githubusercontent.com/privacyfence/privacyfence/main/docs/images/screenshots/gmail-read-thread.png" alt="PrivacyFence approval card for reading a Gmail thread, showing the requesting AI system, its stated reason, a possible-PII warning with the matches highlighted in the message text, and what the AI system will receive" width="700">

## How it works

![How a request travels through PrivacyFence: an AI client (Claude Desktop through the extension, Claude Code over HTTP, or claude.ai through an organization deployment) calls PrivacyFence over MCP; PrivacyFence applies policy, the local PII check, human approval and audit, then calls the service with credentials that stay inside it](https://raw.githubusercontent.com/privacyfence/privacyfence/main/website/assets/architecture.svg)

Claude Desktop connects through the PrivacyFence extension (`PrivacyFence.mcpb`); Claude Code and
other clients that speak Streamable HTTP connect to the local `/mcp` endpoint directly; in an
organization deployment, clients such as claude.ai sign in with OAuth through the organization's
identity provider. [How it works](https://privacyfence.eu/how-it-works/) walks one read and one
write through, card by card.

## Platforms

| Install | Runs on |
|---|---|
| macOS (`.dmg`) | macOS 13 or newer, Apple silicon |
| Windows (`-setup.exe`) | Windows 10 / Windows Server 2016 or newer, x64 |
| Linux (`.deb`) | Ubuntu 24.04, Debian 13 or newer, amd64 |
| Organization deployment | A Linux server with Python 3.11 or newer and systemd (`pip install privacyfence`) |

Tested with Claude Desktop and Claude Code on every install, and with claude.ai through an
organization deployment; any MCP-compatible client can connect. See
[Platform support](https://privacyfence.eu/docs/platform-support/).

## Connectors

| Connector | What an AI client can do through it |
|---|---|
| Gmail | Search and read messages, threads and attachments; create drafts and replies, labels, filters; archive. No tool sends email. |
| Google Drive, Docs & Sheets | Search, read, download, upload, move and write files; edit and format Docs; read, write and format Sheets |
| Google Calendar | Read events, free/busy and rooms; create, update and delete events; out-of-office and working location |
| Google Contacts, Tasks | Read, create and update contacts and tasks |
| Google Apps Script | Read and write project source; read the result of a run you started (PrivacyFence never runs scripts) |
| Slack | List and read channels, DMs and threads; search; send messages; start group chats |
| Telegram | Read and search chats; send messages |
| Salesforce | Read records, search, run reports (read-only) |
| Jira | Read, create, update, comment on and transition issues |
| Confluence | Search and read pages and attachments; create and update pages |

[Connectors](https://privacyfence.eu/connectors/) summarizes what each one reviews and which writes
need approval; the [Tools reference](https://privacyfence.eu/docs/tools-reference/) lists every
tool and its gate.

## Quick start

1. Download the installer for your platform from [privacyfence.eu/download](https://privacyfence.eu/download/) and run it.
2. Sign out and back in once, so your account's new group membership takes effect.
3. Add a passkey when the companion app (menu bar, tray, or applications menu) asks, and keep the recovery code.
4. Open **Settings** from the companion and connect your services.
5. Connect Claude Desktop with `PrivacyFence.mcpb`, or Claude Code with the `/mcp` endpoint.
6. Ask your assistant for something, and approve it on the card.

Step by step for each platform: [Getting started](https://privacyfence.eu/docs/getting-started/),
then [macOS](https://privacyfence.eu/docs/install-macos/), [Windows](https://privacyfence.eu/docs/install-windows/)
or [Linux](https://privacyfence.eu/docs/install-linux/). For claude.ai or a whole team, see
[Organization deployment](https://privacyfence.eu/docs/org-mode-setup-guide/).

## Documentation

- [Getting started](https://privacyfence.eu/docs/getting-started/) and [Connecting a service](https://privacyfence.eu/docs/connecting-a-service/)
- [Approvals and policy](https://privacyfence.eu/docs/approvals-and-policy/): cards, the PII check, always-allow rules, the privacy filter
- [How it works](https://privacyfence.eu/docs/how-it-works/): the daemon, the MCP endpoint, PrivacyFence's own tools, unattended sessions
- [Configuration reference](https://privacyfence.eu/docs/configuration-reference/): every `settings.yaml` and organization-bundle key
- [Organization deployment](https://privacyfence.eu/docs/org-mode-setup-guide/): running PrivacyFence centrally
- [Security and compliance](https://privacyfence.eu/docs/security-and-compliance/): trust boundary, privilege separation, audit log
- All published docs: [privacyfence.eu/docs](https://privacyfence.eu/docs/). Contributing: [CONTRIBUTING.md](https://github.com/privacyfence/privacyfence/blob/main/CONTRIBUTING.md). Changes per release: [CHANGELOG.md](https://github.com/privacyfence/privacyfence/blob/main/CHANGELOG.md).

## Limitations

PrivacyFence is independent open-source software, not a certified compliance product. It has no
certification, business-continuity plan or SLA, and does not by itself make a deployment compliant
with any regulation. It does not protect against root or a local Administrator, or against local
code on an install that is not packaged (a source checkout or a `pip` install). A process running
as you can read the local review screen, though not approve from it, and in local mode the name an
AI client gives is never verified. The full list is
[What PrivacyFence does not claim](https://privacyfence.eu/docs/security-and-compliance/#what-privacyfence-does-not-claim).
To report a vulnerability, see [SECURITY.md](https://github.com/privacyfence/privacyfence/blob/main/SECURITY.md).

## License

Apache License 2.0. See [LICENSE](https://github.com/privacyfence/privacyfence/blob/main/LICENSE) and [NOTICE](https://github.com/privacyfence/privacyfence/blob/main/NOTICE).
