"""build.yml ships the connector config signing tools: to the R2 archive on every tag, and to the
GitHub Release on a stable tag, next to the sync tools. A release builds the organization image,
privacyfence-sync and the SBOM only (ADR 0301), so no installer is among the assets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TOOL_SCRIPT = "scripts/sign_connector_config.py"
PAGE_SOURCE = "website/tools/connector-config/index.html"
PAGE_ASSET = "privacyfence-connector-config-tool.html"


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"))["jobs"]


def _step(job: dict[str, Any], name_part: str) -> dict[str, Any]:
    matches = [s for s in job["steps"] if name_part in s.get("name", "")]
    assert len(matches) == 1, name_part
    return matches[0]


def test_the_sources_exist():
    assert (ROOT / TOOL_SCRIPT).is_file()
    assert (ROOT / PAGE_SOURCE).is_file()


def test_the_r2_upload_carries_the_script_and_the_page_under_its_release_name(jobs):
    run = _step(jobs["finalize-release"], "Publish org admin tools to Cloudflare R2")["run"]
    upload = run[run.index("r2_release.py upload"):]
    assert "scripts/sync_room_directory.py" in upload
    assert "build_org_bundle" not in upload
    assert TOOL_SCRIPT in upload
    assert f"dist/{PAGE_ASSET}" in upload
    assert f"cp {PAGE_SOURCE} dist/{PAGE_ASSET}" in run
    # The copy is made before the upload reads it.
    assert run.index("cp ") < run.index("r2_release.py upload")


def test_the_stable_release_attaches_the_script_and_the_page(jobs):
    job = jobs["finalize-release"]
    stage = _step(job, "Stage the connector config signing page")
    assert stage["if"] == "steps.release.outputs.channel == 'stable'"
    assert f"cp {PAGE_SOURCE} release-assets/{PAGE_ASSET}" in stage["run"]

    attach = _step(job, "Attach release assets")
    assert attach["if"] == "steps.release.outputs.channel == 'stable'"
    files = attach["with"]["files"].split()
    assert "scripts/sync_room_directory.py" in files
    assert TOOL_SCRIPT in files
    assert f"release-assets/{PAGE_ASSET}" in files
    steps = [s.get("name", "") for s in job["steps"]]
    assert steps.index(stage["name"]) < steps.index(attach["name"])


def test_the_release_builds_only_what_remains(jobs):
    assert set(jobs) == {"dedupe", "build-org-image", "sbom", "finalize-release"}
    assert jobs["finalize-release"]["needs"] == ["sbom", "build-org-image"]
    assert "actions" not in jobs["finalize-release"]["permissions"]

    attach = _step(jobs["finalize-release"], "Attach release assets")
    files = attach["with"]["files"].split()
    assert "release-assets/sbom-python.cdx.json" in files
    assert "release-assets/privacyfence-sync-plugin.zip" in files
    for removed in ("dmg", "mcpb", "setup.exe", ".deb", "sbom-shim-npm"):
        assert not any(removed in f for f in files), removed
