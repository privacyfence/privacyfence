"""The admin policy's required-check list must equal what the workflows report.

A listed check that never reports blocks every merge, and a job that is renamed without the list
silently stops gating. `.tld/admin-policy.yaml` is the one list (ADR 0315).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _required() -> list[str]:
    policy = yaml.safe_load((REPO_ROOT / ".tld" / "admin-policy.yaml").read_text(encoding="utf-8"))
    return list(policy["rulesets"]["default_branch"]["required_status_checks"])


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_the_list_has_no_duplicates():
    required = _required()
    assert len(required) == len(set(required))


def test_plain_jobs_exist_in_tests_yml():
    jobs = _workflow("tests.yml")["jobs"]
    reported = set(jobs) | {job["name"] for job in jobs.values() if isinstance(job.get("name"), str)}
    for check in ("test", "static-analysis", "org-mode-smoke", "n8n-node"):
        assert check in _required(), f"{check} is no longer required"
        assert check in reported, f"{check} is required but no job in tests.yml reports it"


def test_python_matrix_legs_are_required():
    versions = _workflow("tests.yml")["jobs"]["test-python-compat"]["strategy"]["matrix"]["python-version"]
    assert versions, "test-python-compat has no python-version matrix any more"
    expected = {f"Test (Python {v}, core suite)" for v in versions}
    actual = {c for c in _required() if c.startswith("Test (Python ")}
    assert actual == expected, (
        "the test-python-compat matrix and the required checks disagree: every matrix leg reports "
        "as its own check and must be listed, and a listed check that no longer runs blocks every merge"
    )


def test_sync_client_matrix_legs_are_required():
    versions = _workflow("tests.yml")["jobs"]["sync"]["strategy"]["matrix"]["python-version"]
    expected = {f"Sync client (Python {v})" for v in versions}
    actual = {c for c in _required() if c.startswith("Sync client (")}
    assert actual == expected


def test_website_build_is_required_and_runs_on_every_pull_request():
    assert "website-build" in _required()
    workflow = _workflow("website-build.yml")
    names = [job.get("name") for job in workflow["jobs"].values()]
    assert names == ["website-build"], f"website-build.yml's job names changed: {names}"
    # PyYAML reads the bare `on:` key as boolean True.
    triggers = workflow[True]
    assert "pull_request" in triggers
    assert not (triggers["pull_request"] or {}).get("paths"), "a paths: filter would wedge PRs"
