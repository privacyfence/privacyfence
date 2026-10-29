#!/usr/bin/env python3
"""Write, and check for, the build-time built-in OAuth apps module.

The built-in apps are PrivacyFence-owned OAuth apps (Google, Slack, Salesforce, Atlassian, Miro and
the Google sign-in client) that organization servers use through the OAuth relay. Their client IDs
and secrets identify the PrivacyFence *application* to each provider, as Telegram's api_id/api_hash
do (scripts/telegram_credentials.py, ADR 0040), and are public in effect (ADR 0254). This repo is
public, so they are never committed: CI supplies them as the PF_APP_* secrets, and the builds that
ship a server -- the org image's wheel in .github/workflows/build.yml and
.github/workflows/publish-pypi.yml -- write them into the git-ignored
``src/privacyfence/_builtin_apps.py`` right before packaging. The desktop installers do not call
this script: desktop mode has no built-in apps.

Subcommands:

``write``
    For each key in KEYS read PF_APP_<KEY>_CLIENT_ID and PF_APP_<KEY>_CLIENT_SECRET, and write the
    module with every key that has both. A key with only one of the two is skipped with a warning.
    PF_APP_SALESFORCE_INSTALL_URL, when it is an https://login.salesforce.com/ URL, becomes the
    salesforce entry's ``install_url``. With no key set, any stale copy is deleted and the exit
    code is 0. A value with a control character exits 2 and writes nothing.

``check-dist [--require] <file>...``
    Report whether each built wheel (``.whl``) and sdist (``.tar.gz``) contains the module. The
    module is git-ignored, so it reaches the sdist -- and through it the wheel -- only via
    MANIFEST.in's explicit ``include``. Missing with ``--require`` (a release tag) exits 1; missing
    without it prints a GitHub Actions ``::warning::`` and exits 0.

Stdlib only, like scripts/telegram_credentials.py.
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
CREDS_PATH = REPO_ROOT / "src" / "privacyfence" / "_builtin_apps.py"
MODULE_SUFFIX = "privacyfence/_builtin_apps.py"
KEYS = ("google_web", "slack", "salesforce", "atlassian_relay", "miro", "google_signin")
INSTALL_URL_VAR = "PF_APP_SALESFORCE_INSTALL_URL"
INSTALL_URL_PREFIX = "https://login.salesforce.com/"


def render(apps: Mapping[str, Mapping[str, str]]) -> str:
    return "APPS = " + repr(dict(sorted(apps.items()))) + "\n"


def _has_control_character(value: str) -> bool:
    return "\n" in value or "\r" in value or "\0" in value


def write(creds_path: Path = CREDS_PATH, environ: Mapping[str, str] = os.environ) -> int:
    apps: dict[str, dict[str, str]] = {}
    status: list[str] = []
    for key in KEYS:
        id_var = f"PF_APP_{key.upper()}_CLIENT_ID"
        secret_var = f"PF_APP_{key.upper()}_CLIENT_SECRET"
        client_id = environ.get(id_var, "")
        client_secret = environ.get(secret_var, "")
        for var, value in ((id_var, client_id), (secret_var, client_secret)):
            if _has_control_character(value):
                print(f"error: {var} contains a control character.", file=sys.stderr)
                return 2
        if bool(client_id) != bool(client_secret):
            print(f"warning: {id_var} and {secret_var} must both be set; skipping {key}.")
        if client_id and client_secret:
            apps[key] = {"client_id": client_id, "client_secret": client_secret}
            status.append(f"built-in app {key}: included")
        else:
            status.append(f"built-in app {key}: not set")

    install_url = environ.get(INSTALL_URL_VAR, "")
    if _has_control_character(install_url):
        print(f"error: {INSTALL_URL_VAR} contains a control character.", file=sys.stderr)
        return 2
    if install_url and "salesforce" in apps:
        if install_url.startswith(INSTALL_URL_PREFIX):
            apps["salesforce"]["install_url"] = install_url
        else:
            print(f"warning: {INSTALL_URL_VAR} must start with {INSTALL_URL_PREFIX}; ignoring it.")

    print("\n".join(status))
    if not apps:
        print("-> No built-in app is set; building without built-in OAuth apps.")
        creds_path.unlink(missing_ok=True)
        return 0
    print("-> Writing built-in OAuth apps...")
    creds_path.write_text(render(apps), encoding="utf-8")
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
    message = f"{', '.join(missing)} lack {MODULE_SUFFIX}; built-in OAuth apps will not work on this install."
    if require:
        print(f"::error::{message} A stable release must ship it (ADR 0254).")
        return 1
    print(f"::warning::{message}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("write", help="write or remove the built-in apps module from the environment")
    check = sub.add_parser("check-dist", help="check built wheels/sdists for the built-in apps module")
    check.add_argument("--require", action="store_true", help="fail (instead of warn) when missing")
    check.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    if args.command == "write":
        return write(CREDS_PATH)
    return check_dist(args.files, args.require)


if __name__ == "__main__":
    raise SystemExit(main())
