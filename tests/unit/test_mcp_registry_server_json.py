"""Tests for scripts/mcp_registry_server_json.py and the real mcpb/server.json.tmpl it fills.

Imported by file path (importlib) rather than as a package, since scripts/ isn't part of the
installed ``privacyfence`` distribution -- same pattern as tests/unit/test_changelog_section.py.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = REPO_ROOT / "scripts" / "mcp_registry_server_json.py"
_spec = importlib.util.spec_from_file_location("mcp_registry_server_json", _SCRIPT_PATH)
mcp_registry_server_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_registry_server_json)

REAL_TEMPLATE = (REPO_ROOT / "mcpb" / "server.json.tmpl").read_text(encoding="utf-8")
SHA = "a" * 64


def test_renders_the_real_template_for_a_stable_version() -> None:
    server = json.loads(mcp_registry_server_json.render(REAL_TEMPLATE, "5.2.1", SHA))

    assert server["name"] == "io.github.privacyfence/privacyfence"
    assert server["version"] == "5.2.1"
    (package,) = server["packages"]
    assert package["registryType"] == "mcpb"
    assert package["identifier"] == (
        "https://github.com/privacyfence/privacyfence/releases/download/v5.2.1/PrivacyFence.mcpb"
    )
    assert package["fileSha256"] == SHA
    assert package["transport"] == {"type": "stdio"}


def test_real_template_description_fits_the_registry_limit() -> None:
    # server.schema.json caps `description` at 100 characters; the registry rejects longer.
    server = json.loads(mcp_registry_server_json.render(REAL_TEMPLATE, "1.0.0", SHA))
    assert len(server["description"]) <= 100


@pytest.mark.parametrize("version", ["5.2.1a1", "5.2.1rc2", "5.2.1.dev3+gabc1234", "v5.2.1", "5.2"])
def test_refuses_anything_but_a_stable_version(version: str) -> None:
    with pytest.raises(ValueError, match="not a stable"):
        mcp_registry_server_json.render(REAL_TEMPLATE, version, SHA)


def test_refuses_a_malformed_hash() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        mcp_registry_server_json.render(REAL_TEMPLATE, "5.2.1", "ABC")


def test_refuses_an_unfilled_placeholder() -> None:
    template = '{"version": "__VERSION__", "other": "__NEW_FIELD__"}'
    with pytest.raises(ValueError, match="__NEW_FIELD__"):
        mcp_registry_server_json.render(template, "5.2.1", SHA)


def test_main_hashes_the_given_file(tmp_path: Path) -> None:
    mcpb = tmp_path / "PrivacyFence.mcpb"
    mcpb.write_bytes(b"not really a zip")
    out = tmp_path / "server.json"

    assert mcp_registry_server_json.main(["--version", "5.2.1", "--mcpb", str(mcpb), "--out", str(out)]) == 0

    server = json.loads(out.read_text(encoding="utf-8"))
    assert server["packages"][0]["fileSha256"] == hashlib.sha256(b"not really a zip").hexdigest()


def test_main_fails_on_a_missing_file(tmp_path: Path) -> None:
    assert (
        mcp_registry_server_json.main(
            ["--version", "5.2.1", "--mcpb", str(tmp_path / "missing.mcpb"), "--out", str(tmp_path / "s.json")]
        )
        == 1
    )
