"""``scripts/dev_start.sh`` tells the shim it registers where a source daemon's data lives.

A source checkout's ``paths.data_dir()`` is the checkout root, while the shim on its own looks in
the per-user directory, so without the override the shim never finds ``mcp_url`` or the control
socket. The variable name is a contract between this script and ``mcpb/shim/src/protocol.ts``
(``DEV_DATA_DIR_ENV``), and both registration paths have to pass it.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_START = (REPO_ROOT / "scripts" / "dev_start.sh").read_text(encoding="utf-8")


def _shim_env_name() -> str:
    protocol = (REPO_ROOT / "mcpb" / "shim" / "src" / "protocol.ts").read_text(encoding="utf-8")
    match = re.search(r'export const DEV_DATA_DIR_ENV = "([A-Z_]+)";', protocol)
    assert match, "DEV_DATA_DIR_ENV not found in protocol.ts"
    return match.group(1)


def test_claude_code_registration_passes_the_data_dir():
    name = _shim_env_name()
    assert f'claude mcp add privacyfence -e "{name}=$DEV_DATA_DIR" -- node "$SHIM_ENTRY"' in DEV_START


def test_claude_desktop_registration_passes_the_data_dir():
    name = _shim_env_name()
    assert f'"env": {{"{name}": dev_data_dir}}' in DEV_START


def test_the_data_dir_comes_from_paths_py():
    assert "DEV_DATA_DIR=\"$(.venv/bin/python -c 'from privacyfence import paths; print(paths.data_dir())')\"" in DEV_START
