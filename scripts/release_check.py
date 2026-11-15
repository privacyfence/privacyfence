#!/usr/bin/env python3
"""Pre-tag release checks -- the `check_script` that `.github/workflows/tld-release.yml` runs.

Runs, in order, and stops at the first failure:

  1. `scripts/r2_release.py channel <version>` -- classifies the version as stable or pre-release
     (and rejects a malformed one).
  2. `scripts/changelog_section.py <version>` -- stable releases only; the CHANGELOG section that
     will ship must exist and be released, never `--allow-unreleased`.
  3. `scripts/tag_release.py <version> --no-fetch --check-only` -- on top of an unmoved `main`,
     no tag on HEAD, valid and sequential version. `--no-fetch` because the workflow has just
     fetched `origin/main` into a clone that holds no credentials (docs/releasing.md, ADR 0312).

Stdlib only, no PrivacyFence install: the release workflow's check step has no setup-python and
no `pip install`, so this runs under whatever `python3` the runner has.

Example:
    python3 scripts/release_check.py 4.2.0
        -> channel: stable -> release notes -> tag checks -> release check passed for v4.2.0 (stable)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def run_step(name: str, argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Runs one step with this interpreter. `argv[0]` is a script path under SCRIPTS."""
    print(f"==> {name}", flush=True)
    return subprocess.run([sys.executable, *argv], text=True, capture_output=capture, check=False)


def write_summary(version: str, notes: str) -> None:
    """Appends the release notes to the GitHub step summary, when there is one."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as handle:
        handle.write(f"### Release notes that will ship for v{version}\n\n{notes}\n")


def _fail(name: str, result: subprocess.CompletedProcess[str]) -> int:
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    print(f"release check failed: {name} (exit {result.returncode})", file=sys.stderr)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", help="Version to check, e.g. 7.1.0 or 7.1.0a1 (no leading v)")
    args = parser.parse_args(argv)
    version = args.version

    result = run_step("release channel", [str(SCRIPTS / "r2_release.py"), "channel", version], capture=True)
    if result.returncode != 0:
        return _fail("release channel", result)
    channel = result.stdout.strip()
    print(f"channel: {channel}")

    if channel == "stable":
        result = run_step("release notes", [str(SCRIPTS / "changelog_section.py"), version], capture=True)
        if result.returncode != 0:
            return _fail("release notes", result)
        notes = result.stdout
        print(notes)
        write_summary(version, notes)

    result = run_step("tag checks", [str(SCRIPTS / "tag_release.py"), version, "--no-fetch", "--check-only"])
    if result.returncode != 0:
        return _fail("tag checks", result)

    print(f"release check passed for v{version} ({channel})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
