# Org mode on mobile, in the website's design — plan

**Status:** proposed, 2026-09-25. Nothing here is implemented. This is a plan document per
[`adr/README.md`](adr/README.md): it is deleted when its work lands, and the decisions below that
meet the ADR bar (the styling approach and its rejected alternatives, the visual invariants, PDF
rendering, the push-notification trust question) are extracted into ADRs first.

**To run it:** start a Claude Code session with `/implement <GitHub URL of this file>`. The
command lands on `main` separately
([privacyfence/privacyfence#748](https://github.com/privacyfence/privacyfence/pull/748)). Until it
does, start the session with `implement <URL>`, and it should read `.claude/commands/implement.md`
from the same ref as this file and follow it.

## Goal

A signed-in org-mode user can do everything they would do at a desk from a phone browser: sign in
through the IdP, see and decide their pending approvals (including passkey step-up), manage their
connections and passkeys, read their audit log, and, as an admin, edit settings. They also learn
about a new approval without keeping a tab open. And the app looks like the website: the same
tokens, components and responsive rules, in light and dark.

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
  the no-runtime-framework constraint. **"Open PDF in a new tab"** needs a new authenticated route for the raw bytes,
  which is a new place card content is served from, and on Android it still hands the file to
  another app. The desktop `<embed>` can stay as is above that width. This is a new server-side
  parser of attacker-supplied files (a PDF is whatever the connector returned), so it runs with
  a page and time limit and inside the same failure handling as `text_extraction.py`. That point
  goes into the ADR.
- **Tables:** below a container width breakpoint, render each row as a stacked label/value block
  (the standard responsive-table pattern), switched by a container query.
  Change `word-break` from breaking anywhere to `overflow-wrap: anywhere`, so text only breaks
  mid-word when a single word cannot fit on a line (an email address, say).
- **Staged download links** (`GET /downloads/{token}`, see [`org-mode-setup-guide.md`](org-mode-setup-guide.md)):
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
The website hit the same problem, and fixed it structurally (see "Styling decision" below).

## Constraints on the styling approach

1. **CSP:** `default-src 'none'`, with `<style>`/`<script>` elements allowed only with the
   per-response nonce ([`web/csp.py`](../src/privacyfence/web/csp.py)). No CDN and no third-party
   origin. All CSS ships inside the Python package and is inlined.
2. **Card documents are self-contained.** An approval card is rendered once and served again on
   every `GET` until decided (see `csp.set_nonce`), and it carries its previews as `data:` URIs.
   Its CSS is inlined too, so what it ships must be small.
3. **No frontend build for these pages.** Markup lives in Python f-strings and JavaScript string
   concatenation. Runtime JavaScript frameworks (React, Vue, Lit) would mean rewriting the
   rendering model, and nothing here needs that.
4. **One component, several containers.** An approval card renders full-screen on a phone, in a
   desktop tab, and inline in an expanded list row. Its layout must respond to the width of its
   *container*, not the viewport. That is the exact case the current viewport-only
   `@media (max-width: 700px)` rules get wrong in the inline-row context.
5. **It must look like the website.** See "Visual design" below.

## Styling decision: the app shares the website's design system

**Decided with the user on 2026-09-25.** The website already solved this problem for itself.
[ADR 0049](adr/0049-website-css-is-plain-modern-css-without-a-framework.md) rebuilt
`privacyfence.eu`'s CSS as design tokens (`website/tokens.css`: colour, a `clamp()` type scale,
spacing, radius, the 44px tap size) plus five layout primitives in `website/chrome.css` (`.shell`,
`.stack`, `.cluster`, `.grid-auto`, `.split`). `.split` stacks under a *container* query, not a
viewport query. Nine written rules define what "responsive" means (the header comment of
`website/styles.css`), and `tests/integration/test_website_layout.py` enforces them on every page
at 320–1440px. The website's look, which is the target, *is* that CSS.

So the app does not adopt a third-party framework. It adopts the website's system: the same token
file, the same primitives, the same components (buttons, header, `<details>` menu), and the same
rules and the same kind of test. It adds only what an application needs and a marketing site does
not (see "Visual design"). This meets the aim behind "a framework, not custom responsiveness":
no per-page media queries, and one tested set of primitives that pages are built from. Under
ADR 0049's rule, "a page that needs its own media queries is a sign a primitive is missing".
The difference is that this system is the one the website already runs on.

Why this beats a framework here:

- **One design system, not two.** Tailwind for the app while the website stays on plain CSS
  would mean re-implementing the website's look in utility classes, and the two would drift apart,
  which is the opposite of "make it look like the website".
- **No build step and no new dependency.** The shared files are plain CSS inlined into the page,
  exactly as `tokens.css` is today (constraints 1–3).
- **Container queries are already the primitive** (constraint 4). `.shell` is a size container
  and `.split` collapses by the space it actually has.
- **Already proven**: the website went through the same audit (hidden nav, 22–40px tap targets,
  per-page breakpoints) and came out passing a browser test at six widths.

### Alternatives considered

| Option | Why not |
|---|---|
| **Tailwind CSS v4 for the app only** (this plan's first draft) | Two styling systems in one repo. The website's look would be rebuilt in utility classes, where it drifts from the real thing. It also adds a pinned build step, a lock and a CI freshness check, and ADR 0049 already weighed and rejected it for the same look. |
| **Tailwind for app and website** | Supersedes a decision taken earlier the same day, redoes the website's finished CSS, and adds a build to the site. Nothing about the app requires it. |
| **Bootstrap 5.3** | About 230KB to inline into every card document; its own visual language would have to be overridden everywhere; its navbar needs its JavaScript. ADR 0049 rejected it for the site. |
| **Pico CSS** (classless) | Styles elements, not layouts, so it would not fix the settings panes; it restyles everything. |
| **Web component kits** (Shoelace/Web Awesome, Ionic) | Runtime JavaScript custom elements to serve and nonce; Ionic's app shell is a rewrite. |

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

## Visual design: the app adopts the website's look

The website and the app look like two products. The website is calm and modern: a cool off-white
page, white cards with 1px hairline borders, large radii, soft wide shadows, one teal accent,
dark-ink primary buttons, pill badges and small uppercase letter-spaced labels. The app has a
warm grey page, blue and magenta accents, 1–4px radii, and a serif (Source Serif 4) on the
approval card. Because the pages are being rebuilt for phones anyway, each surface is restyled
once, while its layout is already being rewritten.

**What changes**

| | App today (`resources/tokens.css`) | Target (`website/tokens.css`, `chrome.css`) |
|---|---|---|
| Page / surface | `#f3f2f2` / `#eae9e9`, warm grey | `--bg #f6f8fb`, `--surface #fff`, `--surface-soft #eef3f8` |
| Text | `#201e1d` | `--ink #14212b`, `--ink-soft #43535f`, `--muted #60707d` |
| Lines | 16% text mix | `--line #dbe3e9` |
| Accent | blue `#0088b0` plus magenta `#d6006c` | teal `--accent #167a70` / `--accent-dark` / `--accent-soft`; no second accent |
| Radii | 1 / 2 / 4px | `--radius-s 10px` (controls), cards about 12–18px, `--radius 22px` (panels), pills 999px |
| Elevation | none | `--shadow`, used sparingly |
| Type | system sans in settings, Source Serif 4 on the card | `--font-sans` and the website's type scale everywhere; the serif and its font files go |
| Buttons | filled accent | `.button.primary` (dark ink) and `.button.secondary` (white, line border) |
| Labels | mixed | `.kicker` (uppercase, letter-spaced); the card already has this shape |
| Header | flat grey bar | the website's floating, rounded sticky header and its `<details>` menu below 900px |

**Font.** `--font-sans` names Inter first but the site never loads it, so visitors see their system
sans. The app uses the same token and ships no webfont, so both look the same on a given device.
Removing Source Serif 4 also takes its embedded files out of every card document, which offsets
the page images p4 adds.

**What the app adds, because the website does not have it.** These go in one app-only file next to
the shared ones, never in the website's files:

- **Dark mode.** The website rules it out of scope (rule 9); the app has one and keeps it. The
  dark palette is derived from the website's own dark panel (`.privacy-card`: `#142b2b` surface,
  `#eaf4f3` text, `#8fd3ca` accent), and it overrides the same token names, so every shared
  primitive gets it for free. That requires the shared files to use tokens where they now
  hard-code `#fff`, `#253743` and similar values. p1 makes that change, and it leaves the website
  pixel-identical.
- **Status semantics.** The approval card shows things the website never had to: deny or
  destructive, write versus read, PII detected, requester "not verified", step-up required. Each
  gets a semantic token (`--danger`, `--warning`, `--info`, `--success`, each with a `-soft` tint,
  and `--focus-ring`) chosen to sit with the teal. Each keeps the non-colour cue it has today
  (text, icon or border style), so nothing relies on colour alone.
- **Application components.** `field` (input, select, textarea), `toggle`, `tabstrip` (the
  settings section picker on a phone), `badge`, and `card`/`panel` as general classes rather than
  the website's page-specific ones. They are built on the shared primitives and tokens.
- **Action hierarchy on the card stays exactly as it is.** Which action is filled, which is
  outlined, and which is visually strongest is a security choice
  ([ADR 0036](adr/0036-card-copy-names-the-caller-through-one-placeholder.md) covers the copy;
  this covers the emphasis). The restyle changes colour and shape, never which button looks like
  the default. Card copy does not change at all.
- **Contrast.** Every text/background token pair meets WCAG AA (4.5:1, or 3:1 for large text and
  UI boundaries) in both themes, enforced by a unit test over the token files.

**One physical source for the shared files.** `tokens.css` and the shared part of `chrome.css`
(primitives, buttons, focus style) must reach both the website build and the Python wheel. They
move to `src/privacyfence/resources/design/`, and `scripts/build_site.py`, which already maps
`tokens.css` from a source path, copies them into the site at the same URLs. The site-only chrome
(header menu, footer, consent banner) stays in `website/chrome.css`. A test fails if a token is
defined anywhere else.

**The rules travel too.** The website's nine responsive rules become the app's rules, minus rule
9 (dark mode), plus one: the approval card's action hierarchy. They are written once, in the shared
file's header comment. The app's phone-layout tests (p1) check the measurable ones on every app
surface, the same way `test_website_layout.py` does for the site. A renderer that contains an
`@media` width query or a colour literal fails a unit test.

**Sign-off before rollout.** Design is a judgement call, and the orchestrator cannot make it.
The design phase (p2) ends with a rendered style guide (every component, both themes, phone and
desktop) plus a before/after of one settings page and one approval card. The run stops there
until you approve it. Only then do the phases that restyle whole surfaces start.

## Phases

The work lands as **one pull request** built from phase branches. Run it with
`/implement <URL of this file>` (see [`.claude/commands/implement.md`](../.claude/commands/implement.md)):
an orchestrator session starts one child session per phase, merges each finished phase into the
feature branch, and opens the single PR to `main` at the end. The manifest below is what it runs.
The prose above is the context each child reads first.

Order and parallelism, from the manifest's `depends_on`:

```
wave 1   p1-shared-design      (website design files shared with the app, test harness; no visual change)
wave 2   p2-design-system      (app theme, components, style guide)  ── you sign off here
wave 3   p3-settings       ∥   p4-review-content
wave 4   p5-card-containers ∥  p6-remaining-pages
wave 5   p7-push
wave 6   p8-retire             (ADRs checked, screenshots refreshed, plan deleted)
```

Phases in the same wave touch disjoint files. The one shared file is `CHANGELOG.md`, and
`[Unreleased]` additions merge mechanically. p2 is a `human_gate` phase: after it merges, the
orchestrator shows you the style guide and waits for your approval before starting wave 3.

**How the phases prove they are done.** p1 adds the phone-layout test harness and marks every case
that fails today `xfail(strict=True, reason="<phase id>")`, naming the phase that owns the fix. A
phase is done when it has removed every xfail carrying its own id and those tests pass. Because
the xfails are strict, a phase cannot fix another phase's case by accident without the suite
noticing. By p8 no xfail from this plan may remain.

**ADR numbers are pre-assigned**, so parallel phases do not collide: 0077 (p1), 0078 (p2),
0079 (p4), 0080 (p7). If `main` has taken a number by the time the final PR opens, the
orchestrator renumbers in its last merge of `main`.

## Implementation manifest

```yaml
plan_slug: org-mode-mobile
feature_branch: feature/org-mode-mobile
max_parallel: 2
verify_after_merge:
  - python3 -m pytest tests/integration/test_browser_smoke.py tests/integration/test_website_layout.py -q
screenshots_after_merge: python3 scripts/render_ui_review.py --out test-results/ui-review   # exists from p2 on
final_checks:
  - "grep -rn 'reason=\"p[1-8]-' tests/ returns nothing: no xfail this plan added is left"
  - the @media-width and colour-literal allow-lists added in p1 are empty
  - docs/org-mode-mobile-plan.md is deleted, and no doc links to it
  - ADRs 0077-0080 (or their renumbered successors) exist, are listed in docs/adr/README.md, and are Accepted
  - CHANGELOG.md has [Unreleased] entries for every user-visible change and no version heading
  - src/privacyfence/resources/tokens.css and the Source Serif 4 font files are gone; src/privacyfence/resources/design/ is the only token source for app and website
  - test_website_layout.py passes and the website is visually unchanged
manual:
  - "Real iPhone (Safari and the installed Home Screen app) and real Android Chrome: sign in through the IdP, approve a write with passkey step-up, approve with the IdP step-up fallback, read a PDF review card, open a staged download link from inside the Claude app, receive a push notification"
  - "Subjective look in light and dark at phone and desktop width (render_ui_review.py output, qa_web_smoke.py), per docs/release-testing.md"

phases:
  - id: p1-shared-design
    title: Share the website's design files with the app; phone test harness
    depends_on: []
    brief: |
      Read "Styling decision" and "Visual design" above in full, and ADR 0049.
      1. Create `src/privacyfence/resources/design/`. Move `website/tokens.css` there. Split
         `website/chrome.css`: the layout primitives (.shell, .stack, .cluster, .grid-auto,
         .split), buttons (.actions, .button, .primary, .secondary), the focus style and
         `.kicker`/`.eyebrow` go to `resources/design/base.css`; the site-only chrome (skip
         link, header and its <details> menu, footer, consent banner, breakpoints for those)
         stays in `website/chrome.css`. Update `scripts/build_site.py`'s file map, the /docs/
         theme's extra_css, and every page's <link> so the built site serves the same files at
         the same URLs (or updated URLs, consistently). Package data must ship the design/
         folder (check MANIFEST.in and pyproject).
      2. Replace hard-coded colours in the moved files (`#fff`, `#253743`, `rgba(...)` shadows
         and so on) with tokens. Add new tokens with the exact current values where needed, so
         the website renders pixel-identically. Prove it: before/after screenshots from
         test_website_layout.py's saved output, compared per page and per width.
      3. Move the header comment's nine rules into base.css's header as the shared rules, keep
         styles.css pointing at them, and add a tenth for the app: "the approval card's action
         hierarchy never changes with styling".
      4. Embed tokens.css and base.css in the app's documents, inlined in the existing nonce'd
         <style> next to the current resources/tokens.css: web_shell.py, settings_window_html.py,
         approval_window_html.py, dialog_window_html.py. Check that no shared class name
         collides with an existing app class (the app mostly uses a pf- prefix). This phase
         changes nothing visually in the app: desktop screenshots of /settings, /approvals and
         a card, before and after, must be identical.
      5. Phone test harness in tests/integration/test_browser_smoke.py, mirroring
         test_website_layout.py's rules. Build a phone context from the existing
         `_MOBILE_EMULATION`, and also run at 320px and 1024px. Give the `org_server` fixture's
         bundle at least one configured connector service, so /connect renders instead of
         404ing. Cover: /approvals; /settings for each section, both admin and non-admin;
         /settings/privacy; /connect; /security; the three bare fallback pages
         (routes_approvals.py:507/522, session_auth.py:461); and a WIDE read card for each
         preview kind (PDF, image, Markdown, a 10-column record table), both full-page and
         expanded inline in an /approvals list row. Assertions: (a) no horizontal document
         overflow; (b) the main content region is at least 90% of the viewport width; (c) every
         visible interactive element is at least 44x44 CSS px below 1024px; (d) viewport meta
         is in effect; (e) for the PDF card, the preview shows an <img> of a rendered page, not
         an <embed>; (f) for table cards, no single word is broken across lines. Every case that
         fails today gets `xfail(strict=True, reason="<owning phase id>")`: settings →
         p3-settings; PDF, table and download → p4-review-content; the card inline in a list row
         → p5-card-containers; shell, list, connect, security and fallback pages →
         p6-remaining-pages. A case that already passes gets no marker.
      6. Unit tests, each with an allow-list seeded with today's offenders (each entry naming
         the phase that removes it): (a) no `@media` width/height query in any renderer under
         src/privacyfence or resources/approval_window/styles.css (prefers-color-scheme and
         prefers-reduced-motion are allowed); (b) no hex or rgb() colour literal in those
         renderers; (c) no CSS custom property named like a design token is defined outside
         resources/design/.
      7. Write ADR 0077: the app shares the website's design system (tokens, primitives, rules)
         from one packaged source, instead of Tailwind or another framework. It extends ADR
         0049 to the app and does not supersede it. Take the alternatives table from this plan.
    acceptance:
      - website visually unchanged at every tested width, test_website_layout.py green
      - app visually unchanged at desktop width (screenshots in the report)
      - new browser tests all pass or xfail strictly with an owning phase id
      - ADR 0077 written and listed in docs/adr/README.md

  - id: p2-design-system
    title: The app's theme, components and style guide in the website's look
    depends_on: [p1-shared-design]
    human_gate: true
    brief: |
      "Visual design" above is the spec.
      1. Add `resources/design/app.css`, which only the app loads: the dark theme (derived from
         the website's .privacy-card palette, overriding the shared token names under
         prefers-color-scheme: dark), the semantic status tokens (danger, warning, info, success,
         each with -soft, and focus-ring), and the app components: field (input, select,
         textarea), toggle, tabstrip, badge, card and panel. All are built on the shared tokens
         and primitives, with hover, focus-visible, disabled and dark states and 44px targets
         below 1024px. Any responsive behaviour uses container queries on .shell-style
         containers, never a viewport width query.
      2. Delete `src/privacyfence/resources/tokens.css` and replace its variable names
         everywhere (a temporary alias block is fine within this phase, but none may be left at
         the end). Have resources/approval_window/styles.css read the shared tokens instead of
         its own palette copy. Remove the Source Serif 4 @font-face rules and delete the font
         files and OFL.txt (check MANIFEST.in and package data).
      3. Restyle the shared chrome: web_shell.py's header becomes the website's header, with the
         same markup pattern, the brand icon, and the <details> menu below 900px. Pages below the
         header are restyled by later phases; in between they pick up the new colours through
         the tokens.
      4. Approval card, colours and type only (its layout is p5's): it moves to --font-sans and
         the new palette. **Keep the action hierarchy exactly as it is**: whichever of
         deny/allow/allow-always is filled, outlined or strongest today stays that way. Only
         colour and shape change, and no card copy changes. Read, write, PII, not-verified and
         step-up indicators each keep their non-colour cue.
      5. Add a unit test that computes WCAG contrast for every text/background and UI-boundary
         token pair in both themes (4.5:1 for text, 3:1 for large text and boundaries). Update
         the existing TestColorScheme dark-mode tests to the new token names.
      6. Add `scripts/render_ui_review.py --out DIR`. It drives the existing Playwright fixtures
         and writes PNGs of: a style guide showing every shared and app component in every
         state, both themes; /approvals, one card of each kind, /settings (General and Privacy
         Filter), /connect and /security, at 393px and 1280px, light and dark. The style guide
         is a static HTML file the script builds from the design files, not a product route.
         The orchestrator runs this after every merge and shows you the output.
      7. Write ADR 0078: the app's visual language is the website's; how the dark palette was
         derived; the status-semantics and action-hierarchy invariants; system font instead of
         a webfont.
    acceptance:
      - resources/tokens.css and the serif fonts are deleted
      - the contrast test passes in both themes
      - the approval card's action emphasis is unchanged (say how you compared it)
      - render_ui_review.py output is attached to the report for the human gate
      - website still visually unchanged; ADR 0078 written

  - id: p3-settings
    title: Settings usable at phone width, in the new look
    depends_on: [p2-design-system]
    brief: |
      Fix and restyle settings_window_html.py, both the org and the local rendering, using the
      shared primitives and p2's components. When the settings container is narrower than the
      website's 900px breakpoint (a container query), `.pf-nav` becomes a `tabstrip` above the
      content, and the Privacy Filter `.pf-subnav` becomes a second strip (or a `field` select),
      so the detail pane gets the full width. At wider widths keep a left nav, styled like the
      website's cards (.split is the likely primitive). Keep the existing
      `data-nav`/`data-privacy-nav` click handling and the role=tablist ARIA. Settings rows
      become `card`s with the label and description above the control at narrow widths;
      switches are the `toggle` component. Remove every xfail with reason="p3-settings" and
      this module's entries in the p1 allow-lists.
    acceptance:
      - all p3-settings xfails removed and passing
      - settings_window_html.py has no allow-list entries left
      - phone and desktop screenshots of each settings section in both themes in the report

  - id: p4-review-content
    title: PDF, tables and download links readable on a phone
    depends_on: [p2-design-system]
    brief: |
      See "Review cards with documents" above.
      1. PDF. Add `pypdfium2` (exact pin) to pyproject and regenerate the locks with
         scripts/update_dependency_locks.sh. Where card_builder.py builds pdf_data_uri, also
         rasterise the first N pages (N=5, configurable, width about 1000px, PNG) in a helper
         module with a page limit, a time limit and a byte limit, and the same failure handling
         text_extraction.py uses: if rendering fails, the card falls back to the text
         extraction, never to nothing. build_preview_body_html emits both: the <embed> shown only
         when the preview container is at least 600px wide (a container query), and otherwise
         the page images plus a "Showing pages 1–N of M" note, in the new look. The card stays
         self-contained (data: URIs), so CSP needs no change. Report how big a 5-page card gets,
         compared with the serif font bytes p2 removed.
      2. Tables. _table_html becomes stacked label/value blocks below the same container width.
         Use `overflow-wrap:anywhere` instead of breaking at every character.
      3. Downloads. When GET /downloads/{token} finds no session, redirect to
         `/login?next=/downloads/<token>` through the existing _safe_next_path allow-list. Add a
         unit test that the claim still requires the same principal after sign-in, and a test
         that `next` cannot be turned into an open redirect.
      4. Write ADR 0079: server-side rasterisation, rejecting pdf.js (a large runtime bundle to
         serve and nonce) and an open-in-new-tab route (a new place card content is served
         from). Include the threat note that this is a new parser of untrusted input.
      Remove every xfail with reason="p4-review-content".
    acceptance:
      - all p4-review-content xfails removed and passing
      - a malformed or encrypted PDF falls back to text, with a unit test
      - dependency locks regenerated and committed; bandit clean
      - ADR 0079 written

  - id: p5-card-containers
    title: Approval card and dialogs on container queries, in the new look
    depends_on: [p4-review-content]
    brief: |
      Replace the viewport `@media (max-width: 700px)` rules in
      resources/approval_window/styles.css, approval_window_html.py and dialog_window_html.py
      with the shared primitives and container queries: the card root is a size container,
      and its wide two-column layout is `.split` or an app component built on it. The card must
      stack correctly when it is inline in an expanded /approvals list row on a desktop, not only
      on a phone. Finish the card's move onto p2's components (panels for the "already
      knows"/preview sections, kicker labels, badges, buttons), and delete styles.css rules as
      they are replaced; ideally styles.css ends up empty and is removed. Keep the action
      hierarchy and all copy unchanged (ADR 0078). Keep the existing TestMobileLayoutViewport and
      TestResponsiveLayout tests green unchanged. Move the 3 hover-only rules and 7 title=
      tooltips in the three renderers onto visible or tap equivalents. Remove every xfail with
      reason="p5-card-containers", and these modules' allow-list entries.
    acceptance:
      - all p5-card-containers xfails removed and passing
      - existing responsive and mobile tests unchanged and green
      - card, PII dialog and choice dialog screenshots in both themes and both widths in the report

  - id: p6-remaining-pages
    title: List, connect, security and fallback pages, in the new look
    depends_on: [p3-settings]
    brief: |
      Move approval_list_html.py, routes_connect.py (including the Telegram phone/code/password
      form; set inputmode and autocomplete correctly for a phone keyboard) and
      routes_security.py onto the shared primitives and p2's components. List rows become
      `card`s. Connection rows use the website's connector-chip look for the service name and a
      `badge` for their state. Render the three bare fallback pages through one small shared
      helper with viewport meta and the new look. Remove every xfail with
      reason="p6-remaining-pages". Every p1 allow-list must be empty at the end of this phase.
    acceptance:
      - all p6-remaining-pages xfails removed and passing
      - all p1 allow-lists are empty
      - screenshots of every touched page in both themes and both widths in the report

  - id: p7-push
    title: Installable org app and web push
    depends_on: [p6-remaining-pages]
    brief: |
      Org mode only; local mode keeps notification tiers 0–1 exactly as today.
      1. Web App Manifest (name, icons from resources/, display standalone, start_url
         /approvals, theme_color and background_color from the design tokens), served on an
         unauthenticated route classified per ADR 0014, and linked from web_shell.py when org
         mode is active. Check the CSP `manifest-src`.
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
      7. Write ADR 0080: the first time approval metadata leaves the org server (via Apple and
         Google push services), the payload limit, the org-wide off switch, and why local mode
         does not get it. Update docs/org-mode-setup-guide.md (egress to the push services, the
         switch) and docs/approvals-and-policy.md's Notifications section (push now exists for
         org mode).
      Tests: unit tests for payload minimisation, subscription auth, 410 cleanup, the switch
      off, and local mode having no route; a browser test that the manifest is served and linked.
      A real push delivery is manual (see manual in this manifest).
    acceptance:
      - payload content is test-proven to be minimal
      - local mode route set is unchanged (test)
      - ADR 0080 written; setup guide and approvals-and-policy.md updated

  - id: p8-retire
    title: Retire the plan
    depends_on: [p3-settings, p4-review-content, p5-card-containers, p6-remaining-pages, p7-push]
    brief: |
      1. Confirm ADRs 0077–0080 cover every item in this plan's "Decisions to extract into ADRs"
         list; add whatever is missing. Set each to Accepted and list them in docs/adr/README.md.
      2. Add this manifest's `manual` items to docs/release-testing.md as a standing "org mode on
         a phone" checklist, and describe the new phone-layout, contrast, @media and
         colour-literal tests in docs/testing-policy.md.
      3. Add a short "Design system" section to docs/coding-and-testing-guidelines.md, and
         repoint any responsive-layout guidance there at the shared design files and rules
         instead of hand-written breakpoints: tokens and primitives live in
         resources/design/, shared with the website; pages compose them; no width media queries
         and no colour literals in renderers.
      4. Regenerate every screenshot in docs/images/screenshots/ with
         scripts/qa_readme_screenshots.py and the approval-screenshot procedure in that folder's
         README, synthetic data only. The website shows some of these, so it will show the new UI.
      5. Delete docs/org-mode-mobile-plan.md, its entry in docs/README.md's contributor docs and
         its entry in scripts/build_site.py's CONTRIBUTOR_DOCS.
      6. Consolidate this plan's CHANGELOG [Unreleased] lines into a coherent group.
    acceptance:
      - every final_check in this manifest passes
      - refreshed screenshots contain no real account data
```

## Decisions to extract into ADRs when this plan retires

- The app shares the website's design system (tokens, primitives, rules) from one packaged
  source, instead of Tailwind, Bootstrap, Pico or web components. This extends ADR 0049 to the
  app, and container queries come with it.
- The app's visual language is the website's; how the dark palette was derived; status semantics
  keep a non-colour cue; the restyle never changes which approval action is visually the default.
- Server-side PDF rasterisation for review on narrow screens, instead of pdf.js or an
  open-in-new-tab route.
- Web push as an off-server notification channel for org mode: its payload limit, and that local
  mode does not get it.
