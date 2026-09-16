import sys

# Same fix-up as src/_daemon_entry.py, and for the same reason: a windowed
# (console=False) Windows build has no sys.stdout/sys.stderr at all, and a
# library that probes them while being imported -- logging.basicConfig(),
# here -- would fail the same way uvicorn once did in the daemon. See
# privacyfence/std_streams.py's own module docstring for the full history.
from privacyfence.std_streams import ensure_std_streams

ensure_std_streams()

from privacyfence.companion import main  # noqa: E402 -- must follow the fix-up above
sys.exit(main())
