"""Domain/business logic behind the web settings page (web/routes_settings.py).

Through P9 this also backed a native macOS webview settings window
(settings_window.py); P10 (D6) deleted that host along with the rest of the AppKit UI layer, leaving the
web settings page (when ``web.settings.enabled`` is set) as the only way to
drive this controller interactively -- editing ``config/settings.yaml`` by
hand remains the headless path either way. This module itself was already
headless-first before that (see docs/coding-and-testing-guidelines.md's
"stay dependency-light" pattern also used by policy/resource_registry.py/
privacy_filter.py) and needed no AppKit/PyObjC imports of its own to begin
with -- ``rumps``/``dialog_window``/``PyObjCTools.AppHelper`` were the
native host's own dependencies, imported here only to marshal callbacks onto
its run loop and to host the Atlassian multi-resource picker (issue #145);
none of that is needed anymore, see ``call_on_main``/``_pick_resource_index``
below.

``SettingsController`` holds the same instance state ``PrivacyFenceMenuBar``
used to hold directly pre-#120, with one method per mutation the old NSMenu
tree performed (see menu_bar.py's git history pre-#120 for the shape this
was extracted from) -- every mutating method follows load config -> mutate
-> save config -> hot-reload -> return a fresh ``snapshot()`` for the caller
(web/routes_settings.py) to push into the page. Long-running work (OAuth
flows, grant name resolution) runs on a background thread via
``_run_async``, with the result marshaled back onto "the main thread" (see
``call_on_main``) before this module touches ``self`` again.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from . import __version__, agent_label, dialog_window_html, org_bundle_signing, org_mode, web_prompt
from .agent_identity import AgentIdentity, AgentSource
from .app_credentials import telegram_app_credentials
from .apps_script_client import AppsScriptClient
from .approval_ui import get_approval_ui
from .audit_log import AuditEntry, AuditLogger, compute_security_config_hash, current_week, get_audit_logger
from .auto_accept import (
    notify_rules_changed,
    set_policy_v2_store_rules,
    set_rules_changed_listener,
)
from .calendar_client import CalendarClient
from .contacts_client import ContactsClient
from .drive_client import DriveClient
from .gmail_client import GmailClient
from .paths import authority_root, data_dir, org_dir
from .pii_detector import set_pii_category_enabled, set_pii_detection_enabled
from .policy import catalogue as policy_catalogue
from .policy import describe as policy_describe
from .policy import propose as policy_propose
from .policy import registry as policy_registry
from .policy import store as policy_store
from .policy.engine import PolicyRule
from .principal import LOCAL_PRINCIPAL
from .privacy_filter import _parse_group as _parse_privacy_group
from .privacy_filter import _VALID_POLICIES as PRIVACY_POLICIES
from .privacy_filter import init_privacy_filter
from .privacy_filter import PrivacyFilterConfigError
from .policy.resource_registry import (
    GRANT_RESOURCE_TYPES,
    GrantResourceType,
    resource_type as grant_resource_type,
)
from .resource_names import ResourceNameResolver, get_resolver
from .secure_files import atomic_write_json, atomic_write_text
from .step_up_config import LiveStepUpConfig, StepUpConfig
from . import telegram_auth
from .tasks_client import TasksClient
from .update_checker import (
    UpdateCheckResult,
    check_for_update,
    mark_remind_later,
    mark_skipped,
)
from .webauthn_stepup import has_credentials as _has_webauthn_credentials
from .webauthn_stepup import observe_step_up_requirement
from .atlassian_oauth import authorize_interactive as atlassian_authorize_interactive
from .salesforce_client import authorize_interactive as salesforce_authorize_interactive
from .slack_client import authorize_interactive as slack_authorize_interactive

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/privacyfence/privacyfence"
LICENSE_NAME = "Apache-2.0"

# All connectors PrivacyFence supports, in display order
ALL_CONNECTORS: list[str] = [
    "gmail", "drive", "contacts", "calendar", "tasks", "apps_script",
    "slack", "jira", "confluence", "salesforce", "telegram",
]

# web.notifications.detail's own three values (settings.yaml.example,
# 5deef1d8:docs/approval-list-ui-ux.md §4.3) -- see set_notifications_detail below
# and web_shell.py's notificationBody() for what each level is allowed to
# read off a pending-approval row.
NOTIFICATIONS_DETAIL_LEVELS: tuple[str, ...] = ("minimal", "standard", "detailed")

# Connectors authenticated via a shared Google OAuth client (org bundle's
# "google" section).
GOOGLE_CONNECTORS: set[str] = {"gmail", "drive", "contacts", "calendar", "tasks", "apps_script"}

# Display names for connectors whose key isn't just its label lower-cased.
_CONNECTOR_LABEL_OVERRIDES: dict[str, str] = {"apps_script": "Apps Script"}


def connector_label(cname: str) -> str:
    return _CONNECTOR_LABEL_OVERRIDES.get(cname, cname.capitalize())

# Which section of the organization config bundle each connector depends on.
# Jira and Confluence share one Atlassian OAuth grant. Telegram is not part
# of the org bundle -- its app credentials are baked into the build (see
# app_credentials.py) and checked separately.
ORG_CONFIG_SERVICE: dict[str, str] = {
    "gmail": "google", "drive": "google", "contacts": "google",
    "calendar": "google", "tasks": "google", "apps_script": "google",
    "slack": "slack",
    "jira": "atlassian", "confluence": "atlassian",
    "salesforce": "salesforce",
}
ORG_BUNDLE_SERVICES: list[str] = ["google", "slack", "salesforce", "atlassian"]

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
# gate.py's _evaluate_auto_accept checks a rule written here unconditionally, regardless of which
# engine ``policy.engine`` names authoritative -- see auto_accept.set_policy_v2_store_rules's own
# docstring for why: a v2-only rule (in particular, anything using one of the three P6-only
# predicates below) has no v1 shadow to be gated behind.
# ---------------------------------------------------------------------------- #


# The catalogue itself moved to policy/catalogue.py at P7, so the bridge's
# privacyfence_propose_policy_change can share it instead of re-deriving it a second time (see that
# module's own docstring). Re-exported here under their original, private spellings so every
# existing caller/test in this file keeps working unchanged -- which is only these four. P7's
# original block carried three more (``_PolicyExtraScope``, ``_POLICY_VALUE_HINTS``,
# ``_extra_operations_for``); nothing referenced them under either spelling, so they were aliases
# kept for callers that had already moved. Import them from ``policy/catalogue.py`` directly if
# one ever needs them again, rather than re-adding a private alias here.
_POLICY_EXTRA_SCOPES = policy_catalogue.EXTRA_SCOPES
_policy_scope_catalogue = policy_catalogue.scope_catalogue
_parse_verbs = policy_catalogue.parse_verbs
_rules_for_catalogue_entry = policy_catalogue.rules_for_catalogue_entry


# ---------------------------------------------------------------------------- #
# The dispatcher seam: every
# background-thread callback in this module needs to marshal back onto "the
# main thread" before touching ``self`` again -- ``self.on_change``/the
# change listeners are expected to push into a live web page, and doing that
# from an arbitrary background thread would race whatever's reading state on
# the ASGI loop. Through P9 this could also mean AppKit's own run loop (the
# native settings window, via ``PyObjCTools.AppHelper.callAfter``); P10
# deleted that host, so ``call_on_main`` below now has exactly one real
# dispatcher to consider.
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
    """§16.2.2's generalized multi-resource picker, driven through whichever
    ``ApprovalUI`` is currently live: register a choice dialog through the
    same registry/blocking mechanism every approval card and confirmation
    already uses (web_prompt.py, dialog_window_html.build_choice_html for
    the markup), so the browser surfaces it as one more pending card with
    the same "no longer pending" landing page an approval link already has.

    ``None`` (no registry -- see approval_ui.py's deferred_registry
    docstring; WebApprovalUI, the only implementation since P10, always has
    one) is what pick_resource's own caller already treats as "fall back to
    the first resource" -- there was never an abort path of its own to
    preserve here either. Through P9 a second branch here fell back to
    dialog_window.show_choice_dialog when no web registry was active and
    pyobjc was present; P10 deleted that native picker along with the rest
    of the AppKit UI layer.
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
    ``work``; do it in ``on_done``. Relocated from menu_bar.py's identical
    helper -- see its own pre-#120 history.
    """
    def _runner() -> None:
        try:
            result = work()
            call_on_main(on_done, True, result)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user via on_done
            call_on_main(on_done, False, exc)

    threading.Thread(target=_runner, daemon=True).start()


def _parse_value_list(raw_text: str) -> list[str] | None:
    """The Auto-accept page's "value" field (a comma-separated list of resource ids/keys/domains)
    -> a v2 rule value, or ``None`` for a value-less scope's empty field. Every v2 identity/valued-
    attribute scope this page can write (``policy.propose.SCOPES_BY_GROUP`` plus the P6 extras --
    see ``_policy_scope_catalogue``) takes a plain list of strings; there is no int-valued scope in
    that catalogue the way v1's ``age_threshold_days``/``time_window_days`` condition rules were,
    since this page writes scopes, not conditions (see this module's own Auto-accept section
    docstring)."""
    values = [v.strip() for v in (raw_text or "").split(",") if v.strip()]
    return values or None


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
# instance methods below of the same name (PSC-5) so web/routes_settings.py's
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
            # init_privacy_filter (SEC-07) already refused to start the
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
    or (PSC-5) an org caller's own ``with principal_scope(principal):``
    block -- see ``_auto_accept_state_from_rules`` below, this function's
    only caller."""
    log_dir = authority_root(Path(data_dir())) / "logs" / "audit"
    return AuditLogger(str(log_dir)).rule_usage() if log_dir.exists() else {}


