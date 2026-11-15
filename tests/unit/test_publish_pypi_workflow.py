"""publish-pypi.yml publishes only privacyfence-sync (ADR 0309)."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/publish-pypi.yml"


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_jobs_are_exactly_the_sync_publish_chain() -> None:
    assert set(_load()["jobs"]) == {
        "dedupe",
        "wait_for_build",
        "build",
        "publish-testpypi-sync",
        "publish-pypi-sync",
    }


def test_publish_jobs_are_stable_only_with_their_environments() -> None:
    jobs = _load()["jobs"]
    for job, environment in (("publish-testpypi-sync", "testpypi"), ("publish-pypi-sync", "pypi")):
        assert jobs[job]["if"] == "needs.build.outputs.channel == 'stable'"
        assert jobs[job]["environment"] == environment


def test_build_writes_no_credentials_and_uploads_only_the_sync_artifact() -> None:
    steps = _load()["jobs"]["build"]["steps"]
    for step in steps:
        assert "credentials" not in step.get("name", "").lower()
        if "upload-artifact" in step.get("uses", ""):
            assert step["with"]["name"] == "pypi-dist-sync"
        assert "secrets." not in yaml.safe_dump(step.get("env", {}))


def test_no_r2_publish_job() -> None:
    assert "publish-r2" not in WORKFLOW.read_text(encoding="utf-8")
