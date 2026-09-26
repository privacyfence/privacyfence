# ADR 0064: Browser notifications stay on the machine

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-02 in
`docs/approval-list-ui-ux.md` §4.1–§4.3, deleted in `f5b57380` — read it with
`git show f5b57380^:docs/approval-list-ui-ux.md`, whose §4 had by then been condensed; the full
text is at `git show a59ae8db^:docs/approval-list-ui-ux.md`). Tiers 0–1 landed in `ad9a9b0f`, merged
as [#189](https://github.com/privacyfence/privacyfence/pull/189); the detail levels in `a59ae8db`,
merged as [#202](https://github.com/privacyfence/privacyfence/pull/202). Implemented.
Amended for org mode by [ADR 0081](0081-org-mode-sends-a-count-only-web-push.md) (a count-only web push); local mode unchanged.

## Context

A pending approval is only useful if the human notices it, and the approvals tab is often in the
background. A notification body is a data-release surface: it shows on a lock screen, a watch, a
shared screen. The gated content it could describe (an event title, a recipient, a document name)
is exactly what PrivacyFence exists to hold back. Web Push would reach a closed tab or a phone,
but only through the browser vendor's push service, a third party that learns when the agent
touches gated data, and it needs VAPID keys, a subscription store and, on iOS, an installed web app.

## Decision

**Notifications are produced by the open page on this machine and never leave it: a title badge
and a local service-worker notification, with no push service, no VAPID keys and no subscription
store.**

- Tier 0 (`web_shell.py`'s `_STREAM_JS`): when the approvals event's row count changes, the
  document title gains a `(N) ` prefix and an `aria-live` region announces the count. No
  permission needed.
- Tier 1: `registration.showNotification()` through `resources/sw.js`, only when the tab is not
  focused, at most one per 5 seconds, one grouped notification (tag `pf-approvals`), never one per
  row. `sw.js` has no `push` handler and fetches nothing; its only other job is routing a click to
  `/approvals`. The permission pre-prompt appears only after a decision, never on page load.
- What the body may say is an allowlist per level, in `notificationBody()`, set by
  `web.notifications.detail` (`SettingsController.set_notifications_detail`,
  `NOTIFICATIONS_DETAIL_LEVELS`), default `minimal`:
  - `minimal`, and any notification for more than one approval: the count only
    ("N approvals pending");
  - `standard`: the connector, the tool name and the direction (`read` for a review gate, `write`
    for a popup gate);
  - `detailed`: `standard` plus the row's `summary`, the one field that can carry gated content.
- `web.notifications.enabled` (default `true`) turns tiers 0 and 1 off together.

## Alternatives considered

- **Tier 2, Web Push.** Not built. It needs a secure context a phone can reach (in practice org
  mode's HTTPS endpoint), iOS allows it only for Home Screen web apps, and even with a content-free
  payload the push service learns the timing of every approval. The plan's condition for ever
  adding it was a bare tickle with no approval data, fetched and rendered locally.
- **Tier 3, a native OS notification from the daemon.** Not built: the native macOS UI was retired
  (ADR 0001, ADR 0002), and a privilege-separated daemon runs outside the user's desktop session.
- **`new Notification()` from the page.** Rejected in favor of `showNotification()`, which works on
  every browser the product targets.

## Consequences

- A closed tab gets no notification; the companion and the badge are what remain.
- Nothing about an approval reaches a third party through notifications.
- `detailed` is opt-in; choosing it puts gated summaries on whatever surface shows notifications.
- The CSP needs `worker-src 'self'` for `sw.js` (ADR 0063).

## Verification

- `tests/unit/test_web_shell.py`'s `TestNotifications`: service worker registration, the badge and
  announcer, no permission prompt on load, `maybeNotify` never reading row fields itself, the
  per-level allowlist in `notificationBody()`, `minimal` as default, the 5 s rate limit, and no
  notification while the tab is focused.

## Related

- [ADR 0005](0005-moving-the-approval-decision-off-the-device.md) — the off-device question a push
  tier would belong to.
- [ADR 0063](0063-the-web-ui-csp-uses-per-response-nonces-not-unsafe-inline.md) — the CSP.
