"""Tests for web/control_channel.py -- #428 Phase 2's replacement for
web_token -> POST /api/bootstrap. POSIX-only: the Unix-domain-socket half is
real and runs on any CI OS this suite already targets; the named-pipe half
needs pywin32 and a real Windows kernel object, so it's exercised for real
only by the Windows-hosted system/integration tests (see
tests/system/test_local_mode_system.py, tests/integration/
test_windows_packaged_smoke.py) -- there is no value in mocking pywin32's
C-extension surface here just to claim coverage on a platform this process
isn't running on.
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

from privacyfence.web import control_channel as cc
from privacyfence.web import session_auth as sa
from privacyfence.web.session_auth import BootstrapStore

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Unix-domain-socket control channel -- POSIX only, see module docstring",
)


def _mint(sock_path: str, message: str = "MINT\n", *, timeout: float = 2.0) -> str:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(sock_path)
        client.sendall(message.encode("utf-8"))
        return client.recv(4096).decode("utf-8")
    finally:
        client.close()


class TestPosixSocketPath:
    def test_lives_under_the_authority_root_by_default(self, monkeypatch):
        # A pure PurePosixPath, and paths.authority_dir() itself mocked
        # rather than paths.data_dir() -- pytest's own tmp_path is already
        # long enough on some CI runners (macOS's /private/var/folders/...
        # prefix) to trip the sun_path-length fallback this class's other
        # tests exercise on purpose, which would make this "short path"
        # case flaky by host rather than by design. authority_dir() being
        # real (secure_mkdir side effects) is also worth avoiding here,
        # since this test only cares about the pure join.
        from pathlib import PurePosixPath

        from privacyfence import paths

        monkeypatch.setattr(paths, "authority_dir", lambda: PurePosixPath("/home/alice/.privacyfence/authority"))
        path = cc.posix_socket_path()
        assert path == PurePosixPath("/home/alice/.privacyfence/authority/control.sock")

    def test_falls_back_to_a_short_temp_path_when_too_long_for_af_unix(self, tmp_path, monkeypatch):
        from privacyfence import paths

        # AF_UNIX's sun_path is a fixed-size kernel buffer (108 bytes on
        # Linux, 104 on macOS) -- a deeply nested data directory (exactly
        # what a descriptively-named pytest tmp_path produces) can't bind
        # there at all, real install paths never get close.
        deep = tmp_path
        for _ in range(6):
            deep = deep / "a-fairly-long-directory-segment-name"
        monkeypatch.setattr(paths, "data_dir", lambda: deep)

        path = cc.posix_socket_path()

        assert len(str(path).encode("utf-8")) < 100
        assert str(path).startswith(__import__("tempfile").gettempdir())

    def test_fallback_path_is_deterministic_for_the_same_authority_dir(self, tmp_path, monkeypatch):
        from privacyfence import paths

        deep = tmp_path
        for _ in range(6):
            deep = deep / "a-fairly-long-directory-segment-name"
        monkeypatch.setattr(paths, "data_dir", lambda: deep)

        assert cc.posix_socket_path() == cc.posix_socket_path()


class TestControlChannelServerPosix:
    def _server(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        bootstrap = BootstrapStore()
        server = cc.ControlChannelServer(bootstrap=bootstrap)
        server.start()
        return server, bootstrap

    def test_binds_a_socket_owner_only(self, tmp_path, monkeypatch):
        server, _bootstrap = self._server(tmp_path, monkeypatch)
        try:
            assert server.address is not None
            from pathlib import Path

            sock_path = Path(server.address)
            assert sock_path.exists()
            assert oct(sock_path.stat().st_mode)[-3:] == "600"
        finally:
            server.stop()

    def test_mint_returns_a_code_that_redeems_a_session(self, tmp_path, monkeypatch):
        from privacyfence.web.server import build_app
        from privacyfence.web.session_auth import LocalSessionStore
        from privacyfence.web_approval_ui import WebApprovalUI
        from starlette.testclient import TestClient

        server, bootstrap = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address)
            assert reply.startswith("OK ")
            code = reply[len("OK "):].strip()

            sessions = LocalSessionStore()
            app = build_app(WebApprovalUI(), sessions=sessions, bootstrap=bootstrap)
            client = TestClient(app, base_url="http://localhost", follow_redirects=False)
            r = client.get(f"/approvals?bootstrap={code}")
            assert "pf_session" in r.headers.get("set-cookie", "")
        finally:
            server.stop()

    def test_each_mint_is_a_fresh_single_use_code(self, tmp_path, monkeypatch):
        server, _bootstrap = self._server(tmp_path, monkeypatch)
        try:
            first = _mint(server.address)[len("OK "):].strip()
            second = _mint(server.address)[len("OK "):].strip()
            assert first != second
        finally:
            server.stop()

    def test_unknown_command_is_rejected(self, tmp_path, monkeypatch):
        server, _bootstrap = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="BOGUS\n")
            assert reply.startswith("ERROR")
        finally:
            server.stop()

    def test_stop_removes_the_socket_file(self, tmp_path, monkeypatch):
        server, _bootstrap = self._server(tmp_path, monkeypatch)
        sock_path = server.address
        server.stop()
        from pathlib import Path

        assert not Path(sock_path).exists()

    def test_a_stale_socket_file_does_not_block_a_fresh_bind(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        stale_path = cc.posix_socket_path()
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.touch()

        server = cc.ControlChannelServer(bootstrap=BootstrapStore())
        try:
            server.start()
            assert _mint(server.address).startswith("OK ")
        finally:
            server.stop()

    def test_refuses_to_take_over_a_socket_owned_by_another_account(self, tmp_path, monkeypatch, caplog):
        # The local-mode-fixes plan's interim multi-user guard (Phase 2 §2.6):
        # a socket file left
        # by a different uid is never unlinked-and-rebound, even a stale
        # one -- see _existing_socket_owner_problem()'s own docstring for
        # the takeover this stops.
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        stranger_path = cc.posix_socket_path()
        stranger_path.parent.mkdir(parents=True, exist_ok=True)
        stranger_path.touch()
        monkeypatch.setattr(cc.os, "geteuid", lambda: stranger_path.stat().st_uid + 1)

        server = cc.ControlChannelServer(bootstrap=BootstrapStore())
        try:
            with caplog.at_level("WARNING"):
                server.start()
            assert server.address is None
            assert "refusing to take it over" in caplog.text
            # The stranger's file is left exactly as it was -- refusing means
            # refusing, not "unlink it anyway and just skip the bind".
            assert stranger_path.exists()
        finally:
            server.stop()


class TestExistingSocketOwnerProblem:
    """The pure check ``_start_posix()`` above is built on -- unit-testable
    without a real socket bind."""

    def test_nothing_there_is_fine(self, tmp_path):
        assert cc._existing_socket_owner_problem(tmp_path / "nothing.sock") is None

    def test_this_processes_own_file_is_fine(self, tmp_path, monkeypatch):
        sock_path = tmp_path / "companion.sock"
        sock_path.touch()
        monkeypatch.setattr(cc.os, "geteuid", lambda: sock_path.stat().st_uid)

        assert cc._existing_socket_owner_problem(sock_path) is None

    def test_a_different_owner_is_a_problem(self, tmp_path, monkeypatch):
        sock_path = tmp_path / "companion.sock"
        sock_path.touch()
        real_uid = sock_path.stat().st_uid
        monkeypatch.setattr(cc.os, "geteuid", lambda: real_uid + 1)

        problem = cc._existing_socket_owner_problem(sock_path)

        assert problem is not None
        assert str(real_uid) in problem
        assert "refusing to take it over" in problem


class TestQuitCommand:
    """#428 Phase 3 (ADR 0002): the companion's tray/launcher "Quit" action
    and the web settings page's own Quit button both end up here -- and
    both respect the same allow_quit flag."""

    def _server(self, tmp_path, monkeypatch, *, allow_quit: bool = True):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore(), allow_quit=allow_quit)
        server.start()
        return server

    def test_quit_triggers_daemon_shutdown(self, tmp_path, monkeypatch):
        from privacyfence import daemon_main

        called = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: called.append(True))
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="QUIT\n")
            assert reply.startswith("OK")
            assert called == [True]
        finally:
            server.stop()

    def test_quit_is_rejected_when_disabled(self, tmp_path, monkeypatch):
        from privacyfence import daemon_main

        called = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: called.append(True))
        server = self._server(tmp_path, monkeypatch, allow_quit=False)
        try:
            reply = _mint(server.address, message="QUIT\n")
            assert reply.startswith("ERROR")
            assert called == []
        finally:
            server.stop()

    def test_quit_is_refused_on_a_privilege_separated_install_even_when_allowed(
        self, tmp_path, monkeypatch,
    ):
        """#428 B4: this socket is 0660 group-shared with the companion on a
        separated install, which puts the agent in the same group -- so
        unlike the ``allow_quit`` case above, this must not be a setting a
        separated install can leave on."""
        from privacyfence import daemon_main, privilege_separation

        called = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: called.append(True))
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        server = self._server(tmp_path, monkeypatch, allow_quit=True)
        try:
            reply = _mint(server.address, message="QUIT\n")
            assert reply.startswith("ERROR")
            assert called == []
        finally:
            server.stop()

    def test_quit_refusal_names_this_platforms_stop_command(self, tmp_path, monkeypatch):
        from privacyfence import privilege_separation

        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(privilege_separation, "current_platform", lambda: "linux")
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="QUIT\n")
            assert privilege_separation.PLATFORM_LAYOUTS["linux"].stop_command in reply
        finally:
            server.stop()


class TestCompanionChannelServer:
    """#428 Phase 3: the reverse-direction channel -- the daemon is the
    client, the companion is the server -- used to hand a connector OAuth
    URL to a process that can still reach the user's browser once #428
    Phase 4 moves the daemon off the user's own desktop session."""

    def _server(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = cc.CompanionChannelServer()
        server.start()
        return server

    def test_open_calls_webbrowser_open(self, tmp_path, monkeypatch):
        opened = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="OPEN https://example.com/callback\n")
            assert reply.startswith("OK")
            assert opened == ["https://example.com/callback"]
        finally:
            server.stop()

    def test_open_rejects_non_http_schemes(self, tmp_path, monkeypatch):
        opened = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="OPEN file:///etc/passwd\n")
            assert reply.startswith("ERROR")
            assert opened == []
        finally:
            server.stop()

    def test_unknown_command_is_rejected(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="MINT\n")
            assert reply.startswith("ERROR")
        finally:
            server.stop()

    def test_companion_and_control_channel_addresses_never_collide(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        assert cc.posix_socket_path() != cc.companion_socket_path()


class TestConfirmEnrollCommand:
    """the enrollment gate: the companion channel's
    second command, and the only gate a *first* passkey enrollment can have
    -- there is no enrolled credential to assert with, and a local-mode
    session is not proof of a human (ADR 0002 decision 6). See this module's
    own docstring on why the answer has to come from this process.

    The dialog itself is the one part not exercised here: it is three
    platform-specific system dialogs, and this suite runs headless on Linux
    CI. What is exercised is everything around it -- the command's own
    dispatch, that a refusal in any form stays a refusal, and that the
    daemon-side client turns each reply into the right ``(confirmed, reason)``
    pair.
    """

    def _server(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = cc.CompanionChannelServer()
        server.start()
        return server

    def test_an_allowed_dialog_answers_ok(self, tmp_path, monkeypatch):
        asked = []
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: asked.append(prompt) or True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="CONFIRM ENROLL\n").startswith("OK")
        finally:
            server.stop()
        # The prompt is this module's own constant, never taken from the
        # request line -- see _handle_companion_request's own docstring.
        assert asked == [cc._CONFIRM_ENROLL_PROMPT]

    def test_a_denied_dialog_answers_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: False)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="CONFIRM ENROLL\n")
        finally:
            server.stop()
        assert reply.startswith("ERROR")
        assert "denied" in reply

    def test_a_desktop_with_no_dialog_program_is_a_refusal_naming_the_fix(self, monkeypatch):
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc, "_LINUX_DIALOG_COMMANDS", ())

        reply = cc._confirm_first_enrollment()

        assert reply.startswith("ERROR")
        # Not just "no": a human who cannot enroll has to be able to act on
        # the reason, and routes_security.py shows this line verbatim.
        assert "zenity" in reply and "kdialog" in reply

    def test_nobody_answering_is_a_refusal_that_says_to_try_again(self, monkeypatch):
        import subprocess

        def _times_out(prompt, *, timeout):
            raise subprocess.TimeoutExpired(cmd="zenity", timeout=timeout)

        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc, "_confirm_linux", _times_out)

        reply = cc._confirm_first_enrollment()

        assert reply.startswith("ERROR")
        assert "try again" in reply

    def test_a_dialog_that_cannot_run_at_all_is_a_refusal(self, monkeypatch):
        def _explodes(prompt, *, timeout):
            raise OSError("no display")

        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc, "_confirm_linux", _explodes)

        assert cc._confirm_first_enrollment().startswith("ERROR")

    def test_each_platform_gets_its_own_dialog(self, monkeypatch):
        called = []
        for name in ("_confirm_macos", "_confirm_windows", "_confirm_linux"):
            monkeypatch.setattr(cc, name, lambda prompt, *, timeout, _n=name: called.append(_n) or True)
        for platform in ("darwin", "win32", "linux", "freebsd"):
            monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda _p=platform: _p)
            assert cc._confirm_first_enrollment() == "OK\n"
        # Anything that is not macOS or Windows takes the POSIX dialog path,
        # same fallthrough privilege_separation.py's own platform dispatch has.
        assert called == ["_confirm_macos", "_confirm_windows", "_confirm_linux", "_confirm_linux"]

    def test_the_macos_dialog_script_carries_no_raw_newline(self, monkeypatch):
        # A raw newline inside an AppleScript string literal is a syntax
        # error, not a line break -- and the prompt is three paragraphs. This
        # is the one platform whose dialog cannot be driven on Linux CI, so
        # the script it would run is checked instead of its result.
        scripts = []

        class _Result:
            returncode = 0
            stdout = f"button returned:{cc._CONFIRM_ALLOW_LABEL}"

        def _capture(argv, **_kwargs):
            scripts.append(argv[-1])
            return _Result()

        monkeypatch.setattr(cc.subprocess, "run", _capture)
        assert cc._confirm_macos(cc._CONFIRM_ENROLL_PROMPT, timeout=1.0) is True
        assert "\n" not in scripts[0]
        # The prompt's paragraph breaks survive as AppleScript's own escape.
        assert "\\n" in scripts[0]

    def test_the_macos_dialog_treats_a_dismissal_as_a_refusal(self, monkeypatch):
        # "Deny" is both the default and the cancel button, so a dismissed
        # dialog (osascript's own nonzero exit) and an explicit Deny land in
        # the same place.
        class _Cancelled:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(cc.subprocess, "run", lambda argv, **kwargs: _Cancelled())
        assert cc._confirm_macos(cc._CONFIRM_ENROLL_PROMPT, timeout=1.0) is False

    def test_the_linux_dialog_runs_the_first_program_that_exists(self, monkeypatch, tmp_path):
        present = tmp_path / "zenity"
        present.write_text("", encoding="utf-8")
        argvs = []

        class _Yes:
            returncode = 0

        monkeypatch.setattr(cc, "_LINUX_DIALOG_COMMANDS", (
            (str(tmp_path / "missing"), lambda prompt: ["missing", prompt]),
            (str(present), lambda prompt: [str(present), prompt]),
        ))
        monkeypatch.setattr(cc.subprocess, "run", lambda argv, **kwargs: argvs.append(argv) or _Yes())

        assert cc._confirm_linux("ask?", timeout=1.0) is True
        assert argvs == [[str(present), "ask?"]]

    def test_the_linux_dialog_reads_a_nonzero_exit_as_no(self, monkeypatch, tmp_path):
        present = tmp_path / "zenity"
        present.write_text("", encoding="utf-8")

        class _No:
            returncode = 1

        monkeypatch.setattr(cc, "_LINUX_DIALOG_COMMANDS", ((str(present), lambda prompt: [str(present)]),))
        monkeypatch.setattr(cc.subprocess, "run", lambda argv, **kwargs: _No())

        assert cc._confirm_linux("ask?", timeout=1.0) is False

    def test_confirm_without_a_subject_is_an_unknown_command(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="CONFIRM\n").startswith("ERROR unknown command")
            assert _mint(server.address, message="CONFIRM SOMETHING\n").startswith("ERROR unknown command")
        finally:
            server.stop()

    def test_open_with_no_url_is_an_unknown_command(self, tmp_path, monkeypatch):
        # Guarding the refactor that gave OPEN and CONFIRM a shared argument
        # split: a bare OPEN used to fail the old ``len(parts) != 2`` check.
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="OPEN\n").startswith("ERROR unknown command")
        finally:
            server.stop()


