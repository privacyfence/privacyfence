"""SSE state-push channel: ``GET /api/state/stream``, one connection per
open tab, carrying ``event: approvals`` --
``approvals.PendingApprovalRegistry.list_pending()`` summaries, polled the
same way ``web/routes_approvals.py``'s own ``/api/approvals/stream`` already
does. The registry can hold several of these at once.

Every open page re-renders from a *full* state list, never a patch --
``window.__pfRenderApprovals(state)`` is an idempotent full re-render
(the approval list's own convention) -- so there is no ordering or patch-application problem to
solve here, only "deliver the latest snapshot, promptly, to every open
connection", including on first connect/reconnect (a reconnect delivers
full state rather than a patch).

This module also owns the loop plumbing a running web server needs:
``set_loop``/``call_soon_threadsafe`` marshal a background-thread callback
(a rules-changed broadcast) onto this stream's own asyncio event loop, so the
subscriber queues are only ever touched from that one loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# How often a connection checks the approvals registry for a change, -- same interval and reasoning as
# routes_approvals.py's own _STREAM_POLL_SECONDS: short enough that a human
# watching the page doesn't perceive a lag, cheap enough (an in-memory dict
# scan) that polling it beats adding a second pub/sub mechanism.
_APPROVALS_POLL_SECONDS = 1.0

# Broadcast by StateStream.close() to wake every open subscription; never a real event name.
_CLOSED_EVENT = "__closed__"

# The web server's own asyncio event loop, captured once at startup (see
# server.py's lifespan wiring), so ``call_soon_threadsafe`` can marshal a
# background-thread callback onto it. Module-level so a plain function
# reference can be registered once and never touched again.
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _loop
    _loop = loop


def clear_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Clear the captured loop only if it is still ``loop``, so a server whose shutdown finishes
    late never clears the loop a newer server in the same process captured since."""
    global _loop
    if _loop is loop:
        _loop = None


def get_loop() -> asyncio.AbstractEventLoop | None:
    """The ASGI app's own running event loop, once captured by
    ``_state_stream_loop_lifespan`` -- ``None`` before startup or after
    shutdown. daemon_main.py's cache-warming uses this (via web/server.py's
    ``WebServer.wait_until_ready``) as the one loop every connector call
    now actually runs on, the direct successor of the old IPC thread's own
    loop for that same purpose."""
    return _loop


def call_soon_threadsafe(fn: Callable[..., None], *args: Any) -> None:
    """Run ``fn(*args)`` on the captured loop. Runs inline if no loop has
    been captured yet (e.g. called before the ASGI app's startup event has
    fired)."""
    if _loop is not None:
        _loop.call_soon_threadsafe(fn, *args)
    else:
        fn(*args)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class StateStream:
    """One instance per running web server, shared by every
    ``/api/state/stream`` connection. ``list_pending`` is
    ``WebApprovalUI.deferred_registry.list_pending``.
    """

    def __init__(self, *, list_pending: Callable[[], list[Any]]) -> None:
        self._list_pending = list_pending
        self._subscribers: set[asyncio.Queue] = set()
        self._closed = False

    def close(self) -> None:
        """End every open subscription and refuse new ones. Called on this stream's own loop by
        WebServer.stop() before uvicorn's graceful shutdown starts: an open SSE response would
        otherwise hold that shutdown for its whole grace period."""
        self._closed = True
        self._broadcast(_CLOSED_EVENT, None)

    def _broadcast(self, event: str, data: Any) -> None:
        for queue in list(self._subscribers):
            # maxsize=1, replace-not-append: each subscriber only ever
            # holds the *latest* pending message, so a burst of rapid
            # changes collapses to one flush instead of a growing backlog.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait((event, data))

    # ------------------------------------------------------------------ #
    # Read side -- the /api/state/stream route.
    # ------------------------------------------------------------------ #

    async def subscribe(
        self,
        is_disconnected: Callable[[], Awaitable[bool]],
        touch: Callable[[], bool] | None = None,
    ):
        """Async generator of SSE-formatted strings for one connection.

        ``touch``, when given, is called once per poll tick: a
        long-lived connection is itself proof the tab is open, so the
        session backing it is refreshed on the same cadence this loop
        already wakes at, instead of only once when the connection was
        opened. It returns whether the session is still live -- once it
        returns False (idle- or absolute-expired), the loop breaks and the
        connection closes, the same way a client disconnect does, rather
        than streaming to a tab whose session no longer exists."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            last_ids = self._approvals_ids()
            yield _sse("approvals", self._approvals_payload())

            while True:
                if self._closed:
                    break
                if await is_disconnected():
                    break
                if touch is not None and not touch():
                    break
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=_APPROVALS_POLL_SECONDS)
                    if event == _CLOSED_EVENT:
                        break
                    yield _sse(event, data)
                except asyncio.TimeoutError:
                    pass
                ids = self._approvals_ids()
                if ids != last_ids:
                    last_ids = ids
                    yield _sse("approvals", self._approvals_payload())
        finally:
            self._subscribers.discard(queue)

    def _approvals_ids(self) -> tuple[str, ...]:
        return tuple(a.id for a in self._list_pending())

    def _approvals_payload(self) -> list[dict[str, Any]]:
        return [a.to_summary_dict() for a in self._list_pending()]
