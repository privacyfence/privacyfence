# ADR 0095: a Gmail signature's `cid:` images are copied from the user's own sent mail

## Status

Accepted — 2026-09-28. Implemented for issue #783.

## Context

ADR 0038 appends the signature that `users.settings.sendAs` stores for the sending address. Some
signatures embed their images as `cid:` references (`<img src="cid:companyLogo">`) rather than
URLs. The sendAs API returns that HTML but not the image parts it points to. Gmail's compose window
attaches those parts itself, so a message composed in Gmail is a `multipart/related` with one
inline image part per reference. A PrivacyFence draft had the references but no parts, so every
image was broken (issue #783).

The Gmail API has no endpoint for a signature's images. Every message the user sent from Gmail with
that signature carries them, under the same Content-IDs.

## Decision

1. For a rich-text draft (one with `body_markdown`) whose signature has `cid:` images,
   `GmailClient.find_signature_images` reads the user's ten most recent `in:sent` messages, newest
   first, and copies the first image part (at most 1 MB) whose `Content-ID` (or `X-Attachment-Id`)
   matches each reference. It keeps only those image parts and stops once every reference is found.
   Results are cached for five minutes, like the sendAs list. A plain-text draft carries no HTML,
   so it never looks.
2. The draft becomes `multipart/related` (the alternative part, then one inline part per image),
   the shape Gmail itself saves.
3. An image not found is replaced by its alt text, or removed when it has none. The saved HTML
   never refers to a part the draft doesn't carry.
4. This happens before gating, like the rest of ADR 0038. The popup's `Signature` row says how many
   images were copied from sent mail or how many were not found. When nothing of the signature is
   left, it says the signature was not appended.

## Alternatives considered

- **Leave `cid:` images broken.** What shipped with #643. Rejected: this bug.
- **Always drop `cid:` images, keeping only their alt text.** No extra reads, but a signature whose
  whole point is a logo loses it. Kept as the fallback for images that can't be found.
- **Ask the user to upload their signature images in PrivacyFence Settings.** A second copy to keep
  in sync with Gmail by hand, for a problem Gmail's own sent mail already solves.
- **Rewrite `cid:` images to hosted URLs.** There is nothing to rewrite them to: the bytes exist only
  inside Gmail messages, and hosting them anywhere else would publish them.
- **Search all mail rather than sent mail.** Received mail can carry any Content-ID, including one
  chosen to match a signature's. Sent mail is what the user's own client wrote.

## Consequences

- A rich-text draft with a `cid:` signature reads up to eleven extra Gmail API calls on a cache
  miss. Only image parts with the wanted Content-IDs are kept; message bodies are fetched with them,
  because the API has no way to fetch part headers alone, but nothing else leaves
  `find_signature_images`, is logged or is shown.
- The copied images are the user's own, from their own mailbox, into their own draft. They never
  reach the MCP client. The reviewer sees that they were added, not their bytes.
- A user who has not sent mail with the signature recently (e.g. a new signature) gets alt text
  until they do. The popup says so.
- A signature whose images are URLs is unchanged: no lookup, HTML appended as-is.

## Verification

- `src/privacyfence/gmail_client.py`: `signature_cid_refs`, `drop_unresolved_cid_images`,
  `GmailClient.find_signature_images`, `_build_body_part`.
- `src/privacyfence/connectors/gmail.py`: `_resolve_draft_sender`, `_DraftSender.preview`.
- `tests/unit/test_gmail_client.py::TestFindSignatureImages`, `::TestSignatureInlineImages`.
- `tests/unit/connectors/test_gmail_connector.py::TestDraftSignatureCidImages`.

## Related

- ADR 0038, which this builds on.
- Issues #643 and #783.
