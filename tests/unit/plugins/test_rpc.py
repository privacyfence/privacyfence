"""RpcPeer over an in-memory socket pair: framing, id spaces, caps, timeouts and error mapping."""
from __future__ import annotations

import asyncio
import json
import logging
import socket

import pytest

from privacyfence.plugins import rpc
from privacyfence.plugins.constants import ERROR_CODES, INVALID_LINES_LIMIT, MAX_IN_FLIGHT
from privacyfence.plugins.protocol import RpcError
from privacyfence.plugins.rpc import RpcPeer

pytestmark = pytest.mark.unit


async def _streams(limit: int = 2**16):
    a, b = socket.socketpair()
    ra, wa = await asyncio.open_connection(sock=a, limit=limit)
    rb, wb = await asyncio.open_connection(sock=b, limit=limit)
    return (ra, wa), (rb, wb)


@pytest.fixture
async def pair():
    """(left, right) peers with no handlers; tests set handlers via ``make``."""
    opened: list[RpcPeer] = []

    async def make(left_handlers=None, right_handlers=None, *, left_notifs=None, right_notifs=None,
                   left_close=None, right_close=None, limit=2**16):
        (ra, wa), (rb, wb) = await _streams(limit)
        left = RpcPeer(ra, wa, handlers=left_handlers or {}, notification_handlers=left_notifs, on_close=left_close)
        right = RpcPeer(rb, wb, handlers=right_handlers or {}, notification_handlers=right_notifs,
                        on_close=right_close)
        await left.start()
        await right.start()
        opened.extend([left, right])
        return left, right

    yield make
    for peer in opened:
        await peer.close()


async def _raw(limit: int = 2**16):
    """A peer plus the raw other end of its socket."""
    (ra, wa), (rb, wb) = await _streams(limit)
    closes: list[str] = []
    peer = RpcPeer(ra, wa, handlers={}, on_close=closes.append)
    await peer.start()
    return peer, rb, wb, closes


class TestRoundTrip:
    async def test_request_both_directions(self, pair):
        async def echo(params):
            return {"echo": params}

        left, right = await pair({"ping": echo}, {"pong": echo})
        assert await left.request("pong", {"a": 1}) == {"echo": {"a": 1}}
        assert await right.request("ping", {"b": 2}) == {"echo": {"b": 2}}

    async def test_independent_id_spaces(self, pair):
        ids: list = []
        peer, rb, wb, _ = await _raw()
        task = asyncio.create_task(peer.request("x", {}, timeout=2))
        sent = json.loads(await rb.readline())
        ids.append(sent["id"])
        # The other side's own request may reuse the same id without being mistaken for a response.
        wb.write(json.dumps({"jsonrpc": "2.0", "id": sent["id"], "method": "unknown", "params": {}}).encode() + b"\n")
        reply = json.loads(await rb.readline())
        assert reply["error"]["data"]["code"] == "method_not_found" and reply["id"] == ids[0]
        wb.write(json.dumps({"jsonrpc": "2.0", "id": sent["id"], "result": 7}).encode() + b"\n")
        assert await task == 7
        await peer.close()

    async def test_none_params_are_an_empty_object(self, pair):
        seen = []

        async def handler(params):
            seen.append(params)

        peer, rb, wb, _ = await _raw()
        peer._handlers["m"] = handler
        wb.write(b'{"jsonrpc":"2.0","id":1,"method":"m"}\n')
        assert json.loads(await rb.readline())["result"] is None
        assert seen == [{}]
        await peer.close()

    async def test_non_object_params_rejected(self, pair):
        peer, rb, wb, _ = await _raw()
        peer._handlers["m"] = lambda params: None
        wb.write(b'{"jsonrpc":"2.0","id":1,"method":"m","params":[1]}\n')
        assert json.loads(await rb.readline())["error"]["data"]["code"] == "invalid_params"
        await peer.close()

    async def test_oversize_outgoing_request_refused(self, monkeypatch, pair):
        monkeypatch.setattr(rpc, "MAX_LINE_BYTES", 100)
        left, _ = await pair()
        with pytest.raises(RpcError) as exc:
            await left.request("m", {"blob": "x" * 200})
        assert exc.value.code == "payload_too_large"
        assert not left.closed


