"""Account-id -> display-name directory shared by the Jira and Confluence clients.

Names are looked up lazily, only for the ids that actually appear in a read, through Jira's user
API (ADR 0116), and remembered in a small cache that can be persisted to disk. This module also
holds the ``@[Name](accountId)`` mention markup helpers used on reads and writes, and the
storage-format helpers Confluence needs.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import threading
from html.parser import HTMLParser
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
# Deliberately loose: a false positive only relabels a name, a false negative leaks an address.
_EMAIL_RE = re.compile(r"[\w.+%'-]+@[\w-]+(?:\.[\w-]+)+")


class AtlassianUsersError(Exception):
    """Raised when a user lookup against Atlassian cannot complete."""


@dataclass
class AtlassianUser:
    account_id: str
    display_name: str
    active: bool = True
    account_type: str = ""  # Atlassian's accountType: "atlassian", "app" or "customer"

    def __post_init__(self) -> None:
        # Every record (API response, cache file, remember()) is built here, so an email-shaped
        # name, typical of Jira Service Management customer accounts, can reach no output (ADR 0118).
        self.display_name = mask_emails(self.display_name, self.account_id)


def mask_emails(name: str, account_id: str) -> str:
    """Replace any email-shaped text with ``Customer account <last 4 of the id>``."""
    label = f"{CUSTOMER_ACCOUNT_LABEL} {account_id[-4:]}"
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
    response = session.get(
        f"{jira_api_base(cloud_id)}/rest/api/3/user/search",
        params={"query": query, "maxResults": max_results},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return [u for u in (parse_user(v) for v in response.json()) if u is not None]


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
    """Tokenise Confluence storage format, collecting every ``<ri:user>`` start tag and the
    ``<ac:link>`` element spans (absolute offsets) that wrap one. A real tokenizer, so a ``>``
    inside a quoted attribute, either quote style and entity-encoded values all behave as they do
    for Confluence's own XML parser."""

    CDATA_CONTENT_ELEMENTS = ()  # <script>/<style> are ordinary elements in storage format
    RCDATA_CONTENT_ELEMENTS = ()

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self._text = text
        self._line_starts = [0]
        for line in text.split("\n")[:-1]:
            self._line_starts.append(self._line_starts[-1] + len(line) + 1)
        self.users: list[dict[str, str]] = []
        self.links: list[tuple[int, int, int | None]] = []  # (start, end, index into users)
        self._open: tuple[int, int | None] | None = None
        self.feed(text)
        self.close()

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "ac:link":
            self._open = (self._offset(), None)
        elif tag == "ri:user":
            values: dict[str, str] = {}
            for key, value in attrs:
                values.setdefault(key, (value or "").strip())
            self.users.append(values)
            if self._open is not None and self._open[1] is None:
                self._open = (self._open[0], len(self.users) - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag == "ac:link" and self._open is not None:
            start, user = self._open
            end = self._text.find(">", self._offset()) + 1
            if end and user is not None:
                self.links.append((start, end, user))
            self._open = None


def storage_mention_ids(html: str) -> list[str]:
    users = _StorageScan(html).users
    return list(dict.fromkeys(u["ri:account-id"] for u in users if u.get("ri:account-id")))


def storage_unrecognised_user_mentions(html: str) -> int:
    """Count ``<ri:user>`` tags with no usable ``ri:account-id`` (legacy ``ri:userkey`` /
    ``ri:username`` forms, or none at all), which cannot be named from the directory."""
    return sum(1 for u in _StorageScan(html).users if not u.get("ri:account-id"))


def storage_mentions_to_text(html: str, names: Mapping[str, str]) -> str:
    scan = _StorageScan(html)
    out: list[str] = []
    cursor = 0
    for start, end, index in scan.links:
        name = names.get(scan.users[index].get("ri:account-id", "")) or UNKNOWN_USER_LABEL
        out.append(html[cursor:start])
        out.append("@" + html_lib.escape(name))
        cursor = end
    out.append(html[cursor:])
    return "".join(out)


def markup_to_storage(text: str) -> str:
    return MENTION_MARKUP_RE.sub(
        lambda m: f'<ac:link><ri:user ri:account-id="{m.group(2)}" /></ac:link>', text
    )


FetchFn = Callable[[list[str]], list[AtlassianUser]]


class AtlassianUserDirectory:
    def __init__(self, cache_file: str = "", cloud_id: str = "") -> None:
        self._cache_file = cache_file
        self._cloud_id = cloud_id
        self._users: dict[str, AtlassianUser] = {}
        self._fetched_at: dict[str, datetime] = {}
        self._negative: dict[str, datetime] = {}
        self._last_failure: datetime | None = None
        self._loaded = False
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def resolve(self, account_ids: Iterable[str], fetch: FetchFn) -> dict[str, str]:
        """Map account ids to display names; never raises."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._load_locked()
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
            got = {u.account_id for u in returned}
            fetched.extend(returned)
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
            self._load_locked()
            now = datetime.now(timezone.utc)
            for user in users:
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
                self._load_locked()
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
                self._users = {u.account_id: u for u in fresh}
                self._fetched_at = {u.account_id: now for u in fresh}
                for account_id in ids:
                    if account_id not in self._users:
                        self._negative[account_id] = now
                self._last_failure = None
                self._save_locked()
                return len(self._users)
        finally:
            self._refresh_lock.release()

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
            raw_users = data.get("users") or {}
        except Exception as exc:
            logger.warning("Could not load Atlassian user cache (non-fatal): %s", exc)
            return
        for account_id, raw in raw_users.items():
            try:
                fetched_at = datetime.fromisoformat(raw["fetched_at"])
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                self._users.setdefault(account_id, AtlassianUser(
                    account_id=raw["account_id"],
                    display_name=raw["display_name"],
                    active=raw.get("active", True),
                    account_type=raw.get("account_type", ""),
                ))
                self._fetched_at.setdefault(account_id, fetched_at)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue

    def _save_locked(self) -> None:
        if not self._cache_file:
            return
        payload = {
            "cloud_id": self._cloud_id,
            "users": {
                account_id: {
                    "account_id": u.account_id,
                    "display_name": u.display_name,
                    "active": u.active,
                    "account_type": u.account_type,
                    "fetched_at": self._fetched_at[account_id].isoformat(),
                }
                for account_id, u in self._users.items()
            },
        }
        try:
            atomic_write_json(self._cache_file, payload)
        except OSError as exc:
            logger.warning("Could not save Atlassian user cache (non-fatal): %s", exc)
