"""#428 Phase 2: the control channel that replaces ``web_token`` -> ``POST
/api/bootstrap`` as the way a bootstrap code gets minted on demand.

Through Phase 1, minting a fresh code without restarting the daemon meant
presenting the persistent local secret (``web/server.py``'s old
``load_or_create_token()``, read from a file under ``paths.authority_dir()``)
as a Bearer header over the same loopback HTTP port the browser uses. That
secret's only remaining job, once SEC-06 got everything else off it, was
authorizing that one endpoint -- and the endpoint itself was reachable by
anything on the machine that could read the token file, agent included.
Phase 2 doesn't try to make that check stronger (ADR 0002's "a chain doesn't
get stronger when you strengthen its middle" -- see ``session_auth.py``'s own
module docstring); it retires the file-and-bearer-header design entirely and
replaces it with a channel a browser cannot reach at all:

- **macOS/Linux**: a Unix domain socket, created 0600 under the same
  ``authority`` root the old token file lived in (falling back to a short
  path in the system temp directory when that one doesn't fit ``AF_UNIX``'s
  fixed-size ``sun_path`` buffer -- see ``posix_socket_path()``).
- **Windows**: a named pipe in the machine-global ``\\\\.\\pipe\\`` namespace,
  created with a security descriptor whose DACL grants access to the current
  user's SID alone -- see ``_current_user_security_attributes()``.

Both are still reachable by anything running as the same OS user, agent
included -- Phase 2 is explicitly "still same uid, so still no security gain
alone" (issue #428). What it buys is the interface: a channel the browser's
own loopback TCP connection categorically cannot speak (no ``fetch()`` to a
Unix socket or a named pipe from a web page), which is what Phase 3's
companion app needs to exist as the thing that *can* speak it, and what
Phase 4's privilege separation needs already file-permissioned/ACL'd the way
a service-owned resource has to be.

The protocol is deliberately minimal -- one command, because minting a
bootstrap code is the one thing this channel replaces: a client sends a
single line, ``MINT\\n``, and gets back either ``OK <code>\\n`` or
``ERROR <reason>\\n``. The code itself is exactly what
``session_auth.BootstrapStore.mint()`` always produced -- this channel is a
new way to *reach* that call, not a new kind of credential. Redeeming the
code is unchanged: a client still does that over the browser's own loopback
HTTP, via ``?bootstrap=<code>`` (``web/server.py``'s ``_BootstrapMiddleware``).
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import socket
import tempfile
import threading
from pathlib import Path

from .. import paths
from .session_auth import BootstrapStore

logger = logging.getLogger(__name__)

_ENCODING = "utf-8"
_MAX_MESSAGE_BYTES = 4096

# AF_UNIX's sun_path is a fixed-size kernel buffer -- 108 bytes on Linux, 104
# on macOS, both including the trailing NUL. Staying comfortably under the
# smaller of the two (with margin for the NUL and any encoding quirks) rather
# than hair-splitting the exact platform limit.
_MAX_SUN_PATH_BYTES = 100

SOCKET_FILE_NAME = "control.sock"


def socket_path_under(authority_dir: Path) -> Path:
    """The pure half of ``posix_socket_path()`` -- everything after
    resolving *which* authority directory to build on, split out so a
    display-only caller (``session_auth.py``'s ``unauthorized_html()``) can
    compute the same fallback logic from a plain ``data_dir() / "authority"``
    join, without going through the real, side-effecting
    ``paths.authority_dir()`` (which creates the directory and runs its
    migration-on-first-use) just to render an error page -- the same
    "pure display string" posture that page's own docstring has always
    taken for the paths it shows."""
    preferred = authority_dir / SOCKET_FILE_NAME
    if len(str(preferred).encode(_ENCODING)) < _MAX_SUN_PATH_BYTES:
        return preferred
    digest = hashlib.sha256(str(preferred.parent).encode(_ENCODING)).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"privacyfence-control-{digest}.sock"


def posix_socket_path() -> Path:
    """Where the control channel binds on macOS/Linux: ``control.sock``
    alongside the old ``web_token`` under ``paths.authority_dir()`` -- unless
    that path is too long for ``AF_UNIX`` to bind at all, which a real
    install's ``~/.privacyfence``/``%LOCALAPPDATA%`` never gets close to but
    a test's own deeply-nested ``tmp_path`` sometimes does. The fallback
    lands in the system temp directory (always short) under a name keyed by
    a hash of the real authority directory, so it stays deterministic and
    reproducible from ``paths.authority_dir()`` alone, without needing a
    discovery file of its own."""
    return socket_path_under(paths.authority_dir())


def pipe_name_for(data_dir: Path) -> str:
    """The pure half of ``windows_pipe_name()`` -- takes a ``data_dir()``
    value directly rather than calling that function itself (also
    side-effecting: it ``secure_mkdir``s the directory), so a caller that
    already knows a *different* process's data directory (a test driving a
    real daemon subprocess against its own sandboxed one, e.g.) can compute
    the exact same pipe name that process's own ``windows_pipe_name()``
    would, without touching this process's filesystem."""
    digest = hashlib.sha256(str(data_dir).encode(_ENCODING)).hexdigest()[:16]
    return f"\\\\.\\pipe\\PrivacyFence-Control-{digest}"


