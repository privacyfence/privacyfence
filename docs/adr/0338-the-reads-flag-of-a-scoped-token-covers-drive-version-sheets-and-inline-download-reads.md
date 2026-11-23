# ADR 0338: The reads flag of a scoped token covers Drive version, Sheets and inline download reads

## Status

Accepted — 2026-11-23. Implemented.
Amends ADR 0254, ADR 0173 and ADR 0216. Relates to ADR 0157 and 0219.

## Context

Issue [#1186](https://github.com/privacyfence/privacyfence/issues/1186): a routine whose scoped token had **Let reads
return personal data** ticked was still denied with "needs a person's confirmation" on three Drive calls
when the personal-data scan found something, although one of the token's rules covered the call.
`drive_sheets_get_values` and `drive_get_revision_content` return cell values and an earlier version's
text to the AI system, exactly as `drive_get_file_content` does (ADR 0254), yet were in no tool set.
`drive_download_file` is under `allow_personal_data` (ADR 0173: "the result holds no row"), but when the
file comes back inline (base64 inside the tool result) the content does reach the AI system, so ADR 0157
excludes it from that flag. No flag covered it.

## Decision

- `drive_get_revision_content` and `drive_sheets_get_values` join `PERSONAL_DATA_READ_TOOLS`.
- A new set, `PERSONAL_DATA_INLINE_READ_TOOLS` (`drive_download_file`), names the tools of
  `PERSONAL_DATA_RULE_TOOLS` whose result can also carry the content. For these, `_personal_data_rule`
  uses `allow_personal_data_reads` when `content_reaches_result` is true and `allow_personal_data`
  otherwise. `drive_download_file` stays in `PERSONAL_DATA_RULE_TOOLS`.
- A rule of the token that covers the call is still required. The scan still runs and the audit entry
  is `auto_accepted` with `pii_detected: true` and `rule_source: "token"`.
- The denial text, the Settings help under the box, `sync`'s preflight note and the reference docs name
  the three cases.
- `privacyfence_check_policy`: the two reads report `allowed_by_token` through the existing reads branch.
  `drive_download_file` reports `allowed_by_token` for a token with both boxes and `applies` for one with
  only the reads box, with a detail saying the box lifts it only when the file comes back inside the result.
- A reads-only token never gets a staged link for a download approved as inline: the gate is told the
  delivery from the metadata size, and the real bytes (a Google Docs export has size 0 in the metadata)
  can turn out too big for the result. `_deliver_org_download` then refuses with the existing
  over-limit text instead of staging, so personal data cannot leave through a link without the first box.

## Alternatives considered

- **Move `drive_download_file` into `PERSONAL_DATA_READ_TOOLS`.** A download delivered as a link would
  then lose `allow_personal_data`, which tokens rely on today.
- **Let the reads box also cover a download delivered as a link.** That content never reaches the AI
  system, and ADR 0173 gives it to the first box.
- **A third box.** One more checkbox for a distinction the person already makes when ticking the reads
  box, the reason ADR 0254 gave.
- **A new `pii_gate` value such as `allowed_if_inline`.** `sync` preflight treats every value but
  `applies` as lifted, so it would stay silent before a run that is then denied.

## Consequences

- Tokens that already carry the reads box gain these three cases, as with ADR 0254: personal data in a
  covered sheet, file version or inline download reaches the AI system without a person seeing it first.
  The person can create a replacement token without the box.
- A reads-only token's download delivered as a link is still denied. A download approved as inline that
  turns out too big for the result is refused instead of staged.
- `allowed_unless_inline` now means a token with only the first box. Preflight warns for
  `drive_download_file` with a reads-only token even when the run turns out inline, because `applies`
  is conservative (ADR 0216).

## Verification

- `tests/unit/test_gate.py`: `TestPersonalDataReads.test_flagged_token_lets_personal_data_through_a_drive_revision_read`,
  `test_flagged_token_lets_personal_data_through_a_sheets_values_read`;
  `TestPersonalDataRules.test_inline_download_keeps_the_scan_with_only_the_exports_flag`,
  `test_reads_flag_lets_an_inline_download_through`,
  `test_reads_flag_alone_does_not_lift_a_download_delivered_as_a_link`.
- `tests/unit/connectors/test_drive_connector.py`:
  `test_reads_only_token_is_refused_when_an_inline_estimate_turns_out_too_big`,
  `test_exports_token_still_gets_a_staged_link_when_an_inline_estimate_turns_out_too_big`.
- `tests/unit/web/test_mcp_dispatch.py` (`TestCheckPolicy`): `test_pii_gate_for_drive_download_with_only_the_reads_flag`,
  `test_pii_gate_for_drive_download_with_both_flags`,
  `test_pii_gate_is_allowed_by_token_for_drive_revision_and_sheets_reads_with_the_reads_flag`,
  `test_pii_scan_tool_sets_stay_in_step_with_the_gate_table`.
- `tests/unit/test_settings_window_html.py`, `sync/tests/test_preflight.py`.

## Related

- ADR [0254](0254-the-reads-flag-of-a-scoped-token-covers-drive-file-content.md), ADR [0219](0219-a-scoped-token-can-let-email-and-slack-reads-return-personal-data.md), ADR [0173](0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md), ADR [0216](0216-privacyfence-check-policy-reports-whether-the-personal-data-scan-applies.md), ADR [0157](0157-a-rule-can-let-personal-data-through-tools-whose-result-holds-no-row.md)
- [`auto_accept.py`](../../src/privacyfence/auto_accept.py), [`gate.py`](../../src/privacyfence/gate.py), [`drive.py`](../../src/privacyfence/connectors/drive.py)
