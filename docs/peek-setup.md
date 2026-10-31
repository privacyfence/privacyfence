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
your Peek server (or, when the install owner has set one on the Admin page, just confirm it), then choose
**Continue**. A browser window opens on the machine running PrivacyFence; sign in to Peek there and
approve the code PrivacyFence shows, then choose **Done** in PrivacyFence. The code is valid for 15
minutes.

Without the desktop window, `privacyfence-app --peek-login` does the same on the command line: it
asks for the server address (unless one is set on the Admin page), prints the page to open and the code to
approve, and saves the token when you approve.

**Organization mode.** Open the connections page (`/connect`) and choose **Connect Peek**. Choose
**Open Peek**, sign in and approve the code, then choose **Done**. **Reconnect Peek** does the same
again. Peek appears there only when the Peek server's address is set: on the Admin page (Features) or by `PRIVACYFENCE_PEEK_BASE_URL` on the server.

The token Peek issues is saved in `credentials/peek_token.json` with the server it belongs to, and
is used only with that server (ADR [0152](adr/0152-which-peek-server-is-used-and-a-token-stays-with-it.md)).

## Automatic accounts

With Peek auto-provisioning, each person who signs in to PrivacyFence gets a Peek account on your
organization's Peek server, or keeps their existing one, and PrivacyFence connects it for them: no
**Connect Peek** clicks. PrivacyFence makes every call; Peek never connects to PrivacyFence, so the two
can stay in separate network zones.

**Set it up.**

1. A Peek admin creates a scoped provisioning key on the Peek server:
   `peek provisioner create --name PrivacyFence`. The key starts with `peekprov_`. It can create and
   release accounts and mint tokens for them; it never reaches an admin account.
2. Set the variables on the server, then restart it: `PRIVACYFENCE_PEEK_BASE_URL` (the Peek server's
   address) and the key, as `PRIVACYFENCE_PEEK_PROVISIONING_KEY` or, better, as
   `PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE` naming a file that holds it (a Docker secret). Setting
   both the plain variable and the file, or a file that cannot be read, refuses the start. The key
   is never stored in the Admin configuration or a bundle. With systemd, add the variables to the
   unit's `EnvironmentFile`; with Docker, to `privacyfence.env`
   ([organization setup guide](org-mode-setup-guide.md), [Docker guide](org-mode-docker.md)).
3. Open **Admin → Services**, find the Peek row and turn **Automatic accounts** on. The row says
   when the key is missing and names the variable the server reads. Or set
   `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` in the server's environment, which does the same: it
   offers Peek and turns automatic accounts on. The row then says it is set by the server, and the
   **Automatic accounts** switch and **Offer** are locked. Removing the variable hands both back to
   the Admin page, which kept its own settings. The variable needs `PRIVACYFENCE_PEEK_BASE_URL` and
   the key, or the server refuses to start, and it does nothing unless the web server runs
   (`web.mcp.enabled`). Outside organization mode it is ignored, with a log warning.

A server upgraded from 6.11 keeps its automatic accounts: the addresses it had are kept and the
Admin page shows them as fixed (see below), and a key variable name it chose keeps working.

**On a single-user install.** With `PRIVACYFENCE_PEEK_AUTO_PROVISION=true`, a single-user install
(no identity provider) connects Peek for its one person, `admin@privacyfence.localhost`, treated as verified
because the install has exactly one person, reachable only with the setup code or a passkey. It
works only when Peek runs on the same computer: `PRIVACYFENCE_PEEK_BASE_URL` must be a `localhost`
address (`localhost`, `127.0.0.1` or `::1`). Peek is connected at each start and at each sign-in.
The Peek install admin needs another address than `admin@privacyfence.localhost`, because Peek refuses to adopt
an admin. Every start counts as a sign-in, so the 90-day rule below never ends the connection. When
you move to an identity provider, run `privacyfence-app --peek-release single-user`.
([ADR 0272](adr/0272-a-single-user-install-provisions-its-one-person-as-verified.md))

