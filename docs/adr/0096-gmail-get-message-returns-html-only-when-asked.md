# ADR 0096: `gmail_get_message` returns the HTML body only when asked, under the same `body` policy

## Status

Accepted — 2026-09-28. Implemented for issue #783.

## Context

`gmail_get_message` returned `body_text` only: the text/plain part, or the HTML part converted to
text. Issue #783 needed the saved HTML of a draft to diagnose broken signature images, and there
was no way to get it from the client.

The approval card's details pane shows the text body. HTML can carry content that text doesn't
show: alt text, link targets, hidden elements, tracking-pixel URLs.

## Decision

1. `gmail_get_message` takes `include_html` (default `false`). When it is true, the result
   carries `body_html`, the message's text/html part (empty for a plain-text message).
2. `body_html` goes through the same `privacy.categories.body` policy as `body_text`. They are the
   same content, so a block or redact applies to both.
3. When requested, the card's new-information section gets an `HTML body` row
   (`Included` / `None (plain-text message)`), and the review-gate PII scan reads the HTML as well
   as the text, so content only the markup holds is still scanned.

## Alternatives considered

- **Always return `body_html`.** Doubles the size of every message result and sends markup an
  assistant rarely needs. Rejected: opt-in costs nothing when it isn't used.
- **A separate `html` privacy category.** A second switch for the same content. Someone who blocks
  the body expects the body blocked, however it is encoded.
- **Show the HTML in the details pane.** Unreadable in the card; the row says it goes, and the scan
  covers what it holds.

## Consequences

- The approval card for an `include_html` read doesn't show the markup itself, only that it is
  included. The text body shown is the same content, and the PII scan covers the rest.
- `gmail_get_thread` is unchanged.

## Verification

- `src/privacyfence/connectors/gmail.py`: `GmailConnector._get_message`.
- `tests/unit/connectors/test_gmail_connector.py::TestGetMessageIncludeHtml`.

## Related

- Issue #783.
