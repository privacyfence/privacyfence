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
daemon, and speaks ``OPEN <url>``, which the daemon's own
``oauth_loopback.py`` sends when it needs a browser opened for a connector
OAuth flow (ADR 0002 decision 5) -- the thing #428 Phase 4 makes mandatory on
Windows, where a service-hosted daemon runs in session 0 and cannot open a
browser in the user's desktop session itself. It reuses this module's own
``_LineProtocolServer`` (the POSIX-socket/Windows-named-pipe plumbing
``ControlChannelServer`` itself is built on) rather than the daemon's own
socket/pipe -- companion and daemon each own the address they *listen* on,
and each is a *client* of the other's.

The companion channel's second command, ``CONFIRM ENROLL``, is the one place
in this codebase where the direction of this channel is the *point* rather
than a platform workaround. Enrolling a passkey when one is already enrolled
can be gated on asserting with the one already there (web/routes_security.py's
``register_options``); the *first* enrollment has nothing to assert with, and
a local-mode ``pf_session`` is not proof of a human -- ADR 0002 decision 6
and this module's own ``MINT`` paragraph above both say so outright. What is
left is this process, because it is the only one that runs where a human can
be asked: ``_confirm_first_enrollment()`` answers by putting the system's own
dialog in front of whoever is at the login session.

**What that does not make it is authentication of the companion**, and this
module is the wrong place to pretend otherwise. ``_verify_companion_peer()``
constrains who may *reach* this server -- on a separated install, the
daemon's service account and nobody else -- but the address it listens on
lives under ``handoff_dir()``, which is group-shared with the logged-in user
by design (``paths.py``: "deliberately *not* a security boundary"), so a
local process running as that user can bind it first and answer for itself.
Companion and agent share a uid; ADR 0002 decision 6's "no peer check could
tell them apart" is as true here as anywhere. What the command buys is that
forging a first enrollment takes impersonating this process -- loud, and
destructive to the OAuth flows that share the socket -- instead of being a
side effect of holding a session. docs/security-and-compliance.md states that
limit in the same terms; keep the two in agreement. See
``request_enrollment_confirmation()`` for the daemon's own side.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import socket
import subprocess  # nosec B404  # the companion's own zenity/kdialog/osascript dialog below -- fixed argv, no shell
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .. import paths, privilege_separation
from . import session_auth
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


# The decision every mint is recorded under (audit_log.py's own vocabulary
# comment says why). Named here rather than inlined at the three call sites
# below so the log and the page that reads it back (web/routes_security.py's
# "Recent sign-ins") cannot drift apart over a typo.
SIGN_IN_MINT_DECISION = "sign_in_code_minted"


def _audit_mint(summary: str) -> None:
    """One entry per bootstrap code this daemon issues, and per attested one
    it refuses. Never allowed to fail the mint it describes -- same posture
    every other non-critical audit call in this codebase takes (docs/
    coding-and-testing-guidelines.md §1.4's "non-critical side effects"
    rule), and more pointedly here than most: a human locked out because the
    audit log could not be written would be locked out by the thing meant to
    reassure them.

    Deferred import, like ``QUIT``'s own ``daemon_main`` below: this module
    is also imported by the companion process (companion.py), which has no
    business carrying the audit log's machinery, and by tests that construct
    a channel with no audit logger initialized at all.
    """
    try:
        from datetime import datetime, timezone

        from ..audit_log import AuditEntry, current_week, get_audit_logger
        from ..principal import LOCAL_PRINCIPAL

        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=secrets.token_hex(6),
            connector="",
            tool="",
            tool_name="",
            summary=summary,
            sender=LOCAL_PRINCIPAL.email or LOCAL_PRINCIPAL.display_name or LOCAL_PRINCIPAL.id,
            decision=SIGN_IN_MINT_DECISION,
            auto_accept_rule="",
            latency_seconds=0.0,
            pii_detected=False,
        ))
    except Exception as exc:  # noqa: BLE001 -- see this function's own docstring
        logger.warning("Audit log write failed for a sign-in code mint: %s", exc)


