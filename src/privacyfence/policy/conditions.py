"""The ten v2 `when:` conditions -- P2 of the policy v2 redesign.

A condition narrows a scope that already matched; it never widens one and never decides
which resources a rule reaches on its own (see the redesign proposal's "Model" section,
"Conditions only ever narrow a scope further"). This module holds the twelve predicates the
redesign proposal's Scope catalogue (`policy/scopes.py`'s counterpart, §04) files as *conditions*
rather than *scopes* -- they describe a property of the item already in scope (its age, whether it
carries an attachment, whether an event is private) rather than which resource is being addressed.

Twelve predicates collapse onto ten conditions: `no_attachments` (Gmail), `no_file_attachments`
(Slack) and `no_media_attachments` (Telegram) are the same idea -- "this item carries nothing
attached" -- wearing three connector-specific names purely because each connector's fetched object
spells the field differently (`attachments`, `files`, `media_type`). One `no_attachments` condition
here dispatches on `ctx.connector` to pick the right field instead of staying three predicates that
happen to agree.

Each `ConditionSelector.holds` is a **behavior-preserving copy** of its
`auto_accept.AutoAcceptEvaluator._rule_*` counterpart -- same signature, same logic, including the
quirks that make some of these `DATA_DEPENDENT_RULES` members rather than `ARGS_ONLY_RULES` (an
absence check silently reads "not present" as `True` when `ctx.raw_data` is `None`, which is unsafe
to preflight but is exactly what a rule that already matched during a real, fully-fetched call needs
to check -- see `auto_accept.DATA_DEPENDENT_RULES`'s own docstring). `resolves_from` is read off
those two existing sets, not duplicated by hand, so this module can't drift from them the way F6
describes. `test_conditions.py` asserts every selector here agrees with its old counterpart on every
fixture, including each absence check with `raw_data=None`. `policy.engine` resolves a rule's
`conditions` through `CONDITION_SELECTORS` by v2 name; the old predicate names are not accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Callable

from ..auto_accept import ReviewContext, _attendee_email, _file_from


class ResolvesFrom(str, Enum):
    """Whether a condition can be decided from call arguments alone, or needs the fetched item.

    Mirrors `auto_accept.ARGS_ONLY_RULES`/`DATA_DEPENDENT_RULES` -- see that module for why an
    absence check (this condition's `no_attachments`, `no_external_attendees`,
    `no_conferencing_link`, `not_shared_drive`) is `FETCHED` even though it degenerately *could* be
    evaluated with `ctx.raw_data is None`: doing so would silently read "nothing fetched yet" as
    "confirmed absent", a false match a real preflight must never produce.
    """

    ARGS = "args"
    FETCHED = "fetched"


@dataclass(frozen=True)
class ConditionSelector:
    """One `when:` predicate: a name, where it resolves from, and how it's checked."""

    name: str
    resolves_from: ResolvesFrom
    holds: Callable[[Any, ReviewContext], bool]


# Field a connector's fetched item carries its attachments/files/media under -- see
# `_no_attachments_holds` below. Every operation `no_attachments` governs today belongs to exactly
# one of these three connectors (gmail.read_message; slack.read_messages; telegram.read_chat_messages).
_ATTACHMENT_FIELD_BY_CONNECTOR: dict[str, str] = {
    "gmail": "attachments",
    "slack": "files",
    "telegram": "media_type",
}


def _older_than_days_holds(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    date_str = getattr(ctx.raw_data, "date", "") or ""
    if not date_str:
        return False
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days >= int(value)
    except Exception:
        return False


def _within_days_holds(value: Any, ctx: ReviewContext) -> bool:
    if not value:
        return False
    start_str = getattr(ctx.raw_data, "start_time", "") or ""
    if not start_str:
        return False
    try:
        dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days_ahead = (dt - datetime.now(timezone.utc)).days
        return 0 <= days_ahead <= int(value)
    except Exception:
        return False


def _past_only_holds(_value: Any, ctx: ReviewContext) -> bool:
    end_str = getattr(ctx.raw_data, "end_time", "") or ""
    if not end_str:
        return False
    try:
        dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except Exception:
        return False


def _no_attachments_holds(_value: Any, ctx: ReviewContext) -> bool:
    field = _ATTACHMENT_FIELD_BY_CONNECTOR.get(ctx.connector)
    if field is None:
        return False
    items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
    return all(not getattr(m, field, None) for m in items)


def _no_external_attendees_holds(_value: Any, ctx: ReviewContext) -> bool:
    if not ctx.my_domain:
        return False
    raw = ctx.raw_data
    attendees = (raw.get("attendees") if isinstance(raw, dict) else getattr(raw, "attendees", None)) or []
    return all(ctx.my_domain in _attendee_email(a) for a in attendees)


def _no_conferencing_link_holds(_value: Any, ctx: ReviewContext) -> bool:
    raw = ctx.raw_data
    return not bool(getattr(raw, "conference_link", "") or getattr(raw, "hangout_link", ""))


def _not_private_holds(_value: Any, ctx: ReviewContext) -> bool:
    visibility = getattr(ctx.raw_data, "visibility", None)
    return (visibility or "default") != "private"


def _not_shared_drive_holds(_value: Any, ctx: ReviewContext) -> bool:
    # A shared-drive file is identified by a non-empty driveId; Drive's "shared" flag means
    # "shared with someone" and is not set for shared-drive files.
    f = _file_from(ctx.raw_data)
    return not getattr(f, "drive_id", "")


def _no_contact_info_change_holds(_value: Any, ctx: ReviewContext) -> bool:
    return not (ctx.args.get("emails") or ctx.args.get("phones"))


def _in_existing_thread_holds(_value: Any, ctx: ReviewContext) -> bool:
    return bool(ctx.args.get("thread_ts"))


CONDITION_SELECTORS: dict[str, ConditionSelector] = {
    "older_than_days": ConditionSelector(
        name="older_than_days",
        resolves_from=ResolvesFrom.FETCHED,
        holds=_older_than_days_holds,
    ),
    "within_days": ConditionSelector(
        name="within_days",
        resolves_from=ResolvesFrom.FETCHED,
        holds=_within_days_holds,
    ),
    "past_only": ConditionSelector(
        name="past_only",
        resolves_from=ResolvesFrom.FETCHED,
        holds=_past_only_holds,
    ),
    "no_attachments": ConditionSelector(
        name="no_attachments",
        resolves_from=ResolvesFrom.FETCHED,
        holds=_no_attachments_holds,
    ),
    "no_external_attendees": ConditionSelector(
        name="no_external_attendees",
        resolves_from=ResolvesFrom.FETCHED,
        holds=_no_external_attendees_holds,
    ),
    "no_conferencing_link": ConditionSelector(
        name="no_conferencing_link",
        resolves_from=ResolvesFrom.FETCHED,
        holds=_no_conferencing_link_holds,
    ),
    "not_private": ConditionSelector(
        name="not_private",
        resolves_from=ResolvesFrom.FETCHED,
        holds=_not_private_holds,
    ),
    "not_shared_drive": ConditionSelector(
        name="not_shared_drive",
        resolves_from=ResolvesFrom.FETCHED,
        holds=_not_shared_drive_holds,
    ),
    "no_contact_info_change": ConditionSelector(
        name="no_contact_info_change",
        resolves_from=ResolvesFrom.ARGS,
        holds=_no_contact_info_change_holds,
    ),
    "in_existing_thread": ConditionSelector(
        name="in_existing_thread",
        resolves_from=ResolvesFrom.ARGS,
        holds=_in_existing_thread_holds,
    ),
}