class TestErrors:
    async def test_rpc_error_maps_both_ways(self, pair):
        async def handler(params):
            raise RpcError("unknown_tool", "no such tool", retryable=True, extra={"tool": "t"})

        left, right = await pair({}, {"m": handler})
        with pytest.raises(RpcError) as exc:
            await left.request("m", {})
        assert exc.value.code == "unknown_tool"
        assert exc.value.detail == "no such tool"
        assert exc.value.retryable is True
        assert exc.value.extra == {"tool": "t"}

    async def test_other_exception_is_internal_error_and_logged(self, pair, caplog):
        async def handler(params):
            raise ValueError("secret detail")

        left, _ = await pair({}, {"m": handler})
        with caplog.at_level(logging.WARNING, logger=rpc.logger.name):
            with pytest.raises(RpcError) as exc:
                await left.request("m", {})
        assert exc.value.code == "internal_error"
        assert exc.value.detail == "handler failed"
        assert "secret detail" not in exc.value.detail
        assert "handler for m failed: ValueError" in caplog.text
        assert "secret detail" not in caplog.text
        assert not any(r.exc_info for r in caplog.records)

    async def test_unknown_method(self, pair):
        left, _ = await pair()
        with pytest.raises(RpcError) as exc:
            await left.request("nope", {})
        assert exc.value.code == "method_not_found"

    @pytest.mark.parametrize(
        "error, code",
        [
            ("junk", "internal_error"),
            ({"code": ERROR_CODES["timeout"], "message": "x"}, "timeout"),
            ({"code": 12345, "message": "x", "data": "no"}, "internal_error"),
            ({"code": -32601, "data": {"code": "made_up", "detail": 5}}, "method_not_found"),
        ],
    )
    async def test_error_object_variants(self, error, code):
        peer, rb, wb, _ = await _raw()
        task = asyncio.create_task(peer.request("m", {}, timeout=2))
        sent = json.loads(await rb.readline())
        wb.write(json.dumps({"jsonrpc": "2.0", "id": sent["id"], "error": error}).encode() + b"\n")
        with pytest.raises(RpcError) as exc:
            await task
        assert exc.value.code == code
        await peer.close()

    async def test_non_json_result_is_internal_error(self, pair):
        async def handler(params):
            return {1, 2}

        left, _ = await pair({}, {"m": handler})
        with pytest.raises(RpcError) as exc:
            await left.request("m", {})
        assert exc.value.code == "internal_error"


class TestTimeout:
    async def test_explicit_timeout(self, pair):
        async def slow(params):
            await asyncio.sleep(5)

        left, _ = await pair({}, {"m": slow})
        with pytest.raises(RpcError) as exc:
            await left.request("m", {}, timeout=0.05)
        assert exc.value.code == "timeout"
        assert left._pending == {}

    async def test_default_comes_from_table(self, monkeypatch, pair):
        monkeypatch.setitem(rpc.TIMEOUT_SECONDS, "slow.method", 0.05)

        async def slow(params):
            await asyncio.sleep(5)

        left, _ = await pair({}, {"slow.method": slow})
        with pytest.raises(RpcError) as exc:
            await left.request("slow.method", {})
        assert exc.value.code == "timeout"


