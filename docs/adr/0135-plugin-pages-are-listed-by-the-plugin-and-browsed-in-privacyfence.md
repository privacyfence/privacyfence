# ADR 0135: A plugin lists its pages, and PrivacyFence serves the page browser

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/plugins/page_index.py`,
`src/privacyfence/plugins/host.py` (`list_pages`, `list_all_pages`),
`src/privacyfence/web/routes_plugin_browser.py`, `src/privacyfence/plugin_browser_html.py`,
`plugin-sdk/src/privacyfence_plugin_sdk/_page_index.py`.
Amends [ADR 0124](0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md).

## Context

A plugin can have many pages. [ADR 0124](0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md)
serves each in a sandbox without `allow-same-origin`, so a link from one plugin page to another
carries no session cookie and gets the owner-only 404. Until now the owner reached only a plugin's
home page, from its card in Settings, or by typing a URL. Something outside the sandbox has to list
the pages and link into them.

## Decision

- Protocol 1.2 adds `pages.list`, a daemon-to-plugin request. The plugin answers with a list of
  entries (path, title, and optionally version, creation and update times, description), at most
  500. Only a running plugin whose manifest has `pages: true` is asked. A plugin that does not
  implement it answers `method_not_found` and is listed with one page, `/`.
- PrivacyFence serves a page browser at `/plugin-pages` (every running plugin with pages) and
  `/plugin-pages/<name>` (one plugin). It is owner-session-only, same-origin and carries the app's
  content security policy. Each entry opens `/plugins/<name><path>` in a new tab.
- The plugin sandbox, cookie and subresource rules of ADR 0124 are unchanged.

## Alternatives considered

- **A same-origin bounce plus `allow-popups allow-popups-to-escape-sandbox` on plugin pages.**
  Rejected. Any site, and any plugin page, could make the owner's browser load any plugin page as
  the owner, which is effectively `SameSite=Lax` for plugin pages; script in a plugin page could
  open any URL in an unsandboxed tab. For a plugin that serves HTML an AI wrote, that is an
  exfiltration and phishing surface.
- **`SameSite=Lax` for the session cookie.** Rejected. It weakens every Settings and approvals
  route.
- **The plugin serves its own browser page.** Rejected. Links from a sandboxed page carry no cookie
  (ADR 0124), and fixing that is the bounce above.
- **Serving a plugin's images and CSS as separate URLs.** Rejected, unchanged: a sandboxed page's
  subresource requests carry no cookie, so a plugin inlines them.
- **A page index in the manifest.** Rejected. The manifest is reviewed and hashed at enable time,
  so a plugin whose pages change at runtime would need re-review for every new page.

## Consequences

- One extra request per plugin per browser view (10 seconds timeout each, sent concurrently).
- A plugin's page list is not reviewed at enable time; the owner sees titles the plugin chose.
- The browser is the place that links into plugin pages; a plugin lists its pages instead of
  linking them.

## Verification

The unit tests for the protocol types, the host page index, the SDK page index and the browser
routes, and the test-host conformance scenario.

## Related

- [Issue 846](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0124](0124-plugin-pages-are-get-only-owner-only-and-sandboxed.md)
- [ADR 0126](0126-the-plugin-sdk-lives-in-this-repository-and-is-published-from-the-same-tag.md)
