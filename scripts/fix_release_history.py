#!/usr/bin/env python3
"""Fix the release-history hygiene issues catalogued in privacyfence/privacyfence#375.

Three separate, one-time cleanups against the GitHub Releases/tags this repo has already
published, bundled into one script because #375 asked for all three to land in the same pass:

1. Delete the `v4.0.0-apha2` release and tag -- a typo ("apha" for "alpha") published between
   `v4.0.0-alpha1` and `v4.0.0-alpha3`. It doesn't match `update_checker.py`'s `_VERSION_RE` any
   more than the correctly-spelled `-alphaN` tags do, so it's inert for update ranking, but it
   sits in the releases/tags list for anyone to find.
2. Rewrite release bodies that still link PRs and "Full Changelog" compares at the pre-transfer
   fork (`andras-tkcs/privacyfence`) instead of this repo. Those links redirect today only because
   GitHub keeps the old path alive after a transfer -- a courtesy, not a guarantee.
3. Flip `prerelease` to true on any alpha/beta/rc-tagged release GitHub still shows as a full
   release. (By the time this script was written the 4.0.0 alpha series had already been
   corrected as an urgent release blocker, separately from this issue -- this command is here so
   the fix is repeatable and reviewable rather than a one-off manual edit, and to catch any future
   release published the same way.)

Same shape as scripts/update_branch_protection.py: `show` prints what's out of sync, `apply`
fixes it (`--dry-run` to preview), and nothing here runs automatically in CI -- a maintainer with
a token that can delete releases and tags decides when to run `apply`.

Before deleting the typo tag, `apply` greps this checkout's tracked tree for the tag name and
refuses to delete anything if it finds a reference -- the same check #375 asked to make "before
deleting the obvious fix". Nothing in the tree has referenced it so far.

Requires GITHUB_TOKEN in the environment: a token with contents:write on this repo (able to
delete a release and a tag ref, and edit a release's body/prerelease flag).

Usage:
    python scripts/fix_release_history.py show
    python scripts/fix_release_history.py apply [--dry-run] [--force-delete-typo-tag]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

OWNER = "privacyfence"
REPO = "privacyfence"
API_ROOT = "https://api.github.com"

REPO_ROOT = Path(__file__).resolve().parents[1]

# The one release #375 identified as a plain typo. A literal, not a pattern -- this script deletes
# exactly this tag/release, never anything it merely guesses looks like a typo.
TYPO_TAG = "v4.0.0-apha2"

OLD_FORK_SLUG = "andras-tkcs/privacyfence"
NEW_ORG_SLUG = "privacyfence/privacyfence"

# Tags shaped like a pre-release (leading "v", then a PEP-440-ish alpha/beta/rc suffix, with or
# without the "-" the early tags used) that should always carry `prerelease: true`.
_PRERELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+-?(?:alpha|beta|rc|a|b)\d+$", re.IGNORECASE)


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required (a token with contents:write on this repo)")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _releases_url(owner: str, repo: str) -> str:
    return f"{API_ROOT}/repos/{owner}/{repo}/releases"


def list_releases(owner: str, repo: str) -> list[dict[str, Any]]:
    """Every release, oldest API page first, following `Link: rel="next"` until exhausted."""
    releases: list[dict[str, Any]] = []
    url: str | None = f"{_releases_url(owner, repo)}?per_page=100"
    while url:
        resp = requests.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        releases.extend(resp.json())
        url = resp.links.get("next", {}).get("url")
    return releases


def find_release_by_tag(releases: list[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    return next((r for r in releases if r.get("tag_name") == tag), None)


def releases_linking_old_fork(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in releases if OLD_FORK_SLUG in (r.get("body") or "")]


def rewrite_body(body: str) -> str:
    return body.replace(OLD_FORK_SLUG, NEW_ORG_SLUG)


def misflagged_prereleases(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r
        for r in releases
        if _PRERELEASE_TAG.match(r.get("tag_name", "")) and not r.get("prerelease")
    ]


def tree_references(tag: str, *, root: Path = REPO_ROOT) -> list[str]:
    """Tracked files (paths relative to `root`) whose content mentions `tag`, via `git grep` so
    only what's actually committed is checked -- not build output or an untracked scratch file."""
    result = subprocess.run(
        ["git", "grep", "--fixed-strings", "--files-with-matches", tag],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):  # 1 == git grep's own "no matches", not an error
        raise SystemExit(f"git grep failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def delete_release_and_tag(owner: str, repo: str, release: dict[str, Any]) -> None:
    resp = requests.delete(f"{_releases_url(owner, repo)}/{release['id']}", headers=_headers(), timeout=30)
    resp.raise_for_status()
    ref_resp = requests.delete(
        f"{API_ROOT}/repos/{owner}/{repo}/git/refs/tags/{release['tag_name']}", headers=_headers(), timeout=30
    )
    if ref_resp.status_code != 404:  # already gone is fine; anything else is a real failure
        ref_resp.raise_for_status()


def update_release_body(owner: str, repo: str, release_id: int, body: str) -> None:
    resp = requests.patch(
        f"{_releases_url(owner, repo)}/{release_id}", headers=_headers(), json={"body": body}, timeout=30
    )
    resp.raise_for_status()


def set_prerelease(owner: str, repo: str, release_id: int, *, prerelease: bool) -> None:
    resp = requests.patch(
        f"{_releases_url(owner, repo)}/{release_id}",
        headers=_headers(),
        json={"prerelease": prerelease},
        timeout=30,
    )
    resp.raise_for_status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner", default=OWNER)
    parser.add_argument("--repo", default=REPO)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show", help="Print what's out of sync, without changing anything")
    apply_parser = subparsers.add_parser("apply", help="Fix everything `show` reports")
    apply_parser.add_argument("--dry-run", action="store_true", help="Print what would change without applying it")
    apply_parser.add_argument(
        "--force-delete-typo-tag",
        action="store_true",
        help=f"Delete {TYPO_TAG} even if the tree still references it (only after you've checked why)",
    )
    args = parser.parse_args(argv)

    releases = list_releases(args.owner, args.repo)
    typo_release = find_release_by_tag(releases, TYPO_TAG)
    fork_linked = releases_linking_old_fork(releases)
    misflagged = misflagged_prereleases(releases)
    references = tree_references(TYPO_TAG) if typo_release else []

    if args.command == "show":
        if typo_release:
            print(f"typo release: {TYPO_TAG} (id {typo_release['id']}) -- would be deleted by `apply`")
            if references:
                print(f"  refuses to delete: referenced in {', '.join(references)}")
        else:
            print(f"typo release: {TYPO_TAG} not found (already deleted)")

        if fork_linked:
            print(f"release bodies linking {OLD_FORK_SLUG} (would be rewritten by `apply`):")
            for release in fork_linked:
                print(f"  {release['tag_name']}")
        else:
            print(f"no release bodies link {OLD_FORK_SLUG}")

        if misflagged:
            print("releases mis-flagged prerelease=false (would be corrected by `apply`):")
            for release in misflagged:
                print(f"  {release['tag_name']}")
        else:
            print("no releases are mis-flagged prerelease=false")
        return 0

    if args.command == "apply":
        if not typo_release and not fork_linked and not misflagged:
            print("already clean -- nothing to do")
            return 0

        if typo_release:
            if references and not args.force_delete_typo_tag:
                print(
                    f"refusing to delete {TYPO_TAG}: still referenced in {', '.join(references)} "
                    "(pass --force-delete-typo-tag once you've checked why)",
                    file=sys.stderr,
                )
                return 1
            print(f"deleting {TYPO_TAG} (release id {typo_release['id']}) and its tag")
            if not args.dry_run:
                delete_release_and_tag(args.owner, args.repo, typo_release)

        for release in fork_linked:
            print(f"rewriting {release['tag_name']}'s body: {OLD_FORK_SLUG} -> {NEW_ORG_SLUG}")
            if not args.dry_run:
                update_release_body(args.owner, args.repo, release["id"], rewrite_body(release["body"]))

        for release in misflagged:
            print(f"setting {release['tag_name']} prerelease=true")
            if not args.dry_run:
                set_prerelease(args.owner, args.repo, release["id"], prerelease=True)

        if args.dry_run:
            print("(dry run -- not applied)")
        else:
            print("applied.")
        return 0

    return 1  # pragma: no cover -- unreachable, argparse enforces required=True above


if __name__ == "__main__":
    sys.exit(main())
