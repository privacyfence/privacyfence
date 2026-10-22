# Inbox-triage scopes: archive by label, token read allowance, Slack and Gmail query scopes

Plan slug: `inbox-triage-scopes`. Source: the "PrivacyFence feature requests: inbox-triage agent"
document of 22 October 2026, four requests plus one configuration step. Run it with `/implement`.

## 1. Goal

The `inbox-triage` routine runs unattended on weekdays. Every call it makes has to pass on an
auto-accept rule, and a hostile email or Slack message must not be able to steer it into anything
beyond its own outputs. After this plan:

1. **Archive by label.** A Gmail label rule can carry the `move` verb. `gmail_archive_message`
   auto-accepts only when the message already carries the rule's label, checked against the
   message's live labels at the time of the call.
2. **A token per routine.** The "rule profile" the request asks for is the existing scoped token
   (ADR 0164), which already carries a named subset of rules and tools, is deny-only, works
   unattended, and is named in the audit log. What is missing is the personal-data allowance for
   body reads: a new token flag lets `gmail_get_message`, `gmail_get_thread`,
   `slack_get_channel_history`, `slack_get_thread_replies` and `slack_search_messages` return
   content with personal data when one of the token's rules covers the call. The same phase fixes
   the New token form so that a token with no rule can be created from the tools that need none
   (Design D2, "Form fix").
3. **Slack scopes.** "Slack, mentions of me" (read, search, no value) and "Slack, direct messages to
   me" (read, no value).
4. **Gmail query scope.** "Gmail, query" (read, download) whose value is one Gmail search
   expression, evaluated by Gmail against the message at call time.

Requests 1, 3 and 4 do not change the "unattended sessions enabled" step: a scoped token starts an
unattended session without the administrator's `unattended_sessions.enabled` opt-in (ADR 0164), so
the only organization-level step left is turning scoped tokens on (`manual_before`).

Two things in the request text differ from what the code can do, and this plan follows the code:

- The request lists "scoped token / rule profile" as missing. It exists. `begin_unattended_session`
  does not get a `profile` argument; the token is the profile, presented as the bearer credential
  of the connection (Design D2).
- The request's acceptance says `privacyfence_check_policy` returns `auto_accept` or
  `requires_review` depending on the message's labels (request 1), or on a query match (request 4).
  `check_policy` decides from the call's arguments only and never fetches (ADR 0204, ADR 0216), so
  for a rule that depends on the fetched message it answers `unknown`. That stays. The acceptance
  that matters, an unattended session labelling and then archiving, is tested at the gate
  (Design D1).

## 2. Current state

- **Gmail label scope.** `src/privacyfence/policy/propose.py:314` declares scope type `gmail.label`
  with one predicate, `label_name_allowlist`, verb `update`, comparing the label named in the
  call's arguments (`scopes.py:241`). `gmail_archive_message` has verb `move`
  (`policy/registry.py:137`, operation key `gmail.archive_message` at `auto_accept.py:98`) and is
  coverable only by `i_am_sender` or `trusted_sender_domain` (`propose.py:309-313`).
- **The live-label selector exists but is unused.** `scopes.py:233` `_label_match_matches` and its
  `SCOPE_SELECTORS["label_match"]` entry (`scopes.py:478`, scope type `gmail.label`, resolves from the
  fetched item) compare a rule's value with `raw_data.labels`, case-insensitively. No
  `PROPOSABLE_SCOPES` entry uses it. `docs/approvals-and-policy.md:397` already documents it.
- **`GmailMessage.labels` holds label ids**, not names (`gmail_client.py:397`, filled from
  `labelIds` at `gmail_client.py:1604`): `INBOX`, `UNREAD`, `Label_123`. A rule value is a name.
- **`_archive_message`** (`connectors/gmail.py:1690`) already fetches the message live and passes it
  as `raw_data` to `gated_call`, so the live-label check needs a name lookup, not a new fetch.
- **Preflight.** `policy/engine.py` `preflight` evaluates only rules whose selectors resolve from
  arguments; a rule that needs the fetched message gives `unknown`. `mcp_dispatch.py:45` strips
  `_CONNECTOR_COMPUTED_ARGS = {"is_self_dm", "is_group_dm"}` from the agent's `check_policy`
  arguments, because the Slack connector computes them itself.
- **Scoped tokens** (`scoped_tokens.py`): fields `rule_ids`, `tools`, `rule_limits` and one flag,
  `allow_personal_data` (`scoped_tokens.py:67`), which `gate._personal_data_rule`
  (`gate.py:746`) honours only for `PERSONAL_DATA_RULE_TOOLS` (`auto_accept.py:384`: exports,
  downloads, page uploads, whose result holds no row). A token created before a rule gained a
  verb never gains that verb: `rule_limits` pins the operations (`gate.py:724`).
  `mcp_dispatch._pii_gate` (`mcp_dispatch.py:74`) reports `applies` for every read tool under a
  token. The audit log already names the call `scoped-token:<id>` with the token's label.
- **Slack.** Scopes: `approved_channel`, `approved_channel_all_results`, `dm_with_myself`
  (connector-computed `is_self_dm`), `group_dm` (`is_group_dm`) in `propose.py`/`scopes.py`.
  `slack_search_messages` needs `approved_channel_all_results` (every result in a named channel).
  `SlackMessage.text` keeps Slack's raw markup (`slack_client.py:1820`), so a mention is
  `<@U…>`. `SlackClient.own_user_id()` (`slack_client.py:1499`) returns the signed-in user id.
- **Gmail read scopes**: only `i_am_sender`, `trusted_sender_domain` (and the label predicate above).
  Rule values are lists of strings; the Settings value field splits on commas
  (`settings_controller.py:356` `_parse_value_list`, called at `settings_controller.py:1822` and
  `web/routes_settings.py:2059`).
- **Scope plumbing.** `SCOPE_SELECTORS` is frozen against the old evaluator (50 predicates,
  `tests/unit/policy/test_scopes.py::TestVocabularyCompleteness`); new predicates go in
  `NEW_SCOPE_SELECTORS` (`scopes.py:618`) and are offered through `catalogue.EXTRA_SCOPES`
  (`catalogue.py:49`), as `grist.document` and `peek.page` are. Every `PROPOSABLE_SCOPES`
  predicate must be in `SCOPE_SELECTORS` (`tests/unit/policy/test_propose.py:219`).

## 3. Design

All decisions are made here. The phase briefs point to these subsections.

### D1. Archive by label (request 1)

- **Scope.** The existing `gmail.label` scope group gains the `move` verb through its existing,
  currently unproposed predicate `label_match`. In `propose.py`, right after the
  `label_name_allowlist` entry, add:
  `_scope("label_match", "gmail.label", "gmail", (Verb.MOVE,), _not_proposed, "this label")`,
  where `_not_proposed(_ctx)` returns `NO_VALUE` (a card never proposes "move on this label": the
  reviewer cannot pick which label of the message should matter). One stored row
  `gmail.label | Proposed Archive | verbs update, move` then compiles to two engine rules:
  `label_name_allowlist` for the label operations and `label_match` for
  `gmail.archive_message`. `SCOPE_SELECTORS["label_match"]` and the old-evaluator equivalence tests
  stay untouched.
- **The check runs against live labels.** `_archive_message` already fetches the message with
  `get_message` on every call. It then maps `message.labels` (ids) to names with the new
  `GmailClient.label_names(label_ids)` and passes
  `dataclasses.replace(message, labels=<names>)` as `raw_data` to `gated_call`. The agent's request
  carries only `message_id`; nothing the agent sends can name a label. An id Gmail no longer lists
  is dropped. A label lookup error fails the call (nothing is archived).
- **Only user labels count.** `GmailClient.label_names` keeps only labels whose `type` is `user`
  (`list_labels` already returns it), so a Gmail system label (`INBOX`, `UNREAD`, `STARRED`,
  `CATEGORY_*`) is never in the names a rule is compared with. A rule whose value is `INBOX` therefore
  never allows an archive, and no value format is added: update-only rules for `UNREAD` or `STARRED`
  (mark as read, star) stay writable.
- **Cost.** Every archive call makes one extra `labels.list` call, also when no `label_match` rule
  exists. Archiving is a low-volume write; accepted.
- **Not an independent check.** The same row grants `update` and `move`, so whatever the agent can
  label it can archive: the label gates the archive, but a steered agent can apply the label first.
  The safety is that archiving is reversible and leaves the message unread (the request's own
  argument). ADR 0218 says so.
