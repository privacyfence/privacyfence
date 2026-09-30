"""The stdio tool catalog Glama starts: it lists every tool and refuses every call."""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.message import SessionMessage

from privacyfence import catalog_server
from privacyfence.catalog_server import (
    CATALOG_CALL_MESSAGE,
    CATALOG_INSTRUCTIONS,
    build_catalog_server,
)
from privacyfence.connector_catalog import catalog_tools

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_lists_the_catalog_in_process():
    async with Client(build_catalog_server()) as client:
        result = await client.list_tools()
        assert [t.name for t in result.tools] == [t.name for t in catalog_tools()]
        assert client.server_info.name == "privacyfence"
        assert client.instructions == CATALOG_INSTRUCTIONS


@pytest.mark.parametrize("name", ["gmail_list_messages", "privacyfence_status", "no_such_tool"])
async def test_every_call_returns_the_catalog_message(name: str):
    async with Client(build_catalog_server()) as client:
        result = await client.call_tool(name, {})
    assert result.is_error
    assert len(result.content) == 1
    assert result.content[0].text == CATALOG_CALL_MESSAGE


@pytest.mark.timeout(90)
async def test_runs_over_stdio_without_touching_home(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "privacyfence.catalog_server"],
        env={
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LOCALAPPDATA": str(home),
            "APPDATA": str(home),
        },
    )
    async with Client(params) as client:
        result = await client.list_tools()
        assert len(result.tools) == len(catalog_tools())
    assert list(home.iterdir()) == []


async def test_serve_stdio_returns_when_input_closes(monkeypatch: pytest.MonkeyPatch):
    @contextlib.asynccontextmanager
    async def closed_stdio():
        rs, rr = anyio.create_memory_object_stream[SessionMessage | Exception](0)
        ws, wr = anyio.create_memory_object_stream[SessionMessage](0)
        await rs.aclose()
        async with rr, ws, wr:
            yield rr, ws

    monkeypatch.setattr(catalog_server, "stdio_server", closed_stdio)
    with anyio.fail_after(10):
        await catalog_server.serve_stdio()


def test_main_runs_serve_stdio(monkeypatch: pytest.MonkeyPatch):
    calls: list[object] = []
    monkeypatch.setattr(catalog_server.anyio, "run", lambda func: calls.append(func))
    catalog_server.main()
    assert calls == [catalog_server.serve_stdio]


def test_imports_nothing_from_the_daemon():
    result = subprocess.run(
        [sys.executable, "-c",
         "import json, sys, privacyfence.catalog_server; print(json.dumps(sorted(sys.modules)))"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    loaded = set(json.loads(result.stdout))
    daemon_only = {
        "privacyfence.daemon_main",
        "privacyfence.update_checker",
        "privacyfence.web.server",
        "privacyfence.web.routes_mcp",
        "privacyfence.settings_controller",
        "privacyfence.companion",
    }
    assert loaded & daemon_only == set()


def test_packaging_doc_names_the_catalog_command():
    text = (REPO_ROOT / "docs" / "packaging.md").read_text(encoding="utf-8")
    assert "privacyfence.catalog_server" in text
    assert "mcp-proxy" in text
