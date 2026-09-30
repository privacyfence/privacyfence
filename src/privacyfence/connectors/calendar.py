"""Google Calendar connector."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date as _date
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..calendar_client import (
    EVENT_COLOR_NAMES,
    VALID_EVENT_SCOPES,
    VALID_SEND_UPDATES,
    VALID_VISIBILITIES,
    WORKING_LOCATION_TYPES,
    CalendarClient,
    CalendarClientError,
    normalize_event_color,
)
from ..connector import Connector, ToolParam, ToolSpec
from ..google_errors import unavailable_error
from ..gate import current_reason, gated_call

logger = logging.getLogger(__name__)


def _day_of_week(iso_str: str) -> str:
    """Return the full weekday name for an ISO 8601 datetime string."""
    try:
        return datetime.fromisoformat(iso_str).strftime("%A")
    except (ValueError, TypeError):
        return ""


def _merged_attendees_display(event) -> str:
    """One UI-facing "Attendees" value combining the organizer and the
    attendee list, marking the organizer inline (e.g. "Alice <a@x.com>
    (organizer), Bob <b@x.com>") -- there is no separate "Organizer" row on
    the approval window; the two are merged instead of shown separately.

    Google's Calendar API normally includes the organizer as one of the
    `attendees` entries (flagged `organizer: true`), but omits the whole
    `attendees` array entirely for events with no other guests -- checked
    directly in calendar_client.py's _parse_event. Relying on `attendees`
    alone would then silently drop the organizer's identity for that common
    (solo-event) case, so this synthesizes an organizer-only entry when
    `attendees` doesn't already include one flagged organizer=True.
    """
    entries = list(event.attendees or [])
    has_organizer_entry = any(a.organizer for a in entries)
    parts = []
    if not has_organizer_entry and event.organizer_email:
        parts.append(f"{event.organizer_email} (organizer)")
    for a in entries:
        label = f"{a.display_name} <{a.email}>" if a.display_name else a.email
        parts.append(f"{label} (organizer)" if a.organizer else label)
    return ", ".join(parts)


def _normalize_color_arg(color: str) -> str:
    """Validate a tool's ``color`` argument before gating, not after -- same
    reasoning as calendar_set_event_visibility's own early visibility check:
    a doomed call shouldn't cost the user an unnecessary approval decision.
    Reuses calendar_client.normalize_event_color's palette validation but
    re-raises as ValueError, matching every other connector-level "reject
    this bad argument before gating" check (CalendarClientError is reserved
    for failures from an actual API call). Returns "" unchanged -- no color
    given is valid, meaning "don't change it".
    """
    if not color:
        return ""
    try:
        return normalize_event_color(color)
    except CalendarClientError as exc:
        raise ValueError(str(exc)) from exc


def _parse_rfc3339(name: str, value: str, *, need_offset: bool) -> datetime:
    example = "2026-10-15T09:00:00+02:00"
    error = ValueError(f"{name} must be an RFC 3339 date-time such as {example}")
    if len(value) == 10:
        raise error
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise error from None
    if need_offset and parsed.tzinfo is None:
        raise error
    return parsed


def _require_rfc3339(name: str, value: str, *, need_offset: bool) -> None:
    """Reject a time argument that is not an RFC 3339 date-time (date-only
    values and, when ``need_offset``, values without an offset included)
    before gating, so a doomed call never costs an approval decision."""
    _parse_rfc3339(name, value, need_offset=need_offset)


def _require_end_after_start(start_time: str, end_time: str) -> None:
    """A naive value is compared as UTC."""
    start = _parse_rfc3339("start_time", start_time, need_offset=False)
    end = _parse_rfc3339("end_time", end_time, need_offset=False)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end <= start:
        raise ValueError("end_time must be after start_time")


_SCOPE_LABELS = {
    "this": "This event",
    "following": "This and following events",
    "all": "All events in the series",
}


def _normalize_scope_arg(scope: str) -> str:
    """Validate a tool's ``scope`` argument before gating -- same reasoning
    as _normalize_color_arg. Re-raises CalendarClientError as ValueError,
    matching every other connector-level "reject this bad argument before
    gating" check."""
    scope = (scope or "this").strip().lower()
    if scope not in VALID_EVENT_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(VALID_EVENT_SCOPES)}, got {scope!r}"
        )
    return scope


def _normalize_send_updates_arg(send_updates: str) -> str:
    """Validate a tool's ``send_updates`` argument before gating. "" (not
    given) is valid and means "let the Calendar API apply its own
    default"."""
    send_updates = (send_updates or "").strip()
    if send_updates and send_updates not in VALID_SEND_UPDATES:
        raise ValueError(
            f"send_updates must be one of {sorted(VALID_SEND_UPDATES)}, got {send_updates!r}"
        )
    return send_updates


def _downgrade_to_busy_only(entry: dict) -> dict:
    """Collapse a colleague's 'events'-sourced free/busy entry (full event
    title/status) down to the same busy-slot-only shape a 'free_busy'-
    sourced entry already has, for calendar.free_busy_full_event_details:
    false. Titles/status never reach Claude this way, regardless of whether
    the authenticated account happens to have full calendar access to that
    colleague. Entries already source="free_busy" (or "error") pass
    through unchanged -- there's nothing to downgrade."""
    if entry.get("source") != "events":
        return entry
    return {
        "email": entry.get("email", ""),
        "source": "free_busy",
        "busy": [
            {"start": e.get("start_time", ""), "end": e.get("end_time", "")}
            for e in (entry.get("events") or [])
        ],
    }


