"""What a Drive folder listing has shown under a folder that has a folder-tree rule.

``ListingGrants`` remembers, per scoped token (or ``""`` when unscoped) and per rule folder, the
subfolder ids that listings returned, each for one hour and never past the grant of the folder that
was listed to reach it. It lives in memory only: nothing is written to disk and a restart forgets it.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable

FOLDER_MIME = "application/vnd.google-apps.folder"
GRANT_TTL_SECONDS = 3600
MAX_GRANTED_PER_ROOT = 2000
_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,100}")


class ListingGrants:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: dict[tuple[str, str], dict[str, float]] = {}

    def _live(self, key: str, root: str, now: float) -> dict[str, float]:
        entries = self._grants.get((key, root), {})
        for folder_id in [f for f, expiry in entries.items() if expiry <= now]:
            del entries[folder_id]
        if not entries:
            self._grants.pop((key, root), None)
        return entries

    def note_listing(self, key: str, roots: Iterable[str], listed_folder_id: str,
                     child_folder_ids: Iterable[str]) -> None:
        children = list(child_folder_ids)
        with self._lock:
            now = self._clock()
            for root in roots:
                entries = self._live(key, root, now)
                if listed_folder_id == root:
                    ceiling = now + GRANT_TTL_SECONDS
                elif listed_folder_id in entries:
                    ceiling = entries[listed_folder_id]
                else:
                    continue
                for child in children:
                    if not _ID_RE.fullmatch(child):
                        continue
                    expiry = min(now + GRANT_TTL_SECONDS, ceiling)
                    if child in entries:
                        entries[child] = min(max(entries[child], expiry), ceiling)
                    elif len(entries) < MAX_GRANTED_PER_ROOT:
                        entries[child] = expiry
                self._grants[(key, root)] = entries

    def granted(self, key: str, roots: Iterable[str]) -> dict[str, list[str]]:
        with self._lock:
            now = self._clock()
            result: dict[str, list[str]] = {}
            for root in roots:
                entries = self._live(key, root, now)
                if entries:
                    result[root] = sorted(entries)
            return result
