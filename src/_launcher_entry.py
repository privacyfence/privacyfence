import sys

# The macOS bundle's main executable (ADR 0031): what a double-click on
# PrivacyFenceApp.app in /Applications runs. Not the daemon -- that runs as
# a service under its own account and is started by launchd from its own
# explicit path (Contents/MacOS/PrivacyFenceApp) -- but the companion's
# --launch: open Approvals, starting the menu-bar companion first if none is
# running. See companion.py's _launch().
#
# The command line is fixed rather than parsed: LaunchServices decides what
# argv a Finder launch carries, and none of it is ours to interpret.
from privacyfence.std_streams import ensure_std_streams

ensure_std_streams()

from privacyfence.companion import main  # noqa: E402 -- must follow the fix-up above
sys.exit(main(["--launch"]))
