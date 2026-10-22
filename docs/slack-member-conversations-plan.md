# Plan: Slack rules for conversations I'm a member of

## Goal

A scheduled triage routine has to read Slack *by time*: every message since its last run, in every
conversation the signed-in user is in, with no fixed channel list and no popup. This is request 5 of
the inbox-triage feature requests of 22 October 2026, which were written after the inbox-triage-scopes
plan. Requests 1–4 shipped in 6.8.0 (ADRs 0218–0223).

After this plan:

- A new scope, **"Slack — conversations I'm a member of"**, auto-accepts
  `slack_get_channel_history` and `slack_get_thread_replies` on any 1:1 DM, group DM or public or
  private channel the user is a member of *at the time of the call* (checked live with Slack), and,
  with the `search` verb, a `slack_search_messages` call made with the new `member_only=true`
  argument. It takes an optional list of channel ids it never covers, for HR or legal channels.
- `slack_search_messages` gains `member_only` and `exclude_channels`. With them, the routine's
  "everything since my last run" sweep is one membership listing plus 1–3 search requests, instead
  of one `conversations.history` call per conversation. That keeps the routine inside Slack's rate
  limits (see "Slack API limits" below).
- A rule saved with only one of two verbs that share an operation key now allows only that verb.
  Today a "Slack — mentions of me" rule saved with only `read` also auto-accepts searches.

Out of scope, with the reasons in Design §D9: a channel-kind filter on the new rule, and a
last-activity timestamp on the Slack listings.

## Current state

Measured on `main` at `0562af43`.

### Policy model

- The engine matches a rule on its **operation key**, not on its verbs:
  `src/privacyfence/policy/engine.py:73-95` (`find_matching_rule`) checks
  `operation_key in rule.operations` and then the scope selector. `slack_get_channel_history`,
  `slack_get_thread_replies` (verb `read`) and `slack_search_messages` (verb `search`) share the
  one key `slack.read_messages` (`src/privacyfence/auto_accept.py:120-122`,
  `src/privacyfence/policy/registry.py:159-161`).
- For proposable scopes, read and search are told apart by **predicate**: `approved_channel` (read)
  and `approved_channel_all_results` (search) (`src/privacyfence/policy/propose.py:343-349`), and
  `describe.rule_verbs` (`src/privacyfence/policy/describe.py:92-108`) credits each predicate only
  with its own verbs.
- The extra scopes (`src/privacyfence/policy/catalogue.py:52-93`, `EXTRA_SCOPES`) have **one
  predicate each**. `_compile_catalogue_rules` (`catalogue.py:225-246`) and `scope_rows.compile_row`
  (`src/privacyfence/policy/scope_rows.py:119-145`) compile a row of any verbs to one `PolicyRule`
  with that predicate. `slack.mentions_of_me` offers `read` and `search`
  (`catalogue.py:77-81`); its selector (`src/privacyfence/policy/scopes.py:624-636`) branches on
  `ctx.tool`, so **a row saved with only `read` still auto-accepts a mentions search**, although
  Settings shows "allow read". That is the bug fixed in phase p1.
- Rows (`auto_accept:` version 3, ADR 0172) store `scope`, `value`, `verbs`, `operations`
  (`scope_rows.py:34-50`). `add_rules` maps a compiled rule back to its scope through
  `scope_of_rule` (`scope_rows.py:259-283`), which matches `extra.predicate == rule.predicate`.
  `describe._catalogue_verbs` (`describe.py:69-89`) and `outdated.missing_operations`
  (`src/privacyfence/policy/outdated.py:30-51`) do the same.
- A scoped token stores, per row id, the `(predicate, operation)` pairs the row compiled to when the
  token was made (`src/privacyfence/scoped_tokens.py:438-441`). `gate._rules_in_scope`
  (`src/privacyfence/gate.py:725-744`) drops any compiled pair not in that limit. Reading the limits
  back from disk is `scoped_tokens._limits_from_json` (`scoped_tokens.py:191-210`).
- Value-less Slack scopes read flags that the connector computes: `is_self_dm`, `is_group_dm`,
  `is_one_to_one_dm`, `own_user_id` (`src/privacyfence/connectors/slack.py:568-700`).
  `web/mcp_dispatch.py:45` (`_CONNECTOR_COMPUTED_ARGS`) strips them from an agent's
  `privacyfence_check_policy` arguments.
- Existing Slack read scopes: `approved_channel`/`approved_channel_all_results` (listed channels),
  `dm_with_myself`, `group_dm` (**every** group DM: `scopes.py:102-103` matches any
  `is_group_dm`. The popup button is labelled "this group DM", but the rule is not per chat),
  `slack.direct_messages_to_me` (every 1:1 DM), `slack.mentions_of_me`. **No scope covers a
  channel by membership.** A user token with `channels:history` can read a public channel the user
  is *not* in, which is why membership has to be checked, not assumed.
- Settings shows the value field only when `group.needs_value`
  (`src/privacyfence/settings_window_html.py:1057`). A blank field submits `''`, which
  `settings_controller._parse_value_list` (`src/privacyfence/settings_controller.py:356-369`) turns
  into `None`.

### Slack client

- `SlackClient.resolve_is_one_to_one_dm`, `resolve_is_group_dm` and `resolve_is_self_dm`
  (`src/privacyfence/slack_client.py:1539-1625`) each call `conversations.info` on a cache miss,
  each into its own cache. `_conversation_ids_for_user(user_id, types=...)`
  (`slack_client.py:1764-1794`) walks `users.conversations` (200 per page) and returns `None` on any
  failure.
- `search_messages` (`slack_client.py:804-855`) makes **one** `search.messages` request per call,
  whichever path it takes (days, or minutes through `_search_recent` at `slack_client.py:878-907`).
  The participant path reads conversation history instead.
- 429 handling: slack_sdk's `RateLimitErrorRetryHandler` with `_RATE_LIMIT_MAX_RETRY_COUNT = 3`
  (`slack_client.py:77-84`). It waits as long as `Retry-After` says.
- `scripts/slack_call_budget.py` counts Slack calls per client entry point against a fake WebClient
  and prices them by tier. Today, with warm caches on a 2000-user / 900-channel / 600-DM / 40-group-DM
  workspace: `search_messages(minutes=60)` = 1 call (`search.messages`);
  `get_channel_history(limit=50)` = 1 call; a participant search = 16 calls.

### Slack API limits that shape this plan

| Method | Tier (requests/min) | Used for |
|---|---|---|
| `conversations.history`, `conversations.replies` | Tier 3 (50) for an internal or Marketplace app; **1/min and 15 messages** for an app distributed outside the Marketplace (`docs/slack-setup.md`) | channel history, thread replies |
| `conversations.info` | Tier 3 (50) | channel type and, from p2, membership |
| `users.conversations` | the budget script uses 100; Slack documents it as Tier 3, see Risks | the user's conversation ids |
| `search.messages` | Tier 2 (20), at most 100 matches per page | search |
| `conversations.list` | Tier 2 (20) | the listings |

