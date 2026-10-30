"""The QA server's deploy: the workflow, the host script, the sudoers rule, the compose override
and the proxy that keeps /mcp closed (docs/connector-qa.md, "The QA server")."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.server.auth.routes import AUTHORIZATION_PATH, REGISTRATION_PATH, REVOCATION_PATH, TOKEN_PATH

from privacyfence.web import oauth_provider, routes_mcp

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-qa-server.yml"
HOST = ROOT / "deploy" / "qa-runner"
RUN_STEP = "sudo -n /usr/local/sbin/pf-qa-deploy"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


class TestWorkflow:
    def test_triggers_and_concurrency(self, workflow):
        on = workflow[True]  # YAML reads the key `on` as a boolean
        assert on["push"]["branches"] == ["main"]
        assert {"src/**", "deploy/docker/**", "deploy/qa-runner/**", "pyproject.toml", "setup.py", "MANIFEST.in",
                "requirements/**", ".github/workflows/deploy-qa-server.yml"} == set(on["push"]["paths"])
        assert "workflow_dispatch" in on
        assert workflow["concurrency"] == {"group": "qa-server-deploy", "cancel-in-progress": True}
        assert workflow["permissions"] == {"contents": "read", "actions": "read"}

    def test_secrets_appear_only_in_step_env(self, workflow):
        text = WORKFLOW.read_text(encoding="utf-8")
        for job in workflow["jobs"].values():
            for step in _steps(job):
                outside_env = {k: v for k, v in step.items() if k != "env"}
                assert "secrets." not in str(outside_env)
        assert "secrets." not in str({k: v for k, v in workflow.items() if k != "jobs"})
        build_steps = _steps(workflow["jobs"]["build"])
        holders = [s for s in build_steps if "secrets." in str(s.get("env", {}))]
        assert len(holders) == 2
        for step in holders:
            names = [k for k in step["env"] if "secrets." in str(step["env"][k])]
            assert all(k.startswith(("PF_APP_", "TELEGRAM_")) for k in names)
        assert "secrets." not in str(workflow["jobs"]["deploy"]) + str(workflow["jobs"]["verify"])
        assert text.count("${{ secrets.") == text.count("secrets.")

    def test_the_build_job_tags_and_uploads_the_image(self, workflow):
        job = workflow["jobs"]["build"]
        assert job["runs-on"] == "ubuntu-latest"
        text = str(job)
        assert "privacyfence-org:qa-main" in text and "linux/amd64" in text and "gzip" in text
        upload = next(s for s in _steps(job) if "upload-artifact" in s.get("uses", ""))
        assert upload["with"]["name"] == "qa-server-image" and upload["with"]["retention-days"] == 3

    def test_the_deploy_job_runs_on_the_live_runner_with_one_command(self, workflow):
        job = workflow["jobs"]["deploy"]
        assert job["runs-on"] == ["self-hosted", "privacyfence-live"]
        assert job["needs"] == "build"
        steps = _steps(job)
        assert not any("checkout" in s.get("uses", "") for s in steps)
        assert "secrets" not in str(job) and "env" not in job and all("env" not in s for s in steps)
        runs = [s["run"] for s in steps if "run" in s]
        assert runs == [RUN_STEP]
        assert [s["uses"].split("@")[0] for s in steps if "uses" in s] == ["actions/download-artifact"]
        assert steps[0]["with"]["name"] == "qa-server-image" and "qa-deploy" in steps[0]["with"]["path"]

    def test_the_verify_job_checks_the_public_address(self, workflow):
        job = workflow["jobs"]["verify"]
        assert job["runs-on"] == "ubuntu-latest" and job["needs"] == "deploy"
        text = str(job)
        assert "/login" in text
        for check in ("GET /mcp", "GET /.well-known/oauth-authorization-server",
                      "GET /.well-known/oauth-protected-resource/mcp", "GET /authorize", "GET /register",
                      "POST /token"):
            assert check in text
        assert "!= 404" in text

    def test_every_action_is_pinned_by_sha(self, workflow):
        for job in workflow["jobs"].values():
            for step in _steps(job):
                if "uses" in step:
                    assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


class TestDeployScript:
    @pytest.fixture
    def script(self) -> str:
        return (HOST / "pf-qa-deploy").read_text(encoding="utf-8")

    def test_it_takes_no_arguments(self, script):
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        assert '"$1"' not in code and "$1" not in code and "$@" not in code and "$*" not in code
        assert '"$#" -ne 0' in code

    def test_it_names_the_fixed_paths_and_the_tag(self, script):
        assert "TARBALL=/home/pfrunner/qa-deploy/privacyfence-org-qa.tar.gz" in script
        assert "IMAGE=privacyfence-org:qa-main" in script
        assert "--project-directory" in script and "/opt/pf-qa/run" in script and "--no-build" in script

    def test_the_checks_run_in_order(self, script):
        order = ["[ -L", "gunzip -t", "docker load", "up -d --no-build", "healthy", "docker image prune -f", 'rm -f "$TARBALL"']
        positions = [script.index(item) for item in order]
        assert positions == sorted(positions)

    def test_it_is_strict_posix_shell(self, script):
        assert script.startswith("#!/bin/sh\n") and "set -eu" in script
        assert subprocess.run(["sh", "-n", str(HOST / "pf-qa-deploy")], capture_output=True).returncode == 0

    def test_it_is_executable(self):
        assert (HOST / "pf-qa-deploy").stat().st_mode & 0o111


def test_the_sudoers_line_allows_no_argument():
    lines = [line for line in (HOST / "sudoers.pf-qa-deploy").read_text().splitlines() if not line.startswith("#")]
    assert lines == ['pfrunner ALL=(root) NOPASSWD: /usr/local/sbin/pf-qa-deploy ""']


class TestComposeOverride:
    @pytest.fixture
    def override(self) -> dict[str, Any]:
        return yaml.safe_load((HOST / "compose.override.yaml").read_text(encoding="utf-8"))

    def test_the_server_runs_as_the_images_user_from_the_local_image(self, override):
        server = override["services"]["privacyfence"]
        assert server["user"] == "10001:10001"
        assert server["pull_policy"] == "never"
        assert server["image"] == "privacyfence-org:qa-main"
        assert "idp_client_secret" in server["secrets"]

    def test_the_data_directory_is_the_one_shared_bind_mount(self, override):
        volumes = override["services"]["privacyfence"]["volumes"]
        assert volumes == ["/home/pfrunner/qa-org-data:/var/lib/privacyfence-org/.privacyfence"]

    def test_peek_sits_on_the_same_network(self, override):
        peek = override["services"]["peek"]
        assert peek["image"] == "ghcr.io/privacyfence/peek:main"
        assert peek["environment"]["PEEK_BASE_URL"] == "https://peek-qa.privacyfence.eu"
        assert peek["networks"] == ["pf"]

    def test_the_override_does_not_weaken_the_base_hardening(self, override):
        server = override["services"]["privacyfence"]
        assert not {"privileged", "cap_add", "read_only", "security_opt", "cap_drop"} & set(server)
        base = yaml.safe_load((ROOT / "deploy" / "docker" / "compose.yaml").read_text(encoding="utf-8"))
        assert base["services"]["privacyfence"]["read_only"] is True


def _matcher() -> list[str]:
    for line in (HOST / "Caddyfile").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts[:2] == ["@mcp", "path"]:
            return parts[2:]
    raise AssertionError("no @mcp matcher")


def _matches(patterns: list[str], path: str) -> bool:
    for pattern in patterns:
        if pattern.endswith("*"):
            if path.startswith(pattern[:-1]):
                return True
        elif path == pattern:
            return True
    return False


class TestCaddyfile:
    @pytest.fixture
    def text(self) -> str:
        return (HOST / "Caddyfile").read_text(encoding="utf-8")

    def test_the_matcher_covers_every_known_path(self):
        patterns = _matcher()
        for path in (
            routes_mcp.MCP_PATH, f"{routes_mcp.MCP_PATH}/", f"{routes_mcp.MCP_PATH}/x", oauth_provider.IDP_CALLBACK_PATH,
            AUTHORIZATION_PATH, TOKEN_PATH, REGISTRATION_PATH, REVOCATION_PATH,
            f"{oauth_provider.CONSENT_PATH}/abc",
            "/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp",
            "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
        ):
            assert _matches(patterns, path), path

    def test_the_matcher_covers_every_route_the_server_mounts(self, tmp_path, monkeypatch):
        from privacyfence.web import org_session
        from tests.unit.web.conftest import isolate_org_oauth_stores, org_idp

        isolate_org_oauth_stores(tmp_path, monkeypatch)
        provider = oauth_provider.OrgOAuthProvider(
            org_idp(), idp_callback_url="https://qa.example.com/oauth/idp/callback",
        )
        routes = routes_mcp.mount_org_oauth(
            provider, issuer_url="https://qa.example.com", sessions=org_session.OrgSessionStore(),
        )
        paths = {re.sub(r"\{[^}]+\}", "x", route.path) for route in routes}
        assert len(paths) >= 8
        patterns = _matcher()
        assert [p for p in sorted(paths) if not _matches(patterns, p)] == []

    def test_the_login_page_is_not_matched(self):
        assert not _matches(_matcher(), "/login") and not _matches(_matcher(), "/connect")

    def test_it_uses_cloudflare_addresses_and_the_origin_certificate(self, text):
        assert "{$PF_CF_RANGES}" in text and "CF-Connecting-IP" in text
        assert "reverse_proxy privacyfence:8765" in text and "reverse_proxy peek:7700" in text
        assert "respond @mcp 404" in text

    def test_no_secret_value_is_in_the_directory(self):
        for path in HOST.iterdir():
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"(?m)^\s*\w*(SECRET|KEY|TOKEN|PASSWORD)=\S", text), path.name