def _rule_row(rule: PolicyRule, usage: dict[str, Any], *, resolved_value: str) -> dict[str, Any]:
    connectors_of_rule = sorted({policy_propose.connector_of_operation(op) for op in rule.operations})
    connector = connectors_of_rule[0] if connectors_of_rule else ""
    return {
        "id": rule.id,
        "sentence": policy_describe.rule_sentence(rule, value_display=resolved_value),
        "connector": connector,
        "connector_label": policy_describe.connector_label(connector) if connector else "",
        "scope_type": policy_describe.scope_type_of(rule),
        "value": resolved_value,
        # Raw (unresolved) value, for the page's right-click-to-copy affordance -- the
        # resolved "value" field above may show a friendly name instead of the id/key a
        # user would actually want to paste elsewhere.
        "value_ids": [str(v) for v in rule.value] if isinstance(rule.value, list) else (
            [str(rule.value)] if rule.value else []
        ),
        "verbs": [
            {"verb": verb.value, "family": policy_registry.VERB_FAMILY[verb].value}
            for verb in policy_describe.rule_verbs(rule)
        ],
        "covered_tools": list(policy_describe.covered_tools(rule)),
        "match_count": usage.get("count", 0),
        "last_matched": _relative_time(usage["last_matched"]) if usage else "",
        "never_matched": not usage,
    }