class TestEnrollmentCommand:
    """Plan item 1.2's daemon-side half: the one question the companion
    cannot answer for itself, because on a separated install the credential
    store lives under ``authority_dir()`` and the logged-in user cannot read
    it."""

    def _server(self, tmp_path, monkeypatch, **kwargs):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore(), **kwargs)
        server.start()
        return server

    def test_reports_the_state_the_daemon_gives_it(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, enrollment_state=lambda: "pending")
        try:
            assert _mint(server.address, message="ENROLLMENT\n") == "OK pending\n"
        finally:
            server.stop()

    def test_an_install_with_nothing_to_answer_with_says_so(self, tmp_path, monkeypatch):
        # A ControlChannelServer built with no step-up config behind it --
        # every test that exercises MINT/QUIT alone. An ERROR line is the
        # shape companion.py already knows how to shrug off.
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="ENROLLMENT\n").startswith("ERROR")
        finally:
            server.stop()

    def test_the_client_reads_the_state_back(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = self._server(tmp_path, monkeypatch, enrollment_state=lambda: "ok")
        try:
            assert cc.enrollment_state(timeout=5.0) == "ok"
        finally:
            server.stop()

    def test_the_client_raises_on_a_daemon_that_cannot_answer(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = self._server(tmp_path, monkeypatch)
        try:
            with pytest.raises(cc.ControlChannelError):
                cc.enrollment_state(timeout=5.0)
        finally:
            server.stop()


class TestStatusCommand:
    """The local-mode-fixes plan's Phase 2: ``STATUS``, ``daemon_status.py``'s
    "the control channel answered" source. Unlike every other command on
    this channel it is never gated on ``allow_quit`` or a passkey -- it
    carries nothing but a version string, a pid and connector-configured-ness,
    so it answers unconditionally whenever a callback is wired up at all."""

    def _server(self, tmp_path, monkeypatch, **kwargs):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore(), **kwargs)
        server.start()
        return server

    def test_reports_the_json_the_daemon_gives_it(self, tmp_path, monkeypatch):
        payload = '{"version": "4.2.0", "pid": 4242}'
        server = self._server(tmp_path, monkeypatch, status=lambda: payload)
        try:
            assert _mint(server.address, message="STATUS\n") == f"OK {payload}\n"
        finally:
            server.stop()

    def test_an_install_with_nothing_to_answer_with_says_so(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="STATUS\n").startswith("ERROR")
        finally:
            server.stop()

    def test_the_client_parses_the_reply_into_a_dict(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "is_windows", lambda: False)
        payload = '{"version": "4.2.0", "pid": 4242, "separated": true, "connectors": {"gmail": "ok"}}'
        server = self._server(tmp_path, monkeypatch, status=lambda: payload)
        try:
            result = cc.request_status(timeout=5.0)
            assert result == {
                "version": "4.2.0", "pid": 4242, "separated": True, "connectors": {"gmail": "ok"},
            }
        finally:
            server.stop()

    def test_the_client_raises_on_a_daemon_that_cannot_answer(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = self._server(tmp_path, monkeypatch)
        try:
            with pytest.raises(cc.ControlChannelError):
                cc.request_status(timeout=5.0)
        finally:
            server.stop()

    def test_the_client_raises_on_malformed_json(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = self._server(tmp_path, monkeypatch, status=lambda: "not json")
        try:
            with pytest.raises(cc.ControlChannelError):
                cc.request_status(timeout=5.0)
        finally:
            server.stop()

    def test_status_is_never_gated_on_allow_quit(self, tmp_path, monkeypatch):
        # Unlike QUIT, this carries no secrets and no ability to act -- an
        # install with allow_quit disabled still answers it.
        payload = '{"version": "4.2.0", "pid": 1}'
        server = self._server(tmp_path, monkeypatch, allow_quit=False, status=lambda: payload)
        try:
            assert _mint(server.address, message="STATUS\n") == f"OK {payload}\n"
        finally:
            server.stop()


class TestRecoveryCommand:
    """Plan item 1.3, on the daemon's own channel. The property under test
    is the one the whole item exists for: whatever happens, the reply on
    this socket never carries a recovery code. This socket is ``0660``
    group-shared with the agent on a separated install."""

    def _server(self, tmp_path, monkeypatch, **kwargs):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore(), **kwargs)
        server.start()
        return server

    def test_a_successful_issue_answers_a_bare_ok(self, tmp_path, monkeypatch):
        issued = []
        server = self._server(
            tmp_path, monkeypatch,
            reissue_recovery_code=lambda: (issued.append("asked"), (True, ""))[1],
        )
        try:
            assert _mint(server.address, message="RECOVERY\n") == "OK\n"
        finally:
            server.stop()
        assert issued == ["asked"]

    def test_a_refusal_passes_the_reason_and_no_code(self, tmp_path, monkeypatch):
        server = self._server(
            tmp_path, monkeypatch,
            reissue_recovery_code=lambda: (False, "issuing a new recovery code was denied"),
        )
        try:
            reply = _mint(server.address, message="RECOVERY\n")
        finally:
            server.stop()
        assert reply == "ERROR issuing a new recovery code was denied\n"

    def test_an_install_with_no_recovery_path_says_so(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="RECOVERY\n").startswith("ERROR")
        finally:
            server.stop()

    def test_the_client_returns_nothing_on_success(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, reissue_recovery_code=lambda: (True, ""))
        try:
            assert cc.request_recovery_code(timeout=5.0) is None
        finally:
            server.stop()

    def test_the_client_raises_with_the_daemon_s_own_reason(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, reissue_recovery_code=lambda: (False, "nope"))
        try:
            with pytest.raises(cc.ControlChannelError) as exc_info:
                cc.request_recovery_code(timeout=5.0)
        finally:
            server.stop()
        assert "nope" in str(exc_info.value)


class TestShowRecoveryCommand:
    """The one command in this module that puts a value from the wire in
    front of a human. What is tested is the gate that makes that acceptable:
    one shape, checked in the dispatch, refused rather than displayed."""

    def _server(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        server = cc.CompanionChannelServer()
        server.start()
        return server

    def test_the_pattern_matches_what_webauthn_stepup_actually_mints(self):
        # The pattern is restated in control_channel.py rather than imported,
        # so that the companion process never pulls py_webauthn in for one
        # regex -- which means something has to hold the two together.
        from privacyfence import webauthn_stepup

        for _ in range(20):
            assert cc._RECOVERY_CODE_PATTERN.match(webauthn_stepup.mint_recovery_code())

    def test_a_well_formed_code_is_shown(self, tmp_path, monkeypatch):
        shown = []
        monkeypatch.setattr(cc, "_message_linux", lambda message, *, timeout: shown.append(message) or True)
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="SHOW RECOVERY A1B2-C3D4-E5F6-1789\n") == "OK\n"
        finally:
            server.stop()
        assert len(shown) == 1
        assert "A1B2-C3D4-E5F6-1789" in shown[0]
        # Everything around the code is this module's own constant.
        assert shown[0].startswith(cc._SHOW_RECOVERY_PREFIX)
        assert shown[0].endswith(cc._SHOW_RECOVERY_SUFFIX)

    @pytest.mark.parametrize("code", [
        "",
        "not-a-code",
        "a1b2-c3d4-e5f6-1789",  # lowercase: not what is ever minted
        "A1B2-C3D4-E5F6",  # three groups
        "A1B2-C3D4-E5F6-1789-0000",  # five
        "A1B2-C3D4-E5F6-1789 and now a sentence of my own",
    ])
    def test_anything_else_is_refused_without_a_dialog(self, tmp_path, monkeypatch, code):
        def _never(message, *, timeout):
            raise AssertionError(f"showed an unchecked string: {message!r}")

        monkeypatch.setattr(cc, "_message_linux", _never)
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message=f"SHOW RECOVERY {code}\n")
        finally:
            server.stop()
        assert reply.startswith("ERROR")

    def test_show_with_an_unknown_subject_is_refused_without_a_dialog(self, tmp_path, monkeypatch):
        # ``SHOW`` carries two subjects now: this class's ``RECOVERY <code>``
        # and Phase 2's ``SHOW <path>``. Anything that is not ``RECOVERY`` is
        # therefore read as a page, and an unknown one answers "unknown page"
        # rather than "unknown command" -- ``RECOVERY`` is not in SHOW_PATHS,
        # so the two subjects cannot collide. What has to stay true either way
        # is what this class is about: an unrecognized subject is refused
        # before anything reaches a dialog.
        def _never(*args, **kwargs):
            raise AssertionError("an unknown SHOW subject reached a dialog")

        monkeypatch.setattr(cc, "_message_linux", _never)
        monkeypatch.setattr(cc, "_confirm_linux", _never)
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="SHOW SOMETHING\n").startswith("ERROR unknown page")
            assert _mint(server.address, message="SHOW\n").startswith("ERROR unknown page")
        finally:
            server.stop()

    def test_a_desktop_with_no_dialog_program_refuses_rather_than_lying(self, monkeypatch):
        # Load-bearing: web/routes_security.py stores the code only once this
        # says it was shown, so "could not show it" has to be distinguishable
        # from "shown".
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc, "_LINUX_MESSAGE_COMMANDS", ())

        reply = cc._show_recovery_code("A1B2-C3D4-E5F6-1789")

        assert reply.startswith("ERROR")
        assert "zenity" in reply or "kdialog" in reply

    def test_the_daemon_side_client_reports_whether_it_was_shown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc, "_message_linux", lambda message, *, timeout: True)
        server = self._server(tmp_path, monkeypatch)
        try:
            assert cc.send_recovery_code("A1B2-C3D4-E5F6-1789", timeout=5.0) == (True, "")
        finally:
            server.stop()

    def test_the_daemon_side_client_reports_a_missing_companion(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)

        shown, reason = cc.send_recovery_code("A1B2-C3D4-E5F6-1789", timeout=0.5)

        assert shown is False
        assert "companion" in reason


