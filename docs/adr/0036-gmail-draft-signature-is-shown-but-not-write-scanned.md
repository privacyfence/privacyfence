# ADR 0036: a Gmail draft's appended signature is shown in the approval popup but not write-scanned

## Status

Accepted — 2026-09-24. Implemented for issue #643.

## Context

The Gmail API stores exactly the MIME it is given, so drafts created through PrivacyFence had no
signature. Issue #643 adds one: the six draft tools take `include_signature` (default from
`settings.yaml`'s `gmail.append_signature_to_drafts`) and `send_as`, and append the signature that
`users.settings.sendAs` stores for the sending address.

Two properties of the approval popup are affected:

- **What the reviewer approves.** A popup-gate write shows the drafted body in `details_text`. The
  signature is fetched from Gmail, not written by Claude, so it could be appended after approval
  without the reviewer seeing it.
- **The write-content flags.** `gate.py` runs `pii_detector.detect_pii_categories()` over every
  popup-gate write's `details` and shows the categories it finds as an informational note (never a
  second confirmation, never the audit log's `pii_detected`; see `gate.py`'s module docstring).
  A signature almost always contains the user's own phone number and often a postal address, so
  scanning it flags nearly every draft.

## Decision

1. The signature is resolved **before** gating (`GmailConnector._resolve_draft_sender`). It is
   appended to `details_text` exactly as `gmail_client` appends it to the text/plain part
   (`signature_plain_text`), and that same resolved HTML is what the client call saves. The preview
   dict gets only a `Signature: Appended (<address>)` row, never the signature text.
2. The write-content scan reads the drafted body **without** the signature. `gated_call` takes a
   `write_content_scan_text` override for this, defaulting to `details`, so every other write is
   scanned exactly as before.
3. An unknown `send_as` address is rejected before gating, so no popup is raised for a draft that
   could not be saved as shown.

## Alternatives considered

- **Append the signature in the client after approval.** The reviewer would approve text that is
  not what gets saved. Rejected: the popup is the one place a person sees a write.
- **Scan the signature too.** Every draft would carry the same "Phone number" note, so the note
  would stop carrying information for the body Claude actually wrote. The signature is the user's
  own contact block, read from their own Gmail settings, and it is displayed in full either way.
- **An allowlist of the user's own phone numbers/addresses in `pii_detector`.** A general
  mechanism for a problem that exists at one call site; it would also suppress those values when
  Claude copies them into a body, where a note may well be wanted.
- **Hide the signature from `details_text` and show only the preview row.** Shorter popup, but
  the reviewer could not see what a stale or unexpected signature says.

## Consequences

- The write-content note can no longer tell the reviewer that the draft contains a phone number
  when the only phone number is in the signature. The number is still visible in the popup.
- `write_content_scan_text` is available to any future popup-gate write whose `details_text` mixes
  the user's own boilerplate with drafted content. It changes only the informational note: the
  real PII gate (`gate="review"`) and `upload_pii_scan_text` are untouched.
- Resolving the signature before gating costs one `sendAs.list` call per draft when a signature or
  `send_as` is requested, cached for five minutes in `GmailClient`. With neither requested there is
  no extra call.

## Verification

- `src/privacyfence/connectors/gmail.py`: `_DraftSender` and `_resolve_draft_sender`.
- `src/privacyfence/gate.py`: `write_content_scan_text`.
- `tests/unit/connectors/test_gmail_connector.py::TestDraftSignatureAndSendAs` (preview, details,
  scan text and client call agree, for all six tools; unknown `send_as` rejected before gating).
- `tests/unit/test_gate.py::TestWriteContentFlags::test_write_content_scan_text_replaces_details_for_the_flags_only`.

## Related

- Issue #643.
- `gate.py`'s module docstring, on why writes get only the informational scan.
