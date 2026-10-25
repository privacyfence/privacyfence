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

A value-less extra scope (the Slack mentions, one-to-one DM and conversations rules) is suggested only
when it narrows to something the call could reach, decided from the call's own arguments with no Slack
request (ADR 0229). An unconditional rule (``gmail.anything``, ``slack.anything``) is never suggested.
"""
from __future__ import annotations

from typing import Any, Callable

from ..auto_accept import ReviewContext
from . import catalogue, propose, scopes
from .registry import TOOL_REGISTRY

# The argument each extra scope's selector reads (``scopes._grist_document_matches`` and friends).
_EXTRA_ARGUMENT: dict[str, str] = {
    "grist.document": "doc_id",
    "peek.page": "slug",
    "miro.board": "board_id",
    "apps_script.project": "script_id",
}

_SLACK_CONVERSATION_READS = frozenset({"slack_get_channel_history", "slack_get_thread_replies"})


def _slack_mentions_of_me(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    # The read selector matches only thread replies, the search selector only a search.
    if tool in {"slack_get_thread_replies", "slack_search_messages"}:
        return {}
    return None


def _slack_direct_messages_to_me(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    # An id that does not start with "D" can never be a one-to-one DM (slack_client).
    if tool in _SLACK_CONVERSATION_READS and str(args.get("channel_id") or "").startswith("D"):
        return {}
    return None


def _slack_my_conversations(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if tool in _SLACK_CONVERSATION_READS:
        return {} if args.get("channel_id") else None
    if tool == "slack_search_messages":
        return {} if args.get("member_only") is True else {"requires_args": {"member_only": True}}
    return None


# These scopes narrow to what the call reaches, and they are decided from the agent's own arguments
# with no Slack request: Slack rate-limits conversations.info/users.conversations per method and the
# real call already spends one (ADR 0229).
_VALUELESS_EXTRAS: dict[str, Callable[[str, dict[str, Any]], dict[str, Any] | None]] = {
    "slack.mentions_of_me": _slack_mentions_of_me,
    "slack.direct_messages_to_me": _slack_direct_messages_to_me,
    "slack.my_conversations": _slack_my_conversations,
}


def suggested_rules(tool: str, args: dict[str, Any], my_email: str = "") -> list[dict[str, Any]]:
    """The rules that would cover ``tool`` called with ``args``, narrowest first.

    Each entry is ``{group, label, value, value_known, verb, operation}``. ``value`` is a list when
    the arguments name the resource, ``[]`` when the rule takes no value, or ``None`` with
    ``value_known`` false when the rule would name a resource the call reaches only at run time. An
    entry may carry ``requires_args``, arguments the call must also pass for the rule to cover it.
    A value-less extra scope is suggested only when it narrows, after all the others; an
    unconditional rule never is, and an unknown or ungoverned tool yields ``[]``.
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
    valueless: list[dict[str, Any]] = []
    for group, extra in catalogue.EXTRA_SCOPES.items():
        if extra.connector != connector or entry.verb not in extra.verbs:
            continue
        if entry.operation in extra.excludes:
            continue
        if not extra.needs_value:
            applies = _VALUELESS_EXTRAS.get(group)
            if applies is None:
                continue
            predicate = catalogue.extra_predicate_for(extra, entry.verb)
            if predicate not in scopes.NEW_SCOPE_SELECTORS:
                continue
            try:
                extra_keys = applies(tool, args)
            except Exception:
                continue  # nosec B112  # a failing applicability check means no suggestion, as for the builders below
            if extra_keys is not None:
                valueless.append({
                    "group": group, "label": labels.get(group, group), "value": [], "value_known": True,
                    "verb": entry.verb.value, "operation": entry.operation, **extra_keys,
                })
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
    suggestions.extend(valueless)
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