def _auto_accept_state_from_rules(
    rules: list[PolicyRule], usage_by_rule_id: dict[str, Any], *,
    resolve_value: Callable[[PolicyRule], str],
) -> dict[str, Any]:
    """The Auto-accept page's state, factored out of ``SettingsController.
    _auto_accept_state`` (PSC-5) so web/routes_settings.py's own org-mode
    state builder can share it. ``resolve_value`` renders one rule's value; both modes
    pass ``cached_rule_value`` (resource ids shown by their cached names), and differ only
    in how a missing name gets resolved -- local's ``_resolved_rule_value`` in the background
    with a snapshot push, org's ``web/routes_settings.py`` ``_await_rule_names`` before the
    render, since org mode has no live push."""
    catalogue = _policy_scope_catalogue()
    rule_rows = [
        _rule_row(rule, usage_by_rule_id.get(rule.id) or {}, resolved_value=resolve_value(rule))
        for rule in rules
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


# ---------------------------------------------------------------------------- #
# Controller
# ---------------------------------------------------------------------------- #

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
    """The Audit Log page's "Recent decisions" rows (AGT-5) -- shared by local mode's
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


class SettingsController:
    """Domain logic for the settings page (web/routes_settings.py). One
    instance, built once by daemon_main.py's run_app() for the daemon's
    whole lifetime -- so ``set_rules_changed_listener`` is registered from
    __init__ here unconditionally, regardless of whether ``web.settings.
    enabled`` ever opts a browser into looking at it. ``wire_unattended_
    listener`` (below) is the equivalent wiring for unattended-session
    changes, but is *not* done from __init__: since P5 retired the bridge,
    the only thing that can ever produce an unattended session is
    web/mcp_dispatch.py's McpDispatcher, and whether one even exists
    depends on web.mcp.enabled -- something this constructor has no
    visibility into. daemon_main.py's own _maybe_start_web_server calls it
    once a dispatcher is actually built.

    ``on_change``, when set, is called with a fresh ``snapshot()`` whenever
    something changes the state out from under a currently-open page (a
    rule added via the approval popup's Always allow, a background auth
    flow finishing, an unattended session starting/ending) -- a simple
    single-callback slot, distinct from ``add_change_listener`` below
    (which the web surface actually uses, since more than one consumer can
    be attached at once); kept as its own attribute mainly because it's a
    convenient single hook for a test to observe ``_push_snapshot()``
    calls. Through P9 this was also how the native settings window wired
    itself in (``controller.on_change = self._push_state``); P10 deleted
    that host, so nothing sets this in a real deployment anymore, but the
    slot itself costs nothing to keep.
    """

    def __init__(
        self,
        config_path: str,
        connectors: list[str],
        connector_host: Any,
        connector_objs: list[Any] | None = None,
        connector_failures: dict[str, str] | None = None,
    ) -> None:
        self._config_path = config_path
        self._connectors = connectors
        self.connector_host = connector_host
        # name -> live Connector wrapper (exposes .client for resolving
        # grant resource names -- see resource_names.py). Populated at
        # startup from daemon_main.py's already-built connectors, refreshed
        # whenever refresh_connectors() re-authenticates/toggles one.
        self._connector_objs: dict[str, Any] = {c.name: c for c in (connector_objs or [])}
        # name -> "no_org_config" | "not_authenticated" | a redacted
        # message, for every *enabled* connector build_connectors() didn't
        # end up producing (issue #396 Phase 1) -- read by _connectors_state
        # below as each row's blocked_by, so "never set up" and "auth
        # expired" stop looking identical to a client asking why a
        # connector is missing. Same refresh cadence as _connector_objs.
        self._connector_failures: dict[str, str] = dict(connector_failures or {})
        self._resolver = get_resolver()
        # Latest known update-check outcome -- None until the first check
        # completes (or forever, if update checking is disabled). The
        # design's General page has no "update available" banner yet (see
        # this module's own docstring/the PR report for that gap) -- this is
        # kept so a future pass can surface it without re-plumbing.
        self._latest_update: UpdateCheckResult | None = None
        # Connector keys with an authenticate/refresh flow currently
        # in flight -- surfaced in snapshot()'s connectors[].busy so the
        # window can disable/spin that row instead of double-firing.
        self._busy_connectors: set[str] = set()
        # Last-seen failure message, surfaced as a small dismissable banner
        # by the page (see settings_window_html.py) -- the design has no
        # toast/error UI of its own, and simply dropping this would silently
        # regress error visibility (see the PR report's "error surfacing"
        # scope note).
        self.error: str = ""
        # Telegram's in-progress phone/code/2FA sign-in, or None when no
        # sign-in is running -- see telegram_start_auth/telegram_submit_code/
        # telegram_submit_2fa/telegram_cancel_auth below and _telegram_auth_
        # state's own docstring for the shape.
        self._telegram_auth: dict[str, Any] | None = None
        # Kept as a single settable slot -- see this class's own docstring
        # for why it's distinct from add_change_listener below.
        self.on_change: Callable[[dict[str, Any]], None] | None = None
        # Additional listeners, notified alongside on_change (§16.8's risk
        # #2: "two on_change consumers... make it a list, test that both
        # fire") -- daemon_main.py subscribes web/state_stream.py's push
        # here so a rule changed over MCP, an OAuth flow finishing, or any
        # other background outcome reaches an open browser tab exactly the
        # way it already reaches an open native window.
        self._change_listeners: list[Callable[[dict[str, Any]], None]] = []
        # issue #396 Part C: fired by refresh_connectors()'s own done()
        # callback whenever the live connector set actually gets swapped --
        # wired to McpDispatcher.notify_tools_changed by daemon_main.py once
        # an MCP dispatcher exists (see set_connectors_changed_listener's
        # own docstring), so an open MCP client learns about newly (or no
        # longer) authenticated connectors without needing to reconnect.
        self._connectors_changed_listener: Callable[[], None] | None = None
        # B9: wired in by wire_step_up (see that method's own docstring for
        # why not a constructor argument) -- None until then, which makes
        # enable_step_up a safe no-op in every test/caller that never wires
        # step-up at all.
        self._step_up: LiveStepUpConfig | None = None

        set_rules_changed_listener(self._on_rules_changed)

    def wire_unattended_listener(self, dispatcher: Any) -> None:
        """Register this controller's push-on-change with ``dispatcher``
        (web.mcp_dispatch.McpDispatcher) -- called by daemon_main.py's
        _maybe_start_web_server once it knows a dispatcher actually exists
        (web.mcp.enabled), the direct successor of what this constructor
        used to do itself, unconditionally, with ipc_server.py's IPCServer
        before P5 retired it."""
        dispatcher.set_unattended_changed_listener(self._on_unattended_changed)

    def wire_step_up(self, step_up: LiveStepUpConfig) -> None:
        """B9: registers the same ``LiveStepUpConfig`` daemon_main.py's
        local-mode boot path hands to web/server.py's ``WebServer`` (and,
        through it, to every route that gates on step-up) -- called from
        ``_maybe_start_web_server`` once that object exists, the same
        after-the-fact wiring ``wire_unattended_listener`` above already
        uses for a dependency this constructor has no way to see yet.
        ``enable_step_up`` below writes through this same object so a
        change it makes to ``config/settings.yaml`` is visible to every
        other consumer on their very next request, with no daemon restart
        -- see step_up_config.py's own ``LiveStepUpConfig`` docstring."""
        self._step_up = step_up

    def set_connectors_changed_listener(self, callback: Callable[[], None] | None) -> None:
        """``callback`` is ``McpDispatcher.notify_tools_changed`` in
        production (issue #396 Part C) -- called, on the main thread, right
        after refresh_connectors() swaps in a freshly-built connector set,
        so an already-connected MCP client is told its tool list changed
        instead of needing a restart to see it. Wired by daemon_main.py
        once an MCP dispatcher exists, the same "wired a step after both
        objects exist" shape wire_unattended_listener above and
        McpDispatcher.set_bootstrap_link_provider both already use. ``None``
        (org mode's per-principal path today, or a test that never wires
        one) makes refresh_connectors() simply skip the notification --
        the connector set still changes, nothing is told about it."""
        self._connectors_changed_listener = callback

    # ------------------------------------------------------------------ #
    # Cross-thread change notifications
    # ------------------------------------------------------------------ #

    def _on_rules_changed(self) -> None:
        """Fired by auto_accept.notify_rules_changed(), possibly from the web
        server's own asyncio thread -- marshal the state push onto the
        main thread."""
        call_on_main(self._push_snapshot)

    def _on_unattended_changed(self) -> None:
        """Fired by web/mcp_dispatch.py's McpDispatcher, on its own asyncio
        thread. No page of the current design surfaces the
        unattended-session count (the old tray menu's top status line is
        gone) -- kept wired for a future pass, same "plumbing survives, UI
        doesn't exist yet" posture as _latest_update above."""
        call_on_main(self._push_snapshot)

    def add_change_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._change_listeners.append(fn)

    def remove_change_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        try:
            self._change_listeners.remove(fn)
        except ValueError:
            pass

    def _push_snapshot(self) -> None:
        state = self.snapshot()
        if self.on_change is not None:
            self.on_change(state)
        for listener in list(self._change_listeners):
            listener(state)

    # ------------------------------------------------------------------ #
    # Config helpers
    # ------------------------------------------------------------------ #

    def _load_config(self) -> dict:
        try:
            with open(self._config_path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Could not load config: %s", exc)
            return {}

    def _save_config(self, cfg: dict) -> None:
        try:
            atomic_write_text(
                self._config_path, yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True),
            )
        except Exception as exc:
            logger.warning("Could not save config: %s", exc)
            return
        try:
            # SEC-23: every settings.yaml write is a privacy-policy change,
            # so the fingerprint every *new* audit entry carries
            # (AuditEntry.security_config_hash) needs to move with it --
            # daemon_main.run_app() only stamps this AuditLogger with a
            # startup-time snapshot, and this is the one place (below every
            # settings.yaml writer -- _save_and_reload, _save_and_reload_
            # privacy, toggle_pii_detection, ...) that a change actually
            # lands on disk.
            get_audit_logger().set_security_config_hash(compute_security_config_hash(cfg))
        except Exception as exc:
            logger.warning("Could not update audit log's security-config hash: %s", exc)

    def _save_and_reload(self, cfg: dict) -> None:
        self._save_config(cfg)
        try:
            # Triggers a snapshot push, so callers don't need a separate explicit push after this.
            notify_rules_changed()
        except Exception as exc:
            logger.warning("Rule hot-reload failed: %s", exc)

    def _save_and_reload_privacy(self, cfg: dict) -> None:
        self._save_config(cfg)
        try:
            init_privacy_filter(cfg)
        except Exception as exc:
            logger.warning("Privacy filter hot-reload failed: %s", exc)

    def _client_for(self, connector: str) -> Any | None:
        """Live client for a connected connector (for resolving/listing
        grant resources), or None if that connector isn't currently
        connected."""
        conn = self._connector_objs.get(connector)
        return getattr(conn, "client", None) if conn is not None else None

    # ------------------------------------------------------------------ #
    # PII detection gate
    # ------------------------------------------------------------------ #

    def toggle_pii_detection(self) -> dict[str, Any]:
        cfg = self._load_config()
        pii_cfg = cfg.setdefault("pii_detection", {})
        enabled = not pii_cfg.get("enabled", True)
        pii_cfg["enabled"] = enabled
        self._save_config(cfg)
        set_pii_detection_enabled(enabled)
        return self.snapshot()

    def toggle_pii_category(self, category_key: str) -> dict[str, Any]:
        cfg = self._load_config()
        pii_cfg = cfg.setdefault("pii_detection", {})
        if not pii_cfg.get("enabled", True):
            # Mirrors the old submenu's grayed-out-without-a-callback state
            # while the master switch is off -- these two categories are
            # meaningless without it.
            return self.snapshot()
        enabled = not pii_cfg.get(category_key, True)
        pii_cfg[category_key] = enabled
        self._save_config(cfg)
        set_pii_category_enabled(category_key, enabled)
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Step-up (WebAuthn) enforcement -- B9 of the 4.1.0 action plan
    # ------------------------------------------------------------------ #

    def enable_step_up(self) -> dict[str, Any]:
        """The browser-reachable counterpart to hand-editing ``config/
        settings.yaml``'s own ``step_up:`` section, which #426 shipped
        enrollment and enforcement for and then left with no way to turn on
        short of a shell (B9's own problem statement -- see step_up_
        config.py's ``LiveStepUpConfig`` docstring for the mechanism this
        method writes through). Always sets *both* ``enabled`` and
        ``require_passkey`` together, never one alone: local mode has no
        IdP fallback, so an ``enabled=True, require_passkey=False`` install
        enforces nothing beyond what ``enabled=False`` already didn't (see
        ``StepUpConfig.from_local_config``'s own docstring) -- "turn
        step-up on, and make it mandatory" (B9's "done when" wording) is
        one action here, not two.

        Refuses -- config untouched, ``self.error`` set, same failure
        surfacing every other guarded action in this class uses -- unless a
        passkey is already enrolled: flipping ``require_passkey`` on with
        nothing enrolled would immediately lock every write approval and
        every sensitive settings action behind a passkey ceremony nobody
        can complete, exactly the state ``local_enrollment_banner`` exists
        to warn about. settings_window_html.py's own Security card never
        renders this action's control before ``self.snapshot()['general']
        ['step_up_has_passkey']`` is true -- this is the same check run
        again server-side, for a stray POST past that client-side gate,
        not the only place it happens.

        Turning step-up back *off* has no counterpart here, deliberately:
        it stays a ``config/settings.yaml`` edit plus a restart, the one
        signal webauthn_stepup.observe_step_up_requirement's "treat this
        install as compromised" banner watches for (see that module's own
        docstring) -- a UI path that could also produce a disable would
        make that banner impossible to trust.
        """
        if self._step_up is None or not _has_webauthn_credentials(LOCAL_PRINCIPAL):
            self.error = "Add a passkey at /security before turning step-up on."
            return self.snapshot()
        cfg = self._load_config()
        step_up_cfg = cfg.setdefault("step_up", {})
        step_up_cfg["enabled"] = True
        step_up_cfg["require_passkey"] = True
        # ADR 0003, "Why not gate the passkey instead": StepUpConfig.
        # from_local_config() itself now refuses require_passkey on an
        # unseparated install -- this is that check's own Settings-page
        # entry point (ADR 0003's Context #2: "step_up.require_passkey is
        # reachable from the Settings page of an unseparated install").
        # Validated *before* _save_config() below, not after: persisting
        # require_passkey: true and only then discovering it refuses to
        # parse would leave settings.yaml in a state the daemon refuses to
        # load on its very next start.
        try:
            new_step_up = StepUpConfig.from_local_config(cfg)
        except org_mode.ConfigurationError as exc:
            self.error = str(exc)
            return self.snapshot()
        self._save_config(cfg)
        self._step_up.update(new_step_up)
        self.error = ""
        # #426 Phase 4's own tracking -- called here, not just left for the
        # next daemon startup to notice, so this transition is audited the
        # moment it happens (see webauthn_stepup.py's own module docstring
        # on why an *enable* observed outside startup is expected now,
        # unlike a disable).
        if observe_step_up_requirement(LOCAL_PRINCIPAL, enabled=True, require_passkey=True) is not None:
            self._audit_step_up_enabled()
        return self.snapshot()

    def _audit_step_up_enabled(self) -> None:
        """Same shape as daemon_main.py's own module-level
        ``_audit_step_up_requirement_change`` (that function's own
        docstring covers why this transition needs its own audit entry
        rather than reusing ``_save_config``'s security-config-hash bump
        above, which records *that* config changed, not *what* changed) --
        duplicated rather than imported because daemon_main.py already
        imports this module to build SettingsController, and this module
        importing back would be circular. Simpler than that function: this
        is only ever reached right after ``enable_step_up`` set both flags
        to ``True``, so there is only the one outcome to record, not a
        general enabled/disabled branch."""
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary="local mode's step_up.require_passkey is now enforced",
                sender="",
                decision="step_up_requirement_enabled",
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for step_up_requirement_enabled: %s", exc)

    # ------------------------------------------------------------------ #
    # Approval notifications (web.notifications -- see web_shell.py's
    # notificationBody() for what each detail level is allowed to say)
    # ------------------------------------------------------------------ #

    def set_notifications_detail(self, level: str) -> dict[str, Any]:
        """The Approval Notifications card's segmented control
        (settings_window_html.py's renderNotificationsCard) -- same
        validate/persist/return-snapshot shape as set_log_level below.
        Unlike set_log_level there is nothing to hot-apply in *this*
        process: the value only ever reaches a browser tab by being baked
        into a page's own web_shell.wrap() call at request time (routes_
        settings.py's settings_page reads it fresh off this same config on
        every request, so the very next render -- including this action's
        own response -- already reflects it)."""
        if level not in NOTIFICATIONS_DETAIL_LEVELS:
            return self.snapshot()
        cfg = self._load_config()
        cfg.setdefault("web", {}).setdefault("notifications", {})["detail"] = level
        self._save_config(cfg)
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Update checker (see update_checker.py)
    # ------------------------------------------------------------------ #

    def toggle_update_check(self) -> dict[str, Any]:
        cfg = self._load_config()
        update_check_cfg = cfg.setdefault("update_check", {})
        update_check_cfg["enabled"] = not update_check_cfg.get("enabled", True)
        self._save_config(cfg)
        return self.snapshot()

    def toggle_update_check_beta(self) -> dict[str, Any]:
        cfg = self._load_config()
        update_check_cfg = cfg.setdefault("update_check", {})
        update_check_cfg["include_beta"] = not update_check_cfg.get("include_beta", False)
        self._save_config(cfg)
        # The toggle itself is a channel switch -- check right away instead
        # of waiting for the next timer pulse.
        self.check_for_updates_now()
        return self.snapshot()

    def on_update_check_timer(self) -> None:
        """Periodic "is it time to check yet?" pulse -- called by
        daemon_main.py's own background timer thread (through P9, menu_bar.
        py's rumps.Timer; deleted at P10 along with the rest of the AppKit
        UI layer). check_for_update() re-derives whether 24h have actually
        passed from its own on-disk timestamp."""
        cfg = self._load_config()
        update_check_cfg = cfg.get("update_check", {}) or {}
        if not update_check_cfg.get("enabled", True):
            return
        self.check_for_updates_now()

    def check_for_updates_now(self) -> dict[str, Any]:
        """Manual "Check for Updates" (About page) or the beta-toggle/timer
        pulse above -- always actually checks, ignoring the 24h throttle
        that's only inside check_for_update() for the periodic pulse's
        implicit callers."""
        cfg = self._load_config()
        include_beta = (cfg.get("update_check", {}) or {}).get("include_beta", False)
        _run_async(lambda: check_for_update(include_beta=include_beta), self._on_update_check_done)
        return self.snapshot()

    def _on_update_check_done(self, ok: bool, result: Any) -> None:
        if not ok:
            logger.warning("Update check failed: %s", result)
            return
        self._latest_update = result
        self._push_snapshot()
        # Through P9 an update found here also popped a native rumps.alert()
        # (Download / Skip This Version / Remind Me Later), when pyobjc was
        # present -- P10 deleted that host along with the rest of the
        # AppKit UI layer. The web settings page's own in-page banner
        # (renderGeneral's g.update_available, driven by _general_state
        # below, with skip_update/remind_later_update as its two dismiss
        # actions) is this surface's only notification now.

    def skip_update(self) -> dict[str, Any]:
        """The web General page's banner "Skip" button."""
        if self._latest_update is not None:
            mark_skipped(self._latest_update.latest_version)
        return self.snapshot()

    def remind_later_update(self) -> dict[str, Any]:
        """The web General page's banner "Remind Me Later" button."""
        mark_remind_later()
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Organization config bundle
    # ------------------------------------------------------------------ #

    def install_org_config_bytes(self, raw: bytes) -> None:
        """The validate-then-write step behind web/routes_settings.py's
        multipart upload (§16.2.4). Through P9 there was also a native
        "choose file" picker (``osascript``'s ``choose file``) calling
        into this same method, run synchronously on the calling (main)
        thread -- P10 deleted that host along with the rest of the AppKit
        UI layer, leaving this the only way an organization config bundle
        gets installed. Sets self.error on failure, clears it on success --
        callers push a fresh snapshot() themselves, the same convention
        every other mutating method here follows.

        SEC-05 (full signing): also runs the bundle through
        org_bundle_signing.verify_and_maybe_pin() before writing anything
        to disk -- a bundle that doesn't verify against a previously
        pinned signing key (or, for a "mode": "org" bundle, isn't signed
        at all) is rejected here rather than being written and only
        caught the next time daemon_main.load_org_config() runs at
        startup. See that module's own docstring for the trust-on-first-
        use model this shares with daemon_main.py's own enforcement of
        it.
        """
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.error = f"Could not read that file as JSON: {exc}"
            return
        if not isinstance(data, dict) or "version" not in data:
            self.error = (
                "That file doesn't look like a PrivacyFence organization config bundle "
                '(expected a JSON object with a "version" field).'
            )
            return

        trust = org_bundle_signing.verify_and_maybe_pin(data, org_dir())
        if not trust.ok:
            self.error = (
                f"Refusing to install: organization config bundle failed signing-key "
                f"verification ({trust.detail}). If a legitimate signing-key rotation is "
                f"expected, an administrator must delete "
                f"{org_bundle_signing.pinned_public_key_path(org_dir())} first."
            )
            return
        if data.get("mode") == "org" and not trust.signed:
            self.error = (
                'That bundle has "mode": "org" but is not signed -- org mode requires a signed '
                "bundle (build one with scripts/build_org_bundle.py --sign-key ...)."
            )
            return
        if trust.newly_pinned:
            logger.info(
                "Organization config bundle signing key trusted for the first time (TOFU) and "
                "pinned to %s", org_bundle_signing.pinned_public_key_path(org_dir()),
            )

        dest = org_dir() / "org_config.json"
        try:
            atomic_write_json(dest, data, indent=2)
        except OSError as exc:
            self.error = f"Could not install organization config: {exc}"
            return

        self.error = ""
        # The live connectors were built from the previous bundle's client
        # credentials and download settings; rebuild them now, or they keep
        # serving the old values until the next restart.
        self.refresh_connectors()

    def would_pin_new_org_signing_key(self, raw: bytes) -> bool:
        """Read-only precheck for web/routes_settings.py's org_config_
        upload route (F5 of the self-approval review): whether installing
        ``raw`` would pin a new organization-config signing key as a side
        effect of install_org_config_bytes above, so that route can
        demand an explicit confirmation before calling it, rather than
        letting the TOFU pin happen silently as a side effect of an
        upload. Malformed input (not valid JSON, not a JSON object) never
        needs confirmation here -- install_org_config_bytes's own
        validation rejects it either way, with or without a confirmation.

        Uses this controller's own module-level org_dir() rather than
        importing paths.org_dir a second, independent way -- see org_
        bundle_signing.py's own module docstring for why that would
        silently diverge from what tests (and settings_controller's own
        callers) monkeypatch.
        """
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        return org_bundle_signing.would_pin_new_key(data, org_dir())

    # ------------------------------------------------------------------ #
    # Connector actions
    # ------------------------------------------------------------------ #

    def _org_config_or_empty(self) -> dict[str, Any]:
        """load_org_config() now raises ``org_mode.ConfigurationError`` for
        a present-but-broken org_config.json (SEC-04) instead of silently
        treating it as absent -- exactly the fail-closed behavior daemon_
        main.py's startup path needs, since that's where "mode" gets
        resolved and org-mode auth gets wired from it. This settings
        surface only ever runs in local mode (org mode mounts no local
        settings page at all -- see daemon_main.py's _maybe_start_web_
        server), so a broken org bundle here isn't a security downgrade,
        just missing Google/Slack/Salesforce/Atlassian app registrations --
        surface it as this controller's usual error banner instead of
        taking the whole settings page down.
        """
        from .daemon_main import load_org_config
        try:
            return load_org_config()
        except org_mode.ConfigurationError as exc:
            self.error = str(exc)
            return {}

    def enable_connector(self, connector: str) -> dict[str, Any]:
        """F6 of the self-approval review: split out of a single
        ``toggle_connector`` so the two directions can be gated
        differently by web/routes_settings.py's _SENSITIVE_ACTIONS. An
        agent that already has connector access gains nothing new by
        *disabling* one (see disable_connector below and that module's
        classification comment), but re-enabling one a human deliberately
        switched off is exactly the access the agent did not have before
        -- the same rationale toggle_grant_capability etc. are already
        gated on."""
        return self._set_connector_enabled(connector, True)

    def disable_connector(self, connector: str) -> dict[str, Any]:
        return self._set_connector_enabled(connector, False)

    def _set_connector_enabled(self, connector: str, enabled: bool) -> dict[str, Any]:
        cfg = self._load_config()
        conn = cfg.setdefault("connectors", {}).setdefault(connector, {})
        conn["enabled"] = enabled
        self._save_config(cfg)
        self.refresh_connectors()
        return self.snapshot()

    def refresh_connectors(self) -> dict[str, Any]:
        """Re-run connector construction (which re-checks auth/enabled state
        for every service) and push the result live into the shared
        ConnectorHost, so authenticating or toggling a connector takes
        effect immediately instead of requiring a restart."""

        def work() -> tuple[list, dict[str, str]]:
            from .daemon_main import build_connectors, load_org_config
            cfg = self._load_config()
            try:
                org_config = load_org_config()
            except org_mode.ConfigurationError:
                # Reported to the user by snapshot()'s own call to
                # _org_config_or_empty() right after -- work() itself must
                # not touch self (see _run_async's own docstring).
                org_config = {}
            return build_connectors(cfg, org_config)

        def done(ok: bool, result: Any) -> None:
            if ok:
                result, failures = result
                self._connectors = [c.name for c in result]
                self._connector_objs = {c.name: c for c in result}
                self._connector_failures = failures
                if self.connector_host is not None:
                    self.connector_host.set_connectors(result)
                # issue #396 Part C: after the live set is actually swapped,
                # not before -- a listener that asks for fresh connector
                # state (McpDispatcher.notify_tools_changed's own broadcast
                # is fire-and-forget, but the principle holds) must see the
                # new set, not the one refresh_connectors() is replacing.
                if self._connectors_changed_listener is not None:
                    self._connectors_changed_listener()
            self._push_snapshot()

        _run_async(work, done)
        return self.snapshot()

    def authenticate_connector(self, connector: str) -> dict[str, Any]:
        """OAuth-style single-click connectors only -- Telegram's
        multi-step phone/code/2FA flow is routed client-side into its own
        modal instead (see settings_window_html.py's connector row
        handling and telegram_start_auth/telegram_submit_code/
        telegram_submit_2fa/telegram_cancel_auth below), so this is never
        called with connector == "telegram" from production JS. A stray
        call is a harmless no-op rather than an error."""
        org_config = self._org_config_or_empty()
        if connector in GOOGLE_CONNECTORS:
            self._authenticate_google(connector, org_config)
        elif connector == "slack":
            self._authenticate_slack(org_config)
        elif connector == "salesforce":
            self._authenticate_salesforce(org_config)
        elif connector in ("jira", "confluence"):
            self._authenticate_atlassian(org_config)
        return self.snapshot()

    def _authenticate_google(self, cname: str, org_config: dict[str, Any]) -> None:
        client_config = _google_client_config(org_config)
        if not client_config:
            self.error = "Google organization config isn't installed yet."
            return
        from .daemon_main import TOKEN_FILES
        client_cls = _GOOGLE_CLIENTS[cname]
        token_file = str(data_dir() / TOKEN_FILES[cname])
        self._busy_connectors.add(cname)

        def work() -> str:
            client = client_cls(client_config=client_config, token_file=token_file)
            client.authorize_interactive()
            return client.check_connection()

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard(cname)
            if ok:
                self.error = ""
                self.refresh_connectors()
            else:
                self.error = f"{connector_label(cname)} authentication failed: {result}"
                self._push_snapshot()

        _run_async(work, done)

    def _authenticate_slack(self, org_config: dict[str, Any]) -> None:
        slack_org = org_config.get("slack") or {}
        if not slack_org.get("client_id"):
            self.error = "Slack organization config isn't installed yet."
            return
        from .daemon_main import TOKEN_FILES
        token_file = str(data_dir() / TOKEN_FILES["slack"])
        self._busy_connectors.add("slack")

        def work() -> dict[str, Any]:
            return slack_authorize_interactive(
                client_id=slack_org["client_id"],
                client_secret=slack_org.get("client_secret", ""),
                token_file=token_file,
                user_scopes=slack_org.get("user_scopes"),
            )

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("slack")
            if ok:
                self.error = ""
                self.refresh_connectors()
            else:
                self.error = f"Slack authentication failed: {result}"
                self._push_snapshot()

        _run_async(work, done)

    def _authenticate_salesforce(self, org_config: dict[str, Any]) -> None:
        sf_org = org_config.get("salesforce") or {}
        if not sf_org.get("consumer_key"):
            self.error = "Salesforce organization config isn't installed yet."
            return
        from .daemon_main import TOKEN_FILES
        token_file = str(data_dir() / TOKEN_FILES["salesforce"])
        self._busy_connectors.add("salesforce")

        def work() -> dict[str, Any]:
            return salesforce_authorize_interactive(
                consumer_key=sf_org["consumer_key"],
                consumer_secret=sf_org.get("consumer_secret", ""),
                token_file=token_file,
                login_url=sf_org.get("login_url", "https://login.salesforce.com"),
            )

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("salesforce")
            if ok:
                self.error = ""
                self.refresh_connectors()
            else:
                self.error = f"Salesforce authentication failed: {result}"
                self._push_snapshot()

        _run_async(work, done)

    def _authenticate_atlassian(self, org_config: dict[str, Any]) -> None:
        atlassian_org = org_config.get("atlassian") or {}
        if not atlassian_org.get("client_id"):
            self.error = "Atlassian organization config isn't installed yet."
            return
        from .daemon_main import TOKEN_FILES
        token_file = str(data_dir() / TOKEN_FILES["atlassian"])
        self._busy_connectors.add("jira")
        self._busy_connectors.add("confluence")

        def pick_resource(resources: list[dict[str, Any]]) -> dict[str, Any]:
            # Runs on work()'s own background thread (see _run_async) --
            # _pick_resource_index below handles marshaling the actual
            # picker onto whichever surface is live (native window or web)
            # and blocking this one until it resolves, the same convention
            # gate.py's asyncio.to_thread(show_*_popup, ...) call sites
            # already use. See that function's own docstring (§16.2.2: "the
            # same pattern P1 already solved" -- generalized here rather
            # than a second, web-only mechanism).
            #
            # Two behaviors carried over deliberately, not by accident
            # (§16.2.2): a cancelled picker (idx is None) falls back to the
            # first resource -- there was never an abort path of its own,
            # on either surface; and the options shown are site URLs, not
            # names.
            options = [r.get("url", r.get("id", "")) for r in resources]
            idx = _pick_resource_index(
                title="PrivacyFence", prompt="Choose the Atlassian site to connect:", options=options,
            )
            return resources[idx] if idx is not None else resources[0]

        def work() -> dict[str, Any]:
            return atlassian_authorize_interactive(
                client_id=atlassian_org["client_id"],
                client_secret=atlassian_org.get("client_secret", ""),
                token_file=token_file,
                pick_resource=pick_resource,
            )

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("jira")
            self._busy_connectors.discard("confluence")
            if ok:
                self.error = ""
                self.refresh_connectors()
            else:
                self.error = f"Atlassian authentication failed: {result}"
                self._push_snapshot()

        _run_async(work, done)

    # -- Telegram: bridge-driven multi-step sign-in (phone -> code -> optional
    # 2FA password), replacing the native rumps.Window-based flow the first
    # pass of issue #120 kept as a deliberate scope boundary. Each step opens
    # its own short-lived TelegramClient/connect/disconnect, exactly like the
    # pre-#120 flow did (see git history at 1f367ca, menu_bar.py's
    # _authenticate_telegram) -- no long-lived connection is held across
    # bridge calls, since a webview round trip can be arbitrarily far apart
    # from the next one. self._telegram_auth carries the phone number and
    # phone_code_hash send_code_request returned, needed by the code step;
    # it's None whenever no Telegram sign-in is in progress. The JS side
    # opens its modal locally (see settings_window_html.py) the moment the
    # Telegram connector row's "Authenticate…" is clicked, before any bridge
    # call -- telegram_start_auth() below is only reached once the user
    # actually submits a phone number.

    def _telegram_auth_state(self) -> dict[str, Any]:
        if self._telegram_auth is None:
            return {"step": None, "error": ""}
        return {"step": self._telegram_auth.get("step"), "error": self._telegram_auth.get("error", "")}

    def telegram_start_auth(self, phone: str) -> dict[str, Any]:
        creds = telegram_app_credentials()
        if not creds:
            self.error = "Telegram app credentials are missing from this build."
            return self.snapshot()
        api_id, api_hash = creds
        phone = (phone or "").strip()
        if not phone:
            self._telegram_auth = {"step": "phone", "error": "Enter a phone number."}
            return self.snapshot()
        from .daemon_main import TOKEN_FILES
        session_file = str(data_dir() / TOKEN_FILES["telegram"])
        self._busy_connectors.add("telegram")
        self._telegram_auth = {"step": "phone", "error": ""}

        def work() -> str:
            import asyncio

            return asyncio.run(telegram_auth.send_code(phone, session_file, api_id, api_hash))

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("telegram")
            if ok:
                self._telegram_auth = {"step": "code", "phone": phone, "phone_code_hash": result, "error": ""}
            else:
                self._telegram_auth = {"step": "phone", "error": str(result)}
            self._push_snapshot()

        _run_async(work, done)
        return self.snapshot()

    def telegram_submit_code(self, code: str) -> dict[str, Any]:
        if self._telegram_auth is None or self._telegram_auth.get("step") != "code":
            return self.snapshot()
        code = (code or "").strip()
        if not code:
            self._telegram_auth["error"] = "Enter the verification code."
            return self.snapshot()
        creds = telegram_app_credentials()
        if not creds:
            self.error = "Telegram app credentials are missing from this build."
            return self.snapshot()
        api_id, api_hash = creds
        from .daemon_main import TOKEN_FILES
        session_file = str(data_dir() / TOKEN_FILES["telegram"])
        phone = self._telegram_auth["phone"]
        phone_code_hash = self._telegram_auth["phone_code_hash"]
        self._busy_connectors.add("telegram")

        def work() -> str:
            import asyncio

            return asyncio.run(telegram_auth.sign_in(phone, code, phone_code_hash, session_file, api_id, api_hash))

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("telegram")
            if not ok:
                if self._telegram_auth is not None:
                    self._telegram_auth["error"] = str(result)
                self._push_snapshot()
                return
            if result == telegram_auth.NEEDS_2FA:
                if self._telegram_auth is not None:
                    self._telegram_auth["step"] = "password"
                    self._telegram_auth["error"] = ""
                self._push_snapshot()
                return
            self._telegram_auth = None
            self.error = ""
            self.refresh_connectors()

        _run_async(work, done)
        return self.snapshot()

    def telegram_submit_2fa(self, password: str) -> dict[str, Any]:
        if self._telegram_auth is None or self._telegram_auth.get("step") != "password":
            return self.snapshot()
        password = (password or "").strip()
        if not password:
            self._telegram_auth["error"] = "Enter your two-step verification password."
            return self.snapshot()
        creds = telegram_app_credentials()
        if not creds:
            self.error = "Telegram app credentials are missing from this build."
            return self.snapshot()
        api_id, api_hash = creds
        from .daemon_main import TOKEN_FILES
        session_file = str(data_dir() / TOKEN_FILES["telegram"])
        self._busy_connectors.add("telegram")

        def work() -> str:
            import asyncio

            return asyncio.run(telegram_auth.sign_in_2fa(password, session_file, api_id, api_hash))

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("telegram")
            if not ok:
                if self._telegram_auth is not None:
                    self._telegram_auth["error"] = str(result)
                self._push_snapshot()
                return
            self._telegram_auth = None
            self.error = ""
            self.refresh_connectors()

        _run_async(work, done)
        return self.snapshot()

    def telegram_cancel_auth(self) -> dict[str, Any]:
        self._telegram_auth = None
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Auto-accept (policy v2) -- P6 of the policy v2 redesign. See this
    # module's own "Auto-accept (policy v2)" section, above, for the
    # catalogue (_policy_scope_catalogue/_POLICY_EXTRA_SCOPES/
    # _rules_for_catalogue_entry) these two actions build on.
    # ------------------------------------------------------------------ #

    def add_policy_rule(self, group: str, value: str, verbs: list) -> dict[str, Any]:
        """The Auto-accept page's "Add rule" form. ``group`` is one of ``_policy_scope_catalogue()``'s
        own ids; ``verbs`` is whichever of that group's own verb checkboxes were checked, as their
        v2 verb strings. Additive only, like every other rule here: a submission that names a
        ``(predicate, value)`` an existing rule already carries widens that rule's own operations
        rather than creating a duplicate row (``policy.store.merge_rules``) -- which is also how
        "add more verbs to an existing rule" works from this same form, no separate edit action
        needed. Silently does nothing for an unrecognized group, a group none of the requested verbs
        govern, or (for the one valued P6 extra, ``apps_script.project``) a value-needing scope
        submitted with none -- see ``_rules_for_catalogue_entry``'s own docstring.
        """
        verb_enums = _parse_verbs(verbs)
        if not verb_enums:
            return self.snapshot()
        new_rules = _rules_for_catalogue_entry(group, _parse_value_list(value), verb_enums)
        if not new_rules:
            return self.snapshot()
        cfg = self._load_config()
        existing = policy_store.compile_rules_from_config(cfg)
        merged = policy_store.merge_rules(existing + new_rules)
        cfg[policy_store.AUTO_ACCEPT_CONFIG_KEY] = policy_store.rules_to_config(merged)
        self._save_and_reload_policy_v2(cfg)
        return self.snapshot()

    def remove_policy_rule(self, rule_id: str) -> dict[str, Any]:
        """Removes the rule with this stable id from the on-disk v2 ``auto_accept:`` section
        entirely -- narrowing (rather than adding a rule) is always a remove-and-re-add-narrower
        here, the same "additive only" posture the redesign proposal's §03 gives the whole model, not
        a smaller-scoped edit action of its own."""
        cfg = self._load_config()
        existing = policy_store.compile_rules_from_config(cfg)
        remaining = [rule for rule in existing if rule.id != rule_id]
        if len(remaining) == len(existing):
            return self.snapshot()
        cfg[policy_store.AUTO_ACCEPT_CONFIG_KEY] = policy_store.rules_to_config(remaining)
        self._save_and_reload_policy_v2(cfg)
        return self.snapshot()

    def _save_and_reload_policy_v2(self, cfg: dict[str, Any]) -> None:
        """Persist ``cfg`` and hot-reload the v2 rule cache (``set_policy_v2_store_rules``) plus
        fire the rules-changed listener broadcast (``notify_rules_changed``, via
        ``_save_and_reload``) that pushes a fresh snapshot to every open tab and re-checks any
        pending approval."""
        self._save_and_reload(cfg)
        try:
            set_policy_v2_store_rules(policy_store.compile_rules_from_config(cfg))
        except Exception as exc:
            logger.warning("Policy v2 store hot-reload failed: %s", exc)

    def _resolve_names_async(
        self, rt: GrantResourceType, resource_ids: list[str], client: Any | None
    ) -> None:
        """Kick off a background name lookup for any of these IDs with no
        cached name yet, then push a fresh snapshot once done. No-ops (and
        doesn't loop) once every ID has a cached name -- see
        resource_names.py's TTL."""
        if client is None:
            return
        missing = [rid for rid in resource_ids if rid and self._resolver.cached_name(rt, rid) is None]
        if not missing:
            return

        def work() -> bool:
            return any(self._resolver.resolve(rt, resource_id, client) for resource_id in missing)

        def done(ok: bool, resolved_something: Any) -> None:
            if ok and resolved_something:
                self._push_snapshot()

        _run_async(work, done)

    # ------------------------------------------------------------------ #
    # Privacy filter (privacy / drive_privacy / slack_privacy / ... groups,
    # plus Calendar's one standalone toggle)
    # ------------------------------------------------------------------ #

    def set_default_policy(self, group: str, policy: str) -> dict[str, Any]:
        if policy not in PRIVACY_POLICIES:
            return self.snapshot()
        cfg = self._load_config()
        cfg.setdefault(group, {})["default_policy"] = policy
        self._save_and_reload_privacy(cfg)
        return self.snapshot()

    def set_category_policy(self, group: str, category: str, policy: str) -> dict[str, Any]:
        if policy not in PRIVACY_POLICIES:
            return self.snapshot()
        cfg = self._load_config()
        categories = cfg.setdefault(group, {}).setdefault("categories", {})
        categories[category] = policy
        self._save_and_reload_privacy(cfg)
        return self.snapshot()

    def toggle_calendar_free_busy(self) -> dict[str, Any]:
        cfg = self._load_config()
        calendar_cfg = cfg.setdefault("calendar", {})
        calendar_cfg["free_busy_full_event_details"] = not calendar_cfg.get(
            "free_busy_full_event_details", True
        )
        self._save_config(cfg)
        self.refresh_connectors()
        return self.snapshot()

    def toggle_gmail_signature(self) -> dict[str, Any]:
        cfg = self._load_config()
        gmail_cfg = cfg.setdefault("gmail", {})
        gmail_cfg["append_signature_to_drafts"] = not gmail_cfg.get("append_signature_to_drafts", False)
        self._save_config(cfg)
        self.refresh_connectors()
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Audit log
    # ------------------------------------------------------------------ #

    def set_log_level(self, level: str) -> dict[str, Any]:
        level = (level or "").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return self.snapshot()
        cfg = self._load_config()
        cfg.setdefault("logging", {})["level"] = level
        self._save_config(cfg)
        from .daemon_main import setup_logging
        setup_logging(cfg)
        return self.snapshot()

    def export_audit_log_path(self) -> str | None:
        """Web/routes_settings.py's download endpoint uses this (§16.2.4):
        builds this week's .xlsx and returns its path for the route to
        serve directly, instead of running ``open`` on the daemon's own
        machine (which would show the file to whoever is sitting at *that*
        machine, not to the browser that made the request -- possibly a
        different device entirely; through P9, that ``open``-based version
        was the native settings window's own equivalent, deleted at P10).
        There is nothing sensible to "download" when there's no audit
        activity for the current week yet (opening the containing folder
        doesn't translate to an HTTP download), so that case is an error
        here rather than a silent open-the-folder fallback. Sets self.error
        (and returns None) on either miss; clears it on success.
        """
        log_dir = authority_root(Path(data_dir())) / "logs" / "audit"
        week = current_week()
        if not log_dir.exists() or not (log_dir / f"{week}.jsonl").exists():
            self.error = "No audit log for this week yet."
            return None

        xlsx_path = AuditLogger(str(log_dir)).export_week_to_excel(week)
        self.error = ""
        return xlsx_path

    # ------------------------------------------------------------------ #
    # About
    # ------------------------------------------------------------------ #

    def quit_app(self) -> None:
        """The web settings page's "Quit PrivacyFence" action
        (web/routes_settings.py's quit_action, behind ``allow_quit``/an
        in-page confirmation). Through P9 this called ``rumps.
        quit_application()`` -- the native menu bar's own equivalent,
        deleted at P10 along with the rest of the AppKit UI layer -- so
        this now signals daemon_main.py's own shutdown wait instead; see
        that module's ``request_shutdown``."""
        from . import daemon_main
        daemon_main.request_shutdown()

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    def any_connector_authenticated(self) -> bool:
        """Whether anything is governed at all yet.

        The same fact ``_connectors_state``'s per-row ``authed`` is built
        from, as one boolean, for callers that only need "is this install
        set up" and shouldn't have to build (or depend on the shape of) a
        full snapshot to find out -- web/routes_approvals.py's list route
        uses it to pick the approvals page's empty state."""
        return bool(self._connectors)

    def snapshot(self) -> dict[str, Any]:
        cfg = self._load_config()
        org_config = self._org_config_or_empty()
        return {
            "error": self.error,
            "general": self._general_state(cfg),
            "connectors": self._connectors_state(cfg, org_config),
            "telegram_auth": self._telegram_auth_state(),
            "auto_accept": self._auto_accept_state(cfg),
            "privacy": self._privacy_state(cfg),
            "audit": self._audit_state(cfg),
            "about": self._about_state(),
        }

    def _general_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        update_cfg = cfg.get("update_check", {}) or {}
        notifications_cfg = (cfg.get("web", {}) or {}).get("notifications", {}) or {}

        org_path = org_dir() / "org_config.json"
        org_installed = org_path.exists()
        org_installed_date = ""
        if org_installed:
            try:
                mtime = datetime.fromtimestamp(org_path.stat().st_mtime)
                # Not strftime("%b %-d, %Y") -- %-d (no leading zero) is a
                # glibc/macOS strftime extension; Windows' CRT raises
                # ValueError: Invalid format string on it. Building the
                # day ourselves keeps the same "Jan 5, 2026" rendering
                # (no leading zero) identically across all three platforms.
                org_installed_date = f"{mtime:%b} {mtime.day}, {mtime:%Y}"
            except OSError:
                org_installed_date = ""

        latest = self._latest_update
        return {
            **_pii_general_fields(cfg),
            "update_check_enabled": update_cfg.get("enabled", True),
            "update_check_beta": update_cfg.get("include_beta", False),
            # The Approval Notifications card's segmented control -- see
            # set_notifications_detail above. notifications_enabled mirrors
            # the same config's enabled flag (already reaching the card a
            # different way, via window.__pfNotificationsEnabled -- baked
            # into web_shell.wrap() at request time -- but carried here too
            # so the card's one render() call has everything it needs off
            # `state` alone).
            "notifications_enabled": notifications_cfg.get("enabled", True),
            "notifications_detail": notifications_cfg.get("detail", "minimal"),
            # §16.2.4: the web General page's own update-available banner --
            # see skip_update/remind_later_update and _on_update_check_done
            # above for the actions it drives.
            "update_available": bool(latest is not None and latest.is_update_available),
            "update_latest_version": latest.latest_version if latest is not None else "",
            "update_release_url": latest.release_url if latest is not None else "",
            "update_is_beta": bool(latest is not None and latest.is_beta),
            "org_installed": org_installed,
            "org_installed_date": org_installed_date,
            "org_button_label": (
                "Install/Update Organization Config…" if org_installed else "Install Organization Config…"
            ),
            "version": __version__,
            # B9: the General page's Security card reads these three to
            # decide whether to show "Turn on step-up", a disabled hint
            # ("add a passkey first"), or nothing (already on) -- see
            # enable_step_up's own docstring and settings_window_html.py's
            # renderGeneral. step_up_on is deliberately "both flags true",
            # not just "enabled": that's the only state this action ever
            # produces, and the only one that enforces anything in local
            # mode (StepUpConfig.from_local_config's own docstring).
            "step_up_available": self._step_up is not None,
            "step_up_on": bool(
                self._step_up is not None and self._step_up.enabled and self._step_up.require_passkey
            ),
            "step_up_has_passkey": _has_webauthn_credentials(LOCAL_PRINCIPAL),
        }

    def _connectors_state(self, cfg: dict[str, Any], org_config: dict[str, Any]) -> list[dict[str, Any]]:
        connectors_cfg: dict[str, dict] = cfg.get("connectors", {}) or {}
        rows = []
        for cname in ALL_CONNECTORS:
            connected = cname in self._connectors
            conn_cfg = connectors_cfg.get(cname, {})
            enabled = conn_cfg.get("enabled", True)
            busy = cname in self._busy_connectors
            if cname == "telegram":
                has_org = telegram_app_credentials() is not None
            else:
                has_org = bool(org_config.get(ORG_CONFIG_SERVICE[cname]))

            rows.append({
                "key": cname,
                "label": connector_label(cname),
                "icon": cname,
                "authed": connected,
                "enabled": enabled,
                "busy": busy,
                "has_org": has_org,
                # None for a connected connector, a deliberately disabled
                # one (never attempted, so build_connectors() never raised
                # for it -- "enabled" above already says that), or one this
                # row simply hasn't been through a build for yet; otherwise
                # "no_org_config" | "not_authenticated" | a redacted
                # message, from build_connectors()'s own per-connector
                # failure map (issue #396 Phase 1).
                "blocked_by": (
                    None if connected or not enabled else self._connector_failures.get(cname)
                ),
                "auth_label": "Reconnect…" if connected else "Authenticate…",
            })
        return rows

    def status_connectors(self) -> list[dict[str, Any]]:
        """privacyfence_status's own connector view (issue #396 Phase 2,
        web/mcp_dispatch.py's ``McpDispatcher.set_connectors_state_provider``
        seam) -- the same underlying state ``_connectors_state`` above
        derives for the settings page, reshaped into the
        ``{name, enabled, authenticated, blocked_by}`` rows the MCP status
        payload documents rather than the page's own
        ``{key, label, icon, authed, busy, has_org, auth_label}`` shape."""
        cfg = self._load_config()
        org_config = self._org_config_or_empty()
        return [
            {
                "name": row["key"], "enabled": row["enabled"],
                "authenticated": row["authed"], "blocked_by": row["blocked_by"],
            }
            for row in self._connectors_state(cfg, org_config)
        ]

    def _auto_accept_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """State for the Auto-accept page (P6): every rule in the on-disk v2 ``auto_accept:``
        section, sentence-rendered, plus the "add a rule" scope catalogue -- one filterable list,
        replacing the per-connector Trusted-*/parallel-rule-row/Sheets-Docs-pointer-page surface
        this method used to build (``_rules_state``/``_drive_grant_summary``/``_grant_entry_label``,
        through P5).

        P8 adds each row's own usage: ``match_count``/``last_matched`` (a relative-time string,
        via ``_relative_time``, empty when the rule has never matched) and ``never_matched``,
        from ``AuditLogger.rule_usage()`` grouped by the same ``rule.id`` this row is keyed on --
        gate.py's ``_evaluate_auto_accept`` stamps every "auto_accepted" audit entry's ``rule_id``
        with exactly this id when (and only when) it can attribute the decision to one row (see
        that field's own docstring), so a count here is never a guess. Reads straight off this
        principal's own ``logs/audit/`` directory, the same way ``_audit_state`` builds "Recent
        decisions" -- not the process-wide ``get_audit_logger()`` singleton, which may be a
        different principal's logger by the time this renders (P6, org mode)."""
        rules = policy_store.compile_rules_from_config(cfg)
        return _auto_accept_state_from_rules(rules, _rule_usage_map(), resolve_value=self._resolved_rule_value)

    def _resolved_rule_value(self, rule: PolicyRule) -> str:
        """A rule's own value, as a comma-separated display string, resolving each id through the
        same cached-name machinery the old grant rows used (``RULE_NAME_TO_RESOURCE_TYPE``,
        ``resource_names.py``) wherever the predicate names an opaque resource id -- a Drive folder,
        a Jira project key, and so on -- rather than showing the raw id, kicking off a background
        resolve for anything not cached yet (``_resolve_names_async``)."""
        rt, str_values = rule_resource_ids(rule)
        if rt is not None:
            self._resolve_names_async(rt, str_values, self._client_for(rt.connector))
        return cached_rule_value(rule, self._resolver)

    def _privacy_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        return _privacy_state_from_config(cfg)

    def _audit_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        log_cfg = cfg.get("logging", {}) or {}
        level = str(log_cfg.get("level", "INFO")).upper()
        log_file = log_cfg.get("file", "logs/privacyfence.log")
        week = current_week()
        log_dir = authority_root(Path(data_dir())) / "logs" / "audit"

        recent: list[dict[str, Any]] = []
        if log_dir.exists():
            recent = audit_rows(AuditLogger(str(log_dir)).recent_entries(20))

        return {
            "log_level": level,
            "log_file": log_file,
            "export_hint": f"logs/audit/{week}.jsonl → {week}.xlsx",
            "recent": recent,
        }

    def _about_state(self) -> dict[str, Any]:
        return _about_state_dict()
