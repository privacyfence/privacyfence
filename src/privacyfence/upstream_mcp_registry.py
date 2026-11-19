"""Registers the saved tool lists of the hosted upstream MCP servers into the policy tables, live.

``UpstreamRegistry.apply`` writes every saved tool into the same module-level tables the built-in
connectors use (gate, operation key, verb, effect text, approval-window layout, the declaration digest an
ADR 0300 approval is pinned to), then rebuilds the two derived values (``TOOL_REGISTRY``,
``propose.ALL_OPERATIONS``) and the per-tool Always allow scopes. It replaces whatever the same
upstream registered before, so an administrator's Save takes effect without a restart; ``clear`` and
``unregister_all`` undo it.

The gate written to ``TOOL_TO_GATE`` is the person gate (``review`` or ``popup``), never ``auto``:
``auto`` is only the ceiling up to which a person may make an Always allow rule for one tool. Such a
tool joins ``policy.scopes.UPSTREAM_AUTO_TOOLS`` and gets one ``ProposableScope`` of its own, so the
card offers the button for that tool alone.

Writers hold a lock; readers take none (a reader can see a half-applied list for one request, never a
gate lower than the person gate of the old or the new list, and never an empty ``TOOL_REGISTRY``).
No network and no logging. See ADR 0331.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import auto_accept, gate, write_effects
from .approval_window_html import WIDE
from .auto_accept import ReviewContext
from .org_mode import ConfigurationError
from .policy import describe, propose
from .policy import registry as policy_registry
from .policy import scopes
from .policy.registry import Verb
from .upstream_mcp_config import UpstreamServer
from .upstream_tools import SavedTool, declaration_digest, person_gate, classify

# The fixed effect text of a popup tool: no text from the server ever reaches the approval window.
EFFECT_BY_TOOL = "Calls {tool} on {label}; the server may change data."


@dataclass
class _Added:
    label: str
    names: set[str] = field(default_factory=set)
    operations: set[str] = field(default_factory=set)
    scopes: tuple[propose.ProposableScope, ...] = ()


class UpstreamRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._added: dict[str, _Added] = {}

    def apply(self, upstream: UpstreamServer, saved: tuple[SavedTool, ...]) -> None:
        """Replace everything ``upstream`` registered before with ``saved``. An exposed name that is
        already a tool of anything else raises ``ConfigurationError`` before a table is touched."""
        with self._lock:
            previous = self._added.get(upstream.id)
            own = previous.names if previous is not None else set()
            names = [f"{upstream.id}_{tool.name}" for tool in saved]
            for name in names:
                if name in auto_accept.TOOL_TO_GATE and name not in own:
                    raise ConfigurationError(f'Tool "{name}" collides with an existing tool')
            added = _Added(label=upstream.label)
            operations = [f"{upstream.id}.{tool.name}" for tool in saved]
            auto_scopes: list[propose.ProposableScope] = []
            for tool, name, operation in zip(saved, names, operations, strict=True):
                configured = tool.gate
                person = person_gate(configured, classify(tool.read_only, tool.destructive)[0])
                auto_accept.TOOL_TO_GATE[name] = person
                auto_accept.TOOL_TO_OPERATION[name] = operation
                policy_registry.TOOL_TO_VERB[name] = Verb(tool.verb)
                if person == "popup":
                    write_effects.EFFECT_BY_TOOL[name] = EFFECT_BY_TOOL.format(tool=tool.name, label=upstream.label)
                else:
                    write_effects.EFFECT_BY_TOOL.pop(name, None)
                gate._TOOL_LAYOUT[name] = WIDE
                scopes.UPSTREAM_DECLARATIONS[operation] = declaration_digest(
                    upstream.url, operation, tool.verb, configured, tool.digest,
                )
                if configured == "auto":
                    scopes.UPSTREAM_AUTO_TOOLS.add(name)
                    auto_scopes.append(_always_allow_scope(upstream.id, tool, name, operation, operations))
                else:
                    scopes.UPSTREAM_AUTO_TOOLS.discard(name)
                added.names.add(name)
                added.operations.add(operation)
            added.scopes = tuple(auto_scopes)
            if previous is not None:
                self._remove(previous.names - added.names, previous.operations - added.operations)
            self._added[upstream.id] = added
            self._refresh()

    def clear(self, upstream_id: str) -> None:
        """Remove everything ``upstream_id`` registered (a no-op for an upstream that registered nothing)."""
        with self._lock:
            added = self._added.pop(upstream_id, None)
            if added is None:
                return
            self._remove(added.names, added.operations)
            self._refresh()

    def unregister_all(self) -> None:
        """Remove every upstream (tests, and shutdown)."""
        with self._lock:
            added = list(self._added.values())
            self._added.clear()
            for entry in added:
                self._remove(entry.names, entry.operations)
            self._refresh()

    @staticmethod
    def _remove(names: set[str], operations: set[str]) -> None:
        for name in names:
            auto_accept.TOOL_TO_GATE.pop(name, None)
            auto_accept.TOOL_TO_OPERATION.pop(name, None)
            policy_registry.TOOL_TO_VERB.pop(name, None)
            write_effects.EFFECT_BY_TOOL.pop(name, None)
            gate._TOOL_LAYOUT.pop(name, None)
            scopes.UPSTREAM_AUTO_TOOLS.discard(name)
        for operation in operations:
            scopes.UPSTREAM_DECLARATIONS.pop(operation, None)

    def _refresh(self) -> None:
        policy_registry.rebuild_registry()
        propose.refresh_all_operations()
        propose.set_upstream_scopes([scope for entry in self._added.values() for scope in entry.scopes])
        describe.set_connector_labels({upstream_id: entry.label for upstream_id, entry in self._added.items()})


def _constant(value: str) -> Callable[[ReviewContext], Any]:
    def value_of(_ctx: ReviewContext) -> Any:
        return value

    return value_of


def _always_allow_scope(
    upstream_id: str, tool: SavedTool, name: str, operation: str, operations: list[str],
) -> propose.ProposableScope:
    """The one scope that lets a card (or the rule picker) write the Always allow rule of ``name``."""
    return propose.ProposableScope(
        id=f"upstream.always_allow/{name}", predicate="upstream.always_allow",
        scope_type="upstream.always_allow", connector=upstream_id, verbs=frozenset({Verb(tool.verb)}),
        value_of=_constant(name), hint=tool.name,
        widening_group=f"upstream.always_allow/{name}",
        excludes=frozenset(op for op in operations if op != operation),
        widenable=False, fixed_value=name,
    )


_registry: UpstreamRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> UpstreamRegistry:
    """The process-wide registry, created on first use."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = UpstreamRegistry()
        return _registry


def reset() -> None:
    """Unregister everything and drop the instance (tests)."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.unregister_all()
        _registry = None
