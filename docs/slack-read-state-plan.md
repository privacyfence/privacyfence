# Plan: Slack reads tell the agent what is unread

## Goal

There is no GitHub issue for this; the request came from the maintainer directly.

A scheduled Slack triage routine reads Slack by time. Its main path is `slack_search_messages`: a
member-only sweep (`minutes`, `member_only=true`, `count=100`, empty `query`, split in halves when
exactly 100 come back) and a mentions search for the user's own id over the same window. It reads
`slack_get_thread_replies` only when it needs a thread to decide which bucket a message goes in.
If the sweep is denied, it falls back to `slack_list_dms`, `slack_list_group_chats` and
`slack_get_channel_history` with `minutes`, once per DM and group DM, and notes "slack sweep
denied, DMs only" in `Skipped_sources`.

None of these tools say whether the user has already read a message, so the routine can only sort
by time. After this change:

- `slack_get_channel_history` and `slack_get_thread_replies` return a `read_state` on every message
  (`"unread"`, `"read"` or `"unknown"`). This needs no extra Slack request.
- `slack_search_messages` takes a new `read_state` argument (default false). With
  `read_state=true` it returns the same field, at the cost of one `conversations.info` request per
  conversation in the results. Its description tells the agent to use this with care because it is
  easily rate-limited, and points to the new tool.
- A new auto-approved tool, `slack_get_read_state`, returns the read state of up to 20 specific
  messages the agent already holds. The agent uses it for the few messages whose read state matters,
  not for every search result.
- `slack_list_dms` and `slack_list_group_chats` do not change. In the fallback, each history read
  already returns read state for free (decided with the user).

## Current state

- `src/privacyfence/connectors/slack.py:44` `_message_to_dict` returns `{ts, channel_id,
  channel_name, user_id, user_name, text, thread_ts, reply_count}`. There is no read state anywhere.
- `src/privacyfence/slack_client.py:324` `SlackMessage` has no read fields. `_parse_message`
  (`:2024`) ignores the raw `last_read`.
- `src/privacyfence/slack_client.py:1674` `resolve_is_member` makes one live `conversations.info`
  call, never cached (ADR 0226). It reads `is_im`, `is_mpim`, `is_member` and `user`, and drops
  `channel.last_read`. The connector calls it first in `_get_channel_history` (`slack.py:605`) and
  `_get_thread_replies` (`slack.py:675`). So both reads already pay for the request that carries the
  conversation's read marker.
- `conversations.replies` puts the user's thread marker on the parent message (`last_read`, with
  `subscribed: true`) when the user follows the thread. This is confirmed live:
  `tests/fixtures/live/slack/get_thread_replies.json` has `"last_read"` and `"subscribed": true` on
  the parent (`ts == thread_ts`).
- `slack_search_messages` (`slack.py:327`): its description is 1023 characters, against the
  1024-character cap that `tests/helpers.py:92` and `tests/unit/web/test_tool_schema_portability.py:45`
  enforce (ADR 0227 notes this). The member-only sweep is one `users.conversations` walk plus 1–3
  `search.messages` pages (`slack_client.py:911` `_search_filtered`).
- Search matches are parsed by `_parse_search_match` (`slack_client.py:965`). It takes `thread_ts`
  only from the raw match. A search match's `permalink` carries the thread root as a `thread_ts`
  query parameter for a reply (`resolve_permalink`, `slack_client.py:1150`). That is the reliable
  way to tell a reply apart in search results.
- Rate limits (`docs/slack-setup.md:40`): `conversations.info` is Tier 3 (50/min).
  `conversations.replies` is Tier 3, but only 1/min and 15 objects for an app distributed outside
  the Marketplace. `scripts/slack_call_budget.py` prices call counts per client entry point.
- Tool tables: an `auto` tool needs a row in `auto_accept.TOOL_TO_GATE` (`auto_accept.py:268` area,
  enforced by `tests/unit/connectors/test_readme_manifest_alignment.py`) and a row in the generated
  `docs/tools-reference.md` (`python3 scripts/generate_tools_reference.py`, enforced by
  `tests/unit/test_docs_tools_reference.py`). A tool needs no `TOOL_TO_OPERATION` or `TOOL_TO_VERB`
  entry unless it is `review` or `popup`.
- The connector tests' `make_connector` (`tests/unit/connectors/test_slack_connector.py:42`) gives a
  `MagicMock` client with plain defaults. Tests at `:921`, `:926`, `:934`, `:1018`, `:1027` and
  `:1036` set `client.resolve_is_member`.
- The QA recorder's `check_slack` (`scripts/qa_fixture_recorder.py:2195`) already runs live
  membership checks through `_check_slack_membership` (`:2180`), tested in
  `tests/unit/test_qa_fixture_recorder.py:2212` (`_membership_client`).

## Design

### D1. Read-state values and the comparison rule

These module-level names go in `slack_client.py`, after `SlackClientError`:

