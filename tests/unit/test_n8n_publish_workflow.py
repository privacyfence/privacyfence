"""The n8n node's npm publish workflow: trusted publishing and provenance, SHA-pinned actions, a
rerun that skips a published version, and no use of the node CLI's release command
(docs/releasing.md, "Publishing the n8n node to npm")."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-n8n-node.yml"


def _load() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


def _run_text(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in _steps(job))


def test_the_workflow_parses_and_has_the_three_jobs() -> None:
    jobs = _load()["jobs"]
    assert set(jobs) == {"dedupe", "wait_for_build", "publish"}
    assert set(jobs["publish"]["needs"]) == {"dedupe", "wait_for_build"}
    assert jobs["publish"]["environment"] == "npm"


def test_only_the_publish_job_has_id_token_write() -> None:
    jobs = _load()["jobs"]
    assert jobs["publish"]["permissions"] == {"id-token": "write", "contents": "read"}
    for name, job in jobs.items():
        if name != "publish":
            assert "id-token" not in job.get("permissions", {}), name
    assert "id-token" not in _load().get("permissions", {})


def test_every_action_is_pinned_to_a_sha() -> None:
    uses = [
        step["uses"]
        for job in _load()["jobs"].values()
        for step in _steps(job)
        if "uses" in step
    ]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref


def test_the_publish_command_uses_provenance_and_no_release_command() -> None:
    publish = _load()["jobs"]["publish"]
    publish_steps = [s for s in _steps(publish) if "npm publish" in s.get("run", "")]
    assert len(publish_steps) == 1
    assert "--provenance" in publish_steps[0]["run"]
    assert "--dry-run" in publish_steps[0]["run"]
    for job in _load()["jobs"].values():
        for step in _steps(job):
            assert "n8n-node release" not in step.get("run", "")
            assert "npm run release" not in step.get("run", "")


def test_the_publish_job_runs_for_stable_tags_only() -> None:
    run = _run_text(_load()["jobs"]["publish"])
    assert 'scripts/r2_release.py channel "$VERSION"' in run
    assert '!= "stable"' in run


def test_a_rerun_skips_an_already_published_version() -> None:
    publish = _load()["jobs"]["publish"]
    check = [s for s in _steps(publish) if "npm view" in s.get("run", "")]
    assert len(check) == 1
    assert '= "$VERSION"' in check[0]["run"]
    published = [s for s in _steps(publish) if s.get("name") == "Publish to npm"]
    assert published[0]["if"] == "steps.published.outputs.published != 'true'"


def test_workflow_dispatch_has_only_dry_run() -> None:
    dispatch = _load()[True]["workflow_dispatch"]  # PyYAML reads the key `on` as True
    assert set(dispatch["inputs"]) == {"dry_run"}
    assert dispatch["inputs"]["dry_run"]["default"] is True


def test_the_dedupe_job_names_its_own_workflow() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "publish-pypi.yml" not in text
    assert "workflows/publish-n8n-node.yml/runs" in text
