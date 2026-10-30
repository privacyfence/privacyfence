"""The Docker deployment files are the ones the operator page shows, and keep what the daemon needs.

``docs/org-mode-docker.md`` embeds ``deploy/docker/compose.yaml`` and ``deploy/docker/Caddyfile``
verbatim, and tells operators to build the bundle with the trusted-proxy address the Compose file
fixes. Nothing else in the suite reads these files; ``test_org_docker_image.py`` runs the image on
its own, without Compose or Caddy. Both files are compared with the page by heading name, so the
page stays readable and a renumbered or moved section does not break the lookup.

Two kinds of check:

- Static assertions in plain Python on the lines the daemon depends on: no published daemon port,
  the hardening settings, Caddy's fixed address inside the subnet and named as the trusted proxy,
  the proxied port, and the data volume path the image declares. These run everywhere.
- ``docker compose config``, which catches a syntax or schema error and the required-variable guard.
  It skips when the Compose plugin is absent.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from privacyfence.web.server import DEFAULT_PORT
from tests.integration.guide_sections import fenced_block

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy" / "docker"
COMPOSE = DEPLOY / "compose.yaml"
CADDYFILE = DEPLOY / "Caddyfile"
DOCKERFILE = DEPLOY / "Dockerfile"
DOC = REPO_ROOT / "docs" / "org-mode-docker.md"

DATA_VOLUME_PATH = "/var/lib/privacyfence-org/.privacyfence"
CADDY_ADDRESS = "172.30.87.2"

PROBE_TIMEOUT_S = 20
COMPOSE_TIMEOUT_S = 60


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return loaded


def _daemon() -> dict[str, Any]:
    service: dict[str, Any] = _compose()["services"]["privacyfence"]
    return service


def test_compose_file_matches_the_doc() -> None:
    assert fenced_block(DOC, "Compose file", "yaml") == COMPOSE.read_text(encoding="utf-8")


def test_caddyfile_matches_the_doc() -> None:
    assert fenced_block(DOC, "Caddyfile", "") == CADDYFILE.read_text(encoding="utf-8")


def test_the_daemon_publishes_no_port() -> None:
    assert "ports" not in _daemon()


def test_the_daemon_runs_hardened() -> None:
    daemon = _daemon()
    assert daemon["read_only"] is True
    assert daemon["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in daemon["security_opt"]
    assert "/tmp" in daemon["tmpfs"]


def test_caddy_is_the_documented_trusted_proxy() -> None:
    compose = _compose()
    address = compose["services"]["caddy"]["networks"]["pf"]["ipv4_address"]
    subnet = compose["networks"]["pf"]["ipam"]["config"][0]["subnet"]
    assert address == CADDY_ADDRESS
    assert ipaddress.ip_address(address) in ipaddress.ip_network(subnet)
    assert f"`PRIVACYFENCE_TRUSTED_PROXIES` | none | `{CADDY_ADDRESS}`" in DOC.read_text(encoding="utf-8")
    env_example = (DEPLOY / "privacyfence.env.example").read_text(encoding="utf-8")
    assert re.search(rf"^PRIVACYFENCE_TRUSTED_PROXIES={re.escape(CADDY_ADDRESS)}$", env_example, flags=re.MULTILINE)


def test_caddy_proxies_to_the_daemon_port() -> None:
    assert f"reverse_proxy privacyfence:{DEFAULT_PORT}" in CADDYFILE.read_text(encoding="utf-8")
    assert DEFAULT_PORT == 8765


def test_the_data_volume_is_the_images_data_directory() -> None:
    assert f"pf-data:{DATA_VOLUME_PATH}" in _daemon()["volumes"]
    (volume_line,) = re.findall(r"^VOLUME .*$", DOCKERFILE.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert DATA_VOLUME_PATH in volume_line


def _run_docker(cmd: list[str], *, env: dict[str, str] | None, timeout: int) -> tuple[int, str]:
    """Run ``cmd`` and return (exit code, stderr), writing to files rather than pipes.

    ``docker compose`` is a CLI plugin that Docker starts as a grandchild. On the Windows runner
    that grandchild can outlive the timeout and keep a pipe open, so ``subprocess.run`` kills the
    ``docker`` process and then blocks forever draining the pipe (the hang that pytest's own
    timeout reported). With files there is nothing to drain once the direct child is gone.
    """
    with tempfile.TemporaryFile("w+", encoding="utf-8") as out, tempfile.TemporaryFile("w+", encoding="utf-8") as err:
        completed = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv, no shell
            cmd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=timeout, check=False
        )
        err.seek(0)
        return completed.returncode, err.read()


def _docker_compose(*args: str, env: dict[str, str]) -> tuple[int, str]:
    return _run_docker(["docker", "compose", "-f", str(COMPOSE), *args], env=env, timeout=COMPOSE_TIMEOUT_S)


@pytest.mark.timeout(PROBE_TIMEOUT_S + 2 * COMPOSE_TIMEOUT_S + 30)  # the suite-wide 30 s would cut the probe short
def test_docker_compose_accepts_the_file() -> None:
    try:
        probe_code, _ = _run_docker(["docker", "compose", "version"], env=None, timeout=PROBE_TIMEOUT_S)
    except FileNotFoundError:
        pytest.skip("docker compose not on PATH")
    except subprocess.TimeoutExpired:
        pytest.skip("docker compose did not answer `version`")
    if probe_code != 0:
        pytest.skip("docker compose not on PATH")
    base = {k: v for k, v in os.environ.items() if k != "PF_HOSTNAME"}

    accepted_code, accepted_err = _docker_compose("config", "--quiet", env={**base, "PF_HOSTNAME": "pf.example.com"})
    assert accepted_code == 0, accepted_err

    refused_code, refused_err = _docker_compose("config", "--quiet", env=base)
    assert refused_code != 0
    assert "set PF_HOSTNAME in .env" in refused_err
