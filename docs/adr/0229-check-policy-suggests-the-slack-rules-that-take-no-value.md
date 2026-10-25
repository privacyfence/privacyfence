# ADR 0229: check_policy suggests the Slack rules that take no value, from the call's own arguments

## Status

Accepted — 2026-10-23. Implemented: `policy.suggest._VALUELESS_EXTRAS`.
Amends ADR 0204.

## Context

ADR 0204 made `privacyfence_check_policy` list the rules that would cover an uncovered call, and
skipped every extra scope with `needs_value=False` so unconditional rules (`gmail.filters`,
`slack.group_chats`) were never suggested. That filter also dropped "Slack — mentions of me",
"Slack — direct messages to me" and "Slack — conversations I'm a member of". They take no value, but
they are not unconditional: each narrows to a property of the fetched item. A runner asking about a
Slack mention search or a read of a DM only ever got the per-channel rule.

## Decision

- **A value-less Slack scope is suggested with `value: []` and `value_known: true`** ("the rule takes
  no value"), plus the usual `group`, `label`, `verb` and `operation`. It may carry `requires_args`,
  a dict of arguments the call must also pass for the rule to cover it; it is present only when
  non-empty. Every other suggestion is unchanged.
- **Applicability is a function of `(tool, args)` only.**
  - `slack.mentions_of_me`: `slack_get_thread_replies` (read) and `slack_search_messages` (search).
  - `slack.direct_messages_to_me`: `slack_get_channel_history` and `slack_get_thread_replies` when
    `channel_id` starts with `D`.
  - `slack.my_conversations`: those two reads with a non-empty `channel_id`; a search with no
    `requires_args` when it passes `member_only=true`, otherwise with
    `requires_args: {"member_only": true}`.
- **Unconditional scopes stay out.** A guard test requires every `needs_value=False` extra scope to
  be in the table or to have the predicate `gmail.anything` or `slack.anything`.
- **Value-less suggestions come after all others,** in `EXTRA_SCOPES` order. A missing predicate or a
  failing applicability check skips the suggestion.

## Alternatives considered

- **Calling `conversations.info` or `users.conversations` from `check_policy`** — Slack rate-limits
  both per method per workspace, the real call already spends one of each, and a runner that
  pre-checks every step would double them. The answer could not be cached either (ADR 0226).
- **Reading the Slack client's in-memory caches** — the prediction would depend on what happened to
  be cached, and the policy layer would reach into the connector.

## Consequences

- The search suggestion for the conversations rule needs `member_only`, and says so.
- The DM suggestion relies on the `D` prefix. A `D` id that turns out to be a self-DM is still
  covered, because `is_im` includes it.
- No Slack request is added.
- Suggestions never read the person's rules (ADR 0204), so a person who already has one of these rules
  still sees it listed while `check_policy` says `unknown`, as for run-time-value suggestions today.
- The `privacyfence-sync` `needed` summary merges the `requires_args` and plain variants of
  `slack.my_conversations` into one line.

## Verification

- `tests/unit/policy/test_suggest.py` (`TestSlackValuelessSuggestions`, the guard and fail-closed tests).
- `tests/unit/web/test_mcp_dispatch.py` (no connector call, `is_member` ignored).
- `sync/tests/test_preflight.py`, `sync/tests/test_permissions.py`.

## Related

- ADR [0204](0204-check-policy-names-the-rules-that-would-cover-an-uncovered-call.md), ADR [0221](0221-slack-rules-cover-mentions-of-the-signed-in-user.md), ADR [0226](0226-a-slack-rule-can-cover-the-conversations-the-user-is-a-member-of.md), ADR [0228](0228-an-extra-scope-can-give-each-verb-its-own-predicate.md)
- [`suggest.py`](../../src/privacyfence/policy/suggest.py)
