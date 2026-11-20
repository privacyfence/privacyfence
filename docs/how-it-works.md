# How PrivacyFence works

PrivacyFence sits between an MCP-compatible AI system (Claude Desktop, Claude Code, ChatGPT, claude.ai
or Gemini Enterprise) and the services it can reach for you: Gmail, Google Drive,
Calendar, Contacts, Tasks, Apps Script, Slack, Telegram, Salesforce, Jira and Confluence. The AI
system never holds your credentials for those services. It asks PrivacyFence, and PrivacyFence
decides whether the request runs straight away, needs your approval first, or is filtered.

This page explains the moving parts. For setting up, see [Getting started](getting-started.md).
For what each tool does and which gate it goes through, see the
[Tools reference](tools-reference.md). For how approvals, rules and filters behave, see
[Approvals and policy](approvals-and-policy.md).

## The pieces

| Piece | What it is | Where you see it |
|---|---|---|
| **Server** | The PrivacyFence process in the Docker image. It holds the connector sign-ins, evaluates policy, keeps pending approvals and the audit log, and serves the web pages and the MCP endpoint on one port (`8765` by default). | Nowhere directly. It runs as a container. |
| **Web pages** | **Approvals** (`/approvals`), where pending requests wait for you, **Connect** (`/connect`), where you connect services, **Settings** (`/settings`), where you manage rules, the privacy filter, passkeys and the audit log, and **Admin** (`/admin`), where an administrator decides which services and features the server offers. | Your browser, at `http://localhost:8765` for a single-user install, or your organization's address. |
| **MCP endpoint** | `/mcp` on the same port, speaking MCP over Streamable HTTP. Every AI system reaches PrivacyFence through it. | Your AI system's connector list. |

