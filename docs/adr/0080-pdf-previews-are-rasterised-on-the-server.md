# ADR 0080: PDF previews are rasterised on the server for narrow screens

## Status

Accepted — 2026-09-26. Builds on [ADR 0078](0078-the-app-shares-the-websites-design-system.md)
(container queries, not viewport breakpoints) and
[ADR 0079](0079-the-apps-visual-language-is-the-websites.md).

## Context

A read card for a PDF (Drive's `drive_get_file_content`, and any connector that passes
`pdf_bytes` to `gated_call`) showed the document as `<embed type="application/pdf"
src="data:…">` in the card's preview pane. The card document is self-contained: it is rendered
once, served again on every `GET` until it is decided, and carries its previews as `data:` URIs
under a CSP of `default-src 'none'` (`web/csp.py`).

On a phone that preview does not work, and no CSS can make it work:

- **Android Chrome has no inline PDF viewer.** An `<embed>` or `<iframe>` of a PDF renders blank,
  or as an "Open" button that hands the file to another app.
- **iOS Safari** renders a static first page that cannot be scrolled.
- **Chromium in a pane about 380 px wide** shows the viewer's toolbar (a hamburger icon and the
  literal text "pdf;bas…") and no page of the document.

So the reviewer was asked to approve a document they could not read.

## Decision

**The server renders the first pages to PNG and inlines them next to the `<embed>`.**
`card_builder.build_card_html` passes the PDF to `pdf_render.render_first_pages`, which uses
[pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (PDFium, the renderer inside Chrome; the
wheel bundles it, Apache-2.0/BSD-3-Clause, on every platform we build) to render the first five
pages at 1000 px wide. `approval_window_html.build_preview_body_html` emits both the `<embed>` and
the page images, and a container query on the preview (`.pf-pdf` in
`resources/approval_window/styles.css`) shows the `<embed>` when the preview is at least 600 px
wide and the pages below that. The pages sit under a "Showing pages 1–N of M" note, so a
reviewer can always tell that they saw part of the document, not all of it. It is a container
query and not a viewport query because the same card is a whole phone screen, a desktop tab, and
a row inside the approvals list.

The images are `data:` URIs, like the image preview already was, so the card stays one
self-contained document and the CSP does not change (`img-src data:` was already allowed).

**A PDF that cannot be rendered falls back to its text, never to nothing.** If rendering fails
for any reason, the narrow view says so and shows the text `text_extraction.extract_text`
reads from the same bytes, the extraction the connectors already run on attachments. If neither parser can read
the file (an encrypted PDF, say), the note says that too. This mirrors `text_extraction.py`'s own
posture: extracting less is the safe failure, raising into the approval path is not.

**Greyscale pages are written as greyscale PNGs.** A page with no colour on it, which is most
text, is encoded with one channel instead of three. Deflate compresses the one channel far
better: a dense page of text measured 25 KB instead of 390 KB. Five such pages add about 180 KB to
the card document once base64-encoded, against the roughly 120 KB of inlined serif font files
ADR 0079 removed. A slide-like page with colour fills measured 68 KB.

## Threat note: a new parser of untrusted input

A PDF here is whatever the connector fetched, and it is parsed before a human has decided
anything, in the same process that holds the connectors' credentials. PDFium is a large C
parser. It is fuzzed continuously as part of Chrome, but it has had memory-safety bugs, and
a malicious file now reaches it on the server, where before it reached only the reviewer's
browser. pypdf (already a dependency, for text extraction) is pure Python and does not widen the
attack surface the same way.

The limits on it, all in `pdf_render.py`:

- **Pages:** only the first five are rendered. **Pixels:** each page is scaled to 1000 px wide and
  down further if it would pass 2 megapixels, so an absurd aspect ratio cannot allocate a huge
  bitmap. **Bytes:** an input over 50 MiB is not opened, and rendering stops before the PNGs pass
  4 MiB in total.
- **Time:** the render runs on a worker thread and the card waits at most 5 seconds. PDFium
  cannot be interrupted from Python mid-page, so a render that overruns is abandoned, not killed:
  the card falls back to text at the deadline and the worker stops at the next page boundary.
  PDFium is not thread-safe, so renders are serialised by a lock; a caller that cannot get the
  lock before its own deadline also falls back to text rather than queueing.
- **Failure:** any exception, including PDFium refusing an encrypted file, means no pages.
- **Version:** `pypdfium2` is pinned exactly in `pyproject.toml`, unlike its neighbours, so a
  new PDFium arrives as a deliberate, reviewed bump and not as a side effect of a lock refresh.
- **Parity:** the page images show exactly the content the `<embed>` already carried, which
  drive.py passes only when the "AI will receive" policy already allows the file's content
  (see `pdf_bytes` in `gate.gated_call`). Nothing is shown that was not shown before.

What these limits do not buy: a hang inside one page render keeps one worker thread busy for as
long as it lasts, and a memory-corruption bug in PDFium would run in the daemon's process.
Isolating the render in a subprocess would contain both; it was not done now because the
approval path would pay a process start per PDF card and the frozen builds (PyInstaller) would
need a second entry point. It is the next step if PDFium's record makes it necessary.

## Alternatives considered

- **pdf.js in the browser.** A large runtime JavaScript bundle (the library and its worker run to
  over a megabyte), which would have to be inlined into every card document or served from
  a new route, nonced under the CSP, and kept up to date as a second PDF parser that also reads
  attacker-supplied files, in the reviewer's browser. It goes against the app's
  no-runtime-framework constraint, and it would load in every host of the card document.
- **An "Open PDF in a new tab" link.** It needs a new authenticated route that serves the raw
  bytes: a new place card content is served from, with its own authorisation, caching and
  lifetime to get right. On Android it still hands the file to another app, and the reviewer
  leaves the card to read what they are deciding about.
- **Keep the `<embed>` everywhere.** It is the defect described above.

## Consequences

- On a phone, and in any preview narrower than 600 px, a reviewer sees the first five pages and
  is told how many there are. Pages after the fifth are not visible in the card at that width.
- The WIDE card is 980 px wide, so its preview pane is about 450 px at every viewport today, and
  the `<embed>` does not show anywhere until a wider preview exists. Desktop reviewers get the
  page images too, with the same page count note. The threshold is the preview's own width
  (`@container (width >= 600px)`), so a wider pane brings the `<embed>` back without a change here.
- A card with a PDF is bigger by its page images, bounded at 4 MiB of PNG before base64.
- The dependency set grows by one package with a native library. PyInstaller finds it through
  its contrib hooks (`hook-pypdfium2`, `hook-pypdfium2_raw`); the packaged-artifact tests in
  `build.yml` are what prove each frozen build still starts.