Reading N conversations one by one costs N `conversations.history` calls plus, with this plan, N
`conversations.info` membership checks. That is about 2 minutes for N=50 on an internal app, and
50 minutes on a distributed one. Search has one more limit: it **includes public channels the user
is not in**, so one unfiltered result from such a channel would stop a membership rule from
covering the whole page. Search can also lag a just-posted message by a minute or two (ADR 0223).

## Design

### D1. Per-verb predicates for extra scopes (phase p1)

`PolicyExtraScope` (`catalogue.py:30-49`) gains one field:

```python
    # Per verb, the predicate a rule for that verb uses when it is not ``predicate``. Read and
    # search share the ``slack.read_messages`` operation key and the engine matches on operation
    # keys, so the predicate is the only place a rule can say which of the two it allows.
    verb_predicates: tuple[tuple[Verb, str], ...] = ()
```

and two helpers in `catalogue.py`, both exported in `__all__`:

```python
def extra_predicate_for(extra: PolicyExtraScope, verb: Verb) -> str:
    """The predicate a rule for ``verb`` uses on ``extra``: its ``verb_predicates`` entry, or
    ``extra.predicate``."""

def extra_predicates(extra: PolicyExtraScope) -> frozenset[str]:
    """``extra.predicate`` and every predicate in ``extra.verb_predicates``."""
```

Every place that compiles or recognises an extra scope uses them:

1. `catalogue._compile_catalogue_rules`: for an extra, group `allowed_verbs` by
   `extra_predicate_for(extra, verb)`. For each predicate (in first-seen order of `allowed_verbs`),
   `operations` = union of `extra_operations_for(extra, v)` over that predicate's verbs; skip a
   predicate whose operations are empty; build
   `PolicyRule(id=predicate, predicate=predicate, value=value, operations=operations)`. Return
   `store.merge_rules([...])`, or `[]` when no rule was built.
2. `scope_rows.compile_row`, extra branch: the same grouping over `row.verbs & set(extra.verbs)`,
   with `operations = row.operations & union(extra_operations_for(extra, v) for v in that
   predicate's verbs)`. One `PolicyRule(id=row.id, predicate=..., value=row.value,
   operations=...)` per predicate with non-empty operations, in `extra.verbs` order.
3. `scope_rows.scope_of_rule`: the extra candidates test
   `rule.predicate in catalogue.extra_predicates(extra)` instead of `extra.predicate == rule.predicate`.
4. `describe._catalogue_verbs`, extra fallback: `verb for extra in EXTRA_SCOPES.values() for verb
   in extra.verbs if catalogue.extra_predicate_for(extra, verb) == predicate`.
5. `outdated.missing_operations`, extra loop: skip unless `rule.predicate in
   catalogue.extra_predicates(extra)`, and only count verbs `v` with
   `catalogue.extra_predicate_for(extra, v) == rule.predicate`.

`describe.row_sentence` already looks up the selector of `extra.predicate` for the scope type. Both
predicates of a scope use the same `scope_type`, so it needs no change.

**The mentions fix.** `EXTRA_SCOPES["slack.mentions_of_me"]` gets
`verb_predicates=((Verb.SEARCH, "slack.mentions_of_me_all_results"),)`. In `scopes.py`:

- `_mentions_of_me_matches` keeps only the thread case: `ctx.tool ==
  "slack_get_thread_replies"` and at least one message mentions the user.
- New `_mentions_of_me_all_results_matches`: `ctx.tool == "slack_search_messages"`, results
  non-empty, none `None`, every one mentions the user.
- Both share one helper, `_mention_flags(ctx) -> list[bool] | None`. It returns `None` when
  `own_user_id` is missing or not a non-empty `str`, or when `raw_data` is empty or holds a `None`.
  Otherwise it returns one flag per message, using the regex the code has today.
- `NEW_SCOPE_SELECTORS["slack.mentions_of_me_all_results"] = ScopeSelector(predicate=
  "slack.mentions_of_me_all_results", scope_type="slack.mentions_of_me", kind=ScopeKind.ATTRIBUTE,
  resolves_from=ResolvesFrom.FETCHED, matches=_mentions_of_me_all_results_matches)`.

The result: a stored mentions row with verbs `{read, search}` compiles to two rules, and behaves as
it does today. A row with only `{read}` compiles to the thread rule only, so searches need review.
A row with only `{search}` compiles to the search rule only.

**Tokens made before the fix.** The limit of a token created before p1 holds
`("slack.mentions_of_me", "slack.read_messages")`, so the new search predicate would be dropped
from it and the routine's mentions search would stop auto-accepting. To prevent that,
`scoped_tokens.py` gets:

```python
# A token's limit records (predicate, operation) pairs as they compiled when the token was made.
# When a scope later splits one predicate per verb (ADR 0227), the old pair stands for both.
_LIMIT_PAIR_SUCCESSORS: dict[tuple[str, str], tuple[str, str]] = {
    ("slack.mentions_of_me", "slack.read_messages"):
        ("slack.mentions_of_me_all_results", "slack.read_messages"),
}
```

`_limits_from_json` adds the successor pair to any limit that holds the key pair. So a token
created before the fix keeps exactly the reach it had, including search under a read-only row.
This is accepted: such a token was made with that reach, and a token expires within its day limit.
New tokens record both pairs, or only the one their row's verbs give.

### D2. Membership in the Slack client (phase p2)

New public methods on `SlackClient`, placed after `resolve_is_one_to_one_dm`:

```python
def resolve_is_member(self, channel_id: str) -> bool:
    """Whether the signed-in user is a member of ``channel_id`` right now, from one live
    ``conversations.info`` call. Never cached: an auto-accept decision depends on it (ADR 0225).
    A 1:1 DM (``is_im``) counts as membership, since a user token sees only its own DMs; any
    other conversation needs ``is_member`` to be exactly ``True``. Reads False on an empty id,
    an API error or an unexpected response. The same response also fills the group-DM,
    one-to-one-DM and IM-counterpart caches, so the connector's other channel-type checks for
    this call make no request of their own."""

def my_conversation_ids(self) -> frozenset[str] | None:
    """The id of every conversation (public and private channels, group DMs, 1:1 DMs) the
    signed-in user is a member of right now, from a live ``users.conversations`` walk. Never
    cached (ADR 0225). None when the own user id or the walk fails."""
```

`resolve_is_member` body, in order:

1. `if not channel_id: return False`.
2. `response = self._client.conversations_info(channel=channel_id)`. On `SlackApiError`, log at
   debug (`"Could not resolve membership for %s: %s"`) and return `False`.
