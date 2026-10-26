# ADR 0234: Slack message reads report the user's own read state from Slack's read markers

## Status

Accepted — 2026-10-26. Implemented. Relates to ADR 0226 and ADR 0227.

## Context

A routine that triages Slack reads it by time. None of the Slack read tools said whether the user
had already read a message, so the routine could only sort by time and re-reported messages the
user had seen.

Slack keeps the answer in the user's own read markers. `conversations.info` reports the marker of a
conversation as `last_read`. `conversations.replies` puts the marker of a thread on the parent
message (`last_read`, with `subscribed: true`) when the user follows the thread. The connector
already makes the `conversations.info` call before every channel-history and thread read, to learn
membership ([ADR 0226](0226-a-slack-rule-can-cover-the-conversations-the-user-is-a-member-of.md)), and discards the
marker it carries.

Search is different: a search over many conversations has no per-conversation `conversations.info`
call, and it costs a request per conversation to add one before the approval card appears
([ADR 0227](0227-member-only-slack-search-drops-other-conversations-before-the-gate.md)).

## Decision

- **A message is `unread` when its `ts` is after the marker, `read` when it is not, and `unknown`
  when there is no marker or no usable `ts`.** The comparison is exact (`decimal.Decimal`, not
  `float`): see `message_read_state` in `src/privacyfence/slack_client.py`. A message the user wrote
  is `read`. Slack's "never read" marker (`0000000000.000000`) makes every message unread.
- **`slack_get_channel_history` and `slack_get_thread_replies` always return `read_state`.** The
  marker comes from the `conversations.info` response the read already makes
  (`SlackClient.conversation_state`, which `resolve_is_member` now wraps), and for a thread reply
  from the parent's `last_read`. No extra Slack request. A reply page without its parent, or a
  thread the user does not follow, reads `unknown`.
- **`slack_search_messages` returns it only with `read_state=true`.** That costs one
  `conversations.info` request per conversation in the results. A reply in a thread reads
  `unknown`, because Slack tracks thread replies separately from the channel. A reply is recognised
  from the match's `thread_ts`, or from the `thread_ts` parameter of its permalink.
- **The marker is read live and never cached.** It moves whenever the user reads, and a routine's
  re-searches would otherwise see stale state, as with membership.
- **The approval card is unchanged and no rule reads `read_state`.** Read state is the user's own
  Slack metadata, not message content.
- **`slack_list_dms` and `slack_list_group_chats` are unchanged.**

## Alternatives considered

- **Read state always on in search** — every search would pay one `conversations.info` per
  conversation before its approval card appears. Opt-in was chosen.
- **Read state on `slack_list_dms` and `slack_list_group_chats`** — one `conversations.info` per
  conversation: about 12 minutes for 600 DMs at 50 requests a minute. The history reads a fallback
  makes already carry it.
- **`unread_count` / `unread_count_display` from `conversations.info`** — Slack documents these only
  for some conversation types, and a count does not say which messages are unread. A per-message
  comparison against `last_read` does.
- **Judging a thread reply by the channel marker** — Slack tracks thread replies separately, so this
  would call a reply read or unread wrongly. Search marks replies `unknown`.
- **Caching markers between calls** — stale after the user reads; see above.
- **Showing read state on the approval card** — it is not message content, and the card's columns
  are covered by preview-minimisation tests.

## Consequences

- A history or thread read costs no extra request for read state.
- A search with `read_state=true` can be rate-limited (`conversations.info` is Tier 3, 50 a minute);
  the tool description says so and points to `slack_get_read_state`
  ([ADR 0235](0235-a-separate-auto-approved-tool-checks-the-read-state-of-named-messages.md)).
- A reply in a search result stays `unknown` until the agent asks for the thread.
- The approach rests on `conversations.info` reporting `last_read` to a user token for channels,
  group DMs and 1:1 DMs. A conversation without a marker reads `unknown`, which is safe.

## Verification

- `tests/unit/test_slack_client.py`: `TestMessageReadState`, `TestConversationState`,
  `TestLastReadMarkers`, `TestThreadLastRead`.
- `tests/unit/connectors/test_slack_connector.py`: `TestReadState`.
- `scripts/qa_fixture_recorder.py` `check_slack`: `conversation_read_marker`,
  `group_dm_read_marker` and `dm_read_marker` run against real Slack in `qa-record-fixture.yml`.

## Related

- [ADR 0226](0226-a-slack-rule-can-cover-the-conversations-the-user-is-a-member-of.md),
  [ADR 0227](0227-member-only-slack-search-drops-other-conversations-before-the-gate.md),
  [ADR 0235](0235-a-separate-auto-approved-tool-checks-the-read-state-of-named-messages.md)
- `src/privacyfence/slack_client.py`, `src/privacyfence/connectors/slack.py`
