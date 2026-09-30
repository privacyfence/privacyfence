# Tool definition quality plan

## Goal

Glama rates PrivacyFence's listing **B** on its Tool Definition Quality Score (TDQS). The score
measures how well each tool's description and input schema help an AI agent pick the right tool
and call it correctly. For v5.3.0 it evaluated 122 tools: 86 A, 27 B and 9 C, average 3.8/5. The
weakest areas, averaged over all tools, were **Parameters** (2.9/5) and **Usage Guidelines**
(3.2/5). The reviewer's comments on the B and C tools repeat three gaps:

1. parameters with no description;
2. no sentence saying what the tool returns (fields, count limits, paging, order);
3. no advice on when to use a similar tool instead.

After this change, every connector tool describes every parameter, says what it returns, and
points to the related tool an agent could confuse it with. A test enforces this. This helps
every AI client, not just Glama's score, because clients read these descriptions to choose tools.
Glama re-scores only after the next release that carries the change. Cutting that release is
outside this plan (see the Risks section).

## Current state

- Tool definitions are `ToolSpec`/`ToolParam` dataclasses (`src/privacyfence/connector.py:17-39`)
  returned by each connector's `tool_specs()` (`src/privacyfence/connectors/*.py`).
  `web/mcp_tools.py:65-95` turns them into the MCP `Tool`. A `ToolParam.description` becomes the
  property's JSON Schema `description`, and an empty one is left out (`_param_schema`, line 65).
- Glama lists the same definitions through `catalog_server.py` → `connector_catalog.catalog_tools()`
  (ADR 0114). The 8 `privacyfence_*` meta-tools (`web/mcp_tools.py`) scored 4.6–4.9 and are
  out of scope.
- Connector tools, measured on `origin/main` at `5e99d00e` with D1's own rules. Counts leave out
  the `reason` parameter, which every tool already describes. "Failing" means no description, one
  under 20 characters, or one that only repeats the name: existing short descriptions have to be
  rewritten too.

  | Connector | Tools | Params failing D1 | Tools without the word `Returns` |
  |---|---|---|---|
  | apps_script | 4 | 5 / 6 | 4 |
  | calendar | 14 | 34 / 54 | 13 |
  | confluence | 10 | 15 / 22 | 8 |
  | contacts | 7 | 17 / 24 | 6 |
  | drive | 23 | 45 / 80 | 23 |
  | gmail | 20 | 54 / 85 | 20 |
  | jira | 8 | 13 / 19 | 7 |
  | salesforce | 4 | 3 / 7 | 3 |
  | slack | 11 | 14 / 21 | 9 |
  | tasks | 8 | 19 / 20 | 8 |
  | telegram | 5 | 7 / 7 | 5 |
  | **total** | **114** | **226 / 345** | **106** |

- `src/privacyfence/connectors/tasks.py:48-126` is typical: `"List tasks in a task list.
  Auto-approved."` and parameters with no description at all. It scored 2.6, the lowest.
- The approval wording already matches the gate for all 114 tools: every tool gated `auto` in
  `auto_accept.TOOL_TO_GATE` contains `Auto-approved`, and every `review` or `popup` tool contains
  `Requires user approval` (42 + 21 + 51).
