# Atlassian setup (Jira and Confluence)

PrivacyFence connects to **Jira Cloud** and **Confluence Cloud** through one Atlassian OAuth 2.0
(3LO) app: a single sign-in covers both connectors. One administrator creates the app once per
organization and puts its client ID and secret into the organization config bundle; users then
approve it in their browser from PrivacyFence.

PrivacyFence supports Atlassian Cloud (`*.atlassian.net` sites) only, not Jira or Confluence Data
Center or Server.

## What you need

- An Atlassian account that can create apps in the
  [developer console](https://developer.atlassian.com/console/myapps/).
- Python 3 to run `scripts/build_org_bundle.py`.
- Which deployment you are building for. An Atlassian app has a single callback URL, so desktop
  installs and an organization server each need **their own app** (and their own bundle). If you
  run only one kind, you need only one app.

## Register the app

1. In the [developer console](https://developer.atlassian.com/console/myapps/), **Create → OAuth
   2.0 integration**. Name it (for example `PrivacyFence`, or `PrivacyFence (server)` for the
   organization server's app) and click **Create**.
2. **Authorization → OAuth 2.0 (3LO) → Add/Configure:** set the **Callback URL** to the local
   redirect (desktop installs' app) or the org redirect (the server's app) from the
   [values table](#values).
3. **Permissions:** add the **Jira API** and **Confluence API**, then their scopes from the values
   table. The two products need different kinds of scope:
   - **Jira: classic scopes.** Jira works with them, and Atlassian recommends classic scopes for
     Jira where they exist. Granular Jira scopes do not map one-to-one and fail with 401 *"scope
     does not match"*.
   - **Confluence: granular scopes.** The Confluence v2 API that PrivacyFence uses to list spaces
     accepts only granular-scoped tokens and returns 401 *"scope does not match"* for classic ones.

   Classic and granular scopes are separate per product, so mixing them in one app is fine.
   `offline_access` (which lets PrivacyFence refresh the token without a new sign-in) is not in the
   Permissions picker; PrivacyFence adds it to the sign-in request itself.
4. **Settings:** copy the **Client ID** and **Secret**.

Changing an app's scopes later does not change existing tokens: each user must **Reconnect…** Jira
or Confluence to get a token with the new scopes.

## Values

| Item | Value |
|---|---|
| Local redirect (desktop installs' app) | `http://127.0.0.1:53684/callback` — exactly this; Atlassian matches the string literally, so `localhost` does not match |
| Org redirect (organization server's app) | `https://<server>/oauth/callback/atlassian`, with `<server>` your server's `--server-issuer-url`. The same URL serves both Jira and Confluence. |
| Jira scopes (classic) | `read:jira-work`, `write:jira-work`, `read:jira-user` |
| Confluence scopes (granular) | `read:space:confluence`, `read:page:confluence`, `write:page:confluence`, `read:content:confluence`, `read:content-details:confluence`, `write:content:confluence`, `read:attachment:confluence` |
| Added automatically | `offline_access` |
| Bundle flags | `--atlassian-client-id <Client ID>` and `--atlassian-client-secret <Secret>` (both required together) |

What the Confluence scopes gate: `read:content-details:confluence` covers CQL and text search and
content details; `write:content:confluence` is needed with `write:page:confluence` to create pages;
`read:attachment:confluence` covers both listing (`confluence_list_attachments`) and downloading
(`confluence_download_attachment`) attachments. PrivacyFence requests no delete scope: no connector
can delete Jira or Confluence content.

## Build and distribute the bundle

`scripts/build_org_bundle.py` is in the PrivacyFence source repository and is attached to every
stable GitHub Release. It needs only Python 3; no PrivacyFence install is required.

```bash
python3 scripts/build_org_bundle.py \
  --atlassian-client-id abcdef01234567890 \
  --atlassian-client-secret abcdef0123456789abcdef0123456789 \
  -o org_config.json --merge
```

`--merge` adds Atlassian to an existing `org_config.json`; drop it if this is the first service in
the bundle. A bundle's `atlassian` section holds one app, so the desktop installs' bundle gets the
desktop app's credentials and the organization server's bundle gets the server app's. Build the
server's bundle with the server flags and install it on the server — see
[org-mode-setup-guide.md](org-mode-setup-guide.md) for signing and the server flags.

## Users connect

Users install the bundle and click **Authenticate…** next to Jira or Confluence (desktop) or
**Connect** (organization server), then sign in and click **Accept** on Atlassian's page. Either
button connects both products. The shared flow is described in
[connecting-a-service.md](connecting-a-service.md). Atlassian-specific points:

- **More than one site:** a desktop install asks which site to connect; an organization server
  connects the first site the account can reach. A user who needs a different site on an
  organization server needs an Atlassian account limited to that site.
- The token refreshes itself; users sign in again only when it is revoked or the app's scopes
  change.

## Troubleshooting

**"The app's callback URL is invalid" during sign-in** — the app's Callback URL does not match the
deployment it is used for: exactly `http://127.0.0.1:53684/callback` for desktop installs, or
`https://<server>/oauth/callback/atlassian` for an organization server. An app holds only one, so
check that the bundle contains the right app's credentials.

**401 right after connecting** — check the Callback URL (above) and that both the Jira API and the
Confluence API, with their scopes, are on the app.

**Confluence connects, but space or page calls fail with 401 "scope does not match"** — the
Confluence scopes were added as classic scopes. Add the granular ones listed above, then have users
**Reconnect…**.

**Jira fails with 401 "scope does not match"** — the Jira scopes were added as granular scopes.
Switch Jira back to the classic scopes listed above, then have users **Reconnect…**.

**`confluence_list_attachments` or `confluence_download_attachment` fail with 401** — the
`read:attachment:confluence` scope is missing from the app, or the user's token predates it. Add
the scope if needed, then have the user **Reconnect…**.

**403 on specific projects or spaces** — the user's Atlassian account has no access to that project
or space. Check permissions in Jira or Confluence.

**Wrong site connected** — on a desktop install, **Reconnect…** and pick the other site.
