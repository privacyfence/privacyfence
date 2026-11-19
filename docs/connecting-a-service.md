# Connecting a service

A **connector** is PrivacyFence's link to one of your accounts: Gmail, Drive, Calendar, Contacts,
Tasks, Apps Script, Slack, Jira, Confluence, Salesforce, Grist, Miro, Peek or Telegram. Connecting
one takes two steps, done by different people:

1. **An administrator offers the service on the Admin page**, once. Google, Slack, Salesforce,
   Atlassian, Miro and Telegram come with built-in PrivacyFence apps, so there is nothing to
   register and no bundle: a fresh Docker single-user install offers all six already, and a
   multi-user server offers a service when an administrator switches it on. An organization that wants its own app instead registers one with the provider
   (see [Provider guides](#provider-guides)) and gives it to PrivacyFence as a signed connector
   config on **Admin → Services**.
2. **Each user signs in once**, from PrivacyFence, with the provider's own sign-in page. Nobody
   copies or pastes a token.

This page covers the Admin page's part and step 2. Every connector except Telegram uses the same browser sign-in; Telegram asks for a phone
number and a code instead.

## The Admin page

**Admin** is an item in the navigation. It is for administrators (the one administrator, in
single-user mode); anyone else who opens it sees "Only administrators can open this page." Every
sensitive change on it asks for a passkey, even when step-up is otherwise off.

- **Services** lists every service with a switch to offer or stop offering it, the app it uses (your
  organization's own, or the built-in PrivacyFence app through `oauth.privacyfence.eu`), a link to the
  provider's setup guide and, per provider, what it needs: Salesforce's install link and login URL,
  Slack's own-app button, Peek and Grist addresses and automatic accounts.
- **Features** holds Jira Assets, the Grist and Peek server addresses, unattended sessions, web push
  and the room directory.
- Administrators also get File delivery, Sign-in, Step-up, Scoped tokens,
  Audit forwarding, Privacy Filter and Stats.

**Changing a service's app makes people reconnect.** Uploading a connector config, removing it
(**Remove our own app**) or installing a bundle with different credentials means everyone who
connected that service has to connect again; the Admin page says so next to the row.

Settings is personal: it holds your own rules and tokens, for administrators as for everyone else.

## Connect

There is no Settings page for connectors. Each user connects their
own accounts on the server's **Connections** page (a service appears there once an administrator
offers it on **Admin → Services**):

1. Go to `https://<your-server>/connect`. If you are not signed in, you are sent to `/login` first
   to sign in with your organization's identity provider.
2. Each service has one row:
   - **Not set up by your organization** — the service is not offered on the Admin page, or no app
     is available for it; there is nothing to click.
   - no badge and a **Connect** link — configured, not yet connected by you.
   - **Connected** and a **Reconnect** link — connected.
3. Click **Connect**. Your browser goes to the provider's consent page and then back to
   `/connect`, which shows *"&lt;Service&gt; connected."* — or *"Could not connect &lt;Service&gt; --
   either sign-in was declined, or your organization hasn't configured it yet."*

A sign-in attempt is valid for **10 minutes**. Returning from the provider after that (or reusing
an old tab) shows *"Invalid or expired sign-in attempt. Return to /connect and try again."*

The connector is available to your AI client as soon as you land back on `/connect`; the server
builds each user's connectors on their next request, without a restart.

Provider differences:

- **Google:** each Google connector (Gmail, Drive, Calendar, Contacts, Tasks, Apps Script) is its
  own sign-in with its own permissions. Connect each one you want.
- **Jira and Confluence** share one Atlassian sign-in: connecting either connects both. The server
  does not ask which site to use: it connects the first site your Atlassian account can reach.
- **Telegram** has its own box on the page: enter your phone number and click **Connect
  Telegram**, then enter the code, then your two-step verification password if asked. **Cancel**
  abandons the attempt. See [telegram-setup.md](telegram-setup.md).
- **Peek** uses Peek's own device sign-in, so there is no OAuth app to register: PrivacyFence shows
  a code, you approve it in Peek in your browser, then choose **Done**. The Peek server's address is
  set on **Admin → Features** (or by `PRIVACYFENCE_PEEK_BASE_URL` on the server); Peek is offered
  only when it is set. A service that is not offered on the Admin page is not listed. See
  [peek-setup.md](peek-setup.md).

### Reconnect

**Reconnect** runs the same sign-in again and replaces the stored credentials. Use it when:

- an administrator changed the app's scopes (existing tokens keep the scopes they were issued
  with until you reconnect) or switched the service to a different app;
- you want a different account or, for Atlassian, a different site;
- the provider rejects the stored credentials (for example, you removed PrivacyFence from your
  account on the provider's side).

PrivacyFence has no **Disconnect** button. To withdraw the access you granted, remove PrivacyFence
from the provider's list of connected apps (for Google, at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions)).

Your AI client sees the state of each connector through the `privacyfence_status` tool: each one is
reported as authenticated or not, with `no_org_config` (never configured), `not_authenticated`
(never signed in, or the credentials expired) or a short reason.

An organization server can also put hosted MCP servers (Canva or Framer, for example) behind PrivacyFence. A server the operator declared appears on the Connect page as one more row; **Connect** signs you in at the vendor with your own account. See [Upstream MCP servers](upstream-mcp.md).

## Provider guides

The built-in apps need none of these. Each guide is for an organization that wants its own app
instead: it covers registering the app, the values to enter, the connector config or bundle, and
provider-specific troubleshooting:

| Connectors | Guide |
|---|---|
| Gmail, Drive, Calendar, Contacts, Tasks, Apps Script | [google-cloud-setup.md](google-cloud-setup.md) |
| Slack | [slack-setup.md](slack-setup.md) |
| Salesforce | [salesforce-setup.md](salesforce-setup.md) |
| Jira, Confluence | [atlassian-setup.md](atlassian-setup.md) |
| Grist | [grist-setup.md](grist-setup.md) |
| Miro | [miro-setup.md](miro-setup.md) |
| Peek | [peek-setup.md](peek-setup.md) |
| Telegram | [telegram-setup.md](telegram-setup.md) |
