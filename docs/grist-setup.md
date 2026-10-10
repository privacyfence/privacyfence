# Grist setup

PrivacyFence connects to [Grist](https://www.getgrist.com/) at `docs.getgrist.com`, on a team
site, or on a self-hosted server. The assistant can list your documents and tables straight away,
read records after you review them, and add or change records, tables and columns only with your
approval. Nothing is deleted: there is no record, column or table delete, no rename and no type
change.

## Two ways to connect

- **OAuth**, when your organization registered a PrivacyFence app in Grist and put it in the
  organization config bundle. OAuth apps are available in Grist's paid editions. You click
  **Authenticate…** (desktop) or **Connect** (organization server) and sign in on Grist's own
  page.
- **A personal API key** otherwise, on any Grist edition. When the bundle holds an OAuth app,
  API keys are not accepted: the organization chose OAuth.

## Connect with an API key

1. In Grist, open **Account settings → Developer → API Key** and create the key, then copy it.
2. On a desktop install, open PrivacyFence's Settings page, **Connectors → Grist →
   Authenticate…**, enter the server address (`https://docs.getgrist.com` for the hosted
   service, or your own server's address, without `/api`) and the key. When the organization
   bundle names the server, only the key is asked for.
3. On an organization server, the connections page asks only for the key; the server comes from
   the bundle.

The key acts as you: it has your access to every document you can open, and Grist offers no
scopes for it. PrivacyFence sends it only to the server it was entered for, stores it in a file
only your account can read, and never logs it.

## Register an OAuth app

For administrators, in Grist's paid editions.

1. In Grist, open **Account settings** → **Developer** → **OAuth apps** → **Register app**.
2. Enter a name such as `PrivacyFence`.
3. Add the redirect URIs, each on its own line: `http://localhost:53685/callback` for desktop
   installs and, for an organization server, `https://<server>/oauth/callback/grist`, with
   `<server>` your server's `--server-issuer-url`. One app can carry both.
4. Allow the permissions `doc:read`, `doc:write`, `doc.schema:write` and `offline_access`.
5. Save, then copy the **client id** and **client secret**.

Grist notes that `doc.schema:write` can reveal any data in a document through formulas.
PrivacyFence requests it only to add tables and columns, and requests nothing beyond the four
permissions above.

## Values

| Item | Value | `build_org_bundle.py` option |
|---|---|---|
| Server address | `https://docs.getgrist.com`, a team site or your self-hosted server; no path ending in `/api` | `--grist-server-url` |
| Client id | From the registered app | `--grist-client-id` |
| Client secret | From the registered app | `--grist-client-secret` |
| Sign-in server | Left unset for getgrist.com and for any server that serves `/.well-known/oauth-authorization-server` itself | `--grist-auth-server-url` |

The client id and secret go together, and the sign-in server needs them.

## Build and distribute the bundle

`scripts/build_org_bundle.py` is in the PrivacyFence source repository and is attached to every
stable GitHub Release. It needs only Python 3.

```bash
python3 scripts/build_org_bundle.py \
  --grist-server-url https://docs.getgrist.com \
  --grist-client-id abc123 \
  --grist-client-secret abcdef0123456789 \
  -o org_config.json --merge
```

`--merge` adds Grist to an existing `org_config.json`. A bundle with only `--grist-server-url`
pins the server for API keys; organization mode needs at least that. See
[org-mode-setup-guide.md](org-mode-setup-guide.md) for signing and the server flags, and
[connecting-a-service.md](connecting-a-service.md) for the shared connect flow.

## What the assistant can do

Every Grist tool has a gate fixed in code. The [tools reference](tools-reference.md#grist) lists
each one.

| Tool | Gate | What it does |
|---|---|---|
| `grist_list_documents` | Without a card | Lists the documents you can open, with workspace and team |
| `grist_list_tables` | Without a card | Lists a document's tables and each table's columns |
| `grist_get_records` | Reviewed before release | Reads records from a table, optionally filtered and sorted, up to 500 per call; a whole table is read page by page |
| `grist_add_records` | Needs your approval | Adds records to a table |
| `grist_update_records` | Needs your approval | Changes cells of existing records; the card shows old and new values |
| `grist_create_table` | Needs your approval | Creates a table with its columns |
| `grist_add_columns` | Needs your approval | Adds columns to an existing table |

Each card names the Grist server the data comes from or goes to.

A table larger than one call is read in pages, in record-id order: each page continues after the
last record id of the one before, so rows added in between are neither repeated nor skipped. Every
page is its own review card, and its **Page** line shows where the page starts, which record ids it
holds and whether more follow. The card does not show the table's total row count.
[ADR 0146](adr/0146-grist-records-page-by-record-id.md) records why.

Under OAuth, some servers do not let apps list documents. The assistant then asks you for the
Document ID, shown in Grist under the document's Settings (the gear icon). One id form is used
everywhere: the full Document ID, not the shorter id in the document's address. An auto-accept
rule names that full Document ID.

## Auto-accept rules

A rule can name a document by its `grist.document` scope, set on **Settings > Auto-accept**. The
approval card never offers "Always allow" for a Grist operation. Every scope is in
[the scope catalogue](approvals-and-policy.md#scope-catalogue).

## Troubleshooting

**`Grist is not authenticated. Use Authenticate… in PrivacyFence Settings.`** — no key or sign-in
is saved yet. Use **Authenticate…** on the Grist row.

**`Grist's saved sign-in could not be read.`** — the saved credential file is damaged. Connect
again.

**`Your organization connects to Grist with OAuth.`** — the bundle holds an OAuth app, so an API
key is not accepted. Use **Authenticate…** (desktop) or **Connect** (organization server).

**`Grist was connected to a different server than your organization uses.`** — the saved
credential belongs to another server than the bundle names. Connect again.

**`Enter the Grist server address, such as https://docs.getgrist.com.`**, **`The Grist server
address must start with https://`**, **`…must not contain a user name, password, query or
fragment.`**, **`Enter the server address without /api.`** — correct the server address.

**`Grist refused the sign-in (HTTP 401).`** — the key or sign-in is wrong, expired or revoked.
Connect again.

**`Grist refused the request (HTTP 403).`** — your Grist account, or what you allowed
PrivacyFence when signing in, does not cover the request.

**`Grist found no such document, table or record (HTTP 404).`** — check the Document ID and the
table id.

**`This Grist server does not let PrivacyFence list your documents.`** — open the document in
Grist, then Settings (the gear icon) → Document ID, and give the assistant that id.

**`Grist sign-in failed: <error>`** — Grist refused the sign-in; the text after the colon is Grist's
own reason. Check the client id, the client secret and the redirect URI registered for the app.

**`Grist did not return a refresh token.`** — the app in Grist must allow `offline_access`.

**`Your Grist sign-in has expired or was revoked.`** — use **Authenticate…** to sign in again.

**`Could not reach the Grist server at <host>`** — check the server address and your network.

**`The Grist server answered with a redirect (HTTP 30x).`** — the server address is not the
final one; enter the address Grist ends up at.

**`Grist's sign-in server answered with a redirect (HTTP 30x).`** — the sign-in server address in
the organization config is not the final one; enter the address it ends up at.

**`Could not reach Grist's sign-in server at <host>`** — check the sign-in server address and your
network.

**`Grist's sign-in settings at <host> are not usable.`** — check the Grist server address, and
the sign-in server if set, in the organization config.
