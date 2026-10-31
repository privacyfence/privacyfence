# Grist setup

PrivacyFence connects to [Grist](https://www.getgrist.com/) at `docs.getgrist.com`, on a team
site, or on a self-hosted server. The assistant can list your documents and tables straight away,
read records after you review them, and add or change records, tables and columns only with your
approval. Nothing is deleted: there is no record, column or table delete, no rename and no type
change.

## Two ways to connect

- **OAuth**, when your organization registered a PrivacyFence app in Grist and gave it to
  PrivacyFence (a signed connector config on an organization server, a bundle section on a desktop
  install). OAuth apps are available in Grist's paid editions. You click
  **Authenticate…** (desktop) or **Connect** (organization server) and sign in on Grist's own
  page.
- **A personal API key** otherwise, on any Grist edition. When an OAuth app is set up,
  API keys are not accepted: the organization chose OAuth.

## Connect with an API key

1. In Grist, open **Account settings → Developer → API Key** and create the key, then copy it.
2. On a desktop install, open PrivacyFence's Settings page, **Connectors → Grist →
   Authenticate…**, enter the server address (`https://docs.getgrist.com` for the hosted
   service, or your own server's address, without `/api`) and the key. When the server's
   address is set on the Admin page, only the key is asked for.
3. On an organization server, the connections page asks only for the key; the server comes from
   the Admin page (**Features**) or from `PRIVACYFENCE_GRIST_SERVER_URL`.

The key acts as you: it has your access to every document you can open, and Grist offers no
scopes for it. PrivacyFence sends it only to the server it was entered for, stores it in a file
only your account can read, and never logs it.

## Automatic API keys

An organization that runs its own Grist (grist-core, which has no OAuth apps) can have
PrivacyFence connect each person's Grist account at sign-in, with no clicks. When a person signs in
to PrivacyFence with a verified email and already has a Grist account, PrivacyFence registers a
Grist API key for that account. This needs the **Grist provisioner**, a small service you run next
to Grist, in Grist's network zone
([`privacyfence/grist-provisioner`](https://github.com/privacyfence/grist-provisioner), see its
README). The provisioner sets or reads the person's key directly in Grist's database. PrivacyFence
calls the provisioner with a scoped provisioning key; the provisioner never connects to
PrivacyFence.

**Set the addresses and the key on the server**, never in the bundle or the Admin page:

- `PRIVACYFENCE_GRIST_SERVER_URL`: the Grist server's address.
- `PRIVACYFENCE_GRIST_PROVISIONER_URL`: the provisioner's address.
- `PRIVACYFENCE_GRIST_PROVISIONING_KEY`, or, better, `PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE`
  naming a file that holds the key (a Docker secret). Setting both, or a file that cannot be read,
  refuses the start.

On a systemd install put them in the unit's `EnvironmentFile` (mode `0600`, owned by root). With
Docker, put them in `privacyfence.env` next to `compose.yaml`, and the key in a Docker secret.
Restart the server.

**Turn it on** under **Admin → Services**: the Grist row has an **Automatic accounts** switch. It
works only without a Grist OAuth app, and the row says when it is unavailable, or when the key is
not set (naming the variable the server reads). Or set `PRIVACYFENCE_GRIST_AUTO_PROVISION=true` in
the server's environment, which does the same: it offers Grist and turns automatic accounts on. The
row then says it is set by the server, and the **Automatic accounts** switch and **Offer** are
locked. Removing the variable hands both back to the Admin page, which kept its own settings. The
variable needs both addresses and the key, or the server refuses to start, and it does nothing unless
the web server runs (`web.mcp.enabled`). Outside organization mode it is ignored, with a log warning.
While it is set, the server refuses an uploaded Grist connector config, because automatic accounts
work only without a Grist OAuth app. A server upgraded from 6.11 keeps its automatic
accounts: its old addresses stay and the Admin page shows them as fixed, and a key variable name
it chose keeps working. The 6.11 bundle flags `--grist-auto-provision`, `--grist-provisioner-url`
and the others are refused by `build_org_bundle.py`.

**On a single-user install.** With `PRIVACYFENCE_GRIST_AUTO_PROVISION=true`, a single-user install
(no identity provider) connects Grist for its one person, `admin@localhost`, treated as verified
because the install has exactly one person, reachable only with the setup code or a passkey. It
works only when Grist and the provisioner run on the same computer: both
`PRIVACYFENCE_GRIST_SERVER_URL` and `PRIVACYFENCE_GRIST_PROVISIONER_URL` must be `localhost`
addresses (`localhost`, `127.0.0.1` or `::1`). Grist is connected at each start and at each sign-in.
The Grist install admin needs another address than `admin@localhost`, because the provisioner
refuses to adopt an admin. Every start counts as a sign-in, so the renewal rule below never ends the
connection. When you move to an identity provider, run `privacyfence-app --grist-release
single-user`. ([ADR 0272](adr/0272-a-single-user-install-provisions-its-one-person-as-verified.md))

**Verified email.** Only a person whose ID token says `email_verified` is `true` is provisioned.
Google always sends it. The **Trust email addresses the identity provider does not mark verified**
checkbox on the Grist row is for identity providers that never send the claim but issue only
verified, organization-owned addresses; it is safe only then, because otherwise anyone who can set
their own email at the provider could get another person's Grist key.

**What a person gets.**

- A key pasted on the connections page always wins; provisioning leaves it alone.
- Grist allows one API key per person. With no key in Grist, the provisioner creates a *managed*
  key. If the person already made a key, it is *adopted*: PrivacyFence uses it and never deletes it,
  because the person's own scripts use it. Regenerating or removing the key in Grist disconnects
  PrivacyFence until the next sweep (within six hours) or sign-in.
- The Grist install admin is never provisioned and connects by hand.
- A person with no Grist account is provisioned once Grist has one: after their first sign-in to
  Grist, or when someone shares a document with their address.
- A managed key expires 90 days after it was last issued and is renewed 30 days ahead while the
  person keeps signing in. So a managed key lives at most about 180 days after the last sign-in; use
  `--grist-release` for anything sooner.

**Release a person.** `privacyfence-app --grist-release PRINCIPAL_ID` (in Docker: `docker compose
exec privacyfence privacyfence-app --grist-release PRINCIPAL_ID`) makes the provisioner delete the key in
Grist if it created it, and PrivacyFence forgets it. It needs the provisioning key in the
environment (or its `_FILE` form). A person who signs in again is provisioned again, so remove them from the identity
provider first. A running daemon keeps a person's Grist connector in memory until it is rebuilt, so
restart the daemon after `--grist-release` when the key was adopted (a deleted managed key stops
working in Grist at once). Running `--grist-release` again for the same person is safe, for example
when the daemon log says a key issued during a release could not be undone.

**When one is down.** PrivacyFence sign-in never waits on Grist or the provisioner; an outage only
delays provisioning, which a background sweep retries every six hours. Grist keeps its own users,
sign-in and keys, so Grist works the same without PrivacyFence.

## Register an OAuth app

For administrators, in Grist's paid editions.

1. In Grist, open **Account settings** → **Developer** → **OAuth apps** → **Register app**.
2. Enter a name such as `PrivacyFence`.
3. Add the redirect URIs, each on its own line: `http://localhost:53685/callback` for desktop
   installs and, for an organization server, `https://<server>/oauth/callback/grist`, with
   `<server>` your server's public address (`PRIVACYFENCE_ISSUER_URL`). One app can carry both.
4. Allow the permissions `doc:read`, `doc:write`, `doc.schema:write` and `offline_access`.
5. Save, then copy the **client id** and **client secret**.

Grist notes that `doc.schema:write` can reveal any data in a document through formulas.
PrivacyFence requests it only to add tables and columns, and requests nothing beyond the four
permissions above.

## Values

| Item | Value | `build_org_bundle.py` option (desktop) |
|---|---|---|
| Server address | `https://docs.getgrist.com`, a team site or your self-hosted server; no path ending in `/api` | `--grist-server-url` (with the OAuth app) |
| Client id | From the registered app | `--grist-client-id` |
| Client secret | From the registered app | `--grist-client-secret` |
| Sign-in server | Left unset for getgrist.com and for any server that serves `/.well-known/oauth-authorization-server` itself | `--grist-auth-server-url` |

The client id and secret go together, and the sign-in server needs them.

## Give PrivacyFence the OAuth app

Grist's OAuth app still goes in a signed file or the bundle; only the server address moved to the
Admin page.

On an organization server, sign the app as a connector config and upload it on **Admin → Services**
(the Grist row, **Upload a connector config…**). The file carries the server address and, if needed,
the sign-in server:

```bash
python3 scripts/sign_connector_config.py keygen --out connector_signing_key.pem   # once
python3 scripts/sign_connector_config.py sign --provider grist --serial 1 \
  --client-id abc123 --client-secret-file grist_secret.txt \
  --server-url https://docs.getgrist.com \
  --key connector_signing_key.pem --output grist.connector.txt
```

A connector config's address is shown on the row as coming from your organization's Grist app; the
Admin page owns the address only when no connector config locks it. Uploading and **Remove our own
app** ask for a passkey and make everyone who connected Grist sign in again. See
[org-mode-setup-guide.md](org-mode-setup-guide.md).

On a desktop install, put the app in the bundle:

```bash
python3 scripts/build_org_bundle.py \
  --grist-server-url https://docs.getgrist.com \
  --grist-client-id abc123 \
  --grist-client-secret abcdef0123456789 \
  -o org_bundle.json --merge
```

`--grist-server-url` is accepted with the client id and secret (the server the OAuth app belongs
to). A server address without an OAuth app is set on **Admin → Features** or with
`PRIVACYFENCE_GRIST_SERVER_URL`; without an address Grist is not listed at all
([ADR 0196](adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md)). See
[connecting-a-service.md](connecting-a-service.md) for the shared connect flow.

## What the assistant can do

Every Grist tool has a gate fixed in code. The [tools reference](tools-reference.md#grist) lists
each one.

| Tool | Gate | What it does |
|---|---|---|
| `grist_list_documents` | Without a card | Lists the documents you can open, with workspace and team |
| `grist_list_tables` | Without a card | Lists a document's tables and each table's columns |
| `grist_get_records` | Reviewed before release | Reads records from a table, optionally filtered and sorted, up to 500 per call; a whole table is read page by page |
| `grist_export_records` | Reviewed before release | Writes a whole table, optionally filtered, to a CSV or JSON file on your computer, in one approval |
| `grist_export_sql` | Reviewed before release | Writes the result of one read-only SQL query over the document to a CSV or JSON file, in one approval |
| `grist_add_records` | Needs your approval | Adds records to a table |
| `grist_update_records` | Needs your approval | Changes cells of existing records; the card shows old and new values |
| `grist_create_table` | Needs your approval | Creates a table with its columns |
| `grist_add_columns` | Needs your approval | Adds columns to an existing table |
| `grist_import_csv` | Needs your approval | Adds every row of a CSV file as a new record, in one approval |
| `grist_update_csv` | Needs your approval | Updates records matched to the rows of a CSV file by one key column, and can add the rows that match nothing |

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

## Bulk import and update from a CSV file

`grist_add_records` and `grist_update_records` take at most 100 records inline. To load or refresh
a larger table, the assistant hands PrivacyFence a CSV file instead, and you approve it once.

- **Handing over the file.** The assistant gives either `local_path`, a path on your computer, or
  `upload_id`, the id `privacyfence_create_upload_slot` returned after the file's bytes were sent
  to its upload URL. On an organization install only `upload_id` is available.
- **Header.** The first row names the columns, by column id or by label as `grist_list_tables`
  shows them. The delimiter is a comma, a semicolon or a tab. Formula columns and columns of
  other types than Text, Numeric, Int, Bool, Date, Choice and Any cannot be written.
- **Cells.** An empty cell is left out: an import leaves the column at Grist's default, and an
  update does not change the cell, unless `clear_empty` is true on `grist_update_csv`: then an
  empty cell clears the value (the card counts them as "Cells to clear"). Columns that are not in
  the CSV are never touched, and a row added by `create_missing` gets no empty values. Dates are written as
  `YYYY-MM-DD`, and numbers with a point as the decimal mark.
- **Update.** `grist_update_csv` matches each row to the record whose key column has the same
  value. The key column must be a Text, Choice, Int or Numeric column, in the header, unique in
  the file and unique in the table. It can also be `id`, the Grist record id, for a table that has
  no other unique column; `create_missing` is not available then. Only cells that differ change.
  Rows that match no record are skipped, or added when the assistant sets `create_missing`.
- **Limits.** At most 20000 rows, 100 columns and 50 MB. A file over a limit, or with a cell that
  does not fit its column, is refused before the card is shown, with the line and column of the
  first problems.
- **The card.** It names the server, the document and the table, and shows the counts, the first
  20 rows (import) or the first 20 changed cells and unmatched rows (update), and the file's
  SHA-256. The approval is bound to that SHA-256: a different file needs a new approval.
- **Writes.** The rows go to Grist in chunks, which are not atomic. If Grist refuses a chunk, the
  assistant is told how many rows were written. After a failed import the rows written so far stay
  in the table; running `grist_update_csv` again with the same file finishes a failed update.

[ADR 0148](adr/0148-grist-bulk-writes-take-a-csv-file-and-are-reviewed-as-counts-and-a-sample.md)
records why.

## Exporting to a file

`grist_export_records` writes a whole table, and `grist_export_sql` the result of one read-only
SQL query, to a CSV or JSON file on your computer. You approve it once, and the assistant gets back
only the row count, the column names and the file's SHA-256, not the rows.

- **Limit.** At most 50,000 rows. A larger table or result is refused; narrow it with a filter or
  the query.
- **Organization installs.** Refused: the file would land on the server, not on your computer.
- **Rules.** A `grist.document` read rule for the document covers both tools, so a scheduled run
  can export without a card. The PII gate still applies: an export whose content trips the scan
  gets a card, and an unattended session is denied.
- **SQL.** `grist_export_sql` needs full copy access to the document, because that is what Grist
  requires for SQL, and it cannot read the `_grist` tables. Cells are written as Grist stores them:
  dates as seconds since 1970, true/false as 1/0.
- **Round trip.** To write an export back with `grist_update_csv`, keep only `id` and the data
  columns you change; `id` is the key column.

[ADR 0155](adr/0155-exports-write-a-file-and-return-only-counts-and-a-hash.md) and
[ADR 0156](adr/0156-grist-export-sql-reads-through-the-sql-endpoint.md) record why.

## Auto-accept rules

A rule can name a document by its `grist.document` scope, set on **Settings > Auto-accept**. The
approval card never offers "Always allow" for a Grist operation. Every scope is in
[the scope catalogue](approvals-and-policy.md#scope-catalogue).

The rule's verbs cover these tools:

| Verb | Tools |
|---|---|
| read | `grist_get_records`, `grist_export_records`, `grist_export_sql` |
| add | `grist_add_records`, `grist_import_csv` |
| update | `grist_update_records`, `grist_update_csv` |
| restructure | `grist_create_table`, `grist_add_columns` |

A CSV import or update rule lets the assistant load a file into that document without a card, so
grant `add` and `update` only for a document you are happy to have bulk-written.

## Troubleshooting

**`Grist is not authenticated. Use Authenticate… in PrivacyFence Settings.`** — no key or sign-in
is saved yet. Use **Authenticate…** on the Grist row.

**`Grist's saved sign-in could not be read.`** — the saved credential file is damaged. Connect
again.

**`Your organization connects to Grist with OAuth.`** — an OAuth app is set up, so an API
key is not accepted. Use **Authenticate…** (desktop) or **Connect** (organization server).

**`Grist was connected to a different server than your organization uses.`** — the saved
credential belongs to another server than the one set for the install. Connect again.

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

**`Grist auto-provisioning is on, but <NAME> is not set; people connect Grist by hand on /connect.`**
(daemon log, at startup) — the provisioning key is missing from the daemon's environment. Set it
and restart.

**`Grist provisioning ready at <host> (<n> accounts managed)`** — the startup check passed.

**`Grist provisioning check failed: <error>`** — the startup check could not use the provisioner;
the error text says why (see the next entries). Provisioning retries at each sweep.

**`Could not reach the Grist provisioner at <host>: <error type>`** — check
`--grist-provisioner-url`, the network and that the provisioner is running.

**`The Grist provisioner answered with a redirect (HTTP 30x).`**, **`This server is not a Grist
provisioner (HTTP 404).`** — `--grist-provisioner-url` is not the provisioner's address.

**`The Grist provisioner refused the provisioning key (HTTP 401).`** — the key in the daemon's
environment is wrong or revoked. Create a new one in the provisioner and restart.

**`Grist has no account for this person yet (HTTP 404).`** — the person has not signed in to Grist
and no document is shared with them. Provisioning retries at each sweep and at their next sign-in.

**`The Grist provisioner has no link for this person (HTTP 404).`** — sign in again; PrivacyFence
links the account first.

**`The Grist provisioner refused this account (HTTP 403): <detail>`** — the account's email domain
is not allowed, or the Grist account is disabled. Fix it in the provisioner or in Grist.

**`The Grist provisioner refused this account (HTTP 409): <detail>`** — the account is the Grist
install admin (connect it by hand), or is linked to another PrivacyFence id or email.

**`The Grist provisioner answered with an error (HTTP 5xx).`**, **`The Grist provisioner answered
with something unexpected.`**, **`The Grist provisioner refused the request (HTTP 4xx): <detail>`**
— see the provisioner's log.

**`Grist provisioning queue is full; <id> waits for the next sweep or sign-in`** — many people
signed in at once; the sweep picks the rest up.

**`Grist provisioning for <id> failed: <error>`** — one person failed; the text after the colon is
one of the messages above. The sweep retries.

**`PRIVACYFENCE_GRIST_AUTO_PROVISION must be true, or unset.`** — the value is anything but `true`.
Remove the variable to hand the switch back to the Admin page.

**`PRIVACYFENCE_GRIST_AUTO_PROVISION=true needs PRIVACYFENCE_GRIST_SERVER_URL and
PRIVACYFENCE_GRIST_PROVISIONER_URL.`** and **`PRIVACYFENCE_GRIST_AUTO_PROVISION=true needs
PRIVACYFENCE_GRIST_PROVISIONING_KEY or PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE.`** — the daemon
refuses to start without the addresses or the key (with a custom key variable, the message names it
and its `_FILE` form). Set them and restart.

**`PRIVACYFENCE_GRIST_AUTO_PROVISION=true cannot be used with a Grist OAuth app.`** — automatic
accounts work only with API keys. Remove the variable or the Grist connector config.

**`On a single-user install, PRIVACYFENCE_GRIST_AUTO_PROVISION=true needs localhost addresses in
PRIVACYFENCE_GRIST_SERVER_URL and PRIVACYFENCE_GRIST_PROVISIONER_URL.`** — a single-user install
connects only a Grist on the same computer.

**`PRIVACYFENCE_GRIST_AUTO_PROVISION is ignored outside organization mode.`** — a log warning: the
server is in local mode, where the variable does nothing.

**`Grist's automatic accounts are set by PRIVACYFENCE_GRIST_AUTO_PROVISION on the server, which works
only without a Grist OAuth app. Remove the variable first.`** — a Grist connector config upload was
refused. **`Grist is set by PRIVACYFENCE_GRIST_AUTO_PROVISION on the server.`** — an Admin change tried
to offer or withdraw Grist while the variable is set.

**`No such account.`**, **`Grist auto-provisioning is not enabled in the organization config.`**,
**`Set <NAME> to the Grist provisioning key first.`** — `--grist-release` messages: check the id,
the Admin page's **Automatic accounts** switch (or `PRIVACYFENCE_GRIST_AUTO_PROVISION`) and the environment.

**`… PrivacyFence has already forgotten the key; run --grist-release again to delete it in
Grist.`** — the provisioner could not be reached for the release. Run the command again.
