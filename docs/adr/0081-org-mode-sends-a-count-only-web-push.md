# ADR 0081: Org mode sends a count-only web push; local mode sends none

## Status

Accepted — 2026-09-26. Amends [ADR 0064](0064-browser-notifications-stay-on-the-machine.md) for
org mode only: local mode's notifications still never leave the machine. Uses the route
classification rule of [ADR 0014](0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md)
for the new routes.

## Context

Org mode is reached from phones. A phone browser suspends a background tab within seconds, so
ADR 0064's tiers (a title badge and `showNotification()` from the open page) never fire there:
a phone user learns about an approval only by opening the page. iOS goes further and exposes web
push only to a site added to the Home Screen, which requires a Web App Manifest; there was none.

ADR 0064 rejected web push for a reason that still holds: the notification travels from the server
through the browser vendor's push service (Apple, Google, Mozilla or Microsoft) to the device, so a
third party learns that something happened, and, depending on the payload, what. Its stated
condition for ever adding push was "a bare tickle with no approval data". This is the first
time anything derived from an approval leaves the org server for a third party.

## Decision

**Org mode sends web push (RFC 8030) when an approval is created for a principal. The payload is
the `minimal` level and nothing more, encrypted to the browser and padded to a fixed size. An org
can turn it off; local mode does not have it.**

1. **The payload is a count.** `web_push.minimal_payload(count)` is the only function that builds
   one: `{"title": "PrivacyFence", "body": "N approval(s) pending"}`, the same text tier 1 shows at
   `minimal`. It takes a number, not an approval. `PushNotifier.on_new_approval` reads only the
   approval's `principal_id`. No tool name, connector, summary, requester, preview or approval id
   can reach a push, whatever `notifications_detail` a desktop install uses. The service worker
   shows only the payload's `title` and `body` strings.
2. **The push service learns timing, not content.** The payload is encrypted to the subscribing
   browser (RFC 8291, `aes128gcm`) and padded to 128 bytes before encryption, so every push is the
   same size and the count cannot be read off the ciphertext length. What Apple, Google, Mozilla or
   Microsoft can see is that this server sent a notification to that device at that time. The
   `Topic` header collapses undelivered notices into one, and `TTL` is the pending-approval lifetime
   (15 minutes).
3. **Rate limit.** At most one push per principal every 5 seconds, the same rate as tier 1.
4. **Subscriptions are per principal**, stored at `users/<principal>/push_subscriptions.json`
   (0600), at most 10 each. `POST`/`DELETE /api/push/subscription` require the org session, the
   double-submit CSRF token and a matching Origin. A browser endpoint belongs to one principal at a
   time: a second person subscribing the same browser profile takes it over. A 404 or 410 from the
   push service removes the subscription (RFC 8030 section 7.3).
5. **The endpoint is an allowlist, not a filter (SSRF).** An endpoint URL comes from a signed-in
   user's browser and becomes a server-side HTTP request, so it is attacker-influenced input. It is
   accepted only as `https`, on the default port, with no userinfo, to a host equal to or under
   `push.apple.com`, `fcm.googleapis.com`, `push.services.mozilla.com` or `notify.windows.com`. It
   is checked when stored and again before each send, and every request is sent with redirects
   off, so neither a subscription nor a push service's response can point this server at an
   internal host. DNS for those names is out of our hands; resolving them to an internal address
   would need the server's resolver to be compromised already.
6. **VAPID key.** A P-256 key pair is generated on first start at `org/web_push_vapid_key.pem`
   (0600, owned by the service account), beside the OAuth client and refresh stores. It is never
   in the org config bundle, which is distributed. The JWT's `sub` is the server's `issuer_url`.
7. **The org-wide switch.** `build_org_bundle.py --no-web-push` writes `web_push.enabled: false`;
   default on. Off means no key is generated, no listener is registered, the subscription routes
   are not mounted and no page offers to subscribe.
8. **Installable app.** `GET /manifest.webmanifest` (`display: standalone`, `start_url:
   /approvals`, `theme_color`/`background_color` from the `--bg` token) and its icons are public
   routes, classified per ADR 0014 in `web/routes_push.py`'s `ROUTE_CLASSIFICATION`: they carry no
   user data and a browser fetches a manifest without cookies. Every org page links the manifest
   and an `apple-touch-icon`. Org mode's CSP adds `manifest-src 'self'` and allows images from
   exactly `<issuer_url>/icons/`; local mode's CSP is unchanged. Unlike push, the manifest is
   served whatever the switch says.
9. **The permission prompt is the existing one.** The pre-prompt still appears only after a
   decision. In org mode, once permission is granted it subscribes the browser and posts the
   subscription; on iOS before installation it shows "Add PrivacyFence to your Home Screen to get
   notifications" instead. A click on a notification opens or focuses `/approvals`.
10. **No new dependency.** VAPID is one ES256 JWT and RFC 8291 is one ECDH, two HKDFs and one
    AES-GCM record, all in the `cryptography` package the repo already ships. The implementation is
    checked against RFC 8291's Appendix A test vector. The push request uses `requests` with a
    fixed timeout and default TLS verification against certifi, like `org_identity.py`.
