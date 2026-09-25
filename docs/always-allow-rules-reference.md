# "Always allow" — per-tool reference

Which **Always allow** buttons an approval card can offer, tool by tool. This page is an appendix
to [Approvals and policy](approvals-and-policy.md#always-allow-and-policy-rules), which explains
how rules work and where else you can create them.

**Generated** by
[`scripts/generate_always_allow_reference.py`](../scripts/generate_always_allow_reference.py) from
[`src/privacyfence/policy/registry.py`](../src/privacyfence/policy/registry.py) and
[`src/privacyfence/policy/propose.py`](../src/privacyfence/policy/propose.py). Don't hand-edit this
file — run the generator and commit its output; a CI test fails if the checked-in copy and a fresh
run disagree.

## How to read this page

Every connector tool has a **gate**: `auto` (runs without asking), `review` (a read you approve
before its content is released) or `popup` (a write you confirm before it happens). `auto` tools
never show a card, so they are left out of this page. The rest are split into
[read tools](#read-tools) (`review`) and [write tools](#write-tools) (`popup`).

The **Always allow buttons** column lists every rule scope the card can propose for that tool.
A scope is only offered when it actually contains the item on the card — "this folder" appears
only when the file has a parent folder, "if I own it" only when you own the file — and **every
scope that matches gets its own button**, so a file you own that also sits in a folder can show
both "Always allow — this folder" and "Always allow — if I own it". When no scope matches, or the
column is empty, the card offers only **Deny** and **Allow once**.

"unconditional" means the button has no scope at all: the rule it writes accepts every future call
of that operation (Gmail drafting is the one case).

Clicking a button opens a confirmation dialog that states the rule as a sentence and lists every
tool it covers. Confirming writes one rule to the `auto_accept:` section of `settings.yaml`. That
rule covers the scope's value (the folder, label, calendar, …) and only the one operation you just
approved — other operations on the same resource still ask. Cancelling the dialog still approves
the request on the card, once.

---

## Read tools

### Apps Script

| Tool | Always allow buttons |
|---|---|
| `apps_script_get_content` |  |
| `apps_script_get_execution_log` |  |

### Calendar

| Tool | Always allow buttons |
|---|---|
| `calendar_get_event_details` | this calendar, if I organize it, no external attendees, non-private events |

### Confluence

| Tool | Always allow buttons |
|---|---|
| `confluence_download_attachment` | this space, if I'm author |
| `confluence_get_page` | this space, if I'm author |
| `confluence_get_page_by_title` | this space, if I'm author |

### Drive

| Tool | Always allow buttons |
|---|---|
| `drive_download_file` | this folder, if I own it |
| `drive_get_file_content` | this folder, if I own it |
| `drive_sheets_get_values` | this folder, if I own it |

### Gmail

| Tool | Always allow buttons |
|---|---|
| `gmail_download_attachment` | if I'm sender, this sender domain |
| `gmail_get_message` | if I'm sender, this sender domain |
| `gmail_get_thread` | if I'm sender, this sender domain |

### Jira

| Tool | Always allow buttons |
|---|---|
| `jira_get_issue` | this project, if I'm reporter, if I'm assignee |

### Salesforce

| Tool | Always allow buttons |
|---|---|
| `salesforce_get_record` | this object type |
| `salesforce_run_report` | this report |
| `salesforce_search` | this object type |

### Slack

| Tool | Always allow buttons |
|---|---|
| `slack_get_channel_history` | this channel, my own DM, this group DM |
| `slack_get_thread_replies` | this channel, my own DM, this group DM |
| `slack_search_messages` | this channel |

### Telegram

| Tool | Always allow buttons |
|---|---|
| `telegram_get_messages` | this chat |
| `telegram_search_messages` | this chat |

---

## Write tools

A write tool with a non-empty column can offer a rule scoped to the resource the call touched —
the folder, label, calendar, project, list, channel or chat — except the six Gmail draft tools,
whose rule is unconditional. Every other write tool offers only **Deny** and **Allow once**.
A few Drive, Docs and Sheets tools also start a short same-file grace window when you click
**Allow once**; that is not a rule, see
[Same-file grace window](approvals-and-policy.md#same-file-grace-window).

### Apps Script

| Tool | Always allow buttons |
|---|---|
| `apps_script_write_content` |  |

### Calendar

| Tool | Always allow buttons |
|---|---|
| `calendar_create_event` | this calendar |
| `calendar_create_out_of_office` |  |
| `calendar_delete_event` | this calendar |
| `calendar_set_event_color` | this calendar |
| `calendar_set_event_visibility` | this calendar |
| `calendar_set_working_location` |  |
| `calendar_update_event` | this calendar |

### Confluence

| Tool | Always allow buttons |
|---|---|
| `confluence_create_page` | this space |
| `confluence_update_page` | this space |

### Contacts

| Tool | Always allow buttons |
|---|---|
| `contacts_add_label` | this label |
| `contacts_create` |  |
| `contacts_remove_label` | this label |
| `contacts_update` |  |

### Drive

| Tool | Always allow buttons |
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

| Tool | Always allow buttons |
|---|---|
| `gmail_add_label` | this label |
| `gmail_archive_message` | if I'm sender, this sender domain |
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

| Tool | Always allow buttons |
|---|---|
| `jira_add_comment` | this project |
| `jira_create_issue` | this project |
| `jira_transition_issue` | this project |
| `jira_update_issue` | this project |

### Slack

| Tool | Always allow buttons |
|---|---|
| `slack_create_group_chat` |  |
| `slack_send_message` | this channel |

### Tasks

| Tool | Always allow buttons |
|---|---|
| `tasks_complete_task` | this list |
| `tasks_create_task` | this list |
| `tasks_move_task` | this list |
| `tasks_uncomplete_task` | this list |
| `tasks_update_task` | this list |

### Telegram

| Tool | Always allow buttons |
|---|---|
| `telegram_send_message` | this chat |

## Rules you can't create from a card

Some operations never offer an **Always allow** button because nothing on the card names a
resource to scope the rule to: Apps Script projects, Gmail filters, and creating a Slack group
chat. You can still allow them from **Settings → Auto-accept → Add a rule**, or by letting the AI
system propose a rule with `privacyfence_propose_policy_change`. Either way the rule is written
only after you confirm it. See [Approvals and policy](approvals-and-policy.md#always-allow-and-policy-rules).
