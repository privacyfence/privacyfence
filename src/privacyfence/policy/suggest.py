"""Which rules would cover an uncovered call, derived from the call's own arguments.

A scheduled runner that gets ``requires_review`` from ``privacyfence_check_policy`` needs to know
which rule to ask for, without keeping its own tool-to-rule table that drifts the moment a connector
or verb changes. This module answers from the same catalogue the approval card's "Always allow"
button and Settings use (``propose.PROPOSABLE_SCOPES``, ``catalogue.EXTRA_SCOPES``), so the answer
is the server's, and a suggestion reveals nothing about the person's own rules: it is the call's
arguments restated in catalogue terms. That is why a scoped token may receive it, though it may not
call ``privacyfence_list_policy``.

Unlike ``propose.proposals_for`` there is no fetched item here (``raw_data`` is ``None``), so a scope
that resolves from the fetched item yields a suggestion whose value is only known at run time.
"""
from __future__ import annotations

from typing import Any

from ..auto_accept import ReviewContext
from . import catalogue, propose, scopes
from .registry import TOOL_REGISTRY

# The argument each extra scope's selector reads (``scopes._grist_document_matches`` and friends).
_EXTRA_ARGUMENT: dict[str, str] = {
    "grist.document": "doc_id",
    "peek.page": "slug",
    "apps_script.project": "script_id",
}


def suggested_rules(tool: str, args: dict[str, Any], my_email: str = "") -> list[dict[str, Any]]:
    """The rules that would cover ``tool`` called with ``args``, narrowest first.

    Each entry is ``{group, label, value, value_known, verb, operation}``. ``value`` is a list when
    the arguments name the resource, or ``None`` with ``value_known`` false when the rule would
    name a resource the call reaches only at run time. An unconditional rule is never suggested, and
    an unknown or ungoverned tool yields ``[]``.
    """
    entry = TOOL_REGISTRY.get(tool)
    if entry is None or entry.operation is None or entry.verb is None:
        return []
    connector = propose.connector_of_operation(entry.operation)
    ctx = ReviewContext(connector="", tool=tool, args=args, raw_data=None, my_email=my_email)
    labels = {item["id"]: item["label"] for item in catalogue.scope_catalogue()}

    # (group, predicate, value builder, needs a value)
    candidates: list[tuple[str, str, Any, bool]] = []
    for scope in propose.PROPOSABLE_SCOPES:
        if scope.connector != connector or entry.verb not in scope.verbs:
            continue
        if entry.operation in scope.excludes or scope.condition is not None or not scope.widenable:
            continue
        candidates.append(
            (scope.widening_group, scope.predicate, scope.value_of, propose.scope_needs_value(scope))
        )
    for group, extra in catalogue.EXTRA_SCOPES.items():
        if extra.connector != connector or entry.verb not in extra.verbs:
            continue
        if entry.operation in extra.excludes or not extra.needs_value:
            continue
        candidates.append((group, extra.predicate, _extra_value_of(group), True))

    suggestions: list[dict[str, Any]] = []
    for group, predicate, value_of, needs_value in candidates:
        try:
            value = value_of(ctx)
            selector = scopes.SCOPE_SELECTORS.get(predicate) or scopes.NEW_SCOPE_SELECTORS.get(predicate)
            if selector is None:
                continue
            if value is propose.NO_VALUE:
                known = False
                if selector.resolves_from is scopes.ResolvesFrom.ARGS or not needs_value:
                    continue
            else:
                if value is None or not selector.matches(value, ctx):
                    continue
                known = True
        except Exception:
            continue  # nosec B112  # a failing builder or selector means no suggestion, the same fail-closed posture as propose._candidate_value
        suggestions.append({
            "group": group, "label": labels.get(group, group),
            "value": value if known else None, "value_known": known,
            "verb": entry.verb.value, "operation": entry.operation,
        })
    return suggestions


def _extra_value_of(group: str) -> Any:
    argument = _EXTRA_ARGUMENT.get(group)
    if argument is None:
        # No call argument names this scope's value (gmail.query): it is known only at run time.
        return lambda _ctx: propose.NO_VALUE

    def value_of(ctx: ReviewContext) -> Any:
        value = ctx.args[argument] if argument in ctx.args else ""
        return [value] if isinstance(value, str) and value else propose.NO_VALUE

    return value_of


__all__ = ["suggested_rules"]
