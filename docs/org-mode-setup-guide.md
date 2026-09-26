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
6. [Signing key and bundle](#6-signing-key-and-bundle)
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

An organization deployment has five parts:

| Part | What it is |
|---|---|
| The daemon | `privacyfence-app`, installed from PyPI into a virtual environment and run by systemd as a dedicated, unprivileged account. It listens on `127.0.0.1:8765`. |
| The bundle | `org_config.json`: the organization's settings (server URL, identity provider, connector apps, step-up, downloads, audit forwarding), built and signed with `build_org_bundle.py`. |
| A reverse proxy | Caddy or nginx on the same host. It terminates HTTPS on your public hostname and forwards to the daemon. |
| Your identity provider | Any OpenID Connect (OIDC) provider your staff already sign in to. It decides who a person is. |
| Connector apps | One OAuth app per service you offer (Google, Slack, Salesforce, Atlassian). Each person authorizes their own account against it. No credentials are shared between people. |

What people see, all at `https://<your-hostname>`:

| Page | Purpose |
|---|---|
| `/login` | Sign in through the identity provider. |
| `/connect` | Connect or reconnect Gmail, Drive, Calendar, Contacts, Tasks, Apps Script, Slack, Salesforce, Jira, Confluence and Telegram. |
| `/approvals` | Pending AI requests to approve or deny. |
| `/security` | Enroll and remove passkeys. |
| `/settings` | Each person's own always-allow rules and recent audit entries; for admins also the install-wide privacy policy and the **AI systems** page. |
| `/mcp` | The MCP endpoint AI clients connect to. It uses OAuth 2.1 with dynamic client registration. |

**This is the only way claude.ai can use PrivacyFence.** A desktop install listens on `localhost`,
which claude.ai's servers cannot reach. Claude Desktop, Claude Code and other MCP-compatible clients
can use either kind of install. [Section 9](#9-first-sign-in-and-validation) shows how to add each
client.

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
  you offer, and, unless you turn push notifications off, to the browser push services:
  `*.push.apple.com`, `fcm.googleapis.com`, `*.push.services.mozilla.com` and
  `*.notify.windows.com` ([section 12](#notifications-on-a-phone)).
- **Admin access to your identity provider**, to register one OIDC client.
- **Admin access to each connector's developer console** for the services you want to offer.
- **`build_org_bundle.py`**, which is not part of the PyPI package. Download it from the GitHub
  Release that matches the PrivacyFence version you install (it is attached to every stable
  release), or take `scripts/build_org_bundle.py` from the repository at that version's tag. It
  needs only the Python standard library, plus the `cryptography` package for signing.

## 3. Service account and install

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
Create the `org/` directory now, as the service account, so it has the right owner before you copy
the bundle in:

```bash
sudo -u privacyfence-org install -d -m 700 /var/lib/privacyfence-org/.privacyfence/org
```

The layout of the data directory is in [section 14](#backup).

## 4. Identity provider

Register one OIDC client for PrivacyFence with your identity provider (for example a "Web
application" client in Google Cloud, an app registration in Microsoft Entra ID, or a client in
Okta or Keycloak). Note its **issuer URL**, **client ID** and **client secret**.

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

Each person's data lives under `users/<sub>/` in the data directory, keyed by the provider's `sub`
claim. A `sub` that contains characters other than letters, digits, `.`, `_`, `@` and `-` (or is
longer than 200 characters) is stored under a stable hash instead (`idp-<hash>`).

### Who is an admin

Admins see the **General**, **Privacy Filter** and **AI systems** pages in Settings. Nobody is an
admin unless you name a claim and its values with `--idp-admin-group-claim` and
`--idp-admin-group-value` ([section 6](#6-signing-key-and-bundle)). A person is an admin when the
claim contains any of the values. A claim that is a single string is matched like a one-item list.

**If Google is your identity provider, use `email` as the admin claim.** Google ID tokens carry no
group membership and no Workspace admin role, so `groups` never matches anyone. List each admin's
exact address:

```bash
  --idp-admin-group-claim email \
  --idp-admin-group-value alice@acme.example.com \
  --idp-admin-group-value bob@acme.example.com \
```

Do not use `hd` (the Workspace domain): every account in the domain has the same value, so every
account would be an admin.

Admin status is decided at sign-in and kept for the browser session. After you change the admin
list and restart the daemon, admins sign out and back in to pick it up.

### Who may sign in

By default, everyone your identity provider authenticates may use PrivacyFence. To narrow that, add
either or both of these to the bundle:

- `--authz-allowed-domain acme.example.com` (repeatable): only email addresses at these domains.
- `--authz-groups-claim groups --authz-required-group privacyfence-users` (value repeatable): only
  people whose claim contains one of the values. This is independent of the admin claim.

A person who fails either check is refused, and no session is created.

## 5. Connector apps

Each connector needs its own OAuth app registered as a **web** app with an HTTPS redirect URI on
your hostname. The daemon builds the redirect URI from your server URL and the service name, and
the connector's console must list it exactly.

| Service | Redirect URI to register | Bundle flags | Setup guide |
|---|---|---|---|
| Gmail | `https://pf.acme.example.com/oauth/callback/gmail` | `--google-client-secret` | [Google Cloud setup](google-cloud-setup.md) |
| Google Drive | `https://pf.acme.example.com/oauth/callback/drive` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Google Calendar | `https://pf.acme.example.com/oauth/callback/calendar` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Google Contacts | `https://pf.acme.example.com/oauth/callback/contacts` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Google Tasks | `https://pf.acme.example.com/oauth/callback/tasks` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Google Apps Script | `https://pf.acme.example.com/oauth/callback/apps_script` | (same Google client) | [Google Cloud setup](google-cloud-setup.md) |
| Slack | `https://pf.acme.example.com/oauth/callback/slack` | `--slack-client-id`, `--slack-client-secret` | [Slack setup](slack-setup.md) |
| Salesforce | `https://pf.acme.example.com/oauth/callback/salesforce` | `--salesforce-consumer-key`, `--salesforce-consumer-secret`, `--salesforce-login-url` | [Salesforce setup](salesforce-setup.md) |
| Jira and Confluence | `https://pf.acme.example.com/oauth/callback/atlassian` | `--atlassian-client-id`, `--atlassian-client-secret` | [Atlassian setup](atlassian-setup.md) |
| Telegram | none | none | [Telegram setup](telegram-setup.md) |

Notes:

- **Google**: one OAuth client of type **Web application** covers all six Google services, but it
  must list all six redirect URIs, one per service you offer. Download its JSON file and pass it to
  `--google-client-secret`. A Desktop app client cannot be used here, because it has no field for
  these redirect URIs.
- **Use a separate Google client for the connectors**, not the one you registered in
  [section 4](#4-identity-provider) for sign-in. A single client works, but Google then reports the
  sign-in scopes (`openid`, `userinfo.email`, `userinfo.profile`) in every connector's granted
  scopes, which makes audit and revocation harder to read.
- **Jira and Confluence** share one Atlassian app and one redirect URI, because an Atlassian OAuth
  app accepts only one callback URL.
- **Telegram** needs nothing from you: its app credentials ship inside PrivacyFence. Each person
  signs in with their phone number on `/connect`.
- A connector appears on `/connect` only if its section is in the bundle.

The same app registrations can also carry the loopback redirect URIs used by desktop installs; the
setup guides cover both.

## 6. Signing key and bundle

The daemon refuses to start in organization mode unless the bundle is signed. The first signed
bundle a server sees pins its signing key (trust on first use). Every later bundle must verify
against that key.

### Generate the signing key

Run this once per organization on an admin workstation, not on the server. It needs the
`cryptography` package:

```bash
python3 -m venv ~/pf-bundle && ~/pf-bundle/bin/pip install cryptography
~/pf-bundle/bin/python build_org_bundle.py --generate-signing-key ~/secure/org-signing-key.pem
```

The private key is written with mode `0600` and its public key is printed. Keep the file secret and
backed up: every future bundle, including `--merge` runs, must be signed with it.

### Build the bundle

```bash
~/pf-bundle/bin/python build_org_bundle.py \
  --org-name "Acme Corp" \
  --mode org \
  --server-issuer-url https://pf.acme.example.com \
  --server-trusted-proxy 127.0.0.1 \
  --idp-issuer https://idp.acme.example.com \
  --idp-client-id <client id from section 4> \
  --idp-client-secret <client secret from section 4> \
  --idp-admin-group-claim groups --idp-admin-group-value privacyfence-admins \
  --google-client-secret /path/to/client_secret.json \
  --slack-client-id ... --slack-client-secret ... \
  --step-up-enabled \
  --sign-key ~/secure/org-signing-key.pem \
  -o org_config.json
```

The script writes `org_config.json` with mode `0600` and prints what it contains.

| Option | Default | What it sets |
|---|---|---|
| `--org-name NAME` | none | The organization name. |
| `-o`, `--output PATH` | `org_config.json` | Where to write the bundle. |
| `--merge` | off | Update the existing bundle at the output path instead of starting over. See below. |
| `--mode org` | none | Required. Without it the bundle describes a desktop install. |
| `--server-issuer-url URL` | none (required) | The public origin people and AI clients use, for example `https://pf.acme.example.com`. Every redirect URI and link is built from it. |
| `--server-bind-host HOST` | `127.0.0.1` | The address the daemon listens on. Change it only when the proxy runs on another host. |
| `--server-port PORT` | `8765` | The port the daemon listens on. |
| `--server-tls-cert PATH`, `--server-tls-key PATH` | none | Let the daemon terminate TLS itself. Leave both unset when the proxy terminates TLS. |
| `--server-trusted-proxy IP` | none | A proxy address whose `X-Forwarded-For`/`X-Forwarded-Proto` headers are trusted (repeatable). See [section 7](#7-reverse-proxy-and-tls). |
| `--idp-issuer URL`, `--idp-client-id ID`, `--idp-client-secret SECRET` | none (required) | The OIDC client from [section 4](#4-identity-provider). |
| `--idp-admin-group-claim CLAIM`, `--idp-admin-group-value VALUE` | nobody is an admin | Who is an admin ([section 4](#who-is-an-admin)). The value is repeatable. |
| `--authz-allowed-domain DOMAIN` | anyone | Email domains allowed to sign in (repeatable). |
| `--authz-groups-claim CLAIM`, `--authz-required-group VALUE` | anyone | A claim and the values required to sign in (value repeatable). |
| `--google-client-secret PATH` | none | The Google web client's JSON file ([section 5](#5-connector-apps)). |
| `--slack-client-id`, `--slack-client-secret` | none | The Slack app. `--slack-scopes` overrides the default user-token scopes; normally leave it unset. |
| `--salesforce-consumer-key`, `--salesforce-consumer-secret` | none | The Salesforce Connected App. |
| `--salesforce-login-url URL` | `https://login.salesforce.com` | Use `https://test.salesforce.com` for sandboxes. |
| `--atlassian-client-id`, `--atlassian-client-secret` | none | The Atlassian app for Jira and Confluence. |
| `--step-up-enabled` / `--step-up-disabled` | off | Require a fresh passkey or sign-in before an approval is released ([section 12](#12-approvals-and-step-up)). |
| `--step-up-scope SCOPE` | `writes_and_pii_reads` | Which approvals need step-up: `writes`, `writes_and_pii_reads` or `writes_and_reads`. |
| `--step-up-require-passkey` / `--step-up-no-require-passkey` | off | Accept only a passkey for step-up, never a fresh sign-in. |
| `--step-up-rp-id DOMAIN` | the hostname of `--server-issuer-url` | The WebAuthn relying-party ID. Set it only to use a parent domain. |
| `--step-up-rp-name NAME` | `PrivacyFence` | The name shown in the operating system's passkey prompt. |
| `--idp-step-up-acr-value ACR` | none | `acr_values` sent when step-up asks the identity provider for a fresh sign-in (repeatable). Without it, the request uses `prompt=login` and `max_age=0` only. |
| `--enable-unattended-sessions` / `--disable-unattended-sessions` | off | Let an AI client declare a scheduled, unattended run (`privacyfence_begin_unattended_session`). The declaration is advisory and grants nothing by itself; see [how it works](how-it-works.md). |
| `--downloads-inline-max-bytes BYTES` | `8000000` | Files up to this size are returned inside the tool result. `0` sends every file through a download link ([section 13](#13-file-delivery)). |
| `--downloads-link-ttl-seconds SECONDS` | `300` | How long a download link stays valid. |
| `--downloads-disable-staging` | staging on | Refuse a file too large to return inline instead of staging it on disk. |
| `--agent-links` / `--no-agent-links` | agent links on | Whether the AI client can fetch a download link itself, or only a signed-in person in a browser can. |
| `--web-push` / `--no-web-push` | on | Push a notification to a person's phone or browser when an approval is waiting. `--no-web-push` turns it off for the whole organization ([section 12](#notifications-on-a-phone)). |
| `--enable-audit-forwarding` / `--disable-audit-forwarding` | off | Send audit entries to syslog or an HTTPS endpoint ([section 14](#monitoring)). |
| `--audit-forwarding-kind syslog\|http` | `syslog` | The forwarding target. |
| `--audit-forwarding-syslog-host HOST` | none | The syslog server (required for `syslog`). |
| `--audit-forwarding-syslog-port PORT` | `6514` | The syslog port. |
| `--audit-forwarding-syslog-protocol udp\|tcp` | `tcp` | The syslog transport. PrivacyFence sends plain syslog; it does not add TLS. |
| `--audit-forwarding-http-url URL` | none | An `https://` endpoint that receives one JSON object per entry (required for `http`). |
| `--audit-forwarding-http-bearer-token-env NAME` | none | The name of an environment variable the daemon reads a bearer token from. The token itself never goes in the bundle; set the variable in the systemd unit ([section 8](#8-hardened-systemd-unit)). |
| `--generate-signing-key PATH` | | Create a signing key and exit. |
| `--sign-key PATH` | none (required with `--mode org`) | Sign the bundle. |

`python3 build_org_bundle.py --help` lists the same options. The daemon also reads a few bundle
keys that the script does not set, and each person's policy lives in `settings.yaml`; the
[configuration reference](configuration-reference.md) lists every key.

**Step-up scope and the default.** If you never pass `--step-up-scope`, the bundle carries no scope
and the daemon uses its default, `writes_and_pii_reads`. If you pass a value, it is written into the
bundle and stays exactly that. To keep step-up to writes only, pass `--step-up-scope writes`
explicitly. `enabled` and `require_passkey` work differently: absent from the bundle, both are off.

### Changing a bundle later

`--merge` reads the existing bundle at the output path, applies only the flags you pass, and signs
it again. Always pass the same `--sign-key`.

Two sections are rebuilt as a whole rather than merged: `--mode org` rebuilds `server` and `idp`
from that run's flags. So to change anything about the server or the identity provider (for
example the admin list, or a rotated client secret), pass `--mode org`, `--server-issuer-url`, every
`--server-*` flag you use (a missing `--server-bind-host` goes back to `127.0.0.1`) and every
`--idp-*` flag again. Connector, step-up, download, authorization and forwarding flags merge into
their existing sections.

### Install the bundle

Copy the bundle to the server and install it as the service account's file:

```bash
sudo install -o privacyfence-org -g privacyfence-org -m 600 org_config.json \
  /var/lib/privacyfence-org/.privacyfence/org/org_config.json
```

Copying the file is the only way to install a bundle on an organization server. The daemon reads
it once at startup, so restart the service after replacing it (`sudo systemctl restart
privacyfence-org`). It refuses to start, and says why, if the bundle is malformed, is missing its
`server` or `idp` section, is unsigned, or does not verify against the pinned key.

## 7. Reverse proxy and TLS

The daemon listens on `127.0.0.1:8765` and never needs to be reachable directly. Put a reverse
proxy in front of it that:

- terminates HTTPS for your public hostname;
- passes the original `Host` header through unchanged. The daemon answers only requests addressed
  to the hostname of `--server-issuer-url`, `127.0.0.1` or `::1`; any other `Host` gets
  `400 Invalid Host header`;
- sets `X-Forwarded-For` and `X-Forwarded-Proto`. The daemon compares a browser's `Origin` header
  with the scheme it believes the request used, so without `X-Forwarded-Proto: https` every
  approval, sign-out and settings change is refused as cross-origin;
- does not buffer responses: the approvals page and `/mcp` stream events;
- allows request bodies of at least 50 MB, the size of an upload slot ([section 13](#13-file-delivery)).

The daemon trusts forwarded headers only from the addresses you list with
`--server-trusted-proxy`, and from nowhere by default. A proxy on the same host must be listed as
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

To have the daemon terminate TLS itself, pass `--server-tls-cert` and `--server-tls-key` and set
`--server-bind-host` to the address it should listen on. The certificate and key must be readable
by the service account. A proxy is still the better choice: it handles certificate renewal and rate
limiting.

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
# Only for --audit-forwarding-http-bearer-token-env; the file holds NAME=token, mode 0600, owned by root.
# EnvironmentFile=/etc/privacyfence-org.env

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
which `Host` names are accepted.

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

1. Open `https://pf.acme.example.com/login` and sign in through your identity provider. You land on
   `/connect`.
2. Click **Connect** next to each service. Each one sends you to that service's consent screen and
   back to `/connect`, which then shows it as connected. Nothing needs restarting.
3. Open `/security` and enroll a passkey if step-up is on ([section 12](#12-approvals-and-step-up)).

People reconnect or check a connection the same way; see
[connecting a service](connecting-a-service.md).

### Add PrivacyFence to an AI client

Every client connects to `https://pf.acme.example.com/mcp`. The client discovers the authorization
server from `/.well-known/oauth-protected-resource/mcp`, registers itself at `/register`, and opens
the organization's sign-in page. After sign-in it holds its own tokens; there is nothing to copy or
paste. Access tokens last one hour and are refreshed silently; a refresh chain lasts 30 days, after
which the person signs in again.

- **claude.ai**: add a custom connector (in claude.ai's connector settings) with the URL
  `https://pf.acme.example.com/mcp`, then click **Connect** on it and sign in. Leave the optional
  OAuth client ID and secret empty; claude.ai registers itself. On Team and Enterprise plans an
  owner adds the connector for the organization and each person connects it with their own
  sign-in.
- **Claude Desktop**: add the same URL as a custom connector.
- **Claude Code**: run `claude mcp add --transport http privacyfence
  https://pf.acme.example.com/mcp`, then `/mcp` inside Claude Code to sign in.
- **Other MCP-compatible clients**: any client that supports the Streamable HTTP transport with
  OAuth 2.1 and dynamic client registration.

Each client registration shows up on the admin's **AI systems** page
([section 11](#11-ai-systems)).

### Validation checklist

Run these through the public hostname, not through `127.0.0.1`:

- [ ] `curl -fsS https://pf.acme.example.com/.well-known/oauth-authorization-server` returns JSON
  whose `issuer` is your public URL.
- [ ] Opening `/approvals` in a private window redirects to `/login`.
- [ ] Signing in shows your email address at the top right of the page.
- [ ] Someone outside `--authz-*` (if set) is refused at sign-in.
- [ ] An AI client connects and lists PrivacyFence's tools.
- [ ] A write request from the client appears in your `/approvals`, and not in a second test user's.
- [ ] Approving it (with step-up, if on) completes the request, and it appears under
  **Settings → Audit Log**.
- [ ] After `sudo systemctl restart privacyfence-org`, the AI client keeps working without a new
  sign-in, and the browser asks you to sign in again.

## 10. Install-wide and per-user policy

Policy is split in two:

| What | Scope | Where | Who changes it |
|---|---|---|---|
| Privacy filter (which content categories are shown, redacted or blocked) and PII detection | Install-wide: applies to everyone | `authority/config/settings.yaml` in the data directory | Admins, on **Settings → Privacy Filter** and **Settings → General** |
| Always-allow rules | Per person | `users/<principal>/authority/config/settings.yaml` | Each person, on **Settings → Auto-accept** or with **Always allow** on an approval card |

**Install-wide policy.** A change made in Settings is written to the file and applies to everyone's
next request, without a restart. You can also edit the file by hand and restart the daemon, but do
not mix the two: saving from Settings rewrites the whole file from the copy the daemon loaded at
startup, which drops any hand edit made since, comments and formatting included. In organization
mode a privacy group that is missing from the file is treated as `block` (a desktop install uses
`allow`), and **Privacy Filter** marks each group that is relying on that default. State every group
explicitly so the file documents the intended policy. The categories and their meaning are in
[approvals and policy](approvals-and-policy.md).

**Per-person rules.** Nobody, admins included, can see or change another person's always-allow
rules from the browser. An administrator who must review or remove someone else's rules edits that
person's file on the server.

Settings pages each person sees:

| Page | Everyone | Admins only |
|---|---|---|
| Auto-accept | yes | |
| Audit Log (your own 20 most recent entries) | yes | |
| About | yes | |
| General (PII detection) | | yes |
| Privacy Filter | | yes |
| AI systems | | yes |

Connector management, update checks, the log level, the Calendar free/busy and Gmail signature
switches, and notification preferences are desktop-only settings and do not appear here.

## 11. AI systems

Every approval card and audit entry names the AI system that made the request. How much that name
can be trusted depends on where it came from:

| Source | Recorded as `agent_source` | Shown as |
|---|---|---|
| An admin pinned the client's registration to an AI system | `oauth_client` | Verified, with the AI system's icon |
| The name the client gave when it registered, or in its MCP handshake | `client_info` | Not verified, with the claimed name |
| Nothing | empty | Unrecognised AI system |

Any program that can reach `/register` can call itself anything, so only a pin makes a name
verified. To pin, an admin opens **Settings → AI systems**, which lists every registered client
with its registered name, client ID and when it was last used, checks that a registration really is
what it claims (for example by matching the last-used time to their own sign-in from claude.ai),
and clicks the AI system to pin it to. **Unpin** removes it.

- A pin names one registration, never a name. A client that registers again gets a new client ID
  and starts unverified.
- A registration unused for 180 days is removed the next time any client registers. Its pin stays
  listed under **Stale pins**, applies to nothing, and can be removed there.
- Pinning and unpinning are sensitive settings writes ([section 12](#12-approvals-and-step-up)) and
  are recorded in the audit log with the admin's identity and the registration's claimed name.
- Pins are stored in `org/agent_pins.json` in the data directory, not in the bundle, because client
  IDs are issued at runtime.

The reasoning is in [ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md),
[ADR 0036](adr/0036-card-copy-names-the-caller-through-one-placeholder.md) and
[ADR 0037](adr/0037-a-local-override-is-a-relabel-and-never-attests.md).

## 12. Approvals and step-up

Each person sees and decides only their own approvals, on `/approvals`. What gets gated, and how
cards and always-allow rules work, is the same as on a desktop install; see
[approvals and policy](approvals-and-policy.md).

**Step-up** asks for proof that a person, not a stolen session, is approving. In organization mode
it is off until the bundle turns it on:

| Setting | Organization default | Flag |
|---|---|---|
| Step-up | off | `--step-up-enabled` |
| Scope | `writes_and_pii_reads`: every write, and every read in which PII was detected | `--step-up-scope` |
| Accepted proof | a passkey, or signing in again at the identity provider | `--step-up-require-passkey` accepts a passkey only |
| Relying-party ID | the hostname of `--server-issuer-url` | `--step-up-rp-id` |

With step-up on:

- A person with an enrolled passkey confirms with it. Passkeys are enrolled on `/security`.
- Without `--step-up-require-passkey`, a person may instead sign in again at the identity provider
  (`prompt=login`, `max_age=0`, plus any `--idp-step-up-acr-value`). The sign-in must be the same
  person the approval belongs to.
- With `--step-up-require-passkey`, only a passkey is accepted. Someone with no passkey is sent to
  `/security` to enroll one.
- Approving several cards at once takes a single passkey confirmation that covers the selected set.
  Someone with no passkey cannot approve several at once, with or without
  `--step-up-require-passkey`: they approve each card on its own, or enroll a passkey first.
  Denying never needs step-up.

**Sensitive settings writes.** Adding or removing an always-allow rule, changing the privacy policy
or PII detection, and pinning or unpinning an AI system are sensitive: they change what gets gated
in future. When the bundle sets both `--step-up-enabled` and `--step-up-require-passkey`, each of
these needs a fresh passkey confirmation, exactly as on a desktop install. With either flag off,
they need only a signed-in session. A fresh identity-provider sign-in is never accepted for them. See
[ADR 0034](adr/0034-sensitive-settings-writes-require-step-up-in-both-modes.md).

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
- **Turning it off for everyone.** Rebuild the bundle with `--no-web-push` (`web_push.enabled:
  false`), install it and restart. The subscription routes are then not served, nothing is sent,
  and the pages no longer offer notifications. Subscriptions already stored stay on disk unused;
  delete `users/*/push_subscriptions.json` to remove them.

The reasoning, and why a desktop install has no push at all, is in
[ADR 0081](adr/0081-org-mode-sends-a-count-only-web-push.md).

## 13. File delivery

The server cannot write into a person's own folders, so download tools (`drive_download_file`,
`gmail_download_attachment`, `confluence_download_attachment`) hand the file back in one of two
ways, after the approval:

| File size | Delivery |
|---|---|
| Up to `--downloads-inline-max-bytes` (8,000,000 bytes by default) | Inline: the file's bytes are in the tool result. |
| Larger | Staged: the file is encrypted on disk and the tool result carries a single-use link that expires after `--downloads-link-ttl-seconds` (300 seconds by default). |

Which link a staged file gets:

| Setting | Link | Who can fetch it |
|---|---|---|
| Agent links on (default) | `https://<host>/mcp-files/fetch/<token>` | Whoever holds the link, once, before it expires. The token is the credential, so the AI client can fetch the file itself. |
| `--no-agent-links` | `https://<host>/downloads/<token>` | Only the person the file belongs to, signed in, in a browser. |

Opening a `/downloads/` link while signed out goes through sign-in and then back to the same
link, so the link still works if it has not expired by then. Signing in as someone else does
not claim it: the file is released only to the person it was staged for.

A missing, expired, already-used or other person's token all get the same `404`. With
`--downloads-disable-staging`, a file too large to return inline is refused instead of staged, and
nothing is written to disk. With `--downloads-inline-max-bytes 0`, every file goes through a link.

Staged files live under `users/<principal>/downloads/` in the data directory. Expired files are
removed the next time that person stages or claims a download, and every staged file is removed
when the daemon starts. The approval gate, not the delivery channel, is where the privacy decision
is made; see [ADR 0017](adr/0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md)
and [ADR 0028](adr/0028-clients-without-the-shim-get-capability-urls.md).

**Uploads.** A local file path means nothing to the server. An AI client first calls
`privacyfence_create_upload_slot` with a file name, gets back an `upload_url`, sends the file there
with an HTTP `PUT`, and passes the returned `upload_id` to the tool that needs the file (for
example `drive_upload_file`'s `upload_id`, or `upload:<upload_id>` in a Gmail attachment list). A
slot takes up to 50,000,000 bytes, is valid for 10 minutes and can be used once, and only by the
person who created it. `drive_upload_file` also accepts the file inline as `content_base64`. Gmail
attachments are limited to 18,000,000 bytes in total per message.

## 14. Operations

### Backup

The data directory is `/var/lib/privacyfence-org/.privacyfence/`. Back up all of it except the
paths marked "skip", with the daemon stopped (or from a filesystem snapshot):

| Path in the data directory | Contents |
|---|---|
| `org/org_config.json` | The installed bundle, including the identity provider and connector client secrets. |
| `org/org_config_signing_pubkey.txt` | The pinned signing key. |
| `org/oauth_clients.json` | Registered AI clients. Without it every client has to register again. |
| `org/oauth_refresh.json` | AI clients' refresh tokens, each sealed under its own token. |
| `org/agent_pins.json` | AI-system pins. |
| `org/web_push_vapid_key.pem` | The server's push-notification key. Without it every browser has to subscribe again. |
| `authority/config/settings.yaml` | Install-wide policy. |
| `authority/logs/audit/` | The install's own audit log and its chain key. |
| `deployment_id` | The install's ID, stamped on every audit entry. |
| `users/<principal>/credentials/` | Each person's connector tokens. |
| `users/<principal>/authority/config/settings.yaml` | Each person's always-allow rules. |
| `users/<principal>/authority/webauthn_credentials.json`, `webauthn_recovery_code.json`, `step_up_state.json` | Each person's passkeys and recovery code. |
| `users/<principal>/logs/audit/` | Each person's audit log and its chain key. |
| `users/<principal>/push_subscriptions.json` | Each person's subscribed browsers. |
| `users/<principal>/downloads/`, `users/<principal>/uploads/` | Skip: short-lived encrypted staging. |
| `logs/privacyfence.log` | Optional: the runtime log. |

Also keep, outside the server: the bundle signing key, the systemd unit, the proxy configuration
and any environment file.

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

Read the release's entry in the [changelog](../CHANGELOG.md) first, download the matching
`build_org_bundle.py` for future bundle changes, and back up before upgrading. To go back, install
the earlier version the same way (`pip install privacyfence==X.Y.Z`); that is safe only if the
earlier version reads the data as the newer one left it, so keep the pre-upgrade backup.

A `settings.yaml` that still has `auto_accept_rules` or `auto_accept_grants` sections is refused at
startup, unless it also carries the `migrated_to_policy_v2` marker, in which case those sections
are removed and the file is rewritten.

### Monitoring

- **Probe:** `GET https://pf.acme.example.com/.well-known/oauth-authorization-server` returns `200`
  without authentication while the daemon and proxy are up. There is no separate health endpoint.
- **Service:** alert on `systemctl is-failed privacyfence-org` and on repeated restarts.
- **Log lines worth alerting on:** `Refusing to start`, `Configuration error`, `Fatal error`,
  `Connector registry is at capacity`, `registered-client limit`, and failures to reach the identity
  provider.
- **Disk:** the data directory grows with audit logs and staged downloads.
- **Audit forwarding:** with `--enable-audit-forwarding`, every audit entry is also sent to syslog
  or your HTTPS endpoint: the install's own log (`authority/logs/audit/`) and each person's
  (`users/<principal>/logs/audit/`), all carrying the install's `deployment_id`. The local logs
  stay the authoritative record, each with a hash chain over its entries.

### Key and secret rotation

- **Bundle signing key:** generate a new key, build the bundle with it, stop the daemon, delete
  `org/org_config_signing_pubkey.txt`, install the new bundle and start. The first start pins the
  new key and logs a warning saying so.
- **Identity provider client secret:** rebuild with `--merge --mode org` and every `--server-*` and
  `--idp-*` flag ([section 6](#changing-a-bundle-later)), install, restart.
- **Connector client secrets:** rebuild with `--merge` and that connector's flags, install,
  restart. People's existing connector tokens keep working unless the provider revokes them.
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
| Inline download | 8,000,000 bytes | `--downloads-inline-max-bytes` |
| Download link lifetime | 300 seconds | `--downloads-link-ttl-seconds` |
| Upload slot | 50,000,000 bytes, 10 minutes | No |
| Gmail attachments per message | 18,000,000 bytes in total | No |
| AI client access token | 1 hour | No |
| AI client refresh chain | 30 days | No |
| Browser session | 30 minutes idle, 24 hours at most | No |

## 15. Troubleshooting

**The daemon will not start.** Run `journalctl -u privacyfence-org -n 50`. The last lines name the
cause:

| Message contains | Fix |
|---|---|
| `has "mode": "org" but is not signed` | Rebuild the bundle with `--sign-key`. |
| `failed signing-key verification` | The bundle was signed with a different key than the pinned one. Sign it with the original key, or rotate the key ([section 14](#key-and-secret-rotation)). |
| `requires an "idp" section` or `requires org_config.json's "server"."issuer_url"` | Rebuild with `--mode org` and all `--server-issuer-url` and `--idp-*` flags. |
| `is not HTTPS -- org mode requires every IdP endpoint to be HTTPS`, or a discovery error | The identity provider's issuer or endpoints are not HTTPS, or the server cannot reach `<issuer>/.well-known/openid-configuration`. |
| `Refusing to start in organization mode:` followed by a permissions finding | A directory in the data directory grants group or other access. Set it back to `0700`, owned by `privacyfence-org`. |
| `auto_accept_rules` or `auto_accept_grants` | See [Upgrade](#upgrade). |

**The service is running but nothing answers.** Check that the install-wide `settings.yaml` has
`web.mcp.enabled: true`.

**Every request returns `400 Invalid Host header`.** The proxy is not passing the public hostname
in `Host`, or `--server-issuer-url` names a different hostname. The startup log line lists the
accepted names.

**Approving, signing out or saving settings fails with "cross-origin request rejected".** The daemon
does not see the request as HTTPS. Make the proxy send `X-Forwarded-Proto: https` and list it with
`--server-trusted-proxy`.

**A connector's consent screen shows `redirect_uri_mismatch` or a similar error.** The connector app
does not list that service's exact redirect URI. Each Google service has its own
(`/oauth/callback/gmail`, `/oauth/callback/apps_script` and so on); see
[section 5](#5-connector-apps).

**A service is missing from `/connect`.** Its section is not in the bundle. Rebuild with its flags
and `--merge`, install and restart.

**Admins do not see Privacy Filter or AI systems.** The admin claim does not match. With Google as
the identity provider, use `email` ([section 4](#who-is-an-admin)). After changing it, restart and
sign out and back in.

**Sign-in fails with an authorization error for some people.** They are outside
`--authz-allowed-domain` or `--authz-required-group`.

**Approving asks for a passkey that the person does not have.** With `--step-up-require-passkey`,
they enroll one on `/security` first. Without it, they can choose to sign in again instead.

**An AI client cannot register: "registered-client limit".** 2,000 clients are registered and none
has been unused for 180 days. Remove `org/oauth_clients.json` entries you no longer need (with the
daemon stopped), or wait for the prune.

**A download link returns 404.** It has expired, was already used, or belongs to someone else. Ask
the AI client to run the download again.

**An upload through nginx fails with 413.** Raise `client_max_body_size` above 50 MB
([section 7](#nginx)).