- Constraints already enforced by tests:
  - `tests/unit/web/test_tool_schema_portability.py`: descriptions are non-empty and at most 1024
    characters (OpenAI's limit).
  - `tests/unit/test_docs_tools_reference.py`: `docs/tools-reference.md`, which
    `scripts/generate_tools_reference.py` renders from the *first sentence* of each description,
    matches a fresh render.
  - `tests/unit/connectors/test_gmail_connector.py:1695`: `include_signature`'s description
    contains `signature`.
- Six descriptions are already over 750 characters: `drive_upload_file` 984,
  `drive_write_doc_content` 909, `confluence_download_attachment` 878,
  `gmail_download_attachment` 871, `drive_get_file_content` 822, `drive_download_file` 815.

## Design

### D1. The completeness check (`tests/helpers.py`)

Add this to `tests/helpers.py`, next to the other shared helpers (§2.5 of the coding guidelines).
Add `import re` to the module's imports, and add `TOOL_TO_GATE` to the existing
`from privacyfence.auto_accept import ReviewContext` line. Sibling names are matched as whole
words, so `gmail_reply_draft_with_attachments` does not count as a mention of `gmail_reply_draft`.

```python
MIN_PARAM_DESCRIPTION_CHARS = 20


def assert_tool_definitions_complete(
    connector: Connector, siblings: dict[str, tuple[str, ...]],
) -> None:
    """Fails, listing every gap at once, unless each of ``connector``'s tools tells an AI client
    what it needs to pick and call it: a description for every parameter, a ``Returns`` sentence,
    the approval wording its gate implies, and a mention of each related tool in ``siblings``
    (keyed by tool name) that the client could confuse it with."""
    specs = {spec.name: spec for spec in connector.tool_specs()}
    problems = [f"siblings names unknown tool {name!r}" for name in sorted(set(siblings) - set(specs))]
    for name, spec in specs.items():
        for param in spec.params:
            text = param.description.strip()
            if param.name != "reason" and (len(text) < MIN_PARAM_DESCRIPTION_CHARS or text.lower() == param.name.lower()):
                problems.append(f"{name}.{param.name}: no useful parameter description")
        if "Returns" not in spec.description:
            problems.append(f"{name}: no 'Returns' sentence")
        if len(spec.description) > 1024:
            problems.append(f"{name}: description is {len(spec.description)} characters, over 1024")
        wording = "Auto-approved" if TOOL_TO_GATE[name] == "auto" else "Requires user approval"
        if wording not in spec.description:
            problems.append(f"{name}: description lacks {wording!r}")
        for other in siblings.get(name, ()):
            if other not in specs:
                problems.append(f"{name}: sibling {other!r} is not a tool of this connector")
            elif not re.search(rf"\b{re.escape(other)}\b", spec.description):
                problems.append(f"{name}: description never mentions {other}")
    assert not problems, "\n".join(problems)
```

Each connector's test module gets this class, placed after `TestDispatch`. `<Name>Connector` and
the module constant are per connector, and the constant holds that connector's rows from D4:

```python
class TestToolDefinitions:
    """What an AI client reads to choose and call these tools: every parameter described, what
    each tool returns, the approval wording its gate implies, and the related tool to use
    instead. Glama's Tool Definition Quality Score grades exactly this."""

    def test_every_tool_definition_is_complete(self):
        assert_tool_definitions_complete(<Name>Connector(MagicMock()), <NAME>_SIBLINGS)
```

`<NAME>_SIBLINGS` is a module-level `dict[str, tuple[str, ...]]` defined right above the class
(`TASKS_SIBLINGS`, `GMAIL_SIBLINGS`, ...). Every connector test module already imports `MagicMock` and
`pytest`, and imports helpers as `from ...helpers import ...`: add `assert_tool_definitions_complete`
to that line.

### D2. How a tool description is written

Keep this order:

1. **The first sentence stays byte-for-byte as it is.** `docs/tools-reference.md` and the website
   render it, and `test_docs_tools_reference.py` fails if it changes. "First sentence" means what
   `scripts/generate_tools_reference.py`'s `first_sentence()` returns. It skips `e.g. `, `i.e. `,
   `etc. ` and `vs. `, and cuts a sentence over its length cap at `: `. So calendar_get_event_details,
   calendar_list_colors, contacts_get, drive_upload_file, jira_get_issue and jira_transition_issue
   have an `e.g.` inside their first sentence, and drive_write_doc_content's rendered sentence ends
   at its first `: `. Never edit text inside that span.
2. The existing detail sentences stay. You may reword them in only two cases: to fit under 1024
   characters (D5), or to turn "comes back as" into a `Returns` sentence. Never drop a behavioural
   fact while rewording.
3. **One `Returns` sentence** (it must contain the word `Returns`, capital R). It names the shape
   the handler actually returns: the keys of the dict, or "a list of {…}" with the dataclass's
   field names as `self._serialize`/`asdict` emits them, and what a write returns (the created or
   updated object, an id, or a status dict). Read the connector's `call()` branch, the client
   method and its `_parse_*` to get this right. If different branches return different shapes,
   name each one. A privacy filter the tool applies (for example `_redact_notes` in `tasks.py`)
   gets a clause: "notes may be redacted by the user's privacy settings".
4. **Limits, order and paging, exactly as the code has them**: the default and cap of
   `max_results`/`limit`/`count` (read the code for a clamp; say "no cap" only if there is none),
   the order results come in if the code or API fixes it, `has_more`/truncation flags, and
   first-page-only behaviour when the client makes a single list call without a page token. Never
   state a limit the code does not have.
5. **One sentence per sibling in D4**, naming the tool and when to use it: "Use `<tool>` instead
   when/to …", or "Get `<param>` from `<tool>`." Write tool names bare, without backticks, as the
   existing descriptions do.
6. **The approval sentence stays last**, with its current wording (`Auto-approved.`,
   `Auto-approved -- <note>.`, `Requires user approval.`). If an existing description has it in the
   middle (`contacts_update`, `contacts_create`, `slack_send_message`), leave it where it is.

Build long descriptions with parenthesised implicit string concatenation, as
`drive_get_file_content` does (`src/privacyfence/connectors/drive.py:214-227`), wrapped at the
file's existing width.

### D3. How a parameter description is written

At least 20 characters, and not just the parameter's own name. Say, where it applies:

- **What it is, and its format**: an id vs. a name or title, RFC 3339 vs. `YYYY-MM-DD`, A1
  notation, JQL/CQL/Gmail search syntax, a JSON string (and its shape), a comma-separated list.
- **Where the value comes from**: "from tasks_list_task_lists (the id field)". This is the main
  help an agent needs for ids.
- **What empty or the default means**: "Empty leaves the title unchanged", "Default 50, capped at
  100", "Default false: only open tasks". Take the default from the `ToolParam`'s `default` and the
  handler's own fallback, never from memory.
- **Accepted values** for a parameter that takes a fixed set, listed in the text (for example
  `'this' | 'following' | 'all'`). Do not add a JSON Schema `enum` (see "Rejected").

Keep an existing parameter description unless it fails the 20-character rule. You may make it more
precise, but it must still contain any word a test asserts (for example `signature` for
`include_signature`). Leave the `reason` parameter's description as it is.

### D4. Sibling map (the `siblings` argument, per connector)

A tool not listed has no required sibling. Every name listed is a tool of the same connector.

**tasks** (`TASKS_SIBLINGS`)

| Tool | Must mention |
|---|---|
| tasks_list_task_lists | tasks_list_tasks |
| tasks_list_tasks | tasks_get_task |
| tasks_get_task | tasks_list_tasks |
| tasks_create_task | tasks_list_task_lists |
| tasks_update_task | tasks_complete_task |
| tasks_complete_task | tasks_uncomplete_task |
| tasks_uncomplete_task | tasks_complete_task |
| tasks_move_task | tasks_list_task_lists |

**telegram** (`TELEGRAM_SIBLINGS`)

| Tool | Must mention |
|---|---|
| telegram_list_chats | telegram_get_messages, telegram_refresh_chat_cache |
| telegram_get_messages | telegram_search_messages, telegram_list_chats |
| telegram_search_messages | telegram_get_messages |
| telegram_send_message | telegram_list_chats |

**gmail** (`GMAIL_SIBLINGS`)

| Tool | Must mention |
|---|---|
| gmail_list_messages | gmail_list_threads, gmail_get_message |
| gmail_list_threads | gmail_list_messages, gmail_get_thread |
| gmail_get_message | gmail_get_thread |
| gmail_get_thread | gmail_get_message |
| gmail_list_message_attachments | gmail_download_attachment |
| gmail_download_attachment | gmail_list_message_attachments |
| gmail_create_draft | gmail_create_draft_with_attachments, gmail_reply_draft |
| gmail_create_draft_with_attachments | gmail_create_draft |
| gmail_reply_draft | gmail_reply_all_draft, gmail_reply_draft_with_attachments |
| gmail_reply_all_draft | gmail_reply_draft, gmail_reply_all_draft_with_attachments |
| gmail_reply_draft_with_attachments | gmail_reply_draft |
| gmail_reply_all_draft_with_attachments | gmail_reply_all_draft |
| gmail_add_label | gmail_list_labels, gmail_create_label, gmail_remove_label |
| gmail_remove_label | gmail_list_labels, gmail_add_label |
| gmail_create_label | gmail_list_labels |
| gmail_create_filter | gmail_update_filter |
| gmail_update_filter | gmail_list_filters, gmail_create_filter |

**drive** (`DRIVE_SIBLINGS`)

| Tool | Must mention |
|---|---|
| drive_list_files | drive_list_folder, drive_get_file_metadata |
| drive_list_folder | drive_list_files |
| drive_get_file_metadata | drive_get_file_content |
| drive_get_file_content | drive_download_file |
| drive_download_file | drive_get_file_content |
| drive_create_blank_file | drive_sheets_create, drive_write_doc_content, drive_upload_file |
| drive_write_file_content | drive_write_doc_content, drive_upload_file |
| drive_upload_file | drive_write_file_content |
| drive_write_doc_content | drive_docs_edit_content |
| drive_docs_edit_content | drive_write_doc_content, drive_docs_format_content |
| drive_docs_format_content | drive_docs_edit_content |
| drive_move_file | drive_list_folder |
| drive_list_shared_drives | drive_list_folder |
| drive_sheets_create | drive_sheets_add_sheet |
| drive_sheets_get_metadata | drive_sheets_get_values |
| drive_sheets_get_values | drive_sheets_get_metadata |
| drive_sheets_write_range | drive_sheets_format_range |
| drive_sheets_add_sheet | drive_sheets_create, drive_sheets_get_metadata |
| drive_sheets_rename_sheet | drive_sheets_get_metadata |
| drive_sheets_insert_dimensions | drive_sheets_delete_dimensions |
| drive_sheets_delete_dimensions | drive_sheets_insert_dimensions |

**calendar** (`CALENDAR_SIBLINGS`)

| Tool | Must mention |
|---|---|
| calendar_list_calendars | calendar_list_events |
| calendar_list_events | calendar_get_event_details, calendar_list_calendars |
| calendar_get_event_details | calendar_list_events |
| calendar_get_free_busy | calendar_list_events |
| calendar_create_event | calendar_create_out_of_office, calendar_list_rooms, calendar_list_colors |
| calendar_update_event | calendar_set_event_color, calendar_set_event_visibility |
| calendar_delete_event | calendar_update_event |
| calendar_create_out_of_office | calendar_create_event |
| calendar_set_working_location | calendar_create_out_of_office |
| calendar_get_event_visibility | calendar_set_event_visibility |
| calendar_set_event_visibility | calendar_get_event_visibility |
| calendar_set_event_color | calendar_list_colors |
| calendar_list_rooms | calendar_create_event |

**contacts** (`CONTACTS_SIBLINGS`)

| Tool | Must mention |
|---|---|
| contacts_list | contacts_search |
| contacts_search | contacts_list, contacts_get |
| contacts_get | contacts_search |
| contacts_create | contacts_search |
| contacts_update | contacts_get |
| contacts_add_label | contacts_remove_label |
| contacts_remove_label | contacts_add_label |

**apps_script** (`APPS_SCRIPT_SIBLINGS`)

| Tool | Must mention |
|---|---|
| apps_script_list_projects | apps_script_get_content |
| apps_script_get_content | apps_script_write_content |
| apps_script_write_content | apps_script_get_content |
| apps_script_get_execution_log | apps_script_list_projects |

**slack** (`SLACK_SIBLINGS`)

| Tool | Must mention |
|---|---|
| slack_list_channels | slack_list_dms, slack_list_group_chats |
| slack_list_dms | slack_list_channels, slack_list_group_chats |
| slack_list_group_chats | slack_list_dms, slack_create_group_chat |
| slack_get_channel_history | slack_get_thread_replies, slack_search_messages |
| slack_get_thread_replies | slack_get_channel_history, slack_resolve_permalink |
| slack_search_messages | slack_get_channel_history |
| slack_resolve_permalink | slack_get_thread_replies |
| slack_create_group_chat | slack_send_message |
| slack_send_message | slack_create_group_chat |

**confluence** (`CONFLUENCE_SIBLINGS`)

| Tool | Must mention |
|---|---|
| confluence_list_spaces | confluence_list_pages |
| confluence_search | confluence_cql_search, confluence_get_page |
| confluence_cql_search | confluence_search |
| confluence_list_pages | confluence_search, confluence_get_page |
| confluence_get_page | confluence_get_page_by_title |
| confluence_get_page_by_title | confluence_get_page |
| confluence_create_page | confluence_update_page |
| confluence_update_page | confluence_get_page |
| confluence_list_attachments | confluence_download_attachment |
| confluence_download_attachment | confluence_list_attachments |

**jira** (`JIRA_SIBLINGS`)

| Tool | Must mention |
|---|---|
| jira_list_projects | jira_search_issues |
| jira_search_issues | jira_get_issue |
| jira_get_issue | jira_search_issues |
| jira_create_issue | jira_list_projects |
| jira_update_issue | jira_transition_issue |
| jira_add_comment | jira_update_issue |
| jira_get_transitions | jira_transition_issue |
| jira_transition_issue | jira_get_transitions |

**salesforce** (`SALESFORCE_SIBLINGS`)

| Tool | Must mention |
|---|---|
| salesforce_list_reports | salesforce_run_report |
| salesforce_run_report | salesforce_list_reports |
| salesforce_search | salesforce_get_record |
| salesforce_get_record | salesforce_search |

### D5. Staying under 1024 characters

For the six long descriptions listed in Current state, and any description the new sentences push
over 1024 characters, add the `Returns` sentence and the sibling sentences first. Then shorten
the *other* non-first sentences of that one description until it fits: drop examples and
parentheticals before you drop behaviour. Keep a margin: aim for at most 1000 characters.

### D6. The worked example: the tasks connector, word for word

Phase p1 applies exactly this text. The later phases copy its style. Descriptions (the first
sentence is unchanged in each):

- **tasks_list_task_lists**: `"List all Google Task lists. Returns a list of {id, title,
  updated}. Pass an id as task_list_id to tasks_list_tasks and the other task tools.
  Auto-approved."`
- **tasks_list_tasks**: `"List tasks in a task list. Returns a list of tasks as {id,
  task_list_id, title, notes, due, status ('needsAction' or 'completed'), completed, updated,
  position, parent, deleted}, only the first page the Tasks API sends (up to 20 tasks), in the
  API's order; notes may be redacted by the user's privacy settings. Use tasks_get_task instead
  when you already have a task's id. Auto-approved."`
- **tasks_get_task**: `"Fetch a single task by id. Returns one task in the same shape
  tasks_list_tasks lists; deleted is true for a task deleted but not yet purged, and notes may be
  redacted by the user's privacy settings. Get the id from tasks_list_tasks. Auto-approved."`
- **tasks_create_task**: `"Create a new task. Returns the created task, with its new id, in
  the same shape tasks_get_task returns. Get task_list_id from tasks_list_task_lists. Requires user
  approval."`
- **tasks_update_task**: `"Update a task's title, notes, or due date. Only the fields you pass
  non-empty change: an empty value leaves that field as it is, so this tool cannot clear notes or a
  due date. Returns the updated task in the same shape tasks_get_task returns. Use
  tasks_complete_task or tasks_uncomplete_task to change whether it is done. Requires user
  approval."`
- **tasks_complete_task**: `"Mark a task as completed. Returns the updated task in the same
  shape tasks_get_task returns, with status 'completed' and the completion time in completed. Use
  tasks_uncomplete_task to undo it. Requires user approval."`
- **tasks_uncomplete_task**: `"Mark a task as not completed. Returns the updated task in the same
  shape tasks_get_task returns, with status 'needsAction' and completed cleared. Use
  tasks_complete_task for the opposite. Requires user approval."`
- **tasks_move_task**: `"Move a task from one list to another. Google Tasks cannot move a task
  between lists, so this copies its title, notes and due date into the destination list and then
  deletes the original: the task gets a new id, comes back not completed, and its subtasks and
  position are not copied. Returns the new task in the same shape tasks_get_task returns. Get both
  list ids from tasks_list_task_lists. Requires user approval."`

Parameter descriptions (`reason` unchanged):

| Tool(s) | Param | Description |
|---|---|---|
| all that take it | task_list_id | `"Id of the task list, from tasks_list_task_lists (its id field, not its title)."` |
| tasks_list_tasks | show_completed | `"Include tasks completed through the API. Default false: only tasks still to do. Tasks completed in Google's own Tasks, Gmail or Calendar apps are hidden and are not returned either way."` |
| get, update, complete, uncomplete | task_id | `"Id of the task, from tasks_list_tasks (its id field)."` |
| tasks_create_task | title | `"Title of the new task, as shown in Google Tasks. Must not be empty."` |
| tasks_create_task | notes | `"Free-text notes for the task (its description). Empty means no notes."` |
| tasks_create_task | due | `"Due date as an RFC 3339 timestamp, e.g. '2026-10-15T00:00:00Z'; Google Tasks keeps only the date. Empty means no due date."` |
| tasks_update_task | title | `"New title for the task. Empty leaves the title unchanged."` |
| tasks_update_task | notes | `"New notes, replacing the current ones. Empty leaves the notes unchanged."` |
| tasks_update_task | due | `"New due date as an RFC 3339 timestamp, e.g. '2026-10-15T00:00:00Z'; only the date is kept. Empty leaves the due date unchanged."` |
| tasks_move_task | source_list_id | `"Id of the list the task is in now, from tasks_list_task_lists."` |
| tasks_move_task | task_id | `"Id of the task to move, from tasks_list_tasks on the source list."` |
| tasks_move_task | destination_list_id | `"Id of the list to move the task to, from tasks_list_task_lists."` |

These facts were checked against `src/privacyfence/tasks_client.py`:
- `list_tasks` makes one `tasks.list` call with no `pageToken` and no `maxResults`, so it gets the
  API's default page of 20 tasks. `list_task_lists` does the same with `tasklists.list`, whose
  default page is 1000 lists, so the description makes no paging claim for lists.
- `list_tasks` sends `showCompleted` but never `showHidden`. Google hides tasks completed in its
  own apps unless `showHidden` is true, so `show_completed=true` returns only tasks completed
  through the API.
- `update_task` receives `title or None`, `notes or None` and `due or None` from
  `connectors/tasks.py`, and `None` keeps the existing value.
- `move_task` inserts `{title, notes, due}` into the destination and deletes the source.
- Reads pass through `_redact_notes`.

### Rejected

- **JSON Schema `enum` on fixed-choice parameters.** It would help Glama's Parameters score, but it
  changes `ToolParam`'s shape (`to_dict`/`from_dict`, `mcp_tools._param_schema`, the portability
  test), and several connectors accept aliases outside a strict set (`calendar_set_event_color`
  takes a color id or a name). A client that enforces `enum` would reject calls that work today.
  Listing the values in the description gets most of the benefit with none of that risk.