```python
READ_STATE_UNREAD = "unread"
READ_STATE_READ = "read"
READ_STATE_UNKNOWN = "unknown"


def message_read_state(ts: str, last_read: str, *, own: bool = False) -> str:
    """Whether the message at ``ts`` is after the signed-in user's Slack read marker ``last_read``.
    A message the user wrote is read. No marker, no ts, or a value that is not a Slack timestamp
    reads as unknown."""
```

The rules, in order:

1. If `own` is true, return `"read"`.
2. If `ts` or `last_read` is empty, return `"unknown"`.
3. Compare as `decimal.Decimal(ts) > decimal.Decimal(last_read)`. If true, return `"unread"`;
   otherwise `"read"`. On `decimal.InvalidOperation` or `TypeError`, return `"unknown"`.

Use `Decimal`, not `float`: the comparison is exact, and a value that is not a Slack timestamp
raises `InvalidOperation` instead of parsing as something else. Slack's "never read" marker, `"0000000000.000000"`, makes every
message unread, which is correct.

### D2. Conversation state (client)

```python
@dataclass
class SlackConversationState:
    """The signed-in user's own state in one conversation, from one live conversations.info call.
    Never cached: membership decides auto-accept rules (ADR 0226), and the read marker moves
    whenever the user reads."""

    is_member: bool = False
    last_read: str = ""
```

`SlackClient.conversation_state(self, channel_id: str) -> SlackConversationState` takes over the
whole body of today's `resolve_is_member`: the same call, the same cache fills (`_channel_is_mpim_cache`,
`_im_channel_cache`, `_im_counterpart_cache`) and the same failure handling. It returns
`SlackConversationState()` for an empty id, a `SlackApiError` or a non-dict `channel`. Otherwise it
returns `SlackConversationState(is_member=is_im or channel.get("is_member") is True,
last_read=str(channel.get("last_read") or ""))`.

`resolve_is_member(channel_id)` becomes `return self.conversation_state(channel_id).is_member`. Keep
its docstring's first sentence, and say that it is `conversation_state`'s `is_member`.

`SlackClient.last_read_markers(self, channel_ids: list[str]) -> dict[str, str]`:

- Drop empty ids and keep the first occurrence of each; `list(dict.fromkeys(...))` does both.
- Run `_map_concurrent(ids, lambda cid: self.conversation_state(cid).last_read)`.
- Return `{id: marker}`. An empty input makes no request and returns `{}`.

`SlackClient.thread_last_read(self, channel_id: str, thread_ts: str) -> str`:

- Make one `conversations_replies(channel=channel_id, ts=thread_ts, limit=1)` call.
- Return `str(m.get("last_read") or "")` from the first message in `messages` whose `ts` equals
  `thread_ts`. Return `""` if there is no such message, on `SlackApiError` (logged at debug, as
  `resolve_channel_name` does), or for an empty argument.
- It never raises and is never cached.

`SlackClient.thread_last_read_markers(self, threads: list[tuple[str, str]]) -> dict[tuple[str, str], str]`
follows the same pattern as `last_read_markers`: dedupe, then `_map_concurrent` over
`thread_last_read`.

### D3. `SlackMessage` fields and parsing (client)

Two fields go at the end of `SlackMessage`, after `timestamp`:

- `last_read: str = ""`: Slack's thread read marker. Slack sets it only on a thread's parent message
  returned by `conversations.replies`, when the user follows the thread.
- `read_state: str = ""`: one of the D1 values. `""` means it was not computed.

Parsing:

- `_parse_message` sets `last_read=str(raw.get("last_read") or "")`.
- `_parse_search_match`: after parsing, if `message.thread_ts` is empty, set it to the permalink's
  `thread_ts` query parameter: `(parse_qs(urlparse(raw.get("permalink") or "").query).get("thread_ts") or [""])[0]`.
  If the result equals the message's own `ts`, store `""` instead, because a thread root is a
  top-level message.

### D4. Read state on the review-gated reads (connector)

`_message_to_dict` adds `"read_state": m.read_state` only when `m.read_state` is non-empty. A
message whose read state was not computed keeps today's exact eight keys.

`_get_channel_history`:

- Replace `is_member = await self._fetch(self._slack.resolve_is_member, channel_id)` with
  `state = await self._fetch(self._slack.conversation_state, channel_id)` and
  `is_member = state.is_member`. Keep the comment above it.
- Then `own = await self._fetch(self._slack.own_user_id)`.
- Before `filtered = ...`, for each message:
  `m.read_state = message_read_state(m.id, state.last_read, own=bool(own) and m.user_id == own)`.

  The channel marker covers every message in `conversations.history`, including thread broadcasts.
  In search, a broadcast has `thread_ts != ts` and so reads `"unknown"` under the reply rule. That is
  intended: search cannot tell a broadcast from a plain reply, and `"unknown"` is the safe answer.

`_get_thread_replies`:

- Use `state` the same way, and reuse the `own_user_id` value that `args` already fetches. Fetch
  it once, into a local variable, and use it in both places.
- Set `thread_marker = next((m.last_read for m in messages if m.id == thread_ts), "")`.
- For each message, the marker is `state.last_read` when `m.id == thread_ts` (the parent) and
  `thread_marker` otherwise. Then set
  `m.read_state = message_read_state(m.id, marker, own=bool(own) and m.user_id == own)`.

