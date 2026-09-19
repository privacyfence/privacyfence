"""v1 -> v2 rule compilation and migration -- P3/P4/P9 of the policy v2 redesign.

Compiles the v1 ``rules_config`` shape -- ``{operation_key: [{"rule": name, "value": value}, ...]}``,
i.e. ``auto_accept_rules`` merged with grant-expanded ``auto_accept_grants`` (``policy.resource_registry.
effective_v1_rules``'s own output) -- into a flat list of ``policy.engine.PolicyRule``.

As of P9, this compiler has exactly one caller: ``migrate_to_policy_v2`` below, the one-time,
on-disk v1 -> v2 migration that keeps "v1 sections stay readable indefinitely for hand-edited
installs" true. Through P8 it also backed ``gate.py``'s shadow-mode comparison against the (now
deleted) v1 ``AutoAcceptEvaluator`` and the ``policy.engine: v1 | v2`` switch that chose which side
was authoritative -- both retired at P9, since there is no more v1 evaluator left to compare
against or fall back to. A hand-edited ``settings.yaml`` that still carries ``auto_accept_rules``/
``auto_accept_grants`` is folded into the v2 ``auto_accept:`` section the next time the daemon
starts, exactly as it always has been; nothing evaluates the v1 sections directly anymore.

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

``migrate_to_policy_v2`` below is built entirely out of this module's own ``compile_rules`` plus
``policy.store``'s ``merge_rules``/``rules_to_config``. Like
``auto_accept.migrate_telegram_search_operation_key``, it is pure (no disk I/O), idempotent
(checks/sets ``policy.store.MIGRATED_TO_POLICY_V2_MARKER``), and never mutates its argument --
``daemon_main.run_app`` (and, for an org principal, ``daemon_main._load_principal_settings``) is the
caller that actually persists the result, backs up the original file first, and logs a summary.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import conditions, resource_registry, scopes, store
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


def migrate_to_policy_v2(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """One-time v1 -> v2 on-disk migration (P4), over ``policy.resource_registry.effective_v1_rules(cfg)``
    -- ``auto_accept_rules`` merged with grant-expanded ``auto_accept_grants``, read straight off
    ``cfg`` rather than requiring the caller to build it, now that nothing else needs that merged
    view for any live purpose (P9 retired the v1 evaluator that used to).

    Idempotent: returns ``(cfg, False)``, the *same* ``cfg`` object, once
    ``store.MIGRATED_TO_POLICY_V2_MARKER`` is already set -- a caller that hasn't checked the return
    value shouldn't pay for, or be able to detect, a copy on every already-migrated startup.
    Otherwise returns a deep copy with a new ``auto_accept:`` section (``store.rules_to_config`` of
    the merged, compiled v1 rule set) and the marker set either way -- but the second element is
    ``True`` only when that section actually got at least one rule. A config with no v1 auto-accept
    rules configured at all compiles to none, and the marker still gets set on the copy returned (so
    a *later* run that does add one starts from "already migrated, nothing more to fold in" rather
    than re-discovering an empty v1 config every startup) -- but the caller is told there's nothing
    worth writing to disk for. ``daemon_main.run_app``/``_load_principal_settings`` read that second
    element as "is there anything to actually persist", not "did this function run". Without this, a
    fresh install with zero configured rules would still perform a real disk write, a ``.bak``, and
    a log line the very first time it starts -- for a migration that moved nothing.

    Deliberately never touches ``auto_accept_rules``/``auto_accept_grants`` -- v1 sections stay on
    disk, readable indefinitely, for a hand-edited install (redesign proposal's P4). Only the marker
    being set changes what a *future* migration run does.
    """
    if cfg.get(store.MIGRATED_TO_POLICY_V2_MARKER):
        return cfg, False
    cfg = deepcopy(cfg)
    compiled = store.merge_rules(compile_rules(resource_registry.effective_v1_rules(cfg)))
    cfg[store.AUTO_ACCEPT_CONFIG_KEY] = store.rules_to_config(compiled)
    cfg[store.MIGRATED_TO_POLICY_V2_MARKER] = True
    return cfg, bool(compiled)


__all__ = ["compile_rule_entry", "compile_rules", "migrate_to_policy_v2"]
