"""The example plugin build script: its arguments and the files it writes, with PyInstaller faked."""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404  # runs the repo's own script with --help only
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_example_plugin.py"

_spec = importlib.util.spec_from_file_location("build_example_plugin", SCRIPT)
assert _spec is not None and _spec.loader is not None
build_example_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_example_plugin)


class FakePyInstaller:
    """Records the argv and leaves an executable where PyInstaller would."""

    def __init__(self) -> None:
        self.argv: list[str] = []

    def __call__(self, argv: list[str], check: bool) -> None:
        assert check is True
        self.argv = argv
        dist = Path(argv[argv.index("--distpath") + 1])
        (dist / argv[argv.index("--name") + 1]).write_text("binary", encoding="utf-8")


@pytest.fixture
def pyinstaller(monkeypatch: pytest.MonkeyPatch) -> FakePyInstaller:
    fake = FakePyInstaller()
    monkeypatch.setattr(build_example_plugin.subprocess, "run", fake)
    return fake


def test_help_exits_zero() -> None:
    result = subprocess.run(  # nosec B603  # fixed argv, no shell
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and "--with-crash-tool" in result.stdout


def test_the_example_name_is_required_and_checked() -> None:
    with pytest.raises(SystemExit) as missing:
        build_example_plugin.main([])
    with pytest.raises(SystemExit) as unknown:
        build_example_plugin.main(["nothing"])
    assert missing.value.code == 2 and unknown.value.code == 2


def test_it_runs_a_one_file_pyinstaller_build_that_bundles_the_sdk(pyinstaller, tmp_path, capsys) -> None:
    assert build_example_plugin.main(["today", "--out", str(tmp_path)]) == 0
    argv = pyinstaller.argv
    assert argv[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "--onefile" in argv
    assert argv[argv.index("--name") + 1] == "today-plugin"
    paths = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--paths"]
    assert paths == [str(REPO_ROOT / "plugin-sdk" / "src"), str(REPO_ROOT / "examples" / "plugins" / "today")]
    assert argv[-1] == str(REPO_ROOT / "examples" / "plugins" / "today" / "today_plugin.py")
    assert capsys.readouterr().out.strip() == str(tmp_path / "today")


def test_it_writes_the_manifest_and_build_flags_beside_the_executable(pyinstaller, tmp_path) -> None:
    build_example_plugin.main(["today", "--out", str(tmp_path)])
    folder = tmp_path / "today"
    assert sorted(p.name for p in folder.iterdir()) == ["build-flags.json", "privacyfence-plugin.yaml", "today-plugin"]
    assert json.loads((folder / "build-flags.json").read_text(encoding="utf-8")) == {"crash_tool": False}
    expected = (REPO_ROOT / "examples" / "plugins" / "today" / "privacyfence-plugin.yaml").read_text(encoding="utf-8")
    assert (folder / "privacyfence-plugin.yaml").read_text(encoding="utf-8") == expected


def test_with_crash_tool_sets_the_flag(pyinstaller, tmp_path) -> None:
    build_example_plugin.main(["today", "--with-crash-tool", "--out", str(tmp_path)])
    assert json.loads((tmp_path / "today" / "build-flags.json").read_text(encoding="utf-8")) == {"crash_tool": True}


def test_the_default_output_folder_is_dist_plugins(pyinstaller, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    build_example_plugin.main(["today"])
    assert (tmp_path / "dist" / "plugins" / "today" / "today-plugin").exists()


def test_pyinstallers_work_and_spec_files_stay_out_of_the_checkout(pyinstaller, tmp_path) -> None:
    build_example_plugin.main(["today", "--out", str(tmp_path)])
    argv = pyinstaller.argv
    for flag in ("--workpath", "--specpath"):
        assert not Path(argv[argv.index(flag) + 1]).is_relative_to(REPO_ROOT)


def test_a_failed_build_raises_and_writes_no_flags(monkeypatch, tmp_path) -> None:
    def failing(argv, check):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(build_example_plugin.subprocess, "run", failing)
    with pytest.raises(subprocess.CalledProcessError):
        build_example_plugin.main(["today", "--out", str(tmp_path)])
    assert not (tmp_path / "today" / "build-flags.json").exists()
