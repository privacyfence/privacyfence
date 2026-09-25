# Connecting a service

A **connector** is PrivacyFence's link to one of your accounts: Gmail, Drive, Calendar, Contacts,
Tasks, Apps Script, Slack, Jira, Confluence, Salesforce or Telegram. Connecting one takes two
steps, done by different people:

1. **An administrator registers an app with the provider once** and packages its credentials into
   an organization config bundle (`org_config.json`). Each provider has its own guide — see
   [Provider guides](#provider-guides) below. Telegram needs no registration.
2. **Each user signs in once**, from PrivacyFence, with the provider's own sign-in page. Nobody
   copies or pastes a token.

This page covers step 2, on a desktop install and on an organization server. Every connector
except Telegram uses the same browser sign-in; Telegram asks for a phone number and a code
instead.

## Install the organization config bundle (desktop install)

Your IT team gives you an `org_config.json` file. Install it once; it covers every provider in it.

1. Open **Settings** from the PrivacyFence menu-bar icon (macOS) or tray icon (Windows) — **Open
   Settings** — or, on Linux, from the **Settings** action of the PrivacyFence launcher.
2. On the **General** page, find the **Organization Configuration** card and click **Install
   Organization Config…** (the button reads **Install/Update Organization Config…** once a bundle
   is installed; the card shows **Installed** with a date, or **Not installed**).
3. Choose the `org_config.json` file.

What can stop the install:

- **A page not opened from the companion.** On a privilege-separated install (see
  [security-and-compliance.md](security-and-compliance.md)), only a Settings page opened through
  the companion app can install a bundle. Reopen Settings from the menu-bar/tray icon or launcher.
- **Passkey step-up.** Installing a bundle is a sensitive settings change; with step-up on (the
  default) you confirm it with your passkey.
- **A new signing key.** The first signed bundle an install sees asks you to confirm that its
  signing key should be trusted. After that, only bundles signed with the same key are accepted.
- **Size.** A bundle larger than 1,000,000 bytes is refused.

You can authenticate connectors straight after installing it, without restarting PrivacyFence.
On an organization server there is nothing for users to install: the administrator puts the bundle on the server (see
[org-mode-setup-guide.md](org-mode-setup-guide.md)).

## Connect on a desktop install

Open **Settings → Connectors**. Each row shows the connector, a [status pill](#status-pills), an
**Authenticate…** link (**Reconnect…** once connected) and an on/off switch.

1. Click **Authenticate…** next to the connector. The pill changes to **Connecting…**.
2. A browser tab opens on the provider's sign-in page. Sign in and approve the requested access.
3. The tab shows a confirmation page; go back to Settings. The pill changes to **Connected**.

The new connector's tools are available straight away. PrivacyFence rebuilds its connector set
while running and tells connected MCP clients that its tool list has changed — no restart of
PrivacyFence or of your AI client is needed.

### Where the browser opens

The browser opens **on the machine running PrivacyFence**, not necessarily the device you clicked
from (Settings says so under the Connectors heading).

- On a privilege-separated install, PrivacyFence runs as its own account without access to your
  desktop, so it asks the companion app — the macOS menu-bar icon, the Windows tray icon, or the
  background companion Linux starts at login — to open the tab for you.
- On an install without privilege separation, PrivacyFence opens the tab itself.
- If no browser can be opened, the sign-in link is written to PrivacyFence's log
  ("Please visit this URL to authorize PrivacyFence: …"). Opening that link in a browser on the
  same machine, before the time limit below, still completes the sign-in.

After you approve, the provider sends your browser back to a short-lived listener on this
machine's loopback address (`127.0.0.1`). It runs only while a sign-in is in progress:

| Provider | Loopback port |
|---|---|
| Google (every Google connector) | picked by the operating system for each sign-in |
| Slack | 53682 |
| Salesforce | 53683 |
| Atlassian (Jira and Confluence) | 53684 |

### Time limit and errors

A sign-in not finished within **3 minutes** fails with *"&lt;Connector&gt; authentication failed:
Timed out waiting for sign-in to complete in the browser."* Click **Authenticate…** again.
Any other failure is shown the same way — *"&lt;Connector&gt; authentication failed: …"* — with the
provider's reason; the provider guide's Troubleshooting section explains the common ones.

Only one sign-in per provider can run at a time. A second one while the first is still waiting
fails with *"Could not bind 127.0.0.1:&lt;port&gt; for the OAuth redirect — is another PrivacyFence
sign-in already in progress?"*

### Provider differences

- **Google:** each Google connector (Gmail, Drive, Calendar, Contacts, Tasks, Apps Script) is its
  own sign-in with its own permissions. Authenticate each one you want.
- **Jira and Confluence** share one Atlassian sign-in: authenticating either connects both. If your
  Atlassian account can reach more than one site, PrivacyFence asks *"Choose the Atlassian site to
  connect:"*. Closing that prompt without choosing connects the first site in the list.
- **Telegram** opens a dialog in the Settings page instead of a browser tab: enter your phone
  number, then the code Telegram sends you, then your two-step verification password if your
  account has one. See [telegram-setup.md](telegram-setup.md).

### Reconnect

**Reconnect…** runs the same sign-in again and replaces the stored credentials. Use it when:

- an administrator changed the app's scopes (existing tokens keep the scopes they were issued
  with until you reconnect);
- you want a different account or, for Atlassian, a different site;
- the provider rejects the stored credentials (for example, you removed PrivacyFence from your
  account on the provider's side).

A connector whose credentials have stopped working is not **Connected** — it shows **Not
connected** with **Authenticate…**, which does the same thing.

### Turning a connector off

The switch at the end of each row turns a connector off without deleting its credentials; its
tools disappear from your AI client. Turning it back on gives access back, so it is a sensitive
settings change and asks for your passkey when step-up is on.

PrivacyFence has no **Disconnect** button. To withdraw the access you granted, remove PrivacyFence
from the provider's list of connected apps (for Google, at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions)).

## Status pills

The pill next to each connector in **Settings → Connectors** shows the first of these that
applies:

| Pill | Meaning | What to do |
|---|---|---|
| **Connecting…** | A sign-in is in progress. The Authenticate link is disabled until it finishes or times out. | Finish the sign-in in the browser tab. |
| **Connected** | The connector is signed in and loaded; its tools are available. | Nothing. |
| **Disabled** | The connector is switched off. | Turn the switch on to use it. |
| **Organization config missing** | The installed bundle has no section for this provider (Google, Slack, Salesforce or Atlassian), or no bundle is installed. | [Install the bundle](#install-the-organization-config-bundle-desktop-install), or ask your administrator to add the provider. |
| **App credentials missing** | Telegram only: this build of PrivacyFence carries no Telegram app credentials. | See [telegram-setup.md](telegram-setup.md). |
| **Not connected** | The provider is configured but the connector is not loaded: you never signed in, the credentials expired or were revoked, or the connector failed its check when PrivacyFence built it. | Click **Authenticate…**. If it keeps failing, check the log (see [platform-support.md](platform-support.md)). |

Your AI client sees the same state through the `privacyfence_status` tool: each connector is
reported as authenticated or not, with `no_org_config` (never configured), `not_authenticated`
(never signed in, or the credentials expired) or a short reason.

## Connect on an organization server

On an organization deployment there is no Settings page for connectors. Each user connects their
own accounts on the server's **Connections** page:

1. Go to `https://<your-server>/connect`. If you are not signed in, you are sent to `/login` first
   to sign in with your organization's identity provider.
2. Each service has one row:
   - **Not set up by your organization** — the server's bundle has no section for it; there is
     nothing to click.
   - no badge and a **Connect** link — configured, not yet connected by you.
   - **Connected** and a **Reconnect** link — connected.
3. Click **Connect**. Your browser goes to the provider's consent page and then back to
   `/connect`, which shows *"&lt;Service&gt; connected."* — or *"Could not connect &lt;Service&gt; --
   either sign-in was declined, or your organization hasn't configured it yet."*

A sign-in attempt is valid for **10 minutes**. Returning from the provider after that (or reusing
an old tab) shows *"Invalid or expired sign-in attempt. Return to /connect and try again."*

The connector is available to your AI client as soon as you land back on `/connect`; the server
builds each user's connectors on their next request, without a restart.

Differences from a desktop install:

- **Jira and Confluence** still share one sign-in, but the server does not ask which site to use:
  it connects the first site your Atlassian account can reach.
- **Telegram** has its own box on the page: enter your phone number and click **Connect
  Telegram**, then enter the code, then your two-step verification password if asked. **Cancel**
  abandons the attempt.

## Provider guides

Each guide covers registering the app, the values to enter, building the bundle and
provider-specific troubleshooting:

| Connectors | Guide |
|---|---|
| Gmail, Drive, Calendar, Contacts, Tasks, Apps Script | [google-cloud-setup.md](google-cloud-setup.md) |
| Slack | [slack-setup.md](slack-setup.md) |
| Salesforce | [salesforce-setup.md](salesforce-setup.md) |
| Jira, Confluence | [atlassian-setup.md](atlassian-setup.md) |
| Telegram | [telegram-setup.md](telegram-setup.md) |
