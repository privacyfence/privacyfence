"""The connector resource-type manifest -- one row per kind of resource a rule can name an identity
of (a Drive folder, a Jira project, a Slack channel, ...).

Two live callers read it:

- ``resource_names.py``'s ``ResourceNameResolver`` calls a ``GrantResourceType``'s ``resolver`` to
  turn an opaque id (a folder id, a project key) into the display name the Auto-accept Settings page
  shows next to a rule's value (``settings_controller.SettingsController._resolved_rule_value``,
  keyed through ``settings_controller.RULE_NAME_TO_RESOURCE_TYPE``, which is built from each
  capability's target predicates).
- ``auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS`` derives its operation list from
  ``DRIVE_SANDBOX_WRITE_TARGETS``.

A new grant-*shaped* feature belongs in ``policy/scopes.py`` as a scope, not as a new entry here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class GrantCapability:
    """One capability of a resource type (e.g. "read auto-accept").

    ``targets`` is the list of (operation_key, rule_name) pairs a capability compiles to -- almost
    always one pair, but e.g. a sandbox folder's "write" capability spans several Drive/Sheets/Docs
    operation keys.
    """

    label: str
    targets: tuple[tuple[str, str], ...]


def _find_by(items: Any, attr: str, target: str) -> str | None:
    """First item's `attr` where str(item.attr) == target, else None."""
    for item in items or []:
        if str(getattr(item, attr, "")) == target:
            return getattr(item, "name", None) or getattr(item, "title", None) or getattr(item, "summary", None)
    return None


def _resolve_drive_file(client: Any, resource_id: str) -> str | None:
    try:
        return client.get_file_metadata(resource_id).name or None
    except Exception:
        return None


def _resolve_task_list(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_task_lists(), "id", resource_id)
    except Exception:
        return None


def _resolve_slack_channel(client: Any, resource_id: str) -> str | None:
    try:
        name = _find_by(client.list_channels(), "id", resource_id)
        return f"#{name}" if name else None
    except Exception:
        return None


def _resolve_telegram_chat(client: Any, resource_id: str) -> str | None:
    try:
        import asyncio

        chats = asyncio.run(client.list_chats())
        return _find_by(chats, "id", resource_id)
    except Exception:
        return None


def _resolve_jira_project(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_projects(), "key", resource_id)
    except Exception:
        return None


def _resolve_confluence_space(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_spaces(), "key", resource_id)
    except Exception:
        return None


def _resolve_calendar(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_calendars(), "id", resource_id)
    except Exception:
        return None


def _resolve_salesforce_report(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_reports(), "id", resource_id)
    except Exception:
        return None


@dataclass(frozen=True)
class GrantResourceType:
    """One kind of resource a rule can name the identity of (a Drive folder, a Jira project, ...).

    ``resolver`` is duck-typed against a live connector client instance -- this module never
    imports a connector/client module itself, so it stays importable without any of the optional
    connector dependencies installed (google-api-python-client, slack_sdk, atlassian-python-api).
    """

    connector: str  # e.g. "drive"
    config_key: str  # resource kind within the connector, e.g. "folders", "task_lists"
    capabilities: dict[str, GrantCapability]
    # (client, resource_id) -> display name, or None if not resolvable right now.
    resolver: Callable[[Any, str], str | None]


# Every (operation_key, rule_name) pair a trusted Drive folder's *read* auto-accept covers.
DRIVE_FOLDER_READ_TARGETS: tuple[tuple[str, str], ...] = (
    ("drive.read_file_contents", "approved_folder"),
    ("drive.download_file", "approved_folder"),
    ("sheets.read_values", "approved_folder"),
)

# Every (operation_key, rule_name) pair a trusted Drive "sandbox" folder's *write* auto-accept
# covers, spanning all three tool families that write into a Drive
# file: Drive's own writes, every Sheets write tool, and every Docs write tool. Still the single
# source of truth ``auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS`` derives its own list from -- that
# grace window is unrelated to grants as a *storage* concept but happens to cover the same tool set.
DRIVE_SANDBOX_WRITE_TARGETS: tuple[tuple[str, str], ...] = (
    ("drive.write_file", "approved_sandbox_folder"),
    ("drive.write_doc", "approved_sandbox_folder"),
    ("sheets.write_range", "approved_sandbox_folder"),
    ("sheets.add_sheet", "approved_sandbox_folder"),
    ("sheets.rename_sheet", "approved_sandbox_folder"),
    ("sheets.format_range", "approved_sandbox_folder"),
    ("sheets.insert_dimensions", "approved_sandbox_folder"),
    ("sheets.delete_dimensions", "approved_sandbox_folder"),
    ("docs.edit_content", "approved_sandbox_folder"),
    ("docs.format_content", "approved_sandbox_folder"),
    ("drive.comment_file", "approved_sandbox_folder"),
    ("drive.upload_file", "parent_folder_allowlist"),
    ("drive.move_file", "move_within_approved_folders"),
)

