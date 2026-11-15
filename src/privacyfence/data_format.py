"""The data-format marker, and the start-up pass that earns it.

PrivacyFence 8 drops the code that upgrades data written by older releases. That is safe only
when every install has run a 7.x release that finished all of those upgrades for every person
and left proof. ``migrate_everything`` runs at every 7.x start, for every principal, and writes
``data_format.json`` in the data directory once nothing is left to convert. The file travels
with the data (and with its backups); 8.x reads its ``format`` key and nothing
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
MINIMUM_FORMAT = 7
DESKTOP_TRACES = ("web_base_url", "mcp_token")
PERSON_DATA = ("users", "org", "credentials", "authority")
MARKER_FILENAME = "data_format.json"
NOT_READY_PREFIX = "Not ready for PrivacyFence 8: "

_status: dict[str, Any] = {"ready": False, "problems": []}


def marker_path(root: Path | None = None) -> Path:
    return (paths.data_dir() if root is None else root) / MARKER_FILENAME


def read_marker(root: Path | None = None) -> dict[str, Any] | None:
    """The stored marker, or ``None`` when there is none. A file that cannot be read, is not a
    JSON object or has no integer ``format`` also gives ``None`` (with a warning), so the next
    successful pass writes a fresh one."""
    path = marker_path(root)
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


_DESKTOP_TEXT = (
    "This data directory ({root}) belongs to a PrivacyFence desktop install. PrivacyFence 8 runs only as an "
    "organization server or the Docker image and does not read desktop data. Keep using PrivacyFence 7 on this "
    "computer, or move to organization mode: https://privacyfence.eu/docs/moving-to-organization-mode/"
)
_CHECKPOINT_TEXT = (
    "This data directory ({root}) has not been prepared for PrivacyFence 8. Install the latest PrivacyFence 7, "
    'start it once, check that the Admin page says "Ready for PrivacyFence 8: yes", then start this release '
    "again: https://privacyfence.eu/docs/moving-to-organization-mode/"
)


def _says_org(root: Path) -> bool:
    """Whether ``org/org_bundle.json`` says ``"mode": "org"``; anything unreadable says no."""
    try:
        bundle = json.loads((root / "org" / "org_bundle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(bundle, dict) and bundle.get("mode") == "org"


def _present(path: Path) -> bool:
    """Whether anything is at ``path``, counting a symlink whose target is gone."""
    return path.is_symlink() or path.exists()


def _holds_data(path: Path) -> bool:
    """Whether ``path`` holds anything: a file, a link (even a dangling one) or a non-empty
    directory. The Docker image creates its data directories empty, and an empty one holds no
    person data."""
    if path.is_symlink() or path.is_file():
        return True
    return path.is_dir() and any(path.iterdir())


def check_upgrade_source(root: Path) -> None:
    """Refuse a data directory 8.x cannot trust (ADR 0302); write the marker into a fresh one.

    In order: data a newer release has moved on; desktop-install traces outside an organization
    server; data that 7.x has not marked as fully upgraded. A data directory that cannot be
    inspected, or a fresh one the marker cannot be written into, is refused as well."""
    marker = read_marker(root)
    check_not_newer(marker)

    try:
        traces = [name for name in DESKTOP_TRACES if _present(root / name) or _present(root / "authority" / name)]
        has_person_data = any(_holds_data(root / name) for name in PERSON_DATA)
    except OSError as exc:
        raise ConfigurationError(f"The data directory ({root}) could not be inspected: {exc}") from exc
    if traces and not _says_org(root):
        raise ConfigurationError(_DESKTOP_TEXT.format(root=root))

    if marker is None or marker["format"] < MINIMUM_FORMAT:
        if has_person_data:
            raise ConfigurationError(_CHECKPOINT_TEXT.format(root=root))
        path = marker_path(root)
        try:
            atomic_write_json(path, {
                "format": DATA_FORMAT,
                "written_by": __version__,
                "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        except OSError as exc:
            raise ConfigurationError(
                f"The data-format marker could not be written to {path}: {exc}. "
                "The data directory must be writable."
            ) from exc


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
