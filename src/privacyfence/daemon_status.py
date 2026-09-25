"""The companion as daemon manager (ADR 0026): read the daemon's run state
without asking for a password.

``docs/adr/0002-local-mode-trust-boundary-and-companion-app.md``'s Amendment
(2026, "the companion becomes the daemon manager") is the design this
module and ``service_control.py`` split in two, the same way ``daemon_
status.py``/``service_control.py`` are named: **status needs no
privileges, and only start/stop/restart do.** This module is the former --
it is what the companion's tray menu polls every few seconds, and what a
human reading ``privacyfence-app --status`` (or the platform script's own
``daemon status``) sees -- and it never shells out to anything that would
put a system password dialog on screen. ``service_control.run_elevated()``
is the latter, kept in its own module for exactly that reason: nothing
that merely wants to *know* the daemon's state should import a module that
also knows how to prompt for a password.

Two independent sources, tried in order, because a daemon that isn't
answering its control channel is a state this module has to describe, not
a failure to propagate:

1. **Ask the daemon itself** (``web/control_channel.py``'s ``STATUS``
   command). If it answers, the daemon is unambiguously
   ``running`` -- there is nothing more authoritative than the daemon
   saying so over its own control channel.
2. **Ask the platform's service manager**, unprivileged
   (``PlatformLayout.daemon_ctl_argv`` -- ``launchctl print``/``systemctl
   show``/``sc query``, none of which need root to read). This is what
   answers when the daemon is stopped, still starting, or has crashed --
   every case where there is no control channel to ask in the first place.

Deliberately *not* a single merged probe: a control-channel answer already
proves everything the service manager could only estimate (a live pid is
not the same claim as "this control socket answered a STATUS request"), so
skipping straight to ``running`` there is not an optimization, it is the
more precise answer.
"""
from __future__ import annotations

import logging
import subprocess  # nosec B404  # fixed argv from PlatformLayout.daemon_ctl_argv below, no shell
from dataclasses import dataclass
from typing import Literal

from . import privilege_separation
from .web.control_channel import ControlChannelError, request_status

logger = logging.getLogger(__name__)

DaemonState = Literal["running", "starting", "stopped", "failed", "unresponsive", "unknown"]

#: How long the unprivileged service-manager probe waits for
#: ``launchctl``/``systemctl``/``sc.exe`` to answer. All three are local,
#: synchronous reads of state the OS already holds in memory -- anything
#: slower than this means the command itself is hung, not that the answer
#: is still coming.
_PROBE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class DaemonStatus:
    """What ``probe()`` reports -- the companion tray menu's whole model of
    "is PrivacyFence running", and the pure input ``companion.py``'s menu-
    building function (kept separate and testable without pystray) renders
    from."""

    state: DaemonState
    version: str | None
    pid: int | None
    #: One human sentence: what a person reading the tray's "Service
    #: Details..." dialog, or a log line, should see. Always non-empty.
    detail: str


def _probe_control_channel() -> DaemonStatus | None:
    """The daemon's own answer, or None to fall back to the service
    manager -- covers both "no daemon is listening at all" (an ordinary,
    expected state everywhere else in this codebase that reads the control
    channel) and "this install predates STATUS" (an older daemon answers
    ``ERROR unknown command``, which ``request_status()`` raises on)."""
    try:
        payload = request_status(timeout=3.0)
    except (OSError, ControlChannelError):
        return None
    version = payload.get("version")
    pid = payload.get("pid")
    version_str = str(version) if version is not None else None
    pid_int = int(pid) if isinstance(pid, int) else None
    detail = (
        f"PrivacyFence {version_str} is running"
        f"{f' (pid {pid_int})' if pid_int is not None else ''}."
        if version_str
        else "PrivacyFence is running."
    )
    return DaemonStatus(state="running", version=version_str, pid=pid_int, detail=detail)


