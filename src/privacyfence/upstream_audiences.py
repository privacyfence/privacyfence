"""The audiences an upstream MCP server reports about itself, as a pure table (ADR 0325).

An operator can declare ``audiences_tool`` on an upstream. PrivacyFence calls that tool on a schedule
and the upstream answers which addresses hold which audiences. This module holds the pure part: the
parser of one answer (``parse_result``), the immutable table it yields (``AudienceSnapshot``) and the
per-upstream holder of the latest table (``UpstreamAudienceSource``), which fails closed when the
table is older than three refresh intervals. It does no I/O, no logging and no auditing, and it never
quotes an address or a part of the answer in an error.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import group_membership

RESULT_VERSION = 1
MAX_AUDIENCES = 200
MAX_ENTRIES = group_membership.MAX_ENTRIES
MAX_ANSWER_CHARS = 1_000_000  # equal to upstream_mcp_client.MAX_RESULT_CHARS, which this module may not import
STALE_FACTOR = 3


class AudiencesResultError(Exception):
    """The answer is unusable. ``reason`` is one of a fixed set of strings and never quotes the answer."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AudienceSnapshot:
    by_email: Mapping[str, frozenset[str]]
    audience_count: int
    people_count: int
    dropped: int
    digest: str  # only to see that the table changed; never logged


class _RepeatedKey(ValueError):
    pass


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _RepeatedKey("repeated key")
    return dict(pairs)


def parse_result(structured: dict[str, Any] | None, text: str) -> AudienceSnapshot:
    """Turn one answer of the audiences tool into a snapshot, or raise ``AudiencesResultError``."""
    value: object
    if isinstance(structured, dict):
        if len(json.dumps(structured)) > MAX_ANSWER_CHARS:
            raise AudiencesResultError("the answer is larger than 1 MB")
        value = structured
    else:
        try:
            value = json.loads(text, object_pairs_hook=_json_pairs)
        except _RepeatedKey:
            raise AudiencesResultError("the answer repeats a key") from None
        except ValueError:
            raise AudiencesResultError("the answer is not a JSON object") from None
        if not isinstance(value, dict):
            raise AudiencesResultError("the answer is not a JSON object")
    if set(value) != {"version", "audiences"}:
        raise AudiencesResultError("the answer must hold exactly version and audiences")
    if type(value["version"]) is not int or value["version"] != RESULT_VERSION:
        raise AudiencesResultError("version must be 1")
    audiences = value["audiences"]
    if not isinstance(audiences, dict) or len(audiences) > MAX_AUDIENCES:
        raise AudiencesResultError("at most 200 audiences")
    entries = 0
    for name, members in audiences.items():
        if not isinstance(name, str) or not group_membership.AUDIENCE_RE.fullmatch(name):
            raise AudiencesResultError(
                "an audience name must be 1-63 characters: lowercase letters, digits, underscores or "
                "hyphens, starting with a letter or digit"
            )
        if not isinstance(members, list) or any(not isinstance(item, str) for item in members):
            raise AudiencesResultError("each audience must list addresses as strings")
        entries += len(members)
        if entries > MAX_ENTRIES:
            raise AudiencesResultError("at most 20000 entries")
    held: dict[str, set[str]] = {}
    dropped = 0
    for name, members in audiences.items():
        for item in members:
            address = group_membership.lookup_email(item)
            if address is None:
                dropped += 1
                continue
            held.setdefault(address, set()).add(name)
    pairs = sorted([name, address] for address, names in held.items() for name in names)
    digest = hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()
    return AudienceSnapshot(
        by_email={address: frozenset(names) for address, names in held.items()},
        audience_count=len(audiences),
        people_count=len(held),
        dropped=dropped,
        digest=digest,
    )


class UpstreamAudienceSource:
    """The latest snapshot of one upstream, safe to read from request handlers and worker threads."""

    def __init__(
        self,
        refresh_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._next_seq = 0
        self._last_seq = 0  # the highest seq applied or failed
        self._snapshot: AudienceSnapshot | None = None
        self._fetched_monotonic = 0.0
        self._fetched_wall: float | None = None
        self._last_error = ""

    def begin(self) -> int:
        with self._lock:
            self._next_seq += 1
            return self._next_seq

    def apply(self, snapshot: AudienceSnapshot, seq: int) -> bool:
        with self._lock:
            if seq < self._last_seq:
                return False
            self._last_seq = seq
            self._snapshot = snapshot
            self._fetched_monotonic = self._clock()
            self._fetched_wall = self._wall()
            self._last_error = ""
            return True

    def record_failure(self, kind: str, seq: int) -> bool:
        with self._lock:
            if seq < self._last_seq:
                return False
            self._last_seq = seq
            self._last_error = kind
            return True

    def _state(self) -> str:
        if self._snapshot is None:
            return "waiting"
        if self._clock() - self._fetched_monotonic > STALE_FACTOR * self._refresh_seconds:
            return "stale"
        return "ready"

    def state(self) -> str:
        with self._lock:
            return self._state()

    def audiences_for(self, address: str) -> frozenset[str]:
        with self._lock:
            if self._snapshot is None or self._state() != "ready":
                return frozenset()
            return self._snapshot.by_email.get(address, frozenset())

    def summary(self) -> dict[str, object]:
        with self._lock:
            snapshot = self._snapshot
            return {
                "state": self._state(),
                "fetched_at": self._fetched_wall,
                "audience_count": snapshot.audience_count if snapshot else 0,
                "people_count": snapshot.people_count if snapshot else 0,
                "dropped": snapshot.dropped if snapshot else 0,
                "last_error": self._last_error,
            }
