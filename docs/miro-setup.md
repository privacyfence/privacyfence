# Miro setup

PrivacyFence connects to [Miro](https://miro.com/).

You need this page only to use your organization's own app instead of the built-in one. An
organization server and the Docker image offer Miro with PrivacyFence's built-in app, and nothing
needs registering. Because Miro sends no PKCE, a sign-in code for the built-in app passes through
`oauth.privacyfence.eu` without it; your own app avoids this.

Do not mark the built-in PrivacyFence app as trusted or pre-approved: that trust would extend to
every PrivacyFence server and to anyone using the public app. To trust an app, register your own.

Your own app is a signed connector config uploaded on **Admin → Services** on an organization
server, and a `miro` section in the bundle on a desktop install (desktop installs have no built-in
app; see [moving to organization mode](moving-to-organization-mode.md)).

With your own app, the assistant can list your boards and tags straight away, read the items and
connector lines of a board after you review them, and add or change sticky notes, cards, texts,
shapes, frames and connector lines only with your approval. Nothing is deleted: there is no delete
of a board, item, frame, connector line or tag, and no sharing or member change.

## Register the PrivacyFence app

For administrators. Miro takes no `scope` parameter at sign-in, so the app's permissions are set
on the app itself.

1. If you have no Developer team in Miro, create one on Miro's "Create a Developer team" page.
2. In the Developer team, choose **Create new app** and name it `PrivacyFence`. Turn on **Expire
   user authorization token**. This can only be chosen when the app is created and cannot be
   changed afterwards: an app without it gives tokens that never expire, and PrivacyFence refuses
   them at sign-in.
3. Under **Permissions**, allow `boards:read` and `boards:write` and nothing else. A sign-in whose
   token lacks either one is refused.
4. Under **Redirect URI for OAuth2.0**, add `http://localhost:53686/callback` for desktop installs
   and, for an organization server, `https://<server>/oauth/callback/miro`, with `<server>` your
   server's public address (`PRIVACYFENCE_ISSUER_URL`). One app can carry both.
5. Copy the **Client ID** and the **Client secret**.
6. Under **Share app**, copy the **Installation URL** for people outside the Developer team. It is
   optional: signing in from PrivacyFence also works without it.

A token belongs to one person and one Miro team, the team chosen on Miro's install screen, and
cannot reach the boards of another team.

## Values

| Item | Value | `build_org_bundle.py` option (desktop) |
|---|---|---|
| Client id | From the registered app | `--miro-client-id` |
| Client secret | From the registered app | `--miro-client-secret` |

Give the two together: with only one, `build_org_bundle.py` stops with `--miro-client-id and
--miro-client-secret must be given together.`

## Give PrivacyFence the app

On an organization server, sign the client ID and secret as a connector config and upload it on
**Admin → Services** (the Miro row, **Upload a connector config…**):

```bash
python3 scripts/sign_connector_config.py keygen --out connector_signing_key.pem   # once
python3 scripts/sign_connector_config.py sign --provider miro --serial 1 \
  --client-id 3458764500000000000 --client-secret-file miro_secret.txt \
  --key connector_signing_key.pem --output miro.connector.txt
```

Uploading asks for a passkey (the first upload also asks you to check the signing key's
fingerprint). **Remove our own app** goes back to the built-in app. Either change makes everyone who
connected Miro sign in again. See [org-mode-setup-guide.md](org-mode-setup-guide.md) for the signing
tools and the serial rule.

On a desktop install, put them in the bundle:

```bash
python3 scripts/build_org_bundle.py \
  --miro-client-id 3458764500000000000 \
  --miro-client-secret abcdef0123456789 \
  -o org_bundle.json --merge
```

`--merge` adds Miro to an existing bundle. Install it on **Admin → Organization configuration**.
Without an app, Miro is not listed on a desktop install
([ADR 0196](adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md)); see
[connecting-a-service.md](connecting-a-service.md) for the shared connect flow.

## Connect

- **Desktop:** open **Settings → Connectors → Miro → Authenticate…**. Your browser opens Miro's
  page on port 53686 of your own computer.
- **Organization server:** open the connections page and choose **Connect** on the Miro row.
- **From a terminal:** `privacyfence-app --miro-oauth` signs in the same way.

Miro's page asks which team to install the app in. A connection covers that one team: connecting
again with another team replaces the first. The sign-in is kept in a file only your account can
read. PrivacyFence renews it before it runs out, and Miro's renewal replaces the old one each time.

## What the assistant can do

Every Miro tool has a gate fixed in code. The [tools reference](tools-reference.md#miro) lists
each one.

| Tool | Gate | What it does |
|---|---|---|
| `miro_list_boards` | Without a card | Lists the boards you can open in the connected team, optionally only those matching a text |
| `miro_get_board` | Without a card | Gets one board's name, team, dates and link |
| `miro_list_tags` | Without a card | Lists the tags defined on a board |
| `miro_list_items` | Reviewed before release | Reads the items on a board, optionally one type or only the items inside one frame |
| `miro_get_items` | Reviewed before release | Reads specific items by their ids |
| `miro_list_connectors` | Reviewed before release | Reads the connector lines and the ids of the items each one joins |
| `miro_create_items` | Needs your approval | Adds sticky notes, cards, texts, shapes or frames |
| `miro_create_connectors` | Needs your approval | Draws connector lines between existing items |
| `miro_update_items` | Needs your approval | Changes text, color, position, size or the frame of existing items; the card shows old and new values |
| `miro_update_board` | Needs your approval | Renames a board or changes its description |

- **Batches.** The three tools that add or change items take up to 20 in one request, under one
  approval card. Creating sticky notes, cards, texts and shapes is all or none; frames are created
  first, one request each; changes and connector lines go one at a time and stop at the first
  error, saying what was already done
  ([ADR 0233](adr/0233-a-miro-write-covers-a-batch-of-up-to-twenty-under-one-card.md)).
- **Frames.** Inside a frame, positions are measured from the frame's top-left corner. Moving an
  item into or out of a frame is checked by reading it back, because Miro answers success to a
  move it ignored; if the item is not where it was asked to go, the assistant is told so.
- **Content** may use simple HTML such as `<p>` and `<strong>`.
- **Comments** cannot be read: Miro's public API has no endpoint for them.
- **Board ids.** Tools take a board id, the part of a board's address after `/app/board/`, which
  `miro_list_boards` also returns. A full link is not accepted.

[ADR 0231](adr/0231-the-miro-connector-reads-adds-and-changes-and-never-deletes.md) records what
the connector leaves out and why.

## Auto-accept rules

A rule can name a board by its `miro.board` scope ("Miro — board"), set on **Settings >
Auto-accept**. The value is the board id, the part of the board's address after `/app/board/`; a
full link is refused when the rule is saved. A suggestion from `privacyfence_check_policy` for an
uncovered Miro call carries the id already. The approval card never offers "Always allow" for a
Miro operation
([ADR 0232](adr/0232-miro-rules-are-per-board-and-set-from-settings-not-the-card.md)). Every scope
is in [the scope catalogue](approvals-and-policy.md#scope-catalogue).

The rule's verbs cover these tools:

| Verb | Tools |
|---|---|
| read | `miro_list_items`, `miro_get_items`, `miro_list_connectors` |
| add | `miro_create_items`, `miro_create_connectors` |
| update | `miro_update_items`, `miro_update_board` |

The three tools that list boards, get a board and list tags run without a card and need no rule.

## Troubleshooting

**`Miro is not authenticated. Use Authenticate… in PrivacyFence Settings.`** — nothing is signed in
yet. Use **Authenticate…** on the Miro row.

**`Miro's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect
again.`** — the saved sign-in file is damaged. Connect again.

**`Your Miro sign-in has expired or was revoked. Use Authenticate… in PrivacyFence Settings to sign
in again.`** — Miro refused the renewal, for instance because the app was uninstalled or the
sign-in was not used for 60 days. Connect again.

**`Miro returned a token that never expires. Create the PrivacyFence app in Miro with "Expire user
authorization token" turned on; see the Miro setup guide.`** — the app was created without that
choice, and it cannot be changed afterwards. Register a new app as above, sign a new connector config (or build a new bundle) and
connect again.

**`The Miro app is missing the permission(s) boards:read, boards:write. Turn on boards:read and
boards:write in the app's settings in Miro, then sign in again.`** — set the permissions on the
app in Miro, then connect again.

**`Miro organization config not installed`** or **`Miro organization config is incomplete:
client_id and client_secret go together.`** — the installed bundle has no Miro app, or only half
of it. Build the bundle with both options and install it again. (On an organization server a
connector config always carries both.)

**`Miro sign-in failed: …`** and **`Miro returned an incomplete sign-in answer (no …).`** — Miro
refused the sign-in or answered without a part PrivacyFence needs. The text after the colon is
Miro's own; check the client id, the client secret and that the redirect URI is on the app exactly
as written above.

**`Could not reach Miro: …`** or **`Could not reach Miro's sign-in server: …`** — no network route
to Miro. Check the connection or the proxy settings.

**`Miro refused the request (HTTP 403). Your Miro account, or the team PrivacyFence was signed in
to, has no access to it.`** — the board is in another team, or your account cannot open it.
Connect again and choose the team that holds the board.

**`Miro found no such board or item (HTTP 404).`** — the board or item id is wrong, or it was
removed. Take the id from `miro_list_boards`.

**`Not a Miro board id: …`** — a name or link was given. Use the id from `miro_list_boards`, or the
part of the board's address after `/app/board/`.

**`Miro's rate limit was reached. Try again in … seconds.`** (HTTP 429) — Miro limits how many
requests an app can make per minute. Wait the time shown; nothing is retried automatically.

**`Miro answered the move of item … but did not make it.`** — Miro accepted a move into or out of
a frame and left the item where it was. Check the frame id and try again.
