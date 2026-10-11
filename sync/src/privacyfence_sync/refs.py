"""File and value references in a recipe. Pure: no I/O of its own."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

VALUE_REF = re.compile(r"\$\{([a-z0-9_-]+(?:\.[a-z0-9_-]+)?):([A-Za-z0-9_.-]+)\}")
FILE_PLACEHOLDER = re.compile(r"\{(file|output):([a-z0-9_.-]+)\}|\{(work_dir|recipe_dir)\}")

_INDEX = re.compile(r"[0-9]+")


class RefError(ValueError):
    """A reference points at something that does not exist."""


def lookup(data: Any, path: str) -> Any:
    """Walk a dotted path through dicts (by key) and lists (by all-digit index)."""
    current = data
    for part in path.split("."):
        if isinstance(current, list) and _INDEX.fullmatch(part):
            index = int(part)
            if index >= len(current):
                raise RefError(f"{path} not found")
            current = current[index]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise RefError(f"{path} not found")
    return current


def _text(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def substitute_value_refs(value: Any, read_json: Callable[[str], Any]) -> Any:
    """Replace ``${ref:path}`` in every string of ``value``.

    A string that is exactly one reference becomes the looked-up value, keeping its JSON type;
    references inside longer text are inserted as text.
    """
    if isinstance(value, str):
        whole = VALUE_REF.fullmatch(value)
        if whole:
            return lookup(read_json(whole.group(1)), whole.group(2))
        return VALUE_REF.sub(lambda m: _text(lookup(read_json(m.group(1)), m.group(2))), value)
    if isinstance(value, list):
        return [substitute_value_refs(item, read_json) for item in value]
    if isinstance(value, dict):
        return {key: substitute_value_refs(item, read_json) for key, item in value.items()}
    return value


def expand_run_arg(
    arg: str,
    *,
    files: dict[str, Path],
    outputs: dict[str, Path],
    work_dir: Path,
    recipe_dir: Path,
    read_json: Callable[[str], Any],
) -> str:
    """Expand the placeholders and value references of one script argument into one argv element."""

    def placeholder(match: re.Match[str]) -> str:
        kind, name, simple = match.group(1), match.group(2), match.group(3)
        if simple == "work_dir":
            return str(work_dir.absolute())
        if simple == "recipe_dir":
            return str(recipe_dir.absolute())
        table = files if kind == "file" else outputs
        if name not in table:
            raise RefError(f"{kind}:{name} not found")
        return str(table[name].absolute())

    expanded = FILE_PLACEHOLDER.sub(placeholder, arg)
    return VALUE_REF.sub(lambda m: _text(lookup(read_json(m.group(1)), m.group(2))), expanded)
