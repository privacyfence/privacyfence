"""Supervisor against the stub plugin as a real child process."""
from __future__ import annotations

import asyncio
import logging
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from privacyfence.plugins import rpc
from privacyfence.plugins import supervisor as sv
from privacyfence.plugins.protocol import InitializeResult, RpcError
from privacyfence.plugins.supervisor import LaunchSpec, StartError, Supervisor, child_env

pytestmark = pytest.mark.unit

STUB = Path(__file__).resolve().parents[2] / "fixtures" / "plugins" / "stub" / "stub_plugin.py"
NAME, VERSION = "stub", "1.2.3"


def _params(purpose: str) -> dict:
    return {
        "protocol_version": "1.0.0",
        "purpose": purpose,
        "mode": "local",
        "plugin": {"name": NAME, "manifest_version": VERSION},
    }


class Harness:
    def __init__(self, tmp_path: Path, mode: str, **overrides) -> None:
        self.states: list[tuple[str, str | None]] = []
        self.ready: list[InitializeResult] = []
        self.sleeps: list[float] = []
        self.ticks = 0
        self.log_path = tmp_path / "logs" / "plugins" / "stub.log"
        self.on_sleep = None
        self.sup = Supervisor(
            LaunchSpec(NAME, [sys.executable, str(STUB), mode], tmp_path, self.log_path),
            initialize_params=_params,
            handlers={},
            notification_handlers={},
            on_state=lambda state, reason: self.states.append((state, reason)),
            on_ready=self._on_ready,
            **{"clock": self._clock, "sleep": self._sleep, **overrides},
        )

    async def _on_ready(self, result: InitializeResult) -> None:
        self.ready.append(result)

    def _clock(self) -> float:
        self.ticks += 1
        return float(self.ticks)

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            await self.on_sleep(len(self.sleeps))

    @property
    def names(self) -> list[str]:
        return [s for s, _ in self.states]

    @property
    def log(self) -> str:
        return self.log_path.read_text()


@pytest.fixture
def make(tmp_path):
    made: list[Harness] = []

    def factory(mode: str, **overrides) -> Harness:
        h = Harness(tmp_path, mode, **overrides)
        made.append(h)
        return h

    yield factory

    # A failing test must not leave a child process behind.
    for h in made:
        child = h.sup._child
        if child is not None:
            sv._signal_child(child.proc, kill=True)


@pytest.fixture(autouse=True)
def fast_stop(monkeypatch):
    monkeypatch.setattr(sv, "SHUTDOWN_GRACE_SECONDS", 3.0)
    monkeypatch.setattr(sv, "TERMINATE_GRACE_SECONDS", 3.0)


class TestHandshake:
    async def test_ok(self, make):
        h = make("ok")
        await h.sup.start()
        assert h.sup.state == "running"
        assert h.names == ["starting", "running"]
        assert [(r.plugin_name, r.plugin_version) for r in h.ready] == [(NAME, VERSION)]
        with pytest.raises(RpcError) as exc:
            await h.sup.peer.request("tool.prepare", {})
        assert exc.value.code == "method_not_found"
        await h.sup.stop()
        assert h.sup.peer is None

    async def test_wrong_name_disables_without_restart(self, make):
        h = make("wrong-name")
        await h.sup.start()
        await h.sup.join()
        assert h.states[-1] == ("disabled", sv.REASON_IDENTITY_DIFFERS)
        assert h.ready == []
        assert h.sleeps == []
        assert h.sup._child.proc.returncode is not None

    async def test_tool_validation_failure_disables(self, make):
        def reject(result):
            raise ValueError("tool names collide")

        h = make("ok", validate_tools=reject)
        await h.sup.start()
        await h.sup.join()
        assert h.states[-1] == ("disabled", "manifest invalid: tool names collide")
        assert h.ready == []
        assert h.sleeps == []

    async def test_validate_tools_receives_the_result(self, make):
        seen = []
        h = make("ok", validate_tools=seen.append)
        await h.sup.start()
        assert [r.plugin_name for r in seen] == [NAME]
        await h.sup.stop()

    async def test_start_twice_is_a_no_op(self, make):
        h = make("ok")
        await h.sup.start()
        await h.sup.start()
        assert h.names == ["starting", "running"]
        await h.sup.stop()

    async def test_missing_executable_is_a_crash(self, make, tmp_path):
        h = make("ok")
        h.sup._spec.argv = [str(tmp_path / "does-not-exist")]
        await h.sup.start()
        await h.sup.join()
        assert h.states[-1][0] == "disabled"
        assert len(h.sleeps) == sv.CRASH_LIMIT - 1

    async def test_on_ready_failure_is_a_crash(self, make):
        h = make("ok")

        async def boom(result):
            raise RuntimeError("nope")

        h.sup._on_ready = boom
        await h.sup.start()
        await h.sup.join()
        assert h.states[-1][1] == "crashed 5 times in 10 minutes"

    async def test_on_state_failure_is_swallowed(self, make):
        h = make("ok")

        def bad(state, reason):
            raise RuntimeError("callback")

        h.sup._on_state = bad
        await h.sup.start()
        assert h.sup.state == "running"
        await h.sup.stop()

    async def test_stop_before_start(self, make):
        h = make("ok")
        await h.sup.stop()
        assert h.states == [("disabled", "disabled by you")]


