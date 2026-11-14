"""Registers the operator's declared upstream MCP tools into the policy tables at startup.

``register`` runs once, before the web server serves (ADR 0283). For every declared tool it writes
the gate, operation key, verb, effect text and approval-window layout into the same module-level
tables the built-in connectors use, then rebuilds the two derived values (``TOOL_REGISTRY``,
``propose.ALL_OPERATIONS``). No scope selector, catalogue entry or proposable scope is added, so an
upstream tool gets no "Always allow" offer and no rule. ``unregister_all`` undoes it (tests only).
"""
from __future__ import annotations

from collections.abc import Sequence

from . import auto_accept, gate, write_effects
from .approval_window_html import WIDE
from .org_mode import ConfigurationError
from .policy import propose
from .policy import registry as policy_registry
from .policy.registry import Verb
from .upstream_mcp_config import ENV_VAR, UpstreamServer

_added: set[str] = set()


def register(upstreams: Sequence[UpstreamServer]) -> None:
    """Write every declared tool into the policy tables. An exposed name that is already a tool
    (or repeats within ``upstreams``) raises ``ConfigurationError`` before anything is written."""
    seen: set[str] = set()
    for upstream in upstreams:
        for tool in upstream.tools:
            if tool.exposed_name in auto_accept.TOOL_TO_GATE or tool.exposed_name in seen:
                raise ConfigurationError(
                    f'{ENV_VAR}: tool "{tool.exposed_name}" collides with an existing tool'
                )
            seen.add(tool.exposed_name)
    for upstream in upstreams:
        for tool in upstream.tools:
            name = tool.exposed_name
            auto_accept.TOOL_TO_GATE[name] = tool.gate
            auto_accept.TOOL_TO_OPERATION[name] = tool.operation
            policy_registry.TOOL_TO_VERB[name] = Verb(tool.verb)
            if tool.gate == "popup":
                write_effects.EFFECT_BY_TOOL[name] = tool.effect
            if tool.gate in ("review", "popup"):
                gate._TOOL_LAYOUT[name] = WIDE
            _added.add(name)
    _refresh()


def unregister_all() -> None:
    """Remove every name ``register`` added and rebuild the derived values."""
    for name in _added:
        auto_accept.TOOL_TO_GATE.pop(name, None)
        auto_accept.TOOL_TO_OPERATION.pop(name, None)
        policy_registry.TOOL_TO_VERB.pop(name, None)
        write_effects.EFFECT_BY_TOOL.pop(name, None)
        gate._TOOL_LAYOUT.pop(name, None)
    _added.clear()
    _refresh()


def _refresh() -> None:
    policy_registry.rebuild_registry()
    propose.refresh_all_operations()
