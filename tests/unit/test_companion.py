"""Tests for privacyfence.companion -- #428 Phase 3 (ADR 0002). Real
coverage of the pure dispatch logic (_open_path/_quit_daemon/_run_action/
main's argparse wiring) plus a couple of true end-to-end runs against real
ControlChannelServer/CompanionChannelServer instances.

*Running* the tray loop still needs a real display and pystray/Pillow,
neither of which this CI OS has (or, on Linux, ever will -- see
companion.py's own module docstring for why pystray isn't even a dependency
here). What _run_tray *wires up* does not: TestTrayLoop below stubs both at
the deferred import _run_tray does itself, and exercises the menu, the
channel's setup/teardown and the Quit ordering. That stopped being optional
when the self-approval plan added two behaviours to that function -- Phase
1's recovery-code item and Phase 2's attested _open_path -- neither of which
anything else reaches.
"""
from __future__ import annotations

import sys
import threading

import pytest

from types import SimpleNamespace

from privacyfence import companion
from privacyfence.web import control_channel as cc
from privacyfence.web.session_auth import BootstrapStore


class TestOpenPath:
    """Three shapes, since the self-approval plan's Phase 2 -- see
    ``_open_path``'s own docstring. Which one runs depends on who owns the
    companion channel, because that is the process the daemon calls back to
    before it will mint a session that can approve."""

    @pytest.fixture(autouse=True)
    def _no_companion_listening(self, monkeypatch):
        """The default for the cases below: nothing else is running, so the
        delegation step finds no companion and falls through. Set explicitly
        rather than left to a real connect attempt against whatever socket
        this machine happens to have."""
        monkeypatch.setattr(companion, "request_show", lambda path: False)
        monkeypatch.setattr(companion._channel_running, "is_set", lambda: False)

    def test_this_process_owning_the_channel_mints_an_attested_link_itself(self, monkeypatch):
        monkeypatch.setattr(companion._channel_running, "is_set", lambda: True)
        calls = []
        monkeypatch.setattr(companion, "open_attested_url", lambda path: (bool(calls.append(path)) or True, ""))
        # Nothing else may be reached on this path: an attested mint is the
        # whole point, and quietly falling back to an unattested one would
        # hand back a session whose Approve buttons refuse.
        monkeypatch.setattr(companion, "mint_bootstrap_code", lambda: pytest.fail("minted unattested"))

        assert companion._open_path("/approvals") is True
        assert calls == ["/approvals"]

    def test_a_failure_to_mint_attested_is_reported_not_downgraded(self, monkeypatch):
        monkeypatch.setattr(companion._channel_running, "is_set", lambda: True)
        monkeypatch.setattr(companion, "open_attested_url", lambda path: (False, "could not open a browser"))
        assert companion._open_path("/approvals") is False

    def test_a_one_shot_click_hands_the_job_to_a_running_companion(self, monkeypatch):
        """Linux's applications-menu click (ADR 0002 decision 4): this
        process exits too soon to answer the daemon's call-back, so the
        autostarted ``--serve`` process does the minting and the opening."""
        shown = []
        monkeypatch.setattr(companion, "request_show", lambda path: bool(shown.append(path)) or True)
        monkeypatch.setattr(companion, "mint_bootstrap_code", lambda: pytest.fail("minted unattested"))

        assert companion._open_path("/approvals") is True
        assert shown == ["/approvals"]

    def test_with_no_companion_at_all_the_link_still_signs_in_to_look(self, monkeypatch, caplog):
        monkeypatch.setattr(companion, "read_base_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(companion, "mint_bootstrap_code", lambda: "abc123")
        opened = []
        monkeypatch.setattr(companion.webbrowser, "open", lambda url: opened.append(url) or True)

        with caplog.at_level("WARNING"):
            assert companion._open_path("/approvals") is True

        assert opened == ["http://127.0.0.1:8765/approvals?bootstrap=abc123"]
        # Said out loud rather than discovered at the Approve button.
        assert "not approve" in caplog.text

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

    def test_service_status_dispatches_to_show_service_status(self, monkeypatch):
        monkeypatch.setattr(companion, "_show_service_status", lambda: True)
        assert companion._run_action(companion.ACTION_SERVICE_STATUS) is True

    @pytest.mark.parametrize(
        "action_const,expected_daemon_action",
        [
            ("ACTION_SERVICE_START", "start"),
            ("ACTION_SERVICE_RESTART", "restart"),
            ("ACTION_SERVICE_STOP", "stop"),
        ],
    )
    def test_service_actions_dispatch_with_the_right_verb(self, monkeypatch, action_const, expected_daemon_action):
        calls = []
        monkeypatch.setattr(companion, "_run_service_action", lambda action: calls.append(action) or True)
        assert companion._run_action(getattr(companion, action_const)) is True
        assert calls == [expected_daemon_action]

    def test_unknown_action_raises(self):
        with pytest.raises(ValueError):
            companion._run_action("bogus")


class TestShowMessage:
    def test_shows_the_message_via_the_platform_dialog(self, monkeypatch):
        shown = []
        monkeypatch.setattr(companion, "_dialog_for", lambda kind: (lambda text, timeout: shown.append((text, timeout)) or True))
        assert companion._show_message("hello") is True
        assert shown == [("hello", 15.0)]

    def test_no_dialog_program_is_a_logged_false(self, monkeypatch, caplog):
        def _raise(kind):
            def _unavailable(text, timeout):
                raise companion._NoDialogAvailable("no zenity/kdialog")
            return _unavailable

        monkeypatch.setattr(companion, "_dialog_for", _raise)
        with caplog.at_level("WARNING"):
            assert companion._show_message("hello") is False
        assert "hello" in caplog.text


class TestShowServiceStatus:
    def test_shows_the_probed_detail(self, monkeypatch):
        status = companion.daemon_status.DaemonStatus(
            state="running", version="4.2.0", pid=1, detail="PrivacyFence 4.2.0 is running (pid 1).",
        )
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: status)
        shown = []
        monkeypatch.setattr(companion, "_show_message", lambda text: shown.append(text) or True)

        assert companion._show_service_status() is True
        assert shown == [status.detail]


