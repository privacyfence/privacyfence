"""The plugin runtime, driven over an in-memory stream pair."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from privacyfence.plugins import constants
import privacyfence_plugin_sdk.plugin as plugin_module
from privacyfence_plugin_sdk.testing import _pages as pages_module
from privacyfence.plugins.protocol import InitializeResult, PrepareResult, args_digest
from privacyfence_plugin_sdk import (
    Bytes,
    Html,
    Plugin,
    Prepared,
    SourceError,
    Text,
    ToolDefinitionError,
    blocks,
    plugin as sdk_plugin,
)

APPROVAL = {"approval_id": "card-1", "decision": "approved", "via": "card", "decided_at": "2026-10-07T10:00:00+00:00"}


def build_plugin() -> Plugin:
    plugin = Plugin(name="demo", version="1.2.0")
    plugin.scope_type("calendar", "Calendar id a call reads")

    @plugin.tool(
        "list_events", description="List events.", read_only=True, scopes=["calendar"],
        params={"calendar_id": {"type": "string", "description": "Which calendar"}},
    )
    async def list_events(ctx, args):
        return Prepared(
            preview=[blocks.fields({"Calendar": args.get("calendar_id", "primary")})],
            payload=[blocks.text("three events")],
            scopes={"calendar": [args.get("calendar_id", "primary")]},
        )

    @plugin.tool("rename", description="Rename a thing.", gate="popup", destructive=True,
                 params={"name": {"type": "string"}}, required=["name"], effect="Renames the thing.")
    async def rename(ctx, args):
        return Prepared(preview=[blocks.text("rename")], state={"seen": args["name"]})

    @rename.execute
    async def do_rename(ctx, prepared, approval):
        return {"renamed": prepared.state["seen"], "by": approval["via"], "principal": ctx.principal.id}

    @plugin.page("/")
    async def home(ctx, request):
        return Html(f"<p>{request.query.get('q', '')}</p>")

    @plugin.page("/data")
    async def data(ctx, request):
        return Bytes(b"\x89PNG", content_type="image/png")

    @plugin.page("/text")
    async def text_page(ctx, request):
        return Text("plain", status=404)

    @plugin.page("/boom")
    async def boom(ctx, request):
        raise RuntimeError("secret detail")

    return plugin


@pytest.fixture
async def daemon(make_daemon):
    d = await make_daemon(build_plugin())
    yield d
    await d.stop()


def prepare_params(principal, call_id="c1", tool="list_events", args=None):
    return {"call_id": call_id, "principal": principal, "tool": tool, "args": args or {}, "reason": None}


def execute_params(principal, call_id="c1", tool="list_events", args=None):
    args = args or {}
    return {"call_id": call_id, "principal": principal, "tool": tool, "args": args,
            "args_digest": args_digest(args), "approval": APPROVAL}


class TestLimits:
    @pytest.mark.parametrize("sdk_name, constant", [
        ("_MAX_LINE_BYTES", "MAX_LINE_BYTES"), ("_MAX_IN_FLIGHT", "MAX_IN_FLIGHT"),
        ("_INVALID_LINES_LIMIT", "INVALID_LINES_LIMIT"), ("_INLINE_RESULT_BYTES", "INLINE_RESULT_BYTES"),
        ("_MAX_TITLE_CHARS", "MAX_TITLE_CHARS"), ("_MAX_EFFECT_CHARS", "MAX_EFFECT_CHARS"),
        ("_MAX_DESCRIPTION_CHARS", "MAX_DESCRIPTION_CHARS"), ("_MAX_TOOLS", "MAX_TOOLS"),
        ("_MAX_SCOPE_VALUES", "MAX_SCOPE_VALUES"), ("_MAX_SCOPE_VALUE_CHARS", "MAX_SCOPE_VALUE_CHARS"),
        ("_MCP_TOOL_NAME_MAX", "MCP_TOOL_NAME_MAX"), ("_MAX_PAGE_PATH_CHARS", "MAX_PAGE_PATH_CHARS"),
        ("_PREPARED_CALL_LIFETIME_SECONDS", "PREPARED_CALL_LIFETIME_SECONDS"),
        ("_CONFIRM_AWAIT_MAX_MS", "CONFIRM_AWAIT_MAX_MS"),
        ("_GATES", "GATES"),
        ("_RESERVED_PLUGIN_NAMES", "RESERVED_PLUGIN_NAMES"),
        ("_MAX_SCOPE_TYPE_DESCRIPTION_CHARS", "MAX_SCOPE_TYPE_DESCRIPTION_CHARS"),
    ])
    def test_matches_the_daemons_constants(self, sdk_name, constant):
        assert getattr(sdk_plugin, sdk_name) == getattr(constants, constant)

    def test_timeouts_and_patterns(self):
        assert sdk_plugin._SOURCE_CALL_TIMEOUT_SECONDS == constants.TIMEOUT_SECONDS["source.call"]
        assert sdk_plugin._CONFIRM_REQUEST_TIMEOUT_SECONDS == constants.TIMEOUT_SECONDS["confirm.request"]
        assert pages_module._WEB_REQUEST_TIMEOUT == constants.TIMEOUT_SECONDS["web.request"]
        for name in ("PLUGIN_NAME_RE", "TOOL_NAME_RE", "SCOPE_TYPE_RE"):
            assert getattr(sdk_plugin, "_" + name).pattern == getattr(constants, name).pattern

    def test_block_limits_and_version(self):
        from privacyfence_plugin_sdk import blocks as sdk_blocks

        assert sdk_blocks._MAX_PREVIEW_BLOCKS == constants.MAX_PREVIEW_BLOCKS
        assert sdk_blocks._MAX_PREVIEW_BYTES == constants.MAX_PREVIEW_BYTES
        assert sdk_blocks._MAX_CELL_CHARS == constants.MAX_CELL_CHARS
        assert sdk_blocks._BLOCK_KEY_RE.pattern == constants.BLOCK_KEY_RE.pattern
        assert sdk_plugin.PROTOCOL_VERSION == constants.PROTOCOL_VERSION

    def test_digest_matches_the_daemons(self):
        args = {"b": 1, "a": "é"}
        assert sdk_plugin.args_digest(args) == args_digest(args)


class TestInitialize:
    async def test_result_shape_parses_with_the_daemons_validator(self, daemon):
        result = await daemon.initialize()
        parsed = InitializeResult.from_wire(result)
        assert parsed.protocol_version == "1.1.0"
        assert (parsed.plugin_name, parsed.plugin_version) == ("demo", "1.2.0")
        assert {t.name for t in parsed.tools} == {"list_events", "rename"}
        assert result["plugin"] == {"name": "demo", "version": "1.2.0"}
        assert result["scope_types"] == [{"name": "calendar", "description": "Calendar id a call reads"}]
        tools = {t["name"]: t for t in result["tools"]}
        assert set(tools) == {"list_events", "rename"}
        assert tools["list_events"]["read_only"] is True
        assert tools["rename"]["destructive"] is True and tools["rename"]["gate"] == "popup"
        assert tools["rename"]["parameters"] == {
            "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    async def test_major_mismatch(self, daemon):
        data = await daemon.error("initialize", {"protocol_version": "2.0.0", "data_dir": "/x"})
        assert data["code"] == "version_mismatch"

    async def test_calls_before_initialize_are_refused(self, daemon, principal):
        data = await daemon.error("tool.prepare", prepare_params(principal))
        assert data["code"] == "invalid_request"

    async def test_unknown_method(self, daemon):
        assert (await daemon.error("nope", {}))["code"] == "method_not_found"

    async def test_introspection_refuses_source_calls(self, daemon):
        await daemon.initialize("introspect")
        ctx = daemon.plugin._ctx("local")
        assert ctx.introspecting is True
        with pytest.raises(SourceError) as info:
            await ctx.source.call("jira.search", jql="x")
        assert info.value.code == "introspection_only"


class TestToolDefinitionErrors:
    def make(self):
        return Plugin(name="demo", version="1.0.0")

    @pytest.mark.parametrize("kwargs, fragment", [
        ({"name": "Bad"}, "must match"),
        ({"description": ""}, "description"),
        ({"description": "x" * 1025}, "description"),
        ({"gate": "weird"}, "gate"),
        ({"destructive": True, "gate": "review"}, "popup gate"),
        ({"destructive": True, "read_only": True, "gate": "popup"}, "both read-only and destructive"),
        ({"params": {"reason": {"type": "string"}}}, "reason"),
        ({"params": {"x": {"type": "array"}}}, "only string, integer, number and boolean"),
        ({"params": {"x": {"type": "string", "enum": ["a"]}}}, "only string, integer, number and boolean"),
        ({"required": ["ghost"]}, "undeclared parameter"),
        ({"title": "t" * 121}, "title"),
        ({"effect": "e" * 201}, "effect"),
        ({"title": "A\nB"}, "tool.title must not contain line breaks, tabs, control or bidirectional characters"),
        ({"effect": "A\tB"}, "tool.effect must not contain line breaks, tabs, control or bidirectional characters"),
        ({"name": "a" * 42}, "must match"),
    ])
    def test_floor_violations_raise_at_registration(self, kwargs, fragment):
        plugin = self.make()
        args = {"name": "ok_tool", "description": "Does it."}
        args.update(kwargs)
        name = args.pop("name")
        with pytest.raises(ToolDefinitionError, match=fragment):
            plugin.tool(name, **args)

    def test_reserved_plugin_name(self):
        with pytest.raises(ValueError, match="is reserved"):
            Plugin("apps", "1.0.0")

    def test_scope_type_rules(self):
        plugin = self.make()
        with pytest.raises(ValueError, match="scope type output is reserved"):
            plugin.scope_type("output", "x")
        for description in ("", "x" * 501):
            with pytest.raises(ValueError, match="needs a description of 1 to 500 characters"):
                plugin.scope_type("cal", description)
        plugin.scope_type("cal", "x" * 500)

    def test_mcp_name_length(self):
        plugin = Plugin(name="p" * 30, version="1")
        with pytest.raises(ToolDefinitionError, match="longer than 64"):
            plugin.tool("t" * 40, description="d")

    def test_duplicate_tool(self):
        plugin = self.make()

        async def fn(ctx, args):
            return Prepared(preview=[])

        plugin.tool("one_tool", description="d", read_only=True)(fn)
        with pytest.raises(ToolDefinitionError, match="already registered"):
            plugin.tool("one_tool", description="d")

    def test_write_tool_without_execute_is_refused_before_serving(self):
        plugin = self.make()

        @plugin.tool("writer", description="d")
        async def writer(ctx, args):
            return Prepared(preview=[])

        with pytest.raises(ToolDefinitionError, match="execute"):
            plugin.tool_definitions()

    def test_undeclared_scope_is_refused(self):
        plugin = self.make()

        @plugin.tool("reader", description="d", read_only=True, scopes=["ghost"])
        async def reader(ctx, args):
            return Prepared(preview=[])

        with pytest.raises(ToolDefinitionError, match="not declared"):
            plugin.tool_definitions()

    def test_bad_plugin_name_and_scope_type(self):
        with pytest.raises(ValueError):
            Plugin(name="Bad_Name", version="1")
        plugin = self.make()
        with pytest.raises(ValueError):
            plugin.scope_type("Bad", "d")
        plugin.scope_type("ok", "d")
        with pytest.raises(ValueError):
            plugin.scope_type("ok", "d")

    async def test_serve_fails_fast_on_a_bad_registry(self):
        plugin = self.make()

        @plugin.tool("writer", description="d")
        async def writer(ctx, args):
            return Prepared(preview=[])

        with pytest.raises(ToolDefinitionError):
            await plugin.serve(asyncio.StreamReader(), None)


class TestPrepareExecute:
    async def test_read_prepare_matches_the_daemons_validator(self, daemon, principal):
        await daemon.initialize()
        result = await daemon.result("tool.prepare", prepare_params(principal, args={"calendar_id": "work"}))
        parsed = PrepareResult.from_wire(result)
        assert parsed.payload == result["payload"]
        assert result["scopes"] == {"calendar": ["work"]}
        assert result["payload"] == [{"type": "text", "text": "three events"}]
        assert result["preview"][0]["type"] == "fields"

    async def test_read_execute_may_repeat(self, daemon, principal):
        await daemon.initialize()
        await daemon.result("tool.prepare", prepare_params(principal))
        first = await daemon.result("tool.execute", execute_params(principal))
        again = await daemon.result("tool.execute", execute_params(principal))
        assert first == again == {"result": None}

    async def test_write_state_reaches_execute_and_is_single_use(self, daemon, principal):
        await daemon.initialize()
        args = {"name": "x"}
        result = await daemon.result("tool.prepare", prepare_params(principal, "w1", "rename", args))
        assert "payload" not in result
        done = await daemon.result("tool.execute", execute_params(principal, "w1", "rename", args))
        assert done == {"result": {"renamed": "x", "by": "card", "principal": "local"}}
        again = await daemon.error("tool.execute", execute_params(principal, "w1", "rename", args))
        assert again["code"] == "unknown_call"

    async def test_digest_mismatch(self, daemon, principal):
        await daemon.initialize()
        await daemon.result("tool.prepare", prepare_params(principal, "w1", "rename", {"name": "x"}))
        data = await daemon.error("tool.execute", execute_params(principal, "w1", "rename", {"name": "evil"}))
        assert data["code"] == "digest_mismatch"
        forged = execute_params(principal, "w1", "rename", {"name": "evil"})
        forged["args_digest"] = args_digest({"name": "x"})
        assert (await daemon.error("tool.execute", forged))["code"] == "digest_mismatch"
        # the mismatch did not consume the prepared call
        ok = await daemon.result("tool.execute", execute_params(principal, "w1", "rename", {"name": "x"}))
        assert ok["result"]["renamed"] == "x"

    async def test_unknown_call_and_tool(self, daemon, principal):
        await daemon.initialize()
        assert (await daemon.error("tool.execute", execute_params(principal, "nope")))["code"] == "unknown_call"
        assert (await daemon.error("tool.prepare", prepare_params(principal, tool="ghost")))["code"] == "unknown_tool"
        await daemon.result("tool.prepare", prepare_params(principal, "c9"))
        wrong_tool = execute_params(principal, "c9", "rename")
        assert (await daemon.error("tool.execute", wrong_tool))["code"] == "unknown_call"

    async def test_prepared_calls_expire(self, daemon, principal):
        await daemon.initialize()
        now = [1000.0]
        daemon.plugin._clock = lambda: now[0]
        await daemon.result("tool.prepare", prepare_params(principal))
        now[0] += constants.PREPARED_CALL_LIFETIME_SECONDS + 1
        assert (await daemon.error("tool.execute", execute_params(principal)))["code"] == "unknown_call"
        assert daemon.plugin._prepared == {}

    async def test_prepared_call_survives_until_the_replay_window_ends(self, daemon, principal):
        await daemon.initialize()
        now = [1000.0]
        daemon.plugin._clock = lambda: now[0]
        await daemon.result("tool.prepare", prepare_params(principal, "w1", "rename", {"name": "x"}))
        now[0] += plugin_module._PREPARED_CALL_LIFETIME_SECONDS - 1
        ok = await daemon.result("tool.execute", execute_params(principal, "w1", "rename", {"name": "x"}))
        assert ok["result"]["renamed"] == "x"

    async def test_store_evicts_the_oldest_beyond_the_cap(self, daemon, principal, monkeypatch):
        await daemon.initialize()
        monkeypatch.setattr(plugin_module, "_MAX_PREPARED_CALLS", 2)
        for call_id in ("c1", "c2", "c3"):
            await daemon.result("tool.prepare", prepare_params(principal, call_id, "rename", {"name": "x"}))
        assert list(daemon.plugin._prepared) == ["c2", "c3"]
        gone = await daemon.error("tool.execute", execute_params(principal, "c1", "rename", {"name": "x"}))
        assert gone["code"] == "unknown_call"
        ok = await daemon.result("tool.execute", execute_params(principal, "c3", "rename", {"name": "x"}))
        assert ok["result"]["renamed"] == "x"

    async def test_an_auto_read_is_dropped_after_execute(self, daemon, principal):
        await daemon.initialize()
        await daemon.result("tool.prepare", prepare_params(principal, "r1"))
        params = execute_params(principal, "r1")
        params["approval"] = {**APPROVAL, "via": "auto"}
        await daemon.result("tool.execute", params)
        assert "r1" not in daemon.plugin._prepared

    async def test_missing_scope_values_and_payload_rules(self, make_daemon, principal):
        plugin = Plugin(name="demo", version="1")
        plugin.scope_type("calendar", "d")

        @plugin.tool("scoped", description="d", read_only=True, scopes=["calendar"])
        async def scoped(ctx, args):
            return Prepared(preview=[], payload=[blocks.text("x")], scopes={})

        @plugin.tool("nopayload", description="d", read_only=True)
        async def nopayload(ctx, args):
            return Prepared(preview=[])

        @plugin.tool("hugepayload", description="d", read_only=True)
        async def hugepayload(ctx, args):
            return Prepared(preview=[], payload=[blocks.text("x" * 200_000)])

        @plugin.tool("badblocks", description="d", read_only=True)
        async def badblocks(ctx, args):
            return Prepared(preview=[{"type": "html", "text": "<b>"}], payload=[])

        @plugin.tool("bigresult", description="d", gate="popup")
        async def bigresult(ctx, args):
            return Prepared(preview=[])

        @bigresult.execute
        async def run_big(ctx, prepared, approval):
            return "x" * 200_000

        @plugin.tool("nanresult", description="d", gate="popup")
        async def nanresult(ctx, args):
            return Prepared(preview=[])

        @nanresult.execute
        async def run_nan(ctx, prepared, approval):
            return {"v": float("nan")}

        daemon = await make_daemon(plugin)
        await daemon.initialize()
        await daemon.result("tool.prepare", prepare_params(principal, "n1", "nanresult"))
        data = await daemon.error("tool.execute", execute_params(principal, "n1", "nanresult"))
        assert (data["code"], data["detail"]) == ("internal_error", "the execute result is not JSON")
        for tool, code in [("scoped", "invalid_params"), ("nopayload", "invalid_params"),
                           ("hugepayload", "payload_too_large"), ("badblocks", "invalid_blocks")]:
            assert (await daemon.error("tool.prepare", prepare_params(principal, tool=tool)))["code"] == code
        await daemon.result("tool.prepare", prepare_params(principal, "b1", "bigresult"))
        data = await daemon.error("tool.execute", execute_params(principal, "b1", "bigresult"))
        assert data["code"] == "payload_too_large"
        await daemon.stop()

    async def test_handler_exceptions_do_not_leak(self, make_daemon, principal):
        plugin = Plugin(name="demo", version="1")

        @plugin.tool("crashy", description="d", read_only=True)
        async def crashy(ctx, args):
            raise RuntimeError("token abc123")

        daemon = await make_daemon(plugin)
        await daemon.initialize()
        data = await daemon.error("tool.prepare", prepare_params(principal, tool="crashy"))
        assert data["code"] == "internal_error"
        assert "abc123" not in json.dumps(data)
        await daemon.stop()


class TestSourceErrors:
    async def test_a_source_error_from_a_handler_keeps_its_code_and_reason(self, make_daemon, principal):
        plugin = Plugin(name="demo", version="1")

        @plugin.tool("unplugged", description="d", read_only=True)
        async def unplugged(ctx, args):
            raise SourceError("connector_unavailable", "the connector is not connected", "not_connected")

        @plugin.tool("strange", description="d", read_only=True)
        async def strange(ctx, args):
            raise SourceError("made_up_code", "x")

        daemon = await make_daemon(plugin)
        await daemon.initialize()
        data = await daemon.error("tool.prepare", prepare_params(principal, tool="unplugged"))
        assert (data["code"], data["detail"], data["reason"]) == (
            "connector_unavailable", "the connector is not connected", "not_connected")
        data = await daemon.error("tool.prepare", prepare_params(principal, tool="strange"))
        assert data["code"] == "internal_error"
        await daemon.stop()

    async def test_a_source_error_from_an_execute_handler_keeps_its_code(self, make_daemon, principal):
        plugin = Plugin(name="demo", version="1")

        @plugin.tool("write", description="d", gate="popup")
        async def write(ctx, args):
            return Prepared(preview=[blocks.text("w")])

        @write.execute
        async def do_write(ctx, prepared, approval):
            raise SourceError("upstream_error", "boom", "rate_limited")

        daemon = await make_daemon(plugin)
        await daemon.initialize()
        await daemon.result("tool.prepare", prepare_params(principal, tool="write"))
        data = await daemon.error("tool.execute", execute_params(principal, tool="write"))
        assert (data["code"], data["reason"]) == ("upstream_error", "rate_limited")
        await daemon.stop()


class TestPages:
    async def web(self, daemon, principal, path, query=None):
        return await daemon.result(
            "web.request", {"principal": principal, "method": "GET", "path": path, "query": query or {}})

    async def test_routing(self, daemon, principal):
        await daemon.initialize()
        for path in ("/", ""):
            page = await self.web(daemon, principal, path, {"q": "hi"})
            assert page["status"] == 200 and page["body"] == "<p>hi</p>"
            assert page["headers"]["content-type"] == "text/html; charset=utf-8"
        assert (await self.web(daemon, principal, "/missing"))["status"] == 404
        assert (await self.web(daemon, principal, "/data/"))["status"] == 404  # exact match only

    async def test_bytes_text_and_failures(self, daemon, principal):
        await daemon.initialize()
        png = await self.web(daemon, principal, "/data")
        assert png["body_encoding"] == "base64" and base64.b64decode(png["body"]) == b"\x89PNG"
        assert png["headers"]["content-type"] == "image/png"
        text = await self.web(daemon, principal, "/text")
        assert (text["status"], text["body"]) == (404, "plain")
        boom = await self.web(daemon, principal, "/boom")
        assert boom["status"] == 500 and "secret" not in boom["body"]

    def test_page_registration_rules(self):
        plugin = Plugin(name="demo", version="1")
        with pytest.raises(ValueError):
            plugin.page("relative")

        @plugin.page("")
        async def home(ctx, request):
            return Text("x")

        with pytest.raises(ValueError, match="already registered"):
            plugin.page("/")(home)


class TestEventsAndPurge:
    async def test_events_reach_handlers(self, make_daemon):
        plugin = Plugin(name="demo", version="1")
        seen = []

        @plugin.on("connector.state_changed")
        async def changed(ctx, params):
            seen.append((params["connector"], params["state"], ctx.principal.id))

        @plugin.on("plugin.disabling")
        async def disabling(ctx, params):
            raise RuntimeError("a failing handler must not break the runner")

        daemon = await make_daemon(plugin)
        await daemon.initialize()
        daemon.notify("connector.state_changed", {"connector": "drive", "state": "signed_out", "principal": "local"})
        daemon.notify("plugin.disabling", {"reason": "user"})
        daemon.notify("unknown.event", {})
        await daemon.result("storage.purge", {"scope": "all"})  # a round trip lets the notifications run
        assert seen == [("drive", "signed_out", "local")]
        await daemon.stop()

    def test_unknown_event_name(self):
        with pytest.raises(ValueError, match="unknown event"):
            Plugin(name="demo", version="1").on("nope")

    async def test_purge(self, make_daemon):
        plugin = Plugin(name="demo", version="1")
        calls = []

        @plugin.on_purge
        async def purge(ctx, scope, principal):
            calls.append((scope, principal))

        with pytest.raises(ValueError):
            plugin.on_purge(purge)
        daemon = await make_daemon(plugin)
        await daemon.initialize()
        assert await daemon.result("storage.purge", {"scope": "principal", "principal": "local"}) == {"purged": True}
        assert (await daemon.error("storage.purge", {"scope": "principal"}))["code"] == "invalid_params"
        assert (await daemon.error("storage.purge", {"scope": "bogus"}))["code"] == "invalid_params"
        assert calls == [("principal", "local")]
        await daemon.stop()

    async def test_purge_without_a_handler_acknowledges(self, daemon):
        await daemon.initialize()
        assert await daemon.result("storage.purge", {"scope": "install"}) == {"purged": True}

    async def test_shutdown_notification_ends_the_runner(self, make_daemon):
        plugin = Plugin(name="demo", version="1")
        seen = []

        @plugin.on("shutdown")
        async def bye(ctx, params):
            seen.append(params["grace_ms"])

        daemon = await make_daemon(plugin)
        await daemon.initialize()
        daemon.notify("shutdown", {"grace_ms": 5000})
        await daemon._task  # returns without end of input
        assert seen == [5000]

    async def test_end_of_input_ends_the_runner(self, make_daemon):
        daemon = await make_daemon(Plugin(name="demo", version="1"))
        await daemon.stop()


class TestSourceAndConfirm:
    async def test_call_and_error_mapping(self, daemon, principal):
        await daemon.initialize()

        def handler(message):
            params = message["params"]
            if params["operation"] == "jira.search":
                return {"result": {"operation": "jira.search", "data": [1], "bytes": 3, "next_cursor": None}}
            return {"error": {"code": -32005, "message": "upstream_error", "data": {
                "code": "upstream_error", "detail": "nope", "retryable": True, "reason": "revision_changed"}}}

        daemon.source_handler = handler
        ctx = daemon.plugin._ctx(principal)
        result = await ctx.source.call("jira.search", jql="x")
        assert (result.data, result.bytes, result.next_cursor) == ([1], 3, None)
        sent = [m for m in daemon.incoming if m["method"] == "source.call"][0]["params"]
        assert sent == {"principal": "local", "operation": "jira.search", "params": {"jql": "x"}}
        with pytest.raises(SourceError) as info:
            await ctx.source.call("drive.download", file_id="f")
        assert (info.value.code, info.value.detail, info.value.reason) == ("upstream_error", "nope", "revision_changed")

    async def test_confirm(self, daemon, principal):
        await daemon.initialize()

        def handler(message):
            if message["method"] == "confirm.request":
                return {"result": {"approval_id": "a1", "expires_at": "later"}}
            return {"result": {"status": "approved", "decided_at": "now"}}

        daemon.source_handler = handler
        ctx = daemon.plugin._ctx(principal)
        approval_id = await ctx.confirm.request("export", "Export?", [blocks.text("rows")])
        assert approval_id == "a1"
        sent = [m for m in daemon.incoming if m["method"] == "confirm.request"][0]["params"]
        assert sent["require_step_up"] is True and sent["principal"] == "local"
        outcome = await ctx.confirm.await_("a1", timeout_ms=10**9)
        assert (outcome.status, outcome.decided_at) == ("approved", "now")
        assert [m for m in daemon.incoming if m["method"] == "confirm.await"][0]["params"]["timeout_ms"] == 300_000
        with pytest.raises(SourceError) as info:
            await ctx.confirm.request("export", "Export?", [{"type": "html"}])
        assert info.value.code == "invalid_blocks"

    async def test_confirm_wait_retries_timeouts(self, daemon, principal):
        await daemon.initialize()
        answers = [{"error": {"code": -32013, "message": "timeout", "data": {"code": "timeout", "detail": "still pending", "retryable": True}}}, {"error": {"code": -32013, "message": "timeout", "data": {"code": "timeout", "detail": "still pending", "retryable": True}}}, {"result": {"status": "approved", "decided_at": "now"}}]
        daemon.source_handler = lambda message: answers.pop(0)
        ctx = daemon.plugin._ctx(principal)
        outcome = await ctx.confirm.wait("a1")
        assert outcome.status == "approved"
        assert [m["method"] for m in daemon.incoming].count("confirm.await") == 3

    async def test_confirm_wait_raises_other_errors(self, daemon, principal):
        await daemon.initialize()
        daemon.source_handler = lambda message: {"error": {"code": -32005, "message": "upstream_error", "data": {
            "code": "upstream_error", "detail": "nope", "retryable": False}}}
        ctx = daemon.plugin._ctx(principal)
        with pytest.raises(SourceError) as info:
            await ctx.confirm.wait("a1")
        assert info.value.code == "upstream_error"
        assert [m["method"] for m in daemon.incoming].count("confirm.await") == 1

    async def test_tools_changed(self, daemon):
        await daemon.initialize()

        @daemon.plugin.tool("later", description="d", read_only=True)
        async def later(ctx, args):
            return Prepared(preview=[], payload=[])

        await daemon.plugin.tools_changed()
        for _ in range(20):
            if any(m["method"] == "tools.changed" for m in daemon.incoming):
                break
            await __import__("asyncio").sleep(0.01)
        sent = [m for m in daemon.incoming if m["method"] == "tools.changed"][0]["params"]["tools"]
        assert {t["name"] for t in sent} == {"list_events", "rename", "later"}

    async def test_tools_changed_needs_a_connection(self):
        with pytest.raises(RuntimeError):
            await Plugin(name="demo", version="1").tools_changed()


def chunk_handler(chunks, *, change_on_first_attempt=False):
    """A daemon-side drive.download: serves ``chunks`` with cursors; optionally changes revision once."""
    state = {"attempt": 0}

    def handler(message):
        params = message["params"]["params"]
        cursor = params.get("cursor")
        index = int(cursor.split(":")[1]) if cursor else 0
        if not cursor:
            state["attempt"] += 1
        revision = "r1" if state["attempt"] == 1 and change_on_first_attempt else "r2"
        if change_on_first_attempt and state["attempt"] == 1 and index == 1:
            return {"error": {"code": -32005, "message": "upstream_error", "data": {
                "code": "upstream_error", "detail": "changed", "retryable": False, "reason": "revision_changed"}}}
        last = index == len(chunks) - 1
        data = {"file_id": params["file_id"], "mime_type": "text/csv", "revision": revision,
                "total_size_bytes": sum(map(len, chunks)), "offset": 0, "length": len(chunks[index]),
                "eof": last, "content_base64": base64.b64encode(chunks[index]).decode()}
        return {"result": {"operation": "drive.download", "data": data, "bytes": 1,
                           "next_cursor": None if last else f"c:{index + 1}"}}

    return handler, state


class TestDownload:
    async def test_cursor_loop_writes_to_the_default_directory(self, daemon, principal, tmp_path):
        await daemon.initialize()
        daemon.source_handler, _ = chunk_handler([b"aaa", b"bbb", b"cc"])
        ctx = daemon.plugin._ctx(principal)
        file = await ctx.source.download("file/1")
        assert file.path == tmp_path / "downloads" / "file_1"
        assert file.path.read_bytes() == b"aaabbbcc"
        assert (file.size, file.revision, file.mime_type) == (8, "r2", "text/csv")
        assert not (tmp_path / "downloads" / "file_1.part").exists()
        sent = [m["params"]["params"] for m in daemon.incoming if m["method"] == "source.call"]
        assert sent == [{"file_id": "file/1"}, {"file_id": "file/1", "cursor": "c:1"},
                        {"file_id": "file/1", "cursor": "c:2"}]

    async def test_one_restart_on_a_revision_change(self, daemon, principal, tmp_path):
        await daemon.initialize()
        daemon.source_handler, state = chunk_handler([b"old", b"old"], change_on_first_attempt=True)
        dest = tmp_path / "elsewhere"
        file = await daemon.plugin._ctx(principal).source.download("f", dest)
        assert state["attempt"] == 2
        assert file.path == dest / "f" and file.path.read_bytes() == b"oldold"
        assert file.revision == "r2"

    async def test_second_revision_change_fails_and_cleans_up(self, daemon, principal, tmp_path):
        await daemon.initialize()

        def always_changing(message):
            return {"error": {"code": -32005, "message": "upstream_error", "data": {
                "code": "upstream_error", "detail": "changed", "retryable": False, "reason": "revision_changed"}}}

        daemon.source_handler = always_changing
        with pytest.raises(SourceError) as info:
            await daemon.plugin._ctx(principal).source.download("f")
        assert info.value.reason == "revision_changed"
        assert list((tmp_path / "downloads").iterdir()) == []
        calls = [m for m in daemon.incoming if m["method"] == "source.call"]
        assert len(calls) == 2

    async def test_other_errors_are_not_retried(self, daemon, principal):
        await daemon.initialize()
        daemon.source_handler = lambda m: {"error": {"code": -32004, "message": "payload_too_large", "data": {
            "code": "payload_too_large", "detail": "", "retryable": False}}}
        with pytest.raises(SourceError) as info:
            await daemon.plugin._ctx(principal).source.download("f")
        assert info.value.code == "payload_too_large"
        assert len([m for m in daemon.incoming if m["method"] == "source.call"]) == 1

    async def test_malformed_results(self, daemon, principal):
        await daemon.initialize()
        ctx = daemon.plugin._ctx(principal)

        def result(data, cursor=None):
            return lambda m: {"result": {"operation": "drive.download", "data": data, "bytes": 0, "next_cursor": cursor}}

        for handler in (result("not a dict"), result({"content_base64": "!!!", "eof": True}),
                        result({"content_base64": "", "eof": False})):
            daemon.source_handler = handler
            with pytest.raises(SourceError) as info:
                await ctx.source.download("f")
            assert info.value.code == "internal_error"


def paged_handler(pages):
    """A daemon-side paged operation: serves ``pages`` for the cursor each page returned."""
    def handler(message):
        params = message["params"]["params"]
        index = int(params["cursor"].split(":")[1]) if "cursor" in params else 0
        last = index == len(pages) - 1
        return {"result": {"operation": message["params"]["operation"], "data": pages[index], "bytes": 1,
                           "next_cursor": None if last else f"p:{index + 1}"}}
    return handler


class TestSourcePaging:
    async def test_pages_iterate_until_null_and_pass_the_cursor(self, daemon, principal):
        await daemon.initialize()
        daemon.source_handler = paged_handler([[1, 2], [3], [4]])
        ctx = daemon.plugin._ctx(principal)
        seen = [page.data async for page in ctx.source.pages("jira.search", jql="x", page_size=2)]
        assert seen == [[1, 2], [3], [4]]
        sent = [m["params"]["params"] for m in daemon.incoming if m["method"] == "source.call"]
        assert sent == [{"jql": "x", "page_size": 2}, {"jql": "x", "page_size": 2, "cursor": "p:1"},
                        {"jql": "x", "page_size": 2, "cursor": "p:2"}]

    async def test_pages_refuse_max_results_and_cursor(self, daemon, principal):
        await daemon.initialize()
        ctx = daemon.plugin._ctx(principal)
        for name in ("max_results", "cursor"):
            with pytest.raises(ValueError):
                async for _ in ctx.source.pages("jira.search", **{name: 5}):
                    pass

    async def test_collect_concatenates(self, daemon, principal):
        await daemon.initialize()
        daemon.source_handler = paged_handler([[1, 2], [3]])
        ctx = daemon.plugin._ctx(principal)
        assert await ctx.source.collect("calendar.list_events", time_min="a") == [1, 2, 3]

    async def test_collect_refuses_sheets(self, daemon, principal):
        await daemon.initialize()
        ctx = daemon.plugin._ctx(principal)
        with pytest.raises(ValueError, match="collect supports only"):
            await ctx.source.collect("sheets.get_values", spreadsheet_id="s")
        assert not [m for m in daemon.incoming if m["method"] == "source.call"]


class TestApprovals:
    def test_digest_of_str_and_bytes(self):
        import hashlib
        from privacyfence_plugin_sdk.plugin import ApprovalsClient
        expected = "sha256:" + hashlib.sha256("héllo".encode()).hexdigest()
        assert ApprovalsClient.digest("héllo") == expected
        assert ApprovalsClient.digest("héllo".encode()) == expected

    async def test_wire_messages(self, daemon, principal):
        await daemon.initialize()

        def handler(message):
            if message["method"] == "approval.request":
                return {"result": {"approval_id": "a1", "status": "pending"}}
            if message["method"] == "approval.check":
                return {"result": {"status": "approved", "approval_id": "a1"}}
            return {"result": {"status": "approved", "decided_at": "now"}}

        daemon.source_handler = handler
        ctx = daemon.plugin._ctx(principal)
        digest = ctx.approvals.digest("body")
        ticket = await ctx.approvals.request(
            "template", "t/1", "body", "Approve", [blocks.text("hi")], page="/approval")
        assert (ticket.approval_id, ticket.status) == ("a1", "pending")
        assert await ctx.approvals.check("template", "t/1", b"body") == "approved"
        outcome = await ctx.approvals.await_("a1", timeout_ms=10**9)
        assert (outcome.status, outcome.decided_at) == ("approved", "now")
        sent = {m["method"]: m["params"] for m in daemon.incoming if m["method"].startswith("approval.")}
        assert sent["approval.request"] == {
            "principal": "local", "kind": "template", "subject_id": "t/1", "digest": digest, "title": "Approve",
            "preview": [blocks.text("hi")], "require_step_up": True, "page": "/approval"}
        assert sent["approval.check"] == {
            "principal": "local", "kind": "template", "subject_id": "t/1", "digest": digest}
        assert sent["approval.await"] == {"approval_id": "a1", "timeout_ms": 300_000}

    async def test_approvals_wait_retries_timeouts(self, daemon, principal):
        await daemon.initialize()
        answers = [{"error": {"code": -32013, "message": "timeout", "data": {"code": "timeout", "detail": "still pending", "retryable": True}}}, {"error": {"code": -32013, "message": "timeout", "data": {"code": "timeout", "detail": "still pending", "retryable": True}}}, {"result": {"status": "approved", "decided_at": "now"}}]
        daemon.source_handler = lambda message: answers.pop(0)
        ctx = daemon.plugin._ctx(principal)
        outcome = await ctx.approvals.wait("a1")
        assert outcome.status == "approved"
        assert [m["method"] for m in daemon.incoming].count("approval.await") == 3

    async def test_approvals_wait_raises_other_errors(self, daemon, principal):
        await daemon.initialize()
        daemon.source_handler = lambda message: {"error": {"code": -32005, "message": "upstream_error", "data": {
            "code": "upstream_error", "detail": "nope", "retryable": False}}}
        ctx = daemon.plugin._ctx(principal)
        with pytest.raises(SourceError) as info:
            await ctx.approvals.wait("a1")
        assert info.value.code == "upstream_error"
        assert [m["method"] for m in daemon.incoming].count("approval.await") == 1

    async def test_invalid_blocks(self, daemon, principal):
        await daemon.initialize()
        ctx = daemon.plugin._ctx(principal)
        with pytest.raises(SourceError) as info:
            await ctx.approvals.request("k", "s", "c", "t", [{"type": "html"}])
        assert info.value.code == "invalid_blocks"


class TestOutputs:
    def ctx(self, daemon, principal, tmp_path, **extra):
        out = tmp_path / "out"
        out.mkdir()
        return daemon.plugin._ctx({**principal, "output_dir": str(out), "output_types": ["text/csv"], **extra}), out

    async def test_atomic_publish_leaves_no_tmp(self, daemon, principal, tmp_path):
        await daemon.initialize()
        ctx, out = self.ctx(daemon, principal, tmp_path)
        assert ctx.outputs.dir == out
        assert ctx.outputs.publish("reports/a.csv", "x,y\n") == "reports/a.csv"
        assert ctx.outputs.publish("b.csv", b"1") == "b.csv"
        assert (out / "reports" / "a.csv").read_text() == "x,y\n"
        assert not [p for p in out.rglob("*") if p.name.endswith(".tmp")]

    async def test_existing_path_refused(self, daemon, principal, tmp_path):
        await daemon.initialize()
        ctx, out = self.ctx(daemon, principal, tmp_path)
        ctx.outputs.publish("a.csv", "1")
        with pytest.raises(FileExistsError):
            ctx.outputs.publish("a.csv", "2")
        assert (out / "a.csv").read_text() == "1"

    @pytest.mark.parametrize("path", ["../a.csv", "a/../b.csv", "/abs.csv", ".hidden.csv", "a//b.csv", "a\\b.csv"])
    async def test_unsafe_paths_refused(self, daemon, principal, tmp_path, path):
        await daemon.initialize()
        ctx, out = self.ctx(daemon, principal, tmp_path)
        with pytest.raises(ValueError):
            ctx.outputs.publish(path, "x")
        assert not list(out.iterdir())
        assert not (tmp_path / "a.csv").exists()

    async def test_wrong_extension_refused(self, daemon, principal, tmp_path):
        await daemon.initialize()
        ctx, out = self.ctx(daemon, principal, tmp_path)
        for name in ("a.json", "a.exe", "noext"):
            with pytest.raises(ValueError, match="extension"):
                ctx.outputs.publish(name, "x")
        assert not list(out.iterdir())

    async def test_no_output_dir(self, daemon, principal):
        await daemon.initialize()
        ctx = daemon.plugin._ctx(principal)
        with pytest.raises(RuntimeError, match="no output folder"):
            ctx.outputs.publish("a.csv", "x")
        with pytest.raises(RuntimeError, match="no output folder"):
            _ = ctx.outputs.dir


class TestRevokedEvent:
    async def test_revoked_reaches_handlers(self, make_daemon):
        plugin = Plugin(name="demo", version="1")
        seen = []

        @plugin.on("approval.revoked")
        async def revoked(ctx, params):
            seen.append((params["approval_id"], ctx.principal.id))

        daemon = await make_daemon(plugin)
        await daemon.initialize()
        daemon.notify("approval.revoked", {"approval_id": "a1", "principal": "local"})
        await daemon.result("storage.purge", {"scope": "all"})
        assert seen == [("a1", "local")]
        await daemon.stop()
