"""Account-id -> display-name directory shared by the Jira and Confluence clients.

Names are looked up lazily, only for the ids that actually appear in a read, through Jira's user
API (ADR 0117), and remembered in a small cache that can be persisted to disk. The accounts in
that cache are reported to Atlassian's personal data reporting API once per cycle, and an entry
Atlassian answers "closed" or "updated" for, or one neither retrieved nor reported within a cycle
and a day, is erased (ADR 0202). This module also
holds the ``@[Name](accountId)`` mention markup helpers used on reads and writes, and the
storage-format helpers Confluence needs.
"""
from __future__ import annotations

import html as html_lib
import contextlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import quote, quote_plus
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .secure_files import atomic_write_json

logger = logging.getLogger(__name__)

USER_CACHE_TTL = timedelta(days=7)
_NEGATIVE_LOOKUP_TTL = timedelta(hours=1)
_FETCH_FAILURE_COOLDOWN = timedelta(minutes=5)
BULK_BATCH_SIZE = 100
_BULK_PAGE_BUDGET = 5
FIND_USERS_MAX_RESULTS = 50
_HTTP_TIMEOUT_SECONDS = 30
ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9:_-]{10,128}")  # always used with .fullmatch()
MENTION_MARKUP_RE = re.compile(r"@\[([^\]\n]{1,200})\]\(([A-Za-z0-9:_-]{10,128})\)")
UNKNOWN_USER_LABEL = "unknown user"
CUSTOMER_ACCOUNT_LABEL = "Customer account"
REPORT_ACCOUNTS_URL = "https://api.atlassian.com/app/report-accounts/"
REPORT_BATCH_SIZE = 90
DEFAULT_REPORT_CYCLE = timedelta(days=7)
_MIN_REPORT_CYCLE = timedelta(hours=1)
_MAX_REPORT_CYCLE = timedelta(days=30)
# How long past its cycle an unreported entry is kept before it is erased instead.
REPORT_GRACE = timedelta(days=1)
_REPORT_RETRY = timedelta(hours=1)
CACHE_FILENAME = "atlassian_user_cache.json"
# The account id shape the reporting API accepts; anything else (Atlassian's placeholder "unknown"
# included) is never reported and simply expires.
_REPORTABLE_ID_RE = re.compile(r"[A-Za-z0-9:-]{1,128}")
# Deliberately loose: a false positive only relabels a name, a false negative leaks an address.
_EMAIL_RE = re.compile(r"[\w.+%'-]+@[\w-]+(?:\.[\w-]+)+")


class AtlassianUsersError(Exception):
    """Raised when a user lookup against Atlassian cannot complete."""


class AtlassianReportDeferred(AtlassianUsersError):
    """The reporting API answered 429; try again after ``retry_after``."""

    def __init__(self, retry_after: timedelta) -> None:
        super().__init__(f"personal data report rate limited; retry in {retry_after}")
        self.retry_after = retry_after


@dataclass
class ReportOutcome:
    statuses: dict[str, str]  # account id -> "closed" or "updated"; absent means no action
    cycle: timedelta | None  # the Cycle-Period header, when Atlassian sent one


@dataclass
class AtlassianUser:
    account_id: str
    display_name: str
    active: bool = True
    account_type: str = ""  # Atlassian's accountType: "atlassian", "app" or "customer"

    def __post_init__(self) -> None:
        # Every record (API response, cache file, remember()) is built here, so an email-shaped
        # name, typical of Jira Service Management customer accounts, can reach no output (ADR 0119).
        self.display_name = mask_emails(self.display_name, self.account_id)


def mask_emails(name: str, account_id: str) -> str:
    """Replace any email-shaped text with ``Customer account <last 4 of the id>``
    (``unknown user`` when there is no id to take the suffix from)."""
    label = f"{CUSTOMER_ACCOUNT_LABEL} {account_id[-4:]}" if account_id else UNKNOWN_USER_LABEL
    return _EMAIL_RE.sub(label, name)


