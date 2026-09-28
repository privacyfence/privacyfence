# Connect ChatGPT

ChatGPT connects to PrivacyFence in two ways:

- **ChatGPT desktop, in local mode.** The ChatGPT app on your computer connects to the local
  daemon's `/mcp` endpoint on `localhost` with your own token. Checked on macOS.
- **ChatGPT on the web, in organization mode**, through a Developer Mode app. ChatGPT on the web
  connects to MCP servers from OpenAI's servers, not from your computer, so it needs a PrivacyFence
  it can reach over public HTTPS. A local install listens on `localhost` only, on purpose, so
  ChatGPT on the web cannot use it.

Which deployment you have is explained in
[Local mode and organization mode](how-it-works.md#local-mode-and-organization-mode).

Both were checked with a **personal ChatGPT plan**. Business, Enterprise and Edu workspaces are not
verified (see [Organization mode](#organization-mode)).

## Local mode

| Platform | ChatGPT with a local install |
|---|---|
| macOS | ChatGPT desktop, as below. |
| Windows | Not verified. The ChatGPT desktop app for Windows may take the same steps; they have not been checked. |
| Linux | Not available: there is no ChatGPT desktop app for Linux. Use [Claude Code](connect-claude-code.md). |

ChatGPT on the web cannot reach a local install on any platform.

### ChatGPT desktop

You need two things from PrivacyFence: the MCP URL and your token. Both are on **Settings → AI
clients**.

1. In PrivacyFence, open **Settings → AI clients**. Note the **MCP URL**, normally
   `http://127.0.0.1:8765/mcp`.
2. Click **Copy token** there, or choose **Copy AI Client Token** in the companion's menu bar menu.
   Your token is now on the clipboard. Treat it like a password.
3. In ChatGPT desktop, open **Settings** → **MCP servers** → **Add server**. The form is titled
   **Connect to a custom MCP**.
4. Fill it in:
   - **Name:** `PrivacyFence`
   - **Type:** **Streamable HTTP**
   - **URL:** the MCP URL from step 1
   - **Headers:** add `Authorization` with the value `Bearer ` followed by the token you copied
5. Save, then turn the server on in a chat.

The token stays the same across restarts, upgrades and reboots, so this is done once. **Rotate
token** on **Settings → AI clients** replaces it; ChatGPT desktop then needs the new one. Without
the companion, `--print-mcp-token` prints the same token in a terminal; the command for each
platform is in [Connect Claude Code](connect-claude-code.md#local-mode).

**Files in local mode.** ChatGPT desktop runs its commands on your computer, so it can use
PrivacyFence's one-time links on `127.0.0.1` directly:

- **Download:** a large file comes back as a one-time link, which ChatGPT fetches and saves where
  you asked. ChatGPT asks for its own permission before writing to a folder such as `~/Downloads`.
- **Upload:** PrivacyFence runs under its own system account and cannot read files in your home
  folder, so a file path is refused, with a message that names `privacyfence_create_upload_slot`.
  ChatGPT then sends the file through an upload link instead.

**Confirmations in ChatGPT desktop.** ChatGPT desktop asks before running a tool that is not
read-only, and offers to always allow it. PrivacyFence's approval card still decides every gated
call; see [Confirmations](#confirmations).

**How ChatGPT desktop is identified.** On a local install every approval card shows the requester
as **Undetected**, whatever the client calls itself
([ADR 0088](adr/0088-local-mode-shows-every-requester-as-undetected.md)). The audit log records the
name the client gives: ChatGPT desktop gives `codex-mcp-client`, the same name as OpenAI's Codex
command-line tool, so the two cannot be told apart
([ADR 0103](adr/0103-chatgpt-is-matched-by-its-registered-name.md)).

## Organization mode

Your administrator gives you the deployment's URL, for example `https://pf.example.com`.

You need a ChatGPT plan with Developer Mode. These steps were checked with a **personal ChatGPT
plan**.

**Business, Enterprise and Edu workspaces are not verified.** On a workspace, an administrator may
have to allow Developer Mode first, and ChatGPT may behave differently from what this page
describes. A connector the workspace administrator publishes for every member is not covered here;
it is tracked in [issue 796](https://github.com/privacyfence/privacyfence/issues/796).

1. Turn on Developer Mode: profile icon → **Settings** → **Security and login** →
   **Developer mode**. OpenAI moves this setting now and then; some of its pages still place it
   under **Settings** → **Apps** → **Advanced settings**.
2. **Settings** → **Apps** → **Create**, and fill in:
   - **Name:** `PrivacyFence`
   - **MCP Server URL:** `https://pf.example.com/mcp`
   - **Authentication:** **OAuth**. Leave any OAuth client ID and secret empty: ChatGPT registers
     itself.

   Confirm that you understand the warning, then click **Create**.
3. Sign in with your organization account when PrivacyFence's sign-in page opens. ChatGPT then
   shows the app as connected, with PrivacyFence's tools.
4. Connect your services at `https://pf.example.com/connect`.

**Turn the app on in every chat.** In a new chat, open the **+** menu, choose **Developer mode**,
and select **PrivacyFence**. A Developer Mode app is off in every new chat until you do.

Access tokens last one hour and are refreshed silently; after 30 days you sign in again. Setting up
the deployment itself is [Organization deployment](org-mode-setup-guide.md).

If the deployment accepts connections only from known addresses, it must also accept the address
ranges OpenAI publishes for ChatGPT's connectors, since every tool call comes from OpenAI's
servers. File links are fetched from ChatGPT's code sandbox instead (see [Files](#files)), which
may use other addresses.

## Files

A local path means nothing to the server, and ChatGPT does not run PrivacyFence's Claude Desktop
extension. Files travel through one-time links instead
([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)):

- **Upload** (for example `drive_upload_file`, or an email attachment): ChatGPT calls
  `privacyfence_create_upload_slot`, sends the file to the returned URL with an HTTP `PUT`, and
  passes the returned `upload_id` to the tool.
- **Download** (for example `drive_download_file`): a file of up to about 75 KB comes back in the
  tool result; a larger one becomes a short-lived, one-time link.

**ChatGPT uses those links from its code sandbox, with no allowlist to set up.** A link works only
if the client can reach the deployment's host
([ADR 0097](adr/0097-a-download-link-needs-a-client-that-can-reach-the-server.md)). ChatGPT's code
sandbox fetches a download link and sends an upload's `PUT` itself, over public HTTPS, so unlike
claude.ai there is no domain list to add the host to.

**Uploads need the next release after 5.0.0 when the approval waits.** In 5.0.0, an upload whose
approval card was still waiting when ChatGPT asked fails after you approve it, with "the upload
expired or was already used", and ChatGPT may then retry by sending the file inside the tool call
instead. The release after 5.0.0 keeps the uploaded file until the approved upload runs
([ADR 0102](adr/0102-an-upload-slot-is-consumed-after-the-gate-not-before.md)).

**To read a document, no download is needed.** `drive_get_file_content` returns a PDF, Word,
PowerPoint or Excel file's text, so ChatGPT can read it without any link. A scanned PDF has no
text; figures and layout are not in the text either.

Sizes, lifetimes and the organization settings are in [Files](how-it-works.md#files) and
[File delivery](org-mode-setup-guide.md#13-file-delivery).

## Confirmations

Two things can ask you before a tool runs:

1. **ChatGPT's own confirmation.** Whether ChatGPT asks is decided by ChatGPT's permission
   setting, not by PrivacyFence. ChatGPT sorts PrivacyFence's tools into **Read** and **Write** by
   their annotations, with no separate group for the two that delete something. On a personal
   plan, with the setting **Allow low-risk tools**, ChatGPT did not ask before creating a calendar
   event. It asked once, before an upload, and offered to always allow that tool; after that it
   did not ask again. OpenAI documents that a lasting **Always allow** is not offered to members of
   a managed workspace, so a workspace member may be asked every time; that is not verified.
2. **PrivacyFence's approval card.** A gated call waits for you at `/approvals`, whatever ChatGPT
   was told or allowed. This is the confirmation that always decides; see
   [Approvals and policy](approvals-and-policy.md).

PrivacyFence always tells the client what each tool does: reads are read-only, writes are writes,
and the two tools that delete something are destructive
([What the AI system is told](how-it-works.md#what-the-ai-system-is-told)). There is no switch that
advertises writes as read-only ([ADR 0089](adr/0089-tool-annotations-are-always-truthful.md)).

**What to expect:** depending on ChatGPT's permission setting and your plan, a write is confirmed
once, on PrivacyFence's card, or twice, once in ChatGPT and once on PrivacyFence's card. Allowing a
tool in ChatGPT only stops ChatGPT's question: every call still goes through PrivacyFence's gate,
and PrivacyFence's card is the one that decides.

## How the client is identified

In organization mode, every approval card and audit entry names the AI system that asked. ChatGPT registers with
PrivacyFence as `ChatGPT`, which PrivacyFence recognises, so its cards name **ChatGPT**. ChatGPT
also gives a name in the MCP handshake, but the registered name is the one used when PrivacyFence
recognises it.

That name is a **claim**: the card says the caller *says* it is that system and marks it
**Not verified**, because any program that can reach the deployment can register under the same
name. An administrator can **pin** ChatGPT's registration on **Settings → AI systems**, for
example by matching its last-used time to their own sign-in from ChatGPT; the cards for that
registration are then verified. See
[Which AI system is asking](how-it-works.md#which-ai-system-is-asking) and
[AI systems](org-mode-setup-guide.md#11-ai-systems).

## Troubleshooting

| What you see | What to do |
|---|---|
| **Create** fails before the sign-in page opens | The URL must end in `/mcp` and be reachable over public HTTPS with a valid certificate. Your administrator can check it with [the validation checklist](org-mode-setup-guide.md#validation-checklist). |
| There is no **Developer mode** setting | Look under **Settings** → **Security and login**. If it is not there either, your plan or workspace does not offer it, or an administrator has not allowed it. Ask your workspace administrator. |
| Sign-in finishes but the app does not appear | Try **Create** once more. |
| The sign-in page refuses you | Your account is not allowed to sign in to this deployment. Ask your administrator. |
| PrivacyFence's tools are missing from a chat | Turn the app on for that chat: **+** → **Developer mode** → **PrivacyFence**. |
| Tools are listed but none of your services' tools | No service is connected yet. Ask ChatGPT to call `privacyfence_status`, then connect services at `/connect`. See [Connecting a service](connecting-a-service.md). |
| A call waits and nothing happens | It is waiting for your approval at `/approvals`. |
| An upload fails after you approve it, with "the upload expired or was already used" | The deployment runs 5.0.0, and the approval waited. Ask your administrator to upgrade to the next release after 5.0.0. |
| Every card says **Not verified** | Expected until an administrator pins your registration. |
| ChatGPT desktop cannot connect to PrivacyFence | Check that PrivacyFence is running, that the URL matches **Settings → AI clients**, and that the header value is `Bearer ` followed by the token. After **Rotate token**, paste the new token. |
| **Copy token** says the companion is not running | Start PrivacyFence's companion app, or run `--print-mcp-token` in a terminal (see [Connect Claude Code](connect-claude-code.md#local-mode)). |

Deployment problems are in the organization guide's
[Troubleshooting](org-mode-setup-guide.md#15-troubleshooting).
