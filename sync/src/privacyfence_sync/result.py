"""The result file ``privacyfence-sync run --result-file`` writes for a scheduler."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from . import __version__

OUTCOMES = {1: "recipe_error", 2: "failed", 3: "denied", 4: "not_signed_in"}
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def outcome_for(exit_code: int, stopped_at: str | None) -> str:
    if exit_code == 0:
        return "ok" if stopped_at is None else "no_change"
    return OUTCOMES[exit_code]


def write_result(
    path: Path,
    *,
    recipe: str | None,
    exit_code: int,
    stopped_at: str | None,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    outcome = outcome_for(exit_code, stopped_at)
    document = {
        "version": 1,
        "recipe": recipe,
        "outcome": outcome,
        "exit_code": exit_code,
        "stopped_at_step": stopped_at if outcome == "no_change" else None,
        "started_at": started_at.strftime(_TIME_FORMAT),
        "finished_at": finished_at.strftime(_TIME_FORMAT),
        "privacyfence_sync_version": __version__,
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)