**What happens.** When a person signs in, in the browser or through an AI client, PrivacyFence queues
them and a single background worker creates or looks up their Peek account, by their PrivacyFence id
and email, and mints a Peek token for it. Sign-in never waits for Peek. The token lives in the same
`credentials/peek_token.json` as a hand-made connection, marked as provisioned, and **/connect** says
that your organization connected the account automatically.

**Who is provisioned.**

- *A verified email.* Only a person whose ID token says `email_verified` is true is provisioned.
  Without that check, anyone who can set their own email at the identity provider could take over
  another person's Peek account. The **Trust email addresses the identity provider does not mark
  verified** checkbox on the Peek row of **Admin → Services** skips the check. Use it only when your
  identity provider never sends the claim but issues only verified, organization-owned addresses,
  such as Microsoft Entra ID work accounts.
- *A person's own sign-in wins.* Someone who connected Peek by hand on **/connect** keeps that
  connection; Peek is not called for them. **Reconnect Peek** replaces a provisioned token with their
  own, and the provisioned one stays valid in Peek until it expires.
- *Peek admins are not adopted.* If the person's Peek account is an admin, Peek refuses, and they
  connect by hand.
- *No email.* A person whose ID token carries no email is skipped.

**Renewal.** Tokens last 90 days and are renewed in the background when 30 days or fewer are left, for
as long as the person has signed in to PrivacyFence within the last 90 days. PrivacyFence keeps the
person's email in `users/<id>/credentials/peek_provisioning.json` for this. Someone who stops signing
in loses the Peek token when it expires, up to about 150 days after their last sign-in (a token
renewed just inside the 90 days lives another 90).

**When someone leaves.** Remove them from the identity provider first, then run
`privacyfence-app --peek-release PRINCIPAL_ID` (with the provisioning key in the environment, or its `_FILE` form). It
revokes PrivacyFence's token for the person and stops renewing it. Their Peek account and pages stay;
disabling the account is the Peek admin's decision. Someone who can still sign in is provisioned
again, so release is for people who can no longer sign in.

**When one product is down.** PrivacyFence sign-in never needs Peek: if Peek is down, only the
provisioning is delayed, and it is retried by a sweep every six hours and at the next sign-in. Peek
and the tokens already issued keep working while PrivacyFence is down. Peek keeps its own users and
sign-in, including people who do not use PrivacyFence.

## Sign in to Peek with PrivacyFence

On an organization server or a single-user install, people can sign in to Peek with **Sign in with SSO**,
which goes through PrivacyFence and uses the identity PrivacyFence already has. See
[Sign in to other apps with PrivacyFence](oidc-provider.md).

## Values

| Value | Where it comes from |
|---|---|
| Peek server address | Your Peek server's own base URL, such as `https://peek.example.com`. Set it on **Admin → Features** or with `PRIVACYFENCE_PEEK_BASE_URL` on the server. |

Only `https://` addresses work (`http://` is allowed for `localhost`, `127.0.0.1` and `::1`), without
a user name, password, query or fragment. A path prefix is kept.

## Where the server address is set

The address is a setting of the install, not of the organization config bundle:

- **Admin → Features** has a form for it, on a desktop install (for the owner) and on an organization
  server (for administrators). Saving asks for a passkey.
- `PRIVACYFENCE_PEEK_BASE_URL` on the server sets it in the environment. The Admin page then shows
  the address and says it is set by that variable, and no form is offered.
- An upgraded 6.11 server keeps the address its signed bundle had, locked in the same way.

Where an address is set, people do not type it; a token saved for another server stops working and
asks to connect again. Organization mode offers Peek only when an address is set. On a desktop
install without one, Peek is not listed at all
([ADR 0196](adr/0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md)). The
bundle's old `--peek-base-url`, `--enable-peek`, `--peek-auto-provision` and related flags are
refused by `build_org_bundle.py`, which names where each went
([ADR 0257](adr/0257-operational-settings-live-in-a-daemon-written-admin-config.md)). Peek needs no
other entry: there is no callback URL and no client secret.

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
one set for the install. Connect again.

**`Peek organization config not installed`** — organization mode with no Peek server address. Ask your
administrator to set it on the Admin page (Features) or with `PRIVACYFENCE_PEEK_BASE_URL`.
**`Peek organization config is not usable: …`** — the address is not valid; set it again.
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

