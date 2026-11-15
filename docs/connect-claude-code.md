# Connect Claude Code

Claude Code connects to PrivacyFence's `/mcp` endpoint and signs in through OAuth. It works with both
deployments:

- **Single-user Docker**: PrivacyFence's Docker image on the same computer. Claude Code connects to
  `http://localhost:8765/mcp` and you sign in with your passkey.
- **Organization mode**: PrivacyFence run centrally by your organization. Claude Code registers
  itself with PrivacyFence's OAuth server and you sign in with your organization account.

Which one you have is explained in
[Single-user and organization deployments](how-it-works.md#single-user-and-organization-deployments).

## Single-user Docker

Start the Docker image on your computer and set up your passkey
([Run it on this computer](org-mode-docker.md#run-it-on-this-computer)). Then:

```bash
claude mcp add --transport http --scope user privacyfence http://localhost:8765/mcp
```

Run `/mcp` inside Claude Code and choose **privacyfence** to sign in. PrivacyFence opens its
sign-in page for your passkey, then a consent page; after that Claude Code holds its own tokens, as
with an organization server. Use `localhost`, not `127.0.0.1`: the passkey belongs to
`localhost`.

## Organization mode

Your administrator gives you the deployment's URL, for example `https://pf.example.com`. Then:

```bash
claude mcp add --transport http --scope user privacyfence https://pf.example.com/mcp
```

Run `/mcp` inside Claude Code and choose **privacyfence** to sign in. Claude Code discovers
PrivacyFence's authorization server, registers itself, and opens your organization's sign-in page
in the browser. After you sign in it holds its own tokens; there is nothing to copy or paste.
Access tokens last one hour and are refreshed silently; after 30 days you sign in again.

### Scoped tokens

If your administrator has turned scoped tokens on, **Settings → Scoped tokens** lets you create a
named, expiring bearer token with its own rules and a chosen list of tools, for a scheduled job that cannot sign
in. The token is shown once, in a dialog with a Copy button; it is not stored and cannot be shown
again. Pass it as the bearer token (`Authorization: Bearer pfst_…`) to `/mcp`. Its lifetime is 90
days by default and at most the administrator's limit. Revoke it in the same page. It is
[deny-only](security-and-compliance.md#scoped-tokens). A token's rules and tools cannot be edited. Deleting or changing an Auto-accept rule does not change a token. **Rename** changes only its name, and clients using it keep working. **Extend** moves the expiry to the number of days you choose from today, never earlier than it is now, with the same secret, rules and tools, so clients using it keep working. For any other change, **Duplicate** fills
the New token form from an existing token, with a numbered name, so you can create a replacement
and revoke the old one, with a copy of its rules. The form lists only the tools the selected rules cover, plus the tools that
need no rule. On the New token form, a second checkbox, **Let reads return personal data**, lets covered Gmail message and thread reads, Slack reads and Drive file content reads return personal data (the scan still runs and is audited). The rules are the token's own: add them on the form (a scope, a value and verbs, as on the Auto-accept page) or copy one of your Auto-accept rules, and they never auto-accept anything for your other AI clients ([ADR 0236](adr/0236-a-scoped-token-holds-its-own-rules.md)). A Gmail query rule such as `in:inbox` does not match a message you sent, so a thread holding your own reply is not covered by it.

Connect your services at `https://pf.example.com/connect`. Setting up the deployment itself is
[Organization deployment](org-mode-setup-guide.md).

#### Let Claude propose a token

Instead of building the token by hand, ask Claude to propose one for the routine. Claude calls `privacyfence_propose_scoped_token`, built from what you approved in the session or from rules it names, and hands you a review link. Your administrator must have turned token proposals on.

- Open the link: **Settings → Scoped tokens** shows the proposal. You can only narrow it: untick a rule, a verb or a tool, or shorten the lifetime. To add more, use **New token**.
- If Claude asked for personal data to be allowed, those boxes start unticked; only you can tick them.
- Continue, then type the token's name to confirm that you are delegating access (and use your passkey, if your installation asks for one).
- The token is shown once, in a dialog, for you to copy. Claude never receives the token, and is not told whether you created it. A proposal lasts one hour and can be used once; Decline discards it. See [ADR 0250](adr/0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md).

**For an n8n workflow.** If your administrator set up [n8n delivery](org-mode-setup-guide.md#delivering-tokens-to-n8n), Claude can pass `deliver_to: {"runtime": "n8n", "workflow_id": "..."}`. The review page then names your n8n host. When you confirm, PrivacyFence creates the token as an n8n credential itself, and Claude calls `privacyfence_runtime_credential_status` to get the credential's id for the MCP Client node. Anyone who can edit workflows in that n8n, Claude included, can use and read the token ([ADR 0278](adr/0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md)). Revoking the token deletes the credential within 5 minutes.

### A cloud routine with a scoped token

A scheduled Claude Code routine that runs in the cloud (claude.ai/code) uses the claude.ai
connector by default. That connector signs in as you, so the routine runs under your Auto-accept
rules. It cannot use a scoped token's rules, its tool list or its personal-data checkboxes. To run
the routine under a scoped token instead, give it a cloud environment of its own that sends the
token to PrivacyFence:

1. **Create the token** for the routine in **Settings → Scoped tokens**, with only the rules and
   tools the job needs. If the routine reads mail or pages that may hold personal data, tick
   **Let reads return personal data**. Use one token per routine
   ([ADR 0220](adr/0220-a-routines-rule-profile-is-a-scoped-token.md)).
2. **Create a personal cloud environment** used only by this routine and save the token in it.
   This is done in the browser, with no command: in claude.ai/code, open the cloud environment
   menu (in a session's title bar), add a new environment or choose **Edit** on an existing one,
   and under **Network secrets** add the token for the deployment's host, for example
   `pf.example.com`, as a bearer token. The environment's network proxy then adds
   `Authorization: Bearer pfst_…` to every request this environment's sessions send to that host.
3. **Add PrivacyFence as an MCP server** in that environment, because the network secret only adds
   the header and gives the routine no tools. Put it in the routine repository's `.mcp.json`, or
   add it from the environment's setup script:

   ```bash
   claude mcp add --transport http privacyfence https://pf.example.com/mcp --header "Authorization: Bearer placeholder"
   ```

   The placeholder makes Claude Code send a header for the proxy to replace, instead of starting
   an OAuth sign-in. Never put the real token in the repository, the setup script or an
   environment variable: those are stored in plain text.
4. **Turn off the claude.ai PrivacyFence connector** for the routine, so its calls do not also
   go out under your own sign-in.
5. **Point the routine at the new environment.**

Calls from the routine are then decided by the token alone. They are
[deny-only](security-and-compliance.md#scoped-tokens), never raise an approval card, and are
recorded in the audit log under the token's name. Things to keep in mind:

- **Keep the environment for this routine only.** The secret goes to every session in the
  environment. On your Default environment, or an environment shared with your organization, other
  sessions would run under the routine's token too.
- **Replace the token before it expires.** At expiry the routine's calls are denied. Settings flags
  a token close to expiry; **Duplicate** it, save the new token in the environment's network
  secrets, then revoke the old one.

## Files

PrivacyFence never opens a path on your computer for Claude Code. Files travel through one-time
links instead ([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)):

- **Upload** (for example `drive_upload_file`, or an email attachment): Claude Code calls
  `privacyfence_create_upload_slot`, sends the file to the returned URL with an HTTP `PUT`, and
  passes the returned `upload:` reference to the tool.
- **Download** (for example `drive_download_file`): the tool returns a one-time link, or the file
  itself when it is small enough.

Sizes, lifetimes and the organization settings are in [Files](how-it-works.md#files).

## Confirmations

Two things can ask you before a tool runs:

1. **Claude Code's own permission prompt.** Whether Claude Code asks before it calls a tool from
   an MCP server depends on its permission mode and rules, not on the tool's annotations. To stop it asking for PrivacyFence's tools, allow
   `mcp__privacyfence` under `/permissions`, or add it to `permissions.allow` in Claude Code's
   settings.
2. **PrivacyFence's approval card.** A gated call waits for you in Approvals, whatever Claude Code
   was told or allowed. This is the confirmation that decides; see
   [Approvals and policy](approvals-and-policy.md).

PrivacyFence always tells the client what each tool does: reads are read-only, writes are writes,
and the tools that delete something (`calendar_delete_event`,
`drive_sheets_delete_dimensions` and `peek_delete_page`) are destructive
([What the AI system is told](how-it-works.md#what-the-ai-system-is-told)). There is no switch that
advertises writes as read-only. If you would rather confirm only once, on PrivacyFence's card,
always-allow PrivacyFence's tools in Claude Code: choose the option not to ask again the first
time it asks about a PrivacyFence tool, or allow `mcp__privacyfence` as above for all of them at
once. Every call still goes through PrivacyFence's gate.

**What we observed** (Claude Code 2.1.283, PrivacyFence 5.0.0a2, 2026-09-28,
[evidence](https://github.com/privacyfence/privacyfence/issues/46#issuecomment-5865032001)): with no permission rules configured, Claude Code did not ask before
`calendar_create_event` or any other PrivacyFence tool in the run; PrivacyFence's approval card was the only
confirmation. If your permission mode or rules make it ask, always-allow as above.

## How the client is identified

Claude Code sends its own name when it registers and in the MCP handshake. It
registers as `Claude Code (<name>)`, where `<name>` is the server name you gave `claude mcp add`,
and PrivacyFence recognises that as Claude Code whatever the name is. That name is a **claim**: the card says the caller *says* it is Claude Code and marks it **Not verified**,
because any program can send the same name, and its redirect URI is a loopback address that any
local program can register. There is nothing an administrator can do about it. See
[Which AI system is asking](how-it-works.md#which-ai-system-is-asking) and
[AI systems](org-mode-setup-guide.md#11-ai-systems).

## Troubleshooting

| What you see | What to do |
|---|---|
| `/mcp` shows `privacyfence` as needing authentication | Choose it in `/mcp` and sign in again. |
| Tools are listed but none of your services' tools | No service is connected yet. Ask Claude Code to call `privacyfence_status`; connect services at `/connect`. See [Connecting a service](connecting-a-service.md). |
| A call waits and nothing happens | It is waiting for your approval. Open `/approvals` on the PrivacyFence server. |
