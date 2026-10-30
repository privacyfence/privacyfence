# Organization deployment

This guide sets up PrivacyFence as one central Linux service that many people in an organization
use. Everyone signs in with the organization's identity provider, connects their own accounts, and
approves their own AI requests in the browser. It is written for the IT administrator who runs the
server.

For a single person on their own computer, use the desktop install instead
([getting started](getting-started.md)).

## Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Service account and install](#3-service-account-and-install)
4. [Identity provider](#4-identity-provider)
5. [Connector apps](#5-connector-apps)
6. [Server settings and your own apps](#6-server-settings-and-your-own-apps)
7. [Reverse proxy and TLS](#7-reverse-proxy-and-tls)
8. [Hardened systemd unit](#8-hardened-systemd-unit)
9. [First sign-in and validation](#9-first-sign-in-and-validation)
10. [Install-wide and per-user policy](#10-install-wide-and-per-user-policy)
11. [AI systems](#11-ai-systems)
12. [Approvals and step-up](#12-approvals-and-step-up)
13. [File delivery](#13-file-delivery)
14. [Operations](#14-operations)
15. [Troubleshooting](#15-troubleshooting)

## 1. Overview

An organization deployment has six parts:

| Part | What it is |
|---|---|
| The daemon | `privacyfence-app`, installed from PyPI into a virtual environment and run by systemd as a dedicated, unprivileged account. It listens on `127.0.0.1:8765`. |
| The server settings | A few environment variables (`privacyfence.env`, or the systemd unit's `EnvironmentFile`): that this is an organization server, its public address, and the identity provider. There is no bundle to build. |
| The Admin page | `/admin`, for administrators: which services are offered, file delivery, sign-in restrictions, step-up, scoped tokens, audit forwarding and the rest. It writes `org/admin_config.json`. |
| A reverse proxy | Caddy or nginx on the same host. It terminates HTTPS on your public hostname and forwards to the daemon. |
| Your identity provider | Any OpenID Connect (OIDC) provider your staff already sign in to, or, for a small Google Workspace team, PrivacyFence's built-in Google sign-in. It decides who a person is. |
| Connector apps | One OAuth app per service you offer. By default PrivacyFence's built-in apps, reached through the PrivacyFence relay at `oauth.privacyfence.eu`; or your organization's own app, uploaded as a signed connector config. Each person authorizes their own account against it. No credentials are shared between people. |

**The order of a first setup.**

1. Point DNS at the server and put the reverse proxy in front of it ([sections 3, 7 and 8](#3-service-account-and-install)).
2. Register one OIDC client with your identity provider ([section 4](#4-identity-provider)), or choose the built-in Google sign-in.
3. Write the environment file ([section 6](#6-server-settings-and-your-own-apps)) and start the daemon.
4. Sign in as a named administrator ([section 9](#9-first-sign-in-and-validation)).
5. Open **Admin → Services** and offer the services you want. With the built-in apps that is all: the server registers with the relay by itself ([section 5](#5-connector-apps)). With the built-in Google sign-in, publish the DNS records the log lists first.
6. Optional: upload a connector config for any provider where you want your own app, and set up step-up, file delivery and the other sections of the [Admin page](#the-admin-page).

What people see, all at `https://<your-hostname>`:

| Page | Purpose |
|---|---|
| `/login` | Sign in through the identity provider. |
| `/signed-out` | Where **Sign out** lands, with a link to sign in again. |
| `/connect` | Connect or reconnect Gmail, Drive, Calendar, Contacts, Tasks, Apps Script, Slack, Salesforce, Jira, Confluence, Grist, Miro, Peek and Telegram. Only the services an administrator offered are listed. |
| `/approvals` | Pending AI requests to approve or deny. |
| `/security` | Enroll and remove passkeys. |
| `/settings` | Each person's own always-allow rules and recent audit entries. |
| `/admin` | Administrators only: everything install-wide ([The Admin page](#the-admin-page)). `/settings/privacy` forwards here. |
| `/mcp` | The MCP endpoint AI clients connect to. It uses OAuth 2.1 with dynamic client registration. |

**This is the only way claude.ai can use PrivacyFence.** A desktop install listens on `localhost`,
which claude.ai's servers cannot reach. Claude Desktop, Claude Code and other MCP-compatible clients
can use either kind of install. [Section 9](#9-first-sign-in-and-validation) shows how to add each
client.

To run the daemon and the reverse proxy as containers, follow
[Organization deployment with Docker](org-mode-docker.md) instead of sections 3, 7 and 8; every
other section applies unchanged.

## 2. Prerequisites

- **A Linux server.** Ubuntu 24.04 is the reference: it ships Python 3.12, and you add
  `python3-venv`. Any other Linux with Python 3.11 or newer, the `venv` module and systemd works the
  same way. Do not install the PrivacyFence `.deb` on this host. The `.deb` is the desktop package:
  it creates its own `privacyfence` account and `/var/lib/privacyfence` layout, and the daemon
  installed here would pick that layout up.
- **A public hostname** (for example `pf.acme.example.com`) with DNS pointing at the server, and a
  TLS certificate for it. Caddy obtains one automatically if ports 80 and 443 are reachable. The
  hostname must be reachable from the internet if people will use claude.ai, because claude.ai
  connects from Anthropic's servers, not from the person's browser.
- **Outbound HTTPS** from the server to your identity provider and to the APIs of every connector
  you offer, to `oauth.privacyfence.eu` if any service uses a built-in app or you use the built-in
  Google sign-in ([section 5](#5-connector-apps)), and, unless you turn push notifications off, to
  the browser push services:
  `*.push.apple.com`, `fcm.googleapis.com`, `*.push.services.mozilla.com` and
  `*.notify.windows.com` ([section 12](#notifications-on-a-phone)).
- **Admin access to your identity provider**, to register one OIDC client.
- **Admin access to each connector's developer console** for the services you want to offer.
- **Only if you register your own connector apps:** a way to sign a connector config. That is
  `sign_connector_config.py`, which is not part of the PyPI package (download it from the GitHub
  Release that matches the version you install, where it is attached to every stable release, or
  take `scripts/sign_connector_config.py` from the repository at that version's tag; it needs the
  `cryptography` package), or the browser tool ([section 6](#signed-connector-configs)). Built-in apps
  need neither.

## 3. Service account and install

With the Docker image, skip this section and sections 7 and 8; see
[Organization deployment with Docker](org-mode-docker.md).

Create a dedicated system account. Its home directory holds all PrivacyFence data:

```bash
sudo apt install python3-venv
sudo useradd --system --home-dir /var/lib/privacyfence-org --create-home \
  --shell /usr/sbin/nologin privacyfence-org
```

Install PrivacyFence into a virtual environment owned by root, so the service cannot change its own
code:

```bash
sudo python3 -m venv /opt/privacyfence/venv
sudo /opt/privacyfence/venv/bin/pip install privacyfence
```

To install a specific version, use `pip install privacyfence==X.Y.Z`. PyPI carries stable releases
only.

The daemon keeps its data in `~/.privacyfence/` of the account it runs as, here
`/var/lib/privacyfence-org/.privacyfence/`. This guide calls that directory the **data directory**.
It creates every directory there with mode `0700` and refuses to start in organization mode if the
data directory, its `org/` directory, or its `users/` tree grants any access to group or others.
Create the `org/` directory now, as the service account, so it has the right owner before the daemon
first writes there:

```bash
sudo -u privacyfence-org install -d -m 700 /var/lib/privacyfence-org/.privacyfence/org
```

The layout of the data directory is in [section 14](#backup).

## 4. Identity provider

Pick one of two ways to sign people in:

- **Your own identity provider (recommended).** Register one OIDC client for PrivacyFence with it
  (for example a "Web application" client in Google Cloud, an app registration in Microsoft Entra ID,
  or a client in Okta or Keycloak). Note its **issuer URL**, **client ID** and **client secret**.
  This is what the rest of this section describes.
- **The built-in Google sign-in**, for a small Google Workspace team that would rather register
  nothing. It is opt-in and has its own rules: see [Built-in Google sign-in](#built-in-google-sign-in).

Register these three redirect URIs on it, with your own hostname:

| Redirect URI | Used when |
|---|---|
| `https://pf.acme.example.com/oauth/idp/callback` | An AI client signs a person in through `/mcp`. |
| `https://pf.acme.example.com/oauth/idp/login-callback` | A person signs in to the web pages at `/login`. |
| `https://pf.acme.example.com/oauth/stepup/callback` | A person confirms an approval by signing in again, when step-up allows that ([section 12](#12-approvals-and-step-up)). |

Requirements the daemon checks:

- The provider publishes OIDC discovery at `<issuer>/.well-known/openid-configuration`, with
  `issuer`, `authorization_endpoint`, `token_endpoint` and `jwks_uri`. The daemon fetches it at
  startup, so the provider must be reachable then.
- The issuer and every endpoint in the discovery document use HTTPS. The daemon refuses plain HTTP.
- ID tokens are signed with `RS256` or `ES256` and carry a `sub` claim.
- Sign-in requests the scopes `openid email profile`.

You give the server the client in the environment file ([section 6](#6-server-settings-and-your-own-apps)):

```ini
PRIVACYFENCE_IDP_ISSUER=https://idp.acme.example.com
PRIVACYFENCE_IDP_CLIENT_ID=<client id>
PRIVACYFENCE_IDP_CLIENT_SECRET_FILE=/etc/privacyfence-org/idp_client_secret
```

Set the issuer and the client ID together or not at all. The secret comes from
`PRIVACYFENCE_IDP_CLIENT_SECRET` or, better, from the file named by `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE`
(a Docker secret; the value then does not show in `docker inspect`), never both. Every
variable is listed in the [configuration reference](configuration-reference.md#environment-variables).

Each person's data lives under `users/<sub>/` in the data directory, keyed by the provider's `sub`
claim. A `sub` that contains characters other than letters, digits, `.`, `_`, `@` and `-` (or is
longer than 200 characters) is stored under a stable hash instead (`idp-<hash>`).

### Who is an admin

Admins see the **Admin** page ([The Admin page](#the-admin-page)). Nobody is an admin unless you
name them, in one of two ways, in the environment file. A person who matches either is an admin.

- **By email**, with `PRIVACYFENCE_ADMIN_EMAILS=alice@acme.example.com,bob@acme.example.com`
  (comma-separated). A person is an admin when the identity provider says their address is
  **verified** (`email_verified` is exactly `true` in the ID token). With Google the account's
  Workspace domain (`hd` claim) must also match the address. Matching ignores case and surrounding
  spaces; an address with a non-ASCII character never matches.
- **By group or role claim**, with `PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM=groups` and
  `PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES=privacyfence-admins` (values comma-separated). A person is an
  admin when the claim contains any of the values. A claim that is a single string is matched like a
  one-item list. The match is exact and case-sensitive.

**Microsoft Entra ID never sends `email_verified`, so its admins cannot be named by email: use the
group (or role) claim.** Providers that do send it (Google, Okta, Keycloak, authentik, Auth0 with
verified addresses) can use either; look at a real ID token before you rely on it.

**A multi-tenant issuer such as `accounts.google.com` accepts any Google account** that has verified
its address, so `PRIVACYFENCE_ADMIN_EMAILS` alone is not enough to keep strangers out of the sign-in
itself. Pair it with allowed domains (Admin → Sign-in, [below](#who-may-sign-in)), or set the
OAuth consent screen's user type to **Internal** in Google Cloud so that only your Workspace
accounts can reach it.

The daemon reads the claim from the **ID token** only, and it always requests the scopes
`openid email profile`. It cannot ask for an extra scope such as `groups`. A claim that your
provider adds only when another scope is requested, or puts only in the access token, never
reaches the daemon, and nobody becomes an admin that way.

A group or role claim works only when you configure the provider to put it in the ID token:

| Identity provider | Use | What to set up at the provider | Value to set |
|---|---|---|---|
| Google | `PRIVACYFENCE_ADMIN_EMAILS` | Nothing. Google ID tokens carry no groups and no Workspace admin role, so `groups` never matches. | Each admin's address. |
| Microsoft Entra ID | `roles` (recommended) | Under **App roles**, create a role, for example `PrivacyFence.Admin`, then assign people or groups to it in **Enterprise applications**. Assigned roles are put in the ID token. | The role's **Value**, for example `PrivacyFence.Admin`. |
| Microsoft Entra ID | `groups` | Under **Token configuration**, add a groups claim and include it in the ID token. | The group's **Object ID** (a GUID), not its name. A person in more than 200 groups gets no `groups` claim; choose **Groups assigned to the application** to avoid that. |
| Okta | `groups` | Add a claim named `groups` of type *Groups* with a filter, for example *Equals* `privacyfence-admins`. On the org authorization server set it under the app's **Sign On** > **OpenID Connect ID Token**. On a custom authorization server add it under **Claims**, with **Include in token type** *ID Token*, *Always*. *Always* matters: with *Any scope* or the `groups` scope it is left out. | The group name. |
| Keycloak | `groups` | On the client's dedicated scope add a **Group Membership** mapper with **Token Claim Name** `groups`, **Add to ID token** on and **Full group path** off. With the full path on, the value is `/privacyfence-admins`, with a leading slash. | The group name. |
| authentik | `groups` | Nothing. The default `profile` scope mapping includes `groups`. | The group name. |
| Auth0 | a custom claim | Auth0 adds no groups or roles to the ID token. Add a post-login Action that sets a namespaced claim, for example `api.idToken.setCustomClaim("https://acme.example.com/roles", event.authorization.roles)`. | `PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM=https://acme.example.com/roles` and the role name. |

For example, Google:

```ini
PRIVACYFENCE_ADMIN_EMAILS=alice@acme.example.com,bob@acme.example.com
```

An Entra ID role, or a group on Okta, Keycloak or authentik:

```ini
PRIVACYFENCE_IDP_ADMIN_GROUP_CLAIM=roles
PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES=PrivacyFence.Admin
```

Do not use `hd` (the Workspace domain) as the claim: every account in the domain has the same value,
so every account would be an admin.

Before you start the server, sign in to the provider as an admin and look at a real ID token, for
example with the provider's token preview (Okta's **Token Preview**, Keycloak's **Client scopes** >
**Evaluate** > **Generated ID token**) or by decoding one. Check that the claim is there and that
its value is exactly what you set. The daemon does not log the claims.

Admin status is decided at sign-in and kept for the browser session. To add or remove an admin
later, change the variable in the environment file, restart the daemon, and have the admins sign out
and back in. The Admin page lists the administrators named by email read-only: it never edits them.

One Google OAuth client can also serve Peek and Grist sign-in if you add their redirect URIs to it,
but a separate client per service in the same Google project is better: each secret can then be
rotated or revoked on its own.

### Who may sign in

By default, everyone your identity provider authenticates may use PrivacyFence. To narrow that, use
the **Sign-in** section of the Admin page (a change is saved and applies at the next restart):

- **Allowed email domains**, for example `acme.example.com`: only email addresses at these domains.
- **Groups claim** and **Required groups**, for example `groups` and `privacyfence-users`: only
  people whose claim contains one of the values. This is independent of the admin claim, but it is
  matched the same way: the claim must be in the ID token, as set up in the table under
  [Who is an admin](#who-is-an-admin). With Google there is no such claim; use allowed domains.

A person who fails either check is refused, and no session is created. The checks apply to
administrators too: an admin outside the allowed domains cannot sign in.

**Breaking in 7.0: allowed domains now need a verified email.** A sign-in is admitted by an allowed
domain only when the identity provider reports the address as verified (`email_verified` is exactly
`true`); a missing or false value is refused. With Google the account must also carry a matching
`hd`. Microsoft Entra ID never sends `email_verified`, so an organization on Entra that restricts
sign-in to domains sets `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL=true` in the environment file to keep
that working (on an upgraded server that still has its bundle, retire the bundle first: its identity
provider settings win over the variable). Google's `hd` rule still applies, and this never makes anyone an admin. Do it only
when your tenant issues only addresses it has verified.

### Built-in Google sign-in

PrivacyFence can sign people in with its own Google OAuth client instead of one you register. It is
opt-in, meant as the quick start for a **small Google Workspace team**: no app to create at Google.
For anything bigger, or if you need Workspace-level control (an Internal consent screen, groups) or
independence from PrivacyFence's services, register your organization's own identity provider app
instead and use the rest of this section.

```ini
PRIVACYFENCE_MODE=org
PRIVACYFENCE_ISSUER_URL=https://pf.acme.example.com
PRIVACYFENCE_IDP=builtin-google
PRIVACYFENCE_ADMIN_EMAILS=alice@acme.example.com,bob@acme.example.com
PRIVACYFENCE_ALLOWED_DOMAINS=acme.example.com
```

- **Requirements.** `PRIVACYFENCE_ADMIN_EMAILS` (at least one), and a public `https://` server
  address with no path, because the sign-in goes through the PrivacyFence relay
  ([section 5](#5-connector-apps)). The build must carry the built-in app; a source checkout does
  not. It cannot be combined with any `PRIVACYFENCE_IDP_*` variable, `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL`
  included. Any other value for `PRIVACYFENCE_IDP` refuses the start.
- **Default deny.** Any Google account can finish Google's side of the sign-in, so the server
  decides alone and refuses everyone by default. Only the listed administrators and people whose
  address is in an allowed domain may sign in, each with a verified address and a Google Workspace
  domain (`hd` claim) that matches it. `PRIVACYFENCE_ALLOWED_DOMAINS` (comma-separated) fills the
  allowed domains on the first start only; afterwards edit them on **Admin → Sign-in**, which also
  names the built-in sign-in and shows the administrators read-only.
- **Not supported with it:** personal Google accounts (Gmail, which has no Workspace domain, and
  `gmail.com` and `googlemail.com` are never accepted as a sign-in domain), and groups: Google sends
  none, so administrators are named by email only.
- **Each sign-in domain is proven in DNS.** The domain of every administrator and every allowed
  domain, at most 20, must have a TXT record `_privacyfence-relay.<domain>` with the value
  `pf-relay=<SHA-256 of the server's relay token>`. The server logs the exact record to add for each
  missing domain at every start and after each registration, and
  `privacyfence-app --relay-dns-records` prints the same lines and exits. Nobody from a domain can
  sign in until its record exists; a domain is checked again within the hour. Until the server has
  registered with the relay `/login` says `Sign-in is starting up`, and while no domain is proven it
  says `Sign-in is waiting for DNS`. The token lives in `org/relay_token.txt`: back it up, because
  replacing it means publishing every record again with the new value the log gives.
- **It depends on PrivacyFence's services.** Every sign-in, and every approval that asks for a fresh
  sign-in, goes through `oauth.privacyfence.eu` and the Google Cloud project that owns the sign-in
  client (`privacyfence-signin`). If either is down or withdrawn, nobody can sign in to any server
  that uses it. The relay terms are at <https://privacyfence.eu/relay-terms/>.

[ADR 0267](adr/0267-an-organization-server-may-opt-in-to-the-built-in-google-sign-in.md) records
the reasoning.

## 5. Connector apps

Each service you offer signs a person in with an OAuth app. For Google, Slack, Salesforce,
Atlassian and Miro you choose, per provider, between:

- **The built-in PrivacyFence app (the default).** You register nothing. The provider redirects to
  PrivacyFence's relay at `oauth.privacyfence.eu`, which forwards the browser to your server. See
  [Built-in apps through the relay](#built-in-apps-through-the-relay).
- **Your organization's own app.** You register it at the provider, sign its client ID and secret
  as a connector config and upload that on **Admin → Services**
  ([Signed connector configs](#signed-connector-configs)). It uses your server's own redirect URIs
  and does not touch the relay.

Grist and Peek have no built-in app: Grist offers OAuth only with your own app (otherwise people
paste an API key), and Peek needs no app at all. Telegram needs no registration either; its app
credentials ship inside PrivacyFence.

A service appears on `/connect` only when an administrator has offered it under **Admin → Services**
([ADR 0196](adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md)). On a
multi-user server nothing is offered until you do. A service that uses a built-in app needs nothing else.

| Service | Redirect URI to register for your own app | Connector config provider | Setup guide |
|---|---|---|---|
| Gmail | `https://pf.acme.example.com/oauth/callback/gmail` | `google` | [Google Cloud setup](google-cloud-setup.md) |
| Google Drive | `https://pf.acme.example.com/oauth/callback/drive` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Google Calendar | `https://pf.acme.example.com/oauth/callback/calendar` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Google Contacts | `https://pf.acme.example.com/oauth/callback/contacts` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Google Tasks | `https://pf.acme.example.com/oauth/callback/tasks` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Google Apps Script | `https://pf.acme.example.com/oauth/callback/apps_script` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Slack | `https://pf.acme.example.com/oauth/callback/slack` | `slack` | [Slack setup](slack-setup.md) |
| Salesforce | `https://pf.acme.example.com/oauth/callback/salesforce` | `salesforce` (consumer key and secret, optional `login_url`) | [Salesforce setup](salesforce-setup.md) |
| Jira and Confluence | `https://pf.acme.example.com/oauth/callback/atlassian` | `atlassian` | [Atlassian setup](atlassian-setup.md) |
| Grist | `https://pf.acme.example.com/oauth/callback/grist` (OAuth only) | `grist` (plus `server_url` and optional `auth_server_url`) | [Grist setup](grist-setup.md); automatic API keys: [Grist setup](grist-setup.md#automatic-api-keys-organization-mode) |
| Miro | `https://pf.acme.example.com/oauth/callback/miro` | `miro` | [Miro setup](miro-setup.md) |
| Peek | none (device sign-in, or automatic accounts) | none | [Peek setup](peek-setup.md), [automatic accounts](peek-setup.md#automatic-accounts-organization-mode) |
| Telegram | none | none | [Telegram setup](telegram-setup.md) |

Notes for your own apps:

- **Google**: one OAuth client of type **Web application** covers all six Google services, but it
  must list all six redirect URIs, one per service you offer. Download its JSON file; its client ID
  and secret go into the `google` connector config. A Desktop app client cannot be used here, because
  it has no field for these redirect URIs.
- **Use a separate Google client for the connectors**, not the one you registered in
  [section 4](#4-identity-provider) for sign-in. A single client works, but Google then reports the
  sign-in scopes (`openid`, `userinfo.email`, `userinfo.profile`) in every connector's granted
  scopes, which makes audit and revocation harder to read.
- **Jira and Confluence** share one Atlassian app and one redirect URI, because an Atlassian OAuth
  app accepts only one callback URL.
- **Peek** has no OAuth app and no callback URL: each person approves a code in Peek itself on
  `/connect`, or accounts are created automatically ([Peek setup](peek-setup.md#automatic-accounts-organization-mode)).
  The Peek server's address is `PRIVACYFENCE_PEEK_BASE_URL` or the field under **Admin → Features**;
  Peek is offered only with it.
- **Telegram** needs no registration. Offer it under **Admin → Services**, and each person signs in
  with their phone number on `/connect`.

The same app registrations can also carry the loopback redirect URIs used by desktop installs; the
setup guides cover both.

### Built-in apps through the relay

A provider accepts only the redirect URLs registered for an app. The built-in apps are
PrivacyFence's own, shared by every server, so their redirect URL is the relay's
(`https://oauth.privacyfence.eu/callback/<service>`). The relay forwards the browser to the server
that started the sign-in, and only to a server that has proven it controls its own host name. Your
administrators do nothing for this: the server registers itself the first time a service that uses
a built-in app is offered, and renews daily. **Admin → Services** shows `Relay registration:
registered …`, `not registered yet`, or `failed: <reason>`, and links the relay terms.

The relay serves a server only if all of these hold:

- **The server address is `https://host` with no path.** `PRIVACYFENCE_ISSUER_URL=https://pf.acme.example.com`
  works; `http://…` or `https://example.com/privacyfence` cannot use the relay, and must use their
  own apps.
- **The server is reachable from the internet at `/.well-known/privacyfence-relay`.** To register,
  the relay fetches that URL from outside and compares the answer with the server's token
  (`org/relay_token.txt`). The server serves the path only while a service uses a built-in app
  and answers 404 otherwise, so a server that never offered one cannot be registered by someone
  else. If you restrict access by IP address ([section 7](#optional-restrict-access-by-ip-address)),
  keep this one path open to everyone, and do not limit port 443 itself to given addresses: the relay
  fetches from Cloudflare's network.
- **The server can make outbound HTTPS requests to `oauth.privacyfence.eu`.**
- **The host name is public.** The relay refuses `localhost`, names ending in `.local`,
  `.internal`, `.home.arpa`, `.lan`, IP addresses, and hosts under a public suffix that anyone can
  register under (such as `x.github.io` or `a.pages.dev`).
- **It stays verified.** The relay forwards a sign-in only for a host it verified in the last 35
  days. The server registers again every day, so only a long outage matters; after one, the next
  daily registration repairs it.
- **It has not been revoked.** PrivacyFence's maintainers can revoke a reported host.

A server at `http://localhost` (single-user mode) needs no registration: the relay forwards its
sign-ins straight back to the browser's own machine.

**What the relay sees.** The browser's redirect from the provider: a one-time authorization code and
the state, which carries your server's host name. It forwards that redirect and keeps neither: the
Worker writes no logs and never sees a token, because your server exchanges the code with the
provider itself. Slack and Miro do not support PKCE, so for them the code that passes through the
relay is not bound to your server; it is single-use and in transit for the length of one redirect.
The relay stores each registered host, when it was verified, and, for the built-in Google sign-in, the
sign-in domains proven in DNS. PrivacyFence's maintainers are emailed when a new host registers, and
they alone can list the registered hosts: there is no public list. The terms are at
<https://privacyfence.eu/relay-terms/>; the reasoning is in
[ADR 0259](adr/0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md).

**Do not mark the built-in PrivacyFence app as trusted or pre-approved** in your provider's admin
console: that trust would extend to every PrivacyFence server and to anyone using the public app. To
trust an app, register your own.

**Slack.** Slack limits apps outside its Marketplace to one history or thread request a minute and
15 messages a call. Until PrivacyFence is listed there, the built-in Slack app reads channel history
through Slack search, and the tool result and description say so (some message subtypes are missing,
a just-posted message can lag, and thread replies come back as top-level matches);
`slack_get_thread_replies` returns at most 15 replies. For full speed, use your workspace's own Slack
app: the Slack row on **Admin → Services** has a **Create your workspace's own Slack app** button
that opens Slack's create page with a ready manifest for this server. Sign the app's client ID and
secret as a connector config and upload it on the same row. An internal Slack app serves one
workspace, and switching apps makes everyone connect Slack again
([ADR 0270](adr/0270-the-built-in-slack-app-reads-through-search-while-slack-throttles-it.md)).

**Salesforce.** The built-in Salesforce app is a package that each customer org installs once. When the
build carries its install link, the Salesforce row on **Admin → Services** shows it with a copy
button; send it to that org's Salesforce administrator. Until they install it, a Connect attempt says
"Your Salesforce administrator has to install PrivacyFence in your org first." An org that blocks
`login.salesforce.com` sets its My Domain address in the **Login URL** field of that row (only
`login.salesforce.com`, `test.salesforce.com` and Salesforce's own `my.salesforce.com` addresses over
https are accepted, because that host receives the sign-in). A connector config's own `login_url`
wins over it.

## 6. Server settings and your own apps

An organization server needs no bundle. Three things configure it:

| What | Where | Section |
|---|---|---|
| The mode, the public address and the identity provider | The environment file | [below](#the-environment-file) |
| Everything operational (services offered, file delivery, sign-in restrictions, step-up, scoped tokens, audit forwarding, push, unattended sessions) | The Admin page, stored in `org/admin_config.json` | [The Admin page](#the-admin-page) |
| Your organization's own OAuth apps | Signed connector configs, uploaded on **Admin → Services** | [below](#signed-connector-configs) |

([ADR 0269](adr/0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md) and
[ADR 0264](adr/0264-an-organization-server-can-start-from-environment-variables.md) give the reasons.)
`build_org_bundle.py` builds desktop bundles only and refuses every organization option
([configuration reference](configuration-reference.md#refused-options)).

### The environment file

For the systemd install, write `/etc/privacyfence-org.env` (root-owned, mode `0600`; the unit in
[section 8](#8-hardened-systemd-unit) reads it). With Docker it is `privacyfence.env` next to
`compose.yaml` ([Docker](org-mode-docker.md#quick-start)).

```ini
PRIVACYFENCE_MODE=org
PRIVACYFENCE_ISSUER_URL=https://pf.acme.example.com
PRIVACYFENCE_TRUSTED_PROXIES=127.0.0.1

PRIVACYFENCE_IDP_ISSUER=https://idp.acme.example.com
PRIVACYFENCE_IDP_CLIENT_ID=<client id from section 4>
PRIVACYFENCE_IDP_CLIENT_SECRET_FILE=/etc/privacyfence-org/idp_client_secret
PRIVACYFENCE_ADMIN_EMAILS=alice@acme.example.com
```

`PRIVACYFENCE_MODE=org` makes it an organization server; `PRIVACYFENCE_ISSUER_URL` is the public
origin from which every redirect URI and link is built; `PRIVACYFENCE_TRUSTED_PROXIES` lists the
reverse proxy ([section 7](#7-reverse-proxy-and-tls)). The client secret is in its own file, owned by
`root` and readable by the service (for example `root:privacyfence-org`, mode `0640`), so it does
not sit in the unit or the process environment listing. [Section 4](#4-identity-provider) covers the
identity provider variables and admins, and the
[configuration reference](configuration-reference.md#environment-variables) lists every variable. The daemon
listens on `127.0.0.1:8765` unless `PRIVACYFENCE_BIND_HOST` and `PRIVACYFENCE_PORT` say otherwise.

Without any identity provider variable the daemon starts in **single-user mode**: one administrator,
served only at `http://localhost` or `http://127.0.0.1`, signed in with a setup code and a passkey.
That is the right start for trying PrivacyFence on one computer ([Docker](org-mode-docker.md#run-it-on-this-computer))
and the wrong one for a server: with a public `PRIVACYFENCE_ISSUER_URL` and no identity provider the
daemon refuses to start. The first start with an identity provider is recorded in
`org/admin_config.json`, so removing the identity provider variables later is refused instead of
silently turning the server into a single-user install.

A legacy bundle's `mode`, `server` and `idp` values win over the environment ([the 6.x
bundle](#a-6x-bundle-on-an-upgraded-server)); the start logs when it ignores a variable.

### Signed connector configs

To use your own OAuth app for a provider, sign its client ID and secret and upload the result. The
signature matters because the server then accepts credentials only from whoever holds your signing
key: a hijacked administrator session, even with its passkey, cannot swap in an attacker's app. It
can only remove your own app, which falls back to the built-in one and is recorded in the audit log.

A connector config is a small signed text file, one per provider (`google`, `slack`, `salesforce`,
`atlassian`, `miro`, `grist`). Make it on your own computer, not on the server.

**1. Make a signing key, once per organization.** With the script (needs the `cryptography` package):

```bash
python3 -m venv ~/pf-sign && ~/pf-sign/bin/pip install cryptography
~/pf-sign/bin/python sign_connector_config.py keygen --out ~/secure/pf-connector-key.pem
```

It writes the private key (mode `0600`, never overwriting a file) and prints the public key and its
**fingerprint**. Write the fingerprint down; you will check it on the first upload. Keep the key
secret and backed up: every future connector config needs it. An organization that already has a 6.x
bundle signing key can use that key unchanged. In the browser tool, **Generate a new key** downloads
`privacyfence-connector-key.pem` and shows the fingerprint.

**2. Sign one provider's credentials.**

```bash
~/pf-sign/bin/python sign_connector_config.py sign \
  --provider slack --serial 2026110201 \
  --client-id 1234.5678 --client-secret-file ./slack_client_secret \
  --key ~/secure/pf-connector-key.pem --output slack.txt
```

The secret comes from `--client-secret-file`, or from a hidden prompt if you leave that out; it is
never taken from the command line, where `ps` and the shell history would show it. Salesforce also
takes `--login-url` (your org's `https://<name>.my.salesforce.com` address), and Grist takes
`--server-url` and `--auth-server-url`. The output file is created with mode `0600` and the tool
never overwrites an existing file.

The same job runs in a browser at <https://privacyfence.eu/tools/connector-config/>, which sends
nothing anywhere: the page is self-contained, with no network access, outside resource or form, and
its Content-Security-Policy pins its own script and style by hash. It downloads
`<provider>.pfconnector`, byte-identical to the script's output for the same input. It signs with the
browser's Ed25519 support (Chrome 137, Firefox 129, Safari 17 or newer); other browsers are told to use
the script. If you would rather not trust a web page with your key, use the offline copy: the script
and the page (`privacyfence-connector-config-tool.html`) are attached to every stable release.

**3. Upload it.** Sign in as an administrator, open **Admin → Services**, and on the provider's row
choose **Upload a connector config…**. The first upload asks you to confirm the signing key's
fingerprint: compare it with the one you wrote down, because the server pins that key on trust. Every
later file must be signed with the same key. The row then shows your organization's own app: its
client ID, the file's serial, the date it was signed and the key's fingerprint. Uploading asks for a
passkey even when step-up is off, takes effect at once, and is recorded in the audit log without the
secret. **Remove our own app** goes back to the built-in app (or to none); everyone who connected that
service has to connect again, and so does everyone after you switch an app.

**The serial rule.** Each provider's serial must be higher than any serial accepted before for that
provider, including one that was later removed, so an older file can never be put back. A date
such as `2026110201` (`YYYYMMDDnn`) works; the browser tool prefills one from today's date.

**Rotating the signing key.** Stop the daemon, delete `org/connector_config_signing_pubkey.txt`, start
it, and upload a config signed with the new key; the first upload pins it after you confirm the
fingerprint. Serials carry over. A stored file that no longer verifies against the pinned key refuses
the start and names the provider.

The files are `org/connector_configs/<provider>.txt`, the pin `org/connector_config_signing_pubkey.txt`
and `org/connector_config_serials.json`; include all three in the [backup](#backup).

### A 6.x bundle on an upgraded server

A server upgraded from 6.x keeps working. On its first start 7.x copies `org/org_config.json` and its
pin files to `org/org_bundle.json`, `org_bundle_signing_pubkey.txt` and `org_bundle_serial.txt`
(the originals are never touched, [Going back to 6.x](#going-back-to-6x)), imports the bundle's app
credentials as connector configs, and copies its operational settings into
`org/admin_config.json`. From then on the bundle is read only for `mode`, `server` and `idp`, and
every start logs:

```text
org/org_bundle.json is read only for its server and identity-provider settings until PrivacyFence 8.
Move them to privacyfence.env (privacyfence-app --print-env-from-bundle), then run privacyfence-app --retire-bundle.
```

The Admin page shows the same notice. A new bundle cannot be installed any more: an
`org_bundle.json` that differs from the adopted one is refused at start. Move to the environment
before PrivacyFence 8 drops the bundle:

```bash
sudo -H -u privacyfence-org /opt/privacyfence/venv/bin/privacyfence-app --print-env-from-bundle \
  --idp-secret-file /var/lib/privacyfence-org/idp_client_secret
```

This prints the `PRIVACYFENCE_*` lines for the bundle's mode, server address and identity provider,
with the client secret written to the file you name (a new file, mode `0600`) and referenced by
`PRIVACYFENCE_IDP_CLIENT_SECRET_FILE`; it never prints the secret. Put the lines in the environment
file (move the secret file where `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE` says, readable by the service
account), restart, and retire the bundle with that environment loaded:

```bash
sudo sh -c 'set -a; . /etc/privacyfence-org.env; set +a; exec sudo -H -u privacyfence-org --preserve-env /opt/privacyfence/venv/bin/privacyfence-app --retire-bundle'
```

It checks that the environment yields the same mode, server address and identity provider (and
refuses, naming the first difference, if it does not), imports the credentials if that has not
happened, records the retirement and deletes `org/org_bundle.json`. Until then a bundle with an `idp`
section wins over the variables. With Docker the same command runs in the container:
`docker compose exec privacyfence privacyfence-app --retire-bundle`.

## 7. Reverse proxy and TLS

The daemon listens on `127.0.0.1:8765` and never needs to be reachable directly. Put a reverse
proxy in front of it that:

- terminates HTTPS for your public hostname;
- passes the original `Host` header through unchanged. The daemon answers only requests addressed
  to the hostname of `PRIVACYFENCE_ISSUER_URL`, `127.0.0.1` or `::1`; any other `Host` gets
  `400 Invalid Host header`;
- sets `X-Forwarded-For` and `X-Forwarded-Proto`. The daemon compares a browser's `Origin` header
  with the scheme it believes the request used, so without `X-Forwarded-Proto: https` every
  approval, sign-out and settings change is refused as cross-origin;
- does not buffer responses: the approvals page and `/mcp` stream events;
- allows request bodies of at least 50 MB, the size of an upload slot ([section 13](#13-file-delivery)).

The daemon trusts forwarded headers only from the addresses you list in
`PRIVACYFENCE_TRUSTED_PROXIES` (comma-separated; wildcards such as `0.0.0.0/0` are refused), and
from nowhere by default. A proxy on the same host must be listed as
`127.0.0.1` (or `::1`) like any other.

Rate-limit `/register`, `/authorize` and `/token` at the proxy. `/register` accepts anonymous
requests by design (that is dynamic client registration). The daemon caps registered clients at
2,000 and pending sign-ins at 1,000, but the proxy is the first line of defence against a request
loop.

### Caddy

Caddy obtains and renews the certificate, keeps the `Host` header, sets both forwarded headers and
streams responses without further configuration:

```
pf.acme.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

### nginx

With a certificate from your own CA or from certbot:

```nginx
limit_req_zone $binary_remote_addr zone=pf_oauth:10m rate=30r/m;

server {
    listen 80;
    server_name pf.acme.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name pf.acme.example.com;

    ssl_certificate     /etc/letsencrypt/live/pf.acme.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pf.acme.example.com/privkey.pem;

    client_max_body_size 60m;

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 3600s;

    location ~ ^/(register|authorize|token)$ {
        limit_req zone=pf_oauth burst=20 nodelay;
        proxy_pass http://127.0.0.1:8765;
    }

    location / {
        proxy_pass http://127.0.0.1:8765;
    }
}
```

`proxy_read_timeout` is long because a tool call can wait for a person to approve it.

`listen 443 ssl http2` works on the nginx 1.24 that Ubuntu 24.04 ships. From nginx 1.25.1,
`http2` on `listen` is deprecated in favour of a separate `http2 on;` line, which older versions
reject as an unknown directive; switch to it only on a newer nginx.

### TLS in the daemon instead

Terminate TLS in the reverse proxy. The daemon can still terminate TLS itself, but only from the
`server.tls` section of an upgraded server's 6.x bundle ([section 6](#a-6x-bundle-on-an-upgraded-server)):
there is no environment variable for it, and `build_org_bundle.py` no longer writes one. A proxy
handles certificate renewal and rate limiting as well.

### Optional: restrict access by IP address

The setup above leaves the whole hostname open to the internet. You can narrow it, in two layers:
the proxy limits the endpoints that only machines call to those machines' addresses, and a host
firewall closes every port the deployment does not use. Neither replaces sign-in; they shrink what
a stranger can reach at all.

**Who calls what.** Only part of the hostname is called by claude.ai's servers. The rest is called
by people's browsers, including phones approving a request, and by claude.ai's code sandbox:

| Path | Called by | Can be limited to |
|---|---|---|
| `/mcp`, `/register`, `/token`, `/revoke`, `/.well-known/*` | The AI client's servers. For claude.ai and Claude Desktop's remote connectors, that is Anthropic. | Anthropic's range, plus the addresses of any other AI client you use. **Except `/.well-known/privacyfence-relay`**, which the PrivacyFence relay calls from Cloudflare's network (below). |
| `/login`, `/authorize`, `/oauth/*`, `/approvals`, `/connect`, `/settings`, `/security`, `/downloads/*`, `/api/*` | People's browsers. A sign-in or connector callback comes from the person's browser, redirected by the provider, not from the provider's own addresses. | Your office or VPN range, or nothing. |
| `/mcp-files/*` (download links and upload slots) | The AI client's code sandbox ([section 13](#13-file-delivery)). | Leave open until you have checked where the sandbox connects from. |

**The relay's path stays open.** If any service uses a built-in app, or you use the built-in Google
sign-in, the relay at `oauth.privacyfence.eu` must be able to fetch
`https://<your host>/.well-known/privacyfence-relay` from the internet to register your server
([section 5](#built-in-apps-through-the-relay)), from Cloudflare's network, so there is no fixed address to allow. Exclude that
one path from the machine rule below. A server that cannot be reached there cannot use the built-in
apps and must use its own, uploaded as signed connector configs.

**Anthropic's range.** Anthropic publishes the addresses claude.ai connects from at
[platform.claude.com/docs/en/api/ip-addresses](https://platform.claude.com/docs/en/api/ip-addresses):
`160.79.104.0/21`, IPv4 only. Check that page whenever you review these rules. Claude Code,
ChatGPT and Gemini Enterprise do not connect from it: Claude Code connects from the person's own
machine, the others from their own providers' networks. If you use them, add their addresses or
your VPN range wherever the rules below list Anthropic's.

With Google (or any other identity provider) as the sign-in, most deployments limit only the
machine endpoints and leave the browser pages open: those pages are useless without a sign-in, and
locking them to a VPN also locks out anyone approving from a phone off the VPN. If you do limit
them, keep the allowed domains set regardless (**Admin → Sign-in**, [section 4](#who-may-sign-in)); an IP rule is
not a substitute for it.

#### Caddy

```
pf.acme.example.com {
    # Machine endpoints: only Anthropic, plus your VPN or office range for other AI clients.
    @machine_denied {
        path /mcp /mcp/* /register /token /revoke /.well-known/*
        not path /.well-known/privacyfence-relay
        not remote_ip 160.79.104.0/21 198.51.100.0/24
    }
    respond @machine_denied 403

    # Optional: browser pages from the VPN or office only. /mcp-files/* stays open.
    # @browser_denied {
    #     not path /mcp /mcp/* /register /token /revoke /.well-known/* /mcp-files/*
    #     not remote_ip 198.51.100.0/24
    # }
    # respond @browser_denied 403

    reverse_proxy 127.0.0.1:8765
}
```

Replace `198.51.100.0/24` with your VPN or office range, or remove it if claude.ai is the only AI
client. Write the paths exactly as shown: `/mcp*` would also match `/mcp-files/...`. `remote_ip`
is the address connected to Caddy; if a CDN or load balancer sits in front, list it under
`servers { trusted_proxies static <range> }` in Caddy's global options and use `client_ip`
instead. Run `caddy validate --config /etc/caddy/Caddyfile` and `sudo systemctl reload caddy` after
each change. With nginx, the same rules are a `geo` block on `$remote_addr` and a `return 403` in a
`location` for the machine paths.

The [monitoring probe](#monitoring) calls `/.well-known/oauth-authorization-server`, so run it from
an address the rule allows, or against `http://127.0.0.1:8765` on the server itself.

#### Host firewall (ufw)

The proxy rules still accept the TCP connection. A host firewall drops everything else before it
reaches anything. On Ubuntu, with `ufw`, and your admin address in place of `203.0.113.10`:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 203.0.113.10 to any port 22 proto tcp   # SSH: your admin address only
sudo ufw allow 443/tcp                                      # HTTPS; the proxy rules decide per path
sudo ufw allow 80/tcp                                       # certificate issuance only; see below
sudo ufw enable
sudo ufw status verbose
```

Add the SSH rule before `ufw enable`, or you lock yourself out of the server. Outgoing stays open:
the daemon needs the identity provider, every connector's API and the push services
([section 2](#2-prerequisites)), and pip needs PyPI.

**Port 443 limited to given addresses.** If people reach the browser pages only through the VPN,
the firewall can limit 443 as well: replace `sudo ufw allow 443/tcp` with one rule per range. That
also blocks the relay's check, so it suits a server that uses only its own apps.

```bash
sudo ufw allow from 160.79.104.0/21 to any port 443 proto tcp   # Anthropic (claude.ai)
sudo ufw allow from 198.51.100.0/24 to any port 443 proto tcp   # VPN or office
```

`/mcp-files/*` is then limited too, so test a file larger than about 75 KB each way before you rely
on it.

**Port 80 is for certificates only.** PrivacyFence serves nothing over plain HTTP. Caddy (or
certbot) uses port 80 only to answer the certificate authority's HTTP-01 challenge; Caddy redirects
every other request on it to HTTPS. Which rule to use depends on where the challenge comes from:

- **Let's Encrypt, or any public certificate authority.** It validates from several addresses that
  it does not publish and changes without notice, so an address rule on port 80 makes renewals fail
  weeks later, when nobody is looking. Keep `sudo ufw allow 80/tcp`. The same applies to port 443 if
  you limited it: Caddy's other challenge, TLS-ALPN-01, arrives there from the same unpublished
  addresses.
- **A certificate authority that validates from fixed addresses** (for example an internal ACME
  server). Allow only those addresses on port 80:

  ```bash
  sudo ufw allow from 192.0.2.53 to any port 80 proto tcp   # your CA's validation address
  ```

- **No port 80 at all.** Switch to the DNS-01 challenge, which proves control of the domain through
  a DNS record instead of a connection to the server. Caddy needs a build with your DNS provider's
  module (for example `tls { dns cloudflare {env.CF_API_TOKEN} }` in the site block); certbot needs
  its DNS plugin. Then delete the port 80 rule (`sudo ufw delete allow 80/tcp`), and port 443 can
  be limited to given addresses without breaking renewals.

**Test before you rely on it.** Connect claude.ai, approve a request, and move a file larger than
about 75 KB each way. Requests from outside the rules show up as `403` in the proxy's log, or not at
all when the firewall dropped them. The file transfers tell you where claude.ai's sandbox connects
from: if every `/mcp-files/` request in the proxy log comes from `160.79.104.0/21`, you can add
`/mcp-files/*` to the machine rule. Otherwise its only protection is the single-use link that
expires after 300 seconds. After the first renewal (`journalctl -u caddy | grep -i certificate`),
confirm the certificate was renewed rather than failing quietly.

## 8. Hardened systemd unit

Save this as `/etc/systemd/system/privacyfence-org.service`:

```ini
[Unit]
Description=PrivacyFence (organization deployment)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=privacyfence-org
Group=privacyfence-org
ExecStart=/opt/privacyfence/venv/bin/privacyfence-app
Restart=on-failure
RestartSec=5
UMask=0077
# The server settings from section 6, plus any audit-forwarding bearer token and the Peek and Grist
# provisioning keys; the file holds NAME=value lines, mode 0600, owned by root.
EnvironmentFile=/etc/privacyfence-org.env

# Filesystem: read-only everywhere except the data directory.
ProtectSystem=strict
ReadWritePaths=/var/lib/privacyfence-org
ProtectHome=true
PrivateTmp=true
PrivateDevices=true

# Privileges and kernel surface.
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictSUIDSGID=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
```

Then start it and follow the log:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now privacyfence-org
journalctl -u privacyfence-org -f
```

On a good start the log contains a line beginning `Org mode active -- MCP-over-HTTP at
https://pf.acme.example.com/mcp`, which also names the identity provider, whether step-up is on and
which `Host` names are accepted. Write the environment file ([section 6](#the-environment-file))
before you start it.

Notes:

- `ReadWritePaths` must be the service account's home directory, because the data directory is
  `~/.privacyfence/` inside it.
- If the daemon terminates TLS itself, keep the certificate and key outside `/home` (hidden by
  `ProtectHome`) or add them with `BindReadOnlyPaths=`.
- On first start the daemon creates the install-wide `settings.yaml` from its packaged example. That
  file must keep `web.mcp.enabled: true` (the packaged value): with it off, the daemon starts but
  serves nothing in organization mode.
- Only one daemon runs per data directory. A second start finds the lock, prints `PrivacyFence
  daemon is already running.` and exits 0. PrivacyFence has no clustering or shared-state
  replication; for higher availability, fail over between hosts with the data directory on the
  standby only while the primary is stopped.
- The daemon's own log is also written to `logs/privacyfence.log` in the data directory.

## 9. First sign-in and validation

### Sign in and connect accounts

1. Open `https://pf.acme.example.com/login` and sign in through your identity provider, as a person
   named as an admin ([section 4](#who-is-an-admin)). With the built-in Google sign-in, publish the
   DNS records the log lists first ([section 4](#built-in-google-sign-in)).
2. Open `/security` and enroll a passkey. Changes on the Admin page ask for one even when step-up is
   off ([The Admin page](#the-admin-page)).
3. Open `/admin`, go to **Services** and offer the services you want. The list is empty until you do.
   For a service that uses a built-in app, check that the page says `Relay registration: registered`
   ([section 5](#built-in-apps-through-the-relay)).
4. Back on `/connect`, click **Connect** next to each service. Each one sends you to that service's
   consent screen and back to `/connect`, which then shows it as connected. Nothing needs restarting.

People reconnect or check a connection the same way; see
[connecting a service](connecting-a-service.md).

### Add PrivacyFence to an AI client

Every client connects to `https://pf.acme.example.com/mcp`. The client discovers the authorization
server from `/.well-known/oauth-protected-resource/mcp`, registers itself at `/register`, and opens
the organization's sign-in page. The first time a person connects a given client, PrivacyFence then
shows a consent page that names the client and the host it returns to, and issues nothing until the
person allows it; the choice is remembered per person, client and redirect address
([ADR 0177](adr/0177-an-org-mode-mcp-client-needs-the-persons-consent-once.md)). After that the client
holds its own tokens; there is nothing to copy or paste. Access tokens last one hour and are refreshed silently; a refresh chain lasts 30 days, after
which the person signs in again.

claude.ai also needs this host on its domain allowlist to move files larger than about 75 KB,
in either direction. On Team and Enterprise plans that list is set by a claude.ai Owner or admin
under **Admin settings → Capabilities**, not by each person; see
[File delivery](#13-file-delivery).

Each AI client has its own page with the exact steps, what it asks before PrivacyFence's card, and
how it is identified: [claude.ai](connect-claude-ai.md), [Claude Desktop](connect-claude-desktop.md)
(as a custom connector), [Claude Code](connect-claude-code.md),
[ChatGPT](connect-chatgpt.md) (Developer Mode) and [Gemini Enterprise](connect-gemini-enterprise.md).
Any other client that supports the Streamable HTTP transport with OAuth 2.1 and dynamic client
registration works the same way.

[Section 11](#11-ai-systems) says which clients are shown as verified.

#### Gemini Enterprise

Gemini Enterprise does not register itself. An admin registers it once with a `curl` call to
`/register`, creates a custom MCP server data store in the Google Cloud console with that client
ID and secret, and each person then authorises it once from the Gemini Enterprise web app. The
steps, the 100-action limit and what it can do with files are on
[Connect Gemini Enterprise](connect-gemini-enterprise.md).

### Validation checklist

Run these through the public hostname, not through `127.0.0.1`:

- [ ] `curl -fsS https://pf.acme.example.com/.well-known/oauth-authorization-server` returns JSON
  whose `issuer` is your public URL.
- [ ] Opening `/approvals` in a private window redirects to `/login`.
- [ ] Signing in shows your email address at the top right of the page.
- [ ] **Sign out** on `/connect` lands on the signed-out page, and `/approvals` then redirects to
  `/login` again.
- [ ] Someone outside the allowed domains or required groups on **Admin → Sign-in** (if set) is refused at sign-in.
- [ ] **Admin → Services** says `Relay registration: registered` for a server that uses a built-in app.
- [ ] An AI client connects and lists PrivacyFence's tools.
- [ ] A write request from the client appears in your `/approvals`, and not in a second test user's.
- [ ] Approving it (with step-up, if on) completes the request, and it appears under
  **Settings → Audit Log**.
- [ ] After `sudo systemctl restart privacyfence-org`, the AI client keeps working without a new
  sign-in, and the browser asks you to sign in again.

## 10. Install-wide and per-user policy

### The Admin page

`/admin` is where administrators manage the server; it is the second item in the navigation for
them. Only people named as admins ([section 4](#who-is-an-admin)) see it: anyone else who opens it
sees "Only administrators can open this page." Each section is a form that saves as a whole, and the
values shown are the ones in force. The settings are stored in `org/admin_config.json`
([configuration reference](configuration-reference.md#admin-config-orgadmin_configjson)).

| Section | What it controls | Takes effect |
|---|---|---|
| Services | Which services are offered, which app each uses (the built-in one through the relay, or your own: [section 6](#signed-connector-configs)), upload and removal of connector configs, the Salesforce login URL, Peek and Grist automatic accounts, relay registration status | At once; automatic accounts after a restart |
| Features | Jira Assets, the Peek and Grist server addresses (hidden when the operator fixed them in the environment), unattended sessions, web push, the room directory | Addresses and Jira Assets at once; unattended sessions and web push after a restart |
| File delivery | Inline size, link lifetime, disk staging, agent links, exports ([section 13](#13-file-delivery)) | After a restart |
| Sign-in | Allowed email domains, groups claim and required groups ([section 4](#who-may-sign-in)); with the built-in Google sign-in, the administrators read-only | After a restart |
| Step-up | Whether approvals need a fresh passkey, and for which approvals ([section 12](#12-approvals-and-step-up)) | After a restart |
| Scoped tokens | Whether people may create scoped tokens, their longest lifetime, whether AI clients may propose one | After a restart |
| Audit forwarding | Syslog or HTTPS forwarding of audit entries ([section 14](#monitoring)) | After a restart |
| Privacy Filter | The install-wide privacy policy and PII detection, below | On the next request |
| Stats | Counts of people, connectors and auto-accept rules; never whose or what | |

After a change that needs a restart, the page lists what is waiting; restart the service when nobody
is mid-approval. Every change that alters what the server does asks for a passkey, **even when
step-up is off**, and is recorded in the audit log under the administrator who made it, without any
secret; withdrawing a service and clearing the room directory are the exceptions. An administrator
with no passkey is told to add one on `/security` first. A change to the environment file
(server address, identity provider, admins) is never made here: that is a file edit and a restart.

On a server upgraded from 6.x, the first start copied the settings the bundle carried into the admin
config once ([section 6](#a-6x-bundle-on-an-upgraded-server)). If you still pass those settings in a
6.x bundle, the daemon logs that they are ignored; manage them on this page instead. The
reasoning is in [ADR 0257](adr/0257-operational-settings-live-in-a-daemon-written-admin-config.md) and
[ADR 0258](adr/0258-administration-is-a-separate-admin-page.md).

### Install-wide and per-user policy

Policy is split in two:

| What | Scope | Where | Who changes it |
|---|---|---|---|
| Privacy filter (which content categories are shown, redacted or blocked) and PII detection | Install-wide: applies to everyone | `authority/config/settings.yaml` in the data directory | Admins, on **Admin → Privacy Filter** |
| Always-allow rules | Per person | `users/<principal>/authority/config/settings.yaml` | Each person, on **Settings → Auto-accept** or with **Always allow** on an approval card |

**Install-wide policy.** A change made on the Admin page is written to the file and applies to everyone's
next request, without a restart. You can also edit the file by hand and restart the daemon, but do
not mix the two: saving from the Admin page rewrites the whole file from the copy the daemon loaded at
startup, which drops any hand edit made since, comments and formatting included. In organization
mode a privacy group that is missing from the file is treated as `block` (a desktop install uses
`allow`), and **Admin → Privacy Filter** marks each group that is relying on that default. State every group
explicitly so the file documents the intended policy. The categories and their meaning are in
[approvals and policy](approvals-and-policy.md).

**Per-person rules.** Nobody, admins included, can see or change another person's always-allow
rules from the browser. An administrator who must review or remove someone else's rules edits that
person's file on the server. The Admin page's **Stats** section counts rules per connector across everyone,
but never shows whose they are or what they allow.

Settings pages each person sees: **Auto-accept**, **Audit Log** (your own 20 most recent entries)
and **About**.

Everything install-wide, including the Privacy Filter, PII detection and Stats, is on the
[Admin page](#the-admin-page) and is not on Settings: Settings is the same page for an
administrator as for everyone else.

Connector management, update checks, the log level, the Calendar free/busy and Gmail signature
switches, and notification preferences are desktop-only settings and do not appear here.

### Moving a person from a desktop install

A person's always-allow rules and step-up level can move from their desktop install to the
organization server (and back) as a file:

1. On the desktop, open **Settings > Auto-accept** and choose **Export…**. The browser saves
   `privacyfence-personal-settings.json`.
2. Sign in to the organization server.
3. Connect the same accounts at `/connect`. A rule names folders, channels and projects by id, so it
   only matches when the same account is connected here.
4. Open **Settings > Auto-accept** and choose **Import…**, then pick the file.
5. Read the preview. Each rule is listed as will be added, will be extended, already there, or
   skipped with the reason. Nothing has changed yet.
6. Confirm. When step-up is in force, approve with your passkey; the approval is for that exact file.

Import only adds: it never removes or narrows a rule or lowers a step-up level, and it applies to the
signed-in person's own settings only. An administrator cannot import for someone else. What does not
move:

- Connector sign-ins and tokens: connect your accounts again on the new install.
- Passkeys: enroll a passkey on the new install at `/security`.
- Scoped tokens: create new ones if your administrator allows them.
- Privacy filter and PII detection: install-wide, a desktop install's own or an administrator's on an
  organization server.
- Connector on/off switches, notification detail, AI system names, update checks and log level:
  desktop-only settings.
- The audit log: it stays on the install that wrote it.
- Rules written by hand in `settings.yaml` that have no entry on the Auto-accept page (custom
  rules): add them again on the page.

See [ADR 0194](adr/0194-personal-settings-are-exported-as-a-file-of-auto-accept-rows-and-the-step-up-level.md)
and [ADR 0195](adr/0195-importing-personal-settings-is-merge-only-previewed-and-step-up-gated-on-the-file-hash.md).

## 11. AI systems

Every approval card and audit entry names the AI system that made the request. How much that name
can be trusted depends on where it came from:

| Source | Recorded as `agent_source` | Shown as |
|---|---|---|
| The client registered only with its vendor's own redirect URI (claude.ai, ChatGPT, Gemini Enterprise) | `oauth_client` | Verified, with the AI system's icon |
| The name the client gave when it registered, or in its MCP handshake | `client_info` | Not verified, with the claimed name |
| Nothing | empty | Unrecognised AI system |

Every vendor's authorization code can only be delivered to a redirect URI the client registered, and
PrivacyFence's authorization server refuses any other. So when a registration's only redirect URIs
are one vendor's own callback, its tokens were obtained by that vendor, and its cards are verified:

- claude.ai: `https://claude.ai/api/mcp/auth_callback`
- ChatGPT: `https://chatgpt.com/connector/oauth/<connector id>`
- Gemini Enterprise: `https://vertexaisearch.cloud.google.com/oauth-redirect`

A registration with any other redirect URI alongside them stays Not verified. Claude Code and
anything else with a loopback redirect (`http://localhost:...`)
stay Not verified, because any local program can register one. Claude Desktop's custom connector
most likely registers through claude.ai and is then shown as Claude. A person, or a program using
their signed-in browser session, can register a client under a vendor's redirect URI and so have
their own calls shown as that vendor, verified; this never reaches another member's calls
([ADR 0224](adr/0224-an-org-registration-is-attested-by-its-redirect-uris.md) says what that leaves
open). There is nothing for an administrator to do: which clients are verified follows from how they registered.

The reasoning is in [ADR 0224](adr/0224-an-org-registration-is-attested-by-its-redirect-uris.md),
[ADR 0225](adr/0225-the-admin-ai-system-pin-is-removed.md),
[ADR 0036](adr/0036-card-copy-names-the-caller-through-one-placeholder.md) and
[ADR 0037](adr/0037-a-local-override-is-a-relabel-and-never-attests.md).

## 12. Approvals and step-up

Each person sees and decides only their own approvals, on `/approvals`. What gets gated, and how
cards and always-allow rules work, is the same as on a desktop install; see
[approvals and policy](approvals-and-policy.md).

**Step-up** asks for proof that a person, not a stolen session, is approving. On a multi-user
server it is off until an administrator turns it on, on **Admin → Step-up**:

| Setting | Organization default | Where |
|---|---|---|
| Step-up | off | Admin → Step-up (`step_up.enabled`) |
| Scope | `writes_and_pii_reads`: every write, and every read in which PII was detected | Admin → Step-up (`step_up.scope`: `writes`, `writes_and_pii_reads` or `writes_and_reads`) |
| Accepted proof | a passkey, or signing in again at the identity provider | Admin → Step-up (`step_up.require_passkey` accepts a passkey only) |
| Fresh sign-in request | `prompt=login` and `max_age=0` | `PRIVACYFENCE_IDP_STEP_UP_ACR_VALUES` adds `acr_values` |
| Relying-party ID and name | the hostname of `PRIVACYFENCE_ISSUER_URL`; `PrivacyFence` | `step_up.rp_id` and `step_up.rp_name` in `org/admin_config.json` only |
| Batch | `single_assertion` | `step_up.batch` in `org/admin_config.json` only |

**The page only tightens step-up.** It offers the current value and stricter ones: a switch that is
on stays on, and the scope choices start at the current scope. To relax it, or to set `rp_id`,
`rp_name` or `batch`, which have no control on the page, stop the daemon, edit `org/admin_config.json`
and start it again ([configuration reference](configuration-reference.md#admin-config-orgadmin_configjson)).
Set `rp_id` only to use a parent domain; it must match the domain passkeys were enrolled under, so
changing it makes existing passkeys unusable. A single-user install starts with step-up on, passkey
required and the default scope. Changes need a restart.

With step-up on:

- A person with an enrolled passkey confirms with it. Passkeys are enrolled on `/security`.
- Without `require_passkey`, a person may instead sign in again at the identity provider
  (`prompt=login`, `max_age=0`, plus any `PRIVACYFENCE_IDP_STEP_UP_ACR_VALUES`). The sign-in must be
  the same person the approval belongs to.
- With `require_passkey`, only a passkey is accepted. Someone with no passkey is sent to
  `/security` to enroll one.
- Approving several cards at once takes a single passkey confirmation that covers the selected set.
  Someone with no passkey cannot approve several at once, with or without `require_passkey`: they
  approve each card on its own, or enroll a passkey first. Denying never needs step-up.

**Sensitive settings writes.** Adding or removing an always-allow rule is sensitive: it changes what
gets gated in future. When step-up and `require_passkey` are both on, it needs a fresh passkey
confirmation, exactly as on a desktop install. With either off, it needs only a signed-in session. A
fresh identity-provider sign-in is never accepted for it. See
[ADR 0034](adr/0034-sensitive-settings-writes-require-step-up-in-both-modes.md). The Admin page is
stricter: its sensitive actions, including the privacy policy and PII detection, always ask for a
passkey ([The Admin page](#the-admin-page)).

The full comparison of step-up on desktop and organization installs is in
[security and compliance](security-and-compliance.md).

### Notifications on a phone

A phone browser suspends a tab it is not showing, so an open `/approvals` tab cannot tell anyone
that a request arrived. Organization mode sends a **web push notification** instead:

- **Turning it on, per person.** Right after someone decides a request on `/approvals`, the page
  offers to turn notifications on. Once they allow it, their browser subscribes, and the server
  notifies that browser whenever a new approval is waiting for them (at most one notification every
  5 seconds). Tapping the notification opens `/approvals`.
- **Shared browsers.** Signing out stops the notifications on that browser, and so does someone
  else signing in on it; the person's other devices keep theirs. A session that only times out
  keeps notifying, since reaching a phone whose session has lapsed is the point.
- **iPhone and iPad.** Safari offers web push only to a site added to the Home Screen. Until it is
  added, the page shows "Add PrivacyFence to your Home Screen to get notifications" instead of the
  offer. Added from Safari's Share menu, PrivacyFence opens as its own app, starting on
  `/approvals`, and can then turn notifications on. Android Chrome and desktop browsers need no
  installation, though they can install it too.
- **What it says.** "PrivacyFence", and "1 approval pending" (or "N approvals pending"). Never the
  tool, the connector, the content, who is asking or anything else about the request, whatever
  the notification detail level a desktop install uses.
- **Where it goes.** The notification is sent from this server to the push service of the person's
  browser: Apple (`*.push.apple.com`), Google (`fcm.googleapis.com`), Mozilla
  (`*.push.services.mozilla.com`) or Microsoft (`*.notify.windows.com`), which delivers it to the
  device. It is encrypted to the browser, so the push service cannot read the text, but it does see
  that this server sent a notification to that device, and when. The server sends to no other
  host: a subscription for any other address is refused. Allow outbound HTTPS to those hosts if
  your firewall restricts egress.
- **Turning it off for everyone.** Switch web push off on **Admin → Features** (`web_push.enabled:
  false`) and restart. The subscription routes are then not served, nothing is sent,
  and the pages no longer offer notifications. Subscriptions already stored stay on disk unused;
  delete `users/*/push_subscriptions.json` to remove them.

The reasoning, and why a desktop install has no push at all, is in
[ADR 0081](adr/0081-org-mode-sends-a-count-only-web-push.md).

## 13. File delivery

The server cannot write into a person's own folders, so download tools (`drive_download_file`,
`gmail_download_attachment`, `confluence_download_attachment`) hand the file back in one of two
ways, after the approval:

| Tool result size | Delivery |
|---|---|
| Up to the inline limit (**Admin → File delivery**, 100,000 bytes by default, a file of about 75 KB) | Inline: the file's bytes, base64-encoded, are in the tool result. |
| Larger | Staged: the file is encrypted on disk and the tool result carries a single-use link that expires after the link lifetime (**Admin → File delivery**, 300 seconds by default). |

The limit is measured on the tool result the client receives, the base64-encoded file and its
JSON envelope together, not on the file: base64 makes a file a third larger. The default fits the
~150,000-character tool-result limit Anthropic documents for claude.ai and Claude Desktop, which
truncate a larger result rather than refuse it. Raise it only if every client your organization
uses accepts larger results; Claude Code, for example, handles more
([ADR 0092](adr/0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md)).

Which link a staged file gets:

| Setting | Link | Who can fetch it |
|---|---|---|
| Agent links on (default) | `https://<host>/mcp-files/fetch/<token>` | Whoever holds the link, once, before it expires. The token is the credential, so the AI client can fetch the file itself, if it can reach `<host>` (see below). |
| Agent links off | `https://<host>/downloads/<token>` | Only the person the file belongs to, signed in, in a browser. |

**The AI client has to be able to reach this host.** A client opens an agent link, and sends an
upload, from wherever it runs its own code, which is not necessarily where it runs the MCP
connection. claude.ai does both from its sandbox, which reaches only the domains on its
domain allowlist. Have a claude.ai Owner or admin put this deployment's host there (**Admin
settings → Capabilities**, with **Code execution and file creation** on), or every file over the
inline limit, and every upload, fails from claude.ai after it has been approved. A
failure there never reaches PrivacyFence; see
[Connect claude.ai](connect-claude-ai.md#files) and
[ADR 0097](adr/0097-a-download-link-needs-a-client-that-can-reach-the-server.md). To read a
document's text, `drive_get_file_content` needs no link at all: it returns a PDF, DOCX, PPTX or
XLSX file as text.

Opening a `/downloads/` link while signed out goes through sign-in and then back to the same
link, so the link still works if it has not expired by then. Signing in as someone else does
not claim it: the file is released only to the person it was staged for.

A missing, expired, already-used or other person's token all get the same `404`. With disk staging
off, a file too large to return inline is refused instead of staged, and nothing is written to disk.
With the inline limit set to `0`, every file goes through a link. Changes to these settings on the
Admin page apply after a restart.

Staged files live under `users/<principal>/downloads/` in the data directory. Expired files are
removed the next time that person stages or claims a download, and every staged file is removed
when the daemon starts. The export tools (`grist_export_records`, `grist_export_sql`, `salesforce_export_report`,
`jira_export_issues`) use the same staged link: after the approval the file is staged and the tool
result carries the link, with no `destination_dir`. They respect `allow_disk_staging` and are
refused before any request when it is off. Turn exports off (`download_delivery.allow_exports: false`,
**Admin → File delivery**) to remove them altogether, `assets_export_objects` included: they are then not
listed and a call is refused, so no export file is held on the server
([ADR 0197](adr/0197-an-organization-can-turn-the-export-tools-off.md))
([ADR 0160](adr/0160-org-mode-exports-are-delivered-by-a-one-time-link.md)).

The approval gate, not the delivery channel, is where the privacy decision
is made; see [ADR 0017](adr/0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md)
and [ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md).

**Uploads.** A local file path means nothing to the server. An AI client first calls
`privacyfence_create_upload_slot` with a file name, gets back an `upload_url`, sends the file there
with an HTTP `PUT`, and passes the returned `upload_id` to the tool that needs the file (for
example `drive_upload_file`'s `upload_id`, or `upload:<upload_id>` in a Gmail attachment list). A
slot takes up to 50,000,000 bytes, is valid for 10 minutes and can be used once, and only by the
person who created it. Once a tool call has read it, the slot is kept until the approved write uses
it, for up to 20 minutes, so an approval that waits for you does not lose the file. `drive_upload_file` also accepts the file inline as `content_base64`. Gmail
attachments are limited to 18,000,000 bytes in total per message.

### Scheduled recipes

`privacyfence-sync` runs recipes on a schedule against this server, with no AI model involved. For
a scheduler on a server, the administrator needs to do one thing and leave one thing alone:

- **Enable scoped tokens** on **Admin → Scoped tokens**. Each person then creates, in Settings, a
  named token with its own rules and the tools one recipe needs. The same section sets how long a
  token may last (1 to 365 days, 90 by default) and whether an AI client may propose a token for a
  person to review. A scoped token does not need unattended sessions.
- **Leave agent links on** (**Admin → File delivery**). They are on by default, and
  `privacyfence-sync` needs them to receive a file; with agent links off, steps that receive a file fail.

Recipe scripts run on the scheduler's machine, never on this server
([ADR 0161](adr/0161-privacyfence-sync-is-a-separate-plain-mcp-client.md)). For the person's side
(listing the rules and tools a recipe needs, storing the token, and reading the result file), see
[On a server, against organization mode](https://github.com/privacyfence/privacyfence/blob/main/sync/README.md#on-a-server-against-organization-mode).

## 14. Operations

### Backup

The data directory is `/var/lib/privacyfence-org/.privacyfence/`. Back up all of it except the
paths marked "skip", with the daemon stopped (or from a filesystem snapshot):

| Path in the data directory | Contents |
|---|---|
| `org/admin_config.json` | Everything set on the [Admin page](#the-admin-page). |
| `org/connector_configs/`, `org/connector_config_signing_pubkey.txt`, `org/connector_config_serials.json` | Your own apps' signed connector configs (with their client secrets), the pinned signing key and the highest serial accepted per provider. |
| `org/relay_token.txt`, `org/relay_status.json` | The token the server proves its host name with, and when the relay last accepted it. With the built-in Google sign-in the token is also the value behind every `_privacyfence-relay` DNS TXT record: if it is lost and replaced, every record must be published again. |
| `org/org_config.json`, `org/org_config_signing_pubkey.txt`, `org/org_config_serial.txt` | An upgraded server's 6.x bundle and pins, which 7.x never changes. Keep them for [going back to 6.x](#going-back-to-6x). |
| `org/org_bundle.json`, `org/org_bundle_signing_pubkey.txt`, `org/org_bundle_serial.txt`, `org/org_bundle_adopted.json` | An upgraded server's adopted copy of the bundle, its pins and the record of the copy. Absent on a server that never had a 6.x bundle, or after `--retire-bundle`. |
| `data_format.json` | The marker that this data is ready for PrivacyFence 8 ([Upgrading to 8.x](#upgrading-to-8x)). |
| `org/oauth_clients.json` | Registered AI clients. Without it every client has to register again. |
| `org/oauth_refresh.json` | AI clients' refresh tokens, each sealed under its own token. |
| `org/web_push_vapid_key.pem` | The server's push-notification key. Without it every browser has to subscribe again. |
| `authority/config/settings.yaml` | Install-wide policy. |
| `authority/logs/audit/` | The install's own audit log and its chain key. |
| `deployment_id` | The install's ID, stamped on every audit entry. |
| `users/<principal>/credentials/` | Each person's connector tokens. |
| `users/<principal>/credentials/peek_provisioning.json` | Peek auto-provisioning state and the person's email, for token renewal. |
| `users/<principal>/credentials/grist_provisioning.json` | Grist auto-provisioning state and the person's email, for key renewal. |
| `users/<principal>/authority/config/settings.yaml` | Each person's always-allow rules. |
| `users/<principal>/authority/webauthn_credentials.json`, `webauthn_recovery_code.json`, `step_up_state.json` | Each person's passkeys and recovery code. |
| `users/<principal>/logs/audit/` | Each person's audit log and its chain key. |
| `users/<principal>/push_subscriptions.json` | Each person's subscribed browsers. |
| `users/<principal>/whats_new.json` | Each person's last seen version, for What's new. |
| `users/<principal>/downloads/`, `users/<principal>/uploads/` | Skip: short-lived encrypted staging. |
| `logs/privacyfence.log` | Optional: the runtime log. |

Also keep, outside the server: the connector-config signing key, the systemd unit, the proxy
configuration, the environment file and the files it points to (the identity provider's client
secret, provisioning keys).

A backup holds live connector tokens and client secrets. Encrypt it and restrict access as you
would the credentials themselves. To restore, stop the daemon, restore the directory with owner
`privacyfence-org` and modes unchanged (the daemon refuses to start if they are looser than
`0700`), start it, and run the [validation checklist](#validation-checklist).

### What a restart keeps

| State | After a restart |
|---|---|
| Registered AI clients and their refresh tokens | Kept. Clients reconnect silently. |
| Access tokens (one hour) | Lost. The client gets one `401` and refreshes. |
| Browser sessions (30 minutes idle, 24 hours at most) | Lost. People sign in again. |
| Pending approvals | Lost. The AI client has to make the request again. |
| Staged downloads and upload slots | Removed. |
| Policy, rules, passkeys, connector tokens, audit logs | Kept. |

### Upgrade

Install the new version into the same virtual environment and restart. Your data is kept:

```bash
sudo /opt/privacyfence/venv/bin/pip install --upgrade privacyfence
sudo systemctl restart privacyfence-org
```

Read the release's entry in the [changelog](../CHANGELOG.md) first and back up before upgrading.

After an upgrade, each person sees a strip linking to What's new (`/whats-new`) the first time they
open PrivacyFence, listing the changelog sections since the version they last used.

A `settings.yaml` that still has `auto_accept_rules` or `auto_accept_grants` sections is refused at
startup, unless it also carries the `migrated_to_policy_v2` marker, in which case those sections
are removed and the file is rewritten.

**Upgrading from 6.x to 7.x.** What happens on the first start of 7.x, and what you do:

- The bundle `org/org_config.json` and its two pin files are copied to `org/org_bundle.json`,
  `org_bundle_signing_pubkey.txt` and `org_bundle_serial.txt`, and the copy is recorded in
  `org/org_bundle_adopted.json`. The 6.x files are never changed. If you edit `org_config.json`
  afterwards, every start logs that the change is ignored, because 7.x reads `org_bundle.json`.
- The bundle's app credentials become connector configs, and its operational settings are copied
  once into `org/admin_config.json` (the start logs how many and writes an `admin_config_imported`
  audit entry). From then on manage them on the [Admin page](#the-admin-page); a later start that finds the
  bundle still carrying them logs that they are ignored.
- The bundle keeps working read-only for its server and identity-provider settings. Move those to
  the environment before PrivacyFence 8 ([section 6](#a-6x-bundle-on-an-upgraded-server)):
  `privacyfence-app --print-env-from-bundle --idp-secret-file PATH` prints the lines to move, and
  `privacyfence-app --retire-bundle` finishes it.
- **Allowed domains now need a verified email** ([section 4](#who-may-sign-in)). On Microsoft Entra
  ID, which never sends one, the allowed-domain check refuses everyone until
  `PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL=true` is set. The legacy bundle's `idp` section wins over
  the variable, so an upgraded Entra server moves its identity provider to the environment first
  (`--print-env-from-bundle`, then `--retire-bundle`), and sets the variable there.
- Every start brings every person's data up to date and, when nothing is left, records
  `data_format.json` ([below](#upgrading-to-8x)).

### Going back to 6.x

You can return to the latest 6.x release (6.10.0 or newer; 6.11.0 or newer if you use Peek or Grist
automatic accounts), because 7.x never changes a 6.x file: `org/org_config.json` and its pins, and
every other file 6.x reads (`settings.yaml`, `scoped_tokens.json`, `org/oauth_clients.json`,
`oauth_refresh.json`, the push key, `credentials/`), stay in a format 6.x reads.

1. Stop 7.x (`sudo systemctl stop privacyfence-org`).
2. Install 6.x: `sudo /opt/privacyfence/venv/bin/pip install "privacyfence<7"`.
3. Start it. It runs with `org_config.json` as it was.

What does not carry back: changes made on the Admin page, connections made through the relay, and
anything made in single-user mode. 6.x runs with the configuration `org_config.json` holds, and
people connect those services again.

To upgrade to 7.x again from 6.x's *current* files, instead of continuing with what 7.x kept, stop
the daemon and delete these files, then start 7.x, which adopts and imports again:

```bash
cd /var/lib/privacyfence-org/.privacyfence/org
sudo rm -f org_bundle.json org_bundle_adopted.json org_bundle_signing_pubkey.txt org_bundle_serial.txt admin_config.json
```

Deleting only `org_bundle.json` re-adopts `org_config.json`; if 7.x has since recorded a newer serial
in `org_bundle_serial.txt`, it refuses that older bundle with the existing "delete the serial pin"
message. Rotating the signing key after the upgrade is covered under
[Key and secret rotation](#key-and-secret-rotation).
If you retired the bundle with `--retire-bundle`, the same list applies.
[ADR 0263](adr/0263-the-6-to-7-upgrade-is-reversible.md) has the reasoning.

### Upgrading to 8.x

PrivacyFence 8 drops the code that upgrades data written by older releases, and removes desktop mode.
That is safe only if 7.x has already finished every upgrade for every person. So **upgrade to 8.x only
after running 7.x at least once with `Ready for PrivacyFence 8: yes.`** at the bottom of the Admin
page ([ADR 0262](adr/0262-7x-is-the-migration-checkpoint-for-8x.md)).

At every start 7.x brings every person's data up to date, whether or not they have signed in since:
older policy sections, version-2 auto-accept rules, scoped tokens that only name rules, and the
retired verb names (rewritten in `settings.yaml` and in each token's own rules; the previous file is
kept beside it as `settings.yaml.pre-canonical-verbs.bak` or `scoped_tokens.json.pre-canonical-verbs.bak`,
readable by the service account only). When nothing is left, and the admin config exists, it writes
`data_format.json` in the data directory. The file travels with the data and its backups. If anything
stops it, the start logs `Not ready for PrivacyFence 8: <problem>`, the Admin page says `Ready for
PrivacyFence 8: not yet.` with the count and the first three problems, and the next start tries
again. Fix the problem, restart, and look again:

| Problem | Fix |
|---|---|
| `the users directory cannot be read` | The service account cannot read `users/`; restore its owner and mode `0700`. |
| `<person>: settings.yaml cannot be read` or `is not a mapping` | The person's `users/<person>/authority/config/settings.yaml` is unreadable or not a YAML mapping. Fix the permissions, or restore the file from a backup. |
| `<person>: could not rewrite settings.yaml` | The service account cannot write that file; fix the owner, mode or disk space. |
| `<person>: settings.yaml still has v1 policy sections` | Remove the `auto_accept_rules` and `auto_accept_grants` sections; the person recreates the rules on **Settings → Auto-accept**. Rules in that format are not converted automatically. |
| `<person>: rules are still in the version-2 format` | The upgrade could not save the file; make it writable and restart. |
| `<person>: scoped_tokens.json cannot be read` | The file is damaged. Restore it from a backup, or remove it (the person's tokens stop working and are created again). |
| `<person>: scoped token <id> still names rules` | The upgrade could not save the file; make it writable and restart. |
| `<person>: could not be migrated` | Read the log lines above it for the cause. |
| `the admin config has not been created` | Start the daemon once more as the service account with write access to `org/`. |
| `the data-format marker could not be written` | The service account cannot write `data_format.json` in the data directory. |

The other direction is refused: 7.x refuses to start on data a newer release has already moved on
(`This data was last used by PrivacyFence <version> … Install <version> or newer.`), so a downgrade
from 8.x needs the backup you took before upgrading.

### Optional: upgrade automatically

A systemd timer can run the upgrade above every night and keep the server on the latest stable
release. `pip install --upgrade` skips pre-releases, and PyPI carries stable releases only, so it
never picks up a test build. The trade-off is the advice above: nobody reads the changelog before
an upgrade, and each one restarts the daemon, which drops pending approvals and browser sessions
([What a restart keeps](#what-a-restart-keeps)). Pick an hour when nobody is approving requests, and
have a nightly backup run before it.

Save the upgrade script as `/usr/local/sbin/privacyfence-upgrade`, owned by root with mode `0755`.
It restarts only when the version changed, then waits up to a minute for the daemon to answer on
`127.0.0.1:8765`. If it does not, it installs the previous version again and restarts:

```bash
#!/usr/bin/env bash
set -euo pipefail

VENV=/opt/privacyfence/venv
UNIT=privacyfence-org
PROBE=http://127.0.0.1:8765/.well-known/oauth-authorization-server

current() { "$VENV/bin/python" -c 'from importlib.metadata import version; print(version("privacyfence"))'; }
healthy() {
    for _ in $(seq 12); do
        sleep 5
        if systemctl is-active --quiet "$UNIT" && curl -fsS -o /dev/null "$PROBE"; then
            return 0
        fi
    done
    return 1
}

before=$(current)
"$VENV/bin/pip" install --quiet --upgrade privacyfence
after=$(current)

if [[ "$before" == "$after" ]]; then
    echo "privacyfence $after is the latest stable release"
    exit 0
fi

echo "Upgraded privacyfence $before -> $after; restarting $UNIT"
systemctl restart "$UNIT"
if healthy; then
    echo "$UNIT is healthy on $after"
    exit 0
fi

echo "$UNIT is not healthy on $after; going back to $before" >&2
"$VENV/bin/pip" install --quiet "privacyfence==$before"
systemctl restart "$UNIT"
exit 1
```

The script runs pip as root because root owns the virtual environment
([section 3](#3-service-account-and-install)); the service account still cannot change its own
code.

`/etc/systemd/system/privacyfence-upgrade.service`:

```ini
[Unit]
Description=Upgrade PrivacyFence to the latest stable release
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/privacyfence-upgrade
```

`/etc/systemd/system/privacyfence-upgrade.timer`:

```ini
[Unit]
Description=Nightly PrivacyFence upgrade

[Timer]
OnCalendar=*-*-* 04:17:00
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

Turn it on, run it once by hand and read the result:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now privacyfence-upgrade.timer
sudo systemctl start privacyfence-upgrade.service
journalctl -u privacyfence-upgrade.service -n 30
systemctl list-timers privacyfence-upgrade.timer
```

Notes:

- **The fallback is a safety net, not a rollback.** It catches a daemon that does not start or does
  not answer. It does not catch one that starts but misbehaves, it does not undo dependency
  upgrades pip made along the way, and going back is safe only if the earlier version reads the
  data as the newer one left it. When it fires, restore from the backup if the older version
  misbehaves too.
- **Alerts.** A failed run leaves `privacyfence-upgrade.service` in the `failed` state. Add
  `OnFailure=` to its `[Unit]` section, pointing at a notification unit, or alert on it alongside
  the daemon ([Monitoring](#monitoring)).
- **Majors.** `pip install --upgrade` moves to 8.x as soon as it is on PyPI. Pin the major in the
  script (`privacyfence<8`) until the Admin page says `Ready for PrivacyFence 8: yes.`
  ([Upgrading to 8.x](#upgrading-to-8x)).
- **Holding a version.** `sudo systemctl disable --now privacyfence-upgrade.timer` stops automatic
  upgrades until you enable the timer again.

### Monitoring

- **Probe:** `GET https://pf.acme.example.com/.well-known/oauth-authorization-server` returns `200`
  without authentication while the daemon and proxy are up. There is no separate health endpoint.
- **Service:** alert on `systemctl is-failed privacyfence-org` and on repeated restarts.
- **Log lines worth alerting on:** `Refusing to start`, `Configuration error`, `Fatal error`,
  `Connector registry is at capacity`, `registered-client limit`, and failures to reach the identity
  provider.
- **Disk:** the data directory grows with audit logs and staged downloads.
- **Audit forwarding:** with audit forwarding on (**Admin → Audit forwarding**), every audit entry is also sent to syslog
  or your HTTPS endpoint (an HTTPS bearer token comes from the environment variable named by
  `audit_forwarding.http.bearer_token_env` in `org/admin_config.json`, which has no control on the
  page; set that name by hand and put the token in the environment file): the install's own log (`authority/logs/audit/`) and each person's
  (`users/<principal>/logs/audit/`), all carrying the install's `deployment_id`. The local logs
  stay the authoritative record, each with a hash chain over its entries.

### Key and secret rotation

- **Connector-config signing key:** stop the daemon, delete `org/connector_config_signing_pubkey.txt`,
  start it and upload a config signed with the new key on **Admin → Services**; confirm its
  fingerprint ([section 6](#signed-connector-configs)). Serials are kept.
- **A bundle's signing key** (the legacy bundle of an upgraded server, or a desktop install's
  bundle): stop the daemon, delete `org/org_bundle_signing_pubkey.txt` and start; the next bundle
  accepted pins the new key. After an upgrade, deleting the new-name pin is the rotation: the 6.x pin
  `org/org_config_signing_pubkey.txt` is not copied over it again. Delete the 6.x pins too only when
  going back to 6.x is no longer wanted. Delete `org_bundle_serial.txt` only to reset the serial.
- **Identity provider client secret:** write the new secret into the file named by
  `PRIVACYFENCE_IDP_CLIENT_SECRET_FILE` (or change `PRIVACYFENCE_IDP_CLIENT_SECRET`) and restart.
- **Connector client secrets:** sign a new connector config for that provider with a higher serial
  and upload it. People's existing connector tokens keep working unless the provider revokes them.
- **The relay token:** `org/relay_token.txt` is kept, not rotated. If you replace it on purpose, the
  server registers again with the new token, and with the built-in Google sign-in every
  `_privacyfence-relay` DNS record must be published again with the new value (the log lists them).
- **Push-notification key:** stop the daemon, delete `org/web_push_vapid_key.pem` and start; the
  daemon generates a new one. Every browser's subscription is bound to the old key, so each person's
  browser subscribes again, under the new key, the next time they open `/approvals`.

### Limits

| Limit | Value | Configurable |
|---|---|---|
| People with connectors loaded at once | 200; a person idle for 30 minutes is unloaded first. Beyond that, requests fail with `Connector registry is at capacity`. | No |
| Registered AI clients | 2,000; registrations unused for 180 days are removed when a new client registers | No |
| Size of one client registration | 8 KiB | No |
| Sign-ins in progress | 1,000 at a time, each valid 5 minutes | No |
| Pending approvals | 50 across the install, 20 per person; a pending approval expires after 15 minutes | `web.approvals` in the install-wide `settings.yaml` ([configuration reference](configuration-reference.md)) |
| Inline download | 100,000-byte tool result, base64 included | Admin → File delivery |
| Download link lifetime | 300 seconds | Admin → File delivery |
| Upload slot | 50,000,000 bytes, 10 minutes; 20 minutes once a tool call has read it | No |
| Gmail attachments per message | 18,000,000 bytes in total | No |
| AI client access token | 1 hour | No |
| AI client refresh chain | 30 days | No |
| Browser session | 30 minutes idle, 24 hours at most | No |

## 15. Troubleshooting

**The daemon will not start.** Run `journalctl -u privacyfence-org -n 50`. The last lines name the
cause:

| Message contains | Fix |
|---|---|
| `PRIVACYFENCE_MODE must be "local" or "org"` | Fix the value in the environment file. |
| `Set both PRIVACYFENCE_IDP_ISSUER and PRIVACYFENCE_IDP_CLIENT_ID, or neither.` | Only one of the two is set. Set both ([section 4](#4-identity-provider)). |
| `Set PRIVACYFENCE_IDP_CLIENT_SECRET or PRIVACYFENCE_IDP_CLIENT_SECRET_FILE, not both.` | Keep one of them. |
| `Organization mode without an identity provider is single-user mode and runs only at http://localhost` | The server has a public `PRIVACYFENCE_ISSUER_URL` but no identity provider. Set `PRIVACYFENCE_IDP_ISSUER` and `PRIVACYFENCE_IDP_CLIENT_ID`, or `PRIVACYFENCE_IDP=builtin-google` ([section 6](#the-environment-file)). |
| `This data directory belonged to an organization server` | The identity provider variables were removed from a server that had them. Put them back; do not start a server's data directory as a single-user install. |
| `PRIVACYFENCE_IDP=builtin-google cannot be combined with your own IdP settings.`, `The built-in Google sign-in needs PRIVACYFENCE_ADMIN_EMAILS.`, `The built-in Google sign-in needs a public https:// server address.`, `This build has no built-in Google sign-in` | See [Built-in Google sign-in](#built-in-google-sign-in). The last means a source checkout: use your own identity provider. |
| `trusted_proxies must name specific proxies, not a wildcard.` | `PRIVACYFENCE_TRUSTED_PROXIES` lists the proxy's address, not `*` or `0.0.0.0/0` ([section 7](#7-reverse-proxy-and-tls)). |
| `The stored <provider> connector config … -- refusing to start` | The file in `org/connector_configs/` no longer verifies against the pinned key, or was damaged. Delete `org/connector_configs/<provider>.txt`, start the server, and upload the provider's config again on **Admin → Services** (a file with a higher serial than the one before; the signing key must be the pinned one, [section 6](#signed-connector-configs)). |
| `org_bundle.json has no "idp" section; set PRIVACYFENCE_IDP_*` | An upgraded server's bundle carries no identity provider. Set the identity provider variables and run `privacyfence-app --retire-bundle` ([section 6](#a-6x-bundle-on-an-upgraded-server)). |
| `Organization servers no longer take a new org_bundle.json`, `has "mode": "org" but is not signed`, `failed signing-key verification`, or `rollback refused` | The adopted `org/org_bundle.json` was edited or replaced. Put the original back (a copy of `org/org_config.json`), and make changes in the environment file and on the Admin page: a server takes no new bundle. |
| `This data was last used by PrivacyFence … Install … or newer.` | The data was written by a newer release. Install that release, or restore the backup from before the upgrade. |
| `is not HTTPS -- org mode requires every IdP endpoint to be HTTPS`, or a discovery error | The identity provider's issuer or endpoints are not HTTPS, or the server cannot reach `<issuer>/.well-known/openid-configuration`. |
| `Refusing to start in organization mode:` followed by a permissions finding | A directory in the data directory grants group or other access. Set it back to `0700`, owned by `privacyfence-org`. |
| `auto_accept_rules` or `auto_accept_grants` | See [Upgrade](#upgrade). |

**The service is running but nothing answers.** Check that the install-wide `settings.yaml` has
`web.mcp.enabled: true`.

**Every request returns `400 Invalid Host header`.** The proxy is not passing the public hostname
in `Host`, or `PRIVACYFENCE_ISSUER_URL` names a different hostname. The startup log line lists the
accepted names.

**Approving, signing out or saving settings fails with "cross-origin request rejected".** The daemon
does not see the request as HTTPS. Make the proxy send `X-Forwarded-Proto: https` and list its address in
`PRIVACYFENCE_TRUSTED_PROXIES`.

**A connector's consent screen shows `redirect_uri_mismatch` or a similar error.** The connector app
does not list that service's exact redirect URI. Each Google service has its own
(`/oauth/callback/gmail`, `/oauth/callback/apps_script` and so on); see
[section 5](#5-connector-apps).

**A service is missing from `/connect`.** It is not offered. An administrator turns it on under
**Admin → Services**; Peek and Grist also need their server address ([section 5](#5-connector-apps)).

**Admins do not see the Admin page.** The admin rule does not match. Look at an admin's ID token:
for admins by email, `email_verified` must be exactly `true` (Microsoft Entra ID never sends it: use
the group or role claim) and, with Google, `hd` must match the address; for a claim, the claim must be
in the ID token (with Google it never is), and its value must be exactly what you set, for example not a
Keycloak group path with a leading `/` or an Entra group's name instead of its Object ID. See
[section 4](#who-is-an-admin). After changing the environment file or the provider, restart and sign
out and back in.

**Sign-in fails for some people.** They are outside the allowed domains or required groups on
**Admin → Sign-in**, or their address is not verified: allowed domains need `email_verified: true`
(and, with Google, a matching `hd`), which Microsoft Entra ID never sends, so an Entra organization sets
`PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL=true` ([section 4](#who-may-sign-in)). With the built-in
Google sign-in the person's domain also needs its DNS record: `/login` says `Sign-in is waiting for
DNS` and `privacyfence-app --relay-dns-records` lists what to publish.

**Approving asks for a passkey that the person does not have.** With passkey required (**Admin → Step-up**),
they enroll one on `/security` first. Without it, they can choose to sign in again instead.

**An AI client cannot register: "registered-client limit".** 2,000 clients are registered and none
has been unused for 180 days. Remove `org/oauth_clients.json` entries you no longer need (with the
daemon stopped), or wait for the prune.

**The Admin page shows `Relay registration failed: …`, or Connect returns to `/connect` with an
error before anyone reaches the provider.** The built-in apps cannot sign in until the server is
registered with the relay ([section 5](#built-in-apps-through-the-relay)). The server tries again by
itself, after one minute and then at growing intervals up to an hour; offering a service or fixing the
cause makes it check at once. The reason after `failed:` is one of:

| Reason | Cause and fix |
|---|---|
| `proof_failed` | The relay could not read the server's token at `https://<host>/.well-known/privacyfence-relay` from the internet. Check DNS and the certificate, that the proxy passes the path through ([section 7](#optional-restrict-access-by-ip-address) excludes it from an IP rule), and that a service using a built-in app is offered: the server answers 404 on that path otherwise. |
| `host_not_public` | The relay refuses the host name: `localhost`, an IP address, a name ending in `.local`, `.internal`, `.home.arpa` or `.lan`, or a host under a public suffix anyone can register under (such as `github.io` or `pages.dev`). Use a name under a domain you own, or your own apps. |
| `revoked` | PrivacyFence's maintainers revoked this host. Use your own apps (uploaded as connector configs) and write to PrivacyFence (see the relay terms). |
| `too_soon` | Too many recent attempts for this host or from this address. Wait; the server retries by itself. |
| `too_many_new_hosts` | At most 5 new hosts can register from one address per UTC day. The server retries the next day; a previous registration is not affected. |
| `invalid_request` | The relay did not accept the request, usually a malformed `PRIVACYFENCE_ISSUER_URL`. |
| `the relay needs an https issuer_url with no path` | The server address is `http://` or has a path. Use `https://host` with no path, or your own apps. |
| `could not reach oauth.privacyfence.eu` or `HTTP <status>` | Outbound HTTPS to `oauth.privacyfence.eu` is blocked, or the relay is down. |

In a person's browser, the relay can also answer **"This sign-in cannot be completed: the PrivacyFence
server it belongs to is not registered with the PrivacyFence relay. Ask your administrator."** The
server has no verified registration within the last 35 days, or the sign-in started on a different
host name than the one registered. Check the status on **Admin → Services**. With the built-in Google
sign-in, **"This Google account's organization is not allowed to sign in to this PrivacyFence server."**
means the account's domain has no proven DNS record yet.

**The log says settings "are ignored".** Three warnings read this way, and none stops the server:

- `The installed bundle carries settings that are now managed on the Admin page and are ignored: …` : a
  bundle still carries operational settings. They were imported once into `org/admin_config.json`;
  change them on the Admin page, not in the bundle.
- `org/org_config.json changed after the upgrade and is ignored` : 7.x reads `org/org_bundle.json`.
  A server takes no new bundle, so put the change in the environment file or on the Admin page.
- `PRIVACYFENCE_IDP_* and PRIVACYFENCE_ADMIN_EMAILS are ignored: org_bundle.json has an "idp" section.` :
  the legacy bundle's identity provider wins over the environment until you retire the bundle
  ([section 6](#a-6x-bundle-on-an-upgraded-server)).

**A download link returns 404.** It has expired, was already used, or belongs to someone else. Ask
the AI client to run the download again.

**An upload through nginx fails with 413.** Raise `client_max_body_size` above 50 MB
([section 7](#nginx)).
