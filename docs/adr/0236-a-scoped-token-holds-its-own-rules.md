# ADR 0236: A scoped token holds its own rules

## Status

Accepted — 2026-10-26. Implemented.
Amends [ADR 0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), [ADR 0166](0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md), [ADR 0172](0172-one-stored-auto-accept-rule-per-scope.md), [ADR 0173](0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md), [ADR 0220](0220-a-routines-rule-profile-is-a-scoped-token.md) and [ADR 0228](0228-an-extra-scope-can-give-each-verb-its-own-predicate.md).
Supersedes [ADR 0198](0198-deleting-an-auto-accept-rule-removes-it-from-scoped-tokens.md).

## Context

A scoped token named a subset of the person's Auto-accept rules by id (ADR 0164). That tied the
token to the person's own access. Two cases show the cost:

- A `privacyfence-sync` job has no AI model and no approval card. The only way to let one of its
  calls through was to add an Auto-accept rule, and that rule then also auto-accepted the same
  calls in every interactive chat.
- A Claude Code routine runs in an environment of its own that reaches `/mcp` with a Bearer token
  and a narrow tool list. It needs a fixed set of rules for its calls, not a share of the rules of
  the person's other clients.

Naming rules by id also needed machinery to stay correct: re-pointing tokens when rule ids changed
(ADR 0172, ADR 0173), pruning a deleted rule's id from tokens (ADR 0198), and a per-rule record of
the verbs a token may use (ADR 0166, ADR 0228).

## Decision

- **A token stores its rules as `ScopeRow`s**, in the same format as the `auto_accept:` rows
  (`scope_rows.row_to_dict` / `row_from_dict`). Predicates come from the catalogue when a row is
  compiled, so a catalogue change that `compile_row` already handles reaches token rows too. A
  row's stored operations bound it, so a verb that later gains a tool does not widen a token. A row
  that no longer parses is dropped from that token with a warning; that only narrows it. A change
  to the row format that needs a rewrite on disk must rewrite token rows too.
- **The token file is version 2.** An entry has `rules` and no `rule_ids`, `rule_limits` or
  `limits_split_verbs`. A token written before this change loads with no rules and keeps its
  stored entry until it is converted.
- **Conversion happens at start-up only** (`policy_upgrade.ensure_token_rules`, and inside the
  settings-version-2 conversion that already ran). Each legacy token gets its own copy of the
  rules it named, with its recorded limits applied. A rule id that names no rule any more, or
  whose limit leaves nothing, is left out. A token whose rule was flagged as allowing personal data
  keeps the transfer of that flag to the token (ADR 0173). An ordinary Auto-accept write never
  touches the token file.
- **A scoped call reads only the token's rules** (`gate._rules_in_scope`). There is no fallback to
  the person's Auto-accept rules. `privacyfence_check_policy` predicts from the token's rows alone.
- **Deleting or changing an Auto-accept rule changes no token.** ADR 0198's pruning is removed.
- **Scoped-token calls are left out of the Auto-accept usage counts** ("Matched Nx"), because a
  copied row keeps the id of the rule it was copied from and would otherwise count for it.
- **The create request carries `rules`**: each entry is `new` (a scope, a value and verbs, built
  as the Auto-accept page's Add rule builds them; a blank value for a scope that takes one is
  refused, so it cannot become an "any resource" rule), `copy` (an exact copy of one of the person's
  Auto-accept rows) or `duplicate` (an exact copy of a row of one of the person's tokens). The
  request is normalised and sorted, so the step-up fingerprint covers every rule request. At most
  50 rules per token. Exact copies are built by `scope_rows.rows_for_rules`.
- **The New token form** adds rules for the token itself, with the same scope, value and verb
  controls as the Auto-accept page, and offers the person's Auto-accept rows as copies. Duplicate
  copies the rules of an existing token. Tokens stay immutable; Duplicate covers changes.
- **Unchanged:** deny-only, no cards, `denied_scoped_token`, expiry, the tool list, both
  personal-data flags, the audit identity `scoped-token:<id>`, and the step-up check.

## Alternatives considered

- **Storing compiled rules on disk** — a predicate rename would need its own token rewrite, as the
  ADR 0228 split needed one.
- **Ids plus a pinned copy** — two sources of truth again.
- **Keeping the person's rules as a fallback** — a token would still gain whatever the person
  later allows interactively, and the token's reach would stay hard to state.
- **A token-only flag on Auto-accept rules** — still one list and one id space; the rules would
  show up beside the person's own, and deleting or updating one would still need pruning.
- **Editing a token's rules in place** — tokens are immutable credentials; revoking and creating
  a new one is already how a change is made, and Duplicate pre-fills the form.
- **Converting on every Auto-accept write** — a write on a version-3 section would then depend on
  the token file and its lock order.

## Consequences

- The person's other clients never gain access from a token rule.
- Deleting an Auto-accept rule no longer narrows a token; revoking the token is the way.
- An Auto-accept rule's Update rule does not reach a token.
- "Matched Nx" no longer counts token calls.
- An older PrivacyFence cannot read a version-2 token file: it skips those entries, so the tokens
  stop working there, and that version's next token change rewrites the file without them.
- A future row-format rewrite must rewrite token rows too (documented in
  `policy_upgrade`).
- A legacy token allows no rule-covered call between the upgrade and the start-up conversion; a
  warning says so on each reload.

## Verification

- `tests/unit/policy/test_scope_rows.py`: `TestRowsForRules` (every catalogue group round-trips
  through storage).
- `tests/unit/test_scoped_tokens.py`: `TestTokenRows`, `TestOwnedEntry`, `TestOwnLegacyRules`,
  `TestRuleRequests`.
- `tests/unit/test_policy_upgrade.py`: `TestEnsureTokenRules`.
- `tests/unit/test_gate.py`, `test_auto_accept.py`, `test_settings_controller.py`,
  `web/test_routes_org_settings.py`: `*_leaves_scoped_tokens_alone`.
- `tests/unit/test_audit_log.py`: `test_rule_usage_leaves_out_scoped_token_calls`.
- `tests/unit/web/test_server.py`: `scope_groups` and per-verb tool lists.
- `tests/integration/test_settings_scoped_token_form_browser.py`: the New token form.

## Related

- ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md),
  [0166](0166-a-stored-rule-is-widened-to-its-verbs-current-operations-only-by-an-explicit-confirmed-update.md),
  [0172](0172-one-stored-auto-accept-rule-per-scope.md),
  [0173](0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md),
  [0198](0198-deleting-an-auto-accept-rule-removes-it-from-scoped-tokens.md),
  [0220](0220-a-routines-rule-profile-is-a-scoped-token.md),
  [0228](0228-an-extra-scope-can-give-each-verb-its-own-predicate.md)
- [`scoped_tokens.py`](../../src/privacyfence/scoped_tokens.py),
  [`policy/scope_rows.py`](../../src/privacyfence/policy/scope_rows.py),
  [`policy_upgrade.py`](../../src/privacyfence/policy_upgrade.py),
  [`gate.py`](../../src/privacyfence/gate.py)
