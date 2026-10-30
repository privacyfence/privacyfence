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
import ipaddress
import json
import logging
import math
import os
import re
import socket
import threading
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import app_credentials, grist_auth, paths, peek_client, scoped_tokens, secure_files
from .org_mode import (
    ADMIN_EMAILS_ENV,
    AuditForwardingConfig,
    AuthzPolicyConfig,
    ConfigurationError,
    DownloadDeliveryConfig,
    Mode,
    WebPushConfig,
    builtin_signin_selected,
    provisioning_key,
)
from .step_up_config import DEFAULT_STEP_UP_SCOPE, STEP_UP_SCOPES, StepUpConfig

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
        # Set by the daemon on the first start that has an identity provider and by nothing else:
        # it marks the data directory as an organization server's, so removing the IdP settings
        # never turns it into a single-user install.
        "org_server": False,
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
    config = _fill(_default_config(), raw)
    config["org_server"] = raw.get("org_server") is True
    return config


def mark_org_server() -> dict[str, Any]:
    """Record that this data directory belongs to an organization server; returns the config.

    Writes only when the flag is not already set. The daemon is the only caller: no Admin-page
    action can set or clear it."""
    with _write_lock:
        config = load() or _default_config()
        if not config["org_server"]:
            config["org_server"] = True
            _write(config)
        return config


def _write(data: dict[str, Any]) -> None:
    secure_files.atomic_write_json(admin_config_path(), data, indent=2, sort_keys=True)


def save(data: dict[str, Any]) -> None:
    with _write_lock:
        _write(data)


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


ALLOWED_DOMAINS_ENV = "PRIVACYFENCE_ALLOWED_DOMAINS"
# A DNS name of at least two labels, lower-case ASCII, ending in a letter label: no IP address, no
# "@", no wildcard and no bare top-level domain.
_DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def allowed_domains_from_env() -> list[str]:
    """``PRIVACYFENCE_ALLOWED_DOMAINS`` as a list of lower-cased domain names; empty when unset.

    A value that is not a domain name refuses the start rather than being dropped, so a typo never
    leaves the server admitting fewer, or other, people than the operator meant."""
    domains: list[str] = []
    for part in os.environ.get(ALLOWED_DOMAINS_ENV, "").split(","):
        raw = part.strip()
        if not raw:
            continue
        # The ASCII check comes first: str.lower maps the Kelvin sign to "k".
        domain = raw.lower() if raw.isascii() else ""
        if not _DOMAIN_RE.match(domain):
            raise ConfigurationError(
                f"{ALLOWED_DOMAINS_ENV} must be a comma-separated list of domain names such as "
                f"acme.com; {raw!r} is not one."
            )
        if domain not in domains:
            domains.append(domain)
    return domains


def _seed_allowed_domains(config: dict[str, Any]) -> None:
    """Start the allowed domains from the environment, unless the bundle already brought some.
    Only a brand-new admin config is seeded: afterwards the list belongs to Admin → Sign-in."""
    domains = allowed_domains_from_env()
    if not domains:
        return
    authz = config["authz"] if isinstance(config.get("authz"), dict) else {}
    if authz.get("allowed_domains"):
        logger.warning(
            "%s is ignored: org_config.json already sets the allowed domains.", ALLOWED_DOMAINS_ENV,
        )
        return
    config["authz"] = {**authz, "allowed_domains": domains}


def import_from_bundle(
    bundle: dict[str, Any], *, mode: Mode, single_user: bool, seed_from_environment: bool = True,
) -> dict[str, Any]:
    """A fresh admin config built from a bundle's operational settings.

    On an organization server ``PRIVACYFENCE_ALLOWED_DOMAINS`` seeds the allowed domains, unless
    ``seed_from_environment`` is false (a later bundle merged into an existing config)."""
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
    if mode == "org" and seed_from_environment:
        _seed_allowed_domains(config)

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