def windows_pipe_name() -> str:
    """Where the control channel listens on Windows -- a named pipe in the
    machine-wide ``\\\\.\\pipe\\`` namespace (there is no per-user or
    per-directory scoping the way a filesystem path gives POSIX), so the name
    itself has to be what keeps two installs (or a test and a real install)
    from colliding: a hash of ``paths.data_dir()``, the one thing that's
    already unique per install. Access control is the security descriptor
    ``_current_user_security_attributes()`` builds, not the name -- this is
    a namespacing device, not a secret."""
    return pipe_name_for(paths.data_dir())


def _handle_request(bootstrap: BootstrapStore, line: str) -> str:
    command = line.strip().split(maxsplit=1)[0].upper() if line.strip() else ""
    if command != "MINT":
        return "ERROR unknown command\n"
    return f"OK {bootstrap.mint()}\n"


class ControlChannelServer:
    """Runs the control channel on its own background thread -- started and
    stopped alongside the rest of ``WebServer``'s lifecycle (see that
    class's own ``start()``/``stop()``), sharing the same ``BootstrapStore``
    the loopback HTTP app's ``_BootstrapMiddleware`` already consumes codes
    from, so a code minted here is redeemed exactly like one minted at
    startup by ``mint_bootstrap_url()``.
    """

    def __init__(self, *, bootstrap: BootstrapStore) -> None:
        self._bootstrap = bootstrap
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # What a client needs to connect: the socket path (POSIX) or pipe
        # name (Windows) this instance actually bound/is listening on --
        # None until start() has run.
        self.address: str | None = None
        self._posix_socket: socket.socket | None = None

    def start(self) -> None:
        if paths.is_windows():
            self._start_windows()
        else:
            self._start_posix()

    def stop(self) -> None:
        self._stop_event.set()
        if paths.is_windows():
            self._unblock_windows_accept()
        elif self._posix_socket is not None:
            with contextlib.suppress(OSError):
                self._posix_socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if not paths.is_windows() and self.address is not None:
            with contextlib.suppress(OSError):
                Path(self.address).unlink()

    # -- POSIX: a Unix domain socket ---------------------------------------- #

    def _start_posix(self) -> None:
        sock_path = posix_socket_path()
        # Any file already at this path is stale: the caller (WebServer,
        # constructed only after daemon_main.py's own single-instance lock
        # succeeds) is the only local-mode process that will ever bind here,
        # so nothing legitimate could still be listening on it.
        with contextlib.suppress(OSError):
            sock_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        sock_path.chmod(0o600)
        sock.listen(8)
        # Short timeout, not a blocking accept() -- lets the accept loop
        # notice _stop_event between connections without needing a
        # self-pipe/wakeup socket just to interrupt a blocking call.
        sock.settimeout(0.5)
        self._posix_socket = sock
        self.address = str(sock_path)
        self._thread = threading.Thread(target=self._accept_loop_posix, name="control-channel", daemon=True)
        self._thread.start()

    def _accept_loop_posix(self) -> None:
        assert self._posix_socket is not None  # nosec B101  # invariant narrowing, not input validation
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._posix_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                self._serve_one_posix(conn)

    def _serve_one_posix(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        try:
            data = conn.recv(_MAX_MESSAGE_BYTES)
        except OSError:
            return
        line = data.decode(_ENCODING, errors="replace")
        try:
            response = _handle_request(self._bootstrap, line)
        except Exception:
            logger.exception("Control channel request failed")
            response = "ERROR internal error\n"
        with contextlib.suppress(OSError):
            conn.sendall(response.encode(_ENCODING))

    # -- Windows: a named pipe, ACL'd to the current user ------------------- #

    def _start_windows(self) -> None:
        self.address = windows_pipe_name()
        self._thread = threading.Thread(target=self._accept_loop_windows, name="control-channel", daemon=True)
        self._thread.start()

    def _accept_loop_windows(self) -> None:
        import pywintypes
        import win32file
        import win32pipe
        import winerror

        pipe_name = self.address
        assert pipe_name is not None  # nosec B101  # invariant narrowing, not input validation
        security_attributes = _current_user_security_attributes()
        while not self._stop_event.is_set():
            try:
                handle = win32pipe.CreateNamedPipe(
                    pipe_name,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    _MAX_MESSAGE_BYTES, _MAX_MESSAGE_BYTES,
                    0,
                    security_attributes,
                )
            except pywintypes.error:
                logger.exception("Could not create control channel pipe instance")
                break
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as exc:
                # A client that connected in the window between
                # CreateNamedPipe and ConnectNamedPipe is reported this way
                # rather than as a failure -- the handle is already usable.
                if exc.winerror != winerror.ERROR_PIPE_CONNECTED:
                    win32file.CloseHandle(handle)
                    continue
            if self._stop_event.is_set():
                # Only ever reached via _unblock_windows_accept()'s own
                # dummy connect, which stop() makes right after setting this
                # event -- nothing else should be treated as a real client.
                win32file.CloseHandle(handle)
                break
            try:
                self._serve_one_windows(handle)
            finally:
                with contextlib.suppress(pywintypes.error):
                    win32pipe.DisconnectNamedPipe(handle)
                win32file.CloseHandle(handle)

    def _serve_one_windows(self, handle) -> None:  # noqa: ANN001 -- a pywin32 PyHANDLE, no type stub
        import pywintypes
        import win32file
        import win32pipe

        # Diagnostic only -- ADR 0002 decision 6 is explicit that a peer's
        # pid/uid can never distinguish "the human" from "the agent" while
        # both run under the same account, so this is logged for whoever
        # reads the daemon's audit trail later, never checked as an
        # authorization gate.
        with contextlib.suppress(Exception):
            logger.debug("Control channel connection from pid %s", win32pipe.GetNamedPipeClientProcessId(handle))
        try:
            _rc, data = win32file.ReadFile(handle, _MAX_MESSAGE_BYTES)
        except pywintypes.error:
            return
        line = data.decode(_ENCODING, errors="replace")
        try:
            response = _handle_request(self._bootstrap, line)
        except Exception:
            logger.exception("Control channel request failed")
            response = "ERROR internal error\n"
        with contextlib.suppress(pywintypes.error):
            win32file.WriteFile(handle, response.encode(_ENCODING))

    def _unblock_windows_accept(self) -> None:
        """``win32pipe.ConnectNamedPipe`` blocks until a client connects --
        the same self-connect trick a POSIX server would need a self-pipe
        for. A dummy client is indistinguishable, on the server side, from a
        request that arrives just as shutdown starts; the accept loop's own
        ``_stop_event`` check (set just before this runs) is what tells
        them apart."""
        if self.address is None:
            return
        with contextlib.suppress(Exception):
            import win32file

            handle = win32file.CreateFile(
                self.address,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            win32file.CloseHandle(handle)


def _current_user_security_attributes():  # noqa: ANN201 -- a pywin32 SECURITY_ATTRIBUTES, no type stub
    """A ``SECURITY_ATTRIBUTES`` whose DACL grants full access to the
    current process token's own user SID and nothing else -- the "real
    security descriptor" half of #428 Phase 2's named-pipe requirement.
    Windows has no filesystem permission bits (``paths.py``'s own
    ``secure_mkdir`` docstring), so a named pipe's ACL is the actual
    access-control primitive here, not a chmod equivalent applied
    afterwards."""
    import ntsecuritycon
    import win32api
    import win32security

    process_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    user_sid, _attributes = win32security.GetTokenInformation(process_token, win32security.TokenUser)

    security_descriptor = win32security.SECURITY_DESCRIPTOR()
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user_sid)
    security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)

    security_attributes = win32security.SECURITY_ATTRIBUTES()
    security_attributes.SECURITY_DESCRIPTOR = security_descriptor
    return security_attributes


__all__ = [
    "SOCKET_FILE_NAME",
    "ControlChannelServer",
    "pipe_name_for",
    "posix_socket_path",
    "socket_path_under",
    "windows_pipe_name",
]
