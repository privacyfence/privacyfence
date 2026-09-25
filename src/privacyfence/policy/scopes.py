"""Scope selectors.

A scope answers "which resources": an **identity** scope names a specific resource (a folder, a
sender address, a Jira project); an **attribute** scope names a property that selects a set (every
file you own, a domain, a channel kind). This module holds a `ScopeSelector` for each scope-bound
predicate -- `policy/conditions.py` holds the conditions, which narrow a scope already matched
rather than selecting resources on their own.

Several predicates share one scope type because they were always the same selector wearing a
different name for each verb that happened to need it (`approved_folder` and
`approved_sandbox_folder` are the *same function* -- see `_approved_folder_matches` below;
`move_within_approved_folders` reuses it for the source and also checks the move's destination)
-- but `SCOPE_SELECTORS` is keyed by **predicate name**, not by scope type. That is what makes each
entry directly, individually testable against the predicate it replaced: `test_scopes.py` asserts
every selector here agrees with its frozen counterpart in `tests/unit/policy/_v1_reference.py` on
every fixture. A selector's `.scope_type` records which scope type it belongs to;
`scope_type_to_predicates()` inverts that for anything that wants to walk a scope type's full
predicate set instead.

`label_name_allowlist` genuinely serves two scope types (`gmail.label` for
`gmail.add_label`/`remove_label`/`create_label`, `contacts.label` for
`contacts.add_label`/`remove_label`) depending on which connector the operation belongs to --
its `.scope_type` is a tuple of both rather than picking one arbitrarily.

`always_allow` covers three operation keys spanning two connectors (`gmail.create_draft`,
`calendar.out_of_office`, `calendar.working_location`) with no resource identity to check at all,
so it is modelled as an explicit, honestly-unconditional "anything in this connector" scope rather
than hidden behind a rule name. Its `.scope_type` is the literal `"<connector>.anything"`
placeholder -- resolving that to a concrete `gmail.anything`/`calendar.anything` per rule is
config-authoring work, not something this module's `matches()` needs to know.

Scope types with no old predicate to be checked against live in `NEW_SCOPE_SELECTORS` rather than
`SCOPE_SELECTORS`, and their own tests exercise `matches()` directly: `drive.file` (no rule ever
named one specific file by id rather than a folder), `apps_script.project` (Apps Script's tools had
no scope at all), and `gmail.anything`/`slack.anything` -- the honestly-unconditional scopes that
make `gmail.create_filter`/`update_filter`/`slack.create_group_chat` configurable from the
Auto-accept Settings page; see their own comment below for why they are not simply more
`always_allow` rules.

``policy.engine.evaluate``/``preflight`` are what consume this module in production.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from ..auto_accept import ReviewContext, _address_of, _domain_of, _file_from


class ScopeKind(str, Enum):
    """Identity scopes name a specific resource; attribute scopes name a property that selects a
    set of resources (see this module's docstring)."""

    IDENTITY = "identity"
    ATTRIBUTE = "attribute"


class ResolvesFrom(str, Enum):
    """Whether a scope can be decided from call arguments alone, or needs the fetched item.

    The *same* scope type can carry selectors with different `resolves_from`: `drive.folder`'s
    item-scoped predicates (`approved_folder`) need the fetched file's own parent, but its
    container-scoped predicate (`parent_folder_allowlist`, which governs an upload whose file
    doesn't exist yet) can only ever read the destination out of `ctx.args`. `resolves_from` is a
    property of the *predicate*, not of the scope type name alone.
    """

    ARGS = "args"
    FETCHED = "fetched"


@dataclass(frozen=True)
class ScopeSelector:
    """One scope-bound predicate: a name, its scope type, its kind, where it resolves from, and
    how it's checked (`matches(value, ctx) -> bool`).
    """

    predicate: str
    scope_type: str | tuple[str, ...]
    kind: ScopeKind
    resolves_from: ResolvesFrom
    matches: Callable[[Any, ReviewContext], bool]


def _values_of(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


# ── Slack channel identity / channel-kind attribute ─────────────────────────────────────────────


def _dm_with_myself_matches(_value: Any, ctx: ReviewContext) -> bool:
    # Every IM id starts with "D", so the channel id cannot tell the self-DM from a DM with anyone
    # else. The Slack connector resolves the IM's counterpart against the signed-in user and passes
    # the verdict; a call that carries no verdict must not match.
    return ctx.args.get("is_self_dm") is True


def _group_dm_matches(_value: Any, ctx: ReviewContext) -> bool:
    return bool(ctx.args.get("is_group_dm", False))


def _approved_channel_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    cid = ctx.args.get("channel_id", "") or ctx.args.get("channel", "") or ""
    return cid in allowed


def _approved_channel_all_results_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
    return bool(items) and all(getattr(m, "channel_id", None) in allowed for m in items)


def _public_channels_only_matches(_value: Any, ctx: ReviewContext) -> bool:
    raw = ctx.raw_data
    items = raw if isinstance(raw, list) else [raw]
    return all(not getattr(m, "is_private", True) for m in items)


# ── Calendar ──────────────────────────────────────────────────────────────────────────────────


def _personal_calendar_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    return ctx.args.get("calendar_id", "") in allowed


def _i_am_organizer_matches(_value: Any, ctx: ReviewContext) -> bool:
    raw = ctx.raw_data
    organizer = (raw.get("organizer_email") if isinstance(raw, dict) else getattr(raw, "organizer_email", "")) or ""
    return bool(ctx.my_email) and ctx.my_email.lower() == organizer.lower()


# ── Confluence ────────────────────────────────────────────────────────────────────────────────


def _i_am_author_matches(_value: Any, ctx: ReviewContext) -> bool:
    raw = ctx.raw_data
    author = (raw.get("author") if isinstance(raw, dict) else getattr(raw, "author", "")) or ""
    return bool(ctx.my_email) and ctx.my_email.lower() in author.lower()


def _approved_space_keys_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = {v.upper() for v in _values_of(value)}
    raw = ctx.raw_data
    space_key = ctx.args.get("space_key") or (raw.get("space_key") if isinstance(raw, dict) else "") or ""
    return bool(space_key) and space_key.upper() in allowed


# ── Drive (incl. Sheets/Docs, which ride Drive's own grant -- see resource_grants.py) ───────────


def _created_this_session_matches(_value: Any, ctx: ReviewContext) -> bool:
    f = _file_from(ctx.raw_data)
    return getattr(f, "id", "") in ctx.session_created_ids


def _file_type_allowlist_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = {v.lower() for v in _values_of(value)}
    f = _file_from(ctx.raw_data)
    return (getattr(f, "mime_type", "") or "").lower() in allowed


def _approved_folder_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    f = _file_from(ctx.raw_data)
    parents = getattr(f, "parent_ids", []) or []
    return bool(set(parents) & allowed)


def _move_within_approved_folders_matches(value: Any, ctx: ReviewContext) -> bool:
    """A move stays inside the approved set only if it both starts and ends there: checking the
    source alone would let a folder grant auto-approve moving a file out of that folder into any
    folder the account can write to."""
    if not _approved_folder_matches(value, ctx):
        return False
    raw = ctx.raw_data
    destination = ctx.args.get("destination_folder_id") or (
        raw.get("destination_folder_id") if isinstance(raw, dict) else ""
    )
    return bool(destination) and destination in set(_values_of(value))


def _parent_folder_allowlist_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    return (ctx.args.get("parent_folder_id") or "") in allowed


def _i_am_owner_matches(_value: Any, ctx: ReviewContext) -> bool:
    if not ctx.my_email:
        return False
    f = _file_from(ctx.raw_data)
    owners = getattr(f, "owners", []) or []
    return any(_address_of(o) == ctx.my_email.lower() for o in owners)


# ── Gmail ─────────────────────────────────────────────────────────────────────────────────────


def _i_am_sender_matches(_value: Any, ctx: ReviewContext) -> bool:
    if not ctx.my_email:
        return False
    sender = getattr(ctx.raw_data, "sender", "") or ""
    return _address_of(sender) == ctx.my_email.lower()


def _trusted_sender_domain_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    raw_sender = getattr(ctx.raw_data, "sender", "") or ""
    email_part = raw_sender
    if "<" in raw_sender and ">" in raw_sender:
        email_part = raw_sender[raw_sender.index("<") + 1 : raw_sender.index(">")]
    domain = email_part.split("@", 1)[-1].lower().strip()
    allowed = {d.lower().strip() for d in _values_of(value)}
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def _label_match_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    labels = {label.lower() for label in (getattr(ctx.raw_data, "labels", []) or [])}
    allowed = {v.lower() for v in _values_of(value)}
    return bool(labels & allowed)


def _label_name_allowlist_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = {v.lower() for v in _values_of(value)}
    label = (ctx.args.get("label_name") or "").lower()
    return label in allowed


def _i_am_sole_recipient_matches(_value: Any, ctx: ReviewContext) -> bool:
    if not ctx.my_email:
        return False
    recips = getattr(ctx.raw_data, "recipients", []) or []
    return len(recips) == 1 and _address_of(recips[0]) == ctx.my_email.lower()


def _to_is_myself_matches(_value: Any, ctx: ReviewContext) -> bool:
    to = ctx.args.get("to", "") or ""
    recipients = [r for r in _values_of(to) if r]
    return bool(ctx.my_email) and bool(recipients) and all(ctx.my_email.lower() in r.lower() for r in recipients)


def _approved_recipient_domain_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    to = ctx.args.get("to", "") or ""
    recipients = [r for r in _values_of(to) if r]
    allowed = {d.lower().strip() for d in _values_of(value)}
    return bool(recipients) and all(_domain_of(r) in allowed for r in recipients)


# ── Jira ──────────────────────────────────────────────────────────────────────────────────────


def _approved_project_keys_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = {v.upper() for v in _values_of(value)}
    project_key = ctx.args.get("project_key", "") or ""
    if not project_key:
        issue_key = ctx.args.get("issue_key", "") or ""
        project_key = issue_key.split("-")[0] if "-" in issue_key else ""
    return bool(project_key) and project_key.upper() in allowed


def _i_am_reporter_matches(_value: Any, ctx: ReviewContext) -> bool:
    raw = ctx.raw_data
    reporter = (raw.get("reporter") if isinstance(raw, dict) else getattr(raw, "reporter", "")) or ""
    return bool(ctx.my_email) and ctx.my_email.lower() in reporter.lower()


def _i_am_assignee_matches(_value: Any, ctx: ReviewContext) -> bool:
    raw = ctx.raw_data
    assignee = (raw.get("assignee") if isinstance(raw, dict) else getattr(raw, "assignee", "")) or ""
    return bool(ctx.my_email) and ctx.my_email.lower() in assignee.lower()


# ── Salesforce ────────────────────────────────────────────────────────────────────────────────


def _approved_object_types_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = {v.lower() for v in _values_of(value)}
    if "object_types" in ctx.args:
        requested = [t.strip().lower() for t in (ctx.args.get("object_types") or "").split(",") if t.strip()]
        return bool(requested) and all(t in allowed for t in requested)
    return ctx.args.get("object_type", "").lower() in allowed


def _approved_report_ids_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    return ctx.args.get("report_id", "") in allowed


# ── Tasks ─────────────────────────────────────────────────────────────────────────────────────


def _approved_task_list_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    if "task_list_id" in ctx.args:
        return ctx.args.get("task_list_id", "") in allowed
    source = ctx.args.get("source_list_id", "")
    destination = ctx.args.get("destination_list_id", "")
    return bool(source) and bool(destination) and source in allowed and destination in allowed


# ── Telegram ──────────────────────────────────────────────────────────────────────────────────


def _approved_chats_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = {str(v) for v in _values_of(value)}
    return str(ctx.args.get("chat_id", "")) in allowed


def _approved_chats_all_results_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = {str(v) for v in _values_of(value)}
    items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
    return bool(items) and all(str(getattr(m, "chat_id", None)) in allowed for m in items)


# ── Generic (no resource identity to scope to -- see the module docstring) ─────────────────────


def _always_allow_matches(_value: Any, _ctx: ReviewContext) -> bool:
    return True


SCOPE_SELECTORS: dict[str, ScopeSelector] = {
    "dm_with_myself": ScopeSelector(
        predicate="dm_with_myself", scope_type="slack.channel_kind", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.ARGS, matches=_dm_with_myself_matches,
    ),
    "send_to_myself": ScopeSelector(
        predicate="send_to_myself", scope_type="slack.channel", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_dm_with_myself_matches,
    ),
    "group_dm": ScopeSelector(
        predicate="group_dm", scope_type="slack.channel_kind", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.ARGS, matches=_group_dm_matches,
    ),
    "approved_channel": ScopeSelector(
        predicate="approved_channel", scope_type="slack.channel", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_channel_matches,
    ),
    "approved_recipient": ScopeSelector(
        predicate="approved_recipient", scope_type="slack.channel", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_channel_matches,
    ),
    "approved_channel_all_results": ScopeSelector(
        predicate="approved_channel_all_results", scope_type="slack.channel", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_approved_channel_all_results_matches,
    ),
    "public_channels_only": ScopeSelector(
        predicate="public_channels_only", scope_type="slack.channel_kind", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_public_channels_only_matches,
    ),
    "personal_calendar": ScopeSelector(
        predicate="personal_calendar", scope_type="calendar.calendar", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_personal_calendar_matches,
    ),
    "i_am_organizer": ScopeSelector(
        predicate="i_am_organizer", scope_type="calendar.organized_by_me", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_i_am_organizer_matches,
    ),
    "i_am_author": ScopeSelector(
        predicate="i_am_author", scope_type="confluence.authored_by_me", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_i_am_author_matches,
    ),
    "approved_space_keys": ScopeSelector(
        predicate="approved_space_keys", scope_type="confluence.space", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_space_keys_matches,
    ),
    "created_this_session": ScopeSelector(
        predicate="created_this_session", scope_type="drive.created_this_session", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_created_this_session_matches,
    ),
    "file_type_allowlist": ScopeSelector(
        predicate="file_type_allowlist", scope_type="drive.file_type", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_file_type_allowlist_matches,
    ),
    "approved_folder": ScopeSelector(
        predicate="approved_folder", scope_type="drive.folder", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_approved_folder_matches,
    ),
    "approved_sandbox_folder": ScopeSelector(
        predicate="approved_sandbox_folder", scope_type="drive.folder", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_approved_folder_matches,
    ),
    "move_within_approved_folders": ScopeSelector(
        predicate="move_within_approved_folders", scope_type="drive.folder", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_move_within_approved_folders_matches,
    ),
    "parent_folder_allowlist": ScopeSelector(
        predicate="parent_folder_allowlist", scope_type="drive.folder", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_parent_folder_allowlist_matches,
    ),
    "i_am_owner": ScopeSelector(
        predicate="i_am_owner", scope_type="drive.owned_by_me", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_i_am_owner_matches,
    ),
    "created_by_me": ScopeSelector(
        predicate="created_by_me", scope_type="drive.owned_by_me", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_i_am_owner_matches,
    ),
    "i_am_sender": ScopeSelector(
        predicate="i_am_sender", scope_type="gmail.sender", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_i_am_sender_matches,
    ),
    "trusted_sender_domain": ScopeSelector(
        predicate="trusted_sender_domain", scope_type="gmail.sender_domain", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_trusted_sender_domain_matches,
    ),
    "label_match": ScopeSelector(
        predicate="label_match", scope_type="gmail.label", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_label_match_matches,
    ),
    "label_name_allowlist": ScopeSelector(
        predicate="label_name_allowlist", scope_type=("gmail.label", "contacts.label"), kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_label_name_allowlist_matches,
    ),
    "i_am_sole_recipient": ScopeSelector(
        predicate="i_am_sole_recipient", scope_type="gmail.recipient", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_i_am_sole_recipient_matches,
    ),
    "to_is_myself": ScopeSelector(
        predicate="to_is_myself", scope_type="gmail.recipient", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_to_is_myself_matches,
    ),
    "approved_recipient_domain": ScopeSelector(
        predicate="approved_recipient_domain", scope_type="gmail.recipient_domain", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_recipient_domain_matches,
    ),
    "approved_project_keys": ScopeSelector(
        predicate="approved_project_keys", scope_type="jira.project", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_project_keys_matches,
    ),
    "i_am_reporter": ScopeSelector(
        predicate="i_am_reporter", scope_type="jira.my_issues", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_i_am_reporter_matches,
    ),
    "i_am_assignee": ScopeSelector(
        predicate="i_am_assignee", scope_type="jira.my_issues", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.FETCHED, matches=_i_am_assignee_matches,
    ),
    "approved_object_types": ScopeSelector(
        predicate="approved_object_types", scope_type="salesforce.object_type", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_object_types_matches,
    ),
    "approved_report_ids": ScopeSelector(
        predicate="approved_report_ids", scope_type="salesforce.report", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_report_ids_matches,
    ),
    "approved_task_list": ScopeSelector(
        predicate="approved_task_list", scope_type="tasks.list", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_task_list_matches,
    ),
    "approved_chats": ScopeSelector(
        predicate="approved_chats", scope_type="telegram.chat", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_approved_chats_matches,
    ),
    "approved_chats_all_results": ScopeSelector(
        predicate="approved_chats_all_results", scope_type="telegram.chat", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_approved_chats_all_results_matches,
    ),
    "always_allow": ScopeSelector(
        predicate="always_allow", scope_type="<connector>.anything", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.ARGS, matches=_always_allow_matches,
    ),
}


def scope_type_to_predicates() -> dict[str, tuple[str, ...]]:
    """Invert `SCOPE_SELECTORS`: every scope type -> the predicate names that land under it,
    in `SCOPE_SELECTORS`' own iteration order. A selector whose `.scope_type` is a tuple (today,
    only `label_name_allowlist`) is filed under each of its scope types.
    """
    by_type: dict[str, list[str]] = {}
    for predicate, selector in SCOPE_SELECTORS.items():
        types = selector.scope_type if isinstance(selector.scope_type, tuple) else (selector.scope_type,)
        for scope_type in types:
            by_type.setdefault(scope_type, []).append(predicate)
    return {scope_type: tuple(predicates) for scope_type, predicates in by_type.items()}


# ── Scope types with no old predicate ───────────────────────────────────────────────────────────
#
# drive.file names one specific file by id, which no other rule does (every Drive rule scopes by
# folder or by attribute); apps_script.project is what makes the three Apps Script operation keys
# configurable at all. There is no old predicate to check either against, so they live here rather
# than in `SCOPE_SELECTORS`, and their own tests exercise `matches()` directly instead of an
# equivalence check.


def _drive_file_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    f = _file_from(ctx.raw_data)
    return getattr(f, "id", "") in allowed


def _apps_script_project_matches(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    allowed = set(_values_of(value))
    return ctx.args.get("script_id", "") in allowed


# `gmail.create_filter`/`update_filter` and `slack.create_group_chat` have no resource identity to
# scope to at all (propose.py's own module docstring explains why: a filter's/group chat's
# *subject* is "the account"/"the audience", not an item any `ScopeSelector` here can name). They
# are honestly-unconditional scopes in the same sense `always_allow` is -- but deliberately their
# own predicates, distinct from
# `always_allow`, rather than reusing it: `always_allow` already carries three `PROPOSABLE_SCOPES`
# entries (Gmail drafting, two calendar conditions) whose declared verbs (`draft`, `read`) would
# make `describe.rule_verbs` wrongly filter a `configure`/`share` rule down to nothing (it credits a
# rule only with the verbs its predicate's own catalogue entries declare, once that predicate has
# any). A predicate with no catalogue entry at all is credited with every verb its operations
# actually carry instead, which is what these two need. `policy_engine.evaluate`'s own matching
# behaviour is identical to `always_allow`'s either way (unconditional `True`).
def _anything_matches(_value: Any, _ctx: ReviewContext) -> bool:
    return True


NEW_SCOPE_SELECTORS: dict[str, ScopeSelector] = {
    "drive.file": ScopeSelector(
        predicate="drive.file", scope_type="drive.file", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.FETCHED, matches=_drive_file_matches,
    ),
    "apps_script.project": ScopeSelector(
        predicate="apps_script.project", scope_type="apps_script.project", kind=ScopeKind.IDENTITY,
        resolves_from=ResolvesFrom.ARGS, matches=_apps_script_project_matches,
    ),
    "gmail.anything": ScopeSelector(
        predicate="gmail.anything", scope_type="gmail.anything", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.ARGS, matches=_anything_matches,
    ),
    "slack.anything": ScopeSelector(
        predicate="slack.anything", scope_type="slack.anything", kind=ScopeKind.ATTRIBUTE,
        resolves_from=ResolvesFrom.ARGS, matches=_anything_matches,
    ),
}
