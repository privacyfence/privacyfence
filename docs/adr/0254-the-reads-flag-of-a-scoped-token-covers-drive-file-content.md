# ADR 0254: The reads flag of a scoped token covers Drive file content

## Status

Accepted — 2026-10-29. Implemented.
Amends ADR 0219. Relates to ADR 0173 and ADR 0164.

## Context

ADR 0219 let a token's `allow_personal_data_reads` flag lift the personal-data scan for Gmail and Slack
reads, because an unattended routine has to read message bodies and a scoped token cannot answer the
card (ADR 0164). `drive_get_file_content` is the same case: it returns the file's text to the AI system
by design, so it is not eligible for `allow_personal_data` (ADR 0173, results that hold no row), and until
now no flag lifted it. A routine reading a Drive file that held, for example, salary data was denied
with "needs a person's confirmation".

## Decision

- `drive_get_file_content` joins `PERSONAL_DATA_READ_TOOLS`. No new flag: the box is still **Let reads
  return personal data**, and a token without it is denied as before.
- A rule of the token that covers the call (`drive.read_file_contents`, for example an `approved_folder`
  rule) is still required. The scan still runs and the audit entry records `pii_detected` and the categories.
- The denial text, `privacyfence_check_policy`'s `pii_gate` (`allowed_by_token`) and the Settings help
  text name Drive file content reads.

## Alternatives considered

- **A separate flag for file contents.** A Drive file can hold far more personal data than a message,
  but one more checkbox for a distinction the person already makes when ticking the reads box; the box's
  help text now names Drive so the choice is informed.
- **Extend `allow_personal_data`.** Its contract is "the result holds no row" (ADR 0173).

## Consequences

- With the box ticked, personal data in a covered Drive file reaches the AI system without a person seeing
  it first. Tokens created earlier with the box ticked gain this too; the person can create a replacement
  without the box.
- `drive_download_file` is unchanged (it stays under `allow_personal_data`).

## Verification

- `tests/unit/test_gate.py` (`TestPersonalDataReads.test_flagged_token_lets_personal_data_through_a_drive_file_content_read`),
  `tests/unit/web/test_mcp_dispatch.py` (`TestCheckPolicy`), `tests/unit/test_settings_window_html.py`.

## Related

- ADR [0219](0219-a-scoped-token-can-let-email-and-slack-reads-return-personal-data.md), ADR [0173](0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md), ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)
- [`auto_accept.py`](../../src/privacyfence/auto_accept.py), [`gate.py`](../../src/privacyfence/gate.py)
