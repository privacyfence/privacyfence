#!/usr/bin/env python3
"""Print one version's section out of CHANGELOG.md.

This is what makes the GitHub Release body and the changelog the same text. `.github/workflows/
build.yml`'s `finalize-release` job runs this script once -- after `needs:` has proven every build
job succeeded -- and hands the result to `softprops/action-gh-release` as `body_path:` in the same
call that attaches the files, so the notes are written once, reviewed in the pull request that
writes them, and tag day involves no writing at all. (Through privacyfence/privacyfence#373 this
ran four times, once per attaching job, each rendering identical text.) Before this, the release
body was whatever GitHub's "generate release notes" button produced -- a list of every merged pull
request, which for 4.0.0 would have been about 127 lines nobody reads.

Direction of the dependency matters: this reads a version *out of* the changelog, it never
determines one. setuptools_scm remains the only version source (see this repo's CLAUDE.md
"Releasing" section), there is no version string in the source tree, and nothing may parse
CHANGELOG.md to find out what version is being built. The workflow passes in the version
setuptools_scm already resolved.

Stdlib only -- no PrivacyFence install required, matching scripts/r2_release.py. The build jobs
call this before (or without) installing anything, and the Windows job calls it through `shell:
bash` like every other cross-platform step there.

Exits non-zero when the version has no section, which is deliberate: a stable tag whose notes
nobody wrote should fail the release build loudly. The alternative is worse than an error --
action-gh-release silently falls back to the release's existing body when `body_path` cannot be
read, so a missing section would otherwise ship the auto-generated pull-request wall this script
exists to replace.

It exits non-zero on a *duplicated* section for the same reason, and that case is the sneakier of
the two. CLAUDE.md's release step says to rename `## [Unreleased]` to `## [X.Y.Z] -- YYYY-MM-DD`,
which is right whenever no such heading exists yet -- but 4.0.0's section was opened early, while
the changelog was being written, so following that step literally would have produced a second
`## [4.0.0]`. Matching the first heading and stopping at the next `##` then emits whichever half
came first and silently drops the other, with exit code 0: a green build shipping half the notes.
The loud failure the missing-section case already gets is what this deserves too.

It exits non-zero on a populated `## [Unreleased]` too, which is the *other* half of that same
trap and the one the duplicate check does not catch. CLAUDE.md's release step is "merge
[Unreleased]'s entries into the existing [4.0.0] section and correct its date"; doing only the
date half leaves a correct, single [4.0.0] heading with every beta fix still stranded above it in
[Unreleased]. The duplicate guard sees nothing wrong -- there is exactly one heading -- and the
release ships notes missing the whole beta cycle, green, at exit 0. Measured on 2026-09-17 against
the real file: 142 lines rendered, containing none of the eSigner, %LOCALAPPDATA%,
privacyfence_status or SameSite entries. `--allow-unreleased` exists for reading a section by hand
mid-cycle; nothing in .github/workflows/ passes it, which is the point.

Pre-release tags (4.0.0a17 and friends) intentionally have no section of their own -- per Keep a
Changelog they are folded into the version they lead to -- which is why the workflow only runs
this for a stable channel. That also means this guard costs a pre-release build nothing: it never
runs for one.

Examples:
    python3 scripts/changelog_section.py 4.0.0
        -> prints the [4.0.0] section body (without its own "## [4.0.0] -- date" heading)

    python3 scripts/changelog_section.py v4.0.0 --changelog path/to/CHANGELOG.md
        -> same; a leading "v" is tolerated the way scripts/r2_release.py tolerates it
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"

# "## [4.0.0] — 2026-09-14", "## [Unreleased]". Only the bracketed version is captured; whatever
# follows it (an em-dashed date, nothing at all) is presentation.
_VERSION_HEADING = re.compile(r"^##\s+\[([^\]]+)\]")

# Any level-2-or-higher-precedence boundary that ends a section. A "### Added" subheading inside
# the section must *not* match, so this is deliberately "## " exactly, not "#{1,2} ".
_SECTION_END = re.compile(r"^##\s")

_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"

# The permanent heading every feature branch adds under, and the one that must be empty by the
# time a release is tagged -- see the module docstring's second trap.
UNRELEASED = "Unreleased"


def _content_lines(text: str) -> list[tuple[str, bool]]:
    """Each line of the changelog, paired with whether a heading is recognizable on it.

    HTML comments are dropped outright -- this file's own top-of-file comment quotes
    "## [Unreleased]" while explaining the convention, and no comment belongs in a release body
    anyway. Fenced code blocks are kept (an entry may legitimately show a command or a config
    snippet) but never scanned for headings, so an example changelog inside a fence can't cut a
    section short.
    """
    kept: list[tuple[str, bool]] = []
    in_comment = False
    in_fence = False
    for line in text.splitlines():
        if in_comment:
            if _COMMENT_CLOSE in line:
                in_comment = False
            continue
        if not in_fence and line.lstrip().startswith(_COMMENT_OPEN) and _COMMENT_CLOSE not in line:
            in_comment = True
            continue
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            kept.append((line, False))
            continue
        kept.append((line, not in_fence))
    return kept


def _normalize(version: str) -> str:
    return version[1:] if version.startswith(("v", "V")) else version


def known_versions(text: str) -> list[str]:
    """Every version this changelog has a section for, in file order."""
    return [
        match.group(1)
        for line, is_heading_line in _content_lines(text)
        if is_heading_line and (match := _VERSION_HEADING.match(line))
    ]


def section(text: str, version: str) -> str:
    """The body of ``version``'s section, without its own heading.

    Raises ``LookupError`` if there is no such section, if there is more than one (see this
    module's docstring for why that is a real and non-obvious failure), or if the section is empty.
    Surrounding blank lines are stripped so the result is the release body exactly as it should
    render, with no leading gap under the title.
    """
    wanted = _normalize(version)
    lines = _content_lines(text)

    headings = [
        index
        for index, (line, is_heading_line) in enumerate(lines)
        if is_heading_line
        and (match := _VERSION_HEADING.match(line))
        and _normalize(match.group(1)) == wanted
    ]
    if not headings:
        raise LookupError(f"CHANGELOG.md has no section for {version}")
    if len(headings) > 1:
        raise LookupError(
            f"CHANGELOG.md has {len(headings)} headings for {version}; only the first would be "
            f"rendered and the rest silently dropped. Merge them into one section."
        )
    start = headings[0] + 1  # the heading itself is the release title on GitHub; don't repeat it

    body = _body_from(lines, start)
    if not body:
        raise LookupError(f"CHANGELOG.md's section for {version} is empty")
    return body


def _body_from(lines: list[tuple[str, bool]], start: int) -> str:
    """Everything from ``start`` up to the next ``## `` boundary, blank-trimmed."""
    end = len(lines)
    for index in range(start, len(lines)):
        line, is_heading_line = lines[index]
        if is_heading_line and _SECTION_END.match(line):
            end = index
            break
    return "\n".join(line for line, _ in lines[start:end]).strip("\n")


def unreleased_body(text: str) -> str:
    """Whatever sits under ``## [Unreleased]``, or ``""`` when it is absent or empty.

    Deliberately does not go through section(): a second ``## [Unreleased]`` would make that raise,
    and this must answer "is anything pending?" rather than refuse. Two Unreleased headings is
    itself pending content, so the first one's body is the right answer either way.
    """
    lines = _content_lines(text)
    for index, (line, is_heading_line) in enumerate(lines):
        match = _VERSION_HEADING.match(line) if is_heading_line else None
        if match and _normalize(match.group(1)) == UNRELEASED:
            return _body_from(lines, index + 1)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="Release version, e.g. 4.0.0 (a leading 'v' is tolerated)")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=DEFAULT_CHANGELOG,
        help=f"Changelog to read (default: {DEFAULT_CHANGELOG})",
    )
    parser.add_argument(
        "--allow-unreleased",
        action="store_true",
        help=(
            "Render even though [Unreleased] still has entries. For reading a section by hand "
            "mid-cycle; a release build must never pass this."
        ),
    )
    args = parser.parse_args(argv)

    try:
        text = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.changelog}: {exc}", file=sys.stderr)
        return 1

    # Resolve the section first: "no section for 3.0.0" is a more basic answer than "merge
    # [Unreleased]", and both are true at once for a mistyped version.
    try:
        body = section(text, args.version)
    except LookupError as exc:
        available = ", ".join(known_versions(text)) or "(none)"
        print(f"error: {exc}\nSections in {args.changelog}: {available}", file=sys.stderr)
        return 1

    if (
        not args.allow_unreleased
        and _normalize(args.version) != UNRELEASED
        and unreleased_body(text)
    ):
        print(
            f"error: {args.changelog}'s [Unreleased] section still has entries, so the notes for "
            f"{args.version} would ship without them. The release PR must merge [Unreleased] into "
            f"[{_normalize(args.version)}] and leave a fresh empty [Unreleased] above it -- see "
            f'CLAUDE.md\'s "Release notes come from CHANGELOG.md". Pass --allow-unreleased to '
            f"render anyway (never from a release build).",
            file=sys.stderr,
        )
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
