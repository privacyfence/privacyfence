#!/usr/bin/env python3
"""Automated gate to run before starting `docs/release-testing.md`.

This does not replace that plan -- it only covers the parts a release should
never be blocked on discovering *manually*: the same automated suite CI runs
(`docs/testing-policy.md`, "Layers 1–4: every PR"). Everything in `release-testing.md`
-- live fixtures, popup smoke, real Cowork/Desktop prompts, DMG install --
still needs a human, a screen, and real accounts, none of which this script
has.

There used to be a version-consistency check here too, comparing
`pyproject.toml`'s `project.version` against `src/privacyfence/__init__.py`'s
`__version__` -- both hand-bumped in the same commit. Neither of those static
strings exists any more (see this repo's CLAUDE.md "Releasing" section):
`__version__` is derived at import time from the git tag via setuptools_scm
(`src/privacyfence/__init__.py`), so there is exactly one source of truth
left and nothing to compare.

    .venv/bin/python scripts/pre_release_check.py

Run from the repo root (with `mcpb/shim/` node_modules already installed via
`npm install`), the same as CI. Exits non-zero if any check fails.

`ruff check .`, `bandit` and `scripts/mypy_strict_modules.py` are included below because they're
CI's *blocking* static-analysis steps (`.github/workflows/tests.yml`'s `static-analysis` job -- see
`[tool.ruff.lint]`/`[tool.bandit]`/`[[tool.mypy.overrides]]` in pyproject.toml). The whole-tree
`mypy src/privacyfence` run stays out of this gate for the same reason it's still
`continue-on-error` in that job -- see `[tool.mypy]` in pyproject.toml; only the modules the
per-module ratchet has promoted are checked here, which is exactly what CI blocks on.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run(description: str, cmd: list[str], cwd: Path) -> bool:
    print(f"--- {description} ({' '.join(cmd)}) ---")
    try:
        result = subprocess.run(cmd, cwd=cwd)
        ok = result.returncode == 0
    except FileNotFoundError:
        # A missing tool (e.g. no `npm` on PATH, or a venv that was never
        # `pip install -e ".[dev,test,lint]"`d) used to blow up main() with
        # a raw traceback -- killing the whole run and skipping every check
        # after it, rather than reporting one FAIL and letting the rest
        # (ruff, bandit, ...) still run. See README.md's "Run from source"
        # for the venv setup and pyproject.toml's `[project.optional-
        # dependencies]` for the `dev`/`test`/`lint` extras this needs.
        print(f"error: '{cmd[0]}' not found on PATH -- is it installed?")
        ok = False
    print(f"--- {description}: {'PASS' if ok else 'FAIL'} ---\n")
    return ok


def main() -> int:
    results: dict[str, bool] = {}

    results["pytest"] = run(
        "pytest",
        [
            "python3", "-m", "pytest", "-v", "--cov=src/privacyfence", "--cov-branch",
            "--cov-report=term-missing", "--cov-report=json:coverage.json",
        ],
        cwd=REPO_ROOT,
    )
    # TST-03: same coverage
    # ratchet CI enforces (.github/workflows/tests.yml) -- see
    # scripts/check_coverage_floor.py's module docstring for the floors.
    # Runs even if pytest itself failed above, same as the shim checks
    # below; the summary loop still reports every result either way.
    results["coverage floor"] = run(
        "coverage floor", ["python3", "scripts/check_coverage_floor.py", "coverage.json"], cwd=REPO_ROOT
    )
    results["shim npm test"] = run("shim npm test", ["npm", "test"], cwd=REPO_ROOT / "mcpb" / "shim")
    results["shim typecheck"] = run(
        "shim typecheck", ["npm", "run", "typecheck"], cwd=REPO_ROOT / "mcpb" / "shim"
    )
    results["ruff"] = run("ruff", ["ruff", "check", "."], cwd=REPO_ROOT)
    results["mypy (promoted modules)"] = run(
        "mypy (promoted modules)",
        ["python3", "scripts/mypy_strict_modules.py"],
        cwd=REPO_ROOT,
    )
    results["bandit"] = run(
        "bandit", ["bandit", "-c", "pyproject.toml", "-r", "src"], cwd=REPO_ROOT
    )

    print("=== Pre-release check summary ===")
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    if not all(results.values()):
        print(
            "\nFix the failures above before starting "
            "docs/release-testing.md."
        )
        return 1

    print(
        "\nAll automated checks passed. Continue with "
        "docs/release-testing.md's \"Human checks\" and \"Platform artifacts\" "
        "sections for the parts automation can't judge before cutting the release."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
