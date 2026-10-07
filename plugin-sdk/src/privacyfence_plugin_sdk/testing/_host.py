"""``PluginTestHost``: plays PrivacyFence's side of the protocol for one plugin, in memory."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import blocks as _blocks
from .._rpc import Peer, RpcError
from ..plugin import PROTOCOL_VERSION, Plugin, args_digest
from ..responses import ToolDefinitionError
from ._gate import Card, Decision, Rules, ToolOutcome, resolve_decision
from ._source import SourceFixtures

# Copied from the protocol, like the limits in plugin.py. The daemon's own tests compare them.
_MAX_LINE_BYTES = 16 * 1024 * 1024
_MAX_IN_FLIGHT = 16
_INVALID_LINES_LIMIT = 3
_INLINE_RESULT_BYTES = 100_000
_MAX_TITLE_CHARS = 120
_MAX_EFFECT_CHARS = 200
_MAX_DESCRIPTION_CHARS = 1024
_MAX_TOOLS = 64
_MAX_SCOPE_VALUES = 100
_MAX_SCOPE_VALUE_CHARS = 200
_MCP_TOOL_NAME_MAX = 64
_MAX_SCOPE_TYPES = 20
_TIMEOUTS = {"initialize": 10.0, "tool.prepare": 30.0, "tool.execute": 60.0}
_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")
_SCOPE_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")
_GATES = ("auto", "review", "popup")
_PARAM_TYPES = frozenset({"string", "integer", "number", "boolean"})
_FORBIDDEN_PARAM_KEYS = frozenset({"enum", "oneOf", "anyOf", "allOf", "items", "properties", "$ref"})

_PREPARE_SENTENCES = {
    "connector_unavailable": "A service this plugin reads from is not connected.",
    "payload_too_large": "The plugin's result is too large to return.",
    "timeout": "The plugin did not answer in time.",
}
_PREPARE_FALLBACK = "The plugin could not prepare this call."
_INVALID_PREVIEW = "The plugin returned an invalid preview."
_LOST_CALL = "The plugin lost track of this call; ask again."

_DEFAULT_PRINCIPAL = {"id": "local", "display_name": "Local user"}


class _PipeWriter:
    """Writes into another StreamReader, like one end of a pipe."""

    def __init__(self, target: asyncio.StreamReader) -> None:
        self._target = target

    def write(self, data: bytes) -> None:
        self._target.feed_data(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._target.feed_eof()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_tool_defs(plugin: Plugin, result: Any, max_gate_floor: str) -> list[dict]:
    """The daemon's floors on an ``initialize`` result; raises ``ToolDefinitionError`` on the first break."""
    if not isinstance(result, dict):
        raise ToolDefinitionError("the initialize result is not an object")
    named = result.get("plugin")
    if not isinstance(named, dict) or (named.get("name"), named.get("version")) != (plugin.name, plugin.version):
        raise ToolDefinitionError("name or version differs from the plugin's own")
    scope_types = result.get("scope_types")
    if not isinstance(scope_types, list) or len(scope_types) > _MAX_SCOPE_TYPES:
        raise ToolDefinitionError(f"scope_types must be a list of at most {_MAX_SCOPE_TYPES} entries")
    declared: set[str] = set()
    for entry in scope_types:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not _SCOPE_TYPE_RE.fullmatch(name):
            raise ToolDefinitionError(f"scope type {name!r} does not match the scope type pattern")
        declared.add(name)
    defs = result.get("tools")
    if not isinstance(defs, list) or len(defs) > _MAX_TOOLS:
        raise ToolDefinitionError(f"tools must be a list of at most {_MAX_TOOLS} entries")
    seen: set[str] = set()
    for tool in defs:
        _check_tool(plugin.name, tool, declared, seen, max_gate_floor)
    return copy.deepcopy(defs)


