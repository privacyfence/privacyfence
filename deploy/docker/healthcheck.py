"""Docker HEALTHCHECK for the organization image.

The probe docs/org-mode-setup-guide.md's Monitoring section names, made from inside the container
to the port the installed bundle sets (``server.port``, default 8765). ``127.0.0.1`` is always in
the daemon's Host allowlist.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request
from pathlib import Path

BUNDLE = Path.home() / ".privacyfence" / "org" / "org_config.json"
PROBE_PATH = "/.well-known/oauth-authorization-server"


def main() -> int:
    try:
        server = json.loads(BUNDLE.read_text(encoding="utf-8")).get("server") or {}
    except (OSError, ValueError):
        return 1
    port = int(server.get("port", 8765))
    tls = server.get("tls") or {}
    if tls.get("cert_file"):
        # The certificate names the public hostname, not 127.0.0.1; this checks liveness only.
        url, context = f"https://127.0.0.1:{port}{PROBE_PATH}", ssl._create_unverified_context()
    else:
        url, context = f"http://127.0.0.1:{port}{PROBE_PATH}", None
    try:
        with urllib.request.urlopen(url, timeout=5, context=context) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
