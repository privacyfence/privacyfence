#!/usr/bin/env python3
"""Create a release tag safely -- the checks this repo's release history says a human keeps missing.

Cutting a release is "tag `main`'s tip and push the tag" (this repo's CLAUDE.md "Releasing"
section) -- there's no version-bump commit and no build step to catch a mistake before it's live.
This script runs the checks that step has no other gate for, before creating the tag locally:

  1. **Top of main.** The checkout is on `main`, the working tree is clean, and local `main` is
     exactly `origin/main` (after a fetch) -- not ahead, not behind. Also refuses to tag a commit
     that already carries a release tag: CLAUDE.md's "One release tag per commit" documents the
     real incident this guards -- `git describe` (which `setuptools_scm` resolves `__version__`
     from) picks *one* tag when a commit carries several, so a second tag on the same commit as an
     existing one can silently build and try to publish under the wrong version
     (https://github.com/privacyfence/privacyfence/actions/runs/35388772087).
  2. **Valid version format.** The tag must be `vMAJOR.MINOR.PATCH`, optionally followed by
     `a<N>`/`b<N>`/`rc<N>` -- the PEP 440 short form CLAUDE.md's "Releasing" section specifies.
     Older spellings this repo's history carries (`-beta1`, `-alpha1`, ...) are tolerated when
     reading *existing* tags (see `_TAG_RE` below) but refused for a *new* one.
  3. **Sequential, no gaps.** A pre-release's number must be exactly one more than the highest
     existing pre-release of the same stage for that `major.minor.patch` (or `1`, if none exist
     yet). A stable release's `major.minor.patch` must be an unskipped bump (patch+1, or
     minor+1/patch=0, or major+1/minor=patch=0) from the latest existing stable release -- unless
     a pre-release already led up to this exact version, in which case that check already ran when
     the first one was tagged.

This does not replace human judgement about *what* to release next -- it only catches the
mechanical slips CLAUDE.md's "Releasing" section calls out by name: `d929510` (a version bump
landing after another release had already claimed that number, back when versions were hand-bumped
commits rather than tags) and the `v4.1.0a6`/`v4.1.0a7` double-tag above. It does not check
`CHANGELOG.md` -- `scripts/changelog_section.py` already gates that at release-build time, so a
missing or unmerged `[Unreleased]` section fails loudly there rather than being duplicated here.

Only creates the tag locally by default -- pushing it starts `build.yml` and `publish-pypi.yml`
immediately (CLAUDE.md's "Releasing" section), which is not something this script does without
`--push` being passed explicitly.

Stdlib only, no PrivacyFence install required -- same as scripts/r2_release.py and
scripts/changelog_section.py. Deliberately doesn't import either: this script carries its own copy
of the version-parsing regexes rather than reaching across scripts/, matching r2_release.py's own
stated reason for doing the same relative to src/privacyfence/update_checker.py.

Examples:
    python3 scripts/tag_release.py 4.2.0a1
        -> checks pass -> creates local tag v4.2.0a1 -> prints the `git push` command to run by hand

    python3 scripts/tag_release.py 4.2.0 --push
        -> checks pass -> creates and pushes v4.2.0

    python3 scripts/tag_release.py 4.2.0a3
        -> error: next a-release for 4.2.0 must be a2 (got a3) if only 4.2.0a1 exists
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# The scheme new tags must use -- CLAUDE.md's "Releasing" section: "a=alpha, b=beta,
# rc=release-candidate (PEP 440 short form)". Same shape as
# src/privacyfence/update_checker.py's _VERSION_RE / scripts/r2_release.py's _VERSION_RE, minus the
# ".dev"/"+local" tail those two also match -- this script only ever writes a tag, never compares
# against a synthesized dev version.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")

# What an *existing* tag may look like -- deliberately more permissive than _VERSION_RE, mirroring
# scripts/r2_release.py's _TAG_RE/_TAG_STAGE_ALIASES verbatim. This repo's history has tags spelled
# "-alpha1", "-beta2", "-preview3", ... that all normalize to the short form; the sequential/no-gaps
# check below needs to see those too or it would think e.g. 3.3.0's beta cycle never happened.
_TAG_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:[-_.]?(alpha|beta|preview|pre|rc|c|a|b)[-_.]?(\d+))?(?:[-_.]?dev(\d+))?(?:\+.*)?$",
    re.IGNORECASE,
)
_TAG_STAGE_ALIASES = {
    "alpha": "a", "a": "a",
    "beta": "b", "b": "b",
    "preview": "rc", "pre": "rc", "c": "rc", "rc": "rc",
}


class TagError(Exception):
    """One of this script's checks failed. Caught in main() and printed without a traceback."""