3. `channel = response.get("channel")`. If it is not a `dict`, return `False`.
4. `is_im = channel.get("is_im") is True`.
5. `self._channel_is_mpim_cache[channel_id] = channel.get("is_mpim") is True`.
6. If `channel_id.startswith("D")`: `self._im_channel_cache[channel_id] = is_im` and
   `self._im_counterpart_cache[channel_id] = str(channel.get("user") or "") if is_im else ""`.
7. `return is_im or channel.get("is_member") is True`.

`my_conversation_ids` body: `own = self.own_user_id()`; if `not own`, return `None`;
`ids = self._conversation_ids_for_user(own, types="public_channel,private_channel,mpim,im")`;
return `None` if `ids is None`, else `frozenset(ids)`.

**Filtered search.** `search_messages` gains two keyword-only parameters:

```python
def search_messages(
    self, query: str = "", count: int = 20, participant: str = "", days: int = 90, minutes: int = 0,
    *, only_channels: frozenset[str] | None = None, exclude_channels: frozenset[str] = frozenset(),
) -> list[SlackMessage]:
```

A result is **kept** when `(only_channels is None or m.channel_id in only_channels) and
m.channel_id not in exclude_channels`. The filter is **active** when `only_channels is not None or
exclude_channels`. While the filter is inactive, every path behaves exactly as today and makes the
same requests.

While the filter is active:

- Participant path: filter `_search_by_participant`'s result list, then truncate to `count`.
- Search paths (days and minutes): new constant and helper.

```python
# Pages a filtered search reads at most (100 matches each, Slack's page maximum). Dropping results
# from conversations the caller is not in can leave a page short; three Tier 2 requests bound it.
_FILTERED_SEARCH_PAGE_CAP = 3

def _search_filtered(
    self, search_query: str, count: int, keep: Callable[[SlackMessage], bool],
    cutoff: datetime | None, sort_kwargs: dict[str, str],
) -> list[SlackMessage]:
```

The loop runs for `page` in `1.._FILTERED_SEARCH_PAGE_CAP`:

1. Call `self._client.search_messages(query=search_query, count=100, page=page, **sort_kwargs)`.
   `SlackApiError` → `SlackClientError(f"search_messages failed: {self._describe_error(exc)}")`.
2. Parse the matches with `_parse_search_match`.
3. When `cutoff` is set, drop matches with `m.timestamp is None or m.timestamp < cutoff`.
4. Extend the result with the kept ones.
5. Stop when `len(result) >= count`, or when the page held fewer than 100 matches, or when
   `page >= (response.get("messages") or {}).get("paging", {}).get("pages", 1)`, or (cutoff set)
   when any match on the page was older than `cutoff`.

After the loop:

- Cutoff set: sort newest first, as `_search_recent` does.
- Then truncate to `count`.
- If the cap was reached with fewer than `count` kept, log a warning:
  `"search_messages filtered search stopped after %d pages with %d of %d results"`.

The minutes path passes `sort_kwargs={"sort": "timestamp", "sort_dir": "desc"}` and the `after:`
query `_search_recent` builds today. The days path passes `{}` and its `after:` query. Factor the
two query builders so both the filtered and unfiltered branches use them.

`scripts/slack_call_budget.py` gets:

- In the fake: `auth_test` returning `{"user_id": "U0007"}`.
- `conversations_info` returning `"is_member": True`.
- `search_messages` honouring `page` and returning
  `"paging": {"pages": 3}` with `count` matches whose channel ids alternate between the channels
  `U0007` is in and ones it is not in.
- Two new cases:
  - `("slack_search_messages(minutes=…, member_only)", lambda c: c.search_messages(minutes=60,
    count=20, only_channels=c.my_conversation_ids()))`
  - `("resolve_is_member()", lambda c: c.resolve_is_member("C0001"))`

### D3. The scope "Slack — conversations I'm a member of" (phase p3)

Catalogue entry, added after `slack.direct_messages_to_me`:

```python
    "slack.my_conversations": PolicyExtraScope(
        predicate="slack.my_conversations", connector="slack",
        verbs=(Verb.READ, Verb.SEARCH),
        label="Slack — conversations I'm a member of", needs_value=False,
        value_hint="C0123456789, G0123456789 (optional: never covered)",
        verb_predicates=((Verb.SEARCH, "slack.my_conversations_all_results"),),
        value_optional=True,
    ),
```

`PolicyExtraScope` gains `value_optional: bool = False`, which means a value may be given but is
not required. `scope_catalogue()` adds `"value_optional": extra.value_optional` to each extra entry
and `"value_optional": False` to each propose-group entry.

Selectors in `scopes.py`:

```python
_MY_CONVERSATION_READ_TOOLS = frozenset({"slack_get_channel_history", "slack_get_thread_replies"})


def _my_conversations_matches(value: Any, ctx: ReviewContext) -> bool:
    if ctx.tool not in _MY_CONVERSATION_READ_TOOLS or ctx.args.get("is_member") is not True:
        return False
    channel_id = ctx.args.get("channel_id", "") or ""
    return bool(channel_id) and channel_id not in set(_values_of(value))


def _my_conversations_all_results_matches(value: Any, ctx: ReviewContext) -> bool:
    if ctx.tool != "slack_search_messages":
        return False
    if ctx.args.get("member_only") is not True or ctx.args.get("all_results_in_my_conversations") is not True:
        return False
    messages = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
    if any(m is None for m in messages):
        return False
    excluded = set(_values_of(value))
    return all(getattr(m, "channel_id", "") not in excluded for m in messages)
```

Both are registered in `NEW_SCOPE_SELECTORS` with `scope_type="slack.my_conversations"`,
`kind=ScopeKind.ATTRIBUTE` and `resolves_from=ResolvesFrom.FETCHED`, so `check_policy` answers
`unknown` for them, like the other run-time checks. An **empty** member-only search matches: it
discloses nothing, and an hourly sweep over a quiet hour must not be denied. This differs on purpose
from `slack.mentions_of_me_all_results`, which needs at least one result. There the results are the
only evidence. Here the evidence is `member_only=true` together with the live membership set the
connector checked.

Value format: `VALUE_FORMATS["slack.my_conversations"] = _SLACK_ID` and
`VALUE_FORMATS["slack.my_conversations_all_results"] = _SLACK_ID` (`value_format.py`).
`_compile_catalogue_rules` normalises the value against `extra_predicates(extra)`, the whole set,
in place of today's `{extra.predicate}`.

