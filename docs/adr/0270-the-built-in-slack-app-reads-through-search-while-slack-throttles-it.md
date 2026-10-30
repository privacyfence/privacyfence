# ADR 0270: The built-in Slack app reads through search while Slack throttles it

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/oauth_apps.py` (`slack_limited`, `slack_manifest`), `src/privacyfence/slack_client.py`.
Relates to [ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md) and [ADR 0269](0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md).

## Context

Slack limits a distributed app that is not in the Slack Marketplace to one request per minute, and 15
messages per call, on `conversations.history` and `conversations.replies`. The Marketplace accepts a
submission only after the app has ten installs on active workspaces, counted across every PrivacyFence
server's users, which a new release will not have. An internal app, installed only in the workspace
that created it, keeps Slack's normal limits.

## Decision

- **The built-in Slack app stays the default,** so Slack connects with no setup like the other
  providers.
- **Limited mode.** `oauth_apps.slack_limited(section)` is true when the Slack section is the built-in
  app and `SLACK_MARKETPLACE_LISTED` is false. In it, `slack_get_channel_history` reads through
  `search.messages` (the channel plus the time range), says so in the result and in the tool
  description, and names the switch to the workspace's own app. Search misses some message subtypes,
  lags slightly, and returns thread replies as top-level matches. When search cannot be scoped to the
  channel it falls back to history and says why. Thread replies keep `conversations.replies`, capped
  at 15, and the result names the limit. Approval cards show a message without its thread preview
  instead of waiting a minute.
- **A one-click own app.** The Slack row on Admin → Services opens Slack's create-app page with a
  manifest (`oauth_apps.slack_manifest`) for an internal app with this server's redirect and the
  default user scopes; the administrator then signs the client ID and secret as a connector config
  ([ADR 0269](0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md)) and
  uploads it on the row. Switching makes everyone who connected Slack connect again.
- The Marketplace listing remains the way out: when it is approved, the flag flips and limited mode
  ends.

## Alternatives considered

- **Dropping the built-in Slack app, so every workspace registers its own.** Rejected: it brings back
  the setup cost the built-in apps remove.
- **Keeping it with no fallback.** Rejected: history reads would be one per minute.
- **Waiting for the Marketplace listing before shipping Slack.** Rejected: the listing needs installs
  that only a shipped Slack connector can produce.

## Consequences

- Channel history is read through search and may be incomplete or late for a workspace on the built-in
  app; thread reads stay slow and short.
- An internal Slack app works only in the workspace that made it. An organization with several
  workspaces keeps the built-in app, or uses an Enterprise Grid organization-wide app.
- Slack sends no PKCE, which is part of the relay's accepted risk
  ([ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md)); the Admin row
  says so, and an own app avoids it.

## Verification

`tests/unit/test_slack_client.py`, `tests/unit/test_oauth_apps.py`, `tests/unit/web/test_routes_admin.py`.
Code: `src/privacyfence/oauth_apps.py`, `src/privacyfence/slack_client.py`.

## Related

ADR 0255, 0259, 0269.