class CalendarConnector(Connector):
    def __init__(self, client: CalendarClient, rooms: list[dict] | None = None) -> None:
        self._calendar = client
        self.my_email: str = ""
        self._calendar_name_cache: dict[str, str] = {}
        # settings.yaml's calendar.free_busy_full_event_details (default
        # true, preserving prior behavior). When false,
        # calendar_get_free_busy always returns busy/free blocks only, even
        # for colleagues the authenticated account has full calendar access
        # to -- see _get_free_busy.
        self.free_busy_full_details: bool = True
        # Static room directory synced into org_config.json by IT (see
        # scripts/sync_room_directory.py) — never a live Admin SDK call, so this
        # connector's own client never needs Workspace-admin directory scope.
        self._rooms: list[dict] = rooms or []

    @property
    def client(self) -> CalendarClient:
        return self._calendar

    @property
    def name(self) -> str:
        return "calendar"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="calendar_list_calendars",
                description=(
                    "List all Google Calendars for the authenticated user. Returns a list of "
                    "{id, summary, primary, access_role}. Pass an id as calendar_id to "
                    "calendar_list_events and the other calendar tools. Auto-approved."
                ),
                params=[ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_list_events",
                description=(
                    "List events from a calendar (id, title, start_time, end_time, all_day, status). "
                    "No attendees, description, or links returned. Returns a list of {id, title, "
                    "start_time, end_time, day_of_week, all_day, status}, recurring events expanded "
                    "into single occurrences and sorted by start time. Reads up to max_results "
                    "events across pages. Use calendar_get_event_details "
                    "to read one event's attendees and description, or calendar_list_calendars to "
                    "find calendar_id. Auto-approved."
                ),
                params=[
                    ToolParam("calendar_id", "str",
                              description="Id of the calendar, from calendar_list_calendars (its id field), "
                                          "or 'primary' for the user's main calendar."),
                    ToolParam("max_results", "int", required=False, default=20,
                              description="Maximum number of events to return. Default 20, "
                                          "clamped to between 1 and 250."),
                    ToolParam("time_min", "str", required=False, default="",
                              description="Only events ending after this time, as an RFC 3339 timestamp "
                                          "with an offset, e.g. '2026-10-15T00:00:00Z' or "
                                          "'2026-10-15T09:00:00+02:00'; a bare date is not accepted. "
                                          "Empty means now."),
                    ToolParam("time_max", "str", required=False, default="",
                              description="Only events starting before this time, same RFC 3339 format "
                                          "as time_min. Empty means no upper bound."),
                    ToolParam("query", "str", required=False, default="",
                              description="Free-text search matched against event fields such as title, "
                                          "description, location and attendees. Empty returns every event "
                                          "in the window."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_get_free_busy",
                description=(
                    "Query colleagues' schedules for a time range. "
                    "For each email, tries to fetch full event details (title, time, status) "
                    "when the authenticated user has calendar access; "
                    "falls back to free/busy slots only when access is unavailable. "
                    "Use this for meeting scheduling. Returns one entry per email: {email, source: "
                    "'events', events: [{id, title, start_time, end_time, status, all_day}]} (up to 50 "
                    "events), or {email, source: 'free_busy', busy: [{start, end}]}, or {email, "
                    "source: 'error', error}; the user's privacy settings may reduce 'events' to busy "
                    "slots. Use calendar_list_events instead for the user's own calendar. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("emails", "str", description="Comma-separated email addresses of the people (or room resource emails) to check, e.g. 'a@example.com,b@example.com'."),
                    ToolParam("time_min", "str", description="Start of the range as an RFC 3339 timestamp with an offset, e.g. '2026-10-15T09:00:00+02:00'. Required; a bare date is not accepted."),
                    ToolParam("time_max", "str", description="End of the range, same RFC 3339 format as time_min. Required."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_get_event_details",
                description=(
                    "Fetch full details of a calendar event including attendees, description "
                    "and location. Returns {id, calendar_id, title, description, start_time, end_time, day_of_week, "
                    "all_day, organizer_email, attendees: [{email, display_name, response_status, "
                    "organizer}], location, status, html_link}; conferencing links and attachments "
                    "are not included. Use calendar_list_events instead to find events. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str",
                              description="Id of the calendar, from calendar_list_calendars (its id field), "
                                          "or 'primary' for the user's main calendar."),
                    ToolParam("event_id", "str",
                              description="Id of the event, from calendar_list_events (its id field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_get_event_visibility",
                description=(
                    "Get a calendar event's visibility setting (default, public, private, "
                    "or confidential) without fetching its full details (attendees, "
                    "description, etc.) the way calendar_get_event_details does. Returns {visibility}. "
                    "Use calendar_set_event_visibility to change it. Auto-approved."
                ),
                params=[
                    ToolParam("calendar_id", "str",
                              description="Id of the calendar, from calendar_list_calendars (its id field), "
                                          "or 'primary' for the user's main calendar."),
                    ToolParam("event_id", "str",
                              description="Id of the event, from calendar_list_events (its id field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_set_event_visibility",
                description=(
                    "Set a calendar event's visibility. 'default' follows the calendar's own "
                    "sharing settings; 'public' makes it visible to anyone who can see the "
                    "calendar; 'private' hides its details from viewers who aren't invited; "
                    "'confidential' is a legacy synonym the Calendar API still accepts for "
                    "'private'. Only visibility changes — no other fields are affected. Returns {id, "
                    "title, visibility}. Use calendar_get_event_visibility to read the current "
                    "value. Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str",
                              description="Id of the calendar, from calendar_list_calendars (its id field), "
                                          "or 'primary' for the user's main calendar."),
                    ToolParam("event_id", "str",
                              description="Id of the event, from calendar_list_events (its id field)."),
                    ToolParam("visibility", "str",
                              description="New visibility: 'default', 'public', 'private' or "
                                          "'confidential' (case-insensitive). Any other value is "
                                          "rejected before approval is requested."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_list_rooms",
                description=(
                    "List meeting rooms and resource calendars from the organization's room "
                    "directory. This is a locally-cached list IT refreshes with "
                    "scripts/sync_room_directory.py, not a live Workspace lookup — it may come back "
                    "empty if IT hasn't synced one yet. Returns a list of {resource_id, resource_name, "
                    "resource_email, building_id, floor_name, capacity, description}, unpaged. "
                    "To check whether a room is actually free before booking, call "
                    "calendar_get_free_busy with its resource_email. Use the room email with "
                    "calendar_create_event or calendar_update_event to book. Auto-approved."
                ),
                params=[
                    ToolParam("query", "str", required=False, default="",
                              description="Optional substring filter matched against room name, "
                                          "building, floor, or description (case-insensitive)"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_list_colors",
                description=(
                    "List Calendar's fixed event color palette: each color's id, name (e.g. "
                    "\"Tomato\", \"Sage\"), and hex background/foreground. Use a color's id or "
                    "name as the color argument to calendar_create_event, calendar_update_event, "
                    "or calendar_set_event_color instead of guessing a numeric id. Returns a list of "
                    "{id, name, background, foreground}, sorted by id (1-11). Auto-approved."
                ),
                params=[ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_create_event",
                description=(
                    "Create a new calendar event. Returns {id, title, start_time, end_time, "
                    "html_link}, plus conference_link when a Google Meet was added. Timed events "
                    "only: all-day events cannot be created. Use calendar_create_out_of_office "
                    "for time off, calendar_list_rooms to find rooms to book and "
                    "calendar_list_colors for color ids. Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str",
                              description="Id of the calendar, from calendar_list_calendars (its id field), "
                                          "or 'primary' for the user's main calendar."),
                    ToolParam("title", "str", description="Title of the event, as shown on the calendar."),
                    ToolParam("start_time", "str",
                              description="Start as an RFC 3339 timestamp, e.g. "
                                          "'2026-10-15T09:00:00+02:00'. A time without an offset is "
                                          "read as UTC. Date-only values are not accepted."),
                    ToolParam("end_time", "str",
                              description="End as an RFC 3339 timestamp, same format as start_time; "
                                          "must be after it."),
                    ToolParam("description", "str", required=False, default="",
                              description="Free-text description of the event. Empty means none."),
                    ToolParam("attendees", "str", required=False, default="",
                              description="Comma-separated email addresses of the guests to invite, e.g. "
                                          "'a@example.com,b@example.com'. Empty invites nobody."),
                    ToolParam("location", "str", required=False, default="",
                              description="Free-text location of the event. Empty means none."),
                    ToolParam("add_google_meet", "bool", required=False, default=False,
                              description="Set to true to add a Google Meet video conference link"),
                    ToolParam("rooms", "str", required=False, default="",
                              description="Comma-separated room resource email addresses to book, from "
                                          "calendar_list_rooms (its resource_email field). Empty books "
                                          "no room."),
                    ToolParam("color", "str", required=False, default="",
                              description="Event color id (1-11) or name, e.g. \"Tomato\" -- see "
                                          "calendar_list_colors. Empty uses the calendar's default "
                                          "color."),
                    ToolParam("recurrence", "str", required=False, default="",
                              description="RRULE line(s) to make this a recurring event, e.g. "
                                          "\"RRULE:FREQ=WEEKLY;COUNT=10\". One rule per line for "
                                          "more than one (RRULE plus EXDATE/RDATE/EXRULE). Omit "
                                          "for a non-recurring event."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_update_event",
                description=(
                    "Update an existing calendar event. For a recurring event, 'scope' controls "
                    "which occurrences this touches: 'this' (default) affects only the given "
                    "event_id; 'following' splits the series so this instance and every later one "
                    "get the changes, leaving earlier ones untouched; 'all' updates the entire "
                    "series. Only the fields you pass non-empty change; guests cannot be edited here. "
                    "Returns {id, title, start_time, end_time, html_link}, plus conference_link "
                    "when the event has a Google Meet. Use calendar_set_event_color or "
                    "calendar_set_event_visibility to change only the color or visibility. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str",
                              description="Id of the calendar, from calendar_list_calendars (its id field), "
                                          "or 'primary' for the user's main calendar."),
                    ToolParam("event_id", "str",
                              description="Id of the event, from calendar_list_events (its id field)."),
                    ToolParam("title", "str", required=False, default="",
                              description="New title. Empty leaves the title unchanged."),
                    ToolParam("start_time", "str", required=False, default="",
                              description="New start as an RFC 3339 timestamp, e.g. "
                                          "'2026-10-15T09:00:00+02:00'; a time without an offset uses "
                                          "the event's own time zone (UTC if it has none). Empty leaves "
                                          "the start unchanged."),
                    ToolParam("end_time", "str", required=False, default="",
                              description="New end, same format as start_time. Empty leaves the "
                                          "end unchanged."),
                    ToolParam("description", "str", required=False, default="",
                              description="New description, replacing the current one. Empty leaves "
                                          "it unchanged."),
                    ToolParam("location", "str", required=False, default="",
                              description="New location, replacing the current one. Empty leaves "
                                          "it unchanged."),
                    ToolParam("add_google_meet", "bool", required=False, default=False,
                              description="Set to true to add a Google Meet link (skipped if one already exists)"),
                    ToolParam("rooms", "str", required=False, default="",
                              description="Comma-separated room resource email addresses to book, from "
                                          "calendar_list_rooms (its resource_email field); they replace "
                                          "the rooms already booked. Empty leaves the rooms unchanged."),
                    ToolParam("color", "str", required=False, default="",
                              description="New event color id (1-11) or name, e.g. \"Tomato\" -- see "
                                          "calendar_list_colors. Empty leaves the color unchanged."),
                    ToolParam("scope", "str", required=False, default="this",
                              description="For a recurring event: 'this', 'following', or 'all'. "
                                          "Ignored (has no other meaning) for a non-recurring event."),
                    ToolParam("send_updates", "str", required=False, default="",
                              description="Who gets a notification email about this change: "
                                          "'none', 'all', or 'externalOnly'. Omit to use Calendar's "
                                          "own default."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_delete_event",
                description=(
                    "Delete a calendar event. For a recurring event, 'scope' controls what's "
                    "deleted: 'this' (default) deletes only the given event_id; 'following' ends "
                    "the series just before this instance, deleting it and every later occurrence "
                    "but keeping earlier ones; 'all' deletes the entire series. Returns {id, deleted: "
                    "true, scope}. Use calendar_update_event instead to change an event rather "
                    "than remove it. Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str",
                              description="Id of the calendar, from calendar_list_calendars (its id field), "
                                          "or 'primary' for the user's main calendar."),
                    ToolParam("event_id", "str",
                              description="Id of the event, from calendar_list_events (its id field)."),
                    ToolParam("scope", "str", required=False, default="this",
                              description="For a recurring event: 'this', 'following', or 'all'. "
                                          "Ignored (has no other meaning) for a non-recurring event."),
                    ToolParam("send_updates", "str", required=False, default="",
                              description="Who gets a notification email about this cancellation: "
                                          "'none', 'all', or 'externalOnly'. Omit to use Calendar's "
                                          "own default."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                destructive=True,
            ),
            ToolSpec(
                name="calendar_set_event_color",
                description=(
                    "Set a calendar event's color. Only the color changes — no other fields are "
                    "affected. Accepts a color id (1-11) or name, e.g. \"Tomato\" -- see "
                    "calendar_list_colors. Returns {id, title, color_id}. Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str",
                              description="Id of the calendar, from calendar_list_calendars (its id field), "
                                          "or 'primary' for the user's main calendar."),
                    ToolParam("event_id", "str",
                              description="Id of the event, from calendar_list_events (its id field)."),
                    ToolParam("color", "str",
                              description="Event color id (1-11) or name, e.g. \"Tomato\" "
                                          "(case-insensitive), from calendar_list_colors. Required."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_create_out_of_office",
                description=(
                    "Create an out-of-office event on the primary calendar. Always auto-declines "
                    "new conflicting meeting invitations that arrive while it's in effect — existing "
                    "invitations already on the calendar are left alone. Returns {id, title, start_time, "
                    "end_time, html_link}. Timed events only (not all-day). Use calendar_create_event "
                    "for an ordinary event. Requires user approval."
                ),
                params=[
                    ToolParam("start_time", "str",
                              description="Start as an RFC 3339 timestamp, e.g. "
                                          "'2026-10-15T09:00:00+02:00'. A time without an offset is "
                                          "read as UTC. Date-only values are not accepted."),
                    ToolParam("end_time", "str",
                              description="End as an RFC 3339 timestamp, same format as start_time; "
                                          "must be after it."),
                    ToolParam("title", "str", required=False, default="Out of Office",
                              description="Title shown on the calendar. Default 'Out of Office'."),
                    ToolParam("decline_message", "str", required=False, default="",
                              description="Message sent to organizers of auto-declined invitations. Empty sends no custom message."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_set_working_location",
                description=(
                    "Set your working-location presence (office or home) for a single day on the "
                    "primary calendar — the same picker Google Calendar's web UI exposes. "
                    "Replaces the day's existing working-location entry if there is one. Returns {id, "
                    "start_time, end_time, html_link}. Use calendar_create_out_of_office for "
                    "time off. Requires user approval."
                ),
                params=[
                    ToolParam("date", "str", description="The day as YYYY-MM-DD, e.g. 2026-07-10."),
                    ToolParam("location", "str",
                              description="Where you work that day: exactly \"office\" or \"home\". "
                                          "Anything else is rejected."),
                    ToolParam("building_id", "str", required=False, default="",
                              description="Workspace building id (office only), from calendar_list_rooms (its building_id field). Empty leaves the building unset."),
                    ToolParam("label", "str", required=False, default="",
                              description="Office label shown on Calendar (office only). Empty shows no label."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "calendar_list_calendars":
            return await self._list_calendars()
        if tool == "calendar_list_events":
            return await self._list_events(**args)
        if tool == "calendar_get_free_busy":
            return await self._get_free_busy(**args)
        if tool == "calendar_get_event_details":
            return await self._get_event_details(**args)
        if tool == "calendar_get_event_visibility":
            return await self._get_event_visibility(**args)
        if tool == "calendar_set_event_visibility":
            return await self._set_event_visibility(**args)
        if tool == "calendar_set_event_color":
            return await self._set_event_color(**args)
        if tool == "calendar_list_rooms":
            return await self._list_rooms(**args)
        if tool == "calendar_list_colors":
            return await self._list_colors(**args)
        if tool == "calendar_create_event":
            return await self._create_event(**args)
        if tool == "calendar_update_event":
            return await self._update_event(**args)
        if tool == "calendar_delete_event":
            return await self._delete_event(**args)
        if tool == "calendar_create_out_of_office":
            return await self._create_out_of_office(**args)
        if tool == "calendar_set_working_location":
            return await self._set_working_location(**args)
        raise ValueError(f"Unknown Calendar tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_calendars(self) -> Any:
        t0 = time.time()
        entries = await self._fetch(self._calendar.list_calendars)
        result = [
            {"id": e.id, "summary": e.summary, "primary": e.primary, "access_role": e.access_role}
            for e in entries
        ]
        self._auto_audit("calendar_list_calendars", "List Calendars",
                         "List all calendars", f"{len(entries)} calendar(s)", t0)
        return result

    async def _list_events(
        self,
        calendar_id: str,
        max_results: int = 20,
        time_min: str = "",
        time_max: str = "",
        query: str = "",
    ) -> Any:
        if time_min:
            _require_rfc3339("time_min", time_min, need_offset=True)
        if time_max:
            _require_rfc3339("time_max", time_max, need_offset=True)
        if not time_min:
            time_min = datetime.now(timezone.utc).isoformat()
        t0 = time.time()
        events = await self._fetch(
            self._calendar.list_events, calendar_id, max_results, time_min, time_max, query
        )
        result = [
            {
                "id": e.id,
                "title": e.title,
                "start_time": e.start_time,
                "end_time": e.end_time,
                "day_of_week": _day_of_week(e.start_time),
                "all_day": e.all_day,
                "status": e.status,
            }
            for e in events
        ]
        self._auto_audit("calendar_list_events", "List Calendar Events",
                         f"List events: {calendar_id}", f"{len(events)} event(s)", t0)
        return result

    async def _get_free_busy(self, emails: str, time_min: str, time_max: str) -> Any:
        _require_rfc3339("time_min", time_min, need_offset=True)
        _require_rfc3339("time_max", time_max, need_offset=True)
        t0 = time.time()
        email_list = [e.strip() for e in emails.split(",") if e.strip()]
        data = await self._fetch(
            self._calendar.get_colleagues_schedule, email_list, time_min, time_max
        )
        if not self.free_busy_full_details:
            data = [_downgrade_to_busy_only(r) for r in data]
        events_count = sum(1 for r in data if r.get("source") == "events")
        fb_count = sum(1 for r in data if r.get("source") == "free_busy")
        summary_note = (
            f"{events_count} with full events, {fb_count} free/busy only"
            if (events_count or fb_count)
            else f"{len(data)} result(s)"
        )
        self._auto_audit("calendar_get_free_busy", "Get Schedule",
                         f"Schedule: {emails}", summary_note, t0)
        return data

    async def _get_event_visibility(self, calendar_id: str, event_id: str) -> Any:
        t0 = time.time()
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        self._auto_audit("calendar_get_event_visibility", "Get Event Visibility",
                         f"Get visibility: {event.title or event_id}", event.visibility, t0)
        return {"visibility": event.visibility}

    async def _list_rooms(self, query: str = "") -> Any:
        """Filters the static room directory synced into org_config.json — no
        network call, so no Workspace-admin directory scope is ever needed here."""
        t0 = time.time()
        if query:
            needle = query.lower()
            result = [
                r for r in self._rooms
                if needle in r.get("resource_name", "").lower()
                or needle in r.get("building_id", "").lower()
                or needle in r.get("floor_name", "").lower()
                or needle in r.get("description", "").lower()
            ]
        else:
            result = list(self._rooms)
        self._auto_audit("calendar_list_rooms", "List Meeting Rooms",
                         f"List rooms{': ' + query if query else ''}", f"{len(result)} room(s)", t0)
        return result

    async def _list_colors(self) -> Any:
        t0 = time.time()
        colors = await self._fetch(self._calendar.list_event_colors)
        result = [
            {"id": c.id, "name": c.name, "background": c.background, "foreground": c.foreground}
            for c in colors
        ]
        self._auto_audit("calendar_list_colors", "List Event Colors",
                         "List event colors", f"{len(result)} color(s)", t0)
        return result

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_event_details(self, calendar_id: str, event_id: str) -> Any:
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        # The preview ("What Claude already knows"): exactly calendar_list_events'
        # own fields (id/title/start/end/day_of_week/all_day/status) -- see
        # 5deef1d8:docs/claude-knowledge-boundary.md's Calendar section. Everything else
        # below is new only once this call is approved.
        preview = {
            "Title": event.title or "(untitled)",
            "Time": f"{event.start_time} – {event.end_time}",
        }
        # new_info ("What will be provided to Claude"): the real values that will
        # actually reach Claude, not a policy summary -- Calendar has no
        # privacy-category schema (see 5deef1d8:docs/claude-knowledge-boundary.md's
        # redaction-scope section), so there's no redact/block placeholder
        # to show here, just the data itself. Every row always present, even
        # when empty (e.g. no location) -- the row structure is fixed per
        # tool, only values vary. No separate "Organizer" row: merged into
        # Attendees (see _merged_attendees_display's docstring for why a
        # naive "just use the attendees list" merge isn't enough on its
        # own). conference_link/hangout_link/attachments are deliberately
        # not surfaced here (nor in filtered_data below) -- Claude gets no
        # path from this tool to a meeting's attached notes/transcript.
        new_info = {
            "Attendees": _merged_attendees_display(event),
            "Location": event.location or "",
            "Description": event.description or "",
        }
        details_lines = [
            f"Location: {event.location or '(none)'}",
            "",
            f"Description:\n{event.description or '(none)'}",
            "",
            "Attendees:",
        ] + ([f"  {p}" for p in _merged_attendees_display(event).split(", ")] if event.attendees or event.organizer_email else ["  (none)"])
        filtered_data = {
            "id": event.id,
            "calendar_id": event.calendar_id,
            "title": event.title,
            "description": event.description,
            "start_time": event.start_time,
            "end_time": event.end_time,
            "day_of_week": _day_of_week(event.start_time),
            "all_day": event.all_day,
            "organizer_email": event.organizer_email,
            "attendees": [
                {"email": a.email, "display_name": a.display_name,
                 "response_status": a.response_status, "organizer": a.organizer}
                for a in (event.attendees or [])
            ],
            "location": event.location,
            "status": event.status,
            "html_link": event.html_link,
        }
        return await gated_call(
            connector=self.name,
            tool="calendar_get_event_details",
            tool_name="Read Calendar Event",
            summary=f"Read \"{event.title}\"",
            sender=event.organizer_email or calendar_id,
            raw_data=event,
            filtered_data=filtered_data,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text="\n".join(details_lines),
            pii_scan_text=event.description or "",
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_event(
        self,
        calendar_id: str,
        title: str,
        start_time: str,
        end_time: str,
        description: str = "",
        attendees: str = "",
        location: str = "",
        add_google_meet: bool = False,
        rooms: str = "",
        color: str = "",
        recurrence: str = "",
    ) -> Any:
        _require_rfc3339("start_time", start_time, need_offset=False)
        _require_rfc3339("end_time", end_time, need_offset=False)
        _require_end_after_start(start_time, end_time)
        color_id = _normalize_color_arg(color)
        attendee_list = [e.strip() for e in attendees.split(",") if e.strip()] if attendees else []
        room_list = [r.strip() for r in rooms.split(",") if r.strip()] if rooms else []
        preview = {
            "Title": title,
            "Time": f"{start_time} – {end_time}",
            "Calendar": await self._calendar_name_for(calendar_id),
        }
        if location:
            preview["Location"] = location
        if add_google_meet:
            preview["Conferencing"] = "Google Meet (will be created)"
        if room_list:
            preview["Rooms"] = ", ".join(room_list)
        if attendee_list:
            preview["Attendees"] = ", ".join(attendee_list)
        if color_id:
            preview["Color"] = EVENT_COLOR_NAMES.get(color_id, color_id)
        if recurrence:
            # Shown verbatim -- humanizing every RRULE/EXDATE/RDATE/EXRULE
            # variant into a friendly sentence is out of scope; the reviewer
            # sees exactly what's being sent to the Calendar API.
            preview["Recurrence"] = recurrence
        raw_data = {
            "calendar_id": calendar_id, "title": title,
            "start_time": start_time, "end_time": end_time,
            "description": description, "attendees": attendee_list,
            "location": location, "add_google_meet": add_google_meet, "rooms": room_list,
            "color": color_id, "recurrence": recurrence,
        }
        await gated_call(
            connector=self.name,
            tool="calendar_create_event",
            tool_name="Create Calendar Event",
            summary=f"Create \"{title}\" on {start_time}",
            sender=calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=description or "No description provided; see preview for event details.",
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "attendees": attendees},
        )
        event = await self._fetch(
            self._calendar.create_event,
            calendar_id, title, start_time, end_time, description,
            attendee_list or None, location, add_google_meet, room_list or None, color_id,
            recurrence,
        )
        result = {"id": event.id, "title": event.title, "start_time": event.start_time,
                  "end_time": event.end_time, "html_link": event.html_link}
        if event.conference_link or event.hangout_link:
            result["conference_link"] = event.conference_link or event.hangout_link
        return result

    async def _update_event(
        self,
        calendar_id: str,
        event_id: str,
        title: str = "",
        start_time: str = "",
        end_time: str = "",
        description: str = "",
        location: str = "",
        add_google_meet: bool = False,
        rooms: str = "",
        color: str = "",
        scope: str = "this",
        send_updates: str = "",
    ) -> Any:
        if start_time:
            _require_rfc3339("start_time", start_time, need_offset=False)
        if end_time:
            _require_rfc3339("end_time", end_time, need_offset=False)
        color_id = _normalize_color_arg(color)
        scope = _normalize_scope_arg(scope)
        send_updates = _normalize_send_updates_arg(send_updates)
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        room_list = [r.strip() for r in rooms.split(",") if r.strip()] if rooms else []
        # Event/Start/End always appear (unlike Description/Location/
        # Conferencing/Rooms below, which are only shown when actually
        # provided) -- each is the plain current value if unchanged, or
        # "old → new" if this call is changing it, so the reviewer always
        # sees what the event's core identity/timing actually is, not just
        # a partial list of what happens to be different this time.
        def _diff_or_value(new_value: str, old_value: str) -> str:
            return f"{old_value} → {new_value}" if new_value and new_value != old_value else old_value

        changed_field_names = []
        if title and title != event.title:
            changed_field_names.append("Event")
        if start_time and start_time != event.start_time:
            changed_field_names.append("Start")
        if end_time and end_time != event.end_time:
            changed_field_names.append("End")
        changes = {}
        if description and description != event.description:
            changes["Description"] = "(changed)"
        if location and location != event.location:
            changes["Location"] = f"{event.location or '(none)'} → {location}"
        if add_google_meet and not (event.conference_link or event.hangout_link):
            changes["Conferencing"] = "Add Google Meet"
        if room_list:
            changes["Rooms"] = f"Book: {', '.join(room_list)}"
        if color_id and color_id != event.color_id:
            old_color = EVENT_COLOR_NAMES.get(event.color_id, event.color_id) if event.color_id else "(default)"
            new_color = EVENT_COLOR_NAMES.get(color_id, color_id)
            changes["Color"] = f"{old_color} → {new_color}"
        changed_field_names.extend(changes.keys())
        # "Applies to" only means anything for a recurring event -- a plain
        # event has no series for scope to distinguish between, so the row
        # is omitted rather than always showing a moot "This event".
        if event.recurring_event_id or event.recurrence:
            changes["Applies to"] = _SCOPE_LABELS[scope]
        preview = {
            "Event": _diff_or_value(title, event.title),
            # Calendar can never change via this tool (no destination-
            # calendar param) -- always the plain current value, same as
            # an unchanged Event/Start/End would be.
            "Calendar": await self._calendar_name_for(calendar_id),
            "Start": _diff_or_value(start_time, event.start_time),
            "End": _diff_or_value(end_time, event.end_time),
            **changes,
        }
        raw_data = {
            "calendar_id": calendar_id, "event_id": event_id,
            "current_title": event.title, "new_title": title,
            "start_time": start_time, "end_time": end_time,
            "description": description, "location": location,
            "add_google_meet": add_google_meet, "rooms": room_list,
            "color": color_id, "scope": scope,
            "organizer_email": event.organizer_email,
            "attendees": [a.email for a in (event.attendees or [])],
        }
        if description and description != event.description:
            details_text = description
        else:
            changed_fields = ", ".join(changed_field_names) or "no fields"
            details_text = f"{changed_fields} will be updated; description is unchanged."
        await gated_call(
            connector=self.name,
            tool="calendar_update_event",
            tool_name="Update Calendar Event",
            summary=f"Update \"{event.title}\"",
            sender=event.organizer_email or calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details_text,
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id, "scope": scope},
        )
        updated = await self._fetch(
            self._calendar.update_event,
            calendar_id, event_id, title or None, start_time or None,
            end_time or None, description or None, location or None,
            add_google_meet, room_list or None, color_id or None,
            scope, send_updates,
        )
        result = {"id": updated.id, "title": updated.title, "start_time": updated.start_time,
                  "end_time": updated.end_time, "html_link": updated.html_link}
        if updated.conference_link or updated.hangout_link:
            result["conference_link"] = updated.conference_link or updated.hangout_link
        return result

    async def _delete_event(
        self,
        calendar_id: str,
        event_id: str,
        scope: str = "this",
        send_updates: str = "",
    ) -> Any:
        scope = _normalize_scope_arg(scope)
        send_updates = _normalize_send_updates_arg(send_updates)
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        preview = {
            "Event": event.title or "(untitled)",
            "Calendar": await self._calendar_name_for(calendar_id),
            "Time": f"{event.start_time} – {event.end_time}",
        }
        if event.recurring_event_id or event.recurrence:
            preview["Applies to"] = _SCOPE_LABELS[scope]
        raw_data = {
            "calendar_id": calendar_id, "event_id": event_id, "scope": scope,
            "organizer_email": event.organizer_email,
            "attendees": [a.email for a in (event.attendees or [])],
        }
        await gated_call(
            connector=self.name,
            tool="calendar_delete_event",
            tool_name="Delete Calendar Event",
            summary=f"Delete \"{event.title}\"",
            sender=event.organizer_email or calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=f"{_SCOPE_LABELS[scope]} will be permanently deleted.",
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id, "scope": scope},
        )
        await self._fetch(self._calendar.delete_event, calendar_id, event_id, scope, send_updates)
        return {"id": event_id, "deleted": True, "scope": scope}

    async def _create_out_of_office(
        self,
        start_time: str,
        end_time: str,
        title: str = "Out of Office",
        decline_message: str = "",
    ) -> Any:
        _require_rfc3339("start_time", start_time, need_offset=False)
        _require_rfc3339("end_time", end_time, need_offset=False)
        _require_end_after_start(start_time, end_time)
        preview = {
            "Title": title,
            "Time": f"{start_time} – {end_time}",
            "Auto-decline": "New conflicting invitations only",
        }
        raw_data = {
            "title": title, "start_time": start_time, "end_time": end_time,
            "decline_message": decline_message,
        }
        await gated_call(
            connector=self.name,
            tool="calendar_create_out_of_office",
            tool_name="Create Out of Office",
            summary=f"Out of Office \"{title}\" {start_time} – {end_time}",
            sender="primary",
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=decline_message or "(no custom decline message)",
            my_email=self.my_email,
            args={},
        )
        event = await self._fetch(
            self._calendar.create_out_of_office, title, start_time, end_time, decline_message,
        )
        return {"id": event.id, "title": event.title, "start_time": event.start_time,
                "end_time": event.end_time, "html_link": event.html_link}

    async def _set_working_location(
        self,
        date: str,
        location: str,
        building_id: str = "",
        label: str = "",
    ) -> Any:
        try:
            _date.fromisoformat(date)
        except (ValueError, TypeError):
            raise ValueError("date must be a day as YYYY-MM-DD, e.g. 2026-07-10") from None
        if location not in WORKING_LOCATION_TYPES:
            raise ValueError(
                f"location must be one of {sorted(WORKING_LOCATION_TYPES)}, got {location!r}"
            )
        location_display = {"office": "Office", "home": "Home"}.get(location, location)
        preview = {"Date": date, "Location": location_display}
        if building_id:
            preview["Building"] = building_id
        if label:
            preview["Label"] = label
        raw_data = {"date": date, "location": location, "building_id": building_id, "label": label}
        await gated_call(
            connector=self.name,
            tool="calendar_set_working_location",
            tool_name="Set Working Location",
            summary=f"Set {location_display} on {date}",
            sender="primary",
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Working location will be set as shown above; no other calendar changes.",
            my_email=self.my_email,
            args={},
        )
        event = await self._fetch(
            self._calendar.set_working_location, date, location, building_id, label,
        )
        return {"id": event.id, "start_time": event.start_time, "end_time": event.end_time,
                "html_link": event.html_link}

    async def _set_event_visibility(self, calendar_id: str, event_id: str, visibility: str) -> Any:
        # Validate before gating, not after -- same reasoning as
        # drive_sheets_insert_dimensions's early dimension check: a doomed
        # call shouldn't cost the user an unnecessary approval decision.
        visibility = visibility.strip().lower()
        if visibility not in VALID_VISIBILITIES:
            raise ValueError(
                f"calendar_set_event_visibility: visibility must be one of "
                f"{sorted(VALID_VISIBILITIES)}, got {visibility!r}"
            )
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        preview = {
            "Event": event.title or "(untitled)",
            "Calendar": await self._calendar_name_for(calendar_id),
            "Visibility": f"{event.visibility} → {visibility}",
        }
        raw_data = {
            "calendar_id": calendar_id, "event_id": event_id, "visibility": visibility,
            "organizer_email": event.organizer_email,
            "attendees": [a.email for a in (event.attendees or [])],
        }
        await gated_call(
            connector=self.name,
            tool="calendar_set_event_visibility",
            tool_name="Set Event Visibility",
            summary=f"Set visibility of \"{event.title}\" to {visibility}",
            sender=event.organizer_email or calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Only the event's visibility will change; no other fields are affected.",
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id, "visibility": visibility},
        )
        updated = await self._fetch(self._calendar.set_event_visibility, calendar_id, event_id, visibility)
        return {"id": updated.id, "title": updated.title, "visibility": updated.visibility}

    async def _set_event_color(self, calendar_id: str, event_id: str, color: str) -> Any:
        color_id = _normalize_color_arg(color)
        if not color_id:
            raise ValueError("calendar_set_event_color: color is required")
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        old_color = EVENT_COLOR_NAMES.get(event.color_id, event.color_id) if event.color_id else "(default)"
        new_color = EVENT_COLOR_NAMES.get(color_id, color_id)
        preview = {
            "Event": event.title or "(untitled)",
            "Calendar": await self._calendar_name_for(calendar_id),
            "Color": f"{old_color} → {new_color}",
        }
        raw_data = {
            "calendar_id": calendar_id, "event_id": event_id, "color": color_id,
            "organizer_email": event.organizer_email,
            "attendees": [a.email for a in (event.attendees or [])],
        }
        await gated_call(
            connector=self.name,
            tool="calendar_set_event_color",
            tool_name="Set Event Color",
            summary=f"Set color of \"{event.title}\" to {new_color}",
            sender=event.organizer_email or calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Only the event's color will change; no other fields are affected.",
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id, "color": color_id},
        )
        updated = await self._fetch(self._calendar.set_event_color, calendar_id, event_id, color_id)
        return {"id": updated.id, "title": updated.title, "color_id": updated.color_id}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except CalendarClientError as exc:
            logger.error("Calendar fetch failed: %s", exc)
            unavailable = unavailable_error("calendar", exc, self.my_email)
            if unavailable is not None:
                raise unavailable from exc
            raise RuntimeError(str(exc)) from exc

    async def _calendar_name_for(self, calendar_id: str) -> str:
        """Best-effort, cached calendar display-name lookup; falls back to
        the raw id (e.g. a room resource calendar_list can't resolve this
        way) rather than blocking the popup on a lookup that can't succeed."""
        if calendar_id in self._calendar_name_cache:
            return self._calendar_name_cache[calendar_id]
        try:
            entry = await self._fetch(self._calendar.get_calendar, calendar_id)
            name = entry.summary or calendar_id
        except RuntimeError:
            name = calendar_id
        self._calendar_name_cache[calendar_id] = name
        return name

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
