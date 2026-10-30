"""The two live-check workflows: the runner they target, where the built-in app credentials
reach, how the QA tokens are copied, and what they refuse (docs/connector-qa.md, "Self-hosted
runner")."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
LIVE_CHECK = WORKFLOWS / "connector-live-check.yml"
RECORD = WORKFLOWS / "qa-record-fixture.yml"

RUNNER_JOBS = {LIVE_CHECK: "live-check", RECORD: "record"}
CONNECTOR_APPS = ("GOOGLE_WEB", "SLACK", "SALESFORCE", "ATLASSIAN_RELAY", "MIRO")
BUILTIN_SECRETS = {f"PF_APP_{app}_CLIENT_{part}" for app in CONNECTOR_APPS for part in ("ID", "SECRET")}
BUILTIN_STEP = "Write built-in app credentials"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _runner_job(path: Path) -> dict[str, Any]:
    return _load(path)["jobs"][RUNNER_JOBS[path]]


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def _run_steps_calling(job: dict[str, Any], text: str) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if text in step.get("run", "")]


@pytest.fixture(params=sorted(RUNNER_JOBS, key=str), ids=lambda path: path.name)
def workflow_path(request: pytest.FixtureRequest) -> Path:
    return request.param


def test_the_runner_job_targets_the_live_runner(workflow_path: Path) -> None:
    assert _runner_job(workflow_path)["runs-on"] == ["self-hosted", "privacyfence-live"]


def test_the_old_runner_label_is_gone(workflow_path: Path) -> None:
    assert "privacyfence-test" not in workflow_path.read_text(encoding="utf-8")


def test_the_environment_addresses_and_principal_variable_are_workflow_env(workflow_path: Path) -> None:
    env = _load(workflow_path)["env"]
    assert env["PRIVACYFENCE_PEEK_BASE_URL"] == "https://peek-qa.privacyfence.eu"
    assert env["PRIVACYFENCE_GRIST_SERVER_URL"] == "https://docs.getgrist.com"
    assert env["PF_QA_PRINCIPAL_ID"] == "${{ vars.PF_QA_PRINCIPAL_ID }}"


def test_the_builtin_step_carries_exactly_the_ten_connector_secrets(workflow_path: Path) -> None:
    step = _step(_runner_job(workflow_path), BUILTIN_STEP)
    assert step["run"].strip() == "python3 scripts/builtin_apps.py write"
    assert set(step["env"]) == BUILTIN_SECRETS
    for name, value in step["env"].items():
        assert value == "${{ secrets." + name + " }}"
    assert "PF_APP_GOOGLE_SIGNIN" not in workflow_path.read_text(encoding="utf-8")


def test_no_builtin_secret_is_in_workflow_or_job_env(workflow_path: Path) -> None:
    workflow = _load(workflow_path)
    for scope in (workflow.get("env", {}), *(job.get("env", {}) for job in workflow["jobs"].values())):
        assert not [name for name in scope if name.startswith("PF_APP_")]
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if step.get("name") != BUILTIN_STEP:
                assert not [name for name in step.get("env", {}) if name.startswith("PF_APP_")]


def test_the_cleanup_step_is_last_and_always_runs(workflow_path: Path) -> None:
    last = _runner_job(workflow_path)["steps"][-1]
    assert last["if"] == "always()"
    assert last["run"].strip() == "rm -f src/privacyfence/_builtin_apps.py"


def test_the_builtin_step_comes_before_the_recorder(workflow_path: Path) -> None:
    steps = _runner_job(workflow_path)["steps"]
    written = steps.index(_step(_runner_job(workflow_path), BUILTIN_STEP))
    first_recorder = min(i for i, step in enumerate(steps) if "qa_fixture_recorder.py" in step.get("run", ""))
    assert written < first_recorder


def test_tokens_are_pulled_before_the_recorder_and_pushed_back_always(workflow_path: Path) -> None:
    job = _runner_job(workflow_path)
    steps = job["steps"]
    (pull,) = _run_steps_calling(job, "qa_runner_tokens.py pull")
    (push,) = _run_steps_calling(job, "qa_runner_tokens.py push")
    assert '--data-dir "$HOME/qa-org-data"' in pull["run"]
    assert '--data-dir "$HOME/qa-org-data"' in push["run"]
    assert push["if"] == "always()"
    assert "if" not in pull
    first_recorder = min(i for i, step in enumerate(steps) if "qa_fixture_recorder.py" in step.get("run", ""))
    assert steps.index(pull) < first_recorder
    assert steps.index(push) > first_recorder


def test_no_step_names_the_old_bundle_or_a_root_level_credentials_folder(workflow_path: Path) -> None:
    text = workflow_path.read_text(encoding="utf-8")
    assert "org/org_config.json" not in text
    assert not re.search(r"(?<![\w./-])credentials/", text)


def test_no_step_writes_the_users_folder_by_hand(workflow_path: Path) -> None:
    for step in _runner_job(workflow_path)["steps"]:
        run = step.get("run", "")
        assert "cp " not in run or "users/" not in run
        assert "mkdir" not in run or "users" not in run


def test_the_optional_bundle_is_copied_only_when_present(workflow_path: Path) -> None:
    job = _runner_job(workflow_path)
    (pull,) = _run_steps_calling(job, "qa_runner_tokens.py pull")
    run = pull["run"]
    assert 'if [ -f "${secrets_dir}/org/org_bundle.json" ]' in run
    assert "tests/fixtures/qa_environment.yaml" in run


def test_the_live_check_reports_the_relay_without_failing_on_it() -> None:
    job = _runner_job(LIVE_CHECK)
    step = _step(job, "Check the relay's health")
    assert step["continue-on-error"] is True
    assert "curl -fsSI" in step["run"]
    assert "https://oauth.privacyfence.eu/health" in step["run"]
    assert "relay: up" in step["run"]
    assert "relay: down" in step["run"]
    assert "GITHUB_STEP_SUMMARY" in step["run"]


def test_the_record_workflow_still_refuses_anything_but_main() -> None:
    job = _runner_job(RECORD)
    refusal = job["steps"][0]
    assert 'refs/heads/main' in refusal["run"]
    assert 'if [ "${GITHUB_REF}" != "refs/heads/main" ]' in refusal["run"]


def test_the_commit_message_names_the_live_runner() -> None:
    text = RECORD.read_text(encoding="utf-8")
    assert "Recorded on the privacyfence-live runner" in text


def test_the_two_workflows_share_one_concurrency_group() -> None:
    assert _load(LIVE_CHECK)["concurrency"]["group"] == _load(RECORD)["concurrency"]["group"]
