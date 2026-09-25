"""Per-principal connector registry: org mode's connectors are per-principal,
like everything else a principal owns.

``connector_host.py``'s ``ConnectorHost`` holds one process-wide
``{name: Connector}`` map, built once by ``daemon_main.build_connectors()``
at daemon startup -- correct for local mode, where there is exactly one
principal for the life of the process. Org mode needs a *set* of these, one
per principal, built lazily (a principal's connectors can't exist before
that principal has authorized the underlying services) and evicted
when idle, since "N users x up to 12 authenticated API clients" is "the
main memory-scaling question."

``ConnectorRegistry`` is that: a lazy, bounded, principal-keyed cache of
``ConnectorHost`` instances. It is deliberately **not** part of
daemon_main.py's local-mode boot sequence, which builds exactly one
``ConnectorHost`` for the local principal directly, so local mode does not
depend on this class at all.

In org mode, ``daemon_main.py``'s ``_start_org_web_server``
builds one ``ConnectorRegistry`` per daemon and the ``/mcp`` dispatcher's
``connectors_provider`` is ``lambda: registry.get(current_principal()).
connectors`` -- ``local`` mode's own single ``ConnectorHost``, built once at
startup for the local principal, is completely unaffected (this class is
still never touched on that path). ``web/routes_connect.py``'s OAuth
callback route calls ``evict(principal.id)`` right after writing a new
service token, so the very next call for that principal rebuilds its
connector set instead of waiting out ``idle_evict_seconds``.

``factory`` is meant to be a thin wrapper around
``daemon_main.build_connectors(config, org_config)`` -- ``build_connectors``
itself doesn't need to change *its connector-building logic* for this to
work correctly per-principal, because every path it resolves through (token
files via ``_resolve_path``, the Slack/Telegram cache files) now goes
through ``paths.user_dir()``, which resolves against whichever principal
``ConnectorRegistry.get()`` below has entered via ``principal_scope`` at the
time ``factory`` runs -- see paths.py's own ``user_dir()`` and
daemon_main.py's ``_resolve_path``. (``build_connectors`` also returns a
per-connector failure-reason map -- ``factory`` itself is
still just ``list[Connector]``, so daemon_main.py's own
``_connectors_for_principal`` unpacks and discards that map; see its
docstring for why org mode has nowhere to surface it yet.)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .connector import Connector
from .connector_host import ConnectorHost
from .principal import Principal, principal_scope

logger = logging.getLogger(__name__)

# Conservative defaults -- an org operator who actually needs different
# numbers has the constructor arguments to change them; these just need to
# be "some real bound", not a tuned production value.
DEFAULT_MAX_PRINCIPALS = 200
DEFAULT_IDLE_EVICT_SECONDS = 30 * 60


class TooManyPrincipalsError(RuntimeError):
    """Raised by ``ConnectorRegistry.get()`` when serving one more principal
    would exceed ``max_principals`` and evicting idle entries first didn't
    make room. Fail closed -- the same "reject further ... rather than
    queueing" posture the per-principal pending-approval cap takes --
    rather than let one busy install's connector memory grow without bound."""


class ConnectorRegistry:
    """Lazily builds and caches one ``ConnectorHost`` per principal."""

    def __init__(
        self,
        factory: Callable[[Principal], list[Connector]],
        *,
        max_principals: int = DEFAULT_MAX_PRINCIPALS,
        idle_evict_seconds: float = DEFAULT_IDLE_EVICT_SECONDS,
    ) -> None:
        self._factory = factory
        self._max_principals = max_principals
        self._idle_evict_seconds = idle_evict_seconds
        self._lock = threading.Lock()
        self._hosts: dict[str, ConnectorHost] = {}
        self._last_used: dict[str, float] = {}

    def get(self, principal: Principal) -> ConnectorHost:
        """The current (or freshly built) ``ConnectorHost`` for
        ``principal``. Building happens outside the registry's own lock --
        ``factory`` does real network calls (each connector's own
        ``check_connection()``), which must never hold up every other
        principal's request while one principal's connectors come up."""
        now = time.monotonic()
        with self._lock:
            host = self._hosts.get(principal.id)
            if host is not None:
                self._last_used[principal.id] = now
                return host
            self._evict_idle_locked(now)
            if len(self._hosts) >= self._max_principals:
                raise TooManyPrincipalsError(
                    f"Connector registry is at capacity ({self._max_principals} principals); "
                    f"cannot build connectors for {principal.id!r} right now."
                )

        with principal_scope(principal):
            connectors = self._factory(principal)
        host = ConnectorHost(connectors)
        with self._lock:
            # Another thread may have built (and cached) this same
            # principal's host while this one was outside the lock above --
            # keep whichever was cached first rather than clobbering it with
            # a second, redundant build.
            existing = self._hosts.get(principal.id)
            if existing is not None:
                self._last_used[principal.id] = time.monotonic()
                return existing
            self._hosts[principal.id] = host
            self._last_used[principal.id] = time.monotonic()
        return host

    def _evict_idle_locked(self, now: float) -> None:
        stale = [
            pid for pid, last in self._last_used.items()
            if (now - last) >= self._idle_evict_seconds
        ]
        for pid in stale:
            self._hosts.pop(pid, None)
            self._last_used.pop(pid, None)
        if stale:
            logger.info("Evicted %d idle principal(s) from the connector registry", len(stale))

    def evict(self, principal_id: str) -> None:
        """Explicit eviction -- e.g. a principal's service credentials
        changed and its connectors need rebuilding from scratch, or an
        admin revokes access."""
        with self._lock:
            self._hosts.pop(principal_id, None)
            self._last_used.pop(principal_id, None)

    @property
    def principal_count(self) -> int:
        """The metric that goes alongside the bound: how many
        principals' connector sets are currently live in memory."""
        with self._lock:
            return len(self._hosts)


__all__ = ["ConnectorRegistry", "TooManyPrincipalsError"]
