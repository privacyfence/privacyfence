# ADR 0226: Member-only Slack search drops results from other conversations before the gate, on the agent's request

## Status

Accepted — 2026-10-22. Implemented: `member_only` and `exclude_channels` on `slack_search_messages`.
Relates to ADR 0221 and ADR 0223.

## Context

Search includes public channels the user is not in, so one unfiltered result from such a channel
stops a membership rule (ADR 0225) from covering the whole page. A routine's "everything since my
last run" sweep must stay inside Slack's rate limits:

| Method | Tier (requests/min) |
|---|---|
| `conversations.history`, `conversations.replies` | Tier 3 (50); 1/min and 15 messages for an app distributed outside the Marketplace |
| `conversations.info`, `users.conversations` | Tier 3 (50) |
| `search.messages` | Tier 2 (20), at most 100 matches per page |

Reading N conversations one by one costs N `conversations.history` plus N `conversations.info`
requests: about 2 minutes for N=50 on an internal app, about 50 minutes on a distributed one.

## Decision

- `slack_search_messages` takes two request arguments. `member_only` keeps only results from
  conversations the user is a member of right now; `exclude_channels` is a comma-separated list of
  channel ids whose results are dropped. Both appear in the card's preview and in the recorded
  request arguments, so the result is not silently partial.
- The client reads `users.conversations` once, then up to `_FILTERED_SEARCH_PAGE_CAP = 3` pages of
  `search.messages` (100 matches each), filtering the raw matches before parsing them. A sweep is
  one membership walk plus 1–3 search requests.
- If three pages hold fewer than `count` kept results, the client logs a warning; the parameter text
  tells the agent to search again with a smaller `minutes` when it gets exactly `count` results.
- A member-only search that finds nothing is an empty page and auto-accepts under the rule.
- If the membership walk fails, the call raises instead of searching unfiltered.

## Alternatives considered

- **Per-conversation sweeps** — one `conversations.history` and one `conversations.info` request per
  conversation, so the minutes noted above for N=50, and a one-request-per-minute floor for apps
  distributed outside the Marketplace.
- **A last-activity timestamp on the listings** — `conversations.list` returns none (its `updated`
  is when the channel object last changed), so the time would cost a `conversations.info` or a
  one-message `conversations.history` request per conversation.
- **Filtering a mixed page inside the rule** — ADR 0221 rejected the same for mentions. The filter
  is an argument the agent passes, so the result is not silently partial.

## Consequences

- Search-index delay can miss a just-posted message (ADR 0223).
- In a busy window the three-page cap can miss older messages; the agent narrows `minutes`.
- The tool description is at the 1024-character cap, so the new behaviour lives in the parameter
  descriptions.

## Verification

- `tests/unit/test_slack_client.py` (filtered search, page cap).
- `tests/unit/connectors/test_slack_connector.py` (arguments, preview, `TestMemberConversationRules`).
- `scripts/slack_call_budget.py` prices the call counts per client entry point.

## Related

- ADR [0221](0221-slack-rules-cover-mentions-of-the-signed-in-user.md), ADR [0223](0223-recent-slack-activity-is-read-through-time-sorted-search.md), ADR [0225](0225-a-slack-rule-can-cover-the-conversations-the-user-is-a-member-of.md)
- [`slack_client.py`](../../src/privacyfence/slack_client.py), [`slack.py`](../../src/privacyfence/connectors/slack.py), [`slack_call_budget.py`](../../scripts/slack_call_budget.py)
