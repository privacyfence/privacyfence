#!/bin/bash
# SessionStart hook for Claude Code on the web -- make a fresh container able to work
# through docs/coding-and-testing-guidelines.md §2.7 without a manual setup step first.
#
# Two problems, in order of how quietly they break:
#
# 1. The web checkout arrives shallow and with no tags at all. docs/releasing.md
#    already spells out what that costs: setuptools_scm resolves the version
#    through `git describe`, so with no tags it falls back to [tool.setuptools_scm]'s
#    fallback_version -- "a placeholder that's never a real shipped version". Both
#    tests.yml and build.yml pass `fetch-depth: 0` to actions/checkout for exactly this
#    reason, and nothing does the equivalent here. A `pip install -e .` in that state
#    bakes the placeholder into the installed metadata, so every later
#    importlib.metadata.version("privacyfence") read -- src/privacyfence/__init__.py's
#    __version__, PrivacyFenceApp.spec's VERSION, build_dmg.sh, build_mcpb.sh -- returns
#    it, and scripts/tag_release.py's sequential-version check reads a tag list that
#    isn't there. It fails silently in both directions, which is why this runs first.
#
# 2. Nothing is installed: no venv, the package isn't importable, bandit isn't on PATH,
#    and mcpb/shim/ has no node_modules. That's several minutes of setup standing between
#    a session and the first command on the §2.7 checklist.
#
# Deliberately synchronous -- no {"async": true} preamble. A session that starts before
# the tag fetch finishes is a session that can install the fallback version, which is
# the whole failure this exists to prevent. The fetch costs ~2s; the installs dominate,
# and the container state is cached afterwards.
set -euo pipefail

# Web sessions only. A local checkout belongs to the maintainer and is already set up per
# README.md's "Run from source" -- and CLAUDE.md's worktree convention means several may
# be live at once, none of which this should reach into and mutate.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

echo "==> Restoring full history and tags (setuptools_scm needs them -- see docs/releasing.md)"
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  # --unshallow fails outright on a complete repo, hence the branch; the bare --tags
  # retry covers a server that refuses the deepening but will still hand over tags.
  git fetch --unshallow --tags origin || git fetch --tags origin
else
  git fetch --tags origin
fi

if git describe --tags >/dev/null 2>&1; then
  echo "    version resolves as: $(git describe --tags)"
else
  # Not fatal -- the session is still usable for everything that doesn't read a version --
  # but it must be said out loud rather than discovered three commands later.
  echo "    WARNING: no tag reachable from HEAD; __version__ will be fallback_version"
fi

echo "==> Installing privacyfence with the dev/test/lint extras"
# Into a .venv, matching README.md's "Run from source" and the `.venv/bin/python` spelling
# scripts/pre_release_check.py's own docstring uses -- but here it is a hard requirement rather
# than a convention. The container's ambient python3 is Debian's, whose patched setuptools drops
# `install_layout` when building an sdist: `pip install -e ".[dev,test,lint]"` against it dies
# building pyaes (a Telethon dependency that ships no wheel) with a bare
# `AttributeError: install_layout`. --upgrade-deps gives the venv its own current
# pip/setuptools, which builds it fine. Debian's own pip is left alone for the same family of
# reason -- it has no RECORD file, so upgrading it in place fails outright.
python3 -m venv --upgrade-deps .venv
.venv/bin/python -m pip install --quiet -e ".[dev,test,lint]"

# Put the venv first on PATH for the rest of the session, so `pytest`, `ruff`, `bandit` and
# `mypy` all mean the ones that were just installed -- the container also carries its own copies
# at /root/.local/bin, which resolve first otherwise and know nothing about this project.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$PWD/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

echo "==> Installing mcpb/shim dependencies"
# Guarded rather than unconditional: this hook also fires on resume/clear/compact, and
# `npm ci` deletes and reinstalls node_modules every time it runs. tests.yml uses `npm ci`
# for lockfile fidelity and so does this, just only when there's nothing there yet.
if [ ! -d mcpb/shim/node_modules ]; then
  (cd mcpb/shim && npm ci --no-audit --no-fund)
else
  echo "    already present, skipping"
fi

# tests/integration/test_browser_smoke.py (and the website browser tests) drive Chromium
# through the playwright package. The web container ships one at $PLAYWRIGHT_BROWSERS_PATH
# and sets PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1, so `playwright install` is off the table.
# That browser is whatever revision the container image was built with, not the one the
# locked playwright expects (at the time of writing, playwright 1.63 wants build 1243, the image ships
# 1194), and the browser fixture's deliberate "Chromium not available" skip then turns the
# whole suite into a green run of skips. A directory existing proves nothing, so launch it:
# first the way the tests do by default, then through PRIVACYFENCE_TEST_CHROMIUM (the
# escape hatch tests/website_site.py's chromium_launch_kwargs() reads) pointed at the
# container's own build, exported for the session when that is the one that works.
BROWSERS="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}"
probe_chromium() {  # $1: executable path, or empty for playwright's own expected build
  PROBE_EXE="$1" .venv/bin/python - <<'PY' 2>&1
import os
from playwright.sync_api import sync_playwright
exe = os.environ.get("PROBE_EXE")
with sync_playwright() as p:
    b = p.chromium.launch(**({"executable_path": exe} if exe else {}))
    page = b.new_page()
    page.set_content("<p>ok</p>")
    assert page.inner_text("p") == "ok"
    print(b.version)
    b.close()
PY
}
if default_out="$(probe_chromium "")"; then
  echo "==> Chromium $default_out launches through playwright (browser tests runnable)"
else
  fallback=""
  for candidate in "$BROWSERS"/chromium_headless_shell-*/chrome-*/headless_shell "$BROWSERS"/chromium-*/chrome-linux/chrome; do
    if [ -x "$candidate" ] && fallback_out="$(probe_chromium "$candidate")"; then
      fallback="$candidate"
      break
    fi
  done
  pw_version="$(.venv/bin/python -c 'from importlib.metadata import version; print(version("playwright"))' 2>/dev/null || echo unknown)"
  if [ -n "$fallback" ]; then
    echo "==> playwright $pw_version's expected Chromium isn't in $BROWSERS; the container's"
    echo "    Chromium $fallback_out launches instead, via PRIVACYFENCE_TEST_CHROMIUM=$fallback"
    echo "    (browser tests runnable against that build -- CI still uses the locked one)"
    if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
      echo "export PRIVACYFENCE_TEST_CHROMIUM=\"$fallback\"" >> "$CLAUDE_ENV_FILE"
    else
      echo "    WARNING: no CLAUDE_ENV_FILE to export it through -- set it by hand, or every browser test SKIPS"
    fi
  else
    echo "==> WARNING: no Chromium launches through playwright $pw_version -- every browser test"
    echo "    (tests/integration/test_browser_smoke.py, the website tests) will SKIP, not pass."
    echo "    Default launch failed with:"
    printf '%s\n' "$default_out" | grep -m1 -E "Executable doesn't exist|Error" | sed 's/^/      /' || true
    echo "    Browsers in $BROWSERS: $(ls "$BROWSERS" 2>/dev/null | tr '\n' ' ')"
  fi
fi

echo "==> Ready. See ci.dispatchable in .claude/toolkit.yaml and .claude/skills/steward/SKILL.md for what to run off-box."
