"""v1 -> v2 in-memory rule compiler -- P3 of the policy v2 redesign.

Compiles the exact ``rules_config`` shape ``auto_accept.AutoAcceptEvaluator.__init__`` already
takes -- ``{operation_key: [{"rule": name, "value": value}, ...]}``, i.e. ``auto_accept_rules``
already merged with grant-expanded ``auto_accept_grants`` (``resource_grants.build_effective_rules``'s
own output, which is also what ``AutoAcceptEvaluator.effective_rules`` holds live) -- into a flat
list of ``policy.engine.PolicyRule``. Grant expansion happens once, upstream, the same place it
already does for v1; this module does not re-derive it, so a compiled rule always reflects whatever
the live v1 evaluator is currently holding.

P3's ``policy/compat.py`` is only this compiler. The on-disk v1 -> v2 migration the redesign
proposal's §09 also files under ``compat.py`` (writing an ``auto_accept:`` section, a ``.bak``, the
``migrated_to_policy_v2`` marker) is P4's job -- nothing on disk changes here, and nothing here reads
or writes ``settings.yaml`` directly.

Because v1's rule list is a flat union (any one entry matching auto-accepts -- there is no
conjunction between entries), and every predicate is either a P2 scope selector or a P2 condition
selector but never both, compiling one v1 entry is unambiguous:

* a **scope** predicate (``rule_name in policy.scopes.SCOPE_SELECTORS``) compiles to a rule whose
  scope is that predicate and carries no ``when:`` conditions -- v1 had no way to attach one;
* a **condition** predicate used on its own (``policy.conditions.condition_for_predicate(rule_name)``
  -- e.g. a bare ``shared_drive_exclusion`` entry, which today auto-accepts *any* write to a
  non-shared-drive file, regardless of folder) compiles to the generic ``always_allow`` scope
  carrying that one condition, the same "honestly unconditional" shape D4 gives ``always_allow``
  itself -- reproducing v1's actual (surprisingly broad) behaviour rather than accidentally
  narrowing it to something safer-looking;
* an unrecognised name (should not exist -- P1/P2 map all 47 v1 predicates) compiles to nothing --
  fail closed, matching ``policy.engine.evaluate``'s own handling of a predicate it can't find.

This makes the translation law trivial to check by construction rather than by review: a compiled
rule's ``operations`` is always exactly ``{operation_key}``, the one v1 entry it came from -- never
that operation's verb family, never another operation sharing the same rule name. Widening is not a
risk this compiler can introduce.
"""
from __future__ import annotations

from typing import Any

from . import conditions, scopes
from .engine import PolicyRule

# The v1 pseudo-rule name `should_auto_accept`/`preflight_from_args` return for a match against
# the in-memory temp-accept grace window (`auto_accept.temp_accept_key`) -- never a configured
# rule, so it never appears in `rules_config` and compiles to nothing here. `policy.engine.evaluate`/
# `preflight` reproduce the same fallback themselves, via the `is_temp_accepted` callback.
_TEMP_ACCEPT_PSEUDO_RULE = "session_temp_accept"

_ALWAYS_ALLOW_PREDICATE = "always_allow"


def compile_rule_entry(operation_key: str, rule_name: str, value: Any) -> "PolicyRule | None":
    """Compile one ``{"rule": rule_name, "value": value}`` v1 entry for ``operation_key`` into a
    ``PolicyRule``, or ``None`` if ``rule_name`` isn't a real predicate."""
    if rule_name == _TEMP_ACCEPT_PSEUDO_RULE:
        return None
    if rule_name in scopes.SCOPE_SELECTORS:
        return PolicyRule(id=rule_name, predicate=rule_name, value=value, operations=frozenset({operation_key}))
    condition = conditions.condition_for_predicate(rule_name)
    if condition is not None:
        return PolicyRule(
            id=rule_name,
            predicate=_ALWAYS_ALLOW_PREDICATE,
            value=None,
            operations=frozenset({operation_key}),
            conditions=((condition.name, value),),
        )
    return None


def compile_rules(rules_config: dict[str, list[dict[str, Any]]]) -> list[PolicyRule]:
    """Compile every operation key's v1 rule list into ``PolicyRule``s, in the same order --
    order doesn't change *whether* something matches (v1's list is a union), but preserving it
    keeps a shadow-mode diff's "which rule matched" comparison meaningful. ``rules_config`` is the
    exact shape ``AutoAcceptEvaluator(rules_config)``/``AutoAcceptEvaluator.effective_rules``
    holds -- pass either directly.
    """
    compiled: list[PolicyRule] = []
    for operation_key, entries in (rules_config or {}).items():
        for entry in entries or ():
            rule = compile_rule_entry(operation_key, entry.get("rule", ""), entry.get("value"))
            if rule is not None:
                compiled.append(rule)
    return compiled


__all__ = ["compile_rule_entry", "compile_rules"]
