"""Shared setup for the plugin framework's end-to-end tests (ADR 0120-0126).

``Stack`` builds what the daemon builds around plugins: a ``PluginHost`` over a temporary plugins
directory holding a copy of ``echo`` (or a refusal variant), an approvals registry, the audit log,
a ``McpDispatcher`` and, when asked, a real ``WebServer`` on a loopback socket. Approval cards are
answered by stubbing the gate's popups, as the guidelines ask; confirmations are answered on the
registry.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import sys
import time
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import yaml

from privacyfence import approval_ui, auto_accept, gate, paths
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.calendar_client import CalendarEvent
from privacyfence.plugins.host import PluginHost
from privacyfence.plugins.manifest import MANIFEST_FILENAME
from privacyfence.principal import LOCAL_PRINCIPAL
from privacyfence.web import state_stream as state_stream_module
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.server import WebServer
from privacyfence.web.session_auth import PROVENANCE_HUMAN
from privacyfence.web_approval_ui import WebApprovalUI
from tests.loop_watch import pending_io

FIXTURES = Path(__file__).resolve().parents[1]
ECHO_DIR = FIXTURES / "echo"
VARIANTS_DIR = FIXTURES / "echo-variants"
SDK_SRC = FIXTURES.parents[2] / "plugin-sdk" / "src"
# The SDK is not installed alongside the daemon; tests that import it get it from the source tree.
if str(SDK_SRC) not in sys.path:
    sys.path.insert(0, str(SDK_SRC))


def load_echo_plugin(module_name: str = "echo_plugin_in_process"):
    """Import ``echo_plugin.py`` into this process (the SDK test host runs a ``Plugin`` object) and
    return its module. Every call returns a fresh module, so state does not leak between tests."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(module_name, ECHO_DIR / "echo_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def calendar_event(title: str = "Standup", **fields: Any) -> CalendarEvent:
    base = dict(
        id="e1", calendar_id="primary", title=title, description="", start_time="2026-10-07T09:00:00Z",
        end_time="2026-10-07T09:30:00Z", all_day=False, organizer_email="", attendees=[], location="",
        hangout_link="", conference_link="", status="confirmed", html_link="",
    )
    return CalendarEvent(**{**base, **fields})


