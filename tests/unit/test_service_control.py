"""Tests for privacyfence.service_control -- the local-mode-fixes plan's
Phase 2 (companion-as-daemon-manager): the elevated half of daemon
management, run_elevated(). Every real subprocess call (osascript/pkexec/
the Windows UAC relaunch) is stubbed; nothing here spawns a real process or
puts a password dialog on screen -- see privilege_separation.py's own
TestPerUserElevationCommand for the sibling coverage of the argv builders
this module shares with `enable --for-user`.
"""
from __future__ import annotations

import shlex
import subprocess

import pytest

from privacyfence import privilege_separation, service_control


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def separated(monkeypatch, tmp_path):
    """A minimal separated install, with a script that passes the
    elevation-safety check -- what every test below needs before it can
    reach the platform-specific argv building."""
    script = tmp_path / "macos_privilege_separation.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
    monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: script)
    monkeypatch.setattr(privilege_separation, "_elevation_script_problem", lambda s: None)
    return script


class TestNotEligibleToElevate:
    def test_an_unseparated_install_never_shells_out(self, monkeypatch, tmp_path):
        script = tmp_path / "macos_privilege_separation.sh"
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(privilege_separation, "marker_unreadable", lambda: False)
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: script)
        monkeypatch.setattr(
            service_control.subprocess, "run", lambda *a, **k: pytest.fail("must not run anything")
        )

        ok, detail = service_control.run_elevated("start")

        assert ok is False
        assert "isn't set up on this machine" in detail
        # Names the command that finishes the install, with this install's
        # own script -- not a dead end.
        assert f"sudo {shlex.quote(str(script))} enable" in detail

    def test_windows_names_the_elevated_powershell_command(self, monkeypatch, tmp_path):
        script = tmp_path / "privilege-separation.ps1"
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: script)

        detail = service_control._unseparated_detail()  # noqa: SLF001

        assert f'-File "{script}" enable' in detail
        assert "elevated PowerShell" in detail

    def test_with_no_script_on_disk_falls_back_to_the_documented_command(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: None)

        detail = service_control._unseparated_detail()  # noqa: SLF001

        assert detail.endswith(privilege_separation.PLATFORM_LAYOUTS["linux"].enable_command)

    def test_with_no_layout_at_all_says_so_without_a_command(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: None)
        monkeypatch.setattr(privilege_separation, "platform_layout", lambda: None)

        detail = service_control._unseparated_detail()  # noqa: SLF001

        assert "isn't set up on this machine" in detail
        assert "run:" not in detail

    def test_an_unreadable_marker_is_still_a_separated_install(self, monkeypatch, separated):
        # A separated root whose mode drifted to 0700 hides its own marker
        # from the logged-in user. That is not an unseparated install, and
        # Start/Restart/Stop must still reach the service.
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(privilege_separation, "marker_unreadable", lambda: True)
        monkeypatch.setattr(service_control.subprocess, "run", lambda *a, **k: _result(returncode=0))

        ok, detail = service_control.run_elevated("restart")

        assert ok is True
        assert "restarted" in detail

    def test_no_provisioning_script_refuses(self, monkeypatch):
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: None)
        monkeypatch.setattr(
            service_control.subprocess, "run", lambda *a, **k: pytest.fail("must not run anything")
        )

        ok, detail = service_control.run_elevated("start")

        assert ok is False
        assert "provisioning script" in detail

    def test_an_untrusted_script_refuses(self, monkeypatch, tmp_path):
        script = tmp_path / "s.sh"
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(privilege_separation, "installer_script_path", lambda: script)
        monkeypatch.setattr(
            privilege_separation, "_elevation_script_problem", lambda s: "is owned by uid 501, not root"
        )
        monkeypatch.setattr(
            service_control.subprocess, "run", lambda *a, **k: pytest.fail("must not run anything")
        )

        ok, detail = service_control.run_elevated("start")

        assert ok is False
        assert "not root" in detail


