from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

RecipeWriter = Callable[..., Path]


@pytest.fixture
def write_recipe(tmp_path: Path) -> RecipeWriter:
    """Write a recipe file (and stub scripts and templates it names) into tmp_path."""

    def write(steps: list[dict[str, Any]], **top: Any) -> Path:
        doc: dict[str, Any] = {"version": 1, "name": "demo", "steps": steps}
        doc.update(top)
        doc = {k: v for k, v in doc.items() if v is not None}
        for step in (s for s in steps if isinstance(s, dict)):
            for key in ("run", "template"):
                if isinstance(step.get(key), str):
                    stub = tmp_path / step[key]
                    stub.parent.mkdir(parents=True, exist_ok=True)
                    stub.touch()
        path = tmp_path / "recipe.yaml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        return path

    return write