def install_echo(plugins_dir: Path, **manifest_edits: Any) -> Path:
    """Copy echo into ``plugins_dir/echo``; ``manifest_edits`` overrides manifest keys."""
    target = plugins_dir / "echo"
    target.mkdir()
    shutil.copy(ECHO_DIR / "echo_plugin.py", target / "echo_plugin.py")
    manifest = yaml.safe_load((ECHO_DIR / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    manifest.update(manifest_edits)
    (target / MANIFEST_FILENAME).write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return target


def install_variant(plugins_dir: Path, case: str) -> Path:
    """Copy ``echo-variants/<case>`` into ``plugins_dir/<name in its manifest>``."""
    source = VARIANTS_DIR / case
    name = yaml.safe_load((source / MANIFEST_FILENAME).read_text(encoding="utf-8"))["name"]
    target = plugins_dir / name
    target.mkdir()
    for path in (*source.iterdir(), VARIANTS_DIR / "variant_plugin.py"):
        shutil.copy(path, target / path.name)
    return target


def resolve_command(manifest, plugin_dir: Path) -> list[str]:
    """How the tests launch a plugin: the interpreter running the tests, never a shebang. The SDK
    location is an argument because the daemon starts plugins with an allow-listed environment."""
    if (plugin_dir / "variant_plugin.py").exists():
        return [sys.executable, str(plugin_dir / "variant_plugin.py"), str(plugin_dir / "tools.json")]
    return [sys.executable, str(plugin_dir / "echo_plugin.py"), str(SDK_SRC)]


class FakeDrive:
    def __init__(self, data: bytes = b"", revision: str = "2026-10-07T10:00:00Z") -> None:
        self.data = data
        self.revision = revision

    def get_file_metadata(self, file_id: str):
        return SimpleNamespace(
            id=file_id, size=len(self.data), mime_type="application/octet-stream", modified_time=self.revision
        )

    def download_range(self, file_id: str, offset: int, length: int) -> bytes:
        return self.data[offset:offset + length]

    def download_file_bytes(self, file_id: str) -> dict:
        return {"data": self.data, "name": "x", "mime_type": "application/octet-stream", "size_bytes": len(self.data)}


class Popups:
    """Stubs for the gate's popups: ``decision`` answers every card and each card is recorded."""

    def __init__(self, monkeypatch, decision: str = "accept") -> None:
        self.decision = decision
        self.read: list[tuple] = []
        self.write: list[tuple] = []
        monkeypatch.setattr(gate, "show_read_popup", self._read)
        monkeypatch.setattr(gate, "show_popup", self._write)
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

    def _read(self, *args, **kwargs):
        self.read.append((args, kwargs))
        return self.decision, None

    def _write(self, *args, **kwargs):
        self.write.append((args, kwargs))
        return self.decision, None


class CardCapture:
    """Answers every card at once, like ``Popups``, but keeps the HTML the human would have seen.

    It replaces the one method that hands a built card to the registry, so the page the real
    approval UI builds from the gate's arguments is what is recorded.
    """

    def __init__(self, monkeypatch, decision: str = "accept") -> None:
        self.html: list[str] = []

        def run_card(ui, html, approval):
            self.html.append(html)
            return decision, None

        monkeypatch.setattr(WebApprovalUI, "_run_card", run_card)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_connectable(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"{host}:{port} never became connectable")


async def until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


async def drain_io(loop: asyncio.AbstractEventLoop, timeout: float = 5.0) -> list[str]:
    """Let the loop finish the I/O its closed clients left behind; what remains after ``timeout``."""
    deadline = time.monotonic() + timeout
    while (leftover := pending_io(loop)) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return leftover


class Stack:
    """The daemon's pieces around plugins, in this process."""

    def __init__(
        self, tmp_path: Path, monkeypatch, *, serve: bool = False, ledger_ttl: float = 60.0,
        capture_cards: bool = False,
    ) -> None:
        self.root = tmp_path
        self.data = tmp_path / "data"
        self.data.mkdir()
        monkeypatch.setattr(paths, "data_dir", lambda: self.data)
        self.plugins = tmp_path / "plugins"
        self.plugins.mkdir()
        self.audit_dir = tmp_path / "audit"
        self.audit_dir.mkdir()
        init_audit_logger(str(self.audit_dir))
        settings = tmp_path / "settings.yaml"
        settings.write_text(yaml.dump({}), encoding="utf-8")
        auto_accept.init_config_path(str(settings))
        self.registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=60.0, ledger_ttl=ledger_ttl)
        self.ui = WebApprovalUI(registry=self.registry)
        approval_ui.init_approval_ui(self.ui)
        # Either the gate's popups are stubbed, or the real approval UI builds the card.
        self.popups = None if capture_cards else Popups(monkeypatch)
        self.cards = CardCapture(monkeypatch) if capture_cards else None
        self.calendar = MagicMock()
        # One mock under both names: the daemon reads a page at a time, and the tests set
        # ``list_events.return_value`` to a plain list and assert on ``list_events``.
        events = self.calendar.list_events_page = self.calendar.list_events
        events.return_value = [calendar_event()]
        events.side_effect = lambda *args, **kwargs: (events.return_value, None)
        self.drive = FakeDrive()
        self.calendar_state: tuple[bool, str | None] = (True, None)
        self.problem: str | None = None
        self.tools_changed = 0
        self.unattended = False
        self.serve = serve
        self.server: WebServer | None = None
        self.web_loop: asyncio.AbstractEventLoop | None = None
        self.dispatcher: McpDispatcher | None = None
        self.hosts: list[PluginHost] = []
        self.stopped = False

    # ------------------------------------------------------------------ building

    def build_host(self, **overrides: Any) -> PluginHost:
        def _count() -> None:
            self.tools_changed += 1
            if self.dispatcher is not None:
                state_stream_module.call_soon_threadsafe(self.dispatcher.notify_tools_changed)

        kwargs: dict[str, Any] = {
            "plugins_dir": self.plugins,
            "connectors_provider": lambda: {
                "calendar": SimpleNamespace(_calendar=self.calendar),
                "drive": SimpleNamespace(_drive=self.drive),
            },
            "connector_state": lambda name: self.calendar_state,
            "registry_provider": lambda: self.registry,
            "trust_check": lambda *_: self.problem,
            "command_resolver": resolve_command,
            "separation_enabled": lambda: True,
            "daemon_version": "9.9.9",
        }
        host = PluginHost(**{**kwargs, **overrides})
        host.set_tools_changed_listener(_count)
        host.set_unattended_provider(lambda: self.unattended)
        self.hosts.append(host)
        return host

    async def start(self, **host_overrides: Any) -> PluginHost:
        """Build the host (and the web server when ``serve``) and start the host on its loop."""
        self.host = self.build_host(**host_overrides)
        if self.serve:
            self.dispatcher = McpDispatcher(
                lambda: {**self.host.connectors()}, registry=self.registry,
            )
            port = free_port()
            self.server = WebServer(self.ui, host="localhost", port=port, mcp_dispatcher=self.dispatcher,
                                    plugin_host=self.host)
            self.server.start()
            wait_until_connectable("localhost", port)
            self.web_loop = self.server.wait_until_ready(timeout=5)
            assert self.web_loop is not None
        await self.run(self.host.start())
        return self.host

    async def restart(self) -> PluginHost:
        """A second host over the same directories, as after a daemon restart."""
        await self.run(self.host.stop_all())
        assert self.server is None, "restart is for host-only stacks"
        self.host = self.build_host()
        await self.run(self.host.start())
        return self.host

    async def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        for host in self.hosts:
            try:
                await self.run(host.stop_all(), host)
            except Exception:  # noqa: BLE001  # best-effort teardown
                pass
        # Clients first: a socket still open when the server goes away is cut by the server, and
        # on Windows its cancelled read can keep the test loop from closing.
        leftover = await drain_io(asyncio.get_running_loop())
        if self.server is not None:
            # Off this loop: the server waits on the connections this loop's clients hold.
            await asyncio.to_thread(self.server.stop)
            assert self.server.stopped, "the web server thread outlived Stack.stop()"
        assert not leftover, f"the test loop still waits on I/O after the clients closed: {leftover}"

    # ------------------------------------------------------------------ running on the host's loop

    async def run(self, coro: Coroutine, host: PluginHost | None = None) -> Any:
        """Await ``coro`` on the loop the host runs on: the web server's when serving, as in the
        daemon, so the plugin processes' pipes never belong to the test's own loop."""
        loop = (host or self.host).loop or self.web_loop
        if loop is None or loop is asyncio.get_running_loop():
            return await coro
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def inspect(self, name: str = "echo") -> dict:
        return await self.run(self.host.inspect(name))

    async def enable(self, name: str = "echo") -> dict:
        summary = await self.inspect(name)
        await self.run(self.host.enable(
            name, executable_sha256=summary["executable_sha256"], manifest_sha256=summary["manifest_sha256"],
        ))
        return summary

    async def page(self, path: str, **query: str) -> dict:
        return await self.run(self.host.web_request("echo", path, query, LOCAL_PRINCIPAL))

    def row(self, name: str = "echo") -> dict:
        return next(r for r in self.host.rows() if r["name"] == name)

    # ------------------------------------------------------------------ audit

    def audit(self) -> list[dict]:
        week_file = self.audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    def audit_for(self, connector: str) -> list[dict]:
        return [e for e in self.audit() if e["connector"] == connector]


async def web_session(server: WebServer):
    """An ``httpx2`` client holding a human session cookie, the way the companion opens the app."""
    import httpx2

    client = httpx2.AsyncClient(base_url=server.base_url, follow_redirects=False)
    code = server.bootstrap.mint(provenance=PROVENANCE_HUMAN)
    response = await client.get(f"/approvals?bootstrap={code}")
    assert response.status_code in (200, 302, 303), response.status_code
    return client


class Mcp:
    """The official MCP client against the server's ``/mcp``, with the notifications it received."""

    def __init__(self, session, notifications: list) -> None:
        self.session = session
        self.notifications = notifications

    async def tool_names(self) -> list[str]:
        return [t.name for t in (await self.session.list_tools()).tools]

    async def call(self, tool: str, reason: str = "an end-to-end test", **args: Any):
        """One tool call; the result's ``structured_content`` is the connector's return value."""
        return await self.session.call_tool(tool, {"reason": reason, **args})

    def list_changed(self) -> int:
        from mcp import types

        return sum(isinstance(n, types.ToolListChangedNotification) for n in self.notifications)


@contextlib.asynccontextmanager
async def mcp_session(server: WebServer):
    import httpx2
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamable_http_client

    notifications: list = []

    async def on_message(message) -> None:
        if isinstance(message, types.ServerNotification):
            notifications.append(message)

    headers = {"Authorization": f"Bearer {server.mcp_token}"}
    async with httpx2.AsyncClient(headers=headers) as http:
        async with streamable_http_client(server.mcp_url, http_client=http) as (read, write):
            async with ClientSession(read, write, message_handler=on_message) as session:
                await session.initialize()
                yield Mcp(session, notifications)
