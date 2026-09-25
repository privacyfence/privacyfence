# Org mode on mobile — plan

**Status:** proposed, 2026-09-25. Nothing here is implemented. This is a plan document per
[`adr/README.md`](adr/README.md): it is deleted when its work lands, and the decisions below that
meet the ADR bar (the framework choice and its rejected alternatives, the push-notification trust
question) are extracted into ADRs first.

## Goal

A signed-in org-mode user can do everything they would do at a desk from a phone browser: sign in
through the IdP, see and decide their pending approvals (including passkey step-up), manage their
connections and passkeys, read their audit log, and, as an admin, edit settings. They also learn
about a new approval without keeping a tab open.

Local mode renders the same pages ([ADR 0033](adr/0033-one-route-layer-per-surface-with-an-auth-adapter-per-mode.md)),
so it gets every layout fix for free. It gets no mobile *reach*, though: local mode serves plain
HTTP on loopback ([ADR 0010](adr/0010-local-mode-serves-plain-http-on-localhost.md)), and a phone
cannot open `127.0.0.1` on a laptop. For local mode the fixes only improve narrow desktop windows.
Getting a local-mode approval onto a phone is [ADR 0005](adr/0005-moving-the-approval-decision-off-the-device.md)'s
question, and this plan does not answer it.

## What is broken today (measured)

