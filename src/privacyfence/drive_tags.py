"""Drive file tags: names, parsing and the Drive ``properties`` encoding.

A tag is stored on a Drive file as a ``properties`` entry named
``privacyfence-tag.<name>`` with the value ``1``. This module holds the pure
helpers that turn user input into tag names and a file's raw ``properties``
into the list of PrivacyFence tags. It does no I/O. Properties written by any
other app, and tag properties with an unexpected value or name, are ignored and
never exposed.
"""

from __future__ import annotations

import re

TAG_PROPERTY_PREFIX = "privacyfence-tag."
TAG_VALUE = "1"
TAG_NAME_RE = r"[a-z0-9][a-z0-9._-]{0,39}"
MAX_TAGS_PER_CALL = 10

_TAG_NAME_PATTERN = re.compile(TAG_NAME_RE)


def normalise_tag(raw: str) -> str:
    """Strip and lowercase ``raw``; raise ``ValueError`` if it is not a valid tag name."""
    name = raw.strip().lower()
    if not _TAG_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"{raw!r} is not a valid tag name. Use lowercase letters, digits, '.', '_' "
            "and '-', 1 to 40 characters, starting with a letter or digit."
        )
    return name


def parse_tag_list(text: str) -> list[str]:
    """Split a comma-separated list into normalised, de-duplicated tag names (order kept)."""
    names: list[str] = []
    for item in text.split(","):
        if not item.strip():
            continue
        name = normalise_tag(item)
        if name not in names:
            names.append(name)
    if len(names) > MAX_TAGS_PER_CALL:
        raise ValueError(f"At most {MAX_TAGS_PER_CALL} tags per call.")
    return names


def tag_property_key(name: str) -> str:
    """The Drive property key that stores tag ``name``."""
    return TAG_PROPERTY_PREFIX + name


def tags_from_properties(properties: dict[str, str] | None) -> list[str]:
    """Sorted PrivacyFence tag names found in a file's raw ``properties``."""
    tags: list[str] = []
    for key, value in (properties or {}).items():
        if not key.startswith(TAG_PROPERTY_PREFIX) or value != TAG_VALUE:
            continue
        name = key[len(TAG_PROPERTY_PREFIX):]
        if _TAG_NAME_PATTERN.fullmatch(name):
            tags.append(name)
    return sorted(tags)
