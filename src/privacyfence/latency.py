"""Timing breadcrumbs for a slow approval -- where the seconds between "Claude
asked" and "the card or its confirmation is on screen" actually went.

Three signals, all log-only (nothing here changes a decision):

- ``StageTimer``: per gated call, each stage's duration keyed by the call's
  ``request_id`` -- logged at DEBUG as it happens, and once at INFO, as a
  single summary line, when any stage took ``SLOW_STAGE_SECONDS`` or more.
- ``to_thread``: ``asyncio.to_thread``, but timing how long the call waited
  for a free worker in the loop's shared default pool separately from how
  long it ran. Every connector fetch, text extraction, PII scan and audit
  lookup shares that pool (``min(32, cpu_count + 4)`` workers), so a long
  queue wait here is the pool being starved, not the work being slow.
- ``loop_health_lifespan``: a background probe on the web server's own loop
  -- the one every page, every SSE stream and every gated call share. Each
  tick measures how late a short sleep woke up (the loop was blocked) and
  how long a no-op took to start on the default pool (the pool was full),
  and logs a WARNING when either crosses ``SLOW_STAGE_SECONDS``.

Only stage names, durations, connector/tool names and request ids are ever
logged -- never arguments, summaries or content (coding guidelines §1.8).
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from typing import Any, AsyncIterator, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

SLOW_STAGE_SECONDS = 1.0
LOOP_PROBE_INTERVAL_SECONDS = 5.0


class StageTimer:
    """Collects ``(stage, seconds)`` pairs for one gated call. See module docstring."""

    def __init__(self, request_id: str, connector: str, tool: str) -> None:
        self.request_id = request_id
        self.connector = connector
        self.tool = tool
        self.stages: list[tuple[str, float]] = []
        self._started = time.monotonic()

    def record(self, stage: str, seconds: float) -> None:
        self.stages.append((stage, seconds))
        logger.debug(
            "request %s %s/%s stage %s took %.3fs", self.request_id, self.connector, self.tool, stage, seconds,
        )

    def is_slow(self) -> bool:
        return any(seconds >= SLOW_STAGE_SECONDS for _stage, seconds in self.stages)

    def summary(self) -> str:
        return " ".join(f"{stage}={seconds:.3f}s" for stage, seconds in self.stages)

    def log_if_slow(self) -> None:
        if self.is_slow():
            logger.info(
                "Slow request %s %s/%s (%.3fs so far): %s", self.request_id, self.connector, self.tool,
                time.monotonic() - self._started, self.summary(),
            )


async def to_thread(timer: StageTimer, stage: str, func: Callable[..., T], *args: Any) -> T:
    """``asyncio.to_thread(func, *args)``, recording ``<stage>.queued`` (waiting for a
    worker) and ``<stage>`` (running) on ``timer``. Context is carried into the worker the
    same way ``asyncio.to_thread`` itself does."""
    submitted = time.monotonic()
    started: list[float] = []

    def call() -> T:
        started.append(time.monotonic())
        return func(*args)

    try:
        return await asyncio.to_thread(call)
    finally:
        finished = time.monotonic()
        if started:
            timer.record(f"{stage}.queued", started[0] - submitted)
            timer.record(stage, finished - started[0])
        else:
            timer.record(f"{stage}.queued", finished - submitted)


def note_executor_queue_wait(what: str, queued_seconds: float) -> None:
    """For gate.py's dedicated popup executor: a dialog whose worker took
    ``SLOW_STAGE_SECONDS`` or more to start is a card the browser shows as
    "Preparing this request" for that long."""
    if queued_seconds >= SLOW_STAGE_SECONDS:
        logger.info("Popup worker for %s started %.3fs after it was queued", what, queued_seconds)
    else:
        logger.debug("Popup worker for %s started %.3fs after it was queued", what, queued_seconds)


def timed_call(func: Callable[..., T], submitted: float, what: str) -> Callable[..., T]:
    """Wrap ``func`` so its first line reports the queue wait since ``submitted``."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        note_executor_queue_wait(what, time.monotonic() - submitted)
        return func(*args, **kwargs)

    return wrapper


async def probe_once(sleep_seconds: float) -> tuple[float, float]:
    """One loop-health sample: ``(loop_lag, pool_wait)`` in seconds."""
    before = time.monotonic()
    await asyncio.sleep(sleep_seconds)
    lag = time.monotonic() - before - sleep_seconds
    submitted = time.monotonic()
    started: list[float] = []
    await asyncio.to_thread(lambda: started.append(time.monotonic()))
    return max(lag, 0.0), started[0] - submitted


async def monitor_loop_health(interval: float = LOOP_PROBE_INTERVAL_SECONDS) -> None:
    """Runs until cancelled. See module docstring."""
    while True:
        lag, pool_wait = await probe_once(interval)
        if lag >= SLOW_STAGE_SECONDS:
            logger.warning("Event loop was blocked for %.3fs -- every page and gated call waited", lag)
        if pool_wait >= SLOW_STAGE_SECONDS:
            logger.warning(
                "Default thread pool was full for %.3fs -- connector calls and PII scans queued", pool_wait,
            )


@contextlib.asynccontextmanager
async def loop_health_lifespan(interval: float = LOOP_PROBE_INTERVAL_SECONDS) -> AsyncIterator[None]:
    """Starts ``monitor_loop_health`` on the app's own loop for the app's lifetime."""
    task = asyncio.create_task(monitor_loop_health(interval), name="pf-loop-health")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
