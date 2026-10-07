"""The plugin manifest ``privacyfence-plugin.yaml``.

Loaded with ``yaml.safe_load``; an unknown key is an error, and so is every field that does not
meet its rule. Whatever the manifest says is only a claim: what a plugin may actually do is still
decided by the human who enables it and by the gate.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from privacyfence.plugins.blocks import clean_text
from privacyfence.plugins.constants import (
    PLUGIN_NAME_RE,
    RESERVED_PLUGIN_NAMES,
    SOURCE_OPERATIONS,
)

MANIFEST_FILENAME = "privacyfence-plugin.yaml"

_SEMVER_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?")
_KEYS = {"name", "display_name", "version", "protocol", "command", "source_operations", "tools",
         "max_gate_floor", "pages", "service_credentials"}
_REQUIRED = {"name", "display_name", "version", "protocol", "command", "tools"}
_MAX_MANIFEST_BYTES = 64 * 1024


class ManifestError(ValueError):
    """The manifest is missing, unreadable or invalid."""


@dataclass(frozen=True)
class Manifest:
    name: str
    display_name: str
    version: str
    protocol: str
    command: tuple[str, ...]
    source_operations: frozenset[str]
    max_gate_floor: str
    pages: bool
    service_credentials: bool


def _str(data: dict, key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ManifestError(f"{key} must be a string")
    return value


def _bool(data: dict, key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise ManifestError(f"{key} must be true or false")
    return value


def load_manifest(plugin_dir: Path, *, mode: str = "local") -> Manifest:
    path = plugin_dir / MANIFEST_FILENAME
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ManifestError(f"{MANIFEST_FILENAME} is larger than {_MAX_MANIFEST_BYTES} bytes")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"{MANIFEST_FILENAME} not found") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{MANIFEST_FILENAME} cannot be read: {exc}") from None
    except yaml.YAMLError as exc:
        raise ManifestError(f"{MANIFEST_FILENAME} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ManifestError("the manifest must be a mapping")
    unknown = [k for k in data if k not in _KEYS]
    if unknown:
        raise ManifestError(f"unknown key {str(unknown[0])!r}")
    for key in sorted(_REQUIRED - data.keys()):
        raise ManifestError(f"{key} is required")

    name = _str(data, "name")
    if not PLUGIN_NAME_RE.fullmatch(name):
        raise ManifestError(f"name {name!r} does not match {PLUGIN_NAME_RE.pattern}")
    if name in RESERVED_PLUGIN_NAMES:
        raise ManifestError(f"name {name!r} is reserved")
    if name != plugin_dir.name:
        raise ManifestError(f"name {name!r} must equal the directory name {plugin_dir.name!r}")

    display_name = _str(data, "display_name")
    if clean_text(display_name) != display_name:
        raise ManifestError("display_name must not contain control or bidirectional characters")
    if not 1 <= len(display_name) <= 60:
        raise ManifestError("display_name must be 1 to 60 characters")
    version = _str(data, "version")
    if not _SEMVER_RE.fullmatch(version):
        raise ManifestError(f"version {version!r} is not MAJOR.MINOR.PATCH")
    protocol = _str(data, "protocol")
    if not re.fullmatch(r"[0-9]+", protocol):
        raise ManifestError("protocol must be a major version string such as \"1\"")

    command = data["command"]
    if (not isinstance(command, list) or not command
            or not all(isinstance(c, str) and c for c in command)):
        raise ManifestError("command must be a non-empty list of non-empty strings")

    ops = data.get("source_operations", [])
    if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops):
        raise ManifestError("source_operations must be a list of strings")
    for op in ops:
        if op not in SOURCE_OPERATIONS:
            raise ManifestError(f"source operation {op!r} is not supported")

    if data["tools"] != "dynamic":
        raise ManifestError("tools must be \"dynamic\"")
    floor = data.get("max_gate_floor", "review")
    if floor not in ("review", "auto"):
        raise ManifestError("max_gate_floor must be \"review\" or \"auto\"")
    pages = _bool(data, "pages")
    service_credentials = _bool(data, "service_credentials")
    if service_credentials and mode == "local":
        raise ManifestError("service_credentials is not allowed in local mode")

    return Manifest(
        name=name, display_name=display_name, version=version, protocol=protocol,
        command=tuple(command), source_operations=frozenset(ops), max_gate_floor=floor,
        pages=pages, service_credentials=service_credentials,
    )


def resolve_command(manifest: Manifest, plugin_dir: Path) -> list[str]:
    """Absolute argv for the plugin; ``command[0]`` must resolve inside ``plugin_dir``."""
    root = plugin_dir.resolve()
    named = plugin_dir / manifest.command[0]
    if sys.platform == "win32" and not named.suffix and not os.path.lexists(named):
        named = named.with_name(named.name + ".exe")
    exe = named.resolve()
    if exe != root and root not in exe.parents:
        raise ManifestError("command[0] resolves outside the plugin directory")
    return [str(exe), *manifest.command[1:]]