def _handle_daemon_request(
    bootstrap: BootstrapStore,
    *,
    allow_quit: bool,
    line: str,
    confirm_companion_mint: Callable[[str], bool] | None = None,
    confirm_console_mint: Callable[[], tuple[bool, str]] | None = None,
) -> str:
    """``confirm_companion_mint``/``confirm_console_mint`` are the two
    call-backs an attested ``MINT`` makes into the companion's own channel
    (``request_mint_attestation()``/``request_sign_in_confirmation()``,
    which is what they default to). Injectable because the real ones talk to
    another process: a test for this dispatch's own logic should not have to
    stand one up.
    """
    parts = line.strip().split(maxsplit=1)
    command = parts[0].upper() if parts else ""
    argument = parts[1].strip() if len(parts) == 2 else ""
    if command == "MINT":
        # The three shapes, and what each one is allowed to claim about who
        # asked -- see session_auth.py's own PROVENANCE_* comment for the
        # finding this answers.
        #
        # A bare MINT stays exactly what it was, including its reply format:
        # it is what the unauthorized page's copy-paste command sends, and
        # what anything on this machine can send, so it mints the session
        # that may view but not approve. The two attested shapes cost a
        # round trip into the companion process, which is the only
        # PrivacyFence process running where a human can be asked at all.
        if not argument:
            _audit_mint(
                "Issued a sign-in code (unattested -- can view what is pending, cannot release it)",
            )
            return f"OK {bootstrap.mint(provenance=session_auth.PROVENANCE_UNATTESTED)}\n"
        subcommand, _, subargument = argument.partition(" ")
        subargument = subargument.strip()
        if subcommand.upper() == "COMPANION":
            # The companion's own Open Approvals/Open Settings click. The
            # nonce is one the *companion* issued to itself moments ago
            # (issue_mint_nonce()); this daemon hands it straight back over
            # the companion's channel and mints an attested code only if the
            # process listening there recognizes it. An agent sending this
            # line has no nonce to send that the companion would recognize,
            # and cannot read the one it did issue.
            confirm = confirm_companion_mint or request_mint_attestation
            if not subargument or not confirm(subargument):
                _audit_mint("Refused a sign-in code that can approve: the companion did not confirm it")
                return "ERROR that mint was not confirmed by the companion\n"
            _audit_mint("Issued a sign-in code that can approve (confirmed by the companion app)")
            return f"OK {bootstrap.mint(provenance=session_auth.PROVENANCE_HUMAN)}\n"
        if subcommand.upper() == "CONSOLE":
            # `privacyfence-app --print-sign-in-link`, the break-glass path
            # (daemon_main.py). Unlike COMPANION there is no click to point
            # at, so the companion asks with its own dialog -- the same
            # ceremony CONFIRM ENROLL uses, and for the same reason: nothing
            # about the connection itself distinguishes the human's terminal
            # from the agent's subprocess, since they share a uid.
            #
            # This blocks this channel's accept loop for as long as the
            # dialog is up, the same way CONFIRM ENROLL blocks the
            # companion's (see CONFIRM_DIALOG_TIMEOUT_SECONDS). Somebody
            # running the break-glass command while also clicking the
            # companion's menu is not a real workflow -- but it is why that
            # timeout is 90 seconds and not five minutes.
            confirm_console = confirm_console_mint or request_sign_in_confirmation
            confirmed, reason = confirm_console()
            if not confirmed:
                _audit_mint(f"Refused a sign-in code requested from a terminal: {reason}")
                return f"ERROR {reason}\n"
            _audit_mint(
                "Issued a sign-in code that can approve, requested from a terminal "
                "(privacyfence-app --print-sign-in-link, confirmed at the companion's dialog)",
            )
            return f"OK {bootstrap.mint(provenance=session_auth.PROVENANCE_HUMAN)}\n"
        return "ERROR unknown command\n"
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


# ── The companion's own mint nonces ───────────────────────────────────────── #
#
# What makes a ``MINT COMPANION`` line attestable. The companion issues one
# of these to itself immediately before asking the daemon for a code, and the
# daemon hands it straight back here (``CONFIRM MINT <nonce>``) before minting
# anything. Neither half is a secret the daemon keeps: the whole point is that
# only the process that *issued* the nonce can recognize it, and that process
# is the one a human clicked.
#
# Short-lived and single-use for the same reason a bootstrap code is: the
# window between a click and the daemon's call-back is a socket round trip,
# so anything longer is a nonce sitting around waiting to be guessed at. A
# nonce that is never redeemed (the daemon is not running, the mint failed)
# simply expires -- nothing sweeps this dict, because its ceiling is the
# number of menu clicks a human makes in 30 seconds.
_MINT_NONCE_TTL_SECONDS = 30

