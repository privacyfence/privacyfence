"""Shared local-mode session/CSRF/bootstrap helpers for every route in the
combined web app -- "/approvals and /settings are one application: one
header, one nav, one palette, one session, links both ways" -- factored
out of web/routes_approvals.py,
which owned this logic alone before web/routes_settings.py needed the exact
same posture on a second set of routes sharing the same session and the
same ``pf_session`` cookie.

**Why the cookie is not a shared secret.** A persistent, never-rotated
secret carried in a URL -- one the daemon logs on every startup and reuses
across restarts -- stays valid after a single leak (a log file, shell
history, a shared screen) until someone deletes it by hand: no expiry, no
single-use exchange, no way to revoke just that one exposure.

So this module uses two purpose-built pieces instead,
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
  "cookie with idle + absolute expiry" the local session needs. Nothing about a
  session id is ever derived from, or comparable to, any other secret in
  this install.

**Minting a code on demand.** A code minted by presenting a persistent
local secret to an HTTP endpoint on the loopback port would be only as
strong as that secret's file permissions -- and a chain doesn't get
stronger by hardening its middle (ADR 0002's own framing): anything on the
machine that could read the file could reach the endpoint too, agent
included. So ``web/control_channel.py``'s ``ControlChannelServer`` -- a Unix domain
socket on macOS/Linux, an ACL'd named pipe on Windows -- is the only way
to mint a code on demand, and it isn't reachable over the loopback port a
browser (or a page's own ``fetch()``) can speak at all (ADR 0002 decision
2). See that module's own docstring for the full reasoning.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from .. import paths, privilege_separation, web_shell
from ..principal import LOCAL_PRINCIPAL_ID, Principal, current_principal
from .csp import nonce_for

SESSION_COOKIE = "pf_session"
BOOTSTRAP_QUERY_PARAM = "bootstrap"

# Short-lived: long enough for a human to actually click the link the
# daemon just logged, short enough that a leaked log line/terminal
# scrollback stops being a usable credential quickly. Single-use (see
# BootstrapStore.consume) matters far more than the exact figure here --
# this is defense in depth on top of that, not the primary control.
BOOTSTRAP_TTL_SECONDS = 10 * 60

# Sliding idle timeout -- same posture and same figure as
# org_session.py's own DEFAULT_IDLE_TIMEOUT_SECONDS (a short idle
# timeout), renewed on every authenticated request so an active user is
# never logged out mid-task; an abandoned tab is.
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60

# Absolute cap from creation, regardless of activity -- the other half of
# "cookie with idle + absolute expiry". Long enough that a background
# daemon someone actively uses through a workday doesn't force a
# re-bootstrap mid-task; short enough that a session has a real ceiling.
# Once it lapses, the answer to "how do I get back in" is a fresh bootstrap
# code: minted automatically on the next daemon restart, or on demand over
# the control channel (see this module's own docstring).
DEFAULT_ABSOLUTE_TIMEOUT_SECONDS = 24 * 60 * 60

# Provenance: how the session in front of us was established, which is the one thing about a ``pf_session``
# nothing downstream used to record.
#
# ADR 0002 decision 6 names three ways a local process reaches a session,
# all of them by design: the companion's own Open Approvals item, a
# bootstrap link a human was handed, and a bare ``MINT`` on the control
# channel (web/control_channel.py) -- which privilege separation *widens*
# from 0600 to 0660 so the companion can reach it, putting the agent in the
# same group. The three produced the same object with the same authority, so
# "a session exists" was treated as "a human is here", and an agent holding
# one could release the write it had itself requested.
#
# ``human`` is a session minted through the companion -- the one PrivacyFence
# process that runs where a human can actually be asked (see
# control_channel.py's ``CONFIRM MINT``/``CONFIRM SIGNIN``). ``unattested``
# is everything else: a bare ``MINT``, or a code that reached a browser by
# some route this daemon cannot attribute to a person. Viewing is unchanged
# either way; approving a decision, and every _SENSITIVE_ACTIONS settings
# change, requires ``human`` (ADR 0062).
#
# What this is not: authentication of the companion. Companion and agent
# share an OS user, so an agent that binds the companion's own address before
# the companion does answers for it -- the identical limit ADR 0003's
# first-enrollment gate already accepts and docs/security-and-compliance.md
# already states. What it buys is that the two silent paths stop being
# interchangeable with the attended one, and that forging the attended one
# costs impersonating a process whose absence the human notices.
PROVENANCE_HUMAN = "human"
PROVENANCE_UNATTESTED = "unattested"


@dataclass
class _Session:
    created_at: float
    last_seen_at: float
    provenance: str = PROVENANCE_UNATTESTED
    # ADR 0008: which OS-user principal this session belongs to, resolved
    # from the control channel's own peer credentials at mint time
    # (web/control_channel.py's MINT/MINT COMPANION/MINT CONSOLE). Defaults
    # to LOCAL_PRINCIPAL_ID -- the safe answer for a caller that predates
    # this field (an old daemon build's own in-process test, this module's
    # existing single-principal tests) is "the one principal local mode has
    # always had", not "unknown".
    principal_id: str = LOCAL_PRINCIPAL_ID


class LocalSessionStore:
    """Server-side session store backing the local-mode ``pf_session``
    cookie -- the direct local-mode counterpart of
    web/org_session.py's ``OrgSessionStore``. Through ADR 0008 this
    docstring said it carried no ``Principal`` at all, because local mode
    had exactly one identity; now that a separated install can have one
    principal per OS user (``os-<uid>``/``os-<sid>``, or ``LOCAL_PRINCIPAL``
    for the install's owner), each session remembers which one minted it --
    see ``_Session.principal_id`` and web/server.py's ``_PrincipalScopeMiddleware``,
    which is what reads it back."""

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

    def create(self, *, provenance: str = PROVENANCE_UNATTESTED, principal_id: str = LOCAL_PRINCIPAL_ID) -> str:
        """``provenance`` defaults to ``unattested`` on purpose: the safe
        answer to "how did this session get here" is "I cannot say", and a
        caller that *can* say (web/server.py's ``_BootstrapMiddleware``,
        passing through whatever the consumed code carried) says so
        explicitly. ``principal_id`` is the same story, one dimension over
        (ADR 0008): the safe default is the one principal every session
        resolved to before this field existed."""
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions[session_id] = _Session(
                created_at=now, last_seen_at=now, provenance=provenance, principal_id=principal_id,
            )
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

    def provenance(self, session_id: str) -> str | None:
        """How ``session_id`` was established, or ``None`` if there is no
        such session. Deliberately does *not* renew ``last_seen_at`` the way
        ``touch()`` does -- every caller reads this alongside an
        authentication check that has already touched the session, and an
        authorization question should not be able to keep a session alive on
        its own."""
        with self._lock:
            session = self._sessions.get(session_id)
            return None if session is None else session.provenance

    def principal_id(self, session_id: str) -> str | None:
        """Which principal ``session_id`` belongs to (ADR 0008), or ``None``
        if there is no such session -- same non-renewing posture as
        ``provenance()``, and the same caller (web/server.py's
        ``_PrincipalScopeMiddleware``)."""
        with self._lock:
            session = self._sessions.get(session_id)
            return None if session is None else session.principal_id

    def destroy(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


class BootstrapStore:
    """One-time codes that exchange for exactly one
    ``LocalSessionStore`` session -- minted server-side, either by
    daemon_main.py at every startup or on demand over the control channel
    (web/control_channel.py) once a previous code/session has already
    expired."""

    def __init__(self, *, ttl_seconds: float = BOOTSTRAP_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        # code -> (expires_at, provenance, principal_id)
        self._codes: dict[str, tuple[float, str, str]] = {}

    def mint(self, *, provenance: str = PROVENANCE_UNATTESTED, principal_id: str = LOCAL_PRINCIPAL_ID) -> str:
        """``provenance`` travels with the code and lands on the session it
        exchanges for -- the mint is the only moment anything knows how this
        credential came to exist, so recording it anywhere later would be
        guesswork. Same defaults-to-``unattested`` reasoning as
        ``LocalSessionStore.create()``. ``principal_id`` (ADR 0008) is the
        same story: the control channel is the only place that ever learns
        which OS user asked, and it is gone the moment this code is minted."""
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._codes[code] = (time.time() + self._ttl_seconds, provenance, principal_id)
        return code

    def consume(self, code: str) -> tuple[str, str] | None:
        """The ``(provenance, principal_id)`` ``code`` was minted with iff it
        was live and unexpired, else ``None`` -- always removes it first, so
        presenting it again (a slow double-click, a replayed request, an
        attacker who intercepted it after the fact) never gets a second
        attempt, successful exchange or not."""
        if not code:
            return None
        with self._lock:
            entry = self._codes.pop(code, None)
        if entry is None:
            return None
        expires_at, provenance, principal_id = entry
        return (provenance, principal_id) if expires_at >= time.time() else None


def authenticated(request: Request, sessions: LocalSessionStore) -> bool:
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if not session_id:
        return False
    return sessions.touch(session_id)


def resolve_principal(request: Request, sessions: LocalSessionStore) -> Principal | None:
    """The small resolve-or-reject helper alongside ``authenticated()`` above:
    ``current_principal()`` when the session cookie is live, else
    ``None`` -- the same ``Principal | None`` shape web/org_session.py's own
    ``authenticated()`` already returns, so web/routes_approvals.py's merged
    route builder can treat both modes' auth gate identically (``resolve_principal(request)``,
    reject on ``None``) even though local mode gets its principal from the
    ambient context var (ADR 0008: set once per request by
    ``_PrincipalScopeMiddleware``, not derived from ``sessions`` the way an
    org session's principal is)."""
    if not authenticated(request, sessions):
        return None
    return current_principal()


def session_provenance(request: Request, sessions: LocalSessionStore) -> str | None:
    """How the session behind ``request``'s cookie was established, or
    ``None`` when there is no live session at all -- which every caller
    treats exactly like ``unattested``, since "I have never seen this
    session" is not a better answer than "I cannot say who established
    it"."""
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if not session_id:
        return None
    return sessions.provenance(session_id)


def is_human_session(request: Request, sessions: LocalSessionStore) -> bool:
    """True iff this request rides a session a human was actually asked
    for (``PROVENANCE_HUMAN``, see this module's own constants). The
    question every approving decision and every sensitive settings action
    asks before it acts; viewing asks nothing."""
    return session_provenance(request, sessions) == PROVENANCE_HUMAN


def human_session_required_json(what: str) -> tuple[dict[str, str], int]:
    """The body and status a route returns when ``is_human_session()`` says
    no -- a ``403`` rather than the ``401`` an *unauthenticated* request
    gets, because the session is perfectly valid and the answer is still
    no. Returned as a plain ``(body, status)`` pair rather than a
    ``JSONResponse`` so this module keeps importing only what its own page
    rendering needs, and each caller builds the response type its own route
    already returns.

    ``what`` names the refused act ("approve a decision", "change this
    setting") -- the message has to be actionable for the human who is
    legitimately looking at the page and has no idea why their click did
    nothing, and the action they need is always the same one: reopen this
    page from the companion, which is what makes a session ``human``."""
    return (
        {
            "error": "human_session_required",
            "message": (
                f"This sign-in session cannot {what}. PrivacyFence can only tell that a person "
                "asked for a session when it was opened from the PrivacyFence companion (the "
                "menu-bar/tray icon, or the PrivacyFence entry in your applications menu) -- "
                "reopen Approvals from there and try again."
            ),
        },
        403,
    )


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="strict", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _companion_availability_sentence() -> str:
    """Whether the reader can expect the companion to already be running,
    which privilege separation decides -- differently per platform.

    It reads as a whole sentence of its own between "open the companion" and
    what that buys, so each branch ends in one. On a separated macOS/Windows
    install the tray item really is started for them at login, so "it should
    already be there" is a useful instruction.
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
        return "Nothing installs or starts it automatically on this install, so you may have to "\
               "launch it yourself first. "
    if privilege_separation.current_platform() == "linux":
        return (
            "This install has no tray icon (ADR 0002 decision 4) -- the Applications-menu "
            "entry is the way in. "
        )
    return "This install runs it at login for you, so it should already be there. "


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
    logger in the process is wrapped in SecretRedactingFormatter,
    whose key=value pattern matches the literal word ``bootstrap`` and
    scrubs the code to ``bootstrap=[REDACTED]`` before the line ever
    reaches a file or a terminal -- restarting PrivacyFence changed nothing
    about that, since the fresh line from the new process is redacted the
    same way.

    This page also used to lead with "ask Claude", which called
    ``privacyfence_get_sign_in_link`` and handed the reader a link inside
    the conversation. That tool is retired: it handed a live session credential to the party the credential
    governs, and its own justification -- a headless daemon with an optional
    companion -- expired when ADR 0003 made the companion mandatory and
    autostarted on all three platforms.

    So the page leads with the companion (ADR 0002), which is
    also the only route to a session that may approve rather than merely
    view (``PROVENANCE_HUMAN`` above), and offers ``privacyfence-app
    --print-sign-in-link`` for a reader whose companion menu is out of
    reach. The discovery file this page used to point at -- the one
    ``web/server.py`` wrote a live link into on every startup -- is gone
    for the same reason the tool is: it sat
    in a group-shared directory, which made it a session for the taking.
    What is still spelled out last, for a reader who has neither of the
    first two, is the control channel's own raw command
    (``web/control_channel.py``) -- that one mints an unattested code, which
    is enough to see what is waiting. ``request`` is used only for the
    response's CSP nonce: unlike the old bearer-header ``curl`` command, the control
    channel is a local socket/pipe, not another HTTP endpoint on this
    page's own origin, so there's no origin left to splice into the
    recovery command.

    The recovery command is platform-dependent, and its socket/pipe address
    is resolved from the real, live directory this install uses
    (``~/.privacyfence`` on POSIX, ``%LOCALAPPDATA%\\PrivacyFence`` on
    Windows -- see ``paths.data_dir()``'s own docstring). It needs no Python -- a packaged
    install doesn't guarantee one on ``PATH``, so this leans on the same kind of
    already-present OS tool instead: POSIX gets ``nc -U`` (the BSD ``nc``
    macOS ships, and the ``netcat-openbsd`` build Debian/Ubuntu's default
    ``nc`` symlinks to, both support connecting to a Unix domain socket via
    ``-U``), Windows gets pure PowerShell against
    ``System.IO.Pipes.NamedPipeClientStream`` (built into every supported
    .NET runtime, so no extra install either)."""
    data_dir = paths.data_dir()
    # handoff_dir() for the address a *reader of this page* has to reach:
    # privilege separation puts the control socket in a user-reachable
    # subdirectory on a privilege-separated install, and this page's whole
    # job is telling a locked-out human where to find it. Identical to
    # data_dir() everywhere else.
    handoff = paths.handoff_dir()
    # Deferred import: control_channel.py imports BootstrapStore from this
    # module, so importing it back at module scope here would be circular.
    from .control_channel import socket_path_under, windows_pipe_name

    if paths.is_windows():
        pipe_name = windows_pipe_name().rsplit("\\", 1)[-1]
        command = (
            "$p=New-Object System.IO.Pipes.NamedPipeClientStream('.','" + pipe_name + "',"
            "[System.IO.Pipes.PipeDirection]::InOut); $p.Connect(5000); "
            "$w=New-Object System.IO.StreamWriter($p); $w.AutoFlush=$true; $w.WriteLine('MINT'); "
            "(New-Object System.IO.StreamReader($p)).ReadLine()"
        )
    else:
        # A plain join, not control_channel.posix_socket_path() -- that
        # calls the real, side-effecting paths.authority_dir() (creates the
        # directory), which this
        # unauthenticated error page has no business triggering on every
        # hit. socket_path_under() is the pure half of that same logic.
        # On a privilege-separated install the socket isn't under ``authority`` at
        # all (paths.control_socket_dir()) -- it lives in the handoff
        # directory so a companion running as the human can still reach it.
        sock_root = handoff if privilege_separation.is_enabled() else data_dir / "authority"
        sock_path = socket_path_under(sock_root)
        command = f"printf 'MINT\\n' | nc -U '{sock_path}'"
    body = (
        "<h1>Not authorized</h1>"
        "<p>Open PrivacyFence's companion app -- a "
        "tray/menu-bar icon on macOS/Windows, or its entry in your Applications menu on Linux -- "
        "and use its Open Approvals (or Open Settings) item to get back in. "
        + _companion_availability_sentence() +
        "It is also the only way back to a session that can <em>approve</em> what is waiting: a "
        "link from anywhere else signs you in to look, not to release.</p>"
        "<p>No companion you can reach right now? From a terminal on this machine, run "
        "<code>privacyfence-app --print-sign-in-link</code> and open the link it prints. Your AI "
        "client cannot do this for you: PrivacyFence never gives a sign-in link to the program "
        "it governs.</p>"
        "<p>This link has expired, was already used, or your session timed out. "
        "(Not the log file: <code>privacyfence.log</code> deliberately redacts this link's "
        "code for security, so it never contains a usable one — restarting PrivacyFence "
        "doesn't change that. The link is not written to any file either, where every program "
        "running as you could read it.)</p>"
        "<p>Neither of the above available? From a terminal on this machine, mint a "
        "view-only link on demand and open what it returns:</p>"
        f"<pre>{command}</pre>"
    )
    return HTMLResponse(
        web_shell.plain_page(body, title="PrivacyFence — Not authorized", nonce=nonce_for(request)),
        status_code=401,
        # This
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
    tracked. Constant-time compare, so the check leaks no timing."""
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
    "PROVENANCE_HUMAN",
    "PROVENANCE_UNATTESTED",
    "SESSION_COOKIE",
    "BootstrapStore",
    "LocalSessionStore",
    "authenticated",
    "check_csrf",
    "check_origin",
    "clear_session_cookie",
    "human_session_required_json",
    "is_human_session",
    "resolve_principal",
    "session_provenance",
    "set_session_cookie",
    "unauthorized_html",
]
