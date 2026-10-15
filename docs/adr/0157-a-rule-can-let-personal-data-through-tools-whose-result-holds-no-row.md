# ADR 0157: A rule can let personal data through the tools whose result holds no row

## Status

Accepted — 2026-10-11. Implemented.
Amends ADR 0155. Its alternative "exempting exports from the PII gate" stays rejected as a blanket exemption; a per-rule opt-in is now the way to let an export through.
Amended by ADR 0173.

## Context

The PII gate (ADR 0015) keeps personal data out of the model's context: when a read's content trips
the scan, a matching auto-accept rule is overridden, a person gets a card, and an unattended session
is denied. The export tools (ADR 0155), `drive_download_file` delivered as a file, and
`peek_upload_page` send their data to a file the user's own processes read or to a page the user's
own rule names. Their result holds no row, so the scan has nothing to protect the model from. A
scheduled sync over a table with names in it was denied on every run.

The gate was never a data-loss control: an exported file lands where the user's own processes can
read it, as before, and a rule that let an ordinary read through would still be scanned.

## Decision

1. `PolicyRule` has a boolean `allow_personal_data`, false by default. Under a rule with the flag,
   the tools in `auto_accept.PERSONAL_DATA_RULE_TOOLS` auto-accept even when the PII scan finds
   something: `grist_export_records`, `grist_export_sql`, `salesforce_export_report`,
   `jira_export_issues`, `peek_upload_page` and `drive_download_file`. Every other tool still scans.
   The flag changes what a PII finding does, not whether a rule matches.
2. The flag is offered on the Settings "Add a rule" form as a checkbox, only for the groups in
   `catalogue.PERSONAL_DATA_GROUPS`: Grist document, Salesforce report, Jira project, Peek page
   and Drive folder. It is set only there, behind passkey step-up. `privacyfence_propose_policy_change`
   never sets it.
3. A flagged rule is a different rule from the plain one on the same scope: the stored id is
   hashed with the flag only when it is true, so every existing id is unchanged, and the two stay
   two rows when merged. A malformed flag drops the rule (fail closed).
4. A call whose result carries the content itself (`content_reaches_result`, an inline Drive
   download) is never covered: the flag does not apply and the scan stays. The size estimate that
   decided "not inline" is binding, so a file approved as a link is never returned inline.
5. The audit entry is the ordinary one: `decision="auto_accepted"`, the rule id, `pii_detected=True`
   and the categories found.

## Alternatives considered

- **Exempting the export tools from the PII gate outright.** A rule made for interactive reads would
  silently start moving personal data; the opt-in is per rule.
- **A `conditions` entry for the flag.** Conditions narrow whether a rule matches; this flag does not
  change matching, only what a PII finding does.
- **Letting `privacyfence_propose_policy_change` set the flag.** The model whose context the gate
  protects would be asking to switch the protection off; the flag stays a Settings-only decision, as
  ADR 0144 keeps Grist rules.
- **A new audit field.** An auto-accepted entry with `pii_detected=True` and a rule id already says
  it. Drive's `pii_already_reviewed` path produces the same combination; both mean a person already
  decided about this personal data, so no field is needed to tell them apart.

## Consequences

- An unattended recipe can export, download from a rule-covered folder and publish a dashboard that
  holds names, once the user ticked the box on that rule.
- Rules written before this change are untouched and keep the scan.
- A flagged rule read by an older daemon is treated as an ordinary rule, which keeps the scan in
  force. If that daemon rewrites the rules file the flag is lost and the stored id no longer matches
  the content. A downgrade is not supported, and nothing handles it in code.
- The tool set is a code constant: a new export-like tool joins it only with its own rules in place.

## Verification

- `tests/unit/test_gate.py`: `TestPersonalDataRules`.
- `tests/unit/policy/test_store.py` (flag in the id, merge, round trip, malformed flag),
  `tests/unit/policy/test_catalogue.py`, `tests/unit/policy/test_describe.py`.
- `tests/unit/test_settings_controller.py`, `tests/unit/web/test_routes_settings.py`,
  `tests/unit/web/test_routes_org_settings.py`, `tests/unit/test_settings_window_html.py`.
- `tests/unit/connectors/test_drive_connector.py` (an inline download tells the gate its content
  reaches the result).

## Related

- [`gate.py`](../../src/privacyfence/gate.py), [`auto_accept.py`](../../src/privacyfence/auto_accept.py),
  [`policy/engine.py`](../../src/privacyfence/policy/engine.py), [`policy/store.py`](../../src/privacyfence/policy/store.py),
  [`policy/catalogue.py`](../../src/privacyfence/policy/catalogue.py)
- ADR [0015](0015-unattended-session-flag-is-advisory-only.md), ADR [0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md), ADR [0144](0144-grist-rules-are-per-document-and-set-from-settings-not-the-card.md), ADR [0155](0155-exports-write-a-file-and-return-only-counts-and-a-hash.md)