_mint_nonce_lock = threading.Lock()
_mint_nonces: dict[str, float] = {}


def issue_mint_nonce() -> str:
    """Mint a nonce for this process's own upcoming ``MINT COMPANION`` --
    companion.py's ``_open_path()`` calls this, then sends the nonce to the
    daemon, which calls back with it."""
    nonce = secrets.token_urlsafe(16)
    with _mint_nonce_lock:
        _mint_nonces[nonce] = time.time() + _MINT_NONCE_TTL_SECONDS
    return nonce


def _consume_mint_nonce(nonce: str) -> bool:
    """True iff this process issued ``nonce`` and it has neither expired nor
    already been redeemed. Removed on every attempt, valid or not -- same
    single-use posture as ``session_auth.BootstrapStore.consume()``."""
    if not nonce:
        return False
    with _mint_nonce_lock:
        expires_at = _mint_nonces.pop(nonce, None)
    return expires_at is not None and expires_at >= time.time()


# ── The companion's own human-confirmation dialog ─────────────────────────── #
#
# What CONFIRM ENROLL puts in front of a human, and the three ways of putting
# it there. Zero new dependencies is the constraint ADR 0002 decision 4 sets
# for this process on every platform (and most tightly on Linux, where it
# rules out a tray icon): these are the system's own dialogs, reached the way
# privilege_separation.py already reaches osascript for its elevation prompt.
#
# The text is fixed here, never taken from the request line. The only caller
# on a separated install is the daemon (_verify_companion_peer), but a fixed
# string is also what keeps this from being a way to put arbitrary words in a
# PrivacyFence-branded dialog on a pre-separation install, where the caller is
# still "anything running as this OS user" -- the same reasoning OPEN's own
# scheme restriction above gives.
_CONFIRM_TITLE = "PrivacyFence"
_CONFIRM_ENROLL_PROMPT = (
    "Allow a new passkey to be enrolled for PrivacyFence?\n\n"
    "This is the first passkey on this install, so it will become what "
    "approving a gated write requires.\n\n"
    "If you did not just click \u201cAdd a passkey\u201d on the PrivacyFence "
    "security page yourself, choose Deny."
)
_CONFIRM_SIGN_IN_PROMPT = (
    "Allow a PrivacyFence sign-in link that can approve?\n\n"
    "Somebody on this machine ran \u201cprivacyfence-app --print-sign-in-link\u201d. The link it "
    "prints will be able to release writes PrivacyFence is holding.\n\n"
    "If you did not just run that command yourself, choose Deny."
)
_CONFIRM_ALLOW_LABEL = "Allow"
_CONFIRM_DENY_LABEL = "Deny"

# How long the dialog is left up. Deliberately shorter than the ~5 minutes a
# WebAuthn registration challenge lives (webauthn_stepup.py's own
# _REGISTRATION_CHALLENGE_TTL_SECONDS): a human who is at the keyboard answers
# in seconds, and this handler runs on _LineProtocolServer's single accept
# loop, so for as long as it is up this process is not answering OPEN either.
# Colliding with a connector OAuth flow means starting one *while* confirming
# a first passkey, which is not a real workflow -- but it is why this is not
# five minutes.
CONFIRM_DIALOG_TIMEOUT_SECONDS = 90

# Absolute paths, not shutil.which() lookups, and for a stronger reason than
# the bandit B607 one privilege_separation.py's own _OSASCRIPT cites: this
# process runs as the logged-in user, which is the same account the agent runs
# as, so PATH is something the adversary this dialog exists to stop can write.
# A which("zenity") would let it ship a "zenity" that answers Allow.
_OSASCRIPT = "/usr/bin/osascript"
_LINUX_DIALOG_COMMANDS = (
    # (argv-prefix, the argv that asks our own question). zenity ships with
    # GNOME, kdialog with KDE; both exit 0 for yes and nonzero for no/closed,
    # which is the whole contract needed here.
    ("/usr/bin/zenity", lambda prompt: [
        "/usr/bin/zenity", "--question", "--no-markup", f"--title={_CONFIRM_TITLE}",
        f"--text={prompt}", f"--ok-label={_CONFIRM_ALLOW_LABEL}", f"--cancel-label={_CONFIRM_DENY_LABEL}",
    ]),
    ("/usr/bin/kdialog", lambda prompt: [
        "/usr/bin/kdialog", f"--title={_CONFIRM_TITLE}", "--warningyesno", prompt,
        f"--yes-label={_CONFIRM_ALLOW_LABEL}", f"--no-label={_CONFIRM_DENY_LABEL}",
    ]),
)


