"""JSON-RPC 2.0 peer over a pair of asyncio streams, plus the stdio transport.

Framing is one JSON object per line, UTF-8, at most ``max_line_bytes`` including the newline. Each
side numbers its own requests. The numeric limits are passed in by the caller, so this module holds
none of its own. Handler failures never leak their text to the other side: only ``RpcError``
details travel.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

logger = logging.getLogger("privacyfence_plugin_sdk")

ERROR_CODES: dict[str, int] = {
    "parse_error": -32700, "invalid_request": -32600, "method_not_found": -32601,
    "invalid_params": -32602, "internal_error": -32603,
    "operation_not_allowed": -32001, "connector_unavailable": -32002, "unknown_principal": -32003,
    "payload_too_large": -32004, "upstream_error": -32005, "org_only_field": -32006,
    "confirmation_refused": -32007, "unknown_tool": -32008, "invalid_blocks": -32009,
    "version_mismatch": -32010, "unknown_call": -32011, "digest_mismatch": -32012,
    "timeout": -32013, "introspection_only": -32014,
}
_CODE_NAMES = {number: name for name, number in ERROR_CODES.items()}

Handler = Callable[[dict], Awaitable[Any]]
NotificationHandler = Callable[[dict], Awaitable[None]]


class RpcError(Exception):
    """An error that travels over the wire as a JSON-RPC error object."""

    def __init__(self, code: str, detail: str = "", *, retryable: bool = False, extra: dict | None = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code if code in ERROR_CODES else "internal_error"
        self.detail = detail
        self.retryable = retryable
        self.extra = dict(extra or {})

    def to_error(self) -> dict:
        data = {**self.extra, "code": self.code, "detail": self.detail, "retryable": self.retryable}
        return {"code": ERROR_CODES[self.code], "message": self.code, "data": data}


def _error_from_wire(error: Any) -> RpcError:
    if not isinstance(error, dict):
        return RpcError("internal_error", "malformed error response")
    data = error.get("data")
    data = data if isinstance(data, dict) else {}
    name = data.get("code")
    if not isinstance(name, str) or name not in ERROR_CODES:
        number = error.get("code")
        name = _CODE_NAMES.get(number, "internal_error") if isinstance(number, int) else "internal_error"
    detail = data.get("detail")
    extra = {k: v for k, v in data.items() if k not in ("code", "detail", "retryable")}
    return RpcError(
        name, detail if isinstance(detail, str) else "", retryable=data.get("retryable") is True, extra=extra
    )


def _is_id(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


class _Reader(Protocol):
    async def readline(self) -> bytes: ...


class _Writer(Protocol):
    def write(self, data: bytes) -> Any: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


_OVERSIZE = b"\x00oversize"


class Peer:
    """Both ends of a JSON-RPC conversation: sends requests and notifications, serves handlers."""

    def __init__(
        self,
        reader: _Reader,
        writer: _Writer,
        *,
        handlers: dict[str, Handler],
        notification_handlers: dict[str, NotificationHandler] | None = None,
        on_close: Callable[[str], None] | None = None,
        max_line_bytes: int,
        max_in_flight: int,
        invalid_lines_limit: int,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._handlers = handlers
        self._notification_handlers = notification_handlers or {}
        self._on_close = on_close
        self._max_line_bytes = max_line_bytes
        self._max_in_flight = max_in_flight
        self._invalid_limit = invalid_lines_limit
        self._ids = itertools.count(1)
        self._pending: dict[Any, asyncio.Future] = {}
        self._out_sem = asyncio.Semaphore(max_in_flight)
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._serving = 0
        self._invalid_streak = 0
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._closed_event = asyncio.Event()
        self.close_reason: str | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, method: str, params: dict, *, timeout: float | None = None) -> Any:
        if self._closed:
            raise RpcError("internal_error", "the daemon closed the connection")
        async with self._out_sem:
            request_id = next(self._ids)
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            try:
                await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                try:
                    return await asyncio.wait_for(future, timeout)
                except asyncio.TimeoutError:
                    raise RpcError("timeout", f"no answer to {method}") from None
            finally:
                self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict) -> None:
        if self._closed:
            raise RpcError("internal_error", "the daemon closed the connection")
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        self._shutdown("closed")
        current = asyncio.current_task()
        tasks = [t for t in (self._reader_task, *self._tasks) if t is not None and t is not current]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            self._writer.close()

    def _shutdown(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_reason = reason
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RpcError("internal_error", "the daemon closed the connection"))
        self._closed_event.set()
        if self._on_close is not None:
            try:
                self._on_close(reason)
            except Exception:
                logger.warning("on_close callback failed", exc_info=True)

    async def _send(self, message: dict) -> None:
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._send_lock:
            self._writer.write(line)
            await self._writer.drain()

    async def _send_quiet(self, message: dict) -> None:
        try:
            await self._send(message)
        except Exception:
            self._shutdown("write_failed")

    async def _read_line(self) -> bytes | None:
        try:
            line = await self._reader.readline()
        except ValueError:
            return _OVERSIZE
        except (ConnectionError, OSError):
            return None
        if not line:
            return None
        if len(line) > self._max_line_bytes:
            return _OVERSIZE
        return line

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._read_line()
                if line is None:
                    self._shutdown("eof")
                    return
                if await self._handle_line(line):
                    self._invalid_streak = 0
                    continue
                self._invalid_streak += 1
                if self._invalid_streak >= self._invalid_limit:
                    self._shutdown("invalid_output")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("rpc reader failed", exc_info=True)
            self._shutdown("reader_failed")

    async def _handle_line(self, line: bytes) -> bool:
        if line is _OVERSIZE:
            return False
        try:
            message = json.loads(line)
        except ValueError:
            await self._send_quiet(self._error_message(None, RpcError("parse_error", "invalid JSON")))
            return False
        if isinstance(message, list):
            await self._send_quiet(self._error_message(None, RpcError("invalid_request", "batches are not supported")))
            return False
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            await self._send_quiet(self._error_message(None, RpcError("invalid_request", "not a JSON-RPC object")))
            return False
        method = message.get("method")
        message_id = message.get("id")
        if isinstance(method, str):
            if "id" not in message:
                self._dispatch_notification(method, message.get("params"))
                return True
            if not _is_id(message_id):
                await self._send_quiet(self._error_message(None, RpcError("invalid_request", "invalid id")))
                return False
            await self._dispatch_request(message_id, method, message.get("params"))
            return True
        if _is_id(message_id) and ("result" in message) != ("error" in message):
            self._dispatch_response(message_id, message)
            return True
        await self._send_quiet(self._error_message(None, RpcError("invalid_request", "not a request or response")))
        return False

    @staticmethod
    def _error_message(message_id: Any, error: RpcError) -> dict:
        return {"jsonrpc": "2.0", "id": message_id, "error": error.to_error()}

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _dispatch_response(self, message_id: Any, message: dict) -> None:
        future = self._pending.get(message_id)
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(_error_from_wire(message["error"]))
        else:
            future.set_result(message["result"])

    async def _dispatch_request(self, message_id: Any, method: str, params: Any) -> None:
        if self._serving >= self._max_in_flight:
            await self._send_quiet(
                self._error_message(message_id, RpcError("invalid_request", "too many requests in flight"))
            )
            return
        self._serving += 1
        self._spawn(self._serve(message_id, method, params))

    async def _serve(self, message_id: Any, method: str, params: Any) -> None:
        try:
            handler = self._handlers.get(method)
            if handler is None:
                raise RpcError("method_not_found", f"unknown method {method}")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise RpcError("invalid_params", "params must be an object")
            try:
                result = await handler(params)
            except RpcError:
                raise
            except Exception:
                logger.warning("handler for %s failed", method, exc_info=True)
                raise RpcError("internal_error", "handler failed") from None
            await self._send_quiet({"jsonrpc": "2.0", "id": message_id, "result": result})
        except RpcError as exc:
            await self._send_quiet(self._error_message(message_id, exc))
        except Exception:
            logger.warning("could not answer %s", method, exc_info=True)
        finally:
            self._serving -= 1

    def _dispatch_notification(self, method: str, params: Any) -> None:
        handler = self._notification_handlers.get(method)
        if handler is None:
            return
        self._spawn(self._notified(handler, method, params if isinstance(params, dict) else {}))

    @staticmethod
    async def _notified(handler: NotificationHandler, method: str, params: dict) -> None:
        try:
            await handler(params)
        except Exception:
            logger.warning("notification handler for %s failed", method, exc_info=True)


class _StdoutWriter:
    """Writes protocol lines to the process's real stdout; flushes off the event loop."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        self._buffer += data

    async def drain(self) -> None:
        data, self._buffer = bytes(self._buffer), bytearray()
        if data:
            await asyncio.to_thread(self._flush, data)

    def _flush(self, data: bytes) -> None:
        self._stream.write(data)
        self._stream.flush()

    def close(self) -> None:
        return None


