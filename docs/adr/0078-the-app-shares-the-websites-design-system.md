# ADR 0078: The app shares the website's design system, instead of adopting a CSS framework

## Status

Accepted — 2026-09-25. Extends [ADR 0049](0049-website-css-is-plain-modern-css-without-a-framework.md)
to the app; does not supersede it.

## Context

The app's pages (the approval list and card, settings, connections, passkeys, and the dialogs)
are rendered by Python modules that hand-write their own CSS in string literals. Each surface
wrote its own flex and width rules, and the responsive fixes that exist (list, card, dialog) were
each a separate, later patch with its own viewport `@media` block. The one surface nobody had
opened on a phone, settings, has none: on a 393 px phone its fixed 190 px sidebar leaves the
content column about 110 px wide, and the Privacy Filter pane about 0 px. Org mode is meant to be
usable from a phone, so every surface has to be rebuilt for narrow widths anyway.

The app also looks like a different product from the website: a warm grey page, blue and magenta
accents, 1–4 px radii and a serif on the card, against the website's cool off-white, teal accent,
large radii and system sans.

The website had solved the same problem for itself a day earlier (ADR 0049): design tokens on
`:root`, five layout primitives (`.shell`, `.stack`, `.cluster`, `.grid-auto`, `.split`, the last
collapsing under a *container* query), nine written rules for what "responsive" means, and a
browser test that enforces them at six widths.

The app's constraints narrow the options:

- **CSP.** `default-src 'none'`; `<style>` and `<script>` only with the per-response nonce
  ([`web/csp.py`](../../src/privacyfence/web/csp.py)); no CDN and no third-party origin. All CSS
  ships inside the Python package and is inlined.
- **Self-contained card documents.** A card is rendered once and served unchanged on every `GET`
  until decided, with its previews as `data:` URIs and its CSS inlined, so what it ships must be
  small.
- **No frontend build.** Markup lives in Python f-strings and JavaScript string concatenation; a
  runtime framework (React, Vue, Lit) would mean rewriting the rendering model.
- **One component, several containers.** The approval card renders full-screen on a phone and in
  a desktop tab of any width, and a host page may put it in a box of its own, so its layout must
  follow its container's width, not the viewport's.

## Decision

1. **One physical source for both.** The website's tokens (`tokens.css`) and the shared part of
   its chrome (`base.css`: the layout primitives, buttons, the focus style, `.kicker`/`.eyebrow`)
   live in `src/privacyfence/resources/design/`, inside the Python package.
   `scripts/build_site.py` publishes them on the website from there, at `/tokens.css` and
   `/base.css`; the site-only chrome (skip link, header and its `<details>` menu, footer, consent
   banner) stays in `website/chrome.css`. The app inlines both files
   ([`design_css.py`](../../src/privacyfence/design_css.py)) at the start of every document's one
   nonce'd `<style>` — `web_shell.py`, `settings_window_html.py`, `approval_window_html.py`,
   `dialog_window_html.py` — before the page's own rules.
2. **The shared files use tokens, not colours.** Colour values live only in `tokens.css`; where the
   website's shared rules hard-coded one (`#fff` on the primary button, its hover `#253743`, the
   button shadow, the eyebrow's line and wash), it became a token with exactly that value, so the
   website renders pixel-identically and the app's dark mode can later override the same names.
3. **The rules travel too.** ADR 0049's nine rules are written once, in `base.css`'s header
   comment, as the shared rules for the website and the app, with a tenth for the app: *the
   approval card's action hierarchy never changes with styling* — which decision is filled,
   outlined or strongest stays as it is ([ADR 0036](0036-card-copy-names-the-caller-through-one-placeholder.md)
   covers the card's copy; this covers its emphasis). The website has no dark mode; the app keeps
   one, which overrides the shared token names and nothing else.
4. **The same kind of test.** `tests/integration/test_browser_smoke.py`'s `TestPhoneLayout`
   checks the measurable rules on every app surface in a real phone emulation and at 320 and
   1024 px, as `test_website_layout.py` does for the site. `tests/unit/test_design_system.py`
   fails on a width `@media` query or a colour literal in a renderer, and on a custom property
   defined outside `resources/design/` other than a knob the shared primitives expose. None of
   these checks has an exception list: every renderer was moved onto the shared system before
   they became unconditional.
5. Anything the app needs and the website does not (dark mode, status colours, form components)
   goes in an app-only file next to the shared ones, never into the website's files.
6. **The card is not embedded in an approvals list row.** Planning this work assumed a card also
   renders inline in an expanded `/approvals` row. It never has: the row's Details disclosure
   shows only the request's metadata, and Review opens the full card page. Embedding the card
   there was considered and rejected (decided with the user on 2026-09-26): the card document is
   self-contained and carries its own nonce'd `<style>` and `<script>`, so putting it in a row
   would take an `<iframe>` and a CSP `frame-src`/`frame-ancestors` change for a context that
   adds nothing the card page does not already give. The browser tests therefore have no
   "card inline in a list row" case; `TestCardContainers` proves the card lays out correctly in
   380, 700 and 1000 px containers on a desktop-sized page, which is what a container-query
   layout has to guarantee for any host.

## Alternatives considered

| Option | Why not |
|---|---|
| **Tailwind CSS v4 for the app only** | Two styling systems in one repo. The website's look would be rebuilt in utility classes, where it drifts from the real thing. It also adds a pinned build step, a lock and a CI freshness check, and ADR 0049 already weighed and rejected it for the same look. |
| **Tailwind for app and website** | Supersedes a decision taken earlier the same day, redoes the website's finished CSS, and adds a build to the site. Nothing about the app requires it. |
| **Bootstrap 5.3** | About 230 KB to inline into every card document; its own visual language would have to be overridden everywhere; its navbar needs its JavaScript. ADR 0049 rejected it for the site. |
| **Pico CSS** (classless) | Styles elements, not layouts, so it would not fix the settings panes; it restyles everything. |
| **Web component kits** (Shoelace/Web Awesome, Ionic) | Runtime JavaScript custom elements to serve and nonce; Ionic's app shell is a rewrite. |

## Consequences

- One design system, not two: a token or primitive changed in `resources/design/` changes the
  website and the app together, and a website-only change has to be made in `website/`.
- No build step and no dependency: the shared files are plain CSS, inlined exactly as the app's
  own token file already was. They add about 8 KB to each app document.
- Container queries become the app's responsive primitive, as they are the website's: `.shell`
  is a size container and `.split` collapses by the space it actually has.
- The shared files are loaded next to other stylesheets (the /docs/ theme, the app's own rules),
  so every rule in them is scoped to a class, apart from the focus style. That style now also
  applies to the /docs/ pages and the app, which had the browser's default focus ring where they
  set none of their own.
- The website depends on a path inside the Python package; `build_site.py` and
  `test_design_system.py` both name it, so a move breaks loudly.

## Related

- [ADR 0049](0049-website-css-is-plain-modern-css-without-a-framework.md) — the website's CSS, which this extends.
- [ADR 0033](0033-one-route-layer-per-surface-with-an-auth-adapter-per-mode.md) — local and org mode render the same pages, so both get the shared system.
- [`src/privacyfence/resources/design/base.css`](../../src/privacyfence/resources/design/base.css) — the shared rules.
