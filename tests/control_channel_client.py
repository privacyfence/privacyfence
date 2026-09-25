"""Shared real client for the daemon's control channel (``privacyfence.web.
control_channel``), for any integration/system test that mints a bootstrap
code against a *real*, separately-spawned daemon process.

Minting on demand goes through a Unix domain socket (macOS/Linux) or an
ACL'd named pipe (Windows), neither of which a plain HTTP client can reach,
so every such test needs a real socket/pipe client. This module is that
client, so it exists once rather than once per test file.

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


# --------------------------------------------------------------------------- #
# Attested minting -- a code that may actually *approve*
#
# Everything above mints with a bare ``MINT``, which lands an
# ``unattested`` session: it may view what is
# pending, but web/routes_approvals.py's ``require_human_session`` gate
# refuses it a step-up result or a sensitive confirm, and
# web/routes_settings.py refuses it every ``_SENSITIVE_ACTIONS`` change (see
# web/session_auth.py's own ``PROVENANCE_*`` comment and ADR 0062). That gate is on
# whenever privilege separation is -- ``web/server.py``'s
# ``require_human_session = privilege_separation.is_enabled()`` -- so it is
# on for every packaged install, which is exactly what the packaged-artifact
# smoke tests drive.
#
# The attested shape a human actually produces is ``MINT COMPANION
# <nonce>``: the companion issues itself a nonce (control_channel.py's
# ``issue_mint_nonce()``), sends it to the daemon, and the daemon hands it
# straight back over the *companion's* own channel (``CONFIRM MINT
# <nonce>``) before minting anything. Only the process that issued the nonce
# can recognize it, and that process is the one a human clicked.
#
# A headless CI runner has no companion -- no menu bar, no tray, no
# applications menu -- so a test that needs a session which can approve has
# to stand in for one, which means binding the companion's own address and
# answering that call-back. That is what the script below does, and it is a
# stand-in for the *process*, not a bypass of the gate: the daemon still
# refuses the mint unless the call-back reaches something holding the nonce
# it was handed.
#
# Generated as source text rather than exposed as a function because its one
# caller on each platform runs it under ``sudo python3 -c`` -- the daemon's
# control socket belongs to the service account, and sudo's own system
# ``python3`` has no ``privacyfence`` (nor ``tests``) package importable.
# Same technique, and the same reason, as the plain-``MINT`` inline scripts
# the packaged smoke modules already carry.
# --------------------------------------------------------------------------- #

_ATTESTED_MINT_BODY = '''
import os, secrets, socket, sys, threading

nonce = secrets.token_urlsafe(16)

# Refuse to take over a live companion rather than unlinking its address out
# from under it: if something answers here, this install has a real
# companion and this stand-in has no business replacing it (and could not
# help anyway -- the nonce it would have to recognize is not this one).
probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
probe.settimeout(TIMEOUT)
try:
    probe.connect(COMPANION)
    sys.stderr.write("a companion is already listening on %s\\n" % COMPANION)
    sys.exit(2)
except (ConnectionRefusedError, FileNotFoundError):
    pass  # nothing there, or a stale socket file left by a dead process
finally:
    probe.close()

try:
    os.unlink(COMPANION)
except FileNotFoundError:
    pass

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(COMPANION)
# The daemon dials this from its own service account, so the bound file has
# to be reachable by it. Root's umask and the handoff directory's setgid bit
# between them do not reliably produce that, and this address lives for one
# round trip and answers exactly one nonce, so widen it outright rather than
# guess at the group.
os.chmod(COMPANION, 0o666)
server.listen(1)
server.settimeout(TIMEOUT)


def answer_callback():
    """The companion half: control_channel.py's ``_handle_companion_
    request`` for ``CONFIRM MINT``, reduced to the one subject this needs."""
    try:
        conn, _ = server.accept()
    except OSError:
        return
    with conn:
        conn.settimeout(TIMEOUT)
        line = conn.recv(4096).decode("utf-8").strip()
        if line == "CONFIRM MINT " + nonce:
            conn.sendall(b"OK\\n")
        else:
            conn.sendall(b"ERROR no sign-in was requested from this companion\\n")


listener = threading.Thread(target=answer_callback, daemon=True)
listener.start()
try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(TIMEOUT)
    try:
        client.connect(CONTROL)
        client.sendall(("MINT COMPANION " + nonce + "\\n").encode("utf-8"))
        reply = client.recv(4096).decode("utf-8")
    finally:
        client.close()
finally:
    listener.join(TIMEOUT)
    server.close()
    # Leave the address as it was found: an unbound companion socket is what
    # a headless install has, and a leftover file here would make a later
    # request_mint_attestation() wait out its own timeout on nothing.
    try:
        os.unlink(COMPANION)
    except FileNotFoundError:
        pass

sys.stdout.write(reply)
'''


def attested_mint_script(
    control_socket: Path | str, companion_socket: Path | str, *, timeout: float = 5.0,
) -> str:
    """Stdlib-only source for ``python3 -c`` that mints an *attested*
    (``human``-provenance) bootstrap code against the real separated daemon
    at ``control_socket``, standing in for the companion at
    ``companion_socket`` -- ``control_channel.companion_socket_path_under
    (paths.handoff_dir())``, which the caller resolves rather than spelling
    out, since that helper falls back to a ``/tmp`` digest for an address too
    long for ``AF_UNIX``.

    Writes the daemon's own reply line (``OK <code>`` or ``ERROR
    <reason>``) to stdout, so the caller parses exactly what the plain-
    ``MINT`` scripts already parse; exits 2, with a reason on stderr, if a
    real companion already owns that address."""
    return (
        f"CONTROL = {str(control_socket)!r}\n"
        f"COMPANION = {str(companion_socket)!r}\n"
        f"TIMEOUT = {float(timeout)!r}\n"
        f"{_ATTESTED_MINT_BODY}"
    )


# --------------------------------------------------------------------------- #
# Standing in for the companion for a whole ceremony
#
# ``attested_mint_script()`` above owns its own address for one round trip,
# because a mint *is* one round trip. A first passkey enrollment is not: it
# is gated on the companion's ``CONFIRM ENROLL`` dialog and then hands the
# freshly minted recovery code back through the same channel (``SHOW
# RECOVERY``), so the stand-in has to stay bound across both, and across the
# two HTTP round trips the test makes in between.
#
# What it answers ``OK`` to is what a human clicking **Allow** answers: there
# is no nonce in either of these, and none in the real companion's handling
# of them either (control_channel.py's ``_confirm_enroll``/
# ``_show_recovery_code`` just put a dialog on screen). So unlike the mint,
# there is nothing here a stand-in could weaken by answering -- the gate's
# whole content is "a process that a human can be asked through was reachable
# and said yes", and on a headless runner that process is this one.
#
# Bounded twice over, because it runs as root: it serves at most
# ``max_requests`` and at most ``serve_seconds``, then unbinds and exits on
# its own even if the test that started it dies.
# --------------------------------------------------------------------------- #

_COMPANION_STAND_IN_BODY = '''
import os, socket, sys, time

probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
probe.settimeout(2.0)
try:
    probe.connect(COMPANION)
    sys.stderr.write("a companion is already listening on %s\\n" % COMPANION)
    sys.exit(2)
except (ConnectionRefusedError, FileNotFoundError):
    pass
finally:
    probe.close()

try:
    os.unlink(COMPANION)
except FileNotFoundError:
    pass

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(COMPANION)
os.chmod(COMPANION, 0o666)  # dialled from the service account -- see above
server.listen(4)

# Printed, and flushed, before serving anything: the parent waits for this
# line rather than sleeping, so the address is provably bound before it makes
# the HTTP call that will have the daemon dial it.
sys.stdout.write("READY\\n")
sys.stdout.flush()

seen = []
answered = 0
deadline = time.monotonic() + SERVE_SECONDS
try:
    while answered < MAX_REQUESTS and time.monotonic() < deadline:
        server.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            conn, _ = server.accept()
        except OSError:
            break
        with conn:
            conn.settimeout(5.0)
            line = conn.recv(4096).decode("utf-8").strip()
            seen.append(" ".join(line.split(" ")[0:2]))
            # Every verb this stands in for is a dialog the human answers
            # yes to; anything else is a request this stand-in was not put
            # up for, and saying so is better than a plausible "OK".
            verb = line.upper()
            if (verb.startswith("CONFIRM ENROLL") or verb.startswith("CONFIRM SIGNIN")
                    or verb.startswith("SHOW RECOVERY ")):
                # Only an answered dialog counts against the budget, so an
                # unrelated call the daemon happens to make in this window
                # cannot use up a slot the ceremony still needs.
                answered += 1
                conn.sendall(b"OK\\n")
            else:
                conn.sendall(b"ERROR this stand-in companion does not answer that\\n")
finally:
    server.close()
    try:
        os.unlink(COMPANION)
    except FileNotFoundError:
        pass

sys.stderr.write("served: %r\\n" % (seen,))
'''


def companion_stand_in_script(
    companion_socket: Path | str, *, max_requests: int = 2, serve_seconds: float = 60.0,
) -> str:
    """Stdlib-only source for ``python3 -c`` that binds the companion's own
    address and answers ``CONFIRM ENROLL``/``CONFIRM SIGNIN``/``SHOW
    RECOVERY`` with the ``OK`` a human clicking **Allow** produces, then
    unbinds.

    Run it as a background child and wait for the single line ``READY`` on
    its stdout before triggering anything that will dial the companion; it
    exits on its own once it has served ``max_requests`` or
    ``serve_seconds`` have passed, whichever comes first, so a test that
    fails mid-ceremony leaves no privileged process and no stale socket
    behind. What it served is written to stderr, for a failure report."""
    return (
        f"COMPANION = {str(companion_socket)!r}\n"
        f"MAX_REQUESTS = {int(max_requests)!r}\n"
        f"SERVE_SECONDS = {float(serve_seconds)!r}\n"
        f"{_COMPANION_STAND_IN_BODY}"
    )
