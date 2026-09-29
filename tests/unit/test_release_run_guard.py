"""Tests for scripts/release_run_guard.py: which run of a doubled tag push cancels itself (ADR 0111).

Imported by file path, like tests/unit/test_r2_release.py: scripts/ isn't part of the installed
distribution.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release_run_guard.py"
_spec = importlib.util.spec_from_file_location("release_run_guard", _SCRIPT_PATH)
release_run_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_run_guard)

SHA = "8aaa1b5076536dab4987717b09b142bfc123409b"


def _run(run_id: int, *, event: str = "push", ref: str = "v5.2.0", sha: str = SHA, status: str = "in_progress") -> dict:
    return {"id": run_id, "event": event, "head_branch": ref, "head_sha": sha, "status": status}


def _dup(runs: list[dict], run_id: int, *, event: str = "push", ref: str = "v5.2.0") -> bool:
    return release_run_guard.is_duplicate(runs, run_id=run_id, event=event, ref_name=ref, head_sha=SHA)


class TestIsDuplicate:
    def test_of_two_runs_for_one_push_only_the_newer_is_the_duplicate(self):
        # The v5.2.0 pair: build.yml runs 36581010122 and 36581012661.
        runs = [_run(36581012661), _run(36581010122)]
        assert _dup(runs, 36581012661) is True
        assert _dup(runs, 36581010122) is False

    def test_a_run_alone_is_not_a_duplicate(self):
        assert _dup([_run(10)], 10) is False

    def test_a_finished_older_run_does_not_stop_a_deliberate_new_one(self):
        # Deleting and re-pushing a tag after its first run finished starts a run that must go ahead.
        assert _dup([_run(10, status="completed"), _run(20)], 20) is False

    @pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
    def test_any_unfinished_status_counts(self, status):
        assert _dup([_run(10, status=status), _run(20)], 20) is True

    def test_a_workflow_dispatch_is_never_a_duplicate(self):
        # The build.yml pre-flight is dispatched by hand against main's tip, the same commit as the tag.
        assert _dup([_run(10), _run(20, event="workflow_dispatch", ref="main")], 20, event="workflow_dispatch", ref="main") is False

    def test_an_older_run_from_another_event_or_ref_does_not_count(self):
        runs = [_run(10, event="workflow_dispatch", ref="main"), _run(11, ref="v5.2.1"), _run(20)]
        assert _dup(runs, 20) is False

    def test_an_older_run_for_another_commit_does_not_count(self):
        assert _dup([_run(10, sha="0" * 40), _run(20)], 20) is False


class TestMain:
    def test_prints_true_for_the_duplicate(self, monkeypatch, capsys):
        payload = {"workflow_runs": [_run(1), _run(2)]}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert release_run_guard.main(["--run-id", "2", "--event", "push", "--ref-name", "v5.2.0", "--head-sha", SHA]) == 0
        assert capsys.readouterr().out == "true\n"

    def test_prints_false_otherwise(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"workflow_runs": [_run(1)]})))
        assert release_run_guard.main(["--run-id", "1", "--event", "push", "--ref-name", "v5.2.0", "--head-sha", SHA]) == 0
        assert capsys.readouterr().out == "false\n"
