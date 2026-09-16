"""Tests for privacyfence.companion -- #428 Phase 3 (ADR 0002). The tray
loop itself (_run_tray) needs a real display and pystray/Pillow, neither of
which this CI OS has (or, on Linux, ever will -- see companion.py's own
module docstring for why pystray isn't even a dependency here), so it's
untested the same way test_control_channel.py leaves the Windows
named-pipe half to a Windows-hosted run: real coverage of the pure
dispatch logic (_open_path/_quit_daemon/_run_action/main's argparse
wiring) plus a couple of true end-to-end runs against real
ControlChannelServer/CompanionChannelServer instances.
"""
from __future__ import annotations

import pytest

from privacyfence import companion
from privacyfence.web import control_channel as cc
from privacyfence.web.session_auth import BootstrapStore


class TestOpenPath:
    def test_returns_false_when_no_daemon_is_running(self, monkeypatch):
        monkeypatch.setattr(companion, "read_base_url", lambda: None)
        assert companion._open_path("/approvals") is False

    def test_mints_and_opens_the_link(self, monkeypatch):
        monkeypatch.setattr(companion, "read_base_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(companion, "mint_bootstrap_code", lambda: "abc123")
        opened = []
        monkeypatch.setattr(companion.webbrowser, "open", lambda url: opened.append(url) or True)

        assert companion._open_path("/approvals") is True
        assert opened == ["http://127.0.0.1:8765/approvals?bootstrap=abc123"]

    def test_returns_false_when_mint_fails(self, monkeypatch):
        monkeypatch.setattr(companion, "read_base_url", lambda: "http://127.0.0.1:8765")

        def _raise():
            raise cc.ControlChannelError("boom")

        monkeypatch.setattr(companion, "mint_bootstrap_code", _raise)
        opened = []
        monkeypatch.setattr(companion.webbrowser, "open", lambda url: opened.append(url) or True)

        assert companion._open_path("/approvals") is False
        assert opened == []


class TestQuitDaemon:
    def test_returns_true_on_success(self, monkeypatch):
        monkeypatch.setattr(companion, "request_quit", lambda: None)
        assert companion._quit_daemon() is True

    def test_returns_false_when_the_daemon_is_unreachable(self, monkeypatch):
        def _raise():
            raise OSError("no such socket")

        monkeypatch.setattr(companion, "request_quit", _raise)
        assert companion._quit_daemon() is False

    def test_returns_false_when_quit_is_declined(self, monkeypatch):
        def _raise():
            raise cc.ControlChannelError("quit is disabled")

        monkeypatch.setattr(companion, "request_quit", _raise)
        assert companion._quit_daemon() is False


class TestRunAction:
    def test_open_approvals_dispatches_to_open_path(self, monkeypatch):
        calls = []
        monkeypatch.setattr(companion, "_open_path", lambda path: calls.append(path) or True)
        assert companion._run_action(companion.ACTION_OPEN_APPROVALS) is True
        assert calls == ["/approvals"]

    def test_open_settings_dispatches_to_open_path(self, monkeypatch):
        calls = []
        monkeypatch.setattr(companion, "_open_path", lambda path: calls.append(path) or True)
        assert companion._run_action(companion.ACTION_OPEN_SETTINGS) is True
        assert calls == ["/settings"]

    def test_quit_dispatches_to_quit_daemon(self, monkeypatch):
        monkeypatch.setattr(companion, "_quit_daemon", lambda: True)
        assert companion._run_action(companion.ACTION_QUIT) is True

    def test_unknown_action_raises(self):
        with pytest.raises(ValueError):
            companion._run_action("bogus")


class TestMainArgvDispatch:
    def test_action_flag_runs_one_shot_and_exits(self, monkeypatch):
        monkeypatch.setattr(companion, "_run_action", lambda action: action == companion.ACTION_QUIT)
        assert companion.main(["--action", "quit"]) == 0

    def test_action_flag_reports_failure_via_exit_code(self, monkeypatch):
        monkeypatch.setattr(companion, "_run_action", lambda action: False)
        assert companion.main(["--action", "open-approvals"]) == 1

    def test_no_action_on_a_non_tray_platform_is_an_error(self, monkeypatch):
        monkeypatch.setattr(companion.sys, "platform", "linux")
        with pytest.raises(SystemExit) as exc_info:
            companion.main([])
        assert exc_info.value.code == 2

    def test_no_action_on_a_tray_platform_runs_the_tray(self, monkeypatch):
        monkeypatch.setattr(companion.sys, "platform", "darwin")
        monkeypatch.setattr(companion, "_run_tray", lambda: 0)
        assert companion.main([]) == 0


class TestCompanionEndToEnd:
    """A real ControlChannelServer (the daemon's) and a real
    CompanionChannelServer (the companion's), wired together exactly the
    way companion.py's own module-level functions reach them -- no
    monkeypatching of the control-channel layer itself, only of
    paths.data_dir() to sandbox both servers' addresses under tmp_path."""

    def test_open_path_against_a_real_daemon(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)

        server = cc.ControlChannelServer(bootstrap=BootstrapStore())
        server.start()
        (tmp_path / cc.WEB_BASE_URL_FILE_NAME).write_text("http://127.0.0.1:8765", encoding="utf-8")
        opened = []
        monkeypatch.setattr(companion.webbrowser, "open", lambda url: opened.append(url) or True)
        try:
            assert companion._open_path("/approvals") is True
            assert len(opened) == 1
            assert opened[0].startswith("http://127.0.0.1:8765/approvals?bootstrap=")
        finally:
            server.stop()

    def test_quit_against_a_real_daemon(self, tmp_path, monkeypatch):
        from privacyfence import daemon_main, paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        called = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: called.append(True))

        server = cc.ControlChannelServer(bootstrap=BootstrapStore())
        server.start()
        try:
            assert companion._quit_daemon() is True
            assert called == [True]
        finally:
            server.stop()

    def test_oauth_loopback_relays_through_a_real_companion_channel(self, tmp_path, monkeypatch):
        """The other direction: oauth_loopback.py's default opener asking a
        running companion to open a URL, exercised against a real
        CompanionChannelServer rather than a mocked request_open_url."""
        from privacyfence import oauth_loopback, paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        opened = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)

        server = cc.CompanionChannelServer()
        server.start()
        try:
            assert oauth_loopback._default_open_browser("https://example.com/callback") is True
            assert opened == ["https://example.com/callback"]
        finally:
            server.stop()
