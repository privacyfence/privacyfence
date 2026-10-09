"""JSON-RPC 2.0 peer over a pair of asyncio streams.

One ``RpcPeer`` sits on each end of a plugin's stdin/stdout. Framing is one JSON object per line,
capped at ``MAX_LINE_BYTES``. Each side numbers its own requests, so the two id spaces never meet.
Everything read from the other end is untrusted: lines that are not valid JSON-RPC are counted, and
``INVALID_LINES_LIMIT`` of them in a row close the peer with the reason ``"invalid_output"``. A line
over the cap is discarded up to its newline and counted once, however it was split on the way.
Handler failures never leak their text to the other side, only ``RpcError`` details travel, and the
log records only the exception's type, since its message can carry request data. Incoming requests
and notifications share the ``MAX_IN_FLIGHT`` cap: a request over it is refused, a notification over
it is dropped and counted in ``dropped_notifications``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from privacyfence.plugins.constants import (
    ERROR_CODES,
    INVALID_LINES_LIMIT,
    MAX_IN_FLIGHT,
    MAX_LINE_BYTES,
    TIMEOUT_SECONDS,
)
from privacyfence.plugins.protocol import RpcError

logger = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[Any]]
NotificationHandler = Callable[[dict], Awaitable[None]]

_OVERSIZE = b"\x00oversize"
_CODE_NAMES = {number: name for name, number in ERROR_CODES.items()}


def _is_id(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


def _error_from_wire(error: Any) -> RpcError:
    """Map an error object the other side sent onto an ``RpcError``; never raises."""
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
        name,
        detail if isinstance(detail, str) else "",
        retryable=data.get("retryable") is True,
        extra=extra,
    )


class RpcPeer:
    """Both ends of a JSON-RPC conversation: sends requests and notifications, serves handlers."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        handlers: dict[str, Handler],
        notification_handlers: dict[str, NotificationHandler] | None = None,
        on_close: Callable[[str], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._handlers = handlers
        self._notification_handlers = notification_handlers or {}
        self._on_close = on_close
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._out_slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._in_flight = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._invalid_streak = 0
        self._closed = False
        self.dropped_notifications = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, method: str, params: dict, *, timeout: float | None = None) -> Any:
        if timeout is None:
            timeout = TIMEOUT_SECONDS.get(method)
        async with self._out_slots:
            if self._closed:
                raise RpcError("internal_error", "peer closed")
            self._next_id += 1
            request_id = self._next_id
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            try:
                await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                return await asyncio.wait_for(future, timeout)
            except TimeoutError:
                raise RpcError("timeout", f"{method} timed out") from None
            finally:
                self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict) -> None:
        if self._closed:
            raise RpcError("internal_error", "peer closed")
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        self._shutdown("closed")
        task = self._reader_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for handler_task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await handler_task
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    def _shutdown(self, reason: str) -> None:
        """Mark closed exactly once: fail waiting requests, stop handlers, close the stream."""
        if self._closed:
            return
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RpcError("internal_error", "peer closed"))
        for task in self._tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            self._writer.close()
        if self._on_close is not None:
            try:
                self._on_close(reason)
            except Exception:
                logger.warning("on_close callback failed", exc_info=True)

    async def _send(self, message: dict) -> None:
        try:
            line = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        except (TypeError, ValueError):
            raise RpcError("internal_error", "message is not JSON") from None
        if len(line) > MAX_LINE_BYTES:
            raise RpcError("payload_too_large", "message exceeds the line limit")
        async with self._write_lock:
            try:
                self._writer.write(line)
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                self._shutdown("write_failed")
                raise RpcError("internal_error", "peer closed") from exc

    async def _send_quiet(self, message: dict) -> None:
        if self._closed:
            return
        try:
            await self._send(message)
        except RpcError as exc:
            if "result" in message:
                # Too big, or not JSON: the caller gets an error instead of waiting for a timeout.
                await self._send_quiet(self._error_message(message["id"], exc))
            else:
                logger.debug("could not send an error response", exc_info=True)

    async def _read_line(self) -> bytes | None:
        """The next line, ``_OVERSIZE`` for one over the cap, ``None`` at end of stream.

        A line over the StreamReader's own limit is dropped piece by piece up to its newline, so
        it is one oversize line however many reads it took to arrive.
        """
        oversize = False
        while True:
            try:
                line = await self._reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                line = exc.partial  # the stream ended; whatever came before that is the last line
                if not line and not oversize:
                    return None
            except asyncio.LimitOverrunError as exc:
                oversize = True
                await self._reader.readexactly(exc.consumed)
                continue
            except (ConnectionError, OSError):
                return None
            if oversize or len(line) > MAX_LINE_BYTES:
                return _OVERSIZE
            return line

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._read_line()
                if line is None:
                    self._shutdown("eof")
                    return
                if not await self._handle_line(line):
                    self._invalid_streak += 1
                    if self._invalid_streak >= INVALID_LINES_LIMIT:
                        self._shutdown("invalid_output")
                        return
                else:
                    self._invalid_streak = 0
        except Exception:
            logger.warning("rpc reader failed", exc_info=True)
            self._shutdown("reader_failed")

    async def _handle_line(self, line: bytes) -> bool:
        """Process one line; ``False`` when it was not valid JSON-RPC."""
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
            if "id" in message:
                if not _is_id(message_id):
                    await self._send_quiet(self._error_message(None, RpcError("invalid_request", "invalid id")))
                    return False
                self._dispatch_request(message_id, method, message.get("params"))
            else:
                self._dispatch_notification(method, message.get("params"))
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
        future = self._pending.get(message_id) if isinstance(message_id, int) else None
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(_error_from_wire(message["error"]))
        else:
            future.set_result(message["result"])

    def _dispatch_request(self, message_id: Any, method: str, params: Any) -> None:
        if self._in_flight >= MAX_IN_FLIGHT:
            self._spawn(
                self._send_quiet(
                    self._error_message(message_id, RpcError("invalid_request", "too many requests in flight"))
                )
            )
            return
        self._in_flight += 1
        self._spawn(self._serve(message_id, method, params))

    async def _serve(self, message_id: Any, method: str, params: Any) -> None:
        try:
            handler = self._handlers.get(method)
            if handler is None:
                raise RpcError("method_not_found", method)
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise RpcError("invalid_params", "params must be an object")
            try:
                result = await handler(params)
            except RpcError:
                raise
            except Exception as exc:
                logger.warning("handler for %s failed: %s", method, type(exc).__name__)
                raise RpcError("internal_error", "handler failed") from None
            response: dict = {"jsonrpc": "2.0", "id": message_id, "result": result}
        except RpcError as exc:
            response = self._error_message(message_id, exc)
        finally:
            self._in_flight -= 1
        await self._send_quiet(response)

    def _dispatch_notification(self, method: str, params: Any) -> None:
        handler = self._notification_handlers.get(method)
        if handler is None:
            return
        if self._in_flight >= MAX_IN_FLIGHT:
            self.dropped_notifications += 1
            logger.debug("dropped a %s notification: too many messages in flight", method)
            return
        self._in_flight += 1
        self._spawn(self._notified(handler, method, params if isinstance(params, dict) else {}))

    async def _notified(self, handler: NotificationHandler, method: str, params: dict) -> None:
        try:
            await handler(params)
        except Exception as exc:
            logger.warning("notification handler for %s failed: %s", method, type(exc).__name__)
        finally:
            self._in_flight -= 1
