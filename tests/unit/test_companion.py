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

from types import SimpleNamespace

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


class _SilentChannel:
    address = "/run/user/1000/privacyfence/companion.sock"

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class TestPendingSeparation:
    """ADR 0003 decision 3's second half. The companion is the only
    PrivacyFence process running inside a real login session as the person
    whose group membership is missing, which is what makes "nobody was
    logged in at install time" a pending step rather than a permanent one."""

    @pytest.fixture
    def never_elevates(self, monkeypatch):
        def _unexpected(user=None):
            raise AssertionError("elevated with nothing pending")

        monkeypatch.setattr(
            companion.privilege_separation, "complete_per_user_separation", _unexpected
        )

    def test_an_install_with_nothing_pending_prompts_nobody(self, monkeypatch, never_elevates):
        # The ordinary case at every companion start after the first -- so it
        # has to cost nothing and, above all, must not elevate.
        monkeypatch.setattr(
            companion.privilege_separation, "separation",
            lambda: SimpleNamespace(service_group="privacyfence"),
        )
        monkeypatch.setattr(
            companion.privilege_separation, "owner_membership_pending", lambda: False
        )

        assert companion._complete_pending_separation() is None

    def test_an_unseparated_install_prompts_nobody(self, monkeypatch, never_elevates):
        # Decision 6's problem, not this one's: there is no group to join and
        # no marker to complete, so there is nothing here to ask about.
        monkeypatch.setattr(companion.privilege_separation, "separation", lambda: None)
        monkeypatch.setattr(
            companion.privilege_separation, "owner_membership_pending", lambda: True
        )

        assert companion._complete_pending_separation() is None

    def test_a_pending_install_is_completed_and_the_next_step_named(self, monkeypatch, caplog):
        monkeypatch.setattr(
            companion.privilege_separation, "owner_membership_pending", lambda: True
        )
        monkeypatch.setattr(
            companion.privilege_separation, "complete_per_user_separation", lambda: True
        )
        monkeypatch.setattr(companion.privilege_separation, "current_user_name", lambda: "alice")
        monkeypatch.setattr(
            companion.privilege_separation, "separation",
            lambda: SimpleNamespace(service_group="privacyfence"),
        )

        with caplog.at_level("WARNING"):
            companion._complete_pending_separation()

        # The one step no elevation can take: group membership is evaluated
        # when a session is created, so this session will never see it.
        assert "log out and back in" in caplog.text
        assert "alice" in caplog.text
        assert "privacyfence" in caplog.text

    def test_a_declined_prompt_says_nothing_more(self, monkeypatch, caplog):
        # privilege_separation has already logged why and what to type; a
        # second line here telling somebody who just cancelled to log out
        # would be wrong as well as noisy.
        monkeypatch.setattr(
            companion.privilege_separation, "separation",
            lambda: SimpleNamespace(service_group="privacyfence"),
        )
        monkeypatch.setattr(
            companion.privilege_separation, "owner_membership_pending", lambda: True
        )
        monkeypatch.setattr(
            companion.privilege_separation, "complete_per_user_separation", lambda: False
        )

        with caplog.at_level("WARNING"):
            companion._complete_pending_separation()

        assert "log out and back in" not in caplog.text

    def test_the_check_runs_off_the_startup_path(self, monkeypatch):
        # A password dialog nobody answers must not hold up the tray icon
        # appearing or the companion channel binding -- the same reason
        # maybe_auto_enable_macos() uses a thread.
        started = []

        class _Thread:
            def __init__(self, **kwargs):
                started.append(kwargs)

            def start(self):
                started.append("started")

        monkeypatch.setattr(companion.threading, "Thread", _Thread)

        companion._start_pending_separation_check()

        assert started[0]["target"] is companion._first_run_checks
        assert started[0]["daemon"] is True
        assert started[-1] == "started"

    def test_serve_starts_the_check(self, monkeypatch):
        # --serve is what a separated Linux install autostarts, which makes
        # it the shape that has to carry this.
        calls = []
        monkeypatch.setattr(
            companion, "_start_pending_separation_check", lambda: calls.append(True)
        )
        monkeypatch.setattr(companion, "CompanionChannelServer", _SilentChannel)

        assert companion._run_serve(wait=lambda: None) == 0
        assert calls == [True]


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