class TestStopRaces:
    async def test_stop_from_on_ready(self, make):
        h = make("ok")

        async def stop_now(result):
            await h.sup.stop()

        h.sup._on_ready = stop_now
        await h.sup.start()
        await h.sup.join()
        assert h.states[-1] == ("disabled", "disabled by you")
        assert "running" not in h.names

    @pytest.mark.parametrize("failure", [RpcError("internal_error", "peer closed"), sv._Fatal("manifest invalid: x")])
    async def test_stop_during_handshake_is_not_a_crash(self, make, failure):
        h = make("ok")

        async def dies_with_the_child(child, purpose):
            await child.gone.wait()
            raise failure

        h.sup._handshake = dies_with_the_child
        starter = asyncio.create_task(h.sup.start())
        while h.sup._child is None:
            await asyncio.sleep(0.01)
        await h.sup.stop()
        await starter
        assert h.sleeps == []
        assert h.states[-1] == ("disabled", "disabled by you")
        assert "backoff" not in h.names

    async def test_join_before_start(self, make):
        await make("ok").sup.join()


class _FakePeer:
    def __init__(self, outcome):
        self.outcome = outcome
        self.closed = False

    async def request(self, method, params):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class TestBeforeSpawn:
    async def test_runs_before_every_spawn_including_restarts(self, make):
        calls: list[int] = []

        async def check() -> None:
            calls.append(len(calls))

        h = make("crash-on-start", before_spawn=check)
        await h.sup.start()
        await h.sup.join()
        assert len(calls) == sv.CRASH_LIMIT == h.names.count("starting")

    async def test_refused_restart_disables_without_retrying(self, make):
        calls: list[int] = []

        async def refuse_the_second() -> None:
            calls.append(1)
            if len(calls) == 2:
                raise StartError("executable or manifest changed, enable again")

        h = make("crash-after-init", before_spawn=refuse_the_second)
        await h.sup.start()
        await h.sup.join()
        assert h.names == ["starting", "running", "backoff", "starting", "disabled"]
        assert h.states[-1] == ("disabled", "executable or manifest changed, enable again")
        assert h.sleeps == [1.0]
        assert len(h.ready) == 1

    async def test_refused_first_start_spawns_nothing(self, make):
        async def refuse() -> None:
            raise StartError("executable is writable by non-administrators")

        h = make("ok", before_spawn=refuse)
        await h.sup.start()
        await h.sup.join()
        assert h.states == [("starting", None), ("disabled", "executable is writable by non-administrators")]
        assert h.sup._child is None
        assert h.sleeps == []

    async def test_unexpected_failure_refuses_the_start(self, make):
        async def broken() -> None:
            raise OSError("disk gone")

        h = make("ok", before_spawn=broken)
        await h.sup.start()
        await h.sup.join()
        assert h.states[-1] == ("disabled", "could not start")
        assert h.sup._child is None and h.sleeps == []


class TestHandshakeChecks:
    async def _run(self, make, outcome):
        h = make("ok")
        child = sv._Child.__new__(sv._Child)
        child.peer = _FakePeer(outcome)  # type: ignore[assignment]
        return await h.sup._handshake(child, "run")

    async def test_plugin_reported_version_mismatch(self, make):
        with pytest.raises(sv._Fatal) as exc:
            await self._run(make, RpcError("version_mismatch"))
        assert exc.value.reason == "protocol major mismatch"

    async def test_other_plugin_error_propagates(self, make):
        with pytest.raises(RpcError) as exc:
            await self._run(make, RpcError("invalid_params"))
        assert exc.value.code == "invalid_params"

    async def test_malformed_result(self, make):
        with pytest.raises(sv._Fatal) as exc:
            await self._run(make, {"protocol_version": "1.0.0"})
        assert exc.value.reason.startswith("manifest invalid: ")


