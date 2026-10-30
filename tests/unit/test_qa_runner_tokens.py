"""scripts/qa_runner_tokens.py: copy the QA principal's tokens from the QA server and back."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "qa_runner_tokens.py"
SECRET = "tok-secret-value-1234"


@pytest.fixture(scope="module")
def tokens():
    spec = importlib.util.spec_from_file_location("qa_runner_tokens", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_principal_variable(monkeypatch):
    monkeypatch.delenv("PF_QA_PRINCIPAL_ID", raising=False)


@pytest.fixture
def dirs(tmp_path):
    data, checkout = tmp_path / "data", tmp_path / "checkout"
    (data / "users").mkdir(parents=True)
    checkout.mkdir()
    return data, checkout


def put(data: Path, principal: str, name: str = "slack_token.json", content: str = SECRET) -> Path:
    path = data / "users" / principal / "credentials" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def run(tokens, command, data, checkout, *extra):
    return tokens.main([command, "--data-dir", str(data), "--checkout", str(checkout), *extra])


def pulled(checkout: Path, name: str = "slack_token.json") -> Path:
    return checkout / "users" / "qa-live" / "credentials" / name


def test_the_single_folder_with_a_token_file_is_used(tokens, dirs, capsys):
    data, checkout = dirs
    put(data, "alice")
    (data / "users" / "bob" / "credentials").mkdir(parents=True)
    (data / "users" / "bob" / "credentials" / "unrelated.txt").write_text("x")
    assert run(tokens, "pull", data, checkout) == 0
    assert pulled(checkout).read_text() == SECRET
    assert json.loads((checkout / ".qa-pull.json").read_text())["principal"] == "alice"
    assert SECRET not in capsys.readouterr().out


def test_the_variable_picks_the_folder(tokens, dirs, monkeypatch):
    data, checkout = dirs
    put(data, "alice", content="a")
    put(data, "bob", content="b")
    monkeypatch.setenv("PF_QA_PRINCIPAL_ID", "bob")
    assert run(tokens, "pull", data, checkout) == 0
    assert pulled(checkout).read_text() == "b"


def test_the_option_beats_the_variable(tokens, dirs, monkeypatch):
    data, checkout = dirs
    put(data, "alice", content="a")
    put(data, "bob", content="b")
    monkeypatch.setenv("PF_QA_PRINCIPAL_ID", "bob")
    assert run(tokens, "pull", data, checkout, "--principal-id", "alice") == 0
    assert pulled(checkout).read_text() == "a"


def test_no_folder_fails(tokens, dirs, capsys):
    data, checkout = dirs
    assert run(tokens, "pull", data, checkout) == 1
    assert "no principal" in capsys.readouterr().err


def test_several_folders_fail_and_name_the_ids(tokens, dirs, capsys):
    data, checkout = dirs
    put(data, "alice", content="a")
    put(data, "bob", content="b")
    assert run(tokens, "pull", data, checkout) == 1
    err = capsys.readouterr().err
    assert "alice" in err and "bob" in err and "PF_QA_PRINCIPAL_ID" in err
    assert not (checkout / "users").exists()


def test_a_named_folder_that_is_missing_fails(tokens, dirs, monkeypatch):
    data, checkout = dirs
    put(data, "alice")
    monkeypatch.setenv("PF_QA_PRINCIPAL_ID", "../etc")
    assert run(tokens, "pull", data, checkout) == 1
    monkeypatch.setenv("PF_QA_PRINCIPAL_ID", "ghost")
    assert run(tokens, "pull", data, checkout) == 1


def test_pulled_files_are_owner_only(tokens, dirs):
    data, checkout = dirs
    put(data, "alice")
    os.chmod(data / "users" / "alice" / "credentials" / "slack_token.json", 0o644)
    run(tokens, "pull", data, checkout)
    assert stat.S_IMODE(pulled(checkout).stat().st_mode) == 0o600


def test_pull_records_each_files_digest(tokens, dirs):
    import hashlib

    data, checkout = dirs
    put(data, "alice")
    run(tokens, "pull", data, checkout)
    record = json.loads((checkout / ".qa-pull.json").read_text())
    assert record["files"] == {"slack_token.json": hashlib.sha256(SECRET.encode()).hexdigest()}


def test_push_writes_only_changed_files(tokens, dirs, capsys):
    data, checkout = dirs
    changed, same = put(data, "alice"), put(data, "alice", "miro_token.json", "m")
    run(tokens, "pull", data, checkout)
    pulled(checkout).write_text("refreshed")
    before = same.stat().st_mtime_ns
    assert run(tokens, "push", data, checkout) == 0
    assert changed.read_text() == "refreshed"
    assert stat.S_IMODE(changed.stat().st_mode) == 0o600
    assert same.stat().st_mtime_ns == before
    assert "refreshed" not in capsys.readouterr().out
    assert [p.name for p in changed.parent.iterdir() if p.name.startswith(".")] == []


def test_push_keeps_a_file_changed_on_the_server(tokens, dirs, capsys):
    data, checkout = dirs
    server = put(data, "alice")
    run(tokens, "pull", data, checkout)
    pulled(checkout).write_text("from the run")
    server.write_text("reconnected")
    assert run(tokens, "push", data, checkout) == 0
    assert server.read_text() == "reconnected"
    assert "changed on the server" in capsys.readouterr().err


def test_push_adds_a_new_file(tokens, dirs):
    data, checkout = dirs
    put(data, "alice")
    run(tokens, "pull", data, checkout)
    pulled(checkout, "miro_token.json").write_text("new")
    run(tokens, "push", data, checkout)
    assert (data / "users" / "alice" / "credentials" / "miro_token.json").read_text() == "new"


def test_push_without_pull_fails(tokens, dirs):
    data, checkout = dirs
    assert run(tokens, "push", data, checkout) == 1


def test_a_symlinked_file_is_refused(tokens, dirs, tmp_path, capsys):
    data, checkout = dirs
    put(data, "alice")
    outside = tmp_path / "outside"
    outside.write_text("x")
    (data / "users" / "alice" / "credentials" / "miro_token.json").symlink_to(outside)
    assert run(tokens, "pull", data, checkout) == 1
    assert "symlink" in capsys.readouterr().err


def test_a_symlinked_checkout_file_is_refused_on_push(tokens, dirs, tmp_path):
    data, checkout = dirs
    server = put(data, "alice")
    run(tokens, "pull", data, checkout)
    outside = tmp_path / "outside"
    outside.write_text("x")
    pulled(checkout, "miro_token.json").symlink_to(outside)
    assert run(tokens, "push", data, checkout) == 1
    assert server.read_text() == SECRET


def test_the_token_names_come_from_the_daemon(tokens):
    from privacyfence import daemon_main

    assert tokens._token_files() == {v.split("/", 1)[1] for v in daemon_main.TOKEN_FILES.values()}
