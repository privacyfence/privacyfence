"""#428 Phase 3 (ADR 0002): the companion app -- a human's own path into
PrivacyFence's web UI that doesn't route a credential through the agent.

See ``docs/adr/0002-local-mode-trust-boundary-and-companion-app.md`` for the
full design; this module is decisions 2-5 of that ADR, built. Scope is
deliberately minimal (decision 2): it mints its own sign-in links over
``web/control_channel.py``'s daemon-owned channel and opens them in the
user's default browser, it can ask the daemon to quit, and (decision 5) it
listens on its *own* channel (``web.control_channel.CompanionChannelServer``)
for the daemon to hand it a URL to open for a connector's OAuth flow. It
renders no PrivacyFence content of its own -- no HTML, no approval or
settings UI reachable from here (decision 3) -- and imports nothing from
``web/server.py`` or any ``web/routes_*.py``/``*_html.py`` module; only
``web/control_channel.py``, a leaf module with no rendering code in it.

Entry point: ``privacyfence-companion`` (``pyproject.toml``'s
``[project.scripts]``) -- a second entry point of the same packaged
application (decision 4), not a new binary.

Platform behavior:
  - **macOS/Windows**: with no ``--action``, runs a persistent process with
    a tray/menu-bar icon (``pystray``) offering Open Approvals, Open
    Settings, New Recovery Code and Quit -- the whole product surface
    decision 2 describes.
    While running, it also runs a ``CompanionChannelServer`` so the
    daemon's own connector OAuth flows (``oauth_loopback.py``) can hand it
    a URL to open instead of calling ``webbrowser.open()`` themselves
    (decision 5) -- what Windows' Session 0 isolation will require once
    #428 Phase 4 lands there. Requires ``pystray``/``Pillow``, declared
    platform-conditionally in ``pyproject.toml``.
  - **Linux**: no tray (decision 4's dependency-budget call) -- ``main()``
    instead dispatches once on a single ``--action`` and exits, invoked by
    ``resources/linux/privacyfence-companion.desktop``'s main ``Exec=``
    (Open Approvals) and its ``Desktop Action`` entries (Settings, New
    Recovery Code, Quit).
    ``--serve`` is the third shape, added by #428 Phase 4 (B5b): the
    ``CompanionChannelServer`` alone, with no tray and no menu, so a
    separated install's daemon -- which now runs as its own account with no
    desktop session -- has something in the user's session to hand a
    connector OAuth URL to. Still zero new dependencies, which is what
    decision 4's Linux budget actually constrains; what it rules out is a
    tray icon, not a socket. On an unseparated Linux install nothing starts
    this and ``oauth_loopback.py``'s direct ``webbrowser.open()`` fallback
    keeps working exactly as before.

``--launch`` (ADR 0031) is what clicking PrivacyFence itself runs -- the
macOS app icon, whose bundle's main executable is a launcher rather than the
daemon, and the Windows Start Menu entry. It opens Approvals through a
running companion, or becomes the tray itself when none is running; see
``_launch()``.

Startup wiring (what autostarts the daemon vs. the companion, on each
platform) did not change in Phase 3 -- that inversion is #428 Phase 4's
job (ADR 0002's own "Consequences"), and has now happened on macOS (a
LaunchAgent running the tray) and Linux (an XDG autostart entry running
``--serve``). On an install that has not opted into privilege separation
the daemon still autostarts itself exactly as before, and running this
entry point is opt-in.

ADR 0003 decision 3 gives this process one job it did not have before, and
it is the reason that decision works at all: ``enable``'s machine half can
now separate an install with no human in sight -- an MDM push, an unattended
``apt`` upgrade, a ``.pkg`` run at the login window -- and record the group
membership as pending. This is the process that closes it. It is the only
PrivacyFence process that runs inside a real login session, as the person
whose membership is missing, so "nobody was logged in at install time" stops
meaning "this install is unprotected forever" and starts meaning "the
companion resolves this by existing". See ``_complete_pending_separation()``.

Phase 1 of the self-approval hardening plan leans on that same property
twice more, and both follow from "the only process that runs where a human
is". Neither renders any PrivacyFence content of its own -- decision 2 still
holds, and the module still imports nothing but ``web/control_channel.py``:

- **The first passkey** (item 1.2). A packaged install now defaults
  ``step_up.enabled``/``step_up.require_passkey`` on
  (``step_up_config.default_local_step_up()``), so a fresh one comes up
  requiring a passkey it does not have -- a fail-closed state in which
  nothing is approved, and one nobody would find on their own. This process
  asks the daemon whether that is the case at each start and, if it is,
  opens ``/security`` with a session already minted. See
  ``_offer_first_enrollment()``.
- **The one-time recovery code** (item 1.3). It is no longer handed to a
  browser on a packaged install; the daemon calls into this process's own
  channel to put it on the desktop. Re-presenting one means issuing a new
  one, since nothing keeps the plaintext, which is what the menu entry
  ``_show_recovery_code()`` backs does.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import shutil
import subprocess  # nosec B404  # fixed argv (notify-send) below, no shell
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Callable
from pathlib import Path

from . import daemon_status, privilege_separation, service_control
from .std_streams import ensure_std_streams
from .web.control_channel import (
    CompanionChannelServer,
    ControlChannelError,
    _dialog_for,
    _NoDialogAvailable,
    enrollment_state,
    mint_bootstrap_code,
    open_attested_url,
    read_base_url,
    request_quit,
    request_recovery_code,
    request_show,
    show_via_companion,
    SHOW_FAILED,
    SHOW_OPENED,
)

logger = logging.getLogger("privacyfence.companion")

# The tray/launcher's whole menu, one entry point per platform (ADR 0002
# decision 2) -- macOS/Windows express these as pystray MenuItems, Linux as
# the .desktop file's main Exec (open-approvals) plus two Desktop Actions
# (open-settings, quit).
ACTION_OPEN_APPROVALS = "open-approvals"
ACTION_OPEN_SETTINGS = "open-settings"
# Plan item 1.3: the only way a recovery code is ever shown a second time.
# It is a menu entry rather than anything on /security because the code is
# not the daemon's to hand a browser any more -- see _show_recovery_code().
ACTION_RECOVERY_CODE = "recovery-code"
# The local-mode-fixes plan's Phase 2 (companion-as-daemon-manager): the
# daemon-management surface ADR 0002's Amendment adds to the companion's
# menu -- see daemon_status.py/
# service_control.py for the split behind these, and _menu_model() below for
# how the macOS/Windows tray turns them into the dynamic status
# line/Start/Restart/Stop items this same set of actions backs.
ACTION_SERVICE_STATUS = "service-status"
ACTION_SERVICE_START = "service-start"
ACTION_SERVICE_RESTART = "service-restart"
ACTION_SERVICE_STOP = "service-stop"
ACTION_QUIT = "quit"
_ACTIONS = (
    ACTION_OPEN_APPROVALS, ACTION_OPEN_SETTINGS, ACTION_RECOVERY_CODE,
    ACTION_SERVICE_STATUS, ACTION_SERVICE_START, ACTION_SERVICE_RESTART, ACTION_SERVICE_STOP,
    ACTION_QUIT,
)

# How long _offer_first_enrollment() keeps looking for a daemon before
# giving up for this login session. A packaged install starts its daemon as
# a system service and this process from the user's own session, so the two
# race at login by design; ten seconds covers that without keeping a thread
# (or a human) waiting on an install where PrivacyFence simply is not
# running, which is an ordinary state this process already tolerates
# everywhere else.
_DAEMON_WAIT_ATTEMPTS = 5
_DAEMON_WAIT_SECONDS = 2.0

_TRAY_PLATFORMS = ("darwin", "win32")

_TRAY_ICON_PATH = Path(__file__).parent / "resources" / "icon_menubar.png"

# Set once this process is actually answering on the companion channel --
# which is what decides whether ``_open_path()`` can mint an attested link
# itself or has to hand the job to whichever process is (see its own
# docstring). A plain module-level Event rather than a parameter threaded
# through ``_run_action``: the tray's menu callbacks are invoked by pystray
# with nothing of ours in scope, and a flag set beside the ``start()`` that
# makes it true cannot fall out of step with it.
_channel_running = threading.Event()

# How often the tray/``--serve`` background poll re-checks
# ``daemon_status.probe()`` and, on the tray, redraws the menu/icon from it.
_STATUS_POLL_SECONDS = 5.0
# ADR 0002's Amendment: a stopped/failed daemon is notified about only once
# it has stayed that way for this long, so a daemon that is merely mid-
# restart (a few seconds of "stopped" between the old process exiting and
# the new one's control socket coming up) never earns one.
_NOTIFY_AFTER_SECONDS = 15.0
# ...and not at all in the minute right after this process itself started,
# since the daemon may simply still be starting up behind it (a login-time
# or post-upgrade race, the same one _offer_first_enrollment() already
# tolerates with its own retry loop).
_NOTIFY_SUPPRESS_AFTER_START_SECONDS = 60.0
_NOTIFY_STATES = frozenset({"stopped", "failed"})
_NOTIFY_TEXT = "PrivacyFence isn't running. Click the menu-bar icon to start it."


@dataclass
class _MenuEntry:
    """One row of the tray/menu-bar menu -- pystray-free on purpose (the
    local-mode-fixes plan's own testing note for this phase: "test the pure
    function that computes the menu model; do not drive pystray"), so
    ``_menu_model()`` below is
    directly unit-testable on every OS this repo's CI actually runs on,
    not only the two ``pystray`` is declared a dependency on."""

    label: str
    #: One of this module's ``ACTION_*`` constants, or None for the
    #: disabled status line, which has nothing to dispatch.
    action: str | None
    enabled: bool = True
    visible: bool = True


_STATUS_SYMBOL = {"running": "●", "starting": "●", "unresponsive": "⚠", "failed": "⚠"}
_STATUS_LABEL = {
    "running": "is running", "starting": "is starting", "stopped": "is not running",
    "failed": "has failed", "unresponsive": "is not responding", "unknown": "status is unknown",
}


def _status_line_text(status: "daemon_status.DaemonStatus") -> str:
    symbol = _STATUS_SYMBOL.get(status.state, "○")  # open circle: stopped/unknown
    version = f" (v{status.version})" if status.version else ""
    return f"{symbol} PrivacyFence {_STATUS_LABEL[status.state]}{version}"


def _menu_model(status: "daemon_status.DaemonStatus") -> list[_MenuEntry]:
    """The tray/menu-bar's whole content, as data -- what ``_run_tray()``
    turns into real ``pystray.MenuItem``s, and what a unit test can check
    without pystray at all. Layout matches ADR 0002's Amendment: a disabled
    status line, the two ADR 0002 decision-2 items unchanged, then exactly
    one of Start/Restart/Stop depending on whether the daemon looks up
    (running/starting/unresponsive all count -- an unresponsive daemon is
    not a stopped one, and Start would just collide with whatever is
    already listening), Service Details for the sentence behind the status
    line's symbol, then the unchanged recovery-code and quit items."""
    running = status.state in ("running", "starting", "unresponsive")
    return [
        _MenuEntry(_status_line_text(status), None, enabled=False),
        _MenuEntry("Open Approvals", ACTION_OPEN_APPROVALS),
        _MenuEntry("Open Settings", ACTION_OPEN_SETTINGS),
        _MenuEntry("Start PrivacyFence…", ACTION_SERVICE_START, visible=not running),
        _MenuEntry("Restart PrivacyFence…", ACTION_SERVICE_RESTART, visible=running),
        _MenuEntry("Stop PrivacyFence…", ACTION_SERVICE_STOP, visible=running),
        _MenuEntry("Service Details…", ACTION_SERVICE_STATUS),
        _MenuEntry("New Recovery Code…", ACTION_RECOVERY_CODE),
        # Renamed from "Quit" (ADR 0002's Amendment): on a privilege-
        # separated install this only ever quits the companion -- the
        # daemon's own QUIT refuses outright (#428 B4) -- and the old label
        # read as an offer this menu cannot make good on.
        _MenuEntry("Quit Companion", ACTION_QUIT),
    ]


@dataclass
class _NotificationState:
    """Carried across polls by the caller (the tray's poll thread, or
    ``--serve``'s) -- kept as an explicit, passed-in object rather than
    closure-captured module state so ``_notification_decision()`` below is
    a pure function a test can drive with fabricated timestamps."""

    bad_since: float | None = None
    notified_state: str | None = None


def _notification_decision(
    poll_state: _NotificationState, status: "daemon_status.DaemonStatus", *, now: float, started_at: float,
) -> bool:
    """Whether *this* poll should show the "PrivacyFence isn't running"
    notification -- mutates ``poll_state`` in place (tracking how long the
    current bad state has persisted, and which state was last notified
    about, so a stopped→failed→stopped flap notifies at most once per
    distinct transition) and returns the yes/no the caller acts on.

    Suppresses for ``_NOTIFY_SUPPRESS_AFTER_START_SECONDS`` after this
    companion process itself started, and again until the bad state has
    held for ``_NOTIFY_AFTER_SECONDS`` -- see the two constants' own
    comments for why each exists."""
    if status.state not in _NOTIFY_STATES:
        poll_state.bad_since = None
        poll_state.notified_state = None
        return False
    if now - started_at < _NOTIFY_SUPPRESS_AFTER_START_SECONDS:
        return False
    if poll_state.bad_since is None:
        poll_state.bad_since = now
        return False
    if now - poll_state.bad_since < _NOTIFY_AFTER_SECONDS:
        return False
    if poll_state.notified_state == status.state:
        return False
    poll_state.notified_state = status.state
    return True


def _open_path(path: str) -> bool:
    """Mint a fresh bootstrap code over the daemon's own control channel and
    open ``path`` in the user's default browser -- this process's whole
    product surface (ADR 0002 decision 2), and, since the self-approval
    plan's Phase 2, the only route to a session that may *approve* rather
    than merely view (web/session_auth.py's ``PROVENANCE_HUMAN``).

    What makes that session attestable is the daemon calling back to
    whichever process owns the companion channel, so which process this is
    decides how the link gets minted:

    1. **This one owns the channel** (the tray loop, or ``--serve``): mint
       it here, answering the daemon's own call-back from the accept loop.
    2. **Another companion owns it** -- Linux's one-shot ``--action``, spawned
       fresh by an applications-menu click while the autostarted ``--serve``
       process holds the address (ADR 0003 decision 5). Ask that process to
       do it (``SHOW``): it is the one the daemon can call back, and it
       opens the browser in the same session this click came from. That
       process asks the human to confirm first, and cannot be talked out of
       it: this line arrives from another process running as the same OS
       user, so it carries no evidence of a human on its own -- see
       ``control_channel._show_page``. On this platform that dialog is the
       click's own confirmation, which is the trade for having no tray icon
       to click instead (ADR 0002 decision 4).
    3. **Nobody owns it**: an unattested link, which still signs the human in
       to look at what is pending. Logged as the reduced thing it is, naming
       the companion, rather than silently handing back a session whose
       Approve buttons will refuse.

    Returns False (logging why) rather than raising: every caller here is a
    menu click or a one-shot launcher invocation, neither of which has
    anywhere useful to propagate an exception to."""
    if _channel_running.is_set():
        opened, reason = open_attested_url(path)
        if not opened:
            logger.error("Could not open %s: %s", path, reason)
        return opened
    if request_show(path):
        return True
    base_url = read_base_url()
    if base_url is None:
        logger.error("PrivacyFence does not appear to be running (local mode) -- nothing to open.")
        return False
    try:
        code = mint_bootstrap_code()
    except (OSError, ControlChannelError) as exc:
        logger.error("Could not reach PrivacyFence's control channel: %s", exc)
        return False
    logger.warning(
        "No PrivacyFence companion is running in this session, so this link can view what is "
        "pending but not approve it. Start PrivacyFence's companion (or run "
        "`privacyfence-app --print-sign-in-link`) for a link that can.",
    )
    return webbrowser.open(f"{base_url}{path}?bootstrap={code}")


def _quit_daemon() -> bool:
    """Ask the daemon to shut down -- the same ``allow_quit``-gated action
    the web settings page's own "Quit PrivacyFence" button takes
    (``settings_controller.py``'s ``quit_app()``), reached here without a
    browser."""
    try:
        request_quit()
    except (OSError, ControlChannelError) as exc:
        logger.error("Could not quit PrivacyFence: %s", exc)
        return False
    return True


def _show_recovery_code() -> bool:
    """Ask the daemon for a *replacement* one-time recovery code (plan item
    1.3). The code never comes back over this call -- the daemon puts it in
    front of the human by calling back into this process's own channel
    (``SHOW RECOVERY``), which is the whole point: nothing that merely
    speaks a socket ever reads one. What this returns is only whether the
    round trip succeeded.

    Issuing one invalidates whatever code was on file, so the daemon asks
    for confirmation on this same desktop first; a human who clicks Deny
    lands here as an ordinary False with the reason logged, same as every
    other failure this menu can hit.
    """
    try:
        request_recovery_code()
    except (OSError, ControlChannelError) as exc:
        logger.error("Could not get a recovery code from PrivacyFence: %s", exc)
        return False
    return True


def _show_message(text: str) -> bool:
    """Put ``text`` in front of whoever is at this login session, with no
    reply expected -- the companion's own local report of a service action's
    outcome, reusing the daemon's own per-platform "statement" dialog
    (``web/control_channel.py``'s ``_dialog_for("message")``) rather than a
    second implementation of the same three dialog primitives.

    False (logging why) when this desktop has neither zenity nor kdialog
    (Linux only -- ADR 0002 decision 4's Linux budget), same posture as
    every other companion action that reports rather than raises."""
    try:
        return _dialog_for("message")(text, timeout=15.0)
    except _NoDialogAvailable as exc:
        logger.warning("Could not show %r: %s", text, exc)
        return False


def _show_service_status() -> bool:
    """The ``Service Details…``/``service-status`` action: probe the daemon
    (``daemon_status.probe()``, this plan's Phase 2) and put its one-sentence
    ``detail`` in front of the human -- the tray already shows the same
    state at a glance, so this is for whoever wants the sentence behind the
    symbol, and the whole of what Linux's one-shot ``ServiceStatus`` Desktop
    Action has."""
    status = daemon_status.probe()
    return _show_message(status.detail)


def _run_service_action(action: "service_control.DaemonAction") -> bool:
    """Start/Restart/Stop, elevated (``service_control.run_elevated()``,
    this plan's Phase 2). Reports the outcome the same way ``_show_service_status``
    does -- except a declined password prompt (``detail == "cancelled"``)
    says nothing further, the same restraint ``service_control.py``'s own
    docstring asks for: a human who just clicked Cancel does not need a
    dialog telling them so."""
    ok, detail = service_control.run_elevated(action)
    if detail != "cancelled":
        _show_message(detail)
    return ok


def _run_action(action: str) -> bool:
    if action == ACTION_OPEN_APPROVALS:
        return _open_path("/approvals")
    if action == ACTION_OPEN_SETTINGS:
        return _open_path("/settings")
    if action == ACTION_RECOVERY_CODE:
        return _show_recovery_code()
    if action == ACTION_SERVICE_STATUS:
        return _show_service_status()
    if action == ACTION_SERVICE_START:
        return _run_service_action("start")
    if action == ACTION_SERVICE_RESTART:
        return _run_service_action("restart")
    if action == ACTION_SERVICE_STOP:
        return _run_service_action("stop")
    if action == ACTION_QUIT:
        return _quit_daemon()
    raise ValueError(f"Unknown companion action: {action!r}")  # pragma: no cover -- argparse restricts choices


def _launch() -> int:
    """``--launch``: what clicking PrivacyFence itself does -- the macOS app
    icon (the bundle's main executable, ``src/_launcher_entry.py``) and the
    Windows Start Menu entry (ADR 0031). Opens Approvals, making sure a
    companion is running to do it:

    1. **A companion is running**: ask it to open Approvals (``SHOW``), the
       same route Linux's applications-menu click takes. It asks the human
       to confirm first -- see ``control_channel._show_page`` for why that
       dialog cannot be skipped for a request arriving from another process.
    2. **It answered but did not open it** (Deny, a dialog nobody answered,
       a daemon it could not reach): stop there. Starting a second companion
       beside one that is plainly running would only fight it for its
       address.
    3. **No companion is running**: on macOS/Windows, *become* it -- run the
       tray here, and open Approvals from it once its channel is up. That
       needs no dialog: this process owns the channel the daemon calls back,
       exactly as a click on the tray's own Open Approvals does. On Linux,
       which has no tray (ADR 0002 decision 4), this is ``--action
       open-approvals``'s own fallback.

    Returns the process exit code: the tray's own when this became it."""
    outcome = show_via_companion("/approvals")
    if outcome == SHOW_OPENED:
        return 0
    if outcome == SHOW_FAILED:
        logger.error(
            "PrivacyFence's companion is running but did not open Approvals (the request was "
            "declined, or it could not reach PrivacyFence). Use its menu-bar/tray icon instead.",
        )
        return 1
    if sys.platform in _TRAY_PLATFORMS:
        return _run_tray(initial_path="/approvals")
    return 0 if _open_path("/approvals") else 1


def _complete_pending_separation() -> None:
    """ADR 0003 decision 3's second half: if this install is separated but
    this session's account is not in its service group yet, ask for the
    password once and close that.

    A no-op -- prompting nobody -- on an unseparated install and on one that
    is already complete, which is the ordinary case at every companion start
    after the first. ``privilege_separation`` itself logs every way this can
    fail to run, each time naming the one command that does it by hand; what
    is left here is the step no command can take, which is the human logging
    out and back in.

    Called once per companion process, and a companion process is one per
    login session, which is the granularity decision 3 actually wants: group
    membership is evaluated when a session is created, so a second attempt
    inside the same session could not observe its own result anyway.

    ADR 0008 ("D2: two identities, not one, per install") retired the
    local-mode-fixes plan's Phase 2 §2.6 interim guard: through that
    guard, an account that was not this install's recorded owner got a
    notification instead of a join, because completing it would have
    silently handed them the owner's own principal. Now that a second
    account gets its own isolated ``os-<uid>``/``os-<sid>`` principal
    instead, there is nothing left to warn about -- any pending service-
    group member is onboarded exactly like the owner always was.
    """
    # Read once, up front: the elevated command below rewrites the marker and
    # drops the cache, so asking again afterwards could answer None on an
    # install somebody ran `disable` against in between -- and the group to
    # name in the message is the one this decision was pending on.
    state = privilege_separation.separation()
    if state is None:
        return
    if not privilege_separation.owner_membership_pending():
        return
    if not privilege_separation.complete_per_user_separation():
        return
    logger.warning(
        "Added %s to the %s group. Group membership is evaluated when a session is created, "
        "so log out and back in before the approvals page or your MCP client can reach "
        "PrivacyFence.",
        privilege_separation.current_user_name(), state.service_group,
    )


def _offer_first_enrollment() -> None:
    """Plan item 1.2: if this install requires a passkey and has none
    enrolled, open ``/security`` with a freshly minted session so the human
    can add one now.

    This is the other half of defaulting step-up on for packaged installs
    (``step_up_config.default_local_step_up()``). A fresh install comes up
    requiring a passkey it does not have yet, which is a deliberately
    fail-closed state -- every approval is refused, and every page carries a
    banner saying why -- but it is only a *safe* state, not a usable one,
    and nobody is looking at a page they have no reason to open. This
    process is the one thing PrivacyFence runs inside a login session, so it
    is the one thing that can put that page in front of somebody. From
    there, the existing flow takes over: the first-enrollment gate asks this
    same companion to confirm, and the recovery code comes back through it
    too (item 1.3).

    Deliberately re-offered at every companion start until a passkey exists,
    rather than once and never again: the state it is reacting to is one
    where PrivacyFence approves nothing at all, so it is worth a tab each
    login until it is fixed. Once one is enrolled the daemon answers ``ok``
    and this does nothing, which is every start after the first.

    A daemon that is not (yet) running is an ordinary answer, not an error
    -- this races the service's own start at login, so it is retried for
    ``_DAEMON_WAIT_ATTEMPTS`` before being left for the next login.
    """
    for attempt in range(_DAEMON_WAIT_ATTEMPTS):
        try:
            state = enrollment_state()
        except OSError:
            if attempt + 1 < _DAEMON_WAIT_ATTEMPTS:
                time.sleep(_DAEMON_WAIT_SECONDS)
                continue
            logger.info("PrivacyFence is not running -- not checking whether a passkey is needed.")
            return
        except ControlChannelError as exc:
            # An older daemon (or one with no step-up config behind its
            # control channel) answers ERROR here. Nothing to do about it
            # from this side, and nothing worth a dialog over.
            logger.info("PrivacyFence did not answer the passkey-enrollment query: %s", exc)
            return
        if state != "pending":
            return
        logger.warning(
            "PrivacyFence requires a passkey and none is enrolled, so no approval can be "
            "released. Opening the security page to add one.",
        )
        _open_path("/security")
        return


def _first_run_checks() -> None:
    """The startup thread's own body: settle privilege separation first,
    then offer enrollment.

    In that order, and not merely for tidiness. Closing the pending per-user
    half (ADR 0003 decision 3) adds this account to the service group, and
    group membership is only evaluated when a session is created -- so until
    the human logs out and back in, they cannot reach the daemon's web UI at
    all, and opening ``/security`` for them would be opening a page that
    cannot load. ``owner_membership_pending()`` is still true in exactly
    that window, which is what this checks before going on.
    """
    _complete_pending_separation()
    if privilege_separation.owner_membership_pending():
        return
    _offer_first_enrollment()


def _start_pending_separation_check() -> None:
    """Runs ``_first_run_checks()`` off the startup path.

    Its own thread for the same reason ``maybe_auto_enable_macos()`` uses
    one: what it may do is put a system password dialog in front of a human,
    and the tray icon appearing (or the companion channel binding) must not
    wait on somebody answering, or ignoring, that dialog. Item 1.2's
    enrollment offer inherits the same thread and the same reasoning -- it
    may sit waiting for a daemon that is still starting.
    """
    threading.Thread(
        target=_first_run_checks,
        name="privacyfence-first-run",
        daemon=True,
    ).start()


def _status_icon_image(base_image, state: str):  # noqa: ANN001, ANN201 -- a PIL Image, no type stub imported at module scope
    """The tray icon for ``state`` -- ``base_image`` unchanged while the
    daemon looks up (running/starting), greyscale otherwise (ADR 0002's
    Amendment: "swap in a grey ... icon variant when the daemon is not
    running"). Computed from ``base_image`` fresh each call rather than
    from whatever the icon currently shows, so repeated polls in the same
    state never compound (grey-of-grey is still grey, but there is no
    reason to rely on that).

    A programmatic grayscale rather than a second shipped PNG: this repo
    has no way to hand-author a second icon asset, and ``Pillow`` is
    already a hard dependency of this entry point -- ``ImageOps.
    grayscale()`` costs nothing new to carry."""
    if state in ("running", "starting"):
        return base_image
    from PIL import ImageOps

    return ImageOps.grayscale(base_image).convert(base_image.mode)


def _run_status_poll_once(
    *, notify_state: _NotificationState, started_at: float,
    on_status: Callable[["daemon_status.DaemonStatus"], None] | None = None,
    on_notify: Callable[[], None] | None = None,
) -> "daemon_status.DaemonStatus":
    """One tick of the background status poll -- the pure-enough core
    ``_run_tray()``'s icon-updating loop and ``_run_serve()``'s
    notify-send-only loop both share, factored out so it is directly
    unit-testable rather than only reachable by letting a real background
    thread run. Probes the daemon once, hands the fresh status to
    ``on_status`` (the tray's own icon/menu redraw; ``--serve`` has none,
    so it passes None), and runs ``on_notify`` exactly when
    ``_notification_decision()`` says this tick should notify."""
    status = daemon_status.probe()
    if on_status is not None:
        on_status(status)
    if on_notify is not None and _notification_decision(
        notify_state, status, now=time.monotonic(), started_at=started_at,
    ):
        on_notify()
    return status


def _on_ui_thread(fn: Callable[[], None]) -> None:
    """Run ``fn`` where the tray may be touched. pystray's macOS backend calls
    AppKit directly from whichever thread asks, and AppKit kills the process
    (SIGTRAP) when an ``NSStatusItem``/``NSMenu`` is changed off the main
    thread -- so there it is queued onto the main run loop ``icon.run()``
    drives. Windows' backend has no such rule."""
    if sys.platform != "darwin":
        fn()
        return
    from Foundation import NSOperationQueue

    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


def _run_tray(initial_path: str | None = None) -> int:
    """macOS/Windows only -- a persistent process with a tray/menu-bar icon
    and this process's own ``CompanionChannelServer`` (decision 5), both
    torn down together on Quit. ``pystray``/``Pillow`` are imported here,
    not at module scope, so importing this module (e.g. from a unit test)
    never requires them on a platform where they aren't even declared as a
    dependency (``pyproject.toml``).

    The local-mode-fixes plan's Phase 2 (ADR 0002's Amendment) adds a live
    status line and Start/Restart/Stop/Service Details items, backed by ``_menu_model()``
    above: the ``pystray.MenuItem``s built below never change identity or
    order once created -- only their ``text``/``enabled``/``visible``,
    each a small callable reading ``_menu_model(_state.status)[index]``, so
    a background poll thread can redraw the whole menu (``icon.
    update_menu()``) without rebuilding it.

    ``initial_path`` (``--launch``, ADR 0031) is opened once the channel is
    up, as if its menu item had been clicked -- on its own thread, since
    minting waits on the daemon calling this process back.
    """
    import pystray
    from PIL import Image

    _start_pending_separation_check()
    channel = CompanionChannelServer()
    channel.start()
    _channel_running.set()
    if initial_path is not None:
        threading.Thread(
            target=_open_path, args=(initial_path,), name="privacyfence-launch-open", daemon=True,
        ).start()

    base_image = Image.open(_TRAY_ICON_PATH)

    class _State:
        status = daemon_status.probe()
        notify = _NotificationState()

    _state = _State()
    started_at = time.monotonic()

    def _model() -> list[_MenuEntry]:
        return _menu_model(_state.status)

    def _on_open_approvals(_icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
        _open_path("/approvals")

    def _on_open_settings(_icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
        _open_path("/settings")

    def _on_quit(icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
        # Stops the daemon first (same as Linux's "quit" Desktop Action) --
        # a tray icon with nothing left to serve has no reason to stay up
        # either, so this process exits right behind it.
        _quit_daemon()
        _channel_running.clear()
        channel.stop()
        icon.stop()

    def _on_recovery_code(_icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
        # Its own thread: the round trip is two dialogs long (confirm, then
        # show), and pystray runs menu callbacks on the thread that also
        # draws the menu -- doing this inline would freeze the tray icon for
        # as long as somebody takes to answer.
        threading.Thread(
            target=_show_recovery_code, name="privacyfence-recovery-code", daemon=True,
        ).start()

    def _on_service_status(_icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
        threading.Thread(
            target=_show_service_status, name="privacyfence-service-status", daemon=True,
        ).start()

    def _make_service_handler(action: "service_control.DaemonAction"):
        def _handler(_icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
            # Its own thread for the same reason recovery-code's is: an
            # elevation prompt plus the platform script's own retry loop can
            # take well past what should ever block the menu from drawing.
            threading.Thread(
                target=_run_service_action, args=(action,),
                name=f"privacyfence-service-{action}", daemon=True,
            ).start()

        return _handler

    _handlers: dict[str, Callable[["pystray.Icon", "pystray.MenuItem"], None]] = {
        ACTION_OPEN_APPROVALS: _on_open_approvals,
        ACTION_OPEN_SETTINGS: _on_open_settings,
        ACTION_SERVICE_START: _make_service_handler("start"),
        ACTION_SERVICE_RESTART: _make_service_handler("restart"),
        ACTION_SERVICE_STOP: _make_service_handler("stop"),
        ACTION_SERVICE_STATUS: _on_service_status,
        ACTION_RECOVERY_CODE: _on_recovery_code,
        ACTION_QUIT: _on_quit,
    }

    items = []
    for index, entry in enumerate(_model()):
        if entry.action is None:
            items.append(pystray.MenuItem(lambda _item, i=index: _model()[i].label, None, enabled=False))
        else:
            items.append(pystray.MenuItem(
                entry.label, _handlers[entry.action],
                enabled=lambda _item, i=index: _model()[i].enabled,
                visible=lambda _item, i=index: _model()[i].visible,
            ))
    menu = pystray.Menu(*items)
    icon = pystray.Icon(
        "privacyfence", _status_icon_image(base_image, _state.status.state), "PrivacyFence", menu,
    )

    poll_stop = threading.Event()

    def _redraw(status: "daemon_status.DaemonStatus") -> None:
        def _apply() -> None:
            _state.status = status
            icon.icon = _status_icon_image(base_image, status.state)
            icon.update_menu()

        _on_ui_thread(_apply)

    def _notify() -> None:
        _on_ui_thread(lambda: icon.notify(_NOTIFY_TEXT))

    def _poll_loop() -> None:
        while not poll_stop.wait(_STATUS_POLL_SECONDS):
            _run_status_poll_once(
                notify_state=_state.notify, started_at=started_at, on_status=_redraw, on_notify=_notify,
            )

    threading.Thread(target=_poll_loop, name="privacyfence-status-poll", daemon=True).start()

    try:
        icon.run()
    finally:
        poll_stop.set()
        _channel_running.clear()
        channel.stop()
    return 0


def _notify_send(text: str) -> None:
    """``--serve``'s own counterpart to the tray's ``icon.notify()`` (this
    plan's Phase 2): ``notify-send``, if this desktop has it. Silently a no-op
    otherwise -- ``notify-send`` is not a PrivacyFence dependency any more
    than zenity/kdialog are (ADR 0002 decision 4's Linux budget), and a
    background poll finding no notifier installed is not worth a log line
    on every single poll."""
    notify_send = shutil.which("notify-send")
    if notify_send is None:
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(  # nosec B603  # fixed argv, no shell; `text` is this module's own constant
            [notify_send, "PrivacyFence", text], capture_output=True, timeout=5, check=False,
        )


def _run_serve(wait: Callable[[], None] | None = None) -> int:
    """``--serve``: the companion channel, plus (this plan's Phase 2) a background
    status poll, until killed.

    #428 Phase 4 (B5b). A separated install's daemon runs under its own
    account with no desktop session, so ``oauth_loopback.py``'s
    ``webbrowser.open()`` has no browser to reach -- the same Session 0
    problem ADR 0002 decision 5 anticipates for Windows, arriving on Linux
    first because that is where Phase 4 landed second and where there is no
    tray process already running a ``CompanionChannelServer``. This is that
    server on its own: no ``pystray``, no icon, no menu, no imports beyond
    what the one-shot ``--action`` path already pulls in.

    The poll thread is this platform's only way to *notice* a stopped/
    failed daemon on its own (there is no tray icon to glance at) -- it
    reuses the exact same ``_notification_decision()`` rule the tray's poll
    does, just sent through ``notify-send`` instead of ``icon.notify()``.

    Blocks on an Event nothing ever sets rather than a sleep loop: SIGTERM's
    default disposition kills the process outright, which is how the XDG
    autostart entry's session teardown ends this, so the only way ``wait``
    returns in a real run is ``KeyboardInterrupt`` from a foreground
    terminal. ``wait`` is injectable for the test that has to get back out
    of here.
    """
    _start_pending_separation_check()
    channel = CompanionChannelServer()
    channel.start()
    if channel.address is None:
        logger.error("Could not start the companion's control channel -- nothing to serve.")
        return 1
    _channel_running.set()
    logger.info("Companion channel listening on %s", channel.address)

    notify_state = _NotificationState()
    started_at = time.monotonic()
    poll_stop = threading.Event()

    def _poll_loop() -> None:
        while not poll_stop.wait(_STATUS_POLL_SECONDS):
            _run_status_poll_once(
                notify_state=notify_state, started_at=started_at,
                on_notify=lambda: _notify_send(_NOTIFY_TEXT),
            )

    threading.Thread(target=_poll_loop, name="privacyfence-status-poll", daemon=True).start()

    try:
        (wait or threading.Event().wait)()
    except KeyboardInterrupt:  # pragma: no cover -- interactive only
        pass
    finally:
        poll_stop.set()
        # Tears the socket down cleanly rather than leaving a stale node
        # behind for the next start to unlink.
        _channel_running.clear()
        channel.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Same fix-up the frozen daemon entry point (src/_daemon_entry.py)
    # needs: a windowed (console=False) frozen build has no
    # sys.stdout/sys.stderr at all, and logging.basicConfig() below would
    # crash probing them the same way uvicorn once did -- see
    # std_streams.py's own module docstring for the full history.
    ensure_std_streams()
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(prog="privacyfence-companion")
    parser.add_argument(
        "--action", choices=_ACTIONS, default=None,
        help=(
            "Run one action and exit, instead of the persistent tray/menu-bar loop. "
            "On Linux, which has no tray (ADR 0002 decision 4), either this or --serve "
            "is required -- see resources/linux/privacyfence-companion.desktop's "
            "Exec/Desktop Action lines."
        ),
    )
    parser.add_argument(
        "--serve", action="store_true",
        help=(
            "Run only the companion's control channel (no tray, no menu) and stay up, so a "
            "privilege-separated daemon can hand this session a connector OAuth URL to open. "
            "What the XDG autostart entry a separated Linux install writes runs -- see "
            "scripts/linux_privilege_separation.sh."
        ),
    )
    parser.add_argument(
        "--launch", action="store_true",
        help=(
            "Open Approvals, starting the tray/menu-bar companion first if none is running -- "
            "what clicking PrivacyFence itself runs (the macOS app icon, the Windows Start Menu "
            "entry; ADR 0031)."
        ),
    )
    args = parser.parse_args(argv)

    if sum((args.action is not None, args.serve, args.launch)) > 1:
        # One runs and exits, another stays up forever; there is no
        # sensible order for "both", and silently picking one would make a
        # mis-written .desktop Exec= look like it worked.
        parser.error("--action, --serve and --launch are mutually exclusive")

    if args.launch:
        return _launch()

    if args.action is not None:
        return 0 if _run_action(args.action) else 1

    if args.serve:
        return _run_serve()

    if sys.platform not in _TRAY_PLATFORMS:
        parser.error(
            "--action or --serve is required on this platform (no tray icon -- see ADR 0002 decision 4)"
        )
    return _run_tray()


if __name__ == "__main__":
    sys.exit(main())
