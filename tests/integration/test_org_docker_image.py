"""Tests for the organization-mode Docker image (deploy/docker/, docs/org-mode-docker.md).

Runs against an image already built by scripts/build_org_image.sh, named by PRIVACYFENCE_ORG_IMAGE
(``privacyfence-org:ci-<arch>`` in CI). The image is exercised the way an operator's Compose file
runs it -- read-only root filesystem, a tmpfs ``/tmp``, every capability dropped, no-new-privileges
-- on the host network against a real signed bundle and a loopback mock identity provider
(tests/integration/mock_idp.py), never a real IdP, certificate or reverse proxy.

Covers:

  - **Image metadata and identity**: the unprivileged ``privacyfence-org`` account (uid/gid 10001),
    the SIGINT stop signal, the health check and the OCI labels.
  - **A fresh volume**: it inherits the image's private (``10001:10001``, mode ``0700``) data
    directory and its ``org/`` subdirectory.
  - **Strict startup**: no bundle (the entrypoint refuses, exit 78) and an unsigned bundle (the
    daemon refuses) both stop the container instead of degrading to local mode.
  - **A running container**: the issuer host is served, an unexpected Host header is rejected,
    ``/login`` redirects to the identity provider, and Docker's own health check turns healthy.
  - **Lifecycle**: ``docker stop`` ends the daemon cleanly and promptly (SIGINT, exit 0), and state
    on the volume survives replacing the container.

Run by ``build.yml``'s ``build-org-image`` job (both architectures, at release-tag time and on a
dispatched pre-flight) and by ``tests.yml``'s ``org-docker-image`` job (amd64, every PR), both
added alongside the Compose files. Without ``PRIVACYFENCE_ORG_IMAGE`` the module is skipped;
``PRIVACYFENCE_REQUIRE_PACKAGED=1`` turns that skip into a failure (tests/conftest.py).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pytest
import requests

if not sys.platform.startswith("linux"):
    pytest.skip("only meaningful on Linux (the test uses --network host)", allow_module_level=True)
if shutil.which("docker") is None:
    pytest.skip("docker not on PATH", allow_module_level=True)
IMAGE = os.environ.get("PRIVACYFENCE_ORG_IMAGE", "")
if not IMAGE:
    pytest.skip(
        "org image not built yet -- set PRIVACYFENCE_ORG_IMAGE (see scripts/build_org_image.sh)",
        allow_module_level=True,
    )

from tests.integration.mock_idp import MockIdp  # noqa: E402

pytestmark = [pytest.mark.system, pytest.mark.packaged, pytest.mark.timeout(900)]

ISSUER_HOST = "pf.example.internal"
DATA_DIR = "/var/lib/privacyfence-org/.privacyfence"


def _docker(*args: str, input: str | None = None, check: bool = True, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], input=input, check=check, capture_output=True, text=True, timeout=timeout,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bundle(idp_issuer: str, port: int) -> dict:
    return {
        "mode": "org",
        "server": {
            "issuer_url": f"https://{ISSUER_HOST}",
            "bind_host": "0.0.0.0",
            "port": port,
            "trusted_proxies": ["127.0.0.1"],
        },
        "idp": {"issuer": idp_issuer, "client_id": "privacyfence-docker", "client_secret": "docker-test-secret"},
    }


def _signed_bundle(idp_issuer: str, port: int) -> dict:
    from privacyfence.org_bundle_signing import generate_keypair, sign_bundle

    private_key, _ = generate_keypair()
    return sign_bundle(_bundle(idp_issuer, port), private_key)


@pytest.fixture
def volume():
    name = f"pf-docker-test-{uuid.uuid4().hex[:8]}"
    _docker("volume", "create", name)
    yield name
    _docker("volume", "rm", "-f", name, check=False)


@pytest.fixture(scope="module")
def mock_idp():
    idp = MockIdp()
    yield idp
    idp.shutdown()


def _install_bundle(volume: str, payload: bytes | str) -> None:
    _docker(
        "run", "--rm", "-i", "-v", f"{volume}:{DATA_DIR}", "--entrypoint", "sh", IMAGE,
        "-c", f"umask 077 && cat > {DATA_DIR}/org/org_config.json",
        input=payload.decode() if isinstance(payload, bytes) else payload,
    )


def _start(request: pytest.FixtureRequest, volume: str, name: str) -> None:
    _docker(
        "run", "-d", "--name", name, "--network", "host", "--read-only", "--tmpfs", "/tmp",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-e", "PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP=1",
        "--health-interval", "2s", "--health-start-period", "60s",
        "-v", f"{volume}:{DATA_DIR}", IMAGE,
    )
    request.addfinalizer(lambda: _docker("rm", "-f", name, check=False))


def _get(port: int, path: str, host: str = ISSUER_HOST, **kwargs) -> requests.Response:
    return requests.get(f"http://127.0.0.1:{port}{path}", headers={"Host": host}, timeout=5, **kwargs)


def _wait_ready(port: int, name: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _docker("inspect", "--format", "{{.State.Status}}", name).stdout.strip()
        if state != "running":
            logs = _docker("logs", name, check=False)
            raise AssertionError(f"container {name} is {state}, not running -- logs:\n{logs.stdout}\n{logs.stderr}")
        try:
            if _get(port, "/.well-known/oauth-authorization-server").status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    logs = _docker("logs", name, check=False)
    raise AssertionError(f"{name} never served on port {port} within {timeout}s -- logs:\n{logs.stdout}\n{logs.stderr}")


def _stat(volume: str, *paths: str) -> list[str]:
    out = _docker(
        "run", "--rm", "-v", f"{volume}:{DATA_DIR}", "--entrypoint", "stat", IMAGE, "-c", "%u:%g %a", *paths,
    ).stdout
    return out.split("\n")[: len(paths)]


def test_runs_as_the_unprivileged_service_account():
    out = _docker("run", "--rm", "--entrypoint", "id", IMAGE).stdout
    assert "uid=10001(privacyfence-org) gid=10001(privacyfence-org)" in out


def test_a_fresh_volume_holds_a_private_data_directory(volume):
    assert _stat(volume, DATA_DIR, f"{DATA_DIR}/org") == ["10001:10001 700", "10001:10001 700"]


def test_image_metadata():
    info = json.loads(_docker("image", "inspect", IMAGE).stdout)[0]
    config = info["Config"]
    assert config["User"] == "10001:10001"
    assert config["StopSignal"] == "SIGINT"
    assert config["Healthcheck"]["Test"][-1] == "/opt/privacyfence/healthcheck.py"
    labels = config["Labels"]
    assert labels["org.opencontainers.image.source"] == "https://github.com/privacyfence/privacyfence"
    assert labels["org.opencontainers.image.licenses"] == "Apache-2.0"
    assert labels["org.opencontainers.image.version"]


def test_refuses_to_start_without_a_bundle(volume):
    result = _docker("run", "--rm", "-v", f"{volume}:{DATA_DIR}", IMAGE, check=False)
    assert result.returncode == 78, result.stderr
    assert "No organization bundle at" in result.stderr


def test_refuses_to_start_on_an_unsigned_bundle(volume, mock_idp):
    _install_bundle(volume, json.dumps(_bundle(mock_idp.base_url, _free_port())))
    result = _docker(
        "run", "--rm", "--network", "host", "-e", "PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP=1",
        "-v", f"{volume}:{DATA_DIR}", IMAGE, check=False,
    )
    assert result.returncode != 0
    assert "not signed" in (result.stdout + result.stderr).lower()


class TestRunningContainer:
    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _service(cls, request, mock_idp):
        # A class-scoped fixture cannot use the function-scoped `volume`, so it manages its own.
        name = f"pf-docker-test-{uuid.uuid4().hex[:8]}"
        port = _free_port()
        _docker("volume", "create", name)
        request.addfinalizer(lambda: _docker("volume", "rm", "-f", name, check=False))
        _install_bundle(name, json.dumps(_signed_bundle(mock_idp.base_url, port)))
        _start(request, name, name)
        _wait_ready(port, name)
        cls.port = port
        cls.container = name

    def test_the_issuer_host_is_served(self):
        assert _get(self.port, "/.well-known/oauth-authorization-server").status_code == 200

    def test_an_unexpected_host_header_is_rejected(self):
        assert _get(self.port, "/.well-known/oauth-authorization-server", host="evil.example").status_code == 400

    def test_login_redirects_to_the_identity_provider(self):
        assert _get(self.port, "/login", allow_redirects=False).status_code == 302

    def test_the_healthcheck_reports_healthy(self):
        deadline = time.monotonic() + 90
        status = ""
        while time.monotonic() < deadline:
            status = _docker("inspect", "--format", "{{.State.Health.Status}}", self.container).stdout.strip()
            if status == "healthy":
                return
            time.sleep(1)
        pytest.fail(f"container never became healthy, last status {status!r}")


def test_docker_stop_shuts_down_cleanly(request, volume, mock_idp):
    port = _free_port()
    name = f"pf-docker-test-{uuid.uuid4().hex[:8]}"
    _install_bundle(volume, json.dumps(_signed_bundle(mock_idp.base_url, port)))
    _start(request, volume, name)
    _wait_ready(port, name)
    started = time.monotonic()
    _docker("stop", "-t", "30", name)
    assert time.monotonic() - started < 15
    assert _docker("inspect", "--format", "{{.State.ExitCode}}", name).stdout.strip() == "0"
    logs = _docker("logs", name)
    assert "Interrupted; shutting down" in logs.stdout + logs.stderr


def test_state_survives_a_new_container_on_the_same_volume(request, volume, mock_idp):
    port = _free_port()
    _install_bundle(volume, json.dumps(_signed_bundle(mock_idp.base_url, port)))

    def deployment_id() -> str:
        return _docker(
            "run", "--rm", "--entrypoint", "cat", "-v", f"{volume}:{DATA_DIR}", IMAGE, f"{DATA_DIR}/deployment_id",
        ).stdout.strip()

    first = f"pf-docker-test-{uuid.uuid4().hex[:8]}"
    _start(request, volume, first)
    _wait_ready(port, first)
    before = deployment_id()
    assert before
    _docker("stop", "-t", "30", first)
    _docker("rm", first)

    second = f"pf-docker-test-{uuid.uuid4().hex[:8]}"
    _start(request, volume, second)
    _wait_ready(port, second)
    assert deployment_id() == before
