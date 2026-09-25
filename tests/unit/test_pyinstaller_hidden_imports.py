"""``scripts/pyinstaller_common.py``'s ``HIDDEN_IMPORTS`` names every connector module.

The list is a backstop for PyInstaller's static analysis missing a connector, so it is only
useful if it is complete. It is read with ``ast`` rather than imported, because importing the
module needs PyInstaller, which is only in the ``dev`` extra.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONNECTORS_PREFIX = "privacyfence.connectors."


def _hidden_imports() -> list[str]:
    tree = ast.parse((REPO_ROOT / "scripts" / "pyinstaller_common.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "HIDDEN_IMPORTS" for t in node.targets
        ):
            return [ast.literal_eval(elt) for elt in node.value.elts]
    raise AssertionError("HIDDEN_IMPORTS not found in scripts/pyinstaller_common.py")


def test_every_connector_module_is_a_hidden_import():
    modules = {
        CONNECTORS_PREFIX + path.stem
        for path in (REPO_ROOT / "src" / "privacyfence" / "connectors").glob("*.py")
        if path.stem != "__init__"
    }
    listed = [name for name in _hidden_imports() if name.startswith(CONNECTORS_PREFIX)]
    assert len(listed) == len(set(listed)), "duplicate connector entry"
    assert set(listed) == modules