class Identity(NamedTuple):
    """The (major, minor, patch, stage, num) a tag or version string names. `stage` is `""` and
    `num` is `0` for a stable release -- there is no pre-release number to compare."""

    major: int
    minor: int
    patch: int
    stage: str
    num: int

    @property
    def line(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def parse_version(version: str) -> Identity:
    """Parses a version *to be tagged* -- strict, canonical short form only. Raises TagError."""
    match = _VERSION_RE.match(version.strip())
    if not match:
        raise TagError(
            f"{version!r} isn't a valid release version -- expected major.minor.patch, optionally "
            "followed by a pre-release suffix a<N>/b<N>/rc<N> (PEP 440 short form; see CLAUDE.md's "
            "\"Releasing\" section). Old spellings like '-beta1' or '-alpha1' are tolerated in this "
            "repo's history but must not be used for a new tag."
        )
    major, minor, patch, stage, num = match.groups()
    return Identity(int(major), int(minor), int(patch), stage or "", int(num) if num else 0)


def _parse_existing_tag(tag: str) -> Identity | None:
    """Parses a tag already in the repo, tolerating this repo's older spellings. Returns None for
    anything that isn't a release tag at all (a non-release tag, or a malformed one) -- those simply
    don't participate in the sequential/no-gaps check."""
    match = _TAG_RE.match(tag.strip())
    if not match:
        return None
    major, minor, patch, stage, num, dev = match.groups()
    if dev is not None:
        return None  # a real pushed tag is never a synthesized dev version
    stage_short = _TAG_STAGE_ALIASES[stage.lower()] if stage else ""
    return Identity(int(major), int(minor), int(patch), stage_short, int(num) if num else 0)


def tag_name(identity: Identity) -> str:
    suffix = f"{identity.stage}{identity.num}" if identity.stage else ""
    return f"v{identity.major}.{identity.minor}.{identity.patch}{suffix}"


def next_version_options(latest_stable: tuple[int, int, int]) -> set[tuple[int, int, int]]:
    """The only `major.minor.patch` triples that don't skip a version after `latest_stable`."""
    major, minor, patch = latest_stable
    return {(major, minor, patch + 1), (major, minor + 1, 0), (major + 1, 0, 0)}


def _raise_not_next(line: tuple[int, int, int], latest_stable: tuple[int, int, int]) -> None:
    options = ", ".join(".".join(map(str, option)) for option in sorted(next_version_options(latest_stable)))
    raise TagError(
        f"{'.'.join(map(str, line))} isn't a valid next version after the latest stable release "
        f"v{'.'.join(map(str, latest_stable))} -- expected one of: {options} (a patch, minor, or "
        "major bump; no version may be skipped)."
    )


def check_sequential(identity: Identity, known: list[Identity]) -> None:
    """Raises TagError unless `identity` continues this repo's release numbering with no gap.
    `known` is every already-tagged release identity (see known_identities()) -- pure and
    git-independent so it's cheap to test against a fabricated history."""
    same_line = [existing for existing in known if existing.line == identity.line]
    stable_identities = [existing for existing in known if not existing.stage]
    latest_stable = max((existing.line for existing in stable_identities), default=None)

    if identity.stage:
        already_stable = [existing for existing in same_line if not existing.stage]
        if already_stable:
            raise TagError(
                f"{'.'.join(map(str, identity.line))} was already released as a stable version -- "
                "a new pre-release needs a higher version."
            )

        stage_siblings = [existing for existing in same_line if existing.stage == identity.stage]
        expected_num = max((existing.num for existing in stage_siblings), default=0) + 1
        if identity.num != expected_num:
            existing_names = ", ".join(
                tag_name(existing) for existing in sorted(stage_siblings, key=lambda existing: existing.num)
            ) or "(none yet)"
            raise TagError(
                f"next {identity.stage}-release for {'.'.join(map(str, identity.line))} must be "
                f"{identity.stage}{expected_num} (got {identity.stage}{identity.num}). Existing "
                f"{identity.stage}-releases for this version: {existing_names}"
            )

        if not same_line and latest_stable is not None and identity.line not in next_version_options(latest_stable):
            _raise_not_next(identity.line, latest_stable)
    else:
        already_stable = [existing for existing in same_line if not existing.stage]
        if already_stable:
            raise TagError(
                f"v{'.'.join(map(str, identity.line))} is already tagged as a stable release "
                f"({', '.join(tag_name(existing) for existing in already_stable)})"
            )

        # A pre-release already targeting this exact version already passed this check when *it*
        # was tagged -- finalizing it to stable isn't a new jump in the version line.
        if not same_line and latest_stable is not None and identity.line not in next_version_options(latest_stable):
            _raise_not_next(identity.line, latest_stable)


def _git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise TagError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def check_top_of_main(cwd: Path, *, remote: str = "origin", branch: str = "main", fetch: bool = True) -> None:
    current = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if current != branch:
        raise TagError(f"checked-out branch is {current!r}, not {branch!r} -- switch to {branch} before tagging")

    status = _git(["status", "--porcelain"], cwd=cwd)
    if status:
        raise TagError("working tree isn't clean -- commit, stash, or discard changes before tagging")

    if fetch:
        _git(["fetch", remote, branch], cwd=cwd)

    head = _git(["rev-parse", "HEAD"], cwd=cwd)
    try:
        remote_head = _git(["rev-parse", f"{remote}/{branch}"], cwd=cwd)
    except TagError as exc:
        raise TagError(f"can't resolve {remote}/{branch}: {exc}") from exc

    if head != remote_head:
        raise TagError(
            f"local {branch} ({head[:12]}) is not {remote}/{branch} ({remote_head[:12]}) -- "
            f"push or pull so local {branch} matches {remote} before tagging."
        )


def check_no_tag_at_head(cwd: Path) -> None:
    head = _git(["rev-parse", "HEAD"], cwd=cwd)
    existing = _git(["tag", "--points-at", head], cwd=cwd)
    tags = [line for line in existing.splitlines() if line.strip()]
    if tags:
        raise TagError(
            f"HEAD already carries tag(s) {', '.join(tags)} -- `git describe` (which setuptools_scm "
            "resolves __version__ from) picks one tag when a commit carries several, so a second "
            "tag here can silently build under the wrong version (CLAUDE.md's \"One release tag per "
            "commit\"). Move the release forward onto a new commit instead."
        )


def known_identities(cwd: Path) -> list[Identity]:
    output = _git(["tag", "--list"], cwd=cwd)
    identities = []
    for line in output.splitlines():
        parsed = _parse_existing_tag(line)
        if parsed is not None:
            identities.append(parsed)
    return identities


def create_tag(cwd: Path, name: str) -> None:
    _git(["tag", name], cwd=cwd)


def push_tag(cwd: Path, remote: str, name: str) -> None:
    _git(["push", remote, name], cwd=cwd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="Version to tag, e.g. 4.2.0 or 4.2.0a1 (a leading 'v' is tolerated)")
    parser.add_argument("--remote", default="origin", help="Remote to check main against and push to (default: origin)")
    parser.add_argument("--branch", default="main", help="Branch a release is cut from (default: main)")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="Path to the git checkout (default: this repo)")
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="Skip 'git fetch' before checking that main is up to date -- only if you already fetched",
    )
    parser.add_argument(
        "--push", action="store_true",
        help="Also push the tag once created -- starts build.yml/publish-pypi.yml immediately",
    )
    args = parser.parse_args(argv)

    try:
        identity = parse_version(args.version)
        name = tag_name(identity)

        check_top_of_main(args.repo, remote=args.remote, branch=args.branch, fetch=not args.no_fetch)
        check_no_tag_at_head(args.repo)

        if _git(["tag", "--list", name], cwd=args.repo):
            raise TagError(f"{name} already exists")

        check_sequential(identity, known_identities(args.repo))

        create_tag(args.repo, name)
        head = _git(["rev-parse", "--short", "HEAD"], cwd=args.repo)
        print(f"created tag {name} at {head}")

        if args.push:
            push_tag(args.repo, args.remote, name)
            print(f"pushed {name} to {args.remote} -- build.yml and publish-pypi.yml are now running")
        else:
            print(f"not pushed. Review, then run:\n  git push {args.remote} {name}")
    except TagError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
