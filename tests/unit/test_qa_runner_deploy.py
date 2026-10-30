"""The QA server's deploy: the workflow, the host script, the sudoers rule, the compose override
and the proxy that keeps /mcp closed (docs/connector-qa.md, "The QA server")."""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
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

    def test_no_job_runs_from_a_branch_other_than_main(self, workflow):
        for name, job in workflow["jobs"].items():
            assert job["if"] == "github.ref == 'refs/heads/main'", name

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
        order = ["[ -L", 'cat "$TARBALL"', "gunzip -t", "manifest.json", "docker load", "up -d --no-build", "healthy",
                 "docker image prune -f", 'rm -f "$TARBALL"']
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        positions = [code.index(item) for item in order]
        assert positions == sorted(positions)

    def test_the_tarball_is_read_once_into_a_root_owned_copy(self, script):
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        reads = [line for line in code.splitlines() if "$TARBALL" in line and "echo" not in line]
        reads = [line.strip() for line in reads]
        assert reads == ['if [ -L "$TARBALL" ] || [ ! -f "$TARBALL" ]; then', 'cat "$TARBALL" > "$work/image.tar.gz"',
                         'rm -f "$TARBALL"']
        assert 'cat "$TARBALL" > "$work/image.tar.gz"' in code
        assert 'docker load -i "$work/image.tar"' in code and "gunzip -c |" not in code

    def test_it_is_strict_posix_shell(self, script):
        assert script.startswith("#!/bin/sh\n") and "set -eu" in script
        assert subprocess.run(["sh", "-n", str(HOST / "pf-qa-deploy")], capture_output=True).returncode == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell script run through sh and its executable bit; the QA host is Linux")
    def test_it_is_executable(self):
        assert (HOST / "pf-qa-deploy").stat().st_mode & 0o111


def _tar(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _image_tar(tags: list[str] | None, *, index: dict[str, Any] | None = None, entries: int = 1) -> bytes:
    manifest = [{"Config": "c.json", "RepoTags": tags, "Layers": []} for _ in range(entries)]
    files = {"manifest.json": json.dumps(manifest).encode()}
    if index is not None:
        files["index.json"] = json.dumps(index).encode()
    return _tar(files)


FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
    load) echo "$FAKE_LOAD_OUTPUT" ;;
    image) [ "$2" = inspect ] ;;
    compose)
        case "$4" in
            ps) echo cid ;;
        esac ;;
    inspect) echo healthy ;;