class TestNotifications:
    async def test_delivered(self, pair):
        got = asyncio.Event()
        seen = []

        async def on_note(params):
            seen.append(params)
            got.set()

        left, right = await pair({}, {}, right_notifs={"hello": on_note})
        await left.notify("hello", {"x": 1})
        await asyncio.wait_for(got.wait(), 2)
        assert seen == [{"x": 1}]

    async def test_unknown_ignored_and_no_response(self):
        peer, rb, wb, closes = await _raw()
        wb.write(b'{"jsonrpc":"2.0","method":"mystery","params":{}}\n')
        wb.write(b'{"jsonrpc":"2.0","method":"mystery","params":5}\n')
        wb.write(b'{"jsonrpc":"2.0","id":9,"result":1}\n')  # a response nobody asked for
        wb.write(b'{"jsonrpc":"2.0","id":1,"method":"m","params":{}}\n')
        reply = json.loads(await asyncio.wait_for(rb.readline(), 2))
        assert reply["id"] == 1 and reply["error"]["data"]["code"] == "method_not_found"
        assert closes == []
        await peer.close()

    async def test_handler_failure_is_logged_not_raised(self, pair, caplog):
        done = asyncio.Event()

        async def boom(params):
            done.set()
            raise ValueError("x")

        left, right = await pair({}, {}, right_notifs={"n": boom})
        with caplog.at_level(logging.WARNING, logger=rpc.logger.name):
            await left.notify("n", {})
            await asyncio.wait_for(done.wait(), 2)
            await asyncio.sleep(0.05)
        assert not right.closed
        assert any("notification handler" in r.message for r in caplog.records)

    async def test_handler_failure_logs_only_the_type(self, pair, caplog):
        done = asyncio.Event()

        async def boom(params):
            done.set()
            raise ValueError(params["secret"])

        left, right = await pair({}, {}, right_notifs={"n": boom})
        with caplog.at_level(logging.DEBUG, logger=rpc.logger.name):
            await left.notify("n", {"secret": "hunter2"})
            await asyncio.wait_for(done.wait(), 2)
            await asyncio.sleep(0.05)
        assert "notification handler for n failed: ValueError" in caplog.text
        assert "hunter2" not in caplog.text
        assert not any(r.exc_info for r in caplog.records)

    async def test_over_the_in_flight_cap_dropped_and_counted(self):
        peer, rb, wb, closes = await _raw()
        release = asyncio.Event()
        seen: list[dict] = []

        async def hold(params):
            seen.append(params)
            await release.wait()

        peer._notification_handlers["n"] = hold
        for i in range(MAX_IN_FLIGHT + 2):
            wb.write(json.dumps({"jsonrpc": "2.0", "method": "n", "params": {"i": i}}).encode() + b"\n")
        await asyncio.wait_for(_until(lambda: peer.dropped_notifications == 2), 2)
        assert len(seen) == MAX_IN_FLIGHT
        # Notifications hold slots the same way requests do.
        peer._handlers["m"] = hold
        wb.write(b'{"jsonrpc":"2.0","id":1,"method":"m","params":{}}\n')
        reply = json.loads(await asyncio.wait_for(rb.readline(), 2))
        assert reply["error"]["data"]["detail"] == "too many requests in flight"
        release.set()
        await asyncio.wait_for(_until(lambda: peer._in_flight == 0), 2)
        wb.write(b'{"jsonrpc":"2.0","method":"n","params":{"i":"after"}}\n')
        await asyncio.wait_for(_until(lambda: {"i": "after"} in seen), 2)
        assert peer.dropped_notifications == 2 and closes == []
        await peer.close()


class TestBatch:
    async def test_batch_gets_invalid_request_and_is_not_processed(self):
        peer, rb, wb, closes = await _raw()
        called = []

        async def handler(params):
            called.append(params)

        peer._handlers["m"] = handler
        wb.write(b'[{"jsonrpc":"2.0","id":1,"method":"m","params":{}}]\n')
        reply = json.loads(await asyncio.wait_for(rb.readline(), 2))
        assert reply["id"] is None and reply["error"]["data"]["code"] == "invalid_request"
        assert called == []
        assert closes == []
        await peer.close()


