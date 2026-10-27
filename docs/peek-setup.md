# Peek setup

PrivacyFence reaches Peek, a self-hosted server for sharing HTML pages inside a company and
collecting comments on them, as **your own Peek account**: your AI client sees and changes the pages
that account manages (an admin account manages every page). There is no OAuth app to register;
people connect with Peek's own device sign-in, the flow `peek login` uses.

## What you need

- A Peek server you can sign in to.
- For `peek_add_comment`, a Peek server with the account comment endpoint:
  `POST /api/uploads/{slug}/account-comments`, answered for the page's owner or an admin using an
  access token, which posts the comment under that account's own name and answers with the new
  comment. The endpoint is part of Peek: a Peek server without it answers 404 or 405, and
  `peek_add_comment` fails with a message saying so (ADR [0153](adr/0153-peek-comments-are-posted-only-through-the-account-comment-endpoint.md)).
  Every other tool works with any Peek server.

## Users connect

**Local mode.** In **Settings > Connectors > Peek**, choose **Authenticate…**. Enter the address of
your Peek server (or, when your organization's bundle pins one, just confirm it), then choose
**Continue**. A browser window opens on the machine running PrivacyFence; sign in to Peek there and
approve the code PrivacyFence shows, then choose **Done** in PrivacyFence. The code is valid for 15
minutes.

Without the desktop window, `privacyfence-app --peek-login` does the same on the command line: it
asks for the server address (unless the bundle pins it), prints the page to open and the code to
approve, and saves the token when you approve.

**Organization mode.** Open the connections page (`/connect`) and choose **Connect Peek**. Choose
**Open Peek**, sign in and approve the code, then choose **Done**. **Reconnect Peek** does the same
again. Peek appears there only when the organization's bundle names the Peek server.

The token Peek issues is saved in `credentials/peek_token.json` with the server it belongs to, and
is used only with that server (ADR [0152](adr/0152-which-peek-server-is-used-and-a-token-stays-with-it.md)).

## Values

| Value | Where it comes from |
|---|---|
| Peek server address | Your Peek server's own base URL, such as `https://peek.example.com`. In `build_org_bundle.py`: `--peek-base-url`. |

Only `https://` addresses work (`http://` is allowed for `localhost`, `127.0.0.1` and `::1`), without
a user name, password, query or fragment. A path prefix is kept.

## Build and distribute the bundle

`--peek-base-url URL` writes the `peek.base_url` entry of the organization config bundle (see
[Configuration reference](configuration-reference.md#build-options)). In local mode it pins the
server, so people do not type it; a token saved for another server stops working and asks to
connect again. Organization mode offers Peek only when the bundle has it. Without a `peek` section,
in either mode, Peek is not listed at all; `--enable-peek` writes the bare switch for local mode when
you do not want to pin a server ([ADR 0196](adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md)).
Peek needs no other entry: there is no callback URL and no client secret.

## What the assistant can do

| Tool | What it does | Gate |
|---|---|---|
| `peek_list_pages` | Lists the pages the account manages, newest first | auto |
| `peek_get_comments` | Reads a page's comments (the latest version, or one you name) | review |
| `peek_get_stats` | Reads visit counts, and the names and times of recent visitors | review |
| `peek_list_versions` | Lists a page's versions, newest first, with size, time and link | auto |
| `peek_list_grants` | Lists the people and Google groups granted access to a restricted page | review |
| `peek_upload_page` | Publishes a page, or a new version of one | popup |
| `peek_set_visibility` | Changes who can open a page | popup |
| `peek_grant_access` | Grants a person or Google group access to a restricted page | popup |
| `peek_revoke_access` | Removes a person's or group's access to a restricted page | popup |
| `peek_add_comment` | Posts a comment under your own Peek account name | popup |
| `peek_rename_page` | Renames a page | popup |
| `peek_delete_page` | Deletes a page with its comments and visit history | popup |

See the [tools reference](tools-reference.md#peek) for every parameter and return value.

- **Rules.** A **Peek — page** rule in **Settings → Auto-accept** names page slugs. *Read* lets
  `peek_get_comments` and `peek_get_stats` run without a card for those pages, and *add* does
  the same for `peek_upload_page` with one of those slugs, which publishes a new version. A rule
  never covers a new page, a visibility change, a rename or a delete, and the card has no "Always
  allow" button for Peek. Ticking **Let exports, downloads and page uploads carry personal data** on the
  scoped token a scheduled job uses lets a version with personal data in it go through without a
  card, which a dashboard refreshed on a schedule needs
  ([Letting personal data through](approvals-and-policy.md#letting-personal-data-through);
  [ADR 0158](adr/0158-peek-rules-name-pages-and-cover-only-new-versions.md)).
- A new version keeps the page's link and its visibility. Comments belong to one version, and are
  read per version; `peek_add_comment` posts on the latest version or the one you give.
- A page has one of three visibilities: `public` (anyone with the link), `signed-in` (anyone signed
  in to Peek) and `restricted` (only you, Peek admins and the people or groups granted access). A
  new page is restricted unless you choose otherwise.
- Grants apply only while a page is restricted. They stay on the page when it becomes public or
  signed-in, and apply again if it is restricted later.
- Peek emails a person or group the link when the page is restricted and they did not have access
  yet, and emails everyone granted access when a page becomes restricted.
- Group grants need Peek's Google group lookup; a Peek server without it accepts grants to people
  only.
- `peek_grant_access` and `peek_revoke_access` always ask, and `peek_list_grants` always shows a
  review card: no **Peek — page** rule covers them.
- A rename can change a page's slug. Old links keep redirecting in browsers, but the other Peek
  tools answer "no such page" for the old slug; use the slug the rename returns, or
  `peek_list_pages`.
- Peek's API cannot read a page's HTML back, so no tool does.

## Privacy

- Visit stats carry visitor names and times only, never IP addresses or browsers (ADR
  [0154](adr/0154-peek-visit-stats-carry-names-and-times-never-ip-addresses.md)).
- Grant email addresses are shown on the review card and the approval cards, and are personal data.
- Every approval card names the Peek server it will talk to.
- A token is sent only to the server it was issued for, over `https` (or loopback `http`), and
  redirects are never followed.
- A published page is approved with its visible text first and then its whole HTML source, because
  scripts, comments, hidden elements and image addresses are published too.

## Troubleshooting

**`Peek is not authenticated. Use Authenticate… in PrivacyFence Settings.`** — no sign-in is saved
yet. Connect as described above.

**`Peek's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect
again.`** — the saved credential file is damaged. Connect again.

**`Peek refused the saved sign-in (HTTP 401). Use Authenticate… in PrivacyFence Settings to connect
again.`** — the token was revoked or expired. Connect again.

**`Peek was connected to a different server than your organization uses. Use Authenticate… in
PrivacyFence Settings to connect again.`** — the saved token belongs to a server other than the
bundle's. Connect again.

**`Peek organization config not installed`** — organization mode without a `peek` entry in the
bundle. Ask your administrator to rebuild the bundle with `--peek-base-url`.
**`Peek organization config is not usable: …`** — the bundle's address is not valid; rebuild it.
**`Peek is not set up by your organization.`** — the same, shown on the connections page.

**`Enter the Peek server address, such as https://peek.example.com.`**, **`The Peek server address
must start with https:// (http:// is allowed only for localhost).`**, **`The Peek server address
must not contain a user name, password, query or fragment.`** — fix the address.

**`Could not reach Peek at <host>: <error>`** — the server is down or not reachable from the machine
running PrivacyFence. Check the address and the network.

**`Peek answered with a redirect (HTTP 3xx). Check the Peek server address.`** — PrivacyFence never
follows redirects, so a sign-in token cannot be sent elsewhere. Type the address Peek itself uses,
without a redirecting alias.

**`Peek has no device sign-in at this address (HTTP 404 or 405). Check the Peek server address.`** —
the address is not a Peek server, or one without device sign-in.

**`Peek answered with a sign-in address on another server. Check the Peek server address.`** — the
sign-in address Peek returned is not on the server you typed. The usual cause is a Peek
`--base-url` that differs from the address typed in PrivacyFence, such as a host alias, a port, or
`http` behind a TLS proxy: type exactly Peek's own base URL.

**`Peek answered the sign-in request without a sign-in code.`**, **`Peek answered the sign-in check
with something unexpected.`**, **`Peek answered with something other than JSON (HTTP …). Check the
Peek server address.`** — the address points at something that is not a Peek server.

**`Peek has not recorded your approval yet. Approve the code in Peek, then choose Done again.`** —
approve the code in the browser first. **`The sign-in was denied in Peek.`** and **`The sign-in code
expired. Start again.`** (the command line says `Run --peek-login again.`) — start the sign-in
again.

**`Peek refused the request (HTTP 403): …`** — the account does not own the page (only its owner or
an admin changes it, or reads its comments).

**`Peek found no such page (HTTP 404). It may have been renamed or deleted: call peek_list_pages to
see the current slugs.`** and **`No Peek page '…' among the pages this account can manage. …`** —
the slug is wrong, the page was deleted or renamed, or belongs to someone else.

**`Peek has no version N of this page (HTTP 404). Retention may have removed it.`** — read the
versions up to the latest number `peek_list_pages` shows.

**`This Peek server does not accept comments from PrivacyFence: it has no account comment endpoint.
See the Peek setup guide.`** — the server predates the account comment endpoint (see
[What you need](#what-you-need)). Update the Peek server.

**`Peek no longer has page passwords. Use restricted, then grant access to people with
peek_grant_access.`** — a page password was given. Restrict the page and grant access instead.

**`visibility must be public, signed-in or restricted.`** — use one of the three visibilities.

**`Peek is limiting how fast these requests can be made (HTTP 429). Wait a minute, then try
again.`** — Peek's rate limit. Wait, then repeat the call.

**`Peek refused the file (HTTP 415): …`** — Peek does not accept the file as HTML; publish an HTML
page.

**`Peek has no version N of this page (HTTP 404). Retention may have removed it.`** when commenting —
the version does not exist (any more). Use a number from `peek_list_versions`.

**`This page's latest version is N.`** — `peek_add_comment` was given a higher version than the
page has.

**`Peek has no such grant on this page (HTTP 404). Call peek_list_grants to see who has access.`** —
nobody with that address and kind has access. Check `peek_list_grants`.

**`This Peek server cannot check Google group membership, so it does not accept group grants. Grant
access to people one by one, or ask the Peek admin to set up group lookup.`** — the Peek server has
no Google group lookup.

**`kind must be user or group.`**, **`Enter one email address, such as alice@example.com.`** — fix
the grant: one address per call, kind `user` or `group`.

**`name must be 1 to 200 characters.`** — the page name, given or taken from the file, is empty or
longer than 200 characters.

**`Peek API error (HTTP 400): file too large or invalid form`** (or HTTP 413) — the page is larger
than the Peek server accepts (2 MiB by default); PrivacyFence itself stops at 10 MB.
