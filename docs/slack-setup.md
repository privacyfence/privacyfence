# Slack setup

PrivacyFence uses a Slack **user token**: your AI client sees exactly the channels, direct messages
and private groups you can see, as you. There is no bot to invite.

You need this page only to use your organization's own app instead of the built-in one. The next two
sections say why you might, and what the built-in app does.

## The built-in app, and limited mode

An organization server and the Docker image offer Slack with PrivacyFence's built-in app: nothing to
register, and each person approves it in their browser from PrivacyFence. If your workspace requires
admin approval for apps, a workspace admin approves it once on Slack's side.

Slack limits apps that are not listed in the Slack Marketplace to one history or thread request a
minute and 15 messages a call. PrivacyFence is not listed yet, so the built-in app runs in **limited
mode**:

- `slack_get_channel_history` reads through Slack search instead. Some message subtypes are missing,
  a message posted a moment ago can take a while to appear, and thread replies come back as top-level
  matches. The result carries a note, "Read through Slack search, because Slack limits history reads
  for this app; an administrator can switch to your workspace's own app on the Admin page.", and so
  does the tool description. For a conversation search cannot be scoped to, it falls back to the
  history call and says why.
- `slack_get_thread_replies` returns at most 15 replies and names the limit in a note.
- Everything else (search, sending, read state) is unaffected.

The built-in Slack row on **Admin → Services** explains the limit. Until PrivacyFence is listed, use
your workspace's own internal app, which reads at full speed.

Because Slack sends no PKCE, a sign-in code for the built-in app passes through
`oauth.privacyfence.eu` without it; your own app avoids this. See
[Security and compliance](security-and-compliance.md#built-in-apps-the-admin-config-and-the-relay).

Do not mark the built-in PrivacyFence app as trusted or pre-approved: that trust would extend to
every PrivacyFence server and to anyone using the public app. To trust an app, register your own.

## Your workspace's own app, in one click

On an organization server, **Admin → Services** has a **Create your workspace's own Slack app**
button on the Slack row. It opens Slack's create page with a manifest filled in for your server (the
redirect URL `<server>/oauth/callback/slack` and the user token scopes below, no bot). If Slack does
not fill the page in, choose **Create New App → From an app manifest** and paste the manifest shown
under **If Slack does not fill in the app**. Then:

1. Leave the app undistributed. It is then an internal app, which serves **one workspace** and keeps
   Slack's full rate limits. **Never activate public distribution** (**Manage Distribution →
   Activate Public Distribution**): Slack would then apply the Marketplace limits to it.
2. Copy the **Client ID** and **Client Secret** from **Basic Information → App Credentials**.
3. Sign them as a connector config and upload the file on the same row (**Upload a connector
   config…**): `python3 scripts/sign_connector_config.py sign --provider slack --serial 1 --client-id
   <Client ID> --client-secret-file <file> --key <signing key> --output slack.connector.txt`, or the
   signing page that works offline in a browser. The first upload asks you to check the signing key's
   fingerprint, and a passkey. See [org-mode-setup-guide.md](org-mode-setup-guide.md).

Switching to your own app, or back with **Remove our own app**, makes everyone who connected Slack
sign in again. An internal app serves one workspace, so people in any other Slack workspace
cannot sign in with it.

On a desktop install the app is a `slack` section in the bundle instead; see below.

## What you need

- Permission to create apps in your Slack workspace (and, if your workspace requires it, an admin
  who approves app installs).
- Python 3 to run `scripts/sign_connector_config.py` (organization server) or
  `scripts/build_org_bundle.py` (desktop install), or a browser for the offline signing page.

## Register the app

If you did not use the Admin button, register the app by hand:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**.
   Name it (for example `PrivacyFence`), pick your workspace and click **Create App**.
