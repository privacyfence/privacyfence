"""The effective org config: the bundle, the admin config and the built-in apps, composed.

Every reader of the organization config (``build_connectors``, the ``_authenticate_*`` methods,
``routes_connect``, ``connector_enabled_by_org`` and each ``from_org_config`` parser) reads one
dict. ``effective_org_config`` builds it. The bundle contributes the organization's own app
credentials, the admin config says which services are offered and carries the operational
settings, and the built-in apps fill in a service the bundle has no credentials for. Built-in
apps exist only in organization mode and always sign in through the relay.

The shared dict is held by several long-lived objects, so it is refreshed in place
(``refresh_in_place``) and iterated through ``snapshot``, both under one lock.
"""
from __future__ import annotations

import copy
import os
import threading
from typing import Any, Literal

from . import admin_config, app_credentials
from .grist_auth import GristClientError, normalize_server_url
from .org_mode import ConfigurationError, Mode
from .peek_client import PeekClientError, normalize_base_url

RELAY_BASE_URL = "https://oauth.privacyfence.eu"
RELAY_MARKER = "relay"
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # nosec B105  # a public endpoint, not a password

# Which built-in app serves each service whose credentials can be built in.
BUILTIN_KEY = {
    "google": "google_web",
    "slack": "slack",
    "salesforce": "salesforce",
    "atlassian": "atlassian_relay",
    "miro": "miro",
}

# Whether the built-in Slack app is listed in the Slack Marketplace. Until it is, Slack limits an
# unlisted app's search and history reads, which the Admin page says. Flipped by hand after
# approval.
SLACK_MARKETPLACE_LISTED: bool = False

AppSource = Literal["bundle", "builtin", "none"]

_ENV_PEEK_BASE_URL = "PRIVACYFENCE_PEEK_BASE_URL"
_ENV_GRIST_SERVER_URL = "PRIVACYFENCE_GRIST_SERVER_URL"
_ENV_GRIST_PROVISIONER_URL = "PRIVACYFENCE_GRIST_PROVISIONER_URL"

_refresh_lock = threading.Lock()


