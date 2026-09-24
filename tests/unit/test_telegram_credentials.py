"""Tests for scripts/telegram_credentials.py -- the one generator of the build-time Telegram app
credentials module, and the check publish-pypi.yml runs over the built wheel and sdist (ADR 0040).

Imported by file path, same pattern as tests/unit/test_changelog_section.py.
"""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = REPO_ROOT / "scripts" / "telegram_credentials.py"
_spec = importlib.util.spec_from_file_location("telegram_credentials", _SCRIPT_PATH)
telegram_credentials = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telegram_credentials)

MODULE = "privacyfence/_telegram_credentials.py"


def _wheel(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, "")
    return path


def _sdist(path: Path, names: list[str]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name in names:
            info = tarfile.TarInfo(name)
            tf.addfile(info, io.BytesIO(b""))
    return path


def test_write_generates_importable_module(tmp_path: Path) -> None:
    creds = tmp_path / "_telegram_credentials.py"
    env = {"TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "0123456789abcdef"}

    assert telegram_credentials.write(creds, env) == 0

    namespace: dict[str, object] = {}
    exec(creds.read_text(encoding="utf-8"), namespace)  # noqa: S102 -- file this test just wrote
    assert namespace["API_ID"] == 12345
    assert namespace["API_HASH"] == "0123456789abcdef"


def test_write_quotes_the_hash_rather_than_interpolating_it(tmp_path: Path) -> None:
    creds = tmp_path / "_telegram_credentials.py"
    hostile = 'abc"\nimport os\nx = "'

    assert telegram_credentials.write(creds, {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": hostile}) == 0

    namespace: dict[str, object] = {}
    exec(creds.read_text(encoding="utf-8"), namespace)  # noqa: S102 -- file this test just wrote
    assert namespace["API_HASH"] == hostile
    assert "os" not in namespace


@pytest.mark.parametrize(
    "env",
    [{}, {"TELEGRAM_API_ID": "1"}, {"TELEGRAM_API_HASH": "abc"}, {"TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": "abc"}],
)
def test_write_without_both_secrets_removes_stale_module(tmp_path: Path, env: dict[str, str]) -> None:
    creds = tmp_path / "_telegram_credentials.py"
    creds.write_text("API_ID = 1\nAPI_HASH = 'stale'\n", encoding="utf-8")

    assert telegram_credentials.write(creds, env) == 0
    assert not creds.exists()
    assert telegram_credentials.write(creds, env) == 0  # nothing to remove is fine too


def test_write_rejects_non_integer_api_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    creds = tmp_path / "_telegram_credentials.py"

    assert telegram_credentials.write(creds, {"TELEGRAM_API_ID": "12a", "TELEGRAM_API_HASH": "x"}) == 2
    assert not creds.exists()
    assert "not an integer" in capsys.readouterr().err


def test_write_output_is_ascii(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # build_installer.ps1 runs this on Windows, where a piped stdout is not UTF-8.
    telegram_credentials.write(tmp_path / "a.py", {})
    telegram_credentials.write(tmp_path / "b.py", {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x"})
    capsys.readouterr().out.encode("ascii")


def test_check_dist_passes_when_every_archive_has_the_module(tmp_path: Path) -> None:
    whl = _wheel(tmp_path / "p-1-py3-none-any.whl", ["privacyfence/__init__.py", MODULE])
    sdist = _sdist(tmp_path / "p-1.tar.gz", ["p-1/src/privacyfence/__init__.py", f"p-1/src/{MODULE}"])

    assert telegram_credentials.check_dist([whl, sdist], require=True) == 0


def test_check_dist_fails_on_stable_when_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    whl = _wheel(tmp_path / "p-1-py3-none-any.whl", [MODULE])
    sdist = _sdist(tmp_path / "p-1.tar.gz", ["p-1/src/privacyfence/__init__.py"])

    assert telegram_credentials.check_dist([whl, sdist], require=True) == 1
    out = capsys.readouterr().out
    assert out.count("::error::") == 1
    assert "p-1.tar.gz" in out.split("::error::")[1]
    assert "p-1-py3-none-any.whl" not in out.split("::error::")[1]


def test_check_dist_only_warns_otherwise(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    whl = _wheel(tmp_path / "p-1-py3-none-any.whl", ["privacyfence/__init__.py"])

    assert telegram_credentials.check_dist([whl], require=False) == 0
    assert "::warning::" in capsys.readouterr().out


def test_check_dist_with_no_files_is_an_error() -> None:
    assert telegram_credentials.check_dist([], require=False) == 2


def test_check_dist_rejects_unknown_archive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a wheel or sdist"):
        telegram_credentials.check_dist([tmp_path / "p-1.zip"], require=False)


def test_main_dispatches_both_subcommands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = tmp_path / "_telegram_credentials.py"
    monkeypatch.setattr(telegram_credentials, "CREDS_PATH", creds)
    monkeypatch.setenv("TELEGRAM_API_ID", "7")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    assert telegram_credentials.main(["write"]) == 0
    assert creds.exists()

    whl = _wheel(tmp_path / "p-1-py3-none-any.whl", [])
    assert telegram_credentials.main(["check-dist", "--require", str(whl)]) == 1


def test_generated_module_is_git_ignored_but_packaged() -> None:
    # The module must never be committed (the repo is public) and must still reach the sdist --
    # setuptools_scm only packages tracked files, so MANIFEST.in's explicit include is what does it.
    relative = telegram_credentials.CREDS_PATH.relative_to(REPO_ROOT).as_posix()
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()

    assert relative in gitignore
    assert f"include {relative}" in manifest


def test_build_scripts_and_pypi_workflow_share_the_generator() -> None:
    callers = [
        "scripts/build_dmg.sh",
        "scripts/build_deb.sh",
        "scripts/build_installer.ps1",
        ".github/workflows/publish-pypi.yml",
    ]
    for caller in callers:
        text = (REPO_ROOT / caller).read_text(encoding="utf-8")
        assert "scripts/telegram_credentials.py write" in text, caller
        assert "API_HASH = " not in text, f"{caller} carries its own copy of the generator"

    workflow = (REPO_ROOT / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")
    assert "scripts/telegram_credentials.py check-dist" in workflow
    assert "secrets.TELEGRAM_API_ID" in workflow
    assert "secrets.TELEGRAM_API_HASH" in workflow
