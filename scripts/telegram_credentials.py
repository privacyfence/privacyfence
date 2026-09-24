#!/usr/bin/env python3
"""Write, and check for, the build-time Telegram app credentials module.

Telegram's api_id/api_hash identify the PrivacyFence *application* to Telegram, not a user or an
organization (see src/privacyfence/app_credentials.py and docs/telegram-setup.md). This repo is
public, so they are never committed: CI supplies them as the TELEGRAM_API_ID/TELEGRAM_API_HASH
secrets, and every build that ships Telegram support writes them into the git-ignored
``src/privacyfence/_telegram_credentials.py`` right before packaging. This script is that one
generator -- scripts/build_dmg.sh, scripts/build_deb.sh, scripts/build_installer.ps1 and
.github/workflows/publish-pypi.yml all call it rather than each carrying their own heredoc. Every
distribution, PyPI included, ships the credentials: ADR 0040.

Subcommands:

``write``
    Write the module from TELEGRAM_API_ID/TELEGRAM_API_HASH when both are set; otherwise delete
    any stale copy, so a local build without the secrets ships without Telegram support rather
    than with whatever an earlier build left behind. Exit 0 either way; exit 2 when
    TELEGRAM_API_ID is set but is not an integer (a mangled secret should not become a build that
    fails only when a user first opens Telegram).

``check-dist [--require] <file>...``
    Report whether each built wheel (``.whl``) and sdist (``.tar.gz``) contains the module. The
    module is git-ignored, and setuptools_scm's file finder only packages tracked files, so it
    reaches the sdist -- and through it the wheel -- only via MANIFEST.in's explicit ``include``;
    this is what proves that still works. Missing with ``--require`` (a stable tag) exits 1;
    missing without it prints a GitHub Actions ``::warning::`` and exits 0.

Stdlib only, like scripts/r2_release.py: the publish-pypi build job calls it before installing
PrivacyFence.
"""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CREDS_PATH = REPO_ROOT / "src" / "privacyfence" / "_telegram_credentials.py"
MODULE_SUFFIX = "privacyfence/_telegram_credentials.py"


def render(api_id: str, api_hash: str) -> str:
    return f"API_ID = {int(api_id)}\nAPI_HASH = {api_hash!r}\n"


def write(creds_path: Path = CREDS_PATH, environ: Mapping[str, str] = os.environ) -> int:
    api_id = environ.get("TELEGRAM_API_ID", "")
    api_hash = environ.get("TELEGRAM_API_HASH", "")
    if not (api_id and api_hash):
        print("-> TELEGRAM_API_ID/TELEGRAM_API_HASH not set; building without Telegram app credentials.")
        creds_path.unlink(missing_ok=True)
        return 0
    try:
        content = render(api_id, api_hash)
    except ValueError:
        print("error: TELEGRAM_API_ID is not an integer.", file=sys.stderr)
        return 2
    print("-> Writing Telegram app credentials...")
    creds_path.write_text(content, encoding="utf-8")
    return 0


def archive_members(path: Path) -> list[str]:
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as tf:
            return tf.getnames()
    raise ValueError(f"not a wheel or sdist: {path}")


def contains_credentials(path: Path) -> bool:
    return any(name.endswith(MODULE_SUFFIX) for name in archive_members(path))


def check_dist(paths: list[Path], require: bool) -> int:
    if not paths:
        print("error: no distribution files given.", file=sys.stderr)
        return 2
    missing = []
    for path in paths:
        if contains_credentials(path):
            print(f"{path.name}: contains {MODULE_SUFFIX}")
        else:
            missing.append(path.name)
    if not missing:
        return 0
    message = f"{', '.join(missing)} lack {MODULE_SUFFIX}; Telegram will not work on this install."
    if require:
        print(f"::error::{message} A stable release must ship it (ADR 0040).")
        return 1
    print(f"::warning::{message}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("write", help="write or remove the credentials module from the environment")
    check = sub.add_parser("check-dist", help="check built wheels/sdists for the credentials module")
    check.add_argument("--require", action="store_true", help="fail (instead of warn) when missing")
    check.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    if args.command == "write":
        return write(CREDS_PATH)
    return check_dist(args.files, args.require)


if __name__ == "__main__":
    raise SystemExit(main())
