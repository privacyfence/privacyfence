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
import logging
import sys
import threading
import time
import webbrowser
from typing import Callable
from pathlib import Path

from . import privilege_separation
from .std_streams import ensure_std_streams
from .web.control_channel import (
    CompanionChannelServer,
    ControlChannelError,
    enrollment_state,
    mint_bootstrap_code,
    open_attested_url,
    read_base_url,
    request_quit,
    request_recovery_code,
    request_show,
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
ACTION_QUIT = "quit"
_ACTIONS = (ACTION_OPEN_APPROVALS, ACTION_OPEN_SETTINGS, ACTION_RECOVERY_CODE, ACTION_QUIT)

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


def _run_action(action: str) -> bool:
    if action == ACTION_OPEN_APPROVALS:
        return _open_path("/approvals")
    if action == ACTION_OPEN_SETTINGS:
        return _open_path("/settings")
    if action == ACTION_RECOVERY_CODE:
        return _show_recovery_code()
    if action == ACTION_QUIT:
        return _quit_daemon()
    raise ValueError(f"Unknown companion action: {action!r}")  # pragma: no cover -- argparse restricts choices


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
    """
    # Read once, up front: the elevated command below rewrites the marker and
    # drops the cache, so asking again afterwards could answer None on an
    # install somebody ran `disable` against in between -- and the group to
    # name in the message is the one this decision was pending on.
    state = privilege_separation.separation()
    if state is None or not privilege_separation.owner_membership_pending():
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


def _run_tray() -> int:
    """macOS/Windows only -- a persistent process with a tray/menu-bar icon
    and this process's own ``CompanionChannelServer`` (decision 5), both
    torn down together on Quit. ``pystray``/``Pillow`` are imported here,
    not at module scope, so importing this module (e.g. from a unit test)
    never requires them on a platform where they aren't even declared as a
    dependency (``pyproject.toml``)."""
    import pystray
    from PIL import Image

    _start_pending_separation_check()
    channel = CompanionChannelServer()
    channel.start()
    _channel_running.set()

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

    menu = pystray.Menu(
        pystray.MenuItem("Open Approvals", _on_open_approvals),
        pystray.MenuItem("Open Settings", _on_open_settings),
        pystray.MenuItem("New Recovery Code\u2026", _on_recovery_code),
        pystray.MenuItem("Quit", _on_quit),
    )
    icon = pystray.Icon("privacyfence", Image.open(_TRAY_ICON_PATH), "PrivacyFence", menu)
    try:
        icon.run()
    finally:
        _channel_running.clear()
        channel.stop()
    return 0


def _run_serve(wait: Callable[[], None] | None = None) -> int:
    """``--serve``: the companion channel and nothing else, until killed.

    #428 Phase 4 (B5b). A separated install's daemon runs under its own
    account with no desktop session, so ``oauth_loopback.py``'s
    ``webbrowser.open()`` has no browser to reach -- the same Session 0
    problem ADR 0002 decision 5 anticipates for Windows, arriving on Linux
    first because that is where Phase 4 landed second and where there is no
    tray process already running a ``CompanionChannelServer``. This is that
    server on its own: no ``pystray``, no icon, no menu, no imports beyond
    what the one-shot ``--action`` path already pulls in.

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
    try:
        (wait or threading.Event().wait)()
    except KeyboardInterrupt:  # pragma: no cover -- interactive only
        pass
    finally:
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
    args = parser.parse_args(argv)

    if args.action is not None and args.serve:
        # One runs and exits, the other stays up forever; there is no
        # sensible order for "both", and silently picking one would make a
        # mis-written .desktop Exec= look like it worked.
        parser.error("--action and --serve are mutually exclusive")

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
