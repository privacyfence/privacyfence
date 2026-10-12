"""The package must not import anything from the privacyfence daemon package."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def daemon_imports(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        found += [n for n in names if n == "privacyfence" or n.startswith("privacyfence.")]
    return found


def test_no_module_imports_privacyfence() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert files
    offenders = {str(f): daemon_imports(f.read_text(encoding="utf-8")) for f in files}
    assert {f: n for f, n in offenders.items() if n} == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import privacyfence", 1),
        ("import privacyfence.config", 1),
        ("from privacyfence import x", 1),
        ("from privacyfence.config import x", 1),
        ("import privacyfence_sync", 0),
        ("from privacyfence_sync.refs import x", 0),
        ("from . import x", 0),
    ],
)
def test_detector_catches_each_import_form(source: str, expected: int) -> None:
    assert len(daemon_imports(source)) == expected