def _step_up_rank(raw: dict[str, Any]) -> tuple[bool, bool, int]:
    """How strict a stored ``step_up`` section is: enabled, passkey required, scope (narrowest
    first). A missing scope is the default scope, as the parser reads it; an unknown one counts as
    the narrowest."""
    scope = raw.get("scope", DEFAULT_STEP_UP_SCOPE)
    return (
        bool(raw.get("enabled", False)),
        bool(raw.get("require_passkey", False)),
        STEP_UP_SCOPES.index(scope) if scope in STEP_UP_SCOPES else 0,
    )


def _step_up_not_weaker(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    return all(new >= old for new, old in zip(_step_up_rank(candidate), _step_up_rank(current), strict=True))


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
    if mode == "org" and os.environ.get(ALLOWED_DOMAINS_ENV, "").strip():
        logger.info(
            "%s is ignored: it only seeds the allowed domains on the first start; change them on "
            "Admin → Sign-in.", ALLOWED_DOMAINS_ENV,
        )
    if loaded["imported_from_bundle_sha256"] == "" and legacy_keys_in(bundle):
        incoming = import_from_bundle(bundle, mode=mode, single_user=single_user, seed_from_environment=False)
        defaults = import_from_bundle({}, mode=mode, single_user=single_user, seed_from_environment=False)
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


# --------------------------------------------------------------------------- #
# Changes made from the Admin page
# --------------------------------------------------------------------------- #

_LOCAL_AND_ORG: frozenset[str] = frozenset({"local", "org"})
_ORG_ONLY: frozenset[str] = frozenset({"org"})
_ACTION_MODES: dict[str, frozenset[str]] = {
    "offer_service": _LOCAL_AND_ORG,
    "withdraw_service": _LOCAL_AND_ORG,
    "set_atlassian_assets": _LOCAL_AND_ORG,
    "set_server_address": _LOCAL_AND_ORG,
    "set_unattended_sessions": _LOCAL_AND_ORG,
    "clear_rooms": _LOCAL_AND_ORG,
    "set_auto_provisioning": _ORG_ONLY,
    "set_download_delivery": _ORG_ONLY,
    "set_web_push": _ORG_ONLY,
    "set_authz": _ORG_ONLY,
    "tighten_step_up": _ORG_ONLY,
    "set_scoped_tokens": _ORG_ONLY,
    "set_audit_forwarding": _ORG_ONLY,
}

MAX_ROOMS = 5000
MAX_ROOM_TEXT = 500
_ROOMS_REFUSAL = "The file is not a room directory written by sync_room_directory.py."
_STEP_UP_REFUSAL = (
    "Step-up can only be made stricter here. To relax it, edit org/admin_config.json and restart "
    "PrivacyFence (ADR 0068)."
)

_ADDRESS_VARIABLE = {"peek": "PRIVACYFENCE_PEEK_BASE_URL", "grist": "PRIVACYFENCE_GRIST_SERVER_URL"}
_PROVISIONER_VARIABLE = "PRIVACYFENCE_GRIST_PROVISIONER_URL"
_AUTO_PROVISION_UNAVAILABLE = {
    "peek": (
        "Automatic accounts need the Peek address from PRIVACYFENCE_PEEK_BASE_URL on the server, "
        "so the provisioning key only goes where the server's operator points it."
    ),
    "grist": (
        "Automatic accounts need PRIVACYFENCE_GRIST_SERVER_URL and "
        "PRIVACYFENCE_GRIST_PROVISIONER_URL on the server."
    ),
}
_GRIST_OAUTH_REASON = "Automatic accounts work only with API keys; this server uses a Grist OAuth app."


def _parse_error(exc: Exception) -> AdminConfigError:
    return AdminConfigError(str(exc).replace("org_bundle.json's ", ""))


def _param(params: dict[str, Any], name: str, kind: type) -> Any:
    if name not in params:
        raise AdminConfigError(f"Missing {name}.")
    value = params[name]
    if isinstance(value, bool) != (kind is bool) or not isinstance(value, kind):
        raise AdminConfigError(f"{name} has the wrong type.")
    return value


def _service_name(params: dict[str, Any], allowed: tuple[str, ...] = SERVICES) -> str:
    service = _param(params, "service", str)
    if service not in allowed:
        raise AdminConfigError("Unknown service.")
    return service


def _text(params: dict[str, Any], name: str) -> str:
    value = _param(params, name, str).strip()
    if any(unicodedata.category(char) in ("Cc", "Zl", "Zp") for char in value):
        raise AdminConfigError(f"{name} must not contain control characters.")
    return value


def _word_list(raw: str) -> list[str]:
    return [item for item in (part.strip() for part in raw.replace("\n", ",").split(",")) if item]


def _env_addresses() -> dict[str, str]:
    from . import oauth_apps

    try:
        return oauth_apps.environment_addresses()
    except ConfigurationError:
        return {}


def _address_source(bundle: dict[str, Any], name: str, admin_service: dict[str, Any], env: dict[str, str]) -> str:
    grist = bundle.get("grist")
    if name == "grist" and isinstance(grist, dict) and grist.get("client_id"):
        return "bundle"
    if env.get(name):
        return "environment"
    if admin_service.get("address_locked"):
        return "imported"
    return "admin"


def _provisioning_key_set(key_env: str) -> bool:
    """Whether the server has a provisioning key; an unreadable key file counts as not set here,
    because the daemon refuses to start on it and the page only shows the state."""
    try:
        return provisioning_key(key_env) is not None
    except ConfigurationError:
        return False


def _auto_provision_state(
    bundle: dict[str, Any],
    name: str,
    admin_service: dict[str, Any],
    env: dict[str, str],
    *,
    mode: Mode,
    single_user: bool,
) -> tuple[bool, str]:
    """Whether automatic accounts can be turned on for ``name``, and why not. The conditions are
    the ones the effective config applies before it passes ``auto_provision`` on."""
    source = _address_source(bundle, name, admin_service, env)
    if source == "bundle":
        return False, _GRIST_OAUTH_REASON
    if mode != "org":
        return False, "Automatic accounts are available only on an organization server."
    if single_user:
        return False, "Automatic accounts need people to sign in through an identity provider."
    trusted = source in ("environment", "imported")
    if name == "grist":
        stored = admin_service.get("auto_provision", {})
        provisioner = env.get("grist_provisioner") or (stored.get("provisioner_url", "") if source == "imported" else "")
        trusted = trusted and bool(provisioner)
    if not trusted:
        return False, _AUTO_PROVISION_UNAVAILABLE[name]
    return True, ""


def _refuse_locked_address(name: str, source: str) -> None:
    variable = _ADDRESS_VARIABLE[name]
    if source == "environment":
        raise AdminConfigError(f"This address is set by {variable} on the server.")
    if source == "imported":
        raise AdminConfigError(
            f"This address came from the signed configuration file; set {variable} on the server to change it."
        )
    if source == "bundle":
        raise AdminConfigError("This address comes from your organization's Grist app in the configuration file.")


def _require_public_https_host(address: str) -> None:
    refusal = AdminConfigError("Use an https:// address on a public host name.")
    parts = urlsplit(address)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host:
        raise refusal
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise refusal
    try:
        found = socket.getaddrinfo(host, None)
    except OSError:
        raise refusal from None
    for entry in found:
        ip = ipaddress.ip_address(str(entry[4][0]).split("%")[0])
        if not ip.is_global:
            raise refusal


def _normalized_address(name: str, value: str, *, mode: Mode) -> str:
    if not value:
        return ""
    normalize = peek_client.normalize_base_url if name == "peek" else grist_auth.normalize_server_url
    try:
        address = normalize(value)
    except (peek_client.PeekClientError, grist_auth.GristClientError) as exc:
        raise AdminConfigError(str(exc)) from exc
    if mode == "org":
        _require_public_https_host(address)
    return address


def _endpoint(url: str) -> tuple[Any, ...]:
    try:
        parts = urlsplit(url)
        return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port)
    except ValueError:
        return (url,)


