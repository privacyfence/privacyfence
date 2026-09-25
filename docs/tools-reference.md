# Tools reference

<!-- GENERATED FILE. Do not edit by hand: run `python scripts/generate_tools_reference.py` and
commit the result. tests/unit/test_docs_tools_reference.py fails when this file is stale. -->

Every connector tool PrivacyFence offers an MCP client, and the gate each one goes through. This
page is **generated** by
[`scripts/generate_tools_reference.py`](../scripts/generate_tools_reference.py) from the connectors'
own tool definitions and the gate table in
[`src/privacyfence/auto_accept.py`](../src/privacyfence/auto_accept.py). Don't edit it by hand.

A connector's tools appear only after its organization config is installed and you have signed in
to that service (see [Connecting a service](connecting-a-service.md)). The eight `privacyfence_*`
tools that PrivacyFence adds itself are not connector tools and have no gate; they are described in
[How PrivacyFence works](how-it-works.md#privacyfences-own-tools).

## Gates

| Gate | What happens when the AI system calls the tool |
|---|---|
| `auto` | Runs straight away with no card. Still recorded in the audit log. |
| `review` | A read. You see a card showing what would be released before the result goes back to the AI system, unless an auto-accept rule covers the call. |
| `popup` | A write or other change. You approve it on a card before it happens, unless an auto-accept rule covers the call. |

A tool's gate is fixed in code; no setting moves a tool to a different gate. Auto-accept rules,
PII detection, the privacy filter and passkey step-up all act within these gates: see
[Approvals and policy](approvals-and-policy.md). What the **Always allow** button proposes for each
`review` and `popup` tool is listed in the
[Always allow reference](always-allow-rules-reference.md).

**Direction** is what the tool itself declares: `read` tools change nothing in the connected
service; `write` tools create, change or send something. A few `write` tools are `auto` because
they create something empty and disclose nothing (for example `drive_create_blank_file`).

**What it does** is the first sentence of the description the AI system is shown for the tool.

## Summary

| Connector | Tools | `auto` | `review` | `popup` |
|---|---:|---:|---:|---:|
| [Gmail](#gmail) | 20 | 5 | 3 | 12 |
| [Google Drive](#google-drive-including-sheets-and-docs) | 23 | 7 | 3 | 13 |
| [Google Calendar](#google-calendar) | 14 | 6 | 1 | 7 |
| [Google Contacts](#google-contacts) | 7 | 3 | 0 | 4 |
| [Google Tasks](#google-tasks) | 8 | 3 | 0 | 5 |
| [Apps Script](#apps-script) | 4 | 1 | 2 | 1 |
| [Slack](#slack) | 11 | 6 | 3 | 2 |
| [Telegram](#telegram) | 5 | 2 | 2 | 1 |
| [Salesforce](#salesforce) | 4 | 1 | 3 | 0 |
| [Jira](#jira) | 8 | 3 | 1 | 4 |
| [Confluence](#confluence) | 10 | 5 | 3 | 2 |
| **Total** | **114** | **42** | **21** | **51** |

## Gmail

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `gmail_list_filters` | read | `auto` | List all Gmail filters with their criteria and actions. |
| `gmail_list_labels` | read | `auto` | List all Gmail labels (system and user-created). |
| `gmail_list_message_attachments` | read | `auto` | List attachment names, MIME types, and sizes for a Gmail message. |
| `gmail_list_messages` | read | `auto` | Search Gmail and return matching message summaries (id, thread_id, subject, sender, date). |
| `gmail_list_threads` | read | `auto` | Search Gmail and return matching thread summaries (id, snippet). |
| `gmail_download_attachment` | read | `review` | Download a Gmail attachment's content. |
| `gmail_get_message` | read | `review` | Fetch a single Gmail message by id, including body, metadata, and attachment list. |
| `gmail_get_thread` | read | `review` | Fetch a full Gmail thread by id, including all messages. |
| `gmail_add_label` | write | `popup` | Add a label to a Gmail message. |
| `gmail_archive_message` | write | `popup` | Archive a Gmail message by removing it from the Inbox. |
| `gmail_create_draft` | write | `popup` | Create a Gmail draft. |
| `gmail_create_draft_with_attachments` | write | `popup` | Create a Gmail draft with one or more local-file attachments. |
| `gmail_create_filter` | write | `popup` | Create a Gmail filter. |
| `gmail_create_label` | write | `popup` | Create a Gmail label. |
| `gmail_remove_label` | write | `popup` | Remove a label from a Gmail message. |
| `gmail_reply_all_draft` | write | `popup` | Create a Gmail draft replying to all participants of a message (original sender plus To/Cc recipients, excluding yourself), staying in the same thread. |
| `gmail_reply_all_draft_with_attachments` | write | `popup` | Create a Gmail draft replying to all participants of a message (original sender plus To/Cc recipients, excluding yourself), staying in the same thread, with one or more local-file attachments. |
| `gmail_reply_draft` | write | `popup` | Create a Gmail draft replying to a single message, staying in the same thread (sets threadId plus In-Reply-To/References so it actually threads, unlike gmail_create_draft). |
| `gmail_reply_draft_with_attachments` | write | `popup` | Create a Gmail draft replying to a single message, staying in the same thread, with one or more local-file attachments. |
| `gmail_update_filter` | write | `popup` | Replace an existing Gmail filter's criteria and actions, identified by filter_id (from gmail_list_filters). |

## Google Drive (including Sheets and Docs)

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `drive_create_blank_file` | write | `auto` | Create a new blank Drive file. |
| `drive_get_file_metadata` | read | `auto` | Fetch metadata for a single Drive file by id (name, owners, times, sharing status). |
| `drive_list_files` | read | `auto` | Search Google Drive and return matching file metadata (id, name, mime_type, owners, sharing status). |
| `drive_list_folder` | read | `auto` | List the direct children of a Drive folder by id. |
| `drive_list_shared_drives` | read | `auto` | List all Google Workspace Shared Drives the user can access (returns id and name for each). |
| `drive_sheets_create` | write | `auto` | Create a new Google Sheets spreadsheet, optionally with named tabs. |
| `drive_sheets_get_metadata` | read | `auto` | List the tabs in a spreadsheet (id, title, index, row/column count). |
| `drive_download_file` | read | `review` | Download a Drive file. |
| `drive_get_file_content` | read | `review` | Fetch the content of a Drive file by id. |
| `drive_sheets_get_values` | read | `review` | Read a range of cells from a spreadsheet: display values by default, or the underlying values, or formulas instead of computed results, and optionally cell formatting alongside them. |
| `drive_add_comment` | write | `popup` | Add a comment to a Drive file. |
| `drive_docs_edit_content` | write | `popup` | Replace one occurrence of existing text in a Google Doc with new Markdown, without touching the rest of the document. |
| `drive_docs_format_content` | write | `popup` | Apply formatting (bold, italic, highlight, text color) to existing text in a Google Doc, located the same way as drive_docs_edit_content, without changing the text itself. |
| `drive_move_file` | write | `popup` | Move a Drive file to a different folder. |
| `drive_sheets_add_sheet` | write | `popup` | Add a new tab to an existing spreadsheet. |
| `drive_sheets_delete_dimensions` | write | `popup` | Delete rows or columns from a sheet tab, including any values, formulas, and formatting they contain. |
| `drive_sheets_format_range` | write | `popup` | Apply formatting to a range in a spreadsheet: bold/italic, colors, number format, horizontal/vertical alignment, text wrap, column width, frozen rows/columns, and merged cells. |
| `drive_sheets_insert_dimensions` | write | `popup` | Insert blank rows or columns into a sheet tab, shifting existing content after the insertion point. |
| `drive_sheets_rename_sheet` | write | `popup` | Rename an existing tab in a spreadsheet. |
| `drive_sheets_write_range` | write | `popup` | Write values and/or formulas into a range of an existing spreadsheet. |
| `drive_upload_file` | write | `popup` | Upload any file (e.g. a PDF or image) to Drive as a new file — use this instead of drive_write_file_content for any binary file, since that tool only writes UTF-8 text. |
| `drive_write_doc_content` | write | `popup` | Write Markdown content to a Google Doc with rich formatting. |
| `drive_write_file_content` | write | `popup` | Write content to an existing Drive file. |

## Google Calendar

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `calendar_get_event_visibility` | read | `auto` | Get a calendar event's visibility setting (default, public, private, or confidential) without fetching its full details (attendees, description, etc.) the way calendar_get_event_details does. |
| `calendar_get_free_busy` | read | `auto` | Query colleagues' schedules for a time range. |
| `calendar_list_calendars` | read | `auto` | List all Google Calendars for the authenticated user. |
| `calendar_list_colors` | read | `auto` | List Calendar's fixed event color palette: each color's id, name (e.g. "Tomato", "Sage"), and hex background/foreground. |
| `calendar_list_events` | read | `auto` | List events from a calendar (id, title, start_time, end_time, all_day, status). |
| `calendar_list_rooms` | read | `auto` | List meeting rooms and resource calendars from the organization's room directory. |
| `calendar_get_event_details` | read | `review` | Fetch full details of a calendar event including attendees, description, conferencing links, and file attachments (e.g. the "Notes by Gemini" and transcript docs Google Meet attaches after a meeting ends). |
| `calendar_create_event` | write | `popup` | Create a new calendar event. |
| `calendar_create_out_of_office` | write | `popup` | Create an out-of-office event on the primary calendar. |
| `calendar_delete_event` | write | `popup` | Delete a calendar event. |
| `calendar_set_event_color` | write | `popup` | Set a calendar event's color. |
| `calendar_set_event_visibility` | write | `popup` | Set a calendar event's visibility. |
| `calendar_set_working_location` | write | `popup` | Set your working-location presence (office or home) for a single day on the primary calendar — the same picker Google Calendar's web UI exposes. |
| `calendar_update_event` | write | `popup` | Update an existing calendar event. |

## Google Contacts

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `contacts_get` | read | `auto` | Fetch a single contact by resource name (e.g. 'people/c12345'). |
| `contacts_list` | read | `auto` | List contacts from the user's Google address book. |
| `contacts_search` | read | `auto` | Search contacts by name or email address. |
| `contacts_add_label` | write | `popup` | Add a label to a contact, creating the label if it doesn't already exist. |
| `contacts_create` | write | `popup` | Create a new contact in the user's Google address book. |
| `contacts_remove_label` | write | `popup` | Remove a label from a contact. |
| `contacts_update` | write | `popup` | Update a contact's fields. |

## Google Tasks

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `tasks_get_task` | read | `auto` | Fetch a single task by id. |
| `tasks_list_task_lists` | read | `auto` | List all Google Task lists. |
| `tasks_list_tasks` | read | `auto` | List tasks in a task list. |
| `tasks_complete_task` | write | `popup` | Mark a task as completed. |
| `tasks_create_task` | write | `popup` | Create a new task. |
| `tasks_move_task` | write | `popup` | Move a task from one list to another. |
| `tasks_uncomplete_task` | write | `popup` | Mark a task as not completed. |
| `tasks_update_task` | write | `popup` | Update a task's title, notes, or due date. |

## Apps Script

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `apps_script_list_projects` | read | `auto` | List standalone Google Apps Script projects visible to the user (id, name, last-modified time). |
| `apps_script_get_content` | read | `review` | Fetch the full source of a Google Apps Script project -- every file (.gs/.html) plus the appsscript.json manifest. |
| `apps_script_get_execution_log` | read | `review` | Read the result of the most recent run(s) of a script that the user triggered themselves outside PrivacyFence (status, duration, which function ran) -- not a live console.log transcript. |
| `apps_script_write_content` | write | `popup` | Write new source to a Google Apps Script project. |

## Slack

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `slack_list_channels` | read | `auto` | List Slack channels visible to the user (id, name, privacy, topic, purpose, member count). |
| `slack_list_dms` | read | `auto` | List 1:1 direct-message conversations visible to the user (id, other participant). |
| `slack_list_group_chats` | read | `auto` | List group-DM conversations visible to the user (id, name, participants). |
| `slack_refresh_channel_cache` | read | `auto` | Force an immediate refresh of PrivacyFence's local cache of Slack channel/DM/group-DM names, used to resolve which conversation a message belongs to in search results and history/thread reads without a per-message conversations.info call. |
| `slack_refresh_user_cache` | read | `auto` | Force an immediate refresh of PrivacyFence's local cache of Slack workspace member names/emails, used to resolve message authors in channel history, thread replies, and search results without a per-message users.info call. |
| `slack_resolve_permalink` | read | `auto` | Parse a Slack message permalink (from a message's "Copy link") into the channel id, timestamp, and (if the link points at a threaded reply) thread root timestamp needed by slack_get_channel_history/slack_get_thread_replies. |
| `slack_get_channel_history` | read | `review` | Fetch recent messages in a Slack channel. |
| `slack_get_thread_replies` | read | `review` | Fetch all replies in a Slack thread. |
| `slack_search_messages` | read | `review` | Search Slack messages matching a query, a participant, or both. |
| `slack_create_group_chat` | write | `popup` | Create (or reopen the existing) group-DM conversation with the given participants and return its channel id, ready for slack_send_message. |
| `slack_send_message` | write | `popup` | Send a message to a Slack channel or DM. |

## Telegram

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `telegram_list_chats` | read | `auto` | List Telegram chats (id, name, type, unread count). |
| `telegram_refresh_chat_cache` | read | `auto` | Force an immediate refresh of PrivacyFence's local cache of Telegram chat/group/channel names, used to resolve which chat a message belongs to in search results and chat history without a per-message lookup. |
| `telegram_get_messages` | read | `review` | Fetch recent messages from a Telegram chat by chat id. |
| `telegram_search_messages` | read | `review` | Search messages across Telegram chats by keyword. |
| `telegram_send_message` | write | `popup` | Send a message to a Telegram chat or user by chat id. |

## Salesforce

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `salesforce_list_reports` | read | `auto` | List Salesforce reports accessible to the user. |
| `salesforce_get_record` | read | `review` | Fetch a Salesforce record by object type and id. |
| `salesforce_run_report` | read | `review` | Run a Salesforce report by id and return the results. |
| `salesforce_search` | read | `review` | Search Salesforce by name or id across one or more object types — the same mechanism as the search bar at the top of the Salesforce UI. |

## Jira

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `jira_get_transitions` | read | `auto` | List the status transitions available for a Jira issue right now (name and target status), given its current workflow state. |
| `jira_list_projects` | read | `auto` | List Jira projects accessible to the user (key, name, type, lead). |
| `jira_search_issues` | read | `auto` | Search Jira issues using JQL. |
| `jira_get_issue` | read | `review` | Fetch full details of a Jira issue by key (e.g. PROJ-123), including description and comments. |
| `jira_add_comment` | write | `popup` | Add a comment to an existing Jira issue. |
| `jira_create_issue` | write | `popup` | Create a new Jira issue. |
| `jira_transition_issue` | write | `popup` | Move a Jira issue to a new status by transition name (e.g. "Done", "In Progress") — call jira_get_transitions first to see what's valid from the issue's current status. |
| `jira_update_issue` | write | `popup` | Update fields on an existing Jira issue (summary, description, priority, and/or custom fields). |

## Confluence

| Tool | Direction | Gate | What it does |
|---|---|---|---|
| `confluence_cql_search` | read | `auto` | Search Confluence using CQL (Confluence Query Language). |
| `confluence_list_attachments` | read | `auto` | List attachment names, media types, and sizes for a Confluence page. |
| `confluence_list_pages` | read | `auto` | List pages in a Confluence space (title, id, version). |
| `confluence_list_spaces` | read | `auto` | List Confluence spaces the user has access to (key, name, type, description). |
| `confluence_search` | read | `auto` | Full-text search across Confluence content. |
| `confluence_download_attachment` | read | `review` | Download a Confluence page attachment's content. |
| `confluence_get_page` | read | `review` | Fetch the full content of a Confluence page by page ID. |
| `confluence_get_page_by_title` | read | `review` | Fetch a Confluence page by space key and exact title. |
| `confluence_create_page` | write | `popup` | Create a new Confluence page in the given space. |
| `confluence_update_page` | write | `popup` | Update the title and/or body of an existing Confluence page. |
