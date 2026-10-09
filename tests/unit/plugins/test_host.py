"""Unit tests for privacyfence.plugins.host.

Plugins are real child processes: the stdlib stub (tests/fixtures/plugins/stub) and a minimal
plugin written with the SDK (``plugin-sdk/src``). ``plugins_dir`` is a temporary directory and the
trust check is injected, so nothing needs an administrator-owned directory.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from privacyfence import auto_accept, paths
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.audit_log import AuditEntry
from privacyfence.calendar_client import CalendarEvent
from privacyfence.plugins import host as host_mod
from privacyfence.plugins import source_ops, storage
from privacyfence.plugins import supervisor as supervisor_mod
from privacyfence.plugins.host import CHANGED_SINCE_REVIEW, PluginHost
from privacyfence.plugins.manifest import MANIFEST_FILENAME, ManifestError
from privacyfence.plugins.state import HASH_DRIFT_REASON
from privacyfence.principal import LOCAL_PRINCIPAL

from ...helpers import policy_rules

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
STUB = REPO / "tests" / "fixtures" / "plugins" / "stub" / "stub_plugin.py"
SDK_SRC = REPO / "plugin-sdk" / "src"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
SDK = "sdk-demo"

SDK_PLUGIN = textwrap.dedent('''
    import asyncio
    import json
    import os
    import sys

    sys.path.insert(0, {sdk_src!r})
    from privacyfence_plugin_sdk import Plugin, Prepared, SourceError, Text

    MODE, NAME = sys.argv[1], sys.argv[2]
    HERE = os.path.dirname(os.path.abspath(__file__))
    plugin = Plugin(NAME, "1.0.0")
    plugin.scope_type("calendar", "A calendar")


    def blocks(text):
        return [{{"type": "text", "text": text}}]


    @plugin.tool("ping", description="Say pong.", gate="auto", read_only=True)
    async def ping(ctx, args):
        return Prepared(preview=blocks("ping"), payload=blocks("pong"))


    CHANGED = os.path.exists(os.path.join(HERE, "destructive-" + NAME))


    @plugin.tool("lookup", description="Look something up.", gate="popup" if CHANGED else "review",
                 read_only=not CHANGED, destructive=CHANGED, scopes=["calendar"])
    async def lookup(ctx, args):
        return Prepared(preview=blocks("lookup"), payload=blocks("found"), scopes={{"calendar": ["primary"]}})


    if CHANGED:
        @lookup.execute
        async def run_lookup(ctx, payload):
            return blocks("done")


    if os.path.exists(os.path.join(HERE, "grow-" + NAME)):
        @plugin.tool("extra", description="A tool nobody reviewed.", gate="review", read_only=True)
        async def extra(ctx, args):
            return Prepared(preview=blocks("extra"), payload=blocks("extra"))


    async def hang(ctx, scope, principal):
        if MODE == "purge-hang":
            await asyncio.sleep(60)


    plugin.on_purge(hang)

    SEEN = []


    @plugin.on("connector.state_changed")
    async def changed(ctx, params):
        SEEN.append(params)


    @plugin.page("/events")
    async def events(ctx, request):
        return Text(json.dumps(SEEN))


    @plugin.page("/info")
    async def info(ctx, request):
        return Text(json.dumps({{"data_dir": str(ctx.data_dir), "storage_dir": str(ctx.principal.storage_dir)}}))


    @plugin.page("/drop")
    async def drop(ctx, request):
        del plugin._reg.tools["lookup"]
        await plugin.tools_changed()
        return Text("ok")


    @plugin.page("/widen")
    async def widen(ctx, request):
        @plugin.tool("wide", description="Added later.", gate="review", read_only=True)
        async def wide(ctx, args):
            return Prepared(preview=blocks("wide"), payload=blocks("wide"))

        await plugin.tools_changed()
        return Text("ok")


    @plugin.page("/source")
    async def source(ctx, request):
        try:
            result = await ctx.source.call("calendar.list_events", time_min="a", time_max="b")
        except SourceError as exc:
            return Text(exc.code)
        return Text(json.dumps(result.data))


    @plugin.page("/confirm")
    async def confirm(ctx, request):
        try:
            return Text(await ctx.confirm.request("publish", "Publish it", blocks("body")))
        except SourceError as exc:
            return Text(exc.code)


    REVOKED = []


    @plugin.on("approval.revoked")
    async def revoked(ctx, params):
        REVOKED.append(params)


    @plugin.page("/revoked")
    async def revoked_page(ctx, request):
        return Text(json.dumps(REVOKED))


    @plugin.page("/approval")
    async def approval(ctx, request):
        try:
            ticket = await ctx.approvals.request("code", "main", "v1", "Run main", blocks("body"))
        except SourceError as exc:
            return Text(exc.code)
        return Text(ticket.approval_id + " " + ticket.status)


    @plugin.page("/approval-check")
    async def approval_check(ctx, request):
        return Text(await ctx.approvals.check("code", "main", "v1"))


    @plugin.page("/outdir")
    async def outdir(ctx, request):
        return Text(json.dumps({{"dir": str(ctx.principal.output_dir)}}))


    @plugin.page("/await")
    async def await_confirm(ctx, request):
        try:
            result = await ctx.confirm.await_(request.query["id"], int(request.query["ms"]))
        except SourceError as exc:
            return Text(exc.code)
        return Text(result.status)


    plugin.run()
''').format(sdk_src=str(SDK_SRC))


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    def summaries(self, decision: str) -> list[str]:
        return [e.summary for e in self.entries if e.decision == decision]


async def until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


class Env:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.root = tmp_path
        self.data = tmp_path / "data"
        self.data.mkdir()
        monkeypatch.setattr(paths, "data_dir", lambda: self.data)
        self.plugins = tmp_path / "plugins"
        self.plugins.mkdir()
        self.script = tmp_path / "sdk_plugin.py"
        self.script.write_text(SDK_PLUGIN, encoding="utf-8")
        self.audit = FakeAudit()
        monkeypatch.setattr(host_mod, "get_audit_logger", lambda: self.audit)
        monkeypatch.setattr(source_ops, "get_audit_logger", lambda: self.audit)
        self.registry = PendingApprovalRegistry(pending_ttl=60.0)
        self.calendar = MagicMock()
        self.calendar_state: tuple[bool, str | None] = (True, None)
        self.argv: dict[str, list[str]] = {}
        self.hosts: list[PluginHost] = []
        self.tools_changed = 0
        self.rows_changed = 0
        self.separation = True
        self.problem: str | None = None

    def add(self, name: str = "stub", *, mode: str = "ok", sdk: bool = False, **manifest) -> Path:
        plugin_dir = self.plugins / name
        plugin_dir.mkdir()
        data = {
            "name": name, "display_name": name.title(), "version": "1.0.0", "protocol": "1",
            "command": ["plugin-bin"], "tools": "dynamic", "max_gate_floor": "auto",
            **({"pages": True, "source_operations": ["calendar.list_events"]} if sdk else {}),
            **manifest,
        }
        (plugin_dir / MANIFEST_FILENAME).write_text(yaml.safe_dump(data), encoding="utf-8")
        (plugin_dir / f"plugin-bin{EXE_SUFFIX}").write_bytes(b"binary " + name.encode())
        self.argv[name] = [sys.executable, str(self.script), mode, name] if sdk else [sys.executable, str(STUB), mode]
        return plugin_dir

    def host(self, **overrides) -> PluginHost:
        def _count_tools() -> None:
            self.tools_changed += 1

        def _count_rows() -> None:
            self.rows_changed += 1

        kwargs = {
            "plugins_dir": self.plugins,
            "connectors_provider": lambda: {"calendar": SimpleNamespace(_calendar=self.calendar)},
            "connector_state": lambda name: self.calendar_state,
            "registry_provider": lambda: self.registry,
            "trust_check": lambda *_: self.problem,
            "command_resolver": lambda manifest, _dir: self.argv[manifest.name],
            "separation_enabled": lambda: self.separation,
            "daemon_version": "9.9.9",
        }
        host = PluginHost(**{**kwargs, **overrides})
        host.set_tools_changed_listener(_count_tools)
        host.set_rows_changed_listener(_count_rows)
        self.hosts.append(host)
        return host

    def row(self, host: PluginHost, name: str) -> dict:
        return next(r for r in host.rows() if r["name"] == name)

    async def enable(self, host: PluginHost, name: str) -> dict:
        summary = await host.inspect(name)
        await host.enable(name, executable_sha256=summary["executable_sha256"], manifest_sha256=summary["manifest_sha256"])
        return summary

    async def page(self, host: PluginHost, path: str, **query: str) -> dict:
        return await host.web_request(SDK, path, query, LOCAL_PRINCIPAL)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    environment = Env(tmp_path, monkeypatch)
    yield environment
    for host in environment.hosts:
        await host.stop_all()


class TestStartEnabled:
    async def test_enabled_plugin_starts_with_the_daemon(self, env):
        env.add(SDK, sdk=True)
        first = env.host()
        await first.start()
        await env.enable(first, SDK)
        await first.stop_all()

        second = env.host()
        await second.start()

        row = env.row(second, SDK)
        assert row["state"] == "running" and row["reason"] == "" and row["enabled"] is True
        assert [spec.name for spec in second.connectors()[SDK].tool_specs()] == ["sdk-demo_ping", "sdk-demo_lookup"]

    async def test_discovered_plugin_waits_for_a_human(self, env):
        env.add("stub")
        host = env.host()

        await host.start()

        assert env.row(host, "stub")["state"] == "discovered"
        assert host.connectors() == {}

    async def test_initialize_params(self, env):
        env.add("stub")
        host = env.host()
        await host.start()

        params = host._initialize_params(host._plugins["stub"], "run")

        shared, per_principal = storage.install_dir("stub"), storage.principal_dir("stub", LOCAL_PRINCIPAL)
        assert params["protocol_version"] == "1.1.0" and params["purpose"] == "run" and params["mode"] == "local"
        assert params["daemon"] == {"name": "privacyfence", "version": "9.9.9"}
        assert params["plugin"] == {"name": "stub", "manifest_version": "1.0.0"}
        assert params["data_dir"] == str(shared) and shared.is_dir()
        assert params["principals"] == [
            {"id": "local", "display_name": "", "storage_dir": str(per_principal)}
        ]
        assert set(params["limits"]) == {"max_line_bytes", "max_in_flight", "inline_result_bytes"}

    async def test_plugin_gets_its_directories(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        body = json.loads((await env.page(host, "/info"))["body"])

        assert body == {
            "data_dir": str(storage.install_dir(SDK)),
            "storage_dir": str(storage.principal_dir(SDK, LOCAL_PRINCIPAL)),
        }

    async def test_invalid_manifest_is_rejected(self, env):
        bad = env.plugins / "broken"
        bad.mkdir()
        (bad / MANIFEST_FILENAME).write_text("name: broken\n", encoding="utf-8")
        host = env.host()

        await host.start()

        row = env.row(host, "broken")
        assert row["state"] == "rejected" and row["reason"].startswith("manifest invalid: ")

    async def test_untrusted_plugin_is_rejected(self, env):
        env.add("stub")
        env.problem = "/somewhere is writable by the user"
        host = env.host()

        await host.start()

        row = env.row(host, "stub")
        assert row["state"] == "rejected" and row["reason"] == "executable is writable by non-administrators"

    async def test_launch_spec_failure_rejects(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        await host.stop_all()

        def refuse(manifest, plugin_dir):
            raise ManifestError("command escapes the plugin directory")

        again = env.host(command_resolver=refuse)
        await again.start()

        row = env.row(again, "stub")
        assert row["state"] == "rejected" and row["reason"] == "manifest invalid: command escapes the plugin directory"

    async def test_start_failure_disables(self, env, monkeypatch):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        await host.stop_all()

        async def boom(self):
            raise OSError("no such file")

        monkeypatch.setattr(supervisor_mod.Supervisor, "start", boom)
        again = env.host()
        await again.start()

        row = env.row(again, "stub")
        assert row["state"] == "disabled" and row["reason"] == "could not start"

    async def test_stop_all_stops_and_clears_the_spool(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        spool = host._get_spool()
        marker = paths.data_dir() / "plugin-spool" / "left-over"
        marker.write_text("x")

        await host.stop_all()

        assert host.connectors() == {} and not marker.exists()
        assert spool is host._get_spool()
        assert env.row(host, "stub")["state"] == "disabled"


class TestFeatureOff:
    async def test_nothing_starts(self, env):
        env.add("stub")
        first = env.host()
        await first.start()
        await env.enable(first, "stub")
        await first.stop_all()

        off = env.host(feature_enabled=False)
        await off.start()

        row = env.row(off, "stub")
        assert row["state"] == "disabled" and row["reason"] == "plugins are turned off in settings"
        assert off.connectors() == {}

    async def test_inspect_and_enable_are_refused(self, env):
        env.add("stub")
        host = env.host(feature_enabled=False)
        await host.start()

        with pytest.raises(ValueError, match="plugins are turned off in settings"):
            await host.inspect("stub")
        with pytest.raises(ValueError, match="plugins are turned off in settings"):
            await host.enable("stub", executable_sha256="a", manifest_sha256="b")

        assert env.row(host, "stub")["last_error"] == "plugins are turned off in settings"


class TestNoSeparation:
    async def test_reason_string(self, env):
        env.add("stub")
        env.separation = False
        host = env.host()

        await host.start()

        row = env.row(host, "stub")
        assert row["state"] == "disabled"
        assert row["reason"] == "plugins need PrivacyFence's background service"
        with pytest.raises(ValueError, match="background service"):
            await host.inspect("stub")

    async def test_losing_separation_stops_a_running_plugin(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        assert env.row(host, "stub")["state"] == "running"

        env.separation = False
        await host.rescan()

        row = env.row(host, "stub")
        assert row["state"] == "disabled" and row["reason"] == "plugins need PrivacyFence's background service"
        assert host.connectors() == {}

        env.separation = True
        await host.rescan()

        assert env.row(host, "stub")["state"] == "running"


class TestEnable:
    async def test_requires_inspection(self, env):
        env.add("stub")
        host = env.host()
        await host.start()

        with pytest.raises(ValueError, match="review it again"):
            await host.enable("stub", executable_sha256="e", manifest_sha256="m")

        assert host._store.load() == {}
        assert env.row(host, "stub")["last_error"] == CHANGED_SINCE_REVIEW

    async def test_hash_changed_since_review_refused(self, env):
        plugin_dir = env.add("stub")
        host = env.host()
        await host.start()
        summary = await host.inspect("stub")

        (plugin_dir / f"plugin-bin{EXE_SUFFIX}").write_bytes(b"swapped after review")
        with pytest.raises(ValueError) as refused:
            await host.enable(
                "stub", executable_sha256=summary["executable_sha256"], manifest_sha256=summary["manifest_sha256"]
            )

        assert str(refused.value) == "The plugin changed since you reviewed it; review it again."
        row = env.row(host, "stub")
        assert row["last_error"] == CHANGED_SINCE_REVIEW and row["review"] is None
        assert host._store.load() == {}
        assert host.connectors() == {}

    async def test_hash_passed_in_must_match_the_review(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        summary = await host.inspect("stub")

        with pytest.raises(ValueError, match="review it again"):
            await host.enable("stub", executable_sha256="0" * 64, manifest_sha256=summary["manifest_sha256"])

        assert host._store.load() == {}

    async def test_unreviewed_tool_fails_start(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        await host.stop_all()
        (env.root / f"grow-{SDK}").write_text("")

        again = env.host()
        await again.start()

        row = env.row(again, SDK)
        assert row["state"] == "disabled" and row["reason"] == HASH_DRIFT_REASON
        assert again._store.load()[SDK].enabled is False
        assert again.connectors() == {}
        assert f"disabled: {HASH_DRIFT_REASON}" in env.audit.summaries("plugin_lifecycle")

    async def test_enable_records_review_and_clears_the_error(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        with pytest.raises(ValueError):
            await host.enable(SDK, executable_sha256="e", manifest_sha256="m")
        assert env.row(host, SDK)["last_error"] == CHANGED_SINCE_REVIEW

        await env.enable(host, SDK)

        record = host._store.load()[SDK]
        assert record.enabled and record.max_gate_floor == "auto"
        assert {s[0] for s in record.reviewed_tools} == {"ping", "lookup"}
        row = env.row(host, SDK)
        assert row["last_error"] is None and row["review"] is None and row["state"] == "running"
        assert env.audit.summaries("plugin_lifecycle")[-1] == "enabled"

    async def test_enable_again_restarts_a_running_plugin(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")

        await env.enable(host, "stub")

        assert env.row(host, "stub")["state"] == "running"

    async def test_enable_unknown_plugin(self, env):
        host = env.host()
        await host.start()

        with pytest.raises(LookupError):
            await host.enable("nothing", executable_sha256="e", manifest_sha256="m")

    async def test_enable_after_the_plugin_was_removed(self, env):
        plugin_dir = env.add("stub")
        host = env.host()
        await host.start()
        summary = await host.inspect("stub")
        for child in plugin_dir.iterdir():
            child.unlink()
        plugin_dir.rmdir()

        with pytest.raises(ValueError, match="no longer installed"):
            await host.enable(
                "stub", executable_sha256=summary["executable_sha256"], manifest_sha256=summary["manifest_sha256"]
            )

    async def test_unexpected_failure_shows_a_generic_error(self, env, monkeypatch):
        env.add("stub")
        host = env.host()
        await host.start()

        def boom(*_args, **_kwargs):
            raise OSError("secret path")

        monkeypatch.setattr(host_mod.trust, "discover", boom)
        with pytest.raises(OSError):
            await host.inspect("stub")

        assert env.row(host, "stub")["last_error"] == "The action failed; the log has the details."


class TestEnableDropsStaleRules:
    @staticmethod
    def _rules(env) -> None:
        config = env.root / "settings.yaml"
        config.write_text(yaml.safe_dump({}), encoding="utf-8")
        auto_accept.init_config_path(str(config))
        auto_accept.add_policy_v2_rules(policy_rules({
            f"plugin.{SDK}.lookup": [{"predicate": f"plugin:{SDK}:calendar", "value": "primary"}],
            f"plugin.{SDK}.ping": [{"predicate": f"plugin:{SDK}:anything"}],
            "gmail.send": [{"predicate": "gmail.anything"}],
        }))

    @staticmethod
    def _remaining() -> list[tuple[str, list[str]]]:
        return sorted((r.predicate, sorted(r.operations)) for r in auto_accept.get_policy_v2_store_rules())

    async def test_unchanged_tools_keep_their_rules(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        self._rules(env)
        before = self._remaining()

        await env.enable(host, SDK)

        assert self._remaining() == before and len(before) == 3

    async def test_tool_turned_destructive_loses_its_rules(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        self._rules(env)
        (env.root / f"destructive-{SDK}").write_text("")

        await env.enable(host, SDK)

        assert self._remaining() == [
            ("gmail.anything", ["gmail.send"]),
            (f"plugin:{SDK}:anything", [f"plugin.{SDK}.ping"]),
        ]
        lookup = next(s for s in host._store.load()[SDK].reviewed_tools if s[0] == "lookup")
        assert lookup[1:4] == ("popup", False, True)
        assert env.row(host, SDK)["state"] == "running"

    async def test_no_rule_survives_for_a_destructive_tool(self, env):
        env.add(SDK, sdk=True)
        (env.root / f"destructive-{SDK}").write_text("")
        host = env.host()
        await host.start()
        self._rules(env)

        await env.enable(host, SDK)

        assert f"plugin.{SDK}.lookup" not in {op for _p, ops in self._remaining() for op in ops}


class TestRestartRechecks:
    async def test_changed_executable_is_not_restarted(self, env, monkeypatch):
        monkeypatch.setattr(supervisor_mod, "RESTART_BACKOFF_SECONDS", (0.0,))
        plugin_dir = env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        supervisor = host._plugins["stub"].supervisor
        spawns: list[int] = []
        original = supervisor._spawn

        async def counting(*args):
            spawns.append(1)
            return await original(*args)

        monkeypatch.setattr(supervisor, "_spawn", counting)
        (plugin_dir / f"plugin-bin{EXE_SUFFIX}").write_bytes(b"swapped while running")

        supervisor._child.proc.kill()
        await until(lambda: env.row(host, "stub")["state"] == "disabled")

        row = env.row(host, "stub")
        assert row["reason"] == HASH_DRIFT_REASON and row["enabled"] is False
        assert host._store.load()["stub"].disabled_reason == HASH_DRIFT_REASON
        assert spawns == []
        assert f"disabled: {HASH_DRIFT_REASON}" in env.audit.summaries("plugin_lifecycle")
        assert host.connectors() == {}

    async def test_writable_executable_is_not_restarted(self, env, monkeypatch):
        monkeypatch.setattr(supervisor_mod, "RESTART_BACKOFF_SECONDS", (0.0,))
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        supervisor = host._plugins["stub"].supervisor
        env.problem = "the plugin directory is group-writable"

        supervisor._child.proc.kill()
        await until(lambda: env.row(host, "stub")["state"] == "disabled")

        reason = "executable is writable by non-administrators"
        assert env.row(host, "stub")["reason"] == reason
        assert host._store.load()["stub"].disabled_reason == reason

    async def test_removed_plugin_is_not_restarted(self, env, monkeypatch):
        monkeypatch.setattr(supervisor_mod, "RESTART_BACKOFF_SECONDS", (0.0,))
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        supervisor = host._plugins["stub"].supervisor
        # Pointing the host elsewhere stands in for removing the directory, which Windows refuses
        # while it is the running plugin's working directory.
        host._plugins_dir = env.root / "empty"
        host._plugins_dir.mkdir()

        supervisor._child.proc.kill()
        await until(lambda: env.row(host, "stub")["state"] == "disabled")

        assert env.row(host, "stub")["reason"] == "plugin is no longer installed"


class TestDisable:
    async def test_disable(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        before = env.tools_changed

        await host.disable(SDK)

        row = env.row(host, SDK)
        assert row["state"] == "disabled" and row["reason"] == "disabled by you" and row["enabled"] is False
        record = host._store.load()[SDK]
        assert record.enabled is False and record.disabled_reason == "disabled by you"
        assert host.connectors() == {}
        assert env.tools_changed > before
        assert env.audit.summaries("plugin_lifecycle")[-1] == "disabled"
        assert "disabled: disabled by you" not in env.audit.summaries("plugin_lifecycle")

    async def test_stays_disabled_after_a_restart_of_the_daemon(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        await host.disable("stub")
        await host.stop_all()

        again = env.host()
        await again.start()

        row = env.row(again, "stub")
        assert row["state"] == "disabled" and row["reason"] == "disabled by you"


class TestCrashLimitRecorded:
    async def test_crash_limit_disables_and_audits(self, env, monkeypatch):
        monkeypatch.setattr(supervisor_mod, "RESTART_BACKOFF_SECONDS", (0.0,))
        env.add("stub", mode="crash-after-init")
        host = env.host()
        await host.start()

        await env.enable(host, "stub")
        await until(lambda: env.row(host, "stub")["reason"] == "crashed 5 times in 10 minutes")

        row = env.row(host, "stub")
        assert row["state"] == "disabled" and row["enabled"] is False
        record = host._store.load()["stub"]
        assert record.enabled is False and record.disabled_reason == "crashed 5 times in 10 minutes"
        assert "disabled: crashed 5 times in 10 minutes" in env.audit.summaries("plugin_lifecycle")
        assert host.connectors() == {}


class TestHashDriftOnStart:
    async def test_changed_executable_is_not_started(self, env):
        plugin_dir = env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        await host.stop_all()
        (plugin_dir / f"plugin-bin{EXE_SUFFIX}").write_bytes(b"something else")

        again = env.host()
        await again.start()

        row = env.row(again, "stub")
        assert row["state"] == "disabled" and row["reason"] == HASH_DRIFT_REASON
        assert again._store.load()["stub"].enabled is False
        assert again.connectors() == {}
        assert f"disabled: {HASH_DRIFT_REASON}" in env.audit.summaries("plugin_lifecycle")

    async def test_rescan_stops_a_running_plugin_that_changed(self, env):
        plugin_dir = env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        assert env.row(host, "stub")["state"] == "running"

        (plugin_dir / MANIFEST_FILENAME).write_text(
            (plugin_dir / MANIFEST_FILENAME).read_text(encoding="utf-8") + "pages: true\n", encoding="utf-8"
        )
        await host.rescan()

        row = env.row(host, "stub")
        assert row["state"] == "disabled" and row["reason"] == HASH_DRIFT_REASON
        assert host.connectors() == {}


class TestInspectReturnsSummary:
    async def test_summary(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()

        summary = await host.inspect(SDK)

        manifest_hash = host._plugins[SDK].discovered.manifest_sha256
        assert summary == {
            "name": SDK,
            "display_name": "Sdk-Demo",
            "version": "1.0.0",
            "executable_sha256": host._plugins[SDK].discovered.executable_sha256,
            "manifest_sha256": manifest_hash,
            "max_gate_floor": "auto",
            "source_operations": ["calendar.list_events"],
            "pages": True,
            "service_credentials": False,
            "outputs": False,
            "output_types": [],
            "tools": [
                {"name": "sdk-demo_ping", "gate": "auto", "read_only": True, "destructive": False,
                 "description": "Say pong."},
                {"name": "sdk-demo_lookup", "gate": "review", "read_only": True, "destructive": False,
                 "description": "Look something up."},
            ],
        }
        assert env.row(host, SDK)["review"] == summary
        assert host.connectors() == {}

    async def test_refuses_a_plugin_with_a_problem(self, env):
        env.add("stub")
        env.problem = "writable"
        host = env.host()
        await host.start()

        with pytest.raises(ValueError, match="executable is writable by non-administrators"):
            await host.inspect("stub")

        assert env.row(host, "stub")["last_error"] == "executable is writable by non-administrators"

    async def test_failed_introspection_is_reported(self, env):
        env.add("stub", mode="crash-on-start")
        host = env.host()
        await host.start()

        with pytest.raises(ValueError, match="could not start"):
            await host.inspect("stub")

        row = env.row(host, "stub")
        assert row["last_error"] == "could not start" and row["review"] is None

    async def test_wrong_identity_is_reported(self, env):
        env.add("stub", mode="wrong-name")
        host = env.host()
        await host.start()

        with pytest.raises(ValueError, match="name or version differs"):
            await host.inspect("stub")

    async def test_unknown_plugin(self, env):
        host = env.host()
        await host.start()

        with pytest.raises(LookupError):
            await host.inspect("nothing")


class TestSourceCallRouted:
    async def test_runs_through_the_source_operations(self, env):
        env.calendar.list_events_page.return_value = ([CalendarEvent(
            id="e1", calendar_id="primary", title="Standup", description="", start_time="2026-01-01T09:00:00Z",
            end_time="2026-01-01T09:30:00Z", all_day=False, organizer_email="", attendees=[], location="",
            hangout_link="", conference_link="", status="confirmed", html_link="",
        )], None)
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        response = await env.page(host, "/source")

        assert json.loads(response["body"])[0]["title"] == "Standup"
        assert env.calendar.list_events_page.call_args.args == ("primary", 250, "a", "b", None)
        assert env.audit.summaries("plugin_source")[-1].startswith("primary; a; b; bytes=")

    async def test_operation_outside_the_manifest_is_refused(self, env):
        env.add(SDK, sdk=True, source_operations=["jira.search"])
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        response = await env.page(host, "/source")

        assert response["body"] == "operation_not_allowed"
        env.calendar.list_events_page.assert_not_called()

    async def test_connector_state_comes_from_the_provider(self, env):
        env.calendar_state = (False, None)
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        assert (await env.page(host, "/source"))["body"] == "connector_unavailable"


class TestConfirmRefusedWhenUnattended:
    async def test_refused_and_audited(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        host.set_unattended_provider(lambda: True)
        await host.start()
        await env.enable(host, SDK)

        response = await env.page(host, "/confirm")

        assert response["body"] == "confirmation_refused"
        assert env.audit.summaries("plugin_confirm") == ["publish; refused"]
        assert env.registry._pending == {}

    async def test_request_and_await(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        approval_id = (await env.page(host, "/confirm"))["body"]

        card = env.registry.get(approval_id)
        assert card is not None and card.kind == "confirm"
        pending = await env.page(host, "/await", id=approval_id, ms="50")
        assert pending["body"] == "timeout"
        env.registry.answer(approval_id, "confirm")
        answered = await env.page(host, "/await", id=approval_id, ms="5000")
        assert answered["body"] == "approved"
        assert env.audit.summaries("plugin_confirm")[0] == "publish; requested"
        await until(lambda: "publish; approved" in env.audit.summaries("plugin_confirm"))


class TestApprovalRouted:
    async def test_request_shows_a_card_and_approval_is_checked(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        assert (await env.page(host, "/approval-check"))["body"] == "unknown"
        approval_id, status = (await env.page(host, "/approval"))["body"].split()

        assert status == "pending"
        card = env.registry.get(approval_id)
        assert card is not None and card.kind == "confirm"
        assert env.audit.summaries("plugin_approval") == ["code; requested"]
        env.registry.answer(approval_id, "confirm")
        await until(lambda: "code; approved" in env.audit.summaries("plugin_approval"))
        assert (await env.page(host, "/approval-check"))["body"] == "approved"
        assert env.row(host, SDK)["approvals"][0]["approval_id"] == approval_id

    async def test_revoke_notifies_the_plugin_and_the_check_says_revoked(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        approval_id = (await env.page(host, "/approval"))["body"].split()[0]
        env.registry.answer(approval_id, "confirm")
        await until(lambda: "code; approved" in env.audit.summaries("plugin_approval"))

        await host.revoke_approval(SDK, approval_id)

        assert (await env.page(host, "/approval-check"))["body"] == "revoked"
        for _ in range(250):
            seen = json.loads((await env.page(host, "/revoked"))["body"])
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen[0]["approval_id"] == approval_id and seen[0]["kind"] == "code"
        assert "code; revoked" in env.audit.summaries("plugin_approval")
        row = env.row(host, SDK)["approvals"][0]
        assert row["revoked_at"] is not None

    async def test_revoking_an_unknown_id_is_a_value_error(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        with pytest.raises(ValueError, match="No such approval."):
            await host.revoke_approval(SDK, "nope")

        assert env.row(host, SDK)["last_error"] == "No such approval."

    async def test_refused_while_unattended_is_audited(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        host.set_unattended_provider(lambda: True)
        await host.start()
        await env.enable(host, SDK)

        assert (await env.page(host, "/approval"))["body"] == "confirmation_refused"
        assert env.audit.summaries("plugin_approval") == ["code; refused"]


class TestApprovalEmbedAllowed:
    async def test_delegates_to_the_service(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        assert await host.approval_embed_allowed(SDK, "missing", "/approval") is False


class TestPurgeForgetsApprovals:
    async def test_purge_deletes_approvals_and_audits_the_count(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        approval_id = (await env.page(host, "/approval"))["body"].split()[0]
        env.registry.answer(approval_id, "confirm")
        await until(lambda: "code; approved" in env.audit.summaries("plugin_approval"))

        await host.purge(SDK)

        assert (await env.page(host, "/approval-check"))["body"] == "unknown"
        assert env.row(host, SDK)["approvals"] == []
        assert "approvals deleted: 1" in env.audit.summaries("plugin_lifecycle")


class TestUninstallForgetsApprovals:
    async def test_removal_deletes_approvals(self, env):
        plugin_dir = env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        approval_id = (await env.page(host, "/approval"))["body"].split()[0]
        env.registry.answer(approval_id, "confirm")
        await until(lambda: "code; approved" in env.audit.summaries("plugin_approval"))
        await host.disable(SDK)
        for child in plugin_dir.iterdir():
            child.unlink()
        plugin_dir.rmdir()

        await host.rescan()

        assert host._approval_store.for_plugin(SDK) == []
        summaries = env.audit.summaries("plugin_lifecycle")
        assert "approvals deleted: 1" in summaries
        assert summaries[-1] == "removed; data and rules deleted"


class TestPendingCardsEndWithThePlugin:
    async def _open_cards(self, env):
        plugin_dir = env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        approval_id = (await env.page(host, "/approval"))["body"].split()[0]
        confirm_id = (await env.page(host, "/confirm"))["body"]
        return host, approval_id, confirm_id, plugin_dir

    async def test_a_running_plugins_connector_owns_the_cards_it_requested(self, env):
        host, approval_id, confirm_id, _ = await self._open_cards(env)
        owns = host.connectors()[SDK]._owns_approval

        assert owns(confirm_id) is True
        assert owns(approval_id) is True
        assert owns("0" * 32) is False

    async def test_disable_expires_pending_cards(self, env):
        host, approval_id, confirm_id, _ = await self._open_cards(env)

        await host.disable(SDK)

        assert env.registry.await_status(approval_id) == "expired"
        assert env.registry.await_status(confirm_id) == "expired"
        assert env.registry.answer(approval_id, "confirm") is False
        await until(lambda: "code; expired" in env.audit.summaries("plugin_approval"))
        assert host._approval_store.for_plugin(SDK) == []

    async def test_purge_expires_pending_cards_and_stores_nothing(self, env):
        host, approval_id, confirm_id, _ = await self._open_cards(env)

        await host.purge(SDK)

        assert env.registry.await_status(approval_id) == "expired"
        assert env.registry.await_status(confirm_id) == "expired"
        assert env.registry.answer(approval_id, "confirm") is False
        assert host._approval_store.for_plugin(SDK) == []

    async def test_removal_expires_pending_cards(self, env):
        host, approval_id, confirm_id, plugin_dir = await self._open_cards(env)
        TestUninstall._remove(plugin_dir)

        await host.rescan()

        assert env.registry.await_status(approval_id) == "expired"
        assert env.registry.await_status(confirm_id) == "expired"
        await until(lambda: "code; expired" in env.audit.summaries("plugin_approval"))
        assert host._approval_store.for_plugin(SDK) == []


class TestOutputDir:
    async def test_created_0700_and_passed_only_with_outputs(self, env):
        env.add(SDK, sdk=True, outputs=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        expected = storage.output_dir(SDK, LOCAL_PRINCIPAL)
        assert json.loads((await env.page(host, "/outdir"))["body"]) == {"dir": str(expected)}
        assert expected.is_dir()
        if sys.platform != "win32":
            assert expected.stat().st_mode & 0o777 == 0o700

    async def test_not_created_without_outputs(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        assert json.loads((await env.page(host, "/outdir"))["body"]) == {"dir": "None"}
        assert not storage.output_dir(SDK, LOCAL_PRINCIPAL).exists()

    async def test_inspect_summary_and_row_report_outputs(self, env):
        env.add(SDK, sdk=True, outputs=True, output_types=["application/json"])
        host = env.host()
        await host.start()

        summary = await host.inspect(SDK)

        assert summary["outputs"] is True and summary["output_types"] == ["application/json"]
        assert env.row(host, SDK)["outputs"] is True


class TestOutputsConnector:
    async def test_appears_when_enabled_and_disappears_when_disabled(self, env):
        env.add(SDK, sdk=True, outputs=True)
        host = env.host()
        await host.start()
        assert "plugin_outputs" not in host.connectors()
        before = env.tools_changed

        try:
            await env.enable(host, SDK)
            assert "plugin_outputs" in host.connectors()
            after_enable = env.tools_changed
            assert after_enable > before

            await host.disable(SDK)
            assert "plugin_outputs" not in host.connectors()
            assert env.tools_changed > after_enable
        finally:
            await host.stop_all()

    async def test_absent_for_a_plugin_without_outputs(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        assert "plugin_outputs" not in host.connectors()


class TestRowsApprovals:
    async def test_newest_first_with_the_documented_fields(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        approval_id = (await env.page(host, "/approval"))["body"].split()[0]
        env.registry.answer(approval_id, "confirm")
        await until(lambda: "code; approved" in env.audit.summaries("plugin_approval"))

        (entry,) = env.row(host, SDK)["approvals"]

        assert set(entry) == {"approval_id", "kind", "subject_id", "digest", "decided_at", "revoked_at"}
        assert entry["kind"] == "code" and entry["subject_id"] == "main" and entry["revoked_at"] is None

    async def test_empty_by_default(self, env):
        env.add("stub")
        host = env.host()
        await host.start()

        assert env.row(host, "stub")["approvals"] == []
        assert env.row(host, "stub")["outputs"] is False


class TestPurge:
    async def test_deletes_after_timeout(self, env):
        env.add(SDK, sdk=True, mode="purge-hang")
        host = env.host()
        host.purge_timeout = 0.3
        await host.start()
        await env.enable(host, SDK)
        leftover = storage.install_dir(SDK) / "cache.db"
        leftover.write_text("x")
        mine = storage.principal_dir(SDK, LOCAL_PRINCIPAL) / "mine.db"
        mine.write_text("y")

        outcome = await host.purge(SDK)

        assert outcome == "timeout"
        assert not leftover.exists() and not mine.exists()
        assert env.audit.summaries("plugin_lifecycle")[-1] == "data purged (timeout)"
        assert env.row(host, SDK)["state"] == "running"

    async def test_ack(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        leftover = storage.install_dir(SDK) / "cache.db"
        leftover.write_text("x")

        outcome = await host.purge(SDK)

        assert outcome == "ack" and not leftover.exists()
        assert env.audit.summaries("plugin_lifecycle")[-1] == "data purged (ack)"
        assert env.row(host, SDK)["state"] == "running"

    async def test_plugin_that_does_not_know_the_method_still_loses_its_data(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        leftover = storage.install_dir("stub") / "cache.db"
        leftover.write_text("x")

        outcome = await host.purge("stub")

        assert outcome == "timeout" and not leftover.exists()

    async def test_not_running_is_not_restarted(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        storage.ensure_dirs("stub", [LOCAL_PRINCIPAL])
        leftover = storage.install_dir("stub") / "cache.db"
        leftover.write_text("x")

        outcome = await host.purge("stub")

        assert outcome == "ack" and not leftover.exists()
        assert env.row(host, "stub")["state"] == "discovered"
        assert env.audit.summaries("plugin_lifecycle")[-1] == "data purged (ack)"


class TestUninstall:
    @staticmethod
    def _remove(plugin_dir: Path) -> None:
        for child in plugin_dir.iterdir():
            child.unlink()
        plugin_dir.rmdir()

    async def test_deletes_directories_and_rules(self, env):
        plugin_dir = env.add(SDK, sdk=True)
        config = env.root / "settings.yaml"
        config.write_text(yaml.safe_dump({}), encoding="utf-8")
        auto_accept.init_config_path(str(config))
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        auto_accept.add_policy_v2_rules(policy_rules({
            f"plugin.{SDK}.lookup": [{"predicate": f"plugin:{SDK}:calendar", "value": "primary"}],
            "gmail.send": [{"predicate": "gmail.anything"}],
        }))
        (storage.install_dir(SDK) / "cache.db").write_text("x")
        assert env.row(host, SDK)["state"] == "running"
        # Windows refuses to delete a running plugin's folder (it is the process's working
        # directory), so the administrator disables it first.
        await host.disable(SDK)

        self._remove(plugin_dir)
        await host.rescan()

        assert host.rows() == []
        assert not (env.data / "plugin-data" / SDK).exists()
        assert host._store.load() == {}
        remaining = [r.predicate for r in auto_accept.get_policy_v2_store_rules()]
        assert remaining == ["gmail.anything"]
        assert env.audit.summaries("plugin_lifecycle")[-1] == "removed; data and rules deleted"
        assert host.connectors() == {}

    async def test_a_running_plugin_that_disappears_is_stopped_and_uninstalled(self, env, monkeypatch):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        proc = host._plugins["stub"].supervisor._child.proc
        assert env.row(host, "stub")["state"] == "running"
        # The folder stays on disk: a running plugin's folder cannot be deleted on Windows.
        monkeypatch.setattr(host_mod.trust, "discover", lambda *_a, **_k: [])

        await host.rescan()

        assert proc.returncode is not None
        assert host.rows() == []
        assert host._store.load() == {}
        assert env.audit.summaries("plugin_lifecycle")[-1] == "removed; data and rules deleted"

    async def test_unreadable_dir_deletes_nothing(self, env):
        plugin_dir = env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        await host.stop_all()
        (storage.install_dir("stub") / "cache.db").write_text("x")
        moved = env.root / "moved-away"
        plugin_dir.parent.rename(moved)

        await host.rescan()

        assert (storage.install_dir("stub") / "cache.db").exists()
        assert "stub" in host._store.load()
        row = env.row(host, "stub")
        assert row["state"] == "missing" and row["reason"] == "plugins directory unreadable"
        assert "removed; data and rules deleted" not in env.audit.summaries("plugin_lifecycle")

        moved.rename(env.plugins)
        await host.rescan()

        assert env.row(host, "stub")["state"] == "running"
        assert (storage.install_dir("stub") / "cache.db").exists()

    async def test_unreadable_dir_at_startup_lists_the_known_plugins_as_missing(self, env):
        env.add("stub")
        first = env.host()
        await first.start()
        await env.enable(first, "stub")
        await first.stop_all()
        gone = env.root / "gone"
        env.plugins.rename(gone)

        second = env.host()
        await second.start()

        row = env.row(second, "stub")
        assert row["state"] == "missing" and row["display_name"] == "stub" and row["version"] == ""
        assert (storage.install_dir("stub")).is_dir()

    async def test_failure_is_retried_on_the_next_rescan(self, env, monkeypatch):
        plugin_dir = env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        await host.stop_all()
        self._remove(plugin_dir)
        calls: list[str] = []

        def failing(name):
            calls.append(name)
            raise OSError("busy")

        monkeypatch.setattr(host_mod.storage, "remove_all", failing)
        await host.rescan()

        assert calls == ["stub"] and "stub" in host._store.load() and "stub" in host._plugins

        monkeypatch.undo()
        await host.rescan()

        assert host._store.load() == {} and host.rows() == []

    async def test_plugin_without_a_record_just_disappears(self, env):
        plugin_dir = env.add("stub")
        host = env.host()
        await host.start()
        assert [r["name"] for r in host.rows()] == ["stub"]

        self._remove(plugin_dir)
        await host.rescan()

        assert host.rows() == []
        assert env.audit.summaries("plugin_lifecycle") == []


class TestToolsChangedNotifiesListener:
    async def test_removal_is_accepted(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        before = env.tools_changed

        assert (await env.page(host, "/drop"))["body"] == "ok"
        await until(lambda: env.tools_changed > before)

        names = [spec.name for spec in host.connectors()[SDK].tool_specs()]
        assert names == ["sdk-demo_ping"]
        assert "tools changed: -lookup" in env.audit.summaries("plugin_lifecycle")
        assert env.row(host, SDK)["tools_note"] == ""


class TestToolsChangedOutsideReviewRejected:
    async def test_added_tool_is_rejected(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        before = env.tools_changed
        connector = host.connectors()[SDK]

        await env.page(host, "/widen")
        await until(lambda: connector.last_tools_rejection is not None)

        assert env.tools_changed == before
        assert [spec.name for spec in connector.tool_specs()] == ["sdk-demo_ping", "sdk-demo_lookup"]
        detail = (
            "tool wide was not in the list reviewed at enable; review and enable the plugin again"
        )
        assert env.row(host, SDK)["tools_note"] == f"last tools change rejected: {detail}"
        assert f"tools change rejected: {detail}" in env.audit.summaries("plugin_lifecycle")


class TestWebRequest:
    async def test_page_and_row(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        response = await env.page(host, "/info")

        assert response["status"] == 200
        assert env.row(host, SDK)["page_url"] == f"/plugins/{SDK}/"

    async def test_page_links_list_only_running_plugins_with_pages(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        assert host.page_links() == []

        await env.enable(host, SDK)
        assert host.page_links() == [(host._plugins[SDK].manifest.display_name, f"/plugins/{SDK}/")]

        host._plugins[SDK].state = "starting"
        assert host.page_links() == []

    async def test_missing_page(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        assert (await env.page(host, "/nothing"))["status"] == 404

    async def test_not_until_the_plugin_is_running(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)
        host._plugins[SDK].state = "starting"

        with pytest.raises(LookupError):
            await env.page(host, "/info")

    async def test_not_running(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()

        with pytest.raises(LookupError):
            await env.page(host, "/info")
        assert env.row(host, SDK)["page_url"] == ""

    async def test_manifest_without_pages(self, env):
        env.add("stub", pages=False)
        host = env.host()
        await host.start()
        await env.enable(host, "stub")

        with pytest.raises(LookupError):
            await host.web_request("stub", "/", {}, LOCAL_PRINCIPAL)

    async def test_unknown_plugin(self, env):
        host = env.host()
        await host.start()

        with pytest.raises(LookupError):
            await host.web_request("nothing", "/", {}, LOCAL_PRINCIPAL)


class TestRowsChangedListener:
    async def test_called_after_every_change(self, env):
        env.add("stub")
        host = env.host()
        await host.start()
        seen = env.rows_changed
        assert seen > 0

        summary = await host.inspect("stub")
        after_inspect = env.rows_changed
        await host.enable(
            "stub", executable_sha256=summary["executable_sha256"], manifest_sha256=summary["manifest_sha256"]
        )
        after_enable = env.rows_changed
        await host.disable("stub")

        assert seen < after_inspect < after_enable < env.rows_changed

    async def test_a_failing_listener_does_not_break_the_host(self, env):
        env.add("stub")
        host = env.host()

        def boom() -> None:
            raise RuntimeError("listener")

        host.set_rows_changed_listener(boom)
        host.set_tools_changed_listener(boom)
        await host.start()
        await env.enable(host, "stub")

        assert env.row(host, "stub")["state"] == "running"

    async def test_connector_changes_reach_a_running_plugin(self, env):
        env.add(SDK, sdk=True)
        host = env.host()
        await host.start()
        await env.enable(host, SDK)

        host.on_connectors_changed([{"key": "gmail", "enabled": True, "authed": False}])
        assert json.loads((await env.page(host, "/events"))["body"]) == []
        host.on_connectors_changed([{"key": "gmail", "enabled": True, "authed": True}])

        async def received() -> list:
            return json.loads((await env.page(host, "/events"))["body"])

        deadline = time.monotonic() + 15
        while not await received():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        assert await received() == [{"connector": "gmail", "state": "signed_in", "principal": "local"}]

    async def test_a_plugin_that_is_not_running_is_not_sent_events(self, env):
        env.add("stub")
        host = env.host()
        await host.start()

        assert host._running_peers() == []
        host.on_connectors_changed([{"key": "gmail", "enabled": True, "authed": False}])
        host.on_connectors_changed([{"key": "gmail", "enabled": False, "authed": False}])

    async def test_events_before_the_host_runs_are_dropped_quietly(self, env):
        host = env.host()

        host.on_connectors_changed([{"key": "gmail", "enabled": True, "authed": False}])
        host.on_connectors_changed([{"key": "gmail", "enabled": False, "authed": False}])

    async def test_a_scheduling_failure_does_not_reach_the_caller(self, env, monkeypatch):
        host = env.host()
        await host.start()
        monkeypatch.setattr(host, "submit", MagicMock(side_effect=RuntimeError("closed")))
        host.on_connectors_changed([{"key": "gmail", "enabled": True, "authed": False}])

        host.on_connectors_changed([{"key": "gmail", "enabled": False, "authed": False}])

    async def test_submit_runs_on_the_hosts_loop(self, env):
        host = env.host()
        await host.start()
        results: list[str] = []

        async def work() -> str:
            results.append("ran")
            return "done"

        future = host.submit(work())

        assert await asyncio.wrap_future(future) == "done" and results == ["ran"]

    async def test_submit_before_start_is_refused(self, env):
        host = env.host()

        async def work() -> None:  # pragma: no cover -- never scheduled
            return None

        with pytest.raises(RuntimeError, match="not running"):
            host.submit(work())


class TestDaemonThreadExecutor:
    async def test_runs_jobs_and_reports_results(self):
        executor = host_mod._DaemonThreadExecutor(2)

        assert await asyncio.wrap_future(executor.submit(lambda a, b: a + b, 1, 2)) == 3

        def fail() -> None:
            raise ValueError("job")

        with pytest.raises(ValueError, match="job"):
            await asyncio.wrap_future(executor.submit(fail))

    async def test_cap(self):
        executor = host_mod._DaemonThreadExecutor(1)
        release = threading.Event()
        first = executor.submit(release.wait)

        with pytest.raises(RuntimeError, match="too many"):
            executor.submit(lambda: None)

        release.set()
        await asyncio.wrap_future(first)
        await until(lambda: executor._active == 0)
        assert await asyncio.wrap_future(executor.submit(lambda: "again")) == "again"

    async def test_cancelled_job_is_not_run(self, monkeypatch):
        started: list = []

        class FakeThread:
            def __init__(self, target, **_kwargs) -> None:
                self.target = target

            def start(self) -> None:
                started.append(self.target)

        # Only the host module's view of threading: the test runner's own threads stay real.
        monkeypatch.setattr(host_mod, "threading", SimpleNamespace(**{**vars(threading), "Thread": FakeThread}))
        executor = host_mod._DaemonThreadExecutor(1)
        ran: list[str] = []
        future = executor.submit(lambda: ran.append("late"))
        assert future.cancel()

        started[0]()

        assert ran == [] and executor._active == 0


@pytest.fixture
def spawned(monkeypatch):
    """Every child process any supervisor spawns during the test, introspection runs included."""
    procs: list = []
    original = supervisor_mod.Supervisor._spawn

    async def spy(self, *args, **kwargs):
        child = await original(self, *args, **kwargs)
        procs.append(child.proc)
        return child

    monkeypatch.setattr(supervisor_mod.Supervisor, "_spawn", spy)
    return procs


def _alive(procs: list) -> list:
    return [p for p in procs if p.returncode is None]


class TestActionsRunOneAtATime:
    async def test_rescan_during_purge_leaves_one_supervisor(self, env, spawned, monkeypatch):
        monkeypatch.setattr(supervisor_mod, "SHUTDOWN_GRACE_SECONDS", 0.5)
        monkeypatch.setattr(supervisor_mod, "TERMINATE_GRACE_SECONDS", 0.5)
        env.add("stub", mode="slow-shutdown")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")

        purge = asyncio.create_task(host.purge("stub"))
        await asyncio.sleep(0)
        await host.rescan()
        await purge

        await until(lambda: len(_alive(spawned)) <= 1)
        live = _alive(spawned)
        supervisor = host._plugins["stub"].supervisor
        assert supervisor is not None
        assert live == [supervisor._child.proc]
        assert env.row(host, "stub")["state"] == "running"

    @pytest.mark.parametrize("disable_first", [True, False])
    async def test_disable_racing_rescan_leaves_nothing_running(self, env, spawned, disable_first):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")

        first, second = (host.disable("stub"), host.rescan()) if disable_first else (host.rescan(), host.disable("stub"))
        await asyncio.gather(first, second)

        await until(lambda: not _alive(spawned))
        row = env.row(host, "stub")
        assert row["state"] == "disabled" and row["reason"] == "disabled by you"
        assert host._store.load()["stub"].enabled is False
        assert host._plugins["stub"].supervisor is None

    async def test_actions_run_one_at_a_time(self, env, spawned):
        env.add(SDK, sdk=True, mode="purge-hang")
        host = env.host()
        host.purge_timeout = 0.5
        await host.start()
        await env.enable(host, SDK)

        purge = asyncio.create_task(host.purge(SDK))
        await asyncio.sleep(0)
        await host.disable(SDK)
        await purge

        summaries = env.audit.summaries("plugin_lifecycle")
        assert summaries.index("data purged (timeout)") < len(summaries) - 1 - summaries[::-1].index("disabled")
        await until(lambda: not _alive(spawned))
        assert host._plugins[SDK].supervisor is None

    async def test_start_plugin_refuses_a_second_supervisor(self, env, spawned, caplog):
        env.add("stub")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        plugin = host._plugins["stub"]
        supervisor = plugin.supervisor
        count = len(spawned)

        with caplog.at_level("WARNING", logger=host_mod.logger.name):
            await host._start_plugin(plugin)

        assert len(spawned) == count
        assert plugin.supervisor is supervisor
        assert any("already has a supervisor" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)

    async def test_action_queued_before_stop_all_does_nothing(self, env, spawned, monkeypatch):
        monkeypatch.setattr(host_mod, "STOP_ALL_LOCK_WAIT_SECONDS", 0.2)
        env.add(SDK, sdk=True, mode="purge-hang")
        env.add("stub")
        host = env.host()
        host.purge_timeout = 2
        await host.start()
        await env.enable(host, SDK)

        purge = asyncio.create_task(host.purge(SDK))
        await asyncio.sleep(0)
        rescan = asyncio.create_task(host.rescan())
        enable = asyncio.create_task(host.enable("stub", executable_sha256="x", manifest_sha256="y"))
        await asyncio.sleep(0)
        await host.stop_all()
        results = await asyncio.gather(purge, rescan, enable, return_exceptions=True)

        assert results[1] is None
        assert isinstance(results[2], ValueError)
        await until(lambda: not _alive(spawned))
        assert env.row(host, "stub")["last_error"] == host_mod.REASON_STOPPING

    async def test_stop_all_does_not_wait_for_a_long_action(self, env, spawned, monkeypatch):
        monkeypatch.setattr(host_mod, "STOP_ALL_LOCK_WAIT_SECONDS", 0.2)
        env.add(SDK, sdk=True, mode="purge-hang")
        host = env.host()
        host.purge_timeout = 2
        await host.start()
        await env.enable(host, SDK)

        purge = asyncio.create_task(host.purge(SDK))
        await asyncio.sleep(0)
        started = time.monotonic()
        await host.stop_all()
        elapsed = time.monotonic() - started

        assert elapsed < 1.5
        assert not _alive(spawned)
        await asyncio.gather(purge, return_exceptions=True)
        assert not _alive(spawned)

    async def test_disable_completes_for_a_plugin_that_stopped_reading(self, env, spawned):
        env.add("stub", mode="stop-reading")
        host = env.host()
        await host.start()
        await env.enable(host, "stub")
        count = len(spawned)
        peer = host._plugins["stub"].supervisor.peer
        filler = asyncio.create_task(peer.notify("x", {"pad": "x" * 512_000}))
        await asyncio.sleep(0.1)

        await asyncio.wait_for(host.disable("stub"), 5)

        await asyncio.gather(filler, return_exceptions=True)
        await until(lambda: not _alive(spawned))
        row = env.row(host, "stub")
        assert row["state"] == "disabled" and row["reason"] == "disabled by you"
        assert host._plugins["stub"].supervisor is None
        assert len(spawned) == count
