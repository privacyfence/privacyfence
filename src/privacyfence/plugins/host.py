"""``PluginHost``: composes the supervisor, trust checks, state, source API, confirmations and
tool exposure into one object the daemon owns (ADR 0120, ADR 0121).

Everything here runs on the web server's event loop. ``start`` records that loop, MCP calls and
page routes already run on it and await host coroutines directly, and Settings (which calls the
controller synchronously, also on the loop) never waits on the host: it calls ``submit``, which
schedules the coroutine and returns at once. Results reach the page through the rows-changed
listener, which the host calls after every state change, inspection result and error.

A plugin is only ever started for an administrator-reviewed record. ``inspect`` runs the binary in
introspection mode (no source reads, no confirmations) and returns what a human is asked to
approve; ``enable`` re-hashes the files and refuses unless they still match both that inspection
and the hashes the caller passed in, so what runs is what was reviewed.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
from collections.abc import Callable, Coroutine, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from privacyfence import __version__, auto_accept, paths, privilege_separation
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.audit_log import AuditEntry, current_week, get_audit_logger
from privacyfence.connector import Connector
from privacyfence.plugins import manifest as manifest_mod
from privacyfence.plugins import source_ops, storage, trust
from privacyfence.plugins.confirm import ConfirmationService
from privacyfence.plugins.constants import (
    AUDIT_PLUGIN_CONFIRM,
    AUDIT_PLUGIN_LIFECYCLE,
    INLINE_RESULT_BYTES,
    MAX_IN_FLIGHT,
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    TIMEOUT_SECONDS,
    mcp_tool_name,
)
from privacyfence.plugins.connector import PluginConnector
from privacyfence.plugins.manifest import Manifest, ManifestError
from privacyfence.plugins.protocol import InitializeResult, RpcError, principal_context
from privacyfence.plugins.spool import DownloadSpool
from privacyfence.plugins.state import HASH_DRIFT_REASON, PluginStateStore, STATE_FILENAME
from privacyfence.plugins.supervisor import LaunchSpec, StartError, Supervisor
from privacyfence.plugins.tools import ToolDefError, tool_signature, validate_tool_defs
from privacyfence.principal import LOCAL_PRINCIPAL, Principal

logger = logging.getLogger(__name__)

REASON_FEATURE_OFF = "plugins are turned off in settings"
REASON_NO_SEPARATION = "plugins need PrivacyFence's background service"
REASON_DIRECTORY_UNREADABLE = "plugins directory unreadable"
CHANGED_SINCE_REVIEW = "The plugin changed since you reviewed it; review it again."
_ACTION_FAILED = "The action failed; the log has the details."
_MAX_CONFIRM_THREADS = 64


class _DaemonThreadExecutor(concurrent.futures.Executor):
    """One daemon thread per submitted job, up to a cap.

    A confirmation finalizer waits for a human, up to the card's lifetime. A pool's threads are
    joined at interpreter exit, which would hold the daemon's shutdown for that long; a daemon
    thread dies with the process instead.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            if self._active >= self._limit:
                raise RuntimeError("too many pending plugin confirmations")
            self._active += 1

        def run() -> None:
            try:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)
            finally:
                with self._lock:
                    self._active -= 1

        threading.Thread(target=run, daemon=True, name="plugin-confirm").start()
        return future