class TestConfirmRecoveryCommand:
    """Issuing a replacement code invalidates whatever the human wrote down,
    so it is asked about first -- which is what keeps a local process that
    speaks the daemon's own socket from doing it unnoticed."""

    def _server(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        server = cc.CompanionChannelServer()
        server.start()
        return server

    def test_an_allowed_dialog_confirms(self, tmp_path, monkeypatch):
        asked = []
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: asked.append(prompt) or True)
        server = self._server(tmp_path, monkeypatch)
        try:
            assert cc.request_recovery_confirmation(timeout=5.0) == (True, "")
        finally:
            server.stop()
        # Its own prompt, not ENROLL's -- the two say different things about
        # what is about to happen, and both are constants of this module.
        assert asked == [cc._CONFIRM_RECOVERY_PROMPT]

    def test_a_denied_dialog_refuses_with_its_own_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: False)
        server = self._server(tmp_path, monkeypatch)
        try:
            confirmed, reason = cc.request_recovery_confirmation(timeout=5.0)
        finally:
            server.stop()
        assert confirmed is False
        assert "recovery code" in reason

    def test_no_companion_running_refuses_and_says_to_start_it(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)

        confirmed, reason = cc.request_recovery_confirmation(timeout=0.5)

        assert confirmed is False
        assert "companion" in reason


