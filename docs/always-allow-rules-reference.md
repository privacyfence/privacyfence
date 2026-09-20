# "Always allow" — per-tool reference

What clicking **Always allow** proposes, tool by tool. **Generated** by
[`scripts/generate_always_allow_reference.py`](../scripts/generate_always_allow_reference.py) from
[`src/privacyfence/policy/registry.py`](../src/privacyfence/policy/registry.py) and
[`src/privacyfence/policy/propose.py`](../src/privacyfence/policy/propose.py) — the same scope
catalogue the popup, the Auto-accept Settings page, and the MCP bridge's
`privacyfence_propose_policy_change` all write through
([`docs/TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#auto-accept) has the schema and the three
surfaces). Don't hand-edit this file — run the generator and commit its output; a CI test fails if
the checked-in copy and a fresh run disagree.

## How this doc is organized

Every gated tool has a **gate**: `auto`, `review`, or `popup`. `auto` tools never show a popup or
any button — nothing to allow — so they're **left out of this doc entirely**. What remains splits
into two sections:

- **[Read tools](#read-tools)** (`review` gate) — the popup offers **Always allow** whenever at
  least one scope in the catalogue below plausibly contains the item just read; otherwise the
  button doesn't appear, and the row's own column is empty.
- **[Write tools](#write-tools)** (`popup` gate) — most write popups never offer Always allow at
  all; the ones that do propose a rule scoped to the one folder/label/calendar/project/space/task
  list the call just touched, narrowest first — never a bare "accept every future write of this
  type" toggle.

Where a tool's row lists more than one candidate, only the first whose scope actually contains the
item under review becomes a button — narrowest declared first (identity scopes before attribute
scopes before condition scopes), per
[`policy/propose.py`](../src/privacyfence/policy/propose.py)'s own declaration order. When two or
more candidates genuinely match the same item at once (e.g. a file you own that's also in an
approved folder), the popup renders one button per match instead of picking one.

**Always allow always writes a v2 rule to the `auto_accept:` section** — one row, scoped to
exactly the operation just gated at first; the confirmation dialog then offers further verbs as
named widening chips (e.g. "also allow format") before anything is written. See
[Auto-accept](TECHNICAL_REFERENCE.md#auto-accept) for the schema and the other two surfaces that
write the identical shape.

---

## Read tools

### Apps Script

| Tool | Always allow proposes |
|---|---|
| `apps_script_get_content` |  |
| `apps_script_get_execution_log` |  |

### Calendar

| Tool | Always allow proposes |
|---|---|
| `calendar_get_event_details` | this calendar, else if I organize it, else no external attendees, else non-private events |

### Confluence

| Tool | Always allow proposes |
|---|---|
| `confluence_download_attachment` | this space, else if I'm author |
| `confluence_get_page` | this space, else if I'm author |
| `confluence_get_page_by_title` | this space, else if I'm author |

### Drive

| Tool | Always allow proposes |
|---|---|
| `drive_download_file` | this folder, else if I own it |
| `drive_get_file_content` | this folder, else if I own it |
| `drive_sheets_get_values` | this folder, else if I own it |

### Gmail

| Tool | Always allow proposes |
|---|---|
| `gmail_download_attachment` | if I'm sender, else this sender domain |
| `gmail_get_message` | if I'm sender, else this sender domain |
| `gmail_get_thread` | if I'm sender, else this sender domain |

### Jira

| Tool | Always allow proposes |
|---|---|
| `jira_get_issue` | this project, else if I'm reporter, else if I'm assignee |

### Salesforce

| Tool | Always allow proposes |
|---|---|
| `salesforce_get_record` | this object type |
| `salesforce_run_report` | this report |
| `salesforce_search` | this object type |

### Slack

| Tool | Always allow proposes |
|---|---|
| `slack_get_channel_history` | this channel, else my own DM, else this group DM |
| `slack_get_thread_replies` | this channel, else my own DM, else this group DM |
| `slack_search_messages` | this channel |

### Telegram

| Tool | Always allow proposes |
|---|---|
| `telegram_get_messages` | this chat |
| `telegram_search_messages` | this chat |

---

## Write tools

Most write tools never offer **Always allow** — auto-accepting a write silently is a materially
bigger blast radius than auto-accepting a read. Every write tool below with a non-empty column is
a narrow, deliberate exception, scoped to the one resource the call just touched. Every other
gated write tool offers exactly Deny / Allow once, with an empty **Always allow proposes** column.
A handful of tools also have a separate, non-persisted grace-window behavior tucked into their
"Allow once" instead — see
[Related but distinct mechanisms](TECHNICAL_REFERENCE.md#auto-accept) for what that is; it isn't
an Always-allow rule and doesn't belong in this column.

### Apps Script

| Tool | Always allow proposes |
|---|---|
| `apps_script_write_content` |  |

### Calendar

| Tool | Always allow proposes |
|---|---|
| `calendar_create_event` | this calendar |
| `calendar_create_out_of_office` |  |
| `calendar_delete_event` | this calendar |
| `calendar_set_event_color` | this calendar |
| `calendar_set_event_visibility` | this calendar |
| `calendar_set_working_location` |  |
| `calendar_update_event` | this calendar |

### Confluence

| Tool | Always allow proposes |
|---|---|
| `confluence_create_page` | this space |
| `confluence_update_page` | this space |

### Contacts

| Tool | Always allow proposes |
|---|---|
| `contacts_add_label` | this label |
| `contacts_create` |  |
| `contacts_remove_label` | this label |
| `contacts_update` |  |

### Drive

| Tool | Always allow proposes |
|---|---|
| `drive_add_comment` | this folder |
| `drive_docs_edit_content` | this folder |
| `drive_docs_format_content` | this folder |
| `drive_move_file` | this folder |
| `drive_sheets_add_sheet` | this folder |
| `drive_sheets_delete_dimensions` | this folder |
| `drive_sheets_format_range` | this folder |
| `drive_sheets_insert_dimensions` | this folder |
| `drive_sheets_rename_sheet` | this folder |
| `drive_sheets_write_range` | this folder |
| `drive_upload_file` | this folder |
| `drive_write_doc_content` | this folder |
| `drive_write_file_content` | this folder |

### Gmail

| Tool | Always allow proposes |
|---|---|
| `gmail_add_label` | this label |
| `gmail_archive_message` | if I'm sender, else this sender domain |
| `gmail_create_draft` | unconditional |
| `gmail_create_draft_with_attachments` | unconditional |
| `gmail_create_filter` |  |
| `gmail_create_label` |  |
| `gmail_remove_label` | this label |
| `gmail_reply_all_draft` | unconditional |
| `gmail_reply_all_draft_with_attachments` | unconditional |
| `gmail_reply_draft` | unconditional |
| `gmail_reply_draft_with_attachments` | unconditional |
| `gmail_update_filter` |  |

### Jira

| Tool | Always allow proposes |
|---|---|
| `jira_add_comment` | this project |
| `jira_create_issue` | this project |
| `jira_transition_issue` | this project |
| `jira_update_issue` | this project |

### Slack

| Tool | Always allow proposes |
|---|---|
| `slack_create_group_chat` |  |
| `slack_send_message` | this channel |

### Tasks

| Tool | Always allow proposes |
|---|---|
| `tasks_complete_task` | this list |
| `tasks_create_task` | this list |
| `tasks_move_task` | this list |
| `tasks_uncomplete_task` | this list |
| `tasks_update_task` | this list |

### Telegram

| Tool | Always allow proposes |
|---|---|
| `telegram_send_message` | this chat |

## Related but distinct mechanisms

These are easy to conflate with Always allow because they sit in the same popups or touch the same
config, but none of them are the "Always allow" button covered above.

**Temp-accept grace window** — an in-memory, non-persisted acceptance for six `popup`-gate writes
expected to fire repeatedly against the same file in a burst
(`privacyfence.auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS`), scoped to one file/spreadsheet for 5
minutes and gone on daemon restart. There's no separate button for it: these popups show only
Deny / Allow once, with a plain disclosure caption above the buttons explaining that Allow once
also arms the grace window.

**Bridge-proposed policy changes** (`privacyfence_propose_policy_change`) — lets Claude itself
propose adding/updating/removing a rule for *any* operation, including tools that never get an
Always-allow button of their own (a Gmail filter, a Slack group chat, an Apps Script project).
Every call still blocks on the same confirmation dialog Always allow uses — there's no way for a
rule to land without a human confirming it. See `privacyfence_list_policy`/
`privacyfence_propose_policy_change` in `src/privacyfence/web/mcp_tools.py` (or the tool's own MCP
description) for the exact request/response shape.