A reply page without its parent, or a thread the user does not follow, reads `"unknown"`.

`_search_messages` takes a new keyword `read_state: bool = False`, passed through `call()` like
`member_only`. When it is true:

- `markers = await self._fetch(self._slack.last_read_markers, [m.channel_id for m in messages])`,
  and `own = await self._fetch(self._slack.own_user_id)`.
- A message is a reply when `m.thread_ts and m.thread_ts != m.id`. A reply's marker is `""`;
  otherwise it is `markers.get(m.channel_id, "")`. Then set
  `m.read_state = message_read_state(m.id, marker, own=bool(own) and m.user_id == own)`.

When `read_state` is false, nothing is computed and no `read_state` key appears in the result.

In all three tools:

- `args` passed to `gated_call` gains `"read_state": read_state` for search only. It is a request
  argument, recorded like `member_only`.
- The approval card does not change: no preview, details or table change, and no new
  `visibility` row. Read state is the user's own Slack metadata, not message content.
- The rule arguments (`is_member`, `all_results_in_my_conversations` and the others) stay exactly as
  they are today.

### D5. Tool definition text (connector)

`slack_get_channel_history`: in its description, replace
`Each message is {ts, channel_id, channel_name, user_id, user_name, text, thread_ts, reply_count}, newest first,`
with
`Each message is {ts, channel_id, channel_name, user_id, user_name, text, thread_ts, reply_count, read_state}, newest first (read_state is 'unread', 'read' or 'unknown', from your own Slack read marker),`.
Change nothing else.

`slack_get_thread_replies`: replace
`Each message is {ts, channel_id, channel_name, user_id, user_name, text, thread_ts, reply_count}, the parent message included, in Slack's order;`
with
`Each message is {ts, channel_id, channel_name, user_id, user_name, text, thread_ts, reply_count, read_state}, the parent message included, in Slack's order; read_state is 'unread', 'read' or 'unknown' (a reply in a thread you do not follow);`.

`slack_search_messages`: the whole description becomes exactly this (1007 characters):

```
Search Slack messages by query, participant, time window, or a combination. For messages from or with someone prefer participant over query -- 'Bob wrote me' is participant='Bob'; 'Bob in a chat with Jane' is participant='Bob,Jane' -- it reads those DM/group-chat conversations directly, more reliably than search; query then filters their text. Defaults to 90 days; widen or disable via days. For the last N minutes in every channel, DM, group DM and thread, pass minutes (query may be empty): newest first; search-index delay can miss the last minute or two. Returns a list of {ts, channel_id, channel_name, user_id, user_name, text, thread_ts, reply_count}, up to count (default 20, max 100); a participant search reads at most 10 conversations. Text and names may be redacted. Use read_state=true with care: one Slack request per conversation, easily rate-limited; prefer slack_get_read_state for the few messages that matter. Use slack_get_channel_history for one known channel. Requires user approval.
```

The new `ToolParam` on `slack_search_messages` goes before `reason`:

```python
ToolParam(
    "read_state", "bool", required=False, default=False,
    description=(
        "Also return each message's read_state: 'unread' or 'read' against your own Slack read "
        "marker for its conversation, or 'unknown' for a thread reply or a conversation with no "
        "marker. Use with care: it costs one extra conversations.info request per conversation in "
        "the results (50/min) before the approval card appears, so a large sweep is easily "
        "rate-limited. For a handful of messages use slack_get_read_state instead. Default false."
    ),
),
```

### D6. New tool `slack_get_read_state` (connector)

This is an auto-approved read (`read_only=True`, no gate, `_auto_audit`), like the listing tools.
It returns only `"unread"`/`"read"`/`"unknown"` for ids the agent already holds, and no message text
(ADR 0235).

```python
ToolSpec(
    name="slack_get_read_state",
    description=(
        "Get your own Slack read state for a few specific messages you already have, without "
        "reading them again. Use it sparingly, only for messages whose read state matters (for "
        "example the ones you are about to report as new), not for every search result: each "
        "distinct conversation costs one Slack request and each distinct thread one rate-limited "
        "conversations.replies request (about a minute each on an app distributed outside the "
        "Slack Marketplace). Returns a list of {channel_id, ts, thread_ts, read_state}, "
        "one per distinct requested message in request order, where read_state is 'unread', 'read' or "
        "'unknown' (a thread you do not follow, or no marker). Prefer it over "
        "slack_search_messages' read_state=true for more than a few conversations. Returns no "
        "message text. Auto-approved."
    ),
    params=[
        ToolParam(
            "messages", "str",
            description=(
                "Comma-separated messages to check, 1 to 20, each channel_id:ts for a top-level "
                "message or channel_id:ts:thread_ts for a reply in a thread, from a message's "
                "channel_id, ts and thread_ts fields in slack_search_messages, "
                "slack_get_channel_history or slack_get_thread_replies. At most 3 distinct threads. "
                "A repeated entry is checked once."
            ),
        ),
        ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
    ],
    read_only=True,
),
```