- **One shared `REASON_PARAM` constant** in place of the 114 copies of the `reason` parameter. It is
  a good cleanup, but it does not affect the score, and it would put every connector file into one
  phase.
- **Rewriting first sentences.** Several are thin ("Create a new task."). But they are the
  published tools reference and the website's tool table, and the added sentences carry the
  missing information without churning those.
- **Fixing the tasks list paging** (first page only). It is a behaviour change with its own tests.
  This plan describes the current behaviour truthfully; see the Risks section.

## ADRs

- **ADR 0115: Tool definitions carry their parameter, return and routing guidance in prose, and a
  test enforces it.** Every connector parameter has a description, every tool description has a
  `Returns` sentence and names the related tool to use instead, and the first sentence stays fixed
  as the published summary. Rejected: JSON Schema `enum` for fixed choices (clients that enforce it
  would reject aliases that work today, such as a color name in `calendar_set_event_color`), and
  rewriting first sentences (they are the published tools reference). Written in p8 from the
  Design and Rejected sections above. It links to `tests/helpers.py` and this change's PR, never
  to this plan.

## Manual steps

None before or during implementation. `connector-live-check.yml` is required by §2.7 for a PR
that touches `src/privacyfence/connectors/**`, and it is in the steward dispatch table, so phase
p8 dispatches it.