def _check_tool(plugin: str, tool: Any, declared: set[str], seen: set[str], max_gate_floor: str) -> None:
    if not isinstance(tool, dict):
        raise ToolDefinitionError("each tool must be an object")
    name = tool.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
        raise ToolDefinitionError(f"tool name {name!r} must match [a-z][a-z0-9_]{{1,40}}")
    if name in seen:
        raise ToolDefinitionError(f"tool {name} is listed twice")
    seen.add(name)
    if len(f"{plugin}_{name}") > _MCP_TOOL_NAME_MAX:
        raise ToolDefinitionError(f"the MCP name {plugin}_{name} is longer than {_MCP_TOOL_NAME_MAX} characters")
    description = tool.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= _MAX_DESCRIPTION_CHARS:
        raise ToolDefinitionError(f"the description of {name} must be 1 to {_MAX_DESCRIPTION_CHARS} characters")
    _check_parameters(name, tool.get("parameters"))
    read_only, destructive, gate = tool.get("read_only"), tool.get("destructive"), tool.get("gate")
    if not isinstance(read_only, bool) or not isinstance(destructive, bool):
        raise ToolDefinitionError(f"read_only and destructive of {name} must be booleans")
    if gate not in _GATES:
        raise ToolDefinitionError(f"gate of {name} must be one of {', '.join(_GATES)}")
    if read_only and destructive:
        raise ToolDefinitionError(f"tool {name} cannot be both read-only and destructive")
    if destructive and gate != "popup":
        raise ToolDefinitionError(f"destructive tool {name} must use the popup gate")
    if gate == "auto" and max_gate_floor != "auto":
        raise ToolDefinitionError(
            f"tool {name} needs max_gate_floor: auto to use the auto gate; pass max_gate_floor='auto' to the host"
        )
    scopes = tool.get("scopes", [])
    if not isinstance(scopes, list):
        raise ToolDefinitionError(f"scopes of {name} must be a list")
    for scope in scopes:
        if not isinstance(scope, str) or not _SCOPE_TYPE_RE.fullmatch(scope) or scope not in declared:
            raise ToolDefinitionError(f"scope type {scope!r} of {name} is not declared")
    effect, title = tool.get("effect"), tool.get("title")
    if effect is not None and (not isinstance(effect, str) or len(effect) > _MAX_EFFECT_CHARS):
        raise ToolDefinitionError(f"the effect of {name} must be at most {_MAX_EFFECT_CHARS} characters")
    if title is not None and (not isinstance(title, str) or len(title) > _MAX_TITLE_CHARS):
        raise ToolDefinitionError(f"the title of {name} must be at most {_MAX_TITLE_CHARS} characters")


def _check_parameters(tool: str, parameters: Any) -> None:
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ToolDefinitionError(f"parameters of {tool} must be an object schema")
    properties = parameters.get("properties", {})
    if not isinstance(properties, dict) or "reason" in properties:
        raise ToolDefinitionError(f"{tool} may not declare a parameter named reason; PrivacyFence adds it")
    for pname, spec in properties.items():
        if not isinstance(spec, dict) or spec.get("type") not in _PARAM_TYPES or _FORBIDDEN_PARAM_KEYS & set(spec):
            raise ToolDefinitionError(
                f"parameter {pname} of {tool}: only string, integer, number and boolean are supported"
            )
    required = parameters.get("required", [])
    if not isinstance(required, list) or any(r not in properties for r in required):
        raise ToolDefinitionError(f"required of {tool} must name declared parameters")


