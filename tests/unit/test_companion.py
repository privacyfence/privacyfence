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

import sys

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

    def test_serve_flag_runs_the_channel_on_a_non_tray_platform(self, monkeypatch):
        # #428 Phase 4 (B5b): what the XDG autostart entry a separated Linux
        # install writes actually runs. Without it there is no persistent
        # process in the user's session for a service-hosted daemon to hand a
        # connector OAuth URL to.
        monkeypatch.setattr(companion.sys, "platform", "linux")
        monkeypatch.setattr(companion, "_run_serve", lambda: 0)
        assert companion.main(["--serve"]) == 0

    def test_serve_reports_a_channel_that_could_not_bind(self, monkeypatch):
        # A non-zero exit rather than a process that sits there looking
        # started: systemd/the desktop session is the only thing watching an
        # autostarted --serve, and "up but deaf" would present as connector
        # OAuth silently never opening a browser.
        class _DeafChannel:
            address = None

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

        monkeypatch.setattr(companion, "CompanionChannelServer", _DeafChannel)
        assert companion._run_serve(wait=lambda: None) == 1

    def test_serve_and_action_together_are_refused(self, monkeypatch):
        # One runs and exits, the other stays up forever. Silently picking
        # either would make a mis-written .desktop Exec= look like it worked.
        with pytest.raises(SystemExit) as exc_info:
            companion.main(["--serve", "--action", "quit"])
        assert exc_info.value.code == 2


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="forces paths.is_windows() to False to exercise the POSIX socket path "
    "deterministically -- see test_control_channel.py's own module-level skip for why that "
    "needs a real AF_UNIX, which this CI OS's Python doesn't expose at all",
)
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

    def test_serve_is_that_same_channel_and_nothing_else(self, tmp_path, monkeypatch):
        """#428 Phase 4 (B5b): `privacyfence-companion --serve` run against a
        real daemon-side caller. This is the whole of what a separated Linux
        install autostarts -- no tray, no menu -- and the thing it has to
        deliver is precisely the relay the previous test exercises, so assert
        it through the same path rather than by inspecting the server object.

        ``wait`` is the injectable seam ``_run_serve`` grows for exactly this:
        a real run blocks on an Event nothing ever sets, so the test supplies
        the body that runs while the channel is up."""
        from privacyfence import oauth_loopback, paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        opened = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)

        def while_serving() -> None:
            assert oauth_loopback._default_open_browser("https://example.com/callback") is True

        assert companion._run_serve(wait=while_serving) == 0
        assert opened == ["https://example.com/callback"]
        # And tore the socket back down on the way out, rather than leaving a
        # stale node for the next login's autostart to trip over.
        assert not cc.companion_socket_path().exists()

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
