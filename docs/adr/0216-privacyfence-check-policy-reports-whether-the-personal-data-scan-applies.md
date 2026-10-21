# ADR 0216: `privacyfence_check_policy` reports whether the personal-data scan applies

## Status

Accepted — 2026-10-21. Implemented.
Relates to ADR 0157, ADR 0164 and ADR 0173.

## Context

`privacyfence-sync preflight` printed `ok: covered by rule …` for a step that an unattended run can
still deny, because the personal-data scan finds personal data in its result. A scoped token's
`allow_personal_data` lifts the scan only for the tools in `PERSONAL_DATA_RULE_TOOLS` (ADR 0173), so a
covered `salesforce_run_report` passed preflight and failed in production (issue 984).
`privacyfence_check_policy` only said `pii_gate_may_apply: true` for every review tool, which does not
tell the caller whether its own token lifts the scan.

## Decision

- **The server reports the answer per call.** `privacyfence_check_policy` adds `pii_gate` and
  `pii_gate_detail` to every result. `pii_gate` is `none` (the `auto` gate, or a popup tool whose
  payload is not scanned), `allowed_by_token`, `allowed_unless_inline` (`drive_download_file`, whose
  content may come back inside the result at run time) or `applies`. `pii_gate_detail` is one line
  saying why a scan applies, and empty otherwise.
- **`UPLOAD_PII_SCAN_TOOLS`** names the popup tools whose payload the gate scans too
  (`drive_upload_file`, `peek_upload_page`); the others report `none`.
- **`pii_gate_may_apply` stays**, unchanged, for existing clients.
- **Preflight warns per step.** For a covered step it prints `; WARNING: <detail>: a run is denied if
  the result holds personal data` when `pii_gate` is `applies`, unattended or attended with a scoped
  token. The warning changes neither the problem count nor the exit code.

## Alternatives considered

- **A copy of the tool list in `sync/`.** It would drift from the server's list.
- **Preflight reads the token's `allow_personal_data` itself.** The server already knows it and
  phrases the reason; the client would have to duplicate the gate's rules.
- **Change `pii_gate_may_apply`.** It would break existing clients.

## Consequences

- `applies` is conservative. A Drive file already PII-reviewed (`pii_already_reviewed`) skips the
  forced confirmation, so the warning can be a false positive.
- A scoped token is denied by the scan attended too, so preflight warns for a scoped token in both modes.
- A server older than this change sends no `pii_gate`, and preflight prints no warning.

## Verification

- `tests/unit/web/test_mcp_dispatch.py` (`TestCheckPolicy`: one test per `pii_gate` value and the
  tool-set drift test).
- `sync/tests/test_preflight.py` (the warning, its absences, the note printed once).

## Related

- [`mcp_dispatch.py`](../../src/privacyfence/web/mcp_dispatch.py), [`gate.py`](../../src/privacyfence/gate.py), [`preflight.py`](../../sync/src/privacyfence_sync/preflight.py)
- ADR [0157](0157-a-rule-can-let-personal-data-through-tools-whose-result-holds-no-row.md), ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), ADR [0173](0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md)