class PluginTestHost:
    """Runs a plugin's real runner over an in-memory stream pair and plays the daemon's side.

    ``max_gate_floor`` is the manifest's ``max_gate_floor``: ``"review"`` (the default) refuses a
    tool on the ``auto`` gate, as PrivacyFence does unless the owner approved ``"auto"`` at enable.
    """

    def __init__(
        self,
        plugin: Plugin,
        mode: str = "local",
        principals: list[dict] | None = None,
        *,
        max_gate_floor: str = "review",
    ) -> None:
        if mode not in ("local", "org"):
            raise ValueError("mode must be 'local' or 'org'")
        if max_gate_floor not in ("review", "auto"):
            raise ValueError("max_gate_floor must be 'review' or 'auto'")
        raw = principals if principals is not None else [dict(_DEFAULT_PRINCIPAL)]
        if not raw or any(not isinstance(p, dict) or not p.get("id") for p in raw):
            raise ValueError("principals must be dicts with an id")
        if mode == "local" and [p["id"] for p in raw] != ["local"]:
            raise ValueError("local mode has exactly one principal, with the id 'local'")
        self.plugin = plugin
        self.mode = mode
        self.max_gate_floor = max_gate_floor
        self._principal_specs = [dict(p) for p in raw]
        self._tools: list[dict] = []
        self._scope_types: list[str] = []
        self.source = SourceFixtures()
        self.rules = Rules(lambda: set(self._scope_types))
        self.audit: list[dict] = []
        self._tmp: Path | None = None
        self._principals: dict[str, dict] = {}
        self._peer: Peer | None = None
        self._serve_task: asyncio.Task | None = None
        self._started = False

    # ------------------------------------------------------------------ lifecycle

    @property
    def tools(self) -> list[dict]:
        """The ``ToolDef`` list the plugin reported at ``initialize``."""
        return copy.deepcopy(self._tools)

    @property
    def data_dir(self) -> Path:
        if self._tmp is None:
            raise RuntimeError("the host is not running")
        return self._tmp / "data"

    async def __aenter__(self) -> PluginTestHost:
        if self._started:
            raise RuntimeError("a PluginTestHost starts once")
        self._started = True
        self.plugin.tool_definitions()  # a bad registry raises ToolDefinitionError here
        self._tmp = Path(tempfile.mkdtemp(prefix="pf-testhost-"))
        try:
            await self._start()
        except BaseException:
            await self._teardown()
            raise
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._teardown()

    async def _start(self) -> None:
        data_dir = self.data_dir
        data_dir.mkdir()
        contexts = []
        for spec in self._principal_specs:
            storage = self._tmp / "principals" / spec["id"]
            storage.mkdir(parents=True)
            context = {"id": spec["id"], "display_name": spec.get("display_name", spec["id"]),
                       "storage_dir": str(storage)}
            if self.mode == "org" and "roles" in spec:
                context["roles"] = list(spec["roles"])
            self._principals[spec["id"]] = context
            contexts.append(context)

        to_plugin = asyncio.StreamReader(limit=_MAX_LINE_BYTES)
        from_plugin = asyncio.StreamReader(limit=_MAX_LINE_BYTES)
        self._serve_task = asyncio.ensure_future(self.plugin.serve(to_plugin, _PipeWriter(from_plugin)))
        self._peer = Peer(
            from_plugin,
            _PipeWriter(to_plugin),
            handlers={
                "source.call": self._handle_source,
                "confirm.request": self._handle_confirm_request,
                "confirm.await": self._handle_confirm_await,
            },
            max_line_bytes=_MAX_LINE_BYTES,
            max_in_flight=_MAX_IN_FLIGHT,
            invalid_lines_limit=_INVALID_LINES_LIMIT,
        )
        await self._peer.start()
        result = await self._peer.request("initialize", {
            "protocol_version": PROTOCOL_VERSION,
            "purpose": "run",
            "mode": self.mode,
            "daemon": {"name": "privacyfence-test-host", "version": "0.0.0"},
            "plugin": {"name": self.plugin.name, "manifest_version": self.plugin.version},
            "data_dir": str(data_dir),
            "principals": contexts,
            "limits": {"max_line_bytes": _MAX_LINE_BYTES, "max_in_flight": _MAX_IN_FLIGHT,
                       "inline_result_bytes": _INLINE_RESULT_BYTES},
        }, timeout=_TIMEOUTS["initialize"])
        self._tools = _check_tool_defs(self.plugin, result, self.max_gate_floor)
        self._scope_types = [s["name"] for s in result["scope_types"]]

    async def _teardown(self) -> None:
        peer, self._peer = self._peer, None
        if peer is not None:
            await peer.close()
        task, self._serve_task = self._serve_task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception:
                pass
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    # ------------------------------------------------------------------ calling tools

    def _principal(self, principal: str | None) -> dict:
        if principal is None:
            return self._principals[self._principal_specs[0]["id"]]
        try:
            return self._principals[principal]
        except KeyError:
            raise ValueError(f"unknown principal {principal!r}") from None

    def _tool(self, name: str) -> dict | None:
        return next((t for t in self._tools if t["name"] == name), None)

    @staticmethod
    def _title(tool: dict) -> str:
        return tool.get("title") or tool["name"].replace("_", " ").capitalize()

    async def call_tool(
        self, name: str, args: dict | None = None, decide: Decision = "approve", principal: str | None = None
    ) -> ToolOutcome:
        """Call one of the plugin's tools the way PrivacyFence would: prepare, gate, execute.

        ``name`` is the plugin's own tool name. ``decide`` answers a card that is shown:
        ``"approve"``, ``"deny"`` or a function of the :class:`Card`. A ``reason`` argument is
        passed on as the call's reason, as the MCP layer does.

        Raises :class:`SourceFixtureMissing` when the plugin made a ``source.call`` that no fixture
        answers.
        """
        peer = self._peer
        if peer is None:
            raise RuntimeError("the host is not running")
        ctx = self._principal(principal)
        tool = self._tool(name)
        if tool is None:
            return ToolOutcome(gate="", error={"code": "unknown_tool", "detail": f"the plugin has no tool {name!r}"})
        args = dict(args or {})
        reason = args.pop("reason", None)
        required = tool["parameters"].get("required", [])
        missing = [r for r in required if r not in args]
        if missing:
            return ToolOutcome(tool["gate"], error={"code": "invalid_params", "detail": f"missing {missing[0]}"})

        mark = len(self.audit)
        self.source.take_missing()
        try:
            outcome = await self._run_call(peer, tool, ctx, args, reason, decide)
        finally:
            missing_fixture = self.source.take_missing()
        outcome.audit = [dict(e) for e in self.audit[mark:]]
        if missing_fixture is not None:
            raise missing_fixture
        return outcome

    async def _run_call(
        self, peer: Peer, tool: dict, ctx: dict, args: dict, reason: str | None, decide: Decision
    ) -> ToolOutcome:
        outcome = ToolOutcome(gate=tool["gate"])
        call_id = uuid.uuid4().hex
        read_only = tool["read_only"]
        try:
            prepared = await peer.request("tool.prepare", {
                "call_id": call_id, "principal": ctx, "tool": tool["name"], "args": args, "reason": reason,
            }, timeout=_TIMEOUTS["tool.prepare"])
        except RpcError as exc:
            outcome.error = {"code": exc.code, "detail": _PREPARE_SENTENCES.get(exc.code, _PREPARE_FALLBACK)}
            return outcome
        card = self._validate_prepared(tool, prepared)
        if card is None:
            outcome.error = {"code": "invalid_preview", "detail": _INVALID_PREVIEW}
            return outcome
        outcome.card = card

        title = self._title(tool)
        mcp_name = f"{self.plugin.name}_{tool['name']}"
        entry = {"connector": self.plugin.name, "tool": mcp_name, "tool_name": title}
        if tool["gate"] == "auto":
            via = "auto"
            self.audit.append({**entry, "decision": "auto_accepted", "auto_accept_rule": "auto"})
        else:
            rule_scope = None
            if not tool["destructive"]:
                rule_scope = self.rules.matching_scope(tool["scopes"], card.scopes)
            if rule_scope is not None:
                via = "rule"
                self.audit.append({
                    **entry, "decision": "auto_accepted",
                    "auto_accept_rule": f"plugin:{self.plugin.name}:{rule_scope}",
                })
            else:
                outcome.card_shown = True
                if not await resolve_decision(decide, card):
                    self.audit.append({**entry, "decision": "denied", "auto_accept_rule": ""})
                    outcome.error = {"code": "denied", "detail": "The request was denied."}
                    return outcome
                via = "card"
                self.audit.append({**entry, "decision": "approved", "auto_accept_rule": ""})

        approval = {"approval_id": f"{via}-{call_id}", "decision": "approved", "via": via, "decided_at": _now()}
        outcome.approval = approval
        try:
            executed = await peer.request("tool.execute", {
                "call_id": call_id, "principal": ctx, "tool": tool["name"], "args": args,
                "args_digest": args_digest(args), "approval": approval,
            }, timeout=_TIMEOUTS["tool.execute"])
        except RpcError as exc:
            if read_only:
                # What the human approved is the prepared payload, whatever execute says.
                outcome.released = {"blocks": card.payload}
                return outcome
            lost = exc.code in ("unknown_call", "digest_mismatch")
            outcome.error = {"code": exc.code, "detail": _LOST_CALL if lost else "The plugin could not run this call."}
            return outcome
        result = executed.get("result") if isinstance(executed, dict) else None
        outcome.result = result
        if read_only:
            outcome.released = {"blocks": card.payload}
        else:
            released = result
            approval_id = executed.get("approval_id") if isinstance(executed, dict) else None
            if approval_id is not None and isinstance(result, dict):
                released = {**result, "approval_id": approval_id}
            outcome.released = released
        return outcome

    @staticmethod
    def _validate_prepared(tool: dict, prepared: Any) -> Card | None:
        """The daemon's checks on a ``tool.prepare`` result. ``None`` means an invalid preview."""
        if not isinstance(prepared, dict):
            return None
        try:
            preview = _blocks.validate_blocks(prepared.get("preview"))
            payload = prepared.get("payload")
            if payload is not None:
                payload = _blocks.validate_blocks(payload, max_bytes=None)
        except ValueError:
            return None
        if tool["read_only"] != (payload is not None):
            return None
        if payload is not None and len(json.dumps({"blocks": payload}, ensure_ascii=False).encode()) > _INLINE_RESULT_BYTES:
            return None
        scopes = prepared.get("scopes", {})
        if not isinstance(scopes, dict):
            return None
        for values in scopes.values():
            if (
                not isinstance(values, list)
                or len(values) > _MAX_SCOPE_VALUES
                or not all(isinstance(v, str) and 0 < len(v) <= _MAX_SCOPE_VALUE_CHARS for v in values)
            ):
                return None
        if any(not scopes.get(s) for s in tool["scopes"]):
            return None
        return Card(preview=preview, payload=payload, scopes={k: list(v) for k, v in scopes.items()})

    # ------------------------------------------------------------------ requests from the plugin

    async def _handle_source(self, params: dict) -> dict:
        def audit(operation: str, summary: str) -> None:
            self.audit.append({
                "connector": f"plugin:{self.plugin.name}", "tool": operation,
                "tool_name": f"{self.plugin.name} source read", "summary": summary, "decision": "plugin_source",
            })

        return self.source.handle(params, mode=self.mode, audit=audit)

    # Hook points for the parts of the daemon this host does not play yet. Each is one place to fill in.

    async def _handle_confirm_request(self, params: dict) -> dict:
        raise NotImplementedError("confirmations are not simulated yet")

    async def _handle_confirm_await(self, params: dict) -> dict:
        raise NotImplementedError("confirmations are not simulated yet")

    async def _handle_web(self, method: str, path: str, query: dict[str, str]) -> Any:
        raise NotImplementedError("pages are not simulated yet")
