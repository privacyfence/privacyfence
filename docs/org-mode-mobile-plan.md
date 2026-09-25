# Org mode on mobile — plan

**Status:** proposed, 2026-09-25. Nothing here is implemented. This is a plan document per
[`adr/README.md`](adr/README.md): it is deleted when its work lands, and the decisions below that
meet the ADR bar (the framework choice and its rejected alternatives, the push-notification trust
question) are extracted into ADRs first.

**To run it:** start a Claude Code session with `/implement <GitHub URL of this file>`. If that
session's checkout has no `/implement` command yet (it is added on the same branch as this plan),
start it with `implement <URL>` instead: the session should read `.claude/commands/implement.md`
from the same ref as this file and follow it.

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

The work lands as **one pull request** built from phase branches. Run it with
`/implement <URL of this file>` (see [`.claude/commands/implement.md`](../.claude/commands/implement.md)):
an orchestrator session starts one child session per phase, merges each finished phase into the
feature branch, and opens the single PR to `main` at the end. The manifest below is what it runs.
The prose above is the context each child reads first.

Order and parallelism, from the manifest's `depends_on`:

```
wave 1   p1-toolchain        (framework, test harness, xfail baseline)
wave 2   p2-settings    ∥    p3-review-content
wave 3   p4-card-containers ∥ p5-remaining-pages
wave 4   p6-push
wave 5   p7-retire           (ADRs checked, real-device checklist, plan deleted)
```

Phases in the same wave touch disjoint files. The one shared file is `CHANGELOG.md`, and
`[Unreleased]` additions merge mechanically.

**How the phases prove they are done.** p1 adds the phone-layout test harness and marks every case
that fails today `xfail(strict=True, reason="<phase id>")`, naming the phase that owns the fix. A
phase is done when it has removed every xfail carrying its own id and those tests pass. Because
the xfails are strict, a phase cannot fix another phase's case by accident without the suite
noticing. By p7 no xfail from this plan may remain.

**ADR numbers are pre-assigned**, so parallel phases do not collide: 0047 (p1), 0048 (p3),
0049 (p4), 0050 (p6). If `main` has taken a number by the time the final PR opens, the
orchestrator renumbers in its last merge of `main`.

## Implementation manifest

