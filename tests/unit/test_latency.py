"""Unit tests for latency.py's timing breadcrumbs."""
from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from privacyfence import latency


class TestStageTimer:
    def test_fast_stages_log_debug_only(self, caplog):
        caplog.set_level(logging.DEBUG, logger="privacyfence.latency")
        timer = latency.StageTimer("req1", "gmail", "gmail_get_message")
        timer.record("pii_scan", 0.01)
        timer.log_if_slow()

        assert not timer.is_slow()
        assert [r.levelno for r in caplog.records] == [logging.DEBUG]
        assert "req1 gmail/gmail_get_message stage pii_scan took 0.010s" in caplog.text

    def test_a_slow_stage_logs_one_info_summary_of_every_stage(self, caplog):
        caplog.set_level(logging.INFO, logger="privacyfence.latency")
        timer = latency.StageTimer("req2", "drive", "drive_get_file_content")
        timer.record("pii_scan.queued", 29.5)
        timer.record("pii_scan", 0.2)
        timer.log_if_slow()

        assert timer.is_slow()
        [record] = caplog.records
        assert record.levelno == logging.INFO
        assert "Slow request req2 drive/drive_get_file_content" in record.getMessage()
        assert "pii_scan.queued=29.500s pii_scan=0.200s" in record.getMessage()


class TestToThread:
    async def test_records_queue_wait_and_run_time_separately(self):
        timer = latency.StageTimer("r", "c", "t")

        result = await latency.to_thread(timer, "scan", lambda a, b: a + b, 2, 3)

        assert result == 5
        assert [stage for stage, _ in timer.stages] == ["scan.queued", "scan"]

    async def test_a_raising_call_is_still_timed_and_reraises(self):
        timer = latency.StageTimer("r", "c", "t")

        def boom():
            raise ValueError("no")

        with pytest.raises(ValueError):
            await latency.to_thread(timer, "scan", boom)
        assert [stage for stage, _ in timer.stages] == ["scan.queued", "scan"]

    async def test_cancelled_before_a_worker_started_records_only_the_queue_wait(self, monkeypatch):
        async def never_starts(call):
            raise asyncio.CancelledError

        monkeypatch.setattr(latency.asyncio, "to_thread", never_starts)
        timer = latency.StageTimer("r", "c", "t")

        with pytest.raises(asyncio.CancelledError):
            await latency.to_thread(timer, "scan", lambda: None)
        assert [stage for stage, _ in timer.stages] == ["scan.queued"]


class TestTimedCall:
    def test_reports_the_queue_wait_then_calls_through(self, caplog, monkeypatch):
        caplog.set_level(logging.DEBUG, logger="privacyfence.latency")
        monkeypatch.setattr(latency.time, "monotonic", lambda: 100.0)

        def show(x, *, y):
            return x + y

        wrapped = latency.timed_call(show, 99.5, "show")

        assert wrapped(1, y=2) == 3
        assert wrapped.__name__ == "show"
        [record] = caplog.records
        assert record.levelno == logging.DEBUG
        assert "Popup worker for show started 0.500s after it was queued" in record.getMessage()

    def test_a_slow_start_logs_info(self, caplog, monkeypatch):
        caplog.set_level(logging.INFO, logger="privacyfence.latency")
        monkeypatch.setattr(latency.time, "monotonic", lambda: 130.0)

        latency.timed_call(lambda: None, 100.0, "show_pii_confirmation_popup")()

        [record] = caplog.records
        assert record.levelno == logging.INFO
        assert "show_pii_confirmation_popup started 30.000s" in record.getMessage()


class TestLoopHealth:
    async def test_probe_on_an_idle_loop_reports_no_lag_or_pool_wait(self):
        lag, pool_wait = await latency.probe_once(0.01)

        assert 0.0 <= lag < latency.SLOW_STAGE_SECONDS
        assert 0.0 <= pool_wait < latency.SLOW_STAGE_SECONDS

    async def test_probe_sees_a_blocked_loop(self):
        async def block_the_loop():
            await asyncio.sleep(0)
            threading.Event().wait(0.3)  # deliberately synchronous: holds the loop itself

        blocker = asyncio.create_task(block_the_loop())
        lag, _pool_wait = await latency.probe_once(0.01)
        await blocker

        assert lag >= 0.25

    async def test_monitor_warns_on_lag_and_on_a_full_pool(self, caplog, monkeypatch):
        caplog.set_level(logging.WARNING, logger="privacyfence.latency")
        samples = iter([(0.0, 0.0), (2.0, 3.0)])

        async def fake_probe(_interval):
            try:
                return next(samples)
            except StopIteration:
                raise asyncio.CancelledError from None

        monkeypatch.setattr(latency, "probe_once", fake_probe)

        with pytest.raises(asyncio.CancelledError):
            await latency.monitor_loop_health(0.01)

        messages = [r.getMessage() for r in caplog.records]
        assert messages == [
            "Event loop was blocked for 2.000s -- every page and gated call waited",
            "Default thread pool was full for 3.000s -- connector calls and PII scans queued",
        ]

    async def test_lifespan_runs_the_monitor_and_stops_it_on_exit(self, monkeypatch):
        started = asyncio.Event()

        async def fake_monitor(_interval):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(latency, "monitor_loop_health", fake_monitor)

        async with latency.loop_health_lifespan(0.01):
            await asyncio.wait_for(started.wait(), timeout=2)
            [task] = [t for t in asyncio.all_tasks() if t.get_name() == "pf-loop-health"]
        assert task.cancelled()