GRANT_RESOURCE_TYPES: tuple[GrantResourceType, ...] = (
    GrantResourceType(
        connector="drive", config_key="folders",
        capabilities={"read": GrantCapability("Read auto-accept", DRIVE_FOLDER_READ_TARGETS)},
        resolver=_resolve_drive_file,
    ),
    GrantResourceType(
        connector="drive", config_key="sandbox_folders",
        capabilities={"write": GrantCapability("Write auto-accept", DRIVE_SANDBOX_WRITE_TARGETS)},
        resolver=_resolve_drive_file,
    ),
    GrantResourceType(
        connector="tasks", config_key="task_lists",
        capabilities={
            "create": GrantCapability("Auto-accept new tasks", (("tasks.create_task", "approved_task_list"),)),
            "edit": GrantCapability("Auto-accept edits", (("tasks.update_task", "approved_task_list"),)),
            "complete": GrantCapability("Auto-accept complete/uncomplete", (
                ("tasks.complete_task", "approved_task_list"),
                ("tasks.uncomplete_task", "approved_task_list"),
            )),
            "move": GrantCapability("Auto-accept moves", (("tasks.move_task", "approved_task_list"),)),
        },
        resolver=_resolve_task_list,
    ),
    GrantResourceType(
        connector="slack", config_key="channels",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("slack.read_messages", "approved_channel"),
                ("slack.read_messages", "approved_channel_all_results"),
            )),
            "send": GrantCapability("Send auto-accept", (("slack.send_message", "approved_recipient"),)),
        },
        resolver=_resolve_slack_channel,
    ),
    GrantResourceType(
        connector="telegram", config_key="chats",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("telegram.read_chat_messages", "approved_chats"),
                ("telegram.read_chat_messages", "approved_chats_all_results"),
            )),
            "send": GrantCapability("Send auto-accept", (("telegram.send_message", "approved_chats"),)),
        },
        resolver=_resolve_telegram_chat,
    ),
    GrantResourceType(
        connector="jira", config_key="projects",
        capabilities={
            "read": GrantCapability("Read auto-accept", (("jira.read_issue", "approved_project_keys"),)),
            "create": GrantCapability("Auto-accept new issues", (("jira.create_issue", "approved_project_keys"),)),
            "comment": GrantCapability("Auto-accept comments", (("jira.add_comment", "approved_project_keys"),)),
            "update": GrantCapability("Auto-accept updates", (("jira.update_issue", "approved_project_keys"),)),
            "transition": GrantCapability("Auto-accept transitions", (
                ("jira.transition_issue", "approved_project_keys"),
            )),
        },
        resolver=_resolve_jira_project,
    ),
    GrantResourceType(
        connector="confluence", config_key="spaces",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("confluence.read_page", "approved_space_keys"),
                ("confluence.download_attachment", "approved_space_keys"),
            )),
            "create": GrantCapability("Auto-accept new pages", (("confluence.create_page", "approved_space_keys"),)),
            "update": GrantCapability("Auto-accept updates", (("confluence.update_page", "approved_space_keys"),)),
        },
        resolver=_resolve_confluence_space,
    ),
    GrantResourceType(
        connector="calendar", config_key="calendars",
        capabilities={
            "read": GrantCapability("Read auto-accept", (("calendar.read_event_details", "personal_calendar"),)),
            "write": GrantCapability("Create/modify auto-accept", (
                ("calendar.create_modify_event", "personal_calendar"),
                ("calendar.set_visibility", "personal_calendar"),
                ("calendar.set_color", "personal_calendar"),
                ("calendar.delete_event", "personal_calendar"),
            )),
        },
        resolver=_resolve_calendar,
    ),
    GrantResourceType(
        connector="salesforce", config_key="reports",
        capabilities={"run": GrantCapability("Read auto-accept", (("salesforce.run_report", "approved_report_ids"),))},
        resolver=_resolve_salesforce_report,
    ),
)

_BY_CONNECTOR_AND_KEY: dict[tuple[str, str], GrantResourceType] = {
    (rt.connector, rt.config_key): rt for rt in GRANT_RESOURCE_TYPES
}


def resource_type(connector: str, config_key: str) -> GrantResourceType | None:
    return _BY_CONNECTOR_AND_KEY.get((connector, config_key))


__all__ = [
    "DRIVE_FOLDER_READ_TARGETS",
    "DRIVE_SANDBOX_WRITE_TARGETS",
    "GRANT_RESOURCE_TYPES",
    "GrantCapability",
    "GrantResourceType",
    "resource_type",
]
