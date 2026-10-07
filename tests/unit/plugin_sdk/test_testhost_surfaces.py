"""The public test host's pages, confirmations, events, purge, shutdown and pytest fixture."""
from __future__ import annotations

import asyncio
import os
import subprocess  # nosec B404  # runs this interpreter on a fixed argv
import sys
import textwrap
from pathlib import Path

import pytest

from privacyfence_plugin_sdk._rpc import RpcError
from privacyfence_plugin_sdk import Bytes, Html, Plugin, Prepared, Text, blocks
from privacyfence_plugin_sdk.testing import PluginTestHost
from privacyfence_plugin_sdk.testing import _pages

pytestmark = pytest.mark.unit

SDK_SRC = Path(__file__).resolve().parents[3] / "plugin-sdk" / "src"

CSP = (
    "sandbox allow-scripts; default-src 'self' data: 'unsafe-inline'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def build_plugin() -> tuple[Plugin, dict]:
    seen: dict = {"pages": [], "events": [], "purges": [], "approval_ids": []}
    plugin = Plugin(name="surfaces", version="1.0.0")

    @plugin.page("/")
    async def home(ctx, request):
        seen["pages"].append((request.method, request.path, dict(request.query)))
        return Html("<h1>Home</h1>")

    @plugin.page("/data.json")
    async def data(ctx, request):
        return Bytes(b'{"a": 1}', content_type="application/json", headers={"Set-Cookie": "x=1", "X-Plugin": "y"})

    @plugin.page("/odd")
    async def odd(ctx, request):
        return Text("odd", status=418)

    @plugin.page("/binary")
    async def binary(ctx, request):
        return Bytes(b"\x00\x01", content_type="application/x-evil")

    @plugin.tool("ask", description="Ask a human.", gate="popup", params={"title": {"type": "string"}})
    async def ask(ctx, args):
        return Prepared(preview=[blocks.text("ask")])

    @ask.execute
    async def do_ask(ctx, prepared, approval):
        approval_id = await ctx.confirm.request("delete_all", "Delete everything?", [blocks.text("really")])
        seen["approval_ids"].append(approval_id)
        result = await ctx.confirm.await_(approval_id, timeout_ms=5000)
        return {"approval_id": approval_id, "status": result.status, "decided_at": result.decided_at}

    @plugin.on("connector.state_changed")
    async def changed(ctx, params):
        await asyncio.sleep(0.01)
        seen["events"].append(("connector.state_changed", params))

    @plugin.on("principal.removed")
    async def removed(ctx, params):
        seen["events"].append(("principal.removed", params))

    @plugin.on("shutdown")
    async def stopping(ctx, params):
        seen["events"].append(("shutdown", params))

    @plugin.on_purge
    async def purge(ctx, scope, principal):
        seen["purges"].append((scope, principal))

    return plugin, seen


async def start_ask(host: PluginTestHost) -> asyncio.Task:
    """Run the tool that opens a confirmation, and wait until the card exists."""
    opened = len(host.confirmations)
    task = asyncio.ensure_future(host.call_tool("ask", {"title": "x"}))
    for _ in range(200):
        if len(host.confirmations) > opened:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("the plugin never opened a confirmation")


async def test_page_headers_match_daemon():
    plugin, _ = build_plugin()
    async with PluginTestHost(plugin) as host:
        for path, status in (("/", 200), ("/missing", 404), ("/data.json", 200), ("/odd", 502), ("/a//b", 400)):
            response = await host.get(path)
            assert response.status == status, path
            assert response.headers["content-security-policy"] == CSP
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["x-frame-options"] == "DENY"
        home = await host.get("/")
        assert home.headers["content-type"] == "text/html; charset=utf-8"
        assert home.text == "<h1>Home</h1>"
        data = await host.get("/data.json")
        assert data.body == b'{"a": 1}'
        assert data.headers["content-type"] == "application/json"
        assert "set-cookie" not in data.headers and "x-plugin" not in data.headers
        assert (await host.get("/binary")).headers["content-type"] == "application/octet-stream"
    assert _pages.SECURITY_HEADERS["content-security-policy"] == CSP


async def test_post_returns_405():
    plugin, seen = build_plugin()
    async with PluginTestHost(plugin) as host:
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            response = await host.request(method, "/")
            assert response.status == 405
            assert response.headers["allow"] == "GET, HEAD"
            assert response.headers["content-security-policy"] == CSP
    assert seen["pages"] == []


async def test_head_is_served():
    plugin, seen = build_plugin()
    async with PluginTestHost(plugin) as host:
        head = await host.request("HEAD", "/?a=1&a=2&b=")
        get = await host.get("/")
        assert head.status == 200 and head.body == b""
        assert head.headers == get.headers
    assert seen["pages"][0] == ("GET", "/", {"a": "2", "b": ""})


@pytest.mark.parametrize("path", [
    "/../etc", "/a/../b", "/%2e%2e/x", "/a%2F..%2Fb", "//x", "/a//b", "/a%2f%2fb", "/a\\b", "/a%5Cb",
    "/a%00b", "/" + "x" * 600,
])
async def test_traversal_rejected(path):
    plugin, seen = build_plugin()
    async with PluginTestHost(plugin) as host:
        response = await host.get(path)
        assert response.status == 400
        assert response.headers["content-security-policy"] == CSP
    assert seen["pages"] == []


def test_normalize_path_rules():
    assert _pages.normalize_path("") == "/"
    assert _pages.normalize_path("a") == "/a"
    assert _pages.normalize_path("/a%20b") == "/a b"
    assert _pages.normalize_path("/a%252e%252e") == "/a%2e%2e"  # decoded once
    assert _pages.normalize_path("/a..b/..c") == "/a..b/..c"
    assert _pages.normalize_path("/" + "x" * 511) is not None
    assert _pages.normalize_path("/" + "x" * 512) is None


def test_filter_response_rules():
    ok = {"status": 200, "headers": {"Content-Type": "TEXT/HTML; charset=UTF-8", "Cache-Control": "public"},
          "body": "hi"}
    assert _pages.filter_response(ok) == (
        200, {"cache-control": "private, no-store", "content-type": "text/html; charset=utf-8"}, b"hi")
    assert _pages.filter_response({"status": 301, "body": ""})[0] == 502
    assert _pages.filter_response({"status": 200, "body": "!!", "body_encoding": "base64"})[0] == 502
    assert _pages.filter_response({"status": 200, "body": "aGk=", "body_encoding": "base64"})[2] == b"hi"
    big = {"status": 200, "body": "x" * (_pages.MAX_PAGE_BODY_BYTES + 1)}
    assert _pages.filter_response(big)[0] == 502
    assert _pages.filter_response({"status": 200, "headers": {"content-type": "text/html; charset=latin-1"},
                                   "body": ""})[1]["content-type"] == "application/octet-stream"
    assert _pages.filter_response("nope")[0] == 502


async def test_confirmation_round_trip():
    plugin, seen = build_plugin()
    async with PluginTestHost(plugin) as host:
        task = await start_ask(host)
        (card,) = host.confirmations
        assert card.status == "pending"
        assert (card.kind, card.title, card.principal) == ("delete_all", "Delete everything?", "local")
        assert card.preview == [blocks.text("really")]
        assert not task.done()
        decided = await host.decide_confirmation(card.approval_id, "approve")
        assert decided.status == "approved"
        outcome = await asyncio.wait_for(task, 5)
        assert outcome.result["status"] == "approved"
        assert outcome.result["decided_at"] == host.confirmations[0].decided_at
        assert outcome.result["approval_id"] == card.approval_id == seen["approval_ids"][0]
        assert [e["summary"] for e in outcome.audit if e["decision"] == "plugin_confirm"] == [
            "delete_all; requested", "delete_all; approved"]
        with pytest.raises(ValueError):
            await host.decide_confirmation(card.approval_id, "deny")
        with pytest.raises(KeyError):
            await host.decide_confirmation("nope", "deny")
        with pytest.raises(ValueError):
            await host.decide_confirmation(card.approval_id, "maybe")

        task = await start_ask(host)
        second = host.confirmations[1]
        await host.decide_confirmation(second.approval_id, "deny")
        assert (await asyncio.wait_for(task, 5)).result["status"] == "denied"


async def test_confirmation_expired():
    plugin, _ = build_plugin()
    async with PluginTestHost(plugin) as host:
        task = await start_ask(host)
        await host.decide_confirmation(host.confirmations[0].approval_id, "expire")
        outcome = await asyncio.wait_for(task, 5)
        assert outcome.result["status"] == "expired"
        assert outcome.result["decided_at"]


async def test_confirmation_refusals():
    plugin, _ = build_plugin()
    async with PluginTestHost(plugin) as host:
        good = {"principal": "local", "kind": "k", "title": "t", "preview": [blocks.text("x")]}
        for change, code in (
            ({"kind": "Bad Kind"}, "invalid_params"),
            ({"title": ""}, "invalid_params"),
            ({"title": "t" * 121}, "invalid_params"),
            ({"preview": [{"type": "nope"}]}, "invalid_blocks"),
            ({"principal": "someone"}, "unknown_principal"),
        ):
            with pytest.raises(RpcError) as caught:
                await host._confirmations.request({**good, **change})
            assert caught.value.code == code
        with pytest.raises(RpcError) as caught:
            await host._confirmations.await_({"approval_id": "unknown"})
        assert caught.value.code == "invalid_params"
        opened = await host._confirmations.request(good)
        with pytest.raises(RpcError) as caught:
            await host._confirmations.await_({"approval_id": opened["approval_id"], "timeout_ms": 20})
        assert caught.value.code == "timeout"
        assert host.confirmations[0].status == "pending"


async def test_events_reach_handlers():
    plugin, seen = build_plugin()
    async with PluginTestHost(plugin) as host:
        params = {"connector": "gmail", "state": "signed_out", "principal": "local"}
        await host.emit("connector.state_changed", params)
        assert seen["events"] == [("connector.state_changed", params)]  # emit waits for a slow handler
        await host.emit("principal.removed", {"principal": "local"})
        await host.emit("plugin.disabling", {"reason": "user"})  # no handler registered: fine
        assert [name for name, _ in seen["events"]] == ["connector.state_changed", "principal.removed"]
        with pytest.raises(ValueError):
            await host.emit("nope")


async def test_purge_calls_handler():
    plugin, seen = build_plugin()
    async with PluginTestHost(plugin) as host:
        assert await host.purge() is True
        assert await host.purge("install") is True
        assert await host.purge("principal", principal="local") is True
        with pytest.raises(ValueError):
            await host.purge("principal")
        with pytest.raises(ValueError):
            await host.purge("everything")
    assert seen["purges"] == [("all", None), ("install", None), ("principal", "local")]


async def test_shutdown_stops_the_plugin():
    plugin, seen = build_plugin()
    async with PluginTestHost(plugin) as host:
        await host.shutdown()
        await host.shutdown()
        assert seen["events"] == [("shutdown", {"grace_ms": 0})]
        assert (await host.get("/")).status == 502  # nothing answers any more


async def test_not_running_host_refuses():
    plugin, _ = build_plugin()
    host = PluginTestHost(plugin)
    with pytest.raises(RuntimeError):
        await host.get("/")
    with pytest.raises(RuntimeError):
        await host.emit("shutdown")


def _run(code: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SDK_SRC)}
    return subprocess.run(  # nosec B603  # fixed argv: this interpreter running a snippet
        [sys.executable, *args, "-c", code] if not args else [sys.executable, *args],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )


def test_pytest_fixture_importable_without_pytest_loaded():
    code = textwrap.dedent("""
        import sys
        import privacyfence_plugin_sdk.testing.pytest as mod
        assert "pytest" not in sys.modules, "importing the fixture module loaded pytest"
        assert "plugin_host" in dir(mod)
        print("ok")
    """)
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_pytest_fixture_builds_a_host(tmp_path):
    (tmp_path / "test_it.py").write_text(textwrap.dedent("""
        import asyncio
        from privacyfence_plugin_sdk import Html, Plugin

        plugin = Plugin(name="fixt", version="1.0.0")

        @plugin.page("/")
        async def home(ctx, request):
            return Html("hi")

        def test_page(plugin_host):
            async def go():
                async with plugin_host(plugin) as host:
                    return (await host.get("/")).text
            assert asyncio.run(go()) == "hi"
    """))
    result = _run("", "-m", "pytest", "-p", "privacyfence_plugin_sdk.testing.pytest", "-p", "no:cacheprovider",
                  "-q", "--rootdir", str(tmp_path), str(tmp_path / "test_it.py"))
    assert result.returncode == 0, result.stdout + result.stderr
