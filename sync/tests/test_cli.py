from __future__ import annotations

import asyncio
import runpy
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import RecipeWriter

from mcp.shared.auth import OAuthToken
from mcp.shared.exceptions import MCPError

from privacyfence_sync import cli
from privacyfence_sync.auth import FileTokenStorage, storage_path
from privacyfence_sync.client import SignInError, SyncError, ToolDenied, ToolFailed


class FakeSession:
    """An async-context-manager stand-in for SyncClient, scripted per tool."""

    def __init__(self, results: dict[str, Any] | None = None, enter_error: BaseException | None = None) -> None:
        self.results = results or {}
        self.enter_error = enter_error
        self.calls: list[str] = []

    async def __aenter__(self) -> FakeSession:
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        self.calls.append(tool)
        item = self.results.get(tool, {})
        if isinstance(item, BaseException):
            raise item
        return item


class Daemon:
    """What connect_token was asked for, and the session it hands out."""

    def __init__(self) -> None:
        self.session = FakeSession()
        self.servers: list[str] = []


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> Daemon:
    fake = Daemon()

    def connect(server: str, token: str) -> FakeSession:
        fake.servers.append(server)
        return fake.session

    monkeypatch.setattr(cli, "connect_token", connect)
    monkeypatch.setenv("PRIVACYFENCE_TOKEN", "secret-token")
    monkeypatch.delenv("PRIVACYFENCE_MCP_URL", raising=False)
    return fake


def simple(write_recipe: RecipeWriter, **top: Any) -> str:
    return str(write_recipe([{"id": "one", "tool": "drive_list_files"}], **top))


def test_check_prints_ok_without_network(
    write_recipe: RecipeWriter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_: object) -> None:
        raise AssertionError("no network in check")

    monkeypatch.setattr(cli, "connect_token", boom)
    assert cli.main(["check", simple(write_recipe)]) == 0
    assert capsys.readouterr().out == "demo: 1 step(s), OK\n"


