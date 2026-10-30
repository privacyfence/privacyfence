"""Tests for scripts/builtin_apps.py -- the one generator of the build-time built-in OAuth apps
module, and the check build.yml and publish-pypi.yml run over the built wheel and sdist (ADR 0255).

Imported by file path, same pattern as tests/unit/test_telegram_credentials.py.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = REPO_ROOT / "scripts" / "builtin_apps.py"
_spec = importlib.util.spec_from_file_location("builtin_apps", _SCRIPT_PATH)
builtin_apps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builtin_apps)

MODULE = "privacyfence/_builtin_apps.py"
ALL_KEYS = ("google_web", "slack", "salesforce", "atlassian_relay", "miro", "google_signin")


def _env(*keys: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in keys:
        env[f"PF_APP_{key.upper()}_CLIENT_ID"] = f"{key}-id"
        env[f"PF_APP_{key.upper()}_CLIENT_SECRET"] = f"{key}-secret"
    return env


def _apps(creds: Path) -> dict[str, dict[str, str]]:
    namespace: dict[str, object] = {}
    exec(creds.read_text(encoding="utf-8"), namespace)  # noqa: S102 -- file this test just wrote
    return namespace["APPS"]  # type: ignore[return-value]


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


def test_keys_are_the_six_apps_in_table_order() -> None:
    assert builtin_apps.KEYS == ALL_KEYS


def test_write_generates_importable_module(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    creds = tmp_path / "_builtin_apps.py"
    env = _env(*ALL_KEYS)

    rc = builtin_apps.write(creds, env)

    assert rc == 0
    assert _apps(creds) == {
        key: {"client_id": f"{key}-id", "client_secret": f"{key}-secret"} for key in ALL_KEYS
    }
    out = capsys.readouterr().out
    for key in ALL_KEYS:
        assert f"built-in app {key}: included" in out


def test_render_is_exactly_one_sorted_assignment() -> None:
    apps = {"slack": {"client_id": "a", "client_secret": "b"}, "miro": {"client_id": "c", "client_secret": "d"}}

    assert builtin_apps.render(apps) == "APPS = " + repr(dict(sorted(apps.items()))) + "\n"
    assert builtin_apps.render(apps).startswith("APPS = {'miro'")


def test_write_quotes_values_rather_than_interpolating_them(tmp_path: Path) -> None:
    creds = tmp_path / "_builtin_apps.py"
    hostile = 'abc"\'\\\timport os; x = "'
    env = {"PF_APP_SLACK_CLIENT_ID": hostile, "PF_APP_SLACK_CLIENT_SECRET": 'q"\'\\z'}

    rc = builtin_apps.write(creds, env)

    assert rc == 0
    text = creds.read_text(encoding="utf-8")
    assert ast.literal_eval(text.removeprefix("APPS = ")) == {
        "slack": {"client_id": hostile, "client_secret": 'q"\'\\z'}
    }
    assert "os" not in _apps(creds) and len(text.splitlines()) == 1


@pytest.mark.parametrize("missing", ["PF_APP_SLACK_CLIENT_ID", "PF_APP_SLACK_CLIENT_SECRET"])
def test_write_with_one_of_two_set_warns_and_skips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    creds = tmp_path / "_builtin_apps.py"
    env = _env("slack", "miro")
    del env[missing]

    rc = builtin_apps.write(creds, env)

    assert rc == 0
    assert set(_apps(creds)) == {"miro"}
    out = capsys.readouterr().out
    assert "warning: PF_APP_SLACK_CLIENT_ID and PF_APP_SLACK_CLIENT_SECRET must both be set; skipping slack." in out
    assert "built-in app slack: not set" in out
    assert "built-in app miro: included" in out


@pytest.mark.parametrize("env", [{}, {"PF_APP_SLACK_CLIENT_ID": "x"}, {"PF_APP_SLACK_CLIENT_ID": "", "PF_APP_SLACK_CLIENT_SECRET": ""}])
def test_write_with_no_complete_key_removes_stale_module(tmp_path: Path, env: dict[str, str]) -> None:
    creds = tmp_path / "_builtin_apps.py"
    creds.write_text("APPS = {'stale': {}}\n", encoding="utf-8")

    assert builtin_apps.write(creds, env) == 0
    assert not creds.exists()
    assert builtin_apps.write(creds, env) == 0  # nothing to remove is fine too


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\0b"])
def test_write_rejects_control_characters(tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str) -> None:
    creds = tmp_path / "_builtin_apps.py"
    env = _env("miro")
    env["PF_APP_SLACK_CLIENT_ID"] = bad
    env["PF_APP_SLACK_CLIENT_SECRET"] = "s"

    rc = builtin_apps.write(creds, env)

    assert rc == 2
    assert not creds.exists()
    assert "error: PF_APP_SLACK_CLIENT_ID contains a control character." in capsys.readouterr().err


def test_write_rejects_control_character_in_the_secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    creds = tmp_path / "_builtin_apps.py"

    rc = builtin_apps.write(creds, {"PF_APP_MIRO_CLIENT_ID": "i", "PF_APP_MIRO_CLIENT_SECRET": "s\n"})

    assert rc == 2
    assert not creds.exists()
    assert "PF_APP_MIRO_CLIENT_SECRET contains a control character" in capsys.readouterr().err


def test_write_stores_a_salesforce_login_install_url(tmp_path: Path) -> None:
    creds = tmp_path / "_builtin_apps.py"
    url = "https://login.salesforce.com/packaging/installPackage.apexp?p0=04t000000000000AAA"
    env = {**_env("salesforce", "slack"), "PF_APP_SALESFORCE_INSTALL_URL": url}

    assert builtin_apps.write(creds, env) == 0

    apps = _apps(creds)
    assert apps["salesforce"] == {"client_id": "salesforce-id", "client_secret": "salesforce-secret", "install_url": url}
    assert "install_url" not in apps["slack"]


@pytest.mark.parametrize(
    "url",
    [
        "http://login.salesforce.com/packaging/installPackage.apexp?p0=04t",
        "https://evil.example/packaging/installPackage.apexp?p0=04t",
        "https://login.salesforce.com.evil.example/x",
    ],
)
def test_write_warns_and_omits_a_foreign_install_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], url: str
) -> None:
    creds = tmp_path / "_builtin_apps.py"

    rc = builtin_apps.write(creds, {**_env("salesforce"), "PF_APP_SALESFORCE_INSTALL_URL": url})

    assert rc == 0
    assert "install_url" not in _apps(creds)["salesforce"]
    assert "warning: PF_APP_SALESFORCE_INSTALL_URL must start with https://login.salesforce.com/" in capsys.readouterr().out


def test_write_rejects_control_character_in_install_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    creds = tmp_path / "_builtin_apps.py"

    rc = builtin_apps.write(creds, {**_env("salesforce"), "PF_APP_SALESFORCE_INSTALL_URL": "https://login.salesforce.com/\nx"})

    assert rc == 2
    assert not creds.exists()
    assert "PF_APP_SALESFORCE_INSTALL_URL contains a control character" in capsys.readouterr().err


def test_write_output_is_ascii(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    builtin_apps.write(tmp_path / "a.py", {})
    builtin_apps.write(tmp_path / "b.py", _env("slack"))
    builtin_apps.write(tmp_path / "c.py", {"PF_APP_SLACK_CLIENT_ID": "x"})
    capsys.readouterr().out.encode("ascii")


def test_check_dist_passes_when_every_archive_has_the_module(tmp_path: Path) -> None:
    whl = _wheel(tmp_path / "p-1-py3-none-any.whl", ["privacyfence/__init__.py", MODULE])
    sdist = _sdist(tmp_path / "p-1.tar.gz", ["p-1/src/privacyfence/__init__.py", f"p-1/src/{MODULE}"])

    assert builtin_apps.check_dist([whl, sdist], require=True) == 0


def test_check_dist_fails_when_required_and_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    whl = _wheel(tmp_path / "p-1-py3-none-any.whl", [MODULE])
    sdist = _sdist(tmp_path / "p-1.tar.gz", ["p-1/src/privacyfence/__init__.py"])

    rc = builtin_apps.check_dist([whl, sdist], require=True)

    assert rc == 1
    out = capsys.readouterr().out
    assert out.count("::error::") == 1
    assert "p-1.tar.gz" in out.split("::error::")[1]
    assert "p-1-py3-none-any.whl" not in out.split("::error::")[1]
    assert "built-in OAuth apps will not work on this install. A stable release must ship it (ADR 0255)." in out


def test_check_dist_only_warns_otherwise(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    whl = _wheel(tmp_path / "p-1-py3-none-any.whl", ["privacyfence/__init__.py"])

    assert builtin_apps.check_dist([whl], require=False) == 0
    assert "::warning::" in capsys.readouterr().out


def test_check_dist_with_no_files_is_an_error() -> None:
    assert builtin_apps.check_dist([], require=False) == 2


def test_check_dist_rejects_unknown_archive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a wheel or sdist"):
        builtin_apps.check_dist([tmp_path / "p-1.zip"], require=False)


def test_main_dispatches_both_subcommands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = tmp_path / "_builtin_apps.py"
    monkeypatch.setattr(builtin_apps, "CREDS_PATH", creds)
    monkeypatch.setenv("PF_APP_MIRO_CLIENT_ID", "i")
    monkeypatch.setenv("PF_APP_MIRO_CLIENT_SECRET", "s")
    assert builtin_apps.main(["write"]) == 0
    assert creds.exists()

    whl = _wheel(tmp_path / "p-1-py3-none-any.whl", [])
    assert builtin_apps.main(["check-dist", "--require", str(whl)]) == 1


def test_generated_module_is_git_ignored_but_packaged() -> None:
    relative = builtin_apps.CREDS_PATH.relative_to(REPO_ROOT).as_posix()
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()

    assert relative in gitignore
    assert f"include {relative}" in manifest


def _build_org_image_job(workflow: str) -> str:
    start = workflow.index("  build-org-image:")
    following = [i for i in (workflow.find("\n  " + name + ":", start + 1) for name in _other_jobs(workflow)) if i > 0]
    return workflow[start : min(following)] if following else workflow[start:]


def _other_jobs(workflow: str) -> list[str]:
    jobs = workflow[workflow.index("\njobs:") :]
    names = [line.strip().rstrip(":") for line in jobs.splitlines() if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":")]
    return [n for n in names if n != "build-org-image"]


def test_server_builds_call_the_generator_and_desktop_builds_do_not() -> None:
    build = (REPO_ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
    pypi = (REPO_ROOT / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")
    org_image = _build_org_image_job(build)

    assert "scripts/builtin_apps.py write" in org_image
    assert "scripts/builtin_apps.py write" in pypi
    assert "scripts/builtin_apps.py check-dist" in build
    assert "scripts/builtin_apps.py check-dist" in pypi

    desktop = build.replace(org_image, "")
    assert "builtin_apps.py" not in desktop
    assert "PF_APP_" not in desktop
    for installer in ("scripts/build_dmg.sh", "scripts/build_deb.sh", "scripts/build_installer.ps1"):
        text = (REPO_ROOT / installer).read_text(encoding="utf-8")
        assert "builtin_apps.py" not in text, installer
        assert "PF_APP_" not in text, installer

    for name, text in (("build.yml", build), ("publish-pypi.yml", pypi)):
        assert "APPS = " not in text, f"{name} carries its own copy of the generator"


def test_secrets_reach_only_the_write_step() -> None:
    build = (REPO_ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
    pypi = (REPO_ROOT / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")

    for text in (_build_org_image_job(build), pypi):
        step = text[text.index("- name: Write built-in app credentials") :]
        step = step[: step.index("run: python3 scripts/builtin_apps.py write")]
        for key in builtin_apps.KEYS:
            for field in ("CLIENT_ID", "CLIENT_SECRET"):
                var = f"PF_APP_{key.upper()}_{field}"
                assert f"{var}: ${{{{ secrets.{var} }}}}" in step
                assert text.count(f"secrets.{var}") == 1, f"{var} is referenced outside the write step"
        assert "PF_APP_SALESFORCE_INSTALL_URL: ${{ vars.PF_APP_SALESFORCE_INSTALL_URL }}" in step
