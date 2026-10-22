# ADR 0219: A scoped token can let email and Slack reads return personal data

## Status

Accepted — 2026-10-22. Implemented.
Amends ADR 0173. Relates to ADR 0164 and ADR 0216.

## Context

An unattended routine that triages email and Slack has to read message bodies. Those reads return
content to the AI system by design, and the personal-data scan stops them with a card, which a scoped
token cannot answer: it is deny-only (ADR 0164). ADR 0173 let a token carry `allow_personal_data`, but
only for the tools whose result holds no row (exports, downloads, page uploads). A routine that reads
mail therefore could not run.

## Decision

- **A second flag.** `ScopedToken.allow_personal_data_reads` (default false, stored only when set, read
  with `is True`, so an older file loads as false). It is independent of `allow_personal_data`: that
  flag keeps covering exports, downloads and page uploads only, and a token that has it does not gain
  the new one.
- **The tools.** `PERSONAL_DATA_READ_TOOLS`: `gmail_get_message`, `gmail_get_thread`,
  `slack_get_channel_history`, `slack_get_thread_replies` and `slack_search_messages`.
- **The gate.** `_personal_data_rule` returns the token's matching rule when the call is made with a
  non-revoked scoped token and either the tool is in `PERSONAL_DATA_READ_TOOLS` and the token has the
  reads flag, or the existing condition for exports holds. A matching rule of the token is still
  required. The scan still runs: the audit entry is `auto_accepted` with `pii_detected: true` and the
  categories found. An unscoped call keeps the scan and its card; a call with a token that lacks the
  flag is denied as `denied_scoped_token`.
- **`privacyfence_check_policy`** reports `pii_gate: allowed_by_token` for these tools when the token
  has the flag, and `applies` with a one-line reason otherwise (ADR 0216).
- **Settings.** The New token form has a second checkbox, **Let reads return personal data**, shown in
  the token list as "personal data in reads allowed". It is part of the step-up fingerprint of the
  create request in both modes, and a created token's audit summary names it.
- **The New token form lists the tools that need no rule while no rule is ticked**, so a token with no
  rule (a connectivity test, a listing job) can be created.

## Alternatives considered

- **Extend `PERSONAL_DATA_RULE_TOOLS` and the existing flag to reads.** It would silently widen every
  token that already carries the flag, and the flag's contract is "the result holds no row".
- **A per-rule flag.** ADR 0173 moved the flag off rules.
- **Lift the scan for the whole token.** Exports and reads are different risks; the person ticks one or
  both.

## Consequences

- With the box ticked, personal data in a covered read reaches the AI system without a person seeing
  it first. The person decides this when creating the token, behind the passkey.
- A token created before this change keeps the flag false.
- Preflight and `check_policy` can tell the caller exactly which of the two flags lifts the scan.

## Verification

- `tests/unit/test_scoped_tokens.py` (`TestAllowPersonalDataReads`), `tests/unit/test_gate.py`
  (`TestPersonalDataReads`).
- `tests/unit/web/test_mcp_dispatch.py` (`TestCheckPolicy`: the `pii_gate` cases and the tool-set drift test).
- `tests/unit/web/test_routes_settings.py`, `tests/unit/web/test_routes_org_settings.py`
  (`TestScopedTokenRoutes`), `tests/unit/test_settings_window_html.py` (`TestScopedTokensSection`).

## Related

- ADR [0173](0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md), ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), ADR [0216](0216-privacyfence-check-policy-reports-whether-the-personal-data-scan-applies.md), ADR [0220](0220-a-routines-rule-profile-is-a-scoped-token.md)
- [`auto_accept.py`](../../src/privacyfence/auto_accept.py), [`scoped_tokens.py`](../../src/privacyfence/scoped_tokens.py), [`gate.py`](../../src/privacyfence/gate.py), [`mcp_dispatch.py`](../../src/privacyfence/web/mcp_dispatch.py)