def test_check_bad_recipe_exits_1(write_recipe: RecipeWriter, capsys: pytest.CaptureFixture[str]) -> None:
    recipe = write_recipe([{"id": "one", "tool": "drive_list_files", "bogus": 1}])
    assert cli.main(["check", str(recipe)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("privacyfence-sync: ") and "unknown key bogus" in err


def test_run_without_token_exits_4(
    write_recipe: RecipeWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PRIVACYFENCE_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    assert cli.main(["run", simple(write_recipe)]) == 4
    assert capsys.readouterr().err == (
        "privacyfence-sync: Not signed in to http://127.0.0.1:8765/mcp, or the sign-in expired. "
        "Run: privacyfence-sync login --server http://127.0.0.1:8765/mcp\n"
    )


def test_run_without_token_uses_oauth(
    write_recipe: RecipeWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PRIVACYFENCE_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    server = "http://127.0.0.1:8765/mcp"
    asyncio.run(FileTokenStorage(storage_path(server)).set_tokens(OAuthToken(access_token="a", token_type="Bearer")))
    session = FakeSession()
    seen: list[tuple[str, FileTokenStorage]] = []

    def connect(url: str, storage: FileTokenStorage) -> FakeSession:
        seen.append((url, storage))
        return session

    monkeypatch.setattr(cli, "connect_oauth", connect)
    assert cli.main(["run", simple(write_recipe)]) == 0
    assert [url for url, _ in seen] == [server]
    assert "drive_list_files" in session.calls


def test_login_runs_status_and_prints_signed_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession({"privacyfence_status": {"ok": True}})
    seen: dict[str, Any] = {}

    def connect(url: str, storage: FileTokenStorage, *, interactive: bool, callback_port: int) -> FakeSession:
        seen.update(url=url, interactive=interactive, port=callback_port)
        return session

    monkeypatch.setattr(cli, "connect_oauth", connect)
    assert cli.main(["login", "--server", "https://pf.example/mcp", "--callback-port", "4000"]) == 0
    assert capsys.readouterr().out == "Signed in to https://pf.example/mcp.\n"
    assert seen == {"url": "https://pf.example/mcp", "interactive": True, "port": 4000}
    assert session.calls == ["privacyfence_status"]


def test_login_unwraps_task_group_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    group = ExceptionGroup("unhandled errors in a TaskGroup", [SyncError("No sign-in arrived within 5 minutes.")])
    monkeypatch.setattr(cli, "connect_oauth", lambda *a, **k: FakeSession(enter_error=group))
    assert cli.main(["login", "--server", "http://x/mcp"]) == 2
    assert capsys.readouterr().err == "privacyfence-sync: No sign-in arrived within 5 minutes.\n"


def test_token_env_names_another_variable(
    write_recipe: RecipeWriter, daemon: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRIVACYFENCE_TOKEN")
    monkeypatch.setenv("MY_TOKEN", "t")
    assert cli.main(["run", simple(write_recipe), "--token-env", "MY_TOKEN"]) == 0


def test_server_resolution_order(write_recipe: RecipeWriter, daemon: Daemon, monkeypatch: pytest.MonkeyPatch) -> None:
    servers = daemon.servers
    plain = simple(write_recipe)

    assert cli.main(["run", plain]) == 0
    assert servers[-1] == "http://127.0.0.1:8765/mcp"
    monkeypatch.setenv("PRIVACYFENCE_MCP_URL", "http://env/mcp")
    assert cli.main(["run", plain]) == 0
    assert servers[-1] == "http://env/mcp"
    with_server = simple(write_recipe, server="http://recipe/mcp")
    assert cli.main(["run", with_server]) == 0
    assert servers[-1] == "http://recipe/mcp"
    assert cli.main(["run", with_server, "--server", "http://flag/mcp"]) == 0
    assert servers[-1] == "http://flag/mcp"


def test_token_is_never_an_argument(write_recipe: RecipeWriter, capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli._parser()
    options = {
        o for action in parser._subparsers._group_actions[0].choices["run"]._actions for o in action.option_strings
    }  # type: ignore[union-attr]
    assert "--token" not in options
    assert cli.main(["run", simple(write_recipe), "--token", "hunter2"]) == 1
    assert "hunter2" not in capsys.readouterr().out


def test_help_exits_0_and_a_usage_error_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert cli.main([]) == 1
    capsys.readouterr()


def test_denied_step_exits_3(write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]) -> None:
    daemon.session = FakeSession({"drive_list_files": ToolDenied("Request denied: no rule")})
    assert cli.main(["run", simple(write_recipe)]) == 3
    err = capsys.readouterr().err
    assert err == "privacyfence-sync: Request denied: no rule\n"
    assert "secret-token" not in err


def test_failed_step_exits_2(write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]) -> None:
    daemon.session = FakeSession({"drive_list_files": ToolFailed("Drive said no")})
    assert cli.main(["run", simple(write_recipe)]) == 2
    assert capsys.readouterr().err == "privacyfence-sync: Drive said no\n"


def test_failed_script_exits_2(
    write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = write_recipe([{"id": "s", "run": "scripts/bad.py"}])
    (tmp_path / "scripts" / "bad.py").write_text("import sys\nsys.exit(3)\n")
    assert cli.main(["run", str(recipe)]) == 2
    assert "step s:" in capsys.readouterr().err.splitlines()[-1]


def test_stopped_run_exits_0(
    write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = write_recipe([{"id": "s", "run": "scripts/stop.py"}, {"id": "after", "tool": "drive_list_files"}])
    (tmp_path / "scripts" / "stop.py").write_text("import sys\nsys.exit(10)\n")
    assert cli.main(["run", str(recipe)]) == 0
    assert "[s] nothing to do, run stopped" in capsys.readouterr().out
    assert "drive_list_files" not in daemon.session.calls


def test_runtime_reference_error_exits_1(
    write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "pick.py").write_text("open('p.json', 'w').write('{}')\n")
    steps = [
        {"id": "pick", "run": "scripts/pick.py", "outputs": {"j": "p.json"}},
        {"id": "one", "tool": "drive_list_files", "args": {"q": "${pick.j:missing}"}},
    ]
    assert cli.main(["run", str(write_recipe(steps))]) == 1
    assert capsys.readouterr().err == "privacyfence-sync: step one: missing not found\n"


def test_bad_template_exits_1(
    write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "pick.py").write_text("open('p.json', 'w').write('{}')\n")
    steps = [
        {"id": "pick", "run": "scripts/pick.py", "outputs": {"j": "p.json"}},
        {"id": "page", "template": "t.html", "data": "pick.j", "output": "page.html"},
    ]
    recipe = write_recipe(steps)
    (tmp_path / "t.html").write_text("no placeholder")
    assert cli.main(["run", str(recipe)]) == 1
    assert "exactly once" in capsys.readouterr().err


def test_sign_in_error_exits_4(write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]) -> None:
    daemon.session = FakeSession(enter_error=SignInError("Not signed in to http://x/mcp"))
    assert cli.main(["run", simple(write_recipe)]) == 4
    assert capsys.readouterr().err == "privacyfence-sync: Not signed in to http://x/mcp\n"


def test_refused_handshake_exits_2_with_a_hint(
    write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.session = FakeSession(enter_error=MCPError(code=-32603, message="Server returned an error response"))
    assert cli.main(["run", simple(write_recipe)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("privacyfence-sync: the server refused the connection (Server returned an error response)")
    assert "token" in err


def test_errors_wrapped_in_a_task_group_are_unwrapped(
    write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [ToolDenied("Request denied")])])
    daemon.session = FakeSession({"drive_list_files": group})
    assert cli.main(["run", simple(write_recipe)]) == 3
    assert capsys.readouterr().err == "privacyfence-sync: Request denied\n"


def test_unreachable_server_exits_2(
    write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.session = FakeSession(enter_error=httpx2.ConnectError("refused"))
    assert cli.main(["run", simple(write_recipe)]) == 2
    assert capsys.readouterr().err == "privacyfence-sync: cannot reach the server: refused\n"


def test_running_the_module_exits_with_main_status(
    write_recipe: RecipeWriter, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["privacyfence-sync", "check", simple(write_recipe)])
    with pytest.warns(RuntimeWarning), pytest.raises(SystemExit) as caught:
        runpy.run_module("privacyfence_sync.cli", run_name="__main__")
    assert caught.value.code == 0
    assert "OK" in capsys.readouterr().out
