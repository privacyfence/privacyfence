"""``EventFanout``: tells running plugins when a connector's state changes (ADR 0120).

``on_connectors_changed(rows)`` diffs each connector's ``(enabled, authed)`` against the previous
call. The first call only records a baseline: nothing changed, the daemon just started looking.
Every event is best-effort, a failed send is logged at debug and never reaches the caller.

``plugin.disabling`` and ``shutdown`` are the supervisor's, ``principal.removed`` is never sent in
local mode.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from privacyfence.plugins.rpc import RpcPeer
from privacyfence.principal import LOCAL_PRINCIPAL

logger = logging.getLogger(__name__)

EVENT_STATE_CHANGED = "connector.state_changed"


def transitions(previous: tuple[bool, bool], current: tuple[bool, bool]) -> str | None:
    """The one state a connector's ``(enabled, authed)`` change amounts to, or ``None``.

    A change of ``enabled`` is reported as that and nothing else, even when ``authed`` moved with
    it; ``authed`` only counts while the connector is enabled."""
    was_enabled, was_authed = previous
    enabled, authed = current
    if enabled != was_enabled:
        return "enabled" if enabled else "disabled"
    if enabled and authed != was_authed:
        return "signed_in" if authed else "signed_out"
    return None


class EventFanout:
    def __init__(self, peers: Callable[[], Iterable[RpcPeer]]) -> None:
        self._peers = peers
        self._seen: dict[str, tuple[bool, bool]] | None = None

    def changes(self, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Record ``rows`` and return the ``connector.state_changed`` payloads they imply."""
        current = {str(r["key"]): (bool(r["enabled"]), bool(r["authed"])) for r in rows if "key" in r}
        previous, self._seen = self._seen, current
        if previous is None:
            return []
        events = []
        for connector, state in current.items():
            if connector not in previous:
                continue
            moved = transitions(previous[connector], state)
            if moved is not None:
                events.append({"connector": connector, "state": moved, "principal": LOCAL_PRINCIPAL.id})
        return events

    async def send(self, events: list[dict[str, str]]) -> None:
        """Notify every running plugin of ``events``; a plugin that cannot be reached is skipped."""
        for peer in list(self._peers()):
            for params in events:
                try:
                    await peer.notify(EVENT_STATE_CHANGED, dict(params))
                except Exception:
                    logger.debug("Could not send %s to a plugin", EVENT_STATE_CHANGED, exc_info=True)


__all__ = ["EVENT_STATE_CHANGED", "EventFanout", "transitions"]