class TestRunServiceAction:
    def test_success_shows_the_outcome(self, monkeypatch):
        monkeypatch.setattr(companion.service_control, "run_elevated", lambda action: (True, "PrivacyFence's background service was started."))
        shown = []
        monkeypatch.setattr(companion, "_show_message", lambda text: shown.append(text) or True)

        assert companion._run_service_action("start") is True
        assert shown == ["PrivacyFence's background service was started."]

    def test_failure_shows_the_reason(self, monkeypatch):
        monkeypatch.setattr(companion.service_control, "run_elevated", lambda action: (False, "could not stop PrivacyFence's service: boom"))
        shown = []
        monkeypatch.setattr(companion, "_show_message", lambda text: shown.append(text) or True)

        assert companion._run_service_action("stop") is False
        assert shown == ["could not stop PrivacyFence's service: boom"]

    def test_a_cancelled_prompt_shows_nothing(self, monkeypatch):
        # A human who just clicked Cancel on the password prompt does not
        # need a second dialog telling them so.
        monkeypatch.setattr(companion.service_control, "run_elevated", lambda action: (False, "cancelled"))
        monkeypatch.setattr(companion, "_show_message", lambda text: pytest.fail("must not show anything"))

        assert companion._run_service_action("restart") is False


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

    def test_a_different_accounts_pending_join_is_completed_too(self, monkeypatch, caplog):
        # ADR 0008 retired the local-mode-fixes plan's Phase 2 §2.6 interim
        # guard: a non-owner account pending join is completed exactly like
        # the owner's own always was -- each gets their own isolated
        # principal, so there is no longer anything to refuse.
        monkeypatch.setattr(
            companion.privilege_separation, "separation",
            lambda: SimpleNamespace(service_group="privacyfence"),
        )
        monkeypatch.setattr(
            companion.privilege_separation, "owner_membership_pending", lambda: True
        )
        monkeypatch.setattr(
            companion.privilege_separation, "complete_per_user_separation", lambda: True
        )
        monkeypatch.setattr(companion.privilege_separation, "current_user_name", lambda: "bob")

        with caplog.at_level("WARNING"):
            companion._complete_pending_separation()

        assert "log out and back in" in caplog.text
        assert "bob" in caplog.text

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