After the feature PR merges, Glama re-scores only on a new release. Cutting one is the
maintainer's call through `/cut-release`, and it is not part of this plan or its PR.

## Risks and open questions

- **A description claim that the code contradicts.** The worker must read the handler and the
  client for every `Returns` and limit sentence. If the code does something surprising (a limit
  that is silently ignored, a shape that differs from the dataclass), describe what the code does.
  If it looks like a bug, add a line to the phase's result note for the PR description, and do not
  fix it in this plan.
- **Tasks paging and hidden tasks.** `tasks_list_tasks` returns only the first page (up to 20
  tasks). It also never sends `showHidden`, so tasks completed in Google's own apps never come
  back. The PR description should suggest a follow-up issue for both. The same first-page pattern may
  exist in other clients. Record each one found in the same way.
- **A first sentence that has to change** because it is wrong, not just thin. Stop with
  `status=blocked` and name the tool. Changing it regenerates `docs/tools-reference.md`, which
  every phase shares.
- **Approval wording that does not match the gate.** All 114 match today, so a failure here means
  the worker edited the approval sentence. Stop with `status=blocked` rather than change
  `TOOL_TO_GATE`.
- **A test that asserts exact description text.** Only `test_gmail_connector.py:1695` does, today.
  If any other test fails on a description string, stop with `status=blocked`. Do not change
  that test.
- **Glama's grading is a model's judgement.** The check in D1 enforces the three gaps the report
  names. It cannot guarantee an A. The appendix lists the reviewer's per-tool comments, so a phase
  can target them.

## Implementation manifest

