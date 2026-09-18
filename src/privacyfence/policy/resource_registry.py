"""The connector resource-type manifest -- one row per kind of resource a rule can name an identity
of (a Drive folder, a Jira project, a Slack channel, ...), shared by three callers that each need a
different slice of it (P9 of the policy v2 redesign):

- ``policy.compat.migrate_to_policy_v2`` reads ``expand_grants``/``effective_v1_rules`` to fold a
  not-yet-migrated, hand-edited ``settings.yaml``'s v1 ``auto_accept_grants`` section into v2 rules
  at startup -- "v1 sections stay readable indefinitely for hand-edited installs" (the redesign
  proposal's P4 exit note). Before P9, this same manifest lived in ``resource_grants.py`` as a live,
  writable module: add/remove/describe a grant, resolve a grant row's display name, three surfaces
  (local Settings' old per-connector page, the popup's own bridge alias, org mode's settings page)
  writing to ``auto_accept_grants`` directly. None of that survives -- every rule, wherever it
  originated, now lives in the v2 ``auto_accept:`` section (``policy/store.py``), and every surface
  writes exactly that -- but the manifest itself (which resource types exist, which capability
  compiles to which (operation_key, rule_name) pairs, how to resolve an id to a display name) is
  still the single source of truth for the two live things below.
- ``resource_names.py``'s ``ResourceNameResolver`` calls a ``GrantResourceType``'s ``resolver`` to
  turn an opaque id (a folder id, a project key) into the display name the Auto-accept Settings page
  shows next to a v2 rule's own value (``settings_controller.SettingsController.
  _resolved_rule_value``) -- the same predicate names double as v2 scope predicates
  (``policy/scopes.py``), so the same resource-type manifest resolves either one's value.
- ``gate.propose_rule_change``'s deprecated ``target="grant"`` alias builds v2 rules straight from
  ``GRANT_RESOURCE_TYPES`` the same way migration does, rather than through a grant-entry CRUD API
  that has no live config section to write into anymore.

A new grant-*shaped* feature belongs in ``policy/scopes.py`` as a v2 scope, not as a new entry here
-- this module only ever grows to keep migration and name resolution correct for what v1 could
already express.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class GrantCapability:
    """One toggleable capability of a v1 grant (e.g. "read auto-accept").

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

    connector: str  # top-level key in v1 auto_accept_grants, e.g. "drive"
    config_key: str  # nested key, e.g. "folders", "task_lists"
    id_field: str  # "id" for most resources; "key" for Jira/Confluence
    capabilities: dict[str, GrantCapability]
    # (client, resource_id) -> display name, or None if not resolvable right now.
    resolver: Callable[[Any, str], str | None]
    # Build the rule value contributed by one v1 grant entry. Defaults to just the id.
    value_of: Callable[[dict[str, Any]], Any] = lambda entry: entry  # noqa: E731

    def id_of(self, entry: dict[str, Any]) -> str:
        return str(entry.get(self.id_field, ""))


def _plain_value_of(id_field: str) -> Callable[[dict[str, Any]], Any]:
    return lambda entry: entry.get(id_field, "")


# Canonical enumeration of every (operation_key, rule_name) pair a trusted Drive folder's *read*
# auto-accept covered under v1 -- see the redesign proposal's F1: three v1 rule names
# (approved_folder here, approved_sandbox_folder/parent_folder_allowlist/
# move_within_approved_folders below) for what v2 collapses onto one drive.folder scope type.
DRIVE_FOLDER_READ_TARGETS: tuple[tuple[str, str], ...] = (
    ("drive.read_file_contents", "approved_folder"),
    ("drive.download_file", "approved_folder"),
    ("sheets.read_values", "approved_folder"),
)