def jira_api_base(cloud_id: str) -> str:
    return f"https://api.atlassian.com/ex/jira/{cloud_id}"


def parse_user(raw: dict[str, Any]) -> AtlassianUser | None:
    account_id = raw.get("accountId")
    if not account_id:
        return None
    return AtlassianUser(
        account_id=account_id,
        display_name=raw.get("displayName") or "",
        active=raw.get("active", True),
        account_type=raw.get("accountType") or "",
    )


def fetch_users_bulk(
    session: requests.Session, cloud_id: str, account_ids: list[str]
) -> list[AtlassianUser]:
    url = f"{jira_api_base(cloud_id)}/rest/api/3/user/bulk"
    users: list[AtlassianUser] = []
    start = 0
    for _ in range(_BULK_PAGE_BUDGET):
        params: list[tuple[str, Any]] = [("accountId", i) for i in account_ids]
        params += [("maxResults", BULK_BATCH_SIZE), ("startAt", start)]
        response = session.get(url, params=params, timeout=_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        values = data.get("values") or []
        users.extend(u for u in (parse_user(v) for v in values) if u is not None)
        if data.get("isLast") or not values:
            break
        start += len(values)
    return users


def search_users(
    session: requests.Session, cloud_id: str, query: str, max_results: int
) -> list[AtlassianUser]:
    params: dict[str, str | int] = {"query": query, "maxResults": max_results}
    response = session.get(
        f"{jira_api_base(cloud_id)}/rest/api/3/user/search",
        params=params,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return [u for u in (parse_user(v) for v in response.json()) if u is not None]


def _parse_seconds(value: str | None) -> timedelta | None:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return timedelta(seconds=seconds) if seconds > 0 else None


def _clamp_cycle(cycle: timedelta) -> timedelta:
    return max(_MIN_REPORT_CYCLE, min(cycle, _MAX_REPORT_CYCLE))


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def report_accounts(
    session: requests.Session, accounts: list[tuple[str, datetime]]
) -> ReportOutcome:
    """POST one batch (at most ``REPORT_BATCH_SIZE``) of account ids, each with the time its data
    was retrieved, to Atlassian's personal data reporting API for OAuth 2.0 (3LO) apps."""
    body = {"accounts": [{"accountId": i, "updatedAt": _rfc3339(t)} for i, t in accounts]}
    response = session.post(REPORT_ACCOUNTS_URL, json=body, timeout=_HTTP_TIMEOUT_SECONDS)
    cycle = _parse_seconds(response.headers.get("Cycle-Period"))
    if response.status_code == 429:
        raise AtlassianReportDeferred(
            _parse_seconds(response.headers.get("Retry-After")) or _REPORT_RETRY
        )
    if response.status_code == 204:
        return ReportOutcome({}, cycle)
    response.raise_for_status()
    statuses: dict[str, str] = {}
    for item in (response.json() or {}).get("accounts") or []:
        if isinstance(item, dict) and item.get("status") in ("closed", "updated"):
            statuses[str(item.get("accountId") or "")] = item["status"]
    return ReportOutcome(statuses, cycle)


def redact_query(exc: Exception, query: str) -> str:
    """``str(exc)`` with the search text removed; requests' HTTPError embeds the request URL."""
    text = str(exc)
    for variant in (query, quote(query, safe=""), quote_plus(query)):
        text = text.replace(variant, "<query>")
    return text


def mention_markup(name: str, account_id: str) -> str:
    clean = re.sub(r"[\[\]\r\n]", "", name).strip() or UNKNOWN_USER_LABEL
    return f"@[{clean}]({account_id})"


def markup_mention_ids(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(2) for m in MENTION_MARKUP_RE.finditer(text)))


def display_markup(text: str, names: Mapping[str, str] | None = None) -> str:
    def replace(match: re.Match[str]) -> str:
        account_id = match.group(2)
        return "@" + (names[account_id] if names and account_id in names else match.group(1))

    return MENTION_MARKUP_RE.sub(replace, text)


class _StorageScan(HTMLParser):
    """Tokenise Confluence storage format, collecting every ``<ri:user>`` start tag (with its
    absolute span) and the ``<ac:link>`` element spans that wrap one. A real tokenizer, so a ``>``
    inside a quoted attribute, either quote style and entity-encoded values all behave as they do
    for Confluence's own XML parser."""

    CDATA_CONTENT_ELEMENTS = ()  # type: ignore[misc]  # HTMLParser declares these Final; storage format has no CDATA elements
    RCDATA_CONTENT_ELEMENTS = ()  # type: ignore[misc]  # HTMLParser declares these Final; storage format has no CDATA elements

    def set_cdata_mode(self, *args: object, **kwargs: object) -> None:
        """Never enter raw-text mode. Some patch releases switch ``<plaintext>`` (and other
        elements) into it regardless of the class attributes above, which would hide any
        ``<ri:user>`` that follows; Confluence's XML parser has no such modes."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self._text = text
        self._line_starts = [0]
        for line in text.split("\n")[:-1]:
            self._line_starts.append(self._line_starts[-1] + len(line) + 1)
        # One entry per <ri:user> tag: every non-blank ri:account-id on it (a repeated attribute
        # lists all its values, so none escapes resolution), empty for legacy/id-less forms.
        self.users: list[list[str]] = []
        self.user_spans: list[tuple[int, int]] = []  # start tag (start, end), parallel to users
        # (start, end, index into users, end of the opening tag) per user link
        self.links: list[tuple[int, int, int, int]] = []
        self._open: tuple[int, int | None, int] | None = None
        self.feed(text)
        self.close()

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "ac:link":
            start = self._offset()
            self._open = (start, None, start + len(self.get_starttag_text() or ""))
        elif tag == "ri:user":
            ids = [(v or "").strip() for k, v in attrs if k == "ri:account-id"]
            self.users.append([i for i in ids if i])
            start = self._offset()
            self.user_spans.append((start, start + len(self.get_starttag_text() or "")))
            if self._open is not None and self._open[1] is None:
                self._open = (self._open[0], len(self.users) - 1, self._open[2])

    def handle_endtag(self, tag: str) -> None:
        if tag == "ac:link" and self._open is not None:
            start, user, body_start = self._open
            end = self._text.find(">", self._offset()) + 1
            if end and user is not None:
                self.links.append((start, end, user, body_start))
            self._open = None


def storage_mention_ids(html: str) -> list[str]:
    users = _StorageScan(html).users
    return list(dict.fromkeys(i for ids in users for i in ids))


def storage_unrecognised_user_mentions(html: str) -> int:
    """Count ``<ri:user>`` tags with no usable ``ri:account-id`` (legacy ``ri:userkey`` /
    ``ri:username`` forms, or none at all), which cannot be named from the directory."""
    return sum(1 for ids in _StorageScan(html).users if not ids)


_USER_END_TAG_RE = re.compile(r"</ri:user\s*>")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_PLAIN_LINK_BODY_RE = re.compile(
    r"<ac:plain-text-link-body\s*>\s*<!\[CDATA\[(.*?)\]\]>\s*</ac:plain-text-link-body\s*>",
    re.DOTALL,
)
_LINK_BODY_OPEN_RE = re.compile(r"<ac:link-body\s*>")
_LINK_BODY_CLOSE_RE = re.compile(r"</ac:link-body\s*>")


def _visible_link_text(fragment: str) -> str:
    """Make the link text of a user link visible to ``html_to_markdown`` (and so to the approval
    card and the PII scan): ``<ac:link-body>`` markup is kept, wrapped as ``(link text: ...)``, and
    the CDATA text of ``<ac:plain-text-link-body>`` (which an HTML tokenizer would not surface) is
    escaped into ordinary text."""
    fragment = _PLAIN_LINK_BODY_RE.sub(
        lambda m: f" (link text: {html_lib.escape(m.group(1))})", fragment
    )
    fragment = _LINK_BODY_OPEN_RE.sub(" (link text: ", fragment)
    fragment = _LINK_BODY_CLOSE_RE.sub(")", fragment)
    return _CDATA_RE.sub(lambda m: html_lib.escape(m.group(1)), fragment)


def _link_close_start(html: str, end: int) -> int:
    """Start of the ``</ac:link>`` closing tag ending at ``end``."""
    return html.rfind("<", 0, end)


def storage_mentions_to_text(html: str, names: Mapping[str, str]) -> str:
    """Replace each user link's ``<ri:user>`` tag with ``@Name``, keeping everything else the link
    holds so that all of its text is still shown and scanned."""
    scan = _StorageScan(html)
    out: list[str] = []
    cursor = 0
    for start, end, index, body_start in scan.links:
        ids = scan.users[index]
        # A tag naming several accounts is ambiguous, so it reads as an unknown user.
        name = (names.get(ids[0]) if len(ids) == 1 else None) or UNKNOWN_USER_LABEL
        user_start, user_end = scan.user_spans[index]
        closing = _USER_END_TAG_RE.match(html, user_end)
        if closing is not None and closing.end() <= end:
            user_end = closing.end()
        out.append(html[cursor:start])
        out.append(_visible_link_text(html[body_start:user_start]))
        out.append("@" + html_lib.escape(name))
        out.append(_visible_link_text(html[user_end:_link_close_start(html, end)]))
        cursor = end
    out.append(html[cursor:])
    return "".join(out)


def markup_to_storage(text: str) -> str:
    return MENTION_MARKUP_RE.sub(
        lambda m: f'<ac:link><ri:user ri:account-id="{m.group(2)}" /></ac:link>', text
    )


FetchFn = Callable[[list[str]], list[AtlassianUser]]
ReportFn = Callable[[list[tuple[str, datetime]]], ReportOutcome]


def _overdue(reference: datetime, cycle: timedelta, now: datetime) -> bool:
    return now - reference > cycle + REPORT_GRACE


def erase_overdue_cache_file(path: str | os.PathLike[str], now: datetime | None = None) -> int:
    """Drop every entry of one cache file neither retrieved nor reported within its cycle and a day;
    return how many were erased. Deletes the file when nothing is left, or when it cannot be read,
    since an unreadable file cannot be reported either. Never raises."""
    now = now or datetime.now(timezone.utc)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        users = data.get("users")
        if not isinstance(users, dict):
            raise TypeError("'users' is not an object")
    except FileNotFoundError:
        return 0
    except Exception as exc:
        logger.warning("Erasing unreadable Atlassian user cache %s: %s", path, exc)
        with contextlib.suppress(OSError):
            os.remove(path)
        return 0
    cycle = _clamp_cycle(_parse_seconds(data.get("report_cycle_seconds")) or DEFAULT_REPORT_CYCLE)
    kept = {}
    for account_id, raw in users.items():
        try:
            stamps = [datetime.fromisoformat(raw["fetched_at"])]
            if raw.get("reported_at"):
                stamps.append(datetime.fromisoformat(raw["reported_at"]))
            reference = max(t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in stamps)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not _overdue(reference, cycle, now):
            kept[account_id] = raw
    erased = len(users) - len(kept)
    if not erased:
        return 0
    try:
        if kept:
            atomic_write_json(str(path), {**data, "users": kept})
        else:
            os.remove(path)
    except OSError as exc:
        logger.warning("Could not erase overdue Atlassian user cache entries in %s: %s", path, exc)
        return 0
    logger.info("Erased %d unreported Atlassian account(s) from %s", erased, path)
    return erased


def erase_overdue_caches(root: str | os.PathLike[str]) -> int:
    """``erase_overdue_cache_file`` for the cache under ``root`` and every principal's under
    ``root/users/*``, including ones whose connectors are not running."""
    base = Path(root)
    candidates = [base / CACHE_FILENAME, *sorted((base / "users").glob(f"*/{CACHE_FILENAME}"))]
    return sum(erase_overdue_cache_file(p) for p in candidates)


class AtlassianUserDirectory:
    """Account id -> name cache.

    With ``background_reports`` on, ``report_soon`` reports the cached accounts that are due on a
    background thread; without it (the default, and every test that builds a directory) it does
    nothing, so no report is ever sent that the caller did not ask for."""

    def __init__(self, cache_file: str = "", cloud_id: str = "", background_reports: bool = False) -> None:
        self._cache_file = cache_file
        self._cloud_id = cloud_id
        self._background_reports = background_reports
        self._users: dict[str, AtlassianUser] = {}
        self._fetched_at: dict[str, datetime] = {}
        self._reported_at: dict[str, datetime] = {}
        self._report_cycle = DEFAULT_REPORT_CYCLE
        self._next_report_attempt: datetime | None = None
        self._negative: dict[str, datetime] = {}
        self._last_failure: datetime | None = None
        self._loaded = False
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._report_lock = threading.Lock()

    def resolve(self, account_ids: Iterable[str], fetch: FetchFn) -> dict[str, str]:
        """Map account ids to display names; never raises."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prepare_locked(now)
            wanted = list(dict.fromkeys(i for i in account_ids if ACCOUNT_ID_RE.fullmatch(i)))
            to_fetch = [i for i in wanted if self._needs_fetch(i, now)]
            cooling_down = (
                self._last_failure is not None and now - self._last_failure < _FETCH_FAILURE_COOLDOWN
            )
            if cooling_down:
                to_fetch = []

        fetched: list[AtlassianUser] = []
        missing: list[str] = []
        failed = False
        for start in range(0, len(to_fetch), BULK_BATCH_SIZE):
            chunk = to_fetch[start:start + BULK_BATCH_SIZE]
            try:
                returned = fetch(chunk)
            except Exception as exc:
                logger.warning(
                    "Could not resolve %d Atlassian account id(s) (non-fatal): %s", len(chunk), exc
                )
                failed = True
                break
            # A blank display name is not a usable name: treat it like a missing id (negative-cached,
            # refused on writes) rather than caching it as resolved.
            named = [u for u in returned if u.display_name.strip()]
            got = {u.account_id for u in named}
            fetched.extend(named)
            missing.extend(i for i in chunk if i not in got)

        with self._lock:
            stamp = datetime.now(timezone.utc)
            if failed:
                self._last_failure = stamp
            for user in fetched:
                self._users[user.account_id] = user
                self._fetched_at[user.account_id] = stamp
                self._negative.pop(user.account_id, None)
            for account_id in missing:
                self._negative[account_id] = stamp
            if fetched:
                self._save_locked()
            return {i: self._users[i].display_name for i in wanted if i in self._users}

    def remember(self, users: Iterable[AtlassianUser]) -> None:
        with self._lock:
            now = datetime.now(timezone.utc)
            self._prepare_locked(now)
            for user in users:
                if not user.display_name.strip():
                    continue
                self._users[user.account_id] = user
                self._fetched_at[user.account_id] = now
                self._negative.pop(user.account_id, None)
            self._save_locked()

    def refresh(self, fetch: FetchFn) -> int:
        """Re-fetch every cached id; return how many users are cached afterwards."""
        if not self._refresh_lock.acquire(blocking=False):
            raise AtlassianUsersError("refresh already in progress")
        try:
            with self._lock:
                self._prepare_locked(datetime.now(timezone.utc))
                ids = list(self._users)
            if not ids:
                return 0
            fresh: list[AtlassianUser] = []
            try:
                for start in range(0, len(ids), BULK_BATCH_SIZE):
                    fresh.extend(fetch(ids[start:start + BULK_BATCH_SIZE]))
            except Exception as exc:
                raise AtlassianUsersError(f"refresh failed: {exc}") from exc
            with self._lock:
                now = datetime.now(timezone.utc)
                fresh = [u for u in fresh if u.display_name.strip()]
                self._users = {u.account_id: u for u in fresh}
                self._fetched_at = {u.account_id: now for u in fresh}
                self._reported_at = {
                    i: t for i, t in self._reported_at.items() if i in self._users
                }
                for account_id in ids:
                    if account_id not in self._users:
                        self._negative[account_id] = now
                self._last_failure = None
                self._save_locked()
                return len(self._users)
        finally:
            self._refresh_lock.release()

    def report_soon(self, report: ReportFn) -> None:
        """Start ``report_due`` on a background thread if accounts are due and no attempt is
        waiting out a retry delay. Does nothing unless the directory was built with
        ``background_reports``; never raises."""
        if not self._background_reports:
            return
        with self._lock:
            now = datetime.now(timezone.utc)
            self._prepare_locked(now)
            if self._next_report_attempt is not None and now < self._next_report_attempt:
                return
            if not any(self._report_due_locked(i, now) for i in self._users):
                return
            # Claimed before the thread starts, so a burst of lookups starts one thread.
            self._next_report_attempt = now + _REPORT_RETRY
        threading.Thread(
            target=self._report_quietly, args=(report,), name="atlassian-personal-data-report",
            daemon=True,
        ).start()

    def _report_quietly(self, report: ReportFn) -> None:
        try:
            erased = self.report_due(report)
        except AtlassianUsersError as exc:
            logger.warning("Atlassian personal data report not sent (will retry): %s", exc)
            return
        if erased:
            logger.info("Erased %d Atlassian account(s) Atlassian reported closed or updated", erased)

    def report_due(self, report: ReportFn) -> int:
        """Report every cached account not reported within the current cycle, in batches of
        ``REPORT_BATCH_SIZE``, and erase each one Atlassian answers "closed" or "updated" for.

        Returns how many accounts were erased. Each batch's outcome is saved before the next is
        sent, so an interrupted run resumes where it stopped. Raises ``AtlassianUsersError`` when a
        batch fails (``AtlassianReportDeferred`` on a 429), after which no report is attempted
        until the retry delay has passed."""
        if not self._report_lock.acquire(blocking=False):
            return 0
        try:
            with self._lock:
                now = datetime.now(timezone.utc)
                self._prepare_locked(now)
                due = [
                    (i, self._fetched_at[i]) for i in self._users if self._report_due_locked(i, now)
                ]
            erased = 0
            for start in range(0, len(due), REPORT_BATCH_SIZE):
                batch = due[start:start + REPORT_BATCH_SIZE]
                try:
                    outcome = report(batch)
                except AtlassianReportDeferred as exc:
                    self._defer_reports(exc.retry_after)
                    raise
                except Exception as exc:
                    self._defer_reports(_REPORT_RETRY)
                    raise AtlassianUsersError(f"personal data report failed: {exc}") from exc
                with self._lock:
                    stamp = datetime.now(timezone.utc)
                    if outcome.cycle is not None:
                        self._report_cycle = _clamp_cycle(outcome.cycle)
                    for account_id, _ in batch:
                        if outcome.statuses.get(account_id):
                            self._erase_locked(account_id)
                            self._negative[account_id] = stamp
                            erased += 1
                        elif account_id in self._users:
                            self._reported_at[account_id] = stamp
                    self._save_locked()
            self._defer_reports(_REPORT_RETRY)
            return erased
        finally:
            self._report_lock.release()

    def _defer_reports(self, delay: timedelta) -> None:
        with self._lock:
            self._next_report_attempt = datetime.now(timezone.utc) + delay

    def _report_due_locked(self, account_id: str, now: datetime) -> bool:
        if account_id == "unknown" or not _REPORTABLE_ID_RE.fullmatch(account_id):
            return False
        reported_at = self._reported_at.get(account_id)
        return reported_at is None or now - reported_at >= self._report_cycle

    def _last_seen_locked(self, account_id: str) -> datetime:
        """When this entry was last retrieved from or reported to Atlassian."""
        fetched_at = self._fetched_at[account_id]
        reported_at = self._reported_at.get(account_id)
        return max(fetched_at, reported_at) if reported_at is not None else fetched_at

    def _erase_locked(self, account_id: str) -> None:
        self._users.pop(account_id, None)
        self._fetched_at.pop(account_id, None)
        self._reported_at.pop(account_id, None)

    def _prepare_locked(self, now: datetime) -> None:
        """Load the cache file once, then erase every entry neither retrieved nor reported within
        the cycle and a day."""
        self._load_locked()
        overdue = [
            i for i in self._users
            if _overdue(self._last_seen_locked(i), self._report_cycle, now)
        ]
        for account_id in overdue:
            self._erase_locked(account_id)
        if overdue:
            self._save_locked()

    def _needs_fetch(self, account_id: str, now: datetime) -> bool:
        fetched_at = self._fetched_at.get(account_id)
        if fetched_at is not None and now - fetched_at < USER_CACHE_TTL:
            return False
        negative_at = self._negative.get(account_id)
        return negative_at is None or now - negative_at >= _NEGATIVE_LOOKUP_TTL

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._cache_file or not os.path.exists(self._cache_file):
            return
        try:
            with open(self._cache_file, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("cloud_id") != self._cloud_id:
                return
            cycle = _parse_seconds(data.get("report_cycle_seconds"))
            if cycle is not None:
                self._report_cycle = _clamp_cycle(cycle)
            raw_users = data.get("users") or {}
            if not isinstance(raw_users, dict):
                raise TypeError("'users' is not an object")
        except Exception as exc:
            logger.warning("Could not load Atlassian user cache (non-fatal): %s", exc)
            return
        for account_id, raw in raw_users.items():
            try:
                if not isinstance(account_id, str):
                    continue
                fetched_at = datetime.fromisoformat(raw["fetched_at"])
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                user = AtlassianUser(
                    account_id=raw["account_id"],
                    display_name=raw["display_name"],
                    active=raw.get("active", True),
                    account_type=raw.get("account_type", ""),
                )
                if not user.display_name.strip():
                    continue
                reported_at = None
                if raw.get("reported_at"):
                    reported_at = datetime.fromisoformat(raw["reported_at"])
                    if reported_at.tzinfo is None:
                        reported_at = reported_at.replace(tzinfo=timezone.utc)
                self._users.setdefault(account_id, user)
                self._fetched_at.setdefault(account_id, fetched_at)
                if reported_at is not None:
                    self._reported_at.setdefault(account_id, reported_at)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue

    def _save_locked(self) -> None:
        if not self._cache_file:
            return
        payload = {
            "cloud_id": self._cloud_id,
            "report_cycle_seconds": int(self._report_cycle.total_seconds()),
            "users": {
                account_id: {
                    "account_id": u.account_id,
                    "display_name": u.display_name,
                    "active": u.active,
                    "account_type": u.account_type,
                    "fetched_at": self._fetched_at[account_id].isoformat(),
                    **(
                        {"reported_at": self._reported_at[account_id].isoformat()}
                        if account_id in self._reported_at else {}
                    ),
                }
                for account_id, u in self._users.items()
            },
        }
        try:
            atomic_write_json(self._cache_file, payload)
        except OSError as exc:
            logger.warning("Could not save Atlassian user cache (non-fatal): %s", exc)
