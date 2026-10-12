"""Script steps: run the user's own Python script with the work folder as its cwd."""

from __future__ import annotations

import os
import subprocess  # nosec B404  # runs the user's own reviewed script, fixed argv, no shell
import sys
from pathlib import Path

from .recipe import STOP_EXIT_CODE, RunStep

_STDERR_TAIL = 2000


class StopRun(Exception):
    """The script asked to stop the run successfully (exit 10)."""


class ScriptError(RuntimeError):
    """A script step failed."""


def script_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("PRIVACYFENCE_")}


def run_script(step: RunStep, argv: list[str], work_dir: Path) -> str:
    """Run the step's script and return its captured stdout."""
    for relative in step.outputs.values():
        (work_dir / relative).parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(  # nosec B603  # fixed argv, no shell; the script is the user's own reviewed file
            [sys.executable, str(step.script), *argv],
            cwd=work_dir,
            env=script_env(),
            capture_output=True,
            text=True,
            timeout=step.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ScriptError(f"step {step.id}: {step.script} ran longer than {step.timeout_seconds} seconds") from None
    sys.stderr.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == STOP_EXIT_CODE:
        raise StopRun(step.id)
    if proc.returncode != 0:
        raise ScriptError(f"step {step.id}: {step.script} exited with {proc.returncode}: {proc.stderr[-_STDERR_TAIL:]}")
    for relative in step.outputs.values():
        if not (work_dir / relative).exists():
            raise ScriptError(f"step {step.id}: {step.script} did not write {work_dir / relative}")
    if step.stdout is not None:
        (work_dir / step.stdout).write_text(proc.stdout, encoding="utf-8")
    return proc.stdout
