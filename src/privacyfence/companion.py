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
    Settings, and Quit -- the whole product surface decision 2 describes.
    While running, it also runs a ``CompanionChannelServer`` so the
    daemon's own connector OAuth flows (``oauth_loopback.py``) can hand it
    a URL to open instead of calling ``webbrowser.open()`` themselves
    (decision 5) -- what Windows' Session 0 isolation will require once
    #428 Phase 4 lands there. Requires ``pystray``/``Pillow``, declared
    platform-conditionally in ``pyproject.toml``.
  - **Linux**: no tray (decision 4's dependency-budget call, and no
    persistent process at all) -- ``main()`` instead dispatches once on a
    single ``--action`` and exits, invoked by
    ``resources/linux/privacyfence.desktop``'s main ``Exec=`` (Open
    Approvals) and its ``Desktop Action`` entries (Settings, Quit). Linux
    connector OAuth flows keep using ``oauth_loopback.py``'s own direct
    ``webbrowser.open()`` fallback -- there's no persistent companion here
    for the daemon to ask.

Startup wiring (what autostarts the daemon vs. the companion, on each
platform) does not change in Phase 3 -- that inversion is #428 Phase 4's
job (ADR 0002's own "Consequences"). Today the daemon still autostarts
itself exactly as before; running this entry point is opt-in.
"""
from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from pathlib import Path

from .std_streams import ensure_std_streams
from .web.control_channel import (
    CompanionChannelServer,
    ControlChannelError,
    mint_bootstrap_code,
    read_base_url,
    request_quit,
)

logger = logging.getLogger("privacyfence.companion")

# The tray/launcher's whole menu, one entry point per platform (ADR 0002
# decision 2) -- macOS/Windows express these as pystray MenuItems, Linux as
# the .desktop file's main Exec (open-approvals) plus two Desktop Actions
# (open-settings, quit).
ACTION_OPEN_APPROVALS = "open-approvals"
ACTION_OPEN_SETTINGS = "open-settings"
ACTION_QUIT = "quit"
_ACTIONS = (ACTION_OPEN_APPROVALS, ACTION_OPEN_SETTINGS, ACTION_QUIT)

_TRAY_PLATFORMS = ("darwin", "win32")

_TRAY_ICON_PATH = Path(__file__).parent / "resources" / "icon_menubar.png"


def _open_path(path: str) -> bool:
    """Mint a fresh bootstrap code over the daemon's own control channel
    and open ``path`` in the user's default browser -- the exact link
    ``web/server.py``'s own ``mint_bootstrap_url()`` would have produced,
    minted the same way daemon_main.py's startup log line is, just from
    here instead of that log line or ``privacyfence_get_sign_in_link``.
    Returns False (logging why) rather than raising: every caller here is a
    menu click or a one-shot launcher invocation, neither of which has
    anywhere useful to propagate an exception to."""
    base_url = read_base_url()
    if base_url is None:
        logger.error("PrivacyFence does not appear to be running (local mode) -- nothing to open.")
        return False
    try:
        code = mint_bootstrap_code()
    except (OSError, ControlChannelError) as exc:
        logger.error("Could not reach PrivacyFence's control channel: %s", exc)
        return False
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


def _run_action(action: str) -> bool:
    if action == ACTION_OPEN_APPROVALS:
        return _open_path("/approvals")
    if action == ACTION_OPEN_SETTINGS:
        return _open_path("/settings")
    if action == ACTION_QUIT:
        return _quit_daemon()
    raise ValueError(f"Unknown companion action: {action!r}")  # pragma: no cover -- argparse restricts choices


def _run_tray() -> int:
    """macOS/Windows only -- a persistent process with a tray/menu-bar icon
    and this process's own ``CompanionChannelServer`` (decision 5), both
    torn down together on Quit. ``pystray``/``Pillow`` are imported here,
    not at module scope, so importing this module (e.g. from a unit test)
    never requires them on a platform where they aren't even declared as a
    dependency (``pyproject.toml``)."""
    import pystray
    from PIL import Image

    channel = CompanionChannelServer()
    channel.start()

    def _on_open_approvals(_icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
        _open_path("/approvals")

    def _on_open_settings(_icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
        _open_path("/settings")

    def _on_quit(icon: "pystray.Icon", _item: "pystray.MenuItem") -> None:
        # Stops the daemon first (same as Linux's "quit" Desktop Action) --
        # a tray icon with nothing left to serve has no reason to stay up
        # either, so this process exits right behind it.
        _quit_daemon()
        channel.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Approvals", _on_open_approvals),
        pystray.MenuItem("Open Settings", _on_open_settings),
        pystray.MenuItem("Quit", _on_quit),
    )
    icon = pystray.Icon("privacyfence", Image.open(_TRAY_ICON_PATH), "PrivacyFence", menu)
    try:
        icon.run()
    finally:
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
            "Required on Linux, which has no tray (ADR 0002 decision 4) -- see "
            "resources/linux/privacyfence.desktop's Exec/Desktop Action lines."
        ),
    )
    args = parser.parse_args(argv)

    if args.action is not None:
        return 0 if _run_action(args.action) else 1

    if sys.platform not in _TRAY_PLATFORMS:
        parser.error("--action is required on this platform (no tray icon -- see ADR 0002 decision 4)")
    return _run_tray()


if __name__ == "__main__":
    sys.exit(main())