Sentence: `describe.py` gets `EXCLUSION_SCOPE_TYPES: frozenset[str] =
frozenset({"slack.my_conversations"})`. In `rule_sentence` and `row_sentence`, when the scope type
is in it and there is a value, the subject is `f"{noun} except {phrase}"`. So the rule reads
`Slack - my conversations except C0123456789: allow read, search`. Without a value it reads
`Slack - my conversations: allow read, search`. `settings_controller.sentence_parts` needs no change,
because it splits `row_sentence` around the value.

Computed arguments: `web/mcp_dispatch.py`'s `_CONNECTOR_COMPUTED_ARGS` gains `"is_member"` and
`"all_results_in_my_conversations"`. `member_only` and `exclude_channels` are the agent's own
arguments and stay in.

Settings page (`settings_window_html.py:1057`): show the value input when
`group.needs_value || group.value_optional`. Its `aria-label` is `"Value, optional
(comma-separated for more than one)"` when `!group.needs_value`, and stays as today otherwise.

Bridge tool text (`web/mcp_tools.py:188`): replace `omit if needs_value is false` with `omit if
needs_value is false, unless value_optional is true and you want to give one`.

### D4. Connector wiring (phase p4)

`_get_channel_history` and `_get_thread_replies` (`connectors/slack.py:568`, `:640`): straight after
`channel_display` is computed, add
`is_member = await self._fetch(self._slack.resolve_is_member, channel_id)`, **before** the
`resolve_is_group_dm`/`resolve_is_self_dm`/`resolve_is_one_to_one_dm` calls, so those hit the
caches it fills. Add `"is_member": is_member` to `args`.

`slack_search_messages` tool spec gets two params, inserted before `reason`:

```python
ToolParam(
    "member_only", "bool", required=False, default=False,
    description=(
        "Only return messages from conversations you are a member of right now (channels, DMs "
        "and group DMs); PrivacyFence asks Slack for your conversations on each call. Use it for "
        "a sweep of everything since your last run; a 'conversations I'm a member of' rule can "
        "then auto-accept the search."
    ),
),
ToolParam(
    "exclude_channels", "str", required=False, default="",
    description=(
        "Comma-separated channel ids whose messages are dropped from the results. Pass the ids "
        "a 'conversations I'm a member of' rule excludes; a result from one of them stops that "
        "rule from covering the search."
    ),
),
```

Append to the tool's description, before "Requires user approval.":
`"With member_only, results come only from conversations you are in, up to 3 search pages."`

`_search_messages(self, query="", count=20, participant="", days=90, minutes=0,
member_only=False, exclude_channels="")`:

1. `excluded = frozenset(c.strip() for c in exclude_channels.split(",") if c.strip())`.
2. If `member_only`: `member_ids = await self._fetch(self._slack.my_conversation_ids)`. If it is
   `None`, raise `RuntimeError("Could not read the Slack conversations you are a member of; try
   again in a minute.")`. Otherwise set `member_ids = None`.
3. `messages = await self._fetch(self._slack.search_messages, query, count, participant, days,
   minutes, only_channels=member_ids, exclude_channels=excluded)`.
4. Preview: if `member_only`, add `preview["Conversations"] = "only ones you are a member of"`. If
   `excluded`, add `preview["Excluded"] = ", ".join(sorted(excluded))`.
5. `args` gains `"member_only": member_only`, `"exclude_channels": sorted(excluded)` and
   `"all_results_in_my_conversations": member_ids is not None and all(m.channel_id in member_ids
   for m in messages)`.

`scripts/qa_fixture_recorder.py` `check_slack`: after the thread-replies check, inside the same
`try`, append a check that records no fixture:

```python
member = client.resolve_is_member(channel_id)
ids = client.my_conversation_ids()
listed = ids is not None and channel_id in ids
ok = member and listed
note = (
    "conversations.info is_member and users.conversations agree" if ok
    else f"is_member={member}, listed by users.conversations={listed}"
)
results.append(CheckResult("slack", "conversation_membership", target, ok, note))
```

This is the live proof that Slack answers the membership questions the way D2 assumes.

### D5. Gate-level test (phase p4)

In `tests/unit/connectors/test_slack_connector.py`, add `TestMemberConversationRules`. It drives the
real `gated_call` with a policy holding one compiled `slack.my_conversations` row (verbs read and
search, value `["C_EXCLUDED"]`), using the existing fake-gate helpers of that file and
`docs/coding-and-testing-guidelines.md` §2.4. It checks:

- history in a member channel auto-accepts;
- history with `resolve_is_member` → False needs review;
- history on `C_EXCLUDED` needs review;
- a `member_only=True` search auto-accepts;
- the same search with `member_only=False` needs review.

### D6. Exclusions

The value lists channel ids the rule never covers. Ids are shown raw in Settings: the name resolver
(`policy/resource_registry.py`) is keyed to grant types and is not extended. The agent has to pass
the same ids as `exclude_channels` on a member-only search. If it does not, a result from an
excluded channel makes the search need review. That fails closed.

### D7. What the routine should do (documented in p5)

Each run:

1. `slack_search_messages(minutes=<since last run>, member_only=true, exclude_channels=<the rule's
   ids>, count=100)`. This costs one `users.conversations` walk (⌈conversations/200⌉ requests) and
   1–3 `search.messages` requests.
2. For a result whose thread needs context, `slack_get_thread_replies`. This costs one
   `conversations.replies` and one `conversations.info` request.
3. Use `slack_get_channel_history` with `minutes` only for a conversation where the search-index
   delay matters.

On an app distributed outside the Marketplace, step 2 is limited to 1 request per minute.

### D8. Names, at a glance

| Thing | Name |
|---|---|
| Catalogue group | `slack.my_conversations` |
| Read predicate | `slack.my_conversations` |
| Search predicate | `slack.my_conversations_all_results` |
| Mentions search predicate | `slack.mentions_of_me_all_results` |
| Computed args | `is_member` (history, replies), `all_results_in_my_conversations` (search) |
| Agent args | `member_only: bool`, `exclude_channels: str` |
| Client methods | `resolve_is_member`, `my_conversation_ids`, `_search_filtered`; constant `_FILTERED_SEARCH_PAGE_CAP = 3` |
| Catalogue fields | `PolicyExtraScope.verb_predicates`, `PolicyExtraScope.value_optional`; helpers `extra_predicate_for`, `extra_predicates` |

### D9. Rejected

- **A channel-kind filter on the new rule.** It is already available. `slack.direct_messages_to_me`
  covers every 1:1 DM, and `group_dm` covers every group DM (it is not per chat, despite its button
  label). A rule for channels only would be a third kind, and nobody has asked for it.
- **A last-message timestamp on `slack_list_dms`, `slack_list_group_chats` and
  `slack_list_channels`.** `conversations.list` returns no last-activity time; its `updated` field
  is when the channel object last changed. The time would cost a `conversations.info` or a
  one-message `conversations.history` per conversation, the same Tier 3 call as just reading it.
  Member-only search answers "what changed" in 1–3 requests.
