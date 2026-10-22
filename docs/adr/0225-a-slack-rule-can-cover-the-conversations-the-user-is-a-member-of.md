# ADR 0225: A Slack rule can cover the conversations the signed-in user is a member of, checked live on each call

## Status

Accepted — 2026-10-22. Implemented: the `slack.my_conversations` scope.
Relates to ADR 0172 and ADR 0221.

## Context

A scheduled triage routine reads Slack by time: everything since its last run, in every conversation
the signed-in user is in, with no fixed channel list. The existing Slack scopes name listed channels,
a channel kind, the user's own DM, every 1:1 DM or mentions. None says "a conversation I am in".

A user token with `channels:history` can also read a public channel the user is *not* in, so
membership has to be checked, not assumed.

## Decision

- **A catalogue scope, `slack.my_conversations`,** "Slack — conversations I'm a member of", with the
  verbs read and search on the `slack.read_messages` operation. It takes an optional list of channel
  ids the rule never covers (HR, legal).
- **Read** (`slack_get_channel_history`, `slack_get_thread_replies`) matches when the connector's
  computed `is_member` argument is `True` and the channel is not excluded. `is_member` comes from one
  live `conversations.info` call (`SlackClient.resolve_is_member`); a 1:1 DM counts as membership.
- **Search** matches when the agent passed `member_only=true` and the connector computed
  `all_results_in_my_conversations`, from a live `users.conversations` walk
  (`SlackClient.my_conversation_ids`), and no result is from an excluded channel. An empty result
  matches, because it discloses nothing.
- **Membership is never cached.** A rule decision must not outlive leaving a channel.
- Both computed arguments are stripped from an agent's `privacyfence_check_policy` arguments, and the
  scope resolves from the fetched item, so `check_policy` answers `unknown`.

## Alternatives considered

- **Caching membership** (as the member display cache does) — a decision could outlive leaving a
  channel.
- **`users.conversations` on every history or replies read** — that is ⌈conversations/200⌉ requests
  per read, against one `conversations.info` request. The same response also fills the group-DM and
  1:1-DM caches, so the connector's other channel-type checks make no request of their own.
- **A channel-kind filter on the rule** — `slack.direct_messages_to_me` already covers every 1:1 DM
  and `group_dm` covers every group DM. Nobody asked for a channels-only rule.

## Consequences

- Each history or replies read costs one extra Tier 3 `conversations.info` request.
- Excluded ids are shown raw in Settings, and the agent has to pass the same ids as
  `exclude_channels` on a member-only search (ADR 0226); if it does not, a result from an excluded
  channel makes the search need a review. That fails closed.
- If `conversations.info` did not report `is_member: true` for a group DM the user is in, group DMs
  would fail closed under this rule and stay covered by `group_dm`.
- `SlackClient._conversation_ids_for_user` returned Slack's channel objects instead of ids, which
  broke the participant fast path on a real workspace; it is fixed in the same change set.

## Verification

- `tests/unit/policy/test_scopes.py`, `tests/unit/policy/test_catalogue.py`,
  `tests/unit/policy/test_describe.py`.
- `tests/unit/test_slack_client.py` (membership and id walks).
- `tests/unit/connectors/test_slack_connector.py` (`TestMemberConversationRules` pairs the captured
  gate input with the real engine).
- `scripts/qa_fixture_recorder.py` `check_slack` (`conversation_membership`,
  `group_dm_membership`) against live Slack.

## Related

- ADR [0172](0172-one-stored-auto-accept-rule-per-scope.md), ADR [0221](0221-slack-rules-cover-mentions-of-the-signed-in-user.md), ADR [0226](0226-member-only-slack-search-drops-other-conversations-before-the-gate.md)
- [`scopes.py`](../../src/privacyfence/policy/scopes.py), [`catalogue.py`](../../src/privacyfence/policy/catalogue.py), [`slack.py`](../../src/privacyfence/connectors/slack.py), [`slack_client.py`](../../src/privacyfence/slack_client.py)