def _port(params: dict[str, Any]) -> int:
    port = _param(params, "syslog_port", int)
    if not 1 <= port <= 65535:
        raise AdminConfigError("Enter a port from 1 to 65535.")
    return port


def _apply_service_toggle(config: dict[str, Any], action: str, params: dict[str, Any]) -> tuple[str, list[str]]:
    name = _service_name(params)
    offered = action == "offer_service"
    service = config["services"][name]
    if service["offered"] == offered:
        return "", []
    service["offered"] = offered
    label = SERVICE_LABELS[name]
    return (f"Offered {label}" if offered else f"Stopped offering {label}"), ["services"]


def _apply_atlassian_assets(config: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str]]:
    enabled = _param(params, "enabled", bool)
    workspace_id = _text(params, "workspace_id") if "workspace_id" in params else ""
    if len(workspace_id) > 200:
        raise AdminConfigError("The workspace id is too long.")
    service = config["services"]["atlassian"]
    if service["assets"] == enabled and service["assets_workspace_id"] == workspace_id:
        return "", []
    service["assets"] = enabled
    service["assets_workspace_id"] = workspace_id
    return f"Set Jira Assets to {'on' if enabled else 'off'}", ["services"]


def _apply_server_address(
    config: dict[str, Any], params: dict[str, Any], bundle: dict[str, Any], *, mode: Mode
) -> tuple[str, list[str]]:
    name = _service_name(params, tuple(_ADDRESS_FIELD))
    value = _param(params, "value", str)
    address = _normalized_address(name, value, mode=mode)
    service = config["services"][name]
    _refuse_locked_address(name, _address_source(bundle, name, service, _env_addresses()))
    field = _ADDRESS_FIELD[name]
    if service[field] == address:
        return "", []
    service[field] = address
    label = SERVICE_LABELS[name]
    return (f"Set the {label} server to {address}" if address else f"Cleared the {label} server"), ["services"]


