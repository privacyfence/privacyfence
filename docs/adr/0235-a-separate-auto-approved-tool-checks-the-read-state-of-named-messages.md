# ADR 0235: A separate, auto-approved tool checks the read state of named messages

## Status

Accepted — 2026-10-26. Implemented as `slack_get_read_state`. Relates to ADR 0234.

## Context

[ADR 0234](0234-slack-reads-report-the-users-own-read-state.md) makes search report read state only
on request, because it costs a Slack request per conversation. A routine usually needs the answer
for only a few messages it is about to report, not for every search result. Reading those messages
again just to learn their state would repeat the content and the approval.

## Decision

- **`slack_get_read_state` takes 1 to 20 messages the agent already holds**, each `channel_id:ts`
  for a top-level message or `channel_id:ts:thread_ts` for a reply, with at most 3 distinct threads.
  A repeated entry is checked once. It returns `{channel_id, ts, thread_ts, read_state}` per
  message, in request order, and no message text.
- **A conversation costs one `conversations.info` request; a thread costs one `conversations.replies`
  request** for the parent's `last_read`. Input is validated before any Slack call.
- **It is auto-approved** (`TOOL_TO_GATE`: `auto`) and writes an audit entry per call.
- **The tool does not know the author**, so it never reports a message as `read` because the user
  wrote it.
- **Three threads per call at most.** `conversations.replies` is limited to 1 request a minute for an
  app distributed outside the Slack Marketplace, and the SDK's rate-limit retry holds the call while
  it waits; three threads keep the worst case near three minutes.

## Alternatives considered

- **Review-gating it** — it returns no content. A card per check would defeat the targeted use the
  tool exists for.
- **More than 3 thread lookups per call** — the worst case waits minutes per extra thread on a
  distributed app.
- **Only `read_state=true` on search** — a large sweep is easily rate-limited, and the agent needs
  the state of only a handful of messages.

## Consequences

- An agent asks only about the messages that matter.
- The tool accepts any `channel_id:ts` the agent makes up. With 20 probes a call it can narrow down
  the user's read marker, and with it when the user last read a conversation. That is behavioural
  metadata about the user's own reading, inside conversations the user can see; the audit log
  records every call. This is accepted.
- A thread the user does not follow reads `unknown`.

## Verification

- `tests/unit/connectors/test_slack_connector.py`: `TestGetReadState`.
- `src/privacyfence/auto_accept.py` `TOOL_TO_GATE`; `docs/tools-reference.md` (generated);
  `tests/unit/connectors/test_readme_manifest_alignment.py`.

## Related

- [ADR 0234](0234-slack-reads-report-the-users-own-read-state.md)
- `src/privacyfence/connectors/slack.py` `_get_read_state`