2. **OAuth & Permissions → Scopes → User Token Scopes** (not Bot Token Scopes): add every scope
   in the [values table](#values). Add no bot scopes.
3. **OAuth & Permissions → Redirect URLs:** add the redirect for each kind of deployment you run
   (see [Values](#values)): the local redirect for desktop installs, the org redirect for an
   organization server. Click **Add**, then **Save URLs**; a URL that was added but not saved is
   not registered. One app can carry both, and the same client ID and secret serve both kinds of
   deployment.
4. **Basic Information → App Credentials:** copy the **Client ID** and **Client Secret**.

**Never activate public distribution** (**Manage Distribution → Activate Public Distribution**),
and never share the connector config or bundle outside your workspace. An app created from scratch
and left undistributed is an internal app. Slack restricts apps distributed outside the Slack
Marketplace to 1 request per minute and 15 messages per call on `conversations.history` and
`conversations.replies`, which `slack_get_channel_history`, `slack_get_thread_replies` and
single-message lookups (permalinks, thread previews on approval cards) rely on. Nothing fails outright; reads just become very slow and
truncated.

If your workspace requires admin approval for app installs, an admin approves each user's first
sign-in on Slack's side. That is a Slack workspace setting, not a PrivacyFence one.

## Rate limits and routines

Slack limits each method by tier (requests per minute). The methods PrivacyFence's Slack tools call:

| Method | Tier | Used for |
|---|---|---|
| `conversations.history`, `conversations.replies` | Tier 3 (50/min); 1/min and 15 messages for an app distributed outside the Marketplace | channel history, thread replies and a thread's read marker |
| `conversations.info` | Tier 3 (50/min) | channel type, membership and your read marker |
| `users.conversations` | Tier 3 (50/min) | the conversations you are a member of |
| `search.messages` | Tier 2 (20/min), at most 100 matches per page | search |
| `conversations.list` | Tier 2 (20/min) | the channel, DM and group chat listings |

A routine that reads Slack by time should not read each conversation one by one: that costs a
`conversations.history` and a `conversations.info` request per conversation, about 2 minutes for 50
conversations on an internal app and about 50 minutes on a distributed one. Each run instead:

1. Calls `slack_search_messages` with `minutes` set to the time since the last run, `member_only`
   true, `exclude_channels` set to the ids the "Slack — conversations I'm a member of" rule excludes,
   and `count` 100. This costs one `users.conversations` walk (one request per 200 conversations)
   and 1–3 `search.messages` requests. Add `read_state` true only when the run needs read state for
   most results: it costs one `conversations.info` request per conversation in the results.
   Otherwise call `slack_get_read_state` afterwards for the few messages that matter (at most 20, at
   most 3 threads).
2. Calls `slack_get_thread_replies` for a result whose thread needs context. This costs one
   `conversations.replies` and one `conversations.info` request. Its replies come back with
   `read_state` from the thread's own marker.
3. Uses `slack_get_channel_history` with `minutes` only for a conversation where the search-index
   delay matters.

On an app distributed outside the Marketplace, step 2 is limited to 1 request per minute. See
[ADR 0227](adr/0227-member-only-slack-search-drops-other-conversations-before-the-gate.md).

## Read state

`read_state` is `unread`, `read` or `unknown`, judged against your own Slack read marker for the
conversation. A message you wrote is `read`. `unknown` means there is no marker to judge by.

A thread reply in search results is `unknown`: Slack tracks the read marker of a thread separately
from the channel's, and a search result does not carry it. `slack_get_thread_replies` and
`slack_get_read_state` read the thread's own marker.

These tools return it:

- `slack_get_channel_history` and `slack_get_thread_replies`, on every message, with no extra request.
- `slack_search_messages`, only with `read_state` true.
- `slack_get_read_state`, for up to 20 messages you name, and no message text.

See [ADR 0234](adr/0234-slack-reads-report-the-users-own-read-state.md) and
[ADR 0235](adr/0235-a-separate-auto-approved-tool-checks-the-read-state-of-named-messages.md).

## Values

| Item | Value |
|---|---|
| Local redirect (desktop installs) | `http://127.0.0.1:53682/callback` — exactly this; Slack requires an exact match |
| Org redirect (organization server) | `<server>/oauth/callback/slack`, with `<server>` your server's public address, `PRIVACYFENCE_ISSUER_URL` (for example `https://pf.acme.example.com/oauth/callback/slack`) |
| User token scopes | `channels:read`, `groups:read`, `im:read`, `mpim:read`, `channels:history`, `groups:history`, `im:history`, `mpim:history`, `users:read`, `users:read.email`, `search:read`, `chat:write`, `im:write`, `channels:write`, `groups:write`, `mpim:write` |
| Connector config (organization server) | `sign_connector_config.py sign --provider slack --client-id <Client ID> --client-secret-file <file holding the Client Secret>` |
| Bundle flags (desktop install) | `--slack-client-id <Client ID>` and `--slack-client-secret <Client Secret>` (both required together); optional `--slack-scopes <scope> …` |

What the less obvious scopes are for: `users:read.email` resolves authors' email addresses;
`im:write` and `mpim:write` open a new direct or group message (`slack_create_group_chat`);
`im:write`, `channels:write`, `groups:write` and `mpim:write` mark a conversation unread (the
`mark_unread` option of `slack_send_message`), depending on the conversation type.

`--slack-scopes` replaces the requested scope list with your own, for desktop installs and an
organization server alike. A scope you leave out breaks the tools that need it, so leave it unset
unless you have a reason.

## Give PrivacyFence the app

On an organization server, sign the Client ID and Client Secret as a connector config and upload it
on **Admin → Services** (see [above](#your-workspaces-own-app-in-one-click)). On a desktop install,
put them in the bundle:

```bash
python3 scripts/build_org_bundle.py \
  --slack-client-id 1234567890.1234567890 \
  --slack-client-secret abcdef0123456789abcdef0123456789 \
  -o org_bundle.json --merge
```

`--merge` adds Slack to an existing bundle; drop it if this is the first service in the bundle.
Install the bundle on **Admin → Organization configuration**
([connecting-a-service.md](connecting-a-service.md)).

## Users connect

Users click **Authenticate…** next to Slack (desktop install, after the bundle is installed) or **Connect**
(organization server), then review the permissions on Slack's page and click **Allow**. The shared
flow is described in [connecting-a-service.md](connecting-a-service.md).

## Troubleshooting

**`missing_scope` errors** — a scope was not on the app when the user signed in. Add it under
**User Token Scopes**, then have each user **Reconnect…** to get a token that includes it.

**`not_in_channel` on history reads** — the token sees only conversations the user is a member of.
Join the channel in Slack first.

**`invalid_auth` or `token_revoked`** — the token was revoked (the user removed the app, or an
admin uninstalled it). **Reconnect…**.

**Slack says `redirect_uri did not match any configured URIs`, or the browser does not come
back** — the URL Slack names after `Passed URI:` is missing from the app's **Redirect URLs**. Add
exactly that URL and click **Save URLs**, then connect again; no bundle rebuild or restart is
needed. It must be exactly `http://127.0.0.1:53682/callback` for desktop installs, or
`<server>/oauth/callback/slack` for an organization server; `localhost` in place of
`127.0.0.1` does not match. If the URL is already listed, check that the app you edited is the one
whose Client ID is in the connector config or bundle.

**Channel history reads through search, or thread replies stop at 15** — the server uses the built-in
app, which Slack limits until PrivacyFence is in the Marketplace. Switch to your workspace's own app
([above](#your-workspaces-own-app-in-one-click)).

**Reads became slow, or channel history stops at 15 messages with your own app** — check **Manage Distribution**.
If public distribution was ever activated, Slack may treat the app as distributed outside the
Marketplace and apply the reduced limits described under
[Register the app](#register-the-app). Deactivate it; if the limits persist, ask Slack support to
confirm the app's distribution status.