Every org page was loaded in the existing Playwright phone emulation
(`tests/integration/test_browser_smoke.py`'s `_MOBILE_EMULATION`: 393×852, `is_mobile`, touch)
against the `org_server` fixture, signed in as an admin.

| Surface | Source | State on a phone |
|---|---|---|
| Shell header/nav | `web_shell.py` | Usable. Has viewport meta and a 480px breakpoint. The nav wraps to two rows. |
| Approval list | `approval_list_html.py` | Usable. It has a 560px breakpoint, 44px row targets, and existing mobile tests. |
| Approval card / PII dialog | `approval_window_html.py`, `dialog_window_html.py`, `resources/approval_window/styles.css` | Usable. It has a 700px breakpoint and is covered by `TestMobileLayoutViewport`/`TestResponsiveLayout`. |
| **Settings** (all sections) | `settings_window_html.py` | **Broken.** The fixed 190px `.pf-nav` sidebar takes about half the width and the content column is squeezed to about 110px: "Approval Notifications" wraps one word per line, the Enable button is clipped off-screen, and toggles overflow their cards. **Privacy Filter** is worse: the 190px nav and the 170px `.pf-subnav` together leave the detail pane about 0px wide, so the admin sees two menus and no editor. The audit row reads "Nothing logg…". No `@media` rule exists for the layout, only for dark mode. |
| Passkeys | `web/routes_security.py` | Usable. It is simple flow content. |
| Connections | `web/routes_connect.py` | Not measured: the browser fixture's bundle configures no services, so the page 404s. The CSS is a 640px-max column with inline `display:flex` rows, so it probably works but needs verifying, especially the Telegram phone/code form. |
| Bare fallback pages | `web/routes_approvals.py:507,522`, `web/session_auth.py:461` | Have no viewport meta, so they render in the ~980px fallback viewport at about 40% scale. |
| Notifications | `web_shell.py` `_STREAM_JS`, `resources/sw.js` | Tiers 0–1 only: title badge and `showNotification` while a tab is open. There is no web push (`sw.js` says tier 2 is "org-mode/P7+"). **On a phone this means no notifications at all**, because mobile browsers suspend background tabs, and iOS delivers web push only to a site installed to the Home Screen, which needs a manifest (none exists). |

### Review cards with documents (measured separately)

The review card layout holds up on a phone: it has no overflow, and Deny/Allow stay pinned at the
bottom. The *content* inside it does not always hold up. Each case below is a WIDE-layout read card
in the same phone emulation:

| Preview kind | Source | State on a phone |
|---|---|---|
| **PDF** (Drive `drive_get_file_content` and similar) | `approval_window_html.build_preview_body_html`: `<embed src="data:application/pdf;base64,…">` | **Broken, and it cannot be fixed with CSS.** In Chromium the built-in viewer's toolbar fills the ~380px pane (a hamburger icon and the literal text "pdf;bas…") and no page of the document is visible. On real devices it is worse: **Android Chrome has no inline PDF viewer**, so `<embed>`/`<iframe>` of a PDF renders blank or as an "Open" button, and iOS Safari renders only a static first page that cannot be scrolled. The reviewer is asked to approve a document they cannot read. |
| Image | same, `<img style="max-width:100%">` | Fine. |
| Markdown (DOCX/PPTX/XLSX extraction, HTML mail) | `_markdown_block_html` | Readable. Wide tables squeeze their columns rather than overflow. |
| Record/list tables (Salesforce, Contacts, Telegram) | `_table_html` | Readable but poor. Five columns at 393px break words mid-word ("Anders/on", "alice.anderson@example.co/m", "V/P"). A 10–15-field Salesforce record would be unreadable. |

What to do about each:

- **PDF: render it on the server, not in the browser plugin.** On phone widths (and in every
  container narrower than about 600px), replace the `<embed>` with page images: the first *N*
  pages rasterised server-side to PNG and inlined as `data:` URIs, the same path the image preview
  already takes. `pypdfium2` is the candidate library: permissively licensed, wheels for every
  platform we build, no system dependency. Always show a "page 1–N of M" note, because the
  reviewer must be able to tell that they saw part of the document, not all of it. Two
  alternatives were considered and rejected. **pdf.js** is a large runtime JavaScript bundle that
  would have to be served and nonced, and would also load in the native windows; it goes against
  constraint 3. **"Open PDF in a new tab"** needs a new authenticated route for the raw bytes,
  which is a new place card content is served from, and on Android it still hands the file to
  another app. The desktop `<embed>` can stay as is above that width. This is a new server-side
  parser of attacker-supplied files (a PDF is whatever the connector returned), so it runs with
  a page and time limit and inside the same failure handling as `text_extraction.py`. That point
  goes into the ADR.
- **Tables:** below a container width breakpoint, render each row as a stacked label/value block
  (the standard responsive-table pattern, which Tailwind container variants express directly).
  Change `word-break` from breaking anywhere to `overflow-wrap: anywhere`, so text only breaks
  mid-word when a single word cannot fit on a line (an email address, say).
- **Staged download links** (`GET /downloads/{token}`, see [`org-mode-download-delivery.md`](org-mode-download-delivery.md)):
  signed out, the route redirects to a bare `/login` (`web/routes_downloads.py`) with no `next`,
  so after IdP sign-in the user lands on `/approvals` and the link is gone. On a desktop browser
  that already has a session this is rare. On a phone it is the normal case, because a link
  tapped inside an AI client app opens an in-app browser with its own empty cookie jar, and the
  300s TTL runs out while the user signs in. Fix: pass the token path as `next` through
  `routes_org_identity`'s allow-listed redirect. The token is still one-time and bound to the
  principal, so the redirect grants nothing new. Then verify on iOS and Android that the
  `Content-Disposition: attachment` response lands in Files/Downloads.

About 10–30 elements per settings section are under 32px tall. Touch targets need the same 44px
treatment the approval list already has.

The core problem is layout architecture, not missing breakpoints. Each surface hand-writes its own
flex/width rules in Python string literals and JavaScript string builders. The responsive fixes so
far (list, card, dialog) were each a separate, later patch, and the one surface without a patch is
the one nobody opened on a phone. Adding a fourth set of hand-written `@media` blocks to
`settings_window_html.py` would fix today's bug and leave the pattern that caused it.

## Constraints on any framework

These rule candidates in or out, so they come before the choice:

1. **CSP:** `default-src 'none'`, with `<style>`/`<script>` elements allowed only with the
   per-response nonce ([`web/csp.py`](../src/privacyfence/web/csp.py)). No CDN and no third-party
   origin. Any framework must ship as static CSS or JS inside the Python package.
2. **The same document must also render without HTTP.** The native settings and approval windows
   load these documents with `loadHTMLString_baseURL_` (see `web_shell.py`'s docstring), and card
   documents are rendered once and reused. Styles must therefore be *inlined* into the document,
   not linked, so the framework's shipped CSS must be small.
3. **No frontend build exists for these pages.** Markup lives in Python f-strings and JavaScript
   string concatenation. Node is already in the toolchain (`mcpb/shim`, pinned exact versions), so
   one pinned build step is acceptable, but runtime JavaScript frameworks (React, Vue, Lit) would
   mean rewriting the rendering model, and nothing here needs that.
4. **One component, several containers.** An approval card renders full-screen on a phone, in a
   fixed-size native window, and inline in an expanded list row. Its layout should respond to the
   width of its *container*, not the viewport. That is the exact case the current viewport-only
   `@media (max-width: 700px)` rules get wrong in the inline-row context.
5. **Existing look and design tokens stay.** `resources/tokens.css` (light/dark palette, radii)
   is the source of truth. A framework that restyles everything into its own visual language is a
   redesign, not a mobile fix.

## Framework decision

### Recommended: Tailwind CSS v4, utilities-only, compiled at build time

- **Zero runtime.** The output is one static CSS file with no JavaScript, so it fits the nonce-only
  CSP unchanged.
- **Small enough to inline.** The compiler scans source files for class names and emits only the
  ones used. It scans any text file, so it reads the `.py` modules directly (`@source "../src/privacyfence"`).
  The expected output is in the low tens of KB, and it can be embedded exactly the way
  `tokens.css` is today (constraint 2).
- **Container queries are built in** (`@container`, `@sm:`/`@md:` variants), which handles
  constraint 4 without hand-written rules.
- **It keeps the existing tokens.** v4's `@theme` block is plain CSS custom properties, so the
  theme maps onto `tokens.css`'s `--color-*`/`--radius-*` (constraint 5), and dark mode continues
  to come from `tokens.css`'s own `prefers-color-scheme` block.
- **Incremental adoption.** Importing only the `utilities` layer, with no Preflight reset, leaves
  every existing page pixel-identical until it is migrated, so each surface can move in its own
  small PR.
- **One pinned build step.** Either `@tailwindcss/cli` at an exact version in a new
  `web-ui/package.json` next to `mcpb/shim`, or the standalone CLI binary pinned by version and
  SHA-256, the same way [ADR 0046](adr/0046-release-ci-pins-codesigntool-by-version-and-sha256.md)
  pins CodeSignTool.

Its one real cost: a class name must appear as a complete literal somewhere in the source. A class
assembled from pieces (`'pf-' + kind`, or `"p-" + str(n)`) is never emitted. The JavaScript string
builders in `settings_window_html.py` mostly use literal classes already, and a unit test that
greps for concatenated class names (below) enforces the rule.

### Alternatives considered

| Option | Why not |
|---|---|
| **Bootstrap 5.3** (vendored, no build) | It has ready-made pieces this plan needs (an offcanvas drawer and a responsive grid) and no build step. But the compiled CSS is about 230KB, too much to inline in every card and settings document (constraint 2); trimming it requires a Sass build, which gives up the no-build advantage. Its visual language would also have to be overridden everywhere to match `tokens.css` (constraint 5), and it has no container-query grid. |
| **Pico CSS** (classless) | Small, with good defaults on semantic HTML. But it styles elements, not layouts: it would not collapse the settings two-pane or three-pane layout, which is the actual bug. It would also restyle every button and input on every page. |
| **Web component kits** (Shoelace/Web Awesome, Ionic) | Runtime JavaScript custom elements that have to be served, nonced and loaded in the native windows too. They fix components we don't have a problem with, and Ionic's app shell is a rewrite. |
| **Open Props + "Every Layout" primitives** | Well regarded, but in practice it is a set of documented patterns we would copy in and maintain ourselves. That is the custom implementation this plan was asked to avoid. |
| **Keep hand-written `@media` rules** | Rejected, for the reason given in "What is broken today". |

## Beyond layout: what "works on mobile" also needs

1. **Being told about an approval (web push, tier 2).** Without it, a phone user only finds an
   approval by opening the page. Scope: a Web App Manifest plus icons so the site can be installed
   (iOS requires installation for push), VAPID keys generated per org install and stored under
   `authority/`-equivalent org state, a per-principal subscription store, and a `push` handler in
   `sw.js`. Push payloads use `notifications_detail`'s `minimal` level at most ("1 approval
   waiting"), never content. Push goes through Apple's and Google's servers, which is the first
   time approval metadata leaves the org server, so this part needs its own ADR before it ships.
2. **Passkey step-up on the phone.** WebAuthn works in mobile Safari and Chrome with the org
   hostname as the RP ID. Verify it end to end on a real iPhone and a real Android device,
   including the IdP step-up fallback (`web/routes_org_stepup.py`) and a phone with no passkey
   enrolled yet.
3. **Links opened inside AI client apps.** An approval URL tapped in the Claude iOS/Android app may
   open in an in-app browser whose cookie jar is not Safari's or Chrome's. The user is sent through
   IdP sign-in again, and passkey behaviour in that view needs checking. Verify, then either
   document it or add a "Open in browser" hint on the sign-in bounce.
4. **Touch-only interaction.** Nothing may depend on `:hover` or `title=` tooltips. There are only
   a few (3 `:hover` rules, 7 `title=` attributes across the three renderers); each needs a visible
   or tap equivalent.

## Phases

Each phase is one PR into `main` with a `CHANGELOG.md` `[Unreleased]` line. Phases 1, 2, 4 and 5
are layout. Phase 3 is the most urgent, because it is the one where a reviewer approves something
they could not read, and it does not depend on the framework, so it can go first. Phases 6–7 are
independent of the rest.

1. **Toolchain.** Add the pinned Tailwind build: `web-ui/tailwind.css` (the `@theme` mapped to
   `tokens.css`, the utilities layer only, `@source` on `src/privacyfence`) compiled to
   `src/privacyfence/resources/ui.css`, committed. Add a CI check that rebuilding produces no diff
   (the same pattern as a lockfile check), and a `MANIFEST.in`/package-data entry. Embed it
   next to `tokens.css` in `web_shell.py`, `settings_window_html.py`, `approval_window_html.py` and
   `dialog_window_html.py`. No visual change.
2. **Settings (the actual bug).** Below the `md` breakpoint, `.pf-nav` becomes a horizontal
   scrollable tab strip, or a `<details>`-based section picker; either needs no JavaScript beyond
   the existing `data-nav` handler. The Privacy Filter `.pf-subnav` collapses the same way, and
   the detail pane gets the full width. Card rows (`.pf-card-row`) stack label above control, and
   all controls get 44px targets. Remove the matching hand-written width rules as each one is
   replaced.
3. **Review content on phones.** PDF page-image rendering, stacked tables, and the download-link
   `next` fix, as described in "Review cards with documents" (adds an ADR for the server-side PDF
   rasteriser).
4. **Card and dialog on container queries.** Replace the `@media (max-width: 700px)` blocks in
   `styles.css`, `approval_window_html.py` and `dialog_window_html.py` with `@container` variants
   on the card root, so the inline-row rendering also stacks correctly.
5. **Everything else.** Move the shell, the approval list, `/connect` (including the Telegram form)
   and `/security` onto the same utilities. Add viewport meta to the three bare fallback pages.
   Replace hover-only affordances.
6. **Installable app plus web push.** Manifest, icons, VAPID, subscription store and `sw.js` push
   handler, as described in "Beyond layout" item 1. This phase adds an ADR for the
   off-server notification channel. Org mode only; local mode keeps tiers 0–1.
7. **Real-device verification.** Items 2 and 3 of "Beyond layout" on physical iOS and Android.
   Add the result to `release-testing.md`'s manual checklist.

## Tests

- Extend `TestMobileLayoutViewport` in `tests/integration/test_browser_smoke.py`, parametrized over
  every org route (`/approvals`, `/approvals/{id}`, `/settings` with each section, `/settings/privacy`,
  `/connect`, `/security`), both admin and non-admin, plus a WIDE read card for each preview kind
  (PDF, image, Markdown, a 10-column record table). Assert no horizontal overflow; assert the
  main content element is at least 90% of the viewport width (this catches the Privacy Filter bug,
  which the overflow check alone misses because nothing overflows); assert every visible
  interactive element is at least 44×44 CSS px.
- Give the `org_server` fixture a bundle with at least one configured service so `/connect`
  renders instead of 404ing.
- Add a unit test that fails when a Python or JavaScript source builds a class attribute by
  concatenating strings.
- Add the CI rebuild-no-diff check from phase 1.
- Only Chromium is available to CI today. iOS Safari runs WebKit, so phase 6's manual pass is
  the only WebKit coverage until Playwright WebKit is added to the browser test job. That is worth
  doing, but it is a separate decision about CI cost.

## Decisions to extract into ADRs when this plan retires

- Tailwind v4, utilities-only, inlined, instead of Bootstrap, Pico, web components, Open Props or
  hand-written CSS (constraints 1–5 above).
- Container queries, not viewport media queries, for components rendered in more than one host.
- Server-side PDF rasterisation for review on narrow screens, instead of pdf.js or an
  open-in-new-tab route.
- Web push as an off-server notification channel for org mode: its payload limit, and that local
  mode does not get it.