class TestMajorMismatch:
    async def test_plugin_not_started(self, make):
        h = make("bad-version")
        await h.sup.start()
        await h.sup.join()
        assert h.states[-1] == ("disabled", "protocol major mismatch")
        assert h.ready == []
        assert h.sleeps == []
        assert h.sup.peer is None
        assert h.sup._child.proc.returncode is not None


class TestCrashLimit:
    async def test_disabled_after_five_in_ten_minutes(self, make):
        h = make("crash-on-start")
        await h.sup.start()
        await h.sup.join()
        assert h.states[-1] == ("disabled", "crashed 5 times in 10 minutes")
        assert h.names.count("starting") == 5
        assert h.ready == []

    async def test_old_crashes_age_out(self, make, monkeypatch):
        h = make("crash-on-start")

        def far_apart() -> float:
            h.ticks += sv.CRASH_WINDOW_SECONDS + 1
            return float(h.ticks)

        h.sup._clock = far_apart

        async def stop_after_eight(count: int) -> None:
            if count == 8:
                await h.sup.stop()

        h.on_sleep = stop_after_eight
        await h.sup.start()
        await h.sup.join()
        assert len(h.sleeps) == 8
        assert set(h.sleeps) == {sv.RESTART_BACKOFF_SECONDS[0]}
        assert "crashed 5 times in 10 minutes" not in [r for _, r in h.states]


class TestBackoffSequence:
    async def test_sequence_then_cap(self, make, monkeypatch):
        monkeypatch.setattr(sv, "CRASH_LIMIT", 10)
        h = make("crash-on-start")

        async def stop_after_seven(count: int) -> None:
            if count == 7:
                await h.sup.stop()

        h.on_sleep = stop_after_seven
        await h.sup.start()
        await h.sup.join()
        assert h.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    async def test_crash_after_init_reports_running_then_backoff(self, make):
        h = make("crash-after-init")

        async def stop_at_two(count: int) -> None:
            if count == 2:
                await h.sup.stop()

        h.on_sleep = stop_at_two
        await h.sup.start()
        await h.sup.join()
        assert h.names[:4] == ["starting", "running", "backoff", "starting"]
        assert h.sleeps == [1.0, 2.0]

    async def test_stop_during_backoff(self, make):
        h = make("crash-on-start")
        gate = asyncio.Event()

        async def hang(count: int) -> None:
            await gate.wait()

        h.on_sleep = hang
        await h.sup.start()
        await asyncio.sleep(0)
        assert h.sup.state == "backoff"
        await h.sup.stop()
        assert h.states[-1] == ("disabled", "disabled by you")
        assert h.sup._task.done()


class TestJunkStdout:
    async def test_counts_as_a_crash(self, make):
        h = make("junk-stdout")

        async def stop_at_one(count: int) -> None:
            await h.sup.stop()

        h.on_sleep = stop_at_one
        await h.sup.start()
        await h.sup.join()
        assert h.names[:3] == ["starting", "running", "backoff"]
        assert h.sleeps == [1.0]
        assert h.sup._child.proc.returncode is not None


class TestShutdown:
    async def test_graceful(self, make):
        h = make("ok")
        await h.sup.start()
        proc = h.sup._child.proc
        started = time.monotonic()
        await h.sup.stop(reason="user")
        assert time.monotonic() - started < 3.0
        assert proc.returncode == 0
        assert h.states[-1] == ("disabled", "disabled by you")
        assert "crashed" not in " ".join(r or "" for _, r in h.states)

    async def test_grace_then_kill(self, make, monkeypatch):
        monkeypatch.setattr(sv, "SHUTDOWN_GRACE_SECONDS", 0.4)
        monkeypatch.setattr(sv, "TERMINATE_GRACE_SECONDS", 0.4)
        h = make("slow-shutdown")
        await h.sup.start()
        proc = h.sup._child.proc
        started = time.monotonic()
        await h.sup.stop(reason="hash_changed")
        elapsed = time.monotonic() - started
        assert proc.returncode is not None
        assert elapsed >= 0.4
        if sys.platform != "win32":
            assert proc.returncode == -9  # SIGTERM was ignored, so the grace ran out and it was killed
            assert elapsed >= 0.8
        assert h.states[-1] == ("disabled", "executable or manifest changed, enable again")
        assert h.sleeps == []

    async def test_stop_does_not_wait_for_a_grandchilds_pipe(self, make):
        h = make("spawn-child")  # its sleeping child inherits stderr
        await h.sup.start()
        started = time.monotonic()
        await asyncio.wait_for(h.sup.stop(), 5)
        # Windows leaves the grandchild running, so the log drain waits its full time there.
        limit = sv.LOG_DRAIN_SECONDS + 1.0 if sys.platform == "win32" else 1.0
        assert time.monotonic() - started < limit

    async def test_daemon_shutdown_sends_no_disabling_notice(self, make):
        h = make("ok")
        await h.sup.start()
        await h.sup.stop(reason="shutdown")
        assert h.states[-1] == ("disabled", None)