class TestInvalidLines:
    async def test_three_junk_lines_close(self):
        peer, rb, wb, closes = await _raw()
        for _ in range(INVALID_LINES_LIMIT):
            wb.write(b"not json\n")
        await asyncio.wait_for(_until(lambda: peer.closed), 2)
        assert closes == ["invalid_output"]

    async def test_valid_line_resets_the_streak(self):
        peer, rb, wb, closes = await _raw()
        for _ in range(INVALID_LINES_LIMIT - 1):
            wb.write(b"not json\n")
        wb.write(b'{"jsonrpc":"2.0","method":"ok"}\n')
        for _ in range(INVALID_LINES_LIMIT - 1):
            wb.write(b"[1]\n")
        await asyncio.sleep(0.1)
        assert not peer.closed
        await peer.close()

    @pytest.mark.parametrize(
        "line",
        [b'"just a string"', b'{"jsonrpc":"1.0","id":1,"method":"m"}', b'{"jsonrpc":"2.0","id":true,"method":"m"}',
         b'{"jsonrpc":"2.0","id":1}', b'{"jsonrpc":"2.0","id":1,"result":1,"error":{}}'],
    )
    async def test_non_rpc_objects_are_invalid(self, line):
        peer, rb, wb, closes = await _raw()
        for _ in range(INVALID_LINES_LIMIT):
            wb.write(line + b"\n")
        await asyncio.wait_for(_until(lambda: peer.closed), 2)
        assert closes == ["invalid_output"]

    async def test_over_cap_line_counts(self, monkeypatch):
        peer, rb, wb, closes = await _raw(limit=64)
        for _ in range(INVALID_LINES_LIMIT):
            wb.write(b"x" * 200 + b"\n")
        await asyncio.wait_for(_until(lambda: peer.closed), 2)
        assert closes == ["invalid_output"]

    async def test_oversize_line_in_pieces_counts_once(self):
        peer, rb, wb, closes = await _raw(limit=64)
        for _ in range(INVALID_LINES_LIMIT - 1):
            # Each piece is over the reader's limit and arrives on its own.
            for piece in (b"x" * 100, b"x" * 100, b"x" * 100, b"x" * 10 + b"\n"):
                wb.write(piece)
                await wb.drain()
                await asyncio.sleep(0.02)
        await asyncio.wait_for(_until(lambda: peer._invalid_streak == INVALID_LINES_LIMIT - 1), 2)
        await asyncio.sleep(0.05)
        assert not peer.closed
        wb.write(b'{"jsonrpc":"2.0","method":"ok"}\n')
        await asyncio.wait_for(_until(lambda: peer._invalid_streak == 0), 2)
        assert closes == []
        await peer.close()

    async def test_oversize_line_then_end_of_stream(self):
        peer, rb, wb, closes = await _raw(limit=64)
        wb.write(b"x" * 200)
        wb.close()
        await asyncio.wait_for(_until(lambda: peer.closed), 2)
        assert closes == ["eof"]

    async def test_explicit_length_check(self, monkeypatch):
        monkeypatch.setattr(rpc, "MAX_LINE_BYTES", 40)
        peer, rb, wb, closes = await _raw(limit=2**16)
        line = b'{"jsonrpc":"2.0","method":"' + b"m" * 60 + b'"}\n'
        for _ in range(INVALID_LINES_LIMIT):
            wb.write(line)
        await asyncio.wait_for(_until(lambda: peer.closed), 2)
        assert closes == ["invalid_output"]

    async def test_pending_requests_fail_when_peer_closes(self):
        peer, rb, wb, closes = await _raw()
        task = asyncio.create_task(peer.request("m", {}, timeout=5))
        await rb.readline()
        for _ in range(INVALID_LINES_LIMIT):
            wb.write(b"junk\n")
        with pytest.raises(RpcError) as exc:
            await task
        assert exc.value.code == "internal_error"
        with pytest.raises(RpcError):
            await peer.request("m", {})
        with pytest.raises(RpcError):
            await peer.notify("m", {})


class TestLifecycle:
    async def test_eof_closes_with_reason(self):
        peer, rb, wb, closes = await _raw()
        wb.close()
        await asyncio.wait_for(_until(lambda: peer.closed), 2)
        assert closes == ["eof"]

    async def test_close_is_idempotent_and_reports_once(self):
        peer, rb, wb, closes = await _raw()
        await peer.close()
        await peer.close()
        assert closes == ["closed"]

    async def test_failing_on_close_is_swallowed(self, caplog):
        (ra, wa), _other = await _streams()
        peer = RpcPeer(ra, wa, handlers={}, on_close=lambda reason: 1 / 0)
        await peer.start()
        with caplog.at_level(logging.WARNING, logger=rpc.logger.name):
            await peer.close()
        assert peer.closed

    async def test_write_after_remote_gone_closes(self):
        (ra, wa), (rb, wb) = await _streams()
        closes: list[str] = []
        peer = RpcPeer(ra, wa, handlers={}, on_close=closes.append)
        await peer.start()
        wb.close()
        await wb.wait_closed()
        await asyncio.wait_for(_until(lambda: peer.closed), 2)
        with pytest.raises(RpcError):
            await peer.notify("m", {})

    async def test_close_cancels_running_handlers(self, pair):
        started = asyncio.Event()
        cancelled = []

        async def hang(params):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        left, right = await pair({}, {"m": hang})
        task = asyncio.create_task(left.request("m", {}, timeout=5))
        await asyncio.wait_for(started.wait(), 2)
        await right.close()
        assert cancelled == [True]
        with pytest.raises(RpcError):
            await task


