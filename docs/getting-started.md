# Getting started

PrivacyFence sits between your AI client and your business accounts (Google, Slack, Salesforce,
Atlassian, Miro, Telegram and more) and asks you before anything sensitive happens. This page is the overview:
what you need, which guide to follow, and what your first approval looks like.

## What you need

- Docker Engine 24 or newer with the Compose plugin 2.24 or newer, on the computer or server that
  runs PrivacyFence ([Organization deployment with Docker](org-mode-docker.md#prerequisites)).
- An MCP-compatible AI client. Claude Desktop and Claude Code connect to a single-user install on
  your own computer or to an organization server; claude.ai, ChatGPT on the web and Gemini
  Enterprise need an [organization deployment](#single-user-or-organization-deployment) on a public
  HTTPS address.
- A passkey authenticator that asks for your PIN, fingerprint or face: Touch ID on a Mac, Windows
  Hello on Windows, a USB or NFC security key, or your phone (the browser shows a QR code). On
  Linux, where there is usually nothing built in, use a security key or your phone.
- The accounts you want PrivacyFence to govern. PrivacyFence's built-in apps cover Google, Slack,
  Salesforce, Atlassian, Miro and Telegram; an organization that registers its own apps gets a
  signed connector config from its IT administrator.

## Single-user or organization deployment

PrivacyFence runs as the Docker image in one of two shapes. For yourself, on your own computer, run
it in **single-user mode**: one container, one administrator, served only at `http://localhost`
([Run it on this computer](org-mode-docker.md#run-it-on-this-computer)). For claude.ai, or to run
PrivacyFence once for a whole team, deploy it as an **organization server**: see
[Organization deployment](org-mode-setup-guide.md). If your organization already runs it and gave
you a URL such as `https://pf.example.com`, there is nothing to install: add
`https://pf.example.com/mcp` to your client as an MCP server, sign in with your organization account
when asked, and connect your services at `https://pf.example.com/connect`.

Then connect your AI client: [Claude Desktop](connect-claude-desktop.md),
[Claude Code](connect-claude-code.md), [ChatGPT](connect-chatgpt.md),
[claude.ai](connect-claude-ai.md) or [Gemini Enterprise](connect-gemini-enterprise.md).

A desktop install of PrivacyFence 7 (the macOS, Windows and Linux installers) is not read by
PrivacyFence 8: see [Moving to organization mode](moving-to-organization-mode.md).

PrivacyFence offers Google, Slack, Salesforce, Atlassian, Miro and Telegram with its built-in apps,
so a fresh single-user install needs no bundle and no app registration. An administrator decides
which services are offered on the **Admin** page. A signed connector config is only for an
organization's own apps. See [Connecting a service](connecting-a-service.md#the-admin-page).

## Your first approval

1. **Add a passkey.** Open `http://localhost:8765/login` and enter the setup code from the
   container's log (`docker logs`). It signs you in and takes you to **Passkeys**. Until a passkey is
   enrolled PrivacyFence approves nothing. Write down the one-time recovery code it shows: it is the
   only way back from a lost authenticator. On an organization server you sign in with your
   organization account and add your passkey on the same page.
2. **Connect your services.** Open **Connect** (`/connect`) and sign in to the services you want
   ([Connecting a service](connecting-a-service.md)). Your AI client sees the new tools without
   reconnecting. Moving from a desktop install? You can bring your always-allow rules with you:
   [Moving a person from a desktop install](org-mode-setup-guide.md#moving-a-person-from-a-desktop-install).
3. **Ask your AI client for something**, such as "summarize my last email". If policy allows it,
   it runs and is written to the audit log. Otherwise the call waits for you: open **Approvals**
   (`/approvals`, or the link the client relays), choose **Review →**, and read the card.
4. **Decide.** **Deny** always works at once. **Allow** asks for your passkey (Touch ID, Windows
   Hello) whenever step-up applies. On a single-user install step-up is on by default with the scope
   `writes_and_pii_reads`: every write, and every read PrivacyFence flagged as containing personal
   data. Changing your rules, policy or PII settings asks for the passkey too. Cards, rules and
   step-up settings are explained in [Approvals and policy](approvals-and-policy.md).

## Troubleshooting

| What you see | What it means and what to do |
|---|---|
| The client lists only `privacyfence_*` tools | No service is connected yet. Ask the client to run `privacyfence_status`; it reports what is missing. |
| The client says it can't give you a sign-in link | Working as designed: PrivacyFence never hands a sign-in to the program it governs. Open the server's Connect page yourself. |
| Nothing can be approved, and every page says so | No passkey is enrolled. See step 1 above. |
| The setup code does not work | It works once, five wrong attempts use it up, and it stops working for good once a passkey is enrolled. Restart the container to get a new one, as long as no passkey is enrolled. |
| Claude Desktop or Claude Code cannot connect | Check that the container runs and that the URL is `http://localhost:8765/mcp` (not `127.0.0.1`). |
