"""Cross-language contract test for the .mcpb shim (D11): does the real
Node shim (mcpb/shim/) actually speak Streamable HTTP to the real Python
``/mcp`` endpoint?

This is the shim's counterpart to test_bridge_daemon_contract.py -- same
reasoning, same shape (spawn the real built artifact, drive it with the
official ``mcp`` Python client over real stdio, assert the round trip
worked), a different transport underneath: a much smaller Node-side test
that drives the shim's stdio transport with the mcp client against a real
/mcp, asserting that one initialize and one tools/call make the round trip
with the bearer header attached and the mcp_url file honoured. That is a
passthrough test, not a schema test: the shim knows no schemas, so there is
nothing else to assert.

Requires Node on PATH; skipped automatically otherwise -- same posture as
test_bridge_daemon_contract.py, and for the same reason this lives under
tests/integration/ rather than tests/unit/. Also requires the `mcp` package
(test-only, see pyproject.toml's [project.optional-dependencies].test).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from privacyfence import local_files as local_files_module  # noqa: E402
from privacyfence import paths as paths_module  # noqa: E402
from privacyfence.connector import Connector, ToolParam, ToolSpec  # noqa: E402
from privacyfence.web.mcp_dispatch import McpDispatcher  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO_ROOT / "mcpb" / "shim"
SHIM_ENTRY = SHIM_DIR / "dist" / "shim.js"

pytestmark = [
    pytest.mark.skipif(
        shutil.which("node") is None,
        reason="Node not on PATH -- this contract test spawns the real shim",
    ),
    # npm install/build (only on the first run per session -- see
    # built_shim_entry) can be slow on a cold cache; the suite's global 30s
    # pytest-timeout is tuned for pure-Python socket tests, not this.
    #
    # This must stay above built_shim_entry's own subprocess timeouts (180s
    # install + 60s build = 240s): a slow-but-succeeding install needs room
    # to finish, and a genuinely stuck one should hit the fixture's own
    # subprocess.TimeoutExpired -> pytest.skip(...) path instead of being
    # killed here first (which surfaces as a hard failure, not a skip).
    pytest.mark.timeout(260),
]


class EchoConnector(Connector):
    """A minimal real connector -- exercises the manifest -> dynamic
    tool-registration -> tool-call path end to end, not a mocked stand-in.
    Deliberately the same shape as test_bridge_daemon_contract.py's
    EchoConnector -- both contract tests should be exercising the same kind
    of real round trip, just over different transports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "contract_test"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="contract_test_echo",
                description="Echoes its arguments back -- used only by the shim<->/mcp contract test.",
                params=[ToolParam("message", "str", required=True)],
                read_only=True,
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        return {"echoed": args}


