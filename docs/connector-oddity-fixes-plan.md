# Connector oddity fixes plan

## Goal

[PR 834](https://github.com/privacyfence/privacyfence/pull/834) completed every connector tool
definition and, while doing so, recorded oddities in the connectors it did not fix. This plan fixes
every one that is a bug or misleading behaviour, so the tool descriptions can stop apologising for
them:

- list and search tools that return one page now read as many pages as `max_results` asks for
  (bounded by a page budget), with the same return shapes as today;
- results that were wrong (Gmail attachments as repr strings, Telegram and Slack missing fields,
  Salesforce unbounded search, unescaped Confluence CQL) are correct;
- filters that ran after truncation now run before it (Slack, contacts);
- writes that could lose data or lie about it are safe (Gmail filter update order, Tasks move and
  clear, contacts JSON, Calendar times, Confluence partial update).

Deliberately **not** fixed (documented limits or new features, decided with the user): Calendar
`rooms` replace-on-update and guest editing, `tasks_list_task_lists` paging, `jira_list_projects`,
Gmail `list_*` skipping a message whose metadata fetch fails (stays, and the description says so),
and any tool-level `page_token` parameter (rejected, see ADR below).

## Current state

Everything below is on `main` (PR 834 merged as `68a4db6b`); line numbers are approximate.

- **No paging anywhere.** No connector tool takes a page token, and no Google client sends
  `pageToken` or reads `nextPageToken`. Only `slack_client.py` walks `response_metadata.next_cursor`
  internally (`list_channels`, `list_dms`, `list_group_chats`; `list_channels` at ~`:513` is the
  model for "filter each page, stop at `max_results` matches").
- **Results shape.** Handlers return plain lists/dicts; `web/mcp_tools.py:113` does
  `json.dumps(value, default=str)`, and a dict result also gets `structuredContent`. A list result
  does not. Changing a list tool to return a dict therefore changes what clients see and is out of
  scope.
- **Description guard.** `tests/helpers.py` `assert_tool_definitions_complete` (ADR 0115) needs, per
  tool: every non-`reason` param described in 20+ characters, a `Returns` sentence, at most 1024
  characters, the gate wording (`Auto-approved` / `Requires user approval`) and each sibling from
  the connector test's sibling map. `docs/tools-reference.md` is generated from each description's
  first sentence by `scripts/generate_tools_reference.py`, and
  `tests/unit/test_docs_tools_reference.py` fails when it is stale.
- Per-connector findings (all confirmed against the code) are in the phase briefs below.

## Design

### D1. Paging (all connectors)

Every client list method that returns one page today follows the pages itself:

- Loop while fewer than `max_results` items are collected **and** the API returns a continuation
  token/link, with a hard budget of `MAX_PAGES = 10` requests per call. Define `MAX_PAGES` as a
  module constant in each client module that pages (no shared module: the clients share no code
  today and the APIs differ).
- Ask each request for `min(remaining, api_page_max)` items where the API allows it.
- Return a bare list truncated to `max_results`, exactly the shape returned now. No new tool
  parameter, no new result key, except the two Slack `cursor` parameters in phase p8.
- Where the API's per-page maximum is smaller than today's `max_results` clamp, keep the clamp.
- The description sentences that say "only the first page" are replaced by
  "Reads up to max_results items across pages." (adapt the noun), and the page budget is not
  mentioned.
- A page that fails raises the client's existing error type, as a single call does today. No
  partial results.

Rejected: a `page_token` parameter plus `{items, next_page_token}` on every list tool. It gives a
client full traversal but turns every bare-list result into a dict (gaining `structuredContent`),
touches each privacy-filter path (`apply_list` works on lists, and the Tasks notes filter would
silently stop applying to a wrapped result) and every list test, for a benefit no client has asked
for. Also rejected: leaving paging as documented limits.

### D2. Description rules for this plan

- Do **not** change any tool's first sentence, except the one named in p4. A changed first sentence
  regenerates `docs/tools-reference.md`, which only p4 may touch.
- Keep each description at 1024 characters or fewer and keep the `Returns`, gate wording and sibling
  mentions `assert_tool_definitions_complete` requires.
- Only p11 edits `CHANGELOG.md` and `docs/adr/`. No other phase touches them.

### D3. Tasks (p1)

- `TasksClient.list_tasks` sends `showCompleted=show_completed` and `showHidden=show_completed`
  (so tasks completed in Google's own apps come back when completed tasks are requested), pages with
  `maxResults=100` and `pageToken`, and returns a list. It gains a `max_results` argument
  (default 100), and the `tasks_list_tasks` tool does **not** gain a parameter: the handler passes
  100.
- `tasks_update_task` gains `clear_notes: bool = False` and `clear_due: bool = False` (descriptions
  of 20+ characters: "Set true to remove the notes; ignored when notes is also given." and the
  same for due). The client switches from `tasks().update` (full PUT rebuilt from a prefetched copy)
  to `tasks().patch(tasklist=, task=, body=)` with only the keys being changed. A key is cleared by
  sending JSON null (as `uncomplete_task` does at `tasks_client.py:~292`). The approval preview shows
  `(cleared)` for a cleared field.
- `TasksClient.move_task` calls `tasks().move(tasklist=source, task=task_id,
  destinationTasklist=destination)` and parses the result under the destination list id. The task
  keeps its id, status, subtasks and position. No new tool parameter. Recurring tasks cannot be
  moved between lists; the API error surfaces as `TasksClientError`. The pinned client is
  google-api-python-client 2.200.0 (`requirements/runtime.lock.txt`), whose bundled discovery
  document has `destinationTasklist`; do not change `pyproject.toml`.
- Update `write_effects.py` (~`:117`, "The previous values are not kept." stays true only for
  non-cleared fields; ~`:120` "Nothing is deleted." becomes true) and the tool descriptions.

### D4. Gmail (p2)

- `gmail_get_message` and `gmail_get_thread` put attachments in results as
  `{"name", "mime_type", "size"}` dicts, the shape `_list_message_attachments` already returns.
  `attachment_id` is dropped from the description (`gmail_download_attachment` keys on name).
- `GmailClient.list_messages` and `list_threads` page per D1 (`pageToken`, `maxResults` per page
  capped by `_clamp_max_results`). A message whose metadata fetch fails is still skipped and logged;
  the description gains "Messages whose metadata cannot be fetched are omitted."
- `GmailClient.update_filter` order becomes: validate; `filters().get` the old filter; if the new
  criteria and action equal the old ones, return the old filter unchanged (no writes); otherwise
  `create` the new filter, then `delete` the old one. If `delete` fails, delete the new filter
  (best effort) and raise `GmailClientError` naming the old and new ids and saying which state
  remains. If `create` fails, the old filter is untouched and the error says so. Update the tool
  description (currently "if creating the new one fails the old one is already gone") and the
  approval `details_text` in `connectors/gmail.py` (`_update_filter`, ~`:1779`).

### D5. Drive (p3)

`list_files`, `list_folder` and `list_shared_drives` add `nextPageToken` to their `fields` masks
(`"nextPageToken, files(...)"`, `"nextPageToken, drives(id,name)"`) and page per D1.
`list_shared_drives` uses `pageSize=min(remaining, 100)` (the API rejects more than 100) while the
tool's `max_results` clamp stays 1000; fix the description that says "capped at 1000" only if it
implies one request.

### D6. Calendar (p4)

- `calendar_get_event_details` first sentence becomes: "Fetch full details of a calendar event
  including attendees, description and location." The `Returns` sentence keeps saying conferencing
  links and attachments are not included. Remove the `drive_get_file_content` hint and the
  Gemini-notes wording. Regenerate `docs/tools-reference.md`.
- `calendar_list_events`: an empty `time_min` becomes `datetime.now(timezone.utc).isoformat()` in the
  connector handler, and the param text says "Empty means now." The client pages per D1
  (`pageToken`, ignore extra pages beyond `max_results`).
- New module-level helper `_require_rfc3339(name: str, value: str, *, need_offset: bool) -> None` in
  `connectors/calendar.py`, raising `ValueError(f"{name} must be an RFC 3339 date-time such as
  2026-10-15T09:00:00+02:00")`. It rejects values `datetime.fromisoformat` cannot parse, rejects
  date-only values (length 10), and when `need_offset` is true rejects values with no offset. Call it
  **before** `gated_call`, in `_list_events` and `_get_free_busy` (`need_offset=True`), and in
  `_create_event`, `_update_event` (only for non-empty values) and `_create_out_of_office`
  (`need_offset=False`). On create and out-of-office, also raise
  `ValueError("end_time must be after start_time")` when both parse and end <= start (a naive value
  is compared as UTC).
- `_set_working_location`: validate `date` with `date.fromisoformat` and `location` against the
  allowed set (the same set the client checks, ~`calendar_client.py:1117`) before `gated_call`,
  raising `ValueError` with the allowed values in the message.

### D7. Contacts (p5)

- `list_contacts` pages per D1; `source` is applied **per page** and paging continues until
  `max_results` matching contacts are found (as `list_channels` does for Slack).
- `search_contacts`: clamp the `searchContacts` `pageSize` to 30; before the first search call send
  one `searchContacts(query="")` warm-up request and ignore its result and errors; fall back to the
  client-side scan **only when `searchContacts` raises `HttpError`**, not when it returns nothing;
  the fallback and the `source="directory"` scan pass `source` to `list_contacts` (so
  `source="personal"` never returns directory-only contacts) and page per D1.
- `_parse_json_list` in `connectors/contacts.py` becomes strict: a non-empty value that is not a JSON
  list of objects each with a non-empty string `value` raises `ValueError("emails must be a JSON
  list such as [{\"value\": \"a@b.com\", \"type\": \"work\"}]")` (and the same for `phones`),
  **before** `gated_call`, following `apps_script.py`'s `_parse_files_json`. Empty string still
  means "unchanged" (update) or "none" (create). `[]` still clears; the update preview shows
  `(cleared)` for a cleared list. Remove "(or invalid JSON)" from the four param descriptions.
- Description fixes: `source` is "contacts with a saved-contact source" / "contacts with a
  Workspace directory source" (a contact can match both) / "no filtering"; the Returns text lists the
  possible `source` values `personal`, `directory`, `both` and `other`; in `contacts_search`,
  `both` (default) searches saved contacts only, and only `directory` scans the directory.

### D8. apps_script and telegram (p6)

- `apps_script_client.list_projects`: add `nextPageToken` to the Drive `fields` mask and page per D1;
  `get_execution_log` pages `listScriptProcesses` per D1 (`pageSize=min(remaining, 50)`).
- `telegram_search_messages` results gain `"chat_id": getattr(m, "chat_id", 0)` (after `id`);
  `telegram_list_chats` results gain `"username": c.username` (empty string when none, no `@`).
  Descriptions say so and say `chat_id` is the value `telegram_get_messages` takes.

### D9. Slack (p7, p8)

- p7: `SlackClient.list_dms` and `list_group_chats` apply `participant` per page, count matches, and
  stop at `max_results` matches, copying `list_channels`. `_search_by_participant` reads history
  pages per conversation: keep `_SEARCH_BY_PARTICIPANT_CONVERSATION_CAP = 10`, add
  `_SEARCH_HISTORY_PAGE_CAP = 5`, and when a query is given keep fetching older pages of that
  conversation (by `latest`) until it has `count` matches or the page cap is hit. The description
  drops "newest first" for the conversation choice and says: reads at most 10 conversations (the
  first 10 Slack lists) and 5 history pages each, so it can return fewer than `count`.
- p8: `slack_get_channel_history` and `slack_get_thread_replies` gain `cursor: str = ""` ("Opaque
  cursor from a previous result's next_cursor; empty starts at the newest messages."). The client
  methods take `cursor`, pass it to `conversations_history` / `conversations_replies`, and return
  `next_cursor` read from `response_metadata.next_cursor` alongside `has_more`. `_message_page_result`
  adds `"next_cursor"` (empty string when none) to the envelope, and its note becomes
  "More messages exist; call again with cursor=<next_cursor>." (no mention of a time range or a
  larger limit). Update `_search_by_participant`'s unpacking. The thread tool's first sentence says
  "all replies" and stays; the Returns sentence explains the cursor.

### D10. Confluence (p9)

- `list_spaces` (`limit` ≤ 250 per request), `list_pages_in_space` (≤ 200) and `list_attachments`
  (≤ 250) follow `raw["_links"]["next"]` per D1 by extracting its `cursor` query parameter and
  sending it back in `params` on the same endpoint. The tool `max_results` clamps become 1000 for
  spaces and pages; `list_attachments` collects up to 500 (default no longer 50), so
  `_download_attachment` finds attachments past the 50th.
- `search`: escape the query for CQL: `query.replace("\\", "\\\\").replace('"', '\\"')`; remove the
  "avoid double quotes" text. `cql_search` still passes its CQL through unmodified.
- `confluence_update_page`: `title` and `body` become `required=False, default=""`. The handler
  raises `ValueError("Provide a new title, a new body, or both")` before `gated_call` when both are
  empty, then uses `current.title` / `current.body` (from the `get_page` call it already makes) for
  whichever is empty, and calls `update_page(page_id, title, body)` unchanged. The preview shows
  `(unchanged)` for an omitted body. An empty body can no longer clear a page; the description says
  so.

### D11. Jira and Salesforce (p10)

- `JiraClient.search_issues` uses `enhanced_jql` with `nextPageToken` (stop on `isLast` or
  `max_results`), page per D1; the tool `max_results` clamp becomes 500.
- `SalesforceClient.search` unscoped: `FIND {term} IN ALL FIELDS LIMIT {max_results}`.
- `SalesforceClient.list_reports` is unchanged (it is a documented 200-row limit).
- `salesforce_search` description: `fields` is `{Id, Name}` with `object_types` set (objects with no
  `Name` field, such as Case, fail the scoped query); unscoped results hold `Id` only; `max_results`
  now applies unscoped too (drop "Applied only when object_types is set").
- Fix the existing client test that feeds an unscoped search a fake `Name`.

## ADRs

- **0116**: List and search tools follow provider pages inside the client, up to the caller's
  `max_results` and a per-call page budget, and keep their bare-list return shape; they do not expose
  a page-token parameter. (Alternatives: `page_token` + `{items, next_page_token}` on every list tool;
  leaving first-page-only.) Number is the next free one after 0115; take the next free number at the
  time p11 runs.

## Manual steps

None. `manual_before` and `manual_after` are empty. The connector changes are covered by the
`connector-live-check.yml` dispatch in `final_checks` (the steward table; not a manual step).

## Risks and open questions

- **Tasks `move`.** If the installed google-api-python-client rejects `destinationTasklist` (a
  `TypeError: Got an unexpected keyword argument`), p1 stops with `status=blocked`. Do not
  fall back to copy + delete.
- **Gmail update no-op comparison.** If `filters().get` returns fields the request shape lacks (label
  ids vs. names), the comparison must normalise both to the same form; if the worker cannot make the
  equality reliable, stop `blocked` instead of skipping the check.
- **Contacts warm-up.** If a test double for `people().searchContacts` is called with an unexpected
  `query=""` first call, update the double; do not drop the warm-up.
- **Slack `get_channel_history` return shape** is consumed by `_search_by_participant` and by the
  connector; any other unpacking of the returned tuple (`grep get_channel_history src tests`) is p7/p8
  territory. If a caller outside `slack.py`/`slack_client.py` exists, stop `blocked`.
- **Confluence `_links.next`** format: if a live fixture shows `next` without a `cursor` query
  parameter, stop `blocked`.
- **Coverage ratchet.** New client code needs tests (`check_coverage_floor.py`); never lower a floor.
- **Live fixtures.** `test_*_client.py::TestLiveFixtureParsing` tests parse recorded provider
  responses; they must keep passing unchanged. If one needs a fixture change, stop `blocked`
  (fixtures are recorded on the runner, not edited by hand).

## Implementation manifest

```yaml
plan_slug: connector-oddity-fixes
feature_branch: fix/connector-oddity-fixes
max_parallel: 2
manual_before: []
manual_after: []
verify_after_merge:
  - python3 -m pytest tests/unit/connectors tests/unit/test_docs_tools_reference.py tests/unit/web/test_tool_schema_portability.py -q
final_checks:
  - docs/connector-oddity-fixes-plan.md is deleted and nothing links to it
  - the ADR from the ADRs section exists, is Accepted, and is in docs/adr/README.md
  - CHANGELOG.md has [Unreleased] entries and no version heading
  - connector-live-check.yml was dispatched against fix/connector-oddity-fixes, succeeded, and its run is linked in the PR body
phases:
  - id: p1-tasks
    title: Tasks - full list, clearable update, native move
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/tasks_client.py
      - src/privacyfence/connectors/tasks.py
      - src/privacyfence/write_effects.py
      - tests/unit/test_tasks_client.py
      - tests/unit/connectors/test_tasks_connector.py
    brief: |
      Implement Design D3 (and D1 for the client) for the Tasks connector.
      1. tasks_client.py `list_tasks`: add `max_results: int = 100`; send `showCompleted=show_completed`,
         `showHidden=show_completed`, `maxResults=min(remaining, 100)`, follow `nextPageToken` with a
         module constant `MAX_PAGES = 10`; return a list truncated to max_results. Keep the list shape.
      2. `update_task`: replace `tasks().update` with `tasks().patch(tasklist=, task=, body=)` containing
         only the changed keys; add `clear_notes: bool = False` and `clear_due: bool = False`
         arguments that put `"notes": None` / `"due": None` in the body; drop the prefetch `get_task`
         used only to rebuild the body if nothing else needs it.
      3. `move_task`: replace get + insert + delete with `tasks().move(tasklist=source_list_id,
         task=task_id, destinationTasklist=destination_list_id).execute()` and parse it under the
         destination list id. Keep the id validation and error wrapping.
      4. connectors/tasks.py: `tasks_update_task` gets the two `clear_*` ToolParams (annotation "bool",
         required False, default False, descriptions per D3) and passes them through; `_update_task`
         preview shows `(cleared)`; `_list_tasks` passes 100 and keeps returning a list (so
         `_redact_notes` still applies; do NOT wrap the result in a dict); rewrite the
         `tasks_list_tasks`, `tasks_update_task` and `tasks_move_task` descriptions (remove "first
         page (up to 20)", "cannot clear", "Google Tasks cannot move a task between lists"; do not
         change first sentences; keep Returns, gate wording, siblings, 1024 limit).
      5. write_effects.py: fix the two lines about previous values and "Nothing is deleted."
         (`grep -n "previous values\|Nothing is deleted" src/privacyfence/write_effects.py`); update
         any test asserting them.
      6. Tests. Update: TestListTasks (client) for the new kwargs and paging (two pages -> one list);
         TestUpdateTask (client) to assert `.patch` bodies, including a clear-notes and a clear-due
         case, and rename `test_due_can_be_explicitly_cleared_by_passing_none_is_not_possible_uses_existing`;
         replace TestMoveTask's insert/delete tests with `.move` tests (kwargs, parsed under the
         destination id, HttpError -> TasksClientError); the connector tests
         `test_list_tasks_passes_show_completed_and_serializes_list`,
         `test_update_task_coerces_empty_strings_to_none`,
         `test_update_task_passes_through_provided_values` for the new call signatures; add one connector
         test that `clear_notes=True` reaches the client and the preview shows `(cleared)`.
         Keep the notes-privacy-filter tests green unchanged.
      Stop with status=blocked if `tasks().move(..., destinationTasklist=...)` raises TypeError under the
      installed client, or if a first sentence of a tools description would have to change.
    acceptance:
      - python3 -m pytest tests/unit/test_tasks_client.py tests/unit/connectors/test_tasks_connector.py -q passes
      - grep -n "insert\|delete" src/privacyfence/tasks_client.py finds no call inside move_task
      - grep -n "showHidden" src/privacyfence/tasks_client.py finds the list_tasks kwarg
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p2-gmail
    title: Gmail - attachment dicts, paging, safe filter update
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/gmail_client.py
      - src/privacyfence/connectors/gmail.py
      - tests/unit/test_gmail_client.py
      - tests/unit/connectors/test_gmail_connector.py
    brief: |
      Implement Design D4 (and D1).
      1. connectors/gmail.py `_get_message` (~:881) and `_get_thread` (~:1001): build
         `[{"name": a.name, "mime_type": a.mime_type, "size": a.size} for a in (attachments or [])]`
         and pass THAT to `apply_list("privacy", "attachments", ...)`; extract one small helper used by
         `_list_message_attachments`, `_get_message` and `_get_thread`. Remove `attachment_id` from the
         `gmail_get_message` description.
      2. gmail_client.py `list_messages` and `list_threads`: page with `pageToken`, `MAX_PAGES = 10`
         module constant, `maxResults=min(remaining, 100)` via `_clamp_max_results`; return the same
         list types truncated to max_results. Keep skipping (and logging) a message whose metadata
         `get` raises HttpError. Update the `gmail_list_messages` / `gmail_list_threads` descriptions:
         remove "first page", add "Messages whose metadata cannot be fetched are omitted." for messages.
      3. `update_filter`: implement the D4 order (get old, no-op if equal, create new, delete old, roll
         back the new one if delete fails, errors naming both ids). Normalise the compared fields the
         same way `create_filter` builds its body. Update the tool description and `_update_filter`'s
         `details_text` (the test at test_gmail_connector.py ~:1345 asserts "deletes the existing
         filter"; change it to the new text you write: "creates the new filter first, then deletes the
         old one").
      4. Tests. Add: connector tests that `gmail_get_message` and `gmail_get_thread` results
         `json.dumps` to dicts (no `Attachment(` in the output); client tests for two-page
         list_messages and list_threads, and for update_filter: creates before deleting (assert call
         order), no-op when unchanged, create failure leaves the old filter, delete failure rolls back
         and names both ids. Replace `test_delete_http_error_becomes_gmail_client_error_and_skips_create`
         and `test_create_http_error_after_delete_reports_original_filter_is_gone`. Update the list
         tests that pin `list_messages.assert_called_once_with("from:alice", 5)` only if the signature
         changed (it should not).
      Stop with status=blocked if the filter equality cannot be made reliable (see plan Risks) or a first
      sentence would have to change.
    acceptance:
      - python3 -m pytest tests/unit/test_gmail_client.py tests/unit/connectors/test_gmail_connector.py -q passes
      - a new test asserts json.dumps(get_message result) contains no "Attachment("
      - a new test asserts filters().create is called before filters().delete in update_filter
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p3-drive
    title: Drive - page the three list tools
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/drive_client.py
      - src/privacyfence/connectors/drive.py
      - tests/unit/test_drive_client.py
      - tests/unit/connectors/test_drive_connector.py
    brief: |
      Implement Design D5 (and D1).
      1. drive_client.py `list_files` (~:1294), `list_folder` (~:1604), `list_shared_drives` (~:2181): add
         `nextPageToken` to the `fields` mask, send `pageToken`, loop with a module constant
         `MAX_PAGES = 10`, `pageSize=min(remaining, 1000)` (100 for shared drives), return the same list
         types truncated to max_results.
      2. connectors/drive.py: rewrite the "only the first page" sentences of drive_list_files,
         drive_list_folder, drive_list_shared_drives (~:180-201, 223-243, 377-392) to "Reads up to
         max_results items across pages."; keep the shared parameter-help constants; do not change first
         sentences.
      3. Tests: for each of the three, a client test with two fake pages (first has `nextPageToken`)
         returning one combined list, one test that stops at max_results, and one that stops at
         MAX_PAGES; shared drives: assert `pageSize` never exceeds 100. Existing tests
         (`TestClampMaxResults`, `TestListFiles`, `TestListFolder`, `TestListSharedDrives`,
         connector `TestAutoTools`) must still pass unchanged apart from the extra `nextPageToken`
         in fields masks they assert.
    acceptance:
      - python3 -m pytest tests/unit/test_drive_client.py tests/unit/connectors/test_drive_connector.py -q passes
      - grep -c "nextPageToken" src/privacyfence/drive_client.py prints at least 3
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p4-calendar
    title: Calendar - details wording, time_min default, time validation, event paging
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/calendar_client.py
      - src/privacyfence/connectors/calendar.py
      - docs/tools-reference.md
      - tests/unit/test_calendar_client.py
      - tests/unit/connectors/test_calendar_connector.py
    brief: |
      Implement Design D6 (and D1 for events.list).
      1. connectors/calendar.py: rewrite the calendar_get_event_details description per D6 (new first
         sentence; drop the Gemini/`drive_get_file_content` wording; keep "conferencing links and
         attachments are not included" in the Returns sentence). Then run
         `python3 scripts/generate_tools_reference.py` and commit the regenerated
         docs/tools-reference.md; the diff must be exactly that one row.
      2. Add `_require_rfc3339` and the end-after-start check per D6 and call them before `gated_call`
         in `_list_events`, `_get_free_busy`, `_create_event`, `_update_event`,
         `_create_out_of_office`. Validate `date` and `location` in `_set_working_location` before the
         gate (read the allowed locations from the client module's existing constant, do not duplicate the list).
      3. `_list_events`: empty `time_min` -> now (UTC, isoformat); update the param text to "Empty means
         now." and the Returns text ("oldest first" wording removed if present).
      4. calendar_client.py `list_events` (~:435): page with `pageToken`, `MAX_PAGES = 10`,
         `maxResults=min(remaining, 250)`; return the same list truncated to max_results; keep
         `singleEvents=True, orderBy="startTime"`.
      5. Tests: update `test_list_events_excludes_description_and_attendees` (the handler now passes a
         non-empty time_min: assert it parses as an aware datetime); rename/replace
         `test_filtered_data_never_carries_conferencing_or_attachments` only if its docstring restates
         the old description (the behaviour it pins stays); add tests: bare date rejected for
         create/list, missing offset rejected for list/free_busy but accepted for create, end <= start
         rejected, bad `date` and bad `location` rejected before the gate (gate mock not called); client
         two-page list_events test. `TestListEvents::test_optional_filters_only_included_when_given`
         must still pass unchanged.
      Stop with status=blocked if the allowed-locations constant does not exist in the client module.
    acceptance:
      - python3 -m pytest tests/unit/test_calendar_client.py tests/unit/connectors/test_calendar_connector.py tests/unit/test_docs_tools_reference.py -q passes
      - git diff --stat main -- docs/tools-reference.md shows one row changed
      - grep -n "drive_get_file_content" src/privacyfence/connectors/calendar.py finds nothing
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p5-contacts
    title: Contacts - per-page source filter, search fixes, strict JSON
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/contacts_client.py
      - src/privacyfence/connectors/contacts.py
      - tests/unit/test_contacts_client.py
      - tests/unit/connectors/test_contacts_connector.py
    brief: |
      Implement Design D7 (and D1).
      1. contacts_client.py `list_contacts`: page `connections().list` with `pageToken`,
         `pageSize=min(max(remaining, 1), 1000)`, `MAX_PAGES = 10`; apply `_matches_source` per page and
         stop at `max_results` matches; keep the list-of-Contact return.
      2. `search_contacts`: `pageSize=min(max_results, 30)`; one warm-up `searchContacts(query="")` call
         (errors ignored) before the first real search; fall back to the client-side scan only on
         HttpError (not on an empty result); pass `source` to the fallback's and the directory path's
         `list_contacts(max_results=1000, source=...)`.
      3. connectors/contacts.py: make `_parse_json_list` strict per D7 (raise ValueError before the gate
         in `_contacts_update` and `_contacts_create`); `(cleared)` in the update preview when the
         parsed list is empty; edit the param descriptions (remove "(or invalid JSON)"; new `source`
         text; Returns lists `personal`, `directory`, `both`, `other`; search's `both` explanation);
         remove "filters after that page is fetched" from contacts_list's text.
      4. Tests. Replace: `TestParseJsonList::test_invalid_json_returns_none` and
         `test_valid_json_but_not_a_list_returns_none` (now raise ValueError), the two connector tests
         `test_invalid_json_emails_falls_back_to_current_value_not_a_bogus_diff` and
         `test_invalid_json_emails_are_dropped_not_shown_and_passed_as_none` (now assert ValueError and
         that the gate/client was not called), `test_falls_back_to_client_side_filter_when_search_returns_empty`
         (now: empty result does NOT fall back). Add: two-page list_contacts with source filter
         reaching max_results; warm-up call is made first; a list of non-objects (`'["a@b.com"]'`) raises
         ValueError; `[]` shows `(cleared)`. Keep `TestSearchContacts` fallback-on-HttpError tests.
    acceptance:
      - python3 -m pytest tests/unit/test_contacts_client.py tests/unit/connectors/test_contacts_connector.py -q passes
      - grep -n "or invalid JSON" src/privacyfence/connectors/contacts.py finds nothing
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p6-appsscript-telegram
    title: apps_script paging and telegram missing fields
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/apps_script_client.py
      - src/privacyfence/connectors/apps_script.py
      - src/privacyfence/connectors/telegram.py
      - tests/unit/test_apps_script_client.py
      - tests/unit/connectors/test_apps_script_connector.py
      - tests/unit/connectors/test_telegram_connector.py
    brief: |
      Implement Design D8 (and D1 for apps_script).
      1. apps_script_client.py `list_projects` (~:206): fields mask `"nextPageToken, files(id,name,createdTime,modifiedTime)"`,
         `pageToken`, `MAX_PAGES = 10`, `pageSize=min(remaining, 1000)`; `get_execution_log` (~:275):
         page `listScriptProcesses` with `pageToken`, `pageSize=min(remaining, 50)`, `MAX_PAGES = 10`.
         Return the same list types truncated to max_results.
      2. connectors/apps_script.py: replace the "only the first page" sentences in
         apps_script_list_projects and apps_script_get_execution_log with "Reads up to max_results
         items across pages."
      3. connectors/telegram.py: add `"chat_id": getattr(m, "chat_id", 0)` to `_search_messages` results
         (after `id`) and `"username": c.username` to `_list_chats` results; update the two descriptions
         (remove "the chat's numeric id is not included"; say chat_id is what telegram_get_messages takes;
         document username as the @handle without the @, empty if none).
      4. Tests: two-page client tests for both apps_script methods; update
         `TestSearchMessages::test_preview_and_result_fields` (add chat_id: 100 from make_message) and
         `TestListChats::test_auto_accepts_and_maps_fields` (add username) in test_telegram_connector.py;
         keep every other test passing unchanged.
    acceptance:
      - python3 -m pytest tests/unit/test_apps_script_client.py tests/unit/connectors/test_apps_script_connector.py tests/unit/connectors/test_telegram_connector.py -q passes
      - grep -n "numeric id is not included" src/privacyfence/connectors/telegram.py finds nothing
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p7-slack-filters
    title: Slack - participant filters before truncation, deeper participant search
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/slack_client.py
      - src/privacyfence/connectors/slack.py
      - tests/unit/test_slack_client.py
      - tests/unit/connectors/test_slack_connector.py
    brief: |
      Implement Design D9 (p7 half). Copy the pattern of `SlackClient.list_channels` (~:513-563) and its
      tests `TestListChannels.test_participant_match_past_max_results_is_still_found` (~:716) and
      `..._found_via_fallback_walk_too` (~:736).
      1. `list_dms` (~:565): with a participant, request `limit=200` pages, filter each page, count
         matches, stop at max_results matches; without one, behave as now.
      2. `list_group_chats` (~:608): compute `allowed_ids` before the loop; filter each page by it; on the
         name-fallback path (`allowed_ids is None`) parse each page with `_map_concurrent` and apply the
         needles per page; stop at max_results matches. Keep the AND semantics of comma-separated
         participants.
      3. `_search_by_participant` (~:836): add `_SEARCH_HISTORY_PAGE_CAP = 5`; when a query is present,
         keep fetching older history pages for a conversation (pass the oldest fetched `ts` as `latest`)
         until it has `count` matches or the cap is hit. Keep the 10-conversation cap.
      4. connectors/slack.py: rewrite the slack_list_dms, slack_list_group_chats (remove "the participant
         filter runs on those first max_results") and slack_search_messages (per D9) descriptions;
         do not change first sentences.
      5. Tests: DM and group-chat participant match beyond max_results raw items (both fast path and
         fallback), search with a query that only matches an older page, cap of 10 conversations
         (assert the 11th is not read) and cap of 5 pages. Existing tests unchanged.
      Do NOT change get_channel_history / get_thread_replies (phase p8 owns them).
    acceptance:
      - python3 -m pytest tests/unit/test_slack_client.py tests/unit/connectors/test_slack_connector.py -q passes
      - grep -n "first max_results" src/privacyfence/connectors/slack.py finds nothing
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p8-slack-cursor
    title: Slack - cursor for channel history and thread replies
    depends_on: [p7-slack-filters]
    complexity: S
    touches:
      - src/privacyfence/slack_client.py
      - src/privacyfence/connectors/slack.py
      - tests/unit/test_slack_client.py
      - tests/unit/connectors/test_slack_connector.py
    brief: |
      Implement Design D9 (p8 half).
      1. slack_client.py `get_channel_history` (~:668) and `get_thread_replies` (~:717): add
         `cursor: str = ""` (sent as `cursor=` only when non-empty) and return `next_cursor` from
         `(response.get("response_metadata") or {}).get("next_cursor") or ""` in addition to
         `has_more`. Update every caller of the changed return shape, including
         `_search_by_participant` (`grep -n "get_channel_history\|get_thread_replies" -r src tests`).
      2. connectors/slack.py: add the `cursor` ToolParam (annotation "str", required False, default "",
         description per D9) to both tools; pass it through; `_message_page_result` (~:55) adds
         `"next_cursor"` and its note becomes
         "More messages exist; call again with cursor=<next_cursor>."; update both descriptions'
         Returns sentences.
      3. Tests: client tests that cursor is sent only when given and next_cursor is returned; connector
         tests for the envelope and the new note; update `test_has_more_surfaces_a_continuation_note`,
         `test_has_more_is_surfaced_to_claude_with_a_note` and `test_has_more_false_carries_no_note`
         to the new envelope; `TestLiveFixtureParsing.test_get_thread_replies_fixture_still_parses`
         must pass unchanged.
      Stop with status=blocked if a caller outside slack.py / slack_client.py unpacks these return values.
    acceptance:
      - python3 -m pytest tests/unit/test_slack_client.py tests/unit/connectors/test_slack_connector.py -q passes
      - grep -n "larger limit\|narrow the time range" src/privacyfence/connectors/slack.py finds nothing
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p9-confluence
    title: Confluence - paging, CQL escaping, partial page update
    depends_on: []
    complexity: M
    touches:
      - src/privacyfence/confluence_client.py
      - src/privacyfence/connectors/confluence.py
      - tests/unit/test_confluence_client.py
      - tests/unit/connectors/test_confluence_connector.py
    brief: |
      Implement Design D10 (and D1 via `_links.next` cursors).
      1. confluence_client.py: add a private helper that, given a v2 response, returns the `cursor` query
         parameter of `raw["_links"]["next"]` (or None); use it in `list_spaces` (per-request limit 250),
         `list_pages_in_space` (200) and `list_attachments` (250, collect up to 500, remove the 50
         default) with `MAX_PAGES = 10`; raise the `confluence_list_spaces` and `confluence_list_pages`
         `max_results` clamps to 1000.
      2. `search`: escape backslash then double quote before building `text ~ "..."`. Leave `cql_search`
         untouched.
      3. connectors/confluence.py: remove the "first page" and "avoid double quotes" sentences; make
         `title` and `body` of `confluence_update_page` `required=False, default=""`; in `_update_page`
         raise ValueError("Provide a new title, a new body, or both") before the gate when both are
         empty, fill the empty one from `current` (the `get_page` result it already fetches), show
         `(unchanged)` for an omitted body in the preview, call `update_page(page_id, title, body)`
         unchanged; update the description to say an empty body no longer clears the page.
      4. Tests: two-page tests for the three list methods (cursor sent on page two); attachments beyond
         the 50th are found by `_download_attachment`; search escaping (a query with `"` and `\`);
         update_page with title only, body only, and neither (ValueError, gate not called). Update
         `test_result_is_serialized_page` only if its call args changed. `TestCqlSearch::test_passes_cql_through_unmodified`
         and `TestLiveFixtureParsing` tests pass unchanged.
      Stop with status=blocked if a recorded live fixture shows `_links.next` without a `cursor` parameter.
    acceptance:
      - python3 -m pytest tests/unit/test_confluence_client.py tests/unit/connectors/test_confluence_connector.py -q passes
      - grep -n "avoid double quotes" src/privacyfence/connectors/confluence.py finds nothing
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p10-jira-salesforce
    title: Jira paging, Salesforce search limit and wording
    depends_on: []
    complexity: S
    touches:
      - src/privacyfence/jira_client.py
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/salesforce_client.py
      - src/privacyfence/connectors/salesforce.py
      - tests/unit/test_jira_client.py
      - tests/unit/connectors/test_jira_connector.py
      - tests/unit/test_salesforce_client.py
      - tests/unit/connectors/test_salesforce_connector.py
    brief: |
      Implement Design D11.
      1. jira_client.py `search_issues` (~:225): call `self._client.enhanced_jql(jql, nextPageToken=..., limit=min(remaining, 100))`
         in a loop (stop on `isLast` or max_results, `MAX_PAGES = 10`); return the same list truncated to
         max_results. Raise the tool clamp in connectors/jira.py to 500 and replace "only the first page"
         wording.
      2. salesforce_client.py `search` unscoped branch (~:418): `FIND {term} IN ALL FIELDS LIMIT {max_results}`
         (int-coerced). Do not touch `list_reports`.
      3. connectors/salesforce.py: reword the salesforce_search Returns text and `max_results` param text
         per D11 (fields shape; max_results applies unscoped too). Keep first sentence.
      4. Tests: two-page jira client test with `nextPageToken`/`isLast`; update
         `test_unscoped_search_builds_plain_find_query` and `test_search_term_is_escaped_in_query` to the
         LIMIT form; change `test_maps_search_records_including_type_from_attributes` so its unscoped fake
         response has no `Name` (real SOSL returns only Id) and assert the result's `fields == {"Id": ...}`;
         connector tests unchanged apart from any wording assertions.
      Stop with status=blocked if `enhanced_jql` is not the method `Jira.jql` delegates to under the
      installed `atlassian` package (check `.venv/**/atlassian/jira/`).
    acceptance:
      - python3 -m pytest tests/unit/test_jira_client.py tests/unit/connectors/test_jira_connector.py tests/unit/test_salesforce_client.py tests/unit/connectors/test_salesforce_connector.py -q passes
      - grep -n "LIMIT" src/privacyfence/salesforce_client.py shows the unscoped FIND with LIMIT
      - python3 -m pytest tests/unit/test_docs_tools_reference.py -q passes with docs/tools-reference.md unmodified
      - ruff check . and python3 scripts/mypy_strict_modules.py pass
  - id: p11-retire
    title: ADR, CHANGELOG, retire the plan
    depends_on: [p1-tasks, p2-gmail, p3-drive, p4-calendar, p5-contacts, p6-appsscript-telegram, p7-slack-filters, p8-slack-cursor, p9-confluence, p10-jira-salesforce]
    complexity: S
    touches:
      - docs/adr/0116-list-tools-page-inside-the-client-and-keep-their-return-shape.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/connector-oddity-fixes-plan.md
    brief: |
      1. Take the next free ADR number in docs/adr/ (0116 unless another branch took it) and write
         `docs/adr/<n>-list-tools-page-inside-the-client-and-keep-their-return-shape.md` using the
         template in docs/adr/README.md: Status Accepted with today's date and "Implemented:" pointing at
         the `MAX_PAGES` loops in `src/privacyfence/*_client.py`; Context (first-page-only lists, ADR 0115
         and PR 834 recorded it); Decision (D1 above); Alternatives (the two rejected in D1, with the
         reasons); Consequences (a client cannot walk past `max_results`; Slack history/replies are the
         exception with a `cursor`); Related ADR 0115. Link no plan document.
      2. Add its row to the index table in docs/adr/README.md.
      3. CHANGELOG.md, under `## [Unreleased]`, add these entries (Fixed unless noted; create the
         subsection headings only if absent, never a version heading): list and search tools across Gmail,
         Drive, Calendar, Contacts, Apps Script, Slack, Confluence and Jira read as many pages as
         max_results asks for; Tasks: completed tasks from Google's apps are listed, notes and due date
         can be cleared, and moving a task keeps its id; Gmail: attachments in message and thread results
         are objects, and a filter update no longer loses the original when it fails; Calendar: empty
         time_min means now and bad times are rejected before approval; Contacts: invalid emails/phones
         JSON is rejected, the source filter no longer drops matches, search no longer scans only 1000;
         Slack: participant filters run before truncation and history/replies accept a cursor;
         Telegram: search results carry chat_id and chats carry username; Confluence: quotes in searches,
         attachments past the 50th, title-only or body-only page updates; Salesforce: unscoped search
         honours max_results.
      4. Delete docs/connector-oddity-fixes-plan.md (`git rm`) and confirm nothing links to it
         (`grep -rn "connector-oddity-fixes-plan" . --include=* ` outside .git finds nothing).
      5. Run the full checks (`/dod` gate rows: pytest with coverage and the ratchet, ruff, bandit, mypy
         strict script).
    acceptance:
      - ls docs/adr | grep -c "list-tools-page-inside-the-client" prints 1 and the file's Status is Accepted
      - grep -n "list-tools-page-inside-the-client" docs/adr/README.md finds the index row
      - grep -n "^## \[" CHANGELOG.md | head -3 shows `## [Unreleased]` above any version heading and no new version heading
      - test ! -e docs/connector-oddity-fixes-plan.md
      - python3 -m pytest tests/unit -q passes
```
