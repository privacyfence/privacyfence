# Connect Claude Code

Claude Code works with both deployments:

- **Local mode**: PrivacyFence installed on the same computer. Claude Code connects to the daemon's
  `/mcp` endpoint on `localhost` with your own bearer token.
- **Organization mode**: PrivacyFence run centrally by your organization. Claude Code registers
  itself with PrivacyFence's OAuth server and you sign in with your organization account.

Which one you have is explained in
[Local mode and organization mode](how-it-works.md#local-mode-and-organization-mode).

## Local mode

Run this from your own account (not elevated, not as the service account), after logging out and
back in once following the install. It needs two things, both specific to your platform:

| Platform | `/mcp` URL file | Token command |
|---|---|---|
| macOS | `/Library/Application Support/PrivacyFence/handoff/mcp_url` | `/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp --print-mcp-token` |
| Windows | `%ProgramData%\PrivacyFence\handoff\mcp_url` | `& "$env:ProgramFiles\PrivacyFence\privacyfence-app.exe" --print-mcp-token`, captured into a variable (the program has no console window, so run bare it prints nothing visible) |
| Linux (`.deb`) | `/var/lib/privacyfence/handoff/mcp_url` | `privacyfence-app --print-mcp-token` |

On macOS:

```bash
PF_HANDOFF="/Library/Application Support/PrivacyFence/handoff"
claude mcp add --transport http --scope user privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
  --header "Authorization: Bearer $(/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp --print-mcp-token)"
```

On Windows, from PowerShell:

```powershell
$url = Get-Content "$env:ProgramData\PrivacyFence\handoff\mcp_url"
$token = & "$env:ProgramFiles\PrivacyFence\privacyfence-app.exe" --print-mcp-token
claude mcp add --transport http --scope user privacyfence $url --header "Authorization: Bearer $token"
```

On Linux:

```bash
PF_HANDOFF=/var/lib/privacyfence/handoff
claude mcp add --transport http --scope user privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
  --header "Authorization: Bearer $(privacyfence-app --print-mcp-token)"
```

- `mcp_url` holds `http://127.0.0.1:8765/mcp` unless you changed `web.port`
  ([Configuration reference](configuration-reference.md)).
- `--print-mcp-token` mints your account's token the first time and prints the same one afterwards.
  Each OS account gets its own token, and its own connectors, rules and approvals
  ([ADR 0008](adr/0008-one-principal-per-os-user.md)).
- `--scope user` makes PrivacyFence available in every project. Leave it out to add it to the
  current project only.
- Run `/mcp` inside Claude Code to check that `privacyfence` is connected.

Any other MCP client that speaks Streamable HTTP takes the same URL and header.

## Organization mode

Your administrator gives you the deployment's URL, for example `https://pf.example.com`. Then:

```bash
claude mcp add --transport http --scope user privacyfence https://pf.example.com/mcp
```

Run `/mcp` inside Claude Code and choose **privacyfence** to sign in. Claude Code discovers
PrivacyFence's authorization server, registers itself, and opens your organization's sign-in page
in the browser. After you sign in it holds its own tokens; there is nothing to copy or paste.
Access tokens last one hour and are refreshed silently; after 30 days you sign in again.

Connect your services at `https://pf.example.com/connect`. Setting up the deployment itself is
[Organization deployment](org-mode-setup-guide.md).

## Files

Claude Code does not run PrivacyFence's Claude Desktop extension, so PrivacyFence never opens a
path on your computer for it. Files travel through one-time links instead
([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)):

- **Upload** (for example `drive_upload_file`, or an email attachment): Claude Code calls
  `privacyfence_create_upload_slot`, sends the file to the returned URL with an HTTP `PUT`, and
  passes the returned `upload_id` to the tool.
- **Download** (for example `drive_download_file`): the tool returns a one-time link, or in
  organization mode the file itself when it is small enough.

Sizes, lifetimes and the organization settings are in [Files](how-it-works.md#files). A source
checkout or `pip` install that runs as you reads and writes paths directly.

## Confirmations

Two things can ask you before a tool runs:

1. **Claude Code's own permission prompt.** Claude Code asks before it calls a tool from an MCP
   server unless you have allowed it. To stop it asking for PrivacyFence's tools, allow
   `mcp__privacyfence` under `/permissions`, or add it to `permissions.allow` in Claude Code's
   settings.
2. **PrivacyFence's approval card.** A gated call waits for you in Approvals, whatever Claude Code
   was told or allowed. This is the confirmation that decides; see
   [Approvals and policy](approvals-and-policy.md).

By default PrivacyFence tells the client what each tool does: reads are read-only, writes are
writes, and the two tools that delete something are destructive
([What the AI system is told](how-it-works.md#what-the-ai-system-is-told)). To have every connector
tool advertised as read-only instead:

- **Local mode, this connection only:** add
  `--header "X-PrivacyFence-Tool-Annotations: all-read-only"` to the `claude mcp add` command. It
  wins over the bundle; any value other than `truthful` or `all-read-only` is refused.
- **Organization mode:** the administrator builds the bundle with
  `--tool-annotations all-read-only`. The header is ignored there.

The organization bundle options are listed in
[Configuration reference](configuration-reference.md#organization-config-bundle-org_configjson).

## How the client is identified

Every approval card and audit entry names the AI system that asked. Claude Code sends its own name
in the MCP handshake, and in organization mode when it registers. That name is a **claim**: the
card says the caller *says* it is Claude Code and marks it **Not verified**, because any program
can send the same name. In organization mode an administrator can **pin** Claude Code's
registration on **Settings → AI systems**; the cards for that registration are then verified.
See [Which AI system is asking](how-it-works.md#which-ai-system-is-asking).

## Troubleshooting

| What you see | What to do |
|---|---|
| `--print-mcp-token` cannot reach the daemon | Log out and back in once after installing, then check that the daemon is running (your platform's install page, "Troubleshooting"). |
| On Windows the token variable is empty | Run the command as yourself, not elevated, and capture it into a variable as above. |
| `/mcp` shows `privacyfence` as failed in local mode | The daemon is stopped, or `web.port` changed after you added it. Start the daemon, then run the `claude mcp add` command again (remove the old entry first with `claude mcp remove privacyfence`). |
| `/mcp` shows `privacyfence` as needing authentication in organization mode | Choose it in `/mcp` and sign in again. |
| Tools are listed but none of your services' tools | No service is connected yet. Ask Claude Code to call `privacyfence_status`; connect services in Settings (local mode) or at `/connect` (organization mode). See [Connecting a service](connecting-a-service.md). |
| A call waits and nothing happens | It is waiting for your approval. Open Approvals from the companion, or `/approvals` in organization mode. |

Platform-specific problems are on [macOS](install-macos.md#troubleshooting),
[Windows](install-windows.md#troubleshooting) and [Linux](install-linux.md#troubleshooting).