class _Plugin:
    """Everything the host knows about one plugin directory."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.discovered: trust.DiscoveredPlugin | None = None
        self.state = "discovered"
        self.reason: str | None = None
        self.supervisor: Supervisor | None = None
        self.connector: PluginConnector | None = None
        self.review: dict | None = None
        self.review_signatures: frozenset[tuple] = frozenset()
        self.last_error: str | None = None
        self.intentional_stop = False
        self.reviewed_violation = False

    @property
    def manifest(self) -> Manifest | None:
        return self.discovered.manifest if self.discovered is not None else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PluginHost:
    def __init__(
        self,
        *,
        plugins_dir: Path | None = None,
        connectors_provider: Callable[[], dict[str, Connector]],
        connector_state: Callable[[str], tuple[bool, str | None]],
        registry_provider: Callable[[], PendingApprovalRegistry],
        trust_check: Callable[[Path, Path], str | None] = trust.admin_only_problem,
        command_resolver: Callable[[Manifest, Path], list[str]] = manifest_mod.resolve_command,
        separation_enabled: Callable[[], bool] = privilege_separation.is_enabled,
        feature_enabled: bool = True,
        daemon_version: str = __version__,
    ) -> None:
        self._plugins_dir = plugins_dir if plugins_dir is not None else trust.plugins_dir()
        self._connectors_provider = connectors_provider
        self._connector_state = connector_state
        self._trust_check = trust_check
        self._command_resolver = command_resolver
        self._separation_enabled = separation_enabled
        self._feature_enabled = feature_enabled
        self._daemon_version = daemon_version
        self._store = PluginStateStore(paths.data_dir() / STATE_FILENAME)
        self._plugins: dict[str, _Plugin] = {}
        self._spool: DownloadSpool | None = None
        self._directory_unreadable = False
        self._connector_rows: list[dict] = []
        self._tools_changed: Callable[[], None] = lambda: None
        self._rows_changed: Callable[[], None] = lambda: None
        self._unattended: Callable[[], bool] = lambda: False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.purge_timeout = TIMEOUT_SECONDS["storage.purge"]
        self._confirm = ConfirmationService(
            registry_provider=registry_provider,
            unattended_active=lambda: self._unattended(),
            executor=_DaemonThreadExecutor(_MAX_CONFIRM_THREADS),
            audit=self._audit_confirm,
        )

    # ── Listeners and the loop ────────────────────────────────────────

    def set_tools_changed_listener(self, fn: Callable[[], None]) -> None:
        self._tools_changed = fn

    def set_unattended_provider(self, fn: Callable[[], bool]) -> None:
        self._unattended = fn

    def set_rows_changed_listener(self, fn: Callable[[], None]) -> None:
        self._rows_changed = fn

    def on_connectors_changed(self, rows: list[dict]) -> None:
        self._connector_rows = list(rows)

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        """Schedule ``coro`` on the host's loop and return at once."""
        if self.loop is None:
            coro.close()
            raise RuntimeError("The plugin host is not running.")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _notify_tools(self) -> None:
        try:
            self._tools_changed()
        except Exception:
            logger.warning("The tools-changed listener failed", exc_info=True)

    def _changed(self) -> None:
        try:
            self._rows_changed()
        except Exception:
            logger.warning("The rows-changed listener failed", exc_info=True)

    # ── Audit ─────────────────────────────────────────────────────────

    def _record(self, plugin: str, tool: str, summary: str, decision: str) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=_now(),
                week=current_week(),
                request_id="",
                connector=f"plugin:{plugin}",
                tool=tool,
                tool_name=f"{plugin} {tool}",
                summary=summary,
                sender="",
                decision=decision,
                auto_accept_rule="",
                latency_seconds=0.0,
                claude_reason="",
            ))
        except Exception:
            logger.warning("Could not write the audit entry for plugin %s", plugin, exc_info=True)

    def _audit_lifecycle(self, plugin: str, summary: str) -> None:
        self._record(plugin, "lifecycle", summary, AUDIT_PLUGIN_LIFECYCLE)

    def _audit_confirm(self, plugin: str, kind: str, status: str) -> None:
        self._record(plugin, "confirm", f"{kind}; {status}", AUDIT_PLUGIN_CONFIRM)

    # ── Lifecycle ─────────────────────────────────────────────────────

    def _blocked_reason(self) -> str | None:
        if not self._feature_enabled:
            return REASON_FEATURE_OFF
        if not self._separation_enabled():
            return REASON_NO_SEPARATION
        return None

    async def start(self) -> None:
        """Remember the loop, list the plugins directory, and start every enabled plugin that
        passes its checks."""
        self.loop = asyncio.get_running_loop()
        await self.rescan()

    async def stop_all(self) -> None:
        running = [p for p in self._plugins.values() if p.supervisor is not None]
        await asyncio.gather(*(self._stop(p, "shutdown") for p in running))
        if self._spool is not None:
            self._spool.clear()

    async def rescan(self) -> None:
        try:
            found = await asyncio.to_thread(trust.discover, self._plugins_dir, trust_check=self._trust_check)
        except OSError as exc:
            # Could not look: that is not "nothing is installed", so nothing is deleted.
            logger.warning("Could not list the plugins directory: %s", exc)
            self._directory_unreadable = True
            self._changed()
            return
        self._directory_unreadable = False
        present = {d.dir_name for d in found}
        for name in sorted(set(self._plugins) | set(self._store.load())):
            if name not in present:
                await self._gone(name)
        for discovered in found:
            await self._settle(discovered)
        await self._start_eligible()
        self._changed()

    async def _gone(self, name: str) -> None:
        """A plugin whose directory is no longer there. With a state record it is uninstalled;
        without one there is nothing of it to delete."""
        plugin = self._plugins.get(name)
        if plugin is not None and plugin.supervisor is not None:
            await self._stop(plugin, "shutdown")
        if plugin is not None and plugin.connector is not None:
            plugin.connector.clear_tools()
            self._notify_tools()
        if name in self._store.load():
            try:
                await asyncio.to_thread(self._uninstall, name)
            except Exception:
                logger.warning("Could not finish removing plugin %s; will retry on the next rescan", name, exc_info=True)
                return
        self._plugins.pop(name, None)

    def _uninstall(self, name: str) -> None:
        storage.remove_all(name)
        prefix = f"plugin:{name}:"
        for rule in list(auto_accept.get_policy_v2_store_rules()):
            if rule.predicate.startswith(prefix):
                auto_accept.remove_policy_v2_rule(rule.id)
        self._store.forget(name)
        self._audit_lifecycle(name, "removed; data and rules deleted")

    async def _settle(self, discovered: trust.DiscoveredPlugin) -> None:
        plugin = self._plugins.setdefault(discovered.dir_name, _Plugin(discovered.dir_name))
        plugin.discovered = discovered
        blocked = self._blocked_reason()
        if plugin.supervisor is not None:
            drift = self._store.check_hashes(discovered)
            if drift is not None:
                await self._stop(plugin, "hash_changed")
                self._mark_disabled(plugin, drift)
            elif blocked is not None:
                await self._stop(plugin, "shutdown")
                plugin.state, plugin.reason = "disabled", blocked
            return
        record = self._store.load().get(plugin.name)
        if blocked is not None:
            plugin.state, plugin.reason = "disabled", blocked
        elif discovered.problem is not None:
            plugin.state, plugin.reason = "rejected", discovered.problem
        elif record is None:
            plugin.state, plugin.reason = "discovered", None
        elif not record.enabled:
            plugin.state, plugin.reason = "disabled", record.disabled_reason
        else:
            plugin.state, plugin.reason = "discovered", None

    def _mark_disabled(self, plugin: _Plugin, reason: str) -> None:
        plugin.state, plugin.reason = "disabled", reason
        self._audit_lifecycle(plugin.name, f"disabled: {reason}")
        if plugin.connector is not None:
            plugin.connector.clear_tools()
            self._notify_tools()

    async def _start_eligible(self) -> None:
        if self._blocked_reason() is not None:
            return
        records = self._store.load()
        todo: list[_Plugin] = []
        for plugin in self._plugins.values():
            record = records.get(plugin.name)
            discovered = plugin.discovered
            if (record is None or not record.enabled or plugin.supervisor is not None
                    or discovered is None or discovered.problem is not None):
                continue
            drift = self._store.check_hashes(discovered)
            if drift is not None:
                self._mark_disabled(plugin, drift)
                continue
            todo.append(plugin)
        await asyncio.gather(*(self._start_plugin(p) for p in todo))

    # ── Starting and stopping one plugin ──────────────────────────────

    def _initialize_params(self, plugin: _Plugin, purpose: str) -> dict:
        manifest = plugin.manifest
        assert manifest is not None  # nosec B101  # only plugins with a manifest are started
        shared, per_principal = storage.ensure_dirs(plugin.name, [LOCAL_PRINCIPAL])
        return {
            "protocol_version": PROTOCOL_VERSION,
            "purpose": purpose,
            "mode": "local",
            "daemon": {"name": "privacyfence", "version": self._daemon_version},
            "plugin": {"name": manifest.name, "manifest_version": manifest.version},
            "data_dir": str(shared),
            "principals": [principal_context(LOCAL_PRINCIPAL, per_principal[LOCAL_PRINCIPAL.id])],
            "limits": {
                "max_line_bytes": MAX_LINE_BYTES,
                "max_in_flight": MAX_IN_FLIGHT,
                "inline_result_bytes": INLINE_RESULT_BYTES,
            },
        }

    def _get_spool(self) -> DownloadSpool:
        if self._spool is None:
            self._spool = DownloadSpool(paths.data_dir() / "plugin-spool")
        return self._spool

    def _launch_spec(self, plugin: _Plugin) -> LaunchSpec:
        discovered = plugin.discovered
        assert discovered is not None and discovered.manifest is not None  # nosec B101
        argv = self._command_resolver(discovered.manifest, discovered.path)
        return LaunchSpec(plugin.name, argv, discovered.path, paths.data_dir() / "logs" / "plugins" / f"{plugin.name}.log")

    def _validate(self, plugin: _Plugin, result: InitializeResult, reviewed: frozenset[tuple] | None) -> None:
        manifest = plugin.manifest
        wire = [t.to_wire() for t in result.tools]
        validate_tool_defs(plugin.name, wire, result.scope_types, manifest)
        if reviewed is not None:
            try:
                validate_tool_defs(plugin.name, wire, result.scope_types, manifest, reviewed=reviewed)
            except ToolDefError:
                plugin.reviewed_violation = True
                raise

    def _handlers(self, plugin: _Plugin) -> tuple[dict, dict]:
        manifest = plugin.manifest
        assert manifest is not None  # nosec B101

        async def source_call(params: dict) -> Any:
            return await source_ops.handle_source_call(
                params,
                plugin=plugin.name,
                manifest=manifest,
                introspecting=False,
                connectors_provider=self._connectors_provider,
                connector_state=self._connector_state,
                spool=self._get_spool(),
            )

        async def confirm_request(params: dict) -> Any:
            try:
                return await self._confirm.request(plugin.name, manifest.display_name, params, introspecting=False)
            except RpcError as exc:
                if exc.code == "confirmation_refused":
                    # Refused only after the service validated the params, so the kind is sound.
                    self._audit_confirm(plugin.name, params["kind"], "refused")
                raise

        async def confirm_await(params: dict) -> Any:
            return await self._confirm.await_(plugin.name, params)

        async def tools_changed(params: dict) -> None:
            connector = plugin.connector
            if connector is None:
                return
            accepted = connector.handle_tools_changed(params)
            if accepted:
                self._notify_tools()
            self._changed()

        handlers = {"source.call": source_call, "confirm.request": confirm_request, "confirm.await": confirm_await}
        return handlers, {"tools.changed": tools_changed}

    async def _start_plugin(self, plugin: _Plugin) -> None:
        discovered = plugin.discovered
        record = self._store.load().get(plugin.name)
        if discovered is None or discovered.manifest is None or record is None:
            return
        manifest = discovered.manifest
        try:
            spec = self._launch_spec(plugin)
        except ManifestError as exc:
            plugin.state, plugin.reason = "rejected", f"manifest invalid: {exc}"
            return
        handlers, notifications = self._handlers(plugin)
        plugin.reviewed_violation = False

        async def on_ready(result: InitializeResult) -> None:
            connector = PluginConnector(
                plugin.name,
                manifest.display_name,
                manifest,
                lambda: supervisor.peer,
                lambda: principal_context(LOCAL_PRINCIPAL, storage.principal_dir(plugin.name, LOCAL_PRINCIPAL)),
                lambda summary: self._audit_lifecycle(plugin.name, summary),
                scope_types=result.scope_types,
                reviewed=record.reviewed,
            )
            connector.set_tools(result.tools)
            plugin.connector = connector

        supervisor = Supervisor(
            spec,
            initialize_params=lambda purpose: self._initialize_params(plugin, purpose),
            handlers=handlers,
            notification_handlers=notifications,
            on_state=lambda state, reason: self._on_state(plugin, state, reason),
            on_ready=on_ready,
            validate_tools=lambda result: self._validate(plugin, result, record.reviewed),
        )
        plugin.supervisor = supervisor
        try:
            await supervisor.start()
        except Exception:
            logger.warning("Plugin %s failed to start", plugin.name, exc_info=True)
            plugin.supervisor = None
            plugin.state, plugin.reason = "disabled", "could not start"
            self._changed()

    def _on_state(self, plugin: _Plugin, state: str, reason: str | None) -> None:
        was_running = plugin.state == "running"
        if state == "disabled" and not plugin.intentional_stop:
            if plugin.reviewed_violation:
                reason = HASH_DRIFT_REASON
            self._store.disable(plugin.name, reason or "")
            plugin.supervisor = None
            self._mark_disabled(plugin, reason or "")
        plugin.state, plugin.reason = state, reason
        if was_running != (state == "running"):
            self._notify_tools()
        self._changed()

    async def _stop(self, plugin: _Plugin, reason: str) -> None:
        """Stop a plugin on purpose: the caller records why, ``_on_state`` does not."""
        supervisor, plugin.supervisor = plugin.supervisor, None
        if supervisor is None:
            return
        plugin.intentional_stop = True
        try:
            await supervisor.stop(reason=reason)
        finally:
            plugin.intentional_stop = False
        if plugin.connector is not None:
            plugin.connector.clear_tools()
            plugin.connector = None
            self._notify_tools()

    # ── Actions ───────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _action(self, name: str) -> Iterator[_Plugin]:
        plugin = self._plugins.get(name)
        if plugin is None:
            raise LookupError(f"No plugin named {name}.")
        try:
            yield plugin
        except Exception as exc:
            if isinstance(exc, ValueError):
                plugin.last_error = str(exc)
            else:
                logger.warning("Action on plugin %s failed", name, exc_info=True)
                plugin.last_error = _ACTION_FAILED
            self._changed()
            raise
        else:
            plugin.last_error = None
            self._changed()

    async def _fresh(self, plugin: _Plugin) -> trust.DiscoveredPlugin:
        """Re-read the plugin from disk, so a review is of what is there now."""
        found = await asyncio.to_thread(trust.discover, self._plugins_dir, trust_check=self._trust_check)
        discovered = next((d for d in found if d.dir_name == plugin.name), None)
        if discovered is None:
            raise ValueError("The plugin is no longer installed.")
        plugin.discovered = discovered
        if discovered.problem is not None or discovered.manifest is None:
            raise ValueError(discovered.problem or "The plugin cannot be used.")
        return discovered

    async def inspect(self, name: str) -> dict:
        with self._action(name) as plugin:
            blocked = self._blocked_reason()
            if blocked is not None:
                raise ValueError(blocked)
            discovered = await self._fresh(plugin)
            manifest = discovered.manifest
            assert manifest is not None  # nosec B101
            captured: dict[str, InitializeResult] = {}

            def validate(result: InitializeResult) -> None:
                self._validate(plugin, result, None)
                captured["result"] = result

            async def never_ready(_result: InitializeResult) -> None:
                return None

            supervisor = Supervisor(
                self._launch_spec(plugin),
                initialize_params=lambda purpose: self._initialize_params(plugin, purpose),
                handlers={},
                notification_handlers={},
                on_state=lambda _state, _reason: None,
                on_ready=never_ready,
                validate_tools=validate,
            )
            try:
                await supervisor.introspect()
            except StartError as exc:
                raise ValueError(exc.reason) from None
            defs = captured["result"].tools
            plugin.review_signatures = frozenset(tool_signature(d) for d in defs)
            plugin.review = {
                "name": plugin.name,
                "display_name": manifest.display_name,
                "version": manifest.version,
                "executable_sha256": discovered.executable_sha256,
                "manifest_sha256": discovered.manifest_sha256,
                "max_gate_floor": manifest.max_gate_floor,
                "source_operations": sorted(manifest.source_operations),
                "pages": manifest.pages,
                "service_credentials": manifest.service_credentials,
                "tools": [
                    {
                        "name": mcp_tool_name(plugin.name, d.name),
                        "gate": d.gate,
                        "read_only": d.read_only,
                        "destructive": d.destructive,
                        "description": d.description,
                    }
                    for d in defs
                ],
            }
            return plugin.review

    async def enable(self, name: str, *, executable_sha256: str, manifest_sha256: str) -> None:
        with self._action(name) as plugin:
            blocked = self._blocked_reason()
            if blocked is not None:
                raise ValueError(blocked)
            discovered = await self._fresh(plugin)
            review = plugin.review
            hashes = (executable_sha256, manifest_sha256)
            if (review is None
                    or (review["executable_sha256"], review["manifest_sha256"]) != hashes
                    or (discovered.executable_sha256, discovered.manifest_sha256) != hashes):
                plugin.review = None
                raise ValueError(CHANGED_SINCE_REVIEW)
            manifest = discovered.manifest
            assert manifest is not None  # nosec B101
            await self._stop(plugin, "shutdown")
            self._store.enable(
                plugin.name,
                version=manifest.version,
                executable_sha256=executable_sha256,
                manifest_sha256=manifest_sha256,
                max_gate_floor=manifest.max_gate_floor,
                reviewed_tools=plugin.review_signatures,
            )
            plugin.review = None
            self._audit_lifecycle(plugin.name, "enabled")
            await self._start_plugin(plugin)

    async def disable(self, name: str) -> None:
        with self._action(name) as plugin:
            await self._stop(plugin, "user")
            self._store.disable(plugin.name, "disabled by you")
            plugin.state, plugin.reason = "disabled", "disabled by you"
            self._audit_lifecycle(plugin.name, "disabled")

    async def purge(self, name: str) -> str:
        """Delete every file the plugin holds. A running plugin is asked first and given
        ``purge_timeout`` to say it let go of them; a hung one does not stop the deletion."""
        with self._action(name) as plugin:
            supervisor = plugin.supervisor
            peer = supervisor.peer if supervisor is not None else None
            acknowledged = True
            if peer is not None:
                try:
                    await peer.request("storage.purge", {"scope": "all"}, timeout=self.purge_timeout)
                except (RpcError, OSError):
                    acknowledged = False
            await asyncio.to_thread(storage.remove_all, plugin.name)
            outcome = "ack" if acknowledged else "timeout"
            self._audit_lifecycle(plugin.name, f"data purged ({outcome})")
            if supervisor is not None:
                await self._stop(plugin, "shutdown")
                await self._start_plugin(plugin)
            return outcome

    # ── Reading ───────────────────────────────────────────────────────

    def connectors(self) -> dict[str, Connector]:
        return {
            name: plugin.connector
            for name, plugin in self._plugins.items()
            if plugin.state == "running" and plugin.connector is not None
        }

    def rows(self) -> list[dict]:
        records = self._store.load()
        names = set(self._plugins) | (set(records) if self._directory_unreadable else set())
        rows = []
        for name in sorted(names):
            plugin = self._plugins.get(name)
            manifest = plugin.manifest if plugin is not None else None
            record = records.get(name)
            state = plugin.state if plugin is not None else "discovered"
            reason = plugin.reason if plugin is not None else None
            if self._directory_unreadable:
                state, reason = "missing", REASON_DIRECTORY_UNREADABLE
            pages = manifest.pages if manifest is not None else False
            rejection = plugin.connector.last_tools_rejection if plugin is not None and plugin.connector else None
            rows.append({
                "name": name,
                "display_name": manifest.display_name if manifest is not None else name,
                "version": manifest.version if manifest is not None else "",
                "state": state,
                "reason": "" if state == "running" else (reason or ""),
                "enabled": bool(record is not None and record.enabled),
                "pages": pages,
                "page_url": f"/plugins/{name}/" if pages and state == "running" else "",
                "tools_note": f"last tools change rejected: {rejection}" if rejection else "",
                "review": plugin.review if plugin is not None else None,
                "last_error": plugin.last_error if plugin is not None else None,
            })
        return rows

    async def web_request(self, name: str, path: str, query: dict[str, str], principal: Principal) -> dict:
        plugin = self._plugins.get(name)
        manifest = plugin.manifest if plugin is not None else None
        supervisor = plugin.supervisor if plugin is not None else None
        peer = supervisor.peer if supervisor is not None else None
        if peer is None or manifest is None or not manifest.pages:
            raise LookupError(f"Plugin {name} is not serving pages.")
        context = principal_context(principal, storage.principal_dir(name, principal))
        return await peer.request(
            "web.request", {"principal": context, "method": "GET", "path": path, "query": query},
        )


__all__ = ["CHANGED_SINCE_REVIEW", "PluginHost"]