def _applescript_quoted(text: str) -> str:
    """Escape ``text`` for a double-quoted AppleScript string literal. Kept
    here rather than imported from privilege_separation.py's own namesake so
    this module's dialog does not depend on that one's shell-command-building
    neighbours -- and because it has to handle one thing that one never sees:
    a **newline**. That function's input is a shell command, which has none;
    this one's is a multi-paragraph prompt, and a raw newline inside an
    AppleScript string literal is a syntax error rather than a line break, so
    it becomes AppleScript's own ``\\n`` escape (which the language does
    support). Backslashes are escaped first, or that escape would itself be
    escaped."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n") + '"'


def _confirm_macos(prompt: str, *, timeout: float) -> bool:
    script = (
        f"display dialog {_applescript_quoted(prompt)} "
        f"buttons {{{_applescript_quoted(_CONFIRM_DENY_LABEL)}, {_applescript_quoted(_CONFIRM_ALLOW_LABEL)}}} "
        f"default button {_applescript_quoted(_CONFIRM_DENY_LABEL)} "
        f"cancel button {_applescript_quoted(_CONFIRM_DENY_LABEL)} "
        f"with title {_applescript_quoted(_CONFIRM_TITLE)} with icon caution"
    )
    result = subprocess.run(  # nosec B603  # fixed argv, no shell; `prompt` is this module's own constant
        [_OSASCRIPT, "-e", script],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    # "Deny" is both the default and the cancel button, so a dismissed dialog
    # (osascript exits 1) and an explicit Deny land in the same place, which
    # is the safe one.
    return result.returncode == 0 and _CONFIRM_ALLOW_LABEL in result.stdout


def _confirm_windows(prompt: str, *, timeout: float) -> bool:  # noqa: ARG001 -- MessageBoxW has no timeout
    """``MessageBoxW`` blocks until the human answers and takes no timeout,
    so ``timeout`` is accepted (for one signature across platforms) and
    unused -- the daemon-side timeout in ``request_enrollment_confirmation``
    is what bounds the wait for the caller either way."""
    import ctypes

    mb_yesno = 0x4
    mb_iconwarning = 0x30
    mb_defbutton2 = 0x100  # "No" is the default, same posture as macOS above
    mb_setforeground = 0x10000
    id_yes = 6
    answer = ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]  # Windows-only
        0, prompt, _CONFIRM_TITLE, mb_yesno | mb_iconwarning | mb_defbutton2 | mb_setforeground,
    )
    return answer == id_yes


class _NoDialogAvailable(Exception):
    """Raised by ``_confirm_linux`` when this desktop has neither zenity nor
    kdialog -- see its own docstring for why that is not a refusal."""


def _confirm_linux(prompt: str, *, timeout: float) -> bool:
    """zenity or kdialog, whichever this desktop has. Neither is a
    PrivacyFence dependency (ADR 0002 decision 4's Linux budget) and neither
    is guaranteed present, so "no dialog program" is a distinct outcome from
    "the human said no" -- it raises, and the caller turns that into a reply
    naming the fix rather than a silent refusal the human cannot act on."""
    for executable, argv_for in _LINUX_DIALOG_COMMANDS:
        if not Path(executable).exists():
            continue
        result = subprocess.run(  # nosec B603  # fixed argv, no shell; `prompt` is this module's own constant
            argv_for(prompt), capture_output=True, text=True, timeout=timeout, check=False,
        )
        return result.returncode == 0
    raise _NoDialogAvailable(
        "no dialog program found -- install zenity or kdialog so PrivacyFence can ask "
        "before a first passkey is enrolled"
    )


def _ask_human(prompt: str, *, subject: str, denied_reason: str) -> str:
    """Put one of this module's own fixed prompts in front of whoever is at
    this login session, and answer with the line the daemon reads back.
    Returns an ``OK``/``ERROR`` line rather than a bool so the "could not
    ask" cases stay distinguishable from "asked, and the answer was no" --
    web/routes_security.py surfaces the reason verbatim on ``/security``,
    and daemon_main.py's ``--print-sign-in-link`` prints it in the terminal
    that asked; both are where somebody who has just been refused is
    already standing.

    Any refusal, for any reason, is the safe answer for both callers: each
    gate exists because the thing it guards (a first passkey, a session that
    can approve) has nothing else to prove itself with, so failing closed
    costs one ceremony and failing open costs the guarantee. ``subject``
    names the ceremony in this process's own log lines; ``denied_reason`` is
    what the daemon reads back on an explicit Deny.
    """
    platform = privilege_separation.current_platform()
    if platform == "darwin":
        confirm = _confirm_macos
    elif platform == "win32":
        confirm = _confirm_windows
    else:
        confirm = _confirm_linux
    try:
        allowed = confirm(prompt, timeout=CONFIRM_DIALOG_TIMEOUT_SECONDS)
    except _NoDialogAvailable as exc:
        logger.warning("Companion could not ask about %s: %s", subject, exc)
        return f"ERROR {exc}\n"
    except subprocess.TimeoutExpired:
        logger.warning("Nobody answered the %s dialog within %ss.", subject, CONFIRM_DIALOG_TIMEOUT_SECONDS)
        return "ERROR nobody answered the confirmation dialog -- try again\n"
    except Exception:
        # Anything else -- a missing osascript, a desktop with no display, a
        # ctypes failure on Windows -- is "could not ask", which is a refusal.
        logger.exception("Companion could not show the %s dialog", subject)
        return "ERROR could not ask for confirmation on this desktop\n"
    if not allowed:
        logger.warning("A %s was denied at the confirmation dialog.", subject)
        return f"ERROR {denied_reason}\n"
    return "OK\n"


def _confirm_first_enrollment() -> str:
    """``CONFIRM ENROLL``'s actual work -- see ``_ask_human()``."""
    return _ask_human(
        _CONFIRM_ENROLL_PROMPT,
        subject="first-passkey enrollment",
        denied_reason="enrollment was denied",
    )


def _confirm_console_sign_in() -> str:
    """``CONFIRM SIGNIN``'s actual work: the dialog behind
    ``privacyfence-app --print-sign-in-link`` (daemon_main.py), which is the
    break-glass way to a session that can approve when the companion's own
    menu is not reachable -- an SSH login, a Linux desktop whose applications
    menu nobody has open, a tray icon that failed to start.

    The dialog is the whole gate. The command runs as the same OS user the
    agent does, and the connection it arrives on carries nothing that tells
    the two apart (ADR 0002 decision 6), so what makes the resulting session
    attributable to a person is that a person clicked Allow on the desktop
    -- not that the request came from a terminal.
    """
    return _ask_human(
        _CONFIRM_SIGN_IN_PROMPT,
        subject="terminal sign-in link",
        denied_reason="the sign-in link was denied",
    )


# What ``SHOW`` may be asked to open -- the companion's own two menu items
# and nothing else. A path allowlist rather than a URL because the base URL
# is this install's own (``read_base_url()``) and the caller has no business
# choosing it: ``SHOW`` reaches a process that can mint a session able to
# approve, so the one thing it must not become is a way to point that
# session's landing page somewhere of the caller's choosing.
SHOW_PATHS = ("/approvals", "/settings")


def _show_page(path: str) -> str:
    """``SHOW <path>``'s actual work -- mint an attested code for ``path``
    and open it here, in the process the daemon can call back. Deferred
    import of nothing: ``_open_attested_url()`` lives in this module so
    companion.py's one-shot and this handler share one implementation."""
    if path not in SHOW_PATHS:
        return "ERROR unknown page\n"
    opened, reason = open_attested_url(path)
    return "OK\n" if opened else f"ERROR {reason}\n"


def _handle_companion_request(line: str) -> str:
    """The companion channel's own dispatch -- ``OPEN <url>`` and
    ``CONFIRM ENROLL``, both sent by the daemon (``request_open_url()``/
    ``request_enrollment_confirmation()`` below) and acted on here, in the
    companion process, which is the one thing in this architecture that
    still runs in the user's desktop session once #428 Phase 4 moves the
    daemon to a service account.

    ``OPEN`` is scheme-restricted to http(s) and ``CONFIRM``'s prompt is a
    constant of this module for the same single reason: pre-Phase-4, or on a
    Phase-4 install this dispatch is even reached from at all (see
    ``_verify_companion_peer()`` -- #428 B10 -- for who that is once
    separated), the caller is at minimum "anything running as the same OS
    user" (companion and daemon are still the same uid pre-Phase-4, agent
    included -- ADR 0002 decision 1), so neither command hands that caller
    an arbitrary ``file://``/custom-scheme URL to open, nor arbitrary words
    to put in a PrivacyFence-branded dialog.
    """
    parts = line.strip().split(maxsplit=1)
    command = parts[0].upper() if parts else ""
    argument = parts[1].strip() if len(parts) == 2 else ""
    if command == "CONFIRM":
        # Each subject spelled out rather than implied by a bare CONFIRM:
        # a new one is a new keyword here, never a change of meaning for
        # a line an older daemon already sends.
        subject, _, subargument = argument.partition(" ")
        subject = subject.upper()
        if subject == "ENROLL":
            return _confirm_first_enrollment()
        if subject == "MINT":
            # No dialog: the human already clicked, in this process, moments
            # ago -- asking again would put a second confirmation in front of
            # somebody who just answered the first one by choosing the menu
            # item. What is being established here is only that the line
            # reached the process that issued the nonce.
            if _consume_mint_nonce(subargument.strip()):
                return "OK\n"
            return "ERROR no sign-in was requested from this companion\n"
        if subject == "SIGNIN":
            return _confirm_console_sign_in()
        return "ERROR unknown command\n"
    if command == "SHOW":
        # A companion that is *not* this process (Linux's one-shot
        # ``--action open-approvals``, spawned fresh by an applications-menu
        # click) asking this one -- the process that owns this address, and
        # so the only one that can answer the daemon's CONFIRM MINT -- to do
        # the minting and the opening on its behalf. Without this, a Linux
        # click could only ever produce an unattested session, because the
        # process the human clicked exits before the daemon could call it
        # back. Same fixed-vocabulary posture as OPEN's scheme check: the
        # caller picks from this module's own paths, never supplies a URL.
        return _show_page(argument)
    if command != "OPEN" or not argument:
        return "ERROR unknown command\n"
    url = argument
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
    """A fresh, single-use bootstrap code, without restarting the daemon or
    going through a browser at all -- and **unattested**
    (session_auth.py's ``PROVENANCE_UNATTESTED``): the session it exchanges
    for may view PrivacyFence but not approve anything, because a bare
    ``MINT`` is reachable by anything running as this OS user and the daemon
    has no way to tell which of them sent it.

    That is what this call has always been; what changed is that the
    resulting session now says so. The attested counterparts are
    ``mint_attested_bootstrap_code()`` (the companion's own menu) and
    ``mint_console_bootstrap_code()`` (``--print-sign-in-link``). Raises ``ControlChannelError`` on anything other than a well-formed
    ``OK <code>`` reply; raises ``OSError`` (uncaught) if no daemon is
    listening at all -- callers that treat "no daemon running" as a normal,
    expected case (companion.py's own) catch that themselves."""
    reply = _send_to_daemon("MINT\n", timeout=timeout)
    if not reply.startswith("OK "):
        raise ControlChannelError(f"control channel mint failed: {reply!r}")
    return reply[len("OK "):].strip()


def mint_attested_bootstrap_code(*, timeout: float = 5.0) -> str:
    """A code that exchanges for a session which may *approve*, not merely
    view (session_auth.py's ``PROVENANCE_HUMAN``) -- what backs the
    companion's own Open Approvals/Open Settings items.

    Only callable from the process that owns the companion channel, because
    the nonce it sends is one this process issues to itself and the daemon
    redeems by calling straight back (``CONFIRM MINT``). A one-shot
    ``--action`` invocation has no channel of its own and must delegate to a
    running companion instead (``request_show()``), or settle for
    ``mint_bootstrap_code()``'s unattested code.

    Raises ``ControlChannelError`` on anything other than a well-formed
    ``OK <code>`` reply -- including the daemon's own refusal when the
    call-back did not confirm -- and ``OSError`` (uncaught) if no daemon is
    listening at all, same as ``mint_bootstrap_code()``."""
    reply = _send_to_daemon(f"MINT COMPANION {issue_mint_nonce()}\n", timeout=timeout)
    if not reply.startswith("OK "):
        raise ControlChannelError(f"control channel mint failed: {reply!r}")
    return reply[len("OK "):].strip()


def mint_console_bootstrap_code(*, timeout: float = CONFIRM_DIALOG_TIMEOUT_SECONDS + 20.0) -> str:
    """``privacyfence-app --print-sign-in-link``'s own mint (daemon_main.py):
    asks the daemon for a code that can approve, which the daemon grants only
    once the companion has put ``_CONFIRM_SIGN_IN_PROMPT`` in front of a human
    and they chose Allow.

    ``timeout`` sits past the companion's own dialog timeout for the same
    reason ``request_enrollment_confirmation()``'s does: the ordinary "nobody
    was at the keyboard" case should be reported by the process that actually
    knows it, in words, rather than guessed at from a socket timing out
    here. Further past it than the daemon's own wait on the companion
    (``request_sign_in_confirmation``, dialog + 5s), since a client that gives
    up at the same moment the daemon is finishing turns a perfectly good
    answer into a bare socket timeout in somebody's terminal."""
    reply = _send_to_daemon("MINT CONSOLE\n", timeout=timeout)
    if not reply.startswith("OK "):
        raise ControlChannelError(_error_reason(reply) or "the sign-in link was not confirmed")
    return reply[len("OK "):].strip()


def _error_reason(reply: str) -> str:
    """The human-facing half of an ``ERROR <reason>`` line. The reason is
    written where it is known (the companion's dialog handler, the daemon's
    own dispatch) and passed through unchanged by everything between, so a
    new reason at either end needs no matching change here."""
    reason = reply.strip()
    if reason.upper().startswith("ERROR"):
        reason = reason[len("ERROR"):].strip()
    return reason


def open_attested_url(path: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """Mint an attested code for ``path`` and open it in this session's
    browser -- the companion's Open Approvals/Open Settings, in one call, so
    the tray handler and the ``SHOW`` handler cannot drift apart. Returns
    ``(opened, reason)``; ``reason`` is empty on success and a short phrase
    otherwise, since both callers report rather than raise."""
    base_url = read_base_url()
    if base_url is None:
        return False, "PrivacyFence does not appear to be running (local mode)"
    try:
        code = mint_attested_bootstrap_code(timeout=timeout)
    except (OSError, ControlChannelError) as exc:
        return False, f"could not reach PrivacyFence's control channel: {exc}"
    try:
        opened = webbrowser.open(f"{base_url}{path}?bootstrap={code}")
    except Exception:
        logger.exception("Companion could not open a browser for %s", path)
        return False, "could not open a browser"
    return (True, "") if opened else (False, "could not open a browser")


def request_show(path: str, *, timeout: float = 5.0) -> bool:
    """Ask a *running* companion to open ``path`` itself (``SHOW``) -- what a
    one-shot ``--action`` invocation does instead of minting, since the
    session it could mint on its own would be unattested (see
    ``mint_attested_bootstrap_code()``). False whenever no companion answers,
    which is the ordinary case on an install where nothing autostarts one:
    companion.py falls back from there rather than failing the click."""
    if path not in SHOW_PATHS:  # pragma: no cover -- callers pass this module's own constants
        raise ValueError(f"path must be one of {SHOW_PATHS}, got {path!r}")
    try:
        if paths.is_windows():
            reply = send_line_windows(companion_pipe_name(), f"SHOW {path}\n", timeout=timeout)
        else:
            reply = send_line_posix(companion_socket_path(), f"SHOW {path}\n", timeout=timeout)
    except (OSError, ControlChannelError):
        return False
    return reply.startswith("OK")


def request_mint_attestation(nonce: str, *, timeout: float = 2.0) -> bool:
    """The daemon's own half of ``MINT COMPANION``: hand ``nonce`` back to
    whatever is listening on the companion's address and believe an ``OK``
    only from a process that recognizes it. False for every other outcome,
    no companion running included -- an unrecognized nonce and an absent
    companion mean the same thing here, which is that nothing has vouched
    for this mint.

    Short timeout, unlike the two confirmation calls either side of it: this
    one asks no human anything, so anything slower than a local socket round
    trip is already wrong."""
    try:
        if paths.is_windows():
            reply = send_line_windows(companion_pipe_name(), f"CONFIRM MINT {nonce}\n", timeout=timeout)
        else:
            reply = send_line_posix(companion_socket_path(), f"CONFIRM MINT {nonce}\n", timeout=timeout)
    except (OSError, ControlChannelError) as exc:
        logger.warning("Could not reach the companion to confirm a mint: %s", exc)
        return False
    return reply.startswith("OK")


def request_sign_in_confirmation(
    *, timeout: float = CONFIRM_DIALOG_TIMEOUT_SECONDS + 5.0,
) -> tuple[bool, str]:
    """The daemon's own half of ``MINT CONSOLE``: ask a running companion to
    confirm, with a human at its login session, a sign-in link that will be
    able to approve. Same shape and same reasoning as
    ``request_enrollment_confirmation()`` below -- "no companion is running"
    is a refusal with an actionable reason, not a soft failure to fall back
    from, because the thing being established is precisely that a human and
    not this machine's agent asked."""
    try:
        if paths.is_windows():
            reply = send_line_windows(companion_pipe_name(), "CONFIRM SIGNIN\n", timeout=timeout)
        else:
            reply = send_line_posix(companion_socket_path(), "CONFIRM SIGNIN\n", timeout=timeout)
    except (OSError, ControlChannelError) as exc:
        logger.warning("Could not reach the companion to confirm a terminal sign-in link: %s", exc)
        return False, (
            "PrivacyFence could not reach its companion app, which is what asks you to confirm a "
            "sign-in link that can approve. Start PrivacyFence's companion (the menu-bar/tray "
            "icon, or the PrivacyFence entry in your applications menu) and try again."
        )
    if reply.startswith("OK"):
        return True, ""
    return False, _error_reason(reply) or "the companion did not confirm this sign-in link"


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


def request_enrollment_confirmation(
    *, timeout: float = CONFIRM_DIALOG_TIMEOUT_SECONDS + 5.0,
) -> tuple[bool, str]:
    """Ask a running companion to confirm a *first* passkey enrollment with
    the human at its own login session -- the daemon-side half of
    ``_confirm_first_enrollment()`` above, and the mirror image of
    ``request_open_url()``: same channel, same client plumbing, opposite
    meaning for a missing companion.

    Returns ``(confirmed, reason)``. ``confirmed`` is True only for an
    explicit ``OK`` from a companion that actually asked somebody;
    ``reason`` is a short, human-facing phrase for every other case, which
    web/routes_security.py puts in front of whoever is trying to enroll.
    Unlike ``request_open_url()``, "no companion is running right now" is
    **not** a soft failure to fall back from -- there is no local fallback
    that would mean anything, since the thing being established is that a
    human and not this machine's agent asked for this. It is a refusal with
    an actionable reason, and the reason names starting the companion.

    ``timeout`` sits just past the companion's own dialog timeout, so the
    ordinary "nobody was at the keyboard" case is reported by the process
    that actually knows it (``_confirm_first_enrollment``) rather than
    guessed at from a socket timing out here.
    """
    try:
        if paths.is_windows():
            reply = send_line_windows(companion_pipe_name(), "CONFIRM ENROLL\n", timeout=timeout)
        else:
            reply = send_line_posix(companion_socket_path(), "CONFIRM ENROLL\n", timeout=timeout)
    except (OSError, ControlChannelError) as exc:
        logger.warning("Could not reach the companion to confirm a first passkey enrollment: %s", exc)
        return False, (
            "PrivacyFence could not reach its companion app, which is what asks you to confirm "
            "a first passkey. Start PrivacyFence's companion (the menu-bar/tray icon, or the "
            "PrivacyFence entry in your applications menu) and try again."
        )
    if reply.startswith("OK"):
        return True, ""
    return False, _error_reason(reply) or "the companion did not confirm this enrollment"


__all__ = [
    "COMPANION_SOCKET_FILE_NAME",
    "CONFIRM_DIALOG_TIMEOUT_SECONDS",
    "SHOW_PATHS",
    "SIGN_IN_MINT_DECISION",
    "SOCKET_FILE_NAME",
    "WEB_BASE_URL_FILE_NAME",
    "CompanionChannelServer",
    "ControlChannelError",
    "ControlChannelServer",
    "companion_pipe_name",
    "companion_pipe_name_for",
    "companion_socket_path",
    "companion_socket_path_under",
    "issue_mint_nonce",
    "mint_attested_bootstrap_code",
    "mint_bootstrap_code",
    "mint_console_bootstrap_code",
    "open_attested_url",
    "pipe_name_for",
    "posix_socket_path",
    "read_base_url",
    "request_enrollment_confirmation",
    "request_mint_attestation",
    "request_open_url",
    "request_quit",
    "request_show",
    "request_sign_in_confirmation",
    "send_line_posix",
    "send_line_windows",
    "socket_path_under",
    "windows_pipe_name",
]