class TestMenuModel:
    """``_menu_model()``/``_status_line_text()`` -- pystray-free on purpose
    (the plan's own testing note: test the pure function, don't drive
    pystray). ``TestTrayLoop`` below still exercises the real
    ``pystray.MenuItem`` wiring end to end, but every state/visibility rule
    itself is checked here directly."""

    def _status(self, state, **overrides):
        defaults = {"version": None, "pid": None, "detail": "detail"}
        defaults.update(overrides)
        return companion.daemon_status.DaemonStatus(state=state, **defaults)

    @pytest.mark.parametrize(
        "state,symbol",
        [
            ("running", "●"),
            ("starting", "●"),
            ("unresponsive", "⚠"),
            ("failed", "⚠"),
            ("stopped", "○"),
            ("unknown", "○"),
        ],
    )
    def test_status_line_symbol_per_state(self, state, symbol):
        text = companion._status_line_text(self._status(state))
        assert text.startswith(symbol)

    def test_status_line_includes_the_version_when_known(self):
        text = companion._status_line_text(self._status("running", version="4.2.0"))
        assert "(v4.2.0)" in text

    def test_status_line_omits_the_version_when_unknown(self):
        text = companion._status_line_text(self._status("stopped"))
        assert "(v" not in text

    @pytest.mark.parametrize("state", ["running", "starting", "unresponsive"])
    def test_running_states_hide_start_and_offer_restart_stop(self, state):
        rows = {row.action: row for row in companion._menu_model(self._status(state)) if row.action}
        assert rows[companion.ACTION_SERVICE_START].visible is False
        assert rows[companion.ACTION_SERVICE_RESTART].visible is True
        assert rows[companion.ACTION_SERVICE_STOP].visible is True

    @pytest.mark.parametrize("state", ["stopped", "failed", "unknown"])
    def test_non_running_states_offer_start_and_hide_restart_stop(self, state):
        rows = {row.action: row for row in companion._menu_model(self._status(state)) if row.action}
        assert rows[companion.ACTION_SERVICE_START].visible is True
        assert rows[companion.ACTION_SERVICE_RESTART].visible is False
        assert rows[companion.ACTION_SERVICE_STOP].visible is False

    def test_every_row_but_the_status_line_is_always_enabled_and_actionable(self):
        rows = companion._menu_model(self._status("running"))
        assert rows[0].action is None
        assert rows[0].enabled is False
        for row in rows[1:]:
            assert row.action is not None
            assert row.enabled is True

    def test_the_menu_covers_every_action_exactly_once(self):
        rows = companion._menu_model(self._status("running"))
        actions = [row.action for row in rows if row.action is not None]
        assert actions == [
            companion.ACTION_OPEN_APPROVALS, companion.ACTION_OPEN_SETTINGS,
            companion.ACTION_SERVICE_START, companion.ACTION_SERVICE_RESTART, companion.ACTION_SERVICE_STOP,
            companion.ACTION_SERVICE_STATUS, companion.ACTION_RECOVERY_CODE, companion.ACTION_QUIT,
        ]


