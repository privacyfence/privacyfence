"""Google Tasks connector.

Reads (list task lists, list tasks, get a task) are low-sensitivity metadata
and stay auto-approved, like every other connector's read-only listing calls.
The one exception is a task's free-text `notes` field, which is filtered
through tasks_privacy's "notes" category (see privacy_filter.py) before
being returned -- unlike title/due/status, notes can carry arbitrary
personal content.
Writes (create/update/complete/uncomplete/move) go through the popup gate,
same as every other connector's writes — this connector used to auto-approve
everything, including writes, which was the one connector whose behavior
didn't match the documented default ("writes require review/popup").
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..google_errors import unavailable_error
from ..gate import current_reason, gated_call
from ..privacy_filter import apply_text
from ..tasks_client import TasksClient, TasksClientError

logger = logging.getLogger(__name__)


class TasksConnector(Connector):
    def __init__(self, client: TasksClient) -> None:
        self._tasks = client
        self.my_email: str = ""
        self._list_name_cache: dict[str, str] = {}

    @property
    def client(self) -> TasksClient:
        return self._tasks

    @property
    def name(self) -> str:
        return "tasks"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="tasks_list_task_lists",
                description=(
                    "List all Google Task lists. "
                    "Returns a list of {id, title, updated}. "
                    "Pass an id as task_list_id to tasks_list_tasks and the other task tools. "
                    "Auto-approved."
                ),
                params=[ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),],
                read_only=True,
            ),
            ToolSpec(
                name="tasks_list_tasks",
                description=(
                    "List tasks in a task list. "
                    "Returns a list of tasks as {id, task_list_id, title, notes, due, "
                    "status ('needsAction' or 'completed'), completed, updated, position, parent, "
                    "deleted}, only the first page the Tasks API sends (up to 20 tasks), in the "
                    "API's order; notes may be redacted by the user's privacy settings. "
                    "Use tasks_get_task instead when you already have a task's id. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("task_list_id", "str",
                              description="Id of the task list, from tasks_list_task_lists (its id field, not its title)."),
                    ToolParam("show_completed", "bool", required=False, default=False,
                              description=(
                                  "Include tasks completed through the API. Default false: only tasks "
                                  "still to do. Tasks completed in Google's own Tasks, Gmail or "
                                  "Calendar apps are hidden and are not returned either way."
                              )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="tasks_get_task",
                description=(
                    "Fetch a single task by id. "
                    "Returns one task in the same shape tasks_list_tasks lists; deleted is true for "
                    "a task deleted but not yet purged, and notes may be redacted by the user's "
                    "privacy settings. "
                    "Get the id from tasks_list_tasks. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("task_list_id", "str",
                              description="Id of the task list, from tasks_list_task_lists (its id field, not its title)."),
                    ToolParam("task_id", "str",
                              description="Id of the task, from tasks_list_tasks (its id field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="tasks_create_task",
                description=(
                    "Create a new task. "
                    "Returns the created task, with its new id, in the same shape tasks_get_task "
                    "returns. "
                    "Get task_list_id from tasks_list_task_lists. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("task_list_id", "str",
                              description="Id of the task list, from tasks_list_task_lists (its id field, not its title)."),
                    ToolParam("title", "str",
                              description="Title of the new task, as shown in Google Tasks. Must not be empty."),
                    ToolParam("notes", "str", required=False, default="",
                              description="Free-text notes for the task (its description). Empty means no notes."),
                    ToolParam("due", "str", required=False, default="",
                              description=(
                                  "Due date as an RFC 3339 timestamp, e.g. '2026-10-15T00:00:00Z'; "
                                  "Google Tasks keeps only the date. Empty means no due date."
                              )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="tasks_update_task",
                description=(
                    "Update a task's title, notes, or due date. "
                    "Only the fields you pass non-empty change: an empty value leaves that field as "
                    "it is, so this tool cannot clear notes or a due date. "
                    "Returns the updated task in the same shape tasks_get_task returns. "
                    "Use tasks_complete_task or tasks_uncomplete_task to change whether it is done. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("task_list_id", "str",
                              description="Id of the task list, from tasks_list_task_lists (its id field, not its title)."),
                    ToolParam("task_id", "str",
                              description="Id of the task, from tasks_list_tasks (its id field)."),
                    ToolParam("title", "str", required=False, default="",
                              description="New title for the task. Empty leaves the title unchanged."),
                    ToolParam("notes", "str", required=False, default="",
                              description="New notes, replacing the current ones. Empty leaves the notes unchanged."),
                    ToolParam("due", "str", required=False, default="",
                              description=(
                                  "New due date as an RFC 3339 timestamp, e.g. '2026-10-15T00:00:00Z'; "
                                  "only the date is kept. Empty leaves the due date unchanged."
                              )),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="tasks_complete_task",
                description=(
                    "Mark a task as completed. "
                    "Returns the updated task in the same shape tasks_get_task returns, with status "
                    "'completed' and the completion time in completed. "
                    "Use tasks_uncomplete_task to undo it. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("task_list_id", "str",
                              description="Id of the task list, from tasks_list_task_lists (its id field, not its title)."),
                    ToolParam("task_id", "str",
                              description="Id of the task, from tasks_list_tasks (its id field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="tasks_uncomplete_task",
                description=(
                    "Mark a task as not completed. "
                    "Returns the updated task in the same shape tasks_get_task returns, with status "
                    "'needsAction' and completed cleared. "
                    "Use tasks_complete_task for the opposite. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("task_list_id", "str",
                              description="Id of the task list, from tasks_list_task_lists (its id field, not its title)."),
                    ToolParam("task_id", "str",
                              description="Id of the task, from tasks_list_tasks (its id field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="tasks_move_task",
                description=(
                    "Move a task from one list to another. "
                    "Google Tasks cannot move a task between lists, so this copies its title, notes "
                    "and due date into the destination list and then deletes the original: the task "
                    "gets a new id, comes back not completed, and its subtasks and position are not "
                    "copied. "
                    "Returns the new task in the same shape tasks_get_task returns. "
                    "Get both list ids from tasks_list_task_lists. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("source_list_id", "str",
                              description="Id of the list the task is in now, from tasks_list_task_lists."),
                    ToolParam("task_id", "str",
                              description="Id of the task to move, from tasks_list_tasks on the source list."),
                    ToolParam("destination_list_id", "str",
                              description="Id of the list to move the task to, from tasks_list_task_lists."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "tasks_list_task_lists":
            return await self._run("tasks_list_task_lists", "List Task Lists", "List all task lists", self._tasks.list_task_lists)
        if tool == "tasks_list_tasks":
            return await self._run("tasks_list_tasks", "List Tasks", f"List tasks in {args.get('task_list_id', '')}", self._tasks.list_tasks, args.get("task_list_id", ""), bool(args.get("show_completed", False)))
        if tool == "tasks_get_task":
            return await self._run("tasks_get_task", "Get Task", f"Get task {args.get('task_id', '')}", self._tasks.get_task, args.get("task_list_id", ""), args.get("task_id", ""))
        if tool == "tasks_create_task":
            return await self._create_task(**args)
        if tool == "tasks_update_task":
            return await self._update_task(**args)
        if tool == "tasks_complete_task":
            return await self._complete_task(**args)
        if tool == "tasks_uncomplete_task":
            return await self._uncomplete_task(**args)
        if tool == "tasks_move_task":
            return await self._move_task(**args)
        raise ValueError(f"Unknown Tasks tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto (no gate) — read-only metadata
    # ------------------------------------------------------------------ #

    async def _run(self, tool: str, tool_name: str, summary: str, func, *func_args) -> Any:
        t0 = time.time()
        result = await self._fetch(func, *func_args)
        self._auto_audit(tool, tool_name, summary, t0)
        # Only the read path -- a write's result dict echoes back notes
        # Claude just wrote itself, so there's nothing to redact there.
        return _redact_notes(self._serialize(result))

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_task(
        self, task_list_id: str, title: str, notes: str = "", due: str = ""
    ) -> Any:
        preview = {"Task list": await self._list_name_for(task_list_id), "Title": title}
        if due:
            preview["Due"] = due
        # v2's right pane: a label-styled "Notes" heading above the body,
        # same treatment jira_create_issue's Description gets -- empty when
        # there are no notes at all (build_preview_body_html falls back to
        # details_text).
        blocks = []
        if notes:
            blocks.append({"type": "heading", "label": "Notes"})
            blocks.append({"type": "text", "text": notes})
        await gated_call(
            connector=self.name,
            tool="tasks_create_task",
            tool_name="Create Task",
            summary=f"Create task: {title}",
            sender="",
            raw_data={"task_list_id": task_list_id, "title": title, "notes": notes, "due": due},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=notes or "No notes provided; see preview for task details.",
            preview_blocks=blocks,
            args={"task_list_id": task_list_id, "title": title},
        )
        result = await self._fetch(self._tasks.create_task, task_list_id, title, notes, due)
        return self._serialize(result)

    async def _update_task(
        self, task_list_id: str, task_id: str, title: str = "", notes: str = "", due: str = ""
    ) -> Any:
        existing = await self._fetch(self._tasks.get_task, task_list_id, task_id)
        # "Task" always appears (identifying context, like calendar_update_
        # event's "Event" or contacts_update's "Name") -- becomes an
        # old → new diff only when title is actually changing. "Due" only
        # appears at all when it's changing (unlike Task, there's no
        # standing identifying reason to show it otherwise), but shown as
        # old → new rather than just the new value when it does.
        changed_field_names = []
        preview = {"Task list": await self._list_name_for(task_list_id), "Task": existing.title}
        if title and title != existing.title:
            preview["Task"] = f"{existing.title} → {title}"
            changed_field_names.append("Task")
        if due and due != existing.due:
            preview["Due"] = f"{existing.due or '(none)'} → {due}"
            changed_field_names.append("Due")
        # v2's right pane: a label-styled "Notes" heading above the body,
        # same treatment jira_get_issue's Description/jira_create_issue's
        # Description already get -- only when notes are actually changing
        # (same "only changing" treatment as Due above).
        blocks = []
        if notes and notes != existing.notes:
            changed_field_names.append("Notes")
            blocks.append({"type": "heading", "label": "Notes"})
            blocks.append({"type": "text", "text": notes})
        if notes and notes != existing.notes:
            details_text = notes
        else:
            changed_fields = ", ".join(changed_field_names) or "no fields"
            details_text = f"{changed_fields} will be updated; notes unchanged."
        await gated_call(
            connector=self.name,
            tool="tasks_update_task",
            tool_name="Update Task",
            summary=f"Update task: {existing.title}",
            sender="",
            raw_data=existing,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details_text,
            preview_blocks=blocks,
            args={"task_list_id": task_list_id, "task_id": task_id},
        )
        result = await self._fetch(
            self._tasks.update_task, task_list_id, task_id,
            title or None, notes or None, due or None,
        )
        return self._serialize(result)

    async def _complete_task(self, task_list_id: str, task_id: str) -> Any:
        existing = await self._fetch(self._tasks.get_task, task_list_id, task_id)
        preview = {"Task list": await self._list_name_for(task_list_id), "Task": existing.title}
        await gated_call(
            connector=self.name,
            tool="tasks_complete_task",
            tool_name="Complete Task",
            summary=f"Complete task: {existing.title}",
            sender="",
            raw_data=existing,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Task will be marked as completed; title and notes are unchanged.",
            args={"task_list_id": task_list_id, "task_id": task_id},
        )
        result = await self._fetch(self._tasks.complete_task, task_list_id, task_id)
        return self._serialize(result)

    async def _uncomplete_task(self, task_list_id: str, task_id: str) -> Any:
        existing = await self._fetch(self._tasks.get_task, task_list_id, task_id)
        preview = {"Task list": await self._list_name_for(task_list_id), "Task": existing.title}
        await gated_call(
            connector=self.name,
            tool="tasks_uncomplete_task",
            tool_name="Uncomplete Task",
            summary=f"Uncomplete task: {existing.title}",
            sender="",
            raw_data=existing,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Task will be marked as not completed; title and notes are unchanged.",
            args={"task_list_id": task_list_id, "task_id": task_id},
        )
        result = await self._fetch(self._tasks.uncomplete_task, task_list_id, task_id)
        return self._serialize(result)

    async def _move_task(
        self, source_list_id: str, task_id: str, destination_list_id: str
    ) -> Any:
        existing = await self._fetch(self._tasks.get_task, source_list_id, task_id)
        # "List": old → new -- a move always changes the list (that's the
        # whole point of the call), so this is always a diff, same
        # reasoning as drive_move_file's own "Folder" field.
        source_name = await self._list_name_for(source_list_id)
        destination_name = await self._list_name_for(destination_list_id)
        preview = {
            "Task": existing.title,
            "List": f"{source_name} → {destination_name}",
        }
        await gated_call(
            connector=self.name,
            tool="tasks_move_task",
            tool_name="Move Task",
            summary=f"Move task: {existing.title}",
            sender="",
            raw_data=existing,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Task will be moved to the new list; title and notes are unchanged.",
            args={
                "source_list_id": source_list_id,
                "task_id": task_id,
                "destination_list_id": destination_list_id,
            },
        )
        result = await self._fetch(self._tasks.move_task, source_list_id, task_id, destination_list_id)
        return self._serialize(result)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except TasksClientError as exc:
            logger.error("Tasks call failed: %s", exc)
            unavailable = unavailable_error("tasks", exc, self.my_email)
            if unavailable is not None:
                raise unavailable from exc
            raise RuntimeError(str(exc)) from exc

    async def _list_name_for(self, task_list_id: str) -> str:
        """Best-effort, cached task-list title lookup; falls back to the raw
        id (e.g. the list was deleted, or a permissions edge case) rather
        than blocking the popup on a lookup that can't succeed."""
        if task_list_id in self._list_name_cache:
            return self._list_name_cache[task_list_id]
        try:
            task_list = await self._fetch(self._tasks.get_task_list, task_list_id)
            name = task_list.title or task_list_id
        except RuntimeError:
            name = task_list_id
        self._list_name_cache[task_list_id] = name
        return name

    @staticmethod
    def _serialize(result: Any) -> Any:
        if isinstance(result, list):
            return [asdict(r) for r in result]
        if hasattr(result, "__dataclass_fields__"):
            return asdict(result)
        return result

    def _auto_audit(self, tool: str, tool_name: str, summary: str, created_at: float) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender="",
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)


def _redact_notes(value: Any) -> Any:
    """Apply tasks_privacy's "notes" category to a serialized Task's
    free-text notes field -- the one field on a task that can carry
    arbitrary personal content, unlike title/due/status. A TaskList dict has
    no "notes" key and passes through untouched; a list of either is handled
    recursively."""
    if isinstance(value, list):
        return [_redact_notes(v) for v in value]
    if isinstance(value, dict) and "notes" in value:
        value["notes"] = apply_text("tasks_privacy", "notes", value.get("notes", "") or "")
    return value