class TestMacosElevation:
    def test_runs_daemon_action_via_osascript(self, monkeypatch, separated):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        seen = {}

        def _run(argv, **kwargs):
            seen["argv"] = argv
            return _result(returncode=0)

        monkeypatch.setattr(service_control.subprocess, "run", _run)

        ok, detail = service_control.run_elevated("restart")

        assert ok is True
        assert "restarted" in detail
        assert seen["argv"][0] == privilege_separation._OSASCRIPT
        # Compare through the same two quoting layers production goes
        # through (shlex for the shell command, then AppleScript's own
        # string-literal escaping on top) rather than against the raw path
        # -- on a runner where tmp_path renders with backslashes, AppleScript
        # quoting doubles them, so a bare shlex.quote(str(separated)) is not
        # a substring of the final argument.
        expected_command = f"{shlex.quote(str(separated))} daemon restart"
        assert privilege_separation._applescript_quoted(expected_command) in seen["argv"][-1]  # noqa: SLF001
        assert "with administrator privileges" in seen["argv"][-1]
        assert "needs an administrator password to restart" in seen["argv"][-1]

    def test_a_declined_prompt_reports_cancelled(self, monkeypatch, separated):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setattr(
            service_control.subprocess, "run",
            lambda *a, **k: _result(returncode=1, stderr="execution error: User canceled. (-128)"),
        )

        ok, detail = service_control.run_elevated("stop")

        assert ok is False
        assert detail == "cancelled"

    def test_a_real_failure_reports_its_detail(self, monkeypatch, separated):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "darwin")
        monkeypatch.setattr(
            service_control.subprocess, "run",
            lambda *a, **k: _result(returncode=1, stderr="something else went wrong"),
        )

        ok, detail = service_control.run_elevated("stop")

        assert ok is False
        assert detail != "cancelled"
        assert "something else went wrong" in detail


class TestLinuxElevation:
    def test_runs_daemon_action_via_pkexec(self, monkeypatch, separated):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: "/usr/bin/pkexec")
        seen = {}

        def _run(argv, **kwargs):
            seen["argv"] = argv
            return _result(returncode=0)

        monkeypatch.setattr(service_control.subprocess, "run", _run)

        ok, detail = service_control.run_elevated("start")

        assert ok is True
        assert seen["argv"] == ["/usr/bin/pkexec", str(separated), "daemon", "start"]

    def test_no_pkexec_refuses_without_shelling_out(self, monkeypatch, separated):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            service_control.subprocess, "run", lambda *a, **k: pytest.fail("must not run anything")
        )

        ok, detail = service_control.run_elevated("start")

        assert ok is False
        assert "pkexec" in detail
        assert f"sudo {shlex.quote(str(separated))} daemon start" in detail

    def test_a_dismissed_polkit_dialog_reports_cancelled(self, monkeypatch, separated):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(privilege_separation.shutil, "which", lambda name: "/usr/bin/pkexec")
        monkeypatch.setattr(service_control.subprocess, "run", lambda *a, **k: _result(returncode=126))

        ok, detail = service_control.run_elevated("stop")

        assert ok is False
        assert detail == "cancelled"


class TestWindowsElevation:
    def test_runs_daemon_action_via_the_shared_runas_argv_builder(self, monkeypatch, separated):
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "win32")
        seen = {}

        def _fake_runas_argv(script, script_arguments, transcript):
            seen["script"] = script
            seen["script_arguments"] = script_arguments
            return ["powershell.exe", "-Command", "whatever"]

        monkeypatch.setattr(privilege_separation, "_windows_runas_argv", _fake_runas_argv)
        monkeypatch.setattr(service_control.subprocess, "run", lambda *a, **k: _result(returncode=0))

        ok, detail = service_control.run_elevated("start")

        assert ok is True
        assert seen["script"] == separated
        assert seen["script_arguments"] == "daemon start"


class TestActionArgvNeverEscapesTheAllowedSet:
    def test_only_start_stop_restart_are_accepted_by_type(self):
        # DaemonAction is a Literal["start", "stop", "restart"] -- this is a
        # documentation test, not a runtime guard: mypy is what actually
        # enforces it (this module deliberately has no runtime validation of
        # `action`, matching every other Literal-typed internal parameter in
        # this codebase).
        import typing

        assert typing.get_args(service_control.DaemonAction) == ("start", "stop", "restart")
