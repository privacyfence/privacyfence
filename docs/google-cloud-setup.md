# Google setup (Gmail, Drive, Calendar, Contacts, Tasks, Apps Script)

One administrator registers PrivacyFence with Google Cloud once per organization and puts the
resulting OAuth client into the organization config bundle. Users then sign in to each Google
connector from PrivacyFence — they never open the Google Cloud console.

## What you need

- A Google Cloud project you can administer (create one at
  [console.cloud.google.com](https://console.cloud.google.com/)).
- For Workspace accounts, ideally the right to create an **Internal** app, so no Google
  verification is needed (see [Consent screen and verification](#3-consent-screen-and-verification)).
- Python 3 to run `scripts/build_org_bundle.py`.
- Which deployment you are building for: **desktop installs**, an **organization server**, or
  both. They need different OAuth client types (step 4).

## Register the app

### 1. Select the project

In the Google Cloud console, pick your project in the project selector (or **New Project**, give it
a name such as `privacyfence`, and select it).

### 2. Enable the APIs

Open **APIs & Services → Library** and enable each API the connectors you plan to offer use:

| API | Used by |
|---|---|
| Gmail API | Gmail |
| Google Drive API | Drive, and Apps Script (to list your script projects) |
| Google Docs API | Drive's document editing tools (`drive_write_doc_content`, `drive_docs_*`) |
| Google Sheets API | Drive's spreadsheet tools (`drive_sheets_*`) |
| People API | Contacts |
| Google Calendar API | Calendar |
| Tasks API | Tasks |
| Apps Script API | Apps Script |

The Docs and Sheets APIs need no scope of their own (they accept Drive's), but each must still be
enabled here or those tools fail with `accessNotConfigured`.

### 3. Consent screen and verification

Open **Google Auth Platform** (also reachable as **APIs & Services → OAuth consent screen**):

1. **Branding:** app name (for example `PrivacyFence`), user support email and developer contact
   email.
2. **Audience:** choose the user type.
   - **Internal** (Google Workspace only): only accounts in your Workspace organization can sign
     in, and Google requires **no verification**, whatever the scopes. Use this when you can.
   - **External**: any Google account. The app starts in **Testing**: only the accounts you add
     as **test users** (up to 100) can sign in, and Google expires their sign-ins after **7 days**,
     so each user has to **Reconnect…** weekly. To lift both limits you publish the app, which
     requires Google's verification (below).
3. **Data access:** you can add the scopes from the [values table](#values) here; they are
   requested at sign-in either way, but verification reviews this list.

**Restricted scopes.** Google classifies `gmail.modify`, `drive` and `drive.metadata.readonly` as
*restricted*; the others PrivacyFence requests are *sensitive*. An External app in production that
requests restricted scopes must pass Google's verification, which includes an annual third-party
security assessment. Unverified, it shows users an "unverified app" warning and is capped in
users. An Internal app avoids all of this. If you offer only some Google connectors, you only
request those connectors' scopes: Calendar, Contacts and Tasks use no restricted scope.

### 4. Create the OAuth client

Open **Google Auth Platform → Clients** (or **APIs & Services → Credentials → + Create
Credentials → OAuth client ID**):

- **Desktop installs:** application type **Desktop app**. No redirect URI is registered: a
  Desktop app client accepts a loopback redirect on any port, which is what PrivacyFence uses.
- **Organization server:** application type **Web application**, with every org redirect URI from
  the [values table](#values) under **Authorized redirect URIs**. Use a client dedicated to the
  connectors, separate from the one your server uses for sign-in (see
  [org-mode-setup-guide.md](org-mode-setup-guide.md)).

The two types are not interchangeable: a Desktop app client cannot hold an HTTPS redirect URI, and
a Web application client cannot accept a loopback redirect on a port picked at sign-in time. If you
run both kinds of deployment, create one client of each type (in the same project is fine).

Download each client's JSON (`client_secret_….json`). Treat it as a secret.

## Values

| Item | Value |
|---|---|
| Local redirect (desktop installs) | `http://localhost:<port>/`, port picked by the operating system at each sign-in. Nothing to register — requires a **Desktop app** client. |
| Org redirects (organization server) | Register all six on the **Web application** client, with `<server>` your server's `--server-issuer-url`:<br>`https://<server>/oauth/callback/gmail`<br>`https://<server>/oauth/callback/drive`<br>`https://<server>/oauth/callback/calendar`<br>`https://<server>/oauth/callback/contacts`<br>`https://<server>/oauth/callback/tasks`<br>`https://<server>/oauth/callback/apps_script` |
| Gmail scopes | `https://www.googleapis.com/auth/gmail.modify`, `https://www.googleapis.com/auth/gmail.settings.basic` |
| Drive scope | `https://www.googleapis.com/auth/drive` |
| Calendar scope | `https://www.googleapis.com/auth/calendar` |
| Contacts scope | `https://www.googleapis.com/auth/contacts` |
| Tasks scope | `https://www.googleapis.com/auth/tasks` |
| Apps Script scopes | `https://www.googleapis.com/auth/script.projects`, `https://www.googleapis.com/auth/script.processes`, `https://www.googleapis.com/auth/drive.metadata.readonly` |
| Bundle flag | `--google-client-secret <path to the downloaded JSON>` |

Each Google connector is a separate sign-in with only its own scopes. PrivacyFence never requests
Admin SDK or Workspace-directory scopes from users; room booking uses a separately synced directory
([below](#optional-room-directory-for-calendar_list_rooms)). Apps Script's
`drive.metadata.readonly` scope reads only file names, IDs and timestamps, to list your script
projects; the connector has no tool that runs a script.

## Build and distribute the bundle

`scripts/build_org_bundle.py` is in the PrivacyFence source repository and is attached to every
stable GitHub Release. It needs only Python 3 (signing with `--sign-key` also needs
`pip install cryptography`); no PrivacyFence install is required.

```bash
python3 scripts/build_org_bundle.py \
  --org-name "Your Company" \
  --google-client-secret /path/to/client_secret.json \
  -o org_config.json
```

Add `--merge` to add Google to an existing `org_config.json` without losing its other sections;
the `google` section itself is replaced by the client you pass. A bundle holds one Google client,
so desktop installs (Desktop app client) and an organization server (Web application client) get
separate bundles.

Distribute the bundle to desktop users by whatever channel you use for internal tools; for an
organization server, build it with the server flags and install it there. Signing, the org-mode
flags and installing on the server are covered in
[org-mode-setup-guide.md](org-mode-setup-guide.md).

### Optional: room directory for `calendar_list_rooms`

Skip this if your organization does not book Workspace rooms; every other Calendar tool works
without it.

`calendar_list_rooms` reads a room list (name, email, building, floor, capacity) stored in the
bundle's `rooms` section, not Google live. `scripts/sync_room_directory.py` (from the same
repository and release) fills it. It needs the Workspace-admin scope
`admin.directory.resource.calendar.readonly`, so it uses a **second** Google Cloud project,
keeping that scope off the client every user signs in with:

1. Create a second project (for example `privacyfence-room-sync`) and enable only the **Admin SDK
   API**.
2. Configure its consent screen as in step 3; only the administrators who run the sync need access.
3. Create a **Desktop app** OAuth client and download its JSON. Never put it in `org_config.json`.
4. Run the sync signed in as an account with the Workspace **Directory Reader** role (or super
   admin):

   ```bash
   pip install google-auth google-auth-oauthlib google-api-python-client
   python3 scripts/sync_room_directory.py \
     --admin-client-secret /path/to/room_sync_client_secret.json \
     --org-config org_config.json
   ```

   It merges the `rooms` section into the bundle and leaves the rest untouched. `--query` filters
   which rooms are fetched; `--token-file` (default `.room_sync_token.json`) caches the sync's own
   sign-in between runs — keep it private.
5. If the bundle is signed, pass `--sign-key <same signing key>` too (needs `pip install
   cryptography`). Without it the script refuses to modify a signed bundle, since the merge would
   invalidate the signature.
6. Redistribute the bundle. Re-run the sync whenever your rooms change.

## Users connect

Users install the bundle and click **Authenticate…** (desktop) or **Connect** (organization
server) for each Google connector they want — the shared flow is described in
[connecting-a-service.md](connecting-a-service.md). Google-specific points:

- Each Google connector is its own sign-in; connecting Gmail does not connect Drive.
- Leave every permission checkbox on Google's consent page ticked. Unticking one fails the sign-in
  (see below).
- **Apps Script** also needs a per-user switch in Google: turn on **Google Apps Script API** at
  [script.google.com/home/usersettings](https://script.google.com/home/usersettings). It is off by
  default and nothing in the Cloud console turns it on; include it in your user instructions.

## Troubleshooting

**"Access blocked: … has not completed the Google verification process"** — the app is External
and in Testing, and the account is not a test user. Add it under **Audience → Test users**, or use
an Internal app.

**"This app isn't verified"** — expected for an unverified External app. Test users can continue
through **Advanced → Go to PrivacyFence**. See [verification](#3-consent-screen-and-verification).

**"Error 403: org_internal"** — the app is Internal and the account is outside your Workspace
organization.

**Sign-ins stop working after a week** — an External app in Testing: Google expires test-user
sign-ins after 7 days. Users can **Reconnect…**; to stop it, use an Internal app or publish and
verify the External one.

**"Error 400: redirect_uri_mismatch"** — the client type does not match the deployment. Desktop
installs need a **Desktop app** client; an organization server needs a **Web application** client
with all six `https://<server>/oauth/callback/<service>` URIs registered, spelled exactly as
PrivacyFence builds them from `--server-issuer-url` (the Apps Script one is `apps_script`, with
an underscore). Check which client the installed bundle actually contains.

**"authentication failed: Google OAuth exchange failed: the granted scopes are missing [...]"** —
a permission was unticked on Google's consent page. **Reconnect…** and leave every box ticked.

**`accessNotConfigured` / "API has not been used in project … or it is disabled"** — the API
behind that tool is not enabled in the project ([step 2](#2-enable-the-apis)). Enabling it takes
effect within a few minutes; users do not need to reconnect.

**Apps Script calls fail with 403 "User has not enabled the Apps Script API"** — the user has not
turned on the per-user switch at
[script.google.com/home/usersettings](https://script.google.com/home/usersettings).

**Scopes changed on the client, or a 403 on a tool that worked before** — **Reconnect…** the
connector so Google issues a token with the current scopes.

**`calendar_list_rooms` returns nothing** — the bundle has no `rooms` section yet. Run the
[room directory sync](#optional-room-directory-for-calendar_list_rooms) and redistribute the
bundle.

**`sync_room_directory.py` fails with "Room directory listing requires Google Workspace admin
access"** — the account you signed in with lacks the **Directory Reader** role. Run it again as an
account that has it.