def _apply_auto_provisioning(
    config: dict[str, Any], params: dict[str, Any], bundle: dict[str, Any], *, mode: Mode, single_user: bool
) -> tuple[str, list[str]]:
    name = _service_name(params, tuple(_ADDRESS_FIELD))
    enabled = _param(params, "enabled", bool)
    trust = _param(params, "trust_unverified_email", bool)
    service = config["services"][name]
    if enabled:
        available, reason = _auto_provision_state(
            bundle, name, service, _env_addresses(), mode=mode, single_user=single_user
        )
        if not available:
            raise AdminConfigError(reason)
    stored = service["auto_provision"]
    if stored["enabled"] == enabled and stored["trust_unverified_email"] == trust:
        return "", []
    was_enabled = stored["enabled"]
    stored["enabled"], stored["trust_unverified_email"] = enabled, trust
    label = SERVICE_LABELS[name]
    if was_enabled != enabled:
        summary = f"Turned automatic {label} accounts {'on' if enabled else 'off'}"
    else:
        summary = f"Changed automatic {label} accounts"
    return summary, ["auto_provision"]


def _replace_section(config: dict[str, Any], key: str, candidate: dict[str, Any], summary: str) -> tuple[str, list[str]]:
    if config[key] == candidate:
        return "", []
    config[key] = candidate
    return summary, [key]


def _apply_download_delivery(config: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str]]:
    candidate = {
        "inline_max_bytes": _param(params, "inline_max_bytes", int),
        "link_ttl_seconds": _param(params, "link_ttl_seconds", int),
        "allow_disk_staging": _param(params, "allow_disk_staging", bool),
        "agent_links": _param(params, "agent_links", bool),
        "allow_exports": _param(params, "allow_exports", bool),
    }
    _validated(DownloadDeliveryConfig.from_org_config, "download_delivery", candidate)
    return _replace_section(config, "download_delivery", {**config["download_delivery"], **candidate}, "Changed file delivery")