Place it right after `slack_search_messages` in `tool_specs()`, and dispatch it in `call()` as
`if tool == "slack_get_read_state": return await self._get_read_state(**args)`, the same way the
other tools are dispatched.

The module constants, beside `_CURSOR_DESCRIPTION`:

- `_READ_STATE_MAX_MESSAGES = 20`
- `_READ_STATE_MAX_THREADS = 3`

`async def _get_read_state(self, messages: str) -> Any`, in the `# Auto` banner group, right after
`_resolve_permalink`:

1. Parse before any Slack call: split on `,`, strip each item and drop empty items.
2. Split each item on `:`.
   - Two parts give `(channel_id, ts, "")`.
   - Three parts give `(channel_id, ts, thread_ts)`. When `thread_ts == ts`, store `""` as the
     thread, because that is the thread root.
   - Any other shape, or an empty channel id or ts, raises
     `ValueError("slack_get_read_state takes channel_id:ts or channel_id:ts:thread_ts entries; got <item>")`.
3. Deduplicate, keeping order. Zero entries, or more than `_READ_STATE_MAX_MESSAGES`, raises
   `ValueError("slack_get_read_state takes 1 to 20 messages")`.
4. More than `_READ_STATE_MAX_THREADS` distinct `(channel_id, thread_ts)` pairs with a non-empty
   thread raises `ValueError("slack_get_read_state takes at most 3 distinct threads per call")`.
5. `t0 = time.time()`.
6. Get `markers` from `last_read_markers` over the channel ids of the entries with no thread, and
   `thread_markers` from `thread_last_read_markers` over the distinct threads. Both go through
   `self._fetch`.
7. For each entry:
   - Use `thread_markers.get((channel_id, thread_ts), "")` for a reply, else
     `markers.get(channel_id, "")`.
   - Call `message_read_state(ts, marker)` with no `own`: the tool does not know the author.
   - Append `{"channel_id": ..., "ts": ..., "thread_ts": ..., "read_state": ...}`.
8. Call `self._auto_audit("slack_get_read_state", "Get Slack Read State", f"Read state of {n} message(s), {u} unread", ",".join(dict.fromkeys(e[0] for e in entries)), t0)`,
   where `n` is the number of distinct entries, `u` the count of `"unread"` results, and the
   sender is the distinct channel ids. Return the list.

`auto_accept.TOOL_TO_GATE` gains `"slack_get_read_state": "auto",` next to `slack_resolve_permalink`.
`SLACK_SIBLINGS` in the connector tests gains `"slack_get_read_state": ("slack_search_messages",)`.
The `slack_search_messages` entry becomes `("slack_get_channel_history", "slack_get_read_state")`.

### D7. Live QA check

`scripts/qa_fixture_recorder.py` gains:

```python
def _check_slack_read_marker(client: SlackClient, method: str, target: str, conversation_id: str) -> CheckResult:
```

It calls `client.conversation_state(conversation_id)`.

- `ok` is `bool(state.last_read)`.
- The note is `"conversations.info reports last_read"` when `ok`, else
  `"conversations.info reported no last_read"`.
- A `SlackClientError` or `TypeError` gives a not-ok result whose note is the exception text, as in
  `_check_slack_membership`.

`check_slack` appends it right after each membership check: `"conversation_read_marker"` for the
channel, and `"group_dm_read_marker"` for `group_dm_id`. After those, it also checks one 1:1 DM, the
type the routine's fallback reads: it calls `client.list_dms(max_results=1)` and, if that returns a
DM, appends `_check_slack_read_marker(client, "dm_read_marker", dm.id, dm.id)`. A `SlackClientError`
from `list_dms` appends a not-ok `dm_read_marker` result whose note is the exception text, and an
empty list appends nothing. None of these record a fixture. This check is what
proves, on real Slack, the assumption D2 rests on: that `conversations.info` reports `last_read` to
a user token for channels and group DMs.

### Rejected alternatives (they become the ADRs' "Alternatives considered")

- **Read state always on in search.** Every search would pay one `conversations.info` per
  conversation before its approval card appears. The user chose opt-in.
- **Read state on `slack_list_dms` / `slack_list_group_chats`.** That is one `conversations.info`
  per conversation: about 12 minutes for 600 DMs at 50/min. The fallback's history reads already
  carry it. The user chose no change.
- **`unread_count` / `unread_count_display` from `conversations.info`.** Slack documents these only
  for some conversation types, and a count does not say which messages are unread. A per-message
  comparison against `last_read` does.
- **Judging a thread reply by the channel marker.** Slack tracks thread replies separately from the
  channel, so this would call a reply read or unread wrongly. Search marks replies `"unknown"`, and
  `slack_get_read_state` or `slack_get_thread_replies` gives the thread's own marker.
- **Caching markers between calls.** A marker moves whenever the user reads, and the routine's
  halving re-searches would see stale state. `conversation_state` stays live, like membership
  (ADR 0226).
- **Making `slack_get_read_state` review-gated.** It returns no content. It is not quite as harmless
  as the listing tools, though: it accepts any `channel_id:ts` the agent makes up, so with 20 probes
  a call the agent can narrow down the user's read marker, and with it when the user last read a
  conversation. That is behavioural metadata about the user's own reading, inside conversations the
  user can see; the audit log records every call. A card per check would defeat the targeted use the
  tool exists for, so it stays auto-approved, and ADR 0235 says this plainly in its Consequences.
