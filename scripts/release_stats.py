#!/usr/bin/env python3
"""Compute the website's public release-stats.json (release-publishing plan Phase 4).

Before this script existed, `.github/workflows/pages.yml` summed every release asset's
`download_count` inline, in a heredoc with no test coverage -- including SBOMs (`*.spdx.json`,
`*.cdx.json`), the org-config scripts (`*.py`), and the sdist/wheel (`*.whl`, `*.tar.gz`), none of
which are installers a user downloaded. That inflated the GitHub half of the KPI this repo actually
wants: "PrivacyFence installer downloads" (see docs/downloads-and-release-kpi.md's KPI section).

This script fixes that by reusing `classify_installer()` from `scripts/r2_release.py` -- the same
DMG / `-setup.exe` / `.deb` filename patterns that already decide what a tagged release's manifest
lists as a downloadable installer (see that module's `_INSTALLERS`). A GitHub Release for a stable
tag is built from exactly those files (`build.yml` attaches the DMG/installer/`.deb` it builds), so
the same classifier applies unchanged here -- one definition of "installer" for both the Cloudflare
and GitHub halves of the KPI, rather than two filters that could quietly drift apart.

The emitted field is named `github_installer_downloads`, not `downloads`, so a reader of
release-stats.json (website/stats.js, or anyone else) can't mistake it for the combined KPI --
`downloads` was ambiguous about whether it already included Cloudflare's counts (it never did).

Usage (same as .github/workflows/pages.yml):
    python3 scripts/release_stats.py --repo privacyfence/privacyfence --output _site/release-stats.json
Reads the GitHub token from the GH_TOKEN environment variable (falls back to GITHUB_TOKEN),
matching actions/github-script's own convention.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from r2_release import classify_installer  # noqa: E402 -- see module docstring for why

API_ROOT = "https://api.github.com"


def is_installer_asset(filename: str) -> bool:
    """True for exactly the files docs/downloads-and-release-kpi.md counts as installer
    downloads (DMG / -setup.exe / .deb) -- everything else (SBOMs, checksums, org-config
    scripts, sdist/wheel, and anything else attached to a release) is excluded."""
    return classify_installer(filename) is not None


def compute_stats(repository: dict[str, Any], releases: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure function of the two GitHub API responses this script fetches -- kept separate from
    the network calls below so it's directly unit-testable without mocking HTTP."""
    installer_downloads = sum(
        int(asset.get("download_count", 0))
        for release in releases
        if not release.get("draft")
        for asset in release.get("assets", [])
        if is_installer_asset(asset.get("name", ""))
    )
    published = [r for r in releases if not r.get("draft") and not r.get("prerelease")]
    latest = published[0].get("tag_name") if published else None

    return {
        "github_installer_downloads": installer_downloads,
        "stars": int(repository.get("stargazers_count", 0)),
        "latest_release": latest,
    }


def _get(url: str, token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "privacyfence-release-stats",
    }
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:  # url is always the fixed API_ROOT https host above
        return json.load(response)


def fetch_and_compute(repo: str, token: str) -> dict[str, Any]:
    repository = _get(f"{API_ROOT}/repos/{repo}", token)
    releases = _get(f"{API_ROOT}/repos/{repo}/releases?per_page=100", token)
    return compute_stats(repository, releases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help='"owner/repo", e.g. privacyfence/privacyfence')
    parser.add_argument("--output", required=True, type=Path, help="where to write release-stats.json")
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GH_TOKEN (or GITHUB_TOKEN) must be set", file=sys.stderr)
        return 2

    data = fetch_and_compute(args.repo, token)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {args.output}: {data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
