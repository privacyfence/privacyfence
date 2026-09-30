# Plan: Atlassian user names and mentions

## Goal

Jira and Confluence return people as opaque Atlassian account ids (`557058:f58131cb-…`,
`5b10ac8d82e05b22cc7d4ef5`), not names. Today PrivacyFence drops a Jira `@mention` from issue
descriptions and comments entirely. A Confluence page returns its author as a raw `authorId`, and
its mentions as `<ri:user ri:account-id="…"/>` markup that the approval preview drops. The agent
also has no way to find a person's account id, so it cannot mention or assign anyone.

After this change:

- **Reads.** Jira descriptions and comments show every mention as `@[Jane Doe](<accountId>)`. A
  Confluence page gains an `author_name` and a `mentions` map from account id to name, and its
  approval card names the author. The approval preview shows `@Jane Doe`.
- **Cache.** Names come from a lazy account-id cache that Jira and Confluence share, modelled on
  Slack's user cache. Only the ids a read actually contains are looked up, 100 per call through
  Jira's user bulk API. They are kept on disk for 7 days. An id that cannot be resolved is not
  retried for an hour, so a read never turns into a stream of lookups.
- **New tools.** `jira_find_users` / `confluence_find_users` return account ids for a name or
  email. `jira_refresh_user_cache` / `confluence_refresh_user_cache` re-fetch the cached names.
- **Writes.** The agent can write `@[Name](<accountId>)` in a Jira comment or description, or in a
  Confluence page body, and it becomes a real mention. It can also assign a Jira issue by
  `assignee_account_id`. The approver sees the name Atlassian holds for each id, never the label
  the agent typed. A mention or assignee whose id cannot be resolved is refused before the
  approval card is shown.

There is no tracking issue. The request came in through `/make-plan`.

## Current state

- **Jira ADF text extraction.** `src/privacyfence/jira_client.py:448` `_extract_adf_text` keeps
  only `text` nodes. An ADF mention node (`{"type": "mention", "attrs": {"id": …, "text": "@…"}}`)
  has no `text` key and no `content`, so it yields `""`. Every mention silently vanishes from
  `description` (`_parse_issue`, `jira_client.py:407`) and from comment bodies (`_parse_comment`,
  `jira_client.py:433`).
- **Jira writes.** `jira_client.py:43` `_text_to_adf` wraps plain text in one paragraph with one
  text node, so a write cannot carry a mention. `create_issue` (`jira_client.py:260`) already takes
  `assignee_account_id`, but the `jira_create_issue` tool (`connectors/jira.py:84`) does not expose
  it, and `jira_update_issue` has no assignee at all.
- **Confluence authors.** `confluence_client.py:564` `_parse_page_v2` stores the raw `authorId` in
  `ConfluencePage.author`. Its docstring says resolving it "would need a separate Users API call
  per page". `connectors/confluence.py:325` shows that id as "Author" on the approval card.
- **Confluence mentions.** Page bodies are storage format (`get_page`, `confluence_client.py:372`).
  A mention there is `<ac:link><ri:user ri:account-id="…" /></ac:link>`. `html_to_text.py`'s
  `_HTMLToMarkdownParser` has no case for `ri:user`, so the preview built at
  `connectors/confluence.py:328` drops it. The agent gets the raw storage body with no name.
- **Scopes.** `atlassian_oauth.py:40` `DEFAULT_SCOPES` includes classic `read:jira-user`, which is
  enough for Jira's `GET /rest/api/3/user/bulk` and `GET /rest/api/3/user/search`. There is no
  Confluence user scope. Atlassian account ids are site-wide, so Jira's API resolves the ids that
  Confluence returns.
- **Shared token.** `daemon_main.py:1455-1501` builds `JiraClient` and `ConfluenceClient` from the
  same `atlassian_config` (one OAuth grant, one `cloud_id`). Neither takes a cache.
- **The model to copy.** `slack_client.py` has a disk-persisted user cache
  (`user_cache_file`, `slack_client.py:394`), a negative-lookup TTL of 1 hour
  (`_NEGATIVE_LOOKUP_TTL`, `slack_client.py:93`), a retry cooldown after a failed refresh of 5
  minutes (`_DIRECTORY_RETRY_COOLDOWN`, `slack_client.py:76`), and `atomic_write_json` persistence.
  It also has the `slack_refresh_user_cache` tool (`connectors/slack.py:171`, handler at
  `connectors/slack.py:408`, `"auto"` in `auto_accept.py:225`). It was added in commit `52768c86`.
  Slack walks the whole workspace roster weekly; this plan deliberately does not (see Design, D1).
- **Tool tables.** A new tool needs:
  - its `ToolSpec` and dispatch line in the connector;
  - a `TOOL_TO_GATE` row in `src/privacyfence/auto_accept.py:175-297` (the Jira rows are at
    268-275, Confluence at 276-286). An `auto` tool gets no `TOOL_TO_OPERATION` row and no
    `policy/registry.py` `TOOL_TO_VERB` row;
  - regenerating `docs/tools-reference.md` with `python3 scripts/generate_tools_reference.py`.

  The tests that enforce this are `tests/unit/connectors/test_readme_manifest_alignment.py`,
  `tests/unit/test_docs_tools_reference.py`, `tests/unit/test_systemic_gate_invariants.py`,
  `tests/unit/policy/test_registry.py` and `tests/helpers.py::assert_all_tools_leave_an_audit_trail`
  (called at the end of each connector test module). `connector_catalog.py` discovers tools on its
  own. The generated tools reference prints only each description's first sentence and the gate.
- **Tool counts today.** Jira has 8 tools (3 auto / 1 review / 4 popup), Confluence 10 (5/3/2), and
  there are 114 in total.
- **Tests.** Client tests: `tests/unit/test_jira_client.py` (`TestExtractAdfText`,
  `TestParseIssue`, `TestParseComment`, `TestTextToAdf`, `TestCreateIssue`, `TestLiveFixtureParsing`
  …) and `tests/unit/test_confluence_client.py` (`TestParsePageV2`, `TestGetPage`,
  `TestGetPageByTitle`, `TestListPagesInSpace` …). Connector tests:
  `tests/unit/connectors/test_jira_connector.py` and
  `tests/unit/connectors/test_confluence_connector.py` (`TestFieldCompleteness` uses
  `assert_no_placeholder_fields`). Daemon wiring: `tests/unit/test_daemon_main.py` has
  `TestBuildConnectorsSlack` (from line 859) and `TestBuildConnectorsAtlassian` (line 989, with a
  `_patch_token` helper and `fake_client_class` stubs). Live fixtures:
  `tests/fixtures/live/jira/get_issue.json` and `tests/fixtures/live/confluence/get_page.json`.
- **Write effects.** `src/privacyfence/write_effects.py:132-141` (`EFFECT_BY_TOOL`) holds the
  one-line "what happens" text for each Jira and Confluence write. A notification that the provider
  lets each person configure is phrased with "may".
- **The `i_am_author` rule.** `policy/scopes.py:145` `_i_am_author_matches` checks whether
  `ctx.my_email` occurs in `raw_data["author"]` of a Confluence read. Today `author` is an account
  id, so the rule never matches. Putting a display name there would let anyone who writes the
  victim's email into their own Atlassian display name get their pages auto-accepted. So `author`
  stays the raw id (D5).