def _run_daemon_ctl(argv: tuple[str, ...]) -> "subprocess.CompletedProcess[str] | None":
    try:
        return subprocess.run(  # nosec B603  # fixed argv from PlatformLayout, no shell, no user input
            list(argv), capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not run %s to check PrivacyFence's status: %s", argv, exc)
        return None


def _macos_status(result: "subprocess.CompletedProcess[str]") -> DaemonStatus:
    """Parse ``launchctl print system/com.privacyfence.daemon``. Loaded jobs
    print a ``state = ...`` line and, while actually running, a ``pid =
    ...`` line; an unloaded job makes ``launchctl`` exit non-zero with
    "Could not find service" on stderr instead of printing either."""
    if result.returncode != 0:
        return DaemonStatus(state="stopped", version=None, pid=None, detail="PrivacyFence is not running.")
    output = result.stdout
    pid = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                pid = int(line[len("pid = "):].strip())
            except ValueError:  # pragma: no cover -- launchctl's own output format
                pid = None
            break
    last_exit_status = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("last exit code = "):
            try:
                last_exit_status = int(line[len("last exit code = "):].strip())
            except ValueError:  # pragma: no cover -- launchctl's own output format
                last_exit_status = None
            break
    if pid is not None:
        return DaemonStatus(
            state="unresponsive", version=None, pid=pid,
            detail=f"PrivacyFence (pid {pid}) is running but not answering its control channel.",
        )
    if last_exit_status not in (None, 0):
        return DaemonStatus(
            state="failed", version=None, pid=None,
            detail=f"PrivacyFence last exited with code {last_exit_status}.",
        )
    return DaemonStatus(state="starting", version=None, pid=None, detail="PrivacyFence is starting.")


def _linux_status(result: "subprocess.CompletedProcess[str]") -> DaemonStatus:
    """Parse ``systemctl show privacyfence-daemon.service -p
    ActiveState,SubState,Result,ExecMainStatus,MainPID``. ``systemctl show``
    always exits 0 and prints every requested property, even for a unit
    that has never existed (every value comes back empty in that case),
    which is why this reads the properties themselves rather than the exit
    code."""
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            properties[key] = value.strip()
    active_state = properties.get("ActiveState", "")
    main_pid = properties.get("MainPID", "0")
    pid = int(main_pid) if main_pid.isdigit() and main_pid != "0" else None
    if pid is not None and active_state == "active":
        return DaemonStatus(
            state="unresponsive", version=None, pid=pid,
            detail=f"PrivacyFence (pid {pid}) is running but not answering its control channel.",
        )
    if active_state == "activating":
        return DaemonStatus(state="starting", version=None, pid=None, detail="PrivacyFence is starting.")
    exec_status = properties.get("ExecMainStatus", "")
    result_property = properties.get("Result", "")
    if active_state == "failed" or (exec_status not in ("", "0") or result_property not in ("", "success")):
        detail = f"PrivacyFence's service last exited with status {exec_status or 'unknown'} ({result_property or 'unknown result'})."
        return DaemonStatus(state="failed", version=None, pid=None, detail=detail)
    return DaemonStatus(state="stopped", version=None, pid=None, detail="PrivacyFence is not running.")


def _windows_status(result: "subprocess.CompletedProcess[str]") -> DaemonStatus:
    """Parse ``sc.exe query PrivacyFence``. A service that was never
    installed makes ``sc.exe`` exit non-zero (1060, "service does not
    exist"), which reads the same as "stopped" here -- there is nothing for
    the companion to offer beyond Start either way."""
    if result.returncode != 0:
        return DaemonStatus(state="stopped", version=None, pid=None, detail="PrivacyFence is not running.")
    state_line = ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("STATE"):
            state_line = line
            break
    if "RUNNING" in state_line:
        return DaemonStatus(
            state="unresponsive", version=None, pid=None,
            detail="PrivacyFence's service is running but not answering its control channel.",
        )
    if "START_PENDING" in state_line or "STOP_PENDING" in state_line:
        return DaemonStatus(state="starting", version=None, pid=None, detail="PrivacyFence is starting.")
    exit_code = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("WIN32_EXIT_CODE"):
            digits = "".join(ch for ch in line.split(":", 1)[-1] if ch.isdigit())
            exit_code = int(digits) if digits else None
            break
    if exit_code not in (None, 0):
        return DaemonStatus(
            state="failed", version=None, pid=None,
            detail=f"PrivacyFence's service last exited with code {exit_code}.",
        )
    return DaemonStatus(state="stopped", version=None, pid=None, detail="PrivacyFence is not running.")


_SERVICE_MANAGER_PARSERS = {
    "darwin": _macos_status,
    "linux": _linux_status,
    "win32": _windows_status,
}


def probe() -> DaemonStatus:
    """The companion tray menu's whole picture of "is PrivacyFence
    running": try the control channel first (module docstring), and fall
    back to this platform's service manager -- unprivileged, exactly the
    reason this and ``service_control.py`` are two modules rather than
    one."""
    answered = _probe_control_channel()
    if answered is not None:
        return answered
    layout = privilege_separation.platform_layout()
    if layout is None:  # pragma: no cover -- SUPPORTED_PLATFORMS gates every real caller
        return DaemonStatus(
            state="unknown", version=None, pid=None,
            detail="No service-manager information is available on this platform.",
        )
    result = _run_daemon_ctl(layout.daemon_ctl_argv)
    if result is None:
        return DaemonStatus(
            state="unknown", version=None, pid=None,
            detail=f"Could not run '{' '.join(layout.daemon_ctl_argv)}' to check PrivacyFence's status.",
        )
    parser = _SERVICE_MANAGER_PARSERS[privilege_separation.current_platform()]
    return parser(result)