esac
exit 0
"""


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell script run through sh and its executable bit; the QA host is Linux")
class TestDeployScriptBehaviour:
    """Runs the real script with the fixed paths pointed at a temporary directory and a fake docker."""

    def _run(self, tmp_path, payload: bytes | None, *, output="Loaded image: privacyfence-org:qa-main", link=False):
        script = (HOST / "pf-qa-deploy").read_text(encoding="utf-8")
        tarball = tmp_path / "in.tar.gz"
        if link:
            (tmp_path / "real").write_bytes(gzip.compress(payload or b""))
            tarball.symlink_to(tmp_path / "real")
        elif payload is not None:
            tarball.write_bytes(gzip.compress(payload))
        patched = script.replace("TARBALL=/home/pfrunner/qa-deploy/privacyfence-org-qa.tar.gz", f"TARBALL={tarball}")
        patched = patched.replace("PROJECT_DIR=/opt/pf-qa/run", f"PROJECT_DIR={tmp_path}").replace("sleep 5", "sleep 0")
        copy = tmp_path / "deploy.sh"
        copy.write_text(patched, encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        log = tmp_path / "docker.log"
        log.write_text("")
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log),
               "FAKE_LOAD_OUTPUT": output}
        result = subprocess.run(["sh", str(copy)], capture_output=True, text=True, env=env, check=False)
        return result, log.read_text().splitlines(), tarball

    def test_the_expected_image_loads_and_the_tarball_is_removed(self, tmp_path):
        result, calls, tarball = self._run(tmp_path, _image_tar(["privacyfence-org:qa-main"]))
        assert result.returncode == 0, result.stderr
        assert any(call.startswith("load -i ") for call in calls)
        assert any(call.startswith("compose ") and "up -d --no-build" in call for call in calls)
        assert not tarball.exists()

    @pytest.mark.parametrize("output", [
        "Loaded image: privacyfence-org:qa-main",
        "Loaded image ID: sha256:" + "a" * 64,
    ])
    def test_both_image_store_output_formats_are_accepted(self, tmp_path, output):
        result, _, _ = self._run(tmp_path, _image_tar(["privacyfence-org:qa-main"]), output=output)
        assert result.returncode == 0, result.stderr

    def test_other_load_output_is_refused(self, tmp_path):
        result, _, _ = self._run(tmp_path, _image_tar(["privacyfence-org:qa-main"]), output="Loaded image: caddy:2")
        assert result.returncode == 1 and "unexpected docker load output" in result.stderr

    @pytest.mark.parametrize("tags", [["caddy:2"], ["privacyfence-org:qa-main", "caddy:2"], [], None])
    def test_a_manifest_with_any_other_tag_is_refused_before_loading(self, tmp_path, tags):
        result, calls, _ = self._run(tmp_path, _image_tar(tags))
        assert result.returncode != 0
        assert not any(call.startswith(("load", "compose")) for call in calls)

    def test_two_images_in_one_archive_are_refused(self, tmp_path):
        result, calls, _ = self._run(tmp_path, _image_tar(["privacyfence-org:qa-main"], entries=2))
        assert result.returncode != 0 and calls == []

    def test_a_name_hidden_in_the_oci_index_is_refused(self, tmp_path):
        index = {"manifests": [{"annotations": {"io.containerd.image.name": "docker.io/library/caddy:2"}}]}
        result, calls, _ = self._run(tmp_path, _image_tar(["privacyfence-org:qa-main"], index=index))
        assert result.returncode != 0 and "also names" in result.stderr and calls == []

    def test_the_expected_names_in_the_oci_index_are_accepted(self, tmp_path):
        index = {"manifests": [{"annotations": {
            "io.containerd.image.name": "docker.io/library/privacyfence-org:qa-main",
            "org.opencontainers.image.ref.name": "qa-main"}}]}
        result, _, _ = self._run(tmp_path, _image_tar(["privacyfence-org:qa-main"], index=index))
        assert result.returncode == 0, result.stderr

    def test_an_archive_without_a_manifest_is_refused(self, tmp_path):
        result, calls, _ = self._run(tmp_path, _tar({"other": b"x"}))
        assert result.returncode != 0 and calls == []

    def test_a_symlink_is_refused(self, tmp_path):
        result, calls, _ = self._run(tmp_path, _image_tar(["privacyfence-org:qa-main"]), link=True)
        assert result.returncode == 1 and calls == []

    def test_a_missing_file_is_refused(self, tmp_path):
        result, calls, _ = self._run(tmp_path, None)
        assert result.returncode == 1 and calls == []


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

    def test_every_image_other_than_the_local_one_is_pinned_by_digest(self, override):
        for name, service in override["services"].items():
            if name == "privacyfence":
                continue
            assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", service["image"]), (name, service["image"])

    def test_the_secret_file_is_readable_by_the_container_user_only(self):
        readme = (HOST / "README.md").read_text(encoding="utf-8")
        assert "`chown 10001:10001`" in readme and "`chmod 0400`" in readme
        assert "(mode 0600)" not in readme

    def test_peek_sits_on_the_same_network(self, override):
        peek = override["services"]["peek"]
        assert peek["image"].startswith("ghcr.io/privacyfence/peek:main@sha256:")
        assert peek["environment"]["PEEK_BASE_URL"] == "https://peek-qa.privacyfence.eu"
        assert peek["networks"] == ["pf"]

    def test_the_override_does_not_weaken_the_base_hardening(self, override):
        server = override["services"]["privacyfence"]
        assert not {"privileged", "cap_add", "read_only", "security_opt", "cap_drop"} & set(server)
        base = yaml.safe_load((ROOT / "deploy" / "docker" / "compose.yaml").read_text(encoding="utf-8"))
        assert base["services"]["privacyfence"]["read_only"] is True


def test_the_qa_environment_limits_the_sign_in_domains():
    text = (HOST / "privacyfence.env.example").read_text(encoding="utf-8")
    assert re.search(r"(?m)^PRIVACYFENCE_ALLOWED_DOMAINS=[a-z0-9.-]+\.[a-z]+$", text)


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
