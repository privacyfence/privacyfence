"""The public test host: gate simulation, scope rules, source fixtures and samples."""
from __future__ import annotations

import base64
import json

import pytest

from privacyfence.plugins import constants, cursors
from privacyfence_plugin_sdk._rpc import RpcError
from privacyfence_plugin_sdk import Html, Plugin, Prepared, ToolDefinitionError, blocks
from privacyfence_plugin_sdk.testing import PluginTestHost, SourceFixtureMissing, samples
from privacyfence_plugin_sdk.testing import _host as host_module
from privacyfence_plugin_sdk.testing import _source as source_module
from privacyfence_plugin_sdk.testing import _approvals as approvals_module
from privacyfence_plugin_sdk.testing import _cursors as cursors_module
from privacyfence_plugin_sdk.testing import _outputs as outputs_module


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
        async with PluginTestHost(plugin, source_operations=("calendar.list_events",)) as host:
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
        async with PluginTestHost(plugin, source_operations=("calendar.list_events",)) as host:
            host.source.when("calendar.list_events").returns([{"title": "Any"}])
            host.source.when("calendar.list_events", time_min="2026-10-07T00:00:00Z").returns([{"title": "Specific"}])
            specific = await host.call_tool("agenda")
            host.source.fail("calendar.list_events", "upstream_error", reason="rate_limited", time_min="2026-10-07T00:00:00Z")
            failed = await host.call_tool("agenda")
        assert specific.released["blocks"][0]["text"] == "Specific"
        # The plugin let the SourceError escape: its code is passed through, and the host shows the
        # fixed sentence for it.
        assert failed.error["code"] == "upstream_error"
        assert failed.error["detail"] == "A service this plugin reads from returned an error."

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

    async def test_drive_download_sample_chunks(self):
        plugin, seen = build_plugin()
        chunk = constants.DRIVE_CHUNK_BYTES
        data = bytes(range(256)) * (chunk // 256) + b"tail" * 1000 + bytes(7)
        async with PluginTestHost(plugin, source_operations=("drive.download",)) as host:
            host.source.load(samples.drive_download(data, revision="r1"))
            outcome = await host.call_tool("fetch", {"file_id": "EXAMPLE-1"})
            assert outcome.error is None
            calls = [c.params for c in host.source.calls]
        assert seen["downloaded"] == data and seen["revision"] == "r1"
        assert len(calls) == 2 and "cursor" not in calls[0]
        assert cursors.decode(calls[1]["cursor"], "drive.download", {"file_id": "EXAMPLE-1"}) == {"r": "r1", "o": chunk}

    async def test_an_operation_outside_source_operations_is_refused(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin, source_operations=("jira.search",)) as host:
            host.source.load(samples.get("calendar.list_events"))
            with pytest.raises(RpcError) as refused:
                host.source._serve({"principal": "local", "operation": "calendar.list_events", "params": {}}, "local")
            outcome = await host.call_tool("agenda")
        assert (refused.value.code, refused.value.detail) == (
            "operation_not_allowed", "the plugin may not use this operation")
        assert outcome.error["code"] == "operation_not_allowed" and outcome.released is None

    def test_an_unknown_source_operation_is_a_value_error(self):
        plugin, _ = build_plugin()
        with pytest.raises(ValueError, match="unknown source operation 'gmail.list'"):
            PluginTestHost(plugin, source_operations=("gmail.list",))

    async def test_pages_off_is_a_404_that_never_reaches_the_plugin(self):
        plugin = Plugin(name="paged", version="1.0.0")
        ran = []

        @plugin.page("/")
        async def home(ctx, request):
            ran.append(request)
            return Html("hi")

        async with PluginTestHost(plugin) as host:
            response = await host.get("/")
            bad = await host.get("/a/../b")
        assert (response.status, response.body) == (404, b"Not Found")
        assert response.headers["x-frame-options"] == "DENY" and "content-security-policy" in response.headers
        assert bad.status == 400 and ran == []

    async def test_an_approval_page_needs_pages(self):
        plugin = Plugin(name="approver", version="1.0.0")

        @plugin.page("/approval")
        async def approval_page(ctx, request):
            return Html("approve")

        async with PluginTestHost(plugin) as host:
            with pytest.raises(RpcError, match="pages: true") as refused:
                await host._approvals.request({
                    "principal": "local", "kind": "template", "subject_id": "a", "digest": "sha256:" + "0" * 64,
                    "title": "T", "preview": [blocks.text("p")], "page": "/approval"})
            assert refused.value.code == "invalid_params"

    async def test_a_google_form_is_not_downloadable(self):
        form = "application/vnd.google-apps.form"
        fixtures = source_module.SourceFixtures()
        fixtures.load(samples.drive_download(b"x", mime_type=form))
        fixtures.load(samples.drive_download(b"doc", file_id="DOC-1", mime_type="application/vnd.google-apps.document"))

        def serve(file_id):
            return fixtures._serve({"principal": "local", "operation": "drive.download",
                                    "params": {"file_id": file_id}}, "local")

        with pytest.raises(RpcError) as refused:
            serve("EXAMPLE-1")
        assert (refused.value.code, refused.value.detail) == (
            "invalid_params", f"files of type {form} cannot be downloaded")
        assert refused.value.extra == {"reason": "not_downloadable"}
        assert serve("DOC-1")["data"]["eof"] is True

    async def test_non_ascii_counts_utf8_bytes(self, monkeypatch):
        monkeypatch.setattr(source_module, "MAX_SOURCE_RESULT_BYTES", 100)
        fixtures = source_module.SourceFixtures()
        data = {"v": "\u00e9" * 20}
        fixtures.load({"operation": "calendar.list_events", "data": data})
        result = fixtures._serve({"principal": "local", "operation": "calendar.list_events",
                                  "params": {"time_min": "2025-01-01T00:00:00Z", "time_max": "2025-01-02T00:00:00Z"}},
                                 "local")
        assert result["bytes"] == len(json.dumps(data, ensure_ascii=False).encode())
        assert result["bytes"] < 100 < len(json.dumps(data).encode())

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
        assert first["next_cursor"] == cursors.encode("drive.download", {"file_id": "EXAMPLE-9"}, {"r": "r2", "o": 4})
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

    def test_drive_cursor_state_is_checked_like_the_daemons(self):
        fixtures = source_module.SourceFixtures()
        fixtures.load(samples.drive_download(b"abcdef", revision="r1", file_id="EXAMPLE-9"))
        bound = {"file_id": "EXAMPLE-9"}

        def serve(cursor):
            return fixtures._serve({"principal": "local", "operation": "drive.download",
                                    "params": {"file_id": "EXAMPLE-9", "cursor": cursor}}, "local")

        for state in ({"r": "r1"}, {"r": "r1", "o": -1}, {"r": "r1", "o": True}, {"r": 1, "o": 1},
                      {"f": "EXAMPLE-9", "r": "r1", "o": 1}, {"r": "r1", "o": 7}):
            with pytest.raises(Exception, match="cursor is not valid") as bad:
                serve(cursors.encode("drive.download", bound, state))
            assert bad.value.code == "invalid_params", state
        with pytest.raises(Exception, match="different call") as foreign:
            serve(cursors.encode("drive.download", {"file_id": "OTHER"}, {"r": "r1", "o": 1}))
        assert foreign.value.code == "invalid_params"
        assert serve(cursors.encode("drive.download", bound, {"r": "r1", "o": 6}))["data"]["eof"] is True

    async def test_a_drive_file_has_no_size_cap(self):
        fixtures = source_module.SourceFixtures()
        assert not hasattr(source_module, "DRIVE_MAX_FILE_BYTES")
        fixtures.load(samples.drive_download(bytes(constants.DRIVE_CHUNK_BYTES * 8 + 1), file_id="BIG-1"))
        reply = fixtures._serve({"principal": "local", "operation": "drive.download",
                                 "params": {"file_id": "BIG-1", "length": 1}}, "local")
        assert reply["data"]["total_size_bytes"] == constants.DRIVE_CHUNK_BYTES * 8 + 1 and reply["next_cursor"]

    _REQUIRED_PARAMS = {
        "salesforce.report_run": {"report_id": "R1"},
        "jira.search": {"jql": "project = X"},
        "drive.download": {"file_id": "F1"},
        "sheets.get_values": {"spreadsheet_id": "S1", "range": "A1:B2"},
        "confluence.get_page": {"page_id": "P1"},
        "calendar.list_events": {"time_min": "2025-01-01T00:00:00Z", "time_max": "2025-01-02T00:00:00Z"},
    }

    @pytest.mark.parametrize("operation", constants.SOURCE_OPERATIONS)
    def test_every_operation_has_a_sample(self, operation):
        fixture = samples.get(operation)
        assert fixture["operation"] == operation and "data" in fixture
        assert samples.get(operation) is not fixture
        fixtures = source_module.SourceFixtures()
        fixtures.load(fixture)
        reply = fixtures._serve({"principal": "local", "operation": operation,
                                  "params": {**self._REQUIRED_PARAMS[operation], **fixture["params"]}}, "local")
        assert reply["data"] == fixture["data"] and reply["bytes"] == len(json.dumps(fixture["data"]))

    def test_sample_data_shapes(self):
        assert set(samples.get("sheets.get_values")["data"]) == {"values", "first_row"}
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
        for name in ("SOURCE_OPERATIONS", "DRIVE_CHUNK_BYTES", "MAX_SOURCE_RESULT_BYTES"):
            assert getattr(source_module, name) == getattr(constants, name), name
        assert cursors_module.CURSOR_MAX_CHARS == constants.CURSOR_MAX_CHARS
        assert outputs_module.OUTPUT_TYPES == constants.OUTPUT_TYPES
        assert outputs_module.DEFAULT_OUTPUT_TYPES == constants.DEFAULT_OUTPUT_TYPES
        assert outputs_module.OUTPUT_MAX_DEPTH == constants.OUTPUT_MAX_DEPTH
        assert approvals_module._KIND_RE.pattern == constants.APPROVAL_KIND_RE.pattern
        assert approvals_module._DIGEST_RE.pattern == constants.DIGEST_RE.pattern
        assert approvals_module._SUBJECT_ID_MAX_CHARS == constants.SUBJECT_ID_MAX_CHARS
        assert approvals_module._AWAIT_MAX_MS == constants.CONFIRM_AWAIT_MAX_MS
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


class TestIntrospect:
    async def test_it_returns_the_tools_a_later_run_reports(self):
        plugin, _ = build_plugin()
        host = PluginTestHost(plugin)
        introspected = await host.introspect()
        async with host:
            assert introspected == host.tools

    async def test_the_sdk_records_the_purpose(self):
        plugin, _ = build_plugin()
        host = PluginTestHost(plugin)
        await host.introspect()
        assert plugin._host.introspecting is True

    async def test_it_ends_with_shutdown_like_a_review(self):
        plugin, _ = build_plugin()
        seen = []

        @plugin.on("shutdown")
        async def on_shutdown(ctx, params):
            seen.append(params)

        await PluginTestHost(plugin).introspect()
        assert seen == [{"grace_ms": 0}]

    async def test_only_source_and_confirm_calls_are_answered_and_refused(self, monkeypatch):
        wired = {}
        real_peer = host_module.Peer

        def spy(*args, **kwargs):
            wired.update(kwargs["handlers"])
            return real_peer(*args, **kwargs)

        monkeypatch.setattr(host_module, "Peer", spy)
        plugin, _ = build_plugin()
        await PluginTestHost(plugin).introspect()
        assert wired == {
            "source.call": host_module._refuse_while_introspecting,
            "confirm.request": host_module._refuse_while_introspecting,
        }

    async def test_source_and_confirm_calls_are_refused(self):
        with pytest.raises(RpcError) as raised:
            await host_module._refuse_while_introspecting({})
        assert raised.value.code == "introspection_only"
        assert raised.value.detail == "not available while introspecting"

    async def test_it_cannot_run_inside_the_host(self):
        plugin, _ = build_plugin()
        async with PluginTestHost(plugin) as host:
            with pytest.raises(RuntimeError, match=r"introspect\(\) runs before the host starts"):
                await host.introspect()


class TestPaging:
    @staticmethod
    def paging_plugin() -> tuple[Plugin, dict]:
        seen: dict = {}
        plugin = Plugin(name="paging", version="1.0.0")

        @plugin.tool("count", description="Count issues.", read_only=True,
                     params={"jql": {"type": "string"}}, required=["jql"])
        async def count(ctx, args):
            pages = [page async for page in ctx.source.pages("jira.search", jql=args["jql"])]
            seen["cursors"] = [p.next_cursor for p in pages]
            seen["keys"] = [[i["key"] for i in p.data] for p in pages]
            return Prepared(preview=[blocks.text("count")], payload=[blocks.text(str(len(pages)))])

        @plugin.tool("collect", description="Collect events.", read_only=True)
        async def collect(ctx, args):
            events = await ctx.source.collect(
                "calendar.list_events", time_min="2026-10-07T00:00:00Z", time_max="2026-10-08T00:00:00Z")
            seen["titles"] = [e["title"] for e in events]
            return Prepared(preview=[blocks.text("collect")], payload=[blocks.text("ok")])

        return plugin, seen

    async def test_returns_pages_serves_each_page_for_the_cursor_before_it(self):
        plugin, seen = self.paging_plugin()
        async with PluginTestHost(plugin, source_operations=("jira.search",)) as host:
            host.source.when("jira.search", jql="project = A").returns_pages(
                [[{"key": "A-1"}], [{"key": "A-2"}], [{"key": "A-3"}]])
            outcome = await host.call_tool("count", {"jql": "project = A"})
            calls = [c.params for c in host.source.calls]
        assert outcome.error is None and seen["keys"] == [["A-1"], ["A-2"], ["A-3"]]
        assert "cursor" not in calls[0] and [c["cursor"] for c in calls[1:]] == seen["cursors"][:2]
        assert seen["cursors"][2] is None
        bound = {"jql": "project = A"}
        assert cursors.decode(seen["cursors"][0], "jira.search", bound) == {"page": 1}
        assert cursors.decode(seen["cursors"][1], "jira.search", bound) == {"page": 2}

    async def test_a_cursor_for_another_query_or_none_of_ours_is_refused(self):
        plugin, _ = self.paging_plugin()
        async with PluginTestHost(plugin, source_operations=("jira.search",)) as host:
            host.source.when("jira.search", jql="project = A").returns_pages([[{"key": "A-1"}], [{"key": "A-2"}]])
            host.source.when("jira.search", jql="project = B").returns_pages([[{"key": "B-1"}], [{"key": "B-2"}]])
            first = host.source.handle(
                {"principal": "local", "operation": "jira.search", "params": {"jql": "project = A"}},
                mode="local", audit=lambda *a: None)
            for foreign in (first["next_cursor"], "!!", cursors.encode("jira.search", {"jql": "x"}, {"page": 1})):
                with pytest.raises(Exception) as bad:
                    host.source.handle(
                        {"principal": "local", "operation": "jira.search",
                         "params": {"jql": "project = B", "cursor": foreign}},
                        mode="local", audit=lambda *a: None)
                assert bad.value.code == "invalid_params"
            second = host.source.handle(
                {"principal": "local", "operation": "jira.search",
                 "params": {"jql": "project = A", "cursor": first["next_cursor"]}},
                mode="local", audit=lambda *a: None)
        assert second["data"] == [{"key": "A-2"}] and second["next_cursor"] is None

    def test_returns_pages_needs_pages_and_issues_its_own_cursors(self):
        fixtures = source_module.SourceFixtures()
        with pytest.raises(ValueError, match="non-empty"):
            fixtures.when("jira.search", jql="x").returns_pages([])
        with pytest.raises(ValueError, match="itself"):
            fixtures.when("jira.search", jql="x", cursor="c").returns_pages([[]])
        fixtures.when("jira.search", jql="x").returns_pages([[1]])
        reply = fixtures._serve({"principal": "local", "operation": "jira.search", "params": {"jql": "x"}}, "local")
        assert reply["data"] == [1] and reply["next_cursor"] is None

    async def test_the_sample_pages_fetch_each_other(self):
        plugin, seen = self.paging_plugin()
        async with PluginTestHost(plugin, source_operations=("calendar.list_events", "jira.search")) as host:
            host.source.load(samples.get("calendar.list_events"))
            host.source.load(samples.get("calendar.list_events", page=2))
            await host.call_tool("collect")
            host.source.load(samples.get("jira.search"))
            host.source.load(samples.get("jira.search", page=2))
            await host.call_tool("count", {"jql": "project = EXAMPLE"})
        assert seen["titles"] == ["Example meeting", "Second example meeting"]
        assert seen["keys"] == [["EXAMPLE-1"], ["EXAMPLE-2"]]

    def test_only_two_samples_have_a_second_page(self):
        for operation in ("jira.search", "calendar.list_events"):
            first, second = samples.get(operation), samples.get(operation, page=2)
            assert first["next_cursor"] and second["next_cursor"] is None
            assert second["params"] == {"cursor": first["next_cursor"]}
        for operation in ("sheets.get_values", "drive.download"):
            with pytest.raises(ValueError, match="no page 2"):
                samples.get(operation, page=2)

    def test_the_private_cursor_copy_matches_the_daemons(self):
        bound = {"jql": "project = A", "page_size": 100}
        state = {"t": "token", "k": 3}
        mine = cursors_module.encode("jira.search", bound, state)
        assert mine == cursors.encode("jira.search", bound, state)
        assert cursors_module.params_digest("jira.search", bound) == cursors.params_digest("jira.search", bound)
        assert cursors_module.decode(mine, "jira.search", bound) == state
        for bad, operation in ((mine, "calendar.list_events"), ("!!", "jira.search"), ("", "jira.search"),
                               ("A" * (constants.CURSOR_MAX_CHARS + 1), "jira.search")):
            with pytest.raises(cursors.CursorError) as theirs:
                cursors.decode(bad, operation, bound)
            with pytest.raises(cursors_module.CursorError) as ours:
                cursors_module.decode(bad, operation, bound)
            assert str(ours.value) == str(theirs.value)


class TestCheckToolDefs:
    @staticmethod
    def result(p, scope_types):
        return {"plugin": {"name": p.name, "version": p.version}, "scope_types": scope_types, "tools": []}

    def test_scope_type_output_is_reserved(self):
        p, _ = build_plugin()
        with pytest.raises(ToolDefinitionError, match="scope type output is reserved"):
            host_module._check_tool_defs(p, self.result(p, [{"name": "output", "description": "x"}]), "review")

    def test_scope_type_description_is_1_to_500_characters(self):
        p, _ = build_plugin()
        for description in ("", "x" * 501, None):
            with pytest.raises(ToolDefinitionError, match="needs a description of 1 to 500 characters"):
                host_module._check_tool_defs(p, self.result(p, [{"name": "cal", "description": description}]), "review")

    def test_a_reserved_plugin_name_is_refused(self):
        p, _ = build_plugin()
        p.name = "apps"
        with pytest.raises(ToolDefinitionError, match="name 'apps' is reserved"):
            host_module._check_tool_defs(p, self.result(p, []), "review")

    @pytest.mark.parametrize("field", ["title", "effect"])
    def test_a_title_or_effect_with_a_line_break_is_refused(self, field):
        p, _ = build_plugin()
        tool = {
            "name": "tt", "description": "d", "parameters": {"type": "object", "properties": {}},
            "read_only": True, "destructive": False, "gate": "review", field: "A\nB",
        }
        result = {**self.result(p, []), "tools": [tool]}
        with pytest.raises(ToolDefinitionError, match=f"tool.{field} must not contain line breaks"):
            host_module._check_tool_defs(p, result, "review")
