"""Name resolution for auto-accept rule values (see policy/resource_registry.py).

The Auto-accept Settings page shows a rule's value by ID (the ground truth the engine matches on)
but should *display* the resource's real name — a folder name, a task list name, a channel name —
not the opaque ID. This module resolves an ID to a name via the relevant connector's own read API
(each ``GrantResourceType`` in policy/resource_registry.py declares its own ``resolver``), and
caches the result so the page doesn't hit the network on every rebuild and still has something
to show immediately after a cold start, before any connector has
reconnected this session.

Two cache layers:
  - in-memory, short TTL — avoids re-resolving on every menu rebuild
    (``_rebuild()`` in menu_bar.py can fire fairly often: config changes,
    connector auth, rule hot-reload).
  - on-disk, no TTL — best-effort "last known name", so a name is available
    the instant the menu is built after a daemon restart, before any
    connector has been re-authenticated this session. Never authoritative;
    a resolution failure never blocks or changes an auto-accept decision,
    it only affects what a menu label says.

Resolution never makes a network call beyond what the connector is already
authorized for (it calls the same client methods used elsewhere in the
daemon), and only when a live, already-authenticated client is available.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .paths import user_dir
from .principal import PrincipalRegistry
from .policy.resource_registry import GrantResourceType
from .secure_files import atomic_write_json

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 15 * 60
# How long a lookup that failed (the connector errored, rather than finding no such resource) waits
# before it is tried again -- long enough that a page render does not retry it on every rebuild.
RETRY_AFTER_FAILURE_SECONDS = 60


def _cache_file() -> Path:
    # Per-principal (ADR 0008): a
    # grant resource id only resolves to a name through *that* principal's
    # own connectors (a Drive folder id means nothing in another user's
    # Drive), so the cache has to be per-principal too, not just the
    # resolver instance holding it. user_dir() is data_dir() itself for the
    # local principal, so this is the exact same path as before this phase.
    return user_dir() / "resource_name_cache.json"


def _cache_key(rt: GrantResourceType, resource_id: str) -> str:
    return f"{rt.connector}.{rt.config_key}:{resource_id}"


class ResourceNameResolver:
    """Not a singleton — the menu bar owns one instance for its lifetime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memory: dict[str, tuple[str, float]] = {}
        # key -> when a live lookup with an authenticated client last found no such resource (the
        # resolver returned None). In memory only: retried after the TTL, and never overwrites the
        # last-known name on disk.
        self._invalid: dict[str, float] = {}
        # key -> when a live lookup last failed (the resolver raised). Not a sign the resource is
        # gone, so never shown as invalid; it only holds off the retry for a minute.
        self._failed: dict[str, float] = {}
        self._disk: dict[str, str] = self._load_disk_cache()

    def _load_disk_cache(self) -> dict[str, str]:
        try:
            with open(_cache_file(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Could not load resource name cache: %s", exc)
            return {}

    def _save_disk_cache(self) -> None:
        try:
            atomic_write_json(_cache_file(), self._disk, indent=2, sort_keys=True)
        except Exception as exc:
            logger.warning("Could not save resource name cache: %s", exc)

    def cached_name(self, rt: GrantResourceType, resource_id: str) -> str | None:
        """Best-known name with no network call — memory, then disk."""
        key = _cache_key(rt, resource_id)
        with self._lock:
            hit = self._memory.get(key)
            if hit is not None:
                return hit[0]
            return self._disk.get(key)

    def is_invalid(self, rt: GrantResourceType, resource_id: str) -> bool:
        """True when a live lookup this session (within the TTL) found no such resource -- most
        likely deleted. False while nothing was attempted yet, so "not resolved yet" and "does not
        exist" stay distinct."""
        key = _cache_key(rt, resource_id)
        with self._lock:
            checked = self._invalid.get(key)
            return checked is not None and (time.monotonic() - checked) < CACHE_TTL_SECONDS

    def needs_resolve(self, rt: GrantResourceType, resource_id: str) -> bool:
        """True when ``resolve`` would hit the connector: no live outcome (name or not-found) is
        fresh yet. A name read from disk alone still counts -- it is only the last known one."""
        key = _cache_key(rt, resource_id)
        with self._lock:
            return not self._recently_checked(key)

    def _recently_checked(self, key: str) -> bool:
        """A live outcome for ``key`` is fresh: a name or a not-found within the TTL, or a failure
        within ``RETRY_AFTER_FAILURE_SECONDS``. Caller holds the lock."""
        now = time.monotonic()
        hit = self._memory.get(key)
        checked = self._invalid.get(key)
        failed = self._failed.get(key)
        return (
            (hit is not None and now - hit[1] < CACHE_TTL_SECONDS)
            or (checked is not None and now - checked < CACHE_TTL_SECONDS)
            or (failed is not None and now - failed < RETRY_AFTER_FAILURE_SECONDS)
        )

    def resolve(self, rt: GrantResourceType, resource_id: str, client: Any | None) -> str | None:
        """Resolve now, live if possible. Safe to call from a background
        thread — this never touches menu bar/AppKit state itself.

        Returns the freshly resolved name, or the last-known cached name if
        resolution isn't possible right now (no client) or fails, or None
        if there's no name available from any source. Only a resolver that
        answers ``None`` marks the id invalid; one that raises is logged and
        retried after ``RETRY_AFTER_FAILURE_SECONDS``.
        """
        key = _cache_key(rt, resource_id)
        with self._lock:
            hit = self._memory.get(key)
            fresh = hit is not None and (time.monotonic() - hit[1]) < CACHE_TTL_SECONDS
            recently_checked = self._recently_checked(key)
        if fresh and hit is not None:
            return hit[0]
        if recently_checked:
            return self.cached_name(rt, resource_id)

        if client is not None:
            try:
                name = rt.resolver(client, resource_id)
            except Exception as exc:
                logger.warning(
                    "Name resolution failed for %s %r: %s", rt.config_key, resource_id, exc
                )
                with self._lock:
                    self._failed[key] = time.monotonic()
                return self.cached_name(rt, resource_id)
            if name:
                with self._lock:
                    self._memory[key] = (name, time.monotonic())
                    self._invalid.pop(key, None)
                    self._failed.pop(key, None)
                    self._disk[key] = name
                self._save_disk_cache()
                return name
            with self._lock:
                self._memory.pop(key, None)
                self._failed.pop(key, None)
                self._invalid[key] = time.monotonic()

        # Live resolution unavailable or failed — fall back to whatever we
        # last knew, without penalizing it as newly "fresh" (so the next
        # call retries live resolution instead of trusting a stale hit).
        with self._lock:
            return self._disk.get(key)


_REGISTRY: PrincipalRegistry[ResourceNameResolver] = PrincipalRegistry(ResourceNameResolver)


def get_resolver() -> ResourceNameResolver:
    return _REGISTRY.get()
