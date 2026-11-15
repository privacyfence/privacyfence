# Connect Claude Desktop

Claude Desktop connects to PrivacyFence as a custom connector. It works with both deployments:

- **Single-user Docker**: PrivacyFence's Docker image on the same computer. Claude Desktop connects
  to `http://localhost:8765/mcp` and you sign in with your passkey.
- **Organization mode**: PrivacyFence run centrally by your organization. Claude Desktop connects
  to it and you sign in with your organization account.

Which one you have is explained in
[Single-user and organization deployments](how-it-works.md#single-user-and-organization-deployments).
Claude Desktop runs on macOS and Windows; on Linux, use [Claude Code](connect-claude-code.md).

## Single-user Docker

Start the Docker image on your computer and set up your passkey
([Run it on this computer](org-mode-docker.md#run-it-on-this-computer)). In Claude Desktop's
connector settings, add a custom connector with the URL `http://localhost:8765/mcp`, click
**Connect** and sign in with your passkey. Leave the optional OAuth client ID and secret empty:
Claude Desktop registers itself.

## Organization mode

Your administrator gives you the deployment's URL, for example `https://pf.example.com`. In Claude
Desktop's connector settings, add a custom connector with the URL `https://pf.example.com/mcp`,
then click **Connect** on it and sign in with your organization account. Leave the optional OAuth
client ID and secret empty: Claude Desktop registers itself. On Team and Enterprise plans an owner
can add the connector for the whole organization, and each person connects it with their own
sign-in.

Access tokens last one hour and are refreshed silently; after 30 days you sign in again. Connect
your services at `https://pf.example.com/connect`. Setting up the deployment itself is
[Organization deployment](org-mode-setup-guide.md).

## Files

A local path means nothing to the server. Claude uploads a file through
`privacyfence_create_upload_slot` and a one-time link, and a download comes back in the tool result
or as a one-time link ([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)).
Sizes, lifetimes and limits are in [Files](how-it-works.md#files).

## Confirmations

Two things can ask you before a tool runs:

1. **Claude Desktop's own tool prompt.** Claude Desktop can ask before it calls a tool, and offers
   to allow it for the rest of the chat or always. Clients use a tool's annotations to decide when to
   ask.
2. **PrivacyFence's approval card.** A gated call waits for you in Approvals, whatever Claude
   Desktop was told or allowed. This is the confirmation that decides; see
   [Approvals and policy](approvals-and-policy.md).

PrivacyFence always tells the client what each tool does: reads are read-only, writes are writes,
and the tools that delete something (`calendar_delete_event`,
`drive_sheets_delete_dimensions` and `peek_delete_page`) are destructive
([What the AI system is told](how-it-works.md#what-the-ai-system-is-told)). So Claude Desktop may
ask before a write, in front of PrivacyFence's own card. There is no switch that
advertises writes as read-only. If you would rather confirm only once, on PrivacyFence's card,
choose to always allow the tool the first time Claude Desktop asks about it; it does not ask about
that tool again. Every call still goes through PrivacyFence's gate.

**What we observed** (Claude Desktop 2.9939.2 with PrivacyFence 5.0.0a2,
2026-09-28, [evidence](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865556451)): with default tool permissions Claude Desktop asked before
`calendar_create_event`. Choosing to always allow the tool stopped it asking, and PrivacyFence's
approval card was still shown for the call.

## How the client is identified

Claude Desktop sends its own name when it registers and in the MCP handshake.
Its custom connector most likely registers through your claude.ai account exactly as claude.ai does,
as `Claude`, but that has not been verified. That name is a **claim**: the card says the caller *says* it is Claude Desktop and marks it **Not verified**,
because any program can send the same name. There is nothing an administrator can do about it.
If the custom connector registers through claude.ai with claude.ai's own callback, it is shown as
Claude, verified; otherwise its name stays a claim. See [Which AI system is asking](how-it-works.md#which-ai-system-is-asking) and
[AI systems](org-mode-setup-guide.md#11-ai-systems).

## Troubleshooting

| What you see | What to do |
|---|---|
| Tools are listed but none of your services' tools | No service is connected yet. Ask Claude to call `privacyfence_status`; connect services at `/connect`. See [Connecting a service](connecting-a-service.md). |
| The custom connector's **Connect** fails | The URL must end in `/mcp` and be reachable over public HTTPS. Your administrator can check it with [the validation checklist](org-mode-setup-guide.md#validation-checklist). |
| A call waits and nothing happens | It is waiting for your approval. Open `/approvals` on the PrivacyFence server. |
