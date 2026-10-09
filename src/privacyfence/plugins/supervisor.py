"""Runs one plugin as a child process: spawn, handshake, restart with backoff, stop.

The child gets a minimal environment (``child_env``), its stderr goes through the daemon into a private log capped at
5 MiB while it runs, and its stdout carries protocol messages only. A crash is an unexpected exit, or a peer that closes
while the process is still there; five crashes inside ten minutes disable the plugin. A failed
handshake check (protocol major, identity, tool definitions) is not a crash: it will fail the same
way every time, so the plugin is disabled with the reason and not restarted. Neither is a refusal
from the ``before_spawn`` hook, which runs before every spawn, restarts included, so the host can
repeat its trust and hash checks on each start (ADR 0121).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from privacyfence.plugins.constants import (
    CRASH_LIMIT,
    CRASH_WINDOW_SECONDS,
    LOG_BACKUP_COUNT,
    LOG_DRAIN_SECONDS,
    LOG_MAX_BYTES,
    LOG_PUMP_CHUNK_BYTES,
    MAX_LINE_BYTES,
    PROTOCOL_MAJOR,
    RESTART_BACKOFF_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    SHUTDOWN_NOTIFY_TIMEOUT_SECONDS,
    TERMINATE_GRACE_SECONDS,
)
from privacyfence.plugins.protocol import InitializeResult, RpcError
from privacyfence.plugins.rpc import RpcPeer
from privacyfence.secure_files import secure_mkdir

logger = logging.getLogger(__name__)

# subprocess.CREATE_* values, spelled out so the module need not import subprocess for them.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_ENV_ALLOWLIST = (
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
)

REASON_MAJOR_MISMATCH = "protocol major mismatch"
REASON_IDENTITY_DIFFERS = "manifest invalid: name or version differs from the plugin's own"
_STOP_REASONS = {
    "user": "disabled by you",
    "hash_changed": "executable or manifest changed, enable again",
}

Handler = Callable[[dict], Awaitable[Any]]
NotificationHandler = Callable[[dict], Awaitable[None]]


def child_env() -> dict[str, str]:
    """The environment a plugin runs with: an allowlist, never a copy of the daemon's."""
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    env["PRIVACYFENCE_PLUGIN"] = "1"
    return env


@dataclass
class LaunchSpec:
    name: str
    argv: list[str]
    cwd: Path
    log_path: Path


class StartError(Exception):
    """``introspect`` could not get an initialize result, or ``before_spawn`` refused a start;
    ``reason`` is the settings text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Fatal(Exception):
    """A handshake check failed: disable with ``reason`` and do not restart."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Child:
    """A running process, its peer, and one event that fires when either is gone."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        handlers: dict[str, Handler],
        notification_handlers: dict[str, NotificationHandler],
    ) -> None:
        assert proc.stdout is not None and proc.stdin is not None  # nosec B101  # PIPE was requested
        self.proc = proc
        self.gone = asyncio.Event()
        self.gone_reason = ""
        self.peer = RpcPeer(
            proc.stdout,
            proc.stdin,
            handlers=handlers,
            notification_handlers=notification_handlers,
            on_close=self.mark_gone,
        )
        self.stderr_pump: asyncio.Task | None = None
        self.exit_watch = asyncio.create_task(self._watch_exit())

    def mark_gone(self, reason: str) -> None:
        if not self.gone.is_set():
            self.gone_reason = reason
            self.gone.set()

    async def _watch_exit(self) -> None:
        await _wait_exit(self.proc)
        self.mark_gone("exit")


_EXIT_POLL_SECONDS = 0.05


async def _wait_exit(proc: asyncio.subprocess.Process, timeout: float | None = None) -> bool:
    """True once ``proc`` has exited (its returncode is set), False when ``timeout`` ran out.

    Polls the returncode instead of awaiting the process's ``wait()``, which on Python 3.11 also
    waits for every pipe to close, and a grandchild can hold the stderr pipe open."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while proc.returncode is None:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_EXIT_POLL_SECONDS)
    return True


