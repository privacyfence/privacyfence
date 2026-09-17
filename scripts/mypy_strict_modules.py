#!/usr/bin/env python3
"""Run mypy, blocking, over exactly the modules promoted by the strictness ratchet.

TST-07 describes mypy as "non-blocking -> blocking per module", and `[tool.mypy]` in
pyproject.toml says per-module `[[tool.mypy.overrides]]` blocks "should be added below as modules
get cleaned up and promoted to blocking". That word -- blocking -- was aspirational: the only mypy
step in `.github/workflows/tests.yml`'s `static-analysis` job was `continue-on-error: true`, so a
promoted module's strict flags changed which errors mypy *printed* and nothing else. Five modules
were promoted that way (privacyfence/privacyfence#377 and the url_safety.py override before it) and
all five could have regressed without failing a single check.

This script is the missing half. CI runs it as an ordinary, non-`continue-on-error` step beside the
informational whole-tree run, so the ratchet's promoted modules genuinely gate the merge while the
~87 pre-existing errors in the rest of the tree stay visible-but-advisory, exactly as before.

pyproject.toml stays the single source of truth for *which* modules those are: the list is read
back out of `[[tool.mypy.overrides]]` rather than repeated in the workflow, so promoting the next
module is still a one-block edit and cannot drift out of sync with what CI enforces. An override
that only relaxes settings (`ignore_errors`, a flag set to `false`) is not a promotion and is
skipped; a promoted module whose file no longer exists is an error, not a silent skip, so a rename
that leaves the override behind fails here instead of quietly shrinking the gate.

Why `--follow-imports=silent`: mypy is handed only the promoted files, but it still has to analyze
everything they import to type them correctly, and would otherwise report *those* modules' errors
too -- dragging the whole tree's pre-existing findings into a blocking step and making promotion
impossible. `silent` keeps the imported modules' types and drops their errors, which is precisely
the per-module semantics the ratchet is after.

Stdlib only (tomllib is 3.11+, this repo's `requires-python` floor), so it runs before/without any
extra install -- same bar as scripts/changelog_section.py and scripts/r2_release.py. mypy itself is
invoked as `sys.executable -m mypy` rather than the `mypy` console script, so it always runs under
the interpreter whose site-packages holds the project and its dependencies; a `mypy` binary from a
different environment cannot see them and reports every third-party import as `Any` (which,
combined with `warn_return_any`, invents failures that do not exist).

Usage:
    python3 scripts/mypy_strict_modules.py
        -> type-check the promoted modules; exit code is mypy's

    python3 scripts/mypy_strict_modules.py --list
        -> print the resolved source paths, one per line, and exit 0
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC_ROOT = REPO_ROOT / "src"

# The flags an override sets to promote a module to blocking. This is the list url_safety.py's
# override spells out and the four in privacyfence/privacyfence#377 copied -- deliberately the
# individual components of `strict` rather than `strict = true`, because on mypy 2.3.1 a
# per-module `strict = true` leaks its global-only components (e.g. `warn_unused_configs`) into
# every other module; see that override's own comment in pyproject.toml.
#
# Matching on "sets at least one of these to true" rather than on the exact set means an override
# that promotes a module only part of the way still gets checked here. An override that merely
# *relaxes* something (`ignore_errors = true`, or any of these set to `false`) sets none of them to
# true and is correctly not treated as a promotion.
STRICTNESS_FLAGS = frozenset(
    {
        "disallow_untyped_defs",
        "disallow_incomplete_defs",
        "disallow_untyped_calls",
        "disallow_any_generics",
        "disallow_any_explicit",
        "disallow_any_unimported",
        "disallow_subclassing_any",
        "disallow_untyped_decorators",
        "warn_return_any",
        "warn_unreachable",
        "no_implicit_reexport",
        "strict_equality",
        "strict_optional",
        "strict",
    }
)


class ResolutionError(Exception):
    """A promoted module name that does not correspond to a file under src/."""


def _module_names(override: dict[str, object]) -> list[str]:
    """The module name(s) one `[[tool.mypy.overrides]]` block applies to.

    mypy accepts either a single string or a list of them under `module`, so both shapes are
    read here even though this repo currently only uses the string form.
    """
    module = override.get("module")
    if isinstance(module, str):
        return [module]
    if isinstance(module, list):
        return [entry for entry in module if isinstance(entry, str)]
    return []


def promoted_modules(pyproject: Path = DEFAULT_PYPROJECT) -> list[str]:
    """Module names that `[[tool.mypy.overrides]]` promotes to blocking, in file order."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    overrides = data.get("tool", {}).get("mypy", {}).get("overrides", [])

    names: list[str] = []
    for override in overrides:
        if not isinstance(override, dict):
            continue
        promotes = any(override.get(flag) is True for flag in STRICTNESS_FLAGS)
        if not promotes:
            continue
        for name in _module_names(override):
            if name not in names:
                names.append(name)
    return names


def module_path(module: str, src_root: Path = SRC_ROOT) -> Path:
    """The source file a dotted module name refers to, under `src_root`.

    Wildcards are rejected rather than expanded: a promotion is a deliberate, per-module decision
    (that is the whole point of a ratchet), and `module = "privacyfence.web.*"` would quietly widen
    the blocking set every time a new file landed in that package.
    """
    if "*" in module:
        raise ResolutionError(
            f"{module!r}: wildcard module patterns are not supported in a promotion -- "
            "promote modules one at a time"
        )

    parts = module.split(".")
    as_module = src_root.joinpath(*parts).with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = src_root.joinpath(*parts, "__init__.py")
    if as_package.is_file():
        return as_package
    raise ResolutionError(
        f"{module!r}: no {as_module.relative_to(src_root.parent)} or "
        f"{as_package.relative_to(src_root.parent)} -- was the module renamed without updating "
        "its [[tool.mypy.overrides]] block in pyproject.toml?"
    )


def promoted_paths(
    pyproject: Path = DEFAULT_PYPROJECT, src_root: Path = SRC_ROOT
) -> list[Path]:
    """Resolved source paths for every promoted module. Raises on one that cannot be resolved."""
    return [module_path(name, src_root) for name in promoted_modules(pyproject)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the resolved source paths instead of running mypy",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=DEFAULT_PYPROJECT,
        help="path to pyproject.toml (default: this repo's)",
    )
    args = parser.parse_args(argv)

    try:
        paths = promoted_paths(args.pyproject)
    except ResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Relative to the repo root, both for readable output and because mypy echoes back whatever
    # form it was given -- absolute paths in an error message are noise in a CI log.
    relative = [
        path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        for path in paths
    ]

    if args.list:
        for path in relative:
            print(path)
        return 0

    if not paths:
        # Not a failure on its own -- there is simply nothing promoted to enforce. The unit test
        # in tests/unit/test_mypy_strict_modules.py is what asserts this repo has not lost its
        # promotions; failing here instead would turn "the ratchet was reset" into a type error,
        # which is the wrong place to report it.
        print(
            "no modules promoted to blocking in [[tool.mypy.overrides]] -- nothing to check",
        )
        return 0

    cmd = [
        sys.executable,
        "-m",
        "mypy",
        "--follow-imports=silent",
        *[str(path) for path in relative],
    ]
    print(f"--- mypy (blocking, promoted modules): {' '.join(cmd[3:])} ---")
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