class TestMessageDialogs:
    """The statement half of the dialog machinery -- ``_confirm_*``'s
    counterpart, for SHOW RECOVERY. Same absolute-path rule, same "neither
    program is a dependency" handling."""

    def test_zenity_is_used_when_present(self, tmp_path, monkeypatch):
        present = tmp_path / "zenity"
        present.write_text("#!/bin/sh\nexit 0\n")
        present.chmod(0o755)
        argv_seen = []
        monkeypatch.setattr(cc, "_LINUX_MESSAGE_COMMANDS", (
            (str(present), lambda message: [str(present), message]),
        ))
        real_run = cc.subprocess.run

        def _run(argv, **kwargs):
            argv_seen.append(argv)
            return real_run([str(present)], **kwargs)

        monkeypatch.setattr(cc.subprocess, "run", _run)
        assert cc._message_linux("hello", timeout=5.0) is True
        assert argv_seen == [[str(present), "hello"]]

    def test_a_nonzero_exit_is_not_shown(self, tmp_path, monkeypatch):
        present = tmp_path / "kdialog"
        present.write_text("#!/bin/sh\nexit 1\n")
        present.chmod(0o755)
        monkeypatch.setattr(cc, "_LINUX_MESSAGE_COMMANDS", ((str(present), lambda message: [str(present)]),))
        assert cc._message_linux("hello", timeout=5.0) is False

    def test_macos_shows_a_single_button_dialog(self, monkeypatch):
        scripts = []

        class _Result:
            returncode = 0
            stdout = ""

        def _run(argv, **kwargs):
            scripts.append(argv[-1])
            return _Result()

        monkeypatch.setattr(cc.subprocess, "run", _run)
        assert cc._message_macos("hello", timeout=1.0) is True
        assert "buttons {\"OK\"}" in scripts[0]
        # No cancel button: this states something, it does not ask.
        assert "cancel button" not in scripts[0]

    def test_a_timed_out_dialog_is_reported_not_assumed_shown(self, monkeypatch):
        def _times_out(message, *, timeout):
            raise cc.subprocess.TimeoutExpired(cmd="zenity", timeout=timeout)

        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc, "_message_linux", _times_out)

        assert cc._show_recovery_code("A1B2-C3D4-E5F6-1789").startswith("ERROR")

    def test_an_exploding_dialog_is_reported_not_assumed_shown(self, monkeypatch):
        def _explodes(message, *, timeout):
            raise RuntimeError("no display")

        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc, "_message_linux", _explodes)

        assert cc._show_recovery_code("A1B2-C3D4-E5F6-1789").startswith("ERROR")

    def test_each_platform_gets_its_own_pair_of_dialogs(self, monkeypatch):
        seen = []
        for name in ("_message_macos", "_message_windows", "_message_linux"):
            monkeypatch.setattr(cc, name, (lambda n: lambda message, *, timeout: seen.append(n) or True)(name))
        for platform in ("darwin", "win32", "linux"):
            monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda p=platform: p)
            assert cc._show_recovery_code("A1B2-C3D4-E5F6-1789") == "OK\n"
        assert seen == ["_message_macos", "_message_windows", "_message_linux"]