class TestNotificationDecision:
    """``_notification_decision()`` -- the tray poll's and ``--serve``'s
    shared rule for when to show "PrivacyFence isn't running"."""

    def _status(self, state):
        return companion.daemon_status.DaemonStatus(state=state, version=None, pid=None, detail="")

    def test_a_healthy_state_never_notifies(self):
        state = companion._NotificationState()
        assert companion._notification_decision(
            state, self._status("running"), now=1000.0, started_at=0.0,
        ) is False

    def test_suppressed_within_the_first_minute_of_the_companions_own_start(self):
        state = companion._NotificationState()
        assert companion._notification_decision(
            state, self._status("stopped"), now=30.0, started_at=0.0,
        ) is False

    def test_suppressed_until_the_bad_state_has_held_for_fifteen_seconds(self):
        state = companion._NotificationState()
        started_at = 0.0
        # First bad poll, well past start-up suppression: records bad_since,
        # does not yet notify.
        assert companion._notification_decision(
            state, self._status("stopped"), now=100.0, started_at=started_at,
        ) is False
        assert state.bad_since == 100.0
        # Still under fifteen seconds since bad_since.
        assert companion._notification_decision(
            state, self._status("stopped"), now=110.0, started_at=started_at,
        ) is False
        # Past it now.
        assert companion._notification_decision(
            state, self._status("stopped"), now=116.0, started_at=started_at,
        ) is True

    def test_notifies_at_most_once_per_distinct_bad_state(self):
        state = companion._NotificationState()
        companion._notification_decision(state, self._status("stopped"), now=100.0, started_at=0.0)
        assert companion._notification_decision(
            state, self._status("stopped"), now=120.0, started_at=0.0,
        ) is True
        # Same bad state again -- already notified, no repeat.
        assert companion._notification_decision(
            state, self._status("stopped"), now=140.0, started_at=0.0,
        ) is False

    def test_a_different_bad_state_notifies_again(self):
        # bad_since tracks "how long has *some* bad state held", not "how
        # long has this exact one" -- a flap from stopped to failed after
        # the fifteen-second threshold has already been crossed notifies
        # immediately about the new state, rather than making the human
        # wait through a second fifteen-second window for a problem that
        # has already been going on at least that long.
        state = companion._NotificationState()
        companion._notification_decision(state, self._status("stopped"), now=100.0, started_at=0.0)
        companion._notification_decision(state, self._status("stopped"), now=120.0, started_at=0.0)
        assert companion._notification_decision(
            state, self._status("failed"), now=121.0, started_at=0.0,
        ) is True

    def test_recovering_then_failing_again_clears_the_notified_marker(self):
        state = companion._NotificationState()
        companion._notification_decision(state, self._status("stopped"), now=100.0, started_at=0.0)
        companion._notification_decision(state, self._status("stopped"), now=120.0, started_at=0.0)
        # Recovers.
        companion._notification_decision(state, self._status("running"), now=125.0, started_at=0.0)
        assert state.bad_since is None
        assert state.notified_state is None
        # Fails the same way again -- notifies again, not suppressed by the
        # earlier "already notified" marker.
        companion._notification_decision(state, self._status("stopped"), now=200.0, started_at=0.0)
        assert companion._notification_decision(
            state, self._status("stopped"), now=220.0, started_at=0.0,
        ) is True


class TestRunStatusPollOnce:
    """The background poll's shared per-tick logic -- factored out of
    ``_run_tray()``'s and ``_run_serve()``'s own nested ``_poll_loop``
    closures specifically so it can be exercised directly rather than only
    by letting a real background thread run on a timer."""

    def test_probes_and_reports_the_status(self, monkeypatch):
        status = companion.daemon_status.DaemonStatus(state="running", version=None, pid=None, detail="")
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: status)

        result = companion._run_status_poll_once(notify_state=companion._NotificationState(), started_at=0.0)

        assert result is status

    def test_calls_on_status_with_the_fresh_probe(self, monkeypatch):
        status = companion.daemon_status.DaemonStatus(state="stopped", version=None, pid=None, detail="")
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: status)
        seen = []

        companion._run_status_poll_once(
            notify_state=companion._NotificationState(), started_at=0.0, on_status=seen.append,
        )

        assert seen == [status]

    def test_on_notify_runs_only_when_the_decision_says_so(self, monkeypatch):
        status = companion.daemon_status.DaemonStatus(state="running", version=None, pid=None, detail="")
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: status)
        notified = []

        # "running" never notifies (TestNotificationDecision covers the
        # actual rule) -- this just checks the wiring: on_notify is called
        # exactly when _notification_decision says True, never otherwise.
        companion._run_status_poll_once(
            notify_state=companion._NotificationState(), started_at=0.0, on_notify=lambda: notified.append(True),
        )

        assert notified == []

    def test_on_notify_fires_for_a_persisted_bad_state(self, monkeypatch):
        status = companion.daemon_status.DaemonStatus(state="stopped", version=None, pid=None, detail="")
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: status)
        monkeypatch.setattr(companion.time, "monotonic", lambda: 100.0)
        state = companion._NotificationState(bad_since=0.0)
        notified = []

        companion._run_status_poll_once(
            notify_state=state, started_at=0.0, on_notify=lambda: notified.append(True),
        )

        assert notified == [True]


