"""The public test host: gate simulation, scope rules, source fixtures and samples."""
from __future__ import annotations

import base64
import json

import pytest

from privacyfence.plugins import constants
from privacyfence_plugin_sdk import Plugin, Prepared, ToolDefinitionError, blocks
from privacyfence_plugin_sdk.testing import PluginTestHost, SourceFixtureMissing, samples
from privacyfence_plugin_sdk.testing import _host as host_module
from privacyfence_plugin_sdk.testing import _source as source_module


def build_plugin() -> tuple[Plugin, dict]:
    """A small plugin with a review read, a popup write, a source read and a download."""
    seen = {"executed": 0}
    plugin = Plugin(name="demo", version="1.0.0")
    plugin.scope_type("calendar", "Calendar id a call reads")

    @plugin.tool(
        "list_events", description="List events.", read_only=True, scopes=["calendar"],
        params={"calendar_id": {"type": "string", "description": "Which calendar"}},
    )
    async def list_events(ctx, args):
        calendars = [c for c in args.get("calendar_id", "primary").split(",") if c]
        return Prepared(
            preview=[blocks.fields({"Calendars": ", ".join(calendars)})],
            payload=[blocks.text("three events")],
            scopes={"calendar": calendars},
        )

    @plugin.tool("rename", description="Rename a thing.", gate="popup", params={"name": {"type": "string"}},
                 required=["name"], effect="Renames the thing.")
    async def rename(ctx, args):
        return Prepared(preview=[blocks.text(f"rename to {args['name']}")], state=args["name"])

    @rename.execute
    async def do_rename(ctx, prepared, approval):
        seen["executed"] += 1
        return {"renamed": prepared.state, "via": approval["via"]}

    @plugin.tool("agenda", description="Today's events from the calendar.", read_only=True, scopes=["calendar"])
    async def agenda(ctx, args):
        result = await ctx.source.call("calendar.list_events", time_min="2026-10-07T00:00:00Z",
                                       time_max="2026-10-08T00:00:00Z")
        titles = [e["title"] for e in result.data]
        return Prepared(preview=[blocks.fields({"Events": len(titles)})],
                        payload=[blocks.text(", ".join(titles))], scopes={"calendar": ["primary"]})

    @plugin.tool("fetch", description="Download a file.", read_only=True, scopes=["calendar"],
                 params={"file_id": {"type": "string"}}, required=["file_id"])
    async def fetch(ctx, args):
        downloaded = await ctx.source.download(args["file_id"])
        seen["downloaded"] = downloaded.path.read_bytes()
        seen["revision"] = downloaded.revision
        return Prepared(preview=[blocks.text("fetch")], payload=[blocks.text(str(downloaded.size))],
                        scopes={"calendar": ["primary"]})

    plugin.seen = seen  # type: ignore[attr-defined]
    return plugin, seen


def auto_plugin(read_only: bool) -> Plugin:
    plugin = Plugin(name="quick", version="1.0.0")

    @plugin.tool("peek", description="Peek.", gate="auto", read_only=read_only)
    async def peek(ctx, args):
        return Prepared(preview=[blocks.text("peek")], payload=[blocks.text("seen")] if read_only else None)

    if not read_only:
        @peek.execute
        async def run(ctx, prepared, approval):
            return {"via": approval["via"]}

    return plugin


