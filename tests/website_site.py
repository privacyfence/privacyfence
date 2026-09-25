"""Test-only helpers for the browser tests of privacyfence.eu's hand-written pages.

`stage_site()` builds the site the way `.github/workflows/pages.yml` does, by replaying that
workflow's own `cp` lines into a temporary directory, rather than serving `website/` as it
sits in the repository. The deployed site is assembled from more than `website/` (the icon and
the screenshots come from elsewhere in the repo), so serving the source tree would test pages
with missing images. Replaying the workflow's copy step also means a file that
pages.yml forgets to deploy is missing here too.

Used by tests/integration/test_website_layout.py (guardrail 12) and
tests/integration/test_website_consent.py (guardrail 13).
"""

from __future__ import annotations

import http.server
import os
import shlex
import shutil
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEBSITE = REPO / "website"
PAGES_WORKFLOW = REPO / ".github" / "workflows" / "pages.yml"

API_ORIGIN = "https://downloads.privacyfence.eu"
GA_ORIGIN = "https://www.googletagmanager.com"


def page_paths() -> list[str]:
    """URL path of every hand-written page: `/`, `/download/`, `/privacy/`, ..."""
    paths = []
    for index in sorted(WEBSITE.rglob("index.html")):
        rel = index.parent.relative_to(WEBSITE).as_posix()
        paths.append("/" if rel == "." else f"/{rel}/")
    return paths


def stage_site(dest: Path) -> Path:
    """Replays pages.yml's `cp` lines with `_site` pointed at `dest`. Returns `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    for raw in PAGES_WORKFLOW.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("mkdir -p "):
            for target in shlex.split(line)[2:]:
                (dest / Path(target).relative_to("_site")).mkdir(parents=True, exist_ok=True)
        elif line.startswith("cp "):
            *sources, target = shlex.split(line)[1:]
            target_path = dest / Path(target.rstrip("/")).relative_to("_site")
            for source in sources:
                if target.endswith("/"):
                    shutil.copy(REPO / source, target_path / Path(source).name)
                else:
                    shutil.copy(REPO / source, target_path)
    return dest


@contextmanager
def serve(directory: Path) -> Iterator[str]:
    """Serves `directory` over a real socket (`file://` changes how relative fetches and the
    page's own origin behave) and yields its base URL, without a trailing slash."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *args):  # noqa: A003 -- silence per-request logging
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def chromium_launch_kwargs() -> dict:
    """Playwright's bundled Chromium by default. `PRIVACYFENCE_TEST_CHROMIUM` points at another
    build, for a machine whose preinstalled Chromium doesn't match the playwright package's
    expected revision (the same escape hatch scripts/qa_web_smoke.py's --chromium-path gives)."""
    path = os.environ.get("PRIVACYFENCE_TEST_CHROMIUM")
    return {"executable_path": path} if path else {}


STABLE_MANIFEST = {
    "schema": 1,
    "version": "4.5.0",
    "channel": "stable",
    "published_at": "2026-09-25T12:00:00Z",
    "artifacts": [
        {
            "id": "macos-arm64",
            "kind": "installer",
            "platform": "macos",
            "architecture": "arm64",
            "filename": "PrivacyFence-4.5.0.dmg",
            "size": 104857600,
            "sha256": "a" * 64,
        },
        {
            "id": "windows-x64",
            "kind": "installer",
            "platform": "windows",
            "architecture": "x64",
            "filename": "PrivacyFence-4.5.0-setup.exe",
            "size": 89128960,
            "sha256": "b" * 64,
        },
        {
            "id": "linux-x64",
            "kind": "installer",
            "platform": "linux",
            "architecture": "x64",
            "filename": "privacyfence_4.5.0_amd64.deb",
            "size": 47028460,
            "sha256": "c" * 64,
        },
    ],
}