def _validated(parser: Any, section: str, candidate: dict[str, Any]) -> None:
    try:
        parser({section: candidate})
    except ConfigurationError as exc:
        raise _parse_error(exc) from exc


def _apply_authz(config: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str]]:
    candidate = {
        "allowed_domains": _word_list(_param(params, "allowed_domains", str)),
        "groups_claim": _param(params, "groups_claim", str).strip(),
        "required_groups": _word_list(_param(params, "required_groups", str)),
    }
    _validated(AuthzPolicyConfig.from_org_config, "authz", candidate)
    return _replace_section(config, "authz", {**config["authz"], **candidate}, "Changed sign-in restrictions")


def _apply_web_push(config: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str]]:
    enabled = _param(params, "enabled", bool)
    candidate = {**config["web_push"], "enabled": enabled}
    _validated(WebPushConfig.from_org_config, "web_push", candidate)
    return _replace_section(config, "web_push", candidate, f"Turned web push {'on' if enabled else 'off'}")


def _apply_unattended_sessions(config: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str]]:
    enabled = _param(params, "enabled", bool)
    candidate = {**config["unattended_sessions"], "enabled": enabled}
    return _replace_section(
        config, "unattended_sessions", candidate, f"Turned unattended sessions {'on' if enabled else 'off'}"
    )


def _apply_tighten_step_up(config: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str]]:
    candidate = {
        "enabled": _param(params, "enabled", bool),
        "require_passkey": _param(params, "require_passkey", bool),
        "scope": _param(params, "scope", str),
    }
    _validated(StepUpConfig.from_org_config, "step_up", candidate)
    current = config["step_up"]
    new_rank, old_rank = _step_up_rank(candidate), _step_up_rank(current)
    if new_rank == old_rank:
        return "", []
    if not _step_up_not_weaker(candidate, current):
        raise AdminConfigError(_STEP_UP_REFUSAL)
    config["step_up"] = {**current, **candidate}
    return "Made step-up stricter", ["step_up"]


def _apply_scoped_tokens(config: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str]]:
    max_days = _param(params, "max_days", int)
    if not 1 <= max_days <= scoped_tokens.LOCAL_MAX_DAYS:
        raise AdminConfigError(f"Scoped tokens can last from 1 to {scoped_tokens.LOCAL_MAX_DAYS} days.")
    candidate = {
        **config["scoped_tokens"],
        "enabled": _param(params, "enabled", bool),
        "max_days": max_days,
        "allow_ai_proposals": _param(params, "allow_ai_proposals", bool),
    }
    return _replace_section(config, "scoped_tokens", candidate, "Changed scoped tokens")


def _apply_audit_forwarding(config: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str]]:
    current = config["audit_forwarding"]
    current_http = current.get("http") if isinstance(current.get("http"), dict) else {}
    current_syslog = current.get("syslog") if isinstance(current.get("syslog"), dict) else {}
    kind = _param(params, "kind", str)
    http_url = _param(params, "http_url", str).strip()
    previous_kind = current.get("kind", "syslog")
    previous_url = str(current_http.get("url", "") or "")
    # The bearer variable's name is never taken from the request. It is also dropped when the
    # forwarding target moves, so a token configured for one destination is not sent to another.
    bearer = str(current_http.get("bearer_token_env", "") or "")
    moved = kind != previous_kind or _endpoint(http_url) != _endpoint(previous_url)
    dropped = bool(bearer) and moved
    candidate = {
        **current,
        "enabled": _param(params, "enabled", bool),
        "kind": kind,
        "syslog": {
            **current_syslog,
            "host": _param(params, "syslog_host", str).strip(),
            "port": _port(params),
            "protocol": _param(params, "syslog_protocol", str),
        },
        "http": {**current_http, "url": http_url, "bearer_token_env": "" if moved else bearer},
    }
    _validated(AuditForwardingConfig.from_org_config, "audit_forwarding", candidate)
    summary = "Changed audit forwarding"
    if dropped:
        summary += "; the bearer token was removed. Set it again in org/admin_config.json."
    return _replace_section(config, "audit_forwarding", candidate, summary)


