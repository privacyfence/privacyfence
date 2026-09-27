# Connect claude.ai

claude.ai works with **organization mode only**. claude.ai connects to MCP servers from
Anthropic's servers, not from your computer, so it needs a PrivacyFence it can reach over public
HTTPS. A local install listens on `localhost` only and is unreachable from outside your computer,
on purpose. Which deployment you have is explained in
[Local mode and organization mode](how-it-works.md#local-mode-and-organization-mode).

## Local mode

Not available. On your own computer, use [Claude Desktop](connect-claude-desktop.md) or
[Claude Code](connect-claude-code.md) instead: both work with a local install on every platform
they run on.

| Platform | claude.ai with a local install |
|---|---|
| macOS | Not available; use Claude Desktop or Claude Code. |
| Windows | Not available; use Claude Desktop or Claude Code. |
| Linux | Not available; use Claude Code. |

## Organization mode

Your administrator gives you the deployment's URL, for example `https://pf.example.com`.

1. In claude.ai's connector settings, add a custom connector with the URL
   `https://pf.example.com/mcp`. Leave the optional OAuth client ID and secret empty: claude.ai
   registers itself.
2. Click **Connect** on the connector and sign in with your organization account.
3. Connect your services at `https://pf.example.com/connect`.

On Team and Enterprise plans an owner adds the connector once for the whole organization, and each
person connects it with their own sign-in. Access tokens last one hour and are refreshed silently;
after 30 days you sign in again. Setting up the deployment itself is
[Organization deployment](org-mode-setup-guide.md).

## Files

A local path means nothing to the server, and claude.ai does not run PrivacyFence's Claude Desktop
extension. Files travel through one-time links instead
([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)):

- **Upload** (for example `drive_upload_file`, or an email attachment): claude.ai calls
  `privacyfence_create_upload_slot`, sends the file to the returned URL with an HTTP `PUT`, and
  passes the returned `upload_id` to the tool.
- **Download** (for example `drive_download_file`): a small file comes back in the tool result; a
  larger one becomes a short-lived, one-time link.

Sizes, lifetimes and the organization settings are in [Files](how-it-works.md#files) and
[File delivery](org-mode-setup-guide.md#13-file-delivery).

## Confirmations

Two things can ask you before a tool runs:

1. **claude.ai's own tool prompt.** claude.ai can ask before it calls a tool from a connector, and
   decides from the tool's annotations. On the Team plan, a tool marked as a write prompts on every
   call and cannot be allowed for the whole task, and an organization cannot pre-approve it
   ([ADR 0076](adr/0076-every-connector-tool-is-advertised-read-only.md)).
2. **PrivacyFence's approval card.** A gated call waits for you at `/approvals`, whatever claude.ai
   was told or allowed. This is the confirmation that decides; see
   [Approvals and policy](approvals-and-policy.md).

PrivacyFence advertises every connector tool to the client as read-only, non-destructive and
idempotent, so claude.ai does not put a second confirmation in front of PrivacyFence's own
([What the AI system is told](how-it-works.md#what-the-ai-system-is-told)). The organization bundle
options are listed in
[Configuration reference](configuration-reference.md#organization-config-bundle-org_configjson).

## How the client is identified

Every approval card and audit entry names the AI system that asked. claude.ai gives a name when it
registers with PrivacyFence and again in the MCP handshake; the registered name is the one used.
That name is a **claim**: the card says the caller *says* it is that system and marks it
**Not verified**, because any program that can reach the deployment can register under the same
name. An administrator can **pin** claude.ai's registration on **Settings → AI systems**, for
example by matching its last-used time to their own sign-in from claude.ai; the cards for that
registration are then verified. See
[Which AI system is asking](how-it-works.md#which-ai-system-is-asking) and
[AI systems](org-mode-setup-guide.md#11-ai-systems).

## Troubleshooting

| What you see | What to do |
|---|---|
| **Connect** fails before the sign-in page opens | The URL must end in `/mcp` and be reachable over public HTTPS with a valid certificate. Your administrator can check it with [the validation checklist](org-mode-setup-guide.md#validation-checklist). |
| The sign-in page refuses you | Your account is not allowed to sign in to this deployment. Ask your administrator. |
| The connector was working and now asks you to connect again | A sign-in lasts 30 days. Click **Connect** again. |
| Tools are listed but none of your services' tools | No service is connected yet. Ask Claude to call `privacyfence_status`, then connect services at `/connect`. See [Connecting a service](connecting-a-service.md). |
| A call waits and nothing happens | It is waiting for your approval at `/approvals`. |
| Every card says **Not verified** | Expected until an administrator pins your registration. |

Deployment problems are in the organization guide's
[Troubleshooting](org-mode-setup-guide.md#15-troubleshooting).