- **More than 3 thread lookups per call.** `conversations.replies` is 1/min on an app distributed
  outside the Marketplace, and the SDK's rate-limit retry (`_RATE_LIMIT_MAX_RETRY_COUNT = 3`) holds
  the call while it waits. Three threads keep the worst case near three minutes. ADR 0235 records
  this, and the tool description warns about it.
- **Showing read state on the approval card.** It is not message content, and the card's columns
  are covered by preview-minimisation tests. No reviewer asked for it.

## ADRs

- **0234**: Slack message reads report the user's own read state from Slack's read markers. History
  and thread reads always do, from the `conversations.info` and `conversations.replies` responses
  they already make. Search does only on request (`read_state`), and judges thread replies as
  unknown. Listings are unchanged. Relates to ADR 0226 and ADR 0227.
- **0235**: A separate, auto-approved `slack_get_read_state` tool checks the read state of at most
  20 named messages (at most 3 threads), so the agent asks only about the messages that matter.
  Relates to ADR 0234.

Before writing them, re-check `docs/adr/` for the next free numbers. If a parallel branch has taken
0234 or 0235, take the next free ones and update every reference.

## Manual steps

None. The live check (D7) runs on the self-hosted runner through `qa-record-fixture.yml`, dispatched
by the last phase (`ci.dispatchable`).

## Risks and open questions

- **`conversations.info` does not return `last_read` for some conversation type on a user token.**
  Unit tests cannot see this. D7's live check does. If the dispatched run reports
  `conversation_read_marker`, `group_dm_read_marker` or `dm_read_marker` not ok, the phase stops with
  `status=blocked` and reports the note. Affected messages would read `"unknown"`, which is safe but
  less useful.
- **Real search matches already carry `thread_ts`.** Then D3's permalink fallback never runs. That
  is harmless and needs no change.
- **A test pins the exact `args` dict or `call()` kwargs of `slack_search_messages`.** Adding
  `read_state` changes it. Update that test to include `"read_state": False`. If a policy test
  (`tests/unit/policy/`) breaks because of the new arg, stop with `status=blocked`: D4 assumes no
  rule reads it.
- **A description lands over 1024 characters.** D5 gives exact text that was measured.
  `tests/helpers.py`'s `assert_tool_definitions_complete` is the check. If D5's text fails a
  wording rule there, stop with `status=blocked` rather than rewording freely.
- **The tool-list tables.** `TOOL_TO_GATE` and the generated `docs/tools-reference.md` are the only
  tables an auto Slack tool needs; `policy/registry.py`'s `TOOL_REGISTRY` is built from
  `TOOL_TO_GATE`. If a test asks for an operation key or a verb for `slack_get_read_state`, stop
  with `status=blocked`: an auto tool should need neither.

## Implementation manifest

```yaml
plan_slug: slack-read-state
feature_branch: feature/slack-read-state
max_parallel: 1
verify_after_merge:
  - python3 -m pytest tests/unit/test_slack_client.py tests/unit/connectors/test_slack_connector.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_qa_fixture_recorder.py -q
final_checks:
  - docs/slack-read-state-plan.md is deleted and `grep -rn "slack-read-state-plan" --include=*.md --include=*.py .` finds nothing outside .git
  - docs/adr/0234-*.md and docs/adr/0235-*.md exist, are Accepted, and are listed in docs/adr/README.md's index
  - CHANGELOG.md has the two entries under `## [Unreleased]` and no new version heading
  - a qa-record-fixture.yml run (connector=slack) for this branch's code is green, with conversation_read_marker ok, and its URL is in the PR description
