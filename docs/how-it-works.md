# How PrivacyFence works

PrivacyFence sits between an MCP-compatible AI system (Claude Desktop, Claude Code, claude.ai
through an organization deployment) and the services it can reach for you: Gmail, Google Drive,
Calendar, Contacts, Tasks, Apps Script, Slack, Telegram, Salesforce, Jira and Confluence. The AI
system never holds your credentials for those services. It asks PrivacyFence, and PrivacyFence
decides whether the request runs straight away, needs your approval first, or is filtered.

This page explains the moving parts. For installing, see [Getting started](getting-started.md).
For what each tool does and which gate it goes through, see the
[Tools reference](tools-reference.md). For how approvals, rules and filters behave, see
[Approvals and policy](approvals-and-policy.md).

## The pieces

| Piece | What it is | Where you see it |
|---|---|---|
| **Daemon** | The background service. It holds the connector sign-ins, evaluates policy, keeps pending approvals and the audit log, and serves the web pages and the MCP endpoint on one local port (`8765` by default, `web.port`). | Nowhere directly. It runs as a system service on a packaged install. |
| **Companion app** | Your way into the daemon's web pages. On macOS it is a menu-bar icon, on Windows a tray icon; on Linux it is the **PrivacyFence** entry in the applications menu, whose right-click actions open Settings, show a new recovery code, and start, stop or check the service. It opens pages in your browser already signed in, starts and stops the service, and shows your recovery code. | Menu bar, tray, or applications menu. |
| **Web pages** | **Approvals** (`/approvals`), where pending requests wait for you, and **Settings** (`/settings`), where you connect services and manage rules, the privacy filter, passkeys and the audit log. | Your browser, at `http://localhost:8765`. |
| **MCP endpoint** | `/mcp` on the same port, speaking MCP over Streamable HTTP. Every AI system reaches PrivacyFence through it. | Your AI system's connector list. |
| **Claude Desktop extension** | `PrivacyFence.mcpb`, a small Node.js shim Claude Desktop runs. It connects Claude Desktop to `/mcp` and reads and writes local files on the daemon's behalf. | Claude Desktop's extensions. |

The companion menu (macOS and Windows) has a status line, **Open Approvals**, **Open Settings**,
one of **Start PrivacyFence…** / **Restart PrivacyFence…** / **Stop PrivacyFence…** (depending on
whether the service is running), **Service Details…**, **New Recovery Code…** and
**Quit Companion**. Quitting the companion leaves the daemon running.

On a packaged install the daemon runs under its own service account, so the AI system (which runs
as you) cannot read PrivacyFence's credentials, rules or audit log, or approve its own requests. The
layout and the reasons are in [Security and compliance](security-and-compliance.md). Where each
file lives on each platform is in [Platform support](platform-support.md).

### Opening PrivacyFence opens Approvals

Clicking PrivacyFence itself (the app in `/Applications` on macOS, the Start Menu entry on Windows,
the applications-menu entry on Linux) opens **Approvals** through the companion on every platform:

- If a companion is running, it asks you to allow opening the page, then opens it.
- If no companion is running (macOS and Windows), the click starts the tray or menu-bar icon and
  opens Approvals from it, with no extra dialog.

Settings is one click away from the page, or from **Open Settings** in the companion. See
[ADR 0031](adr/0031-clicking-privacyfence-opens-approvals-through-the-companion.md).

If the companion cannot be reached (an SSH session, a tray icon that failed to start), run
`privacyfence-app --print-sign-in-link` in your own terminal. It prints a one-time link to
Approvals. If the companion is running, it asks you to confirm first and the link can approve;
if nothing confirms it, the link is view-only: it shows what is pending but cannot release
anything. The link is used up by its first visit.

## Local mode and organization mode

- **Local mode** is the default. One daemon on your own computer serves you. The web pages and
  `/mcp` listen on `localhost` only, so an AI system on another machine, including claude.ai,
  cannot reach it.
- **Organization mode** is one Linux server that serves many people. People sign in through the
  organization's identity provider, AI systems authorize through PrivacyFence's own OAuth 2.1
  server, and each person's connectors, rules and approvals are kept separate. claude.ai can reach
  PrivacyFence only this way. See [Organization deployment](org-mode-setup-guide.md).

Which mode a daemon runs in is set by the organization config bundle's `mode` key; with no bundle,
or no `mode`, it runs in local mode. See [Configuration reference](configuration-reference.md).

## How an AI system connects

### Claude Desktop: the extension

Install `PrivacyFence.mcpb` into Claude Desktop (the macOS DMG and the Windows installer ship it).
Claude Desktop starts the shim, and the shim:

1. waits until the daemon's `/mcp` endpoint answers;
2. asks the daemon for your MCP token over its local control channel (the `MINT MCP` command);
3. reads the `/mcp` URL from the file the daemon writes when it starts (`mcp_url` in the handoff
   directory; see [Platform support](platform-support.md));
