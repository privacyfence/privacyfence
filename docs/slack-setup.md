# Slack setup

PrivacyFence uses a Slack **user token**: your AI client sees exactly the channels, direct messages
and private groups you can see, as you. There is no bot to invite. One administrator creates a
Slack app once per workspace and puts its client ID and secret into the organization config
bundle; users then approve the app in their browser from PrivacyFence.

## What you need

- Permission to create apps in your Slack workspace (and, if your workspace requires it, an admin
  who approves app installs).
- Python 3 to run `scripts/build_org_bundle.py`.

## Register the app

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
and never share the bundle outside your workspace. An app created from scratch and left
undistributed is an internal app. Slack restricts apps distributed outside the Slack Marketplace
to 1 request per minute and 15 messages per call on `conversations.history` and
`conversations.replies`, which `slack_get_channel_history`, `slack_get_thread_replies` and
single-message lookups (permalinks, thread previews on approval cards) rely on. Nothing fails outright; reads just become very slow and
truncated.

If your workspace requires admin approval for app installs, an admin approves each user's first
sign-in on Slack's side. That is a Slack workspace setting, not a PrivacyFence one.

## Rate limits and routines

Slack limits each method by tier (requests per minute). The methods PrivacyFence's Slack tools call:

| Method | Tier | Used for |
|---|---|---|
| `conversations.history`, `conversations.replies` | Tier 3 (50/min); 1/min and 15 messages for an app distributed outside the Marketplace | channel history, thread replies |
| `conversations.info` | Tier 3 (50/min) | channel type and membership |
| `users.conversations` | Tier 3 (50/min) | the conversations you are a member of |
| `search.messages` | Tier 2 (20/min), at most 100 matches per page | search |
| `conversations.list` | Tier 2 (20/min) | the channel, DM and group chat listings |

A routine that reads Slack by time should not read each conversation one by one: that costs a
`conversations.history` and a `conversations.info` request per conversation, about 2 minutes for 50
conversations on an internal app and about 50 minutes on a distributed one. Each run instead:

1. Calls `slack_search_messages` with `minutes` set to the time since the last run, `member_only`
   true, `exclude_channels` set to the ids the "Slack — conversations I'm a member of" rule excludes,
   and `count` 100. This costs one `users.conversations` walk (one request per 200 conversations)
   and 1–3 `search.messages` requests.
2. Calls `slack_get_thread_replies` for a result whose thread needs context. This costs one
   `conversations.replies` and one `conversations.info` request.
3. Uses `slack_get_channel_history` with `minutes` only for a conversation where the search-index
   delay matters.

On an app distributed outside the Marketplace, step 2 is limited to 1 request per minute. See
[ADR 0226](adr/0226-member-only-slack-search-drops-other-conversations-before-the-gate.md).

## Values

| Item | Value |
|---|---|
| Local redirect (desktop installs) | `http://127.0.0.1:53682/callback` — exactly this; Slack requires an exact match |
| Org redirect (organization server) | `<server-issuer-url>/oauth/callback/slack`, with `<server-issuer-url>` the bundle's `--server-issuer-url` (for example `https://pf.acme.example.com/oauth/callback/slack`) |
| User token scopes | `channels:read`, `groups:read`, `im:read`, `mpim:read`, `channels:history`, `groups:history`, `im:history`, `mpim:history`, `users:read`, `users:read.email`, `search:read`, `chat:write`, `im:write`, `channels:write`, `groups:write`, `mpim:write` |
| Bundle flags | `--slack-client-id <Client ID>` and `--slack-client-secret <Client Secret>` (both required together); optional `--slack-scopes <scope> …` |

What the less obvious scopes are for: `users:read.email` resolves authors' email addresses;
`im:write` and `mpim:write` open a new direct or group message (`slack_create_group_chat`);
`im:write`, `channels:write`, `groups:write` and `mpim:write` mark a conversation unread (the
`mark_unread` option of `slack_send_message`), depending on the conversation type.

`--slack-scopes` replaces the requested scope list with your own, for desktop installs and an
organization server alike. A scope you leave out breaks the tools that need it, so leave it unset
unless you have a reason.

## Build and distribute the bundle

`scripts/build_org_bundle.py` is in the PrivacyFence source repository and is attached to every
stable GitHub Release. It needs only Python 3; no PrivacyFence install is required.

```bash
python3 scripts/build_org_bundle.py \
  --slack-client-id 1234567890.1234567890 \
  --slack-client-secret abcdef0123456789abcdef0123456789 \
  -o org_config.json --merge
```

`--merge` adds Slack to an existing `org_config.json`; drop it if this is the first service in the
bundle. Distribute the bundle to desktop users, or build it with the server flags and install it on
your organization server — see [org-mode-setup-guide.md](org-mode-setup-guide.md) for signing and
the server flags.

## Users connect

Users install the bundle and click **Authenticate…** next to Slack (desktop) or **Connect**
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
`<server-issuer-url>/oauth/callback/slack` for an organization server; `localhost` in place of
`127.0.0.1` does not match. If the URL is already listed, check that the app you edited is the one
whose Client ID is in the bundle (`--slack-client-id`).

**Reads became slow, or channel history stops at 15 messages** — check **Manage Distribution**.
If public distribution was ever activated, Slack may treat the app as distributed outside the
Marketplace and apply the reduced limits described under
[Register the app](#register-the-app). Deactivate it; if the limits persist, ask Slack support to
confirm the app's distribution status.