def _section(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def credentials_source(bundle: dict[str, Any], service: str, *, mode: Mode) -> AppSource:
    """Where ``service``'s OAuth app comes from: the bundle, a built-in app, or nowhere."""
    section = _section(bundle, service)
    field = "consumer_key" if service == "salesforce" else "client_id"
    if section.get(field):
        return "bundle"
    if mode == "org" and app_credentials.builtin_app(BUILTIN_KEY[service]) is not None:
        return "builtin"
    return "none"


def environment_addresses() -> dict[str, str]:
    """The Peek and Grist server addresses set in the environment, normalised.

    Keys are ``peek``, ``grist`` and ``grist_provisioner``; an unset or empty variable is left
    out. A value that is not a usable address raises ``ConfigurationError`` naming the variable.
    """
    found: dict[str, str] = {}
    for key, variable, normalize, error in (
        ("peek", _ENV_PEEK_BASE_URL, normalize_base_url, PeekClientError),
        ("grist", _ENV_GRIST_SERVER_URL, normalize_server_url, GristClientError),
        ("grist_provisioner", _ENV_GRIST_PROVISIONER_URL, normalize_server_url, GristClientError),
    ):
        raw = os.environ.get(variable, "").strip()
        if not raw:
            continue
        try:
            found[key] = normalize(raw)
        except error as exc:
            raise ConfigurationError(f"{variable} is not usable: {exc}") from exc
    return found


def _builtin_section(service: str, app: dict[str, str]) -> dict[str, Any]:
    if service == "salesforce":
        section: dict[str, Any] = {
            "consumer_key": app["client_id"],
            "consumer_secret": app["client_secret"],
        }
    else:
        section = {"client_id": app["client_id"], "client_secret": app["client_secret"]}
    if service == "google":
        section["auth_uri"] = GOOGLE_AUTH_URI
        section["token_uri"] = GOOGLE_TOKEN_URI
    section[RELAY_MARKER] = True
    return section


def _credentials_section(bundle: dict[str, Any], service: str, *, mode: Mode) -> dict[str, Any] | None:
    if credentials_source(bundle, service, mode=mode) == "bundle":
        return copy.deepcopy(_section(bundle, service))
    app = app_credentials.builtin_app(BUILTIN_KEY[service]) if mode == "org" else None
    return _builtin_section(service, app) if app is not None else None


def _auto_provision_enabled(admin_service: dict[str, Any], *, mode: Mode, single_user: bool) -> bool:
    return bool(admin_service.get("auto_provision", {}).get("enabled")) and mode == "org" and not single_user


def _peek_section(
    admin_service: dict[str, Any], env: dict[str, str], *, mode: Mode, single_user: bool
) -> dict[str, Any] | None:
    admin_address = admin_service.get("base_url", "")
    base_url = env.get("peek") or admin_address
    if not base_url:
        return {"enabled": True} if mode == "local" else None
    section: dict[str, Any] = {"base_url": base_url}
    trusted = "peek" in env or bool(admin_service.get("address_locked"))
    if trusted and _auto_provision_enabled(admin_service, mode=mode, single_user=single_user):
        stored = admin_service["auto_provision"]
        section["auto_provision"] = {
            "enabled": True,
            "key_env": stored["key_env"],
            "trust_unverified_email": bool(stored["trust_unverified_email"]),
        }
    return section


def _grist_section(
    bundle: dict[str, Any],
    admin_service: dict[str, Any],
    env: dict[str, str],
    *,
    mode: Mode,
    single_user: bool,
) -> dict[str, Any] | None:
    bundled = _section(bundle, "grist")
    if bundled.get("client_id"):
        return copy.deepcopy(bundled)
    server_url = env.get("grist") or admin_service.get("server_url", "")
    if not server_url:
        return {"enabled": True} if mode == "local" else None
    section: dict[str, Any] = {"server_url": server_url}
    locked = bool(admin_service.get("address_locked"))
    stored = admin_service["auto_provision"]
    provisioner_url = env.get("grist_provisioner") or (stored.get("provisioner_url", "") if locked else "")
    if (
        ("grist" in env or locked)
        and provisioner_url
        and _auto_provision_enabled(admin_service, mode=mode, single_user=single_user)
    ):
        section["auto_provision"] = {
            "enabled": True,
            "key_env": stored["key_env"],
            "trust_unverified_email": bool(stored["trust_unverified_email"]),
            "provisioner_url": provisioner_url,
        }
    return section


def _cleaned_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """The bundle without the settings the admin config now owns, and without any relay marker
    (only a built-in app is on the relay)."""
    result = {key: value for key, value in bundle.items() if key not in admin_config.LEGACY_BUNDLE_KEYS}
    for key, value in list(result.items()):
        if not isinstance(value, dict):
            continue
        section = {k: v for k, v in value.items() if k != RELAY_MARKER}
        if key == "atlassian":
            section.pop("assets", None)
            section.pop("assets_workspace_id", None)
        elif key == "grist":
            section.pop("auto_provision", None)
            if not section.get("client_id"):
                section.pop("server_url", None)
        result[key] = copy.deepcopy(section)
    return result


def effective_org_config(
    bundle: dict[str, Any], admin: dict[str, Any], *, mode: Mode, single_user: bool = False
) -> dict[str, Any]:
    """The one dict every reader of the organization config uses. Mutates neither input."""
    cleaned = _cleaned_bundle(bundle)
    result = dict(cleaned)
    services = admin.get("services", {})

    def service_config(name: str) -> dict[str, Any]:
        value = services.get(name, {})
        return value if isinstance(value, dict) else {}

    offered = {name: bool(service_config(name).get("offered", False)) for name in admin_config.SERVICES}
    for name in admin_config.SERVICES:
        if not offered[name]:
            result.pop(name, None)

    env = environment_addresses()
    for name in ("google", "slack", "salesforce", "miro"):
        if offered[name]:
            section = _credentials_section(cleaned, name, mode=mode)
            if section is None:
                result.pop(name, None)
            else:
                result[name] = section
    if offered["atlassian"]:
        section = _credentials_section(cleaned, "atlassian", mode=mode)
        if section is None:
            result.pop("atlassian", None)
        else:
            atlassian = service_config("atlassian")
            section["assets"] = bool(atlassian.get("assets", False))
            if atlassian.get("assets_workspace_id"):
                section["assets_workspace_id"] = atlassian["assets_workspace_id"]
            result["atlassian"] = section
    if offered["telegram"]:
        if app_credentials.telegram_app_credentials() is not None:
            result["telegram"] = {"enabled": True}
    if offered["grist"]:
        grist = _grist_section(cleaned, service_config("grist"), env, mode=mode, single_user=single_user)
        if grist is None:
            result.pop("grist", None)
        else:
            result["grist"] = grist
    if offered["peek"]:
        peek = _peek_section(service_config("peek"), env, mode=mode, single_user=single_user)
        if peek is not None:
            result["peek"] = peek

    for key in admin_config.OPERATIONAL_SECTIONS:
        if key in admin:
            result[key] = copy.deepcopy(admin[key])
    return result


def refresh_in_place(target: dict[str, Any], new: dict[str, Any]) -> None:
    """Make ``target`` equal ``new`` while keeping the same object, which several long-lived
    holders share. Readers that iterate use ``snapshot``."""
    with _refresh_lock:
        for key in [key for key in target if key not in new]:
            del target[key]
        target.update(new)


def snapshot(org_config: dict[str, Any]) -> dict[str, Any]:
    """An independent copy of the shared config, taken between two refreshes."""
    with _refresh_lock:
        return dict(org_config)


def relay_redirect_uri(grant: str) -> str:
    return f"{RELAY_BASE_URL}/callback/{grant}"


def uses_relay(section: dict[str, Any]) -> bool:
    return section.get(RELAY_MARKER) is True
