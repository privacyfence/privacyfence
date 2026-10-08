"""Where plugins live, how they are found, and whether they can be trusted to run.

A plugin is trusted code (ADR 0121): an administrator installs it into a directory only
administrators can write, outside both the installed app (which upgrades replace) and the service
account's data root (which the service account can write). Before every start the host checks that
nothing but an administrator can rewrite the executable, the plugin's directory or any directory
above it, and it records sha256 hashes of the executable and the manifest at enable so a later
change disables the plugin. The directory is deliberately not configurable: a configurable path
could point somewhere the user, and therefore the AI client, can write.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from privacyfence import privilege_separation
from privacyfence.plugins.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    load_manifest,
    resolve_command,
)

logger = logging.getLogger(__name__)

LINUX_PLUGINS_DIR = Path("/usr/local/lib/privacyfence/plugins")
MACOS_PLUGINS_DIR = Path("/Library/PrivacyFence/plugins")
WINDOWS_PLUGINS_DIR_NAME = "PrivacyFence Plugins"
_WINDOWS_PROGRAM_FILES_FALLBACK = "C:\\Program Files"

PROTOCOL_MAJOR_MISMATCH = "protocol major mismatch"
NOT_ADMIN_ONLY = "executable is writable by non-administrators"
SYMLINKED_PLUGIN_DIR = "plugin directory is a symbolic link"
OUTSIDE_PLUGINS_DIR = "executable is outside the plugins directory"
_SUPPORTED_PROTOCOL = "1"

_HASH_BLOCK_BYTES = 1024 * 1024


def plugins_dir() -> Path:
    """This platform's administrator-only plugins directory."""
    platform = privilege_separation.current_platform()
    if platform == "win32":
        program_files = os.environ.get("ProgramFiles") or _WINDOWS_PROGRAM_FILES_FALLBACK
        return Path(program_files) / WINDOWS_PLUGINS_DIR_NAME
    if platform == "darwin":
        return MACOS_PLUGINS_DIR
    return LINUX_PLUGINS_DIR


@dataclass(frozen=True)
class DiscoveredPlugin:
    """One subdirectory of the plugins directory, as found on disk.

    ``problem`` is a reason string Settings shows as is, or ``None`` when the plugin may be
    enabled. A hash is ``""`` when its file could not be read.
    """

    dir_name: str
    path: Path
    manifest: Manifest | None
    problem: str | None
    executable_sha256: str
    manifest_sha256: str


def sha256_file(path: Path) -> str:
    """Hex sha256 of ``path``, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _sha256_or_empty(path: Path) -> str:
    try:
        return sha256_file(path)
    except OSError as exc:
        logger.warning("Could not hash %s: %s", path, exc)
        return ""


_STRICT = "strict"
_INSIDE = "inside"
_ANCESTOR = "ancestor"


def _paths_to_check(plugin_dir: Path, executable: Path) -> list[tuple[Path, str]]:
    """The executable, any directory between it and the plugin directory, the plugin directory,
    then every directory above it up to and including the filesystem root, each paired with the
    rule it is held to: strict for the executable, the plugin directory's for the directories up to
    and including it, the ancestors' above that.

    Real paths: a symlink anywhere on the way is followed, so the directories checked are the ones
    the operating system actually walks to reach the file it runs.
    """
    real_dir = plugin_dir.resolve()
    real_exe = executable.resolve()
    between = [p for p in real_exe.parents if real_dir in p.parents]
    return [
        (executable, _STRICT),
        *((path, _INSIDE) for path in (*between, real_dir)),
        *((path, _ANCESTOR) for path in real_dir.parents),
    ]


def admin_only_problem(plugin_dir: Path, executable: Path) -> str | None:
    """The first path that someone other than an administrator can rewrite, as a
    human-readable problem, or ``None`` when every one of them is administrator-only.

    Checked in order: the executable, the directories between it and ``plugin_dir``,
    ``plugin_dir``, then every ancestor of ``plugin_dir`` up to and including the filesystem
    root. Any one of them writable by the user would let the user, and so the AI client, swap
    the code the service account runs (ADR 0058, ADR 0121).

    The executable is held to the strict rule. On Windows every directory, from the one holding
    the executable up to the filesystem root, ignores inherit-only entries, which grant nothing
    on the directory carrying them (every folder under ``%ProgramFiles%`` inherits one for
    ``CREATOR OWNER``; what it grants a child is checked on that child). The ancestors of
    ``plugin_dir`` also ignore the create-folder right a default ``C:\\`` gives every signed-in
    user, since they only have to be safe from having an entry renamed, replaced or deleted. On
    POSIX all three rules are the same. A directory the strict rule refuses is asked again under
    its narrower rule, and still reported with the strict reason when that refuses it too.
    """
    narrower = {
        _INSIDE: privilege_separation.admin_only_plugin_dir_write_problem,
        _ANCESTOR: privilege_separation.admin_only_ancestor_write_problem,
    }
    for path, rule in _paths_to_check(plugin_dir, executable):
        problem = privilege_separation.admin_only_write_problem(path)
        if problem is not None and rule in narrower and narrower[rule](path) is None:
            problem = None
        if problem is not None:
            return problem
    return None


def _inspect(
    plugins_dir: Path, entry_path: Path, trust_check: Callable[[Path, Path], str | None],
) -> DiscoveredPlugin:
    name = entry_path.name
    manifest_sha256 = _sha256_or_empty(entry_path / MANIFEST_FILENAME)
    try:
        manifest = load_manifest(entry_path)
        executable = Path(resolve_command(manifest, entry_path)[0])
        if executable == entry_path.resolve():
            raise ManifestError("command[0] resolves to the plugin directory itself")
    except ManifestError as exc:
        return DiscoveredPlugin(name, entry_path, None, f"manifest invalid: {exc}", "", manifest_sha256)
    executable_sha256 = _sha256_or_empty(executable)
    problem: str | None = None
    if plugins_dir.resolve() not in executable.parents:
        # A junction, or anything else that leads out of the plugins directory, would leave the
        # directory holding it out of the checked chain.
        problem = OUTSIDE_PLUGINS_DIR
    elif manifest.protocol != _SUPPORTED_PROTOCOL:
        problem = PROTOCOL_MAJOR_MISMATCH
    else:
        detail = trust_check(entry_path, executable)
        if detail is not None:
            logger.warning("Plugin %s is not administrator-only: %s", name, detail)
            problem = NOT_ADMIN_ONLY
    return DiscoveredPlugin(name, entry_path, manifest, problem, executable_sha256, manifest_sha256)


def discover(
    plugins_dir: Path,
    *,
    trust_check: Callable[[Path, Path], str | None] = admin_only_problem,
) -> list[DiscoveredPlugin]:
    """Every plugin in ``plugins_dir``, sorted by directory name.

    Each immediate subdirectory is one plugin; hidden (``.``-prefixed) entries and plain files
    are skipped. A symbolic link to a directory is listed but refused: following it would check
    the directories above its target instead of the plugins directory that holds the link.
    Raises ``OSError`` when ``plugins_dir`` itself cannot be listed, so a caller can tell "no
    plugins" from "could not look" and never treats a failed listing as an uninstall.
    """
    with os.scandir(plugins_dir) as entries:
        listed = sorted(
            ((Path(entry.path), entry.is_symlink()) for entry in entries
             if not entry.name.startswith(".") and entry.is_dir()),
            key=lambda item: item[0].name,
        )
    return [
        DiscoveredPlugin(path.name, path, None, SYMLINKED_PLUGIN_DIR, "", "") if symlink
        else _inspect(plugins_dir, path, trust_check)
        for path, symlink in listed
    ]