def _feed_from_thread(stdin: Any, loop: asyncio.AbstractEventLoop, reader: asyncio.StreamReader, limit: int) -> None:
    """Blocking stdin reader for platforms where asyncio cannot watch a pipe on stdin."""
    try:
        while True:
            line = stdin.readline(limit + 1)
            if not line:
                break
            loop.call_soon_threadsafe(reader.feed_data, line)
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(reader.feed_eof)


async def open_stdio(max_line_bytes: int) -> tuple[asyncio.StreamReader, _StdoutWriter]:
    """Reader and writer over the process's stdin and stdout.

    Anything else that prints to ``sys.stdout`` afterwards goes to stderr, so it cannot corrupt the
    protocol stream.
    """
    loop = asyncio.get_running_loop()
    out = sys.stdout.buffer
    sys.stdout = sys.stderr
    reader = asyncio.StreamReader(limit=max_line_bytes, loop=loop)
    connected = False
    if sys.platform != "win32":
        try:
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            connected = True
        except (ValueError, OSError, NotImplementedError):
            connected = False
    if not connected:
        threading.Thread(
            target=_feed_from_thread,
            args=(sys.stdin.buffer, loop, reader, max_line_bytes),
            name="privacyfence-stdin",
            daemon=True,
        ).start()
    return reader, _StdoutWriter(out)
