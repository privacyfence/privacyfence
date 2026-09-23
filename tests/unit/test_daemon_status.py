"""Tests for privacyfence.daemon_status -- the local-mode-fixes plan's Phase
2 (companion-as-daemon-manager). See that module's own docstring for the
two-source probe order this exercises: the control channel first, each
platform's service manager (parsed from a fixed subprocess.CompletedProcess,
never a real launchctl/systemctl/sc.exe) as the fallback.
"""
from __future__ import annotations

import subprocess

from privacyfence import daemon_status
from privacyfence.web.control_channel import ControlChannelError


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestProbeControlChannel:
    def test_a_full_payload_reports_running(self, monkeypatch):
        monkeypatch.setattr(
            daemon_status, "request_status",
            lambda timeout=3.0: {"version": "4.2.0", "pid": 4242},
        )

        status = daemon_status.probe()

        assert status.state == "running"
        assert status.version == "4.2.0"
        assert status.pid == 4242
        assert "4.2.0" in status.detail
        assert "4242" in status.detail

    def test_no_daemon_listening_falls_through(self, monkeypatch):
        def _raise(timeout=3.0):
            raise OSError("no such socket")

        monkeypatch.setattr(daemon_status, "request_status", _raise)
        monkeypatch.setattr(daemon_status.privilege_separation, "platform_layout", lambda: None)

        status = daemon_status.probe()

        assert status.state == "unknown"

    def test_a_malformed_reply_falls_through_too(self, monkeypatch):
        def _raise(timeout=3.0):
            raise ControlChannelError("bad reply")

        monkeypatch.setattr(daemon_status, "request_status", _raise)
        monkeypatch.setattr(daemon_status.privilege_separation, "platform_layout", lambda: None)

        status = daemon_status.probe()

        assert status.state == "unknown"

    def test_a_missing_version_still_reports_running(self, monkeypatch):
        monkeypatch.setattr(daemon_status, "request_status", lambda timeout=3.0: {"pid": 99})

        status = daemon_status.probe()

        assert status.state == "running"
        assert status.version is None
        assert status.pid == 99


