from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from privacyfence_sync.recipe import RunStep
from privacyfence_sync.transform import ScriptError, StopRun, run_script


def make_step(
    tmp_path: Path,
    body: str,
    *,
    outputs: dict[str, str] | None = None,
    stdout: str | None = None,
    timeout_seconds: int = 30,
) -> tuple[RunStep, Path]:
    script = tmp_path / "script.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    step = RunStep("s", script, (), outputs or {}, stdout, timeout_seconds)
    return step, work


def test_passes_argv_and_runs_in_work_dir(tmp_path: Path) -> None:
    step, work = make_step(
        tmp_path,
        """
        import json, os, sys
        open("out.json", "w").write(json.dumps([sys.argv[1:], os.getcwd()]))
        """,
        outputs={"o": "out.json"},
    )
    run_script(step, ["a b", "--c"], work)
    argv, cwd = json.loads((work / "out.json").read_text())
    assert argv == ["a b", "--c"]
    assert Path(cwd).resolve() == work.resolve()


def test_env_has_no_privacyfence_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVACYFENCE_TOKEN", "x")
    monkeypatch.setenv("KEEP_ME", "y")
    step, work = make_step(
        tmp_path,
        """
        import os
        print(sorted(k for k in os.environ if k.startswith("PRIVACYFENCE_")), os.environ["KEEP_ME"])
        """,
    )
    assert run_script(step, [], work).strip() == "[] y"


def test_creates_output_parent_folders(tmp_path: Path) -> None:
    step, work = make_step(
        tmp_path,
        'open("plan/deep/x.csv", "w").write("1")',
        outputs={"x": "plan/deep/x.csv"},
    )
    run_script(step, [], work)
    assert (work / "plan/deep/x.csv").read_text() == "1"


def test_missing_output_raises(tmp_path: Path) -> None:
    step, work = make_step(tmp_path, "pass", outputs={"x": "x.csv"})
    with pytest.raises(ScriptError, match="did not write"):
        run_script(step, [], work)


def test_stdout_is_saved_when_asked(tmp_path: Path) -> None:
    step, work = make_step(tmp_path, 'print("hello")', stdout="out.txt")
    assert run_script(step, [], work) == "hello\n"
    assert (work / "out.txt").read_text() == "hello\n"


def test_exit_10_raises_stop_run(tmp_path: Path) -> None:
    step, work = make_step(tmp_path, "raise SystemExit(10)")
    with pytest.raises(StopRun) as info:
        run_script(step, [], work)
    assert info.value.args == ("s",)


def test_nonzero_exit_raises_with_stderr_tail(tmp_path: Path) -> None:
    step, work = make_step(
        tmp_path,
        """
        import sys
        sys.stderr.write("A" * 3000 + "THE-END")
        raise SystemExit(3)
        """,
    )
    with pytest.raises(ScriptError, match="exited with 3") as info:
        run_script(step, [], work)
    message = str(info.value)
    assert message.endswith("A" * 1993 + "THE-END")
    assert "A" * 1994 not in message


def test_timeout_raises(tmp_path: Path) -> None:
    step, work = make_step(tmp_path, "import time; time.sleep(30)", timeout_seconds=1)
    with pytest.raises(ScriptError, match="ran longer than 1 seconds"):
        run_script(step, [], work)