def _rotate_log(path: Path, *, force: bool = False) -> None:
    """Shift ``path`` to ``.1`` (and ``.1`` to ``.2`` …) once it is over the size limit, or at once
    when ``force`` is set."""
    if not force:
        try:
            if path.stat().st_size <= LOG_MAX_BYTES:
                return
        except FileNotFoundError:
            return
    path.with_name(f"{path.name}.{LOG_BACKUP_COUNT}").unlink(missing_ok=True)
    for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def _open_log(path: Path) -> int:
    secure_mkdir(path.parent)
    _rotate_log(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if sys.platform != "win32":  # pragma: no branch -- Windows has no mode bits to assert
        os.fchmod(fd, 0o600)
    return fd


class _StderrLog:
    """The plugin's log file, written chunk by chunk so it never grows past ``LOG_MAX_BYTES``.

    Two pumps of one supervisor can overlap after a restart (the old child's is still draining
    when the new one starts), so ``append`` runs under a lock. The file is opened per chunk: no
    descriptor is shared with children and no open handle blocks a rename on Windows. A line may
    be split across files."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            while data:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    if sys.platform != "win32":  # pragma: no branch -- Windows has no mode bits to assert
                        os.fchmod(fd, 0o600)
                    size = os.fstat(fd).st_size
                    if size < LOG_MAX_BYTES:
                        room = data[: LOG_MAX_BYTES - size]
                        data = data[len(room):]
                        view = memoryview(room)
                        while view:
                            view = view[os.write(fd, view):]
                        continue
                finally:
                    os.close(fd)
                _rotate_log(self.path, force=True)


async def _pump_stderr(stream: asyncio.StreamReader, log: _StderrLog, name: str) -> None:
    """Copy the plugin's stderr into ``log`` until EOF. Never cancelled: it ends when the last
    holder of the pipe closes it. A write error is logged once and the rest is discarded, so a
    full disk never blocks the child."""
    failed = False
    while chunk := await stream.read(LOG_PUMP_CHUNK_BYTES):
        if failed:
            continue
        try:
            await asyncio.to_thread(log.append, chunk)
        except OSError as exc:
            failed = True
            logger.warning("plugin %s: could not write its log: %s", name, exc)


def _signal_child(proc: asyncio.subprocess.Process, *, kill: bool) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":  # pragma: no cover -- Windows only
            if kill:
                proc.kill()
            else:
                proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):  # pragma: no cover -- exited between the check and the signal
        pass


class Supervisor:
    def __init__(
        self,
        spec: LaunchSpec,
        *,
        initialize_params: Callable[[str], dict],
        handlers: dict[str, Handler],
        notification_handlers: dict[str, NotificationHandler],
        on_state: Callable[[str, str | None], None],
        on_ready: Callable[[InitializeResult], Awaitable[None]],
        validate_tools: Callable[[InitializeResult], None] = lambda _result: None,
        before_spawn: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._spec = spec
        self._initialize_params = initialize_params
        self._handlers = handlers
        self._notification_handlers = notification_handlers
        self._on_state = on_state
        self._on_ready = on_ready
        self._validate_tools = validate_tools
        self._before_spawn = before_spawn
        self._clock = clock
        self._sleep = sleep
        self._state = "discovered"
        self._child: _Child | None = None
        self._task: asyncio.Task[None] | None = None
        self._first_done = asyncio.Event()
        self._stopping = False
        self._crashes: list[float] = []
        self._stderr_log = _StderrLog(spec.log_path)
        self._pumps: set[asyncio.Task] = set()

    @property
    def peer(self) -> RpcPeer | None:
        child = self._child
        if self._state == "running" and child is not None:
            return child.peer
        return None

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str, reason: str | None = None) -> None:
        self._state = state
        logger.info("plugin %s: %s%s", self._spec.name, state, f" ({reason})" if reason else "")
        try:
            self._on_state(state, reason)
        except Exception:
            logger.warning("plugin %s: on_state callback failed", self._spec.name, exc_info=True)

    async def start(self) -> None:
        """Spawn and handshake. Returns once the first attempt has settled; the plugin then keeps
        being restarted on crashes until ``stop`` or the crash limit."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._crashes = []
        self._first_done = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        await self._first_done.wait()

    async def join(self) -> None:
        """Wait until the supervision loop has ended (disabled or stopped)."""
        task = self._task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def introspect(self) -> InitializeResult:
        """One start with purpose ``introspect``, then stop. Never restarts, never reports state."""

        async def refuse(_params: dict) -> Any:
            raise RpcError("introspection_only", "not available while introspecting")

        handlers: dict[str, Handler] = {"source.call": refuse, "confirm.request": refuse}
        child = await self._spawn(handlers, {})
        try:
            result = await self._handshake(child, "introspect")
        except _Fatal as exc:
            raise StartError(exc.reason) from None
        except Exception as exc:
            raise StartError("could not start") from exc
        else:
            return result
        finally:
            await self._stop_child(child, "shutdown")

    async def stop(self, *, reason: str = "user") -> None:
        self._stopping = True
        child = self._child
        if child is not None:
            await self._stop_child(child, reason)
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._first_done.set()
        self._set_state("disabled", _STOP_REASONS.get(reason))

    async def _spawn(self, handlers: dict[str, Handler], notification_handlers: dict[str, NotificationHandler]) -> _Child:
        spec = self._spec
        flags: dict[str, Any] = {}
        if sys.platform == "win32":  # pragma: no cover -- Windows only
            flags["creationflags"] = (
                _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
            )
        else:
            flags["start_new_session"] = True
        os.close(_open_log(spec.log_path))  # the folder, the rotation at start, mode 0600
        proc = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=spec.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
            limit=MAX_LINE_BYTES,
            **flags,
        )
        assert proc.stderr is not None  # nosec B101  # PIPE was requested
        pump = asyncio.create_task(_pump_stderr(proc.stderr, self._stderr_log, spec.name))
        self._pumps.add(pump)
        pump.add_done_callback(self._pumps.discard)
        child = _Child(proc, handlers, notification_handlers)
        child.stderr_pump = pump
        await child.peer.start()
        self._child = child
        logger.info("plugin %s: spawned pid %s", spec.name, proc.pid)
        return child

    async def _handshake(self, child: _Child, purpose: str) -> InitializeResult:
        params = self._initialize_params(purpose)
        try:
            raw = await child.peer.request("initialize", params)
        except RpcError as exc:
            if exc.code == "version_mismatch":
                raise _Fatal(REASON_MAJOR_MISMATCH) from None
            raise
        try:
            result = InitializeResult.from_wire(raw)
        except RpcError as exc:
            raise _Fatal(f"manifest invalid: {exc.detail}") from None
        if result.protocol_version.split(".")[0] != str(PROTOCOL_MAJOR):
            raise _Fatal(REASON_MAJOR_MISMATCH)
        expected = params.get("plugin", {})
        if (result.plugin_name, result.plugin_version) != (expected.get("name"), expected.get("manifest_version")):
            raise _Fatal(REASON_IDENTITY_DIFFERS)
        try:
            self._validate_tools(result)
        except Exception as exc:
            raise _Fatal(f"manifest invalid: {exc}") from None
        return result

    async def _run(self) -> None:
        try:
            while not self._stopping:
                outcome = await self._attempt()
                self._first_done.set()
                if outcome != "crashed":
                    return
                now = self._clock()
                self._crashes = [t for t in self._crashes if now - t < CRASH_WINDOW_SECONDS]
                self._crashes.append(now)
                count = len(self._crashes)
                if count >= CRASH_LIMIT:
                    minutes = int(CRASH_WINDOW_SECONDS // 60)
                    self._set_state("disabled", f"crashed {CRASH_LIMIT} times in {minutes} minutes")
                    return
                self._set_state("backoff", f"crashed {count} time{'s' if count != 1 else ''}")
                await self._sleep(RESTART_BACKOFF_SECONDS[min(count - 1, len(RESTART_BACKOFF_SECONDS) - 1)])
        finally:
            self._first_done.set()

    async def _attempt(self) -> str:
        """One spawn-to-exit cycle: ``"crashed"``, ``"fatal"`` or ``"stopped"``."""
        self._set_state("starting")
        child: _Child | None = None
        try:
            await self._check_before_spawn()
            child = await self._spawn(self._handlers, self._notification_handlers)
            result = await self._handshake(child, "run")
            await self._on_ready(result)
            if self._stopping:
                return "stopped"
            self._set_state("running")
            self._first_done.set()
            await child.gone.wait()
            if self._stopping:
                return "stopped"
            logger.warning("plugin %s: crashed (%s)", self._spec.name, child.gone_reason)
            return "crashed"
        except _Fatal as exc:
            if self._stopping:
                return "stopped"
            self._set_state("disabled", exc.reason)
            return "fatal"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._stopping:
                return "stopped"
            logger.warning("plugin %s: failed to start (%s)", self._spec.name, type(exc).__name__)
            return "crashed"
        finally:
            if child is not None:
                await self._reap(child)

    async def _check_before_spawn(self) -> None:
        """Run the ``before_spawn`` hook; any failure refuses the start for good."""
        if self._before_spawn is None:
            return
        try:
            await self._before_spawn()
        except StartError as exc:
            raise _Fatal(exc.reason) from None
        except Exception as exc:
            logger.warning("plugin %s: start check failed (%s)", self._spec.name, type(exc).__name__)
            raise _Fatal("could not start") from None

    @staticmethod
    async def _reap(child: _Child) -> None:
        """Make sure the process and its process group are gone and the peer closed.

        On POSIX the whole group is killed even when the plugin itself has already exited, so
        nothing it started outlives it.
        """
        if sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(child.proc.pid, signal.SIGKILL)
        elif child.proc.returncode is None:  # pragma: no cover -- Windows only
            _signal_child(child.proc, kill=True)
        await _wait_exit(child.proc)
        await child.exit_watch
        if child.stderr_pump is not None:
            await asyncio.wait({child.stderr_pump}, timeout=LOG_DRAIN_SECONDS)
        await child.peer.close()

    async def _stop_child(self, child: _Child, reason: str) -> None:
        peer = child.peer
        delivered = False
        if child.proc.returncode is None and not peer.closed:
            with contextlib.suppress(RpcError):
                if reason != "shutdown":
                    await peer.notify("plugin.disabling", {"reason": reason}, timeout=SHUTDOWN_NOTIFY_TIMEOUT_SECONDS)
                await peer.notify(
                    "shutdown", {"grace_ms": int(SHUTDOWN_GRACE_SECONDS * 1000)}, timeout=SHUTDOWN_NOTIFY_TIMEOUT_SECONDS
                )
                delivered = True
        # Not `peer.closed`: a plugin that exits cleanly on "shutdown" closes the peer too.
        if not delivered or not await self._exited(child, SHUTDOWN_GRACE_SECONDS):
            _signal_child(child.proc, kill=False)
            if not await self._exited(child, TERMINATE_GRACE_SECONDS):
                _signal_child(child.proc, kill=True)
        await self._reap(child)

    @staticmethod
    async def _exited(child: _Child, timeout: float) -> bool:
        return await _wait_exit(child.proc, timeout)