class FileBridgeTestConnector(Connector):
    """ADR 0007: a real connector whose tools call straight into
    local_files.py (require_local_files/read_local_file/deliver_file),
    exactly like drive.py/gmail.py/confluence.py do -- proves the real
    Python and TypeScript halves of the file-bridge wire protocol agree,
    the same "spawn the real artifact, drive it end to end" posture this
    whole module already applies to the plain-passthrough case above.
    ``local_files.force_bridge_for_tests(True)`` (set by the test that uses
    this connector) is what makes ``can_access_user_files()`` false here
    without a real privilege-separated install."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "file_bridge_test"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="file_bridge_test_upload",
                description="Reads local_path (via the file bridge) and echoes its content back.",
                params=[ToolParam("local_path", "str", required=True)],
                read_only=False,
            ),
            ToolSpec(
                name="file_bridge_test_download",
                description="Writes a fixed payload to destination_dir (via the file bridge).",
                params=[ToolParam("destination_dir", "str", required=True)],
                read_only=False,
            ),
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        if tool == "file_bridge_test_upload":
            local_path = args["local_path"]
            local_files_module.require_local_files(
                [local_path], max_total_bytes=10_000, download_mode="local",
            )
            data = local_files_module.read_local_file(local_path, download_mode="local")
            return {"content": data.decode("utf-8")}
        if tool == "file_bridge_test_download":
            return local_files_module.deliver_file(
                args["destination_dir"], "bridged.txt", b"content from the daemon",
                "text/plain", download_mode="local",
            )
        raise ValueError(f"unknown tool: {tool!r}")


def _free_port() -> int:
    """A real, currently-unused TCP port -- WebServer.start() doesn't report
    back the OS-assigned port for ``port=0`` (see web/server.py's own
    ``mcp_url`` property, which is built from the ``port`` the caller
    passed in), so unlike a bridge test's ephemeral daemon socket, this test
    needs a real port number *before* starting the server."""
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


def _shim_data_dir(shim_home: Path) -> Path:
    """The one directory both the (monkeypatched) Python side and the real
    Node shim subprocess must agree is ``paths.data_dir()`` -- mirrors
    protocol.ts's own ``dataDir()``: ``.privacyfence`` under ``shim_home``
    on POSIX, ``PrivacyFence`` under it on Windows (where the shim finds it
    via ``LOCALAPPDATA=<shim_home>``, set in the test's own spawn env --
    see paths.py's ``is_windows()``/``windows_data_dir()`` for why the two
    platforms don't share one path shape)."""
    return shim_home / ("PrivacyFence" if paths_module.is_windows() else ".privacyfence")


@pytest.fixture
def shim_home():
    """A tmp HOME/LOCALAPPDATA root whose discovery files (mcp_url,
    mcp_token) both the real WebServer (monkeypatched to treat
    ``_shim_data_dir()`` of this directory as ``paths.data_dir()`` below)
    and the real shim subprocess (which derives its own copy of that same
    directory from ``$HOME``/``$LOCALAPPDATA`` exactly like production, via
    mcpb/shim/src/protocol.ts's ``dataDir()``) will agree on."""
    directory = Path(f"/tmp/pf-shim-ct-{uuid.uuid4().hex[:8]}")
    _shim_data_dir(directory).mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
async def running_mcp_server(shim_home, monkeypatch):
    # web/server.py and web/mcp_auth.py both resolve every file they write
    # (web_token, mcp_token, mcp_url) through paths.data_dir() -- patching
    # that one function is enough to redirect all of them into
    # _shim_data_dir(shim_home), matching what the real daemon does under a
    # real HOME/LOCALAPPDATA.
    monkeypatch.setattr(paths_module, "data_dir", lambda: _shim_data_dir(shim_home))

    connector = EchoConnector()
    dispatcher = McpDispatcher(lambda: {"contract_test": connector})
    port = _free_port()
    server = WebServer(WebApprovalUI(), host="localhost", port=port, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield connector
    finally:
        server.stop()


@pytest.fixture
async def running_file_bridge_server(shim_home, monkeypatch):
    """Same shape as running_mcp_server, registering FileBridgeTestConnector
    instead and forcing the file bridge on (see local_files.
    force_bridge_for_tests's own docstring) so this test exercises the
    upload/download handshakes even though nothing here is actually
    privilege-separated."""
    monkeypatch.setattr(paths_module, "data_dir", lambda: _shim_data_dir(shim_home))
    local_files_module.force_bridge_for_tests(True)

    connector = FileBridgeTestConnector()
    dispatcher = McpDispatcher(lambda: {"file_bridge_test": connector})
    port = _free_port()
    server = WebServer(WebApprovalUI(), host="localhost", port=port, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield connector
    finally:
        server.stop()
        local_files_module.force_bridge_for_tests(False)


@pytest.fixture(scope="session")
def built_shim_entry() -> Path:
    """(Re)builds mcpb/shim/dist/shim.js once per test session -- see
    test_bridge_daemon_contract.py's built_bridge_entry for the identical
    reasoning (skip rather than fail when npm/node aren't fully set up, so
    this test degrades gracefully in environments that only have `node` on
    PATH for other reasons)."""
    # Resolve to npm's actual path rather than passing the bare "npm" --
    # on Windows npm is npm.cmd, and subprocess's CreateProcess (unlike
    # cmd.exe) never consults PATHEXT itself, so a bare "npm" raises
    # FileNotFoundError ([WinError 2]) even though shutil.which() (which
    # does consult PATHEXT) just found it one line above.
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm not on PATH -- this fixture builds the shim via `npm install`/`npm run build`")
    try:
        subprocess.run(
            [npm, "install", "--silent"], cwd=SHIM_DIR, check=True, capture_output=True, timeout=180
        )
        subprocess.run(
            [npm, "run", "build", "--silent"], cwd=SHIM_DIR, check=True, capture_output=True, timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"could not build mcpb/shim/dist/shim.js: {exc}")
    if not SHIM_ENTRY.exists():
        pytest.skip(f"{SHIM_ENTRY} missing after build")
    return SHIM_ENTRY


async def test_shim_proxies_a_real_initialize_and_tool_call_over_mcp(
    running_mcp_server, built_shim_entry, shim_home
):
    """The single highest-value assertion here: a real, freshly-built
    `node mcpb/shim/dist/shim.js`, given only $HOME (no config file, no
    token on the command line -- see mcpb/manifest.json.tmpl's
    server.mcp_config), discovers mcp_url/mcp_token itself, attaches the
    bearer header, and round-trips a real tool call through a real Python
    /mcp endpoint with neither side knowing the other's language."""
    params = StdioServerParameters(
        command="node",
        args=[str(built_shim_entry)],
        # protocol.ts's dataDir() resolves mcp_url/mcp_token via
        # LOCALAPPDATA on Windows and Node's os.homedir() (which reads
        # $USERPROFILE there, never $HOME) everywhere else -- set all
        # three so the spawned shim agrees with shim_home/_shim_data_dir()
        # regardless of which platform this actually runs on.
        env={"HOME": str(shim_home), "USERPROFILE": str(shim_home), "LOCALAPPDATA": str(shim_home)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "contract_test_echo" in names
            assert "privacyfence_check_policy" in names

            result = await session.call_tool(
                "contract_test_echo", {"message": "hello through the shim"}
            )
            assert result.is_error is not True
            assert result.structured_content == {"echoed": {"message": "hello through the shim"}}

    assert running_mcp_server.calls == [("contract_test_echo", {"message": "hello through the shim"})]


async def test_shim_file_bridge_upload_and_download_round_trip(
    running_file_bridge_server, built_shim_entry, shim_home, tmp_path,
):
    """ADR 0007's own cross-language proof: the real shim and the real
    daemon agree on the need_uploads/deliver wire format, not just each
    side's own guess at it (tests/unit/test_local_files.py and
    mcpb/shim/test/fileBridge.test.ts each already prove their own half in
    isolation). local_files.force_bridge_for_tests(True) (set by the
    running_file_bridge_server fixture) is what makes this exercise the
    bridge at all, since nothing in this test environment is actually
    privilege-separated.
    """
    upload_source = tmp_path / "source.txt"
    upload_source.write_text("uploaded through the file bridge")
    download_dest = tmp_path / "downloads"
    download_dest.mkdir()

    params = StdioServerParameters(
        command="node",
        args=[str(built_shim_entry)],
        env={"HOME": str(shim_home), "USERPROFILE": str(shim_home), "LOCALAPPDATA": str(shim_home)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            upload_result = await session.call_tool(
                "file_bridge_test_upload", {"local_path": str(upload_source)},
            )
            assert upload_result.is_error is not True
            assert upload_result.structured_content == {"content": "uploaded through the file bridge"}

            download_result = await session.call_tool(
                "file_bridge_test_download", {"destination_dir": str(download_dest)},
            )
            assert download_result.is_error is not True
            content = download_result.structured_content
            assert content["delivery"] == "local_disk"
            saved_path = Path(content["path"])
            assert saved_path == download_dest / "bridged.txt"
            assert saved_path.read_bytes() == b"content from the daemon"

    # The upload tool is dispatched twice: once before any bytes are
    # staged (require_local_files() raises LocalFilesNeeded, which the
    # shim turns into the upload handshake instead of an error -- see
    # routes_mcp.py's handle_call_tool), and once more on the resend, once
    # the shim has PUT the file and the daemon can claim it. Both
    # dispatches reach the connector; only the second one returns.
    assert running_file_bridge_server.calls == [
        ("file_bridge_test_upload", {"local_path": str(upload_source)}),
        ("file_bridge_test_upload", {"local_path": str(upload_source)}),
        ("file_bridge_test_download", {"destination_dir": str(download_dest)}),
    ]
