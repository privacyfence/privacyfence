"""Wiring checks for the organization Docker image jobs in the CI workflows.

build.yml's ``build-org-image`` matrix and ``finalize-release`` gating, and tests.yml's
``org-docker-image`` job. These are the properties a workflow edit can break without any other
test noticing: the runners, the permissions, the artifact name, the strictness switch and the rule
that nothing in build.yml touches a registry (ADR 0209).

It also covers publish-org-image.yml, the only workflow that pushes the image: its job chain, its
permissions, that it pushes the images build.yml tested instead of building, and that no step
is skipped for a pre-release (ADR 0267).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
IMAGE_TEST = "test_org_docker_image.py"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def build() -> dict[str, Any]:
    return _load("build.yml")


@pytest.fixture(scope="module")
def tests_yml() -> dict[str, Any]:
    return _load("tests.yml")


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


def _image_test_step(job: dict[str, Any]) -> dict[str, Any]:
    matches = [s for s in _steps(job) if IMAGE_TEST in s.get("run", "")]
    assert len(matches) == 1
    return matches[0]


def test_matrix_is_native_runners_per_architecture(build: dict[str, Any]) -> None:
    include = build["jobs"]["build-org-image"]["strategy"]["matrix"]["include"]
    assert {entry["arch"]: entry["runner"] for entry in include} == {
        "amd64": "ubuntu-24.04",
        "arm64": "ubuntu-24.04-arm",
    }
    assert build["jobs"]["build-org-image"]["runs-on"] == "${{ matrix.runner }}"


def test_permissions_are_read_only_and_nothing_can_write_packages(build: dict[str, Any]) -> None:
    assert build["jobs"]["build-org-image"]["permissions"] == {"contents": "read"}
    for name, job in build["jobs"].items():
        assert "packages" not in (job.get("permissions") or {}), name
    assert "packages" not in (build.get("permissions") or {})


def test_uploaded_artifact_is_per_arch_and_outside_the_release_pattern(build: dict[str, Any]) -> None:
    uploads = [
        s["with"]
        for s in _steps(build["jobs"]["build-org-image"])
        if str(s.get("uses", "")).startswith("actions/upload-artifact@") and s.get("if") is None
    ]
    assert [u["name"] for u in uploads] == ["org-image-${{ matrix.arch }}"]
    for arch in ("amd64", "arm64"):
        assert not fnmatch.fnmatch(uploads[0]["name"].replace("${{ matrix.arch }}", arch), "PrivacyFence-*")


def test_image_test_step_requires_the_packaged_test_to_run(build: dict[str, Any]) -> None:
    step = _image_test_step(build["jobs"]["build-org-image"])
    assert step["env"]["PRIVACYFENCE_REQUIRE_PACKAGED"] == "1"
    assert step["env"]["PRIVACYFENCE_ORG_IMAGE"] == "privacyfence-org:ci-${{ matrix.arch }}"


def test_finalize_release_waits_for_the_org_image(build: dict[str, Any]) -> None:
    assert "build-org-image" in build["jobs"]["finalize-release"]["needs"]


def test_build_yml_never_pushes_to_a_registry(build: dict[str, Any]) -> None:
    for job_name, job in build["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run", "")
            assert not re.search(r"\bdocker\s+(push|login)\b", run), (job_name, step.get("name"))


def test_tests_yml_has_the_per_pr_image_job(tests_yml: dict[str, Any]) -> None:
    job = tests_yml["jobs"]["org-docker-image"]
    step = _image_test_step(job)
    assert step["env"]["PRIVACYFENCE_REQUIRE_PACKAGED"] == "1"


@pytest.mark.parametrize("workflow", ["build.yml", "tests.yml"])
def test_every_action_is_pinned_to_a_commit_sha(workflow: str) -> None:
    for job_name, job in _load(workflow)["jobs"].items():
        for step in job.get("steps", []):
            if "uses" in step:
                assert re.search(r"@[0-9a-f]{40}$", step["uses"]), (job_name, step["uses"])


@pytest.fixture(scope="module")
def publish() -> dict[str, Any]:
    return _load("publish-org-image.yml")


def _push_step(publish: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in _steps(publish["jobs"]["publish"]) if s.get("id") == "push")


def test_publish_triggers_are_the_tag_push_and_a_manual_rerun(publish: dict[str, Any]) -> None:
    triggers = publish.get("on", publish.get(True))
    assert triggers == {"push": {"tags": ["v*"]}, "workflow_dispatch": None}


def test_publish_jobs_chain_dedupe_wait_for_build_publish(publish: dict[str, Any]) -> None:
    jobs = publish["jobs"]
    assert set(jobs) == {"dedupe", "wait_for_build", "publish"}
    assert jobs["wait_for_build"]["needs"] == "dedupe"
    assert jobs["publish"]["needs"] == "wait_for_build"
    assert jobs["wait_for_build"]["outputs"] == {"run_id": "${{ steps.wait.outputs.run_id }}"}


def test_only_the_publish_job_can_write_packages_and_attest(publish: dict[str, Any]) -> None:
    assert publish["permissions"] == {"contents": "read"}
    powerful = {"packages", "id-token", "attestations"}
    for name, job in publish["jobs"].items():
        granted = {k for k, v in (job.get("permissions") or {}).items() if v == "write"}
        assert (granted & powerful) == (powerful if name == "publish" else set()), name


def test_publish_downloads_the_images_of_the_run_it_waited_for(publish: dict[str, Any]) -> None:
    downloads = [
        s["with"]
        for s in _steps(publish["jobs"]["publish"])
        if str(s.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert len(downloads) == 1
    assert downloads[0]["run-id"] == "${{ needs.wait_for_build.outputs.run_id }}"
    assert downloads[0]["pattern"] == "org-image-*"


def test_no_publish_step_is_gated_on_the_stable_channel(publish: dict[str, Any]) -> None:
    for job_name, job in publish["jobs"].items():
        for step in job.get("steps", []):
            assert "channel" not in str(step.get("if", "")), (job_name, step.get("name"))
    names = [s.get("name") for s in _steps(publish["jobs"]["publish"])]
    assert "Note that a pre-release publishes no image" not in names


def test_publish_summary_says_no_floating_tag_moved_for_a_pre_release(publish: dict[str, Any]) -> None:
    step = _push_step(publish)
    assert step["env"]["CHANNEL"] == "${{ steps.release.outputs.channel }}"
    assert "no floating tag" in step["run"]


def test_builtin_check_requires_the_apps_on_every_release_tag() -> None:
    steps = _steps(_load("build.yml")["jobs"]["build-org-image"])
    check = [s for s in steps if "builtin_apps.py check-dist" in s.get("run", "")]
    assert len(check) == 1
    run = check[0]["run"]
    assert "REQUIRE=(--require)" in run
    assert "refs/tags/*" in run
    assert "channel" not in run and "stable" not in run


def test_publish_pushes_the_tested_images_and_never_builds(publish: dict[str, Any]) -> None:
    run = _push_step(publish)["run"]
    assert "scripts/org_image_tags.py" in run
    assert "docker load" in run
    assert not re.search(r"\bdocker\s+(buildx\s+)?build\b", run)
    assert "buildx build" not in run


def test_publish_attests_the_pushed_digest(publish: dict[str, Any]) -> None:
    step = next(
        s for s in _steps(publish["jobs"]["publish"]) if str(s.get("uses", "")).startswith("actions/attest-build-provenance@")
    )
    assert step["with"]["subject-digest"] == "${{ steps.push.outputs.digest }}"
    assert step["with"]["push-to-registry"] is True


def test_publish_actions_are_pinned_to_a_commit_sha(publish: dict[str, Any]) -> None:
    for job_name, job in publish["jobs"].items():
        for step in job.get("steps", []):
            if "uses" in step:
                assert re.search(r"@[0-9a-f]{40}$", step["uses"]), (job_name, step["uses"])
