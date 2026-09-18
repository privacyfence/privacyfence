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
  user's SID alone -- plus, once #428 Phase 4 has split the daemon and the
  companion into two accounts, to each of those. See
  ``_current_user_security_attributes()``.

Both are still reachable by anything running as the same OS user, agent
included -- Phase 2 is explicitly "still same uid, so still no security gain
alone" (issue #428). What it buys is the interface: a channel the browser's
own loopback TCP connection categorically cannot speak (no ``fetch()`` to a
Unix socket or a named pipe from a web page), which is what Phase 3's
companion app needs to exist as the thing that *can* speak it, and what
Phase 4's privilege separation needs already file-permissioned/ACL'd the way
a service-owned resource has to be.

The protocol is deliberately minimal -- originally one command (``MINT``),
because minting a bootstrap code was the one thing this channel replaced.
Phase 3 (ADR 0002, ``docs/adr/0002-local-mode-trust-boundary-and-companion-
app.md``) adds a second: ``QUIT``, so the companion's tray/menu-bar "Quit"
item and Linux's XDG launcher "Quit" action can stop the daemon without a
browser -- gated by the same ``allow_quit`` setting the web settings page's
own Quit action already respects. Phase 4 (#428 B4) narrows that further: on
a privilege-separated install this socket is ``0660`` group-shared so the
companion can still reach it, which puts the agent in the same group, so
``QUIT`` refuses unconditionally there regardless of ``allow_quit`` -- a
system service is not this channel's to stop, only its service manager's
(``privilege_separation.PlatformLayout.stop_command``). A client sends a
single line, ``MINT\\n`` or ``QUIT\\n``, and gets back either
``OK[ <value>]\\n`` or
``ERROR <reason>\\n``. The MINT code itself is exactly what
``session_auth.BootstrapStore.mint()`` always produced -- this channel is a
new way to *reach* that call, not a new kind of credential. Redeeming the
code is unchanged: a client still does that over the browser's own loopback
HTTP, via ``?bootstrap=<code>`` (``web/server.py``'s ``_BootstrapMiddleware``).

Phase 3 also adds a second, independent channel running in the *opposite*
direction: ``CompanionChannelServer`` is owned by the companion app, not the
daemon, and speaks one command, ``OPEN <url>``, that the daemon's own
``oauth_loopback.py`` sends when it needs a browser opened for a connector
OAuth flow (ADR 0002 decision 5) -- the thing #428 Phase 4 makes mandatory on
Windows, where a service-hosted daemon runs in session 0 and cannot open a
browser in the user's desktop session itself. It reuses this module's own
``_LineProtocolServer`` (the POSIX-socket/Windows-named-pipe plumbing
``ControlChannelServer`` itself is built on) rather than the daemon's own
socket/pipe -- companion and daemon each own the address they *listen* on,
and each is a *client* of the other's.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import socket
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .. import paths, privilege_separation
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

# Phase 3: the companion's own listening address lives under a different
# name (same directory), so the two channels' sockets/pipes can never
# collide -- see CompanionChannelServer's own docstring.
COMPANION_SOCKET_FILE_NAME = "companion.sock"

# Phase 3: WebServer.start() writes this file (mirroring MCP_URL_FILE_NAME,
# web/server.py) so the companion -- a separate process that never imports
# web/server.py itself, see companion.py's own module docstring for why --
# can learn this install's local-mode base URL (``http://127.0.0.1:<port>``)
# without hardcoding the default port. Lives here, not in web/server.py,
# specifically so companion.py can read it via this module alone.
WEB_BASE_URL_FILE_NAME = "web_base_url"


def read_base_url() -> str | None:
    """The companion's own way to learn this install's local-mode base URL
    -- None if the daemon isn't currently running (the file is cleared on
    WebServer.stop()) or is running in org mode, which has no local
    base_url() concept for a companion to reach at all (org mode is out of
    #428's scope -- ADR 0002's own "Out of scope")."""
    path = paths.handoff_dir() / WEB_BASE_URL_FILE_NAME
    if not path.exists():
        return None
    return path.read_text(encoding=_ENCODING).strip() or None


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
    discovery file of its own.

    ``paths.control_socket_dir()`` rather than ``paths.authority_dir()``
    directly since #428 Phase 4: the two are the same directory on an
    ordinary install, but a privilege-separated one makes ``authority_dir()``
    ``0700`` under the daemon's own service account, and the companion --
    which runs as the logged-in human and is this channel's whole reason for
    existing -- has to still be able to connect. See that function, and
    ``paths.handoff_dir()``, for why relocating the socket gives up nothing
    Phase 4 claims."""
    return socket_path_under(paths.control_socket_dir())


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


def companion_socket_path_under(data_dir: Path) -> Path:
    """The companion channel's own equivalent of ``socket_path_under()`` --
    same fallback-when-too-long-for-AF_UNIX logic, a different file name so
    it can never collide with the daemon's own ``control.sock``. Rooted
    directly at ``data_dir``, not an ``authority`` subdirectory: unlike
    ``control.sock`` (which authorizes minting a *human* session, so it
    belongs among the human-authority files ``paths.authority_dir()``
    collects for #428 Phase 4), this is just the address a *daemon* reaches
    to ask a *companion* to open a browser tab -- nothing #428 Phase 4 needs
    to re-own. Rooted at ``paths.handoff_dir()`` in practice (see
    ``companion_socket_path()``), which *is* ``data_dir()`` on an ordinary
    install and the user-reachable subdirectory of it on a separated one --
    where the companion, running as the logged-in human, could not create a
    socket under the service-account-owned root at all."""
    preferred = data_dir / COMPANION_SOCKET_FILE_NAME
    if len(str(preferred).encode(_ENCODING)) < _MAX_SUN_PATH_BYTES:
        return preferred
    digest = hashlib.sha256(str(preferred.parent).encode(_ENCODING)).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"privacyfence-companion-{digest}.sock"


def companion_socket_path() -> Path:
    return companion_socket_path_under(paths.handoff_dir())


def companion_pipe_name_for(data_dir: Path) -> str:
    digest = hashlib.sha256(str(data_dir).encode(_ENCODING)).hexdigest()[:16]
    return f"\\\\.\\pipe\\PrivacyFence-Companion-{digest}"


def companion_pipe_name() -> str:
    return companion_pipe_name_for(paths.data_dir())


def _handle_daemon_request(bootstrap: BootstrapStore, *, allow_quit: bool, line: str) -> str:
    parts = line.strip().split(maxsplit=1)
    command = parts[0].upper() if parts else ""
    if command == "MINT":
        return f"OK {bootstrap.mint()}\n"
    if command == "QUIT":
        if privilege_separation.is_enabled():
            # #428 B4: this socket is 0660 group-shared with the companion
            # on a separated install, which puts the agent in the same
            # group -- so unlike ``allow_quit`` below, this is not a setting
            # an install can leave on. A system service is not this
            # channel's to stop, whatever ``allow_quit`` says.
            layout = privilege_separation.platform_layout()
            how = f" Use '{layout.stop_command}' instead." if layout is not None else ""
            return f"ERROR quit is not available on a privilege-separated install.{how}\n"
        if not allow_quit:
            return "ERROR quit is disabled\n"
        # Deferred import: daemon_main.py is the process entry point, which
        # constructs the WebServer (and therefore this channel) itself --
        # importing it back at module scope here would be circular, and
        # would also make every test that constructs a ControlChannelServer
        # directly (test_control_channel.py) drag in the whole daemon
        # startup module for no reason. Mirrors settings_controller.py's own
        # quit_app(), the web settings page's equivalent of this command.
        from .. import daemon_main

        daemon_main.request_shutdown()
        return "OK\n"
    return "ERROR unknown command\n"


def _peer_uid_posix(conn: socket.socket) -> int | None:
    """The uid of the process on the other end of a connected ``AF_UNIX``
    socket -- Linux and macOS each expose this through a different
    ``getsockopt``, so this picks whichever one this platform actually has
    rather than assuming Linux's. None if neither is available (any other
    POSIX platform, or the lookup itself failed), which ``_verify_companion_
    peer()`` treats as "cannot vouch for this peer" rather than as a real
    uid."""
    if hasattr(socket, "SO_PEERCRED"):
        # Linux: struct ucred { pid_t pid; uid_t uid; gid_t gid; } -- three
        # native ints, in that order.
        import struct

        try:
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return None
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    if privilege_separation.current_platform() == "darwin":
        return _peer_uid_macos(conn)
    return None


def _peer_uid_macos(conn: socket.socket) -> int | None:
    """macOS's equivalent of Linux's ``SO_PEERCRED``:
    ``getsockopt(SOL_LOCAL, LOCAL_PEERCRED)`` fills a ``struct xucred``
    (``<sys/un.h>``), not Linux's ``struct ucred`` -- there is no Python-
    level API for either on this platform, so this goes through ``libc`` via
    ``ctypes``, the same way ``windows_acl.py`` goes through ``pywin32`` for
    the Windows primitive this module has no stdlib equivalent for either."""
    import ctypes

    sol_local = 0
    local_peercred = 0x001

    class _Xucred(ctypes.Structure):
        _fields_ = [
            ("cr_version", ctypes.c_uint),
            ("cr_uid", ctypes.c_uint),
            ("cr_ngroups", ctypes.c_short),
            ("cr_groups", ctypes.c_uint * 16),
        ]

    libc = ctypes.CDLL(None, use_errno=True)  # dlopen(NULL): this process's own libc
    xucred = _Xucred()
    size = ctypes.c_uint(ctypes.sizeof(xucred))
    rc = libc.getsockopt(conn.fileno(), sol_local, local_peercred, ctypes.byref(xucred), ctypes.byref(size))
    if rc != 0:
        return None
    return xucred.cr_uid


def _verify_companion_peer(conn: socket.socket) -> str | None:
    """The companion channel's own gate (#428 B10) -- called before the
    handler on every connection, POSIX only (Windows named pipes are ACL'd
    instead, see ``_current_user_security_attributes()``). Before separation
    this channel is in the same boat ADR 0002 decision 6 describes for the
    daemon's own MINT/QUIT channel: companion, agent and daemon are all one
    uid, so no peer check could tell them apart, and none is attempted here
    either. After separation the daemon moves to its own service account
    while the companion -- and the agent, sharing the logged-in user's uid
    and this socket's group -- stay put: the one case in this codebase where
    a peer's real uid actually distinguishes the caller this channel exists
    for (the daemon, relaying its own ``oauth_loopback.py`` request) from
    the one it does not (the agent, reachable through the same ``0660``
    group). Returns an ``ERROR`` line to send back and refuse the
    connection without invoking the handler, or None to let it proceed."""
    if not privilege_separation.is_enabled():
        return None
    expected_uid = privilege_separation.service_account_uid()
    peer_uid = _peer_uid_posix(conn)
    if expected_uid is None or peer_uid != expected_uid:
        logger.warning(
            "Companion channel: refused a connection from uid %r (expected the daemon's "
            "service account, uid %r).", peer_uid, expected_uid,
        )
        return "ERROR peer not authorized\n"
    return None


def _handle_companion_request(line: str) -> str:
    """The companion channel's own dispatch -- one command, ``OPEN <url>``,
    sent by the daemon (``request_open_url()`` below) and acted on here, in
    the companion process, which is the one thing in this architecture that
    still runs in the user's desktop session once #428 Phase 4 moves the
    daemon to a service account. Scheme-restricted to http(s): pre-Phase-4,
    or on a Phase-4 install this dispatch is even reached from at all (see
    ``_verify_companion_peer()`` -- #428 B10 -- for who that is once
    separated), the caller is at minimum "anything running as the same OS
    user" (companion and daemon are still the same uid pre-Phase-4, agent
    included -- ADR 0002 decision 1), so it's worth not handing that caller
    a way to open an arbitrary ``file://``/custom-scheme URL for the one
    capability this process trades away nothing else to gain."""
    parts = line.strip().split(maxsplit=1)
    command = parts[0].upper() if parts else ""
    if command != "OPEN" or len(parts) != 2:
        return "ERROR unknown command\n"
    url = parts[1].strip()
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        return "ERROR unsupported scheme\n"
    try:
        opened = webbrowser.open(url)
    except Exception:
        logger.exception("Companion could not open a browser for %s", url)
        return "ERROR could not open a browser\n"
    return "OK\n" if opened else "ERROR could not open a browser\n"


class _LineProtocolServer:
    """Shared POSIX-socket/Windows-named-pipe accept-loop plumbing for a
    request/response, one-line-per-message local IPC server -- what
    ``ControlChannelServer`` and ``CompanionChannelServer`` are both built
    on. The two concrete classes exist (rather than callers constructing
    this directly) because their names describe security-relevant roles --
    which process listens for the daemon's own MINT/QUIT channel versus the
    companion's own OPEN channel -- not because the low-level mechanics
    differ between them; those stay in exactly one place so a fix like the
    Windows ``FlushFileBuffers`` one below only has to be made once.
    """

    def __init__(
        self,
        *,
        handler: Callable[[str], str],
        socket_path: Callable[[], Path],
        pipe_name: Callable[[], str],
        thread_name: str,
        verify_peer: Callable[[socket.socket], str | None] | None = None,
    ) -> None:
        self._handler = handler
        self._socket_path_fn = socket_path
        self._pipe_name_fn = pipe_name
        self._thread_name = thread_name
        # POSIX only (see _serve_one_posix) -- CompanionChannelServer's own
        # #428 B10 gate; None everywhere else (ADR 0002 decision 6).
        self._verify_peer = verify_peer
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # What a client needs to connect: the socket path (POSIX) or pipe
        # name (Windows) this instance actually bound/is listening on --
        # None until start() has run.
        self.address: str | None = None
        self._posix_socket: socket.socket | None = None
        self._windows_security_attributes = None

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
        sock_path = self._socket_path_fn()
        # Any file already at this path is stale: the caller (WebServer or
        # companion.py, constructed only after their own single-instance
        # start-up has succeeded) is the only process that will ever bind
        # here, so nothing legitimate could still be listening on it.
        with contextlib.suppress(OSError):
            sock_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        # 0600 while daemon, companion and agent are all one uid -- nothing
        # else on the machine could connect anyway. 0660 on a #428 Phase 4
        # install, where the two ends are two accounts and the shared
        # ``_privacyfence`` group is what still lets them reach each other;
        # connect(2) on a unix socket needs *write* permission on the node,
        # so the group bits have to be rw, not r. See
        # privilege_separation.socket_mode() for why this channel is
        # deliberately not narrower than that.
        sock_path.chmod(privilege_separation.socket_mode())
        sock.listen(8)
        # Short timeout, not a blocking accept() -- lets the accept loop
        # notice _stop_event between connections without needing a
        # self-pipe/wakeup socket just to interrupt a blocking call.
        sock.settimeout(0.5)
        self._posix_socket = sock
        self.address = str(sock_path)
        self._thread = threading.Thread(target=self._accept_loop_posix, name=self._thread_name, daemon=True)
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
        if self._verify_peer is not None:
            refusal = self._verify_peer(conn)
            if refusal is not None:
                with contextlib.suppress(OSError):
                    conn.sendall(refusal.encode(_ENCODING))
                    # Then drain, before the caller's ``with conn:`` closes.
                    # A refused peer is typically still mid-send -- it
                    # connected and is writing its request -- and closing a
                    # socket whose receive queue still holds unread data
                    # resets the connection, so that peer's own send() fails
                    # with EPIPE before it ever gets to read the refusal
                    # just queued above. Reading it first is what makes the
                    # diagnostic actually arrive; ``request_open_url()``'s
                    # caller would otherwise see a broken pipe instead of
                    # the "ERROR ..." line explaining why it was refused.
                    # No new worst case: the accepted path's own recv below
                    # already spends this same 5s budget on a silent client.
                    conn.recv(_MAX_MESSAGE_BYTES)
                return
        try:
            data = conn.recv(_MAX_MESSAGE_BYTES)
        except OSError:
            return
        line = data.decode(_ENCODING, errors="replace")
        try:
            response = self._handler(line)
        except Exception:
            logger.exception("Control channel request failed")
            response = "ERROR internal error\n"
        with contextlib.suppress(OSError):
            conn.sendall(response.encode(_ENCODING))

    # -- Windows: a named pipe, ACL'd to the current user ------------------- #

    def _start_windows(self) -> None:
        self.address = self._pipe_name_fn()
        self._windows_security_attributes = _current_user_security_attributes()
        # Created synchronously, on this (the caller's) thread, before the
        # accept-loop thread even starts -- the same "start() doesn't
        # return until the channel is actually reachable" guarantee
        # _start_posix()'s own synchronous bind()+listen() gives for free.
        # Without this, a client (a real one, or this class's own tests)
        # calling CreateFile/WaitNamedPipe on ``self.address`` right after
        # start() returns could race the background thread's first
        # CreateNamedPipe call and find no instance there yet.
        first_handle = self._create_windows_pipe_instance()
        self._thread = threading.Thread(
            target=self._accept_loop_windows, args=(first_handle,), name=self._thread_name, daemon=True,
        )
        self._thread.start()

    def _create_windows_pipe_instance(self):  # noqa: ANN201 -- a pywin32 PyHANDLE, no type stub
        import win32pipe

        return win32pipe.CreateNamedPipe(
            self.address,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            _MAX_MESSAGE_BYTES, _MAX_MESSAGE_BYTES,
            0,
            self._windows_security_attributes,
        )

    def _accept_loop_windows(self, first_handle) -> None:  # noqa: ANN001 -- a pywin32 PyHANDLE, no type stub
        import pywintypes
        import win32file
        import win32pipe
        import winerror

        handle = first_handle
        while not self._stop_event.is_set():
            if handle is None:
                try:
                    handle = self._create_windows_pipe_instance()
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
                    handle = None
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
                # FlushFileBuffers blocks until the client has actually read
                # everything WriteFile handed it -- without this,
                # DisconnectNamedPipe can (and, under real load, reliably
                # does) tear the pipe down before the client's own ReadFile
                # completes, which the client then sees as
                # ERROR_PIPE_NOT_CONNECTED ("no process is on the other end
                # of the pipe") rather than as its actual reply. Documented
                # Win32 named-pipe server behavior, not a defensive guess.
                with contextlib.suppress(pywintypes.error):
                    win32file.FlushFileBuffers(handle)
                with contextlib.suppress(pywintypes.error):
                    win32pipe.DisconnectNamedPipe(handle)
                win32file.CloseHandle(handle)
                handle = None

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
            response = self._handler(line)
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


class ControlChannelServer:
    """Runs the daemon's own control channel (``MINT``/``QUIT``) on its own
    background thread -- started and stopped alongside the rest of
    ``WebServer``'s lifecycle (see that class's own ``start()``/``stop()``),
    sharing the same ``BootstrapStore`` the loopback HTTP app's
    ``_BootstrapMiddleware`` already consumes codes from, so a code minted
    here is redeemed exactly like one minted at startup by
    ``mint_bootstrap_url()``. ``allow_quit`` mirrors the web settings page's
    own flag (``settings.yaml``'s ``allow_quit``, default true): an
    administrator who's disabled quitting from the browser has disabled it
    here too, not just in one of the two places it's reachable from. On a
    privilege-separated install ``QUIT`` refuses regardless of
    ``allow_quit`` -- see ``_handle_daemon_request()``.
    """

    def __init__(self, *, bootstrap: BootstrapStore, allow_quit: bool = True) -> None:
        self._bootstrap = bootstrap
        self._allow_quit = allow_quit
        self._impl = _LineProtocolServer(
            handler=self._handle,
            socket_path=posix_socket_path,
            pipe_name=windows_pipe_name,
            thread_name="control-channel",
        )

    def _handle(self, line: str) -> str:
        return _handle_daemon_request(self._bootstrap, allow_quit=self._allow_quit, line=line)

    @property
    def address(self) -> str | None:
        return self._impl.address

    def start(self) -> None:
        self._impl.start()

    def stop(self) -> None:
        self._impl.stop()


class CompanionChannelServer:
    """Runs the companion app's own control channel (``OPEN <url>``) --
    started/stopped alongside the companion's tray/menu-bar loop
    (``companion.py``), on an address only the companion itself listens on
    (``companion_socket_path()``/``companion_pipe_name()``, never
    ``ControlChannelServer``'s own). The daemon is this channel's client,
    via ``request_open_url()`` below -- see this module's own docstring for
    why the two channels run in opposite directions.

    On a #428 Phase 4 separated install (POSIX only -- see
    ``_verify_companion_peer()``), a connection is refused unless it comes
    from the daemon's own service-account uid: this socket is ``0660``
    group-shared with the agent, same as ``ControlChannelServer``'s, but
    unlike that one, separation *does* put a different uid on the other end
    of the connection this channel exists to accept (#428 B10).
    """

    def __init__(self) -> None:
        self._impl = _LineProtocolServer(
            handler=_handle_companion_request,
            socket_path=companion_socket_path,
            pipe_name=companion_pipe_name,
            thread_name="companion-channel",
            verify_peer=_verify_companion_peer,
        )

    @property
    def address(self) -> str | None:
        return self._impl.address

    def start(self) -> None:
        self._impl.start()

    def stop(self) -> None:
        self._impl.stop()


def _current_user_security_attributes():  # noqa: ANN201 -- a pywin32 SECURITY_ATTRIBUTES, no type stub
    """A ``SECURITY_ATTRIBUTES`` whose DACL grants full access to the
    current process token's own user SID -- plus, on a #428 Phase 4
    separated install, to the two accounts that now sit on the other end of
    these channels. The "real security descriptor" half of #428 Phase 2's
    named-pipe requirement. Windows has no filesystem permission bits
    (``paths.py``'s own ``secure_mkdir`` docstring), so a named pipe's ACL
    is the actual access-control primitive here, not a chmod equivalent
    applied afterwards.

    Shared by both ``ControlChannelServer`` and ``CompanionChannelServer``'s
    pipes, and deliberately the same descriptor for both. Before Phase 4
    that was one SID, because daemon, companion and agent were one account.
    After it they are two -- the daemon is ``NT SERVICE\\PrivacyFence``, the
    companion is the logged-in human -- and each end has to be reachable by
    the other, which is exactly what ``privilege_separation.socket_mode()``'s
    ``0660`` expresses on POSIX. This is that, in the primitive Windows has.

    Not narrower than that, for the reason both channels' own docstrings
    already give: ADR 0002 decision 6 draws the boundary at ``authority/``,
    not here, because the companion and the agent share a session and no ACL
    can tell them apart. An account named by
    ``windows_channel_trustees()`` that does not exist on this machine
    (a marker left behind by a half-removed install) is skipped rather than
    raised on -- the pipe still comes up, ACL'd to this process alone, and
    ``audit_layout()`` is what reports the layout as broken.
    """
    import ntsecuritycon
    import win32api
    import win32security

    from .. import windows_acl

    process_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    user_sid, _attributes = win32security.GetTokenInformation(process_token, win32security.TokenUser)

    security_descriptor = win32security.SECURITY_DESCRIPTOR()
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user_sid)
    for trustee in privilege_separation.windows_channel_trustees():
        sid = windows_acl.lookup_account_sid(trustee)
        if sid is None:
            logger.warning(
                "Control channel: no account named %r on this machine -- its end of the "
                "channel will not be able to connect.", trustee,
            )
            continue
        if sid == user_sid:
            continue
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)

    security_attributes = win32security.SECURITY_ATTRIBUTES()
    security_attributes.SECURITY_DESCRIPTOR = security_descriptor
    return security_attributes


# --------------------------------------------------------------------------- #
# Clients -- Phase 3: the companion app is the daemon's control channel's own
# client (mint_bootstrap_code/request_quit), and the daemon is the
# companion channel's client (request_open_url). Both directions share the
# same low-level send-one-line-get-one-line-back mechanics
# (send_line_posix/send_line_windows) -- the daemon's own Python, per ADR
# 0002 decision 4, rather than a reimplementation.
# --------------------------------------------------------------------------- #

class ControlChannelError(Exception):
    """Raised by ``mint_bootstrap_code()``/``request_quit()`` when the
    daemon's control channel is unreachable (no companion-visible daemon
    running) or replies with something other than a well-formed ``OK``."""


def send_line_posix(socket_path: Path | str, message: str, *, timeout: float) -> str:
    """Connect to a Unix domain socket, send ``message``, and return
    whatever comes back -- the shared low-level half of every POSIX client
    in this module (and, before Phase 3, of ``tests/control_channel_client.
    py``'s own near-identical helper, kept there for its sandboxed-data-dir
    resolution, not because the socket I/O itself needs to differ)."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(message.encode(_ENCODING))
        return client.recv(_MAX_MESSAGE_BYTES).decode(_ENCODING)
    finally:
        client.close()


def send_line_windows(pipe_name: str, message: str, *, timeout: float) -> str:
    """The named-pipe equivalent of ``send_line_posix()`` -- pywin32 is a
    transitive runtime dependency already (via ``mcp.os.win32.utilities``),
    so it's always importable here.

    ``pywintypes.error`` isn't an ``OSError`` subclass, unlike everything
    ``socket.connect()`` raises on the POSIX side for the same "nothing is
    listening" case -- caught here and re-raised as ``OSError`` (connection
    phase) / ``ControlChannelError`` (post-connection phase), so every
    caller up the stack (``mint_bootstrap_code``/``request_quit``/
    ``request_open_url``, ``companion.py``, ``oauth_loopback.py``'s default
    opener) needs to know about exactly one exception shape for each case,
    regardless of platform, instead of also needing a pywintypes-specific
    except clause of its own."""
    import pywintypes
    import win32file
    import win32pipe

    try:
        win32pipe.WaitNamedPipe(pipe_name, int(timeout * 1000))
        handle = win32file.CreateFile(
            pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None,
        )
    except pywintypes.error as exc:
        raise OSError(f"could not reach the control channel at {pipe_name}: {exc}") from exc
    try:
        try:
            win32file.WriteFile(handle, message.encode(_ENCODING))
            _rc, data = win32file.ReadFile(handle, _MAX_MESSAGE_BYTES)
        except pywintypes.error as exc:
            raise ControlChannelError(f"control channel request failed: {exc}") from exc
    finally:
        win32file.CloseHandle(handle)
    return data.decode(_ENCODING)


def _send_to_daemon(message: str, *, timeout: float) -> str:
    if paths.is_windows():
        return send_line_windows(windows_pipe_name(), message, timeout=timeout)
    return send_line_posix(posix_socket_path(), message, timeout=timeout)


def mint_bootstrap_code(*, timeout: float = 5.0) -> str:
    """The companion's own way to get a fresh, single-use bootstrap code
    without restarting the daemon or going through a browser at all --
    what backs its "Open Approvals"/"Open Settings" actions (companion.py).
    Raises ``ControlChannelError`` on anything other than a well-formed
    ``OK <code>`` reply; raises ``OSError`` (uncaught) if no daemon is
    listening at all -- callers that treat "no daemon running" as a normal,
    expected case (companion.py's own) catch that themselves."""
    reply = _send_to_daemon("MINT\n", timeout=timeout)
    if not reply.startswith("OK "):
        raise ControlChannelError(f"control channel mint failed: {reply!r}")
    return reply[len("OK "):].strip()


def request_quit(*, timeout: float = 5.0) -> None:
    """Asks the daemon to shut down -- what backs the companion's "Quit"
    tray item and Linux's XDG launcher "Quit" action. Raises
    ``ControlChannelError`` if the daemon declines (``allow_quit`` is
    disabled, or the install is privilege-separated -- see
    ``_handle_daemon_request()``) or replies unexpectedly; raises
    ``OSError`` if no daemon is listening at all, same as
    ``mint_bootstrap_code()``."""
    reply = _send_to_daemon("QUIT\n", timeout=timeout)
    if not reply.startswith("OK"):
        raise ControlChannelError(f"control channel quit failed: {reply!r}")


def request_open_url(url: str, *, timeout: float = 2.0) -> bool:
    """Best-effort: asks a running companion to open ``url`` in the user's
    browser (ADR 0002 decision 5) -- returns False (never raises) for
    anything that means "no companion is running right now", which is the
    normal case until a human starts one (Phase 3 doesn't autostart it --
    that's Phase 4's job): no socket/pipe present, connection refused, or a
    timeout. ``oauth_loopback.py``'s default browser opener falls back to
    calling ``webbrowser.open()`` directly when this returns False, so a
    missing companion never blocks a connector's OAuth flow."""
    try:
        if paths.is_windows():
            reply = send_line_windows(companion_pipe_name(), f"OPEN {url}\n", timeout=timeout)
        else:
            reply = send_line_posix(companion_socket_path(), f"OPEN {url}\n", timeout=timeout)
    except (OSError, ControlChannelError):
        return False
    return reply.startswith("OK")


__all__ = [
    "COMPANION_SOCKET_FILE_NAME",
    "SOCKET_FILE_NAME",
    "WEB_BASE_URL_FILE_NAME",
    "CompanionChannelServer",
    "ControlChannelError",
    "ControlChannelServer",
    "companion_pipe_name",
    "companion_pipe_name_for",
    "companion_socket_path",
    "companion_socket_path_under",
    "mint_bootstrap_code",
    "pipe_name_for",
    "posix_socket_path",
    "read_base_url",
    "request_open_url",
    "request_quit",
    "send_line_posix",
    "send_line_windows",
    "socket_path_under",
    "windows_pipe_name",
]