11. **A subscription ends with the session that posted it, not with its expiry.** Each stored
    subscription records a SHA-256 of the sign-in session id that last posted it, never the id,
    which is a bearer credential and the page's CSRF token. `POST /logout` removes every
    subscription that session posted, whichever principal holds it: that browser stops receiving
    pushes, the same person's other devices do not. A new sign-in over an earlier session in the
    same browser (the login callback still carries the old cookie) moves the earlier session's
    subscriptions to the new one if it is the same person and removes them if not, so the next
    person on a shared browser inherits nothing even when the last one never signed out. This is
    server-side, so it holds when the page's script never ran; the sign-out form also calls
    `unsubscribe()` in the browser first, giving up after 1.5 seconds rather than hold up signing
    out. A session that merely expires (30 minutes idle, 24 hours absolute) keeps its
    subscriptions, because notifying a phone whose session has idled out is what push is for. The
    subscription API is unchanged: the session comes from the request's cookie.

**Local mode gets none of this.** It serves plain HTTP on loopback (ADR 0010), which no phone can
reach, and a desktop browser already gets tier 1 while the page is open. Adding push there would
send a third party the timing of every approval on a single-user machine for no reach gained.
Local mode mounts no manifest or subscription route, links no manifest and never subscribes; its
route set is pinned by a test.

## Alternatives considered

- **A bare tickle, with the service worker fetching the count.** ADR 0064's suggestion. The push
  service would learn the same thing (timing), but the fetch needs a live session, and org sessions
  expire after 30 minutes idle, so on a phone it would usually fail and show a generic text.
  Encrypting and padding the count reveals no more than a tickle and always works.
- **Payloads at the `standard` or `detailed` level.** Rejected: encrypted or not, the connector,
  tool or summary would then sit in the lock-screen notification of a device the org does not
  control, and org mode has no per-principal detail setting to consent with. The count is enough to
  make someone open the app, where the details are behind sign-in.
- **`pywebpush`/`py_vapid`.** Rejected: two new dependencies (plus `http_ece`) to avoid about 60
  lines over a library already audited and pinned, and `pywebpush` makes its own HTTP call, outside
  the repo's timeout and redirect conventions.
- **Accept any `https` endpoint and block private addresses.** Rejected: resolving and checking the
  address is racy (DNS rebinding) and easy to get wrong for IPv6, IPv4-mapped and link-local
  ranges. The set of push services browsers use is small and stable.
- **Only a per-person opt-out.** Not enough: an org whose policy forbids approval metadata leaving
  its infrastructure needs one switch that holds for everyone.
- **Sign-out only in the page (`unsubscribe()` plus a `DELETE`).** Not enough on its own: the
  sign-out may come from a page without the script, or the script may fail, and the server would
  keep sending. The page's `unsubscribe()` stays, as the browser's half.
- **Ending subscriptions when the session expires.** Rejected: a phone's session idles out after
  30 minutes, so push would stop for exactly the people it is for.
- **Storing the session id with the subscription.** Rejected: it is a live credential. Its hash
  identifies the session just as well, and the id's 256 random bits make the hash irreversible.
- **A push relay run by the org.** Would still end at Apple's or Google's service for the device;
  it moves the problem without removing it.

## Consequences

- A phone user can learn about an approval without the app open, once they allow notifications (and
  on iOS, once they add the app to the Home Screen).
- Apple, Google, Mozilla or Microsoft learn when each subscribed device was notified. Admins who
  cannot accept that turn push off; the setup guide lists the hosts for egress rules.
- Signing out stops pushes to that browser, and signing in as someone else there stops the previous
  person's. A browser whose session expired and that nobody signs in to again keeps receiving the
  count-only notice until the push service expires the subscription; the notice names no one and
  nothing. `OrgSessionStore.destroy_all_for` ("sign out everywhere", not yet called by any route)
  does not remove subscriptions; a route that calls it should also call
  `PushSubscriptionStore.remove_session` for each session it ends. Sessions are in memory, so a
  server restart ends them without a sign-out, and subscriptions stay until the next sign-in over
  them.
- Deleting `org/web_push_vapid_key.pem` invalidates every subscription; browsers resubscribe on the
  next visit to `/approvals`, where the page replaces a subscription made for another key.
- A new browser push service needs a code change to the allowlist.

## Verification

- `tests/unit/test_web_push.py`: the RFC 8291 vector; that what reaches the push service decrypts to
  `minimal_payload` and that no approval field appears in the request or the log; equal ciphertext
  sizes for different counts; the endpoint allowlist; 404/410 cleanup; the rate limit.
- `tests/unit/web/test_routes_push.py`: route classification, subscription auth (session, CSRF,
  Origin), the switch off, local mode's route set unchanged, and sign-out removing that browser's
  subscriptions only, a later sign-in by someone else inheriting none, and sign-out with push off.
- `tests/integration/test_browser_smoke.py`'s `TestInstallableOrgApp` and `TestWebPushSubscription`:
  Chromium loads and parses the manifest under the org CSP; the pre-prompt subscribes and the server
  stores it; the sign-out form unsubscribes the browser; the iOS hint.
- A real delivery to an iPhone (installed) and to Android Chrome is a manual release check.

## Related

- [ADR 0064](0064-browser-notifications-stay-on-the-machine.md): tiers 0–1, which this leaves as
  they are.
- [ADR 0010](0010-local-mode-serves-plain-http-on-localhost.md): why local mode has no phone reach.
- [ADR 0014](0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md): route
  classification.
- `src/privacyfence/web_push.py`, `src/privacyfence/web/routes_push.py`,
  `src/privacyfence/resources/sw.js`, `docs/org-mode-setup-guide.md` ("Notifications on a phone").
