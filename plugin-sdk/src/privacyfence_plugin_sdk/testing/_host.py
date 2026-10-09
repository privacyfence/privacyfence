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
from typing import Any, Callable, Iterable

from .. import blocks as _blocks
from .._rpc import Peer, RpcError
from ..plugin import (
    _MAX_SCOPE_TYPE_DESCRIPTION_CHARS,
    _RESERVED_PLUGIN_NAMES,
    PROTOCOL_VERSION,
    Plugin,
    args_digest,
)
from ..responses import ToolDefinitionError
from .. import _page_index
from . import _pages
from ._approvals import Approval, Approvals
from ._confirm import Confirmation, Confirmations
from ._gate import Card, Decision, Rules, ToolOutcome, flatten_text, resolve_decision
from ._outputs import DEFAULT_OUTPUT_TYPES, OutputFile, check_types, list_outputs
from ._source import SOURCE_OPERATIONS, SourceFixtures

# Copied from the protocol, like the limits in plugin.py. The daemon's own tests compare them.
_MAX_LINE_BYTES = 16 * 1024 * 1024
_MAX_IN_FLIGHT = 16
_INVALID_LINES_LIMIT = 3
_INLINE_RESULT_BYTES = 100_000
_WRITE_RESULT_MAX_BYTES = 2048
_WRITE_RESULT_WITHHELD = (
    "The action ran, but its result was withheld because it was larger than 2,048 bytes "
    "or may contain personal data."
)
_MAX_TITLE_CHARS = 120
_MAX_EFFECT_CHARS = 200
_MAX_DESCRIPTION_CHARS = 1024
_MAX_TOOLS = 64
_MAX_SCOPE_VALUES = 100
_MAX_SCOPE_VALUE_CHARS = 200
_MCP_TOOL_NAME_MAX = 64
_MAX_SCOPE_TYPES = 20

_TIMEOUTS = {"initialize": 10.0, "tool.prepare": 30.0, "tool.execute": 60.0, "pages.list": 10.0}
_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")
_SCOPE_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")
_GATES = ("auto", "review", "popup")
_PARAM_TYPES = frozenset({"string", "integer", "number", "boolean"})
_FORBIDDEN_PARAM_KEYS = frozenset({"enum", "oneOf", "anyOf", "allOf", "items", "properties", "$ref"})

_PREPARE_SENTENCES = {
    "connector_unavailable": "A service this plugin reads from is not connected.",
    "payload_too_large": "The plugin's result is too large to return.",
    "timeout": "The plugin did not answer in time.",
    "upstream_error": "A service this plugin reads from returned an error.",
}
_PREPARE_FALLBACK = "The plugin could not prepare this call."
_INVALID_PREVIEW = "The plugin returned an invalid preview."
_LOST_CALL = "The plugin lost track of this call; ask again."

_DEFAULT_PRINCIPAL = {"id": "local", "display_name": "Local user"}
_EVENT_NAMES = (
    "connector.state_changed", "plugin.disabling", "shutdown", "principal.removed", "approval.revoked",
)
_PURGE_SCOPES = ("all", "install", "principal")
_PURGE_TIMEOUT = 30.0
_SETTLE_TIMEOUT = 5.0


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


