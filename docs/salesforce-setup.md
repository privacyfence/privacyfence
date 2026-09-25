# Salesforce setup

PrivacyFence signs in to Salesforce with OAuth 2.0 (the web server flow with PKCE). Nobody types a
Salesforce password, security token or API key into PrivacyFence. One administrator registers an
app in Salesforce once per org and puts its consumer key and secret into the organization config
bundle; users then sign in on Salesforce's own page from PrivacyFence.

## What you need

- A Salesforce administrator who can create an **External Client App** or a **Connected App**.
- Your org's login URL: `https://login.salesforce.com` (production), `https://test.salesforce.com`
  (sandbox), or your **My Domain** URL (see [Values](#values)).
- Python 3 to run `scripts/build_org_bundle.py`.

## Register the app

Salesforce offers two kinds of app registration, and either works:

- **External Client App** (**Setup → External Client App Manager → New External Client App**),
  which Salesforce recommends for new integrations. Set **Distribution State** to **Local**.
- **Connected App** (**Setup → App Manager → New Connected App**), if your org still creates
  those.

In either:

1. Enter a name (for example `PrivacyFence`) and a contact email.
2. Enable **OAuth** and enter the **Callback URL**: the local redirect, and for an organization
   server the org redirect on a second line. One app can carry both, and the same consumer key and
   secret serve both kinds of deployment.
3. Add the two **OAuth scopes** from the [values table](#values).
4. Leave the web server (authorization code) flow enabled. PrivacyFence sends PKCE and the consumer
   secret, so the **Require PKCE** and **Require secret for Web Server Flow** options can stay on.
5. **Refresh token rotation** (an External Client App option): on or off, PrivacyFence works — with
   rotation on, it stores the new refresh token Salesforce issues at each refresh. This guide
   recommends turning it **on**, so a leaked refresh token is useless once PrivacyFence has used
   it.
6. **Refresh token policy:** *Refresh token is valid until revoked* keeps users signed in. A shorter
   policy works too; users then **Reconnect…** when it expires.
7. Save, then copy the **Consumer Key** and **Consumer Secret** (in a Connected App: **View →
   Manage Consumer Details**; in an External Client App: **Settings → OAuth Settings → Consumer Key
   and Secret**). Salesforce may ask you to verify your identity first.

A new app can take **2–10 minutes** to become active; a sign-in straight after saving may fail.

If the app's policy is **Admin approved users are pre-authorized**, also assign the profiles or
permission sets whose users may use it.

## Values

| Item | Value |
|---|---|
| Local redirect (desktop installs) | `http://localhost:53683/callback` — exactly this. Salesforce accepts `http://` only when the host is literally `localhost`, which is why this redirect uses `localhost` rather than `127.0.0.1`. |
| Org redirect (organization server) | `https://<server>/oauth/callback/salesforce`, with `<server>` your server's `--server-issuer-url` |
| OAuth scopes | **Manage user data via APIs (`api`)** and **Perform requests at any time (`refresh_token`, `offline_access`)**. PrivacyFence requests `api refresh_token`. |
| Login URL | `https://login.salesforce.com` (default), `https://test.salesforce.com` for a sandbox, or your My Domain URL, e.g. `https://acme.my.salesforce.com` or, for a sandbox, `https://acme--uat.sandbox.my.salesforce.com` |
| Bundle flags | `--salesforce-consumer-key <Consumer Key>` and `--salesforce-consumer-secret <Consumer Secret>` (both required together); `--salesforce-login-url <URL>` (default `https://login.salesforce.com`) |

**Use your My Domain URL** when your org turns on **Prevent login from https://login.salesforce.com**
(or the test.salesforce.com equivalent) in its My Domain settings, or when users sign in through
single sign-on configured on My Domain. The login URL is used both for sign-in and for refreshing
tokens; give the bare origin, with no path.

## Build and distribute the bundle

`scripts/build_org_bundle.py` is in the PrivacyFence source repository and is attached to every
stable GitHub Release. It needs only Python 3; no PrivacyFence install is required.

```bash
python3 scripts/build_org_bundle.py \
  --salesforce-consumer-key 3MVG9... \
  --salesforce-consumer-secret abcdef0123456789 \
  --salesforce-login-url https://acme.my.salesforce.com \
  -o org_config.json --merge
```

`--merge` adds Salesforce to an existing `org_config.json`; drop it if this is the first service in
the bundle. A bundle holds one login URL, so users of a sandbox and of production need separate
bundles. Distribute the bundle to desktop users, or build it with the server flags and install it
on your organization server — see [org-mode-setup-guide.md](org-mode-setup-guide.md) for signing
and the server flags.

## Users connect

Users install the bundle and click **Authenticate…** next to Salesforce (desktop) or **Connect**
(organization server), sign in on Salesforce's page and click **Allow**. The shared flow is
described in [connecting-a-service.md](connecting-a-service.md).

After that, PrivacyFence refreshes the access token by itself: when Salesforce reports an expired
session, it refreshes once and retries the call.

## Troubleshooting

**`redirect_uri_mismatch`** — the Callback URL on the app must include
`http://localhost:53683/callback` for desktop installs, and
`https://<server>/oauth/callback/salesforce` on its own line for an organization server.

**Salesforce will not save an `http://` Callback URL** — only `http://localhost:…` is accepted
without HTTPS. Enter `http://localhost:53683/callback` exactly; `127.0.0.1` is rejected.

**`invalid_client_id` or `invalid client credentials`** — check that the consumer key and secret in
the bundle match the app. A brand-new app can also return this for its first few minutes.

**The login page says you cannot log in here, or points you to your My Domain** — the org blocks
login from `login.salesforce.com`. Rebuild the bundle with `--salesforce-login-url` set to your My
Domain URL.

**Sandbox users cannot sign in** — the bundle's login URL is the production one. Use
`https://test.salesforce.com` or the sandbox's My Domain URL.

**`OAUTH_APP_ACCESS_DENIED` or "user hasn't approved this consumer"** — the app admits only
pre-authorized users. Assign the user's profile or permission set to the app.

**The connector stops working, and the log shows `invalid_grant: expired access/refresh token`** —
the refresh token expired under the app's refresh token policy, or was revoked. **Reconnect…**.

**`REQUEST_LIMIT_EXCEEDED`** — your org hit Salesforce's daily API request limit. It resets over
the following 24 hours; the limit itself is set by your Salesforce edition and licences.
