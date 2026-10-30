"""PrivacyFence application-level credentials.

Telegram's api_id/api_hash identify the PrivacyFence *application* to
Telegram's API (MTProto has no concept of "organization" the way OAuth
does) — they are the same for every user and every phone number, and are
not a substitute for per-user auth (still phone + code + optional 2FA, see
menu_bar.py). Because this repo is public, the real values are never
committed: every release build -- DMG, Windows installer, ``.deb`` and the
PyPI sdist/wheel -- bakes them in from CI secrets (see
scripts/telegram_credentials.py and ADR 0040) into the git-ignored
``_telegram_credentials`` module generated right before packaging. Local/dev
builds fall back to the
PRIVACYFENCE_TELEGRAM_API_ID / PRIVACYFENCE_TELEGRAM_API_HASH env vars.

The built-in OAuth apps (Google, Slack, Salesforce, Atlassian, Miro and the Google sign-in client)
work the same way: scripts/builtin_apps.py writes the git-ignored ``_builtin_apps`` module from the
PF_APP_* CI secrets into the org image's wheel and the PyPI build, and ``builtin_app`` reads it
(ADR 0255). Desktop installers ship none. Local/dev builds point PRIVACYFENCE_BUILTIN_APPS at a
JSON file of the same shape.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

BUILTIN_APP_KEYS: tuple[str, ...] = (
    "google_web",
    "slack",
    "salesforce",
    "atlassian_relay",
    "miro",
    "google_signin",
)


def telegram_app_credentials() -> tuple[int, str] | None:
    try:
        from . import _telegram_credentials  # type: ignore[attr-defined]  # generated at build time; absent in a source checkout
    except ImportError:
        pass
    else:
        return int(_telegram_credentials.API_ID), _telegram_credentials.API_HASH

    api_id = os.environ.get("PRIVACYFENCE_TELEGRAM_API_ID")
    api_hash = os.environ.get("PRIVACYFENCE_TELEGRAM_API_HASH")
    if api_id and api_hash:
        return int(api_id), api_hash
    return None


def _builtin_apps_from_file() -> dict[str, dict[str, str]] | None:
    path = os.environ.get("PRIVACYFENCE_BUILTIN_APPS")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        logger.warning("PRIVACYFENCE_BUILTIN_APPS does not point to a readable JSON object; ignoring it.")
        return None
    return data


def builtin_app(key: str) -> dict[str, str] | None:
    try:
        from . import _builtin_apps  # type: ignore[attr-defined]  # generated at build time; absent in a source checkout
    except ImportError:
        apps = _builtin_apps_from_file()
    else:
        apps = _builtin_apps.APPS
    app = (apps or {}).get(key)
    return dict(app) if isinstance(app, dict) else None
