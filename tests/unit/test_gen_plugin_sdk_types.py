"""The plugin SDK's generated protocol types match the protocol schema they come from."""

from __future__ import annotations

import json
import subprocess  # nosec B404  # runs the repo's own generator script
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_plugin_sdk_types.py"
SCHEMA = REPO_ROOT / "docs" / "plugin-protocol" / "protocol.schema.json"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603  # fixed argv, no shell
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )


def test_checked_in_types_are_current() -> None:
    result = _run("--check")
    assert result.returncode == 0, result.stderr


def test_modified_schema_makes_the_check_fail(tmp_path: Path) -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    schema["$defs"]["ShutdownParams"]["properties"]["extra_field"] = {"type": "string"}
    modified = tmp_path / "protocol.schema.json"
    modified.write_text(json.dumps(schema), encoding="utf-8")

    result = _run("--check", "--schema", str(modified))

    assert result.returncode == 1
    assert "stale at line" in result.stderr
