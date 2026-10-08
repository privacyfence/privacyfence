"""Where a plugin keeps its files, and removing them (ADR 0120).

Two directories per plugin, both handed over at ``initialize`` and created ``0700`` first:

- install-wide: ``<data root>/plugin-data/<name>/shared``;
- per principal: ``<that principal's root>/plugin-data/<name>/user``.

The local principal's root is the data root itself, so the two stay distinct only by their last
component. ``remove_all`` is the one deletion path, shared by purge and uninstall, so neither can
leave a copy behind.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from privacyfence import paths
from privacyfence.principal import Principal
from privacyfence.secure_files import secure_mkdir

PLUGIN_DATA_DIRNAME = "plugin-data"
_SHARED = "shared"
_USER = "user"
_OUTPUTS = "outputs"


def install_dir(name: str) -> Path:
    return paths.data_dir() / PLUGIN_DATA_DIRNAME / name / _SHARED


def principal_dir(name: str, principal: Principal) -> Path:
    return paths.user_dir(principal) / PLUGIN_DATA_DIRNAME / name / _USER


def output_dir(name: str, principal: Principal) -> Path:
    """Where a plugin with ``outputs: true`` publishes files for one principal (ADR 0130).

    Under ``plugin-data/<name>``, so ``remove_all`` covers it. The caller creates it ``0700``.
    """
    return paths.user_dir(principal) / PLUGIN_DATA_DIRNAME / name / _OUTPUTS


def ensure_dirs(name: str, principals: list[Principal]) -> tuple[Path, dict[str, Path]]:
    """Create (or re-tighten) the install-wide directory and one per principal.

    Returns the install-wide path and a map from principal id to that principal's path.
    """
    shared = secure_mkdir(install_dir(name), 0o700)
    per_principal = {p.id: secure_mkdir(principal_dir(name, p), 0o700) for p in principals}
    return shared, per_principal


def _roots(name: str) -> list[Path]:
    data = paths.data_dir()
    roots = [data / PLUGIN_DATA_DIRNAME / name]
    users = data / "users"
    if users.is_dir():
        roots.extend(user / PLUGIN_DATA_DIRNAME / name for user in sorted(users.iterdir()) if user.is_dir())
    return roots


def remove_all(name: str) -> None:
    """Delete every directory a plugin owns, install-wide and per principal. Missing is fine.

    Other principals are found by listing ``<data root>/users``, so no principal is created by
    asking for its directory.
    """
    for root in _roots(name):
        if root.exists():
            shutil.rmtree(root)


__all__ = ["ensure_dirs", "install_dir", "output_dir", "principal_dir", "remove_all"]