class TestStuckReader:
    async def test_disable_kills_a_plugin_that_stopped_reading(self, make, monkeypatch):
        monkeypatch.setattr(sv, "SHUTDOWN_NOTIFY_TIMEOUT_SECONDS", 0.2)
        h = make("stop-reading")
        await h.sup.start()
        proc = h.sup._child.proc
        filler = asyncio.create_task(h.sup.peer.notify("x", {"pad": "x" * 512_000}))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        await asyncio.wait_for(h.sup.stop(reason="user"), 5)
        elapsed = time.monotonic() - started
        assert proc.returncode is not None
        assert h.states[-1] == ("disabled", "disabled by you")
        assert "backoff" not in h.names
        assert h.sleeps == []
        assert elapsed < sv.TERMINATE_GRACE_SECONDS + 1
        await asyncio.gather(filler, return_exceptions=True)

    async def test_a_send_timeout_while_running_is_a_crash(self, make, monkeypatch, caplog):
        monkeypatch.setattr(rpc, "SEND_TIMEOUT_SECONDS", 0.3)
        h = make("stop-reading")

        async def stop_at_first(count: int) -> None:
            await h.sup.stop()

        h.on_sleep = stop_at_first
        await h.sup.start()
        proc = h.sup._child.proc
        with caplog.at_level(logging.INFO, logger=sv.logger.name):
            with pytest.raises(RpcError) as exc:
                await h.sup.peer.notify("x", {"pad": "x" * 512_000})
            assert exc.value.code == "timeout"
            await asyncio.wait_for(h.sup.join(), 5)
        assert ("backoff", "crashed 1 time") in h.states
        assert h.sleeps == [1.0]
        assert "crashed (write_timeout)" in caplog.text
        assert proc.returncode is not None


