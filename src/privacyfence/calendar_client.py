"""Google Calendar API client.

Handles OAuth2 authorization and full read/write access to Google Calendar.
All event data is normalized into simple dataclasses so the rest of the
application never has to deal with the raw Calendar API payload shape.

Per project conventions we always use the documented Google client libraries
(`googleapiclient`, `google.auth`) and authenticate via the standard
google-auth-oauthlib installed-app flow.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .secure_files import atomic_write_text

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Values the Calendar API accepts for Event.visibility. "confidential" is a
# legacy synonym for "private" that the API still accepts on write.
VALID_VISIBILITIES = {"default", "public", "private", "confidential"}

# calendar_update_event/calendar_delete_event's edit-scope semantics for a
# recurring event instance -- "this" acts on exactly the given event id
# (the only behavior that existed before recurrence support, and still the
# default), "all" redirects to the series' master event, "following"
# splits the series in two at this instance (see
# _truncate_recurrence_before/_continuing_recurrence below).
VALID_EVENT_SCOPES = {"this", "following", "all"}

# Calendar API's sendUpdates values for insert/update/delete -- who gets a
# notification email about the change. "" (the default everywhere this is
# accepted below) omits the argument entirely, leaving the Calendar API's
# own default in effect rather than this client silently picking one.
VALID_SEND_UPDATES = {"none", "all", "externalOnly"}

# Calendar's fixed event color palette (Event.colorId, "1".."11"). The
# Calendar API's own colors().get() endpoint returns each id's hex
# background/foreground (see list_event_colors) but, unlike calendarList
# colors, never returns a name for it -- these are the names Calendar's own
# web UI shows for each id, kept here as a static, well-known mapping so
# callers can pass "Tomato" instead of memorizing "11".
EVENT_COLOR_NAMES: dict[str, str] = {
    "1": "Lavender",
    "2": "Sage",
    "3": "Grape",
    "4": "Flamingo",
    "5": "Banana",
    "6": "Tangerine",
    "7": "Peacock",
    "8": "Graphite",
    "9": "Blueberry",
    "10": "Basil",
    "11": "Tomato",
}


def normalize_event_color(color: str) -> str:
    """Resolve a ``color`` argument (a numeric colorId "1".."11", or a case-
    insensitive name like "Tomato") to the numeric colorId the Calendar API
    expects. Raises CalendarClientError for anything else.
    """
    color = color.strip()
    if color in EVENT_COLOR_NAMES:
        return color
    lowered = color.lower()
    for color_id, name in EVENT_COLOR_NAMES.items():
        if name.lower() == lowered:
            return color_id
    raise CalendarClientError(
        f"color must be an event color id (1-11) or name "
        f"({', '.join(EVENT_COLOR_NAMES.values())}), got {color!r}"
    )


class CalendarClientError(Exception):
    """Raised for unrecoverable Calendar client problems (auth, config, API)."""


@dataclass
class CalendarListEntry:
    id: str
    summary: str   # display name
    description: str
    primary: bool
    access_role: str


@dataclass
class CalendarAttendee:
    email: str
    display_name: str
    response_status: str  # "accepted" | "declined" | "tentative" | "needsAction"
    organizer: bool = False


@dataclass
class CalendarAttachment:
    """A file attached to an event — e.g. the "Notes by Gemini" doc and
    transcript that Google Meet's Gemini note-taker attaches to the Calendar
    event after a meeting ends. ``file_id`` is a Drive file id and can be
    passed straight to ``drive_get_file_content`` to read the content,
    provided the authenticated user has Drive access to it."""

    file_id: str
    title: str
    mime_type: str
    file_url: str
    icon_link: str = ""


@dataclass
class CalendarEvent:
    id: str
    calendar_id: str
    title: str
    description: str
    start_time: str   # ISO 8601
    end_time: str     # ISO 8601
    all_day: bool
    organizer_email: str
    attendees: list[CalendarAttendee]
    location: str
    hangout_link: str
    conference_link: str
    status: str       # "confirmed" | "tentative" | "cancelled"
    html_link: str
    attachments: list[CalendarAttachment] = field(default_factory=list)
    visibility: str = "default"  # "default" | "public" | "private" | "confidential"
    color_id: str = ""  # "1".."11" (see EVENT_COLOR_NAMES), or "" for the calendar's default color
    recurrence: list[str] = field(default_factory=list)  # raw RRULE/EXDATE/RDATE/EXRULE lines --
        # only ever present on a series' own master event, empty otherwise (including on
        # individual expanded instances, which carry recurring_event_id instead)
    recurring_event_id: str = ""  # non-empty iff this is one expanded instance of a recurring
        # series -- the id of that series' master event (a distinct id from this instance's own)
    original_start_time: str = ""  # this instance's originally-scheduled start (ISO 8601 or
        # date), before any per-instance reschedule -- only present on a recurring instance

    def short_summary(self) -> str:
        return f"{self.title} ({self.start_time})"


@dataclass
class EventColor:
    """One entry of Calendar's fixed event color palette, as returned by
    colors().get() plus the name from EVENT_COLOR_NAMES (the API itself
    never returns a name for an event color)."""

    id: str
    name: str
    background: str
    foreground: str


@dataclass
class CalendarRoom:
    resource_id: str
    resource_name: str
    resource_email: str
    building_id: str
    floor_name: str
    capacity: int
    description: str


@dataclass
class FreeBusySlot:
    start: str
    end: str


@dataclass
class FreeBusyResult:
    email: str
    busy: list[FreeBusySlot]


def _has_timezone(iso_str: str) -> bool:
    """Return True if the ISO 8601 string already carries timezone information."""
    try:
        return datetime.fromisoformat(iso_str).tzinfo is not None
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Recurring-series splitting (update_event/delete_event scope="following"):
# Google Calendar's API has no single-call primitive for "this and following
# events" -- the documented approach is to end the original series with an
# UNTIL on its RRULE and, for an update, insert a new recurring event
# starting at that instance with the remaining pattern. These helpers do the
# RRULE string surgery that takes; see update_event's own docstring for the
# full two-call sequence and its documented limitations.
# --------------------------------------------------------------------------- #

def _rrule_parts(line: str) -> tuple[str, list[str]]:
    """Split one RRULE line ("RRULE:FREQ=WEEKLY;COUNT=10") into its
    "RRULE" prefix and a list of "KEY=VALUE" parts, for editing individual
    parts without disturbing the rest."""
    prefix, _, body = line.partition(":")
    return prefix, [p for p in body.split(";") if p]


def _set_rrule_until(line: str, until_value: str) -> str:
    """Replace (or add) an RRULE's UNTIL part with ``until_value``,
    dropping any existing COUNT -- RRULE forbids specifying both. This is
    the "old half" of a series split at some instance."""
    prefix, parts = _rrule_parts(line)
    kept = [p for p in parts if not p.startswith("UNTIL=") and not p.startswith("COUNT=")]
    kept.append(f"UNTIL={until_value}")
    return f"{prefix}:{';'.join(kept)}"


def _strip_rrule_bounds(line: str) -> str:
    """Drop UNTIL/COUNT from an RRULE, leaving it open-ended. This is the
    "new half" of a series split at some instance: it continues the
    original pattern indefinitely rather than trying to carry over an
    exact remaining occurrence count, which isn't recoverable from the
    original RRULE alone -- a documented simplification, not an oversight."""
    prefix, parts = _rrule_parts(line)
    kept = [p for p in parts if not p.startswith("UNTIL=") and not p.startswith("COUNT=")]
    return f"{prefix}:{';'.join(kept)}"


def _until_before(start: dict[str, str]) -> str:
    """The RRULE UNTIL value that excludes the instance whose Calendar API
    ``start`` dict (or ``originalStartTime``) is given -- one second before
    a timed start, one day before an all-day start. UNTIL is *inclusive*,
    so landing exactly on the instance's own start would still recur it.
    Per RFC 5545, UNTIL must be a bare date for an all-day DTSTART, and a
    UTC date-time (trailing "Z") otherwise."""
    if "date" in start and "dateTime" not in start:
        cutoff_date = _date.fromisoformat(start["date"]) - timedelta(days=1)
        return cutoff_date.strftime("%Y%m%d")
    dt = datetime.fromisoformat(start.get("dateTime", ""))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    cutoff_dt = dt - timedelta(seconds=1)
    return cutoff_dt.strftime("%Y%m%dT%H%M%SZ")


def _truncate_recurrence_before(recurrence: list[str], start: dict[str, str]) -> list[str]:
    """The master series' own ``recurrence`` list, truncated so it ends
    just before ``start`` -- the "old half" of a series split at some
    instance. Non-RRULE lines (EXDATE/RDATE/EXRULE) pass through
    unchanged; any landing on or after the split point become inert once
    past the new UNTIL, so leaving them in is harmless for this half."""
    until_value = _until_before(start)
    return [
        _set_rrule_until(line, until_value) if line.startswith("RRULE:") else line
        for line in recurrence
    ]


def _continuing_recurrence(recurrence: list[str]) -> list[str]:
    """The recurrence list for the "new half" of a series split at some
    instance -- same RRULE pattern(s), no end bound. EXDATE/RDATE/EXRULE
    lines are dropped rather than carried over: they named specific dates
    in the old series, and reapplying them verbatim to a newly-inserted
    event risks either the Calendar API rejecting an EXDATE that no longer
    matches an occurrence, or silently resurrecting one the old series had
    cancelled. Documented limitation, not a crash risk either way."""
    return [_strip_rrule_bounds(line) for line in recurrence if line.startswith("RRULE:")]


class CalendarClient:
    """Google Calendar client with OAuth2 token caching."""

    def __init__(self, client_config: dict, token_file: str) -> None:
        self._client_config = client_config
        self._token_file = token_file
        # googleapiclient service objects (and the httplib2 transport they
        # wrap) are not thread-safe. Requests are dispatched to a thread per
        # call (see connectors/*.py._fetch), so a single shared service can
        # have two threads read/write the same socket concurrently,
        # corrupting the connection (observed as SSL: WRONG_VERSION_NUMBER
        # on a later, unrelated request reusing the same connection). Keep
        # one service per thread instead of one shared instance.
        self._local = threading.local()
        self._creds_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token.

        ``client_config`` comes from the organization config bundle (installed
        via PrivacyFence Settings), not a file on disk.
        """
        if not self._client_config:
            raise CalendarClientError(
                "No Google organization config installed. Install/Update "
                "Organization Config from PrivacyFence Settings first."
            )
        logger.info("Starting Calendar interactive OAuth flow")
        flow = InstalledAppFlow.from_client_config(self._client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("Calendar OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        # Guards concurrent refresh/save of the shared token file when
        # multiple threads hit an expired token at the same time.
        with self._creds_lock:
            if not os.path.exists(self._token_file):
                raise CalendarClientError(
                    f"No OAuth token found at '{self._token_file}'. "
                    "Run with '--calendar-oauth' to authorize."
                )
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Calendar OAuth token")
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    raise CalendarClientError(
                        f"Failed to refresh Calendar OAuth token: {exc}. "
                        "Re-run with '--calendar-oauth' to re-authorize."
                    ) from exc
                self._save_token(creds)
                return creds
            raise CalendarClientError(
                "Cached Calendar OAuth token is invalid. Re-run with '--calendar-oauth'."
            )

    def _save_token(self, creds: Credentials) -> None:
        atomic_write_text(self._token_file, creds.to_json())

    def _get_service(self):
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self._load_credentials()
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            self._local.service = service
            logger.debug("Calendar API service initialized for thread %s", threading.current_thread().name)
        return service

    # ------------------------------------------------------------------ #
    # Connection check
    # ------------------------------------------------------------------ #

    def check_connection(self) -> str:
        """Verify credentials. Returns the primary calendar email."""
        try:
            primary = self._get_service().calendars().get(calendarId="primary").execute()
        except HttpError as exc:
            raise CalendarClientError(f"Calendar connection check failed: {exc}") from exc
        email = primary.get("id", "unknown")
        logger.info("Connected to Calendar as %s", email)
        return email

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #

    def list_calendars(self) -> list[CalendarListEntry]:
        """List all calendars for the authenticated user."""
        try:
            result = self._get_service().calendarList().list().execute()
        except HttpError as exc:
            raise CalendarClientError(f"list_calendars failed: {exc}") from exc
        entries = []
        for raw in result.get("items", []):
            entries.append(CalendarListEntry(
                id=raw.get("id", ""),
                summary=raw.get("summary", ""),
                description=raw.get("description", ""),
                primary=bool(raw.get("primary", False)),
                access_role=raw.get("accessRole", ""),
            ))
        logger.info("list_calendars returned %d calendar(s)", len(entries))
        return entries

    def get_calendar(self, calendar_id: str) -> CalendarListEntry:
        """Fetch a single calendar's entry by id (accepts the 'primary' alias)."""
        if not calendar_id:
            raise CalendarClientError("get_calendar requires a calendar_id")
        try:
            raw = self._get_service().calendarList().get(calendarId=calendar_id).execute()
        except HttpError as exc:
            raise CalendarClientError(f"get_calendar({calendar_id}) failed: {exc}") from exc
        return CalendarListEntry(
            id=raw.get("id", ""),
            summary=raw.get("summary", ""),
            description=raw.get("description", ""),
            primary=bool(raw.get("primary", False)),
            access_role=raw.get("accessRole", ""),
        )

    def list_event_colors(self) -> list[EventColor]:
        """List Calendar's fixed event color palette (id, name, hex background/
        foreground) via colors().get() -- so a caller can show/pick "Tomato"
        instead of guessing what numeric colorId 11 looks like. The Calendar
        API returns colorList entries under "event" (and, separately,
        "calendar" -- a different palette for whole calendars, not exposed
        here since nothing in this client sets a calendar's own color).
        """
        try:
            result = self._get_service().colors().get().execute()
        except HttpError as exc:
            raise CalendarClientError(f"list_event_colors failed: {exc}") from exc
        colors = []
        for color_id, raw in result.get("event", {}).items():
            colors.append(EventColor(
                id=color_id,
                name=EVENT_COLOR_NAMES.get(color_id, color_id),
                background=raw.get("background", ""),
                foreground=raw.get("foreground", ""),
            ))
        colors.sort(key=lambda c: int(c.id))
        logger.info("list_event_colors returned %d color(s)", len(colors))
        return colors

    def list_events(
        self,
        calendar_id: str,
        max_results: int = 20,
        time_min: str = "",
        time_max: str = "",
        query: str = "",
    ) -> list[CalendarEvent]:
        """List events from a calendar."""
        max_results = max(1, min(int(max_results), 250))
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min:
            kwargs["timeMin"] = time_min
        if time_max:
            kwargs["timeMax"] = time_max
        if query:
            kwargs["q"] = query
        try:
            result = self._get_service().events().list(**kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"list_events({calendar_id}) failed: {exc}") from exc
        events = [self._parse_event(raw, calendar_id) for raw in result.get("items", [])]
        logger.info("list_events %s returned %d event(s)", calendar_id, len(events))
        return events

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        """Fetch a single event by id, including its attachments.

        Attachments (e.g. Google Meet's Gemini note-taker attaching the
        meeting notes/transcript doc once a meeting ends) come back in the
        response with no extra parameter needed. ``supportsAttachments`` only
        applies to ``insert``/``update``/``patch``/``import`` — the Calendar
        API's ``events.get`` doesn't accept it at all, and passing it raises
        a client-side ``TypeError`` before any request is even sent.
        """
        if not calendar_id or not event_id:
            raise CalendarClientError("get_event requires calendar_id and event_id")
        try:
            raw = (
                self._get_service()
                .events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"get_event({calendar_id}, {event_id}) failed: {exc}") from exc
        event = self._parse_event(raw, calendar_id)
        logger.info("get_event %s: %s", event_id, event.short_summary())
        return event

    def get_free_busy(
        self, emails: list[str], time_min: str, time_max: str
    ) -> list[FreeBusyResult]:
        """Query free/busy status for a list of emails."""
        if not emails:
            raise CalendarClientError("get_free_busy requires a non-empty emails list")
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": e} for e in emails],
        }
        try:
            result = self._get_service().freebusy().query(body=body).execute()
        except HttpError as exc:
            raise CalendarClientError(f"get_free_busy failed: {exc}") from exc
        calendars = result.get("calendars", {})
        results = []
        for email in emails:
            cal_data = calendars.get(email, {})
            busy_periods = [
                FreeBusySlot(start=b.get("start", ""), end=b.get("end", ""))
                for b in cal_data.get("busy", [])
            ]
            results.append(FreeBusyResult(email=email, busy=busy_periods))
        logger.info("get_free_busy: %d email(s) queried", len(emails))
        return results

    def get_colleagues_schedule(
        self, emails: list[str], time_min: str, time_max: str
    ) -> list[dict]:
        """Try events.list per calendar; fall back to free/busy for inaccessible calendars.

        Returns a list of per-email dicts with source="events" (full event titles) or
        source="free_busy" (only busy slots) depending on what the authenticated user
        can see.
        """
        results = []
        fallback_emails: list[str] = []

        for email in emails:
            try:
                events = self.list_events(
                    email, max_results=50, time_min=time_min, time_max=time_max
                )
                results.append({
                    "email": email,
                    "source": "events",
                    "events": [
                        {
                            "id": e.id,
                            "title": e.title,
                            "start_time": e.start_time,
                            "end_time": e.end_time,
                            "status": e.status,
                            "all_day": e.all_day,
                        }
                        for e in events
                    ],
                })
            except CalendarClientError:
                fallback_emails.append(email)

        if fallback_emails:
            try:
                fb = self.get_free_busy(fallback_emails, time_min, time_max)
                for r in fb:
                    results.append({
                        "email": r.email,
                        "source": "free_busy",
                        "busy": [{"start": s.start, "end": s.end} for s in r.busy],
                    })
            except CalendarClientError as exc:
                for email in fallback_emails:
                    results.append({"email": email, "source": "error", "error": str(exc)})

        return results

    # ------------------------------------------------------------------ #
    # Write operations
    # ------------------------------------------------------------------ #

    def create_event(
        self,
        calendar_id: str,
        title: str,
        start_time: str,
        end_time: str,
        description: str = "",
        attendees: list[str] | None = None,
        location: str = "",
        add_google_meet: bool = False,
        room_emails: list[str] | None = None,
        color: str = "",
        recurrence: str = "",
    ) -> CalendarEvent:
        """Create a new event and return the created CalendarEvent.

        ``recurrence`` is one or more RRULE/EXDATE/RDATE/EXRULE lines
        (e.g. ``"RRULE:FREQ=WEEKLY;COUNT=10"``), one per line -- passed
        straight through to the Calendar API's own ``recurrence`` field.
        Empty (the default) creates a non-recurring event, unchanged from
        this method's behavior before recurrence support existed.
        """
        recurrence_lines = [line.strip() for line in recurrence.splitlines() if line.strip()]
        start_entry: dict[str, str] = {"dateTime": start_time}
        end_entry: dict[str, str] = {"dateTime": end_time}
        # Only inject a UTC fallback when the ISO string has no embedded
        # offset -- if the caller already includes one (e.g. "+02:00"),
        # preserve it. A recurring event is the one exception: the Calendar
        # API requires an explicit IANA timeZone on start/end regardless of
        # whether dateTime already carries an offset -- expanding a
        # recurrence across DST needs a named zone, not just a fixed
        # instant's offset -- and rejects a recurring event that omits it
        # with "Missing time zone definition for start time" (confirmed
        # against the real API by qa_fixture_recorder.py's --lifecycle
        # check). "UTC" is the same fallback used everywhere else in this
        # client; there's no IANA zone name to recover from a bare offset.
        if not _has_timezone(start_time) or recurrence_lines:
            start_entry["timeZone"] = "UTC"
        if not _has_timezone(end_time) or recurrence_lines:
            end_entry["timeZone"] = "UTC"
        body: dict[str, Any] = {
            "summary": title,
            "start": start_entry,
            "end": end_entry,
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if color:
            body["colorId"] = normalize_event_color(color)
        if recurrence_lines:
            body["recurrence"] = recurrence_lines
        all_attendees = list(attendees or [])
        if room_emails:
            body["attendees"] = (
                [{"email": e} for e in all_attendees]
                + [{"email": r, "resource": True} for r in room_emails]
            )
        elif all_attendees:
            body["attendees"] = [{"email": e} for e in all_attendees]
        kwargs: dict[str, Any] = {"calendarId": calendar_id, "body": body}
        if add_google_meet:
            body["conferenceData"] = {
                "createRequest": {
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    "requestId": str(uuid.uuid4()),
                }
            }
            kwargs["conferenceDataVersion"] = 1
        try:
            raw = self._get_service().events().insert(**kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"create_event({calendar_id}) failed: {exc}") from exc
        event = self._parse_event(raw, calendar_id)
        logger.info("create_event: %s", event.short_summary())
        return event

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        title: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        description: str | None = None,
        location: str | None = None,
        add_google_meet: bool = False,
        room_emails: list[str] | None = None,
        color: str | None = None,
        scope: str = "this",
        send_updates: str = "",
    ) -> CalendarEvent:
        """Update fields on an existing event and return the updated CalendarEvent.

        ``scope`` controls which occurrences of a recurring event this
        touches, matching Google Calendar's own "This event" / "This and
        following events" / "All events" edit picker (VALID_EVENT_SCOPES):

        - ``"this"`` (default, and the only meaning for a non-recurring
          event): acts on exactly ``event_id`` as given -- unchanged from
          this method's behavior before recurrence support existed.
        - ``"all"``: redirects to the series' master event
          (``recurringEventId``) when ``event_id`` names one instance, so
          the whole series updates in one call.
        - ``"following"``: splits the series at this instance -- the
          master's own ``recurrence`` gets an UNTIL ending the old series
          just before this instance, and a new event is inserted starting
          here, carrying this call's changes and the original recurrence
          pattern continued open-ended. There is no single Calendar API
          call for this; see _update_event_following for the two-call
          sequence and its documented limitations.

        ``send_updates`` is Calendar's own ``sendUpdates`` -- "none",
        "all", or "externalOnly" -- controlling who gets a notification
        email about the change. "" (default) omits the argument, leaving
        the Calendar API's own default in effect.
        """
        if scope not in VALID_EVENT_SCOPES:
            raise CalendarClientError(
                f"update_event: scope must be one of {sorted(VALID_EVENT_SCOPES)}, got {scope!r}"
            )
        if send_updates and send_updates not in VALID_SEND_UPDATES:
            raise CalendarClientError(
                f"update_event: send_updates must be one of {sorted(VALID_SEND_UPDATES)}, got {send_updates!r}"
            )
        # Validate before the fetch, not after -- a doomed call shouldn't
        # cost a wasted events().get() round trip, same reasoning as
        # set_event_visibility/set_event_color's own early validation.
        color_id = normalize_event_color(color) if color is not None else None

        try:
            raw = (
                self._get_service()
                .events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"update_event get({event_id}) failed: {exc}") from exc

        if scope == "following":
            return self._update_event_following(
                calendar_id, event_id, raw, title=title, start_time=start_time, end_time=end_time,
                description=description, location=location, add_google_meet=add_google_meet,
                room_emails=room_emails, color_id=color_id, send_updates=send_updates,
            )

        target_id = event_id
        if scope == "all" and raw.get("recurringEventId"):
            target_id = raw["recurringEventId"]
            try:
                raw = (
                    self._get_service()
                    .events()
                    .get(calendarId=calendar_id, eventId=target_id)
                    .execute()
                )
            except HttpError as exc:
                raise CalendarClientError(f"update_event get master({target_id}) failed: {exc}") from exc

        self._apply_event_field_changes(
            raw, title=title, description=description, location=location, color_id=color_id,
            start_time=start_time, end_time=end_time, room_emails=room_emails,
        )

        kwargs: dict[str, Any] = {"calendarId": calendar_id, "eventId": target_id, "body": raw}
        if add_google_meet and not raw.get("conferenceData"):
            raw["conferenceData"] = {
                "createRequest": {
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    "requestId": str(uuid.uuid4()),
                }
            }
            kwargs["conferenceDataVersion"] = 1
        elif raw.get("conferenceData"):
            kwargs["conferenceDataVersion"] = 1
        if send_updates:
            kwargs["sendUpdates"] = send_updates

        try:
            updated = self._get_service().events().update(**kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"update_event({target_id}) failed: {exc}") from exc
        event = self._parse_event(updated, calendar_id)
        logger.info("update_event: %s", event.short_summary())
        return event

    def _apply_event_field_changes(
        self,
        raw: dict[str, Any],
        *,
        title: str | None,
        description: str | None,
        location: str | None,
        color_id: str | None,
        start_time: str | None,
        end_time: str | None,
        room_emails: list[str] | None,
    ) -> None:
        """Mutate ``raw`` in place with only the fields actually given --
        the same fetch-modify-update field application update_event has
        always done, factored out so every scope branch (this/all/
        following) applies changes identically."""
        if title is not None:
            raw["summary"] = title
        if description is not None:
            raw["description"] = description
        if location is not None:
            raw["location"] = location
        if color_id is not None:
            raw["colorId"] = color_id
        if start_time is not None:
            raw.setdefault("start", {})["dateTime"] = start_time
            raw["start"].setdefault("timeZone", "UTC")
        if end_time is not None:
            raw.setdefault("end", {})["dateTime"] = end_time
            raw["end"].setdefault("timeZone", "UTC")
        if room_emails:
            existing = [a for a in raw.get("attendees", []) if not a.get("resource")]
            raw["attendees"] = existing + [{"email": r, "resource": True} for r in room_emails]

    def _update_event_following(
        self,
        calendar_id: str,
        event_id: str,
        instance_raw: dict[str, Any],
        *,
        title: str | None,
        start_time: str | None,
        end_time: str | None,
        description: str | None,
        location: str | None,
        add_google_meet: bool,
        room_emails: list[str] | None,
        color_id: str | None,
        send_updates: str,
    ) -> CalendarEvent:
        """scope="following": split the series at ``event_id`` (already
        fetched as ``instance_raw``) -- truncate the master's recurrence
        with an UNTIL just before this instance, then insert a new event
        here carrying this call's changes and the original recurrence
        pattern continued open-ended (see _continuing_recurrence).

        Known limitations, documented rather than silently wrong: the new
        half's recurrence drops any EXDATE/RDATE/EXRULE the old series had
        (see _continuing_recurrence), and conferencing (a Meet link tied
        to the old event id) isn't carried over -- pass ``add_google_meet``
        again on this call for the new half to get its own.
        """
        recurring_event_id = instance_raw.get("recurringEventId", "")
        if recurring_event_id:
            split_point = instance_raw.get("originalStartTime") or instance_raw.get("start") or {}
            try:
                master_raw = (
                    self._get_service()
                    .events()
                    .get(calendarId=calendar_id, eventId=recurring_event_id)
                    .execute()
                )
            except HttpError as exc:
                raise CalendarClientError(
                    f"update_event get master({recurring_event_id}) failed: {exc}"
                ) from exc
        elif instance_raw.get("recurrence"):
            # event_id already names the series' own master -- "following"
            # from the master's own first instance is the whole series.
            recurring_event_id = event_id
            master_raw = instance_raw
            split_point = instance_raw.get("start") or {}
        else:
            raise CalendarClientError(
                f"update_event: event {event_id!r} is not part of a recurring series; "
                "scope='following' requires a recurring event"
            )

        master_recurrence = master_raw.get("recurrence") or []
        truncate_kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "eventId": recurring_event_id,
            "body": {**master_raw, "recurrence": _truncate_recurrence_before(master_recurrence, split_point)},
        }
        if send_updates:
            truncate_kwargs["sendUpdates"] = send_updates
        try:
            self._get_service().events().update(**truncate_kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(
                f"update_event truncate master({recurring_event_id}) failed: {exc}"
            ) from exc

        new_body = dict(instance_raw)
        for key in (
            "id", "recurringEventId", "originalStartTime", "etag", "iCalUID",
            "htmlLink", "hangoutLink", "conferenceData", "created", "updated", "sequence", "status",
        ):
            new_body.pop(key, None)
        new_body["recurrence"] = _continuing_recurrence(master_recurrence)
        self._apply_event_field_changes(
            new_body, title=title, description=description, location=location, color_id=color_id,
            start_time=start_time, end_time=end_time, room_emails=room_emails,
        )

        insert_kwargs: dict[str, Any] = {"calendarId": calendar_id, "body": new_body}
        if add_google_meet:
            new_body["conferenceData"] = {
                "createRequest": {
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    "requestId": str(uuid.uuid4()),
                }
            }
            insert_kwargs["conferenceDataVersion"] = 1
        if send_updates:
            insert_kwargs["sendUpdates"] = send_updates

        try:
            created = self._get_service().events().insert(**insert_kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"update_event insert new series({event_id}) failed: {exc}") from exc
        event = self._parse_event(created, calendar_id)
        logger.info("update_event: split series at %s -> new head %s", event_id, event.id)
        return event

    def delete_event(self, calendar_id: str, event_id: str, scope: str = "this", send_updates: str = "") -> None:
        """Delete an event. ``scope`` mirrors update_event's own "this"/
        "following"/"all" semantics (see its docstring):

        - ``"this"``: deletes exactly ``event_id``.
        - ``"all"``: deletes the whole series via its master id.
        - ``"following"``: truncates the series' recurrence so it ends
          just before this instance, rather than deleting anything
          outright -- the instances after the cutoff simply stop being
          generated. There is nothing to insert here, unlike
          update_event's own "following": deleting the remainder needs no
          replacement series.
        """
        if scope not in VALID_EVENT_SCOPES:
            raise CalendarClientError(
                f"delete_event: scope must be one of {sorted(VALID_EVENT_SCOPES)}, got {scope!r}"
            )
        if send_updates and send_updates not in VALID_SEND_UPDATES:
            raise CalendarClientError(
                f"delete_event: send_updates must be one of {sorted(VALID_SEND_UPDATES)}, got {send_updates!r}"
            )

        if scope == "this":
            self._delete_event_id(calendar_id, event_id, send_updates)
            logger.info("delete_event: %s (this instance)", event_id)
            return

        try:
            raw = (
                self._get_service()
                .events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"delete_event get({event_id}) failed: {exc}") from exc
        recurring_event_id = raw.get("recurringEventId", "")

        if scope == "all":
            target_id = recurring_event_id or event_id
            self._delete_event_id(calendar_id, target_id, send_updates)
            logger.info("delete_event: %s (entire series)", target_id)
            return

        # scope == "following"
        if recurring_event_id:
            split_point = raw.get("originalStartTime") or raw.get("start") or {}
        elif raw.get("recurrence"):
            # event_id already names the master -- "following" from its
            # own first instance deletes the whole series.
            self._delete_event_id(calendar_id, event_id, send_updates)
            logger.info("delete_event: %s (entire series)", event_id)
            return
        else:
            raise CalendarClientError(
                f"delete_event: event {event_id!r} is not part of a recurring series; "
                "scope='following' requires a recurring event"
            )

        try:
            master_raw = (
                self._get_service()
                .events()
                .get(calendarId=calendar_id, eventId=recurring_event_id)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"delete_event get master({recurring_event_id}) failed: {exc}") from exc
        truncated = _truncate_recurrence_before(master_raw.get("recurrence") or [], split_point)
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "eventId": recurring_event_id,
            "body": {**master_raw, "recurrence": truncated},
        }
        if send_updates:
            kwargs["sendUpdates"] = send_updates
        try:
            self._get_service().events().update(**kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"delete_event truncate master({recurring_event_id}) failed: {exc}") from exc
        logger.info(
            "delete_event: truncated series %s before %s (this and following)",
            recurring_event_id, event_id,
        )

    def _delete_event_id(self, calendar_id: str, event_id: str, send_updates: str) -> None:
        kwargs: dict[str, Any] = {"calendarId": calendar_id, "eventId": event_id}
        if send_updates:
            kwargs["sendUpdates"] = send_updates
        try:
            self._get_service().events().delete(**kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"delete_event({event_id}) failed: {exc}") from exc

    def set_event_visibility(self, calendar_id: str, event_id: str, visibility: str) -> CalendarEvent:
        """Set an event's visibility, leaving every other field untouched.

        ``visibility`` is one of VALID_VISIBILITIES ("default", "public",
        "private", "confidential"). Fetches the event's current full body
        first (same fetch-modify-update shape as update_event) so only the
        visibility field actually changes in the update request.
        """
        visibility = visibility.strip().lower()
        if visibility not in VALID_VISIBILITIES:
            raise CalendarClientError(
                f"set_event_visibility: visibility must be one of "
                f"{sorted(VALID_VISIBILITIES)}, got {visibility!r}"
            )
        try:
            raw = (
                self._get_service()
                .events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"set_event_visibility get({event_id}) failed: {exc}") from exc
        raw["visibility"] = visibility
        try:
            updated = (
                self._get_service()
                .events()
                .update(calendarId=calendar_id, eventId=event_id, body=raw)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"set_event_visibility update({event_id}) failed: {exc}") from exc
        event = self._parse_event(updated, calendar_id)
        logger.info("set_event_visibility: %s -> %s", event.short_summary(), visibility)
        return event

    def set_event_color(self, calendar_id: str, event_id: str, color: str) -> CalendarEvent:
        """Set an event's color, leaving every other field untouched.

        ``color`` is a numeric colorId ("1".."11") or name (see
        EVENT_COLOR_NAMES, e.g. "Tomato"). Fetches the event's current full
        body first (same fetch-modify-update shape as set_event_visibility)
        so only the color field actually changes in the update request.
        """
        color_id = normalize_event_color(color)
        try:
            raw = (
                self._get_service()
                .events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"set_event_color get({event_id}) failed: {exc}") from exc
        raw["colorId"] = color_id
        try:
            updated = (
                self._get_service()
                .events()
                .update(calendarId=calendar_id, eventId=event_id, body=raw)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"set_event_color update({event_id}) failed: {exc}") from exc
        event = self._parse_event(updated, calendar_id)
        logger.info("set_event_color: %s -> %s", event.short_summary(), color_id)
        return event

    def create_out_of_office(
        self,
        title: str,
        start_time: str,
        end_time: str,
        decline_message: str = "",
    ) -> CalendarEvent:
        """Create an out-of-office event that auto-declines new conflicting
        meeting invitations only.

        The Calendar API also offers "decline all conflicts" and "decline
        none" (see ``outOfOfficeProperties.autoDeclineMode``), but this
        method always uses ``declineOnlyNewConflictingInvitations`` — it
        isn't exposed as a parameter, by design. Out-of-office events are
        only supported on the primary calendar and can't be all-day events
        (both Calendar API constraints, not this client's choice).
        """
        start_entry: dict[str, str] = {"dateTime": start_time}
        end_entry: dict[str, str] = {"dateTime": end_time}
        if not _has_timezone(start_time):
            start_entry["timeZone"] = "UTC"
        if not _has_timezone(end_time):
            end_entry["timeZone"] = "UTC"
        out_of_office_properties: dict[str, Any] = {
            "autoDeclineMode": "declineOnlyNewConflictingInvitations",
        }
        if decline_message:
            out_of_office_properties["declineMessage"] = decline_message
        body: dict[str, Any] = {
            "summary": title,
            "start": start_entry,
            "end": end_entry,
            "eventType": "outOfOffice",
            "transparency": "opaque",
            "outOfOfficeProperties": out_of_office_properties,
        }
        try:
            raw = self._get_service().events().insert(calendarId="primary", body=body).execute()
        except HttpError as exc:
            raise CalendarClientError(f"create_out_of_office failed: {exc}") from exc
        event = self._parse_event(raw, "primary")
        logger.info("create_out_of_office: %s", event.short_summary())
        return event

    def set_working_location(
        self,
        date: str,
        location: str,
        building_id: str = "",
        label: str = "",
    ) -> CalendarEvent:
        """Set a working-location event for a single day — the same
        "where are you working today" presence picker Google Calendar's web
        UI exposes. Only ``"office"`` and ``"home"`` are supported (Calendar
        also has a third ``customLocation`` type, not offered here).

        Working location events are restricted to the primary calendar and
        Calendar requires ``visibility="public"`` + ``transparency="transparent"``
        on them (Calendar API constraints, not this client's choice).
        """
        location_key = {"office": "officeLocation", "home": "homeOffice"}.get(location)
        if location_key is None:
            raise CalendarClientError(
                f"set_working_location: location must be 'office' or 'home', got {location!r}"
            )
        working_location_properties: dict[str, Any] = {"type": location_key}
        if location_key == "officeLocation":
            office: dict[str, Any] = {}
            if building_id:
                office["buildingId"] = building_id
            if label:
                office["label"] = label
            working_location_properties["officeLocation"] = office
        # All-day (date-only) events use an exclusive end date -- Calendar
        # rejects start == end as a zero-length event, so a single-day
        # working-location event's end date is the following day.
        end_date = (_date.fromisoformat(date) + timedelta(days=1)).isoformat()
        body: dict[str, Any] = {
            "start": {"date": date},
            "end": {"date": end_date},
            "eventType": "workingLocation",
            "visibility": "public",
            "transparency": "transparent",
            "workingLocationProperties": working_location_properties,
        }
        # Calendar's insert API has no "one working-location event per day"
        # constraint of its own -- calling this twice for the same date
        # would otherwise leave two overlapping all-day entries instead of
        # one, unlike the Calendar web UI's own "set your working location"
        # picker, which always replaces the day's existing entry. Look for
        # one first and update it in place when found.
        existing_id = self._find_working_location_event_id(date, end_date)
        try:
            if existing_id is not None:
                raw = (
                    self._get_service()
                    .events()
                    .update(calendarId="primary", eventId=existing_id, body=body)
                    .execute()
                )
            else:
                raw = self._get_service().events().insert(calendarId="primary", body=body).execute()
        except HttpError as exc:
            raise CalendarClientError(f"set_working_location failed: {exc}") from exc
        event = self._parse_event(raw, "primary")
        logger.info(
            "set_working_location: %s on %s (%s)", location, date,
            "replaced existing entry" if existing_id is not None else "created new entry",
        )
        return event

    def _find_working_location_event_id(self, date: str, end_date: str) -> str | None:
        """The id of an existing workingLocation event already covering
        this exact day, if any -- see set_working_location()'s own comment
        for why it needs to update that instead of inserting a duplicate.
        Returns the first match if more than one somehow already exists
        (pre-existing duplicates aren't cleaned up here, only prevented
        going forward)."""
        try:
            result = (
                self._get_service()
                .events()
                .list(
                    calendarId="primary",
                    timeMin=f"{date}T00:00:00Z",
                    timeMax=f"{end_date}T00:00:00Z",
                    eventTypes=["workingLocation"],
                    singleEvents=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"set_working_location lookup failed: {exc}") from exc
        items = result.get("items", [])
        return items[0]["id"] if items else None

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    def _parse_event(self, raw: dict[str, Any], calendar_id: str) -> CalendarEvent:
        start = raw.get("start", {})
        end = raw.get("end", {})
        all_day = "date" in start and "dateTime" not in start
        start_time = start.get("dateTime") or start.get("date", "")
        end_time = end.get("dateTime") or end.get("date", "")

        organizer = raw.get("organizer", {})
        organizer_email = organizer.get("email", "")

        raw_attendees = raw.get("attendees", []) or []
        attendees = [
            CalendarAttendee(
                email=a.get("email", ""),
                display_name=a.get("displayName", ""),
                response_status=a.get("responseStatus", "needsAction"),
                organizer=bool(a.get("organizer", False)),
            )
            for a in raw_attendees
        ]

        conference_data = raw.get("conferenceData") or {}
        conference_link = ""
        for ep in conference_data.get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                conference_link = ep.get("uri", "")
                break

        attachments = [
            CalendarAttachment(
                file_id=a.get("fileId", ""),
                title=a.get("title", ""),
                mime_type=a.get("mimeType", ""),
                file_url=a.get("fileUrl", ""),
                icon_link=a.get("iconLink", ""),
            )
            for a in raw.get("attachments", []) or []
        ]

        original_start = raw.get("originalStartTime") or {}
        original_start_time = original_start.get("dateTime") or original_start.get("date", "")

        return CalendarEvent(
            id=raw.get("id", ""),
            calendar_id=calendar_id,
            title=raw.get("summary", ""),
            description=raw.get("description", ""),
            start_time=start_time,
            end_time=end_time,
            all_day=all_day,
            organizer_email=organizer_email,
            attendees=attendees,
            location=raw.get("location", ""),
            hangout_link=raw.get("hangoutLink", ""),
            conference_link=conference_link,
            status=raw.get("status", "confirmed"),
            html_link=raw.get("htmlLink", ""),
            attachments=attachments,
            visibility=raw.get("visibility", "default"),
            color_id=raw.get("colorId", ""),
            recurrence=list(raw.get("recurrence") or []),
            recurring_event_id=raw.get("recurringEventId", ""),
            original_start_time=original_start_time,
        )
