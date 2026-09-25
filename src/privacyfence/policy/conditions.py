"""The ten `when:` conditions a rule can carry.

A condition narrows a scope that already matched; it never widens one and never decides which
resources a rule reaches on its own. The predicates here describe a property of the item already in
scope (its age, whether it carries an attachment, whether an event is private) rather than which
resource is being addressed -- that is `policy/scopes.py`'s job.

Three connector-specific attachment checks collapse onto one condition: Gmail's `attachments`,
Slack's `files` and Telegram's `media_type` are the same idea -- "this item carries nothing
attached" -- spelled differently by each connector's fetched object, so `no_attachments` dispatches
on `ctx.connector` to pick the right field instead of staying three predicates that happen to agree.

`test_conditions.py` checks every selector here against a frozen copy of the predicate it replaced
(`tests/unit/policy/_v1_reference.py`), including each absence check with `raw_data=None`.
`policy.engine` resolves a rule's `conditions` through `CONDITION_SELECTORS` by name; the old
predicate names are not accepted.
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

    An absence check (`no_attachments`, `no_external_attendees`, `no_conferencing_link`,
    `not_shared_drive`) is `FETCHED` even though it degenerately *could* be evaluated with
    `ctx.raw_data is None`: doing so would silently read "nothing fetched yet" as "confirmed
    absent", a false match a real preflight must never produce.
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

