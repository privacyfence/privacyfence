"""The admin config: ``org/admin_config.json``.

Everything an administrator controls that is neither a startup setting nor an app credential
lives here: which services are offered, the Peek and Grist server addresses, Jira Assets, the
room directory, unattended sessions, file delivery, web push, sign-in restrictions, step-up,
scoped tokens and audit forwarding. The daemon is the only writer. Its trust level is
``settings.yaml``'s (the install directory), not the signed bundle's.

The operational sections are stored in exactly the shape the bundle used for them, so the
existing ``from_org_config`` parsers read them unchanged. A 6.x bundle's copy of these settings is
imported once (``import_from_bundle`` / ``ensure``) and afterwards ignored.

No function here puts a client secret, or the value of a bearer-token environment variable, into
a message, a log line or a return value; an ``AdminConfigError`` names the file, never its content.

``oauth_apps`` imports this module, so this module must not import ``oauth_apps``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

from . import paths, secure_files
from .org_mode import Mode
from .step_up_config import STEP_UP_SCOPES

logger = logging.getLogger(__name__)

VERSION = 1
FILENAME = "admin_config.json"

SERVICES = ("google", "slack", "salesforce", "atlassian", "miro", "telegram", "grist", "peek")
SERVICE_LABELS = {
    "google": "Google",
    "slack": "Slack",
    "salesforce": "Salesforce",
    "atlassian": "Atlassian",
    "miro": "Miro",
    "telegram": "Telegram",
    "grist": "Grist",
    "peek": "Peek",
}
OPERATIONAL_SECTIONS = (
    "rooms",
    "rooms_synced_at",
    "unattended_sessions",
    "download_delivery",
    "web_push",
    "authz",
    "step_up",
    "scoped_tokens",
    "audit_forwarding",
)
LEGACY_BUNDLE_KEYS = (
    "download_delivery",
    "web_push",
    "authz",
    "step_up",
    "scoped_tokens",
    "audit_forwarding",
    "unattended_sessions",
    "rooms",
    "rooms_synced_at",
    "telegram",
    "peek",
)

# Services with built-in apps, offered on a fresh org-mode single-user install (desktop mode has
# none, and a multi-user server's first offered service is the admin's opt-in).
_BUILTIN_SERVICES = ("google", "slack", "salesforce", "atlassian", "miro", "telegram")

_DEFAULT_KEY_ENV = {
    "peek": "PRIVACYFENCE_PEEK_PROVISIONING_KEY",
    "grist": "PRIVACYFENCE_GRIST_PROVISIONING_KEY",
}
_ADDRESS_FIELD = {"peek": "base_url", "grist": "server_url"}

# The packaged-local step-up defaults a single-user install starts with.
_SINGLE_USER_STEP_UP = {"enabled": True, "require_passkey": True, "scope": "writes_and_pii_reads"}

_write_lock = threading.Lock()


class AdminConfigError(ValueError):
    """The admin config file is unreadable, malformed or of an unknown version."""


def admin_config_path() -> Path:
    return paths.org_dir() / FILENAME


def _default_service(name: str) -> dict[str, Any]:
    service: dict[str, Any] = {"offered": False}
    if name == "atlassian":
        service.update({"assets": False, "assets_workspace_id": ""})
    elif name in _ADDRESS_FIELD:
        auto_provision: dict[str, Any] = {
            "enabled": False,
            "trust_unverified_email": False,
            "key_env": _DEFAULT_KEY_ENV[name],
        }
        if name == "grist":
            auto_provision["provisioner_url"] = ""
        service.update({_ADDRESS_FIELD[name]: "", "address_locked": False, "auto_provision": auto_provision})
    return service


def _default_config() -> dict[str, Any]:
    return {
        "version": VERSION,
        "imported_from_bundle_sha256": "",
        "services": {name: _default_service(name) for name in SERVICES},
        "rooms": [],
        "rooms_synced_at": "",
        "unattended_sessions": {"enabled": False},
        "download_delivery": {},
        "web_push": {},
        "authz": {},
        "step_up": {},
        "scoped_tokens": {},
        "audit_forwarding": {},
    }


def _fill(default: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """``given`` with every key of ``default`` it lacks filled in, recursively. A value that
    should be an object but is not is replaced by the default."""
    result = copy.deepcopy(given)
    for key, default_value in default.items():
        if key not in result:
            result[key] = copy.deepcopy(default_value)
        elif isinstance(default_value, dict):
            current = result[key]
            result[key] = _fill(default_value, current) if isinstance(current, dict) else copy.deepcopy(default_value)
    return result


def load() -> dict[str, Any] | None:
    """The stored admin config, normalized; ``None`` when there is no file."""
    path = admin_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        raise AdminConfigError(f"{path} could not be read.") from None
    try:
        raw = json.loads(text)
    except ValueError:
        raise AdminConfigError(f"{path} is not valid JSON.") from None
    if not isinstance(raw, dict):
        raise AdminConfigError(f"{path} must hold a JSON object.")
    version = raw.get("version")
    if isinstance(version, bool) or version != VERSION:
        raise AdminConfigError(f"{path} has an unsupported version; this PrivacyFence reads version {VERSION}.")
    return _fill(_default_config(), raw)


def save(data: dict[str, Any]) -> None:
    with _write_lock:
        secure_files.atomic_write_json(admin_config_path(), data, indent=2, sort_keys=True)


def _section(bundle: dict[str, Any], key: str) -> dict[str, Any]:
    value = bundle.get(key)
    return value if isinstance(value, dict) else {}


def _bundle_hash(bundle: dict[str, Any]) -> str:
    if not bundle:
        return ""
    return hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _import_auto_provision(service: dict[str, Any], name: str, section: dict[str, Any]) -> None:
    raw = section.get("auto_provision")
    if not isinstance(raw, dict):
        return
    target = service["auto_provision"]
    target["enabled"] = bool(raw.get("enabled", False))
    target["trust_unverified_email"] = bool(raw.get("trust_unverified_email", False))
    key_env = raw.get("key_env")
    if isinstance(key_env, str) and key_env:
        target["key_env"] = key_env
    # A 6.11 server with automatic accounts on keeps working through the upgrade: its signed
    # bundle named the address the provisioning key may be sent to, so the address stays locked.
    if target["enabled"] and service[_ADDRESS_FIELD[name]]:
        service["address_locked"] = True
        if name == "grist":
            provisioner_url = raw.get("provisioner_url")
            if isinstance(provisioner_url, str):
                target["provisioner_url"] = provisioner_url


def import_from_bundle(bundle: dict[str, Any], *, mode: Mode, single_user: bool) -> dict[str, Any]:
    """A fresh admin config built from a bundle's operational settings."""
    config = _default_config()
    services = config["services"]

    if any(_section(bundle, name) for name in SERVICES):
        for name in SERVICES:
            services[name]["offered"] = bool(_section(bundle, name))
    elif mode == "local":
        services["telegram"]["offered"] = True
    elif single_user:
        for name in _BUILTIN_SERVICES:
            services[name]["offered"] = True

    atlassian = _section(bundle, "atlassian")
    if "assets" in atlassian:
        services["atlassian"]["assets"] = bool(atlassian["assets"])
    if atlassian.get("assets_workspace_id"):
        services["atlassian"]["assets_workspace_id"] = str(atlassian["assets_workspace_id"])

    peek = _section(bundle, "peek")
    if peek.get("base_url"):
        services["peek"]["base_url"] = str(peek["base_url"])
    _import_auto_provision(services["peek"], "peek", peek)

    grist = _section(bundle, "grist")
    if not grist.get("client_id"):
        if grist.get("server_url"):
            services["grist"]["server_url"] = str(grist["server_url"])
        _import_auto_provision(services["grist"], "grist", grist)

    for key in OPERATIONAL_SECTIONS:
        if key in bundle:
            config[key] = copy.deepcopy(bundle[key])
    if single_user and "step_up" not in bundle:
        config["step_up"] = dict(_SINGLE_USER_STEP_UP)

    config["imported_from_bundle_sha256"] = _bundle_hash(bundle)
    return config


