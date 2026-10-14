# ADR 0173: Personal data is allowed per scoped token, not per rule

## Status

Accepted — 2026-10-13. Implemented.
Amends ADR 0157 and ADR 0164. The per-rule opt-in ADR 0157 introduced is replaced by a per-token one; ADR 0164's token gains one property.

## Context

ADR 0157 let a rule carry `allow_personal_data`, so exports, file downloads and Peek page uploads
that hold personal data could be auto-accepted. The flag worked for any client, scoped or not, so an
interactive Claude session that matched the same rule also skipped the personal-data card. The flag
was also part of a rule's identity, which is why one folder could appear twice: once with the box
ticked and once without.

The calls the flag exists for come from scheduled jobs that hold a scoped token (ADR 0164). A person
sitting in a Claude session has no reason to skip the card.

## Decision

- **`ScopedToken.allow_personal_data` replaces `PolicyRule.allow_personal_data`.** It is one
  checkbox per token, covering all of the token's rules: **"Let exports, downloads and page uploads
  carry personal data"**, with help text saying it applies only to calls made with this token and
  only to Drive downloads delivered as a file, Grist, Salesforce and Jira exports, and Peek page
  uploads. Token creation is already a step-up-gated sensitive action in both modes (ADR 0164, ADR
  0165), and the flag is part of the create request's fingerprint, so nothing new is classified.
  A token with the flag shows the badge "personal data allowed".
- **The gate checks the token.** `gate._personal_data_rule` returns nothing unless the current call
  carries a scoped token that is not the revoked scope and has the flag. The same six tools and the
  same `content_reaches_result` condition as before still decide which calls are eligible, and a
  matching rule within the token's scope is still required. A call made without a token always stops
  for review when personal data is found.
- **The rule flag is gone.** `PolicyRule.allow_personal_data`, `store.rule_id_for`'s parameter,
  `catalogue.PERSONAL_DATA_GROUPS`, the rule form's checkbox and the `personal_data` field of the add
  actions are removed. `rule_from_dict` ignores a stored `allow_personal_data` key.
- **The upgrade moves the flag.** A token that carried any rule whose stored dict had
  `allow_personal_data: true` gets the token flag when the rules are converted (ADR 0172), and the
  upgrade logs the labels of the tokens this happened to.

## Alternatives considered

- **Keep the per-rule flag.** It applied to any client, including interactive ones, and it is what
  made the same scope show up twice.
- **A per-rule flag inside a token.** More interface for a distinction the maintainer did not want:
  a token that may export personal data from one folder but not another.

## Consequences

- An unscoped session that used to auto-accept a flagged export now gets the card.
- A token that carried one flagged rule now allows personal data on all of its rules' exports,
  downloads and uploads. This is wider for that token than before; the upgrade log names those
  tokens, and a person can create a replacement without the flag (a token cannot be edited, and
  Duplicate fills the form from it).
- The flag cannot be proposed by the AI system: token creation is a Settings action behind the
  passkey.
- A Drive download whose content comes back inside the tool result keeps the check, as before.

## Verification

- `tests/unit/test_gate.py` (`TestPersonalDataRules`:
  `test_flagged_token_lets_personal_data_through_export`,
  `test_unscoped_call_never_lets_personal_data_through`, `test_token_without_flag_keeps_the_scan`,
  `test_revoked_scope_keeps_the_scan`, `test_inline_download_keeps_the_scan_even_with_flag`).
- `tests/unit/test_scoped_tokens.py` (the flag round-trips, an old file loads as false, `create()`
  stores it, `test_flag_transfer_returns_labels`).
- `tests/unit/policy/test_store.py` (`test_rule_from_dict_ignores_allow_personal_data`,
  `test_rule_id_for_has_no_personal_data_variant`).
- `tests/unit/policy/test_scope_rows.py` (`test_screenshot_case_merges_to_one_row`).
- `tests/unit/test_settings_window_html.py` (the token checkbox and badge; the rule form has no
  personal-data checkbox) and `tests/unit/web/test_routes_settings.py` (the fingerprint covers the
  flag; a non-bool flag is rejected).

## Related

- [`gate.py`](../../src/privacyfence/gate.py), [`scoped_tokens.py`](../../src/privacyfence/scoped_tokens.py)
- ADR [0155](0155-exports-write-a-file-and-return-only-counts-and-a-hash.md), ADR [0157](0157-a-rule-can-let-personal-data-through-tools-whose-result-holds-no-row.md), ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), ADR [0172](0172-one-stored-auto-accept-rule-per-scope.md)