### Automatic accounts

**`PRIVACYFENCE_PEEK_BASE_URL is not usable: …`** — the daemon refuses to start with a bad address in
the environment. Fix the variable and restart.

**`Peek auto-provisioning is on, but … is not set; people connect Peek by hand on /connect.`** —
the daemon's environment lacks the key. Set the variable named in the message (see the setup above)
and restart.

**`PRIVACYFENCE_PEEK_AUTO_PROVISION must be true, or unset.`** — the value is anything but `true`.
Remove the variable to hand the switch back to the Admin page.

**`PRIVACYFENCE_PEEK_AUTO_PROVISION=true needs PRIVACYFENCE_PEEK_BASE_URL.`** and
**`PRIVACYFENCE_PEEK_AUTO_PROVISION=true needs PRIVACYFENCE_PEEK_PROVISIONING_KEY or
PRIVACYFENCE_PEEK_PROVISIONING_KEY_FILE.`** — the daemon refuses to start without the address or the
key (with a custom key variable, the message names it and its `_FILE` form). Set it and restart.

**`On a single-user install, PRIVACYFENCE_PEEK_AUTO_PROVISION=true needs a localhost address in
PRIVACYFENCE_PEEK_BASE_URL.`** — a single-user install connects only a Peek on the same computer.

**`PRIVACYFENCE_PEEK_AUTO_PROVISION is ignored outside organization mode.`** — a log warning: the
server is in local mode, where the variable does nothing.

**`Peek is set by PRIVACYFENCE_PEEK_AUTO_PROVISION on the server.`** — an Admin change tried to
offer or withdraw Peek while the variable is set. Remove the variable first.

**`Peek provisioning ready at <host> (N accounts managed)`** — the startup check passed.
**`Peek provisioning check failed: …`** — the message after the colon says why, and is one of the
errors below. Provisioning is retried by the six-hourly sweep.

**`Peek refused the provisioning key (HTTP 401). Check the key in the daemon's environment.`** — the
key is wrong, revoked or expired. Create a new one with `peek provisioner create` and restart.

**`This Peek server has no provisioning API (HTTP 404). Update Peek, or turn off Peek
auto-provisioning.`** — the server is too old or the address is not a Peek server.

**`Peek refused to provision this account (HTTP 409): account is a Peek admin; connect it by hand`** —
the person is a Peek admin. They connect on **/connect**.

**`Peek refused to provision this account (HTTP 409): account is linked to another external id or
email`** — the email already belongs to a Peek account linked to a different PrivacyFence person.
Fix the account in Peek.

**`Peek refused to provision this account (HTTP 403): account email domain is not allowed`** — the
Peek server does not allow that email domain. Allow it in Peek, or the person connects by hand.
**`… (HTTP 403): account disabled`** — the Peek admin disabled the account; enable it in Peek.

**`Peek has no provisioned account for this person (HTTP 404).`** — Peek lost the link between the
person and the account. It is retried at the person's next sign-in.

**`Not a Peek provisioning id: …`** — the PrivacyFence account id has characters Peek's external id
does not allow. That person connects by hand.

**`Peek provisioning queue is full; <id> waits for the next sweep or sign-in`** — more than 1000 people
are waiting, which only happens when Peek has been slow or down for a long time. Nothing is lost;
the sweep or the person's next sign-in queues them again.

**`Peek provisioning sweep failed (will retry): …`**, **`Peek provisioning for <id> failed: …`**,
**`Peek provisioning worker error: …`** — an unexpected failure; the next sweep tries again. If it
repeats, check that Peek is reachable from the daemon.

**`No such account.`**, **`Peek auto-provisioning is not enabled in the organization config.`**,
**`Set <name> to the Peek provisioning key first.`** — messages of `--peek-release`. Check the
account id (a directory under `users/`), that **Automatic accounts** is on for Peek (the Admin switch or `PRIVACYFENCE_PEEK_AUTO_PROVISION`), and that the
key variable is set in the shell that runs the command. A Peek error is printed as it is; see above.
