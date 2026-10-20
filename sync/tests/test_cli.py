from __future__ import annotations

import asyncio
import json
import os
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
from privacyfence_sync.transform import script_env
from privacyfence_sync.client import SignInError, SyncError, ToolDenied, ToolFailed


class FakeSession:
    """An async-context-manager stand-in for SyncClient, scripted per tool."""

    def __init__(self, results: dict[str, Any] | None = None, enter_error: BaseException | None = None) -> None:
        self.results = results or {}
        self.enter_error = enter_error
        self.calls: list[str] = []
        self.tool_names: frozenset[str] = frozenset()

    async def list_tool_names(self) -> frozenset[str]:
        return self.tool_names

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


def test_custom_token_env_is_hidden_from_scripts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MY_TOKEN", "t")
    seen: list[str] = []
    monkeypatch.setattr(cli, "connect_token", lambda server, token: seen.append(token) or FakeSession())
    cli._connect("http://x/mcp", "MY_TOKEN")
    assert seen == ["t"]
    assert "MY_TOKEN" not in os.environ
    assert "MY_TOKEN" not in script_env()


def test_server_resolution_order(write_recipe: RecipeWriter, daemon: Daemon, monkeypatch: pytest.MonkeyPatch) -> None:
    servers = daemon.servers

    def main(*argv: str) -> int:
        monkeypatch.setenv("PRIVACYFENCE_TOKEN", "secret-token")  # a run takes the token out of the environment
        return cli.main(list(argv))

    plain = simple(write_recipe)

    assert main("run", plain) == 0
    assert servers[-1] == "http://127.0.0.1:8765/mcp"
    monkeypatch.setenv("PRIVACYFENCE_MCP_URL", "http://env/mcp")
    assert main("run", plain) == 0
    assert servers[-1] == "http://env/mcp"
    with_server = simple(write_recipe, server="http://recipe/mcp")
    assert main("run", with_server) == 0
    assert servers[-1] == "http://recipe/mcp"
    assert main("run", with_server, "--server", "http://flag/mcp") == 0
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


_META = {
    "privacyfence_status",
    "privacyfence_check_policy",
    "privacyfence_create_upload_slot",
    "privacyfence_begin_unattended_session",
    "privacyfence_end_unattended_session",
}


def test_preflight_ok_exits_0(write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]) -> None:
    daemon.session.tool_names = frozenset(_META | {"drive_list_files"})
    daemon.session.results["privacyfence_check_policy"] = {"gate": "auto"}
    assert cli.main(["preflight", simple(write_recipe)]) == 0
    assert "preflight: 1 step(s) checked, 0 problem(s)" in capsys.readouterr().out
    assert "privacyfence_end_unattended_session" in daemon.session.calls


def test_preflight_problem_exits_3(
    write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["preflight", simple(write_recipe), "--attended"]) == 3
    captured = capsys.readouterr()
    assert "NOT AVAILABLE" in captured.out and captured.err == ""


def test_preflight_without_token_exits_4(
    write_recipe: RecipeWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PRIVACYFENCE_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    assert cli.main(["preflight", simple(write_recipe)]) == 4
    assert "privacyfence-sync login" in capsys.readouterr().err


def test_preflight_unwraps_task_group_errors(
    write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.session.enter_error = ExceptionGroup("tg", [SignInError("expired")])
    assert cli.main(["preflight", simple(write_recipe)]) == 4
    assert capsys.readouterr().err == "privacyfence-sync: expired\n"


def test_preflight_help_mentions_attended(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["preflight", "--help"]) == 0
    assert "--attended" in capsys.readouterr().out


def read_result(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_result_file_ok(write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path) -> None:
    result = tmp_path / "out" / "result.json"
    result.parent.mkdir()
    assert cli.main(["run", simple(write_recipe), "--result-file", str(result)]) == 0
    document = read_result(result)
    assert document["outcome"] == "ok" and document["recipe"] == "demo"
    assert document["exit_code"] == 0 and document["stopped_at_step"] is None


def test_result_file_no_change(write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path) -> None:
    recipe = write_recipe([{"id": "s", "run": "scripts/stop.py"}, {"id": "after", "tool": "drive_list_files"}])
    (tmp_path / "scripts" / "stop.py").write_text("import sys\nsys.exit(10)\n")
    result = tmp_path / "result.json"
    assert cli.main(["run", str(recipe), "--result-file", str(result)]) == 0
    document = read_result(result)
    assert document["outcome"] == "no_change" and document["stopped_at_step"] == "s"
    assert document["exit_code"] == 0


def test_result_file_denied(
    write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.session = FakeSession({"drive_list_files": ToolDenied("Request denied: no rule")})
    result = tmp_path / "result.json"
    assert cli.main(["run", simple(write_recipe), "--result-file", str(result)]) == 3
    document = read_result(result)
    assert document["outcome"] == "denied" and document["exit_code"] == 3
    assert "Request denied" not in result.read_text(encoding="utf-8")
    assert capsys.readouterr().err == "privacyfence-sync: Request denied: no rule\n"


def test_result_file_for_a_bad_recipe(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    recipe = write_recipe([{"id": "one", "tool": "drive_list_files", "bogus": 1}])
    result = tmp_path / "result.json"
    assert cli.main(["run", str(recipe), "--result-file", str(result)]) == 1
    document = read_result(result)
    assert document["outcome"] == "recipe_error" and document["recipe"] is None


def test_unwritable_result_file_keeps_the_exit_code(
    write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "missing-folder" / "result.json"
    assert cli.main(["run", simple(write_recipe), "--result-file", str(result)]) == 0
    assert capsys.readouterr().err.startswith("privacyfence-sync: could not write the result file: ")


def test_unexpected_error_propagates_and_writes_no_result_file(
    write_recipe: RecipeWriter, daemon: Daemon, tmp_path: Path
) -> None:
    daemon.session = FakeSession({"drive_list_files": RuntimeError("bug")})
    result = tmp_path / "result.json"
    with pytest.raises(RuntimeError):
        cli.main(["run", simple(write_recipe), "--result-file", str(result)])
    assert not result.exists()


def test_permissions_exits_0_when_rules_are_needed(
    write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.session.tool_names = frozenset(_META | {"drive_list_files"})
    daemon.session.results["privacyfence_check_policy"] = {"verdict": "requires_review", "suggested_rules": []}
    assert cli.main(["permissions", simple(write_recipe), "--json"]) == 0
    assert '"status": "needs_rule"' in capsys.readouterr().out
    assert "privacyfence_begin_unattended_session" not in daemon.session.calls


def test_permissions_denied_status_exits_4(
    write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.session.results["privacyfence_status"] = ToolDenied("no")
    assert cli.main(["permissions", simple(write_recipe)]) == 4
    assert "revoked or has expired" in capsys.readouterr().err


def test_permissions_unwraps_task_group_errors(
    write_recipe: RecipeWriter, daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    group = ExceptionGroup("unhandled errors in a TaskGroup", [SignInError("expired")])
    daemon.session.results["privacyfence_status"] = group
    assert cli.main(["permissions", simple(write_recipe)]) == 4


def test_permissions_bad_recipe_exits_1(write_recipe: RecipeWriter, capsys: pytest.CaptureFixture[str]) -> None:
    recipe = write_recipe([{"id": "one", "tool": "drive_list_files", "bogus": 1}])
    assert cli.main(["permissions", str(recipe)]) == 1
    assert "unknown key bogus" in capsys.readouterr().err