def _alive(pid: int) -> bool:
    """Whether ``pid`` is a live process; a zombie nobody has reaped yet counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = subprocess.run(  # noqa: S603 -- fixed argv
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False,  # noqa: S607
    ).stdout.strip()
    return bool(status) and not status.startswith("Z")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows terminates the plugin only")
class TestProcessGroup:
    async def _grandchild(self, h: Harness) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if h.log_path.exists():
                line = next((x for x in h.log.splitlines() if x.startswith("CHILD:")), None)
                if line is not None:
                    return int(line[len("CHILD:"):])
            await asyncio.sleep(0.05)
        raise AssertionError("the stub did not report its child")

    async def _gone(self, pid: int) -> bool:
        deadline = time.monotonic() + 5
        while _alive(pid):
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def test_children_die_after_a_graceful_exit(self, make):
        h = make("spawn-child")
        await h.sup.start()
        pid = await self._grandchild(h)
        try:
            assert _alive(pid)
            await h.sup.stop()
            assert h.sup._child.proc.returncode == 0  # the plugin exited by itself
            assert await self._gone(pid)
        finally:
            if _alive(pid):
                os.kill(pid, 9)


class TestEnvironment:
    def test_child_env_is_an_allowlist(self, monkeypatch):
        for key in ("PATH", "TZ", "LANG"):
            monkeypatch.setenv(key, "v")
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.setenv("PRIVACYFENCE_SECRET", "s")
        env = child_env()
        assert env["PRIVACYFENCE_PLUGIN"] == "1"
        assert "PRIVACYFENCE_SECRET" not in env
        assert "LC_ALL" not in env
        assert env["TZ"] == "v"
        assert set(env) <= set(sv._ENV_ALLOWLIST) | {"PRIVACYFENCE_PLUGIN"}

    async def test_daemon_env_not_inherited(self, make, monkeypatch):
        monkeypatch.setenv("PF_SENTINEL_TOKEN", "hunter2")
        h = make("echo-env")
        await h.sup.start()
        await h.sup.stop()
        line = next(line for line in h.log.splitlines() if line.startswith("ENV:"))
        keys = line[len("ENV:"):].split(",")
        assert "PF_SENTINEL_TOKEN" not in keys
        assert "hunter2" not in h.log
        assert "PRIVACYFENCE_PLUGIN" in keys


class TestLog:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    async def test_log_is_private(self, make):
        h = make("crash-on-start", clock=lambda: 0.0)
        h.log_path.parent.mkdir(parents=True)
        h.log_path.write_text("old\n")
        h.log_path.chmod(0o644)
        await h.sup.start()
        await h.sup.stop()
        assert stat.S_IMODE(h.log_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(h.log_path.parent.stat().st_mode) == 0o700

    async def test_stderr_goes_to_the_log(self, make):
        h = make("crash-on-start")
        await h.sup.start()
        await h.sup.join()
        assert "stub: crashing on start" in h.log

    async def test_rotation(self, make, monkeypatch):
        monkeypatch.setattr(sv, "LOG_MAX_BYTES", 10)
        h = make("ok")
        h.log_path.parent.mkdir(parents=True)
        h.log_path.write_text("current" * 5)
        for index in (1, 2, 3):
            h.log_path.with_name(f"stub.log.{index}").write_text(f"gen{index}")
        await h.sup.start()
        await h.sup.stop()
        assert h.log_path.with_name("stub.log.1").read_text() == "current" * 5
        assert h.log_path.with_name("stub.log.2").read_text() == "gen1"
        assert h.log_path.with_name("stub.log.3").read_text() == "gen2"
        assert not h.log_path.with_name("stub.log.4").exists()
        assert "current" not in h.log

    async def test_small_log_is_appended_to(self, make):
        h = make("ok")
        h.log_path.parent.mkdir(parents=True)
        h.log_path.write_text("keep\n")
        await h.sup.start()
        await h.sup.stop()
        assert h.log.startswith("keep\n")
        assert not h.log_path.with_name("stub.log.1").exists()

    def test_stderr_log_rotates_while_writing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sv, "LOG_MAX_BYTES", 10)
        log = sv._StderrLog(tmp_path / "x.log")
        log.append(b"a" * 25)
        sizes = [(tmp_path / name).stat().st_size for name in ("x.log", "x.log.1", "x.log.2")]
        assert sizes == [5, 10, 10]

    def test_stderr_log_appends_from_two_threads(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sv, "LOG_MAX_BYTES", 10_000)
        log = sv._StderrLog(tmp_path / "x.log")

        def work() -> None:
            for _ in range(100):
                log.append(b"y" * 50)

        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sizes = [p.stat().st_size for p in tmp_path.glob("x.log*")]
        assert sum(sizes) == 10_000
        assert max(sizes) <= 10_000

    async def test_running_log_stays_under_the_cap(self, make, monkeypatch):
        monkeypatch.setattr(sv, "LOG_MAX_BYTES", 4096)
        h = make("spam-stderr")
        await h.sup.start()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if h.log_path.exists() and "SPAM_DONE" in h.log:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("the plugin's stderr never reached the log")
            assert h.sup.state == "running"
            assert h.log_path.stat().st_size <= 4096
            for index in (1, 2, 3):
                assert h.log_path.with_name(f"stub.log.{index}").stat().st_size <= 4096
            assert not h.log_path.with_name("stub.log.4").exists()
        finally:
            await h.sup.stop()


class TestIntrospect:
    async def test_returns_result_and_stops(self, make):
        h = make("ok")
        result = await h.sup.introspect()
        assert (result.plugin_name, result.plugin_version) == (NAME, VERSION)
        assert h.sup._child.proc.returncode == 0
        assert h.states == []
        assert h.ready == []
        assert "SOURCE_CALL:introspection_only" in h.log

    @pytest.mark.parametrize(
        "mode, reason",
        [
            ("bad-version", "protocol major mismatch"),
            ("wrong-name", sv.REASON_IDENTITY_DIFFERS),
            ("crash-on-start", "could not start"),
        ],
    )
    async def test_failures(self, make, mode, reason):
        h = make(mode)
        with pytest.raises(StartError) as exc:
            await h.sup.introspect()
        assert exc.value.reason == reason
        assert h.sup._child.proc.returncode is not None


class TestRotateHelper:
    def test_missing_log_is_fine(self, tmp_path):
        sv._rotate_log(tmp_path / "none.log")

    def test_without_backups(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sv, "LOG_MAX_BYTES", 1)
        log = tmp_path / "a.log"
        log.write_text("xxxx")
        sv._rotate_log(log)
        assert not log.exists() and (tmp_path / "a.log.1").read_text() == "xxxx"