- **Website counts.** `tests/unit/test_website_connectors_page.py` checks the tool counts printed
  on `website/connectors/index.html` (line 29 "Eleven connectors, 114 tools"; the Jira card at
  133/135; the Confluence card at 143/145) and `website/how-it-works/index.html:54` ("Each of the
  114 connector tools") against `docs/tools-reference.md`.
- **Live checks.** `scripts/qa_fixture_recorder.py`'s `check_jira`/`check_confluence` call only
  list/get endpoints, and the seed issue and page contain no mentions. So `connector-live-check.yml`
  never exercises a user lookup; only `manual_after` ma1 does.

## Design

### D1. Lazy per-id cache, not a roster snapshot

Unlike Slack, PrivacyFence never lists the whole site's users. It resolves only the ids that
actually appear, in batches, and remembers them.

Rejected alternatives:

- **A weekly full-directory walk (`GET /rest/api/3/users/search`), like Slack.** On a large site
  that is many calls. It also puts the whole company's roster on disk. And the benefit Slack gets
  from it, offline name→id matching for participant filters, is covered here by the live
  `*_find_users` tool.
- **A new `read:user:confluence` scope.** That would force every existing user to re-authenticate
  with Atlassian. Confluence uses Jira's user API with the token it already has. On a
  Confluence-only site (no Jira product), lookups fail, and the cooldown in D2 keeps retries to one
  attempt per 5 minutes. Names then fall back as D4 and D5 describe.

### D2. New module `src/privacyfence/atlassian_users.py`

Module docstring: one paragraph. It is the account-id → display-name directory shared by the Jira
and Confluence clients, looked up lazily through Jira's user API (ADR 0115). It also holds the
`@[Name](accountId)` mention markup helpers used on reads and writes.

Constants (exact names and values):

```python
USER_CACHE_TTL = timedelta(days=7)
_NEGATIVE_LOOKUP_TTL = timedelta(hours=1)
_FETCH_FAILURE_COOLDOWN = timedelta(minutes=5)
BULK_BATCH_SIZE = 100
_BULK_PAGE_BUDGET = 5
FIND_USERS_MAX_RESULTS = 50
_HTTP_TIMEOUT_SECONDS = 30
ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9:_-]{10,128}")   # always used with .fullmatch()
MENTION_MARKUP_RE = re.compile(r"@\[([^\]\n]{1,200})\]\(([A-Za-z0-9:_-]{10,128})\)")
_STORAGE_USER_RE = re.compile(r'<ri:user\b[^>]*?\bri:account-id="([^"]+)"[^>]*>')
_STORAGE_MENTION_RE = re.compile(
    r'<ac:link\b[^>]*>\s*<ri:user\b[^>]*?\bri:account-id="([^"]+)"[^>]*?(?:/>|>\s*</ri:user>)\s*'
    r'(?:<ac:(?:plain-text-)?link-body>.*?</ac:(?:plain-text-)?link-body>\s*)?</ac:link>',
    re.S,
)
UNKNOWN_USER_LABEL = "unknown user"
```

Every "matches `ACCOUNT_ID_RE`" below means `ACCOUNT_ID_RE.fullmatch(value)` (a `.match` with `$`
would accept a trailing newline). All timestamps are `datetime.now(timezone.utc)`; they are saved
with `isoformat()` and loaded with `datetime.fromisoformat`.

Types:

```python
class AtlassianUsersError(Exception):
    """Raised when a user lookup against Atlassian cannot complete."""

@dataclass
class AtlassianUser:
    account_id: str
    display_name: str
    active: bool = True
    account_type: str = ""   # Atlassian's accountType: "atlassian", "app" or "customer"
```

Module functions (HTTP helpers take the caller's authenticated `requests.Session`, so each client
keeps its own token refresh):

- `jira_api_base(cloud_id: str) -> str` returns `f"https://api.atlassian.com/ex/jira/{cloud_id}"`.
- `parse_user(raw: dict[str, Any]) -> AtlassianUser | None` reads `accountId`, `displayName`,
  `active` (default `True`) and `accountType` (default `""`). It returns `None` when `accountId` is
  missing or empty. Email is never read or kept.
- `fetch_users_bulk(session: requests.Session, cloud_id: str, account_ids: list[str]) -> list[AtlassianUser]`:
  - Calls `GET {jira_api_base(cloud_id)}/rest/api/3/user/bulk` with
    `params=[("accountId", i) for i in account_ids] + [("maxResults", BULK_BATCH_SIZE), ("startAt", start)]`
    and `timeout=_HTTP_TIMEOUT_SECONDS`, then calls `response.raise_for_status()`.
  - Parses `values` with `parse_user`. While `isLast` is falsy, it follows `startAt += len(values)`,
    for at most `_BULK_PAGE_BUDGET` pages, and stops on an empty page.
  - Callers pass at most `BULK_BATCH_SIZE` ids.
  - It raises whatever `requests` raises and never catches.
- `search_users(session: requests.Session, cloud_id: str, query: str, max_results: int) -> list[AtlassianUser]`:
  - Calls `GET {base}/rest/api/3/user/search` with `params={"query": query, "maxResults": max_results}`
    and the same timeout, then `raise_for_status()`.
  - The response is a JSON list, parsed with `parse_user`; `None` entries are dropped.
- `mention_markup(name: str, account_id: str) -> str`:
  - Removes `[`, `]`, `\r` and `\n` from `name` and strips it. An empty result becomes
    `UNKNOWN_USER_LABEL`.
  - Returns `f"@[{clean}]({account_id})"`.
- `markup_mention_ids(text: str) -> list[str]` returns the account ids of the `MENTION_MARKUP_RE`
  matches, in order, without duplicates.
- `display_markup(text: str, names: Mapping[str, str] | None = None) -> str` replaces each markup
  match with `"@" + (names[id] if names and id in names else label)`. It is used for everything a
  human sees.
- `storage_mention_ids(html: str) -> list[str]` returns the `_STORAGE_USER_RE` group 1 values, in
  order, without duplicates.
- `storage_mentions_to_text(html: str, names: Mapping[str, str]) -> str` replaces each
  `_STORAGE_MENTION_RE` match with `"@" + html.escape(names.get(id) or UNKNOWN_USER_LABEL)`. Use
  the stdlib `html` module imported as `import html as html_lib`, so the parameter name `html`
  does not shadow it.
- `markup_to_storage(text: str) -> str` replaces each `MENTION_MARKUP_RE` match with
  `f'<ac:link><ri:user ri:account-id="{account_id}" /></ac:link>'`. The id is safe to interpolate
  because the regex character class admits no quote or angle bracket.

Class `AtlassianUserDirectory`:

- `__init__(self, cache_file: str = "", cloud_id: str = "") -> None`. `cache_file=""` keeps the
  cache in memory only.
- State:
  - `self._users: dict[str, AtlassianUser]` and `self._fetched_at: dict[str, datetime]`;
  - `self._negative: dict[str, datetime]`;
  - `self._last_failure: datetime | None`;
  - `self._loaded = False`;
  - `self._lock = threading.Lock()`, which guards state only and is never held during HTTP;
  - `self._refresh_lock = threading.Lock()`, which keeps refreshes single-flight.
- `resolve(self, account_ids: Iterable[str], fetch: Callable[[list[str]], list[AtlassianUser]]) -> dict[str, str]`
  never raises. The network call happens outside `_lock`:
  1. Under `_lock`: load from disk once.
  2. Keep the ids matching `ACCOUNT_ID_RE`, without duplicates.
  3. An id needs fetching when it is not cached, or when its `_fetched_at` is at least
     `USER_CACHE_TTL` old, and it has no `_negative` entry younger than `_NEGATIVE_LOOKUP_TTL`.
  4. Still under `_lock`, decide: fetch only if any need it and `_last_failure` is `None` or at
     least `_FETCH_FAILURE_COOLDOWN` ago. Release `_lock`, then call `fetch` in chunks of
     `BULK_BATCH_SIZE`. Re-acquire `_lock` to merge the results.
     - Every returned user is stored with `fetched_at = now`.
     - Every requested id that did not come back goes into `_negative`.
     - An exception from `fetch` sets `_last_failure = now`, is logged once with
       `logger.warning("Could not resolve %d Atlassian account id(s) (non-fatal): %s", n, exc)`,
       and stops the remaining chunks.
  5. Under `_lock`: save to disk if anything was stored.
  6. Under `_lock`: return `{id: user.display_name}` for every requested id now in `_users`. A stale name counts
     when the refetch failed.
- `remember(self, users: Iterable[AtlassianUser]) -> None` stores each user with `fetched_at = now`,
  drops it from `_negative` and saves to disk. It runs under `_lock` and loads first.
- `refresh(self, fetch: Callable[[list[str]], list[AtlassianUser]]) -> int`:
  - It takes `_refresh_lock` with `acquire(blocking=False)`. If that fails it raises
    `AtlassianUsersError("refresh already in progress")`. It releases the lock in `finally`.
  - Under `_lock` it loads and snapshots the cached ids. It releases `_lock` for the fetches and
    re-acquires it to swap in the result.
  - It re-fetches every cached id in `BULK_BATCH_SIZE` chunks into a new dict.
  - Any exception from `fetch` raises `AtlassianUsersError(f"refresh failed: {exc}")` from it and
    leaves the cache unchanged.
  - On success it replaces `_users`/`_fetched_at` (ids that did not come back are dropped, and go
    into `_negative`), clears `_last_failure`, saves, and returns `len(self._users)`.
  - With nothing cached it returns 0 without calling `fetch`.
- Disk format, written with `secure_files.atomic_write_json(self._cache_file, payload)`:
  `{"cloud_id": <cloud_id>, "users": {<id>: {"account_id", "display_name", "active", "account_type", "fetched_at": <iso>}}}`.
- Loading:
  - The file is ignored (the cache starts empty) when it is missing or unreadable, when it is not
    a dict, or when its `cloud_id` differs from the constructor's.
  - An entry that fails to parse is skipped.
  - Errors are logged at `warning` with "(non-fatal)" and never raised.
  - Save errors (`OSError`) are logged the same way.

Nothing is kept at module level, so `tests/conftest.py` needs no reset line.

### D3. Client API (both clients)

`JiraClient.__init__(self, config, token_file=None, user_directory: AtlassianUserDirectory | None = None)`
and the same for `ConfluenceClient`. When `user_directory` is `None`, the client builds
`AtlassianUserDirectory(cloud_id=cloud_id)` (memory only), after the existing
missing-token/cloud-id check. The directory is stored as `self._users`. A caller that passes one
must have built it with the same `cloud_id`; `daemon_main` does (D7).

Each client calls the helpers as `atlassian_users.fetch_users_bulk(...)` /
`atlassian_users.search_users(...)` after `from . import atlassian_users`. The module-attribute
form lets tests monkeypatch `privacyfence.atlassian_users.fetch_users_bulk`. Each client gains:

- `_fetch_users_bulk(self, account_ids: list[str]) -> list[AtlassianUser]` makes the bulk call with
  one token refresh and retry on 401.
  - **Jira:** `return self._request(atlassian_users.fetch_users_bulk, self._session, self._config.get("cloud_id", ""), account_ids)`.
  - **Confluence:** its `_request` also refreshes on 403/404, which would rotate the refresh token
    for nothing on a Confluence-only site. So add `_request_jira_api(self, fn, *args)`. It retries
    only when `atlassian_oauth.is_unauthorized(exc)` and `self._try_refresh()`, and is otherwise
    identical to Jira's `_request`. Use it here and in `find_users`.

    The gateway may also answer a token without Jira access with 401. So when the retry after a
    successful refresh fails with 401 again, set `self._jira_api_denied_at = now`. While that is
    less than `_NEGATIVE_LOOKUP_TTL` (import it from `atlassian_users`) ago, a 401 re-raises
    without calling `_try_refresh`. That rotates the shared refresh token at most once an hour.
    Initialise the attribute to `None` in `__init__`.
- `resolve_user_names(self, account_ids: list[str]) -> dict[str, str]` returns
  `self._users.resolve(account_ids, self._fetch_users_bulk)` and never raises.
- `find_users(self, query: str, max_results: int = 10) -> list[AtlassianUser]`:
  - A blank `query` raises `<Client>Error("find_users requires a non-empty query")`.
  - Clamps `max_results` to `1..FIND_USERS_MAX_RESULTS` and calls `search_users` through the
    401-retry wrapper.
  - Any exception becomes `<Client>Error(f"find_users failed: {exc}")`.
  - On success it calls `self._users.remember(users)` and returns `users`.
- `refresh_user_cache(self) -> int` returns `self._users.refresh(self._fetch_users_bulk)`, and turns
  an `AtlassianUsersError` into `<Client>Error(str(exc))`.

### D4. Jira reads

- **Extracting text.** `JiraClient._extract_adf_text(node, names: Mapping[str, str] | None = None)`.
  - A node with `type == "mention"` renders as
    `mention_markup(<name>, id)`, where the name is `names.get(id)` if present, else
    `attrs.get("text", "").lstrip("@")`, else `UNKNOWN_USER_LABEL`. This applies when
    `attrs.get("id")` matches `ACCOUNT_ID_RE`.
  - A mention node without a valid id renders as `attrs.get("text", "")`.
  - Recursion passes `names` down. All other behavior is unchanged: existing
    `TestExtractAdfText` cases keep passing untouched.
- **Collecting ids.** A new static method `JiraClient._collect_adf_mention_ids(node) -> list[str]`
  returns every valid mention id in document order, without duplicates.
- **Parsing.** `_parse_issue(raw, include_description=False, names=None)` and
  `_parse_comment(raw, names=None)` pass `names` into `_extract_adf_text`.
- **`get_issue`.** It collects ids from `fields.description` when that is a dict, calls
  `self.resolve_user_names(ids)` only when ids is non-empty, and passes the result to
  `_parse_issue`.
- **`get_issue_comments`.** It collects ids across all comment bodies, makes one
  `resolve_user_names` call when non-empty, and passes the names to each `_parse_comment`.
- **Fallback.** When a name cannot be resolved (a Confluence-only site, a missing "Browse users and
  groups" permission, the cooldown), the name comes from the ADF `attrs.text` that Jira supplies.
  So the result never gets worse than today, which is an empty string.

### D5. Confluence reads

- **`ConfluencePage` fields.** It gains `author_name: str = ""` and
  `mentions: dict[str, str] = field(default_factory=dict)`, placed after `url`. Import `field`.
  `author` keeps the raw account id, because the `i_am_author` rule reads it (Current state).
- **`_parse_page_v2`.** Its signature becomes
  `_parse_page_v2(raw, include_body=False, space_key="", names: Mapping[str, str] | None = None)`.
  - `author = raw.get("authorId", "")`, unchanged.
  - `author_name = (names or {}).get(author, "")`.
  - `mentions = {i: names[i] for i in storage_mention_ids(body) if names and i in names}`.
  - Update its docstring: `author` stays the opaque id, and `author_name` carries the name from
    the shared user directory when one is available.
- **`get_page` / `get_page_by_title`.** Each gathers
  `[i for i in [raw.get("authorId", "")] + storage_mention_ids(body) if i]` from the raw response.
  It makes one `resolve_user_names` call only when that list is non-empty, then parses.
- **`list_pages_in_space`.** It gathers every page's non-empty `authorId` and makes one
  `resolve_user_names` call when there is at least one.
- **The body sent to the agent is unchanged.** It stays raw storage format, so the agent can edit
  it and write it back without losing mentions. The agent learns the names from `mentions`.

### D6. Connectors: read previews and the four new tools

**Jira `jira_get_issue` preview.**

- `details_text`, `pii_scan_text` and the `text` block use `display_markup(issue.description)`.
- The comments table's Comment cells and `pii_scan_text` use `display_markup(c.body)`.
- The agent's `result` keeps the markup.
- Append this sentence to the tool description, after the existing text:
  ` Mentions appear as @[Name](accountId).`

**Confluence `_get_page` / `_get_page_by_title` preview.**

- `body_text = html_to_markdown(storage_mentions_to_text(body_raw, page.mentions))`.
- "Author" in `new_info` becomes `page.author_name or page.author or "(unknown)"`. The `sender=`
  argument becomes `page.author_name or page.author or page_id` (`or space_key` in
  `_get_page_by_title`). `raw_data`/`filtered_data` stay `asdict(page)`, so `author` is still the
  id that `i_am_author` reads.
- Append to both descriptions:
  ` The result's author_name names the author, and its mentions field maps each @mentioned account id in the body to a name.`

**The four new tools.** All are `read_only=True`, with no gate. Each takes a required `reason`
param, uses the same `reason` description as the other tools, calls `_auto_audit`, and gets an
`"auto"` row in `TOOL_TO_GATE`. Exact specs:

| Tool | Params (after none, before `reason`) | Description (exact) | Returns | `_auto_audit(tool, tool_name, summary, sender, t0)` |
|---|---|---|---|---|
| `jira_find_users` | `ToolParam("query", "str", description="Part of a person's name or email address")`, `ToolParam("max_results", "int", required=False, default=10)` | `Find Atlassian users by name or email and return their account ids. Auto-approved -- mention someone in a comment or description by writing @[Name](accountId), or assign an issue with assignee_account_id. Email addresses are never returned.` | `[asdict(u) for u in users]` | `("jira_find_users", "Find Jira Users", f"Find users: {query[:80]}", f"{len(users)} user(s)", t0)` |
| `jira_refresh_user_cache` | (none) | `Re-fetch the names of every Atlassian account id PrivacyFence has cached. Auto-approved -- use this when a renamed or newly added person shows up wrong; cached names otherwise refresh on their own after 7 days.` | `{"cached_users": count}` | `("jira_refresh_user_cache", "Refresh Atlassian User Cache", "Refresh Atlassian user cache", f"{count} user(s)", t0)` |
| `confluence_find_users` | same as `jira_find_users` | `Find Atlassian users by name or email and return their account ids. Auto-approved -- mention someone in a page body by writing @[Name](accountId). Email addresses are never returned.` | same | `("confluence_find_users", "Find Confluence Users", …same…)` |
| `confluence_refresh_user_cache` | (none) | same text as `jira_refresh_user_cache` | same | `("confluence_refresh_user_cache", "Refresh Atlassian User Cache", …same…)` |

Handlers are `_find_users(self, query: str, max_results: int = 10)` and
`_refresh_user_cache(self)`. They go in each connector's auto-tools section and call the client
through the connector's existing `_fetch`, which turns a client error into `RuntimeError`.

**Tool counts.** After this, Jira has 10 tools (5/1/4), Confluence 12 (7/3/2), and the total is
118 (46/21/51). The generator recomputes these.

### D7. Daemon wiring

In `daemon_main.build_connectors`, directly after `atlassian_config = {...}` (line 1466), add:

```python
atlassian_users = AtlassianUserDirectory(
    cache_file=str(user_dir() / "atlassian_user_cache.json"),
    cloud_id=atlassian_config.get("cloud_id", ""),
)
```

Pass `user_directory=atlassian_users` to both `JiraClient(...)` and `ConfluenceClient(...)`, so the
two share one instance and one file. Nothing is warmed at startup: the cache is lazy, and
`_warm_connector_caches` is left alone.

### D8. Jira writes

- **Building ADF.** `jira_client._text_to_adf(text: str, names: Mapping[str, str] | None = None)`.
  - Text with no `MENTION_MARKUP_RE` match returns exactly today's structure, so existing tests
    pass unchanged.
  - Otherwise it builds one paragraph whose `content` alternates text nodes (non-empty segments
    only) and mention nodes:
    `{"type": "mention", "attrs": {"id": account_id, "text": "@" + (names.get(account_id) if names and account_id in names else label)}}`.
- **Client signatures.**
  - `add_comment(issue_key, body, mention_names: Mapping[str, str] | None = None)`.
  - `create_issue(..., labels=None, mention_names: Mapping[str, str] | None = None)`, which passes
    them to `_text_to_adf(description, mention_names)`.
  - `update_issue` is unchanged, since the connector builds its `fields`.
- **Connector helper.** Add
  `async _resolve_write_accounts(self, text: str, extra_ids: list[str]) -> dict[str, str]` to
  `connectors/jira.py`.
  - It computes `ids = markup_mention_ids(text) + [i for i in extra_ids if i]`, without
    duplicates.
  - With no ids it returns `{}`.
  - Otherwise it calls `names = await self._fetch(self._jira.resolve_user_names, ids)`.
  - Any id missing from `names` raises
    `ValueError(f"Unknown Atlassian account id(s): {', '.join(missing)}. Look the person up with jira_find_users and use their account_id.")`.
  - It returns `names`.
- **Preview rows.** When the text has mentions, add
  `preview["Mentions"] = ", ".join(names[i] for i in markup_mention_ids(text))`.
  When there is an assignee, add `preview["Assignee"] = names[assignee_account_id]`. Both use the
  directory's names, never the agent's labels.
- **`_add_comment`.**
  - It resolves before `gated_call`.
  - `summary=f"Comment on {issue_key}: {display_markup(body, names)[:80]}"`, so the card title
    never shows the agent's label either.
  - `details_text = display_markup(body, names)`.
  - The `raw_data`/`args` body stays the agent's original text.
  - It calls `self._jira.add_comment(issue_key, body, names)`.
  - Extend the `body` param description to
    `Comment text (plain text). Mention someone with @[Name](accountId) -- find the id with jira_find_users.`
- **`_create_issue`.**
  - New param `ToolParam("assignee_account_id", "str", required=False, default="", description="Atlassian account id to assign the issue to -- find it with jira_find_users")`,
    placed before `reason`.
  - It resolves `description` plus `[assignee_account_id]`.
  - The payload/args dict gains `"assignee_account_id"` only when it is non-empty.
  - The description block text and `details_text` use `display_markup(description, names)`.
  - It calls
    `self._jira.create_issue(project_key, summary, issue_type, description, priority, assignee_account_id, None, names)`.
- **`_update_issue`.**
  - It gets the same new param.
  - When set, `fields["assignee"] = {"accountId": assignee_account_id}` and
    `preview["Assignee"] = f"→ {names[assignee_account_id]}"`.
  - When `description` is set, `fields["description"] = _text_to_adf(description, names)`, and
    `details_text = display_markup(description, names)`.
  - An update with only an assignee counts as "at least one field".
  - Append ` Pass assignee_account_id to reassign it.` to the tool's description (after the
    existing text, first sentence unchanged).
- **Write-effects text.** Update `write_effects.py`'s rows to these exact strings:
  - `jira_create_issue`: `"A new issue is created. The project's watchers, the assignee and anyone @mentioned may be notified."`
  - `jira_add_comment`: `"A comment is added, visible to everyone who can see the issue. Anyone @mentioned may be notified."`
  - `jira_update_issue`: `"The issue's fields are changed. Its watchers, a new assignee and anyone newly @mentioned may be notified."`
  - `confluence_create_page`: `"A new page is created in that space, visible to everyone with access to it. Anyone @mentioned may be notified."`
  - `confluence_update_page`: `"The page's contents are replaced. The previous version stays in the page's history. Anyone newly @mentioned may be notified."`

### D9. Confluence writes

In `connectors/confluence.py`, `_create_page` and `_update_page` do this before `gated_call`:

1. `markup_ids = markup_mention_ids(body)`, `existing_ids = storage_mention_ids(body)`, and `ids` =
   both lists joined without duplicates.
2. If `ids` is non-empty, `names = await self._fetch(self._confluence.resolve_user_names, ids)`,
   else `{}`.
3. Any id in `markup_ids` missing from `names` raises
   `ValueError(f"Unknown Atlassian account id(s): {', '.join(missing)}. Look the person up with confluence_find_users and use their account_id.")`.
   Ids in `existing_ids` are not refused: raw storage mentions already work today and may be
   unresolvable on a Confluence-only site.
4. `storage_body = markup_to_storage(body)`.
5. If `ids` is non-empty,
   `preview["Mentions"] = ", ".join(names.get(i) or f"unknown account {i}" for i in ids)`.
6. `raw_data["body"]`, `details_text` and the client call all use `storage_body`.

Append ` Mention someone with @[Name](accountId) -- find the id with confluence_find_users.` to
the `body` param descriptions of both tools.

Known limitation, accepted: `markup_to_storage` rewrites the markup anywhere in the body, including
inside an attribute value or a CDATA code block, and a literal `@[text](long_anchor)` there is
refused as an unknown id. The agent already writes arbitrary storage format, so this grants
nothing new. It only means such literal text cannot be written through these tools.

### D10. What does not change

- No privacy-filter category is added. Jira already returns assignee, reporter and comment-author
  names unfiltered. Mention names in a body sit inside content the human approves at the review
  gate. `*_find_users` returns no email.
- `*_find_users` is auto-approved: it is a directory lookup that returns names and ids only, the
  same sensitivity as `jira_search_issues` returning assignee names (ADR 0117).
- No OAuth scope is added (D1).
- `ConfluencePage.author` stays the raw account id, so the `i_am_author` auto-accept rule still
  never matches on a display name (D5).
- The first sentence of every existing tool description stays exactly as it is, so
  `docs/tools-reference.md` changes only through the four new rows and the count row.

## ADRs

- **0115** — Atlassian account ids are resolved through a lazy, disk-persisted per-id cache that
  Jira and Confluence share, using Jira's user API with the existing `read:jira-user` scope.
  Rejected: a weekly full-roster snapshot like Slack's, and adding `read:user:confluence`.
- **0116** — An approver sees Atlassian's own name for every account id a Jira or Confluence write
  mentions or assigns, never the agent's label. A mention-markup or assignee id that cannot be
  resolved is refused before the approval card. Raw storage-format mentions already in a
  Confluence body are shown, not refused.
- **0117** — `jira_find_users` / `confluence_find_users` are auto-approved and never return an
  email address.

## Manual steps

- `manual_before`: none. The Jira user API needs only `read:jira-user`, which every existing token
  already has.
- `manual_after`:
  - **ma1**: on the QA Atlassian site, read an issue and a page that mention someone, and check the
    names and approval previews.
  - **ma2**: post a Jira comment and create a Confluence page that each mention someone. Check that
    the approval card names the right person and that the mention notifies them.

Step-by-step page: https://claude.ai/artifact/QtNMCYuE915oX7TTD96n66

The last phase dispatches `connector-live-check.yml` for §2.7's QA row. That check makes no user
lookup, so ma1 is the real-site check of the new endpoints.

## Risks and open questions

- **Endpoint and node shapes are verified by hand only.** The bulk/search response shapes (D2), the
  ADF mention node (D4) and the storage mention (D5) come from Atlassian's documentation. No
  committed fixture contains a mention, and `connector-live-check.yml` makes no user lookup.
  `manual_after` ma1 is what verifies them on a real site. If ma1 shows names missing, the D2
  parser or the D4/D5 node handling is the first suspect.
- **401 from the Jira API on a Confluence-only site.** D3's `_jira_api_denied_at` limits token
  refreshes to one an hour. The symptom if that fails is a "Confluence access token refreshed" log
  line every 5 minutes.
- **"Browse users and groups".** If the site's admin removed this global permission, bulk and
  search return 403. Reads fall back to the ADF text and to raw ids. Writes that mention or assign
  are refused with the "Unknown Atlassian account id(s)" error. This is intended (ADR 0116), and
  `docs/atlassian-setup.md` documents it in the last phase.
- **Existing tests that pin client calls.** `test_jira_connector.py:290`
  (`create_issue.assert_called_once_with("ENG", "New bug", "Task", "", "")`) and `:332`
  (`add_comment.assert_called_once_with("ENG-42", "On it")`) change with D8. p5's brief updates
  them. Any other existing test that fails in p5/p6 means the brief missed a call site: stop with
  `status=blocked` and name it.
- **Code history.** `tests/unit/test_code_no_history.py` blocks phase names, plan ids and bare
  issue numbers in code comments and docstrings. Workers cite ADRs 0115–0117 by number only, and
  those ADRs are written in the last phase.

## Implementation manifest

```yaml
plan_slug: atlassian-user-names
feature_branch: feature/atlassian-user-names
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/QtNMCYuE915oX7TTD96n66
manual_steps_source: docs/atlassian-user-names-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-verify-reads
    title: Read a Jira issue and a Confluence page that @mention someone on the QA site
    why: Proves real Atlassian responses resolve to names in both the agent's result and the approval preview, which unit tests with stubbed responses cannot.
  - id: ma2-verify-mention-writes
    title: Post a Jira comment and create a Confluence page that @mention someone, and check the notification
    why: Only a real site shows that the mention node notifies the right person and that the approval card names them.
verify_after_merge:
  - ruff check .
  - python3 -m pytest tests/unit/test_jira_client.py tests/unit/test_confluence_client.py tests/unit/connectors/test_jira_connector.py tests/unit/connectors/test_confluence_connector.py tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_docs_tools_reference.py tests/unit/test_systemic_gate_invariants.py tests/unit/policy/test_registry.py tests/unit/test_code_no_history.py -q
final_checks:
  - docs/atlassian-user-names-plan.md and docs/atlassian-user-names-plan-manual-steps.html are deleted and nothing links to them
  - docs/adr/0115-*.md, docs/adr/0116-*.md and docs/adr/0117-*.md exist with Status Accepted and are listed in docs/adr/README.md's index
  - CHANGELOG.md has [Unreleased] entries and no version heading
  - python3 -m pytest -q passes and python3 scripts/check_coverage_floor.py coverage.json passes (run with --cov as in docs/coding-and-testing-guidelines.md §2.7)
phases:
  - id: p1-user-directory
    title: atlassian_users module — lazy shared account-id cache and mention markup helpers
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/atlassian_users.py
      - tests/unit/test_atlassian_users.py
    brief: |
      Read the plan's "Design" sections D1 and D2 first; they are the spec. Copy the style of
      src/privacyfence/slack_client.py's directory cache (_load_user_directory_from_disk,
      _save_user_directory_to_disk, _NEGATIVE_LOOKUP_TTL, _DIRECTORY_RETRY_COOLDOWN) where D2
      does not say otherwise.
      1. Create src/privacyfence/atlassian_users.py exactly as D2 specifies: module docstring,
         `from __future__ import annotations`, logger = logging.getLogger(__name__), the constants
         with the exact names and values in D2, AtlassianUsersError, the AtlassianUser dataclass,
         jira_api_base, parse_user, fetch_users_bulk, search_users, mention_markup,
         markup_mention_ids, display_markup, storage_mention_ids, storage_mentions_to_text,
         markup_to_storage, and AtlassianUserDirectory with resolve, remember and refresh.
         Persist with secure_files.atomic_write_json. Type-hint everything (X | None, not
         Optional). No comments that restate what a line does; no plan/phase references.
      2. Create tests/unit/test_atlassian_users.py (module docstring naming the module and the
         invariant "a read never costs more than one bulk call per 100 unseen ids, and an
         unresolvable id is not retried within an hour"; pytestmark = pytest.mark.unit). Use
         MagicMock sessions whose .get returns a response with .json() and .raise_for_status(),
         freezegun for time, tmp_path for cache files. Classes and minimum cases:
         - TestParseUser: full record; missing accountId -> None; emailAddress in raw is not on the result.
         - TestFetchUsersBulk: params carry one ("accountId", id) per id plus maxResults=100 and
           startAt=0; follows isLast=False to a second page with startAt advanced; stops after
           _BULK_PAGE_BUDGET pages when isLast never turns true; raise_for_status error propagates.
         - TestSearchUsers: query and maxResults params; list response parsed; entries without
           accountId dropped.
         - TestMentionMarkup: mention_markup strips brackets/newlines and falls back to
           "unknown user"; markup_mention_ids order + dedup; display_markup with and without
           names (names win over the label); storage_mention_ids; storage_mentions_to_text
           html-escapes a name like "<b>x</b>" and handles <ac:link-body> and
           <ac:plain-text-link-body> variants and the non-self-closing
           <ri:user ri:account-id="..."></ri:user> form; markup_to_storage output string exact; a markup
           id containing a quote does not match MENTION_MARKUP_RE.
         - TestDirectoryConcurrency: a fetch that blocks on a threading.Event (in a worker
           thread) does not block a concurrent resolve of an already-cached id (join with a
           timeout, no sleeps); a second refresh while one is in progress raises
           AtlassianUsersError("refresh already in progress").
         - TestDirectoryResolve: first resolve calls fetch once with only valid, deduplicated ids;
           second resolve within 7 days makes no fetch call; after 7 days (freezegun) refetches;
           >100 ids -> chunks of 100; id absent from the response is negative-cached and not
           refetched within 1 hour but is after; fetch raising -> {} returned, warning logged,
           and no fetch within 5 minutes, one after; stale name returned when refetch fails;
           invalid ids ("", "a b", "x"*200, a valid id plus a trailing "\n") never passed to fetch.
         - TestDirectoryPersistence: resolve writes the file with the D2 format and cloud_id;
           a new directory with the same file + cloud_id resolves from disk with no fetch; a
           different cloud_id ignores the file; corrupt JSON and a non-dict are ignored with no
           raise; cache_file="" writes nothing.
         - TestDirectoryRememberAndRefresh: remember makes a later resolve fetch-free and clears
           a negative entry; refresh re-fetches all cached ids, drops ids not returned, returns
           the count; refresh with an empty cache returns 0 without calling fetch; refresh whose
           fetch raises -> AtlassianUsersError and the cache is unchanged.
      3. Run: ruff check src/privacyfence/atlassian_users.py tests/unit/test_atlassian_users.py;
         python3 -m pytest tests/unit/test_atlassian_users.py -q --cov=privacyfence.atlassian_users
         --cov-branch --cov-report=term-missing and reach 100% line and branch coverage of the new
         module; python3 -m pytest tests/unit/test_code_no_history.py -q.
      No CHANGELOG line in this phase (the last phase writes it).
      Whole-suite coverage must stay at 100% (see p2's brief for the command).
      Stop condition: if secure_files.atomic_write_json's signature is not (path, data, *, mode=...,
      **json_kwargs), stop with status=blocked and quote it.
    acceptance:
      - python3 -m pytest tests/unit/test_atlassian_users.py -q passes
      - coverage of src/privacyfence/atlassian_users.py is 100% lines and branches in the command from step 3
      - ruff check . passes
      - grep -n "USER_CACHE_TTL = timedelta(days=7)" src/privacyfence/atlassian_users.py matches
      - python3 -m pytest tests/unit/test_code_no_history.py -q passes

  - id: p2-jira-client-reads
    title: JiraClient resolves ADF mentions and gains find_users / refresh_user_cache
    depends_on: [p1-user-directory]
    complexity: M
    touches:
      - src/privacyfence/jira_client.py
      - tests/unit/test_jira_client.py
    brief: |
      Spec: plan Design D3 (Jira parts) and D4. Do not touch _text_to_adf or any write method
      (that is a later phase).
      Every phase rule: do not edit CHANGELOG.md and do not dispatch connector-live-check.yml;
      p7-retire-plan does both. Report /dod's CHANGELOG and live-QA rows as "owed by
      p7-retire-plan". Whole-suite coverage must stay at 100% (python3 -m pytest
      --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json, then
      python3 scripts/check_coverage_floor.py coverage.json): cover every new branch.
      1. In src/privacyfence/jira_client.py: `from . import atlassian_users` and import
         AtlassianUser, AtlassianUserDirectory, AtlassianUsersError, ACCOUNT_ID_RE,
         UNKNOWN_USER_LABEL, mention_markup from it. Add the `user_directory` constructor param
         (D3), stored as self._users. Add _fetch_users_bulk, resolve_user_names, find_users,
         refresh_user_cache (D3) in a new "Users" banner section after "Connection". Update the
         module docstring with one short paragraph on the shared user directory (ADR 0115).
      2. Change _extract_adf_text to take `names: Mapping[str, str] | None = None` and render
         mention nodes per D4; add static _collect_adf_mention_ids. Thread `names` through
         _parse_issue and _parse_comment. Make get_issue and get_issue_comments resolve per D4
         (no resolve call when there are no mention ids).
      3. Extend tests/unit/test_jira_client.py:
         - TestExtractAdfText: mention with names hit -> "@[Jane Doe](<id>)"; names miss ->
           uses attrs.text without its "@"; no text and no name -> "@[unknown user](<id>)";
           mention with invalid id -> attrs.text; mention nested inside a paragraph among text.
         - new TestCollectAdfMentionIds: nested order and dedup, invalid ids skipped.
         - TestGetIssue / TestGetIssueComments: monkeypatch
           privacyfence.atlassian_users.fetch_users_bulk; one bulk call for a description (and
           one for all comments together) containing mentions; zero calls when there are none;
           rendered markup uses the fetched name.
         - new TestUserLookups: find_users blank query -> JiraClientError; max_results clamped to
           1..50; results remembered (a later resolve_user_names for that id makes no bulk call);
           search failure -> JiraClientError; refresh_user_cache returns the count and maps
           AtlassianUsersError to JiraClientError; _fetch_users_bulk refreshes and retries once on
           401 (reuse this module's unauthorized_error() helper and TestRequest's refresh stub).
         - TestConstruction: default directory is memory-only; a passed directory is used.
         - TestLiveFixtureParsing must still pass unchanged.
      4. Run ruff check ., python3 -m pytest tests/unit/test_jira_client.py -q, and
         python3 -m pytest tests/unit/test_code_no_history.py -q.
      Stop condition: if tests/fixtures/live/jira/get_issue.json contains a node with
      "type": "mention" whose account id is not at attrs.id, stop with status=blocked and quote
      the node shape.
    acceptance:
      - python3 -m pytest tests/unit/test_jira_client.py -q passes
      - grep -n "def resolve_user_names" src/privacyfence/jira_client.py matches
      - grep -n "def find_users" src/privacyfence/jira_client.py matches
      - grep -n "def refresh_user_cache" src/privacyfence/jira_client.py matches
      - ruff check . passes

  - id: p3-confluence-client-reads
    title: ConfluenceClient resolves authors and storage mentions and gains find_users / refresh_user_cache
    depends_on: [p1-user-directory]
    complexity: M
    touches:
      - src/privacyfence/confluence_client.py
      - tests/unit/test_confluence_client.py
    brief: |
      Spec: plan Design D3 (Confluence parts, including _request_jira_api and
      _jira_api_denied_at) and D5. Do not touch create_page/update_page.
      Every phase rule: do not edit CHANGELOG.md and do not dispatch connector-live-check.yml;
      p7-retire-plan does both. Report /dod's CHANGELOG and live-QA rows as "owed by
      p7-retire-plan". Whole-suite coverage must stay at 100% (python3 -m pytest
      --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json, then
      python3 scripts/check_coverage_floor.py coverage.json): cover every new branch.
      1. In src/privacyfence/confluence_client.py: `from . import atlassian_users`, import
         AtlassianUser, AtlassianUserDirectory, AtlassianUsersError, storage_mention_ids from it,
         and is_unauthorized from .atlassian_oauth. Add the user_directory constructor param,
         _request_jira_api, _fetch_users_bulk, resolve_user_names, find_users,
         refresh_user_cache (D3) in a new "Users" banner section after "Connection". Update the
         module docstring with one short paragraph on the shared user directory and why its
         calls retry only on 401 (ADR 0115).
      2. Add author_name and mentions to ConfluencePage (D5; import field). author stays the raw
         authorId. Change _parse_page_v2
         per D5 and update its docstring. Make get_page, get_page_by_title and
         list_pages_in_space resolve per D5 (no resolve call when there are no ids).
      3. Extend tests/unit/test_confluence_client.py:
         - TestParsePageV2: author is always the raw authorId; author_name from names, "" when
           unresolved; mentions contains only resolved ids present in the body.
         - TestGetPage / TestGetPageByTitle: monkeypatch
           privacyfence.atlassian_users.fetch_users_bulk; exactly one bulk call carrying the
           author and every storage mention id; no bulk call when the page has neither (the
           existing TestGetPage.test_fetches_with_body has no authorId); body returned
           byte-for-byte unchanged.
         - TestListPagesInSpace: one bulk call for all authors.
         - new TestUserLookups: same cases as the Jira phase's TestUserLookups, plus: a 404 from
           the bulk call does NOT call _try_refresh (monkeypatch _try_refresh to fail the test if
           called), and a 401 does, once; a 401 that survives the refresh sets
           _jira_api_denied_at, and a second 401 within the hour does not call _try_refresh
           (freezegun), but one after the hour does.
         - TestLiveFixtureParsing must still pass unchanged.
      4. Run ruff check ., python3 -m pytest tests/unit/test_confluence_client.py -q, and
         python3 -m pytest tests/unit/test_code_no_history.py -q.
      Stop condition: if tests/fixtures/live/confluence/get_page.json's body stores a mention in
      a shape other than <ri:user ri:account-id="...">, stop with status=blocked and quote it.
    acceptance:
      - python3 -m pytest tests/unit/test_confluence_client.py -q passes
      - grep -n "author_name: str" src/privacyfence/confluence_client.py matches
      - grep -n "def _request_jira_api" src/privacyfence/confluence_client.py matches
      - ruff check . passes

  - id: p4-connector-tools-and-wiring
    title: Four new auto tools, name-aware read previews, and one shared directory in build_connectors
    depends_on: [p2-jira-client-reads, p3-confluence-client-reads]
    complexity: M
    touches:
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/connectors/confluence.py
      - src/privacyfence/auto_accept.py
      - src/privacyfence/daemon_main.py
      - docs/tools-reference.md
      - website/connectors/index.html
      - website/how-it-works/index.html
      - tests/unit/connectors/test_jira_connector.py
      - tests/unit/connectors/test_confluence_connector.py
      - tests/unit/test_daemon_main.py
      - tests/unit/test_gate_real_evaluator.py
    brief: |
      Spec: plan Design D6 and D7. Pattern to copy: Slack's slack_refresh_user_cache (spec at
      src/privacyfence/connectors/slack.py:171, handler at :408, tests in
      tests/unit/connectors/test_slack_connector.py around line 195-248), from commit 52768c86.
      Every phase rule: do not edit CHANGELOG.md and do not dispatch connector-live-check.yml;
      p7-retire-plan does both. Report /dod's CHANGELOG and live-QA rows as "owed by
      p7-retire-plan". Whole-suite coverage must stay at 100% (python3 -m pytest
      --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json, then
      python3 scripts/check_coverage_floor.py coverage.json): cover every new branch.
      1. connectors/jira.py: add jira_find_users and jira_refresh_user_cache ToolSpecs exactly as
         D6's table says (place them after jira_get_transitions), their dispatch lines in call(),
         and handlers _find_users/_refresh_user_cache in the "Auto" section. Append the D6
         sentence to jira_get_issue's description. In _get_issue apply display_markup (import
         from ..atlassian_users) to description text, comment bodies, details_text and
         pii_scan_text exactly as D6 says; `result` keeps the markup.
      2. connectors/confluence.py: add confluence_find_users and confluence_refresh_user_cache
         (after confluence_list_attachments), dispatch, handlers in the "Always-allowed" section.
         Append the D6 sentence to both get-page descriptions. In _get_page and
         _get_page_by_title build body_text with storage_mentions_to_text(body_raw, page.mentions)
         before html_to_markdown, and set the "Author" new_info row and sender= exactly as D6
         says (author_name first). Leave raw_data/filtered_data as asdict(page).
      3. auto_accept.py TOOL_TO_GATE: add "jira_find_users": "auto",
         "jira_refresh_user_cache": "auto" in the Jira block and "confluence_find_users": "auto",
         "confluence_refresh_user_cache": "auto" in the Confluence block, matching the column
         alignment there. Nothing in TOOL_TO_OPERATION or policy/registry.py.
      4. daemon_main.py: import AtlassianUserDirectory from .atlassian_users; add D7's block and
         pass user_directory=atlassian_users to both clients.
      5. Regenerate docs: python3 scripts/generate_tools_reference.py. The diff must be exactly
         the four new rows plus the Jira/Confluence/Total count changes (Jira 10 = 5/1/4,
         Confluence 12 = 7/3/2, Total 118 = 46/21/51).
         Then update the website counts that tests/unit/test_website_connectors_page.py checks:
         in website/connectors/index.html the Jira card to data-tools="10" data-auto="5"
         data-review="1" data-popup="4" with the text "10 tools: 5 without a card · 1 reviewed ·
         4 need approval", the Confluence card to data-tools="12" data-auto="7" data-review="3"
         data-popup="2" with "12 tools: 7 without a card · 3 reviewed · 2 need approval", and
         "Eleven connectors, 114 tools" to "Eleven connectors, 118 tools"; in
         website/how-it-works/index.html "Each of the 114 connector tools" to "Each of the 118
         connector tools". Copy the exact surrounding markup already on those lines.
      6. Tests:
         - test_jira_connector.py and test_confluence_connector.py: one class per new tool
           (TestFindUsers, TestRefreshUserCache) proving no gated_call, an audit entry with the
           tool name, the D6 return shape, and client error -> RuntimeError. Extend the get-issue
           / get-page preview tests: a description/comment/body with a mention shows "@Jane Doe"
           in details_text and preview_blocks while the returned data keeps the markup / raw
           storage. Give the mocked client in each module's assert_all_tools_leave_an_audit_trail
           test return values for find_users (a list of AtlassianUser) and refresh_user_cache
           (an int).
         - test_daemon_main.py: add test_both_clients_share_one_user_directory to the EXISTING
           TestBuildConnectorsAtlassian (line 989). Use its _patch_token helper and the
           fake_client_class stubs its other tests use; assert
           jira_fake.captured_kwargs["user_directory"] is
           confluence_fake.captured_kwargs["user_directory"], and that the directory's
           _cache_file == str(data_dir() / "atlassian_user_cache.json"), compared the way
           TestBuildConnectorsSlack compares its cache paths.
         - test_gate_real_evaluator.py: next to the existing i_am_author test (around line 655),
           add a regression test that a Confluence page whose author_name contains my_email but
           whose author is an account id does NOT match the i_am_author rule.
      7. Run: ruff check .; python3 -m pytest tests/unit/connectors tests/unit/test_daemon_main.py
         tests/unit/test_docs_tools_reference.py tests/unit/test_systemic_gate_invariants.py
         tests/unit/policy tests/unit/test_connector_catalog.py tests/unit/web/test_tool_schema_portability.py
         tests/unit/test_website_connectors_page.py tests/unit/test_gate_real_evaluator.py
         tests/unit/test_code_no_history.py -q.
      Stop condition: if generate_tools_reference.py changes any row other than the four new ones
      and the count row, stop with status=blocked and paste the diff.
    acceptance:
      - python3 -m pytest tests/unit/connectors tests/unit/test_daemon_main.py tests/unit/test_docs_tools_reference.py tests/unit/test_systemic_gate_invariants.py tests/unit/policy tests/unit/test_connector_catalog.py tests/unit/web/test_tool_schema_portability.py tests/unit/test_website_connectors_page.py tests/unit/test_gate_real_evaluator.py -q passes
      - grep -cE '^\| `(jira|confluence)_(find_users|refresh_user_cache)`' docs/tools-reference.md prints 4
      - grep -n "atlassian_user_cache.json" src/privacyfence/daemon_main.py matches
      - ruff check . passes

  - id: p5-jira-mention-writes
    title: Jira comments and descriptions carry real mentions; create/update take assignee_account_id
    depends_on: [p4-connector-tools-and-wiring]
    complexity: M
    touches:
      - src/privacyfence/jira_client.py
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/write_effects.py
      - tests/unit/test_jira_client.py
      - tests/unit/connectors/test_jira_connector.py
      - tests/unit/test_write_effects.py
    brief: |
      Spec: plan Design D8. The approver must always see the directory's name for an id, never
      the agent's label (ADR 0116).
      Every phase rule: do not edit CHANGELOG.md and do not dispatch connector-live-check.yml;
      p7-retire-plan does both. Report /dod's CHANGELOG and live-QA rows as "owed by
      p7-retire-plan". Whole-suite coverage must stay at 100% (python3 -m pytest
      --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json, then
      python3 scripts/check_coverage_floor.py coverage.json): cover every new branch.
      1. jira_client.py: _text_to_adf(text, names=None) per D8; add mention_names to add_comment
         and create_issue per D8. Existing TestTextToAdf cases must pass unchanged.
      2. connectors/jira.py: add _resolve_write_accounts (Helpers section), the
         assignee_account_id ToolParam on jira_create_issue and jira_update_issue (exact text in
         D8, before reason), and the _add_comment/_create_issue/_update_issue changes in D8,
         including the Mentions/Assignee preview rows, the display_markup summary in
         _add_comment, the jira_add_comment body param description and the sentence appended to
         jira_update_issue's description. Do not change the first sentence of any tool
         description.
      3. write_effects.py: replace the five strings listed at the end of D8 exactly. Add a test to
         the existing tests/unit/test_write_effects.py (in a new class TestAtlassianMentionEffects)
         asserting the five new strings.
      4. Tests:
         - test_jira_client.py TestTextToAdf: markup -> [text, mention, text] nodes; names override
           the label in attrs.text; empty leading/trailing text segments omitted; TestAddComment /
           TestCreateIssue pass mention_names through.
         - test_jira_connector.py: a comment with @[Fake Name](<id>) where the directory returns
           "Real Name" -> preview["Mentions"] == "Real Name", details_text shows "@Real Name", the
           client is called with the names; an unresolvable id -> ValueError before gated_call
           (gated_call_spy not called); create/update with assignee_account_id -> preview
           "Assignee" shows the directory name, update fields carry {"accountId": id}, an
           assignee-only update is accepted, an unresolvable assignee -> ValueError; a body with
           no mentions makes no resolve_user_names call; with a label differing from the
           directory name, "Fake Name" is in none of kwargs["summary"], kwargs["details_text"] or
           kwargs["preview"].
         - Update the two existing assertions D8 changes:
           TestCreateIssue.test_result_is_serialized_issue (line ~290) to
           assert_called_once_with("ENG", "New bug", "Task", "", "", "", None, {}) and
           TestAddComment.test_preview_and_gate (line ~332) to
           assert_called_once_with("ENG-42", "On it", {}).
      5. Run ruff check .; python3 -m pytest tests/unit/test_jira_client.py
         tests/unit/connectors/test_jira_connector.py tests/unit/test_docs_tools_reference.py
         tests/unit/connectors/test_readme_manifest_alignment.py tests/unit/test_write_effects.py
         tests/unit/test_code_no_history.py -q.
      Stop condition: if test_docs_tools_reference.py fails, a first sentence changed; revert it.
      If some policy test pins jira_create_issue's args dict shape and fails, stop with
      status=blocked and name the test.
    acceptance:
      - python3 -m pytest tests/unit/test_jira_client.py tests/unit/connectors/test_jira_connector.py tests/unit/test_docs_tools_reference.py -q passes
      - python3 -c "from privacyfence.write_effects import EFFECT_BY_TOOL as E; assert all('@mentioned' in E[t] for t in ('jira_create_issue','jira_add_comment','jira_update_issue','confluence_create_page','confluence_update_page'))" exits 0
      - grep -n "assignee_account_id" src/privacyfence/connectors/jira.py matches
      - ruff check . passes

  - id: p6-confluence-mention-writes
    title: Confluence page writes turn @[Name](accountId) into storage mentions with named approval rows
    depends_on: [p4-connector-tools-and-wiring]
    complexity: S
    touches:
      - src/privacyfence/connectors/confluence.py
      - tests/unit/connectors/test_confluence_connector.py
    brief: |
      Spec: plan Design D9 (ADR 0116).
      Every phase rule: do not edit CHANGELOG.md and do not dispatch connector-live-check.yml;
      p7-retire-plan does both. Report /dod's CHANGELOG and live-QA rows as "owed by
      p7-retire-plan". Whole-suite coverage must stay at 100% (python3 -m pytest
      --cov=src/privacyfence --cov-branch --cov-report=json:coverage.json, then
      python3 scripts/check_coverage_floor.py coverage.json): cover every new branch.
      1. connectors/confluence.py: in _create_page and _update_page implement D9 steps 1-6
         (imports from ..atlassian_users: markup_mention_ids, storage_mention_ids,
         markup_to_storage). Append the D9 sentence to both tools' body ToolParam descriptions.
         Do not change the first sentence of any tool description.
      2. tests/unit/connectors/test_confluence_connector.py: create and update each with a markup
         mention whose directory name differs from the label -> preview["Mentions"] is the
         directory name, raw_data["body"], details_text and the client call carry
         <ac:link><ri:user ri:account-id="<id>" /></ac:link>; an unresolvable markup id ->
         ValueError before gated_call; an unresolvable raw <ri:user> already in the body is
         listed as "unknown account <id>" and the write proceeds; a body with no mentions makes
         no resolve_user_names call and has no "Mentions" row.
      3. Run ruff check .; python3 -m pytest tests/unit/connectors/test_confluence_connector.py
         tests/unit/test_docs_tools_reference.py tests/unit/test_code_no_history.py -q.
      Stop condition: if test_docs_tools_reference.py fails, a first sentence changed; revert it.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_confluence_connector.py tests/unit/test_docs_tools_reference.py -q passes
      - grep -n "markup_to_storage" src/privacyfence/connectors/confluence.py matches
      - ruff check . passes

  - id: p7-retire-plan
    title: ADRs 0115-0117, reference docs, changelog, live check, and delete the plan
    depends_on: [p5-jira-mention-writes, p6-confluence-mention-writes]
    complexity: S
    touches:
      - docs/adr/0115-atlassian-account-ids-resolve-through-a-lazy-shared-cache.md
      - docs/adr/0116-an-approver-sees-atlassians-name-for-every-mentioned-or-assigned-account.md
      - docs/adr/0117-atlassian-find-users-is-auto-approved-and-returns-no-email.md
      - docs/adr/README.md
      - docs/atlassian-setup.md
      - docs/connector-qa.md
      - CHANGELOG.md
      - docs/atlassian-user-names-plan.md
      - docs/atlassian-user-names-plan-manual-steps.html
      - src/privacyfence/atlassian_users.py
      - src/privacyfence/jira_client.py
      - src/privacyfence/confluence_client.py
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/connectors/confluence.py
    brief: |
      1. Write the three ADRs from this plan's "ADRs" section using docs/adr/README.md's template,
         Status "Accepted", Date today. Context/Alternatives come from Design D1 (0115), D8/D9
         (0116) and D10 (0117). Link source files (src/privacyfence/atlassian_users.py,
         connectors/jira.py, connectors/confluence.py) and commit 52768c86 for the Slack model;
         never link this plan. If 0115-0117 are taken on main by then, use the next free numbers
         and rename the files, updating every "ADR 011x" reference in the src files listed in
         touches. Section labels such as "D1" or "D8" are this plan's; do not copy them into ADRs
         or docs.
      2. docs/adr/README.md: add three index rows.
      3. docs/atlassian-setup.md: add a new "## Names and mentions" section between "## Values"
         and "## Build and distribute the bundle":
         Jira's user API (read:jira-user) resolves names for both products; names are cached per
         user in atlassian_user_cache.json for 7 days; jira_refresh_user_cache /
         confluence_refresh_user_cache re-fetch them; the agent mentions with @[Name](accountId)
         and finds ids with jira_find_users / confluence_find_users; a Confluence-only site
         shows raw account ids and cannot use mention markup. Add a Troubleshooting entry:
         "Mentions show raw account ids, or a write fails with 'Unknown Atlassian account id'" ->
         the site has no Jira, or the "Browse users and groups" global permission was removed
         for the user.
      4. docs/connector-qa.md: in the Jira checks and Confluence checks lists, add one line each
         for reading an item with a mention and for jira_find_users / confluence_find_users.
      5. CHANGELOG.md under ## [Unreleased] (create an "### Added" subheading there if missing;
         never a version heading), one entry: Jira and Confluence show @mentioned people and
         Confluence page authors by name, through a shared cache that looks each person up once
         a week at most; new jira_find_users, confluence_find_users, jira_refresh_user_cache and
         confluence_refresh_user_cache tools; the agent can @mention people in Jira comments and
         descriptions and Confluence pages, and assign Jira issues, with the approval card naming
         each person.
      6. Delete docs/atlassian-user-names-plan.md and docs/atlassian-user-names-plan-manual-steps.html;
         grep -rn "atlassian-user-names-plan" . --exclude-dir=.git must print nothing.
      7. Run python3 scripts/generate_tools_reference.py and confirm no diff; run
         python3 -m pytest tests/unit/test_docs_no_history.py tests/unit/test_build_site.py
         tests/unit/test_code_no_history.py -q; run the full §2.7 gate (/dod).
      8. Dispatch connector-live-check.yml against feature/atlassian-user-names with the GitHub
         MCP actions_run_trigger tool (it is on main already), wait for it, and put the run URL
         and pass/fail for jira and confluence in your final report so the PR can link it. This
         check proves the existing Jira/Confluence calls still work; it makes no user lookup
         (manual_after ma1 covers that). Do not dispatch qa-record-fixture.yml. If the jira or
         confluence check fails, stop with status=blocked and paste the failing lines.
    acceptance:
      - ls docs/adr/0115-*.md docs/adr/0116-*.md docs/adr/0117-*.md succeeds (or the renumbered files named in the report)
      - grep -rn "atlassian-user-names-plan" . --exclude-dir=.git prints nothing
      - grep -n "atlassian_user_cache.json" docs/atlassian-setup.md matches
      - python3 -m pytest tests/unit/test_code_no_history.py tests/unit/test_docs_tools_reference.py tests/unit/test_docs_no_history.py tests/unit/test_build_site.py -q passes
      - the final report contains the connector-live-check.yml run URL
```
