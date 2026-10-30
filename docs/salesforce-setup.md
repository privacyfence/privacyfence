# Salesforce setup

PrivacyFence signs in to Salesforce with OAuth 2.0 (the web server flow with PKCE). Nobody types a
Salesforce password, security token or API key into PrivacyFence.

You need most of this page only to use your organization's own app instead of the built-in one.
The next section is for everyone.

## The built-in app: install the package once per Salesforce org

An organization server and the Docker image offer Salesforce with PrivacyFence's built-in app. It
is an External Client App that ships in a managed package, so each Salesforce org installs it once:

1. On **Admin → Services**, the Salesforce row shows an **Install link** with a **Copy link** button.
   Send it to the org's Salesforce administrator. (The row shows it only when your build carries one.)
2. The Salesforce administrator opens the link and installs the package for admins only.
3. In **Setup → External Client App Manager → PrivacyFence → Policies** they set who may connect:
   either **Permitted users: All users may self-authorize**, or **Admin approved users are
   pre-authorized**, with the permission sets of the people who may connect.

Until that is done Salesforce refuses the sign-in, and the Connect page says "Your Salesforce
administrator has to install PrivacyFence in your org first."

If your org blocks login from `login.salesforce.com` (My Domain's **Prevent login from
https://login.salesforce.com**), set the **Login URL** field on the Salesforce row of **Admin →
Services** to your My Domain URL (`https://<name>.my.salesforce.com`, or
`https://<name>--<sandbox>.sandbox.my.salesforce.com`) or to `https://test.salesforce.com` for a
sandbox. Only those Salesforce hosts over https are accepted, because the host you enter receives
the sign-in. Saving asks for a passkey and takes effect at once.

Do not mark the built-in PrivacyFence app as trusted or pre-approved: that trust would extend to
every PrivacyFence server and to anyone using the public app. To trust an app, register your own.

Desktop installs have no built-in app (desktop mode is deprecated; see
[moving to organization mode](moving-to-organization-mode.md)): they use the bundle described
below.

## Your own app

Register your own app when you want Salesforce to treat PrivacyFence as your organization's app. On
an organization server the app is a signed connector config uploaded on **Admin → Services**; on a
desktop install it is a `salesforce` section in the bundle.

## What you need

- A Salesforce administrator who can create an **External Client App**.
- Your org's login URL: `https://login.salesforce.com` (production), `https://test.salesforce.com`
  (sandbox), or your **My Domain** URL (see [Values](#values)).
- Python 3 to run `scripts/sign_connector_config.py` (organization server) or
  `scripts/build_org_bundle.py` (desktop install), or a browser for the offline signing page.

## Register the app

Salesforce no longer lets an org create new Connected Apps, so your own app is an **External Client
App** (**Setup → External Client App Manager → New External Client App**). Set **Distribution
State** to **Local**: a Local app works only in the org that created it, which is what an
organization's own app needs.

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
7. Save, then copy the **Consumer Key** and **Consumer Secret** (**Settings → OAuth Settings →
   Consumer Key and Secret**). Salesforce may ask you to verify your identity first.

A new app can take **2–10 minutes** to become active; a sign-in straight after saving may fail.

If the app's policy is **Admin approved users are pre-authorized**, also assign the profiles or
permission sets whose users may use it.

## Values

| Item | Value |
|---|---|
| Local redirect (desktop installs) | `http://localhost:53683/callback` — exactly this. Salesforce accepts `http://` only when the host is literally `localhost`, which is why this redirect uses `localhost` rather than `127.0.0.1`. |
| Org redirect (organization server) | `https://<server>/oauth/callback/salesforce`, with `<server>` your server's public address (`PRIVACYFENCE_ISSUER_URL`) |
| OAuth scopes | **Manage user data via APIs (`api`)** and **Perform requests at any time (`refresh_token`, `offline_access`)**. PrivacyFence requests `api refresh_token`. |
| Login URL | `https://login.salesforce.com` (default), `https://test.salesforce.com` for a sandbox, or your My Domain URL, e.g. `https://acme.my.salesforce.com` or, for a sandbox, `https://acme--uat.sandbox.my.salesforce.com` |
| Connector config (organization server) | `sign_connector_config.py sign --provider salesforce --client-id <Consumer Key> --client-secret-file <file holding the Consumer Secret>`, with `--login-url <URL>` if your org needs one |
| Bundle flags (desktop install) | `--salesforce-consumer-key <Consumer Key>` and `--salesforce-consumer-secret <Consumer Secret>` (both required together); `--salesforce-login-url <URL>` (default `https://login.salesforce.com`) |

**Use your My Domain URL** when your org turns on **Prevent login from https://login.salesforce.com**
(or the test.salesforce.com equivalent) in its My Domain settings, or when users sign in through
single sign-on configured on My Domain. The login URL is used both for sign-in and for refreshing
tokens; give the bare origin, with no path.

## Give PrivacyFence the app

### Organization server: a signed connector config

```bash
python3 scripts/sign_connector_config.py keygen --out connector_signing_key.pem   # once
python3 scripts/sign_connector_config.py sign --provider salesforce --serial 1 \
  --client-id 3MVG9... --client-secret-file salesforce_secret.txt \
  --login-url https://acme.my.salesforce.com \
  --key connector_signing_key.pem --output salesforce.connector.txt
```

Upload the file on **Admin → Services** (the Salesforce row, **Upload a connector config…**). It asks
for a passkey; the first upload also asks you to check the signing key's fingerprint. A connector
config's own login URL wins over the **Login URL** field. **Remove our own app** goes back to the
built-in app and, like uploading, makes everyone who connected Salesforce sign in again. The signing
tools and the serial rule are described in [org-mode-setup-guide.md](org-mode-setup-guide.md).

### Desktop install: the bundle

`scripts/build_org_bundle.py` is in the PrivacyFence source repository and is attached to every
stable GitHub Release. It needs only Python 3; no PrivacyFence install is required.

```bash
python3 scripts/build_org_bundle.py \
  --salesforce-consumer-key 3MVG9... \
  --salesforce-consumer-secret abcdef0123456789 \
  --salesforce-login-url https://acme.my.salesforce.com \
  -o org_bundle.json --merge
```

`--merge` adds Salesforce to an existing bundle; drop it if this is the first service in the
bundle. A bundle holds one login URL, so users of a sandbox and of production need separate
bundles. Install the bundle on **Admin → Organization configuration**
([connecting-a-service.md](connecting-a-service.md)).

## Users connect

Users click **Authenticate…** next to Salesforce (desktop install, after the bundle is installed) or **Connect**
(organization server), sign in on Salesforce's page and click **Allow**. The shared flow is
described in [connecting-a-service.md](connecting-a-service.md).

After that, PrivacyFence refreshes the access token by itself: when Salesforce reports an expired
session, it refreshes once and retries the call.

## Creating and updating records

The `api` scope covers writes, so connecting needs nothing more. What a user can change is decided
by their own Salesforce profile and permission sets.

Every write shows an approval card. A batch of up to 200 records of one object type is all or none,
and nothing deletes. An update card shows each field's current and new value.

An object-type rule covers creating and updating records only when it has the add and update verbs;
add them with Edit verbs on Settings → Auto-accept.

## Reading large reports

Salesforce returns at most 2,000 detail rows for one run of a report. To read more, pass `page_by`
to `salesforce_run_report`. PrivacyFence then runs the saved report once per page, sorted by that column and starting after the last value of the
previous page, and returns a `next_cursor` until the report is done. The saved report is not
changed.

- `page_by` must be a column with a different value in every row and no blanks. An Auto Number field
  added to the report is best. Record-ID columns are not guaranteed to work, because Salesforce
  compares text case-insensitively. A read that would lose rows fails with no rows instead of
  returning a partial result.
- A grouped report is read as one table, with the grouping columns first. A report grouped by
  week, month, quarter or year, and a joined report, cannot be paged.
- Each page is one report run and counts against the org's hourly report-run limits.
  `salesforce.report_max_pages` in `settings.yaml` (default 50, which is 100,000 rows) caps the runs
  one read may use; a read that needs more fails.
- Totals and other aggregates on a later page cover only the rows from that page on.

To write a whole report to a CSV or JSON file instead of reading it into the conversation, the
assistant uses `salesforce_export_report`. It pages through the report itself with `page_by`, shows
one review card, and returns only the row count, the column names and the file's SHA-256. It takes
the same `columns` and `filters` as `salesforce_run_report`, so an incremental pull needs no new
parameter. For example, these filters export only the rows changed since a date:

```json
[{"column": "LAST_UPDATE", "operator": "greaterOrEqual", "value": "2026-10-04"}]
```

and `columns` is a comma-separated list of the report's own column API names, such as
`OPPORTUNITY_ID` for the IDs only (use the name `reportMetadata.detailColumns` shows for your
report). An approved-report rule covers the export of that report, and the PII gate still
applies. `salesforce.report_max_pages` caps the runs an export may use.

`cell_format` chooses what each cell holds: `value` (the default) writes the raw value, `label` the text shown in Salesforce, and `both` writes each column twice, the raw value under the column's name and the shown text as `<column>.label`, plus `<column>.currency` for money columns. The column set is the same on every run, whatever the data.
See [ADR 0155](adr/0155-exports-write-a-file-and-return-only-counts-and-a-hash.md).

## Troubleshooting

**`redirect_uri_mismatch`** — the Callback URL on the app must include
`http://localhost:53683/callback` for desktop installs, and
`https://<server>/oauth/callback/salesforce` on its own line for an organization server.

**Salesforce will not save an `http://` Callback URL** — only `http://localhost:…` is accepted
without HTTPS. Enter `http://localhost:53683/callback` exactly; `127.0.0.1` is rejected.

**`invalid_client_id` or `invalid client credentials`** — check that the consumer key and secret in
the connector config or bundle match the app. A brand-new app can also return this for its first few minutes.

**The login page says you cannot log in here, or points you to your My Domain** — the org blocks
login from `login.salesforce.com`. Set the **Login URL** on **Admin → Services** to your My Domain
URL (or, with your own app, sign the connector config with `--login-url`, or rebuild the desktop
bundle with `--salesforce-login-url`).

**Sandbox users cannot sign in** — the login URL is the production one. Use
`https://test.salesforce.com` or the sandbox's My Domain URL.

**"Your Salesforce administrator has to install PrivacyFence in your org first."** — the built-in
app's package is not installed in that Salesforce org. Send its administrator the install link from
**Admin → Services**.

**`OAUTH_APP_ACCESS_DENIED` or "user hasn't approved this consumer"** — the app admits only
pre-authorized users. Assign the user's profile or permission set to the app (for the built-in app:
**Setup → External Client App Manager → PrivacyFence → Policies**).

**The connector stops working, and the log shows `invalid_grant: expired access/refresh token`** —
the refresh token expired under the app's refresh token policy, or was revoked. **Reconnect…**.

**`REQUEST_LIMIT_EXCEEDED`** — your org hit Salesforce's daily API request limit. It resets over
the following 24 hours; the limit itself is set by your Salesforce edition and licences.
