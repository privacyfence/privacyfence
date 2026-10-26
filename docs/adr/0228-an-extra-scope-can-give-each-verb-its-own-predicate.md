# ADR 0228: An extra scope can give each verb its own predicate, so a rule allows only the verbs it was saved with

## Status

Accepted — 2026-10-22. Implemented: `PolicyExtraScope.verb_predicates`.
Amends ADR 0221.
Amended by [ADR 0236](0236-a-scoped-token-holds-its-own-rules.md): a token holds its own copy of its rules.

## Context

The engine matches a rule on its operation key, not on its verbs. `slack_get_channel_history`,
`slack_get_thread_replies` (read) and `slack_search_messages` (search) share the key
`slack.read_messages`. An extra scope had one predicate, so a "Slack — mentions of me" row saved with
only `read` still auto-accepted a mentions search, although Settings showed "allow read".

## Decision

- **`PolicyExtraScope` gains `verb_predicates`,** the predicate a rule for a given verb uses when it
  is not `predicate`. A row compiles to one rule per predicate its verbs need, and
  `scope_of_rule`, `describe` and `outdated` recognise every predicate of a scope.
- **Mentions** gets `slack.mentions_of_me_all_results` for search. `slack.mentions_of_me` keeps only
  the thread case (ADR 0221 amended: a mentions rule's search verb is now its own predicate).
- **`slack.my_conversations`** (ADR 0226) uses the same mechanism for its search verb.
- **Older scoped tokens keep their reach** through `_LIMIT_PAIR_SUCCESSORS` in `scoped_tokens.py`,
  gated by a stored `limits_split_verbs` flag that tokens made from now on carry.

## Alternatives considered

- **Two catalogue groups** (one for read, one for search) — the exclude list would be entered twice,
  and the mentions bug would still need its own mechanism.
- **Telling the engine about verbs** — it would change every rule's evaluation path for the one
  operation key that carries two verbs.

## Consequences

- A stored mentions row with `{read, search}` compiles to two rules and behaves as before; one with
  only `{read}` no longer covers searches. This is the fix, and the changelog says so.
- A token made before the change on a read+search mentions row keeps its search through
  `_LIMIT_PAIR_SUCCESSORS`; one on a read-only row loses it. If search is later added to that row, a
  token from before the change gains it back, and expires within its day limit. Tokens made after the
  change never get successors, so tokens only narrow (ADR 0172, ADR 0164).
- Any v2 `slack.mentions_of_me` rule that still exists migrates to a read-only row.
- Another scope that splits its predicates later needs an entry in `_LIMIT_PAIR_SUCCESSORS` for old
  tokens.

## Verification

- `tests/unit/policy/test_catalogue.py`, `test_scope_rows.py`, `test_scopes.py`, `test_describe.py`,
  `test_engine.py`.
- `tests/unit/test_scoped_tokens.py` (limit successors).

## Related

- ADR [0164](0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md), ADR [0172](0172-one-stored-auto-accept-rule-per-scope.md), ADR [0221](0221-slack-rules-cover-mentions-of-the-signed-in-user.md), ADR [0226](0226-a-slack-rule-can-cover-the-conversations-the-user-is-a-member-of.md)
- [`catalogue.py`](../../src/privacyfence/policy/catalogue.py), [`scope_rows.py`](../../src/privacyfence/policy/scope_rows.py), [`scoped_tokens.py`](../../src/privacyfence/scoped_tokens.py)
