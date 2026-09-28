# Connect Gemini CLI

> **Verification pending.** These steps follow Gemini CLI 0.61.0's own documentation and source,
> and have not yet been checked against a running PrivacyFence. Progress is tracked in
> [issue 392](https://github.com/privacyfence/privacyfence/issues/392).

Gemini CLI works with both deployments:

- **Local mode**: PrivacyFence installed on the same computer. Gemini CLI connects to the daemon's
  `/mcp` endpoint on `localhost` with your own bearer token.
- **Organization mode**: PrivacyFence run centrally by your organization. Gemini CLI registers
  itself with PrivacyFence's OAuth server and you sign in with your organization account.

Which one you have is explained in
[Local mode and organization mode](how-it-works.md#local-mode-and-organization-mode).

Gemini CLI connects straight to `/mcp` over HTTP. It does not use PrivacyFence's Claude Desktop
extension, and there is nothing else to install for it.

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
gemini mcp add --scope user --transport http privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
  --header "Authorization: Bearer $(/Applications/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceApp --print-mcp-token)"
```

On Windows, from PowerShell:

```powershell
$url = Get-Content "$env:ProgramData\PrivacyFence\handoff\mcp_url"
$token = & "$env:ProgramFiles\PrivacyFence\privacyfence-app.exe" --print-mcp-token
gemini mcp add --scope user --transport http privacyfence $url --header "Authorization: Bearer $token"
```

On Linux:

```bash
PF_HANDOFF=/var/lib/privacyfence/handoff
gemini mcp add --scope user --transport http privacyfence "$(cat "$PF_HANDOFF/mcp_url")" \
  --header "Authorization: Bearer $(privacyfence-app --print-mcp-token)"
```

Or add the server to Gemini CLI's settings by hand, in `~/.gemini/settings.json` (on Windows,
`%USERPROFILE%\.gemini\settings.json`), with the URL from `mcp_url` and the token the command
prints:

```json
{
  "mcpServers": {
    "privacyfence": {
      "httpUrl": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }
  }
}
```

- `mcp_url` holds `http://127.0.0.1:8765/mcp` unless you changed `web.port`
  ([Configuration reference](configuration-reference.md)).
- `--print-mcp-token` mints your account's token the first time and prints the same one afterwards.
  Each OS account gets its own token, and its own connectors, rules and approvals
  ([ADR 0008](adr/0008-one-principal-per-os-user.md)). The token is stored in
  `settings.json` in plain text; keep that file to yourself.
- `--scope user` makes PrivacyFence available in every project. Without it, `gemini mcp add` adds
  it to the current project's `.gemini/settings.json` only.
- Run `/mcp` inside Gemini CLI, or `gemini mcp list` from your shell, to check that `privacyfence`
  is connected.

## Organization mode

Your administrator gives you the deployment's URL, for example `https://pf.example.com`. Then:

```bash
gemini mcp add --scope user --transport http privacyfence https://pf.example.com/mcp
```

or, in `~/.gemini/settings.json`, the same entry without `headers`:

```json
{
  "mcpServers": {
    "privacyfence": {"httpUrl": "https://pf.example.com/mcp"}
  }
}
```

Inside Gemini CLI, run `/mcp auth privacyfence`. Gemini CLI discovers PrivacyFence's authorization
server, registers itself, and opens your organization's sign-in page in the browser; the browser
returns to a callback on `localhost`, so run Gemini CLI on a computer with a browser. After you
sign in it holds its own tokens (in `~/.gemini/mcp-oauth-tokens.json`); there is nothing to copy or
paste. Access tokens last one hour and are refreshed silently; after 30 days you run
`/mcp auth privacyfence` again.

Connect your services at `https://pf.example.com/connect`. Setting up the deployment itself is
[Organization deployment](org-mode-setup-guide.md).

## Files

Gemini CLI does not run PrivacyFence's Claude Desktop extension, so PrivacyFence never opens a path
on your computer for it. Files travel through one-time links instead
([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)):

- **Upload** (for example `drive_upload_file`, or an email attachment): Gemini CLI calls
  `privacyfence_create_upload_slot`, sends the file to the returned URL with an HTTP `PUT`, and
  passes the returned `upload_id` to the tool.
- **Download** (for example `drive_download_file`): the tool returns a one-time link, or in
  organization mode the file itself when it is small enough.

To read a Google document rather than save it, ask for its text: `drive_get_file_content` returns
it directly, with no link to fetch.

Sizes, lifetimes and the organization settings are in [Files](how-it-works.md#files). A source
checkout or `pip` install that runs as you reads and writes paths directly.

## Confirmations

Two things can ask you before a tool runs:

1. **Gemini CLI's own confirmation.** By default Gemini CLI asks before it calls a tool from an MCP
   server, and offers to proceed once, always allow that tool, or always allow every tool from
   that server. Setting `"trust": true` on the `privacyfence` entry in `settings.json` (or adding
   the server with `gemini mcp add --trust`) stops it asking for any of PrivacyFence's tools.
2. **PrivacyFence's approval card.** A gated call waits for you in Approvals, whatever Gemini CLI
   was told or allowed. This is the confirmation that decides; see
   [Approvals and policy](approvals-and-policy.md).

PrivacyFence always tells the client what each tool does: reads are read-only, writes are writes,
and the two tools that delete something are destructive
([What the AI system is told](how-it-works.md#what-the-ai-system-is-told)). There is no switch that
advertises writes as read-only. Gemini CLI's Plan Mode offers only read-only tools, so
PrivacyFence's writes are not available there. If you would rather confirm only once, on
PrivacyFence's card, always allow PrivacyFence's tools in Gemini CLI, or trust the server as
above. Every call still goes through PrivacyFence's gate.

## How the client is identified

In local mode every approval card and Audit Log row shows the requester as **Undetected**: every AI
system on your computer uses the same credential, so PrivacyFence cannot tell them apart. The audit
log still records the name Gemini CLI sent, `gemini-cli-mcp-client`.

In organization mode, Gemini CLI sends its own name when it registers and in the MCP handshake. It
registers as `Gemini CLI MCP Client`, and names itself `gemini-cli-mcp-client` in the handshake,
which PrivacyFence recognises as Gemini CLI. That name is a **claim**: the card says the caller
*says* it is Gemini CLI and marks it **Not verified**, because any program can send the same name.
An administrator can **pin** Gemini CLI's registration on **Settings → AI systems**; the cards for
that registration are then verified. See
[Which AI system is asking](how-it-works.md#which-ai-system-is-asking).

## Troubleshooting

| What you see | What to do |
|---|---|
| `--print-mcp-token` cannot reach the daemon | Log out and back in once after installing, then check that the daemon is running (your platform's install page, "Troubleshooting"). |
| On Windows the token variable is empty | Run the command as yourself, not elevated, and capture it into a variable as above. |
| `/mcp` shows `privacyfence` as disconnected in local mode | The daemon is stopped, or `web.port` changed after you added it. Start the daemon, then run the `gemini mcp add` command again (remove the old entry first with `gemini mcp remove privacyfence`). |
| `/mcp` shows `privacyfence` as needing authentication in organization mode | Run `/mcp auth privacyfence` and sign in again. |
| The sign-in page never returns to Gemini CLI | Gemini CLI waits for the browser on a `localhost` port, so it cannot sign in over SSH or in a container without a browser. Sign in on a computer with a browser. |
| Tools are listed but none of your services' tools | No service is connected yet. Ask Gemini CLI to call `privacyfence_status`; connect services in Settings (local mode) or at `/connect` (organization mode). See [Connecting a service](connecting-a-service.md). |
| A call waits and nothing happens | It is waiting for your approval. Open Approvals from the companion, or `/approvals` in organization mode. |

Platform-specific problems are on [macOS](install-macos.md#troubleshooting),
[Windows](install-windows.md#troubleshooting) and [Linux](install-linux.md#troubleshooting).
