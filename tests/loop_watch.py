"""What an event loop is still waiting on in the operating system, and a watchdog for closing one.

On Windows, closing a ``ProactorEventLoop`` waits, without a time limit, for every overlapped
operation in its proactor's cache to complete. One that never completes hangs the test's teardown
until pytest-timeout ends the whole run with a stack dump that names ``GetQueuedCompletionStatus``
but not the operation. ``install_close_watchdog`` makes that wait report what it is waiting for.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

CLOSE_REPORT_AFTER_SECONDS = 5.0
CLOSE_WATCHDOG_THREAD_NAME = "proactor-close-watchdog"


def pending_io(loop: asyncio.AbstractEventLoop) -> list[str]:
    """The loop's outstanding OS-level work, apart from its own wake-up socket: overlapped
    operations on a proactor loop, registered descriptors on a selector loop."""
    own = getattr(loop, "_ssock", None)
    proactor = getattr(loop, "_proactor", None)
    if proactor is not None:
        entries = list(getattr(proactor, "_cache", {}).values())
        return [f"{fut!r} on {_describe(obj)}" for fut, _ov, obj, _cb in entries if obj is not own]
    selector = getattr(loop, "_selector", None)
    if selector is None or selector.get_map() is None:
        return []
    own_fd = own.fileno() if own is not None and own.fileno() != -1 else None
    return [
        f"fd {key.fd} {_describe(key.fileobj)}: {key.data!r}"
        for key in list(selector.get_map().values())
        if key.fd != own_fd
    ]


def _describe(obj: Any) -> str:
    try:
        return repr(obj)
    except Exception as exc:  # noqa: BLE001  # a half-closed object must not stop the report
        return f"<unprintable {type(obj).__name__}: {exc}>"


def close_report(proactor: Any, loop: asyncio.AbstractEventLoop | None, waited: float) -> str:
    lines = [f"Closing {proactor!r} has waited {waited:.1f} s. Still pending:"]
    for fut, _ov, obj, _cb in list(getattr(proactor, "_cache", {}).values()):
        lines.append(f"  {fut!r} on {_describe(obj)}")
    if loop is not None:
        tasks = asyncio.all_tasks(loop)
        lines.append(f"Tasks on {loop!r}: {len(tasks)}")
        lines.extend(f"  {task!r}" for task in tasks)
    return "\n".join(lines)


def watched_close(
    close: Callable[[Any], None], report: Callable[[str], None], after: float = CLOSE_REPORT_AFTER_SECONDS,
) -> Callable[[Any], None]:
    """``close`` (a proactor's) that hands ``report`` a description of what it is still waiting on
    once it has taken longer than ``after`` seconds, and then keeps waiting."""

    def wrapper(proactor: Any) -> None:
        started = time.monotonic()
        loop = getattr(proactor, "_loop", None)
        timer = threading.Timer(after, lambda: report(close_report(proactor, loop, time.monotonic() - started)))
        timer.name = CLOSE_WATCHDOG_THREAD_NAME
        timer.daemon = True
        timer.start()
        try:
            close(proactor)
        finally:
            timer.cancel()

    return wrapper


def install_close_watchdog(report: Callable[[str], None]) -> None:
    """Wrap ``IocpProactor.close`` (Windows only) with ``watched_close``."""
    if sys.platform != "win32":
        return
    proactor_class = asyncio.windows_events.IocpProactor
    if not getattr(proactor_class.close, "_close_watchdog", False):
        wrapper = watched_close(proactor_class.close, report)
        wrapper._close_watchdog = True  # type: ignore[attr-defined]
        proactor_class.close = wrapper
