"""The data-format marker, and the start-up pass that earns it.

PrivacyFence 8 drops the code that upgrades data written by older releases. That is safe only
when every install has run a 7.x release that finished all of those upgrades for every person
and left proof. ``migrate_everything`` runs at every 7.x start, for every principal, and writes
``data_format.json`` in the data directory once nothing is left to convert. The file travels
with the data (and with its backups); 8.x and its installers read its ``format`` key and nothing
else, so that key never changes shape.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from . import __version__, admin_config, paths, policy_upgrade, scoped_tokens
from .org_mode import ConfigurationError
from .policy import store as policy_store
from .principal import Principal, principal_scope
from .secure_files import atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)

DATA_FORMAT = 7
MARKER_FILENAME = "data_format.json"
NOT_READY_PREFIX = "Not ready for PrivacyFence 8: "

_status: dict[str, Any] = {"ready": False, "problems": []}


def marker_path() -> Path:
    return paths.data_dir() / MARKER_FILENAME


def read_marker() -> dict[str, Any] | None:
    """The stored marker, or ``None`` when there is none. A file that cannot be read, is not a
    JSON object or has no integer ``format`` also gives ``None`` (with a warning), so the next
    successful pass writes a fresh one."""
    path = marker_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("%s could not be read, so it will be written again: %s", path, exc)
        return None
    try:
        marker = json.loads(text)
    except ValueError:
        marker = None
    if not isinstance(marker, dict) or not isinstance(marker.get("format"), int) or isinstance(marker["format"], bool):
        logger.warning("%s is not a valid data-format marker, so it will be written again", path)
        return None
    return marker


def check_not_newer(marker: dict[str, Any] | None) -> None:
    """Refuse data that a newer release has already moved on: this release cannot read it."""
    if marker is None or marker["format"] <= DATA_FORMAT:
        return
    written_by = marker.get("written_by")
    raise ConfigurationError(
        f"This data was last used by PrivacyFence {written_by} (data format {marker['format']}). "
        f"This release, {__version__}, cannot read it. Install {written_by} or newer."
    )


def status() -> dict[str, Any]:
    """``{"ready": bool, "problems": list[str]}`` from the last ``migrate_everything`` run."""
    return {"ready": _status["ready"], "problems": list(_status["problems"])}


def reset_for_tests() -> None:
    _status["ready"] = False
    _status["problems"] = []


def _read_mapping(path: str) -> dict[str, Any] | None:
    """The mapping in a YAML file, ``{}`` for an empty one, ``None`` when it is something else."""
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    return config if isinstance(config, dict) else None


def _unfinished_tokens(principal: Principal) -> list[str]:
    """Problems for the principal's ``scoped_tokens.json``: unreadable, or an entry that still
    only names rules."""
    path = paths.authority_dir(principal) / scoped_tokens.STORE_FILE_NAME
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["tokens"]
        if not isinstance(entries, list):
            raise TypeError("tokens must be a list")
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError):
        return [f"{principal.id}: scoped_tokens.json cannot be read"]
    return [
        f"{principal.id}: scoped token {entry.get('id', 'without an id')} still names rules"
        for entry in entries
        if isinstance(entry, dict) and "rule_ids" in entry and "rules" not in entry
    ]


def _migrate_principal(principal: Principal, log: Callable[[str], None]) -> list[str]:
    from . import daemon_main  # function-local: daemon_main imports this module

    pid = principal.id
    with principal_scope(principal):
        resolved = daemon_main._resolve_authority_path("config/settings.yaml")
        config: dict[str, Any] = {}
        if os.path.exists(resolved):
            try:
                loaded = _read_mapping(resolved)
            except (OSError, ValueError, yaml.YAMLError):
                return [f"{pid}: settings.yaml cannot be read"]
            if loaded is None:
                return [f"{pid}: settings.yaml is not a mapping"]
            config = loaded

            if policy_store.drop_converted_v1_sections(config):
                try:
                    atomic_write_text(resolved, yaml.safe_dump(config, default_flow_style=False, allow_unicode=True))
                except OSError:
                    return [f"{pid}: could not rewrite settings.yaml"]
            try:
                policy_store.reject_v1_sections(config, resolved)
            except policy_store.V1PolicyConfigError:
                return [f"{pid}: settings.yaml still has v1 policy sections"]

            policy_upgrade.ensure_scope_rows(config, resolved, principal, log=log)
            # ensure_scope_rows reports success after a failed write, so what is on disk decides.
            try:
                reread = _read_mapping(resolved)
            except (OSError, ValueError, yaml.YAMLError):
                return [f"{pid}: settings.yaml cannot be read"]
            if reread is None:
                return [f"{pid}: settings.yaml is not a mapping"]
            if policy_upgrade._v2_rules(reread.get("auto_accept")) is not None:
                return [f"{pid}: rules are still in the version-2 format"]
            config = reread

        policy_upgrade.ensure_token_rules(config, principal, log=log)
        problems = _unfinished_tokens(principal)
        problems.extend(policy_upgrade.ensure_canonical_verbs(config, resolved, principal))
        return problems


def migrate_everything(*, log: Callable[[str], None]) -> list[str]:
    """Bring every principal's data to the current format and, when nothing is left over, record
    the marker. Returns the problems found (empty when everything succeeded); a problem with one
    person's files never stops the others and never raises. Problems are logged through ``log``
    as they are found, and the marker is written only when there are none."""
    from .web.mcp_auth import scoped_token_principals

    problems: list[str] = []
    try:
        principals = list(scoped_token_principals())
    except OSError:
        principals = []
        problems.append("the users directory cannot be read")
    for principal in principals:
        try:
            problems.extend(_migrate_principal(principal, log))
        except Exception:  # noqa: BLE001 -- one person's data must not stop the start
            logger.exception("Migrating %s failed", principal.id)
            problems.append(f"{principal.id}: could not be migrated")

    try:
        admin_present = admin_config.load() is not None
    except admin_config.AdminConfigError:
        admin_present = False
    if not admin_present:
        problems.append("the admin config has not been created")

    if not problems:
        marker = read_marker()
        if marker is None or marker["format"] < DATA_FORMAT:
            try:
                atomic_write_json(marker_path(), {
                    "format": DATA_FORMAT,
                    "written_by": __version__,
                    "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
            except OSError:
                problems.append("the data-format marker could not be written")

    for problem in problems:
        log(f"{NOT_READY_PREFIX}{problem}")
    _status["ready"] = not problems
    _status["problems"] = list(problems)
    return problems