```yaml
plan_slug: org-mode-mobile
feature_branch: feature/org-mode-mobile
max_parallel: 2
verify_after_merge:
  - python3 -m pytest tests/integration/test_browser_smoke.py -q
  - python3 scripts/check_ui_css_fresh.py        # exists from p1 on
final_checks:
  - "grep -rn 'reason=\"p[1-7]-' tests/ returns nothing: no xfail this plan added is left"
  - docs/org-mode-mobile-plan.md is deleted, and docs/README.md no longer points to it
  - ADRs 0047-0050 exist, are listed in docs/adr/README.md, and are Accepted
  - CHANGELOG.md has [Unreleased] entries for every user-visible change and no version heading
manual:
  - "Real iPhone (Safari and the installed Home Screen app) and real Android Chrome: sign in through the IdP, approve a write with passkey step-up, approve with the IdP step-up fallback, read a PDF review card, open a staged download link from inside the Claude app, receive a push notification"
  - "Subjective look in light and dark at phone width (qa_web_smoke.py), per docs/release-testing.md"

phases:
  - id: p1-toolchain
    title: Tailwind build, phone test harness, xfail baseline
    depends_on: []
    brief: |
      1. Add a pinned Tailwind CSS v4 build. Put `web-ui/package.json` next to `mcpb/shim`,
         with `@tailwindcss/cli` at an exact version (no ^ or ~) and a committed
         package-lock.json. The entry file is `web-ui/tailwind.css`:
         `@import "tailwindcss/theme.css" layer(theme); @import "tailwindcss/utilities.css" layer(utilities);`
         No Preflight. An `@theme` block maps colours, radii and the spacing base onto the
         existing `resources/tokens.css` variables, with `--color-*: initial` so no default
         Tailwind palette leaks in. Point `@source` at `../src/privacyfence`. The compiled output
         is `src/privacyfence/resources/ui.css`, minified and committed.
      2. Add `scripts/check_ui_css_fresh.py`: rebuild into a temporary file, diff it against the
         committed file, exit 1 on any difference. Wire it into tests.yml's lint job, which needs
         Node (it already has it for the shim), and make sure the package data ships `ui.css`
         (check MANIFEST.in and pyproject's package-data).
      3. Embed ui.css immediately after tokens.css wherever tokens.css is embedded today:
         web_shell.py, settings_window_html.py, approval_window_html.py, dialog_window_html.py.
         Use the same read-once module constant, in the same nonce'd <style>.
      4. Cascade-layer gotcha, so it is documented once: Tailwind v4's utilities live in
         `@layer utilities`, and **unlayered** CSS beats any layered rule regardless of
         specificity. A utility class therefore cannot override an existing hand-written rule; a
         migrating phase must delete or narrow the hand-written rule it replaces. Write this in
         ADR 0047 and as a comment at the top of web-ui/tailwind.css.
      5. Find out whether any native WKWebView/WebView2 host still loads these documents. The
         settings_controller.py docstring says the native settings window was retired after P9.
         If one does, check that Tailwind v4's browser floor (Safari 16.4) fits
         docs/platform-support.md's macOS 13 minimum. If it does not, stop and report status=blocked.
      6. Test harness in tests/integration/test_browser_smoke.py. Build a phone context from the
         existing `_MOBILE_EMULATION`. Give the `org_server` fixture's bundle at least one
         configured connector service, so /connect renders instead of 404ing. Add parametrized
         tests over: /approvals; /settings for each section, both admin and non-admin;
         /settings/privacy; /connect; /security; the three bare fallback pages
         (routes_approvals.py:507/522, session_auth.py:461); and a WIDE read card for each preview
         kind (PDF, image, Markdown, a 10-column record table), both full-page and expanded
         inline in an /approvals list row. Assertions: (a) no horizontal document overflow;
         (b) the main content region is at least 90% of the viewport width; (c) every visible
         interactive element is at least 44x44 CSS px; (d) viewport meta is in effect
         (innerWidth == 393); (e) for the PDF card, the preview shows document pixels, i.e. an
         <img> of a rendered page, not an <embed>; (f) for table cards, no single word is broken
         across lines. Every case that fails today gets `xfail(strict=True, reason="<owning
         phase id>")`, assigned like this: settings → p2-settings; PDF, table and download →
         p3-review-content; the card inline in a list row → p4-card-containers; shell, list,
         connect, security and fallback pages → p5-remaining-pages. A case that already passes
         gets no marker.
      7. Unit test: fail when a class attribute is built by string concatenation in
         src/privacyfence (Python f-string or JS `+` inside a class="..." value) outside an
         explicit allow-list. Seed the allow-list with today's offenders, each naming the phase
         that will clean it up.
      8. Write ADR 0047 (Tailwind v4, utilities-only, compiled and inlined), using this plan's
         "Constraints" and "Alternatives considered" sections.
      This phase changes nothing visually. Screenshots before and after of /settings,
      /approvals and a card at desktop width must be identical.
    acceptance:
      - web-ui builds reproducibly and check_ui_css_fresh.py passes in CI's lint job
      - the new browser tests all pass or xfail strictly with an owning phase id
      - no visual change at desktop width (screenshots in the report)
      - ADR 0047 is written and listed in docs/adr/README.md

  - id: p2-settings
    title: Settings usable at phone width
    depends_on: [p1-toolchain]
    brief: |
      Fix settings_window_html.py, both the org and the local rendering. Below Tailwind's `md`
      breakpoint, `.pf-nav` becomes a horizontally scrollable tab strip above the content, and
      the Privacy Filter `.pf-subnav` becomes a second strip (or a native <select>), so the
      detail pane gets the full width. Keep the existing `data-nav`/`data-privacy-nav` click
      handling and the role=tablist ARIA; add no new JS framework. `.pf-card-row` stacks the
      label above its control at narrow widths. Every control gets a 44px target on coarse
      pointers (`pointer-coarse:` variant). Replace the hand-written rules you are replacing,
      because of the cascade-layer note in ADR 0047. Remove every xfail with
      reason="p2-settings", and the p2 entries from the class-concatenation allow-list.
    acceptance:
      - all p2-settings xfails removed and passing
      - desktop-width settings screenshots are unchanged, or any change is intended and noted
      - phone screenshots of each settings section and Privacy Filter in the report

  - id: p3-review-content
    title: PDF, tables and download links readable on a phone
    depends_on: [p1-toolchain]
    brief: |
      See "Review cards with documents" above.
      1. PDF. Add `pypdfium2` (exact pin) to pyproject and regenerate the locks with
         scripts/update_dependency_locks.sh. Where card_builder.py builds pdf_data_uri, also
         rasterise the first N pages (N=5, configurable, width about 1000px, PNG) in a helper
         module with a page limit, a time limit and a byte limit, and the same failure handling
         text_extraction.py uses: if rendering fails, the card falls back to the text
         extraction, never to nothing. build_preview_body_html emits both: the <embed> inside a
         container shown only at `@md` container width and up, and the page images plus a
         "Showing pages 1–N of M" note otherwise. The card stays self-contained (data: URIs),
         so CSP needs no change. Check the card's size budget and say in the report how big a
         5-page card gets.
      2. Tables. _table_html becomes stacked label/value blocks below the `@md` container
         width. Use `overflow-wrap:anywhere` instead of breaking at every character.
      3. Downloads. When GET /downloads/{token} finds no session, redirect to
         `/login?next=/downloads/<token>` through the existing _safe_next_path allow-list. Add a
         unit test that the claim still requires the same principal after sign-in, and a test
         that `next` cannot be turned into an open redirect.
      4. Write ADR 0048: server-side rasterisation, rejecting pdf.js and an open-in-new-tab
         route. Include the threat note that this is a new parser of untrusted input.
      Remove every xfail with reason="p3-review-content".
    acceptance:
      - all p3-review-content xfails removed and passing
      - a malformed or encrypted PDF falls back to text, with a unit test
      - dependency locks regenerated and committed; bandit clean
      - ADR 0048 written

  - id: p4-card-containers
    title: Approval card and dialogs on container queries
    depends_on: [p1-toolchain, p3-review-content]
    brief: |
      Replace the viewport `@media (max-width: 700px)` layout rules in
      resources/approval_window/styles.css, approval_window_html.py and dialog_window_html.py
      with container queries. Make the card root `@container`, and use `@md:`-style variants or
      `@container` rules in ui.css's source. The card must stack correctly when it is inline in
      an expanded /approvals list row on a desktop, not only on a phone. Keep the existing
      TestMobileLayoutViewport and TestResponsiveLayout tests green unchanged. Move the 3
      hover-only rules and 7 title= tooltips in the three renderers onto visible or tap
      equivalents. Write ADR 0049 (container queries for components rendered in more than one
      host). Remove every xfail with reason="p4-card-containers".
    acceptance:
      - all p4-card-containers xfails removed and passing
      - existing responsive and mobile tests unchanged and green
      - ADR 0049 written

  - id: p5-remaining-pages
    title: Shell, list, connect, security and fallback pages
    depends_on: [p1-toolchain, p2-settings]
    brief: |
      Move web_shell.py's header/nav, approval_list_html.py, routes_connect.py (including the
      Telegram phone/code/password form; set inputmode and autocomplete correctly for a phone
      keyboard) and routes_security.py onto the same utilities, with 44px targets on coarse
      pointers. On a phone the shell nav must fit on one row or scroll horizontally, never wrap
      into a second header row. Add viewport meta to the three bare fallback pages (better:
      render them through one small shared helper). Remove every xfail with
      reason="p5-remaining-pages" and every remaining class-concatenation allow-list entry.
    acceptance:
      - all p5-remaining-pages xfails removed and passing
      - the class-concatenation allow-list is empty
      - desktop screenshots unchanged, or any change is intended and noted

  - id: p6-push
    title: Installable org app and web push
    depends_on: [p5-remaining-pages]
    brief: |
      Org mode only; local mode keeps notification tiers 0–1 exactly as today.
      1. Web App Manifest (name, icons from resources/, display standalone, start_url
         /approvals), served on an unauthenticated route classified per ADR 0014, and linked
         from web_shell.py when org mode is active. Check the CSP `manifest-src`.
      2. VAPID: a key pair generated on first start and stored with the org state that holds the
         other server secrets (0600, service account). Never put it in the bundle.
      3. Per-principal push subscriptions: POST/DELETE /api/push/subscription (session + CSRF,
         classified). Store them per principal under users/<principal>/. Drop a subscription on
         404/410 from the push service.
      4. On a new pending approval for a principal, send a push with the payload at most
         notifications_detail=minimal ("1 approval waiting"). No tool name, no content, no
         requester. Add an org config bundle switch (build_org_bundle.py flag) to turn push off
         for the whole org; default on. Rate-limit per principal, the same as tier 1.
      5. sw.js gets `push` and `notificationclick` handlers. The click opens or focuses /approvals.
      6. web_shell's existing permission pre-prompt subscribes in org mode. On iOS, when not
         installed, show an "Add to Home Screen to get notifications" hint instead.
      7. Write ADR 0050: the first time approval metadata leaves the org server (via Apple and
         Google push services), the payload limit, the org-wide off switch, and why local mode
         does not get it. Update docs/org-mode-setup-guide.md (egress to the push services, the
         switch) and docs/approval-list-ui-ux.md §4 (tier 2 now exists for org mode).
      Tests: unit tests for payload minimisation, subscription auth, 410 cleanup, the switch
      off, and local mode having no route; a browser test that the manifest is served and linked.
      A real push delivery is manual (see manual in this manifest).
    acceptance:
      - payload content is test-proven to be minimal
      - local mode route set is unchanged (test)
      - ADR 0050 written; setup guide and approval-list-ui-ux.md updated

  - id: p7-retire
    title: Retire the plan
    depends_on: [p2-settings, p3-review-content, p4-card-containers, p5-remaining-pages, p6-push]
    brief: |
      1. Confirm ADRs 0047–0050 cover every item in this plan's "Decisions to extract into ADRs"
         list; add whatever is missing. Set each to Accepted and list them in docs/adr/README.md.
      2. Add this manifest's `manual` items to docs/release-testing.md as a standing "org mode on
         a phone" checklist, and describe the new phone-layout tests in docs/testing-policy.md.
      3. Update docs/approval-list-ui-ux.md's responsive section to point at ui.css, container
         queries and the new tests instead of hand-written breakpoints.
      4. Delete docs/org-mode-mobile-plan.md and restore docs/README.md's "no active plan"
         sentence. Keep .claude/commands/implement.md; it is reusable.
      5. Consolidate this plan's CHANGELOG [Unreleased] lines into a coherent group.
    acceptance:
      - every final_check in this manifest passes
```

## Decisions to extract into ADRs when this plan retires

- Tailwind v4, utilities-only, inlined, instead of Bootstrap, Pico, web components, Open Props or
  hand-written CSS (constraints 1–5 above).
- Container queries, not viewport media queries, for components rendered in more than one host.
- Server-side PDF rasterisation for review on narrow screens, instead of pdf.js or an
  open-in-new-tab route.
- Web push as an off-server notification channel for org mode: its payload limit, and that local
  mode does not get it.
