"""Test-only helpers for the tests of privacyfence.eu.

`built_site()` is the site exactly as `scripts/build_site.py` builds it for deployment: the
hand-written pages assembled from their partials, the static files from the build manifest, and
`/docs/` when the docs generator is installed. The tests check that output rather than `website/`
as it sits in the repository, because the deployed site is assembled from more than `website/`
(the partials, the icon and screenshots from elsewhere in the repo, the docs), and because a file
the build manifest forgets is then missing here too.

Built once per test session, offline: `/download/` is not pre-rendered from the live Worker, so
what the browser tests see is download.js rendering the manifest they stub. `/docs/` is rendered
from the working tree when the `docs` extra is installed (the website-build workflow installs it)
and left out otherwise. `PRIVACYFENCE_SITE_DIR` points the tests at a site built beforehand
instead, which is how .github/workflows/website-build.yml runs them on its own build.

Used by the website guardrail tests: tests/integration/test_website_layout.py (12),
tests/integration/test_website_consent.py (13), tests/unit/test_website_pages.py and the rest of
tests/unit/test_website_*.py.
"""

from __future__ import annotations

import atexit
import functools
import http.server
import importlib.util
import os
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEBSITE = REPO / "website"

sys.path.insert(0, str(REPO / "scripts"))
import build_site  # noqa: E402 -- scripts/ is not a package

API_ORIGIN = "https://downloads.privacyfence.eu"
GA_ORIGIN = "https://www.googletagmanager.com"


def docs_generator_installed() -> bool:
    return importlib.util.find_spec("zensical") is not None


@functools.cache
def built_site() -> Path:
    """The built site's directory: `PRIVACYFENCE_SITE_DIR`, or a fresh offline build."""
    prebuilt = os.environ.get("PRIVACYFENCE_SITE_DIR")
    if prebuilt:
        return Path(prebuilt).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="pf-site-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    out = tmp / "_site"
    build_site.build(out, docs_ref="worktree" if docs_generator_installed() else None, fetch_manifest=False)
    return out


def docs_built(site: Path | None = None) -> bool:
    return ((site or built_site()) / "docs" / "index.html").is_file()


def page_paths(site: Path | None = None) -> list[str]:
    """URL paths the browser guardrails load: every hand-written page in the build manifest, and
    the docs layout sample when /docs/ was built."""
    site = site or built_site()
    return list(build_site.PAGES) + [
        p for p in build_site.DOCS_LAYOUT_SAMPLE if (site / p.lstrip("/") / "index.html").is_file()
    ]


def read_page(path: str, site: Path | None = None) -> str:
    return ((site or built_site()) / path.lstrip("/") / "index.html").read_text(encoding="utf-8")


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
