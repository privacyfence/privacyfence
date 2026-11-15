"""What's new after an upgrade (organization mode, ADR 0238).

Reads the changelog that ships with PrivacyFence (``privacyfence/resources/CHANGELOG.md`` in an
installed wheel, ADR 0237; the repository's ``CHANGELOG.md`` in a source checkout) and keeps, per
person, the last version they were shown the release notes for. It has no web imports: the strip,
the page and the routes live in ``web_shell`` and ``web/routes_whats_new``.

Version ordering follows ``parse_version`` -- a stable release outranks every
pre-release of the same ``major.minor.patch``, and a section whose heading does not parse is
dropped. A pre-release has no section of its own (its entries sit in ``[Unreleased]`` of the tree
it was tagged from), so for a pre-release the ``[Unreleased]`` section is shown first. A stable
running version never shows ``[Unreleased]``.

The changelog is read only *for* a known version -- "which sections are newer than the one this
person saw, up to the one running" -- never to *determine* a version, which ADR 0023 and rule 2 of
``CHANGELOG.md`` forbid.

``read_last_seen``, ``mark_seen`` and ``pending_version`` never raise: a page view must not fail
because the seen state could not be read or saved. They never log file contents.
"""
from __future__ import annotations

import functools
import json
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import __version__, paths, secure_files
from .principal import Principal

logger = logging.getLogger(__name__)

# Pre-release stage -> sort rank. A missing suffix (stable) is STABLE_RANK, the highest rank -- a
# stable tag always outranks every pre-release of the same major.minor.patch. Unlike the old
# dashed/spelled-out scheme, an unrecognized suffix here isn't silently treated as stable: it means
# the whole string fails to match _VERSION_RE (see below), so parse_version() returns None and the
# caller ignores it -- these tags are only ever produced by setuptools_scm or a human directly
# writing one, both of which stick to this exact scheme, so "recognized" and "well-formed" coincide.
_STAGE_RANK: dict[str, int] = {"a": 1, "b": 2, "rc": 3}
STABLE_RANK = 4

# Matches an exact release tag ("4.0.0", "4.0.0a12", "4.1.0rc1") and a between-tags dev build
# ("4.0.1.dev3+gabc1234", optionally with a ".dYYYYMMDD" dirty-worktree suffix setuptools_scm adds
# after the +local part) -- see the module docstring. The trailing "(?:\+.*)?" swallows that whole
# +local segment; it plays no part in ranking.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?(?:\+.*)?$")


def parse_version(raw: str) -> tuple[int, int, int, int, int, float] | None:
    """Parse a `major.minor.patch[<stage><n>][.dev<n>][+local]` version/tag string, tolerating a
    leading 'v'. Returns (major, minor, patch, stage_rank, stage_num, dev_num) -- directly
    tuple-comparable for correct precedence, e.g. 2.2.0b1 < 2.2.0rc1 < 2.2.0, and
    2.2.0rc2.dev1 (a dev build guessing at the *next* rc) sorts between 2.2.0rc1 and 2.2.0rc2.
    dev_num is +inf for anything that isn't itself a dev build, so a finished release always outranks
    its own dev builds. Returns None (never raises) if the string doesn't match at all."""
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    stage_name = m.group(4)
    stage_num = int(m.group(5)) if m.group(5) else 0
    stage_rank = _STAGE_RANK[stage_name] if stage_name else STABLE_RANK
    dev_num = int(m.group(6)) if m.group(6) else math.inf
    return (major, minor, patch, stage_rank, stage_num, dev_num)


def is_newer(remote_version: str, local_version: str | None = None) -> bool:
    """True iff remote_version's parsed tuple is strictly greater than local_version's (module's
    own __version__ by default -- looked up at call time, not baked into the signature at import
    time, so it stays correct as new tags get pushed and is monkeypatch-friendly in tests). A local
    dev build (`__version__` between release tags, e.g. `4.0.1.dev3+gabc1234` -- see the module
    docstring) ranks below the release it's guessing at, so a not-yet-tagged dev build never
    false-positives against its own upcoming release but does correctly report "newer" once any
    tagged release ships."""
    if local_version is None:
        local_version = __version__
    remote = parse_version(remote_version)
    local = parse_version(local_version)
    if remote is None or local is None:
        return False
    return remote > local

STATE_FILENAME = "whats_new.json"  # in paths.user_dir(principal); {"last_seen_version": "6.9.1"}
UNRELEASED = "Unreleased"
MAX_SECTIONS = 10  # the page shows at most this many (web/routes_whats_new.py)

_PACKAGE_DIR = Path(__file__).resolve().parent  # test seam for changelog_path()
_REPO_ROOT = _PACKAGE_DIR.parents[1]  # test seam for changelog_path()

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(r"^##\s+\[([^\]]+)\](?:\s*[—–-]+\s*(\S.*?))?\s*$")
_LINK_DEFINITION_RE = re.compile(r"^\[[^\]]+\]:\s")


@dataclass(frozen=True)
class ReleaseNotes:
    version: str  # "6.9.1", or UNRELEASED
    date: str  # "2026-10-26", or "" when the heading has none
    body: str  # the section's Markdown, continuation lines joined, no "## [x]" heading


