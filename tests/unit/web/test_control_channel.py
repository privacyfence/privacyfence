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
