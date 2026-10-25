from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from privacyfence_sync import __version__
from privacyfence_sync.result import outcome_for, write_result


@pytest.mark.parametrize(
    ("exit_code", "stopped_at", "outcome"),
    [
        (0, None, "ok"),
        (0, "build", "no_change"),
        (1, None, "recipe_error"),
        (2, None, "failed"),
        (3, None, "denied"),
        (4, None, "not_signed_in"),
    ],
)
def test_outcome_for(exit_code: int, stopped_at: str | None, outcome: str) -> None:
    assert outcome_for(exit_code, stopped_at) == outcome


def test_write_result_document_and_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    write_result(
        path,
        recipe="pipeline-overview",
        exit_code=0,
        stopped_at="build",
        started_at=datetime(2026, 10, 20, 6, 15, 0, 500, tzinfo=timezone.utc),
        finished_at=datetime(2026, 10, 20, 6, 15, 42, tzinfo=timezone.utc),
    )
    text = path.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert json.loads(text) == {
        "version": 1,
        "recipe": "pipeline-overview",
        "outcome": "no_change",
        "exit_code": 0,
        "stopped_at_step": "build",
        "started_at": "2026-10-20T06:15:00Z",
        "finished_at": "2026-10-20T06:15:42Z",
        "privacyfence_sync_version": __version__,
    }
    assert [p.name for p in tmp_path.iterdir()] == ["result.json"]


def test_write_result_leaves_the_step_null_unless_no_change(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    now = datetime.now(timezone.utc)
    write_result(path, recipe=None, exit_code=1, stopped_at="x", started_at=now, finished_at=now)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["recipe"] is None and document["stopped_at_step"] is None
    assert document["outcome"] == "recipe_error"