- **Caching membership** (as `_MEMBERSHIP_CACHE_TTL` caches members for display). A rule decision
  must not outlive leaving a channel. The live check costs one Tier 3 call per history or replies
  read, and p2 makes that call answer the three channel-type checks too.
- **Checking membership with `users.conversations` for history and replies.** That is
  ⌈conversations/200⌉ requests per read instead of one.
- **Filtering a mixed search page inside the rule** (ADR 0221 rejected the same for mentions). Here
  the filter is an argument the agent passes, shown in the card's preview and recorded in the
  request arguments, so the result is not silently partial.
- **Two catalogue groups** (one for read, one for search) instead of per-verb predicates. The
  exclude list would have to be entered twice, and the mentions bug would need its own mechanism.
- **Telling the engine about verbs.** That would change every rule's evaluation path for one
  operation key that carries two verbs.

## ADRs

The next free number on `main` is 0224, but the open `plan/org-agent-redirect-attestation` already
claims 0224. These take 0225–0227. The retirement phase renumbers them if another branch merged
them first (ADR README rule 6).

- **0225 — A Slack rule can cover the conversations the signed-in user is a member of, checked
  live on each call.** Membership comes from `conversations.info` for one conversation and from
  `users.conversations` for a search, and is never cached. The rule can take an exclude list. Relates
  to 0172, 0221.
- **0226 — Member-only Slack search drops results from other conversations before the gate, on the
  agent's request.** `member_only` and `exclude_channels` are request arguments. Up to 3 pages are
  read. An empty page auto-accepts. Records the rate-limit reasoning and rejects per-conversation
  sweeps and listing timestamps. Relates to 0221, 0223.
- **0227 — An extra scope can give each verb its own predicate, so a rule allows only the verbs it
  was saved with.** Fixes the read-only mentions rule. Existing token limits keep their reach
  through `_LIMIT_PAIR_SUCCESSORS`. Amends 0221.

## Manual steps

No `manual_before`. Everything a phase needs exists. The live Slack check runs on the self-hosted
runner through `qa-record-fixture.yml`, in phase p4.

