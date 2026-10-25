"""Tests for scripts/check_coverage_floor.py.

The ratchet gates every merge, so it must be able to fail: each case below writes a small
``coverage.json`` and checks the exit code and the report the script prints.

Imported by file path (importlib), same pattern as tests/unit/test_tag_release.py -- scripts/
isn't part of the installed ``privacyfence`` distribution.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_coverage_floor.py"
_spec = importlib.util.spec_from_file_location("check_coverage_floor", _SCRIPT_PATH)
check_coverage_floor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_coverage_floor)


def _write_report(tmp_path: Path, *, overall: float | None = None, overrides: dict[str, float] | None = None) -> Path:
    """A coverage.json with every MODULE_FLOORS entry exactly at its floor, ``overall`` at the
    overall floor, and ``overrides`` (module -> percent, or None to drop the module) applied."""
    files = {
        module: {"summary": {"percent_covered": floor}} for module, floor in check_coverage_floor.MODULE_FLOORS.items()
    }
    for module, percent in (overrides or {}).items():
        if percent is None:
            files.pop(module)
        else:
            files[module] = {"summary": {"percent_covered": percent}}
    total = check_coverage_floor.OVERALL_FLOOR if overall is None else overall
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"totals": {"percent_covered": total}, "files": files}))
    return path


_MODULE = next(iter(check_coverage_floor.MODULE_FLOORS))
_MODULE_FLOOR = check_coverage_floor.MODULE_FLOORS[_MODULE]


def test_everything_at_its_floor_passes(tmp_path, capsys):
    assert check_coverage_floor.main([str(_write_report(tmp_path))]) == 0
    assert "Coverage ratchet passed" in capsys.readouterr().out


def test_overall_below_floor_fails(tmp_path, capsys):
    report = _write_report(tmp_path, overall=check_coverage_floor.OVERALL_FLOOR - 0.5)
    assert check_coverage_floor.main([str(report)]) == 1
    out = capsys.readouterr().out
    assert "Coverage ratchet failed" in out
    assert "overall coverage" in out


def test_one_module_below_its_floor_fails_and_is_named(tmp_path, capsys):
    report = _write_report(tmp_path, overrides={_MODULE: _MODULE_FLOOR - 3})
    assert check_coverage_floor.main([str(report)]) == 1
    out = capsys.readouterr().out
    assert _MODULE in out
    assert "[FAIL]" in out


def test_module_missing_from_report_fails(tmp_path, capsys):
    report = _write_report(tmp_path, overrides={_MODULE: None})
    assert check_coverage_floor.main([str(report)]) == 1
    assert "[MISSING]" in capsys.readouterr().out


def test_missing_report_file_exits_2(tmp_path, capsys):
    assert check_coverage_floor.main([str(tmp_path / "nope.json")]) == 2
    assert "not found" in capsys.readouterr().err


def test_module_two_points_above_floor_warns_but_passes(tmp_path, capsys):
    report = _write_report(tmp_path, overrides={_MODULE: _MODULE_FLOOR + 2})
    assert check_coverage_floor.main([str(report)]) == 0
    out = capsys.readouterr().out
    assert "Coverage ratchet passed" in out
    warning = [line for line in out.splitlines() if line.startswith("::warning::stale coverage floor")]
    assert len(warning) == 1
    assert _MODULE in warning[0]


def test_overall_far_above_floor_warns_but_passes(tmp_path, capsys):
    report = _write_report(tmp_path, overall=check_coverage_floor.OVERALL_FLOOR + 1)
    assert check_coverage_floor.main([str(report)]) == 0
    out = capsys.readouterr().out
    assert any(line.startswith("::warning::stale coverage floor: overall") for line in out.splitlines())


@pytest.mark.parametrize("above", [0.0, 0.5, 1.0])
def test_small_headroom_does_not_warn(tmp_path, capsys, above):
    report = _write_report(tmp_path, overrides={_MODULE: _MODULE_FLOOR + above})
    assert check_coverage_floor.main([str(report)]) == 0
    assert "::warning::" not in capsys.readouterr().out


# --- --platform windows ----------------------------------------------------------------------- #

_WINDOWS_MODULE = next(iter(check_coverage_floor.WINDOWS_MODULE_FLOORS))
# Floors are 0.0 until measured, so tests pin their own values rather than depend on them.
_WINDOWS_FLOORS = {
    "src/privacyfence/windows_service.py": 50.0,
    "src/privacyfence/windows_acl.py": 80.0,
    "src/privacyfence/web/control_channel.py": 70.0,
}


@pytest.fixture
def windows_floors(monkeypatch):
    monkeypatch.setattr(check_coverage_floor, "WINDOWS_MODULE_FLOORS", dict(_WINDOWS_FLOORS))
    return _WINDOWS_FLOORS


def _write_windows_report(
    tmp_path: Path, *, overrides: dict[str, float | None] | None = None, backslashes: bool = False
) -> Path:
    """A Windows-shaped coverage.json: only the Windows modules, each at its floor, with the overall
    total far below OVERALL_FLOOR and no MODULE_FLOORS entries."""
    files = dict(_WINDOWS_FLOORS)
    for module, percent in (overrides or {}).items():
        if percent is None:
            files.pop(module)
        else:
            files[module] = percent
    if backslashes:
        files = {module.replace("/", "\\"): percent for module, percent in files.items()}
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps(
            {
                "totals": {"percent_covered": 40.0},
                "files": {module: {"summary": {"percent_covered": percent}} for module, percent in files.items()},
            }
        )
    )
    return path


def test_windows_mode_passes_when_every_windows_module_is_at_its_floor(tmp_path, capsys, windows_floors):
    assert check_coverage_floor.main(["--platform", "windows", str(_write_windows_report(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "Windows module floors:" in out
    assert "Coverage ratchet passed" in out


def test_windows_mode_fails_below_a_windows_floor(tmp_path, capsys, windows_floors):
    report = _write_windows_report(tmp_path, overrides={_WINDOWS_MODULE: windows_floors[_WINDOWS_MODULE] - 3})
    assert check_coverage_floor.main(["--platform", "windows", str(report)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert _WINDOWS_MODULE in out
    assert "Coverage ratchet failed" in out


def test_windows_mode_fails_when_a_windows_module_is_missing(tmp_path, capsys, windows_floors):
    report = _write_windows_report(tmp_path, overrides={_WINDOWS_MODULE: None})
    assert check_coverage_floor.main(["--platform", "windows", str(report)]) == 1
    assert "[MISSING]" in capsys.readouterr().out


def test_windows_mode_ignores_the_linux_floors_and_the_overall_floor(tmp_path, capsys, windows_floors):
    # The report has no MODULE_FLOORS entries and an overall total of 40%, both fatal in Linux mode.
    report = _write_windows_report(tmp_path)
    assert check_coverage_floor.main([str(report)]) == 1
    capsys.readouterr()
    assert check_coverage_floor.main(["--platform", "windows", str(report)]) == 0
    out = capsys.readouterr().out
    assert "Overall coverage" not in out
    assert "Security-critical module floors" not in out


def test_windows_backslash_paths_are_normalised(tmp_path, capsys, windows_floors):
    report = _write_windows_report(tmp_path, backslashes=True)
    assert check_coverage_floor.main(["--platform", "windows", str(report)]) == 0
    assert "[MISSING]" not in capsys.readouterr().out


def test_default_platform_is_linux(tmp_path, capsys):
    assert check_coverage_floor.main([str(_write_report(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "Overall coverage" in out
    assert "Windows module floors" not in out
