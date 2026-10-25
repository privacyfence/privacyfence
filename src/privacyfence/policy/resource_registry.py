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


# Every resolver below returns a name, or ``None`` only when a lookup that worked found no such
# resource -- the Auto-accept page shows that as a deleted one. A lookup that could not answer (an
# API error, a timeout, a list cut off before the id could appear) raises instead, so a hiccup is
# never shown as a deletion; ``ResourceNameResolver.resolve`` logs it and retries later.

# How many Slack channels / Telegram chats a resolver lists before it gives up. An id missing from
# a list this long may sit past the cut-off, so that miss raises rather than reading as deleted.
_SLACK_CHANNEL_LIMIT = 1000
_TELEGRAM_CHAT_LIMIT = 200
_SALESFORCE_REPORT_LIMIT = 200  # SalesforceClient.list_reports's own LIMIT


class InconclusiveLookup(LookupError):
    """A lookup that answered, but not completely enough to say the resource does not exist."""


def _find_by(items: Any, attr: str, target: str) -> str | None:
    """First item's `attr` where str(item.attr) == target, else None."""
    for item in items or []:
        if str(getattr(item, attr, "")) == target:
            return getattr(item, "name", None) or getattr(item, "title", None) or getattr(item, "summary", None)
    return None


def _find_in_list(items: Any, attr: str, target: str, *, limit: int) -> str | None:
    """``_find_by`` over a list the client cut off at ``limit``: a miss in a full-length list is
    inconclusive, not a deletion."""
    items = list(items or [])
    name = _find_by(items, attr, target)
    if name is None and len(items) >= limit:
        raise InconclusiveLookup(f"{target!r} is not among the first {limit} results")
    return name


def _is_not_found(exc: BaseException) -> bool:
    """True when ``exc``, or an exception it was raised from, is an HTTP 404 -- the provider saying
    there is no such resource. Reads ``requests``' ``response.status_code`` and
    googleapiclient's ``resp.status``."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(getattr(current, "response", None), "status_code", None)
        if status is None:
            status = getattr(getattr(current, "resp", None), "status", None)
        if str(status) == "404":
            return True
        current = current.__cause__ or current.__context__
    return False


def _unless_not_found(lookup: Callable[[], Any]) -> Any:
    """``lookup()``, or ``None`` when it fails with a 404; any other failure raises."""
    try:
        return lookup()
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise


def _resolve_drive_file(client: Any, resource_id: str) -> str | None:
    """``My Drive: name`` or ``<Shared Drive name>: name`` -- a file or folder's origin comes first,
    as the same name can exist in several drives. A Shared Drive's own root shows just the drive's
    name."""
    meta = _unless_not_found(lambda: client.get_file_metadata(resource_id))
    name = getattr(meta, "name", None) if meta is not None else None
    if not name:
        return None
    drive_id = getattr(meta, "drive_id", "")
    if not drive_id:
        return f"My Drive: {name}"
    drive_name = _try_lookup(lambda: client.get_shared_drive_name(drive_id)) or "Shared Drive"
    return drive_name if resource_id == drive_id else f"{drive_name}: {name}"


def _resolve_task_list(client: Any, resource_id: str) -> str | None:
    return _find_by(client.list_task_lists(), "id", resource_id)


def _resolve_slack_channel(client: Any, resource_id: str) -> str | None:
    channels = client.list_channels(max_results=_SLACK_CHANNEL_LIMIT)
    name = _find_in_list(channels, "id", resource_id, limit=_SLACK_CHANNEL_LIMIT)
    return f"#{name}" if name else None


def _resolve_telegram_chat(client: Any, resource_id: str) -> str | None:
    import asyncio

    chats = asyncio.run(client.list_chats(limit=_TELEGRAM_CHAT_LIMIT))
    return _find_in_list(chats, "id", resource_id, limit=_TELEGRAM_CHAT_LIMIT)


def _resolve_jira_project(client: Any, resource_id: str) -> str | None:
    # By key, not by searching list_projects(), which stops at 50 projects.
    project = _unless_not_found(lambda: client.get_project(resource_id))
    return (getattr(project, "name", None) or None) if project is not None else None


def _resolve_assets_schema(client: Any, resource_id: str) -> str | None:
    schema = _unless_not_found(lambda: client.get_schema(resource_id))
    return (getattr(schema, "name", None) or None) if schema is not None else None


def _resolve_confluence_space(client: Any, resource_id: str) -> str | None:
    # By key, not by searching list_spaces(), which stops at 50 spaces.
    space = client.get_space(resource_id)
    return (getattr(space, "name", None) or None) if space is not None else None


def _resolve_calendar(client: Any, resource_id: str) -> str | None:
    return _find_by(client.list_calendars(), "id", resource_id)


def _first_name(*lookups: Callable[[], str | None]) -> str | None:
    """The first lookup that yields a name; one that raises just falls through to the next. When
    none yields a name, ``None`` if at least one of them answered, else the last error is raised:
    lookups that all failed have not shown the resource is gone."""
    error: Exception | None = None
    answered = False
    for lookup in lookups:
        try:
            name = lookup()
        except Exception as exc:
            error = exc
            continue
        if name:
            return name
        answered = True
    if error is not None and not answered:
        raise error
    return None


def _try_lookup(lookup: Callable[[], str | None]) -> str | None:
    try:
        return lookup()
    except Exception:
        return None


def _resolve_salesforce_report(client: Any, resource_id: str) -> str | None:
    # A rule may hold the 15-character form of an ID while Salesforce returns the 18-character one
    # (same 15-character prefix), and the report list is capped, so look the report up by ID first.
    def by_list() -> str | None:
        reports = list(client.list_reports() or [])
        for report in reports:
            rid = str(report.id)
            if rid == resource_id or (len(resource_id) >= 15 and rid[:15] == resource_id[:15]):
                return report.name or None
        if len(reports) >= _SALESFORCE_REPORT_LIMIT:
            raise InconclusiveLookup(f"{resource_id!r} is not among the first {_SALESFORCE_REPORT_LIMIT} reports")
        return None

    return _first_name(lambda: client.get_report_name(resource_id), by_list)


def _resolve_grist_document(client: Any, resource_id: str) -> str | None:
    def by_id() -> str | None:
        document = _unless_not_found(lambda: client.get_document(resource_id))
        return (getattr(document, "name", None) or None) if document is not None else None

    return _first_name(by_id, lambda: _find_by(client.list_documents(), "id", resource_id))


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
    # (client, resource_id) -> display name; None when there is no such resource, and raises when
    # the lookup could not tell (see the resolvers above).
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
    ("drive.copy_file", "move_within_approved_folders"),
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
            "read": GrantCapability("Read auto-accept", (
                ("jira.read_issue", "approved_project_keys"),
                ("jira.read_issue", "approved_project_keys_all_results"),
            )),
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
        connector="assets", config_key="schemas",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("assets.read_object", "approved_schema_ids"),
                ("assets.read_object", "approved_schema_ids_all_results"),
            )),
        },
        resolver=_resolve_assets_schema,
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
    GrantResourceType(
        connector="grist", config_key="documents",
        # Name resolution only: grist.document is governed through the policy engine's own scope,
        # not a grant capability, so the Auto-accept page maps its predicate here by hand
        # (settings_controller.RULE_NAME_TO_RESOURCE_TYPE).
        capabilities={},
        resolver=_resolve_grist_document,
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
