# ADR 0221: Slack rules cover mentions of the signed-in user

## Status

Accepted — 2026-10-22. Implemented.
Relates to ADR 0172.

## Context

A triage routine should read the Slack messages that address the person, and nothing else. The Slack
scopes name channels, channel kinds or the person's own DM, none of which says "this message mentions
me". Slack keeps a mention as raw markup in the message text, `<@U…>` or `<@U…|name>`, and
`SlackClient.own_user_id()` returns the signed-in user's id.

## Decision

- **A value-less attribute scope, `slack.mentions_of_me`,** with the verbs read and search, in the
  catalogue's extra scopes (not in the frozen selector table of the old evaluator). It governs the
  existing `slack.read_messages` operation.
- **The connector computes `own_user_id`** for the thread and search calls and the selector reads it
  from the call's arguments; an empty id never matches. The name is a connector-computed argument, so
  an agent cannot send it to `privacyfence_check_policy`.
- **The match depends on the tool.** `slack_search_messages`: results exist and every result mentions
  the user. `slack_get_thread_replies`: at least one message of the thread, parent or reply, mentions
  the user, and the agent receives the whole thread. Any other tool, `slack_get_channel_history`
  included, never matches.
- **The scope resolves from the fetched item,** so `check_policy` answers `unknown`.
- **Whole thread.** Slack offers no parent-only read, so a thread with one mention is read in full.
  This is a product decision.

## Alternatives considered

- **Filter a mixed search page down to the mentions.** An unattended read would return a partial page
  and the audit log would not show the filtering.
- **A parent-only thread rule.** No tool reads only a parent.
- **Add the selector to the frozen table.** That table is pinned against the old evaluator.

## Consequences

- Anyone who can reply in a shared channel can make a thread readable by mentioning the user, and with
  the token's reads flag (ADR 0219) its personal data too. This is accepted.
- A search page that mixes mention and other results is not covered; the agent has to search so that
  every result mentions the user.
- The one-to-one DM scope, `slack.direct_messages_to_me`, is an ordinary attribute scope that the
  connector feeds with a computed flag; it needs no decision of its own.

## Verification

- `tests/unit/policy/test_scopes.py` (`TestSlackMentionsOfMe`, `TestSlackDirectMessagesToMe`),
  `tests/unit/policy/test_catalogue.py` (`TestSlackExtras`), `tests/unit/policy/test_engine.py`
  (`TestSlackScopes`), `tests/unit/policy/test_describe.py` (`TestExtraScopeVerbs`).
- `tests/unit/connectors/test_slack_connector.py` (`TestComputedScopeArgs`),
  `tests/unit/test_slack_client.py` (`TestResolveIsOneToOneDm`).
- `tests/unit/web/test_mcp_dispatch.py` (computed arguments are not passed to preflight).

## Related

- ADR [0172](0172-one-stored-auto-accept-rule-per-scope.md), ADR [0219](0219-a-scoped-token-can-let-email-and-slack-reads-return-personal-data.md)
- [`scopes.py`](../../src/privacyfence/policy/scopes.py), [`catalogue.py`](../../src/privacyfence/policy/catalogue.py), [`slack.py`](../../src/privacyfence/connectors/slack.py), [`slack_client.py`](../../src/privacyfence/slack_client.py)
