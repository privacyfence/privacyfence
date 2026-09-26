"""One date format for the approval card's message tables.

Connectors hand the card whatever their API calls a timestamp: Slack's
message ``ts`` (``"1757926320.000100"``, epoch seconds that double as the
message ID), Telegram's ISO string with seconds and a UTC offset, Jira's
``"2026-09-16T18:02:00.000+0000"``. Shown as-is, the "Date" column of one
connector's table reads nothing like the next one's, and Slack's is not a
date a person can read at all.

This is for the *preview row only*. The data returned to the AI keeps the
connector's raw value -- Slack in particular needs the exact ``ts`` to
address a thread or a message -- so callers format the table cell, never the
result dict.

UTC rather than the viewer's local zone: the card is rendered by the daemon,
which in an organization deployment is not on the approver's machine, so its
local zone is not reliably theirs. Labelled "UTC" so it is never mistaken
for local time.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

_FORMAT = "%Y-%m-%d %H:%M UTC"


def format_preview_datetime(value: object) -> str:
    """``value`` as ``"YYYY-MM-DD HH:MM UTC"``.

    Accepts a ``datetime`` (naive is taken as UTC), epoch seconds as a
    number or numeric string (Slack's ``ts``), or an ISO 8601 string (with
    or without an offset; naive is taken as UTC). A date with no time of day
    (``"2026-07-01"``) stays ``"2026-07-01"`` -- no midnight is invented for
    it. Anything else -- including
    a value that fails to parse -- comes back as ``str(value)`` unchanged: a
    raw date on the card is better than a blank one. ``None``/empty is ``""``.
    """
    if value is None or value == "":
        return ""
    dt: datetime | None = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value.isoformat()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = _from_epoch(float(value))
    elif isinstance(value, str):
        text = value.strip()
        try:
            dt = _from_epoch(float(text))
        except ValueError:
            if len(text) == 10:
                try:
                    return date.fromisoformat(text).isoformat()
                except ValueError:
                    return str(value)
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                dt = None
    if dt is None:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_FORMAT)


def _from_epoch(seconds: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
