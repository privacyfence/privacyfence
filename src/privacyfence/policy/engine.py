"""The v2 rule evaluator -- P3 of the policy v2 redesign.

Replaces the rule-evaluation half of ``auto_accept.AutoAcceptEvaluator`` (``should_auto_accept``,
``preflight_from_args``, the temp-accept window) with one built on P1's registry and P2's scope/
condition selectors, so a rule's three-way preflight verdict is *derived* from each selector's own
declared ``resolves_from`` rather than kept in sync by hand across ``ARGS_ONLY_RULES``/
``DATA_DEPENDENT_RULES`` (F6).

This module knows nothing about where a ``PolicyRule`` list comes from -- ``policy/compat.py``
compiles one from today's ``auto_accept_rules``/``auto_accept_grants`` config; ``policy/store.py``
(P4) compiles one straight from the on-disk v2 ``auto_accept:`` schema. Either way, ``evaluate()``
and ``preflight()`` are drop-in replacements for ``should_auto_accept()``/``preflight_from_args()``:
same ``(bool, matched_rule_id)`` / ``(verdict, matched_rule_id, reason)`` shapes, same fail-closed
behaviour on an unrecognised predicate or an evaluation error, same temp-accept fallback -- delegated
to whichever store the caller passes in (``is_temp_accepted``), rather than re-implemented here, so a
v2-authoritative gate.py and its v1 shadow keep sharing one grace-window store instead of drifting the
moment a user clicks "Allow once" (see ``auto_accept.AutoAcceptEvaluator.is_temp_accepted``).

For P3, ``gate.py`` runs this alongside the old evaluator for one release (shadow mode) rather than
replacing it outright -- see the redesign proposal's "Safety net": both engines run on every real
call, the old one decides by default, and a disagreement is logged at ``WARNING``, never at the
content level. ``policy_engine_config.PolicyEngineConfig`` is the switch that makes this engine
authoritative instead.
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
    """One compiled v2 rule: ``predicate`` + ``value`` select a scope (via
    ``scopes.SCOPE_SELECTORS``/``scopes.NEW_SCOPE_SELECTORS``), narrowed by zero or more
    ``conditions`` (via ``conditions.CONDITION_SELECTORS``) that must all hold. ``operations``
    is the set of v1 operation keys this rule applies to -- the operation key doesn't disappear
    in v2, it stays the engine's internal address (redesign proposal §09); it's what a future
    schema's user-facing verb list compiles down to.

    ``id`` is a stable-enough identifier for logging and shadow-mode diffing. It is *not* the
    real, immutable rule id the on-disk v2 schema will mint (redesign proposal §07) -- for a
    ``policy/compat.py``-compiled rule there is no such id yet, since nothing on disk changes in
    P3; ``compat.py`` uses the original v1 rule name, which is exactly what today's
    ``AutoAcceptEvaluator.should_auto_accept`` already reports as ``matched_rule``, so a shadow
    disagreement log can compare the two directly.
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
    """The rule object ``evaluate()`` below would match, or ``None`` -- factored out for P8 (rule
    attribution): a caller that needs to know *which row* matched, not just its ``.id`` (which,
    for a ``policy/compat.py``-compiled rule, is the ambiguous v1 predicate name, not a stable
    per-resource identity -- see ``policy.store.rule_id_for_rule``), needs the object itself, and
    re-deriving it from ``evaluate()``'s returned id would be wrong whenever two rules in the same
    list happen to share one (exactly the F9 shape this whole redesign exists to fix). Never
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
    """v2 counterpart of ``AutoAcceptEvaluator.should_auto_accept``. Same ``(bool,
    matched_rule_id)`` shape: the first rule (in ``rules``' own order) whose ``operations``
    contains ``operation_key``, whose scope matches, and whose conditions all hold wins: an
    ordinary union, no precedence beyond "first configured, first checked" (``effect`` is
    reserved to ``"allow"`` only in v2 -- see the redesign proposal's D3). An unrecognised
    predicate, or a selector/condition that raises, is treated as a non-match rather than
    propagated -- fail closed, exactly as ``should_auto_accept``'s own ``except Exception``
    around ``self._evaluate`` does.
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
    """v2 counterpart of ``AutoAcceptEvaluator.preflight_from_args``. Never touches
    ``ctx.raw_data`` (always ``None`` here, same as the original), and only ever evaluates a
    rule whose scope selector *and* every attached condition selector declare
    ``resolves_from == ResolvesFrom.ARGS`` -- derived per rule from the selectors it actually
    uses, rather than kept in a hand-maintained set (F6). Returns ``(verdict, matched_rule_id,
    reason)`` with ``verdict`` one of ``"auto_accept"``, ``"requires_review"``, ``"unknown"``,
    same semantics as the original.
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