phases:
  - id: p1-client-read-markers
    title: SlackClient learns read markers and read-state comparison
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/slack_client.py
      - tests/unit/test_slack_client.py
      - scripts/slack_call_budget.py
    brief: |
      Read docs/slack-read-state-plan.md sections Design D1, D2 and D3 first; they hold every name
      and rule. Must-read docs: CLAUDE.md, CONTRIBUTING.md, docs/coding-and-testing-guidelines.md
      (§1.2 no history in comments, §1.4 errors), docs/testing-policy.md.
      1. In src/privacyfence/slack_client.py, after SlackClientError, add READ_STATE_UNREAD,
         READ_STATE_READ, READ_STATE_UNKNOWN and message_read_state exactly as D1 says (import
         `decimal` at the top with the other stdlib imports).
      2. Add the SlackConversationState dataclass (D2) next to the other dataclasses (after
         SlackGroupChat).
      3. Add `last_read: str = ""` and `read_state: str = ""` at the end of SlackMessage (D3), and
         set `last_read` in `_parse_message`.
      4. Rename resolve_is_member's body into `conversation_state` (D2) and make resolve_is_member
         the one-line wrapper. Every existing TestResolveIsMember test must still pass unchanged.
      5. Add last_read_markers, thread_last_read and thread_last_read_markers (D2) right after
         conversation_state.
      6. In _parse_search_match, add the permalink thread_ts fallback (D3).
      7. Tests in tests/unit/test_slack_client.py:
         - a new `class TestMessageReadState` covering own -> read, empty ts or marker -> unknown,
           ts after/equal/before the marker, "0000000000.000000" -> unread, a non-numeric marker
           -> unknown, a non-str marker (None) -> unknown, and a same-second pair 1e-6 apart
           ("1700000000.000100" vs "1700000000.000099") ordered correctly;
         - `class TestConversationState`: last_read is returned, a missing last_read gives "", an
           error gives SlackConversationState(), and the caches are filled exactly as
           TestResolveIsMember already asserts;
         - `class TestLastReadMarkers`: dedupe (two ids, one repeated -> two conversations_info
           calls) and an empty list makes no call;
         - `class TestThreadLastRead`: it reads the parent's last_read from a
           conversations_replies response (reuse the live fixture
           tests/fixtures/live/slack/get_thread_replies.json as TestLiveFixtureParsing does, and
           compare with raw["messages"][0]["last_read"], not a hard-coded value), it
           passes limit=1, there is no parent -> "", SlackApiError -> "";
         - in TestParseMessage, last_read is parsed from the raw message;
         - in TestSearchMessages, a match with no thread_ts and a permalink carrying
           ?thread_ts=X gets thread_ts X, and a permalink whose thread_ts equals the match's own
           ts gets "".
      8. In scripts/slack_call_budget.py's `cases`, add
         ("conversation_state()", lambda c: c.conversation_state("C0001")) after the
         resolve_is_member row, and
         ("slack_get_read_state(20 messages, 3 threads)", lambda c: (c.last_read_markers([f"C{i:04d}" for i in range(15)]), c.thread_last_read_markers([("C0001", f"170000000{i}.0001") for i in range(3)])))
         at the end. Run `python3 scripts/slack_call_budget.py` and
         `python3 scripts/slack_call_budget.py --distributed` and confirm both exit 0 (its fake
         web client already handles conversations_replies).
      9. Run `ruff check .` and `python3 -m mypy src/privacyfence`.
      Stop with status=blocked if TestResolveIsMember needs changes to pass after step 4: the
      wrapper must be behaviour-identical.
    acceptance:
      - python3 -m pytest tests/unit/test_slack_client.py -q passes
      - python3 -m pytest tests/unit/test_slack_client.py -q -k "TestMessageReadState or TestConversationState or TestLastReadMarkers or TestThreadLastRead" selects at least 15 tests, all passing
      - grep -n "def conversation_state\|def last_read_markers\|def thread_last_read\b\|def thread_last_read_markers\|def message_read_state" src/privacyfence/slack_client.py prints 5 lines
      - python3 scripts/slack_call_budget.py exits 0, and python3 scripts/slack_call_budget.py --distributed exits 0
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p2-connector-read-state
    title: History, thread and opt-in search reads return read_state
    depends_on: [p1-client-read-markers]
    complexity: M
    touches:
      - src/privacyfence/connectors/slack.py
      - tests/unit/connectors/test_slack_connector.py
      - docs/tools-reference.md
      - CHANGELOG.md
    brief: |
      Read docs/slack-read-state-plan.md sections Design D1, D4 and D5 first. D5 holds exact
      description text: copy it verbatim, splitting it into adjacent string literals the way the
      surrounding code does.
      1. In src/privacyfence/connectors/slack.py import message_read_state from ..slack_client
         alongside the existing imports. Update _message_to_dict (D4).
      2. _get_channel_history and _get_thread_replies: switch to conversation_state and compute
         read_state per D4. In _get_thread_replies, fetch own_user_id once into a local variable
         and use it for both read_state and args["own_user_id"].
      3. _search_messages: add the read_state keyword, compute it per D4 only when it is true, and
         add "read_state": read_state to args.
      4. tool_specs(): apply the three D5 description changes and add the read_state ToolParam to
         slack_search_messages before reason.
      5. tests/unit/connectors/test_slack_connector.py:
         - In make_connector, replace `client.resolve_is_member.return_value = False` and its
           comment with `client.conversation_state.return_value = SlackConversationState()`
           (comment: "Default to 'not a member, no read marker' so args and read_state carry
           plain values."), and add `client.last_read_markers.return_value = {}` (import
           SlackConversationState from privacyfence.slack_client).
         - test_preview_and_gate (~line 314) asserts raw_data equals
           [make_message(text="a" * 100)]; the read_state is now set in place, so make it
           [make_message(text="a" * 100, read_state="unknown")]. Fix any other raw_data
           equality the same way.
         - Change the tests at lines ~921, ~926, ~934, ~1018, ~1027 and ~1036 that set or assert
           client.resolve_is_member to use client.conversation_state with
           SlackConversationState(is_member=...). The ordering test at ~934 keeps its order
           assertion, with a side_effect returning SlackConversationState(is_member=True).
         - Add `class TestReadState` with:
           - history: messages before/after a marker -> read/unread in filtered_data["messages"];
             own message after the marker -> read;
           - history with SlackConversationState() (no marker) -> every message unknown;
           - thread: the parent judged by the channel marker and replies by the parent's
             last_read; a parent with no last_read -> replies unknown;
           - search without read_state: no read_state key in any result, and
             client.last_read_markers not called;
           - search with read_state=true: a top-level message judged by its channel's marker, a
             reply (thread_ts != ts) unknown, last_read_markers called once with the result
             channel ids, and gated_call args["read_state"] is True;
           - the preview is unchanged: set(preview) == {"Channel"} for history and thread without
             minutes, and for search with query="x", read_state=true,
             set(preview) == {"Query", "Since"}.
         - Update any test that pins slack_search_messages' args dict to include
           "read_state": False.
      6. Run `python3 scripts/generate_tools_reference.py` (the search first sentence changed) and
         commit docs/tools-reference.md.
      7. CHANGELOG.md, under `## [Unreleased]`, add a `### Added` heading above the existing `### Fixed` (create it if it is missing), and this line:
         `- **Slack reads say what you have not read yet.** Channel history and thread reads mark each message unread, read or unknown against your own Slack read marker; a search does too when the agent asks for it with read_state, which costs one Slack request per conversation in the results. Search results now carry the thread_ts of a reply in a thread.`
      8. Run ruff check . and python3 -m mypy src/privacyfence.
      Stop with status=blocked if a tests/unit/policy/ test fails because of the new search arg (the plan's Risks).
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_slack_connector.py -q passes, including the new TestReadState class
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/policy -q passes
      - python3 -c "import sys; sys.path.insert(0,'src'); from unittest.mock import MagicMock; from privacyfence.connectors.slack import SlackConnector; d={s.name: s.description for s in SlackConnector(MagicMock()).tool_specs()}; print(len(d['slack_search_messages']) <= 1024, 'easily rate-limited' in d['slack_search_messages'])" prints "True True"
      - grep -n "Slack reads say what you have not read yet" CHANGELOG.md matches one line, above the first `## [6.` heading
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p3-get-read-state-tool
    title: New auto-approved slack_get_read_state tool
    depends_on: [p2-connector-read-state]
    complexity: M
    touches:
      - src/privacyfence/connectors/slack.py
      - tests/unit/connectors/test_slack_connector.py
      - src/privacyfence/auto_accept.py
      - docs/tools-reference.md
      - CHANGELOG.md
    brief: |
      Read docs/slack-read-state-plan.md section Design D6 first; it holds the exact ToolSpec,
      error strings, caps and the audit call. Pattern to copy for an auto Slack tool:
      slack_resolve_permalink, added in commit fda669c7 (`git show fda669c7`).
      1. In src/privacyfence/connectors/slack.py add _READ_STATE_MAX_MESSAGES and
         _READ_STATE_MAX_THREADS, the ToolSpec (after slack_search_messages), the dispatch line in
         call(), and _get_read_state next to _resolve_permalink, all as D6 says.
      2. src/privacyfence/auto_accept.py: add "slack_get_read_state": "auto" to TOOL_TO_GATE next
         to slack_resolve_permalink, aligned like its neighbours.
      3. policy/registry.py needs no change: TOOL_REGISTRY is built from TOOL_TO_GATE
         (tests/unit/policy/test_registry.py). If a test asks for an operation key or a verb for
         the new tool, stop with status=blocked.
      4. tests/unit/connectors/test_slack_connector.py:
         - update SLACK_SIBLINGS per D6;
         - add `class TestGetReadState`: it never calls gated_call (the gated_call_spy stays
           empty) and writes one audit entry (copy TestResolvePermalink's audit assertion
           pattern); mixed top-level and thread entries map to the right marker and keep request
           order; a duplicate entry is checked once; channel_id:ts:ts is treated as top-level;
           the ValueError texts for a malformed item, 0 entries, 21 entries and 4 distinct
           threads, each raised with client.last_read_markers and
           client.thread_last_read_markers not called;
         - give make_connector `client.thread_last_read_markers.return_value = {}`;
         - in TestEveryToolIsAudited's arg_overrides dict, add
           "slack_get_read_state": {"messages": "C1:1700000000.000100"} (build_stub_args passes
           "stub", which does not parse).
      5. Run `python3 scripts/generate_tools_reference.py` and commit docs/tools-reference.md.
      6. CHANGELOG.md under `## [Unreleased]` → `### Added`, add:
         `- **slack_get_read_state.** A new Slack tool returns whether up to 20 specific messages are unread, read or unknown, without reading them again, so an agent checks only the messages that matter instead of every search result. Auto-approved; it returns no message text.`
      7. Run ruff check . and python3 -m mypy src/privacyfence.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_slack_connector.py -q passes, including TestGetReadState and TestToolDefinitions
      - python3 -m pytest tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_systemic_gate_invariants.py tests/unit/policy -q passes
      - grep -n '"slack_get_read_state"' src/privacyfence/auto_accept.py matches a line containing "auto"
      - grep -n "slack_get_read_state" docs/tools-reference.md matches one table row with gate `auto`
      - ruff check . and python3 -m mypy src/privacyfence pass
  - id: p4-qa-docs-adrs-retire
    title: Live read-marker check, Slack setup docs, ADRs 0234-0235, retire the plan
    depends_on: [p3-get-read-state-tool]
    complexity: M
    touches:
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/live/slack/**
      - docs/slack-setup.md
      - docs/connector-qa.md
      - docs/adr/0234-*.md
      - docs/adr/0235-*.md
      - docs/adr/README.md
      - docs/slack-read-state-plan.md
    brief: |
      Read docs/slack-read-state-plan.md completely; this phase writes its decisions into ADRs and
      then deletes it.
      1. scripts/qa_fixture_recorder.py: add _check_slack_read_marker and call it from check_slack
         as Design D7 says.
      2. tests/unit/test_qa_fixture_recorder.py: in _membership_client add a `last_read` keyword
         (default "1700000000.000100") and monkeypatch client.conversation_state to return
         SlackConversationState(is_member=member, last_read=last_read). Add tests: ok when a
         marker is reported; not ok with note "conversations.info reported no last_read" when
         last_read=""; group_dm_read_marker runs only when group_dm_id is set; dm_read_marker
         uses the first id list_dms(max_results=1) returns (monkeypatch client.list_dms), is
         absent when list_dms returns [], and is not ok with the error text when list_dms raises
         SlackClientError. Update any test that counts check_slack's results.
      3. docs/slack-setup.md:
         - In the rate-limit table, change the conversations.info "Used for" cell to "channel type,
           membership and your read marker", and the conversations.replies row's cell to
           "channel history, thread replies and a thread's read marker".
         - In the routine steps, add to step 1: "Add `read_state` true only when the run needs
           read state for most results: it costs one `conversations.info` request per
           conversation in the results. Otherwise call `slack_get_read_state` afterwards for the
           few messages that matter (at most 20, at most 3 threads)." Add to step 2: "Its
           replies come back with `read_state` from the thread's own marker."
         - Add a short `## Read state` section after the routine steps, saying: what
           unread/read/unknown mean (against your own Slack read marker; your own messages are
           read); that a thread reply in search results is unknown, and why (Slack tracks threads
           separately); and which tools return it. Link ADR 0234 and ADR 0235.
         Describe current behaviour only, with no history wording (tests/unit/test_docs_no_history.py).
      3a. docs/connector-qa.md:
         - In the recorder table's `slack` row (around line 381), replace
           "checks `conversation_membership` and `group_dm_membership` (no fixture)" with
           "checks `conversation_membership`, `group_dm_membership`, `conversation_read_marker`,
           `group_dm_read_marker` and `dm_read_marker` (no fixture)".
         - In the `slack.group_dm_id` checklist item (around line 123), replace "It enables the
           group-DM membership check; without it that check is skipped." with "It enables the
           group-DM membership and read-marker checks; without it those checks are skipped."
           Then add one sentence after it: "The 1:1 DM read-marker check uses the QA account's
           first DM and is skipped when it has none."
      4. Write docs/adr/0234-slack-reads-report-the-users-own-read-state.md and
         docs/adr/0235-a-separate-auto-approved-tool-checks-the-read-state-of-named-messages.md
         from the template in docs/adr/README.md. Use Status "Accepted — <today>. Implemented."
         Take Context and Decision from Design D1-D6 and Alternatives considered from "Rejected
         alternatives". Link source files and ADRs 0226 and 0227, never this plan. Re-check the
         numbers are still free first (the plan's ADRs section). Add both rows to docs/adr/README.md's
         index in number order, with Status "Accepted; relates to 0226, 0227" for 0234 and
         "Accepted; relates to 0234" for 0235.
      5. Delete docs/slack-read-state-plan.md. Confirm
         `grep -rn "slack-read-state-plan" --include=*.md --include=*.py .` finds nothing.
      6. Run ruff check ., python3 -m pytest tests/unit -q, and commit and push the phase branch.
      7. Dispatch the live check with the GitHub MCP actions_run_trigger: workflow
         qa-record-fixture.yml, ref main, inputs connector=slack and branch=<this phase's pushed
         branch>. Wait for it (a queued run behind the connector-live-check concurrency group is
         normal), then pull: it may commit a refreshed tests/fixtures/live/slack/get_thread_replies.json.
         Review that diff for account identifiers, tenant URLs or private content before keeping
         it (docs/connector-qa.md, "Reviewing recorded fixtures"). Put the run URL and its
         check_slack lines in your final report.
      Stop with status=blocked and report the note if conversation_read_marker,
      group_dm_read_marker or dm_read_marker is not ok in that run.
    acceptance:
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py tests/unit/test_docs_no_history.py tests/unit/test_code_no_history.py -q passes
      - test ! -e docs/slack-read-state-plan.md
      - ls docs/adr/0234-*.md docs/adr/0235-*.md lists two files, and grep -c "0234\|0235" docs/adr/README.md is at least 2
      - grep -n "slack_get_read_state" docs/slack-setup.md matches
      - the dispatched qa-record-fixture.yml run for connector=slack concluded success with conversation_read_marker ok, and its URL is in the phase report
      - python3 -m pytest tests/unit -q and ruff check . pass
```
