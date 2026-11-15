"""Group membership store, resolvers and approvals for upstream audiences.

An upstream's audiences come from Workspace groups: the operator maps a group
(``mgmt@company``) to audiences in ``admin_config.json``, an n8n job sends the
current membership through the ``privacyfence_update_group_membership`` tool,
and :class:`MembershipStore` keeps it in ``group_membership.json`` under the
organization data directory.

The trust model (plan 3.7): removals apply at once; an addition to a group that
grants only the unrestricted audience (``all``) applies at once; an addition to a
group that grants any other audience *waits* for an admin's approval, bound to
the exact (email, group, audience) triple. A snapshot that would add more people
than ``max_additions`` within 24 hours is rejected whole. Pending approvals are
derived from the stored members and the current map, never stored.

Two resolvers answer "which audiences does this verified email hold":
:class:`SnapshotResolver` over the store, and :class:`StaticFileResolver` over a
hand-written file (bootstrap and tests, no approvals).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from . import paths, secure_files

logger = logging.getLogger(__name__)

UNRESTRICTED_AUDIENCE = "all"
FILENAME = "group_membership.json"
MAX_GROUPS = 500
MAX_ENTRIES = 20000
MAX_PENDING = 500
WINDOW_SECONDS = 86400
DEFAULT_MAX_ADDITIONS = 20
MAX_HISTORY = 50

# A group name is email-shaped and lower case; an audience name is a short slug. Both are
# public: the ``admin_config.json`` parser reuses them.
GROUP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9.-]{1,253}")
AUDIENCE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


@dataclass(frozen=True)
class Pending:
    """People waiting for approval to hold ``audience`` through ``group``."""

    group: str
    audience: str
    emails: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotResult:
    status: Literal["applied", "rejected"]
    added: int
    removed: int
    parked: int
    ignored_groups: tuple[str, ...]
    reason: str


class MembershipResolver(Protocol):
    def audiences_for(self, email: str, group_audiences: Mapping[str, frozenset[str]]) -> frozenset[str]:
        """The audiences ``email`` holds; ``frozenset()`` for an empty or unverified email."""
        ...


def normalize_email(value: Any) -> str | None:
    """Lower-cased ``value`` when it is an email-shaped string (exactly one ``@``, no whitespace)."""
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or email.count("@") != 1 or any(c.isspace() for c in email):
        return None
    local, _, domain = email.partition("@")
    if not local or not domain:
        return None
    return email


def _normalized_map(group_audiences: Mapping[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    return {str(g).lower(): frozenset(a) for g, a in group_audiences.items()}


def _rejected(reason: str, ignored: tuple[str, ...] = ()) -> SnapshotResult:
    return SnapshotResult("rejected", 0, 0, 0, ignored, reason)


class MembershipStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._state = self._empty()
        self._sig: tuple[int, int, int] | None = None
        self._refresh()

    # ------------------------------------------------------------------ state

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1,
            "members": {},
            "approvals": [],
            "declined": [],
            "history": [],
            "updated_at": None,
            "last_rejected": None,
        }

    def _signature(self) -> tuple[int, int, int] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _refresh(self) -> None:
        """Re-read the file when it changed (another process, e.g. a CLI, may have written it)."""
        sig = self._signature()
        if sig == self._sig:
            return
        if sig is None:
            self._state, self._sig = self._empty(), None
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            state = self._validated(raw)
        except (OSError, ValueError) as exc:
            logger.warning("Group membership file %s is unreadable; keeping the last good state: %s", self._path, exc)
            self._sig = sig
            return
        self._state, self._sig = state, sig

    def _validated(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        state = self._empty()
        members = raw.get("members", {})
        if not isinstance(members, dict):
            raise ValueError("members is not an object")
        for group, emails in members.items():
            if not isinstance(group, str) or not isinstance(emails, list) or not all(isinstance(e, str) for e in emails):
                raise ValueError("members is malformed")
            state["members"][group] = sorted(set(emails))
        for key in ("approvals", "declined"):
            entries = raw.get(key, [])
            if not isinstance(entries, list):
                raise ValueError(f"{key} is not a list")
            triples: list[list[str]] = []
            for entry in entries:
                if not (isinstance(entry, list) and len(entry) == 3 and all(isinstance(p, str) for p in entry)):
                    raise ValueError(f"{key} is malformed")
                triples.append(list(entry))
            state[key] = triples
        history = raw.get("history", [])
        if not isinstance(history, list) or not all(isinstance(h, dict) for h in history):
            raise ValueError("history is malformed")
        state["history"] = history
        updated_at = raw.get("updated_at")
        state["updated_at"] = float(updated_at) if isinstance(updated_at, (int, float)) else None
        last_rejected = raw.get("last_rejected")
        state["last_rejected"] = last_rejected if isinstance(last_rejected, dict) else None
        return state

    def _write(self, state: dict[str, Any]) -> None:
        secure_files.atomic_write_json(self._path, state, indent=2, sort_keys=True)
        self._state = state
        self._sig = self._signature()

    # ------------------------------------------------------------------ reads

    @property
    def updated_at(self) -> float | None:
        with self._lock:
            self._refresh()
            return self._state["updated_at"]

    def last_rejected(self) -> dict[str, Any] | None:
        with self._lock:
            self._refresh()
            last = self._state["last_rejected"]
            return dict(last) if last else None

    def members_of(self, email: str) -> frozenset[str]:
        email = (email or "").strip().lower()
        if not email:
            return frozenset()
        with self._lock:
            self._refresh()
            return frozenset(g for g, emails in self._state["members"].items() if email in emails)

    def has_approval(self, email: str, group: str, audience: str) -> bool:
        with self._lock:
            self._refresh()
            return [email, group, audience] in self._state["approvals"]

    def pending(self, group_audiences: Mapping[str, frozenset[str]]) -> list[Pending]:
        with self._lock:
            self._refresh()
            return self._pending(self._state, _normalized_map(group_audiences))

    @staticmethod
    def _pending(state: Mapping[str, Any], mapping: Mapping[str, frozenset[str]]) -> list[Pending]:
        approved = {tuple(t) for t in state["approvals"]}
        declined = {tuple(t) for t in state["declined"]}
        out: list[Pending] = []
        for group in sorted(mapping):
            emails = state["members"].get(group, [])
            for audience in sorted(mapping[group]):
                if audience == UNRESTRICTED_AUDIENCE:
                    continue
                waiting = tuple(
                    e for e in sorted(emails) if (e, group, audience) not in approved and (e, group, audience) not in declined
                )
                if waiting:
                    out.append(Pending(group, audience, waiting))
        return out

    # ------------------------------------------------------------------ snapshot

    def apply_snapshot(
        self,
        groups: Any,
        *,
        group_audiences: Mapping[str, frozenset[str]],
        now: float,
        max_additions: int,
    ) -> SnapshotResult:
        mapping = _normalized_map(group_audiences)
        with self._lock:
            self._refresh()
            result, new_state = self._apply(groups, mapping, now, max_additions)
            if new_state is not None:
                self._write(new_state)
                return result
            self._record_rejection(result.reason, now)
            return result

    def _record_rejection(self, reason: str, now: float) -> None:
        state = json.loads(json.dumps(self._state))
        state["last_rejected"] = {"reason": reason, "at": now}
        try:
            self._write(state)
        except OSError as exc:
            logger.warning("Could not record the rejected group membership snapshot: %s", exc)
            self._state = state

    def _apply(
        self, groups: Any, mapping: Mapping[str, frozenset[str]], now: float, max_additions: int
    ) -> tuple[SnapshotResult, dict[str, Any] | None]:
        # 1. Validate and normalise.
        if not isinstance(groups, Mapping) or len(groups) > MAX_GROUPS:
            return _rejected("invalid_snapshot"), None
        incoming: dict[str, set[str]] = {}
        entries = 0
        for name, emails in groups.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(emails, (list, tuple)):
                return _rejected("invalid_snapshot"), None
            group = name.strip().lower()
            bucket = incoming.setdefault(group, set())
            for raw in emails:
                email = normalize_email(raw)
                if email is None:
                    return _rejected("invalid_snapshot"), None
                bucket.add(email)
                entries += 1
        if len(incoming) > MAX_GROUPS or entries > MAX_ENTRIES:
            return _rejected("invalid_snapshot"), None

        # 2. Only groups some upstream maps are kept.
        ignored = tuple(sorted(g for g in incoming if g not in mapping))
        stored: dict[str, set[str]] = {g: set(e) for g, e in self._state["members"].items()}
        new_members: dict[str, set[str]] = {}
        added_by_group: dict[str, set[str]] = {}
        removed_by_group: dict[str, set[str]] = {}
        for group in sorted(set(mapping) | set(stored)):
            before = stored.get(group, set())
            after = incoming.get(group, set()) if group in mapping else set()
            # 3. A group missing from the snapshot (or no longer mapped) loses every member.
            if after:
                new_members[group] = after
            if after - before:
                added_by_group[group] = after - before
            if before - after:
                removed_by_group[group] = before - after
        added = sum(len(v) for v in added_by_group.values())
        removed = sum(len(v) for v in removed_by_group.values())

        # 4. The addition cap (the first snapshot ever is the bootstrap and exempt).
        bootstrap = self._state["updated_at"] is None
        if not bootstrap:
            recent = sum(
                int(h.get("added", 0))
                for h in self._state["history"]
                if not h.get("bootstrap") and float(h.get("at", 0)) > now - WINDOW_SECONDS
            )
            if added + recent > max_additions:
                return _rejected("too_many_additions", ignored), None

        # 5. Removals drop approvals and declines; the rest is stored.
        def keep(triple: list[str]) -> bool:
            email, group, _audience = triple
            return email not in removed_by_group.get(group, set())

        state = json.loads(json.dumps(self._state))
        state["members"] = {g: sorted(e) for g, e in new_members.items()}
        state["approvals"] = [t for t in state["approvals"] if keep(t)]
        state["declined"] = [t for t in state["declined"] if keep(t)]
        pending = self._pending(state, mapping)
        if sum(len(p.emails) for p in pending) > MAX_PENDING:
            return _rejected("too_many_pending", ignored), None
        parked = sum(
            1
            for group, emails in added_by_group.items()
            for _email in emails
            for audience in mapping.get(group, frozenset())
            if audience != UNRESTRICTED_AUDIENCE
        )
        state["history"] = (
            state["history"] + [{"at": now, "added": added, "removed": removed, "bootstrap": bootstrap}]
        )[-MAX_HISTORY:]
        state["updated_at"] = now
        return SnapshotResult("applied", added, removed, parked, ignored, ""), state

    # ------------------------------------------------------------------ approvals

    def approve(
        self,
        group: str,
        audience: str,
        email: str | None = None,
        *,
        group_audiences: Mapping[str, frozenset[str]],
        now: float,
    ) -> int:
        """Approve ``email`` (or everyone pending) for ``audience`` through ``group``; returns the count changed."""
        return self._decide("approvals", "declined", group, audience, email, group_audiences, now)

    def decline(
        self,
        group: str,
        audience: str,
        email: str | None = None,
        *,
        group_audiences: Mapping[str, frozenset[str]],
        now: float,
    ) -> int:
        """Decline ``email`` (or everyone pending); a decline is forgotten when the person leaves the group."""
        return self._decide("declined", "approvals", group, audience, email, group_audiences, now)

    def _decide(
        self,
        add_key: str,
        drop_key: str,
        group: str,
        audience: str,
        email: str | None,
        group_audiences: Mapping[str, frozenset[str]],
        now: float,
    ) -> int:
        mapping = _normalized_map(group_audiences)
        group = (group or "").strip().lower()
        audience = (audience or "").strip().lower()
        if audience == UNRESTRICTED_AUDIENCE or audience not in mapping.get(group, frozenset()):
            return 0
        with self._lock:
            self._refresh()
            members = set(self._state["members"].get(group, []))
            if email is None:
                targets = {
                    e
                    for p in self._pending(self._state, mapping)
                    if p.group == group and p.audience == audience
                    for e in p.emails
                }
            else:
                normalized = normalize_email(email)
                targets = {normalized} if normalized and normalized in members else set()
            state = json.loads(json.dumps(self._state))
            changed = 0
            for target in sorted(targets):
                triple = [target, group, audience]
                touched = False
                if triple in state[drop_key]:
                    state[drop_key].remove(triple)
                    touched = True
                if triple not in state[add_key]:
                    state[add_key].append(triple)
                    touched = True
                changed += touched
            if changed:
                self._write(state)
            return changed


# ---------------------------------------------------------------------- resolvers


class SnapshotResolver:
    """Audiences from the store the sync tool writes: members only, restricted audiences once approved."""

    def __init__(self, store: MembershipStore) -> None:
        self._store = store

    def audiences_for(self, email: str, group_audiences: Mapping[str, frozenset[str]]) -> frozenset[str]:
        email = (email or "").strip().lower()
        if not email:
            return frozenset()
        mapping = _normalized_map(group_audiences)
        held: set[str] = set()
        for group in self._store.members_of(email):
            for audience in mapping.get(group, frozenset()):
                if audience == UNRESTRICTED_AUDIENCE or self._store.has_approval(email, group, audience):
                    held.add(audience)
        return frozenset(held)


class StaticFileResolver:
    """Audiences from ``{"version": 1, "groups": {"group": ["email", ...]}}``; no approvals."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._sig: tuple[int, int, int] | None = None
        self._groups: dict[str, frozenset[str]] = {}

    def _refresh(self) -> None:
        try:
            st = self._path.stat()
        except OSError:
            if self._sig is not None:
                logger.warning("Static group membership file %s is gone; keeping the last good data.", self._path)
            return
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return
        self._sig = sig
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            groups = raw["groups"]
            if not isinstance(groups, dict):
                raise ValueError("groups is not an object")
            parsed: dict[str, frozenset[str]] = {}
            for name, emails in groups.items():
                if not isinstance(name, str) or not isinstance(emails, list) or not all(isinstance(e, str) for e in emails):
                    raise ValueError("groups is malformed")
                parsed[name.lower()] = frozenset(e.strip().lower() for e in emails)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Static group membership file %s is malformed; keeping the last good data: %s", self._path, exc)
            return
        self._groups = parsed

    def audiences_for(self, email: str, group_audiences: Mapping[str, frozenset[str]]) -> frozenset[str]:
        email = (email or "").strip().lower()
        if not email:
            return frozenset()
        mapping = _normalized_map(group_audiences)
        with self._lock:
            self._refresh()
            groups = self._groups
        held: set[str] = set()
        for group, emails in groups.items():
            if email in emails:
                held |= mapping.get(group, frozenset())
        return frozenset(held)


# ---------------------------------------------------------------------- singleton

_STORE: MembershipStore | None = None


def get_store() -> MembershipStore:
    global _STORE
    if _STORE is None:
        _STORE = MembershipStore(paths.org_dir() / FILENAME)
    return _STORE


def current_store() -> MembershipStore | None:
    """The store set by ``set_store`` or built by ``get_store``, or ``None``; never creates one."""
    return _STORE


def set_store(store: MembershipStore | None) -> None:
    global _STORE
    _STORE = store


__all__ = [
    "AUDIENCE_RE",
    "DEFAULT_MAX_ADDITIONS",
    "FILENAME",
    "GROUP_NAME_RE",
    "MAX_ENTRIES",
    "MAX_GROUPS",
    "MAX_PENDING",
    "MembershipResolver",
    "MembershipStore",
    "Pending",
    "SnapshotResolver",
    "SnapshotResult",
    "StaticFileResolver",
    "UNRESTRICTED_AUDIENCE",
    "WINDOW_SECONDS",
    "current_store",
    "get_store",
    "normalize_email",
    "set_store",
]
