# Getting started

PrivacyFence sits between your AI client and your business accounts (Google, Slack, Salesforce,
Atlassian, Telegram) and asks you before anything sensitive happens. This page is the overview:
what you need, which guide to follow, and what your first approval looks like.

## What you need

- A supported computer: see the [support matrix](platform-support.md#support-matrix).
- Administrator rights on it, for the install.
- An MCP-compatible AI client. Claude Desktop and Claude Code are tested with every install;
  claude.ai works through an [organization deployment](#local-or-organization-deployment) only.
- A passkey authenticator that asks for your PIN, fingerprint or face: Touch ID on a Mac, Windows
  Hello on Windows, a USB or NFC security key, or your phone (the browser shows a QR code). On
  Linux, where there is usually nothing built in, use a security key or your phone.
- The accounts you want PrivacyFence to govern, and the organization configuration file your IT
  administrator gave you, if any.

## Pick your platform

- [Install on macOS](install-macos.md)
- [Install on Windows](install-windows.md)
- [Install on Linux](install-linux.md)

Each guide covers download and checksum, install, connecting Claude Desktop and Claude Code,
troubleshooting, uninstall and purge.

## Local or organization deployment

The guides above install PrivacyFence **locally**: it runs on your own computer and listens on
`localhost` only, so a client on the same computer (Claude Desktop, Claude Code) can reach it and
claude.ai cannot. For claude.ai, or to run PrivacyFence once for a whole team, deploy it in
**organization mode** on a server: see [Organization deployment](org-mode-setup-guide.md). If your
organization already runs it and gave you a URL such as `https://pf.example.com`, there is nothing
to install: add `https://pf.example.com/mcp` to your client as an MCP server, sign in with your
organization account when asked, and connect your services at `https://pf.example.com/connect`.

## Your first approval

1. **Add a passkey.** After you log back in, the companion (menu-bar icon, tray icon, or
   Applications-menu entry) opens the **Passkeys** page for you, and keeps doing so at every start
   until one is enrolled. Until then PrivacyFence approves nothing. Write down the one-time
   recovery code it shows: it is the only way back from a lost authenticator. The companion's
   **New Recovery Code…** issues a new one later but never shows the old one again.
2. **Connect your services.** Open **Settings** from the companion, upload your organization
   configuration if you have one, and sign in to the services you want on the **Connectors** page
   ([Connecting a service](connecting-a-service.md)). Your AI client sees the new tools without
   reconnecting.
3. **Ask your AI client for something**, such as "summarize my last email". If policy allows it,
   it runs and is written to the audit log. Otherwise the call waits for you: open **Approvals**
   from the companion (or the link the client relays), choose **Review →**, and read the card.
4. **Decide.** **Deny** always works at once. **Allow** asks for your passkey (Touch ID, Windows
   Hello) whenever step-up applies. On a packaged install step-up is on by default with the scope
   `writes_and_pii_reads`: every write, and every read PrivacyFence flagged as containing personal
   data. Changing your rules, policy or PII settings asks for the passkey too. Cards, rules and
   step-up settings are explained in [Approvals and policy](approvals-and-policy.md).

## Troubleshooting on every platform

Platform-specific problems are in each install guide. The `status` command below is the one for
your platform in [Platform support](platform-support.md#start-stop-and-status).

| What you see | What it means and what to do |
|---|---|
| `status` shows `PENDING USER` | The install is separated, but no person has been added to its service group yet (it was installed as root, unattended, or by a management tool). The companion completes this at the first login, after asking for an administrator password; or an administrator runs `enable --for-user <name>` (`-ForUser` on Windows). Then log out and back in. |
| `status` shows `PENDING SIGNOUT` (Windows) | You were added to `PrivacyFenceUsers` after this session started. Sign out and back in. |
| Another person on the same computer wants to use PrivacyFence | An administrator runs `enable --for-user <name>` for them ([Adding a second account](platform-support.md#adding-a-second-account)). They get their own separate policy, approvals and passkeys; the install's owner stays the same. |
| The companion says the daemon is not running, or Claude Desktop reports no PrivacyFence server | Choose **Start PrivacyFence…** from the companion's menu (on Linux, the **Start PrivacyFence** action of the Applications-menu entry). The Claude Desktop extension never starts the daemon on a packaged install; it waits for it. |
| The client can't reach PrivacyFence right after the install | You haven't logged out and back in since installing. |
| The client lists only `privacyfence_*` tools | No service is connected yet. Ask the client to run `privacyfence_status`; it reports what is missing. |
| The client says it can't give you a sign-in link | Working as designed: PrivacyFence never hands a sign-in to the program it governs. Use the companion. |
| Nothing can be approved, and every page says so | No passkey is enrolled. See step 1 above. |
