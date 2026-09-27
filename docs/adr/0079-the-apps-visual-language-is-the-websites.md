# ADR 0079: The app's visual language is the website's, with a dark mode derived from it

## Status

Accepted — 2026-09-26. Builds on [ADR 0078](0078-the-app-shares-the-websites-design-system.md),
which gave the app the website's design files; this one decides how the app looks on them.

## Context

ADR 0078 made `src/privacyfence/resources/design/` (`tokens.css`, `base.css`) the one source of
the website's tokens and primitives and inlined them into every document the app renders. The
app still painted itself with its own palette, `resources/tokens.css`: a warm grey page
(`#f3f2f2`), a blue accent (`#0088b0`) and a magenta second accent (`#d6006c`), 1–4 px radii, and
Source Serif 4 on the approval card, embedded as three base64 WOFF2 files (about 120 KB of every
card document). That file was itself a hand-kept copy of the card stylesheet's own `:root`
block, kept in sync by a test.

Three things the website does not need, the app does:

- **Dark mode.** The website rules it out (its rule 9); the app has had one since the card
  first shipped, and people approve requests at night.
- **Status semantics.** The approval card shows deny/destructive, write versus read, PII
  detected, a requester that is not verified, and step-up required. The website has one accent
  and no states like these.
- **The action hierarchy.** On the card, Allow once is the filled button, Deny is outlined in the
  danger colour, and Always allow is a small underlined link, deliberately unlike the other two so
  a fast click aimed at the primary cannot land on it. Which decision looks like the default is a
  security property of the card, not a matter of taste.

## Decision

**The app's visual language is the website's.** The same tokens (`--bg`, `--surface`, `--ink`,
the teal `--accent`, `--line`, the radii and the type scale), the same buttons (the dark-ink
`.primary`, the white `.secondary`), the same uppercase kicker labels, and the website's header:
a floating, rounded, sticky bar with the brand mark, whose links collapse into a `<details>` menu
when there is no room (`web_shell.py`). The website switches at a 900 px viewport; the app, which
has no viewport breakpoints (shared rule 6), makes the same switch with a container query on the
header's wrapper. `resources/tokens.css` is deleted, and so is the magenta second accent.

**What the app adds lives in one app-only file**, `resources/design/app.css`, which
`design_css.DOCUMENT_CSS` inlines after the shared files and `scripts/build_site.py` never
publishes: the dark palette, the status tokens, and the application components (field, toggle,
tabstrip, badge, card, panel), each with hover, focus-visible, disabled and dark states and 44 px
targets.

**The dark palette is derived from the website's one dark surface**, the home page's
`.privacy-card` (`website/styles.css`): its `#142b2b` background is the app's `--surface`, its
`#eaf4f3` text is `--ink`, its `#c6d8d5` copy is `--ink-soft`, and its `#8fd3ca` kicker is
`--accent`. The page (`--bg`, `#0e1f1f`) is a step darker than the surface, and `--surface-soft` a
step lighter, the way a dark UI shows elevation. The dark block overrides the shared token names
and defines no names of its own, so every shared primitive and every page is dark-mode-correct
without a rule of its own. A dark palette invented from nothing, or the old app's inverted grey
ramps, were the alternatives; either would have been a third look next to the website's light
and dark surfaces.

**Status semantics are tokens with a non-colour cue.** `--danger`, `--warning`, `--info` and
`--success`, each with a `-soft` tint, plus `--focus-ring` and `--control-line` (the edge of a
form control, which needs 3:1 where the decorative `--line` hairline does not). They are chosen to
sit next to the teal: a brick red, an ochre, a slate blue and a green that is not the accent. On
the card: a read is the accent, a write is `--warning` (it was magenta), the review-gate PII card
and its in-text marks are `--danger`, and the write-gate content-flag card is `--warning`, the
lower-alarm family it always had. Every one of these keeps the cue it had without colour: the
"Read"/"Write" pill text, the ⚠️ and the sentence on the PII cards, the underline under a PII
mark, the dashed "Not verified" badge and "?" glyph against the solid "Verified" badge, and the
step-up banner's text. Nothing on a PrivacyFence page relies on colour alone.

**The action hierarchy never changes with styling** (shared rule 10 in `base.css`). The restyle
changed Allow once from a blue fill to the website's dark-ink `.primary` fill (inverted, light on
dark, in dark mode), Deny's outline from a 45 % mix to the solid danger colour, and all three
controls' radius and font. It did not change which one is filled, which is outlined, which is a
link, or their sizes. `TestActionHierarchy` in `tests/integration/test_browser_smoke.py` measures
this on the rendered card in both themes, at phone and desktop widths, with one and with several
Always-allow candidates: exactly one opaque fill (Allow once), a solid border on Deny, no border
and an underline on Always allow, and Always allow in smaller type and shorter than the two
buttons. The primary is ink rather than the accent or a status colour so that it never reads as a
status, least of all a cautionary one on a write card.

**System font, no webfont.** `--font-sans` names Inter first, but the website never loads it, so
visitors see their platform's system sans. The app uses the same token and ships no font files, so
the two look the same on a given device. Source Serif 4, its three embedded WOFF2 files and its
OFL licence text are deleted; a card document no longer carries about 120 KB of font data, which
also leaves room for the page images p4 of the plan adds for PDFs.

**Contrast is tested, not reviewed.** `tests/unit/test_design_contrast.py` computes WCAG contrast
from the token files for every text/background pair and every control-boundary pair in both
themes: 4.5:1 for text, 3:1 for boundaries and focus rings. No token is used only for large text,
so no pair gets the 3:1 large-text allowance.

## Consequences

- One look across the website and the app, in light, and a dark mode that is recognisably the
  same product. Pages that have not been rebuilt yet (settings, connections, passkeys, the list)
  already pick up the new palette through the token names; their layouts are rebuilt by later
  phases of the mobile work.
- A change to the website's tokens changes the app. That is the point of ADR 0078; the contrast
  test is what catches a website change that the app's dark mode, or a status pair, cannot carry.
- The dark palette is the app's alone. If the website ever gains a dark mode, it should start from
  `app.css`'s block rather than derive a second one.
- The card renders in a platform font, so its text metrics differ slightly by OS. Its layout is
  flex regions that scroll inside themselves, and its fixed-height rows are clamped by line count,
  not measured in pixels, so nothing depends on one font's metrics.
- A future restyle of the card must keep `TestActionHierarchy` green unchanged. Changing which
  decision looks like the default needs a new ADR, not a CSS change.
