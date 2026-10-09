"""Put the SDK sources on the import path and give tests an in-memory daemon end."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

SDK_SRC = Path(__file__).resolve().parents[3] / "plugin-sdk" / "src"
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))


class _PipeWriter:
    """Writes into another StreamReader, like one end of a pipe."""

    def __init__(self, target: asyncio.StreamReader) -> None:
        self._target = target

    def write(self, data: bytes) -> None:
        self._target.feed_data(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._target.feed_eof()


class FakeDaemon:
    """The daemon's end of a plugin's stdio, driven by hand."""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.to_plugin = asyncio.StreamReader()
        self.from_plugin = asyncio.StreamReader()
        self._next_id = 1
        self.incoming: list[dict] = []  # requests and notifications the plugin sent
        self.source_handler = None
        self._task: asyncio.Task | None = None
        self._pump: asyncio.Task | None = None
        self._responses: dict[int, asyncio.Future] = {}

    async def start(self) -> None:
        self._task = asyncio.ensure_future(
            self.plugin.serve(self.to_plugin, _PipeWriter(self.from_plugin))
        )
        self._pump = asyncio.ensure_future(self._read())
        await asyncio.sleep(0)

    async def _read(self) -> None:
        while True:
            line = await self.from_plugin.readline()
            if not line:
                return
            message = json.loads(line)
            if "method" in message:
                self.incoming.append(message)
                if "id" in message:
                    await self._answer(message)
            else:
                future = self._responses.get(message["id"])
                if future is not None and not future.done():
                    future.set_result(message)

    async def _answer(self, message: dict) -> None:
        handler = self.source_handler
        if handler is None:
            reply = {"jsonrpc": "2.0", "id": message["id"], "error": {
                "code": -32601, "message": "method_not_found",
                "data": {"code": "method_not_found", "detail": "", "retryable": False}}}
        else:
            reply = {"jsonrpc": "2.0", "id": message["id"], **handler(message)}
        self.send_raw(reply)

    def send_raw(self, message: dict) -> None:
        self.to_plugin.feed_data(json.dumps(message).encode() + b"\n")

    async def call(self, method: str, params: dict) -> dict:
        """Send a request and return the whole response message."""
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        self.send_raw({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await asyncio.wait_for(future, 5)

    async def result(self, method: str, params: dict):
        message = await self.call(method, params)
        assert "error" not in message, message
        return message["result"]

    async def error(self, method: str, params: dict) -> dict:
        message = await self.call(method, params)
        assert "error" in message, message
        return message["error"]["data"]

    def notify(self, method: str, params: dict) -> None:
        self.send_raw({"jsonrpc": "2.0", "method": method, "params": params})

    async def stop(self) -> None:
        self.to_plugin.feed_eof()
        if self._task is not None:
            await asyncio.wait_for(self._task, 5)
        if self._pump is not None:
            self._pump.cancel()

    async def initialize(self, purpose: str = "run") -> dict:
        return await self.result("initialize", {
            "protocol_version": "1.0.0", "purpose": purpose, "mode": "local",
            "daemon": {"name": "privacyfence", "version": "9.9.9"},
            "plugin": {"name": self.plugin.name, "manifest_version": self.plugin.version},
            "data_dir": str(self.data_dir),
            "principals": [PRINCIPAL],
            "limits": {"max_line_bytes": 16 * 1024 * 1024, "max_in_flight": 16, "inline_result_bytes": 100000},
        })

    data_dir = Path("/tmp/pf-sdk-test")


PRINCIPAL = {"id": "local", "display_name": "Local user", "storage_dir": "/tmp/pf-sdk-test/principals/local"}


@pytest.fixture
def principal() -> dict:
    return dict(PRINCIPAL)


@pytest.fixture
def make_daemon(tmp_path):
    started: list[FakeDaemon] = []

    async def make(plugin) -> FakeDaemon:
        daemon = FakeDaemon(plugin)
        daemon.data_dir = tmp_path
        await daemon.start()
        started.append(daemon)
        return daemon

    yield make
