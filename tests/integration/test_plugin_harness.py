"""The plugin end-to-end harness leaves the test's event loop with nothing to wait on.

pytest-asyncio closes each test's loop after the test, and on Windows that close waits for every
overlapped operation the loop still owns. So the plugin processes run on the web server's loop, as
in the daemon, and ``Stack.stop`` checks that the clients a test opened are gone before it stops
the server.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.fixtures.plugins.echo import harness
from tests.fixtures.plugins.echo.harness import Stack, install_echo, mcp_session, web_session
from tests.loop_watch import pending_io

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    stack = Stack(tmp_path, monkeypatch, serve=True)
    install_echo(stack.plugins)
    await stack.start()
    await stack.enable()
    try:
        yield stack
    finally:
        await stack.stop()


def other_tasks() -> set[asyncio.Task]:
    return asyncio.all_tasks() - {asyncio.current_task()}


async def test_the_plugin_process_belongs_to_the_web_servers_loop(stack):
    assert stack.row()["state"] == "running"
    assert stack.host.loop is stack.web_loop
    assert stack.web_loop is not asyncio.get_running_loop()
    assert pending_io(asyncio.get_running_loop()) == []
    assert other_tasks() == set()


async def test_a_closed_mcp_session_leaves_no_task_or_io_behind(stack):
    async with mcp_session(stack.server) as mcp:
        assert "echo_auto_read" in await mcp.tool_names()
        await mcp.call("echo_auto_read", text="hi")
        assert pending_io(asyncio.get_running_loop()) != []

    assert other_tasks() == set()
    assert await harness.drain_io(asyncio.get_running_loop(), timeout=2.0) == []


async def test_a_closed_web_session_leaves_no_io_behind(stack):
    client = await web_session(stack.server)
    try:
        assert (await client.get("/plugins/echo/")).status_code == 200
    finally:
        await client.aclose()

    assert await harness.drain_io(asyncio.get_running_loop(), timeout=2.0) == []


async def test_stop_names_a_client_still_reading_and_still_stops_the_server(tmp_path, monkeypatch):
    stack = Stack(tmp_path, monkeypatch, serve=True)
    await stack.start()
    reader, writer = await asyncio.open_connection("localhost", stack.server.port)
    reading = asyncio.ensure_future(reader.read())
    try:
        original = harness.drain_io
        monkeypatch.setattr(harness, "drain_io", lambda loop: original(loop, timeout=0.3))

        with pytest.raises(AssertionError, match="still waits on I/O after the clients closed"):
            await stack.stop()

        # The client was still connected when the leftovers were taken: the server went after it.
        assert stack.server.stopped
        assert await asyncio.wait_for(reading, 5) == b""
    finally:
        reading.cancel()
        writer.close()
