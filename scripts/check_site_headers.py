#!/usr/bin/env python3
"""Compares the security headers a deployed site sends with the expected ones.

    python3 scripts/check_site_headers.py https://privacyfence.eu

Requests <base>/, <base>/docs/ and <base>/download/ without following redirects and compares
each header scripts/site_security_headers.py expects for that path (names case-insensitive, values
exact). Exits 0 when all match, 1 otherwise. Stdlib only.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_security_headers import headers_for_path  # noqa: E402 -- a sibling script, not a package

PATHS = ("/", "/docs/", "/download/")
TIMEOUT_SECONDS = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_headers(url: str) -> dict[str, str]:
    """The response headers for `url`, lower-cased names; a 3xx/4xx/5xx still has headers."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=TIMEOUT_SECONDS) as response:
            return {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return {k.lower(): v for k, v in exc.headers.items()}


def check(base_url: str) -> list[str]:
    """One line per header, `[ ok ] ...` or `[FAIL] ...`, in request order."""
    base = base_url.rstrip("/")
    lines: list[str] = []
    for path in PATHS:
        try:
            got = _fetch_headers(f"{base}{path}")
        except (urllib.error.URLError, OSError) as exc:
            lines.append(f"[FAIL] {path} request: could not fetch ({exc})")
            continue
        for name, expected in headers_for_path(path).items():
            actual = got.get(name.lower())
            if actual == expected:
                lines.append(f"[ ok ] {path} {name}")
            else:
                lines.append(f"[FAIL] {path} {name}: expected {expected!r} got {actual!r}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_site_headers.py <base-url>", file=sys.stderr)
        return 2
    lines = check(args[0])
    print("\n".join(lines))
    failed = any(line.startswith("[FAIL]") for line in lines)
    print("Site security headers differ." if failed else "Site security headers match.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