def changelog_path() -> Path | None:
    """The bundled changelog (an installed wheel), else the repository's (a source checkout or
    editable install), else None. Reads the module constants at call time so tests can point them
    at a temporary directory."""
    for candidate in (_PACKAGE_DIR / "resources" / "CHANGELOG.md", _REPO_ROOT / "CHANGELOG.md"):
        if candidate.is_file():
            return candidate
    return None


def state_path(principal: Principal) -> Path:
    """Where this person's seen version lives. The one place that decides it, and the seam the
    root test fixture patches so no test writes into the checkout."""
    return paths.user_dir(principal) / STATE_FILENAME


def _finish(version: str, date: str, lines: list[str]) -> ReleaseNotes | None:
    joined: list[str] = []
    for line in lines:
        if joined and joined[-1].strip() and line.strip() and line[0] in " \t":
            joined[-1] += " " + line.strip()
        else:
            joined.append(line)
    body = "\n".join(joined).strip("\n")
    if version != UNRELEASED and parse_version(version) is None:
        return None
    if not any(line.strip() and not line.startswith("#") for line in body.splitlines()):
        return None
    return ReleaseNotes(version=version, date=date, body=body)


def parse_changelog(text: str) -> list[ReleaseNotes]:
    """Split a Keep-a-Changelog style file into its ``## [x]`` sections, in file order."""
    sections: list[ReleaseNotes] = []
    current: tuple[str, str] | None = None
    lines: list[str] = []

    def close() -> None:
        if current is not None:
            notes = _finish(current[0], current[1], lines)
            if notes is not None:
                sections.append(notes)

    for line in _COMMENT_RE.sub("", text).splitlines():
        if _LINK_DEFINITION_RE.match(line):
            break
        heading = _HEADING_RE.match(line)
        if heading or re.match(r"^##\s", line):
            close()
            current = (heading.group(1), heading.group(2) or "") if heading else None
            lines = []
        else:
            lines.append(line)
    close()
    return sections


@functools.lru_cache(maxsize=1)
def load_changelog() -> tuple[ReleaseNotes, ...]:
    """The parsed changelog, or () when none is bundled or it cannot be read."""
    path = changelog_path()
    if path is None:
        return ()
    try:
        return tuple(parse_changelog(path.read_text(encoding="utf-8")))
    except OSError as exc:
        logger.warning("Could not read the bundled changelog (%s)", type(exc).__name__)
        return ()


def running_version() -> str:
    import privacyfence

    return privacyfence.__version__


def is_prerelease(version: str) -> bool:
    parsed = parse_version(version)
    return parsed is not None and (parsed[3] != STABLE_RANK or parsed[5] != math.inf)


def notes_since(sections: Sequence[ReleaseNotes], last_seen: str | None, running: str) -> list[ReleaseNotes]:
    """Sections newer than ``last_seen`` (all of them when None) up to and including ``running``,
    newest first. A pre-release running version also gets ``[Unreleased]`` first. Not capped."""
    r = parse_version(running)
    if r is None:
        return []
    ls = parse_version(last_seen) if last_seen is not None else None
    stable: list[tuple[tuple[int, int, int, int, int, float], ReleaseNotes]] = []
    for section in sections:
        if section.version == UNRELEASED:
            continue
        pv = parse_version(section.version)
        if pv is not None and pv <= r and (ls is None or pv > ls):
            stable.append((pv, section))
    stable.sort(key=lambda item: item[0], reverse=True)
    result = [section for _, section in stable]
    if is_prerelease(running):
        unreleased = [section for section in sections if section.version == UNRELEASED]
        result = unreleased[:1] + result
    return result


def read_last_seen(principal: Principal) -> str | None:
    """The version this person last saw, or None (never seen, or the state is unusable). Never
    raises; an unusable file is logged without its contents."""
    try:
        path = state_path(principal)
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Could not read What's new state for %s (%s)", principal.id, type(exc).__name__)
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("What's new state for %s is not valid JSON", principal.id)
        return None
    value = data.get("last_seen_version") if isinstance(data, dict) else None
    if not isinstance(value, str) or parse_version(value) is None:
        logger.warning("What's new state for %s has an unexpected shape", principal.id)
        return None
    return value


def mark_seen(principal: Principal, version: str) -> None:
    """Record ``version`` as seen. Never moves the recorded version backwards, ignores a version
    that does not parse, and never raises."""
    if parse_version(version) is None:
        return
    current = read_last_seen(principal)
    if current is not None and not is_newer(version, current):
        return
    try:
        path = state_path(principal)
        secure_files.secure_mkdir(path.parent)
        secure_files.atomic_write_json(path, {"last_seen_version": version})
    except (OSError, ValueError) as exc:
        logger.warning("Could not save What's new state for %s (%s)", principal.id, type(exc).__name__)


def pending_version(principal: Principal) -> str:
    """The version the strip should announce to this person, or "". A person seen for the first
    time gets no news (their state is recorded instead). Never raises."""
    running = running_version()
    last = read_last_seen(principal)
    if last is None:
        mark_seen(principal, running)
        return ""
    if not is_newer(running, last):
        return ""
    return running if notes_since(load_changelog(), last, running) else ""
