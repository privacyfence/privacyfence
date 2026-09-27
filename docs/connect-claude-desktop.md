# Connect Claude Desktop

Claude Desktop works with both deployments:

- **Local mode**: PrivacyFence installed on the same computer. Claude Desktop connects through
  one of PrivacyFence's two extensions, `PrivacyFence.mcpb` or `PrivacyFence-no-prompts.mcpb`,
  which finds the running daemon by itself.
- **Organization mode**: PrivacyFence run centrally by your organization. Claude Desktop connects
  to it as a custom connector and you sign in with your organization account.

Which one you have is explained in
[Local mode and organization mode](how-it-works.md#local-mode-and-organization-mode). Claude Desktop
runs on macOS and Windows; on Linux, use [Claude Code](connect-claude-code.md).

## Local mode

PrivacyFence comes with two extensions. Install **one** of them; which one is explained in
[Confirmations](#confirmations):

| Platform | Where the extensions are | How to install one |
|---|---|---|
| macOS | `PrivacyFence.mcpb` and `PrivacyFence-no-prompts.mcpb` on the DMG | Double-click it; see [Install on macOS](install-macos.md#connect-claude-desktop). |
| Windows | `PrivacyFence-<version>.mcpb` and `PrivacyFence-no-prompts-<version>.mcpb` in `%ProgramFiles%\PrivacyFence\` | The installer's last page offers the default one; see [Install on Windows](install-windows.md#connect-claude-desktop). |
| Linux | none | Claude Desktop does not run on Linux. |

Claude Desktop opens and offers to install the extension; accept. There is nothing to configure and
no token to copy. The extension:

1. waits until the daemon's `/mcp` endpoint answers;
2. asks the daemon for your MCP token over its local control channel;
3. reads the `/mcp` URL from the file the daemon writes when it starts (`mcp_url` in the handoff
   directory; see [Platform support](platform-support.md));
4. relays MCP messages between Claude Desktop and `/mcp`, and reads and writes local files for the
   tools that need them (see [Files](#files)).

Nothing is edited in Claude Desktop's configuration, and no token is stored in a file you can read.
On a packaged install the extension never starts the daemon: the daemon belongs to the system and
its own account. If the daemon is stopped, the extension waits for it and logs that you should
choose **Start PrivacyFence…** from the companion's menu.

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

Do not install the extension as well when you use an organization deployment: it only talks to a
PrivacyFence on the same computer.

## Files

- **Local mode, with the extension.** Tools that read or save a file (`drive_upload_file`'s
  `local_path`, `drive_download_file`'s `destination_dir`, email attachments) work with paths on
  your computer. The extension reads or writes the file as you and passes the bytes to or from the
  daemon ([ADR 0007](adr/0007-local-file-bridge.md)). On macOS, the first time a tool reads or
  saves a file outside Claude's own folders, macOS may ask whether Claude may access that folder;
  allow it once per folder.
- **Organization mode, as a custom connector.** A local path means nothing to the server. Claude
  uploads through `privacyfence_create_upload_slot` and a one-time link, and a download comes back
  in the tool result or as a one-time link
  ([ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md)).

Sizes, lifetimes and limits are in [Files](how-it-works.md#files).

## Confirmations

Two things can ask you before a tool runs:

1. **Claude Desktop's own tool prompt.** Claude Desktop can ask before it calls a tool, and offers
   to allow it for the rest of the chat or always. Clients use a tool's annotations to decide when to
   ask.
2. **PrivacyFence's approval card.** A gated call waits for you in Approvals, whatever Claude
   Desktop was told or allowed. This is the confirmation that decides; see
   [Approvals and policy](approvals-and-policy.md).

By default PrivacyFence tells the client what each tool does: reads are read-only, writes are
writes, and the two tools that delete something are destructive
([What the AI system is told](how-it-works.md#what-the-ai-system-is-told)). So Claude Desktop may
ask before a write, in front of PrivacyFence's own card.

In local mode, the extension you install decides which of the two you get:

| Extension | Shown in Claude Desktop as | Claude Desktop asks before a write | PrivacyFence's approval |
|---|---|---|---|
| `PrivacyFence.mcpb` | PrivacyFence | It may, depending on your Claude Desktop settings | Asks, as configured |
| `PrivacyFence-no-prompts.mcpb` | PrivacyFence (no Claude prompts) | No: every tool is advertised read-only | Asks, as configured |

- **Pick `PrivacyFence.mcpb`** if you want Claude Desktop to tell you, before a write, what it is
  about to do, or if you have set Claude Desktop to allow PrivacyFence's tools anyway.
- **Pick `PrivacyFence-no-prompts.mcpb`** if you would otherwise confirm the same write twice, once
  in Claude Desktop and once on PrivacyFence's card. Nothing is less protected: every call still
  goes through PrivacyFence's gate, and the card is the confirmation that decides.

Install only one. With both, Claude Desktop lists every tool twice; to switch, remove the one you
have under **Settings → Extensions** and install the other. The no-prompts extension sends
`X-PrivacyFence-Tool-Annotations: all-read-only` on every request, which wins over the
organization bundle's setting on this computer.

In organization mode there is no extension to choose: the administrator decides for everyone by
building the organization bundle with `--tool-annotations all-read-only`, which also applies in
local mode when no extension asks otherwise. The organization bundle options are listed in
[Configuration reference](configuration-reference.md#organization-config-bundle-org_configjson).

## How the client is identified

Every approval card and audit entry names the AI system that asked. Claude Desktop sends its own
name in the MCP handshake, and in organization mode when it registers. That name is a **claim**:
the card says the caller *says* it is that system and marks it **Not verified**, because any
program can send the same name. In organization mode an administrator can **pin** Claude Desktop's
registration on **Settings → AI systems**; the cards for that registration are then verified.
See [Which AI system is asking](how-it-works.md#which-ai-system-is-asking).

## Troubleshooting

| What you see | What to do |
|---|---|
| Claude Desktop reports no PrivacyFence server, or the extension's tools never appear | The daemon is stopped. Choose **Start PrivacyFence…** from the companion's menu; the extension picks it up by itself. |
| Double-clicking `PrivacyFence.mcpb` does nothing on Windows | Drag the file onto Claude Desktop's **Settings → Extensions** page instead; see [Install on Windows](install-windows.md#connect-claude-desktop). |
| Every tool appears twice | Two PrivacyFence connections are installed: both extensions, or an extension and a custom connector. Remove one. |
| Tools are listed but none of your services' tools | No service is connected yet. Ask Claude to call `privacyfence_status`; connect services in Settings (local mode) or at `/connect` (organization mode). See [Connecting a service](connecting-a-service.md). |
| The custom connector's **Connect** fails | The URL must end in `/mcp` and be reachable over public HTTPS. Your administrator can check it with [the validation checklist](org-mode-setup-guide.md#validation-checklist). |
| A call waits and nothing happens | It is waiting for your approval. Open Approvals from the companion, or `/approvals` in organization mode. |

Platform-specific problems are on [macOS](install-macos.md#troubleshooting) and
[Windows](install-windows.md#troubleshooting).