The server runs in its own container, so the AI system (which runs as you, or on another
company's servers) cannot read PrivacyFence's credentials, rules or audit log, and an approval needs
your passkey, which the AI system does not hold. The layout and the reasons are in
[Security and compliance](security-and-compliance.md). What stays in the data volume is in
[Organization deployment with Docker](org-mode-docker.md#what-stays-in-the-data-volume).

## Single-user and organization deployments

PrivacyFence runs in organization mode only. It comes in two shapes of the same image:

- **An organization server** serves many people. People sign in through the organization's identity
  provider, AI systems authorize through PrivacyFence's own OAuth 2.1 server, and each person's
  connectors, rules and approvals are kept separate. claude.ai can reach PrivacyFence only this
  way. See [Organization deployment](org-mode-setup-guide.md). It runs from the
  `ghcr.io/privacyfence/privacyfence-org` Docker image configured by environment variables
  ([Organization deployment with Docker](org-mode-docker.md)).
- **Single-user mode** is the same image with no identity provider: one administrator, served only
  on `localhost`, who signs in with a passkey after a one-time setup code. It is how the Docker image
  starts on your own computer
  ([Run it on this computer](org-mode-docker.md#run-it-on-this-computer)). It is no protection against
  an agent that can run `docker` on that computer
  ([Single-user mode](security-and-compliance.md#single-user-mode)).

The mode is organization mode; `PRIVACYFENCE_MODE`, when set, must be `org`. A desktop (local) install of PrivacyFence 7 is not read by 8: see
[Moving to organization mode](moving-to-organization-mode.md). See
[Configuration reference](configuration-reference.md#environment-variables).

## How an AI system connects

Each AI client has its own setup page. The client discovers PrivacyFence's authorization server from
`/mcp`, registers itself as an OAuth client, you sign in in the browser (through the organization's
identity provider, or with your passkey on a single-user install), and the client receives its own
access token. There is no token to copy.

| AI client | How it connects | Setup |
|---|---|---|
| Claude Desktop | custom connector | [Connect Claude Desktop](connect-claude-desktop.md) |
| Claude Code | OAuth sign-in | [Connect Claude Code](connect-claude-code.md) |
| ChatGPT | Developer Mode app with OAuth sign-in | [Connect ChatGPT](connect-chatgpt.md) |
| claude.ai | custom connector | [Connect claude.ai](connect-claude-ai.md) |
| Gemini Enterprise | custom MCP server data store | [Connect Gemini Enterprise](connect-gemini-enterprise.md) |

claude.ai, ChatGPT and Gemini Enterprise call PrivacyFence from their own servers, so they need an
organization server on a public HTTPS address. Any other MCP client that supports Streamable HTTP
connects the way Claude Code does, with OAuth 2.1 and dynamic client registration.

### Scoped tokens

Instead of its own credential, a client can hold a **scoped token** created in Settings: a named
bearer token with an expiry, its own rules and a list of tools. It sees only those tools,
and a call it makes is auto-accepted only through one of its own rules; otherwise it is denied at
once, with no approval card. A box on the token lets covered Gmail, Slack and Drive file content reads return personal data, which the scan still reports in the audit log. Revoking it affects no other client. The
administrator must turn the feature on. Claude can also propose a token for you to review and create ([ADR 0250](adr/0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md)). See [Scoped tokens](security-and-compliance.md#scoped-tokens).

### What the AI system is told

When a client connects, PrivacyFence's MCP `initialize` answer includes short instructions: what
PrivacyFence is, that a tool list with no connector tools means the install is not set up yet (not
that PrivacyFence is irrelevant), and to call `privacyfence_status` before the first
PrivacyFence-governed action. It also advertises tool-list change notifications: when you connect,
disable or re-enable a service in Settings, connected clients are told to refresh their tool list.

The tool list holds one tool per enabled, signed-in connector tool, plus the `privacyfence_*`
tools below: eight always, `privacyfence_propose_scoped_token` only where token proposals are on (by the administrator's choice), and `privacyfence_runtime_credential_status` where n8n delivery is also set up. Each connector tool carries MCP annotations that say what it does: a read is
read-only and idempotent; a write is neither. Only the three tools that delete something,
`calendar_delete_event` (an event), `drive_sheets_delete_dimensions` (rows or columns of a
spreadsheet) and `peek_delete_page` (a Peek page with its comments and visit history), are marked
destructive; an overwrite is a write, not a deletion.

These annotations are always true. PrivacyFence is a privacy tool and does not tell an AI system
anything false, so there is no setting, header or extension that advertises a write as read-only.

Annotations are hints for the client's own interface, not a security boundary: the gate is what
decides. A client may use them to ask for its own confirmation before a write, in front of
PrivacyFence's approval. If you do not want that second question, tell the client to always allow
PrivacyFence's tools; Claude Desktop and Claude Code both offer it (see
[Connect Claude Desktop](connect-claude-desktop.md#confirmations) and
[Connect Claude Code](connect-claude-code.md#confirmations)). Nothing is less protected when you
do: every call still goes through PrivacyFence's gate, and PrivacyFence's approval card is the
confirmation that decides.

The reasoning is in [ADR 0086](adr/0086-tool-annotations-are-truthful-by-default.md) and
[ADR 0089](adr/0089-tool-annotations-are-always-truthful.md).

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
result; if an argument differs or the content the card showed has changed, the repeat gets a new card. While the approval is pending, the call does nothing: no data is returned, no file is
handed out and no change is made. A pending approval nobody decides expires after 15 minutes
(`web.approvals.pending_ttl_seconds`).

In the audit log, the `approval_pending` row, the row for your decision and, for a download, the
row for the file being fetched share one `request_id`. A decision the AI system never came back for
is logged as `expired` with what you decided and when (`expired_decision`, `decided_at`), so it
reads differently from a card nobody answered
([ADR 0093](adr/0093-a-pending-approval-releases-nothing.md)). The limits are in the
[Configuration reference](configuration-reference.md); cards, the PII check, passkey step-up and
rules are in [Approvals and policy](approvals-and-policy.md).

## PrivacyFence's own tools

PrivacyFence adds eight tools of its own, plus `privacyfence_propose_scoped_token` where token proposals are on (by the administrator's choice) and `privacyfence_runtime_credential_status` where n8n delivery is also set up. They are not connector tools and have no gate. Unlike
connector tools, each carries its real MCP annotations.

| Tool | What it does | Required arguments | Annotations |
|---|---|---|---|
| `privacyfence_status` | Reports the mode, whether setup is complete, and each connector's state (`enabled`, `authenticated`, `blocked_by`: `no_org_config`, `not_authenticated` or a short reason), plus what to tell you next. It never returns a sign-in link: it tells the AI system to send you to the server's Connect page, or to ask your administrator. | `reason` | read-only, idempotent |
| `privacyfence_check_policy` | Predicts whether a specific call would auto-accept (`auto_accept`), needs a human (`requires_review`), or depends on content it cannot see yet (`unknown`), and names the rule that would match. When a call would not auto-accept, `suggested_rules` lists the rules that would cover it (scope, value and verb), derived from the call's own arguments; a value of `null` means it is whatever the call reaches at run time. A value of `[]` means the rule takes none (the Slack rules for mentions of you, your conversations and one-to-one DMs), and `requires_args` names arguments the call must also pass (`member_only` for a search under the conversations rule); Slack is not asked. Scoped tokens get it too. Makes no call and has no side effects. `pii_gate_may_apply` is true for `review` tools. `pii_gate` says whether the PII scan of real content can still stop the call: `none` (it never reaches the scan), `allowed_by_token` (the caller's scoped token lifts it), `allowed_unless_inline` (a Drive download, lifted unless the file comes back inside the result) or `applies`; `pii_gate_detail` says why. | `connector`, `tool`, `reason` (plus `args`) | read-only, idempotent |
| `privacyfence_list_policy` | Lists every auto-accept rule (id, a readable sentence, verbs, the tools it covers) and the scope catalogue that `privacyfence_propose_policy_change` accepts. | `reason` | read-only, idempotent |
| `privacyfence_propose_policy_change` | Proposes adding, updating or removing a rule. Always opens a confirmation on Approvals that you must accept, and needs the same passkey step-up as changing a rule in Settings. Refused in an unattended session. | `operation` (`add`, `update`, `remove`), `reason` | not read-only, non-destructive, idempotent |
| `privacyfence_await_approval` | Waits for one or more pending approvals and reports each one's status (`pending`, `approved`, `denied`, `expired`, `unknown`), never content; a denial's intent and note, when you gave them, come under `denial_feedback` (see [When you deny a call](tools-reference.md#when-you-deny-a-call)). Waits at most 120 seconds per call (default 30). | `approval_ids` | read-only, idempotent |
| `privacyfence_begin_unattended_session` | Declares this connection a scheduled run with nobody watching. See [Unattended sessions](#unattended-sessions). | `reason` | not read-only, non-destructive, idempotent |
| `privacyfence_end_unattended_session` | Clears that declaration for this connection. | `reason` | not read-only, non-destructive, idempotent |
| `privacyfence_propose_scoped_token` | Proposes a scoped token for a routine, built from the calls approved in this session (`from_session`) or from rules and tools it names. Returns a review link at once and nothing else: you review and can only narrow it in **Settings → Scoped tokens**, confirm by typing its name, and copy the token yourself. Claude never receives the token or learns whether it was created. With `deliver_to: {runtime: "n8n", workflow_id}` (servers with n8n delivery set up) PrivacyFence instead puts the token into n8n itself once you confirm. Not offered to a scoped token, refused in an unattended session. See [ADR 0250](adr/0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md). | `reason`, `label` | not read-only, non-destructive, not idempotent |
| `privacyfence_runtime_credential_status` | Reports whether a proposal made with `deliver_to` was delivered to n8n, and the credential to use (`credential_id`, `credential_name`, `credential_type`, `mcp_url`, `token_expires_at`, and `node` for a [PrivacyFence node](n8n-node.md) credential); `pending` while you have not decided, `expired` (with `token_expired_at` and a message asking the person to extend the token) once the token has lapsed, `not_delivered` otherwise. Never returns the token. Only for an organization server with n8n delivery; not offered to a scoped token. See [ADR 0280](adr/0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md). | `proposal_id`, `reason` | read-only, non-destructive, idempotent |
| `privacyfence_create_upload_slot` | Gives a client a one-time URL to upload a file to. See [Files](#files). | `filename`, `reason` (plus `size_bytes`) | not read-only, non-destructive, not idempotent |

`reason` is one sentence from the AI system on why it is making the call. It is logged, and it is
self-reported and unverified, exactly like the `reason` every gated connector tool takes.

## Files

Files move through PrivacyFence in three ways
([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)). The tools that take a file
are `drive_upload_file`, the `gmail_*_with_attachments` tools and the other connectors' upload tools;
they accept an `upload:<id>` reference, never a path on your computer.

- **Upload.** The AI system calls `privacyfence_create_upload_slot`, then `PUT`s the file to the
  returned URL (no `Authorization` header: the URL is the one-time credential; up to 50 MB, valid
  for 10 minutes, single use), then passes the returned `upload:<id>` reference to the tool
  (`drive_upload_file`'s `upload_id`, or `"upload:<upload_id>"` in `attachments`).
- **Download.** Small downloads come back inside the tool result, base64-encoded; larger ones become
  a short-lived, one-time link. By default "small" means the whole tool result fits 100,000 bytes, a
  file of about 75 KB, because claude.ai and Claude Desktop truncate a tool result over about 150,000
  characters
  ([ADR 0092](adr/0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)). A link
  only works for a client that can reach the server: claude.ai needs the host on its domain
  allowlist ([ADR 0097](adr/0097-a-download-link-needs-a-client-that-can-reach-the-server.md)). The
  limits are settings (`download_delivery.*`, see
  [Configuration reference](configuration-reference.md)).
- **Exports.** The export tools (`grist_export_records`, `grist_export_sql`, `salesforce_export_report`,
  `jira_export_issues`) return a one-time download link instead of saving a file, and only counts and
  a hash in the result ([ADR 0160](adr/0160-org-mode-exports-are-delivered-by-a-one-time-link.md)).
  An organization can turn the export tools off entirely with `download_delivery.allow_exports: false`
  ([ADR 0197](adr/0197-an-organization-can-turn-the-export-tools-off.md)).

Uploading or creating a slot approves nothing: the card for the tool that uses the file is where
you decide.

## Which AI system is asking

Several AI systems can share one PrivacyFence. Each card and each audit-log entry names the AI
system that made the request, and says how far that name can be trusted
([ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md),
[ADR 0224](adr/0224-an-org-registration-is-attested-by-its-redirect-uris.md)).
The audit log records four fields: `agent_id`, `agent_name`, `agent_version` and `agent_source`. A separate `allowed_by` field says whether a person, their own rule, an organization rule or a scoped token's rule allowed the call ([ADR 0334](adr/0334-the-audit-entry-names-who-allowed-a-call.md)).

| `agent_source` | Trust | Recorded when |
|---|---|---|
| `oauth_client` | attested | The OAuth client registered only with a vendor's own redirect URI (claude.ai, ChatGPT, Gemini Enterprise), so its tokens were obtained by that vendor. No administrator step; see [AI systems](org-mode-setup-guide.md#11-ai-systems). This is the only attested source. |
| `client_info` | claimed | The name the client itself sent: the MCP handshake's `clientInfo.name`, an OAuth client's registered name, or an `agent_overrides` relabel. |
| `""` (empty) | unknown | No usable name. Recorded as unknown, never guessed. |

A call made with a [scoped token](security-and-compliance.md#scoped-tokens) is recorded as the
agent `scoped-token:<id>`, named after the token, with source `override` (attested). The value
`endpoint` is reserved and never recorded.

- A card calls a claimed name exactly that: the header says the caller *says*
  it is that system and marks it **Not verified**, and the rest of the card says "the AI system"
  ([ADR 0036](adr/0036-card-copy-names-the-caller-through-one-placeholder.md)).
- `agent_overrides` in `settings.yaml` maps a client name PrivacyFence does not recognise to a
  known AI system. It changes only the recorded name, not what the card shows, and is always
  recorded as `client_info`, because any program can send that name
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
works only when the administrator turns it on (the unattended-sessions switch under **Admin → Features**;
it is stored as `unattended_sessions.enabled` in `org/admin_config.json`, and a change needs a restart);
otherwise it returns an error. The flag is
advisory: it changes what happens when nothing authorizes a call, never what is authorized
([ADR 0015](adr/0015-unattended-session-flag-is-advisory-only.md)). Pair it with
`privacyfence_check_policy` to plan which steps can run unattended. A scheduled data sync that needs no AI model at all is run by [`privacyfence-sync`](privacyfence-sync.md).
