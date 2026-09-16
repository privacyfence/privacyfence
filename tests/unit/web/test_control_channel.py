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
    def test_lives_under_the_authority_root_by_default(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        path = cc.posix_socket_path()
        assert path == tmp_path / "authority" / "control.sock"

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
            reply = _mint(server.address, message="QUIT\n")
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