class TestPluginTestHost:
    async def test_review_read_released_equals_card_payload(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            outcome = await host.call_tool("list_events", {"calendar_id": "primary", "reason": "plan"})
        assert outcome.error is None
        assert (outcome.gate, outcome.card_shown) == ("review", True)
        assert outcome.card.payload == [{"type": "text", "text": "three events"}]
        assert outcome.card.scopes == {"calendar": ["primary"]}
        assert outcome.released == {"blocks": outcome.card.payload}
        assert outcome.approval["via"] == "card" and outcome.approval["approval_id"].startswith("card-")
        assert [e["decision"] for e in outcome.audit] == ["approved"]

    async def test_deny_releases_nothing_and_never_executes(self):
        plugin, seen = build_plugin()
        async with PluginTestHost(plugin) as host:
            outcome = await host.call_tool("rename", {"name": "x"}, decide="deny")
            read = await host.call_tool("list_events", decide=lambda card: "deny")
        assert outcome.card_shown and outcome.released is None and outcome.error["code"] == "denied"
        assert read.released is None and seen["executed"] == 0
        assert outcome.audit[0]["decision"] == "denied"

    async def test_decide_function_sees_the_card(self):
        plugin, _ = build_plugin()
        shown = []

        async def decide(card):
            shown.append(card)
            return True

        async with PluginTestHost(plugin) as host:
            outcome = await host.call_tool("list_events", decide=decide)
        assert shown == [outcome.card] and outcome.released is not None

    async def test_scope_rule_skips_card(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            host.rules.allow_scope("calendar", ["primary", "team"])
            outcome = await host.call_tool("list_events", {"calendar_id": "primary,team"}, decide="deny")
        assert not outcome.card_shown
        assert outcome.approval["via"] == "rule"
        assert outcome.released == {"blocks": [{"type": "text", "text": "three events"}]}
        assert outcome.audit[0]["auto_accept_rule"] == "plugin:demo:calendar"

    async def test_scope_rule_mismatch_shows_card(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            host.rules.allow_scope("calendar", ["primary"])
            extra = await host.call_tool("list_events", {"calendar_id": "primary,other"}, decide="deny")
            other = await host.call_tool("list_events", {"calendar_id": "other"}, decide="deny")
            host.rules.clear()
            host.rules.allow_scope("calendar", [])
            nothing = await host.call_tool("list_events", {"calendar_id": "primary"}, decide="deny")
            with pytest.raises(ValueError, match="no scope type"):
                host.rules.allow_scope("nope", ["x"])
        assert extra.card_shown and other.card_shown and nothing.card_shown

    async def test_scope_rule_ignores_destructive_and_other_tools(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            host.rules.allow_scope("calendar", ["primary"])
            write = await host.call_tool("rename", {"name": "x"}, decide="deny")
        assert write.card_shown and write.error["code"] == "denied"

    async def test_source_fixture_missing_raises(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            with pytest.raises(SourceFixtureMissing, match="calendar.list_events"):
                await host.call_tool("agenda")
            assert [c.operation for c in host.source.calls] == ["calendar.list_events"]
            host.source.load(samples.get("calendar.list_events"))
            outcome = await host.call_tool("agenda")
        assert outcome.released == {"blocks": [{"type": "text", "text": "Example meeting"}]}
        assert [e["decision"] for e in outcome.audit] == ["plugin_source", "approved"]
        assert outcome.audit[0]["connector"] == "plugin:demo"

    async def test_source_when_fail_and_specificity(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            host.source.when("calendar.list_events").returns([{"title": "Any"}])
            host.source.when("calendar.list_events", time_min="2026-10-07T00:00:00Z").returns([{"title": "Specific"}])
            specific = await host.call_tool("agenda")
            host.source.fail("calendar.list_events", "upstream_error", reason="rate_limited", time_min="2026-10-07T00:00:00Z")
            failed = await host.call_tool("agenda")
        assert specific.released["blocks"][0]["text"] == "Specific"
        assert failed.error["code"] == "internal_error"  # the plugin let the SourceError escape

    async def test_source_refuses_org_fields_and_unknown_principals(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            for params, code in [
                ({"principal": "local", "operation": "jira.search", "params": {}, "credential": "x"}, "org_only_field"),
                ({"principal": "other", "operation": "jira.search", "params": {}}, "unknown_principal"),
                ({"principal": "local", "operation": "slack.read", "params": {}}, "operation_not_allowed"),
            ]:
                with pytest.raises(Exception) as caught:
                    await host._handle_source(params)
                assert caught.value.code == code

    async def test_tool_floor_violation_raises(self):
        plugin = auto_plugin(read_only=False)
        with pytest.raises(ToolDefinitionError, match="needs max_gate_floor: auto"):
            async with PluginTestHost(plugin):
                pytest.fail("the host must not start")
        plugin, _ = build_plugin()
        plugin._reg.tools["agenda"].definition["parameters"]["properties"]["tags"] = {"type": "array"}
        with pytest.raises(ToolDefinitionError, match="only string, integer, number and boolean"):
            async with PluginTestHost(plugin):
                pytest.fail("the host must not start")

    async def test_auto_read_needs_floor(self):
        with pytest.raises(ToolDefinitionError, match="auto"):
            async with PluginTestHost(auto_plugin(read_only=True)):
                pytest.fail("the host must not start")
        async with PluginTestHost(auto_plugin(read_only=True), max_gate_floor="auto") as host:
            outcome = await host.call_tool("peek", decide="deny")
        assert not outcome.card_shown and outcome.approval["via"] == "auto"
        assert outcome.released == {"blocks": [{"type": "text", "text": "seen"}]}
        assert outcome.audit[0]["decision"] == "auto_accepted"

    async def test_write_executes_once(self):
        plugin, seen = build_plugin()
        async with PluginTestHost(plugin) as host:
            outcome = await host.call_tool("rename", {"name": "new"})
            assert seen["executed"] == 1
            again = await host.call_tool("rename", {"name": "new"})
        assert outcome.card.payload is None
        assert outcome.result == {"renamed": "new", "via": "card"} == outcome.released
        assert seen["executed"] == 2 and again.card_shown  # a second call is a new card, not a replay

    async def test_unknown_tool_and_missing_argument(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            assert (await host.call_tool("nope")).error["code"] == "unknown_tool"
            assert (await host.call_tool("rename")).error["code"] == "invalid_params"
            with pytest.raises(ValueError, match="unknown principal"):
                await host.call_tool("list_events", principal="x")

    async def test_invalid_preview_is_refused(self):
        plugin = Plugin(name="bad", version="1.0.0")

        @plugin.tool("greedy", description="Needs a scope.", read_only=True)
        async def greedy(ctx, args):
            return Prepared(preview=[{"type": "text", "text": "ok"}], payload=[{"type": "text", "text": "x"}])

        plugin.tool_definitions()
        async with PluginTestHost(plugin) as host:
            host._tools[0]["scopes"] = ["calendar"]
            outcome = await host.call_tool("greedy")
        assert outcome.error["code"] == "invalid_preview" and outcome.released is None

    async def test_hooks_for_the_next_part_raise(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            with pytest.raises(NotImplementedError):
                await host._handle_confirm_request({})
            with pytest.raises(NotImplementedError):
                await host._handle_web("GET", "/", {})

    async def test_drive_download_sample_chunks(self):
        plugin, seen = build_plugin()
        chunk = constants.DRIVE_CHUNK_BYTES
        data = bytes(range(256)) * (chunk // 256) + b"tail" * 1000 + bytes(7)
        async with PluginTestHost(plugin) as host:
            host.source.load(samples.drive_download(data, revision="r1"))
            outcome = await host.call_tool("fetch", {"file_id": "EXAMPLE-1"})
            assert outcome.error is None
            calls = [c.params for c in host.source.calls]
        assert seen["downloaded"] == data and seen["revision"] == "r1"
        assert len(calls) == 2 and "cursor" not in calls[0]
        cursor = json.loads(base64.urlsafe_b64decode(calls[1]["cursor"]))
        assert cursor == {"f": "EXAMPLE-1", "r": "r1", "o": chunk}

    def test_drive_download_sample_matches_the_daemons_chunk_shape(self):
        fixtures = source_module.SourceFixtures()
        fixtures.load(samples.drive_download(b"abcdef", revision="r2", file_id="EXAMPLE-9", mime_type="text/plain"))

        def serve(**params):
            return fixtures._serve({"principal": "local", "operation": "drive.download",
                                    "params": {"file_id": "EXAMPLE-9", **params}}, "local")

        first = serve(length=4)
        assert first["data"] == {
            "file_id": "EXAMPLE-9", "mime_type": "text/plain", "revision": "r2", "total_size_bytes": 6,
            "offset": 0, "length": 4, "eof": False, "content_base64": base64.b64encode(b"abcd").decode(),
        }
        assert first["next_cursor"] == source_module.encode_cursor("EXAMPLE-9", "r2", 4)
        last = serve(cursor=first["next_cursor"])
        assert last["data"]["eof"] is True and last["next_cursor"] is None
        assert base64.b64decode(last["data"]["content_base64"]) == b"ef"
        assert serve(offset=2, length=1)["data"]["offset"] == 2

        fixtures.load(samples.drive_download(b"abcdef", revision="r3", file_id="EXAMPLE-9"))
        with pytest.raises(Exception) as changed:
            serve(cursor=first["next_cursor"])
        assert changed.value.code == "upstream_error" and changed.value.extra == {"reason": "revision_changed"}
        for params in ({"offset": 1, "cursor": first["next_cursor"]}, {"cursor": "!!"}, {"length": 0}, {"offset": 99}):
            with pytest.raises(Exception) as bad:
                serve(**params)
            assert bad.value.code == "invalid_params"

    @pytest.mark.parametrize("operation", constants.SOURCE_OPERATIONS)
    def test_every_operation_has_a_sample(self, operation):
        fixture = samples.get(operation)
        assert fixture["operation"] == operation and "data" in fixture
        assert samples.get(operation) is not fixture
        fixtures = source_module.SourceFixtures()
        fixtures.load(fixture)
        reply = fixtures._serve({"principal": "local", "operation": operation, "params": fixture["params"]}, "local")
        assert reply["data"] == fixture["data"] and reply["bytes"] == len(json.dumps(fixture["data"]))

    def test_sample_data_shapes(self):
        assert set(samples.get("sheets.get_values")["data"]) == {"values"}
        assert set(samples.get("drive.download")["data"]) == {
            "file_id", "mime_type", "revision", "total_size_bytes", "offset", "length", "eof", "content_base64"}
        with pytest.raises(ValueError, match="unknown source operation"):
            samples.get("slack.read")

    def test_limits_match_the_daemons_constants(self):
        for name in ("MAX_LINE_BYTES", "MAX_IN_FLIGHT", "INVALID_LINES_LIMIT", "INLINE_RESULT_BYTES", "MAX_TITLE_CHARS",
                     "MAX_EFFECT_CHARS", "MAX_DESCRIPTION_CHARS", "MAX_TOOLS", "MAX_SCOPE_VALUES",
                     "MAX_SCOPE_VALUE_CHARS", "MCP_TOOL_NAME_MAX"):
            assert getattr(host_module, "_" + name) == getattr(constants, name), name
        assert host_module._MAX_SCOPE_TYPES == 20
        for name in ("SOURCE_OPERATIONS", "DRIVE_CHUNK_BYTES", "DRIVE_MAX_FILE_BYTES", "MAX_SOURCE_RESULT_BYTES"):
            assert getattr(source_module, name) == getattr(constants, name), name
        assert host_module._GATES == constants.GATES
        assert {k: host_module._TIMEOUTS[k] for k in host_module._TIMEOUTS} == {
            k: constants.TIMEOUT_SECONDS[k] for k in host_module._TIMEOUTS}
        for pattern in ("TOOL_NAME_RE", "SCOPE_TYPE_RE"):
            assert getattr(host_module, "_" + pattern).pattern == getattr(constants, pattern).pattern

    def test_constructor_validation(self):
        plugin, _ = build_plugin()
        with pytest.raises(ValueError):
            PluginTestHost(plugin, mode="x")
        with pytest.raises(ValueError):
            PluginTestHost(plugin, max_gate_floor="popup")
        with pytest.raises(ValueError):
            PluginTestHost(plugin, principals=[{"id": "alice"}])
        PluginTestHost(plugin, mode="org", principals=[{"id": "alice", "display_name": "Alice Example"}])
