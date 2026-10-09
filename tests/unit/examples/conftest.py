"""Put the SDK sources and the ``today`` example on the import path.

The example is built only on the SDK and the SDK is not installed alongside the daemon, so tests
import both from the source tree, the way the build script's ``--paths`` does for PyInstaller.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

for path in (REPO_ROOT / "plugin-sdk" / "src", REPO_ROOT / "examples" / "plugins" / "today"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