def apply_change(
    action: str, params: dict[str, Any], *, bundle: dict[str, Any], mode: Mode, single_user: bool
) -> tuple[str, list[str]]:
    """Validate and store one Admin-page change; returns ``(summary, changed_sections)``.

    Everything that is not a parameter comes from the caller, never the request: the dispatcher
    passes only the action's declared parameters. A change that alters nothing returns
    ``("", [])`` and writes nothing. The privacy actions are not handled here.
    """
    modes = _ACTION_MODES.get(action)
    if modes is None:
        raise AdminConfigError("Unknown action.")
    if mode not in modes:
        raise AdminConfigError("This setting is available on organization servers only.")
    with _write_lock:
        config = config_for(bundle, mode=mode, single_user=single_user)
        before = copy.deepcopy(config)
        if action in ("offer_service", "withdraw_service"):
            result = _apply_service_toggle(config, action, params)
        elif action == "set_atlassian_assets":
            result = _apply_atlassian_assets(config, params)
        elif action == "set_server_address":
            result = _apply_server_address(config, params, bundle, mode=mode)
        elif action == "set_auto_provisioning":
            result = _apply_auto_provisioning(config, params, bundle, mode=mode, single_user=single_user)
        elif action == "set_unattended_sessions":
            result = _apply_unattended_sessions(config, params)
        elif action == "clear_rooms":
            config["rooms"], config["rooms_synced_at"] = [], ""
            result = ("Cleared the room directory", ["rooms"]) if before["rooms"] or before["rooms_synced_at"] else ("", [])
        elif action == "set_download_delivery":
            result = _apply_download_delivery(config, params)
        elif action == "set_web_push":
            result = _apply_web_push(config, params)
        elif action == "set_authz":
            result = _apply_authz(config, params)
        elif action == "tighten_step_up":
            result = _apply_tighten_step_up(config, params)
        elif action == "set_scoped_tokens":
            result = _apply_scoped_tokens(config, params)
        else:
            result = _apply_audit_forwarding(config, params)
        config["org_server"] = before["org_server"]
        if result[0] and config != before:
            _write(config)
        return result


# --------------------------------------------------------------------------- #
# The room directory
# --------------------------------------------------------------------------- #


def _clean_text(value: str) -> bool:
    return len(value) <= MAX_ROOM_TEXT and not any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in value)


def _room_ok(room: Any) -> bool:
    if not isinstance(room, dict):
        return False
    email = room.get("resource_email")
    if not isinstance(email, str) or not email.strip():
        return False
    for key, value in room.items():
        if not isinstance(key, str) or not _clean_text(key):
            return False
        if isinstance(value, str):
            if not _clean_text(value):
                return False
        elif isinstance(value, float):
            if not math.isfinite(value):
                return False
        elif value is not None and not isinstance(value, (bool, int)):
            return False
    return True


def replace_rooms(data: Any, *, bundle: dict[str, Any], mode: Mode, single_user: bool) -> str:
    """Replace the room directory with the file ``sync_room_directory.py`` wrote.

    Room text reaches agents as tool output, so it is held to a narrow shape: flat objects of
    short strings without control characters, numbers, booleans and nulls.
    """
    if not isinstance(data, dict) or not isinstance(data.get("rooms"), list):
        raise AdminConfigError(_ROOMS_REFUSAL)
    rooms = data["rooms"]
    if len(rooms) > MAX_ROOMS:
        raise AdminConfigError(f"A room directory can hold at most {MAX_ROOMS} rooms.")
    synced_at = data.get("rooms_synced_at", "")
    if not isinstance(synced_at, str) or not _clean_text(synced_at) or not all(_room_ok(room) for room in rooms):
        raise AdminConfigError(_ROOMS_REFUSAL)
    with _write_lock:
        config = config_for(bundle, mode=mode, single_user=single_user)
        config["rooms"] = copy.deepcopy(rooms)
        config["rooms_synced_at"] = synced_at
        _write(config)
    return f"Replaced the room directory ({len(rooms)} rooms)"


