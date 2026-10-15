"""The rule evaluator -- the only one there is.

Built on the tool registry (`policy/registry.py`) and the scope/condition selectors
(`policy/scopes.py`, `policy/conditions.py`), so a rule's three-way preflight verdict is *derived*
from each selector's own declared ``resolves_from`` rather than kept in sync by hand in a separate
list of "arguments-only" and "needs the fetched item" predicates, which is how the old evaluator
drifted.

This module knows nothing about where a ``PolicyRule`` list comes from -- in production
``policy/store.py`` compiles one from the on-disk ``auto_accept:`` section. ``evaluate()`` returns
``(bool, matched_rule_id)`` and ``preflight()`` returns ``(verdict, matched_rule_id, reason)``; both
fail closed on an unrecognised predicate or an evaluation error. The temp-accept grace window is
delegated to whichever store the caller passes in (``is_temp_accepted``) rather than re-implemented
here, so every caller shares the one store a click on "Allow once" writes to.

The old evaluator, and the ``policy.engine`` switch that once chose between it and this one, are
gone ([ADR 0004](../../../docs/adr/0004-retire-the-v1-auto-accept-config-model.md)). What is left of
that migration's safety net is the equivalence harness that proves each selector behaves like the
predicate it replaced (``tests/unit/policy/_v1_reference.py``). The one-time settings conversion is
gone too (ADR 0041).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..auto_accept import ReviewContext, temp_accept_key
from . import conditions, scopes

logger = logging.getLogger(__name__)

IsTempAccepted = Callable[[str, "str | None"], bool]


@dataclass(frozen=True)
class PolicyRule:
    """One compiled rule: ``predicate`` + ``value`` select a scope (via
    ``scopes.SCOPE_SELECTORS``/``scopes.NEW_SCOPE_SELECTORS``), narrowed by zero or more
    ``conditions`` (via ``conditions.CONDITION_SELECTORS``) that must all hold. ``operations``
    is the set of operation keys this rule applies to -- the operation key stays the engine's
    internal address, and a user-facing verb list compiles down to it.

    ``id`` is the rule's identifier for logging. A rule read back from disk carries the
    content-derived id ``policy.store.rule_id_for`` minted, and that stored id is what decisions
    are attributed to (ADR 0074). ``policy.store.rule_id_for_rule`` recomputes the same id from a
    rule's content.
    """

    id: str
    predicate: str
    value: Any
    operations: frozenset[str]
    conditions: tuple[tuple[str, Any], ...] = ()


def _selector_for(predicate: str) -> "scopes.ScopeSelector | None":
    return scopes.SCOPE_SELECTORS.get(predicate) or scopes.NEW_SCOPE_SELECTORS.get(predicate)


def _conditions_hold(rule: PolicyRule, ctx: ReviewContext) -> bool:
    for name, value in rule.conditions:
        selector = conditions.CONDITION_SELECTORS.get(name)
        if selector is None or not selector.holds(value, ctx):
            return False
    return True


def find_matching_rule(
    rules: Iterable[PolicyRule], operation_key: str, ctx: ReviewContext,
) -> "PolicyRule | None":
    """The rule object ``evaluate()`` below would match, or ``None`` -- for rule attribution:
    gate.py's ``_evaluate_auto_accept`` records the matched rule's stored ``.id`` (ADR 0074), and
    a caller that needs *which row* matched gets the object itself rather than looking a rule
    back up by an id two rules in one list could share. Never
    considers the temp-accept grace window -- that is a session-scoped fallback, not a rule row,
    which is exactly why a caller resolving a decision to "one rule row" should get ``None`` here
    for it, not a pseudo-rule.
    """
    for rule in rules:
        if operation_key not in rule.operations:
            continue
        selector = _selector_for(rule.predicate)
        if selector is None:
            continue
        try:
            if not selector.matches(rule.value, ctx):
                continue
            if not _conditions_hold(rule, ctx):
                continue
        except Exception as exc:
            logger.warning("Rule %r evaluation error: %s", rule.id, exc)
            continue
        return rule
    return None


def evaluate(
    rules: Iterable[PolicyRule],
    operation_key: str,
    ctx: ReviewContext,
    *,
    is_temp_accepted: IsTempAccepted | None = None,
) -> tuple[bool, str]:
    """Whether a real call auto-accepts, as ``(bool, matched_rule_id)``: the first rule (in
    ``rules``' own order) whose ``operations`` contains ``operation_key``, whose scope matches, and
    whose conditions all hold wins: an ordinary union, no precedence beyond "first configured,
    first checked" (every rule allows; there is no deny rule to order against). An unrecognised
    predicate, or a selector/condition that raises, is treated as a non-match rather than
    propagated -- fail closed.
    """
    rule = find_matching_rule(rules, operation_key, ctx)
    if rule is not None:
        return True, rule.id
    if is_temp_accepted is not None and is_temp_accepted(operation_key, temp_accept_key(operation_key, ctx)):
        return True, "session_temp_accept"
    return False, ""


def preflight(
    rules: Iterable[PolicyRule],
    operation_key: str,
    args: dict,
    *,
    my_email: str = "",
    is_temp_accepted: IsTempAccepted | None = None,
) -> tuple[str, str, str]:
    """What a call would get before it is made, from its arguments alone. Never touches
    ``ctx.raw_data`` (always ``None`` here), and only ever evaluates a rule whose scope selector
    *and* every attached condition selector declare ``resolves_from == ResolvesFrom.ARGS`` --
    derived per rule from the selectors it actually uses, rather than kept in a hand-maintained
    set. Returns ``(verdict, matched_rule_id, reason)`` with ``verdict`` one of
    ``"auto_accept"``, ``"requires_review"``, ``"unknown"``.
    """
    ctx = ReviewContext(connector="", tool="", args=args or {}, raw_data=None, my_email=my_email)

    file_key = temp_accept_key(operation_key, ctx)
    if is_temp_accepted is not None and is_temp_accepted(operation_key, file_key):
        return "auto_accept", "session_temp_accept", "Matched an active same-file temp-accept grace window."

    configured = [rule for rule in rules if operation_key in rule.operations]
    if not configured:
        return "requires_review", "", "No auto-accept rule is configured for this operation."

    undetermined: list[str] = []
    for rule in configured:
        selector = _selector_for(rule.predicate)
        if selector is None:
            undetermined.append(rule.id)
            continue
        condition_selectors = [conditions.CONDITION_SELECTORS.get(name) for name, _ in rule.conditions]
        args_only = selector.resolves_from is scopes.ResolvesFrom.ARGS and all(
            cs is not None and cs.resolves_from is conditions.ResolvesFrom.ARGS for cs in condition_selectors
        )
        if not args_only:
            undetermined.append(rule.id)
            continue
        try:
            if selector.matches(rule.value, ctx) and _conditions_hold(rule, ctx):
                return "auto_accept", rule.id, f"Matched args-only rule {rule.id!r}."
        except Exception:
            undetermined.append(rule.id)

    if undetermined:
        return (
            "unknown",
            "",
            "Depends on the fetched item's data, not just this call's arguments "
            "(rule(s): " + ", ".join(sorted(set(undetermined))) + ").",
        )
    return "requires_review", "", "No configured rule matches these arguments."


__all__ = ["PolicyRule", "evaluate", "find_matching_rule", "preflight"]