class TestRecoveryCodeAction:
    """Plan item 1.3's "re-presented by the companion" half. The code itself
    never travels back over this call -- the daemon calls back into this
    process's own channel to put it on screen -- so all this action can do
    is succeed or explain why it didn't."""

    def test_asks_the_daemon_and_reports_success(self, monkeypatch):
        asked = []
        monkeypatch.setattr(companion, "request_recovery_code", lambda: asked.append("asked"))
        assert companion._show_recovery_code() is True
        assert asked == ["asked"]

    def test_a_refusal_is_logged_not_raised(self, monkeypatch, caplog):
        def _raise():
            raise cc.ControlChannelError("issuing a new recovery code was denied")

        monkeypatch.setattr(companion, "request_recovery_code", _raise)
        with caplog.at_level("ERROR"):
            assert companion._show_recovery_code() is False
        assert "denied" in caplog.text

    def test_no_daemon_running_is_a_plain_false(self, monkeypatch):
        def _raise():
            raise OSError("no such socket")

        monkeypatch.setattr(companion, "request_recovery_code", _raise)
        assert companion._show_recovery_code() is False

    def test_the_action_is_dispatchable(self, monkeypatch):
        monkeypatch.setattr(companion, "_show_recovery_code", lambda: True)
        assert companion._run_action(companion.ACTION_RECOVERY_CODE) is True

    def test_argparse_accepts_it(self, monkeypatch):
        # The Linux Desktop Action for this (resources/linux/privacyfence-
        # companion.desktop) runs exactly this argv.
        seen = []
        monkeypatch.setattr(companion, "_run_action", lambda action: seen.append(action) or True)
        assert companion.main(["--action", "recovery-code"]) == 0
        assert seen == [companion.ACTION_RECOVERY_CODE]


class TestFirstEnrollmentOffer:
    """Plan item 1.2: the other half of defaulting step-up on for packaged
    installs. A fresh install requires a passkey it does not have, which is
    a safe state (nothing is approved) but not a usable one, and nobody is
    looking at a page they have no reason to open."""

    def test_a_pending_enrollment_opens_the_security_page(self, monkeypatch, caplog):
        monkeypatch.setattr(companion, "enrollment_state", lambda: "pending")
        opened = []
        monkeypatch.setattr(companion, "_open_path", lambda path: opened.append(path) or True)

        with caplog.at_level("WARNING"):
            companion._offer_first_enrollment()

        assert opened == ["/security"]
        # The log line has to stand on its own for anyone reading it without
        # the browser tab in front of them.
        assert "no approval can be released" in caplog.text

    def test_an_install_with_a_passkey_opens_nothing(self, monkeypatch):
        monkeypatch.setattr(companion, "enrollment_state", lambda: "ok")

        def _unexpected(path):
            raise AssertionError(f"opened {path} with nothing pending")

        monkeypatch.setattr(companion, "_open_path", _unexpected)
        companion._offer_first_enrollment()

    def test_a_daemon_that_is_still_starting_is_retried(self, monkeypatch):
        # A packaged install starts its daemon as a system service and this
        # process from the user's own session: the two race at every login.
        answers = [OSError("not yet"), OSError("not yet"), "pending"]
        slept = []
        monkeypatch.setattr(companion.time, "sleep", lambda seconds: slept.append(seconds))

        def _state():
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(companion, "enrollment_state", _state)
        opened = []
        monkeypatch.setattr(companion, "_open_path", lambda path: opened.append(path) or True)

        companion._offer_first_enrollment()

        assert opened == ["/security"]
        assert slept == [companion._DAEMON_WAIT_SECONDS] * 2

    def test_a_daemon_that_never_arrives_is_left_for_the_next_login(self, monkeypatch):
        def _raise():
            raise OSError("no such socket")

        monkeypatch.setattr(companion.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(companion, "enrollment_state", _raise)

        def _unexpected(path):
            raise AssertionError(f"opened {path} with no daemon running")

        monkeypatch.setattr(companion, "_open_path", _unexpected)
        companion._offer_first_enrollment()

    def test_a_daemon_that_cannot_answer_is_not_worth_a_browser_tab(self, monkeypatch):
        # An older daemon, or one with no step-up config behind its control
        # channel, answers ERROR. Nothing to act on from this side.
        def _raise():
            raise cc.ControlChannelError("enrollment state is not available on this install")

        monkeypatch.setattr(companion, "enrollment_state", _raise)

        def _unexpected(path):
            raise AssertionError(f"opened {path} on an ERROR reply")

        monkeypatch.setattr(companion, "_open_path", _unexpected)
        companion._offer_first_enrollment()

    def test_enrollment_is_not_offered_while_group_membership_is_pending(self, monkeypatch):
        # Group membership is evaluated when a session is created, so until
        # the human logs out and back in they cannot reach the web UI at all
        # -- opening /security would be opening a page that cannot load.
        monkeypatch.setattr(companion, "_complete_pending_separation", lambda: None)
        monkeypatch.setattr(
            companion.privilege_separation, "owner_membership_pending", lambda: True
        )

        def _unexpected():
            raise AssertionError("asked about enrollment before the relog")

        monkeypatch.setattr(companion, "_offer_first_enrollment", _unexpected)
        companion._first_run_checks()

    def test_separation_is_settled_before_enrollment_is_offered(self, monkeypatch):
        order = []
        monkeypatch.setattr(
            companion, "_complete_pending_separation", lambda: order.append("separation"),
        )
        monkeypatch.setattr(
            companion.privilege_separation, "owner_membership_pending", lambda: False
        )
        monkeypatch.setattr(companion, "_offer_first_enrollment", lambda: order.append("enrollment"))

        companion._first_run_checks()

        assert order == ["separation", "enrollment"]