class TestBrokenStreams:
    async def test_oversize_result_becomes_error_response(self, monkeypatch, pair):
        async def big(params):
            return "x" * 500

        left, _ = await pair({}, {"m": big})
        monkeypatch.setattr(rpc, "MAX_LINE_BYTES", 200)
        with pytest.raises(RpcError) as exc:
            await left.request("m", {})
        assert exc.value.code == "payload_too_large"

    async def test_response_after_close_is_dropped(self):
        peer, rb, wb, closes = await _raw()
        started = asyncio.Event()

        async def stubborn(params):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return "late"

        peer._handlers["m"] = stubborn
        wb.write(b'{"jsonrpc":"2.0","id":1,"method":"m","params":{}}\n')
        await asyncio.wait_for(started.wait(), 2)
        await peer.close()
        assert await rb.read() == b""

    async def test_reset_while_reading_closes_as_eof(self):
        class Reader:
            async def readuntil(self, separator=b"\n"):
                raise ConnectionResetError

        (_, wa), _other = await _streams()
        closes: list[str] = []
        peer = RpcPeer(Reader(), wa, handlers={}, on_close=closes.append)  # type: ignore[arg-type]
        await peer.start()
        await asyncio.wait_for(_until(lambda: peer.closed), 2)
        assert closes == ["eof"]

    async def test_reader_crash_closes(self, caplog):
        class Reader:
            async def readuntil(self, separator=b"\n"):
                raise RuntimeError("boom")

        (_, wa), _other = await _streams()
        closes: list[str] = []
        peer = RpcPeer(Reader(), wa, handlers={}, on_close=closes.append)  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING, logger=rpc.logger.name):
            await peer.start()
            await asyncio.wait_for(_until(lambda: peer.closed), 2)
        assert closes == ["reader_failed"]

    async def test_write_failure_closes(self):
        class Writer:
            def write(self, data):
                raise BrokenPipeError

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        (ra, _), _other = await _streams()
        closes: list[str] = []
        peer = RpcPeer(ra, Writer(), handlers={}, on_close=closes.append)  # type: ignore[arg-type]
        with pytest.raises(RpcError):
            await peer.notify("m", {})
        assert closes == ["write_failed"]

    async def test_non_json_params_raise(self, pair):
        left, _ = await pair()
        with pytest.raises(RpcError) as exc:
            await left.request("m", {"x": {1, 2}})
        assert exc.value.code == "internal_error"


class TestInFlight:
    async def test_incoming_cap_answers_invalid_request(self):
        peer, rb, wb, closes = await _raw()
        release = asyncio.Event()

        async def hold(params):
            await release.wait()
            return "done"

        peer._handlers["m"] = hold
        for i in range(MAX_IN_FLIGHT + 1):
            wb.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "m", "params": {}}).encode() + b"\n")
        reply = json.loads(await asyncio.wait_for(rb.readline(), 2))
        assert reply["id"] == MAX_IN_FLIGHT
        assert reply["error"]["data"]["code"] == "invalid_request"
        assert reply["error"]["data"]["detail"] == "too many requests in flight"
        release.set()
        results = [json.loads(await asyncio.wait_for(rb.readline(), 2)) for _ in range(MAX_IN_FLIGHT)]
        assert all(r["result"] == "done" for r in results)
        await peer.close()

    async def test_outgoing_cap_waits(self):
        peer, rb, wb, closes = await _raw()
        tasks = [asyncio.create_task(peer.request("m", {}, timeout=5)) for _ in range(MAX_IN_FLIGHT + 2)]
        sent = [json.loads(await asyncio.wait_for(rb.readline(), 2)) for _ in range(MAX_IN_FLIGHT)]
        await asyncio.sleep(0.1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(rb.readline(), 0.1)
        wb.write(json.dumps({"jsonrpc": "2.0", "id": sent[0]["id"], "result": 0}).encode() + b"\n")
        extra = json.loads(await asyncio.wait_for(rb.readline(), 2))
        assert extra["id"] == MAX_IN_FLIGHT + 1
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await peer.close()

    async def test_request_waiting_for_slot_fails_when_closed(self):
        peer, rb, wb, closes = await _raw()
        tasks = [asyncio.create_task(peer.request("m", {}, timeout=5)) for _ in range(MAX_IN_FLIGHT + 1)]
        for _ in range(MAX_IN_FLIGHT):
            await rb.readline()
        await peer.close()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(r, RpcError) for r in results)


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0.01)
