#!/usr/bin/env python3
"""Report (never gate) whether the graphical-session/autostart workflows have a green run behind
this release tag -- privacyfence/privacyfence#374, option 1 ("Report, don't gate").

`linux-graphical-session.yml`, `windows-graphical-session.yml` and `macos-graphical-session.yml`
are the only automated coverage for the thing every desktop user depends on and nobody notices
until it breaks: the daemon starting itself at login. They run on packaging-related `main` pushes,
a weekly schedule, and manual dispatch -- never on a tag push, and never as a `needs:` of
`build.yml`'s `finalize-release` job, because this tier's own runtime cost must not sit on a
release's critical path (see docs/testing-policy.md's Layer 6 row and this repo's CLAUDE.md). That
leaves a gap: a tag can ship with autostart broken as long as the last packaging-touching push to
`main` was green and nothing since then re-ran any of the three workflows.

This script closes the "nobody looked" half of that gap without touching the "must not gate"
half. It first resolves which branch this release actually came from (`resolve_release_branch` --
`main`, unless `commit` is on a `releases/*` branch, since those get the same packaging-related
trigger as `main`; see CLAUDE.md's branch-protection section), then for each workflow reads the
single most recent *completed* run on that branch and checks two things: that its commit is
actually an ancestor of the commit being released (a run for a commit the branch hasn't reached
yet says nothing about this tag), and that it succeeded. Anything else -- no run at all, the
latest run not yet reachable from this tag, or a reachable run that failed -- prints a
`::warning::` annotation and nothing more. `finalize-release` runs this as an ordinary step; see
that job's own comment. It always exits 0.

Usage (matches scripts/release_stats.py's own conventions -- reads GH_TOKEN/GITHUB_TOKEN, and
keeps the pure decision (`evaluate`) separate from the network fetch and the `git` ancestor check
so it's unit-testable without mocking either):

    python3 scripts/check_graphical_session_coverage.py --repo privacyfence/privacyfence \
        --commit "$GITHUB_SHA"

Run from a checkout with full history (`fetch-depth: 0`, same requirement as setuptools_scm's own
tag resolution -- see this repo's CLAUDE.md). Full history here means every branch, not just the
one being released, which is what makes `resolve_release_branch` possible in the first place; it
also shells out to `git merge-base --is-ancestor` to confirm a run's commit actually precedes the
one being released.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_ROOT = "https://api.github.com"

# The three workflows privacyfence/privacyfence#374 is about -- see module docstring.
WORKFLOWS = ("linux-graphical-session.yml", "windows-graphical-session.yml", "macos-graphical-session.yml")


def _get(url: str, token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "privacyfence-check-graphical-session-coverage",
    }
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:  # url is always the fixed API_ROOT https host above
        return json.load(response)


def resolve_release_branch(commit: str) -> str:
    """The branch `commit` is actually being released from: the `releases/*` branch containing it,
    if any, else `main`. `linux-graphical-session.yml`/`windows-graphical-session.yml` trigger on
    packaging-related pushes to `main` *and* to `releases/**` alike (CLAUDE.md's branch-protection
    section), so during a `releases/*` cycle their coverage lives on that branch, not `main` --
    querying `branch=main` unconditionally reads a branch this release never touched. Requires the
    full-history checkout the module docstring already asks for: `fetch-depth: 0` fetches every
    branch, not just the one being released, so `git branch -r --contains` can see them all."""
    result = subprocess.run(
        ["git", "branch", "-r", "--contains", commit, "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    )
    release_branches = sorted(
        line.removeprefix("origin/")
        for line in result.stdout.splitlines()
        if line.startswith("origin/releases/")
    )
    return release_branches[0] if release_branches else "main"


def fetch_latest_completed_run(repo: str, workflow: str, branch: str, token: str) -> dict[str, Any] | None:
    """The single most recent *completed* run of `workflow` on `branch`, or None if it has never
    completed one there. The Actions API returns runs newest-first by default, so `per_page=1`
    alone is enough -- no separate sort."""
    url = (
        f"{API_ROOT}/repos/{repo}/actions/workflows/{workflow}/runs"
        f"?branch={urllib.parse.quote(branch, safe='')}&status=completed&per_page=1"
    )
    runs = _get(url, token).get("workflow_runs", [])
    return runs[0] if runs else None


def is_ancestor(candidate_sha: str, commit: str) -> bool:
    """True if `candidate_sha` is `commit` itself or one of its ancestors. Requires the calling
    checkout to have full history -- see module docstring."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate_sha, commit],
        check=False,
    )
    return result.returncode == 0


def evaluate(workflow: str, run: dict[str, Any] | None, run_is_ancestor: bool) -> str | None:
    """Pure function of one workflow's already-fetched latest run (or None) and whether that run's
    commit is an ancestor of the commit being released (already resolved by the caller via
    `is_ancestor`, which needs a real checkout). Returns a warning message, or None if that
    workflow's coverage is fine."""
    if run is None:
        return (
            f"{workflow} has no completed run at all -- no autostart coverage exists for this "
            "release (privacyfence/privacyfence#374)."
        )
    if not run_is_ancestor:
        return (
            f"{workflow}'s most recent completed run ({run.get('html_url')}) is not an ancestor "
            "of this release's commit -- no autostart coverage exists for this exact release yet "
            "(privacyfence/privacyfence#374)."
        )
    conclusion = run.get("conclusion")
    if conclusion != "success":
        return (
            f"{workflow}'s most recent run reachable from this release did not succeed "
            f"(conclusion={conclusion}): {run.get('html_url')} (privacyfence/privacyfence#374)."
        )
    return None


def check_all(repo: str, commit: str, token: str, workflows: tuple[str, ...] = WORKFLOWS) -> list[str]:
    branch = resolve_release_branch(commit)
    warnings: list[str] = []
    for workflow in workflows:
        try:
            run = fetch_latest_completed_run(repo, workflow, branch, token)
        except (urllib.error.URLError, TimeoutError) as exc:
            warnings.append(f"could not check {workflow}'s latest run: {exc}")
            continue
        run_is_ancestor = run is not None and is_ancestor(run["head_sha"], commit)
        message = evaluate(workflow, run, run_is_ancestor)
        if message:
            warnings.append(message)
    return warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help='"owner/repo", e.g. privacyfence/privacyfence')
    parser.add_argument("--commit", required=True, help="the commit being released (usually $GITHUB_SHA)")
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GH_TOKEN (or GITHUB_TOKEN) must be set", file=sys.stderr)
        return 2

    warnings = check_all(args.repo, args.commit, token)
    for message in warnings:
        print(f"::warning::{message}")
    if not warnings:
        print(
            "Autostart coverage (" + ", ".join(WORKFLOWS) + ") "
            "is green and reachable from this release."
        )

    # Deliberately always 0 -- this reports, it never gates the release. See module docstring.
    return 0


if __name__ == "__main__":
    sys.exit(main())