class TestNotifySend:
    def test_no_notifier_installed_is_a_silent_no_op(self, monkeypatch):
        monkeypatch.setattr(companion.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            companion.subprocess, "run", lambda *a, **k: pytest.fail("must not run anything")
        )
        companion._notify_send("PrivacyFence isn't running.")

    def test_runs_notify_send_with_the_text(self, monkeypatch):
        monkeypatch.setattr(companion.shutil, "which", lambda name: "/usr/bin/notify-send")
        seen = {}

        def _run(argv, **kwargs):
            seen["argv"] = argv
            return None

        monkeypatch.setattr(companion.subprocess, "run", _run)

        companion._notify_send("PrivacyFence isn't running.")

        assert seen["argv"] == ["/usr/bin/notify-send", "PrivacyFence", "PrivacyFence isn't running."]

    def test_a_failing_notify_send_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(companion.shutil, "which", lambda name: "/usr/bin/notify-send")

        def _raise(*a, **k):
            raise OSError("no display")

        monkeypatch.setattr(companion.subprocess, "run", _raise)

        companion._notify_send("PrivacyFence isn't running.")  # must not raise


class TestTrayLoop:
    """``_run_tray`` itself -- the one part of this module the header above
    called untestable. Running the real loop still is: it needs a display,
    and pystray/Pillow are not dependencies on this OS. What is testable,
    with those two stubbed at the import ``_run_tray`` does itself, is
    everything the tray is *wiring up* -- and both halves of the self-approval
    plan added to it (Phase 1 the recovery-code item, Phase 2 the attested
    ``_open_path``), so the menu had grown two behaviours nothing checked.
    """

    @pytest.fixture
    def tray(self, monkeypatch):
        """A fake ``pystray``/``PIL.Image`` pair, installed in ``sys.modules``
        so ``_run_tray``'s own deferred import picks them up, plus a captured
        icon so the test can invoke the menu callbacks the way a click would.

        The local-mode-fixes plan's Phase 2: ``daemon_status.probe()`` is stubbed to a fixed
        ``running`` status by default -- ``_run_tray()`` calls it before the
        menu is even built, and a real probe would reach for a control
        socket/service manager this test has no business touching. The
        background poll thread is also neutered (``_STATUS_POLL_SECONDS``
        patched to a value ``_run_tray`` never actually waits out, since the
        fake ``Icon.run()`` returns immediately and ``finally:`` stops it
        right after)."""
        captured = SimpleNamespace(icon=None, ran=False, stopped=False, run_until=None)

        class _Icon:
            def __init__(self, name, image, title, menu):
                self.name, self.image, self.title, self.menu = name, image, title, menu
                self.icon = image
                self.notified = []
                self.update_menu_calls = 0
                captured.icon = self

            def run(self):
                # By default returns immediately, same as every other test
                # here needs (the tray "loop" is over as soon as this
                # returns). test_the_poll_loop_redraws_the_icon_and_menu
                # sets captured.run_until to a real Event and waits on it
                # (bounded) instead, so the real background poll thread gets
                # a chance to tick at least once before teardown.
                if captured.run_until is not None:
                    captured.run_until.wait(timeout=2.0)
                captured.ran = True

            def stop(self):
                captured.stopped = True

            def update_menu(self):
                self.update_menu_calls += 1
                # Signals captured.run_until, if a test is waiting on one --
                # see run()'s own comment. Set here rather than tied to the
                # probe itself, so run() only unblocks once a full poll tick
                # (probe -> redraw -> update_menu) has actually finished,
                # not merely started.
                if captured.run_until is not None:
                    captured.run_until.set()

            def notify(self, text):
                self.notified.append(text)

        class _MenuItem:
            def __init__(self, text, action, *, enabled=True, visible=True):
                self.text, self.action, self.enabled, self.visible = text, action, enabled, visible

        class _Menu:
            def __init__(self, *items):
                self.items = items

        class _FakeImage:
            """Enough of a PIL ``Image`` for ``_status_icon_image()``: a
            ``.mode`` to round-trip through ``.convert()``, and identity
            preserved so a test can tell "still the color image" apart from
            "greyscaled" without needing real pixel data."""

            def __init__(self, label):
                self.label, self.mode = label, "RGBA"

            def convert(self, mode):
                return self

        pystray = SimpleNamespace(Icon=_Icon, Menu=_Menu, MenuItem=_MenuItem)
        pil_image = SimpleNamespace(open=lambda path: _FakeImage(f"color:{path}"))
        pil_image_ops = SimpleNamespace(grayscale=lambda image: _FakeImage(f"grey:{image.label}"))
        monkeypatch.setitem(sys.modules, "pystray", pystray)
        monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(Image=pil_image, ImageOps=pil_image_ops))
        monkeypatch.setitem(sys.modules, "PIL.Image", pil_image)
        monkeypatch.setitem(sys.modules, "PIL.ImageOps", pil_image_ops)

        # Nothing real starts: the separation check spawns a thread, and the
        # channel would bind a socket this test has no business owning.
        monkeypatch.setattr(companion, "_start_pending_separation_check", lambda: None)
        channel = SimpleNamespace(started=False, stopped=False)
        channel.start = lambda: setattr(channel, "started", True)
        channel.stop = lambda: setattr(channel, "stopped", True)
        monkeypatch.setattr(companion, "CompanionChannelServer", lambda: channel)
        captured.channel = channel

        running = companion.daemon_status.DaemonStatus(
            state="running", version="4.2.0", pid=4242, detail="PrivacyFence 4.2.0 is running (pid 4242).",
        )
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: running)
        return captured

    @staticmethod
    def _resolve(value, item):
        return value(item) if callable(value) else value

    def _model(self, tray):
        """Every menu row as plain data -- ``text``/``enabled``/``visible``
        resolved the way real pystray would resolve a callable (calling it
        with the ``MenuItem`` itself), keyed by label for the rows whose
        label is a plain string."""
        rows = []
        for item in tray.icon.menu.items:
            rows.append({
                "label": self._resolve(item.text, item),
                "action": item.action,
                "enabled": self._resolve(item.enabled, item),
                "visible": self._resolve(item.visible, item),
            })
        return rows

    def _items(self, tray):
        return {row["label"]: row["action"] for row in self._model(tray) if isinstance(row["label"], str)}

    def test_the_menu_matches_the_documented_layout(self, tray):
        assert companion._run_tray() == 0
        rows = self._model(tray)
        assert [row["label"] for row in rows] == [
            "● PrivacyFence is running (v4.2.0)",
            "Open Approvals", "Open Settings",
            "Start PrivacyFence…", "Restart PrivacyFence…", "Stop PrivacyFence…",
            "Service Details…", "New Recovery Code…", "Quit Companion",
        ]
        assert rows[0]["enabled"] is False
        # Running: Start is hidden, Restart/Stop are offered.
        assert rows[3]["visible"] is False
        assert rows[4]["visible"] is True
        assert rows[5]["visible"] is True
        assert tray.ran is True

    def test_the_icon_greys_out_when_the_daemon_is_not_running(self, tray, monkeypatch):
        stopped = companion.daemon_status.DaemonStatus(
            state="stopped", version=None, pid=None, detail="PrivacyFence is not running.",
        )
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: stopped)
        companion._run_tray()
        assert tray.icon.image.label.startswith("grey:")

    def test_the_icon_stays_in_color_while_running(self, tray):
        companion._run_tray()
        assert tray.icon.image.label.startswith("color:")

    def test_a_stopped_daemon_offers_start_not_restart_or_stop(self, tray, monkeypatch):
        stopped = companion.daemon_status.DaemonStatus(
            state="stopped", version=None, pid=None, detail="PrivacyFence is not running.",
        )
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: stopped)
        companion._run_tray()
        rows = self._model(tray)
        assert rows[3]["visible"] is True
        assert rows[4]["visible"] is False
        assert rows[5]["visible"] is False

    def test_the_channel_is_up_while_the_icon_runs_and_torn_down_after(self, tray):
        assert companion._run_tray() == 0
        assert tray.channel.started is True
        # The finally: arm -- a tray process that exits leaves nothing bound,
        # so the next companion to start can take the address.
        assert tray.channel.stopped is True
        assert companion._channel_running.is_set() is False

    def test_the_poll_loop_redraws_the_icon_and_menu(self, tray, monkeypatch):
        # Every other test here neuters the background poll thread (the
        # fake Icon.run() returns immediately, so poll_stop is set before
        # the poll thread's first wait() call). This one instead lets it
        # run for real: a tiny poll interval, and tray.run_until (an Event
        # the fake update_menu() sets on its first call) is what run()
        # blocks on -- bounded at 2s, so the test waits only as long as one
        # real poll tick (probe -> redraw -> update_menu) actually takes,
        # rather than a fixed sleep.
        monkeypatch.setattr(companion, "_STATUS_POLL_SECONDS", 0.01)
        stopped_status = companion.daemon_status.DaemonStatus(
            state="stopped", version=None, pid=None, detail="PrivacyFence is not running.",
        )
        monkeypatch.setattr(companion.daemon_status, "probe", lambda: stopped_status)
        tray.run_until = threading.Event()

        assert companion._run_tray() == 0

        assert tray.icon.update_menu_calls >= 1
        assert tray.icon.image.label.startswith("grey:")  # the poll's own redraw, not the initial one

    def test_open_items_dispatch_to_open_path(self, tray, monkeypatch):
        opened = []
        monkeypatch.setattr(companion, "_open_path", opened.append)
        companion._run_tray()
        items = self._items(tray)
        items["Open Approvals"](tray.icon, None)
        items["Open Settings"](tray.icon, None)
        assert opened == ["/approvals", "/settings"]

    def test_quit_stops_the_daemon_before_taking_the_icon_down(self, tray, monkeypatch):
        order = []
        monkeypatch.setattr(companion, "_quit_daemon", lambda: order.append("daemon") or True)
        companion._run_tray()
        tray.channel.stop = lambda: order.append("channel")
        tray.icon.stop = lambda: order.append("icon")
        self._items(tray)["Quit Companion"](tray.icon, None)
        # A tray icon with nothing left to serve has no reason to stay up --
        # and the daemon has to be asked first, since stopping the channel
        # first would remove the way to ask.
        assert order == ["daemon", "channel", "icon"]

    def test_the_recovery_item_runs_off_the_menu_thread(self, tray, monkeypatch):
        # pystray runs menu callbacks on the thread that draws the menu, and
        # this round trip is two dialogs long -- inline would freeze the icon
        # for as long as somebody takes to answer.
        threads = []

        class _Thread:
            def __init__(self, *, target, name, daemon, args=()):
                self.target, self.name, self.daemon, self.args = target, name, daemon, args
                threads.append(self)

            def start(self):
                # The background status-poll thread is real (module-level
                # daemon thread, not one of the click handlers this test is
                # about) -- running it synchronously here would call
                # `poll_stop.wait(5.0)` on this test's own thread, for real,
                # before `_run_tray()` ever reaches `icon.run()`.
                if self.name != "privacyfence-status-poll":
                    self.target(*self.args)

        monkeypatch.setattr(companion.threading, "Thread", _Thread)
        shown = []
        monkeypatch.setattr(companion, "_show_recovery_code", lambda: shown.append(True) or True)
        companion._run_tray()
        self._items(tray)["New Recovery Code…"](tray.icon, None)
        assert shown == [True]

    def test_the_service_actions_run_off_their_own_thread(self, tray, monkeypatch):
        threads = []

        class _Thread:
            def __init__(self, *, target, name, daemon, args=()):
                self.target, self.name, self.daemon, self.args = target, name, daemon, args
                threads.append(self)

            def start(self):
                if self.name != "privacyfence-status-poll":
                    self.target(*self.args)

        monkeypatch.setattr(companion.threading, "Thread", _Thread)
        calls = []
        monkeypatch.setattr(companion, "_run_service_action", lambda action: calls.append(action) or True)
        monkeypatch.setattr(companion, "_show_service_status", lambda: calls.append("status") or True)
        companion._run_tray()
        items = self._items(tray)
        items["Restart PrivacyFence…"](tray.icon, None)
        items["Service Details…"](tray.icon, None)
        assert calls == ["restart", "status"]
        assert all(t.daemon is True for t in threads)
