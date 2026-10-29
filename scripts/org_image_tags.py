#!/usr/bin/env python3
"""Decide which image tags a release publishes for the organization Docker image.

`publish-org-image.yml` calls this with the release version and applies the tags it prints, one
per line. For a stable `X.Y.Z`, the version is always published; the floating tags `X.Y`, `X` and
`latest` are included only when no already-tagged stable release is higher in that scope, so
floating tags only move forward -- re-running an older patch release never drags `latest` (or
`6.5`) back.

A pre-release (PEP 440 short form: `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN`) gets exactly its own version
and nothing else, however high it is, so `latest` never points at a pre-release and the floating
tags, and the unattended upgrades that follow them, never reach one (ADR 0267). Pre-release and
malformed git tags are ignored when comparing stable versions, and any other version string is
refused.

Stdlib only, no PrivacyFence install required -- same as scripts/tag_release.py, and like it this
imports no other script.

Example:
    python3 scripts/org_image_tags.py 6.4.3
        -> prints 6.4.3 and 6.4 if v6.5.0 is already tagged
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_PRE_RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+(?:a|b|rc)\d+$")
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def image_tags(version: str, known_tags: list[str]) -> list[str]:
    if _PRE_RELEASE_RE.match(version):
        return [version]
    match = _VERSION_RE.match(version)
    if match is None:
        raise ValueError(f"not a release version (X.Y.Z, or X.Y.ZaN/bN/rcN), got {version}")
    major, minor, patch = (int(part) for part in match.groups())
    known = [
        (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        for m in (_TAG_RE.match(tag) for tag in known_tags)
        if m is not None
    ]
    tags = [version]
    if not any(k[0] == major and k[1] == minor and k[2] > patch for k in known):
        tags.append(f"{major}.{minor}")
    if not any(k[0] == major and (k[1], k[2]) > (minor, patch) for k in known):
        tags.append(str(major))
    if not any(k > (major, minor, patch) for k in known):
        tags.append("latest")
    return tags


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: org_image_tags.py VERSION", file=sys.stderr)
        return 2
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        tags = image_tags(argv[0], result.stdout.split())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("\n".join(tags))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
