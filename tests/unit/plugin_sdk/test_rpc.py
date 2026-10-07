"""The SDK's JSON-RPC peer: framing, limits and error shapes."""
from __future__ import annotations

import asyncio
import json

import pytest

from privacyfence_plugin_sdk._rpc import ERROR_CODES, Peer, RpcError
from privacyfence.plugins.constants import ERROR_CODES as DAEMON_ERROR_CODES

from .conftest import _PipeWriter

LIMITS = {"max_line_bytes": 1024, "max_in_flight": 2, "invalid_lines_limit": 3}


async def make_peer(handlers=None, notifications=None, on_close=None, **limits):
    to_peer, from_peer = asyncio.StreamReader(), asyncio.StreamReader()
    peer = Peer(to_peer, _PipeWriter(from_peer), handlers=handlers or {}, notification_handlers=notifications,
                on_close=on_close, **{**LIMITS, **limits})
    await peer.start()
    return peer, to_peer, from_peer


async def send(reader, message):
    reader.feed_data((message if isinstance(message, bytes) else json.dumps(message).encode()) + b"\n")


async def next_message(reader):
    return json.loads(await asyncio.wait_for(reader.readline(), 5))


def test_error_codes_match_the_daemons():
    assert ERROR_CODES == DAEMON_ERROR_CODES


class TestServing:
    async def test_request_gets_a_result(self):
        async def echo(params):
            return {"got": params}

        peer, to_peer, from_peer = await make_peer({"echo": echo})
        await send(to_peer, {"jsonrpc": "2.0", "id": 7, "method": "echo", "params": {"a": 1}})
        assert await next_message(from_peer) == {"jsonrpc": "2.0", "id": 7, "result": {"got": {"a": 1}}}
        await peer.close()

    async def test_errors(self):
        async def typed(params):
            raise RpcError("unknown_tool", "no", retryable=True, extra={"reason": "x"})

        async def broken(params):
            raise RuntimeError("secret")

        peer, to_peer, from_peer = await make_peer({"typed": typed, "broken": broken})
        await send(to_peer, {"jsonrpc": "2.0", "id": 1, "method": "typed"})
        error = (await next_message(from_peer))["error"]
        assert error["code"] == -32008 and error["data"] == {
            "reason": "x", "code": "unknown_tool", "detail": "no", "retryable": True}
        await send(to_peer, {"jsonrpc": "2.0", "id": 2, "method": "broken"})
        error = (await next_message(from_peer))["error"]
        assert error["data"]["code"] == "internal_error" and "secret" not in json.dumps(error)
        await send(to_peer, {"jsonrpc": "2.0", "id": 3, "method": "missing"})
        assert (await next_message(from_peer))["error"]["data"]["code"] == "method_not_found"
        await send(to_peer, {"jsonrpc": "2.0", "id": 4, "method": "typed", "params": [1]})
        assert (await next_message(from_peer))["error"]["data"]["code"] == "invalid_params"
        await peer.close()

    async def test_notifications_get_no_reply_and_unknown_ones_are_ignored(self):
        seen = []

        async def note(params):
            seen.append(params)

        peer, to_peer, from_peer = await make_peer(notifications={"note": note})
        await send(to_peer, {"jsonrpc": "2.0", "method": "note", "params": {"n": 1}})
        await send(to_peer, {"jsonrpc": "2.0", "method": "other"})
        await send(to_peer, {"jsonrpc": "2.0", "id": 1, "method": "missing"})
        assert (await next_message(from_peer))["id"] == 1  # the only reply
        assert seen == [{"n": 1}]
        await peer.close()

    async def test_batch_is_refused(self):
        peer, to_peer, from_peer = await make_peer()
        await send(to_peer, b"[]")
        assert (await next_message(from_peer))["error"]["data"]["code"] == "invalid_request"
        await peer.close()

    async def test_too_many_in_flight(self):
        gate = asyncio.Event()

        async def slow(params):
            await gate.wait()
            return 1

        peer, to_peer, from_peer = await make_peer({"slow": slow})
        for i in range(3):
            await send(to_peer, {"jsonrpc": "2.0", "id": i, "method": "slow"})
        refused = await next_message(from_peer)
        assert refused["id"] == 2 and refused["error"]["data"]["detail"] == "too many requests in flight"
        gate.set()
        await peer.close()


