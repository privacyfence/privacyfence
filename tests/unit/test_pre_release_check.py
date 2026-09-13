"""Tests for scripts/pre_release_check.py's ``run()`` helper.

Imported by file path (importlib) rather than as a package, since scripts/ isn't part of the
installed ``privacyfence`` distribution -- same pattern as tests/unit/test_r2_release.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "pre_release_check.py"
_spec = importlib.util.spec_from_file_location("pre_release_check", _SCRIPT_PATH)
pre_release_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pre_release_check)


class TestRun:
    def test_missing_tool_fails_cleanly_instead_of_raising(self, tmp_path, capsys):
        # A tool absent from PATH (e.g. no `npm` installed, or a venv never `pip install`d with
        # the `test`/`lint` extras) used to raise FileNotFoundError out of subprocess.run() and
        # crash main() entirely -- skipping every check queued after it. It should instead be
        # reported as an ordinary FAIL so the rest of the checks still run.
        ok = pre_release_check.run(
            "missing tool", ["this-binary-does-not-exist-privacyfence-test"], cwd=tmp_path
        )

        assert ok is False
        assert "not found on PATH" in capsys.readouterr().out

    def test_successful_command_still_passes(self, tmp_path, capsys):
        ok = pre_release_check.run("echo", ["python3", "-c", "pass"], cwd=tmp_path)

        assert ok is True
        assert "PASS" in capsys.readouterr().out
