"""Shared local-mode session/CSRF/bootstrap helpers for every route in the
combined web app -- "/approvals and /settings are one application: one
header, one nav, one palette, one session, links both ways" -- factored
out of web/routes_approvals.py,
which owned this logic alone before web/routes_settings.py needed the exact
same posture on a second set of routes sharing the same session and the
same ``pf_session`` cookie.

**SEC-06.** Through
v4.0.0a12 this module's whole model was "the cookie's own value is the one
shared secret everyone in the install has" -- the persistent, never-
rotated ``web_token`` itself, carried in the ``?token=`` query string the
daemon logged on every startup and reused, unchanged, across restarts
forever. That meant a token that leaked once (a log file, shell history, a
shared screen) stayed valid until someone manually deleted the token file
and restarted the daemon: no expiry, no single-use exchange, no way to
revoke just that one exposure.

This module now splits that one secret into two purpose-built pieces,
mirroring web/org_session.py's own real-session model (minus the
per-principal identity org mode needs and local mode doesn't):

- ``BootstrapStore`` mints short-lived, single-use codes -- the only thing
  ever carried in a URL (``?bootstrap=<code>``) or written to a log line
  (see daemon_main.py's own startup logging and web/server.py's
  ``_BootstrapMiddleware``). A code is consumed -- deleted -- the instant
  it's checked, valid or not, so it is exactly one attempt at establishing
  a session, never a standing credential.
- ``LocalSessionStore`` holds the real, server-side sessions a bootstrap
  exchange mints: an independent random id, a sliding idle timeout, and a
  hard absolute timeout from creation regardless of activity -- the
  "cookie with idle + absolute expiry" SEC-06 asks for. Nothing about a
  session id is ever derived from, or comparable to, any other secret in
  this install.

**#428 Phase 2.** Minting a fresh code on demand -- once a previous session
or link has already expired, without restarting the daemon -- used to mean
presenting a persistent local secret (``web_token``) as an ``Authorization``
header to ``POST /api/bootstrap``, over the same loopback HTTP port a
browser uses. That secret's only remaining job, once SEC-06 got everything
else off it, was authorizing that one endpoint -- and a chain doesn't get
stronger by hardening its middle (ADR 0002's own framing): anything on the
machine that could read the token file could reach the endpoint too, agent
included. Phase 2 retires that design rather than reinforcing it:
``web/control_channel.py``'s ``ControlChannelServer`` -- a Unix domain
socket on macOS/Linux, an ACL'd named pipe on Windows -- is now the only way
to mint a code on demand, and it isn't reachable over the loopback port a
browser (or a page's own ``fetch()``) can speak at all. See that module's
own docstring for the full reasoning.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from .. import paths, privilege_separation

SESSION_COOKIE = "pf_session"
BOOTSTRAP_QUERY_PARAM = "bootstrap"

# Short-lived: long enough for a human to actually click the link the
# daemon just logged, short enough that a leaked log line/terminal
# scrollback stops being a usable credential quickly. Single-use (see
# BootstrapStore.consume) matters far more than the exact figure here --
# this is defense in depth on top of that, not the primary control.
BOOTSTRAP_TTL_SECONDS = 10 * 60

# Sliding idle timeout -- same posture and same figure as
# org_session.py's own DEFAULT_IDLE_TIMEOUT_SECONDS (§9.4: "short idle
# timeout"), renewed on every authenticated request so an active user is
# never logged out mid-task; an abandoned tab is.
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60

# Absolute cap from creation, regardless of activity -- SEC-06's own
# "cookie with idle + absolute expiry". Long enough that a background
# daemon someone actively uses through a workday doesn't force a
# re-bootstrap mid-task; short enough that a session -- unlike the
# previous design's literally-forever token -- has a real ceiling. Once it
# lapses, the answer to "how do I get back in" is a fresh bootstrap code:
# minted automatically on the next daemon restart, or on demand via
# ``POST /api/bootstrap`` (see this module's own docstring).
DEFAULT_ABSOLUTE_TIMEOUT_SECONDS = 24 * 60 * 60


@dataclass
class _Session:
    created_at: float
    last_seen_at: float


class LocalSessionStore:
    """Server-side session store backing the local-mode ``pf_session``
    cookie (SEC-06) -- the direct local-mode counterpart of
    web/org_session.py's ``OrgSessionStore``, minus the ``Principal`` each
    org session carries (local mode has exactly one identity, the same
    ``LOCAL_PRINCIPAL`` it always resolves to; see web/server.py's
    ``_default_principal``)."""

    def __init__(
        self,
        *,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        absolute_timeout_seconds: float = DEFAULT_ABSOLUTE_TIMEOUT_SECONDS,
    ) -> None:
        self._idle_timeout_seconds = idle_timeout_seconds
        self._absolute_timeout_seconds = absolute_timeout_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def create(self) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions[session_id] = _Session(created_at=now, last_seen_at=now)
        return session_id

    def touch(self, session_id: str) -> bool:
        """True, and refreshes ``last_seen_at``, iff ``session_id`` is live
        and neither idle- nor absolute-expired -- an expired session found
        here is evicted on the spot, the same "just not authenticated, not
        a 500" posture ``OrgSessionStore.get()`` takes for an unknown or
        stale cookie."""
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            if (now - session.last_seen_at) > self._idle_timeout_seconds:
                del self._sessions[session_id]
                return False
            if (now - session.created_at) > self._absolute_timeout_seconds:
                del self._sessions[session_id]
                return False
            session.last_seen_at = now
            return True

    def destroy(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


class BootstrapStore:
    """One-time codes (SEC-06) that exchange for exactly one
    ``LocalSessionStore`` session -- minted server-side, either by
    daemon_main.py at every startup (the direct replacement for logging
    the long-lived token itself) or on demand via ``POST /api/bootstrap``
    (web/server.py) once a previous code/session has already expired."""

    def __init__(self, *, ttl_seconds: float = BOOTSTRAP_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._codes: dict[str, float] = {}  # code -> expires_at

    def mint(self) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._codes[code] = time.time() + self._ttl_seconds
        return code

    def consume(self, code: str) -> bool:
        """True iff ``code`` was live and unexpired -- always removes it
        first, so presenting it again (a slow double-click, a replayed
        request, an attacker who intercepted it after the fact) never gets
        a second attempt, successful exchange or not."""
        if not code:
            return False
        with self._lock:
            expires_at = self._codes.pop(code, None)
        return expires_at is not None and expires_at >= time.time()


def authenticated(request: Request, sessions: LocalSessionStore) -> bool:
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if not session_id:
        return False
    return sessions.touch(session_id)


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="strict", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _companion_availability_sentence() -> str:
    """Whether the reader can expect the companion to already be running,
    which #428 Phase 4 changed -- differently per platform.

    On a separated macOS/Windows install the tray item really is started for
    them at login, so "it should already be there" is a useful instruction.
    On Linux it is not: what a separated install autostarts is the invisible
    ``--serve`` channel (companion.py), the thing that lets the daemon open a
    browser for connector OAuth from outside the user's session. The
    affordance this paragraph is pointing at -- the Applications-menu entry
    -- is present whether or not anything has been separated, and is launched
    by clicking it. Telling a Linux reader to look for something that started
    itself would send them hunting for a tray icon this platform has never
    had (ADR 0002 decision 4).
    """
    if not privilege_separation.is_enabled():
        return "Nothing installs or starts it automatically yet, so "
    if privilege_separation.current_platform() == "linux":
        return (
            "This install has no tray icon (ADR 0002 decision 4) -- the Applications-menu "
            "entry is the way in. "
        )
    return "This install runs it at login for you (#428 Phase 4), so it should already be there. "


def unauthorized_html(request: Request) -> Response:
    """The page a human actually lands on with no valid ``pf_session``
    cookie -- most commonly a bootstrap link that's already been used (it's
    single-use by design, see ``BootstrapStore.consume``) or a session that
    idle-/absolute-timed out, reopened by something like a browser
    restoring a previously-open tab verbatim rather than a fresh click on a
    freshly-logged link.

    This used to point readers at ``privacyfence.log`` for "the newest
    sign-in link PrivacyFence logged" -- advice that never worked and never
    will: daemon_main.py's startup log line embeds the link, but every
    logger in the process is wrapped in SecretRedactingFormatter (SEC-10),
    whose key=value pattern matches the literal word ``bootstrap`` and
    scrubs the code to ``bootstrap=[REDACTED]`` before the line ever
    reaches a file or a terminal -- restarting PrivacyFence changed nothing
    about that, since the fresh line from the new process is redacted the
    same way. The three things that actually work, in the order most
    readers can actually use them: asking a connected MCP client (e.g.
    Claude -- installed alongside PrivacyFence per README.md's Quick start,
    so this is available even on a first run, before Settings has ever been
    opened) to call ``privacyfence_get_sign_in_link`` (web/mcp_tools.py),
    which mints one and hands it straight back in the conversation -- no
    terminal at all -- is also the one this page now leads with (issue
    #423's proposed-fix part 3): P10 removed the menu bar, so "ask Claude"
    is the intended recovery path on a headless install, not a fallback
    buried under the "why you're here" line; the discovery file
    ``web/server.py``'s
    ``mint_bootstrap_url()`` writes outside the logging pipeline every time
    PrivacyFence (re)starts, for a reader who'd rather grab it themselves;
    and minting a fresh code on demand through the control channel (#428
    Phase 2, ``web/control_channel.py``) without restarting anything, for a
    reader with neither -- this page spells out the actual command for that
    last one rather than just naming the channel, since a reader who's
    landed here from a dead link and has no MCP client connected yet is
    exactly the audience that finding this self-explanatory matters most
    for.

    #428 Phase 3 (ADR 0002) adds a fifth path, and the page now leads with
    it ahead of "ask Claude": PrivacyFence's optional companion app (a
    tray/menu-bar icon on macOS/Windows, an Applications-menu entry on
    Linux) mints and opens a fresh link itself, from its own Open Approvals/
    Open Settings items -- no MCP client, no terminal. Nothing installs or
    autostarts it yet, though (that's #428 Phase 4), so this page can't
    assume it's running and still lists the other four. ``request`` is
    otherwise unused here: unlike the old bearer-header
    ``curl`` command, the control channel is a local socket/pipe, not
    another HTTP endpoint on this page's own origin, so there's no origin
    left to splice into the recovery command.

    The discovery-file path and the recovery command are both platform-
    dependent -- ``paths.data_dir()`` resolves to the real, live directory
    this install actually writes ``approvals_url`` into (``~/.privacyfence``
    on POSIX, ``%LOCALAPPDATA%\\PrivacyFence`` on Windows, see that
    function's own docstring). Neither command needs Python -- a packaged
    install doesn't guarantee one on ``PATH`` any more than the pre-Phase-2
    page's ``curl`` was guaranteed, so this leans on the same kind of
    already-present OS tool instead: POSIX gets ``nc -U`` (the BSD ``nc``
    macOS ships, and the ``netcat-openbsd`` build Debian/Ubuntu's default
    ``nc`` symlinks to, both support connecting to a Unix domain socket via
    ``-U``), Windows gets pure PowerShell against
    ``System.IO.Pipes.NamedPipeClientStream`` (built into every supported
    .NET runtime, so no extra install either)."""
    data_dir = paths.data_dir()
    # handoff_dir() for the files a *reader of this page* goes looking for:
    # #428 Phase 4 moves them to a user-reachable subdirectory on a
    # privilege-separated install, and this page's whole job is telling a
    # locked-out human where to look. Identical to data_dir() everywhere
    # else. Unlike authority_dir(), neither call runs a migration.
    handoff = paths.handoff_dir()
    # Deferred import: control_channel.py imports BootstrapStore from this
    # module, so importing it back at module scope here would be circular.
    from .control_channel import socket_path_under, windows_pipe_name

    if paths.is_windows():
        approvals_url_path = f"{handoff}\\approvals_url"
        pipe_name = windows_pipe_name().rsplit("\\", 1)[-1]
        command = (
            "$p=New-Object System.IO.Pipes.NamedPipeClientStream('.','" + pipe_name + "',"
            "[System.IO.Pipes.PipeDirection]::InOut); $p.Connect(5000); "
            "$w=New-Object System.IO.StreamWriter($p); $w.AutoFlush=$true; $w.WriteLine('MINT'); "
            "(New-Object System.IO.StreamReader($p)).ReadLine()"
        )
    else:
        approvals_url_path = f"{handoff}/approvals_url"
        # A plain join, not control_channel.posix_socket_path() -- that
        # calls the real, side-effecting paths.authority_dir() (creates the
        # directory, runs its migration-on-first-use), which this
        # unauthenticated error page has no business triggering on every
        # hit. socket_path_under() is the pure half of that same logic.
        # On a #428 Phase 4 install the socket isn't under ``authority`` at
        # all (paths.control_socket_dir()) -- it moved to the handoff
        # directory so a companion running as the human can still reach it.
        sock_root = handoff if privilege_separation.is_enabled() else data_dir / "authority"
        sock_path = socket_path_under(sock_root)
        command = f"printf 'MINT\\n' | nc -U '{sock_path}'"
    return HTMLResponse(
        "<!DOCTYPE html><html><body style=\"font:15px sans-serif;padding:40px;max-width:640px\">"
        "<p><strong>Not authorized.</strong> If PrivacyFence's companion app is running -- a "
        "tray/menu-bar icon on macOS/Windows, or its entry in your Applications menu on Linux -- "
        "use its Open Approvals (or Open Settings) item to get back in directly, no MCP client or "
        "terminal needed. " + _companion_availability_sentence() + "if that's not an option:</p>"
        "<p>Ask Claude (or any other MCP client already "
        "connected to PrivacyFence) to get you back in — it can call the "
        "<code>privacyfence_get_sign_in_link</code> tool and hand you a fresh sign-in link "
        "directly, no terminal needed. That's the fastest way back in on a headless "
        "install, so it leads here.</p>"
        "<p>This link has expired, was already used, or your session timed out.</p>"
        "<p>Prefer to grab it yourself? PrivacyFence just wrote the current one to "
        f"<code>{approvals_url_path}</code> (or <code>settings_url</code> for "
        "Settings) — every startup, and every time an old one is superseded, replaces "
        "it with a fresh one. (Not the log file: <code>privacyfence.log</code> "
        "deliberately redacts this link's code for security, so it never contains a "
        "usable one — restarting PrivacyFence doesn't change that.)</p>"
        "<p>No MCP client connected yet, and don't want to restart PrivacyFence just for "
        "this? From a terminal on this machine, mint a new one on demand and open the "
        "link it returns:</p>"
        "<pre style=\"white-space:pre-wrap;background:#f0f0f0;padding:10px;"
        f"border-radius:4px\">{command}</pre>"
        "</body></html>",
        status_code=401,
        # SEC-18: this
        # page carries a live control-channel path/pipe name (the exact
        # command a reader is meant to copy-paste) -- no-store even on the
        # 401 branch, not just the authenticated pages it stands in for.
        headers={"Cache-Control": "no-store"},
    )


def check_csrf(request: Request, csrf: str | None) -> bool:
    """Double-submit check: the session cookie (HttpOnly, so page JS never
    reads it -- it can only have been set by this server's own
    set_session_cookie) must equal the csrf value the page's own bridge
    shim baked in at render time -- the session id itself doubles as the
    CSRF token, the same reasoning web/org_session.py's own check_csrf
    gives for why no separate per-session value needs to be minted and
    tracked. Constant-time compare -- same posture ipc_server.py's own
    token check took for ~/.privacyfence/ipc_token, before P5 deleted
    both."""
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie or not csrf:
        return False
    return hmac.compare_digest(cookie, csrf)


def check_origin(request: Request) -> bool:
    """Defense in depth on top of the double-submit token above -- a
    same-site page couldn't forge the cookie value into its own request
    body, but this also stops a same-origin-cookie-jar edge case from ever
    mattering. ``None`` (no Origin header at all, e.g. a same-origin
    navigation in some browsers) is accepted -- only a *mismatched* Origin
    is rejected."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin == f"{request.url.scheme}://{request.url.netloc}"


__all__ = [
    "BOOTSTRAP_QUERY_PARAM",
    "BOOTSTRAP_TTL_SECONDS",
    "DEFAULT_ABSOLUTE_TIMEOUT_SECONDS",
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "SESSION_COOKIE",
    "BootstrapStore",
    "LocalSessionStore",
    "authenticated",
    "check_csrf",
    "check_origin",
    "clear_session_cookie",
    "set_session_cookie",
    "unauthorized_html",
]