# --------------------------------------------------------------------------- #
# The Admin page's service rows
# --------------------------------------------------------------------------- #


def _service_row(
    bundle: dict[str, Any], admin: dict[str, Any], name: str, env: dict[str, str], *, mode: Mode, single_user: bool
) -> dict[str, Any]:
    from . import oauth_apps

    stored = admin.get("services", {}).get(name, {})
    stored = stored if isinstance(stored, dict) else {}
    section = _section(bundle, name)
    app_source = "server"
    client_id = ""
    if name in oauth_apps.BUILTIN_KEY:
        app_source = oauth_apps.credentials_source(bundle, name, mode=mode)
        if app_source == "bundle":
            client_id = str(section.get("consumer_key" if name == "salesforce" else "client_id") or "")
        elif app_source == "builtin":
            app = app_credentials.builtin_app(oauth_apps.BUILTIN_KEY[name])
            client_id = str(app["client_id"]) if app else ""
    elif name == "telegram":
        app_source = "builtin" if app_credentials.telegram_app_credentials() is not None else "none"
    elif name == "grist":
        client_id = str(section.get("client_id") or "")

    options: dict[str, Any] = {}
    if name == "atlassian":
        options = {
            "assets": bool(stored.get("assets", False)),
            "assets_workspace_id": str(stored.get("assets_workspace_id", "") or ""),
        }
    elif name in _ADDRESS_FIELD:
        source = _address_source(bundle, name, stored, env)
        if source == "bundle":
            address = str(section.get("server_url") or "")
        else:
            address = env.get(name) or str(stored.get(_ADDRESS_FIELD[name], "") or "")
        available, reason = _auto_provision_state(bundle, name, stored, env, mode=mode, single_user=single_user)
        auto = stored.get("auto_provision", {})
        key_env = str(auto.get("key_env", "") or _DEFAULT_KEY_ENV[name])
        options = {
            _ADDRESS_FIELD[name]: address,
            "address_source": source,
            "key_env": key_env,
            "locked": source != "admin",
            "auto_provision": {
                "enabled": bool(auto.get("enabled", False)),
                "trust_unverified_email": bool(auto.get("trust_unverified_email", False)),
                "available": available,
                "reason": reason,
                "key_set": _provisioning_key_set(key_env),
            },
        }
    return {
        "key": name,
        "label": SERVICE_LABELS[name],
        "offered": bool(stored.get("offered", False)),
        "app_source": app_source,
        "client_id": client_id,
        "relay": mode == "org" and app_source == "builtin" and name != "telegram",
        "options": options,
    }


def signin_state(bundle: dict[str, Any]) -> dict[str, Any]:
    """What the Admin page's Sign-in section says about the identity provider: whether it is
    PrivacyFence's built-in Google sign-in and, if so, the admin addresses from the environment
    (shown read-only, since only the server's operator can change them)."""
    if not builtin_signin_selected(bundle):
        return {"builtin": False, "admin_emails": []}
    emails = [part.strip() for part in os.environ.get(ADMIN_EMAILS_ENV, "").split(",") if part.strip()]
    return {"builtin": True, "admin_emails": emails}


def service_rows(
    bundle: dict[str, Any], admin: dict[str, Any], *, mode: Mode, single_user: bool = False
) -> list[dict[str, Any]]:
    """One row per service for the Admin page. A row never carries a secret: not a client
    secret, not a provisioning key, only whether the key is set."""
    env = _env_addresses()
    return [_service_row(bundle, admin, name, env, mode=mode, single_user=single_user) for name in SERVICES]