class TestInvalidLines:
    async def test_three_bad_lines_in_a_row_close_the_peer(self):
        closed = []
        peer, to_peer, from_peer = await make_peer(on_close=closed.append)
        for line in (b"junk", b"{}", b"x" * 2000):
            await send(to_peer, line)
        await asyncio.wait_for(peer.wait_closed(), 5)
        assert closed == ["invalid_output"] and peer.close_reason == "invalid_output"

    async def test_a_good_line_resets_the_streak(self):
        async def ok(params):
            return 1

        peer, to_peer, from_peer = await make_peer({"ok": ok})
        for _ in range(2):
            await send(to_peer, b"junk")
        await send(to_peer, {"jsonrpc": "2.0", "id": 1, "method": "ok"})
        for _ in range(2):
            await send(to_peer, b"junk")
        await send(to_peer, {"jsonrpc": "2.0", "id": 2, "method": "ok"})
        replies = []
        while len(replies) < 6:
            replies.append(await next_message(from_peer))
        assert not peer.closed
        await peer.close()

    async def test_eof_closes(self):
        peer, to_peer, _ = await make_peer()
        to_peer.feed_eof()
        await asyncio.wait_for(peer.wait_closed(), 5)
        assert peer.close_reason == "eof"


class TestRequests:
    async def test_result_error_and_timeout(self):
        peer, to_peer, from_peer = await make_peer()
        task = asyncio.ensure_future(peer.request("m", {"a": 1}))
        sent = await next_message(from_peer)
        assert sent == {"jsonrpc": "2.0", "id": 1, "method": "m", "params": {"a": 1}}
        await send(to_peer, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        assert await task == {"ok": True}

        task = asyncio.ensure_future(peer.request("m", {}))
        await next_message(from_peer)
        await send(to_peer, {"jsonrpc": "2.0", "id": 2, "error": {"code": -32005, "message": "upstream_error", "data": {
            "code": "upstream_error", "detail": "d", "retryable": False, "reason": "r"}}})
        with pytest.raises(RpcError) as info:
            await task
        assert (info.value.code, info.value.detail, info.value.extra) == ("upstream_error", "d", {"reason": "r"})

        with pytest.raises(RpcError) as info:
            await peer.request("slow", {}, timeout=0.05)
        assert info.value.code == "timeout"
        await peer.close()

    async def test_pending_requests_fail_when_the_peer_closes(self):
        peer, to_peer, from_peer = await make_peer()
        task = asyncio.ensure_future(peer.request("m", {}))
        await next_message(from_peer)
        to_peer.feed_eof()
        with pytest.raises(RpcError) as info:
            await task
        assert info.value.code == "internal_error"
        with pytest.raises(RpcError):
            await peer.request("m", {})
        with pytest.raises(RpcError):
            await peer.notify("m", {})

    async def test_notify_writes_a_line_without_id(self):
        peer, _, from_peer = await make_peer()
        await peer.notify("tools.changed", {"tools": []})
        assert await next_message(from_peer) == {"jsonrpc": "2.0", "method": "tools.changed", "params": {"tools": []}}
        await peer.close()


def test_rpc_error_unknown_code_becomes_internal():
    assert RpcError("made_up").code == "internal_error"


class TestStdio:
    """The real stdio transport, in a child process."""

    SCRIPT = (
        "import sys\n"
        "from privacyfence_plugin_sdk import Plugin\n"
        "plugin = Plugin(name='demo', version='1.0.0')\n"
        "@plugin.on('shutdown')\n"
        "async def bye(ctx, params):\n"
        "    print('stray output')\n"
        "plugin.run()\n"
    )

    async def test_run_serves_stdin_and_stdout_until_shutdown(self, tmp_path):
        import os
        import sys

        from .conftest import SDK_SRC

        script = tmp_path / "plugin_main.py"
        script.write_text(self.SCRIPT)
        env = {**os.environ, "PYTHONPATH": str(SDK_SRC)}
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocol_version": "1.0.0", "purpose": "run", "data_dir": str(tmp_path)}}
        process.stdin.write(json.dumps(init).encode() + b"\n")
        await process.stdin.drain()
        reply = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert reply["result"]["plugin"] == {"name": "demo", "version": "1.0.0"}
        process.stdin.write(b'{"jsonrpc":"2.0","method":"shutdown","params":{"grace_ms":1}}\n')
        await process.stdin.drain()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0
        assert stdout == b""  # the stray print went to stderr, not the protocol stream
        assert b"stray output" in stderr
