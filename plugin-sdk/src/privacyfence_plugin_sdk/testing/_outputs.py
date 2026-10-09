"""Output files as the daemon lists them: which files of a plugin's output folder an agent can see."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Copied from the protocol, like the limits in plugin.py. The daemon's own tests compare them.
OUTPUT_TYPES: dict[str, tuple[str, ...]] = {
    "application/json": (".json",),
    "text/csv": (".csv",),
    "text/html": (".html", ".htm"),
    "text/plain": (".txt",),
    "text/markdown": (".md",),
}
DEFAULT_OUTPUT_TYPES = ("application/json", "text/csv")
OUTPUT_MAX_DEPTH = 8


@dataclass(frozen=True)
class OutputFile:
    """A published file, as ``plugin_outputs_list`` reports it. ``modified`` is RFC 3339 UTC."""

    path: str
    size: int
    modified: str
    mime_type: str


def check_types(output_types: object) -> tuple[str, ...]:
    if not isinstance(output_types, (tuple, list)) or not output_types:
        raise ValueError("output_types must be a non-empty list of MIME types")
    unknown = [t for t in output_types if t not in OUTPUT_TYPES]
    if unknown:
        raise ValueError(f"unknown output type {unknown[0]!r}; expected one of {', '.join(OUTPUT_TYPES)}")
    return tuple(dict.fromkeys(output_types))


def list_outputs(root: Path, output_types: tuple[str, ...], prefix: str = "") -> list[OutputFile]:
    """Published files sorted by path that start with ``prefix``.

    A file is published when it is a regular file (never a symlink) at most ``OUTPUT_MAX_DEPTH``
    segments down, with no dot-prefixed segment (a ``.name.tmp`` the plugin has not renamed yet) and an
    extension of one of ``output_types``.
    """
    suffixes = {ext: mime for mime in output_types for ext in OUTPUT_TYPES.get(mime, ())}
    try:
        base = root.resolve(strict=True)
    except OSError:
        return []
    found: list[OutputFile] = []

    def walk(directory: str, parts: tuple[str, ...]) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            rel = (*parts, entry.name)
            if len(rel) > OUTPUT_MAX_DEPTH:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                walk(entry.path, rel)
            elif stat.S_ISREG(info.st_mode):
                mime = suffixes.get(os.path.splitext(entry.name)[1].lower())
                if mime is not None:
                    modified = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
                    found.append(OutputFile(
                        "/".join(rel), info.st_size,
                        modified.isoformat(timespec="seconds").replace("+00:00", "Z"), mime,
                    ))

    walk(str(base), ())
    found.sort(key=lambda f: f.path)
    return [f for f in found if f.path.startswith(prefix)]
