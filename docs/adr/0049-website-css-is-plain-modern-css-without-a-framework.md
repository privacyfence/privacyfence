# ADR 0049: The website's CSS is plain modern CSS, with no framework

## Status

Accepted — 2026-09-25. Implemented in `website/styles.css`.

## Context

`privacyfence.eu`'s marketing pages are hand-written HTML with one self-hosted stylesheet. Every
page has to read and align well from a 320 px phone to a wide desktop. Measured on 2026-09-25,
the site had no page-level horizontal scroll and stacked its sections correctly, but below 900 px
every header link except the call to action was `display: none`, with no menu to reach them;
tap targets were 22–40 px; and each page added its own breakpoints to one flat file. The site is
about to grow from two pages to about fifteen, with seven header destinations.

The site loads no third-party resources apart from analytics after consent, and until the
documentation site's build lands it has no build step at all.

Decided as H2 of the website plan ([PR #683](https://github.com/privacyfence/privacyfence/pull/683)).

## Decision

1. No CSS framework. The stylesheet is rebuilt into design tokens on `:root` (colour, type
   scale with `clamp()`, spacing, radius, the 44 px tap size) and five layout primitives:
   `.shell` (centred container, also a size container), `.stack`, `.cluster`, `.grid-auto`
   (`repeat(auto-fit, minmax(…))`) and `.split` (two columns that stack under a container-query
   threshold). The existing section classes keep their names and are expressed on the
   primitives, so the desktop design is unchanged.
2. Below the 900 px breakpoint the header links move into a `<details>`/`<summary>` menu: it
   works without JavaScript, is keyboard-operable and is announced as expandable.
3. The rules for what "responsive" means (tested widths, no horizontal scroll, reachable header
   destinations, 44 px tap targets below 1024 px) are the stylesheet's header comment, and a
   browser test enforces them on every page.
4. The generated documentation site gets responsiveness from its generator's theme and shares
   the tokens through the theme's extra-CSS hook, rather than this stylesheet styling it.

## Alternatives considered

- **Tailwind CSS.** Needs a build step (a pinned binary or an npm toolchain) locked and audited
  like the other dependency locks, for about fifteen pages. Every page's markup would be
  rewritten into utility classes, redoing the design rather than keeping it, and utility-class
  HTML is harder to review than semantic class names. Its standalone CLI is the likeliest
  successor if this decision is revisited, since it can run inside a Python site build without
  Node.
- **Bootstrap 5.** About 25 KB of CSS for a grid and components that would be overridden to keep
  the current look, and its collapsing navbar needs Bootstrap's JavaScript.
- **Pico CSS or another classless framework.** Small and JavaScript-free, but restyles every
  element, which conflicts with the existing design.

## Consequences

- No dependency, no build step, and no third-party request for styling.
- Container queries, `clamp()` and `:has()` are required: all are Baseline in current browsers.
  An old browser gets the stacked, single-column layout, which is readable.
- New pages must be built from the primitives. A page that needs its own media queries is a
  sign a primitive is missing.
- Revisit with a new ADR if the page count or the design effort outgrows this (for example, a
  designer joins and wants a component library).

## Verification

- `tests/integration/test_website_layout.py` loads every page under `website/` at 320, 360, 390,
  768, 1024 and 1440 px and asserts no horizontal scroll, no element past the viewport outside a
  scroll container, every header destination reachable, and 44 px tap targets below 1024 px. It
  saves a full-page screenshot of each page at each width, uploaded by `tests.yml` as
  `website-layout-screenshots`.

## Related

- [PR #683](https://github.com/privacyfence/privacyfence/pull/683) — the website plan (decisions H1–H3).