class TestRequestEnrollmentConfirmation:
    """The daemon's side of ``CONFIRM ENROLL``. Unlike ``request_open_url``,
    a missing companion is a refusal here rather than something to fall back
    from -- there is no local fallback that would mean anything, since what is
    being established is that a human asked."""

    def _no_windows(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)

    def test_no_companion_running_refuses_and_says_to_start_it(self, tmp_path, monkeypatch):
        self._no_windows(tmp_path, monkeypatch)

        confirmed, reason = cc.request_enrollment_confirmation(timeout=0.5)

        assert confirmed is False
        assert "companion" in reason

    def test_an_ok_reply_confirms(self, tmp_path, monkeypatch):
        self._no_windows(tmp_path, monkeypatch)
        monkeypatch.setattr(cc, "_confirm_first_enrollment", lambda: "OK\n")
        server = cc.CompanionChannelServer()
        server.start()
        try:
            assert cc.request_enrollment_confirmation(timeout=5.0) == (True, "")
        finally:
            server.stop()

    def test_an_error_reply_passes_the_companion_s_own_reason_through(self, tmp_path, monkeypatch):
        self._no_windows(tmp_path, monkeypatch)
        monkeypatch.setattr(cc, "_confirm_first_enrollment", lambda: "ERROR enrollment was denied\n")
        server = cc.CompanionChannelServer()
        server.start()
        try:
            confirmed, reason = cc.request_enrollment_confirmation(timeout=5.0)
        finally:
            server.stop()
        # The "ERROR " prefix is protocol, not prose -- stripped, so what
        # reaches /security is the sentence the companion actually wrote.
        assert (confirmed, reason) == (False, "enrollment was denied")

    def test_an_unexplained_error_still_produces_a_reason(self, tmp_path, monkeypatch):
        self._no_windows(tmp_path, monkeypatch)
        monkeypatch.setattr(cc, "_confirm_first_enrollment", lambda: "ERROR\n")
        server = cc.CompanionChannelServer()
        server.start()
        try:
            confirmed, reason = cc.request_enrollment_confirmation(timeout=5.0)
        finally:
            server.stop()
        assert confirmed is False
        assert reason


class TestCompanionChannelPeerVerification:
    """#428 B10: unlike ``ControlChannelServer``'s MINT/QUIT (ADR 0002
    decision 6 -- no peer check, deliberately, because companion and agent
    share a uid), this channel's whole reason to exist is the daemon asking
    the companion to open a URL, and separation is the one case where the
    two ends really do have different uids. The test client below connects
    from this very process, so ``SO_PEERCRED`` genuinely reports this
    process's own real uid -- what's faked is only which account
    ``service_account_uid()`` resolves to, not the kernel-reported peer.
    """

    def _server(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = cc.CompanionChannelServer()
        server.start()
        return server

    def test_unseparated_install_checks_nothing(self, tmp_path, monkeypatch):
        opened = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="OPEN https://example.com/callback\n")
            assert reply.startswith("OK")
            assert opened == ["https://example.com/callback"]
        finally:
            server.stop()

    def test_accepts_a_connection_from_the_service_account_uid(self, tmp_path, monkeypatch):
        from privacyfence import privilege_separation

        opened = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(privilege_separation, "service_account_uid", lambda: os.getuid())
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="OPEN https://example.com/callback\n")
            assert reply.startswith("OK")
            assert opened == ["https://example.com/callback"]
        finally:
            server.stop()

    def test_refuses_a_connection_from_any_other_uid(self, tmp_path, monkeypatch):
        from privacyfence import privilege_separation

        opened = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(privilege_separation, "service_account_uid", lambda: os.getuid() + 1)
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="OPEN https://example.com/callback\n")
            assert reply.startswith("ERROR")
            assert opened == []
        finally:
            server.stop()

    def test_refuses_when_the_service_account_uid_cannot_be_resolved(self, tmp_path, monkeypatch):
        """A half-removed install (the marker names an account that no
        longer exists) fails closed rather than falling back to accepting
        anyone -- ``service_account_uid()`` returns None for exactly this
        case."""
        from privacyfence import privilege_separation

        opened = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(privilege_separation, "service_account_uid", lambda: None)
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="OPEN https://example.com/callback\n")
            assert reply.startswith("ERROR")
            assert opened == []
        finally:
            server.stop()

    def test_a_refused_peer_still_mid_send_gets_the_refusal_not_a_broken_pipe(
        self, tmp_path, monkeypatch,
    ):
        """The refusal path used to close without reading the request, and
        closing a socket whose receive queue still holds unread data resets
        the connection -- so the refused peer's own send() failed with
        EPIPE before it could read the refusal, and request_open_url()'s
        caller saw a broken pipe instead of the "ERROR ..." line explaining
        why it was refused.

        The other tests in this class never caught it because their client
        wins that race on an unloaded machine; CI, loaded, did not (a
        BrokenPipeError out of the client's own sendall). Sleeping between
        connect() and sendall() makes the losing interleaving the only one,
        so this is a deterministic version of that flake rather than a
        second roll of the same dice.
        """
        import time

        from privacyfence import paths, privilege_separation

        opened = []
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: opened.append(url) or True)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(privilege_separation, "service_account_uid", lambda: os.getuid() + 1)

        server = cc.CompanionChannelServer()
        server.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5.0)
            try:
                client.connect(server.address)
                # Long enough that the server has certainly refused and
                # reached its close before this send starts.
                time.sleep(0.25)
                client.sendall(b"OPEN https://example.com/callback\n")
                reply = client.recv(4096).decode("utf-8")
            finally:
                client.close()
            assert reply.startswith("ERROR"), reply
            assert opened == []
        finally:
            server.stop()

    def test_control_channel_server_never_checks_peer_identity(self, tmp_path, monkeypatch):
        """ADR 0002 decision 6: the daemon's own MINT/QUIT channel is
        deliberately not gated this way, separated or not -- companion and
        agent share a uid there regardless."""
        from privacyfence import paths, privilege_separation
        from privacyfence.web.session_auth import BootstrapStore

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(privilege_separation, "service_account_uid", lambda: os.getuid() + 1)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore())
        server.start()
        try:
            reply = _mint(server.address)
            assert reply.startswith("OK ")
        finally:
            server.stop()


