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
from privacyfence_plugin_sdk import Bytes, Html, PageEntry, Plugin, Prepared, Text, blocks
from privacyfence_plugin_sdk.testing import PluginTestHost
from privacyfence_plugin_sdk.testing import _host as host_module
from privacyfence_plugin_sdk.testing import _pages
from privacyfence_plugin_sdk.plugin import ApprovalsClient

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
    async with PluginTestHost(plugin, pages=True) as host:
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
    async with PluginTestHost(plugin, pages=True) as host:
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


def test_filter_response_drops_a_204_body():
    status, _, body = _pages.filter_response({"status": 204, "body": "not allowed"})
    assert (status, body) == (204, b"")


async def test_page_slower_than_the_timeout_is_502(monkeypatch):
    monkeypatch.setattr(_pages, "_WEB_REQUEST_TIMEOUT", 0.2)
    plugin = Plugin(name="slow", version="1.0.0")

    @plugin.page("/")
    async def home(ctx, request):
        await asyncio.sleep(2)
        return Html("late")

    async with PluginTestHost(plugin, pages=True) as host:
        response = await host.get("/")
    assert response.status == 502
    assert response.body == b"The plugin did not answer."


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
    async with PluginTestHost(plugin, pages=True) as host:
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
                async with plugin_host(plugin, pages=True) as host:
                    return (await host.get("/")).text
            assert asyncio.run(go()) == "hi"
    """))
    result = _run("", "-m", "pytest", "-p", "privacyfence_plugin_sdk.testing.pytest", "-p", "no:cacheprovider",
                  "-q", "--rootdir", str(tmp_path), str(tmp_path / "test_it.py"))
    assert result.returncode == 0, result.stdout + result.stderr


def build_approval_plugin(with_page: bool = True) -> tuple[Plugin, dict]:
    seen: dict = {"revoked": [], "tickets": [], "checks": []}
    plugin = Plugin(name="approver", version="1.0.0")

    if with_page:
        @plugin.page("/approval")
        async def approval_page(ctx, request):
            return Text(request.query.get("pf_approval", ""))

    @plugin.tool("approve", description="Ask for approval.", gate="popup",
                 params={"text": {"type": "string"}, "page": {"type": "string"}}, required=["text"])
    async def approve(ctx, args):
        return Prepared(preview=[blocks.text("approve")], state=args)

    @approve.execute
    async def do_approve(ctx, prepared, approval):
        args = prepared.state
        before = await ctx.approvals.check("template", "templates/a", args["text"])
        ticket = await ctx.approvals.request(
            "template", "templates/a", args["text"], "Approve template", [blocks.text("template")],
            page=args.get("page"),
        )
        seen["tickets"].append(ticket)
        return {"before": before, "approval_id": ticket.approval_id, "status": ticket.status}

    @plugin.tool("wait", description="Wait for an approval.", gate="popup",
                 params={"approval_id": {"type": "string"}, "text": {"type": "string"}}, required=["approval_id", "text"])
    async def wait(ctx, args):
        return Prepared(preview=[blocks.text("wait")], state=args)

    @wait.execute
    async def do_wait(ctx, prepared, approval):
        result = await ctx.approvals.await_(prepared.state["approval_id"], timeout_ms=5000)
        after = await ctx.approvals.check("template", "templates/a", prepared.state["text"])
        return {"status": result.status, "decided_at": result.decided_at, "after": after}

    @plugin.on("approval.revoked")
    async def revoked(ctx, params):
        seen["revoked"].append(params)

    return plugin, seen


class TestApprovals:
    async def test_request_opens_a_card_and_approving_it_stores_the_approval(self):
        plugin, seen = build_approval_plugin()
        async with PluginTestHost(plugin, pages=True) as host:
            first = (await host.call_tool("approve", {"text": "v1", "page": "/approval"})).result
            assert first["before"] == "unknown" and first["status"] == "pending"
            [card] = host.approvals
            assert card.status == "pending" and card.kind == "template" and card.subject_id == "templates/a"
            assert card.approval_id == first["approval_id"] and card.digest == _digest("v1")
            assert card.frame_path == f"/approval?pf_approval={card.approval_id}"
            shown = await host.get("/approval", query={"pf_approval": card.approval_id})
            assert shown.text == card.approval_id

            # the same request while the card waits is the same card
            again = (await host.call_tool("approve", {"text": "v1"})).result
            assert again["approval_id"] == card.approval_id and len(host.approvals) == 1

            decided = await host.decide_approval(card.approval_id, "approve")
            assert decided.status == "approved" and decided.decided_at
            waited = (await host.call_tool("wait", {"approval_id": card.approval_id, "text": "v1"})).result
            assert waited["status"] == "approved" and waited["after"] == "approved"

            # approved: no new card, and await answers for the id without a card
            stored = (await host.call_tool("approve", {"text": "v1"})).result
            assert stored["before"] == "approved" and stored["status"] == "approved"
            assert stored["approval_id"] == card.approval_id and len(host.approvals) == 1
            # other content is another thing
            other = (await host.call_tool("approve", {"text": "v2"})).result
            assert other["before"] == "unknown" and other["status"] == "pending" and len(host.approvals) == 2
        assert [a["decision"] for a in host.audit if a["decision"] == "plugin_approval"] == [
            "plugin_approval"] * 3
        assert [a["summary"] for a in host.audit if a["decision"] == "plugin_approval"] == [
            "template; requested", "template; approved", "template; requested"]

    async def test_denied_and_expired_cards_store_nothing(self):
        plugin, _ = build_approval_plugin()
        async with PluginTestHost(plugin) as host:
            for decision, status in (("deny", "denied"), ("expire", "expired")):
                ticket = (await host.call_tool("approve", {"text": "v1"})).result
                assert ticket["status"] == "pending"
                await host.decide_approval(ticket["approval_id"], decision)
                waited = (await host.call_tool("wait", {"approval_id": ticket["approval_id"], "text": "v1"})).result
                assert waited["status"] == status and waited["after"] == "unknown"
            assert [a.status for a in host.approvals] == ["denied", "expired"]  # each retry opened a new card
            with pytest.raises(ValueError, match="already"):
                await host.decide_approval(host.approvals[0].approval_id, "approve")
            with pytest.raises(ValueError, match="decision must be"):
                await host.decide_approval(host.approvals[0].approval_id, "maybe")
            with pytest.raises(KeyError):
                await host.decide_approval("nope", "approve")

    async def test_revoking_sends_the_event_and_check_says_revoked(self):
        plugin, seen = build_approval_plugin()
        async with PluginTestHost(plugin) as host:
            ticket = (await host.call_tool("approve", {"text": "v1"})).result
            with pytest.raises(KeyError):
                await host.revoke_approval(ticket["approval_id"])  # nothing stored yet
            await host.decide_approval(ticket["approval_id"], "approve")
            record = await host.revoke_approval(ticket["approval_id"])
            assert record.revoked_at and host.approvals[0].revoked_at == record.revoked_at
            assert seen["revoked"] == [{
                "approval_id": ticket["approval_id"], "kind": "template", "subject_id": "templates/a",
                "digest": _digest("v1"),
            }]
            await host.revoke_approval(ticket["approval_id"])
            assert len(seen["revoked"]) == 1  # revoking twice changes and sends nothing
            checked = (await host.call_tool("approve", {"text": "v1"})).result
            assert checked["before"] == "revoked" and checked["status"] == "pending"  # a revoked one asks again
            await host.decide_approval(checked["approval_id"], "approve")
            again = (await host.call_tool("approve", {"text": "v1"})).result
            assert again["before"] == "approved" and again["approval_id"] == checked["approval_id"]

    async def test_request_validation_matches_the_daemons(self):
        plugin, _ = build_approval_plugin()
        good = {"principal": "local", "kind": "template", "subject_id": "a", "digest": _digest("x"),
                "title": "T", "preview": [blocks.text("p")]}
        async with PluginTestHost(plugin, pages=True) as host:
            service = host._approvals
            for change, code in (
                ({"kind": "Bad Kind"}, "invalid_params"), ({"subject_id": ""}, "invalid_params"),
                ({"subject_id": "a‮b"}, "invalid_params"), ({"subject_id": "a\nb"}, "invalid_params"),
                ({"subject_id": "x" * 201}, "invalid_params"),
                ({"digest": "sha256:ABC"}, "invalid_params"), ({"title": ""}, "invalid_params"),
                ({"title": "t" * 121}, "invalid_params"), ({"require_step_up": "yes"}, "invalid_params"),
                ({"preview": [{"type": "nope"}]}, "invalid_blocks"), ({"principal": "bob"}, "unknown_principal"),
                ({"page": "approval"}, "invalid_params"), ({"page": "/a?x=1"}, "invalid_params"),
                ({"page": "/a%3Fx"}, "invalid_params"), ({"page": "/a%23x"}, "invalid_params"),
                ({"page": "/../x"}, "invalid_params"),
            ):
                with pytest.raises(RpcError) as bad:
                    await service.request({**good, **change})
                assert bad.value.code == code, change
            ok = await service.request({**good, "page": "/approval"})
            assert ok["status"] == "pending"
            with pytest.raises(RpcError) as bad:
                await service.check({**{k: good[k] for k in ("principal", "kind", "subject_id")}, "digest": "nope"})
            assert bad.value.code == "invalid_params"
            with pytest.raises(RpcError) as unknown:
                await service.await_({"approval_id": "approval-nope"})
            assert unknown.value.code == "invalid_params"
            with pytest.raises(RpcError) as slow:
                await service.await_({"approval_id": ok["approval_id"], "timeout_ms": 0})
            assert slow.value.code == "timeout" and slow.value.retryable
            with pytest.raises(RpcError):
                await service.await_({"approval_id": ok["approval_id"], "timeout_ms": 300_001})

    async def test_a_page_needs_the_plugin_to_have_pages(self):
        plugin, _ = build_approval_plugin(with_page=False)
        async with PluginTestHost(plugin) as host:
            with pytest.raises(RpcError, match="pages: true") as refused:
                await host._approvals.request({
                    "principal": "local", "kind": "template", "subject_id": "a", "digest": _digest("x"),
                    "title": "T", "preview": [blocks.text("p")], "page": "/approval"})
            assert refused.value.code == "invalid_params" and host.approvals == []

    def test_revoked_is_an_event_the_host_can_send(self):
        assert "approval.revoked" in host_module._EVENT_NAMES


def _digest(text: str) -> str:
    return ApprovalsClient.digest(text)


def build_output_plugin() -> Plugin:
    plugin = Plugin(name="reporter", version="1.0.0")

    @plugin.tool("publish", description="Publish a file.", gate="popup",
                 params={"name": {"type": "string"}, "text": {"type": "string"}}, required=["name", "text"])
    async def publish(ctx, args):
        return Prepared(preview=[blocks.text("publish")], state=args)

    @publish.execute
    async def do_publish(ctx, prepared, approval):
        return {"path": ctx.outputs.publish(prepared.state["name"], prepared.state["text"]),
                "dir": str(ctx.outputs.dir)}

    return plugin


class TestListPages:
    @staticmethod
    def plugin_with(entries=None):
        plugin = Plugin(name="listing", version="1.0.0")

        @plugin.page("/")
        async def home(ctx, request):
            return Html("home")

        if entries is not None:
            @plugin.page_index
            async def index(ctx):
                return entries

        return plugin

    async def test_it_returns_the_entries(self):
        entries = [PageEntry("/", "Home"), PageEntry("/a", "A", version="v2")]
        async with PluginTestHost(self.plugin_with(entries), pages=True) as host:
            assert await host.list_pages() == [e.to_wire() for e in entries]

    async def test_without_an_index_it_lists_the_root_page(self):
        async with PluginTestHost(self.plugin_with(), pages=True) as host:
            assert await host.list_pages() == [{"path": "/", "title": "listing"}]

    async def test_an_invalid_entry_raises_assertion_error(self):
        async with PluginTestHost(self.plugin_with([PageEntry("/x y", "X")]), pages=True) as host:
            with pytest.raises(AssertionError, match=r"pages\[0\]\.path"):
                await host.list_pages()

    async def test_it_needs_the_plugin_to_have_pages(self):
        async with PluginTestHost(self.plugin_with([PageEntry("/", "Home")])) as host:
            with pytest.raises(LookupError):
                await host.list_pages()


class TestOutputs:
    async def test_publish_lands_in_the_folder_and_is_listed(self):
        async with PluginTestHost(build_output_plugin(), outputs=True) as host:
            result = (await host.call_tool("publish", {"name": "reports/a.csv", "text": "x,y\n1,2\n"})).result
            assert result["path"] == "reports/a.csv" and result["dir"] == str(host.output_dir)
            assert (host.output_dir / "reports" / "a.csv").read_text() == "x,y\n1,2\n"
            await host.call_tool("publish", {"name": "reports/b.json", "text": "{}"})
            await host.call_tool("publish", {"name": "top.csv", "text": "z"})
            files = host.list_outputs()
            assert [f.path for f in files] == ["reports/a.csv", "reports/b.json", "top.csv"]
            assert [f.mime_type for f in files] == ["text/csv", "application/json", "text/csv"]
            assert files[0].size == 8 and files[0].modified.endswith("Z")
            assert [f.path for f in host.list_outputs("reports/")] == ["reports/a.csv", "reports/b.json"]
            assert host.list_outputs("nothing/") == []

    async def test_the_listing_hides_what_the_daemon_hides(self, tmp_path):
        async with PluginTestHost(build_output_plugin(), outputs=True, output_types=("text/csv",)) as host:
            root = host.output_dir
            (root / "ok.csv").write_text("1")
            (root / "wrong.json").write_text("{}")
            (root / ".hidden.csv").write_text("1")
            (root / ".a.csv.tmp").write_text("1")
            (root / ".dot").mkdir()
            (root / ".dot" / "in.csv").write_text("1")
            (root / "ok.CSV.bak").write_text("1")
            (root / "dir").mkdir()
            (root / "dir" / "deep.csv").write_text("1")
            outside = tmp_path / "secret.csv"
            outside.write_text("s")
            (root / "link.csv").symlink_to(outside)
            (root / "linked").symlink_to(tmp_path, target_is_directory=True)
            deep = root.joinpath(*"abcdefghi")
            deep.mkdir(parents=True)
            (deep / "too.csv").write_text("1")
            (root.joinpath(*"abcdefg") / "fits.csv").write_text("1")
            assert [f.path for f in host.list_outputs()] == ["a/b/c/d/e/f/g/fits.csv", "dir/deep.csv", "ok.csv"]

    async def test_publish_refusals_come_from_the_output_types(self):
        async with PluginTestHost(build_output_plugin(), outputs=True, output_types=("text/csv",)) as host:
            outcome = await host.call_tool("publish", {"name": "a.json", "text": "{}"})
            assert outcome.error is not None and host.list_outputs() == []
            await host.call_tool("publish", {"name": "a.csv", "text": "1"})
            again = await host.call_tool("publish", {"name": "a.csv", "text": "2"})
            assert again.error is not None and (host.output_dir / "a.csv").read_text() == "1"

    async def test_each_principal_has_its_own_folder(self):
        principals = [{"id": "alice"}, {"id": "bob"}]
        async with PluginTestHost(build_output_plugin(), mode="org", principals=principals, outputs=True) as host:
            await host.call_tool("publish", {"name": "a.csv", "text": "1"}, principal="alice")
            await host.call_tool("publish", {"name": "b.csv", "text": "1"}, principal="bob")
            assert [f.path for f in host.list_outputs(principal="alice")] == ["a.csv"]
            assert [f.path for f in host.list_outputs(principal="bob")] == ["b.csv"]
            assert host.output_dir == host._output_dir("alice") != host._output_dir("bob")

    async def test_a_host_without_outputs_has_no_folder(self):
        async with PluginTestHost(build_output_plugin()) as host:
            with pytest.raises(RuntimeError, match="outputs=True"):
                _ = host.output_dir
            with pytest.raises(RuntimeError):
                host.list_outputs()
            outcome = await host.call_tool("publish", {"name": "a.csv", "text": "1"})
            assert outcome.error is not None

    def test_constructor_validation(self):
        plugin = build_output_plugin()
        with pytest.raises(ValueError, match="needs outputs=True"):
            PluginTestHost(plugin, output_types=("text/csv",))
        with pytest.raises(ValueError, match="unknown output type"):
            PluginTestHost(plugin, outputs=True, output_types=("image/png",))
        with pytest.raises(ValueError, match="non-empty"):
            PluginTestHost(plugin, outputs=True, output_types=())
