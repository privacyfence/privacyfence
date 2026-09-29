#!/usr/bin/env python3
"""Decide whether this workflow run is a duplicate of another run for the same tag push.

GitHub occasionally starts a workflow twice for one push event. It did for `v5.2.0`: one tag push,
two `build.yml` runs and two `publish-pypi.yml` runs, two seconds apart. Two `build.yml` runs
for one tag cannot both succeed. Each builds and signs its own artifacts, `r2_release.py upload`
refuses to overwrite an object with different bytes, and so each run uploads whichever half it
reaches first and fails on the other half. Neither finalizes a release. See
docs/adr/0111-a-duplicate-release-run-cancels-itself.md.

`build.yml` and `publish-pypi.yml` call this in their first job, with the runs of their own
workflow on the same commit piped in (the `workflow_runs` list GitHub's
`GET /repos/{owner}/{repo}/actions/workflows/{workflow}/runs?head_sha=...` returns). It prints
`true` when this run is the duplicate, and the job then cancels its own run.

A run is the duplicate when an older run of the same workflow was started by the same event for
the same ref and commit and is still going. "Older" is the lower run ID, so of two duplicates,
exactly one decides it is the duplicate, whichever order their first jobs run in. "Still going"
keeps a deliberate later run working: deleting and re-pushing a tag after its first run finished,
or re-running by hand, starts a run whose predecessors have all completed.

Only `push` runs are ever duplicates. A `workflow_dispatch` is always somebody asking for a run.

Stdlib only, like the other release-time scripts here; it needs no PrivacyFence install.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any


def is_duplicate(runs: Iterable[Mapping[str, Any]], *, run_id: int, event: str, ref_name: str, head_sha: str) -> bool:
    if event != "push":
        return False
    return any(
        run["id"] < run_id
        and run["event"] == event
        and run["head_branch"] == ref_name
        and run["head_sha"] == head_sha
        and run["status"] != "completed"
        for run in runs
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", type=int, required=True, help="This run's ID (GITHUB_RUN_ID)")
    parser.add_argument("--event", required=True, help="The event that started this run (GITHUB_EVENT_NAME)")
    parser.add_argument("--ref-name", required=True, help="The tag or branch this run is for (GITHUB_REF_NAME)")
    parser.add_argument("--head-sha", required=True, help="The commit this run is for (GITHUB_SHA)")
    args = parser.parse_args(argv)

    runs = json.load(sys.stdin)["workflow_runs"]
    duplicate = is_duplicate(runs, run_id=args.run_id, event=args.event, ref_name=args.ref_name, head_sha=args.head_sha)
    print("true" if duplicate else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