4. relays MCP messages between Claude Desktop and `/mcp`, and handles local file reads and writes
   (see [Files](#files)).

Nothing is edited in Claude Desktop's configuration, and no token is stored in a file you can read.

### Claude Code and other HTTP clients: connect to `/mcp` directly

A client that speaks MCP over Streamable HTTP connects to `/mcp` with a bearer token. Get your token
with `privacyfence-app --print-mcp-token`. It runs the same `MINT MCP` request the shim does and
prints the token on its own line, so you can use it in a command:

```bash
claude mcp add --transport http privacyfence "$(cat "<handoff directory>/mcp_url")" \
  --header "Authorization: Bearer $(privacyfence-app --print-mcp-token)"
```

The handoff directory for each platform is listed in [Platform support](platform-support.md). The
URL is normally `http://localhost:8765/mcp`.

- **One token per OS account.** Each account the install serves gets its own token and its own
  connectors, rules and approvals; the command returns the same token each time you run it. See
  [ADR 0008](adr/0008-one-principal-per-os-user.md).
- The control channel only answers accounts the install serves. If the command cannot reach the
  daemon right after installing, log out and back in first.
- On Windows the program has no console window, so redirect its output to a file to read it. See
  [Installing on Windows](install-windows.md).

In organization mode there is no local token: the AI system registers as an OAuth client, you sign
in through your identity provider in the browser, and the AI system receives its own access token.

### What the AI system is told

When a client connects, PrivacyFence's MCP `initialize` answer includes short instructions: what
PrivacyFence is, that a tool list with no connector tools means the install is not set up yet (not
that PrivacyFence is irrelevant), and to call `privacyfence_status` before the first
PrivacyFence-governed action. It also advertises tool-list change notifications: when you connect,
disable or re-enable a service in Settings, connected clients are told to refresh their tool list.

The tool list holds one tool per enabled, signed-in connector tool, plus the eight `privacyfence_*`
tools below. Every connector tool is advertised with the same MCP annotations: read-only,
non-destructive, idempotent. Those annotations are hints for the client's own interface, not a
security boundary; they stop the client adding a second confirmation in front of PrivacyFence's
real one. The gate is what decides.

## What happens on a tool call

Every connector tool has a **gate**, fixed in code:

- **`auto`**: runs straight away and is recorded in the audit log.
- **`review`** (reads) and **`popup`** (writes): run straight away only if an auto-accept rule
  covers the call. Otherwise PrivacyFence creates a pending approval, shown as a card on
  **Approvals** and, if you allow it, as a browser notification.

The [Tools reference](tools-reference.md) lists every tool's gate. A gated call waits up to
30 seconds (`web.approvals.hold_window_seconds`) for you to decide. If you decide in time, the
call returns its result as usual. If not, it returns straight away with:

```json
{"status": "approval_pending", "approval_id": "…", "url": "http://localhost:8765/approvals/…",
 "expires_at": "…", "pending_count": 1, "binder_url": null, "message": "…"}
```

The `message` tells the AI system to send you the `url` rather than wait silently. Once a request
from you is already pending, later gated calls return this result immediately instead of each
waiting 30 seconds (`web.approvals.adaptive_hold`). When more than one is pending, `binder_url`
points at a page where you can decide them together.

After you approve, the AI system repeats the original call with the same arguments and gets the
result. A pending approval nobody decides expires after 15 minutes
(`web.approvals.pending_ttl_seconds`). The limits are in the
[Configuration reference](configuration-reference.md); cards, the PII check, passkey step-up and
rules are in [Approvals and policy](approvals-and-policy.md).

## PrivacyFence's own tools

PrivacyFence adds eight tools of its own. They are not connector tools and have no gate. Unlike
connector tools, each carries its real MCP annotations.

| Tool | What it does | Required arguments | Annotations |
|---|---|---|---|
| `privacyfence_status` | Reports the mode, whether setup is complete, and each connector's state (`enabled`, `authenticated`, `blocked_by`: `no_org_config`, `not_authenticated` or a short reason), plus what to tell you next. It never returns a sign-in link: in local mode it tells the AI system to send you to the companion's **Open Settings**; in organization mode, to your administrator. | `reason` | read-only, idempotent |
| `privacyfence_check_policy` | Predicts whether a specific call would auto-accept (`auto_accept`), needs a human (`requires_review`), or depends on content it cannot see yet (`unknown`), and names the rule that would match. Makes no call and has no side effects. For `review` tools it always reports that the PII check may still apply. | `connector`, `tool`, `reason` (plus `args`) | read-only, idempotent |
| `privacyfence_list_policy` | Lists every auto-accept rule (id, a readable sentence, verbs, the tools it covers) and the scope catalogue that `privacyfence_propose_policy_change` accepts. | `reason` | read-only, idempotent |
| `privacyfence_propose_policy_change` | Proposes adding, updating or removing a rule. Always opens a confirmation on Approvals that you must accept, and needs the same passkey step-up as changing a rule in Settings. Refused in an unattended session. | `operation` (`add`, `update`, `remove`), `reason` | not read-only, non-destructive, idempotent |
| `privacyfence_await_approval` | Waits for one or more pending approvals and reports each one's status (`pending`, `approved`, `denied`, `expired`, `unknown`), never content. Waits at most 120 seconds per call (default 30). | `approval_ids` | read-only, idempotent |
| `privacyfence_begin_unattended_session` | Declares this connection a scheduled run with nobody watching. See [Unattended sessions](#unattended-sessions). | `reason` | not read-only, non-destructive, idempotent |
| `privacyfence_end_unattended_session` | Clears that declaration for this connection. | `reason` | not read-only, non-destructive, idempotent |
| `privacyfence_create_upload_slot` | Gives a client without the extension a one-time URL to upload a local file to. See [Files](#files). | `filename`, `reason` (plus `size_bytes`) | not read-only, non-destructive, not idempotent |

`reason` is one sentence from the AI system on why it is making the call. It is logged, and it is
self-reported and unverified, exactly like the `reason` every gated connector tool takes.

## Files

Some tools read or write files on your computer: `drive_upload_file` (`local_path`),
`drive_download_file` (`destination_dir`), the `gmail_*_with_attachments` tools, and the Gmail and
Confluence attachment downloads. On a packaged install the daemon runs as its own account and
cannot open files in your home folder, so the file travels a different way depending on the
client ([ADR 0007](adr/0007-local-file-bridge.md),
[ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)):

- **Claude Desktop with the extension.** The shim reads or writes the file as you and passes the
  bytes to or from the daemon.
- **Any other client.** For an upload, the AI system calls `privacyfence_create_upload_slot`, then
  `PUT`s the file to the returned URL (no `Authorization` header: the URL is the one-time
  credential; up to 50 MB, valid for 10 minutes, single use), then passes the returned `upload_id`
  to the tool (`drive_upload_file`'s `upload_id`, or `"upload:<upload_id>"` in `attachments`). For
  a download, the tool returns a one-time link instead of writing to `destination_dir`.
- **An install that is not privilege-separated** (a source checkout, a `pip`/`pipx` install): the
  daemon runs as you and reads and writes the file itself.

- **Organization mode.** Small downloads come back inside the tool result; larger ones become a
  short-lived, one-time link. The limits are organization bundle settings
  (`download_delivery.*`, see [Configuration reference](configuration-reference.md)).

In local mode a downloaded file is held in the daemon's memory before it is handed over, so every
local-mode download is limited to 200 MB by default (`file_bridge.max_download_bytes`).

Uploading or creating a slot approves nothing: the card for the tool that uses the file is where
you decide.

## Which AI system is asking

Several AI systems can share one PrivacyFence. Each card and each audit-log entry names the AI
system that made the request, and says how far that name can be trusted
([ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)).
The audit log records four fields: `agent_id`, `agent_name`, `agent_version` and `agent_source`.

| `agent_source` | Trust | Recorded when |
|---|---|---|
| `oauth_client` | attested | Organization mode only: an administrator has pinned the AI system's OAuth client to a known AI system on the **AI systems** settings page. This is the only attested source. |
| `client_info` | claimed | The name the client itself sent: the MCP handshake's `clientInfo.name`, an unpinned OAuth client's registered name, or a local `agent_overrides` relabel. |
| `""` (empty) | unknown | No usable name. Recorded as unknown, never guessed. |

Two further values, `endpoint` and `override`, are reserved and never recorded.

- A card calls a claimed name exactly that: the header says the caller *says* it is that system and
  marks it **Not verified**, and the rest of the card says "the AI system"
  ([ADR 0036](adr/0036-card-copy-names-the-caller-through-one-placeholder.md)).
- `agent_overrides` in `settings.yaml` maps a client name PrivacyFence does not recognise to a
  known AI system. It changes the label only and is always recorded as `client_info`, because any
  program on your computer can send that name
  ([ADR 0037](adr/0037-a-local-override-is-a-relabel-and-never-attests.md)).
- Attribution is reporting only. The same call gets the same decision, the same rule match and the
  same data whatever name the client gives.
- `privacyfence_*` tool calls are not attributed; their audit entries leave the four fields empty.

## Unattended sessions

A scheduled run (for example a routine that fires overnight) has nobody to answer a card. The AI
system can call `privacyfence_begin_unattended_session` at the start of such a run. For the rest of
that connection:

- a gated call that no auto-accept rule covers is denied immediately instead of waiting, and
  audited as `denied_unattended`, so "no human was asked" is never confused with "a human said
  no";
- so is a call a rule covers but the PII check would still route to a human;
- everything that auto-accepts still auto-accepts; the flag never widens what runs;
- `privacyfence_propose_policy_change` is refused.

The flag ends with `privacyfence_end_unattended_session` or when the connection closes. The tool
works only when the organization config bundle turns it on (`unattended_sessions.enabled`, set with
`build_org_bundle.py --enable-unattended-sessions`); otherwise it returns an error. The flag is
advisory: it changes what happens when nothing authorizes a call, never what is authorized
([ADR 0015](adr/0015-unattended-session-flag-is-advisory-only.md)). Pair it with
`privacyfence_check_policy` to plan which steps can run unattended.
