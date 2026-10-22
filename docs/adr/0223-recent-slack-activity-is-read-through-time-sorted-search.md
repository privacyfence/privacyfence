# ADR 0223: Recent Slack activity is read through a time-sorted search

## Status

Accepted — 2026-10-22. Implemented: `SlackClient._search_recent` and the `minutes` parameter of `slack_search_messages` and `slack_get_channel_history`.

## Context

An agent could not ask "what happened in my Slack in the last hour?" across every channel, 1:1 DM,
group DM and thread reply. `slack_search_messages` filtered only by whole days, needed search text
or a participant, and returned Slack's relevance order. `slack_get_channel_history` had no time
filter at all.

Apps distributed outside the Slack Marketplace get 1 request per minute and 15 messages per call on
`conversations.history` and `conversations.replies` (`docs/slack-setup.md`), so reading every
conversation one by one is not an option.

## Decision

A "last N minutes" Slack read across conversations uses one `search.messages` request sorted newest
first, with the exact cutoff applied to each match in the client. Each conversation's own history is
read only for one named conversation (`slack_get_channel_history`) or participant.

`minutes` (1-10080) on both tools overrides `days`, and on `slack_search_messages` makes `query`
and `participant` optional.

## Alternatives considered

- **Reading every conversation's history with `oldest`** — exact, but costs one
  `conversations.history` call per conversation plus one `conversations.replies` call per active
  thread. At 1 request per minute for apps outside the Marketplace, that is tens of minutes on an
  ordinary workspace.
- **A separate `slack_recent_activity` tool** — it would need its own row in `auto_accept.py`'s
  operation, gate and personal-data tables, in `policy/registry.py`'s verbs, and in the scope
  selectors, for the same `slack.read_messages` operation that `slack_search_messages` already has.
- **Sorting the existing `days` search by timestamp too** — changes what today's searches return.
  Out of scope.
- **Events API / Socket Mode** — needs an always-on listener that stores messages, a different trust
  and storage model from PrivacyFence's on-demand reads.

## Consequences

- Search-index delay can miss a just-posted message; the tool description says so.
- One search request per call. Results are capped at `count` (default 20, at most 100) and keep ADR
  0116's bare-list shape.
- A minutes-only search shows an empty sender in the audit entry.

## Verification

The `test_minutes_*` tests in `tests/unit/test_slack_client.py` and
`tests/unit/connectors/test_slack_connector.py`, and the Recent window check in
`docs/connector-qa.md`.

## Related

- ADR 0116 (list tools page inside the client and keep their return shape)
- `docs/slack-setup.md`