- **Rejected.** Matching on the label ids (a rule would carry opaque ids and break when a label is
  recreated). Making `GmailMessage.labels` carry names (it is returned to the agent by
  `gmail_get_message`, and renaming its content changes the tool's output). A new predicate name
  (the old one already has the right semantics and an equivalence-tested selector).
- **Legacy rows.** A hand-written custom row that uses `label_match` on a read operation (the old
  evaluator's semantics) shows as allowing nothing in Settings once the predicate has a catalogue
  entry with only `move`, though the engine still honours it. No such row is written by any current
  surface; accepted.
- **Known consequence.** The add-label card's "Always allow" widening list gains "also allow move
  (1 tool)" for a label, because widenings follow the group's verbs. It is an explicit chip the
  reviewer chooses; no change.
- **`check_policy`.** For `gmail_archive_message` with a `label_match` rule configured,
  `check_policy` answers `unknown` ("Depends on the fetched item's data…") and lists
  `suggested_rules` with group `gmail.label`, verb `move`, `value: null`. Unchanged code; this is
  tested.
- **Existing tokens.** A token created before the row gained `move` does not get it (`rule_limits`).
  The rollout creates a new token after editing the rules; the manual steps say so.

### D2. Per-token personal-data allowance for reads (request 2)

- **Profile = scoped token.** No new object, no `profile` argument on
  `privacyfence_begin_unattended_session`. A routine's rules, tools, expiry and revocation are the
  token's (ADR 0164); the audit log records `scoped-token:<id>` and the token's name on every call;
  a call outside the token is denied as `denied_scoped_token` even when an install-wide rule
  exists. The routine presents the token through a Claude Code cloud environment: a network secret
  for the PrivacyFence host and `claude mcp add` in the setup script (Risk 1, verified).
- **New flag.** `ScopedToken.allow_personal_data_reads: bool = False`, stored as
  `allow_personal_data_reads: true` only when set, read with `is True` (an old file loads as false).
  It is independent of `allow_personal_data`: that flag keeps covering exports, downloads and page
  uploads only. A token that has the old flag does not gain the new one.
- **Tools.** `auto_accept.PERSONAL_DATA_READ_TOOLS` = `gmail_get_message`, `gmail_get_thread`,
  `slack_get_channel_history`, `slack_get_thread_replies`, `slack_search_messages`. These return
  content to the AI system by design; the flag is the person's decision that a covered read may
  carry personal data.
- **Gate.** `gate._personal_data_rule` returns the token's matching rule when the call is made with
  a non-revoked scoped token and either (a) the tool is in `PERSONAL_DATA_READ_TOOLS` and the token
  has `allow_personal_data_reads`, or (b) the existing condition for `PERSONAL_DATA_RULE_TOOLS`
  holds. A matching rule of the token is still required. The scan still runs: the audit entry is
  `auto_accepted` with `pii_detected: true` and the categories found, exactly as for an export.
  An unscoped call keeps the scan and its card; a call with a token that lacks the flag is denied
  (`denied_scoped_token`) as today.
- **`check_policy`.** `_pii_gate` returns `("allowed_by_token", "")` for a tool in
  `PERSONAL_DATA_READ_TOOLS` when the token has the flag, and otherwise
  `("applies", "the token does not allow personal data in reads, so the personal-data scan applies to <tool>")`.
- **Settings.** The New token form gets a second checkbox below the existing one. Label: `Let reads
  return personal data`. Help text: `Applies only to calls made with this token, and only to Gmail
  message and thread reads and Slack channel, thread and search reads that one of the token's rules
  covers. The personal-data scan still runs and the audit log records what it found, but the AI
  system receives the content. Other calls still stop for review when personal data is found.`
  Chip in the token list: `personal data in reads allowed`. The create request carries
  `allow_personal_data_reads` (boolean, default false; a non-boolean is a malformed body, as for the
  existing flag) and it is part of the step-up fingerprint. The audit summary of a created token
  appends `, personal data in reads allowed`.
- **Form fix.** The New token form hides the tools that need no rule (`auto_tools`, for example
  `gmail_list_labels`) in a collapsed disclosure titled `Tools that need no rule, from other
  connectors (N)` and, while no rule is ticked, the hint says `Select a rule to see the tools it
  covers.` A person who wants a token with no rule (a connectivity test, a read-only listing job)
  finds no tool to pick. While no rule is ticked, the disclosure is open and titled `Tools that need
  no rule (N)`, and the hint reads `Select a rule to see the tools it covers, or pick a tool that
  needs no rule.` Once a rule is ticked, the form behaves as before (covered tools per connector, the
  rest in the collapsed `from other connectors` disclosure).
- **Rejected.** Extending `PERSONAL_DATA_RULE_TOOLS` and the existing flag to reads (silently
  widens every token that already carries the flag; the flag's contract is "the result holds no
  row"). A per-rule flag (ADR 0173 moved the flag off rules). Lifting the scan for the whole token
  (exports and reads are different risks; the person can tick one or both).

### D3. Slack scopes (request 3)

- **Two extra scopes** in `catalogue.EXTRA_SCOPES`, both value-less, both connector `slack`:
  - id and predicate `slack.mentions_of_me`, verbs `(READ, SEARCH)`, label `Slack — mentions of me`;
  - id and predicate `slack.direct_messages_to_me`, verbs `(READ,)`, label `Slack — direct messages to me`.
  Their selectors are in `scopes.NEW_SCOPE_SELECTORS`, scope types equal to the predicate names
  (so a rule reads `Slack - mentions of me: allow read, search`), kind `ATTRIBUTE`, and both
  `resolves_from=FETCHED` so `check_policy` honestly answers `unknown`.
- **`slack.direct_messages_to_me`** matches when `ctx.args.get("is_one_to_one_dm") is True`. The
  Slack connector computes it for `slack_get_channel_history` and `slack_get_thread_replies` with the
  new `SlackClient.resolve_is_one_to_one_dm(channel_id)`: true only for a channel id starting with
  `D` that `conversations_info` reports as `is_im`; cached on success only; any error or unknown
  channel reads false. A DM with yourself is a 1:1 DM too.
- **`slack.mentions_of_me`** needs `ctx.args["own_user_id"]` (computed by the connector from
  `SlackClient.own_user_id()`; empty never matches) and `ctx.raw_data` (the list of
  `SlackMessage`). A message mentions the user when its text matches
  `<@{own_user_id}>` or `<@{own_user_id}|name>`. By tool:
  - `slack_search_messages`: non-empty results and **every** result mentions the user;
  - `slack_get_thread_replies`: non-empty and **at least one** message of the thread (parent or
    reply) mentions the user. Slack offers no parent-only read, so the agent receives the whole
    thread. This is the product decision recorded with the user;
  - any other tool, including `slack_get_channel_history`: never matches.
  A search page that mixes mention and non-mention results is not covered; the agent must search so
  that every result mentions the user.
- **Computed arguments.** The Slack connector adds `is_one_to_one_dm` to the history and thread
  calls and `own_user_id` to the thread and search calls. `mcp_dispatch._CONNECTOR_COMPUTED_ARGS`
  gains `is_one_to_one_dm` and `own_user_id`, so an agent cannot supply them to `check_policy`.
- **Verbs.** Both scopes govern the existing operation key `slack.read_messages`; the READ/SEARCH
  choice in the form is the same cosmetic double-verb the key already has.
- **Accepted consequence.** A thread read is covered when any message on it mentions the user, so
  anyone who can reply in a shared channel can make a thread readable by mentioning the user, and
  with the token's reads flag its personal data too. ADR 0221 records it.
- **Rule wording.** `describe._catalogue_verbs` falls back to the verbs of the `EXTRA_SCOPES` entry
  with the rule's predicate when no `PROPOSABLE_SCOPES` entry has it, so the direct-messages rule
  reads `allow read` and lists no search tool, instead of crediting every verb of
  `slack.read_messages`.
- **Rejected.** Filtering mixed search results down to the mentions (an unattended read would then
  return a partial page and hide the filtering from the audit log). A parent-only thread rule (no
  tool exists for it). Putting these predicates in `SCOPE_SELECTORS` (frozen against the old
  evaluator).

### D4. Gmail query scope (request 4)

- **Scope.** `catalogue.EXTRA_SCOPES["gmail.query"]`: predicate `gmail.query`, connector `gmail`,
  verbs `(READ, DOWNLOAD)`, label `Gmail — query`, `needs_value=True`, value hint
  `in:inbox -category:promotions -label:"Proposed SPAM"`. Operations follow from the registry:
  `gmail.read_message`, `gmail.read_thread`, `gmail.download_attachment`. Selector
  `scopes.NEW_SCOPE_SELECTORS["gmail.query"]`, scope type `gmail.query`, kind `ATTRIBUTE`,
  `resolves_from=FETCHED` (rule sentence: `Gmail - query in:inbox …: allow read`).
- **Value.** The whole value is one Gmail search expression, stored as a one-item list. The
  Settings value field does not split it on commas for this scope:
  `catalogue.SINGLE_VALUE_GROUPS = frozenset({"gmail.query"})`, and
  `settings_controller._parse_value_list(raw_text, group="")` returns `[raw_text.strip()]` (or
  `None` when blank) for a group in that set. A bridge call with a list of several strings is a rule
  with several alternative queries. `value_format.VALUE_FORMATS["gmail.query"]`: 1 to 500
  characters, no control characters (`[^\x00-\x1f\x7f]{1,500}`); what `a Gmail search query`, how
  `a Gmail search expression as you would type it into Gmail's search box, up to 500 characters`,
  example `in:inbox -category:promotions`. The text must also have balanced parentheses and an even
  number of double quotes (`value_format.normalise_rule_value` checks this for `gmail.query` and
  raises `RuleValueError("The Gmail query has unbalanced parentheses or quotes.")`), because the
  value is wrapped in parentheses when it is evaluated and `in:inbox) OR (in:anywhere` would widen
  the rule. The value is set by the policy owner, never by the agent
  (a rule change always needs a human confirmation, and is refused in an unattended session).
- **Evaluation is Gmail's own, in the connector, before gating.** The selector never calls Gmail.
  The Gmail connector computes `matching_queries`, the list of query strings among the call's
  `gmail.query` rule values that match, and passes it in `args`; the selector matches when any rule
  value is in that list.
  - `gate.rule_values(predicate, operation_key) -> list[str]` returns the deduplicated string values
    of the current call's rules (the token's own rules under a scoped token, ADR 0164) that use
    `predicate` for `operation_key`.
  - `GmailMessage.rfc822_id` (new, default `""`) is the message's `Message-ID` header without the
    angle brackets.
  - `GmailClient.messages_matching(query, messages)` takes `(gmail_id, rfc822_id)` pairs and returns
    the set of those Gmail ids that `messages.list` finds with
    `q = "(<query>) (rfc822msgid:<id1> OR rfc822msgid:<id2> …)"`, `maxResults=100`, one page,
    intersected with the pairs' Gmail ids. It returns the empty set (fail closed) for a blank query,
    no messages, more than `MAX_QUERY_MATCH_MESSAGES = 25` messages, or any id that does not match
    `\A[A-Za-z0-9._%+=-]{1,200}@[A-Za-z0-9.-]{1,255}\Z`. The strict id pattern keeps a sender-chosen
    `Message-ID` from adding Gmail search syntax, and intersecting by Gmail id keeps a duplicate
    `Message-ID` on another message from vouching for this one. An API error raises
    `GmailClientError`.
  - `GmailConnector._matching_queries(tool, messages)` (the operation key comes from
    `auto_accept.TOOL_TO_OPERATION[tool]`) evaluates at most
    `MAX_QUERY_RULES_PER_CALL = 10` query values (rule order), one `messages_matching` call each. A
    query is in the result only when the found set equals all the messages. An evaluation error is
    logged without the query text or message content and counts as no match, so the call falls back
    to the review gate instead of failing. With no query rules there is no Gmail call. The search is
    run with `asyncio.to_thread(self._gmail.messages_matching, ...)`, not through `_fetch`, because
    `_fetch` logs the exception text and a Google `HttpError` text carries the request URL with the
    query and the `Message-ID`s; `messages_matching` raises `GmailClientError` with the fixed text
    `messages_matching failed (HTTP <status>)` and the connector catches `GmailClientError` only.
  - Used by `gmail_get_message` (one message), `gmail_get_thread` (**every message of the thread must
    match**, the product decision recorded with the user) and `gmail_download_attachment` (the
    message). `args["matching_queries"]` is added to the call's `args` only when the list is not empty
    (a call with no matching query has the same `args` as before), and the name is added to
    `_CONNECTOR_COMPUTED_ARGS` so an agent cannot supply it to `check_policy`.
  - `policy/suggest.py` knows `_EXTRA_ARGUMENT` only for `grist.document`, `peek.page` and
    `apps_script.project`; `gmail.query` has no argument that names it, so `_extra_value_of` returns a
    builder that gives `NO_VALUE` for a group not in that table. `suggested_rules` then lists a
    `gmail.query` suggestion with `value: null` and `value_known: false` for an uncovered Gmail read.
- **PII.** Unchanged: a query rule is a read rule, so the scan applies unless the call is made with
  a token whose `allow_personal_data_reads` is set (D2).
- **Rejected.** Evaluating the query inside the selector (blocks the event loop with network I/O,
  and `policy/` modules do no I/O). Re-implementing Gmail search locally (never equal to Gmail's).
  Listing the query's results and looking for the message (unbounded paging). Trusting a
  caller-supplied match. "Any message of the thread matches" (releases history outside the query).
- **Usability note.** A per-message operator such as `in:inbox` never matches a message the user sent
  (sent mail lacks `INBOX`), so a thread containing the user's own reply is not covered by
  `in:inbox …`. ADR 0222 and the user documentation say so.
- **Fail closed.** A `Message-ID` Gmail cannot search by (unusual characters) never matches.
  Search-index delay after a label change can make a just-labelled message not match yet.

### D5. Documentation, ADRs, changelog

ADR numbers are the next five free numbers at the time of the last phase (`0218` to `0222` as this is
written; renumber if `main` has taken them). Each ADR follows `docs/adr/0216-*.md`'s layout.

## 4. ADRs

One decision per ADR (`docs/adr/README.md`, rule 1). Numbers are the next free ones at the time of the
last phase: `0218` to `0222` as this is written.

- `0218`, "A Gmail label rule can allow archiving a message that carries the label": D1, including
  that `check_policy` stays argument-only and answers `unknown`, that the label is not an
  independent check on a steered agent, and why labels are matched by name after a lookup.
- `0219`, "A scoped token can let email and Slack reads return personal data": the reads flag of D2;
  amends ADR 0173 (that flag stays limited to results without rows; a second flag covers reads).
- `0220`, "A routine's rule profile is a scoped token": D2's first bullet: no new profile object and
  no `profile` argument on `privacyfence_begin_unattended_session`; relates to ADR 0164 and ADR 0015.
- `0221`, "Slack rules cover mentions of the signed-in user": the mentions scope of D3 including the
  whole-thread decision and the accepted consequence. The one-to-one DM scope is an ordinary
  attribute scope and needs no ADR.
- `0222`, "A Gmail query rule is evaluated by Gmail against the message at call time": D4, including
  the every-message-of-a-thread decision, the `Message-ID` guard and the usability note.

## 5. Manual steps

No phase needs anything from the user before it starts (`manual_before` is empty): every phase is
unit-tested. One item after the last merge (`manual_after`): the rollout and first run on the
user's own install, which only a person can verify against live Gmail and Slack. It is checked once a
release containing the pull request is installed; the pull request lists it unchecked.

The step-by-step page, https://claude.ai/artifact/8Euv1y7YBbp7wZpvFcsDeo (source
`docs/inbox-triage-scopes-plan-manual-steps.html`), also has a recommended first part that is not in
the manifest because no phase depends on it: turn scoped tokens on and prove with a throwaway token
that the scheduled task can present a bearer token (Risk 1, now verified). The page records the
environment setup that worked.

- `ma1-rollout-and-first-run`: edit the rules, create the `inbox-triage` token, run the routine
  once and read the audit log.

## 6. Risks and open questions

1. **How the routine presents the token (resolved).** A Claude Code cloud routine reaches PrivacyFence
   with the token when the environment allows the host, holds the token as a network secret for
   `<host>/mcp`, and registers the server with `claude mcp add` in its setup script, and the routine
   does not also carry the claude.ai connector, which always signs in as the person. Verified with a
   throwaway token on 22 October 2026. A Cowork routine through the claude.ai connector cannot do it.
2. **`check_policy` is not the oracle the request pictured.** Rules that depend on the fetched
   message give `unknown`. A worker that finds a test or doc expecting `auto_accept` for such a rule
   must stop with `status=blocked`.
3. **Gmail `rfc822msgid:` behaviour with unusual ids and index delay.** Fail closed. Phase
   `p5a-gmail-query-core` adds a check to `scripts/qa_fixture_recorder.py` and dispatches
   `qa-record-fixture.yml`; if the live account does not return the seed message for
   `in:anywhere`, stop with `status=blocked`.
4. **Adding `move` to `gmail.label` changes expectations in existing tests** (verbs of the group,
   widenings on the add-label card, the generated "Always allow" reference). Update the expectation;
   stop if a test shows a rule gaining `move` without the row asking for it.
5. **Existing tests enumerate things the plan adds to** (verbs and widenings of the `gmail.label`
   group, `_CONNECTOR_COMPUTED_ARGS`, exact `args` dicts in the Slack and Gmail connector tests, the
   fake `store_create`). Each phase brief names the ones the review found; a worker that finds
   another updates its expectation and says so.
6. **Slack mention searches are the agent's job.** A page that mixes mention and other results is
   not covered. Nothing here tells the agent how to word the search.
7. **ADR numbers may be taken by a parallel branch.** Take the next free numbers in `p6`.
8. **Recorder change (p5a).** If `scripts/qa_fixture_recorder.py` cannot hold a check-only result
   without a fixture file, stop with `status=blocked`.

## Implementation manifest

```yaml
plan_slug: inbox-triage-scopes
feature_branch: feature/inbox-triage-scopes
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/8Euv1y7YBbp7wZpvFcsDeo
manual_steps_source: docs/inbox-triage-scopes-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-rollout-and-first-run
    title: Roll out the rules and token on your install and run the routine once
    why: Only a person can check an unattended label-then-archive, a flagged body read and the Slack and Gmail query rules against the live accounts. The recorder check covers Gmail's search syntax only.
verify_after_merge:
  - python3 -m pytest tests/unit/policy tests/unit/test_scoped_tokens.py tests/unit/test_gate.py tests/unit/test_gate_real_evaluator.py -q
  - python3 -m pytest tests/unit/connectors/test_gmail_connector.py tests/unit/connectors/test_slack_connector.py tests/unit/test_gmail_client.py tests/unit/test_slack_client.py -q
  - python3 -m pytest tests/unit/web/test_mcp_dispatch.py tests/unit/web/test_routes_settings.py tests/unit/web/test_routes_org_settings.py tests/unit/test_settings_window_html.py tests/unit/test_settings_controller.py -q
  - python3 -m pytest tests/unit/test_generate_always_allow_reference.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py -q
final_checks:
  - docs/inbox-triage-scopes-plan.md and docs/inbox-triage-scopes-plan-manual-steps.html are deleted and nothing links to them
  - Five new ADRs exist in docs/adr, are Accepted, and are listed in docs/adr/README.md (the plan's ADRs section names them)
  - CHANGELOG.md has entries under ## [Unreleased] and no new version heading
phases:
  - id: p1-gmail-label-move
    title: A Gmail label rule can allow archiving the messages that carry the label
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/policy/propose.py
      - src/privacyfence/gmail_client.py
      - src/privacyfence/connectors/gmail.py
      - docs/always-allow-rules-reference.md
      - tests/unit/policy/test_propose.py
      - tests/unit/policy/test_scope_rows.py
      - tests/unit/policy/test_suggest.py
      - tests/unit/test_gmail_client.py
      - tests/unit/test_gate_real_evaluator.py
      - tests/unit/connectors/test_gmail_connector.py
    brief: |
      Implement Design D1 of docs/inbox-triage-scopes-plan.md. Read it, ADR 0172, ADR 0074 and
      docs/coding-and-testing-guidelines.md section 2.7 first.

      1. src/privacyfence/policy/propose.py: after the helper `_sender_domain`, add
         `def _not_proposed(_ctx: ReviewContext) -> Any:` returning `NO_VALUE`, with a one-line
         docstring saying why (a card cannot pick which label of the message should matter, so this
         scope is configured deliberately and never proposed off one gated call). In
         PROPOSABLE_SCOPES, right after the `label_name_allowlist` entry in the Gmail block (line
         about 314), add
         `_scope("label_match", "gmail.label", "gmail", (Verb.MOVE,), _not_proposed, "this label"),`
         with a short comment: archiving is allowed only when the message already carries the
         label, checked against its live labels. Do not touch scopes.py: `label_match` is already
         in SCOPE_SELECTORS.
      2. Do not touch value_format.py: no format is added for `label_match` (Design D1, "Only user
         labels count"). If `tests/unit/policy/test_value_format.py` has a test asserting that every
         valued PROPOSABLE predicate has a format or is in UNVALIDATED_VALUE_PREDICATES, add
         `label_match` to UNVALIDATED_VALUE_PREDICATES in value_format.py and say so in the report.
      3. src/privacyfence/gmail_client.py: after `list_labels` add
         `def label_names(self, label_ids: Iterable[str]) -> list[str]:` that builds
         `{label["id"]: label["name"] for label in self.list_labels() if label["type"] == "user"}` and
         returns the names of the given ids in input order, dropping an id that is not a listed user
         label. Docstring: ids to user label names, one labels.list call; system labels never count.
         Add the `Iterable` import if missing.
      4. src/privacyfence/connectors/gmail.py `_archive_message`: after the `get_message` fetch add
         `label_names = await self._fetch(self._gmail.label_names, message.labels) if message.labels else []`
         and `gate_message = dataclasses.replace(message, labels=label_names)`, and pass
         `raw_data=gate_message` to `gated_call`. Add `import dataclasses` if missing. A comment
         states why: rules compare label names and Gmail returns ids, looked up on every call. Do not
         change `_add_label`, `_remove_label`, gmail_get_message's output, or the preview.
      5. Tests, with these exact names (copy the setup of the nearest existing class in each file):
         - tests/unit/policy/test_propose.py `TestGmailLabelMove`:
           `test_gmail_label_group_offers_update_and_move`, `test_move_operations_are_archive_only`,
           `test_label_match_is_never_proposed_from_a_popup` (proposals_for on gmail_archive_message
           with raw_data labels ["Proposed Archive"] yields no label proposal).
         - tests/unit/policy/test_scope_rows.py (module-level functions, the file has no classes):
           `test_gmail_label_row_with_update_and_move_compiles_to_two_rules` (predicates
           label_name_allowlist and label_match; label_match carries only gmail.archive_message),
           `test_gmail_label_row_with_only_update_has_no_label_match_rule`.
         - tests/unit/policy/test_suggest.py `TestGmailArchiveSuggestion`:
           `test_archive_suggests_a_label_rule_with_value_known_at_run_time` (group gmail.label,
           verb move, value None, value_known False).
         - tests/unit/test_gmail_client.py `TestLabelNames`:
           `test_maps_ids_to_names_in_order_and_drops_unknown_ids`,
           `test_system_labels_are_dropped` (INBOX, UNREAD, CATEGORY_PROMOTIONS never appear).
         - tests/unit/test_gate_real_evaluator.py `TestArchiveByLabel` (real rules through
           `policy_rules` or the row helpers the neighbouring classes use):
           `test_auto_accepts_when_the_message_carries_the_label`,
           `test_needs_review_when_the_message_lacks_the_label`,
           `test_label_rule_with_only_update_does_not_cover_archive`,
           `test_label_match_is_case_insensitive`, `test_a_system_label_value_never_allows_an_archive`,
           `test_preflight_is_unknown_for_a_label_rule` (gate.preflight_auto_accept for
           gmail.archive_message returns verdict "unknown").
         - tests/unit/connectors/test_gmail_connector.py `TestArchiveByLabel`:
           `test_archive_gate_receives_label_names_not_ids`,
           `test_archive_without_labels_skips_the_label_lookup`,
           `test_label_lookup_error_prevents_the_archive`.
      6. Run `python scripts/generate_always_allow_reference.py` and commit
         docs/always-allow-rules-reference.md if it changed. Update any existing test expectation
         that listed the verbs or widenings of the gmail.label group (they now include move); change
         nothing else in those tests.
      7. Do not edit user-facing docs or the changelog (p6 does), and do not dispatch
         qa-record-fixture.yml (p5a does it for gmail).
      Stop with status=blocked if: `test_propose.py` or `test_scopes.py` show `label_match` must be
      removed from SCOPE_SELECTORS or re-implemented, or if gmail.archive_message gets a verb other
      than move in the registry.
    acceptance:
      - "python3 -m pytest tests/unit/policy/test_propose.py::TestGmailLabelMove tests/unit/policy/test_scope_rows.py -k gmail_label tests/unit/policy/test_suggest.py::TestGmailArchiveSuggestion -q passes"
      - "python3 -m pytest tests/unit/test_gmail_client.py::TestLabelNames tests/unit/test_gate_real_evaluator.py::TestArchiveByLabel tests/unit/connectors/test_gmail_connector.py::TestArchiveByLabel -q passes"
      - "python3 -m pytest tests/unit/test_generate_always_allow_reference.py -q passes"
      - "grep -n label_match src/privacyfence/policy/propose.py matches one PROPOSABLE_SCOPES entry"
      - "ruff check . reports no problems and python3 -m pytest tests/unit -q passes"
      - "python3 -m pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json passes, python3 scripts/check_coverage_floor.py coverage.json passes, and the term-missing report lists no line or branch of the files this phase changed as missed"

  - id: p2-token-read-pii-core
    title: Scoped tokens can allow personal data in email and Slack reads
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/auto_accept.py
      - src/privacyfence/scoped_tokens.py
      - src/privacyfence/gate.py
      - src/privacyfence/web/mcp_dispatch.py
      - tests/unit/test_scoped_tokens.py
      - tests/unit/test_gate.py
      - tests/unit/web/test_mcp_dispatch.py
    brief: |
      Implement the server side of Design D2 of docs/inbox-triage-scopes-plan.md. Read ADR 0164,
      ADR 0173 and ADR 0216 first. The Settings page and the create routes are p3's.

      1. src/privacyfence/auto_accept.py: after PERSONAL_DATA_RULE_TOOLS add
         `PERSONAL_DATA_READ_TOOLS: frozenset[str] = frozenset({"gmail_get_message", "gmail_get_thread", "slack_get_channel_history", "slack_get_thread_replies", "slack_search_messages"})`
         with a comment: reads whose content reaches the AI system by design, which a token's
         allow_personal_data_reads flag may let through. Name no ADR number in the comment. Add the
         name to `__all__`.
      2. src/privacyfence/scoped_tokens.py: add the field
         `allow_personal_data_reads: bool = False` right after `allow_personal_data` in `ScopedToken`;
         write it in `_to_json` only when true (like the existing flag); read it in `_from_json` with
         `raw.get("allow_personal_data_reads", False) is True`; add the keyword
         `allow_personal_data_reads: bool = False` to `ScopedTokenStore.create` after
         `allow_personal_data` and pass it into the `ScopedToken(...)` it builds. `_migrated` uses
         `dataclasses.replace`, so it keeps the field; confirm with a test, change nothing there.
      3. src/privacyfence/gate.py `_personal_data_rule`: restructure as follows and update the
         docstring (import `PERSONAL_DATA_READ_TOOLS` with the other auto_accept names):
         `scope = current_token_scope()`; `if scope is None or scope is REVOKED_SCOPE: return None`;
         `if tool in PERSONAL_DATA_READ_TOOLS: allowed = scope.allow_personal_data_reads`;
         `elif tool in PERSONAL_DATA_RULE_TOOLS and not content_reaches_result: allowed = scope.allow_personal_data`;
         `else: return None`; `if not allowed: return None`; then the existing
         `_rules_in_scope(scope)` / `find_matching_rule` code unchanged. Do not change `gated_call`:
         the existing branch `if pii_forces_confirmation or upload_pii_categories:` already audits
         `auto_accepted` with `pii_detected=True` and the categories.
      4. src/privacyfence/web/mcp_dispatch.py `_pii_gate`: after the unscoped/revoked check, add
         `if tool in PERSONAL_DATA_READ_TOOLS:` returning `("allowed_by_token", "")` when
         `scope.allow_personal_data_reads`, else `("applies", f"the token does not allow personal data in reads, so the personal-data scan applies to {tool}")`.
         The remaining branches stay in their current order. Import the constant. Do not edit
         mcp_tools.py descriptions (the check_policy description is at its length limit).
      5. Tests, exact names:
         - tests/unit/test_scoped_tokens.py `TestAllowPersonalDataReads`: `test_flag_round_trips_through_the_file`,
           `test_a_file_without_the_key_loads_false`, `test_create_stores_the_flag`,
           `test_the_two_flags_are_independent`, `test_migration_keeps_the_flag`.
         - tests/unit/test_gate.py `TestPersonalDataReads`:
           `test_flagged_token_lets_personal_data_through_a_gmail_thread_read`,
           `test_flagged_token_lets_personal_data_through_a_slack_search`,
           `test_token_without_the_flag_is_denied`, `test_unscoped_call_keeps_the_scan`,
           `test_revoked_scope_keeps_the_scan`, `test_exports_flag_does_not_lift_reads`,
           `test_reads_flag_does_not_lift_exports`, `test_flagged_token_still_needs_a_matching_rule`,
           `test_audit_entry_records_pii_detected_and_the_rule` (copy `TestPersonalDataRules`).
         - tests/unit/web/test_mcp_dispatch.py `TestCheckPolicy`:
           `test_pii_gate_is_allowed_by_token_for_a_read_with_the_reads_flag`,
           `test_pii_gate_applies_for_a_read_without_the_reads_flag`,
           `test_pii_gate_for_a_read_ignores_the_exports_flag`. Also extend the existing
           tool-set drift test so it covers PERSONAL_DATA_READ_TOOLS (every tool in it is
           review-gated).
      6. No docs or changelog here (p6).
      Stop with status=blocked if a read tool in PERSONAL_DATA_READ_TOOLS is not gate "review" in
      TOOL_TO_GATE, or if gated_call does not reach `_personal_data_rule` for a review-gated read
      that finds personal data.
    acceptance:
      - "python3 -m pytest tests/unit/test_scoped_tokens.py::TestAllowPersonalDataReads tests/unit/test_gate.py::TestPersonalDataReads -q passes"
      - "python3 -m pytest tests/unit/web/test_mcp_dispatch.py -q passes"
      - "grep -n PERSONAL_DATA_READ_TOOLS src/privacyfence/auto_accept.py src/privacyfence/gate.py src/privacyfence/web/mcp_dispatch.py matches in all three files"
      - "ruff check . reports no problems and python3 scripts/mypy_strict_modules.py and python3 -m mypy src/privacyfence pass"
      - "python3 -m pytest tests/unit -q passes"
      - "python3 -m pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json passes, python3 scripts/check_coverage_floor.py coverage.json passes, and the term-missing report lists no line or branch of the files this phase changed as missed"

  - id: p3-token-read-pii-ui
    title: The New token form carries the reads flag and lists the tools that need no rule
    depends_on: [p2-token-read-pii-core]
    complexity: M
    touches:
      - src/privacyfence/web/server.py
      - src/privacyfence/web/routes_settings.py
      - src/privacyfence/settings_window_html.py
      - tests/unit/web/test_routes_settings.py
      - tests/unit/web/test_routes_org_settings.py
      - tests/unit/test_settings_window_html.py
      - tests/unit/web/test_server.py
      - tests/integration/test_settings_scoped_token_form_browser.py
    brief: |
      Implement the Settings half of Design D2 (strings are fixed there). The flag exists in
      ScopedToken after p2; this phase only carries it through the web layer by mirroring the
      existing `allow_personal_data` flag. Run `grep -n "allow_personal_data\|stPersonalData\|data-st-personal-data" <file>` on each
      file below and add the parallel `allow_personal_data_reads` / `stPersonalDataReads` /
      `data-st-personal-data-reads` for every hit, with these specifics:

      1. src/privacyfence/web/server.py: the token dict in the page data (key
         `"allow_personal_data_reads": token.allow_personal_data_reads`); the local-mode `_create`
         closure takes a sixth parameter `allow_personal_data_reads: bool` and passes it to
         `store.create`.
      2. src/privacyfence/web/routes_settings.py: `ScopedTokenWiring.store_create` type becomes
         `Callable[[str, int, list[str], list[str], bool, bool], tuple[ScopedToken, str]]`;
         `_scoped_create_fingerprint_body` reads `allow_personal_data_reads` (default False, a
         non-bool returns None like the existing flag) and returns it in the dict; the local
         create route passes `bound["allow_personal_data_reads"]` as the sixth argument; the
         organization create route passes it as the keyword to `store.create`; the audit summary
         appends `, personal data in reads allowed` when set (after the existing suffix). The local
         route's audit summary has no personal-data suffix today and gets none.
      3. src/privacyfence/settings_window_html.py: state field, the chip
         `personal data in reads allowed` in the token list, the duplicate-from-token fill, the
         reset after create, a second checkbox under the existing one with the label and help text
         from Design D2, the `create_scoped_token` post payload key `allow_personal_data_reads`,
         and the click handler for `[data-st-personal-data-reads]`.
      4. Tests, exact names:
         - tests/unit/web/test_routes_settings.py `TestScopedTokenRoutes`:
           `test_create_passes_the_reads_flag_to_the_store`,
           `test_fingerprint_covers_the_reads_flag`, `test_non_bool_reads_flag_is_rejected`.
         - tests/unit/web/test_routes_org_settings.py `TestScopedTokenRoutes`:
           `test_org_create_stores_the_reads_flag`, `test_org_audit_summary_names_the_reads_flag`.
         - tests/unit/test_settings_window_html.py `TestScopedTokensSection`:
           `test_reads_checkbox_label_and_help_are_rendered`, `test_reads_badge_is_rendered`,
           `test_create_payload_carries_the_reads_flag`.
         - tests/unit/test_settings_window_html.py `TestScopedTokensSection`:
           `test_no_rule_selected_opens_the_tools_that_need_no_rule`,
           `test_rule_selected_keeps_the_other_connectors_disclosure_closed`.
         - Existing tests that break because `store_create` takes a sixth argument: the fake
           `_create` in tests/unit/web/test_routes_settings.py (line about 1513, five parameters) and
           the recorded call tuple it asserts (about 1583), and the five-argument calls to
           `wiring.store_create` in tests/unit/web/test_server.py (about 2120, 2121, 2139). Give the
           fake the sixth parameter and pass `False` in those calls; do not give the real
           parameter a default.
         - tests/unit/web/test_server.py: extend the scoped-token wiring test so the closure
           stores the flag (`test_local_wiring_stores_the_reads_flag`).
         - tests/integration/test_settings_scoped_token_form_browser.py: if it ticks the existing
           personal-data checkbox, tick the new one the same way in a second test
           (`test_reads_checkbox_is_sent_with_the_create_request`); run it with
           PRIVACYFENCE_TEST_CHROMIUM as the session hook sets it, and report a SKIPPED result as
           skipped, not as passed.
      5. Form fix (Design D2, "Form fix"), in settings_window_html.py around the `Tools` block of the
         New token form (the code that builds `data-st-no-rule-hint` and `data-st-other-tools`):
         when `anyRule` is false, add the attribute ` open` to the `<details data-st-other-tools="1">`
         element and use the summary text `'Tools that need no rule (' + otherTools.length + ')'`; when
         `anyRule` is true, keep the current closed element and summary. Change the no-rule hint text
         to `Select a rule to see the tools it covers, or pick a tool that needs no rule.` Update the
         existing assertions that name the old summary or the closed state for the no-rule case
         (tests/unit/test_settings_window_html.py near the `data-st-other-tools` assertions, and
         tests/integration/test_settings_scoped_token_form_browser.py near lines 119-142), and keep
         their assertions for the rule-selected case.
      6. No docs or changelog (p6).
      Stop with status=blocked if the organization and local create routes do not both go through
      `_scoped_create_fingerprint_body`.
    acceptance:
      - "python3 -m pytest tests/unit/web/test_routes_settings.py::TestScopedTokenRoutes tests/unit/web/test_routes_org_settings.py::TestScopedTokenRoutes tests/unit/test_settings_window_html.py::TestScopedTokensSection tests/unit/web/test_server.py -q passes"
      - "grep -n 'Select a rule to see the tools it covers, or pick a tool that needs no rule' src/privacyfence/settings_window_html.py matches one line"
      - "grep -l allow_personal_data_reads src/privacyfence/web/server.py src/privacyfence/web/routes_settings.py src/privacyfence/settings_window_html.py lists all three files"
      - "python3 -m pytest tests/integration/test_settings_scoped_token_form_browser.py -q passes, or every test in it reports skipped with the session hook's Chromium warning stated in the report"
      - "ruff check . reports no problems and python3 -m pytest tests/unit -q passes"
      - "python3 -m pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json passes, python3 scripts/check_coverage_floor.py coverage.json passes, and the term-missing report lists no line or branch of the files this phase changed as missed"

  - id: p4-slack-scopes
    title: Slack scopes for mentions of me and for one-to-one DMs
    depends_on: [p2-token-read-pii-core]
    complexity: M
    touches:
      - src/privacyfence/slack_client.py
      - src/privacyfence/connectors/slack.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/describe.py
      - src/privacyfence/web/mcp_dispatch.py
      - tests/unit/test_slack_client.py
      - tests/unit/connectors/test_slack_connector.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_describe.py
      - tests/unit/policy/test_engine.py
      - tests/unit/web/test_mcp_dispatch.py
    brief: |
      Implement Design D3 of docs/inbox-triage-scopes-plan.md. Read ADR 0172 and the comment above
      `NEW_SCOPE_SELECTORS` in scopes.py first.

      1. src/privacyfence/slack_client.py: add `self._im_channel_cache: dict[str, bool] = {}` next to
         `_channel_is_mpim_cache` (line about 424) and, after `resolve_is_group_dm`, add
         `def resolve_is_one_to_one_dm(self, channel_id: str) -> bool:`: return False unless
         `channel_id.startswith("D")`; return the cached value if present; otherwise call
         `self._client.conversations_info(channel=channel_id)` and read
         `bool((response.get("channel") or {}).get("is_im", False))`; cache and return it; on
         `SlackApiError` log at debug (as `resolve_is_group_dm` does) and return False without caching.
         Docstring: why the id prefix alone is not enough and that it reads False when unsure.
      2. src/privacyfence/connectors/slack.py: in `_get_channel_history` add
         `is_one_to_one_dm = await self._fetch(self._slack.resolve_is_one_to_one_dm, channel_id)` and
         put `"is_one_to_one_dm": is_one_to_one_dm` in the `args` of its `gated_call`; in
         `_get_thread_replies` do the same and also add `"own_user_id": await self._fetch(self._slack.own_user_id)`;
         in `_search_messages` add only `"own_user_id"` to its `args`. Nothing else in those methods
         changes.
      3. src/privacyfence/policy/scopes.py: before `NEW_SCOPE_SELECTORS`, add
         `_one_to_one_dm_matches(_value, ctx)` returning `ctx.args.get("is_one_to_one_dm") is True`,
         and `_mentions_of_me_matches(_value, ctx)`:
         `own = ctx.args.get("own_user_id")`; return False unless `own` is a non-empty str;
         `messages = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]`; return False
         if it is empty or holds None; build
         `pattern = re.compile(r"<@" + re.escape(own) + r"(?:\|[^>]*)?>")`;
         `mentions = [bool(pattern.search(getattr(m, "text", "") or "")) for m in messages]`;
         `if ctx.tool == "slack_search_messages": return all(mentions)`;
         `if ctx.tool == "slack_get_thread_replies": return any(mentions)`; `return False`.
         Register in NEW_SCOPE_SELECTORS:
         `"slack.direct_messages_to_me": ScopeSelector(predicate="slack.direct_messages_to_me", scope_type="slack.direct_messages_to_me", kind=ScopeKind.ATTRIBUTE, resolves_from=ResolvesFrom.FETCHED, matches=_one_to_one_dm_matches)` and
         `"slack.mentions_of_me"` likewise with `_mentions_of_me_matches`. Add `import re` if missing.
         Extend the comment above NEW_SCOPE_SELECTORS with one sentence saying these two read
         connector-computed arguments and so resolve from the fetched item.
      4. src/privacyfence/policy/catalogue.py: add to EXTRA_SCOPES
         `"slack.mentions_of_me": PolicyExtraScope(predicate="slack.mentions_of_me", connector="slack", verbs=(Verb.READ, Verb.SEARCH), label="Slack — mentions of me", needs_value=False, value_hint="")` and
         `"slack.direct_messages_to_me": PolicyExtraScope(predicate="slack.direct_messages_to_me", connector="slack", verbs=(Verb.READ,), label="Slack — direct messages to me", needs_value=False, value_hint="")`.
         Update the module docstring's list of extra scopes by one clause.
      5. src/privacyfence/web/mcp_dispatch.py: `_CONNECTOR_COMPUTED_ARGS` becomes
         `frozenset({"is_self_dm", "is_group_dm", "is_one_to_one_dm", "own_user_id"})`.
         src/privacyfence/policy/describe.py `_catalogue_verbs`: when no PROPOSABLE_SCOPES entry has
         the predicate, return the union of `extra.verbs` of the `catalogue.EXTRA_SCOPES` entries
         whose `predicate` equals it (import `catalogue` inside the function; catalogue imports
         describe), else the empty set as today (Design D3, "Rule wording"). If an existing test in
         tests/unit/policy/test_describe.py or test_catalogue.py changes meaning for an older extra
         scope (Grist, Peek, Apps Script, Gmail filters, Slack group chats), stop with status=blocked.
      5b. Existing Slack connector tests break: `make_connector` in
         tests/unit/connectors/test_slack_connector.py (line about 36-50) builds the Slack client from
         a bare MagicMock, so the two new calls return MagicMocks that land in `args`. Set
         `client.resolve_is_one_to_one_dm.return_value = False` and `client.own_user_id.return_value = ""`
         there, and update the exact `args` assertions for history, thread replies and search (lines
         about 303, 574, 638, 657, 706, 718) to include the new keys.
      6. Tests, exact names:
         - tests/unit/test_slack_client.py `TestResolveIsOneToOneDm`: `test_im_channel_is_true`,
           `test_channel_and_group_dm_are_false`, `test_non_d_id_makes_no_api_call`,
           `test_lookup_error_is_false_and_not_cached`, `test_success_is_cached`.
         - tests/unit/policy/test_scopes.py `TestSlackMentionsOfMe`:
           `test_search_needs_every_result_to_mention_me`, `test_thread_replies_need_one_mention`,
           `test_channel_history_never_matches`, `test_no_own_user_id_never_matches`,
           `test_empty_results_never_match`, `test_mention_with_name_suffix_matches`,
           `test_another_users_mention_does_not_match`; `TestSlackDirectMessagesToMe`:
           `test_matches_only_when_the_connector_says_so`, `test_a_truthy_non_bool_does_not_match`.
         - tests/unit/policy/test_catalogue.py `TestSlackExtras`:
           `test_mentions_scope_offers_read_and_search`, `test_direct_messages_scope_offers_read`,
           `test_rules_compile_to_the_read_messages_operation`, `test_neither_scope_takes_a_value`.
         - tests/unit/policy/test_engine.py `TestSlackScopes`:
           `test_preflight_is_unknown_for_a_mentions_rule`,
           `test_a_search_rule_auto_accepts_through_evaluate`.
         - tests/unit/connectors/test_slack_connector.py `TestComputedScopeArgs`:
           `test_channel_history_args_carry_is_one_to_one_dm`,
           `test_thread_replies_args_carry_both_values`, `test_search_args_carry_own_user_id`.
         - tests/unit/web/test_mcp_dispatch.py `TestCheckPolicy`:
           `test_connector_computed_slack_args_are_not_passed_to_preflight` (monkeypatch
           `mcp_dispatch.preflight_auto_accept` to record its `args` argument; an agent that sends
           is_one_to_one_dm and own_user_id gets a preflight call without them).
         - tests/unit/policy/test_describe.py `TestExtraScopeVerbs`:
           `test_direct_messages_rule_reads_allow_read_and_lists_no_search_tool`,
           `test_mentions_rule_lists_read_and_search`.
      7. This phase changes slack_client.py, so after pushing the phase branch dispatch
         `qa-record-fixture.yml` on `main` with inputs `connector=slack` and `branch=<the phase
         branch>` (ci.dispatchable in .claude/toolkit.yaml), wait for it, `git pull`, and read any
         fixture diff before committing it: no account identifier, token or private content may
         enter the repository. Report the run URL.
      Stop with status=blocked if `conversations_info` is mocked in neighbouring tests in a way that
      cannot express `is_im`, or if the dispatched run fails for a reason other than a queued run.
    acceptance:
      - "python3 -m pytest tests/unit/test_slack_client.py::TestResolveIsOneToOneDm tests/unit/policy/test_scopes.py::TestSlackMentionsOfMe tests/unit/policy/test_scopes.py::TestSlackDirectMessagesToMe tests/unit/policy/test_catalogue.py::TestSlackExtras tests/unit/policy/test_engine.py::TestSlackScopes -q passes"
      - "python3 -m pytest tests/unit/connectors/test_slack_connector.py tests/unit/policy/test_describe.py::TestExtraScopeVerbs tests/unit/web/test_mcp_dispatch.py -q passes"
      - "grep -n is_one_to_one_dm src/privacyfence/slack_client.py src/privacyfence/connectors/slack.py src/privacyfence/policy/scopes.py src/privacyfence/web/mcp_dispatch.py matches in all four files"
      - "the qa-record-fixture.yml run for connector slack on the phase branch concluded success (URL in the report)"
      - "ruff check . reports no problems and python3 -m pytest tests/unit -q passes"
      - "python3 -m pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json passes, python3 scripts/check_coverage_floor.py coverage.json passes, and the term-missing report lists no line or branch of the files this phase changed as missed"

  - id: p5c-sync-personal-data-note
    title: The privacyfence-sync documentation and preflight note describe the reads flag
    depends_on: [p2-token-read-pii-core]
    complexity: S
    touches:
      - sync/README.md
      - plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md
      - sync/src/privacyfence_sync/preflight.py
      - sync/tests/test_preflight.py
    brief: |
      The token flag of Design D2 makes three statements in the sync package false: that only exports,
      downloads and page uploads can be allowed to carry personal data, and that other reads are
      always scanned. Correct them.

      1. sync/README.md, the "Personal data" note (search `grep -n "Personal data" sync/README.md`,
         around line 124-142) and the preflight paragraph (around lines 715-717): keep the existing
         **Let exports, downloads and page uploads carry personal data** text, add a sentence and a
         list for the second checkbox **Let reads return personal data**, which covers
         `gmail_get_message`, `gmail_get_thread`, `slack_get_channel_history`,
         `slack_get_thread_replies` and `slack_search_messages` when a rule of the token covers the
         call, and replace the claim that other reads are always scanned by one that names only reads
         outside both lists. Say that the scan still runs and the audit log records what it found.
      2. Copy sync/README.md over
         plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md (`cp`); the
         sync dod_conditional row requires the copy to be identical.
      3. sync/src/privacyfence_sync/preflight.py `_PII_NOTE`: the text becomes
         `note: a WARNING step is denied only when what it returns holds personal data; a scoped token can allow that for exports, downloads and page uploads, and for Gmail and Slack reads (see the README)`.
         Update `NOTE` (and any assertion that quotes the old text) in sync/tests/test_preflight.py.
      4. Run, from the repository root: `pip install -e "./sync[test]"`, then
         `pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-report=term-missing --cov-fail-under=100`,
         `mypy sync/src` and `bandit -r sync/src` (the sync row of dod_conditional in
         .claude/toolkit.yaml).
      Stop with status=blocked if a test in tests/unit/test_sync_plugin.py objects to the README copy.
    acceptance:
      - "pytest sync/tests -q --cov=privacyfence_sync --cov-branch --cov-report=term-missing --cov-fail-under=100 passes"
      - "mypy sync/src and bandit -r sync/src pass"
      - "cmp sync/README.md plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md exits 0"
      - "grep -c 'Let reads return personal data' sync/README.md prints 1 or more"
      - "python3 -m pytest tests/unit/test_sync_plugin.py -q passes"

  - id: p5a-gmail-query-core
    title: Gmail query scope - client search, selector, catalogue entry and live check
    depends_on: [p1-gmail-label-move, p4-slack-scopes]
    complexity: M
    touches:
      - src/privacyfence/gmail_client.py
      - src/privacyfence/policy/scopes.py
      - src/privacyfence/policy/catalogue.py
      - src/privacyfence/policy/value_format.py
      - src/privacyfence/policy/suggest.py
      - scripts/qa_fixture_recorder.py
      - tests/unit/test_gmail_client.py
      - tests/unit/policy/test_suggest.py
      - tests/unit/policy/test_scopes.py
      - tests/unit/policy/test_catalogue.py
      - tests/unit/policy/test_value_format.py
      - tests/unit/test_qa_fixture_recorder.py
    brief: |
      Implement the first half of Design D4 of docs/inbox-triage-scopes-plan.md: everything except
      the connector wiring, `gate.rule_values` and the Settings value parsing (p5b).

      1. src/privacyfence/gmail_client.py: add the field `rfc822_id: str = ""` to `GmailMessage`
         (after `labels`) and set it in `_parse_message` from `headers.get("message-id", "")` with
         surrounding whitespace and one pair of angle brackets removed. Add module constants
         `MAX_QUERY_MATCH_MESSAGES = 25` and
         `_RFC822_ID_RE = re.compile(r"\A[A-Za-z0-9._%+=-]{1,200}@[A-Za-z0-9.-]{1,255}\Z")`, and
         `def messages_matching(self, query: str, messages: Sequence[tuple[str, str]]) -> set[str]:`
         exactly as Design D4 specifies: blank query, no messages, more than
         MAX_QUERY_MATCH_MESSAGES, or any id failing the pattern returns `set()`; otherwise one
         `service.users().messages().list(userId="me", q=f"({query}) ({terms})", maxResults=100)`
         with `terms = " OR ".join(f"rfc822msgid:{rfc822_id}" ...)`; return the response's message
         ids intersected with the pairs' Gmail ids; `HttpError` becomes `GmailClientError` with the
         the fixed text `messages_matching failed (HTTP <status>)` (status from `exc.status_code`, empty
         when absent) and never the exception text, which carries the request URL with the query and
         the Message-IDs. Never log the query.
      2. src/privacyfence/policy/scopes.py: before `NEW_SCOPE_SELECTORS` add
         `_gmail_query_matches(value, ctx)`: False if `not value`; `matched = ctx.args.get("matching_queries")`;
         False unless it is a list; else `any(q in matched for q in _values_of(value))`. Register
         `"gmail.query": ScopeSelector(predicate="gmail.query", scope_type="gmail.query", kind=ScopeKind.ATTRIBUTE, resolves_from=ResolvesFrom.FETCHED, matches=_gmail_query_matches)`
         in NEW_SCOPE_SELECTORS with a comment: the connector evaluates the query with Gmail itself
         and passes the matching rule values.
      3. src/privacyfence/policy/catalogue.py: add to EXTRA_SCOPES
         `"gmail.query": PolicyExtraScope(predicate="gmail.query", connector="gmail", verbs=(Verb.READ, Verb.DOWNLOAD), label="Gmail — query", needs_value=True, value_hint='in:inbox -category:promotions -label:"Proposed SPAM"')`
         and define `SINGLE_VALUE_GROUPS: frozenset[str] = frozenset({"gmail.query"})` with a comment
         (the value is one search expression; commas belong to it). Export it in `__all__`.
      4. src/privacyfence/policy/value_format.py: add
         `"gmail.query": _fmt(r"[^\x00-\x1f\x7f]{1,500}", "a Gmail search query", "a Gmail search expression as you would type it into Gmail's search box, up to 500 characters", "in:inbox -category:promotions")`
         to VALUE_FORMATS. `catalogue._compile_catalogue_rules` passes `{extra.predicate}` to
         `normalise_rule_value`, so the predicate key is what is looked up. In
         `normalise_rule_value`, after the format check of each item, when `"gmail.query"` is in
         `predicates` and the item has unbalanced parentheses (a `)` before its `(`, or a different
         count) or an odd number of `"` characters, raise
         `RuleValueError("The Gmail query has unbalanced parentheses or quotes.")`. Put the check in a
         small private function `_balanced(text)`.
      4b. src/privacyfence/policy/suggest.py: `_extra_value_of(group)` looks `group` up in
         `_EXTRA_ARGUMENT`, which has no `gmail.query`, and `suggested_rules` calls it outside its
         `try`, so every uncovered Gmail read would raise `KeyError`. Change `_extra_value_of` to use
         `_EXTRA_ARGUMENT.get(group)` and, when it is `None`, return a builder that returns
         `propose.NO_VALUE`. A `gmail.query` suggestion is then listed with `value` None and
         `value_known` False (its selector resolves from the fetched item).
      5. scripts/qa_fixture_recorder.py `check_gmail`: after the existing `get_message` check, when
         that check fetched the seed message and `message.rfc822_id` is non-empty, add a check-only
         result `CheckResult("gmail", "messages_matching", seed_message_id, ok, note)` where
         `ok` is true when `client.messages_matching("in:anywhere", [(seed_message_id, message.rfc822_id)])`
         equals `{seed_message_id}` and
         `client.messages_matching("label:privacyfence-qa-no-such-label", [...])` is empty; a missing
         id gives `ok=False` with the note `seed message has no Message-ID header`; a
         `GmailClientError` gives `ok=False` with its fixed text (it carries no URL). Read how the neighbouring results with no
         fixture file are handled before writing it.
      6. Tests, exact names:
         - tests/unit/test_gmail_client.py `TestMessagesMatching`:
           `test_returns_only_our_ids_that_the_search_found`,
           `test_the_query_is_wrapped_and_joined_with_msgid_terms`,
           `test_a_message_id_with_search_syntax_fails_closed`,
           `test_more_than_25_messages_fails_closed`, `test_blank_query_fails_closed`,
           `test_no_messages_fails_closed`, `test_a_found_id_of_another_message_is_ignored`,
           `test_api_error_raises_gmail_client_error`; `TestRfc822Id`:
           `test_parse_message_strips_angle_brackets`, `test_missing_header_is_empty`.
         - tests/unit/policy/test_scopes.py `TestGmailQuery`:
           `test_matches_when_the_rule_value_was_evaluated_as_matching`,
           `test_no_computed_argument_never_matches`, `test_any_of_several_rule_values_matches`,
           `test_a_blank_value_never_matches`.
         - tests/unit/policy/test_catalogue.py `TestGmailQueryExtra`: `test_offers_read_and_download`,
           `test_operations_are_the_three_read_family_keys`, `test_value_is_kept_whole_in_the_rule`,
           `test_single_value_groups_names_gmail_query`.
         - tests/unit/policy/test_value_format.py `TestGmailQueryValue`:
           `test_accepts_a_normal_query_with_commas_and_quotes`, `test_refuses_control_characters`,
           `test_refuses_a_query_over_500_characters`, `test_refuses_unbalanced_parentheses`
           (parametrize `in:inbox) OR (in:anywhere`, `(in:inbox`), `test_refuses_an_odd_number_of_quotes`.
         - tests/unit/policy/test_suggest.py `TestGmailQuerySuggestion`:
           `test_an_uncovered_gmail_read_suggests_the_query_scope_with_no_known_value` and
           `test_a_gmail_read_no_longer_raises_for_the_query_scope`; and the existing
           tests/unit/web/test_mcp_dispatch.py check_policy test for gmail_get_message must still
           pass (run it, do not edit it).
         - tests/unit/test_qa_fixture_recorder.py: one test for the new result
           (`test_check_gmail_reports_messages_matching`), following the file's existing fake-client
           pattern.
      7. This phase changes gmail_client.py (and p1 did earlier): after pushing the phase branch
         dispatch `qa-record-fixture.yml` on `main` with `connector=gmail` and `branch=<the phase
         branch>`, wait, `git pull`, read the fixture diff (no account identifier, token or private
         content may enter the repository) and report the run URL and the `messages_matching` line
         of its output.
      Stop with status=blocked if the dispatched run reports `messages_matching` as failed (the live
      account does not return the seed message for `in:anywhere`), or if a check-only result cannot
      be expressed in the recorder.
    acceptance:
      - "python3 -m pytest tests/unit/test_gmail_client.py::TestMessagesMatching tests/unit/test_gmail_client.py::TestRfc822Id tests/unit/policy/test_scopes.py::TestGmailQuery tests/unit/policy/test_catalogue.py::TestGmailQueryExtra tests/unit/policy/test_value_format.py::TestGmailQueryValue tests/unit/policy/test_suggest.py::TestGmailQuerySuggestion tests/unit/test_qa_fixture_recorder.py tests/unit/web/test_mcp_dispatch.py -q passes"
      - "grep -n SINGLE_VALUE_GROUPS src/privacyfence/policy/catalogue.py matches the definition and the __all__ entry"
      - "the qa-record-fixture.yml run for connector gmail on the phase branch concluded success and its output lists messages_matching as ok (URL in the report)"
      - "ruff check . reports no problems and python3 -m pytest tests/unit -q passes"
      - "python3 -m pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json passes, python3 scripts/check_coverage_floor.py coverage.json passes, and the term-missing report lists no line or branch of the files this phase changed as missed"

  - id: p5b-gmail-query-wiring
    title: Gmail query scope - connector evaluation, rule values and Settings value parsing
    depends_on: [p5a-gmail-query-core, p3-token-read-pii-ui, p2-token-read-pii-core]
    complexity: M
    touches:
      - src/privacyfence/gate.py
      - src/privacyfence/connectors/gmail.py
      - src/privacyfence/web/mcp_dispatch.py
      - src/privacyfence/settings_controller.py
      - src/privacyfence/web/routes_settings.py
      - tests/unit/test_gate.py
      - tests/unit/test_gate_real_evaluator.py
      - tests/unit/connectors/test_gmail_connector.py
      - tests/unit/web/test_mcp_dispatch.py
      - tests/unit/test_settings_controller.py
      - tests/unit/web/test_routes_org_settings.py
    brief: |
      Implement the second half of Design D4 of docs/inbox-triage-scopes-plan.md.

      1. src/privacyfence/gate.py: add (near `_rules_in_scope`) the public function
         `def rule_values(predicate: str, operation_key: str) -> list[str]:` iterating
         `_rules_in_scope(current_token_scope())`, keeping rules whose `predicate` equals the argument
         and whose `operations` contain `operation_key`, and collecting each string value (a rule value
         is a list or a single value) once, in rule order, skipping empty and non-string items.
         Docstring: what a connector uses it for (computing, before gating, what a rule's selector
         compares against) and that a scoped token sees only its own rules. Add it to `__all__` if
         the module has one.
      2. src/privacyfence/connectors/gmail.py: add module constant `MAX_QUERY_RULES_PER_CALL = 10`
         and `async def _matching_queries(self, tool: str, messages: list[GmailMessage]) -> list[str]`
         as in Design D4: `operation_key = TOOL_TO_OPERATION[tool]`;
         `queries = rule_values("gmail.query", operation_key)[:MAX_QUERY_RULES_PER_CALL]`; return `[]`
         when there are no queries or no messages; for each query call
         `await asyncio.to_thread(self._gmail.messages_matching, query, pairs)` with
         `pairs = [(m.id, m.rfc822_id) for m in messages]`, not through `_fetch` (which logs the
         exception text at ERROR); catch `GmailClientError` only, log a warning that names the tool
         and says the query could not be evaluated, with no query text, no exception text and no
         message content, and treat it as no match; a query counts as matching only when the returned set equals `{m.id for m in messages}`.
         Use it in `_get_message` (messages `[message]`), `_get_thread` (`thread.messages`) and
         `_download_attachment` (`[message]`), each adding `"matching_queries": <result>` to the
         `args` of its `gated_call` only when the result is not empty (existing assertions of
         `args == {"message_id": ...}` at tests/unit/connectors/test_gmail_connector.py lines about
         212, 428, 710 then stay as they are). Import `rule_values` from `..gate` and `TOOL_TO_OPERATION` from
         `..auto_accept` the way the module imports `gated_call`.
      3. src/privacyfence/web/mcp_dispatch.py: add `"matching_queries"` to `_CONNECTOR_COMPUTED_ARGS`
         (it then holds five names).
      4. src/privacyfence/settings_controller.py: change `_parse_value_list` to
         `def _parse_value_list(raw_text: str, group: str = "") -> list[str] | None:` returning
         `[raw_text.strip()]` (or `None` when blank) when `group in catalogue.SINGLE_VALUE_GROUPS`
         (import the way the module already imports the catalogue), else the current comma split.
         Update its docstring in one sentence. Pass `group` at the call in `add_policy_rule`
         (line about 1822) and at the call in src/privacyfence/web/routes_settings.py (line about
         2059, read the surrounding handler for the group variable).
      5. Tests, exact names:
         - tests/unit/test_gate.py `TestRuleValues`: `test_returns_values_of_matching_predicate_and_operation`,
           `test_deduplicates_and_keeps_rule_order`, `test_under_a_scoped_token_only_the_tokens_rules`,
           `test_non_string_and_empty_values_are_ignored`, `test_other_operations_are_ignored`.
         - tests/unit/connectors/test_gmail_connector.py `TestGmailQueryArgs`:
           `test_get_message_args_carry_matching_queries`,
           `test_get_thread_needs_every_message_to_match`,
           `test_download_attachment_args_carry_matching_queries`,
           `test_no_query_rules_makes_no_search_call`,
           `test_a_search_failure_is_a_non_match_and_the_call_goes_on`,
           `test_at_most_ten_query_rules_are_evaluated`.
         - tests/unit/test_gate_real_evaluator.py `TestGmailQueryRule` (real rules, args shaped as the
           connector builds them): `test_thread_read_auto_accepts_when_the_query_matched`,
           `test_thread_read_needs_review_when_it_did_not`,
           `test_a_query_rule_does_not_cover_other_operations`,
           `test_preflight_is_unknown_for_a_query_rule`.
         - tests/unit/web/test_mcp_dispatch.py `TestCheckPolicy`:
           `test_matching_queries_is_not_passed_to_preflight` (monkeypatch
           `mcp_dispatch.preflight_auto_accept` to record its `args`; an agent-supplied
           matching_queries is gone).
         - tests/unit/test_settings_controller.py `TestAddPolicyRule`:
           `test_gmail_query_value_is_not_split_on_commas`, `test_other_scopes_still_split_on_commas`.
         - tests/unit/web/test_routes_org_settings.py: `test_add_policy_rule_keeps_a_gmail_query_whole`
           (the organization action at routes_settings.py line about 2059, inside
           `_apply_org_action`; the local route goes through the controller, covered above).
      6. This phase changes connectors/gmail.py after p5a's dispatch, so dispatch
         `qa-record-fixture.yml` on `main` again with `connector=gmail` and `branch=<the phase
         branch>`, wait, `git pull`, read any fixture diff (no account identifier, token or private
         content), and report the run URL.
      7. No docs or changelog (p6).
      Stop with status=blocked if `TOOL_TO_OPERATION` has no entry for one of the three tools, or if
      `asyncio.to_thread` cannot be used in the connector's existing call style.
    acceptance:
      - "python3 -m pytest tests/unit/test_gate.py::TestRuleValues tests/unit/connectors/test_gmail_connector.py::TestGmailQueryArgs tests/unit/test_gate_real_evaluator.py::TestGmailQueryRule -q passes"
      - "python3 -m pytest tests/unit/web/test_mcp_dispatch.py tests/unit/test_settings_controller.py tests/unit/web/test_routes_org_settings.py -q passes"
      - "grep -n matching_queries src/privacyfence/connectors/gmail.py src/privacyfence/web/mcp_dispatch.py src/privacyfence/policy/scopes.py matches in all three files"
      - "ruff check . reports no problems and python3 -m mypy src/privacyfence and python3 scripts/mypy_strict_modules.py pass"
      - "the qa-record-fixture.yml run for connector gmail on the phase branch concluded success (URL in the report)"
      - "python3 -m pytest tests/unit -q passes"
      - "python3 -m pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json passes, python3 scripts/check_coverage_floor.py coverage.json passes, and the term-missing report lists no line or branch of the files this phase changed as missed"

  - id: p6-retire
    title: ADRs, user documentation, changelog, and retire the plan
    depends_on: [p1-gmail-label-move, p2-token-read-pii-core, p3-token-read-pii-ui, p4-slack-scopes, p5a-gmail-query-core, p5b-gmail-query-wiring, p5c-sync-personal-data-note]
    complexity: M
    touches:
      - docs/adr/README.md
      - docs/adr/0218-a-gmail-label-rule-can-allow-archiving-a-message-that-carries-the-label.md
      - docs/adr/0219-a-scoped-token-can-let-email-and-slack-reads-return-personal-data.md
      - docs/adr/0220-a-routines-rule-profile-is-a-scoped-token.md
      - docs/adr/0221-slack-rules-cover-mentions-of-the-signed-in-user.md
      - docs/adr/0222-a-gmail-query-rule-is-evaluated-by-gmail-against-the-message-at-call-time.md
      - docs/adr/0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md
      - docs/approvals-and-policy.md
      - docs/security-and-compliance.md
      - docs/connect-claude-code.md
      - docs/how-it-works.md
      - CHANGELOG.md
      - docs/inbox-triage-scopes-plan.md
      - docs/inbox-triage-scopes-plan-manual-steps.html
    brief: |
      Retire the plan. Read docs/adr/README.md (rules 1 to 7) and docs/adr/0216-*.md for the layout
      first. Write present-tense documentation: no "new", "now", "previously" (tests
      test_docs_no_history.py and test_code_no_history.py enforce it).

      1. ADRs. Take the next five free numbers in docs/adr (`0218` to `0222` when this plan was
         written; use the next free ones and rename the files in `touches` if main has taken them).
         Each has Status (`Accepted — <today's date>. Implemented.`, plus an `Amends`/`Relates to`
         line), Context, Decision, Alternatives considered, Consequences, Verification (the test
         classes from the phases) and Related. The content comes from the Design subsections of
         docs/inbox-triage-scopes-plan.md and the section 4 bullets; do not link the plan document.
         - `a-gmail-label-rule-can-allow-archiving-a-message-that-carries-the-label` (D1; relates to
           ADR 0172, ADR 0204; records that check_policy stays argument-only and answers unknown).
         - `a-scoped-token-can-let-email-and-slack-reads-return-personal-data` (D2; amends ADR 0173
           and relates to ADR 0164, ADR 0216; records that a routine profile is a scoped token).
         - `a-routines-rule-profile-is-a-scoped-token` (D2's first bullet; relates to ADR 0164 and
           ADR 0015).
         - `slack-rules-cover-mentions-of-the-signed-in-user` (D3's mentions scope, with the
           whole-thread decision and the accepted consequence).
         - `a-gmail-query-rule-is-evaluated-by-gmail-against-the-message-at-call-time` (D4, with the
           usability note).
         The label ADR also records that the label is not an independent check on a steered agent.
         Add the five rows to the index table in docs/adr/README.md in the same format. Add an
         `Amended by` line to ADR 0173's Status (the only edit allowed to an accepted ADR).
      2. docs/approvals-and-policy.md: in the scope table, replace the "Gmail label" row's last
         cell with `the message carries one of the labels, compared by name with its live labels (this lets a rule allow archiving); the label being applied or removed is one of these`;
         add rows `Gmail query | attribute | one Gmail search expression | gmail.query | Gmail's own search, run against the message when it is read, finds it; for a thread, every message matches`,
         `Slack mentions of me | attribute | — | slack.mentions_of_me | a search whose every result @-mentions you, or a thread read in which at least one message does`,
         `Slack direct messages to me | attribute | — | slack.direct_messages_to_me | the channel is a one-to-one DM`. In "Letting personal data
         through" add a paragraph "Personal data in reads" describing the second checkbox, its tool
         list (PERSONAL_DATA_READ_TOOLS) and that the scan still runs and is audited. Add one
         sentence that a rule whose match depends on the fetched message gives `unknown` from
         `privacyfence_check_policy`.
      3. docs/security-and-compliance.md "Scoped tokens": a bullet "Personal data in reads" next to
         the existing personal-data text, and a bullet "One token per routine" saying a token keeps
         the operations its rules had when it was created, so edit a rule's verbs first and then
         create the token.
      4. docs/connect-claude-code.md: in both "Scoped tokens" sections add the same two sentences
         (second checkbox; edit rules before creating the token). docs/how-it-works.md "Scoped tokens"
         paragraph: one sentence on the reads flag.
      5. CHANGELOG.md under `## [Unreleased]` (add `### Added`): four entries, bold lead sentence in
         the style of 6.7.0's: a Gmail label rule can allow archiving; scoped tokens can let email and
         Slack reads return personal data; Slack rules for mentions of you and one-to-one DMs; a Gmail
         query rule. Add a `### Fixed` entry: the New token form shows the tools that need no rule
         when no rule is selected, so a token without rules can be created. Do not open a version
         heading.
      6. Delete docs/inbox-triage-scopes-plan.md and docs/inbox-triage-scopes-plan-manual-steps.html
         with `git rm`, and check nothing links to either (`grep -rn inbox-triage-scopes .` returns
         nothing).
      6b. The user documentation in step 2 to 4 also says, in the Gmail query row and the connect
         page, that a per-message operator such as `in:inbox` does not match a message the user sent.
      7. Run /dod (the definition-of-done gate from .claude/toolkit.yaml) and report its table.
      Stop with status=blocked if an ADR number you need is taken by a branch you cannot see (say
      which) or if a documentation test objects to a link you cannot fix.
    acceptance:
      - "python3 -m pytest tests/unit/test_generate_always_allow_reference.py tests/unit/test_docs_references_exist.py tests/unit/test_docs_links.py tests/unit/test_docs_no_history.py tests/unit/test_code_no_history.py tests/unit/test_changelog_section.py -q passes"
      - "ls docs/adr shows five new ADR files numbered consecutively after 0217 (or after the highest number on main) and grep -c for each file name in docs/adr/README.md is 1"
      - "grep -rn inbox-triage-scopes . --exclude-dir=.git returns no match"
      - "grep -n '^## \\[' CHANGELOG.md | head -2 shows ## [Unreleased] followed by the 6.7.0 heading"
      - "/dod reports every row passed"
```