async def _refuse_while_introspecting(_params: dict) -> Any:
    raise RpcError("introspection_only", "not available while introspecting")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_tool_defs(plugin: Plugin, result: Any, max_gate_floor: str) -> list[dict]:
    """The daemon's floors on an ``initialize`` result; raises ``ToolDefinitionError`` on the first break."""
    if not isinstance(result, dict):
        raise ToolDefinitionError("the initialize result is not an object")
    named = result.get("plugin")
    if not isinstance(named, dict) or (named.get("name"), named.get("version")) != (plugin.name, plugin.version):
        raise ToolDefinitionError("name or version differs from the plugin's own")
    if plugin.name in _RESERVED_PLUGIN_NAMES:
        raise ToolDefinitionError(f"name {plugin.name!r} is reserved")
    scope_types = result.get("scope_types")
    if not isinstance(scope_types, list) or len(scope_types) > _MAX_SCOPE_TYPES:
        raise ToolDefinitionError(f"scope_types must be a list of at most {_MAX_SCOPE_TYPES} entries")
    declared: set[str] = set()
    for entry in scope_types:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not _SCOPE_TYPE_RE.fullmatch(name):
            raise ToolDefinitionError(f"scope type {name!r} does not match the scope type pattern")
        if name == "output":
            raise ToolDefinitionError("scope type output is reserved")
        description = entry.get("description")
        if not isinstance(description, str) or not 1 <= len(description) <= _MAX_SCOPE_TYPE_DESCRIPTION_CHARS:
            raise ToolDefinitionError(
                f"scope type {name} needs a description of 1 to {_MAX_SCOPE_TYPE_DESCRIPTION_CHARS} characters"
            )
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
    for field_name, text in (("title", title), ("effect", effect)):
        if text is not None and _blocks.clean_line(text) != text:
            raise ToolDefinitionError(
                f"tool.{field_name} must not contain line breaks, tabs, control or bidirectional characters"
            )


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

    ``outputs=True`` is the manifest's ``outputs: true``: every principal gets an output folder
    (``host.output_dir``) that ``ctx.outputs`` writes to, and ``output_types`` (the manifest's
    ``output_types``, ``application/json`` and ``text/csv`` by default) decides which extensions it
    may use and which files ``host.list_outputs`` shows.

    ``source_operations`` is the manifest's ``source_operations``: the ``source.call`` operations the
    plugin may use, none by default. Any other operation is refused with ``operation_not_allowed``,
    and a name that is not a source operation raises ``ValueError``.

    ``pages=True`` is the manifest's ``pages: true``: the plugin serves pages, and approval pages
    are available. Without it every ``host.get`` / ``host.request`` answers 404 and never reaches
    the plugin.

    ``pii`` stands in for the daemon's PII detector: a function of the text a review card shows
    that returns True when it finds personal data. A flagged call always shows its card, whatever
    "Always allow" rules exist, and the card has ``pii_flagged`` set. ``None`` flags nothing, and
    ``auto`` and ``popup`` gates are never scanned.

    A write tool's result over 2,048 bytes is withheld from ``outcome.released``, as the daemon
    does; ``outcome.result`` stays what the plugin returned. Only that cap is mirrored: the
    daemon also withholds a result its PII detector flags, which this host cannot run.
    """

    def __init__(
        self,
        plugin: Plugin,
        mode: str = "local",
        principals: list[dict] | None = None,
        *,
        max_gate_floor: str = "review",
        outputs: bool = False,
        output_types: tuple[str, ...] | list[str] | None = None,
        source_operations: Iterable[str] = (),
        pages: bool = False,
        pii: Callable[[str], bool] | None = None,
    ) -> None:
        if mode not in ("local", "org"):
            raise ValueError("mode must be 'local' or 'org'")
        if max_gate_floor not in ("review", "auto"):
            raise ValueError("max_gate_floor must be 'review' or 'auto'")
        if output_types is not None and not outputs:
            raise ValueError("output_types needs outputs=True")
        allowed = frozenset(source_operations)
        for operation in sorted(allowed, key=str):
            if operation not in SOURCE_OPERATIONS:
                raise ValueError(f"unknown source operation {operation!r}")
        self.source_operations = allowed
        self.pages = bool(pages)
        self._pii = pii
        self.outputs = bool(outputs)
        self._output_types = check_types(DEFAULT_OUTPUT_TYPES if output_types is None else output_types) if outputs else ()
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
        self.source = SourceFixtures(self.source_operations)
        self.rules = Rules(lambda: set(self._scope_types))
        self.audit: list[dict] = []
        self._confirmations = Confirmations(
            plugin.name, lambda: set(self._principals), lambda entry: self.audit.append(entry)
        )
        self._approvals = Approvals(
            plugin.name, lambda: set(self._principals), lambda: self.pages,
            lambda entry: self.audit.append(entry),
        )
        self._stopped = False
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

    async def introspect(self) -> list[dict]:
        """Start the plugin with purpose ``introspect``, as PrivacyFence does when you review a plugin,
        check its tool list, stop it and return the tools. ``source.call`` and ``confirm.request`` are
        refused with ``introspection_only``. Runs before the host is entered, and leaves it ready for
        ``async with``."""
        if self._started or self._peer is not None:
            raise RuntimeError("introspect() runs before the host starts")
        self.plugin.tool_definitions()
        self._tmp = Path(tempfile.mkdtemp(prefix="pf-testhost-"))
        try:
            await self._start(purpose="introspect")
            return self.tools
        finally:
            await self._end_introspection()
            await self._teardown()

    async def _end_introspection(self) -> None:
        """Send ``shutdown`` and let the runner stop, as PrivacyFence does when an inspection start
        ends, so the plugin's shutdown handlers run here too."""
        peer, task = self._peer, self._serve_task
        if peer is None:
            return
        with contextlib.suppress(Exception):
            await peer.notify("shutdown", {"grace_ms": 0})
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), _SETTLE_TIMEOUT)

    async def _start(self, purpose: str = "run") -> None:
        data_dir = self.data_dir
        data_dir.mkdir()
        self._principals = {}
        contexts = []
        for spec in self._principal_specs:
            storage = self._tmp / "principals" / spec["id"]
            storage.mkdir(parents=True)
            context = {"id": spec["id"], "display_name": spec.get("display_name", spec["id"]),
                       "storage_dir": str(storage)}
            if self.outputs:
                output = self._tmp / "outputs" / spec["id"]
                output.mkdir(parents=True, mode=0o700)
                context["output_dir"] = str(output)
                context["output_types"] = list(self._output_types)
            if self.mode == "org" and "roles" in spec:
                context["roles"] = list(spec["roles"])
            self._principals[spec["id"]] = context
            contexts.append(context)

        to_plugin = asyncio.StreamReader(limit=_MAX_LINE_BYTES)
        from_plugin = asyncio.StreamReader(limit=_MAX_LINE_BYTES)
        self._serve_task = asyncio.ensure_future(self.plugin.serve(to_plugin, _PipeWriter(from_plugin)))
        if purpose == "introspect":
            handlers = {"source.call": _refuse_while_introspecting,
                        "confirm.request": _refuse_while_introspecting}
        else:
            handlers = {
                "source.call": self._handle_source,
                "confirm.request": self._confirmations.request,
                "confirm.await": self._confirmations.await_,
                "approval.request": self._approvals.request,
                "approval.check": self._approvals.check,
                "approval.await": self._approvals.await_,
            }
        self._peer = Peer(
            from_plugin,
            _PipeWriter(to_plugin),
            handlers=handlers,
            max_line_bytes=_MAX_LINE_BYTES,
            max_in_flight=_MAX_IN_FLIGHT,
            invalid_lines_limit=_INVALID_LINES_LIMIT,
        )
        await self._peer.start()
        result = await self._peer.request("initialize", {
            "protocol_version": PROTOCOL_VERSION,
            "purpose": purpose,
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
            flagged = False
            if tool["gate"] == "review":
                if read_only:
                    text = flatten_text(card.payload)
                else:
                    text = json.dumps(
                        {"plugin": self.plugin.name, "tool": tool["name"], "scopes": card.scopes},
                        default=str, indent=2, ensure_ascii=False,
                    )
                flagged = bool(self._pii and self._pii(text))
            if flagged:
                card.pii_flagged = True
            elif not tool["destructive"]:
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
                    self.audit.append({
                        **entry, "decision": "denied", "auto_accept_rule": "",
                        **({"pii_detected": True} if flagged else {}),
                    })
                    outcome.error = {"code": "denied", "detail": "The request was denied."}
                    return outcome
                via = "card"
                self.audit.append({
                    **entry, "decision": "approved", "auto_accept_rule": "",
                    **({"pii_detected": True} if flagged else {}),
                })

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
            if len(json.dumps(released, separators=(",", ":"), ensure_ascii=False).encode()) > _WRITE_RESULT_MAX_BYTES:
                released = {"withheld": True, "message": _WRITE_RESULT_WITHHELD}
                if approval_id in self._confirmations._cards or approval_id in self._approvals._cards:
                    released["approval_id"] = approval_id
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
        if payload is not None and len(json.dumps({"blocks": payload}, separators=(",", ":"), ensure_ascii=False).encode()) > _INLINE_RESULT_BYTES:
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

    # ------------------------------------------------------------------ pages

    def _running_peer(self) -> Peer:
        peer = self._peer
        if peer is None:
            raise RuntimeError("the host is not running")
        return peer

    async def get(
        self, path: str, *, principal: str | None = None, query: dict[str, str] | None = None
    ) -> _pages.PageResponse:
        """Fetch a plugin page the way the owner's browser would: ``GET`` with the daemon's rules and headers."""
        return await self.request("GET", path, principal=principal, query=query)

    async def request(
        self, method: str, path: str, *, principal: str | None = None, query: dict[str, str] | None = None
    ) -> _pages.PageResponse:
        """One request to a plugin page. ``path`` is relative to the plugin and may carry a ``?query``.

        ``GET`` and ``HEAD`` reach the plugin (a ``HEAD`` keeps the headers and drops the body); any
        other method gets 405 and a path the daemon rejects gets 400, and neither reaches the plugin.
        """
        peer = self._running_peer()
        return await _pages.serve(peer, self._principal(principal), method, path, query, enabled=self.pages)

    async def list_pages(self, principal: str | None = None) -> list[dict]:
        """The pages the plugin lists for the page browser, as the daemon would validate them.

        A plugin without a page index lists its root page. An invalid list raises ``AssertionError``.
        """
        if not self.pages:
            raise LookupError(f"plugin {self.plugin.name} does not serve pages")
        peer = self._running_peer()
        try:
            result = await peer.request(
                "pages.list", {"principal": self._principal(principal)}, timeout=_TIMEOUTS["pages.list"])
        except RpcError as exc:
            if exc.code == "method_not_found":
                return [{"path": "/", "title": self.plugin.name}]
            if exc.code == "invalid_params":
                raise AssertionError(exc.detail) from None
            raise
        try:
            if not isinstance(result, dict) or set(result) != {"pages"}:
                raise ValueError("pages.list result must be an object holding only pages")
            return _page_index.validate_page_entries(result["pages"])
        except ValueError as exc:
            raise AssertionError(str(exc)) from None

    # ------------------------------------------------------------------ confirmations

    @property
    def confirmations(self) -> list[Confirmation]:
        """Every confirmation the plugin opened, oldest first, with its current ``status``."""
        return self._confirmations.cards

    async def decide_confirmation(self, approval_id: str, decision: str) -> Confirmation:
        """Answer a confirmation card: ``"approve"``, ``"deny"`` or ``"expire"``. A confirm card takes no deny note."""
        card = self._confirmations.decide(approval_id, decision)
        await asyncio.sleep(0)  # let a plugin that awaits the card see the answer
        return card

    # ------------------------------------------------------------------ approvals

    @property
    def approvals(self) -> list[Approval]:
        """Every approval card the plugin opened, oldest first, with its current ``status``.

        A request for something already approved shows no card, so it adds nothing here.
        """
        return self._approvals.cards

    async def decide_approval(self, approval_id: str, decision: str) -> Approval:
        """Answer an approval card: ``"approve"`` (which stores it), ``"deny"`` or ``"expire"``."""
        card = self._approvals.decide(approval_id, decision)
        await asyncio.sleep(0)  # let a plugin that awaits the card see the answer
        return card

    async def revoke_approval(self, approval_id: str) -> Approval:
        """Take back a stored approval: ``check`` answers ``revoked`` and the plugin gets ``approval.revoked``.

        Revoking one that is already revoked changes nothing and sends nothing.
        """
        before = next((a for a in self._approvals.cards if a.approval_id == approval_id), None)
        record = self._approvals.revoke(approval_id)
        if before is None or before.revoked_at is None:
            await self.emit("approval.revoked", {
                "approval_id": record.approval_id, "kind": record.kind,
                "subject_id": record.subject_id, "digest": record.digest,
            })
        return record

    # ------------------------------------------------------------------ outputs

    def _output_dir(self, principal: str | None) -> Path:
        if not self.outputs:
            raise RuntimeError("the host has no output folder; pass outputs=True")
        ctx = self._principal(principal)
        return Path(ctx["output_dir"])

    @property
    def output_dir(self) -> Path:
        """The first principal's output folder, where ``ctx.outputs.publish`` writes."""
        return self._output_dir(None)

    def list_outputs(self, prefix: str = "", *, principal: str | None = None) -> list[OutputFile]:
        """The files an agent could list: published, not hidden, and of one of the ``output_types``."""
        return list_outputs(self._output_dir(principal), self._output_types, prefix)

    # ------------------------------------------------------------------ events, purge, shutdown

    async def emit(self, event: str, params: dict | None = None) -> None:
        """Send the plugin an event and wait until its handlers have run."""
        peer = self._running_peer()
        if event not in _EVENT_NAMES:
            raise ValueError(f"unknown event {event}; expected one of {', '.join(_EVENT_NAMES)}")
        serving = getattr(getattr(self.plugin, "_host", None), "peer", None)
        before = set(getattr(serving, "_tasks", ()))
        await peer.notify(event, dict(params or {}))
        fresh: set[asyncio.Task] = set()
        for _ in range(50):
            await asyncio.sleep(0)
            fresh = set(getattr(serving, "_tasks", ())) - before
            if fresh:
                break
        if fresh:
            await asyncio.wait(fresh, timeout=_SETTLE_TIMEOUT)

    async def purge(self, scope: str = "all", principal: str | None = None) -> bool:
        """Ask the plugin to delete its data, as the Settings action does. Returns the plugin's ``purged``."""
        peer = self._running_peer()
        if scope not in _PURGE_SCOPES:
            raise ValueError(f"scope must be one of {', '.join(_PURGE_SCOPES)}")
        params: dict[str, Any] = {"scope": scope}
        if principal is not None:
            params["principal"] = self._principal(principal)["id"]
        elif scope == "principal":
            raise ValueError("a principal purge needs a principal")
        result = await peer.request("storage.purge", params, timeout=_PURGE_TIMEOUT)
        return bool(isinstance(result, dict) and result.get("purged") is True)

    async def shutdown(self, grace_ms: int = 0) -> None:
        """Send ``shutdown`` and wait for the plugin's runner to stop. Leaving the ``async with`` is still fine."""
        if self._stopped:
            return
        await self.emit("shutdown", {"grace_ms": grace_ms})
        task = self._serve_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), _SETTLE_TIMEOUT)
        self._stopped = True