# Canonical enumeration of every (operation_key, rule_name) pair a trusted Drive "sandbox" folder's
# *write* auto-accept covered under v1, spanning all three tool families that write into a Drive
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
        connector="drive", config_key="folders", id_field="id",
        capabilities={"read": GrantCapability("Read auto-accept", DRIVE_FOLDER_READ_TARGETS)},
        resolver=_resolve_drive_file, value_of=_plain_value_of("id"),
    ),
    GrantResourceType(
        connector="drive", config_key="sandbox_folders", id_field="id",
        capabilities={"write": GrantCapability("Write auto-accept", DRIVE_SANDBOX_WRITE_TARGETS)},
        resolver=_resolve_drive_file, value_of=_plain_value_of("id"),
    ),
    GrantResourceType(
        connector="tasks", config_key="task_lists", id_field="id",
        capabilities={
            "create": GrantCapability("Auto-accept new tasks", (("tasks.create_task", "approved_task_list"),)),
            "edit": GrantCapability("Auto-accept edits", (("tasks.update_task", "approved_task_list"),)),
            "complete": GrantCapability("Auto-accept complete/uncomplete", (
                ("tasks.complete_task", "approved_task_list"),
                ("tasks.uncomplete_task", "approved_task_list"),
            )),
            "move": GrantCapability("Auto-accept moves", (("tasks.move_task", "approved_task_list"),)),
        },
        resolver=_resolve_task_list, value_of=_plain_value_of("id"),
    ),
    GrantResourceType(
        connector="slack", config_key="channels", id_field="id",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("slack.read_messages", "approved_channel"),
                ("slack.read_messages", "approved_channel_all_results"),
            )),
            "send": GrantCapability("Send auto-accept", (("slack.send_message", "approved_recipient"),)),
        },
        resolver=_resolve_slack_channel, value_of=_plain_value_of("id"),
    ),
    GrantResourceType(
        connector="telegram", config_key="chats", id_field="id",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("telegram.read_chat_messages", "approved_chats"),
                ("telegram.read_chat_messages", "approved_chats_all_results"),
            )),
            "send": GrantCapability("Send auto-accept", (("telegram.send_message", "approved_chats"),)),
        },
        resolver=_resolve_telegram_chat, value_of=_plain_value_of("id"),
    ),
    GrantResourceType(
        connector="jira", config_key="projects", id_field="key",
        capabilities={
            "read": GrantCapability("Read auto-accept", (("jira.read_issue", "approved_project_keys"),)),
            "create": GrantCapability("Auto-accept new issues", (("jira.create_issue", "approved_project_keys"),)),
            "comment": GrantCapability("Auto-accept comments", (("jira.add_comment", "approved_project_keys"),)),
            "update": GrantCapability("Auto-accept updates", (("jira.update_issue", "approved_project_keys"),)),
            "transition": GrantCapability("Auto-accept transitions", (
                ("jira.transition_issue", "approved_project_keys"),
            )),
        },
        resolver=_resolve_jira_project, value_of=_plain_value_of("key"),
    ),
    GrantResourceType(
        connector="confluence", config_key="spaces", id_field="key",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("confluence.read_page", "approved_space_keys"),
                ("confluence.download_attachment", "approved_space_keys"),
            )),
            "create": GrantCapability("Auto-accept new pages", (("confluence.create_page", "approved_space_keys"),)),
            "update": GrantCapability("Auto-accept updates", (("confluence.update_page", "approved_space_keys"),)),
        },
        resolver=_resolve_confluence_space, value_of=_plain_value_of("key"),
    ),
    GrantResourceType(
        connector="calendar", config_key="calendars", id_field="id",
        capabilities={
            "read": GrantCapability("Read auto-accept", (("calendar.read_event_details", "personal_calendar"),)),
            "write": GrantCapability("Create/modify auto-accept", (
                ("calendar.create_modify_event", "personal_calendar"),
                ("calendar.set_visibility", "personal_calendar"),
                ("calendar.set_color", "personal_calendar"),
                ("calendar.delete_event", "personal_calendar"),
            )),
        },
        resolver=_resolve_calendar, value_of=_plain_value_of("id"),
    ),
    GrantResourceType(
        connector="salesforce", config_key="reports", id_field="id",
        capabilities={"run": GrantCapability("Read auto-accept", (("salesforce.run_report", "approved_report_ids"),))},
        resolver=_resolve_salesforce_report, value_of=_plain_value_of("id"),
    ),
)

_BY_CONNECTOR_AND_KEY: dict[tuple[str, str], GrantResourceType] = {
    (rt.connector, rt.config_key): rt for rt in GRANT_RESOURCE_TYPES
}


def resource_type(connector: str, config_key: str) -> GrantResourceType | None:
    return _BY_CONNECTOR_AND_KEY.get((connector, config_key))


def get_grant_entries(grants_cfg: dict[str, Any], rt: GrantResourceType) -> list[dict[str, Any]]:
    return list((grants_cfg.get(rt.connector) or {}).get(rt.config_key) or [])


def expand_grants(grants_cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Compile a v1 ``auto_accept_grants`` section into the ``{operation_key: [{"rule", "value"},
    ...]}`` shape ``policy.compat.compile_rules`` already knows how to turn into v2 ``PolicyRule``s
    -- migration's read of a grant is otherwise identical to its read of a plain
    ``auto_accept_rules`` entry."""
    buckets: dict[tuple[str, str], list[Any]] = {}
    for rt in GRANT_RESOURCE_TYPES:
        entries = get_grant_entries(grants_cfg, rt)
        if not entries:
            continue
        for capability_key, capability in rt.capabilities.items():
            values = []
            for entry in entries:
                if not entry.get(capability_key):
                    continue
                value = rt.value_of(entry)
                if value not in values:
                    values.append(value)
            if not values:
                continue
            for op_key, rule_name in capability.targets:
                bucket = buckets.setdefault((op_key, rule_name), [])
                for value in values:
                    if value not in bucket:
                        bucket.append(value)

    compiled: dict[str, list[dict[str, Any]]] = {}
    for (op_key, rule_name), values in buckets.items():
        compiled.setdefault(op_key, []).append({"rule": rule_name, "value": values})
    return compiled


def effective_v1_rules(cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Everything a not-yet-migrated config's ``auto_accept_rules`` plus grant-expanded
    ``auto_accept_grants`` amount to -- what ``policy.compat.migrate_to_policy_v2`` compiles from.
    Replaces ``resource_grants.build_effective_rules``, read-only and migration-only."""
    rules: dict[str, list[dict[str, Any]]] = {
        op_key: [dict(r) for r in (op_rules or [])]
        for op_key, op_rules in (cfg.get("auto_accept_rules") or {}).items()
    }
    for op_key, entries in expand_grants(cfg.get("auto_accept_grants") or {}).items():
        rules.setdefault(op_key, []).extend(entries)
    return rules


__all__ = [
    "DRIVE_FOLDER_READ_TARGETS",
    "DRIVE_SANDBOX_WRITE_TARGETS",
    "GRANT_RESOURCE_TYPES",
    "GrantCapability",
    "GrantResourceType",
    "effective_v1_rules",
    "expand_grants",
    "get_grant_entries",
    "resource_type",
]