```yaml
plan_slug: tool-definition-quality
feature_branch: feature/tool-definition-quality
max_parallel: 2
verify_after_merge:
  - python3 -m pytest tests/unit/connectors tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/test_connector_catalog.py tests/unit/test_catalog_server.py -q
  - python3 scripts/generate_tools_reference.py && git diff --exit-code docs/tools-reference.md
  - ruff check .
final_checks:
  - docs/tool-definition-quality-plan.md is deleted and nothing links to it (git grep -n tool-definition-quality-plan returns nothing)
  - docs/adr/0115-*.md exists, is Accepted, and is in docs/adr/README.md's index
  - Spot-check 10 tools across at least 5 connectors against their handlers - each Returns sentence names real fields or a status shape, and each stated default or limit matches the code
  - CHANGELOG.md has an [Unreleased] entry for this change and no new version heading
  - docs/tools-reference.md is unchanged against main (git diff --exit-code origin/main -- docs/tools-reference.md)
  - The PR description links the connector-live-check.yml run p8 dispatched, and lists the paging or behaviour oddities phases reported
phases:
  - id: p1-check-and-tasks
    title: Add the tool-definition completeness check and apply it to the tasks connector
    depends_on: []
    complexity: S
    touches:
      - tests/helpers.py
      - src/privacyfence/connectors/tasks.py
      - tests/unit/connectors/test_tasks_connector.py
    brief: |
      Read the plan's Design section D1, D2, D3, D4 (tasks rows) and D6 first.
      1. In tests/helpers.py, add `from privacyfence.auto_accept import TOOL_TO_GATE` next to the
         existing `ReviewContext` import, then add `MIN_PARAM_DESCRIPTION_CHARS` and
         `assert_tool_definitions_complete` exactly as written in D1, after `build_stub_args`.
      2. In src/privacyfence/connectors/tasks.py `tool_specs()`, replace each tool's description
         and add each parameter's `description=` exactly as D6 gives them. Keep the `reason`
         parameters and all other ToolParam fields unchanged. Wrap long strings with parenthesised
         implicit concatenation as drive.py:214-227 does.
      3. In tests/unit/connectors/test_tasks_connector.py, add `assert_tool_definitions_complete` to
         the existing `from ...helpers import ...` line,
         define `TASKS_SIBLINGS` with the D4 tasks rows, and add `class TestToolDefinitions` from D1
         right after `class TestDispatch`, with `TasksConnector(MagicMock())`.
      4. Import `Connector`, `ToolParam` and `ToolSpec` from `privacyfence.connector` in that module.
         In the same class, add `test_the_check_reports_a_missing_parameter_description`: build a
         minimal `Connector` subclass inside the test whose `name` is "tasks" and whose
         `tool_specs()` returns one `ToolSpec(name="tasks_get_task", description="Fetch a task.
         Returns it. Auto-approved.", params=[ToolParam("task_id", "str")])`, whose `call` raises
         NotImplementedError, and assert that `pytest.raises(AssertionError, match="tasks_get_task.task_id")`
         fires when the check runs with `siblings={}`. Give it a docstring saying it guards the
         check itself against passing vacuously.
      5. Run python3 scripts/generate_tools_reference.py. docs/tools-reference.md must not change.
         If it does, a first sentence changed: restore it.
      Do not edit CHANGELOG.md or dispatch connector-live-check.yml: p8 owns both. Report those
      /dod rows as deferred to p8.
      Stop with status=blocked if any test other than those you added fails on description text.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_tasks_connector.py -q passes
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - python3 scripts/generate_tools_reference.py && git diff --exit-code docs/tools-reference.md exits 0
      - grep -n "def assert_tool_definitions_complete" tests/helpers.py prints one line
      - ruff check tests/helpers.py src/privacyfence/connectors/tasks.py tests/unit/connectors/test_tasks_connector.py passes

  - id: p2-gmail
    title: Complete the gmail tool definitions
    depends_on: [p1-check-and-tasks]
    complexity: M
    touches:
      - src/privacyfence/connectors/gmail.py
      - tests/unit/connectors/test_gmail_connector.py
    brief: |
      Read the plan's Design D2, D3, D4 (gmail rows) and D5, the appendix rows for gmail, and the
      p1 commit on the feature branch (git log --grep "Plan-Phase: tool-definition-quality/p1-check-and-tasks") as the
      style to copy.
      1. For each of the 20 tools in src/privacyfence/connectors/gmail.py `tool_specs()`, read its
         `call()` branch, the GmailClient method it reaches (src/privacyfence/gmail_client.py) and
         any `_parse_*`, and extend the description per D2: keep the first sentence exactly, add
         one `Returns` sentence, the limits, order and paging the code has, and one sentence per
         D4 sibling.
      2. Give every parameter except `reason` a description per D3. The `query` parameters use
         Gmail search syntax (say so, with one example such as 'from:alice is:unread'). Say the
         format of `to`/`cc`/`bcc` exactly as the handler parses them. Say what `body` vs
         `body_markdown` does, and that `include_signature`'s text still contains "signature".
      3. Keep gmail_download_attachment at or under 1000 characters per D5.
      4. In tests/unit/connectors/test_gmail_connector.py, add `assert_tool_definitions_complete` to
         the existing `from ...helpers import ...` line, define `GMAIL_SIBLINGS` with the D4
         gmail rows, and add `class TestToolDefinitions` per D1 after `class TestDispatch`.
      5. Run python3 scripts/generate_tools_reference.py. docs/tools-reference.md must not change.
      Do not edit CHANGELOG.md or dispatch connector-live-check.yml: p8 owns both. Report those
      /dod rows as deferred to p8.
      Stop with status=blocked if a first sentence would have to change, or if a test other than
      the one at test_gmail_connector.py:1695 asserts description text and fails.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_gmail_connector.py -q passes
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - python3 scripts/generate_tools_reference.py && git diff --exit-code docs/tools-reference.md exits 0
      - ruff check src/privacyfence/connectors/gmail.py tests/unit/connectors/test_gmail_connector.py passes

  - id: p3-drive
    title: Complete the drive (Drive, Docs and Sheets) tool definitions
    depends_on: [p1-check-and-tasks]
    complexity: M
    touches:
      - src/privacyfence/connectors/drive.py
      - tests/unit/connectors/test_drive_connector.py
    brief: |
      Read the plan's Design D2, D3, D4 (drive rows) and D5, the appendix rows for drive, and the
      p1 commit (git log --grep "Plan-Phase: tool-definition-quality/p1-check-and-tasks") as the style to copy.
      1. For each of the 23 tools in src/privacyfence/connectors/drive.py `tool_specs()`, read its
         `call()` branch, the client method it reaches (src/privacyfence/drive_client.py and the
         Sheets/Docs helpers it calls) and any `_parse_*`, and extend the description per D2.
      2. Give every parameter except `reason` a description per D3: `query` is Drive's `q` search
         syntax (say so, with one example such as "name contains 'budget'"), if the handler passes
         it through as `q` (read the client to confirm, and describe what it actually does
         otherwise). `range_a1` is A1 notation including the tab name. `sheet_id` is the numeric
         tab id from drive_sheets_get_metadata, not the spreadsheet id. `values` is the JSON shape
         the handler parses.
      3. Four drive descriptions are over 800 characters (drive_upload_file 984,
         drive_write_doc_content 909, drive_get_file_content 822, drive_download_file 815). Keep
         every drive description at or under 1000 characters per D5.
      4. In tests/unit/connectors/test_drive_connector.py, add `DRIVE_SIBLINGS` (D4 drive rows) and
         `class TestToolDefinitions` per D1 after `class TestDispatch`.
      5. Run python3 scripts/generate_tools_reference.py. docs/tools-reference.md must not change.
      Do not edit CHANGELOG.md or dispatch connector-live-check.yml: p8 owns both. Report those
      /dod rows as deferred to p8.
      Stop with status=blocked if a first sentence would have to change, or if an existing test
      fails on description text.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_drive_connector.py -q passes
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - python3 scripts/generate_tools_reference.py && git diff --exit-code docs/tools-reference.md exits 0
      - ruff check src/privacyfence/connectors/drive.py tests/unit/connectors/test_drive_connector.py passes

  - id: p4-calendar
    title: Complete the calendar tool definitions
    depends_on: [p1-check-and-tasks]
    complexity: M
    touches:
      - src/privacyfence/connectors/calendar.py
      - tests/unit/connectors/test_calendar_connector.py
    brief: |
      Read the plan's Design D2, D3, D4 (calendar rows), the appendix rows for calendar, and the p1
      commit (git log --grep "Plan-Phase: tool-definition-quality/p1-check-and-tasks") as the style to copy.
      1. For each of the 14 tools in src/privacyfence/connectors/calendar.py `tool_specs()`, read
         its `call()` branch and the client method (src/privacyfence/calendar_client.py), and
         extend the description per D2.
      2. Give every parameter except `reason` a description per D3. Times: state the exact format
         the handler accepts for start_time/end_time/time_min/time_max (RFC 3339 with offset, or
         date-only for all-day events, whatever the handler actually parses), and the default
         window when time_min/time_max are empty. `attendees`, `emails` and `rooms`: the list
         format the handler parses. `recurrence`: the RRULE format the handler passes on.
         `color`: the ids or names calendar_list_colors returns. `scope` and `send_updates`: the
         accepted values listed in the text.
      3. In tests/unit/connectors/test_calendar_connector.py, add `CALENDAR_SIBLINGS` (D4
         calendar rows) and `class TestToolDefinitions` per D1 after `class TestDispatch`.
      4. Run python3 scripts/generate_tools_reference.py. docs/tools-reference.md must not change.
      Do not edit CHANGELOG.md or dispatch connector-live-check.yml: p8 owns both. Report those
      /dod rows as deferred to p8.
      Stop with status=blocked if a first sentence would have to change, or if an existing test
      fails on description text.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_calendar_connector.py -q passes
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - python3 scripts/generate_tools_reference.py && git diff --exit-code docs/tools-reference.md exits 0
      - ruff check src/privacyfence/connectors/calendar.py tests/unit/connectors/test_calendar_connector.py passes

  - id: p5-contacts-apps-script
    title: Complete the contacts and apps_script tool definitions
    depends_on: [p1-check-and-tasks]
    complexity: M
    touches:
      - src/privacyfence/connectors/contacts.py
      - src/privacyfence/connectors/apps_script.py
      - tests/unit/connectors/test_contacts_connector.py
      - tests/unit/connectors/test_apps_script_connector.py
    brief: |
      Read the plan's Design D2, D3, D4 (contacts and apps_script rows), the appendix rows for
      contacts, and the p1 commit (git log --grep "Plan-Phase: tool-definition-quality/p1-check-and-tasks") as the style
      to copy.
      1. For each of the 7 tools in src/privacyfence/connectors/contacts.py and the 4 in
         src/privacyfence/connectors/apps_script.py, read the `call()` branch and client method
         (src/privacyfence/contacts_client.py, src/privacyfence/apps_script_client.py), and extend
         the description per D2.
      2. Give every parameter except `reason` a description per D3. `resource_name` is
         'people/c…' from contacts_search or contacts_list. `source` lists its accepted values and
         default. `emails`/`phones` state the JSON string shape the handler parses. `files` in
         apps_script_write_content states the JSON shape the handler parses, and that it replaces
         the whole file set.
      3. In test_contacts_connector.py add `CONTACTS_SIBLINGS`, and in test_apps_script_connector.py
         add `APPS_SCRIPT_SIBLINGS` (D4 rows), each with `class TestToolDefinitions` per D1 after
         `class TestDispatch`.
      4. Run python3 scripts/generate_tools_reference.py. docs/tools-reference.md must not change.
      Do not edit CHANGELOG.md or dispatch connector-live-check.yml: p8 owns both. Report those
      /dod rows as deferred to p8.
      Stop with status=blocked if a first sentence would have to change, or if an existing test
      fails on description text.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_contacts_connector.py tests/unit/connectors/test_apps_script_connector.py -q passes
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - python3 scripts/generate_tools_reference.py && git diff --exit-code docs/tools-reference.md exits 0
      - ruff check src/privacyfence/connectors/contacts.py src/privacyfence/connectors/apps_script.py tests/unit/connectors/test_contacts_connector.py tests/unit/connectors/test_apps_script_connector.py passes

  - id: p6-slack-telegram
    title: Complete the slack and telegram tool definitions
    depends_on: [p1-check-and-tasks]
    complexity: M
    touches:
      - src/privacyfence/connectors/slack.py
      - src/privacyfence/connectors/telegram.py
      - tests/unit/connectors/test_slack_connector.py
      - tests/unit/connectors/test_telegram_connector.py
    brief: |
      Read the plan's Design D2, D3, D4 (slack and telegram rows), the appendix rows for slack and
      telegram, and the p1 commit (git log --grep "Plan-Phase: tool-definition-quality/p1-check-and-tasks") as the style
      to copy.
      1. For each of the 11 tools in src/privacyfence/connectors/slack.py and the 5 in
         src/privacyfence/connectors/telegram.py, read the `call()` branch and client method
         (src/privacyfence/slack_client.py, src/privacyfence/telegram_client.py), and extend the
         description per D2. For telegram, say how many messages `limit` returns by default and
         its cap, in what order, and whether results are newest-first.
      2. Give every parameter except `reason` a description per D3. `channel_id` and `chat_id`: say
         which list tool they come from and whether a name is accepted (read the resolver).
         `thread_ts`: the parent message's ts, from slack_get_channel_history or
         slack_resolve_permalink. `participant`: the forms the resolver accepts. `days`: the search
         window and its default.
      3. In test_slack_connector.py add `SLACK_SIBLINGS`, and in test_telegram_connector.py add
         `TELEGRAM_SIBLINGS` (D4 rows), each with `class TestToolDefinitions` per D1 after
         `class TestDispatch`.
      4. Run python3 scripts/generate_tools_reference.py. docs/tools-reference.md must not change.
      Do not edit CHANGELOG.md or dispatch connector-live-check.yml: p8 owns both. Report those
      /dod rows as deferred to p8.
      Stop with status=blocked if a first sentence would have to change, or if an existing test
      fails on description text.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_slack_connector.py tests/unit/connectors/test_telegram_connector.py -q passes
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - python3 scripts/generate_tools_reference.py && git diff --exit-code docs/tools-reference.md exits 0
      - ruff check src/privacyfence/connectors/slack.py src/privacyfence/connectors/telegram.py tests/unit/connectors/test_slack_connector.py tests/unit/connectors/test_telegram_connector.py passes

  - id: p7-confluence-jira-salesforce
    title: Complete the confluence, jira and salesforce tool definitions
    depends_on: [p1-check-and-tasks]
    complexity: M
    touches:
      - src/privacyfence/connectors/confluence.py
      - src/privacyfence/connectors/jira.py
      - src/privacyfence/connectors/salesforce.py
      - tests/unit/connectors/test_confluence_connector.py
      - tests/unit/connectors/test_jira_connector.py
      - tests/unit/connectors/test_salesforce_connector.py
    brief: |
      Read the plan's Design D2, D3, D4 (confluence, jira and salesforce rows) and D5, the appendix
      rows for those three, and the p1 commit (git log --grep "Plan-Phase: tool-definition-quality/p1-check-and-tasks") as
      the style to copy.
      1. For each of the 10 confluence, 8 jira and 4 salesforce tools, read the `call()` branch
         and client method (src/privacyfence/confluence_client.py, jira_client.py,
         salesforce_client.py), and extend the description per D2. confluence_search vs
         confluence_cql_search: say what query each sends (read the client) and when CQL is the
         better choice. Keep confluence_download_attachment at or under 1000 characters (D5).
      2. Give every parameter except `reason` a description per D3: `jql` and `cql` with one
         example each, `space_key` vs `page_id` and where each comes from, `body` in HTML storage
         format, `issue_type` and `priority` as names ('Task', 'Bug'; 'High'), `custom_fields` in
         the JSON shape the handler parses, `object_type` as the Salesforce API name ('Account',
         'Opportunity'), `report_id` from salesforce_list_reports.
      3. In the three test modules add `CONFLUENCE_SIBLINGS`, `JIRA_SIBLINGS` and
         `SALESFORCE_SIBLINGS` (D4 rows), each with `class TestToolDefinitions` per D1 after
         `class TestDispatch`.
      4. Run python3 scripts/generate_tools_reference.py. docs/tools-reference.md must not change.
      Do not edit CHANGELOG.md or dispatch connector-live-check.yml: p8 owns both. Report those
      /dod rows as deferred to p8.
      Stop with status=blocked if a first sentence would have to change, or if an existing test
      fails on description text.
    acceptance:
      - python3 -m pytest tests/unit/connectors/test_confluence_connector.py tests/unit/connectors/test_jira_connector.py tests/unit/connectors/test_salesforce_connector.py -q passes
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py tests/unit/test_docs_tools_reference.py tests/unit/connectors/test_readme_manifest_alignment.py -q passes
      - python3 scripts/generate_tools_reference.py && git diff --exit-code docs/tools-reference.md exits 0
      - ruff check src/privacyfence/connectors/confluence.py src/privacyfence/connectors/jira.py src/privacyfence/connectors/salesforce.py tests/unit/connectors/test_confluence_connector.py tests/unit/connectors/test_jira_connector.py tests/unit/connectors/test_salesforce_connector.py passes

  - id: p8-retire
    title: Guard every future connector, ADR 0115, document the rule, changelog, live check, delete the plan
    depends_on: [p2-gmail, p3-drive, p4-calendar, p5-contacts-apps-script, p6-slack-telegram, p7-confluence-jira-salesforce]
    complexity: S
    touches:
      - tests/unit/web/test_tool_schema_portability.py
      - docs/coding-and-testing-guidelines.md
      - docs/adr/0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/tool-definition-quality-plan.md
    brief: |
      1. In tests/unit/web/test_tool_schema_portability.py, add a bare module-level test
         `test_every_connector_tool_definition_is_complete`, parametrized over `CONNECTORS.values()`
         (`ids=list(CONNECTORS)`). It calls `assert_tool_definitions_complete(connector, {})`,
         imported with `from ...helpers import assert_tool_definitions_complete`, so a connector added later cannot ship parameters without descriptions,
         with no Returns sentence, or with the wrong approval wording. Mention it in the module
         docstring's list of limits in one clause.
      2. In docs/coding-and-testing-guidelines.md §3's "Client and connector" table, add a row:
         What = "Every tool description keeps a one-sentence summary first, then a `Returns`
         sentence, the related tool to use instead, and the approval wording its gate implies;
         every parameter but `reason` has a description saying its format, where the value comes
         from and what empty means. Fixed choices are listed in the text, not as a JSON Schema
         `enum`, because some connectors accept aliases a strict client would reject (ADR 0115)." Where =
         "`src/privacyfence/connectors/<name>.py`'s `tool_specs()`". Enforced by = "**enforced**:
         `tests/helpers.py`'s `assert_tool_definitions_complete`, run for every connector by
         `tests/unit/web/test_tool_schema_portability.py`, and with the sibling map by each
         connector's `TestToolDefinitions`". Add to §2.6's checklist item list: "6.
         `TestToolDefinitions`, calling `assert_tool_definitions_complete` with the connector's
         sibling map." Add `assert_tool_definitions_complete` to §2.5's list of shared helpers in
         `tests/helpers.py`, with a one-clause description.
      3. Write docs/adr/0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md
         using the template in docs/adr/README.md ("## Template"), with Status "Accepted —
         <today's date>. Implemented: `tests/helpers.py`'s `assert_tool_definitions_complete`,
         `src/privacyfence/connectors/*.py`'s `tool_specs()`." Decision and Alternatives considered
         come from the plan's ADRs bullet, Design D2/D3 and "Rejected" (the enum, shared
         REASON_PARAM and first-sentence rewrite rejections). Context: Glama's TDQS for v5.3.0 (B,
         Parameters 2.9/5, Usage Guidelines 3.2/5) and that clients choose tools from these
         descriptions. Related: ADR 0114 and ADR 0089. Do not link to the plan document. Add its
         row to the index table at the end of docs/adr/README.md, after 0114, in the same format.
      4. In CHANGELOG.md under `## [Unreleased]`, add a `### Changed` subsection (create it if
         absent) with: "- Every connector tool now describes each of its parameters, says what it
         returns (fields, limits and paging), and names the related tool to use instead, so AI
         clients choose and call the right tool more often." Do not add a version heading.
      5. Delete docs/tool-definition-quality-plan.md. Run `git grep -n tool-definition-quality-plan`.
         It must print nothing.
      6. Dispatch `connector-live-check.yml` against the feature branch (steward dispatch table;
         §2.7 requires it for a PR touching src/privacyfence/connectors/**). Put the run URL in
         the phase result so the PR description links it. A missing credential on the runner is
         not a blocker for this phase. Report it instead.
    acceptance:
      - python3 -m pytest tests/unit/web/test_tool_schema_portability.py -q -k every_connector_tool_definition_is_complete reports 11 passed
      - python3 -m pytest tests/unit -q passes
      - git grep -n tool-definition-quality-plan prints nothing
      - grep -n "assert_tool_definitions_complete" docs/coding-and-testing-guidelines.md prints at least two lines
      - grep -n "0115" docs/adr/README.md prints one index row, and the ADR file's Status line starts with "Accepted"
      - grep -n "tool-definition-quality-plan" docs/adr/0115-*.md prints nothing
      - python3 -c "import re;t=open('CHANGELOG.md').read();u=t.split('## [Unreleased]')[1].split('\n## [')[0];assert 'names the related tool' in u" exits 0
```

## Appendix: Glama's comments on the B and C tools (v5.3.0)

Each row is the reviewer's summary for that tool. The per-dimension comments are in the report
the maintainer has; these summaries name the gap.

| Tool | Grade | Glama's summary |
|---|---|---|
| `calendar_create_event` | C 2.9/5 | The definition states the operation clearly and discloses the approval gate, which is real value beyond the annotations. However, it offers no when-to-use guidance against siblings and no parameter or side-effect context, which is a significant gap for a 12-parameter event-creation tool. |
| `calendar_list_calendars` | B 3.2/5 | A clear, minimal definition of a read-only list operation whose safety profile is already carried by annotations. Its main weaknesses are the absence of routing guidance among many sibling list tools and the unexplained 'Auto-approved' note. |
| `calendar_list_events` | B 3.2/5 | A purpose-clear listing tool that usefully bounds its output fields and discloses auto-approval, well above the annotation baseline. Its main weakness is parameter documentation: with 17% schema coverage on six params, the description should have explained time_min/time_max and max_results but instead spent its words on return fields. |
| `confluence_cql_search` | B 3.2/5 | A terse, accurate definition that names CQL and adds an approval-status hint, but it fails to route the agent between this tool and confluence_search and says nothing about result shape or the max_results parameter. Adequate as a minimum-viable definition, with clear gaps in routing and parameter semantics. |
| `confluence_list_pages` | C 2.9/5 | A terse, adequately clear definition that states what it lists but does little else: no routing against the many search/list siblings, no parameter semantics for space_key or max_results, and no pagination context. Annotations cover the read-only safety profile, so the main weaknesses are guidance and completeness rather than behavioral accuracy. |
| `confluence_search` | B 3.2/5 | A clean, readable definition that names the resource and return shape, with 'Auto-approved' as a useful governance note. Its main gaps are the absence of routing guidance against confluence_cql_search and any description of max_results or pagination behavior. |
| `contacts_remove_label` | B 3.1/5 | A clear, tightly written action statement that correctly signals the approval gate, but it is under-specified for a mutation tool: no usage routing against the sibling contacts_add_label/contacts_update, and two of three required parameters are undocumented in both the schema and the description. |
| `drive_add_comment` | B 3.3/5 | A tight, well-front-loaded definition whose main strength is disclosing the user-approval precondition that annotations do not capture. Its weakness is near-zero parameter guidance against a schema that documents only one of three fields, plus no differentiation or failure-mode context. |
| `drive_create_blank_file` | B 3.0/5 | A clean, well-scoped statement of purpose that is let down by missing usage guidance and near-total silence on parameters and behavior. The 'Auto-approved' note is a genuinely useful addition beyond the annotations, but it does not offset the 25% schema coverage gap. |
| `drive_list_files` | B 3.2/5 | A concise, well-scoped search definition that helpfully declares its return payload and matches its read-only annotations. Its weaknesses are the absence of when-to-use guidance versus sibling Drive tools and any explanation of max_results or pagination behavior. |
| `drive_list_folder` | B 3.0/5 | A clean, well-scoped read tool description whose main weakness is the absence of any routing guidance against the near-identical drive_list_files sibling, plus 33% schema coverage that leaves max_results and the id format undocumented. Annotations already carry the safety profile, so the description's job is mostly done, but parameter and pagination context are thin. |
| `drive_list_shared_drives` | B 3.1/5 | A clean, well-scoped one-liner that identifies the resource and its return fields and adds useful approval context beyond the annotations. Its main gaps are the absence of any when-to-use guidance relative to sibling drive listing tools and no coverage of the undocumented max_results parameter or pagination behavior. |
| `drive_sheets_add_sheet` | C 2.9/5 | A terse definition that correctly identifies the action and usefully discloses an approval gate, but is otherwise thin: no routing guidance versus siblings and no support for the four undocumented parameters. Adequate for the core purpose, insufficient for correct parameterized invocation. |
| `drive_sheets_create` | B 3.3/5 | A concise, clearly-purposed creation tool description that benefits from the 'Auto-approved' signal. It falls short on routing guidance against sibling creation tools and on the two undocumented parameters, leaving the agent to open the schema for the essentials. |
| `drive_sheets_get_metadata` | B 3.0/5 | A concise, specific metadata tool description that clearly says it lists tabs and what fields are returned. It is weakened by no usage guidance versus sibling read tools and by minimal parameter help, while 'Auto-approved' adds useful approval context beyond the read-only annotations. |
| `drive_write_file_content` | B 3.2/5 | A terse, clear-purpose write tool whose main value-add is disclosing the user-approval requirement, which annotations do not convey. It falls short on differentiating from several overlapping write siblings and on explaining overwrite semantics for a non-idempotent mutation. |
| `gmail_add_label` | B 3.1/5 | A concise, clear-purpose definition that correctly names the action and surfaces the approval requirement, which is real behavioral value beyond the annotations. Its main weaknesses are the absence of any usage guidance versus label-related siblings and thin parameter semantics under low schema coverage. |
| `gmail_create_draft` | C 2.9/5 | A terse definition whose only real addition over the structured data is the user-approval requirement. It identifies the action correctly but never distinguishes itself from the reply-draft or with-attachments siblings, and it leaves nine parameters essentially undocumented in prose. Adequate as a label, thin as an instruction. |
| `gmail_get_thread` | B 3.4/5 | A concise, accurate read-tool definition that names its resource and adds the useful 'requires user approval' context absent from the annotations. Its main weaknesses are the absence of any alternative-tool routing and no detail on the required thread_id parameter. |
| `gmail_remove_label` | B 3.2/5 | A terse but accurate definition: it names the operation clearly and correctly reinforces the approval-gated mutation described by the annotations. Its main weakness is parameter coverage — label_name and message_id are undocumented in both schema and description — leaving an agent without enough context to invoke it confidently beyond the obvious fields. |
| `jira_create_issue` | C 2.9/5 | A terse but accurate definition. It correctly conveys the create action and adds the valuable approval-required behavioral note, but provides essentially no when-to-use routing or parameter semantics, leaving the agent dependent on the schema and annotations. |
| `jira_list_projects` | B 3.0/5 | A clean, appropriately short definition that names the resource, the returned fields, and the auto-approval behavior. Its weaknesses are the lack of any when-to-use routing against the many sibling list tools and the untouched max_results parameter, which the schema also fails to document. |
| `jira_search_issues` | B 3.2/5 | A compact, mostly clear definition that names the operation and its return scope, and correctly aligns with its read-only/idempotent annotations. It falls short on usage routing versus sibling Jira tools and on parameter/return detail, which matters given no output schema and only partial schema description coverage. |
| `salesforce_list_reports` | B 3.2/5 | The definition correctly identifies a read-only list operation and is consistent with its annotations, but it is thin: no when-to-use guidance relative to salesforce_run_report, and no detail on result shape or pagination despite the absence of an output schema. The cryptic 'Auto-approved' phrase adds marginal behavioral value but is not explained. |
| `salesforce_run_report` | B 3.0/5 | A terse but accurate definition that names the action and flags the user-approval requirement, consistent with its read-only annotations. Its main weaknesses are the absence of any routing versus salesforce_list_reports and no help on the undocumented report_id parameter. |
| `tasks_create_task` | C 2.9/5 | A terse definition that names the operation cleanly and surfaces the user-approval requirement, which is real value beyond the annotations. However, it provides no when-to-use routing against sibling task tools and does not compensate for 40% schema coverage, leaving the required parameters and their semantics undocumented. |
| `tasks_get_task` | B 3.0/5 | A terse, structurally clean definition whose read-only nature is already covered by annotations; its only added value is the 'Auto-approved' note. It leaves the two required id parameters undocumented, which is a real gap for a tool that requires both a task list id and a task id. |
| `tasks_list_task_lists` | B 3.2/5 | Adequate but thin: it names the resource and action clearly and is concise, while annotations carry the safety profile. It offers no usage guidance or sibling differentiation beyond the implicit contrast with tasks_list_tasks, and the 'Auto-approved' note is the only behavioral context it adds. |
| `tasks_list_tasks` | C 2.6/5 | A minimal definition that names the operation and nothing more. Annotations cover the safety profile and "Auto-approved" is a minor addition, but the description does not compensate for weak parameter coverage or provide any routing guidance among the many sibling list/get task tools. |
| `tasks_move_task` | C 2.9/5 | A clear, well-structured core sentence whose biggest gap is the undocumented parameters and the unelaborated approval workflow. The 'Requires user approval' note adds real value over the annotations, but overall the definition is too sparse for a non-idempotent mutation tool. |
| `tasks_uncomplete_task` | B 3.2/5 | A concise, clear action statement that correctly adds an approval requirement beyond the annotations. However, it provides no usage alternatives and does nothing to document the two undescribed required parameters, leaving meaningful gaps for an agent. |
| `tasks_update_task` | B 3.3/5 | A terse but accurate definition: it names the editable fields and surfaces an approval prerequisite that annotations do not carry. Its main gaps are the absence of usage routing against sibling task tools and undocumented identifier parameters, which the 17% schema coverage does not fill in. |
| `telegram_get_messages` | C 2.9/5 | A concise, correctly scoped fetch tool whose strongest contribution is the user-approval requirement, which annotations alone would not convey. It falls short on usage guidance against sibling retrieval tools and on documenting the limit parameter, so an agent has to guess both the alternatives and the pagination behavior. |
| `telegram_list_chats` | B 3.0/5 | Adequate but thin: it names the resource and returned fields, and the annotations carry the safety profile, so the agent knows this is a safe read. It falls short on usage routing versus sibling list/search tools and on explaining the undocumented `limit` parameter. |
| `telegram_search_messages` | B 3.1/5 | A compact, accurate definition that names the action well and flags the approval requirement, but offers no when-to-use routing against telegram_get_messages or slack_search_messages and adds almost nothing for the undocumented query and limit parameters. Adequate but with clear gaps, landing in the middle tier. |
| `telegram_send_message` | B 3.2/5 | A clear, appropriately terse definition whose main strength is the approval prerequisite, a behavioral fact not captured by annotations. Its weaknesses are the absence of any when-to-use routing (notably against slack_send_message) and thin parameter semantics for a low-coverage schema. |
