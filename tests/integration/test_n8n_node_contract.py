"""Cross-language contract test for the n8n community node (n8n/): does the node's own
transport client (n8n/nodes/PrivacyFence/transport/) speak Streamable HTTP to the real Python
``/mcp`` endpoint with a scoped token?

Same shape as test_shim_mcp_contract.py: a real ``WebServer`` with a real connector and a real
scoped token, driven over real HTTP by ``n8n/test/contract-driver.mjs`` (run with ``node --import
tsx``), which prints one JSON line. Requires Node and npm on PATH; skipped otherwise, and when
``npm ci`` in ``n8n/`` cannot finish.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from privacyfence import paths as paths_module
from privacyfence import scoped_tokens
from privacyfence.auto_accept import TOOL_TO_GATE
from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.principal import LOCAL_PRINCIPAL
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.routes_mcp import SCOPED_META_TOOLS
from privacyfence.web.server import WebServer
from privacyfence.web_approval_ui import WebApprovalUI

REPO_ROOT = Path(__file__).resolve().parents[2]
N8N_DIR = REPO_ROOT / "n8n"
DRIVER = N8N_DIR / "test" / "contract-driver.mjs"

ECHO_TOOL = "contract_test_echo"
OTHER_TOOL = "contract_test_other"

pytestmark = [
    pytest.mark.skipif(
        shutil.which("node") is None,
        reason="Node not on PATH -- this contract test runs the node's real transport client",
    ),
    # Above the fixture's own 300s `npm ci` timeout, so a stuck install hits the fixture's
    # TimeoutExpired -> skip path instead of being killed here as a hard failure.
    pytest.mark.timeout(360),
]


class EchoConnector(Connector):
    @property
    def name(self) -> str:
        return "contract_test"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name=ECHO_TOOL, description="Echoes its arguments back.",
                     params=[ToolParam("message", "str", required=True)], read_only=True),
            ToolSpec(name=OTHER_TOOL, description="A second tool the token does not cover.",
                     params=[], read_only=True),
        ]

    async def call(self, tool: str, args: dict) -> object:
        return {"echoed": args}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_connectable(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.05)
    raise TimeoutError(f"{host}:{port} never became connectable") from last_exc


@pytest.fixture(scope="session")
def n8n_dependencies() -> Path:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm not on PATH -- this fixture runs `npm ci` in n8n/")
    try:
        subprocess.run([npm, "ci", "--silent"], cwd=N8N_DIR, check=True, capture_output=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"could not install n8n/ dependencies: {exc}")
    return N8N_DIR


@pytest.fixture
def daemon(monkeypatch):
    """A real WebServer with a store over a tmp dir; yields (mcp_url, store, token, secret)."""
    home = Path(f"/tmp/pf-n8n-ct-{uuid.uuid4().hex[:8]}")
    home.mkdir()
    monkeypatch.setattr(paths_module, "data_dir", lambda: home)
    # A token can only name tools the gate knows; register the echo tool as an ungated one.
    monkeypatch.setitem(TOOL_TO_GATE, ECHO_TOOL, "auto")
    policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)
    store = scoped_tokens.ScopedTokenStore(lambda: [LOCAL_PRINCIPAL])
    store.load()
    scoped_tokens.set_store(store)
    token, secret = store.create(
        LOCAL_PRINCIPAL, label="n8n-contract", days=30, rules=[], tools=[ECHO_TOOL], policy=policy,
    )
    dispatcher = McpDispatcher(
        lambda: {"contract_test": EchoConnector()},
        scoped_token_policy=scoped_tokens.policy_from_org_config({}, org_mode=False),
    )
    port = _free_port()
    server = WebServer(WebApprovalUI(), host="localhost", port=port, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield server.mcp_url, store, token, secret
    finally:
        server.stop()
        scoped_tokens.set_store(None)
        shutil.rmtree(home, ignore_errors=True)


def _drive(url: str, secret: str, tool: str, arguments: dict | None = None) -> dict:
    node = shutil.which("node")
    assert node is not None
    done = subprocess.run(
        [node, "--import", "tsx", str(DRIVER), url, secret, tool, json.dumps(arguments or {})],
        cwd=N8N_DIR, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_node_lists_the_tokens_tools_and_calls_one(daemon, n8n_dependencies):
    url, _, _, secret = daemon
    out = _drive(url, secret, ECHO_TOOL, {"message": "hello n8n"})

    assert set(out["tools"]) == {ECHO_TOOL} | set(SCOPED_META_TOOLS)
    assert out["result"] == {"json": {"echoed": {"message": "hello n8n"}}}
    assert out["closedSessionRefused"] is True


def test_a_tool_outside_the_token_is_denied(daemon, n8n_dependencies):
    url, _, _, secret = daemon
    out = _drive(url, secret, OTHER_TOOL)

    assert out["result"]["error"].startswith("Request denied")


def test_a_revoked_token_gives_the_refused_token_text(daemon, n8n_dependencies):
    url, store, token, secret = daemon
    assert store.revoke(LOCAL_PRINCIPAL, token.id)
    out = _drive(url, secret, ECHO_TOOL, {"message": "x"})

    assert "refused the token" in out["error"]
    assert "revoked or expired" in out["error"]
