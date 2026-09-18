"""Shared real client for #428 Phase 2's control channel (``privacyfence.web.
control_channel``), for any integration/system test that mints a bootstrap
code against a *real*, separately-spawned daemon process.

Before Phase 2, that meant a plain ``POST /api/bootstrap`` over the same
loopback HTTP port the browser uses, authenticated by the persistent
``web_token`` file (see ``test_local_mode_system.py``'s git history for what
that helper used to look like). Phase 2 retired both the endpoint and the
file -- minting on demand now goes through a Unix domain socket (macOS/
Linux) or an ACL'd named pipe (Windows) instead, neither of which a plain
HTTP client can reach, so every test that used to POST a Bearer header now
needs a real socket/pipe client instead. This module is that client, so it
exists once rather than once per test file.

A test driving a real subprocess (spawned against its own sandboxed
``data_dir()``, e.g. ``tests/system/test_local_mode_system.py``'s own
``_daemon``) cannot just call ``privacyfence.web.control_channel``'s own
``posix_socket_path()``/``windows_pipe_name()`` -- those resolve against
*this* process's ``paths.data_dir()``, not the sandboxed one the subprocess
was given. ``resolve_posix_socket_path()``/``resolve_windows_pipe_name()``
below wrap that module's own pure helpers (``socket_path_under()``/
``pipe_name_for()``) instead, which take the target data directory directly
and have no side effects of their own -- the same computation the real
subprocess did, from the same known sandbox path, without touching this
process's filesystem or the sandbox's.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

_ENCODING = "utf-8"
_MINT_REQUEST = b"MINT\n"


def resolve_posix_socket_path(data_dir: Path) -> Path:
    from privacyfence.web.control_channel import socket_path_under

    return socket_path_under(Path(data_dir) / "authority")


def resolve_windows_pipe_name(data_dir: Path) -> str:
    from privacyfence.web.control_channel import pipe_name_for

    return pipe_name_for(Path(data_dir))


def mint_bootstrap_code_posix(socket_path: Path | str, *, timeout: float = 5.0) -> str:
    """Connects to a real Unix domain socket and returns the minted code --
    raises ``AssertionError`` (not a bare protocol error) on anything other
    than a well-formed ``OK <code>`` reply, so a caller can assert on this
    call directly without a separate response-shape check."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(_MINT_REQUEST)
        reply = client.recv(4096).decode(_ENCODING)
    finally:
        client.close()
    if not reply.startswith("OK "):
        raise AssertionError(f"control channel mint failed: {reply!r}")
    return reply[len("OK "):].strip()


def windows_pipe_exists(pipe_name: str) -> bool:
    """True iff a server is currently listening on ``pipe_name``. Named
    pipes aren't filesystem objects, so ``Path.exists()`` (the POSIX
    socket's own liveness check) doesn't apply -- opening the pipe and
    immediately closing it again is the closest equivalent, used only by
    tests asserting a daemon has *not* started yet (e.g. that a silent
    install itself never launches it, only the autostart task does)."""
    import pywintypes
    import win32file

    try:
        handle = win32file.CreateFile(
            pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None,
        )
    except pywintypes.error:
        return False
    win32file.CloseHandle(handle)
    return True


def mint_bootstrap_code_windows(pipe_name: str, *, timeout_ms: int = 5000) -> str:
    """Connects to a real named pipe and returns the minted code -- pywin32
    is a transitive runtime dependency already (via ``mcp.os.win32.
    utilities``), so it's always importable on the Windows CI runners this
    actually executes on; see ``privacyfence.web.control_channel``'s own
    module docstring for why the daemon side needs it too."""
    import pywintypes
    import win32file
    import win32pipe

    win32pipe.WaitNamedPipe(pipe_name, timeout_ms)
    handle = win32file.CreateFile(
        pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0, None, win32file.OPEN_EXISTING, 0, None,
    )
    try:
        win32file.WriteFile(handle, _MINT_REQUEST)
        try:
            _rc, data = win32file.ReadFile(handle, 4096)
        except pywintypes.error as exc:
            raise AssertionError(f"control channel mint failed to read a reply: {exc}") from exc
    finally:
        win32file.CloseHandle(handle)
    reply = data.decode(_ENCODING)
    if not reply.startswith("OK "):
        raise AssertionError(f"control channel mint failed: {reply!r}")
    return reply[len("OK "):].strip()


def mint_bootstrap_code(data_dir: Path, *, timeout: float = 5.0) -> str:
    """Platform-dispatching convenience: resolves the right address for
    ``data_dir`` and mints a code against it, in one call. Every real
    daemon/system test in this repo runs cross-platform (``tests/system/
    test_local_mode_system.py``'s own docstring), so this is what most
    callers actually want over calling the POSIX/Windows halves directly."""
    if sys.platform == "win32":
        return mint_bootstrap_code_windows(resolve_windows_pipe_name(data_dir), timeout_ms=int(timeout * 1000))
    return mint_bootstrap_code_posix(resolve_posix_socket_path(data_dir), timeout=timeout)
