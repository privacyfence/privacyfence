"""Settings state helpers: the connector/privacy/auto-accept constants, the pure state builders, the main-thread dispatcher seam and the audit-row renderer that the settings page and its routes share."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__, agent_label, dialog_window_html, org_mode, web_prompt
from .agent_identity import AgentIdentity, AgentSource
from .apps_script_client import AppsScriptClient
from .approval_ui import get_approval_ui
from .audit_log import AuditEntry, AuditLogger
from .calendar_client import CalendarClient
from .contacts_client import ContactsClient
from .drive_client import DriveClient
from .gmail_client import GmailClient
from .paths import authority_root, data_dir
from .policy import catalogue as policy_catalogue
from .policy import describe as policy_describe
from .policy import propose as policy_propose
from .policy import registry as policy_registry
from .policy import scope_rows as policy_scope_rows
from .policy.engine import PolicyRule
from .policy.resource_registry import (
    GRANT_RESOURCE_TYPES,
    GrantResourceType,
    resource_type as grant_resource_type,
)
from .privacy_filter import _parse_group as _parse_privacy_group
from .privacy_filter import PrivacyFilterConfigError
from .resource_names import ResourceNameResolver
from .tasks_client import TasksClient

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/privacyfence/privacyfence"
LICENSE_NAME = "Apache-2.0"

# All connectors PrivacyFence supports, in display order
ALL_CONNECTORS: list[str] = [
    "gmail", "drive", "contacts", "calendar", "tasks", "apps_script",
    "slack", "jira", "confluence", "assets", "salesforce", "telegram", "grist", "peek", "miro",
]

# web.notifications.detail's own three values (settings.yaml.example,
# ADR 0064) -- see set_notifications_detail below
# and web_shell.py's notificationBody() for what each level is allowed to
# read off a pending-approval row.
NOTIFICATIONS_DETAIL_LEVELS: tuple[str, ...] = ("minimal", "standard", "detailed")

# Connectors authenticated via a shared Google OAuth client (org bundle's
# "google" section).
GOOGLE_CONNECTORS: set[str] = {"gmail", "drive", "contacts", "calendar", "tasks", "apps_script"}

# Display names for connectors whose key isn't just its label lower-cased.
_CONNECTOR_LABEL_OVERRIDES: dict[str, str] = {"apps_script": "Apps Script", "assets": "Jira Assets"}


def connector_label(cname: str) -> str:
    return _CONNECTOR_LABEL_OVERRIDES.get(cname, cname.capitalize())

# Which section of the organization config bundle each connector depends on (ADR 0196). Jira and
# Confluence share one Atlassian OAuth grant. A connector whose section is absent is neither shown
# nor usable.
ORG_CONFIG_SERVICE: dict[str, str] = org_mode.CONNECTOR_ORG_SECTION
ORG_BUNDLE_SERVICES: list[str] = ["google", "slack", "salesforce", "atlassian", "telegram", "grist", "peek", "miro"]

# Categories individually toggleable under the PII Detection Gate, on top of
# its own master enabled switch. Keys match pii_detector._OPTIONAL_CATEGORIES
# and the settings.yaml field names.
PII_OPTIONAL_CATEGORIES: list[tuple[str, str]] = [
    ("detect_ip_addresses", "Detect IP Addresses"),
    ("detect_financial_figures", "Detect Financial Figures"),
]

_GOOGLE_CLIENTS: dict[str, type] = {
    "gmail": GmailClient,
    "drive": DriveClient,
    "calendar": CalendarClient,
    "contacts": ContactsClient,
    "tasks": TasksClient,
    "apps_script": AppsScriptClient,
}

# Display metadata for the Privacy Filter page -- mirrors the group/category
# schema documented in resources/settings.yaml.example and enforced by
# privacy_filter.py. Every group privacy_filter.py knows about needs an
# entry here (and a matching PRIVACY_CATEGORY_LABELS sub-dict) to actually
# show up in the window.
PRIVACY_GROUP_LABELS: dict[str, str] = {
    "privacy": "Gmail",
    "drive_privacy": "Drive & Sheets",
    "slack_privacy": "Slack",
    "contacts_privacy": "Contacts",
    "tasks_privacy": "Tasks",
    "confluence_privacy": "Confluence",
}
PRIVACY_CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "privacy": {
        "body": "Message body",
        "metadata": "Metadata (sender / recipients / date / subject)",
        "attachments": "Attachment metadata",
        "thread_history": "Thread history",
    },
    "drive_privacy": {
        "file_content": "Document content",
        "file_metadata": "File metadata (name / owners / dates / sharing)",
        "file_list": "File list results",
        "folder_structure": "Folder structure",
    },
    "slack_privacy": {
        "message_content": "Message text",
        "user_identity": "User identity (names / emails)",
        "channel_list": "Channel list",
        "thread_content": "Thread replies",
        "dm_list": "DM list",
        "group_chat_list": "Group chat list",
    },
    "contacts_privacy": {
        "notes": "Contact notes (free-text biography)",
    },
    "tasks_privacy": {
        "notes": "Task notes (free-text)",
    },
    "confluence_privacy": {
        "search_excerpt": "Search result excerpt",
        "attachments": "Attachment metadata",
    },
}

# Rule names whose value is the same kind of opaque resource ID a grant entry
# stores (a Drive folder ID, a Jira project key, ...), mapped to the resource
# type that knows how to resolve one to a display name -- so a hand-authored
# rule entry under one of these names still shows a real name instead of a
# raw ID, the same way a grant entry does. Mostly the grant-covered rule
# names, plus a few that hold the same kind of ID but aren't tied to any
# grant capability -- parent_folder_allowlist has no "auto-accept uploads
# into this folder" toggle in the grants UI, it's a hand-authored-only
# allowlist, but its values are still plain Drive folder IDs worth resolving.
RULE_NAME_TO_RESOURCE_TYPE: dict[str, GrantResourceType] = {
    rule_name: rt
    for rt in GRANT_RESOURCE_TYPES
    for capability in rt.capabilities.values()
    for _op_key, rule_name in capability.targets
}
_drive_folder_rt = grant_resource_type("drive", "folders")
assert _drive_folder_rt is not None  # nosec B101  # invariant narrowing, not input validation; "drive"/"folders" is a literal above
RULE_NAME_TO_RESOURCE_TYPE["parent_folder_allowlist"] = _drive_folder_rt
_grist_document_rt = grant_resource_type("grist", "documents")
assert _grist_document_rt is not None  # nosec B101  # invariant narrowing; "grist"/"documents" is a literal above
RULE_NAME_TO_RESOURCE_TYPE["grist.document"] = _grist_document_rt

# Drive/Sheets URLs paste-able into a grant's ID field, so the user can copy
# the browser address bar instead of hand-extracting the ID segment. Order
# matters: a file URL also contains "/d/" so the folder pattern is tried
# first.
_DRIVE_FOLDER_URL_RE = re.compile(r"/folders/([a-zA-Z0-9_-]+)")
_DRIVE_FILE_URL_RE = re.compile(r"/d/([a-zA-Z0-9_-]+)")


def _extract_drive_id(text: str) -> str:
    """Pull a Drive/Sheets file or folder ID out of a pasted URL, or accept
    a bare ID as-is. Returns "" if nothing usable was found."""
    text = text.strip()
    for pattern in (_DRIVE_FOLDER_URL_RE, _DRIVE_FILE_URL_RE):
        m = pattern.search(text)
        if m:
            return m.group(1)
    if text and "/" not in text and " " not in text:
        return text
    return ""


def _short_id(resource_id: str, head: int = 8, tail: int = 6) -> str:
    if len(resource_id) <= head + tail + 1:
        return resource_id
    return f"{resource_id[:head]}…{resource_id[-tail:]}"


def _google_client_config(org_config: dict[str, Any]) -> dict[str, Any]:
    google = org_config.get("google") or {}
    if not google.get("client_id") or not google.get("client_secret"):
        return {}
    return {"installed": google}


# ---------------------------------------------------------------------------- #
# Auto-accept -- one filterable rule list, sentence-rendered by policy.describe. Every rule this
# page writes lands in the on-disk ``auto_accept:`` section (policy.store), the one config section
# every surface (this page, the approval popup's "Always allow", the MCP propose tool) writes.
#
# gate.py's _evaluate_auto_accept checks every rule written here; there is one rule engine and
# no switch between engines.
# ---------------------------------------------------------------------------- #


# The catalogue itself lives in policy/catalogue.py, so the bridge's
# privacyfence_propose_policy_change can share it instead of re-deriving it a second time (see that
# module's own docstring). Re-exported here under private spellings for the callers/tests in this
# file -- which is only these four. Import anything else from ``policy/catalogue.py`` directly
# rather than adding a private alias here.
_POLICY_EXTRA_SCOPES = policy_catalogue.EXTRA_SCOPES
_policy_scope_catalogue = policy_catalogue.scope_catalogue
_parse_verbs = policy_catalogue.parse_verbs
_rules_for_catalogue_entry = policy_catalogue.rules_for_catalogue_entry
_parse_value_list = policy_catalogue.parse_value_list


# ---------------------------------------------------------------------------- #
# The dispatcher seam: every
# background-thread callback in this module needs to marshal back onto "the
# main thread" before touching ``self`` again -- ``self.on_change``/the
# change listeners are expected to push into a live web page, and doing that
# from an arbitrary background thread would race whatever's reading state on
# the ASGI loop. ``call_on_main`` below has exactly one real dispatcher to
# consider.
#
# ``call_on_main`` is the one place that decides where "the main thread"
# actually is: whatever dispatcher daemon_main.py registered via
# set_main_dispatcher() for the web server's own asyncio event loop
# (state_stream.call_soon_threadsafe -- see that module), else -- nothing
# hosting one yet, e.g. this module imported standalone, or a unit test --
# ``fn`` just runs immediately on the calling (background) thread. That
# fallback is correct, not a compromise: there is nothing to protect when no
# host has attached (self.on_change is None and no web listener has
# subscribed), so running inline only changes *when* on_done observably
# runs, not whether it's safe to.
# ---------------------------------------------------------------------------- #

_main_dispatch: Callable[..., None] | None = None


def set_main_dispatcher(dispatch: Callable[..., None] | None) -> None:
    """``dispatch(fn, *args)`` must be safe to call from any thread and must
    arrange for ``fn(*args)`` to run later on whatever thread it is that
    "the main context" means for that dispatcher (the web server's event
    loop, for state_stream.call_soon_threadsafe). Passing ``None`` clears a
    previously-registered dispatcher -- daemon_main.py never needs to
    (there's one process, one web server, for its whole lifetime), but
    tests do, via tests/conftest.py's singleton reset.
    """
    global _main_dispatch
    _main_dispatch = dispatch


def call_on_main(fn: Callable[..., None], *args: Any) -> None:
    """Marshal ``fn(*args)`` onto "the main thread" -- see this section's
    own comment above for what that resolves to and why."""
    if _main_dispatch is not None:
        _main_dispatch(fn, *args)
        return
    fn(*args)


def _pick_resource_index(*, title: str, prompt: str, options: list[str]) -> int | None:
    """The generalized multi-resource picker, driven through whichever
    ``ApprovalUI`` is currently live: register a choice dialog through the
    same registry/blocking mechanism every approval card and confirmation
    already uses (web_prompt.py, dialog_window_html.build_choice_html for
    the markup), so the browser surfaces it as one more pending card with
    the same "no longer pending" landing page an approval link already has.

    ``None`` (no registry -- see approval_ui.py's deferred_registry
    docstring; WebApprovalUI, the only implementation, always has one) is
    what pick_resource's own caller already treats as "fall back to the
    first resource" -- there is no abort path of its own.
    """
    registry = get_approval_ui().deferred_registry
    if registry is None:
        return None
    html = dialog_window_html.build_choice_html(title=title, prompt=prompt, options=options)
    return web_prompt.block_on_choice(registry, html)


def _run_async(work: Callable[[], Any], on_done: Callable[[bool, Any], None]) -> None:
    """Run ``work()`` on a background thread.

    ``on_done(ok, result)`` is called on the main thread via
    ``call_on_main`` -- ``result`` is the return value on success, or the
    raised exception on failure. Never touch ``self``/the page from
    ``work``; do it in ``on_done``.
    """
    def _runner() -> None:
        try:
            result = work()
            call_on_main(on_done, True, result)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user via on_done
            call_on_main(on_done, False, exc)

    threading.Thread(target=_runner, daemon=True).start()


def _relative_time(timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


# ---------------------------------------------------------------------------- #
# State builders with no per-instance dependency (no resolver cache, no
# connector registry) -- pure functions of a config dict, factored out of the
# instance methods below of the same name (ADR 0032) so web/routes_settings.py's
# own org-mode state builder can share them instead of re-deriving the same
# PII-field/privacy-policy/about shape a second time. Each instance method
# further down is now a thin wrapper calling straight through.
# ---------------------------------------------------------------------------- #


def _pii_general_fields(cfg: dict[str, Any]) -> dict[str, Any]:
    """The General page's PII Detection Gate card, both hardcoded fields --
    ``pii_detector.optional_category_keys()`` happens to be exactly these
    two (``detect_ip_addresses``/``detect_financial_figures``) today, which
    is what makes reusing this fixed shape for org mode's own install-wide
    ``pii_detection:`` block accurate rather than merely convenient; see
    that module's own ``_OPTIONAL_CATEGORIES``."""
    pii_cfg = cfg.get("pii_detection", {}) or {}
    return {
        "pii_enabled": pii_cfg.get("enabled", True),
        "pii_ip": pii_cfg.get("detect_ip_addresses", True),
        "pii_financial": pii_cfg.get("detect_financial_figures", True),
    }


def _privacy_state_from_config(cfg: dict[str, Any], *, fail_safe_default: str = "allow") -> dict[str, Any]:
    """``fail_safe_default`` threads straight through to ``_parse_privacy_
    group`` for a group genuinely absent from ``cfg`` -- local mode's own
    call below keeps this module's long-standing "allow" default; org
    mode's caller (web/routes_settings.py's own org state builder) passes
    "block", the same fail-closed posture the former web/org_settings_
    pages.py's ``_privacy_policy_view`` used and this function must keep
    matching now that the two share it."""
    groups = [{"key": g, "label": label} for g, label in PRIVACY_GROUP_LABELS.items()]
    groups.append({"key": "calendar", "label": "Calendar"})

    default_policy: dict[str, str] = {}
    categories: dict[str, list[dict[str, Any]]] = {}
    for group in PRIVACY_GROUP_LABELS:
        try:
            parsed = _parse_privacy_group(cfg.get(group), group=group, fail_safe_default=fail_safe_default)
        except PrivacyFilterConfigError as exc:
            # init_privacy_filter already refused to start the
            # daemon on a malformed group at startup, so reaching this
            # is only possible if settings.yaml was hand-edited on disk
            # to something malformed *after* that -- the live enforced
            # policy (_REGISTRY, still whatever last validated config
            # loaded) is unaffected either way. Render the settings page
            # as "allow" for this group rather than 500ing on it, same
            # defensive posture _load_config() itself already takes for
            # a config file that fails to parse at all.
            logger.warning("Could not render current %s settings: %s", group, exc)
            parsed = {"default_policy": "allow", "categories": {}}
        default_policy[group] = parsed["default_policy"]
        cat_list = []
        for cat_key, cat_label in PRIVACY_CATEGORY_LABELS.get(group, {}).items():
            policy = parsed["categories"].get(cat_key, parsed["default_policy"])
            cat_list.append({"key": cat_key, "label": cat_label, "policy": policy})
        categories[group] = cat_list

    calendar_cfg = cfg.get("calendar", {}) or {}
    gmail_cfg = cfg.get("gmail", {}) or {}
    return {
        "groups": groups,
        "default_policy": default_policy,
        "categories": categories,
        "calendar_free_busy": bool(calendar_cfg.get("free_busy_full_event_details", True)),
        "gmail_append_signature": bool(gmail_cfg.get("append_signature_to_drafts", False)),
    }


def _about_state_dict() -> dict[str, Any]:
    return {
        "version": __version__,
        "license": LICENSE_NAME,
        "repo_url": REPO_URL,
    }


def _rule_usage_map() -> dict[str, Any]:
    """``AuditLogger.rule_usage()``, grouped by ``rule.id``, off *this*
    principal's own ``logs/audit/`` directory -- ``authority_root``/
    ``data_dir`` are themselves principal-scoped (the same contextvar
    ``principal_scope`` sets), so this already reads whichever principal is
    currently scoped when called, local mode's single ``LOCAL_PRINCIPAL``
    or an org caller's own ``with principal_scope(principal):``
    block -- see ``_auto_accept_state_from_rows`` below, this function's
    only caller."""
    log_dir = authority_root(Path(data_dir())) / "logs" / "audit"
    return AuditLogger(str(log_dir)).rule_usage() if log_dir.exists() else {}


def _rule_row(
    row: policy_scope_rows.ScopeRow, usage: dict[str, Any], *, resolved_value: str,
    value_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """One stored scope row as the Auto-accept page's row. ``usage`` maps ids to rule usage; the
    row's count is summed over its own id and every ``former_ids`` entry (the v2 rules merged into
    it), and ``last_matched`` is the latest of them."""
    compiled = policy_scope_rows.compile_rows([row])
    operations = row.operations
    connectors_of_rule = sorted({policy_propose.connector_of_operation(op) for op in operations})
    connector = connectors_of_rule[0] if connectors_of_rule else ""
    matches = [usage[i] for i in (row.id, *row.former_ids) if usage.get(i)]
    count = sum(m.get("count", 0) for m in matches)
    last = max((m["last_matched"] for m in matches if m.get("last_matched")), default=None)
    covered = sorted({tool for rule in compiled for tool in policy_describe.covered_tools(rule)})
    return {
        "id": row.id,
        "sentence": policy_describe.row_sentence(row, value_display=resolved_value),
        # The sentence around the value and the value itself, so the page can show the value by id or
        # by name and color an invalid one; "sentence" above stays the by-name rendering.
        "sentence_parts": sentence_parts(row),
        "value_items": value_items,
        "connector": connector,
        "connector_label": policy_describe.connector_label(connector) if connector else "",
        "scope": row.scope,
        "scope_verbs": [v.value for v in policy_scope_rows.scope_verbs(row.scope)],
        "scope_type": policy_describe.scope_type_of(compiled[0]) if compiled else "",
        "value": resolved_value,
        # Raw (unresolved) value, for the page's right-click-to-copy affordance -- the
        # resolved "value" field above may show a friendly name instead of the id/key a
        # user would actually want to paste elsewhere.
        "value_ids": [str(v) for v in row.value] if isinstance(row.value, list) else (
            [str(row.value)] if row.value else []
        ),
        "verbs": [
            {"verb": verb.value, "family": policy_registry.VERB_FAMILY[verb].value}
            for verb in sorted(row.verbs, key=policy_propose.verb_sort_key)
        ],
        "covered_tools": covered,
        # Tools the row's verbs imply but its stored operations do not reach (ADR 0166).
        "missing_tools": list(policy_scope_rows.missing_tools(row)),
        "match_count": count,
        "last_matched": _relative_time(last) if last else "",
        "never_matched": not matches,
    }


def _auto_accept_state_from_rows(
    rows: list[policy_scope_rows.ScopeRow], usage_by_rule_id: dict[str, Any], *,
    resolve_value: Callable[[policy_scope_rows.ScopeRow], str], resolver: ResourceNameResolver,
) -> dict[str, Any]:
    """The Auto-accept page's state, factored out of ``SettingsController.
    _auto_accept_state`` (ADR 0032) so web/routes_settings.py's own org-mode
    state builder can share it. ``resolve_value`` renders one row's value; both modes
    pass ``cached_row_value`` (resource ids shown by their cached names), and differ only
    in how a missing name gets resolved -- local's ``_resolved_rule_value`` in the background
    with a snapshot push, org's ``web/routes_settings.py`` ``_await_rule_names`` before the
    render, since org mode has no live push."""
    catalogue = _policy_scope_catalogue()
    rule_rows = [
        _rule_row(row, usage_by_rule_id, resolved_value=resolve_value(row), value_items=row_value_items(row, resolver))
        for row in rows
    ]
    rule_rows.sort(key=lambda row: row["sentence"])
    connectors = sorted({row["connector"] for row in rule_rows if row["connector"]}
                         | {entry["connector"] for entry in catalogue})
    return {"rules": rule_rows, "scope_groups": catalogue, "connectors": connectors}


def rule_resource_ids(rule: PolicyRule) -> tuple[GrantResourceType | None, list[str]]:
    """A rule's own value as a list of strings, plus the resource type that can resolve each one
    to a display name (``RULE_NAME_TO_RESOURCE_TYPE``) -- ``None`` when the predicate's values are
    not opaque resource ids (a domain, a label, ...) and are shown as-is."""
    values = rule.value if isinstance(rule.value, list) else ([rule.value] if rule.value else [])
    return RULE_NAME_TO_RESOURCE_TYPE.get(rule.predicate), [str(v) for v in values]


def cached_rule_value(rule: PolicyRule, resolver: ResourceNameResolver) -> str:
    """A rule's value as the Auto-accept page's comma-separated display string: each resource id
    shown by its last-known name (``resolver.cached_name`` -- no network call), or a shortened id
    until one is known. Shared by local mode's ``SettingsController._resolved_rule_value`` and org
    mode's ``web/routes_settings.py`` state builder; the two differ only in how a missing name gets
    resolved, never in how a known one is shown."""
    rt, str_values = rule_resource_ids(rule)
    if rt is None:
        return ", ".join(str_values)
    return ", ".join(resolver.cached_name(rt, v) or _short_id(v) for v in str_values)


def row_value_items(row: policy_scope_rows.ScopeRow, resolver: ResourceNameResolver) -> list[dict[str, Any]]:
    """A row's values as the pages' ID/name toggle needs them: one ``{id, short_id, name, status}``
    per value. ``status`` is ``plain`` for a value that is not an opaque resource id (shown
    as-is), ``resolved`` with a ``name``, ``invalid`` when a live lookup found no such resource (the
    last-known ``name``, if any, is kept), or ``pending`` while no lookup has finished. Reads the
    cache only -- no network call."""
    compiled = policy_scope_rows.compile_row(row)
    if compiled:
        rt, str_values = rule_resource_ids(compiled[0])
    else:
        rt = None
        values = row.value if isinstance(row.value, list) else ([row.value] if row.value else [])
        str_values = [str(v) for v in values]
    items = []
    for value in str_values:
        name = resolver.cached_name(rt, value) if rt is not None else None
        if rt is None:
            status = "plain"
        elif resolver.is_invalid(rt, value):
            status = "invalid"
        else:
            status = "resolved" if name else "pending"
        items.append({"id": value, "short_id": _short_id(value) if rt is not None else value,
                      "name": name, "status": status})
    return items


def sentence_parts(row: policy_scope_rows.ScopeRow) -> list[str] | None:
    """The row's sentence split around its value, ``[before, after]``, so a page can put the value
    -- by id or by name, colored by its status -- in between. ``None`` when the row has no value."""
    if not row.value:
        return None
    marker = "\x00"
    before, found, after = policy_describe.row_sentence(row, value_display=marker).partition(marker)
    return [before, after] if found else None


def cached_row_value(row: policy_scope_rows.ScopeRow, resolver: ResourceNameResolver) -> str:
    """``cached_rule_value`` for a scope row: applied to its first compiled rule, or the raw value
    when the row compiles to nothing."""
    compiled = policy_scope_rows.compile_row(row)
    if compiled:
        return cached_rule_value(compiled[0], resolver)
    values = row.value if isinstance(row.value, list) else ([row.value] if row.value else [])
    return ", ".join(str(v) for v in values)


def _entry_agent(entry: AuditEntry) -> AgentIdentity:
    """The ``AgentIdentity`` an audit entry recorded, rebuilt from its four ``agent_*`` fields. An
    ``agent_source`` this build does not know (a hand-edited or future log line) reads as no
    signal at all -- unknown, never promoted to a tier the entry did not earn."""
    try:
        source = AgentSource(entry.agent_source or "")
    except ValueError:
        source = AgentSource.NONE
    return AgentIdentity(
        id=entry.agent_id or "", name=entry.agent_name or "", version=entry.agent_version or "", source=source,
    )


def audit_rows(entries: list[AuditEntry]) -> list[dict[str, Any]]:
    """The Audit Log page's "Recent decisions" rows -- shared by local mode's
    ``_audit_state`` and org mode's own page state, so both show the same columns. ``agent`` is
    ``agent_label.AgentLabel.to_dict()``: the same tiered wording the approval card and list use
    (``agent_label.py``), raw text the page escapes."""
    return [
        {
            "connector": entry.connector.capitalize() if entry.connector else "",
            "tool": entry.tool_name or entry.tool,
            "decision": entry.decision,
            "time": _relative_time(entry.timestamp),
            "agent": agent_label.label_for(_entry_agent(entry)).to_dict(),
        }
        for entry in entries
    ]