def legacy_keys_in(bundle: dict[str, Any]) -> list[str]:
    """The operational settings a bundle still carries, as dotted names."""
    names = [key for key in LEGACY_BUNDLE_KEYS if key in bundle]
    atlassian = _section(bundle, "atlassian")
    names.extend(f"atlassian.{key}" for key in ("assets", "assets_workspace_id") if key in atlassian)
    grist = _section(bundle, "grist")
    if "server_url" in grist and not grist.get("client_id"):
        names.append("grist.server_url")
    if "auto_provision" in grist:
        names.append("grist.auto_provision")
    return names


def _step_up_not_weaker(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    def rank(raw: dict[str, Any]) -> tuple[bool, bool, int]:
        scope = raw.get("scope")
        return (
            bool(raw.get("enabled", False)),
            bool(raw.get("require_passkey", False)),
            STEP_UP_SCOPES.index(scope) if scope in STEP_UP_SCOPES else 0,
        )

    return all(new >= old for new, old in zip(rank(candidate), rank(current), strict=True))


def _merge_reimport(current: dict[str, Any], incoming: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Fold a bundle that arrived after the first start into ``current`` without overwriting an
    Admin-page decision: a legacy value is copied only where ``current`` still holds its default,
    step-up is never lowered, ``offered`` is never turned off, ``auto_provision.enabled`` and
    anything the file carries beyond this schema (``org_server``) stay as they are."""
    merged = copy.deepcopy(current)
    replaced: list[str] = []

    for key in OPERATIONAL_SECTIONS:
        if current[key] != defaults[key] or incoming[key] == current[key]:
            continue
        if key == "step_up" and not _step_up_not_weaker(incoming[key], current[key]):
            continue
        merged[key] = copy.deepcopy(incoming[key])
        replaced.append(key)

    for name in SERVICES:
        old, new, default = current["services"][name], incoming["services"][name], defaults["services"][name]
        target = merged["services"][name]
        target["offered"] = bool(old["offered"] or new["offered"])
        fields = ["assets", "assets_workspace_id"] if name == "atlassian" else []
        if name in _ADDRESS_FIELD:
            fields.append(_ADDRESS_FIELD[name])
        for field in fields:
            if old[field] == default[field] and new[field] != old[field]:
                target[field] = new[field]
                replaced.append(f"services.{name}.{field}")
                if name in _ADDRESS_FIELD:
                    target["address_locked"] = new["address_locked"]
        if name in _ADDRESS_FIELD:
            for field in ("trust_unverified_email", "key_env", "provisioner_url"):
                if field in default["auto_provision"]:
                    if old["auto_provision"][field] == default["auto_provision"][field] and new["auto_provision"][field] != old["auto_provision"][field]:
                        target["auto_provision"][field] = new["auto_provision"][field]
                        replaced.append(f"services.{name}.auto_provision.{field}")

    for name in replaced:
        logger.warning("A later org_bundle.json replaced %s in org/admin_config.json (it still held its default).", name)
    merged["imported_from_bundle_sha256"] = incoming["imported_from_bundle_sha256"]
    return merged


def ensure(bundle: dict[str, Any], *, mode: Mode, single_user: bool) -> tuple[dict[str, Any], bool]:
    """The admin config, importing the bundle's settings first when needed.

    Returns ``(config, imported)``. With no file the bundle is imported and saved. An existing file
    is returned untouched, except once: when it was created with no bundle (its recorded hash is
    empty) and a bundle carrying legacy settings has arrived since, that bundle is merged in. The
    new config records the bundle's hash, so this happens at most once.
    """
    loaded = load()
    if loaded is None:
        created = import_from_bundle(bundle, mode=mode, single_user=single_user)
        save(created)
        return created, True
    if loaded["imported_from_bundle_sha256"] == "" and legacy_keys_in(bundle):
        incoming = import_from_bundle(bundle, mode=mode, single_user=single_user)
        defaults = import_from_bundle({}, mode=mode, single_user=single_user)
        merged = _merge_reimport(loaded, incoming, defaults)
        save(merged)
        return merged, True
    return loaded, False


def config_for(bundle: dict[str, Any], *, mode: Mode, single_user: bool) -> dict[str, Any]:
    """Like ``ensure`` but never writes: for readers that run without the daemon."""
    loaded = load()
    if loaded is not None:
        return loaded
    return import_from_bundle(bundle, mode=mode, single_user=single_user)