class TestControlChannelClientFunctions:
    """The companion's/daemon's own client-side helpers -- what
    companion.py and oauth_loopback.py actually call, rather than
    hand-rolling socket I/O themselves (ADR 0002 decision 4)."""

    def test_mint_bootstrap_code_returns_a_bare_code(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore())
        server.start()
        try:
            code = cc.mint_bootstrap_code()
            assert code and " " not in code
        finally:
            server.stop()

    def test_mint_bootstrap_code_raises_when_no_daemon_is_listening(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        with pytest.raises(OSError):
            cc.mint_bootstrap_code(timeout=0.5)

    def test_request_quit_triggers_shutdown(self, tmp_path, monkeypatch):
        from privacyfence import daemon_main, paths

        called = []
        monkeypatch.setattr(daemon_main, "request_shutdown", lambda: called.append(True))
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore())
        server.start()
        try:
            cc.request_quit()
            assert called == [True]
        finally:
            server.stop()

    def test_request_quit_raises_on_error_reply(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore(), allow_quit=False)
        server.start()
        try:
            with pytest.raises(cc.ControlChannelError):
                cc.request_quit()
        finally:
            server.stop()

    def test_request_quit_raises_when_privilege_separated(self, tmp_path, monkeypatch):
        from privacyfence import paths, privilege_separation

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore(), allow_quit=True)
        server.start()
        try:
            with pytest.raises(cc.ControlChannelError):
                cc.request_quit()
        finally:
            server.stop()

    def test_request_open_url_returns_false_when_no_companion_is_running(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        assert cc.request_open_url("https://example.com", timeout=0.5) is False

    def test_request_open_url_returns_true_when_the_companion_opens_it(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: True)
        server = cc.CompanionChannelServer()
        server.start()
        try:
            assert cc.request_open_url("https://example.com/callback") is True
        finally:
            server.stop()


class TestReadBaseUrl:
    def test_none_when_no_daemon_has_written_it(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        assert cc.read_base_url() is None

    def test_reads_back_what_was_written(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        (tmp_path / cc.WEB_BASE_URL_FILE_NAME).write_text("http://127.0.0.1:8765", encoding="utf-8")
        assert cc.read_base_url() == "http://127.0.0.1:8765"


class TestAttestedMintCommands:
    """The self-approval plan's Phase 2: ``MINT`` grew two attested shapes,
    because the one it had could not say who asked for the session it minted
    -- and every session it minted could approve (web/session_auth.py's own
    ``PROVENANCE_*`` comment).

    Both shapes cost a round trip into the companion process. Neither is
    authentication of that process (companion and agent share a uid, ADR 0002
    decision 6) -- what they establish is that the line reached the process a
    human is actually in front of, which the bare ``MINT`` never did.
    """

    def _dispatch(self, line: str, store: BootstrapStore, **kwargs) -> str:
        return cc._handle_daemon_request(store, allow_quit=True, line=line, **kwargs)

    def test_a_bare_mint_is_unattested(self):
        store = BootstrapStore()
        reply = self._dispatch("MINT\n", store)
        assert reply.startswith("OK ")
        provenance, principal_id = store.consume(reply[len("OK "):].strip())
        assert provenance == sa.PROVENANCE_UNATTESTED
        assert principal_id == "local"

    def test_a_confirmed_companion_nonce_mints_a_human_code(self):
        store = BootstrapStore()
        seen: list[str] = []
        reply = self._dispatch(
            "MINT COMPANION abc123\n", store,
            confirm_companion_mint=lambda nonce: bool(seen.append(nonce)) or True,
        )
        assert seen == ["abc123"]
        provenance, principal_id = store.consume(reply[len("OK "):].strip())
        assert provenance == sa.PROVENANCE_HUMAN
        assert principal_id == "local"

    def test_an_unconfirmed_companion_nonce_mints_nothing_at_all(self):
        store = BootstrapStore()
        reply = self._dispatch("MINT COMPANION abc123\n", store, confirm_companion_mint=lambda nonce: False)
        # Not "mints an unattested one instead": a caller that asked for an
        # attested code and was refused must not be handed a weaker one it
        # would then treat as the thing it asked for.
        assert reply == "ERROR that mint was not confirmed by the companion\n"
        provenance = store.consume(reply.split()[-1])
        assert provenance is None

    def test_a_companion_mint_with_no_nonce_is_refused_without_asking(self):
        asked: list[str] = []
        reply = self._dispatch(
            "MINT COMPANION\n", BootstrapStore(),
            confirm_companion_mint=lambda nonce: bool(asked.append(nonce)) or True,
        )
        assert reply.startswith("ERROR")
        assert asked == []

    def test_a_confirmed_console_mint_is_human(self):
        store = BootstrapStore()
        reply = self._dispatch("MINT CONSOLE\n", store, confirm_console_mint=lambda: (True, ""))
        provenance, principal_id = store.consume(reply[len("OK "):].strip())
        assert provenance == sa.PROVENANCE_HUMAN
        assert principal_id == "local"

    def test_a_denied_console_mint_passes_the_reason_back(self):
        reply = self._dispatch(
            "MINT CONSOLE\n", BootstrapStore(),
            confirm_console_mint=lambda: (False, "the sign-in link was denied"),
        )
        assert reply == "ERROR the sign-in link was denied\n"

    def test_an_unknown_mint_shape_is_not_a_silently_unattested_mint(self):
        assert self._dispatch("MINT SOMETHING\n", BootstrapStore()) == "ERROR unknown command\n"


class TestMintNonces:
    """What makes ``MINT COMPANION`` attestable: a nonce the *companion*
    issues to itself and the daemon hands straight back. Nothing here is a
    secret the daemon keeps -- only the process that issued one can
    recognize it, and that is the process a human clicked."""

    def test_an_issued_nonce_is_accepted_exactly_once(self):
        nonce = cc.issue_mint_nonce()
        assert cc._consume_mint_nonce(nonce) is True
        assert cc._consume_mint_nonce(nonce) is False

    def test_a_nonce_this_process_never_issued_is_refused(self):
        assert cc._consume_mint_nonce("not-a-real-nonce") is False
        assert cc._consume_mint_nonce("") is False

    def test_an_expired_nonce_is_refused(self, monkeypatch):
        fake_now = [1000.0]
        monkeypatch.setattr(cc.time, "time", lambda: fake_now[0])
        nonce = cc.issue_mint_nonce()
        fake_now[0] += cc._MINT_NONCE_TTL_SECONDS + 1
        assert cc._consume_mint_nonce(nonce) is False

    def test_two_nonces_are_distinct(self):
        assert cc.issue_mint_nonce() != cc.issue_mint_nonce()


class TestConfirmMintCommand:
    """``CONFIRM MINT <nonce>`` on the companion's own channel -- the
    call-back half of the pair above. No dialog: the human already clicked
    the menu item in this process moments ago, and asking again would put a
    second confirmation in front of somebody who just answered the first."""

    def _server(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        server = cc.CompanionChannelServer()
        server.start()
        return server

    def test_a_nonce_this_companion_issued_is_confirmed(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch)
        nonce = cc.issue_mint_nonce()
        try:
            assert _mint(server.address, message=f"CONFIRM MINT {nonce}\n").startswith("OK")
            # Single-use all the way through: replaying the same line is a
            # refusal, not a second attested session.
            assert _mint(server.address, message=f"CONFIRM MINT {nonce}\n").startswith("ERROR")
        finally:
            server.stop()

    def test_an_unknown_nonce_is_refused(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch)
        try:
            reply = _mint(server.address, message="CONFIRM MINT made-up\n")
        finally:
            server.stop()
        assert reply.startswith("ERROR")
        assert "no sign-in was requested" in reply

    def test_an_unknown_confirm_subject_is_still_unknown(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch)
        try:
            assert _mint(server.address, message="CONFIRM SOMETHING\n") == "ERROR unknown command\n"
        finally:
            server.stop()


class TestConfirmSignInCommand:
    """``CONFIRM SIGNIN``: the dialog behind ``privacyfence-app
    --print-sign-in-link``. The dialog *is* the gate -- the command runs as
    the same OS user the agent does, so what makes the resulting session
    attributable to a person is that a person clicked Allow."""

    def test_an_allowed_dialog_answers_ok(self, monkeypatch):
        asked = []
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: asked.append(prompt) or True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        assert cc._handle_companion_request("CONFIRM SIGNIN\n") == "OK\n"
        assert asked == [cc._CONFIRM_SIGN_IN_PROMPT]

    def test_a_denied_dialog_answers_error(self, monkeypatch):
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: False)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        reply = cc._handle_companion_request("CONFIRM SIGNIN\n")
        assert reply.startswith("ERROR") and "denied" in reply

    def test_the_daemon_side_turns_a_missing_companion_into_an_actionable_refusal(
        self, tmp_path, monkeypatch,
    ):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)

        confirmed, reason = cc.request_sign_in_confirmation(timeout=0.5)

        assert confirmed is False
        assert "companion" in reason


class TestShowCommand:
    """``SHOW <path>``: a one-shot companion invocation (Linux's
    applications-menu click) handing the job to whichever process owns the
    channel, because that is the only one the daemon can call back.

    Unlike ``CONFIRM MINT``, this one has no evidence of its own that a
    human is behind it -- it arrives from another process running as this
    same OS user, and the agent is indistinguishable from the menu click it
    exists for. So the dialog is what makes the session it produces
    attributable to a person, and these tests exist mostly to pin that it
    cannot be skipped.
    """

    @pytest.fixture
    def allow(self, monkeypatch):
        asked: list[str] = []
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: asked.append(prompt) or True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        return asked

    def test_an_allowed_dialog_opens_an_attested_link(self, allow, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(cc, "open_attested_url", lambda path: (bool(opened.append(path)) or True, ""))

        assert cc._handle_companion_request("SHOW /approvals\n") == "OK\n"

        assert opened == ["/approvals"]
        assert allow == [cc._CONFIRM_SHOW_PROMPT]

    def test_a_denied_dialog_mints_nothing_at_all(self, monkeypatch):
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: False)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc, "open_attested_url", lambda path: pytest.fail("minted after a Deny"))

        reply = cc._handle_companion_request("SHOW /approvals\n")

        assert reply.startswith("ERROR")
        assert "denied" in reply

    def test_a_page_outside_the_allowlist_is_refused_without_asking(self, allow):
        assert cc._handle_companion_request("SHOW /security\n") == "ERROR unknown page\n"
        # Not a URL either: SHOW reaches the process that can mint a session
        # able to approve, so the caller never picks where it lands.
        assert cc._handle_companion_request("SHOW http://evil.example/\n") == "ERROR unknown page\n"
        assert allow == []

    def test_a_failure_to_open_passes_its_reason_back(self, allow, monkeypatch):
        monkeypatch.setattr(cc, "open_attested_url", lambda path: (False, "could not open a browser"))
        assert cc._handle_companion_request("SHOW /settings\n") == "ERROR could not open a browser\n"


class TestOpenAttestedUrl:
    """The companion's own Open Approvals, end to end within this module --
    one implementation shared by the tray handler and the ``SHOW`` handler so
    the two cannot drift apart."""

    def test_it_mints_through_the_companion_shape_and_opens_that_url(self, monkeypatch):
        monkeypatch.setattr(cc, "read_base_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(cc, "mint_attested_bootstrap_code", lambda *, timeout: "the-code")
        opened: list[str] = []
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: bool(opened.append(url)) or True)

        assert cc.open_attested_url("/approvals") == (True, "")
        assert opened == ["http://127.0.0.1:8765/approvals?bootstrap=the-code"]

    def test_no_daemon_running_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(cc, "read_base_url", lambda: None)
        opened, reason = cc.open_attested_url("/approvals")
        assert opened is False
        assert "does not appear to be running" in reason

    def test_an_unreachable_control_channel_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(cc, "read_base_url", lambda: "http://127.0.0.1:8765")

        def _boom(*, timeout):
            raise cc.ControlChannelError("mint refused")

        monkeypatch.setattr(cc, "mint_attested_bootstrap_code", _boom)
        opened, reason = cc.open_attested_url("/approvals")
        assert opened is False
        assert "mint refused" in reason

    def test_a_browser_that_will_not_open_is_reported(self, monkeypatch):
        monkeypatch.setattr(cc, "read_base_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(cc, "mint_attested_bootstrap_code", lambda *, timeout: "the-code")
        monkeypatch.setattr(cc.webbrowser, "open", lambda url: False)
        assert cc.open_attested_url("/approvals") == (False, "could not open a browser")


class TestEveryMintIsAudited:
    """The self-approval plan's Phase 2: before it, exactly one of the three
    ways to a session wrote an audit entry -- the MCP sign-in-link tool, now
    retired -- and the two silent ones were the two anything on this machine
    could use. The log recorded the sanctioned path and not the reachable
    ones."""

    @pytest.fixture(autouse=True)
    def _isolated_audit_log(self, tmp_path):
        from privacyfence.audit_log import init_audit_logger

        init_audit_logger(str(tmp_path / "audit"))
        self._audit_dir = tmp_path / "audit"

    def _entries(self):
        import json

        from privacyfence.audit_log import current_week

        path = self._audit_dir / f"{current_week()}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _dispatch(self, line: str, **kwargs) -> str:
        return cc._handle_daemon_request(BootstrapStore(), allow_quit=True, line=line, **kwargs)

    def test_a_bare_mint_is_audited_as_unattested(self):
        self._dispatch("MINT\n")
        entries = self._entries()
        assert [e["decision"] for e in entries] == [cc.SIGN_IN_MINT_DECISION]
        assert "unattested" in entries[0]["summary"]

    def test_a_companion_mint_is_audited_as_one_that_can_approve(self):
        self._dispatch("MINT COMPANION n1\n", confirm_companion_mint=lambda nonce: True)
        assert "can approve" in self._entries()[0]["summary"]

    def test_a_refused_companion_mint_is_audited_too(self):
        self._dispatch("MINT COMPANION n1\n", confirm_companion_mint=lambda nonce: False)
        summary = self._entries()[0]["summary"]
        assert summary.startswith("Refused")
        assert "did not confirm" in summary

    def test_a_console_mint_names_the_command_that_asked(self):
        self._dispatch("MINT CONSOLE\n", confirm_console_mint=lambda: (True, ""))
        assert "--print-sign-in-link" in self._entries()[0]["summary"]

    def test_a_refused_console_mint_carries_the_reason(self):
        self._dispatch("MINT CONSOLE\n", confirm_console_mint=lambda: (False, "the sign-in link was denied"))
        assert "the sign-in link was denied" in self._entries()[0]["summary"]

    def test_an_unwritable_audit_log_does_not_cost_the_human_their_link(self, monkeypatch):
        """A human locked out because the audit log could not be written
        would be locked out by the thing meant to reassure them."""
        from privacyfence.audit_log import get_audit_logger

        def _boom(entry):
            raise OSError("disk full")

        monkeypatch.setattr(get_audit_logger(), "record", _boom)

        reply = self._dispatch("MINT\n")

        assert reply.startswith("OK ")
        assert self._entries() == []


class TestAttestedMintClientHelpers:
    """The client halves of the two attested shapes, driven against *both*
    real servers at once -- which is the only way to exercise what makes
    them attested: the daemon's call-back into the companion process while
    that process is waiting on its own reply. A test that stubbed either
    side would prove the protocol to itself."""

    @pytest.fixture
    def both_channels(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        bootstrap = BootstrapStore()
        daemon = cc.ControlChannelServer(bootstrap=bootstrap)
        companion = cc.CompanionChannelServer()
        daemon.start()
        companion.start()
        try:
            yield bootstrap
        finally:
            companion.stop()
            daemon.stop()

    def test_the_companion_shape_mints_a_code_that_may_approve(self, both_channels):
        code = cc.mint_attested_bootstrap_code()

        provenance, principal_id = both_channels.consume(code)
        assert provenance == sa.PROVENANCE_HUMAN
        assert principal_id == "local"

    def test_a_nonce_the_companion_never_issued_gets_no_code(self, both_channels, monkeypatch):
        """What an agent sending this line by hand hits: it cannot produce a
        nonce the companion would recognize, and it cannot read the one the
        companion did issue."""
        monkeypatch.setattr(cc, "issue_mint_nonce", lambda: "not-a-real-nonce")

        with pytest.raises(cc.ControlChannelError):
            cc.mint_attested_bootstrap_code()

    def test_the_console_shape_mints_one_when_the_dialog_is_allowed(self, both_channels, monkeypatch):
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")

        code = cc.mint_console_bootstrap_code(timeout=5.0)

        provenance, principal_id = both_channels.consume(code)
        assert provenance == sa.PROVENANCE_HUMAN
        assert principal_id == "local"

    def test_a_denied_dialog_reaches_the_terminal_in_words(self, both_channels, monkeypatch):
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: False)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")

        with pytest.raises(cc.ControlChannelError) as excinfo:
            cc.mint_console_bootstrap_code(timeout=5.0)

        # The reason is written where it is known -- the companion's dialog
        # handler -- and passed through unchanged by everything between.
        assert "denied" in str(excinfo.value)
        assert "ERROR" not in str(excinfo.value)  # protocol, not prose

    def test_request_show_reaches_the_running_companion(self, both_channels, monkeypatch):
        shown = []
        monkeypatch.setattr(cc, "_confirm_linux", lambda prompt, *, timeout: True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc, "open_attested_url", lambda path: (bool(shown.append(path)) or True, ""))

        assert cc.request_show("/approvals", timeout=10.0) is True
        assert shown == ["/approvals"]

    def test_request_mint_attestation_is_true_only_for_a_live_nonce(self, both_channels):
        nonce = cc.issue_mint_nonce()

        assert cc.request_mint_attestation(nonce) is True
        assert cc.request_mint_attestation(nonce) is False  # single-use


class TestAttestedMintWithNoCompanion:
    """Every one of these is the same answer from a different angle: with no
    companion running, nothing can vouch for a mint, and the caller is told
    rather than quietly handed a weaker credential."""

    @pytest.fixture(autouse=True)
    def _daemon_only(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        server = cc.ControlChannelServer(bootstrap=BootstrapStore())
        server.start()
        try:
            yield
        finally:
            server.stop()

    def test_the_companion_shape_raises(self):
        with pytest.raises(cc.ControlChannelError):
            cc.mint_attested_bootstrap_code(timeout=2.0)

    def test_the_console_shape_raises_with_a_reason_naming_the_companion(self):
        with pytest.raises(cc.ControlChannelError) as excinfo:
            cc.mint_console_bootstrap_code(timeout=2.0)
        assert "companion" in str(excinfo.value)

    def test_request_show_is_false_rather_than_raising(self):
        # companion.py falls back from here rather than failing the click.
        assert cc.request_show("/approvals", timeout=1.0) is False

    def test_request_mint_attestation_is_false_rather_than_raising(self):
        assert cc.request_mint_attestation("some-nonce", timeout=1.0) is False

    def test_a_bare_mint_still_works(self):
        # The view-only path is the one that must never depend on anything
        # else being up -- it is what a locked-out human falls back to.
        assert cc.mint_bootstrap_code()


class TestPeerIdentityPosix:
    """ADR 0008: ``peer_identity_posix()`` over a real ``socket.socketpair()``
    -- the same primitive ``_peer_uid_posix()`` already uses, generalized
    into a resolvable identity rather than a bare uid."""

    def test_resolves_this_process_own_identity(self):
        import pwd

        server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            peer = cc.peer_identity_posix(server_sock)
            assert peer is not None
            assert peer.uid == str(os.getuid())
            assert peer.name == pwd.getpwuid(os.getuid()).pw_name
        finally:
            server_sock.close()
            client_sock.close()

    def test_none_when_so_peercred_is_unavailable(self, monkeypatch):
        monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "freebsd")
        server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            assert cc.peer_identity_posix(server_sock) is None
        finally:
            server_sock.close()
            client_sock.close()


class TestPrincipalIdForPeer:
    """ADR 0008's own owner/non-owner mapping -- the local-mode-fixes plan's
    §3.1 "the account named by the marker's owner_user maps to the existing
    LOCAL_PRINCIPAL; every other service-group member maps to os-<uid>"."""

    def test_unseparated_is_always_local(self, monkeypatch):
        monkeypatch.setattr(cc.privilege_separation, "is_enabled", lambda: False)
        peer = cc.OsUser(uid="12345", name="whoever")

        assert cc.principal_id_for_peer(peer) == "local"

    def test_no_peer_identity_is_always_local(self, monkeypatch):
        monkeypatch.setattr(cc.privilege_separation, "is_enabled", lambda: True)

        assert cc.principal_id_for_peer(None) == "local"

    def test_the_owner_maps_to_local(self, monkeypatch):
        monkeypatch.setattr(cc.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc.privilege_separation, "owner_uid", lambda: 1001)
        peer = cc.OsUser(uid="1001", name="alice")

        assert cc.principal_id_for_peer(peer) == "local"

    def test_a_different_uid_maps_to_its_own_os_principal(self, monkeypatch):
        monkeypatch.setattr(cc.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc.privilege_separation, "owner_uid", lambda: 1001)
        peer = cc.OsUser(uid="1002", name="bob")

        assert cc.principal_id_for_peer(peer) == "os-1002"

    def test_an_unresolvable_owner_still_maps_a_peer_to_its_own_principal(self, monkeypatch):
        # A half-removed install (the marker names an owner account that no
        # longer exists): fails toward isolation, not toward everyone
        # sharing LOCAL_PRINCIPAL.
        monkeypatch.setattr(cc.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc.privilege_separation, "owner_uid", lambda: None)
        peer = cc.OsUser(uid="1002", name="bob")

        assert cc.principal_id_for_peer(peer) == "os-1002"

    def test_windows_uses_owner_sid(self, monkeypatch):
        monkeypatch.setattr(cc.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "win32")
        monkeypatch.setattr(cc.privilege_separation, "owner_sid", lambda: "S-1-5-21-1-2-3-1001")
        owner_peer = cc.OsUser(uid="S-1-5-21-1-2-3-1001", name="alice")
        other_peer = cc.OsUser(uid="S-1-5-21-1-2-3-1002", name="bob")

        assert cc.principal_id_for_peer(owner_peer) == "local"
        assert cc.principal_id_for_peer(other_peer) == "os-S-1-5-21-1-2-3-1002"


class TestMintMcpToken:
    """ADR 0008 §3.2: ``MINT MCP``/``ROTATE MCP``, and the peer-scoped
    dispatch every ``ControlChannelServer`` command now runs inside."""

    def _dispatch(self, line: str, *, mint_mcp_token, peer=None) -> str:
        server = cc._LineProtocolServer(
            handler=lambda ln: cc._handle_daemon_request(
                BootstrapStore(), allow_quit=True, line=ln, mint_mcp_token=mint_mcp_token,
            ),
            socket_path=lambda: None, pipe_name=lambda: None, thread_name="t",
            scope_by_peer_principal=True,
        )
        return server._dispatch(line, peer)

    def test_mint_mcp_with_no_callback_is_refused(self):
        reply = self._dispatch("MINT MCP\n", mint_mcp_token=None)
        assert reply.startswith("ERROR")

    def test_rotate_mcp_with_no_callback_is_refused(self):
        reply = self._dispatch("ROTATE MCP\n", mint_mcp_token=None)
        assert reply.startswith("ERROR")

    def test_mint_mcp_runs_the_callback_inside_the_peers_own_scope(self, monkeypatch):
        monkeypatch.setattr(cc.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc.privilege_separation, "owner_uid", lambda: 1001)
        seen = []

        def mint(rotate: bool) -> str:
            from privacyfence.principal import current_principal

            seen.append((rotate, current_principal().id))
            return "sometoken"

        reply = self._dispatch(
            "MINT MCP\n", mint_mcp_token=mint, peer=cc.OsUser(uid="9999", name="bob"),
        )
        assert reply == "OK sometoken\n"
        assert seen == [(False, "os-9999")]

    def test_rotate_mcp_passes_rotate_true(self):
        seen = []

        def mint(rotate: bool) -> str:
            seen.append(rotate)
            return "freshtoken"

        reply = self._dispatch("ROTATE MCP\n", mint_mcp_token=mint)
        assert reply == "OK freshtoken\n"
        assert seen == [True]

    def test_mint_binds_the_session_to_the_peers_principal(self, monkeypatch):
        monkeypatch.setattr(cc.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(cc.privilege_separation, "current_platform", lambda: "linux")
        monkeypatch.setattr(cc.privilege_separation, "owner_uid", lambda: 1001)
        store = BootstrapStore()
        server = cc._LineProtocolServer(
            handler=lambda ln: cc._handle_daemon_request(store, allow_quit=True, line=ln),
            socket_path=lambda: None, pipe_name=lambda: None, thread_name="t",
            scope_by_peer_principal=True,
        )
        reply = server._dispatch("MINT\n", cc.OsUser(uid="4242", name="carol"))
        assert reply.startswith("OK ")
        code = reply[len("OK "):].strip()
        provenance, principal_id = store.consume(code)
        assert provenance == sa.PROVENANCE_UNATTESTED
        assert principal_id == "os-4242"


class TestPerUserCompanionAddress:
    """ADR 0008 §3.5: the owner keeps the unsuffixed address; any other
    principal gets its own, so distinct OS users' companions can never
    collide."""

    def test_the_owner_keeps_the_unsuffixed_address(self, tmp_path):
        path = cc.companion_socket_path_under(tmp_path, "local")
        assert path == tmp_path / "companion.sock"

    def test_another_principal_gets_a_suffixed_address(self, tmp_path):
        path = cc.companion_socket_path_under(tmp_path, "os-1002")
        assert path == tmp_path / "companion-1002.sock"

    def test_two_principals_never_collide(self, tmp_path):
        a = cc.companion_socket_path_under(tmp_path, "os-1001")
        b = cc.companion_socket_path_under(tmp_path, "os-1002")
        assert a != b

    def test_pipe_name_is_suffixed_the_same_way(self, tmp_path):
        owner_pipe = cc.companion_pipe_name_for(tmp_path, "local")
        other_pipe = cc.companion_pipe_name_for(tmp_path, "os-S-1-5-21-1-2-3-1002")
        assert owner_pipe != other_pipe
        assert owner_pipe.startswith("\\\\.\\pipe\\PrivacyFence-Companion-")
        assert other_pipe.endswith("-S-1-5-21-1-2-3-1002")
