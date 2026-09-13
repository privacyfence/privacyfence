#!/usr/bin/env bash
# Regenerate requirements/*.lock.txt from pyproject.toml -- see
# requirements/README.md for what these two files are and why they're
# hash-locked (SEC-19, Phase 2.4).
#
# Prerequisites: `uv` on PATH (https://docs.astral.sh/uv/getting-started/installation/) -- kept
# out of the `dev` extra so that installing `.[dev]` doesn't pull in a tool only this script uses.
#
# Usage: ./scripts/update_dependency_locks.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v uv &>/dev/null; then
  echo "uv not found -- install it (dependency-audit.yml's lockfile-freshness job compiles/checks" >&2
  echo "requirements/*.lock.txt with it too) and retry: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# PYTHON_FLOOR must match pyproject.toml's `requires-python` lower bound. `uv pip compile
# --universal` does NOT read that bound out of pyproject.toml on its own -- without an explicit
# `--python-version`, it resolves markers against whichever interpreter happens to be invoking `uv`
# (its own version, not pyproject.toml's declared floor), which turned this script's very first
# `--universal` migration into exactly the kind of "depends on the machine that ran it" trap it was
# meant to fix: some packages (e.g. pyopenssl, referencing, starlette) only need `typing-extensions`
# for `python_version < "3.13"`, so compiling on a 3.13 interpreter silently drops that dependency
# edge from the lock file, while compiling on a 3.11 (or any pre-3.13) interpreter keeps it --
# dependency-audit.yml's lockfile-freshness job (which runs under actions/setup-python 3.13) then
# disagreed with a contributor regenerating locally under a different ambient Python, exactly the
# "compiling interpreter's version leaks into the lock file" failure mode the old pip-tools-based
# version of this script warned about, just relocated from "which markers get evaluated" to "which
# floor a universal resolution assumes". Pinning `--python-version` explicitly to the package's own
# declared floor removes the ambient-interpreter dependence entirely and is also the *correct*
# choice on its own merits: it's the oldest Python this package claims to support, so it's the one
# resolution guaranteed to need everything every supported version needs.
PYTHON_FLOOR="3.11"

# --universal: resolve one lock file that's installable on every OS/architecture this project
# ships for (macOS, Linux, Windows), instead of only the platform this script happens to run on.
# A plain (non-universal) `pip-compile`/`uv pip compile` resolves environment markers (e.g. a
# package's `pywin32>=310; sys_platform == "win32"`-style conditional dependency) against the
# invoking interpreter's own platform -- so a lock file compiled on Linux silently drops every
# Windows-only transitive dependency, even though pyproject.toml never changed. That's exactly what
# happened to mcp's `pywin32` dependency here: this script always ran on Linux, so
# requirements/runtime.lock.txt never had a `pywin32` line at all, and build.yml's `build-windows`
# job's `pip install --require-hashes -r requirements/runtime.lock.txt` step failed the first time
# it ran, on the first tag build after that step was added -- pip's --require-hashes mode refuses
# an unpinned transitive dependency it needs to install, and pywin32 wasn't in the file to pin.
# `--universal` resolves for the union of supported platforms up front and marks each
# platform-specific package with its own environment marker (`pywin32==312 ; sys_platform ==
# 'win32'`) right in the lock file; `pip install --require-hashes` then evaluates that marker
# itself at install time on whichever OS it's running on, so the same lock file installs correctly
# -- with hash verification -- on macOS, Linux and Windows alike, without needing a
# `runtime-windows.lock.txt`-style split or an unpinned exception for one package.
#
# --python-version: see PYTHON_FLOOR above -- makes the Python-version half of the resolution just
# as machine-independent as --universal makes the platform half.
#
# --generate-hashes: the whole point of this file (see module docstring above).
#
# --no-strip-extras: keep a resolved package's own `[extra]` annotation (e.g. `pyjwt[crypto]`,
# pulled in by mcp) on its pinned line instead of flattening it away. Purely cosmetic/provenance --
# uv resolves and pins an extra's own transitive dependencies into the lock file either way -- but
# it matches what this file already looked like under the old pip-tools-based script and keeps the
# `# via <package>` trail legible.
#
# No `--upgrade` needed here (unlike the old pip-tools invocation): `uv pip compile` always does a
# full fresh resolution against whatever's on the index right now rather than treating an existing
# output file as a constraint, so there's no equivalent "stale unless told otherwise" trap to guard
# against.
uv pip compile --universal --python-version "$PYTHON_FLOOR" --generate-hashes --no-strip-extras \
  --output-file=requirements/runtime.lock.txt \
  pyproject.toml

uv pip compile --universal --python-version "$PYTHON_FLOOR" --generate-hashes --no-strip-extras \
  --extra dev --extra test --extra lint \
  --output-file=requirements/dev.lock.txt \
  pyproject.toml

echo "Regenerated requirements/runtime.lock.txt and requirements/dev.lock.txt."
