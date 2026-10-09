# ADR 0140: A plugin page opens links in new tabs only when its manifest says so

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/plugins/pages.py` (`CSP_NEW_TABS`),
`src/privacyfence/plugins/manifest.py`, `src/privacyfence/plugins/host.py` (`page_new_tabs`),
`src/privacyfence/web/csp.py`, `src/privacyfence/web/routes_plugins.py`,
`src/privacyfence/web/server.py` (`_SecurityHeadersMiddleware`), `src/privacyfence/settings_window_html.py`.
Amends [ADR 0124](0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md).

## Context

A plugin can show its owner an HTML dashboard that an AI built: a self-contained page of several
hundred kilobytes, with inline script and style, JSON in a `<script type="application/json">`,
`data:` fonts and images, and no network requests. Its links to Jira, Salesforce and reports use
`target="_blank"`.

Two questions followed from [ADR 0124](0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md).

1. **Links.** A sandboxed top-level page may navigate its own tab, so same-tab links already work.
   A `target="_blank"` link does nothing, because the sandbox has no `allow-popups`. The dashboards
   need new tabs.
2. **The policy.** Whether the content security policy already lets such a page run was never
   written down or tested in a browser. It does, because every fetch directive falls back to
   `default-src 'self' data: 'unsafe-inline'`:

   | Need of a dashboard | Allowed by | Result |
   |---|---|---|
   | Inline `<script>` | `'unsafe-inline'` | allowed |
   | `<script type="application/json">` | not executed, so no directive applies | allowed |
   | Inline `<style>` and `style="…"` | `'unsafe-inline'` | allowed |
   | `data:` images | `data:` | allowed |
   | `data:` fonts | `data:` | allowed |
   | `eval`, `new Function` | no `'unsafe-eval'` | blocked |
   | `blob:` URLs, external hosts | not listed | blocked |
   | File downloads (`<a download>`) | the sandbox has no `allow-downloads` | blocked |

## Decision

1. **Same-tab links need no change.** The policy needs no change either; both are now stated in
   `docs/plugin-protocol.md` and covered by real-browser tests.
2. **`page_new_tabs`.** A new optional manifest key, a boolean defaulting to `false`, and an error
   without `pages: true`. A running plugin that sets it has its pages served with
   `CSP_NEW_TABS`, the plain policy with `allow-popups allow-popups-to-escape-sandbox` added to the
   sandbox. `CSP` and `CSP_EMBEDDED` are unchanged. The page an approval card frames is never given
   popups: the embedded policy wins. The decision is made per request by `plugin_new_tabs_for`, which
   the plugin page route sets after the owner and name checks and the middleware reads; the
   `/plugins/<name>` redirect never sets it.
3. **The owner decides at review.** The manifest key is part of the manifest hash, so a plugin that adds
   it must be enabled again, and the Review and enable dialog says "Its pages can open links in new
   tabs." with "A tab opened that way is outside PrivacyFence's sandbox."

## Alternatives considered

- **`allow-popups allow-popups-to-escape-sandbox` for every plugin page.** It loosens the sandbox for
  plugins that never link out, and a plugin that shows AI-written HTML is where the owner should
  decide. Rejected for the per-plugin key.
- **`allow-popups` without `allow-popups-to-escape-sandbox`.** The new tab inherits the sandbox: Jira
  and Salesforce would run in an opaque origin without their own cookies or storage, and a target
  that sends `Cross-Origin-Opener-Policy` would fail to load in a sandboxed popup.
- **`allow-top-navigation` or `allow-same-origin`.** Not needed (a top-level page navigates itself),
  or back in the app's origin (ADR 0124).
- **Rewriting the page's links in the daemon** (adding `rel="noopener"`, turning `_blank` into `_top`).
  The daemon does not parse or rewrite plugin HTML, and the opener policy below already severs the
  opener.
- **A PrivacyFence "leaving" interstitial.** The new tab carries no PrivacyFence session anyway, and
  the review dialog states the capability. Not worth a route.

## Consequences

The risks, weighed:

- **An escaped popup is an unsandboxed top-level tab.** It can be any URL, PrivacyFence's own
  included. Its navigation is initiated by the opaque-origin plugin page, so it is cross-site, and
  the `SameSite=Strict` session cookie is not sent: a popup to `/settings`, `/approvals/<id>` or
  another plugin page gets the owner-only 404 or the sign-in page. Every GET route either needs that
  cookie or is a capability URL whose token the page cannot know, and no GET route changes state. A
  same-tab navigation already reached the same URLs the same way, so popups add no GET-CSRF reach.
- **`window.opener`.** `Cross-Origin-Opener-Policy: same-origin` and the opaque origin put the popup in
  a new browsing-context group: its `window.opener` is `null` and the plugin page's handle to it is
  closed. It cannot navigate the plugin tab, and the plugin page cannot script it.
  `Referrer-Policy: no-referrer` keeps the plugin page's URL out of the new tab's `document.referrer`.
- **Popups without a user gesture.** `allow-popups` does not require a click; the browser's popup
  blocker does. PrivacyFence adds nothing here, and the test browser runs with the blocker off, so the
  blocked case is a manual check.
- **Phishing look-alikes.** A page could open a look-alike sign-in page in a new tab; it could already
  navigate its own tab there. The tab shows its real URL, and the dialog names the capability.
  Accepted.
- **Exfiltration.** A plugin page can already send what it shows to any host by navigating its own tab
  (navigation is not governed by the policy). Popups do not widen that. The sandbox is not a data-loss
  boundary for page content.
- **Per-plugin review.** A plugin author asks for the capability, and the owner sees it for each plugin.
- **Still blocked.** `eval` and `new Function` (a charting library that needs them does not run) and
  file downloads (`<a download>`) stay blocked. Each is a separate decision.

## Verification

- `tests/integration/test_plugin_pages_browser.py` in real Chromium: `TestCspAllowsSelfContainedPages`
  (inline script and style, `style` attributes, `data:` images and fonts, a blocked font host reported),
  and `TestNewTabs` (a same-tab link to an external site loads; a `_blank` link does nothing without
  the key; with it, the new tab opens with `window.opener === null` and an empty referrer and the
  plugin tab is untouched; a new tab into PrivacyFence gets the 404 and the plugin is never asked).
- `tests/unit/web/test_routes_plugins.py` (which policy each case gets, embed winning, a non-owner
  never reaching the host) and `tests/unit/web/test_server.py` (the middleware keeps the opener and
  referrer headers on the new policy).
- `tests/unit/plugins/test_manifest.py`, `test_pages.py`, `test_host.py` and `tests/unit/web/test_csp.py`.
- `scripts/plugin_page_preview.py` opens any HTML file, or a built-in check page, as a plugin page in the
  owner's own browser, for the checks the test browser cannot make (Firefox, Safari, the popup blocker).

## Related

- [Issue 846](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0124](0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md)
- [ADR 0139](0139-plugin-pages-are-listed-by-the-plugin-and-browsed-in-privacyfence.md)
- [ADR 0127](0127-a-plugin-approval-binds-to-its-content-digest-and-persists-until-revoked.md)