class TestProbeFallsBackToServiceManager:
    """probe() when the control channel doesn't answer -- covered generally
    here; each platform's own output parsing is covered by the
    TestMacosStatus/TestLinuxStatus/TestWindowsStatus classes below."""

    def test_no_daemon_and_no_platform_layout_is_unknown(self, monkeypatch):
        monkeypatch.setattr(daemon_status, "request_status", lambda timeout=3.0: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(daemon_status.privilege_separation, "platform_layout", lambda: None)

        status = daemon_status.probe()

        assert status.state == "unknown"
        assert "platform" in status.detail

    def test_a_failing_subprocess_call_is_unknown(self, monkeypatch, tmp_path):
        from privacyfence.privilege_separation import PlatformLayout

        monkeypatch.setattr(daemon_status, "request_status", lambda timeout=3.0: (_ for _ in ()).throw(OSError()))
        layout = PlatformLayout(
            system_root=tmp_path, service_account="x", service_group="x", installer="x",
            status_command="x", start_command="x", stop_command="x", enable_command="x",
            daemon_ctl_argv=("definitely-not-a-real-command-xyz",),
        )
        monkeypatch.setattr(daemon_status.privilege_separation, "platform_layout", lambda: layout)

        status = daemon_status.probe()

        assert status.state == "unknown"

    def test_dispatches_to_the_right_platform_parser(self, monkeypatch, tmp_path):
        from privacyfence.privilege_separation import PlatformLayout

        monkeypatch.setattr(daemon_status, "request_status", lambda timeout=3.0: (_ for _ in ()).throw(OSError()))
        layout = PlatformLayout(
            system_root=tmp_path, service_account="x", service_group="x", installer="x",
            status_command="x", start_command="x", stop_command="x", enable_command="x",
            daemon_ctl_argv=("true",),
        )
        monkeypatch.setattr(daemon_status.privilege_separation, "platform_layout", lambda: layout)
        monkeypatch.setattr(daemon_status, "_run_daemon_ctl", lambda argv: _result(returncode=1))
        monkeypatch.setattr(daemon_status.privilege_separation, "current_platform", lambda: "linux")

        status = daemon_status.probe()

        # _linux_status() on a returncode-1-but-no-properties result: not
        # active, not activating, not failed -> stopped.
        assert status.state == "stopped"


class TestMacosStatus:
    def test_not_loaded_is_stopped(self):
        assert daemon_status._macos_status(_result(returncode=1)).state == "stopped"

    def test_loaded_with_a_pid_is_unresponsive(self):
        result = _result(returncode=0, stdout="state = running\n\tpid = 4242\n")
        status = daemon_status._macos_status(result)
        assert status.state == "unresponsive"
        assert status.pid == 4242

    def test_loaded_with_no_pid_and_a_bad_exit_is_failed(self):
        result = _result(returncode=0, stdout="state = waiting\n\tlast exit code = 17\n")
        status = daemon_status._macos_status(result)
        assert status.state == "failed"
        assert "17" in status.detail

    def test_loaded_with_no_pid_and_a_clean_exit_is_starting(self):
        result = _result(returncode=0, stdout="state = waiting\n\tlast exit code = 0\n")
        assert daemon_status._macos_status(result).state == "starting"

    def test_loaded_with_no_pid_and_no_exit_info_is_starting(self):
        result = _result(returncode=0, stdout="state = waiting\n")
        assert daemon_status._macos_status(result).state == "starting"


class TestLinuxStatus:
    def _show(self, **properties: str) -> str:
        return "\n".join(f"{k}={v}" for k, v in properties.items())

    def test_active_with_a_pid_is_unresponsive(self):
        result = _result(stdout=self._show(ActiveState="active", SubState="running", MainPID="4242"))
        status = daemon_status._linux_status(result)
        assert status.state == "unresponsive"
        assert status.pid == 4242

    def test_activating_is_starting(self):
        result = _result(stdout=self._show(ActiveState="activating", MainPID="0"))
        assert daemon_status._linux_status(result).state == "starting"

    def test_inactive_is_stopped(self):
        result = _result(stdout=self._show(ActiveState="inactive", SubState="dead", MainPID="0", Result="success"))
        assert daemon_status._linux_status(result).state == "stopped"

    def test_a_failed_unit_is_failed(self):
        result = _result(stdout=self._show(ActiveState="failed", MainPID="0", ExecMainStatus="1", Result="exit-code"))
        status = daemon_status._linux_status(result)
        assert status.state == "failed"
        assert "1" in status.detail

    def test_a_nonzero_exec_status_with_inactive_state_is_failed(self):
        result = self._show(ActiveState="inactive", MainPID="0", ExecMainStatus="137", Result="signal")
        status = daemon_status._linux_status(_result(stdout=result))
        assert status.state == "failed"


class TestWindowsStatus:
    def test_service_missing_is_stopped(self):
        assert daemon_status._windows_status(_result(returncode=1060)).state == "stopped"

    def test_running_is_unresponsive(self):
        result = _result(stdout="        STATE              : 4  RUNNING\n")
        assert daemon_status._windows_status(result).state == "unresponsive"

    def test_start_pending_is_starting(self):
        result = _result(stdout="        STATE              : 2  START_PENDING\n")
        assert daemon_status._windows_status(result).state == "starting"

    def test_stopped_with_a_bad_exit_code_is_failed(self):
        result = _result(stdout="        STATE              : 1  STOPPED\n        WIN32_EXIT_CODE    : 17  (0x11)\n")
        status = daemon_status._windows_status(result)
        assert status.state == "failed"
        assert "17" in status.detail

    def test_stopped_cleanly_is_stopped(self):
        result = _result(stdout="        STATE              : 1  STOPPED\n        WIN32_EXIT_CODE    : 0  (0x0)\n")
        assert daemon_status._windows_status(result).state == "stopped"