`manual_after` (the step-by-step page is
[Slack member-conversations: manual checks](https://claude.ai/artifact/FXwwTf5ZujCCvihUBwZdMJ)):

- **ma1-live-member-rule**: on the TKCS install, add the new rule with one excluded channel, add it
  to the inbox-triage token, and check the five verdicts of D5 against real Slack in an unattended
  session.
- **ma2-mentions-verbs**: check that a read-only mentions rule no longer covers a search, that a
  read+search one still does, and that the existing inbox-triage token still covers its mentions
  search.

## Risks and open questions

- **`is_member` on group DMs.** D2 assumes `conversations.info` returns `is_member: true` for a
  group DM the user is in. If it does not, group DMs fail closed: they need review under the new
  rule but stay covered by `group_dm`. The p4 QA check proves the channel case only. A p2 worker who
  finds a recorded fixture in `tests/fixtures/live/slack/` contradicting the `is_member` or `is_im`
  shape stops with `status=blocked`.
- **Tier of `users.conversations`.** `scripts/slack_call_budget.py` prices it at 100 per minute.
  Slack's method page should be checked in p5 (D7 docs). Whichever is right, one walk per search is
  well under the limit.
- **Search reaches public channels.** If Slack ever stopped returning results from public channels
  the user is not in, member-only search would simply drop nothing. That is not a risk to
  correctness.
- **Existing tests that pin today's mentions behaviour.** `tests/unit/policy/test_scopes.py`
  (`TestSlackMentionsOfMe`), `tests/unit/policy/test_engine.py`
  (`TestSlackScopes.test_a_search_rule_auto_accepts_through_evaluate`) and
  `tests/unit/policy/test_catalogue.py` (`test_rules_compile_to_the_read_messages_operation`)
  assert that one `slack.mentions_of_me` predicate covers search. p1 changes them on purpose, and
  says so in its commit message. Any *other* failing test in p1 that is not about mentions means
  the compile change leaked into proposable scopes: stop with `status=blocked`.
- **`_is_lossless` in `add_rules`.** It must stay true for a single per-verb rule. If
  `TestAddRules` in `tests/unit/policy/test_scope_rows.py` shows a search-only mentions rule
  becoming a custom row, D1 step 2 or 3 is wrong: stop with `status=blocked`.
- **ADR number clash.** See ADRs.

## Implementation manifest

```yaml
plan_slug: slack-member-conversations
feature_branch: feature/slack-member-conversations
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/FXwwTf5ZujCCvihUBwZdMJ
manual_steps_source: docs/slack-member-conversations-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-live-member-rule
    title: Check the "conversations I'm a member of" rule against real Slack in an unattended session
    why: CI fakes Slack; only a real workspace shows live membership, the exclude list and member-only search together under a scoped token
  - id: ma2-mentions-verbs
    title: Check that mentions rules now honour their verbs and the existing token still covers its search
    why: proves ADR 0227's split on a real rule set and that a token made before it kept its reach
verify_after_merge:
  - python3 -m pytest tests/unit/policy tests/unit/test_slack_client.py tests/unit/connectors/test_slack_connector.py tests/unit/test_scoped_tokens.py tests/unit/web/test_mcp_dispatch.py tests/unit/test_settings_window_html.py tests/unit/test_qa_fixture_recorder.py -q
  - python3 scripts/slack_call_budget.py
final_checks:
  - docs/slack-member-conversations-plan.md and docs/slack-member-conversations-plan-manual-steps.html are deleted and `git grep -n "slack-member-conversations-plan"` finds nothing
  - ADRs 0225, 0226 and 0227 (or their renumbered successors) exist, are Accepted, and are listed in docs/adr/README.md's index
  - CHANGELOG.md has the entries under "## [Unreleased]" and no new version heading
  - "`git grep -n \"slack.mentions_of_me_all_results\" src/privacyfence/policy` matches in scopes.py and catalogue.py"
phases:
  - id: p1-extra-verb-predicates
    title: Extra scopes can give each verb its own predicate; mentions rules honour their verbs
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/scope_rows.py
      - src/privacyfence/policy/describe.py
      - src/privacyfence/policy/outdated.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/scoped_tokens.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_scope_rows.py
      - tests/unit/policy/test_describe.py
      - tests/unit/policy/test_outdated.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_engine.py
      - tests/unit/test_scoped_tokens.py
    brief: |
      Read docs/slack-member-conversations-plan.md, sections "Current state / Policy model" and
      "Design D1", before you start. Must-read: CLAUDE.md, CONTRIBUTING.md,
      docs/coding-and-testing-guidelines.md (§1.5 the gate is a security boundary, §2), docs/adr/0172-*,
      docs/adr/0221-*.
      1. catalogue.py: add the `verb_predicates` field to `PolicyExtraScope` (exact comment and
         default in D1), and the helpers `extra_predicate_for` and `extra_predicates`, both in
         `__all__`. Rewrite the extra branch of `_compile_catalogue_rules` per D1 item 1. Leave
         the value normalisation on `{extra.predicate}` for now: p3 widens it.
      2. scope_rows.py: rewrite the extra branch of `compile_row` per D1 item 2, and the extra
         candidates in `scope_of_rule` per D1 item 3.
      3. describe.py: `_catalogue_verbs` per D1 item 4. outdated.py: `missing_operations` per D1
         item 5.
      4. scopes.py: split the mentions selector per D1 "The mentions fix": `_mention_flags`,
         `_mentions_of_me_matches` (thread only), `_mentions_of_me_all_results_matches` (search
         only), and the new `NEW_SCOPE_SELECTORS` entry. Update the module docstring sentence at
         scopes.py:38 to name both predicates.
      5. catalogue.py: give `EXTRA_SCOPES["slack.mentions_of_me"]`
         `verb_predicates=((Verb.SEARCH, "slack.mentions_of_me_all_results"),)`.
      6. scoped_tokens.py: add `_LIMIT_PAIR_SUCCESSORS` (exact text in D1) and apply it at the end
         of `_limits_from_json`. For each rid, `pairs | {succ for pair, succ in
         _LIMIT_PAIR_SUCCESSORS.items() if pair in pairs}`.
      7. Tests. Write each new one failing first.
         - test_scopes.py `TestSlackMentionsOfMe`: thread cases on "slack.mentions_of_me"; search
           cases move to a new `TestSlackMentionsOfMeAllResults` on
           "slack.mentions_of_me_all_results"; add
           `test_thread_selector_never_matches_a_search` and
           `test_search_selector_never_matches_a_thread`.
         - test_engine.py `TestSlackScopes`: the search test uses the `_all_results` predicate; add
           `test_a_read_only_mentions_rule_does_not_auto_accept_a_search`. It compiles rules with
           `catalogue.rules_for_catalogue_entry("slack.mentions_of_me", None, [Verb.READ])` and
           evaluates a search whose results all mention the user, expecting `False`.
         - test_catalogue.py `TestSlackExtras`: `test_rules_compile_to_the_read_messages_operation`
           now expects the predicates `{"slack.mentions_of_me", "slack.mentions_of_me_all_results"}`
           for read+search; add `test_mentions_search_only_compiles_the_all_results_predicate`.
         - test_scope_rows.py: add `TestExtraVerbPredicates`. A row with scope "slack.mentions_of_me"
           and verbs {read, search} compiles to two rules sharing the row id; {read} to one rule
           with predicate "slack.mentions_of_me"; `add_rules` of a search-only compiled rule
           yields a scoped row (scope "slack.mentions_of_me", verbs {search}), not a custom row.
         - test_describe.py: `rule_verbs` of a "slack.mentions_of_me_all_results" rule is
           (Verb.SEARCH,); of a "slack.mentions_of_me" rule is (Verb.READ,).
         - test_outdated.py: a "slack.mentions_of_me_all_results" rule on {"slack.read_messages"}
           reports no missing operations.
         - tests/unit/test_scoped_tokens.py: a token JSON whose rule_limits holds
           ["slack.mentions_of_me", "slack.read_messages"] reads back with the
           `_all_results` pair too; a limit without it is unchanged.
      8. Run `ruff check .` and `python3 -m pytest tests/unit -q`. Commit with a message that says
         the mentions tests changed on purpose (read-only mentions rules no longer cover search).
      Stop with status=blocked if a test about a proposable scope (not Slack mentions) fails after
      step 2, or if `add_rules` turns a single mentions rule into a custom row (see the plan's
      Risks).
    acceptance:
      - python3 -m pytest tests/unit/policy/test_engine.py::TestSlackScopes::test_a_read_only_mentions_rule_does_not_auto_accept_a_search -q passes
      - python3 -m pytest tests/unit/policy/test_scope_rows.py::TestExtraVerbPredicates -q passes
      - python3 -m pytest tests/unit/policy tests/unit/test_scoped_tokens.py -q passes
      - grep -n "_LIMIT_PAIR_SUCCESSORS" src/privacyfence/scoped_tokens.py matches at least twice
      - grep -n "def extra_predicate_for\|def extra_predicates" src/privacyfence/policy/catalogue.py matches twice
      - ruff check . passes

  - id: p2-slack-client-membership
    title: Slack client checks membership live and can filter a search to the user's conversations
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/slack_client.py
      - scripts/slack_call_budget.py
      - tests/unit/test_slack_client.py
    brief: |
      Read docs/slack-member-conversations-plan.md, sections "Current state / Slack client",
      "Slack API limits" and "Design D2", before you start. Must-read: CLAUDE.md, CONTRIBUTING.md,
      docs/coding-and-testing-guidelines.md, docs/adr/0116-*, docs/adr/0223-*.
      1. slack_client.py: add `resolve_is_member` after `resolve_is_one_to_one_dm`, with the exact
         docstring and the seven body steps of D2. Add `my_conversation_ids` after it (D2).
      2. Add `_FILTERED_SEARCH_PAGE_CAP = 3` with its comment, next to `_MAX_WINDOW_MINUTES`, and
         `_search_filtered` per D2. Import `Callable` from `typing` if it is not imported.
      3. `search_messages`: add the keyword-only `only_channels` and `exclude_channels` parameters
         and route per D2. Unfiltered calls must make exactly today's requests: the existing tests
         in tests/unit/test_slack_client.py for `search_messages` and `_search_recent` pass
         unchanged. Extend the docstring with one paragraph on the filter and the page cap.
      4. scripts/slack_call_budget.py: the fake and case changes listed at the end of D2.
      5. Tests in tests/unit/test_slack_client.py. Write each failing first, and use the fake-WebClient
         style the file already uses.
         - `TestResolveIsMember`: a channel with is_member true → True; is_member false → False;
           is_member missing → False; an IM (is_im true, no is_member) → True; SlackApiError →
           False; an empty id → False with no API call; two calls → two conversations_info calls
           (not cached); after it, `resolve_is_group_dm`, `resolve_is_one_to_one_dm` and
           `resolve_is_self_dm` for the same id make no further conversations_info call.
         - `TestMyConversationIds`: returns the users.conversations ids across pages as a
           frozenset, with types "public_channel,private_channel,mpim,im"; None when auth.test
           fails; None when users.conversations raises.
         - `TestFilteredSearch`:
           - `test_only_channels_drops_other_conversations`;
           - `test_exclude_channels_drops_listed_ids`;
           - `test_reads_a_second_page_when_the_first_is_short_of_count`;
           - `test_stops_at_the_page_cap` (asserts 3 search_messages calls and the warning);
           - `test_minutes_path_stops_at_a_page_older_than_the_cutoff`;
           - `test_participant_path_filters_its_result`;
           - `test_unfiltered_search_makes_one_request` (page not passed).
      6. Run `python3 scripts/slack_call_budget.py`. Its warm member-only case must show
         users.conversations and at most 3 search.messages calls, and the resolve_is_member case
         exactly 1 conversations.info call. Put both lines in the commit message.
      7. Run `ruff check .` and `python3 -m pytest tests/unit -q`.
      Stop with status=blocked if a recorded fixture under tests/fixtures/live/slack/ contradicts
      the conversations.info shape D2 relies on (`is_member`, `is_im`, `is_mpim`, `user`).
    acceptance:
      - python3 -m pytest tests/unit/test_slack_client.py -k "ResolveIsMember or MyConversationIds or FilteredSearch" -q passes
      - python3 -m pytest tests/unit/test_slack_client.py -q passes
      - python3 scripts/slack_call_budget.py exits 0 and its output contains "member_only" and "resolve_is_member()"
      - grep -n "_FILTERED_SEARCH_PAGE_CAP = 3" src/privacyfence/slack_client.py matches
      - ruff check . passes

  - id: p3-member-scope-policy
    title: The "conversations I'm a member of" scope, its selectors, optional exclude value and Settings field
    depends_on: [p1-extra-verb-predicates]
    complexity: M
    touches:
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/value_format.py
      - src/privacyfence/policy/describe.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/web/mcp_tools.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_value_format.py
      - tests/unit/policy/test_describe.py
      - tests/unit/policy/test_engine.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/test_settings_window_html.py
    brief: |
      Read docs/slack-member-conversations-plan.md "Design D3", "D6" and "D8" first. p1 has added
      `verb_predicates`, `extra_predicate_for` and `extra_predicates`; use them. Pattern commit for
      a Slack extra scope: ae787c42.
      1. catalogue.py: add `value_optional: bool = False` to `PolicyExtraScope`; add the
         "slack.my_conversations" entry exactly as in D3; add "value_optional" to every
         `scope_catalogue()` entry (D3); in `_compile_catalogue_rules`, normalise the value
         against `extra_predicates(extra)` for an extra. Update the module docstring's list of
         extra scopes with "and the scope for conversations the signed-in user is a member of".
      2. scopes.py: add `_MY_CONVERSATION_READ_TOOLS`, `_my_conversations_matches`,
         `_my_conversations_all_results_matches` (exact code in D3), and their two
         `NEW_SCOPE_SELECTORS` entries (scope_type "slack.my_conversations", ATTRIBUTE, FETCHED).
         Extend the module docstring sentence about computed arguments with `is_member` and
         `all_results_in_my_conversations`.
      3. value_format.py: the two `VALUE_FORMATS` entries (D3).
      4. describe.py: `EXCLUSION_SCOPE_TYPES` and the "except" subject in `rule_sentence` and
         `row_sentence` (D3).
      5. web/mcp_dispatch.py: add "is_member" and "all_results_in_my_conversations" to
         `_CONNECTOR_COMPUTED_ARGS`. web/mcp_tools.py:188: the wording change in D3.
      6. settings_window_html.py:1057: show the input for `group.needs_value || group.value_optional`,
         with the aria-label from D3.
      7. Tests, each failing first:
         - test_scopes.py: new `TestSlackMyConversations`:
           - history in a member channel → True;
           - is_member False / missing / "true" → False;
           - excluded channel → False;
           - thread replies → True;
           - search tool → False;
           - empty channel_id → False.
         - test_scopes.py: new `TestSlackMyConversationsAllResults`:
           - member_only search with every result's channel not excluded → True;
           - empty result list → True;
           - raw_data None → False;
           - member_only False → False;
           - all_results_in_my_conversations False → False;
           - one result in an excluded channel → False;
           - history tool → False.
         - test_catalogue.py: in `TestSlackExtras`:
           - the new entry offers ["read", "search"], with needs_value False and value_optional True;
           - read+search with value ["C1"] compiles to rules with predicates
             {"slack.my_conversations", "slack.my_conversations_all_results"}, both with value ["C1"];
           - read with no value compiles;
           - a value "#general" raises RuleValueError;
           - every propose-group entry carries value_optional False.
         - test_value_format.py: "C0123456789" accepted and "#hr" refused for both predicates.
         - test_describe.py: the sentence with value ["C1"] is
           "Slack - my conversations except C1: allow read, search", and without a value
           "Slack - my conversations: allow read, search".
         - test_engine.py `TestSlackScopes`: `preflight` on a "slack.my_conversations" rule is
           ("unknown", "").
         - tests/unit/web/test_mcp_dispatch.py: an agent's check_policy args carrying is_member=True
           and all_results_in_my_conversations=True do not reach preflight (copy the existing
           computed-args test).
         - tests/unit/test_settings_window_html.py: the rendered script source contains
           `group.needs_value || group.value_optional`.
      8. Run `ruff check .`, `python3 -m pytest tests/unit -q` and `python3 -m mypy src/privacyfence`.
      Stop with status=blocked if `scope_catalogue()`'s new key breaks a test outside the files
      above (for example an org-settings snapshot): it means a consumer this plan did not find.
    acceptance:
      - python3 -m pytest tests/unit/policy/test_scopes.py -k "MyConversations" -q passes
      - python3 -m pytest tests/unit/policy tests/unit/web/test_mcp_dispatch.py tests/unit/test_settings_window_html.py -q passes
      - grep -n "\"slack.my_conversations\": PolicyExtraScope" src/privacyfence/policy/catalogue.py matches
      - grep -n "all_results_in_my_conversations" src/privacyfence/web/mcp_dispatch.py matches
      - python3 -m mypy src/privacyfence passes
      - ruff check . passes

  - id: p4-slack-connector-wiring
    title: Slack connector computes membership, adds member-only search, and the live QA check proves it
    depends_on: [p2-slack-client-membership, p3-member-scope-policy]
    complexity: M
    touches:
      - src/privacyfence/connectors/slack.py
      - scripts/qa_fixture_recorder.py
      - tests/unit/connectors/test_slack_connector.py
      - tests/unit/test_qa_fixture_recorder.py
      - tests/fixtures/live/slack/**
    brief: |
      Read docs/slack-member-conversations-plan.md "Design D4", "D5" and "D7" first. Pattern
      commits: ae787c42 (computed Slack args) and feea59a8 (adding a search parameter).
      1. connectors/slack.py `_get_channel_history` and `_get_thread_replies`: compute `is_member`
         before the three channel-type resolvers and add it to `args` (D4).
      2. The `slack_search_messages` tool spec: the two `ToolParam`s and the description sentence,
         exact text in D4. `_search_messages`: the new parameters and steps 1–5 of D4.
      3. scripts/qa_fixture_recorder.py `check_slack`: the "conversation_membership" CheckResult
         (exact code in D4). It records no fixture file.
      4. Tests, each failing first:
         - tests/unit/connectors/test_slack_connector.py `TestComputedScopeArgs`: history and
           replies pass `is_member` from `resolve_is_member`; `resolve_is_member` is called before
           `resolve_is_group_dm`.
         - New `TestMemberOnlySearch`:
           - member_only passes `only_channels=<my_conversation_ids()>`;
           - exclude_channels " C1, ,C2 " passes frozenset({"C1","C2"}) and args
             exclude_channels ["C1","C2"];
           - all_results_in_my_conversations is True for an all-member result and for an empty
             one, and False when member_only is False;
           - my_conversation_ids None raises RuntimeError with the D4 message and never reaches
             gated_call;
           - the preview carries "Conversations" and "Excluded".
         - `TestMemberConversationRules` per D5.
         - tests/unit/test_qa_fixture_recorder.py `TestCheckSlack`: the membership result is ok
           when both agree, not ok when either disagrees, and records no fixture.
      5. Run `ruff check .`, `python3 -m pytest tests/unit -q`, `python3 -m mypy src/privacyfence`
         and `pytest tests/integration -v` (the tool schema changed, so
         test_mcp_daemon_contract.py must pass).
      6. Push the feature branch's current state. Then dispatch `qa-record-fixture.yml` on `main`
         with inputs connector=slack and branch=<this phase branch>. Wait for it to finish and
         pull. Its report must show `slack conversation_membership` ok. If it records an updated
         get_thread_replies.json, review the diff per docs/connector-qa.md "Reviewing recorded
         fixtures" before keeping it.
      Stop with status=blocked if the QA run shows conversation_membership not ok: D2's
      assumption about Slack's answers is wrong and the plan must be revised. Also stop if the
      dispatch cannot be made from this session (the profile's ci.dispatchable notes say how);
      report the run URL or the refusal.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_slack_connector.py -k "MemberOnlySearch or MemberConversationRules or ComputedScopeArgs" -q passes
      - python3 -m pytest tests/unit/test_qa_fixture_recorder.py -k CheckSlack -q passes
      - pytest tests/integration -q passes
      - grep -n "\"member_only\", \"bool\"" src/privacyfence/connectors/slack.py matches
      - the qa-record-fixture.yml run for connector=slack on this branch reports conversation_membership ok (run URL in the phase report)

  - id: p5-retire
    title: ADRs 0225-0227, Slack rate-limit docs, reference docs, changelog, and retire the plan
    depends_on: [p4-slack-connector-wiring]
    complexity: S
    touches:
      - docs/adr/0225-*.md
      - docs/adr/0226-*.md
      - docs/adr/0227-*.md
      - docs/adr/README.md
      - docs/adr/0221-slack-rules-cover-mentions-of-the-signed-in-user.md
      - docs/approvals-and-policy.md
      - docs/slack-setup.md
      - docs/tools-reference.md
      - docs/connector-qa.md
      - CHANGELOG.md
      - docs/slack-member-conversations-plan.md
      - docs/slack-member-conversations-plan-manual-steps.html
    brief: |
      Read docs/slack-member-conversations-plan.md in full, and docs/adr/README.md (rules and
      template).
      1. Check the next free ADR number on origin/main (`git fetch origin main && git ls-tree
         --name-only origin/main docs/adr/`). If 0225–0227 are taken, use the next three free
         numbers everywhere below.
      2. Write the three ADRs from the plan's "ADRs" section, D1–D9 and Risks, using the README
         template. Status "Accepted — <today>." Link source files and commits, never the plan.
         - 0225's Alternatives: caching membership; users.conversations per read; a kind filter.
         - 0226's: per-conversation sweeps (with the rate numbers from "Slack API limits");
           listing timestamps; filtering inside the rule.
         - 0227's: two catalogue groups; telling the engine about verbs. Its Consequences name
           `_LIMIT_PAIR_SUCCESSORS`.
         Add the three index rows to docs/adr/README.md. Add one line to 0221's Status:
         "Amended by 0227 (a mentions rule's search verb is now its own predicate)."
      3. docs/approvals-and-policy.md, the scope table near line 403:
         - change the "Slack mentions of me" row to name both predicates (search:
           every result mentions you; read: a thread with one mention);
         - add a row "Slack conversations I'm a member of | attribute | channel ids to exclude
           (optional) | `slack.my_conversations`, `slack.my_conversations_all_results` | you are a
           member of the conversation now (checked with Slack on each call) and it is not
           excluded; for a search made with member_only, every result is from such a
           conversation";
         - in the "Slack channel kind" row, say `group_dm` covers every group DM.
      4. docs/slack-setup.md: a new section "Rate limits and routines" after "Register the app".
         It has a table of the methods in the plan's "Slack API limits" with each method's tier,
         and the routine pattern of D7. Before writing the tiers, fetch each method's page
         (https://api.slack.com/methods/<method>) with WebFetch and use the tier it states. If a
         page cannot be fetched, keep the plan's figure and say so in the phase report.
      5. docs/tools-reference.md: regenerate or hand-edit the slack_search_messages row the way
         feea59a8 did. docs/connector-qa.md "Slack checks": add a bullet "**Member conversations.**
         With a 'conversations I'm a member of' rule, slack_get_channel_history on
         privacyfence-qa-control reads without a card; a member_only search of the last 60
         minutes reads without a card; the same search without member_only prompts." Add
         `conversation_membership` to the recorder table row for slack near line 356 (no fixture).
      6. CHANGELOG.md under "## [Unreleased]", three lines:
         - the new rule;
         - member_only and exclude_channels on slack_search_messages;
         - "A 'Slack — mentions of me' rule saved with only read no longer auto-accepts searches;
           add search to it if you relied on that."
      7. Delete docs/slack-member-conversations-plan.md and
         docs/slack-member-conversations-plan-manual-steps.html; `git grep -n
         "slack-member-conversations-plan"` must find nothing.
      8. Run `python3 -m pytest tests/unit -q` (the docs tests, such as test_docs_no_history.py,
         run there) and `ruff check .`.
    acceptance:
      - ls docs/adr/ shows three new ADR files for this plan, each containing "## Status" and "Accepted"
      - grep -n "slack.my_conversations" docs/approvals-and-policy.md matches
      - grep -n "Rate limits and routines" docs/slack-setup.md matches
      - git grep -n "slack-member-conversations-plan" finds nothing
      - python3 -m pytest tests/unit -q passes
```
