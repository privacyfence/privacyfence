"""tests/loop_watch.py: what a loop still waits on, and the report a slow proactor close prints."""
from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
import types

import pytest

from tests import loop_watch
from tests.loop_watch import install_close_watchdog, pending_io, watched_close

pytestmark = pytest.mark.unit


class Named:
    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


class TestPendingIo:
    async def test_a_fresh_loop_waits_on_nothing(self):
        assert pending_io(asyncio.get_running_loop()) == []

    async def test_a_socket_being_read_is_listed_until_the_read_ends(self):
        loop = asyncio.get_running_loop()
        left, right = socket.socketpair()
        left.setblocking(False)
        try:
            read = asyncio.ensure_future(loop.sock_recv(left, 1))
            await asyncio.sleep(0)
            [entry] = pending_io(loop)
            assert f"fd {left.fileno()}" in entry

            right.send(b"x")
            assert await read == b"x"
            assert pending_io(loop) == []
        finally:
            left.close()
            right.close()

    def test_a_proactor_lists_its_cache_but_not_the_loops_own_wake_up_read(self):
        own = object()
        proactor = types.SimpleNamespace(_cache={
            1: (Named("<wake-up read>"), None, own, None),
            2: (Named("<_OverlappedFuture pending>"), None, Named("<socket to 127.0.0.1:8000>"), None),
        })
        loop = types.SimpleNamespace(_ssock=own, _proactor=proactor)

        assert pending_io(loop) == ["<_OverlappedFuture pending> on <socket to 127.0.0.1:8000>"]


class FakeProactor:
    def __init__(self, loop, close_for: float) -> None:
        self._loop = loop
        self._cache = {
            7: (Named("<_OverlappedFuture cancelled>"), None, Named("<socket raddr=('127.0.0.1', 8000)>"), None),
        }
        self.close_for = close_for

    def close(self) -> None:
        time.sleep(self.close_for)


class TestWatchedClose:
    def test_a_slow_close_reports_its_pending_operations_and_tasks_then_finishes(self):
        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(asyncio.sleep(60), name="still-reading")
            loop.run_until_complete(asyncio.sleep(0))
            reports: list[str] = []
            close = watched_close(FakeProactor.close, reports.append, after=0.05)

            close(FakeProactor(loop, close_for=0.3))

            [report] = reports
            assert "Still pending:" in report
            assert "<_OverlappedFuture cancelled> on <socket raddr=('127.0.0.1', 8000)>" in report
            assert "Tasks on" in report and "still-reading" in report
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        finally:
            loop.close()

    def test_a_prompt_close_reports_nothing(self):
        reports: list[str] = []
        close = watched_close(FakeProactor.close, reports.append, after=0.2)

        close(FakeProactor(None, close_for=0.0))
        time.sleep(0.3)

        assert reports == []
        assert not [t for t in threading.enumerate() if isinstance(t, threading.Timer) and t.is_alive()]


class TestInstall:
    def test_wraps_the_windows_proactor_close_once(self, monkeypatch):
        closed: list[object] = []
        fake = types.ModuleType("asyncio.windows_events")
        fake.IocpProactor = type("IocpProactor", (), {"close": lambda self: closed.append(self)})
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "asyncio.windows_events", fake)
        monkeypatch.delattr(asyncio, "windows_events", raising=False)

        install_close_watchdog(lambda text: None)
        wrapped = fake.IocpProactor.close
        install_close_watchdog(lambda text: None)
        proactor = fake.IocpProactor()
        proactor.close()

        assert fake.IocpProactor.close is wrapped
        assert closed == [proactor]

    def test_leaves_other_platforms_alone(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(loop_watch, "watched_close", lambda *a, **k: pytest.fail("wrapped on linux"))

        install_close_watchdog(lambda text: None)
