"""One plain sentence per write tool, naming what the operation changes and
whether it can be taken back.

A read card ends with "What will be provided to Claude" -- a straight
statement of the consequence of approving. A write card had no equivalent:
it showed the payload (§1) and Claude's stated reason (§2), and nothing
anywhere said what would actually happen. That is the difference between
approving a *payload* and approving an *outcome*, and it matters most
exactly where the payload looks harmless: "Add Gmail Label" and "Send Slack
Message" present almost identically, and only one of them is irreversible.

The sentence is a property of the operation, not of the call, so it lives
here as a table keyed by tool id rather than being passed in from each of
the ~50 ``gated_call(gate="popup")`` sites. That also means the whole of
this copy can be read, and reviewed, in one place -- same reasoning
auto_accept.TOOL_TO_OPERATION is a table rather than a per-call-site
argument.

Two rules for the wording, both of which the sentences below follow:

1. **Say what does not happen, when that is the reassurance.** "A label is
   added. Nothing is sent, moved or deleted." is the sentence that lets
   somebody approve quickly and correctly. Leaving the second half out
   makes a harmless operation look as open-ended as a destructive one.
2. **Never soften an irreversible one.** "The message is posted and cannot
   be unsent." is the point of the whole card. Where recovery exists but
   is not obvious -- a Sheets range overwrite, a row deletion -- the
   sentence names where to find it rather than implying the change is
   cheap.

Claims here are about the operation's own observable effect. Where a
provider makes something configurable (Jira's notification schemes, calendar
invitations), the wording says "may" rather than asserting a behavior this
codebase does not control.

Coverage is enforced, not assumed: tests/unit/test_write_effects.py fails
the moment a tool reaches the write gate without an entry here, the same
posture approvals.PendingApproval.is_batchable()'s own coverage test takes.
"""
from __future__ import annotations

