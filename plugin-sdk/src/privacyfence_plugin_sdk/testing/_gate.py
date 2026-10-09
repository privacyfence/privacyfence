"""The simulated gate: scope rules, the card a human would see, and what a call produced."""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Union


@dataclass
class Card:
    """What a review or popup card shows: the plugin's preview, a read's payload, and its scopes.

    ``pii_flagged`` is True when the host's ``pii`` check flagged the call, which overrides any
    "Always allow" rule.
    """

    preview: list[dict]
    payload: list[dict] | None
    scopes: dict[str, list[str]]
    pii_flagged: bool = False


@dataclass
class ToolOutcome:
    """The result of ``PluginTestHost.call_tool``.

    ``gate`` is the tool's declared gate. ``card`` is what ``tool.prepare`` returned, whether or not
    a human saw it; ``card_shown`` says whether the gate showed it. ``released`` is what the AI
    would receive: for a read the prepared payload as ``{"blocks": [...]}``, for a write the
    execute result. ``result`` is the plugin's raw execute result. ``approval`` is what
    ``tool.execute`` carried. ``error`` is ``{"code", "detail"}`` when the call did not release.
    """

    gate: str
    card_shown: bool = False
    card: Card | None = None
    released: Any = None
    result: Any = None
    approval: dict | None = None
    audit: list[dict] = field(default_factory=list)
    error: dict | None = None


Decision = Union[str, bool, Callable[[Card], Union[str, bool, Awaitable[Union[str, bool]]]]]


async def resolve_decision(decide: Decision, card: Card) -> bool:
    """True when the card is approved. ``decide`` is ``"approve"``, ``"deny"`` or a function of the card."""
    value: Any = decide
    if callable(decide):
        value = decide(card)
        if inspect.isawaitable(value):
            value = await value
    if value in ("approve", True):
        return True
    if value in ("deny", False):
        return False
    raise ValueError(f"decide must be 'approve', 'deny' or a function returning one of them, not {value!r}")


def scope_rule_matches(returned: Any, allowed: frozenset[str]) -> bool:
    """A rule matches only when every value the call returned is in the rule.

    A missing scope, an empty list and an empty rule never match.
    """
    if not isinstance(returned, (list, tuple)) or not returned or not allowed:
        return False
    return {str(v) for v in returned} <= allowed


class Rules:
    """``host.rules``: the "Always allow" rules a person created from cards."""

    def __init__(self, declared: Callable[[], set[str]]) -> None:
        self._declared = declared
        self._rules: list[tuple[str, frozenset[str]]] = []

    def allow_scope(self, scope_type: str, values: list[str] | tuple[str, ...] | set[str]) -> None:
        """Allow calls whose returned ``scope_type`` values all lie in ``values``.

        Each call to this method is a separate rule, like each "Always allow" click: two rules for
        the same type do not combine, so a call that returned values from both still shows a card.
        """
        if scope_type not in self._declared():
            raise ValueError(f"the plugin declares no scope type {scope_type!r}")
        if isinstance(values, str):
            raise ValueError("values must be a list of strings, not one string")
        allowed = frozenset(str(v) for v in values if v not in (None, ""))
        self._rules.append((scope_type, allowed))

    def clear(self) -> None:
        self._rules.clear()

    def matching_scope(self, tool_scopes: list[str], returned: dict[str, list[str]]) -> str | None:
        """The scope type of the first rule that accepts the call, or ``None``."""
        for scope_type, allowed in self._rules:
            if scope_type in tool_scopes and scope_rule_matches(returned.get(scope_type), allowed):
                return scope_type
        return None


def flatten_text(blocks: list[dict]) -> str:
    """Every string in the blocks, one per line, for the PII scan."""
    lines: list[str] = []
    for block in blocks:
        kind = block["type"]
        if kind == "fields":
            for item in block["items"]:
                lines += [item["label"], item["value"]]
        elif kind == "table":
            keys = [c["key"] for c in block["columns"]]
            lines += [c["label"] for c in block["columns"]]
            for row in block["rows"]:
                lines += [str(row[k]) for k in keys if row.get(k) is not None]
        else:
            lines.append(block["text"])
    return "\n".join(lines)