EFFECT_BY_TOOL: dict[str, str] = {
    # ── Gmail ────────────────────────────────────────────────────────────
    "gmail_create_draft": "A draft is saved. Nothing is sent.",
    "gmail_reply_draft": "A reply draft is saved. Nothing is sent.",
    "gmail_reply_all_draft":
        "A reply draft addressed to everyone on the thread is saved. Nothing is sent.",
    "gmail_create_draft_with_attachments":
        "A draft, with its attachments, is saved. Nothing is sent.",
    "gmail_reply_draft_with_attachments":
        "A reply draft, with its attachments, is saved. Nothing is sent.",
    "gmail_reply_all_draft_with_attachments":
        "A reply draft addressed to everyone on the thread, with its attachments, is saved. "
        "Nothing is sent.",
    "gmail_add_label": "A label is added. Nothing is sent, moved or deleted.",
    "gmail_remove_label": "A label is removed. The message itself is not deleted.",
    "gmail_archive_message":
        "The message leaves the inbox. It is not deleted and can still be found by search.",
    "gmail_create_filter":
        "A filter is created. It acts on mail arriving from now on, not on mail already received.",
    "gmail_update_filter":
        "The filter's rules change. Mail it has already acted on is not revisited.",
    "gmail_create_label": "A new label is created. No mail is changed.",

    # ── Google Calendar ──────────────────────────────────────────────────
    "calendar_create_event":
        "An event is created. If it lists attendees, they may be invited and notified.",
    "calendar_update_event":
        "The event changes. Its attendees may be notified of the change.",
    "calendar_create_out_of_office":
        "An out-of-office event is created, and may automatically decline meetings in that window.",
    "calendar_set_working_location":
        "Your working location for that day changes. It is visible to anyone who can see your calendar.",
    "calendar_set_event_visibility":
        "Who can see the event's details changes. The event itself is not moved or deleted.",
    "calendar_set_event_color": "The event's colour changes in your calendar. Nothing else changes.",

    # ── Google Contacts ──────────────────────────────────────────────────
    "contacts_create": "A new contact is saved to your address book.",
    "contacts_update": "The contact's saved details are replaced. The previous values are not kept.",
    "contacts_add_label": "A label is added to the contact. No contact details change.",
    "contacts_remove_label": "A label is removed from the contact. The contact itself is not deleted.",

    # ── Google Drive / Docs / Sheets ─────────────────────────────────────
    "drive_upload_file": "A new file is uploaded. Nothing already in Drive is replaced.",
    "drive_write_doc_content":
        "The document's contents are replaced. The previous version stays in Drive's version history.",
    "drive_docs_edit_content":
        "The document is edited in place. The previous version stays in Drive's version history.",
    "drive_docs_format_content": "Text formatting changes. The words themselves are unchanged.",
    "drive_write_file_content":
        "The file's contents are replaced. The previous version stays in Drive's version history.",
    "drive_move_file": "The file moves to another folder. It is not copied and not deleted.",
    "drive_add_comment": "A comment is added, visible to everyone with access to the file.",
    "drive_sheets_write_range":
        "Every cell in that range is overwritten. The previous values survive only in "
        "Sheets' version history.",
    "drive_sheets_add_sheet": "A new tab is added. Existing tabs are unchanged.",
    "drive_sheets_rename_sheet":
        "The tab is renamed. Formulas that refer to it by name are updated to match.",
    "drive_sheets_format_range": "Cell formatting changes. The values themselves are unchanged.",
    "drive_sheets_insert_dimensions":
        "Empty rows or columns are inserted, shifting existing cells down or across.",
    "drive_sheets_delete_dimensions":
        "Rows or columns are deleted, with everything in them. They can be recovered only "
        "through Sheets' version history.",

    # ── Google Tasks ─────────────────────────────────────────────────────
    "tasks_create_task": "A new task is added to that list.",
    "tasks_update_task": "The task's details are replaced. The previous values are not kept.",
    "tasks_complete_task": "The task is marked complete. This can be undone.",
    "tasks_uncomplete_task": "The task is marked not complete again.",
    "tasks_move_task": "The task moves within or between lists. Nothing is deleted.",

    # ── Slack ────────────────────────────────────────────────────────────
    "slack_send_message": "The message is posted and cannot be unsent.",
    "slack_create_group_chat":
        "A group conversation is created and everyone named is added to it. They can see it "
        "immediately.",

    # ── Telegram ─────────────────────────────────────────────────────────
    "telegram_send_message": "The message is delivered and cannot be unsent.",

    # ── Jira ─────────────────────────────────────────────────────────────
    "jira_create_issue": "A new issue is created. The project's watchers may be notified.",
    "jira_add_comment": "A comment is added, visible to everyone who can see the issue.",
    "jira_update_issue": "The issue's fields are changed. Its watchers may be notified.",
    "jira_transition_issue": "The issue moves to another status. Its watchers may be notified.",

    # ── Confluence ───────────────────────────────────────────────────────
    "confluence_create_page":
        "A new page is created in that space, visible to everyone with access to it.",
    "confluence_update_page":
        "The page's contents are replaced. The previous version stays in the page's history.",

    # ── Apps Script ──────────────────────────────────────────────────────
    "apps_script_write_content":
        "The project's source is replaced. The previous source is recoverable only if it was "
        "already saved as a version.",
}

# The card's own label for this row -- see card_builder.build_card_html.
EFFECT_LABEL = "Effect"


def effect_for(tool: str) -> str:
    """The effect sentence for ``tool``, or "" if there is none.

    Returning "" rather than a generic fallback is deliberate: a vague
    "This performs a write" would occupy the row that exists to say
    something specific, and would read as if the specific answer had been
    considered and found to be that. An unlisted tool renders no row at
    all, which is the honest rendering of "nobody has written this yet" --
    and the coverage test means that state cannot reach a release.
    """
    return EFFECT_BY_TOOL.get(tool, "")
