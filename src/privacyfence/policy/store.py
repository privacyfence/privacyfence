"""The on-disk v2 policy schema -- P4 of the policy v2 redesign.

``policy/engine.py``'s own docstring (P3) names this module in advance: "a later phase's
``policy/store.py`` will compile [a ``PolicyRule`` list] straight from the on-disk v2 schema" --
this is that phase. It defines the ``auto_accept:`` section ``config/settings.yaml`` grows once a
config is migrated (``policy/compat.migrate_to_policy_v2``), and the loader that reads it back into
the exact ``PolicyRule`` shape ``policy.engine.evaluate``/``preflight`` already consume.

Schema (nothing else in this repo writes or reads this key yet outside this module and its
migration caller):

.. code-block:: yaml

    auto_accept:
      version: 2
      rules:
        - id: r-3f9a1c2b8e
          predicate: approved_sandbox_folder
          value: ["1CdeF..."]
          operations: [drive.write_file, sheets.write_range]
          conditions: [[shared_drive_exclusion, null]]

``id`` is content-derived (``rule_id_for``) rather than a random UUID: the same (predicate, value,
conditions) triple always mints the same id, regardless of which operations carry it or how many
times a config is (re-)migrated, so a rule's identity in the audit log and in a future editing UI
(P8) doesn't churn on every restart. ``operations`` stays the v1 operation-key vocabulary --
``policy.engine.PolicyRule.operations``'s own docstring already explains why the key doesn't
disappear in v2 -- a later surface phase (P5/P6) is what actually renders these as verbs.

Every function here is pure and fails closed: a malformed ``auto_accept:`` section (wrong type, a
rule entry missing a required field, an ``operations``/``conditions`` value of the wrong shape)
never raises and never invents a match -- it's dropped, exactly like ``policy.compat.compile_rule_entry``
already treats an unrecognised v1 predicate name. This module never reads or writes
``settings.yaml`` itself; ``policy/compat.py`` (the migration) and ``daemon_main.run_app`` (the
actual disk I/O) are the callers that do.
"""
from __future__ import annotations

import hashlib
from typing import Any

from . import registry
from .engine import PolicyRule

AUTO_ACCEPT_CONFIG_KEY = "auto_accept"
SCHEMA_VERSION = 2

# Set on the config dict (alongside ``auto_accept_rules``/``auto_accept_grants``, which are left
# in place -- "v1 sections stay readable indefinitely for hand-edited installs", per the redesign
# proposal's P4) the first time ``policy.compat.migrate_to_policy_v2`` runs. Mirrors
# ``resource_grants.MIGRATION_MARKER``/``auto_accept.TELEGRAM_SEARCH_OPERATION_KEY_MIGRATION_MARKER``'s
# own in-config-dict, checked-and-set-once shape.
MIGRATED_TO_POLICY_V2_MARKER = "migrated_to_policy_v2"

_DESTRUCTIVE_OR_SEND_FAMILIES = frozenset({registry.VerbFamily.DESTRUCTIVE, registry.VerbFamily.SEND})


def rule_id_for(predicate: str, value: Any, conditions: tuple[tuple[str, Any], ...]) -> str:
    """A stable id for a rule's *meaning* -- what it trusts and under what conditions -- never for
    which operations carry it. Two compiled rules that agree on ``(predicate, value, conditions)``
    always get the same id, which is what lets ``merge_rules`` below union their operations into one
    on-disk row without minting a fresh id for the merge."""
    key = repr((predicate, _sortable(value), conditions))
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return f"r-{digest}"


def _sortable(value: Any) -> Any:
    """``list`` values are compared/hashed order-independently -- a migrated rule's id must not
    depend on the order grant expansion happened to produce its resource-id list in."""
    if isinstance(value, list):
        return tuple(sorted((repr(v) for v in value)))
    return value


def merge_rules(rules: list[PolicyRule]) -> list[PolicyRule]:
    """Union rules that share a ``(predicate, value, conditions)`` key into one rule spanning every
    operation any of them covered, preserving first-seen order. This changes nothing about *whether*
    a call matches -- ``policy.engine.evaluate`` already treats ``rule.operations`` as an unordered
    set membership test -- it only makes the on-disk config as compact as the redesign proposal's
    "Model" section describes ("one sentence... Allow read, update and format on the folder...")
    instead of one row per v1 operation key.
    """
    merged: dict[tuple[str, Any, tuple[tuple[str, Any], ...]], PolicyRule] = {}
    order: list[tuple[str, Any, tuple[tuple[str, Any], ...]]] = []
    for rule in rules:
        key = (rule.predicate, _sortable(rule.value), rule.conditions)
        existing = merged.get(key)
        if existing is None:
            merged[key] = PolicyRule(
                id=rule_id_for(rule.predicate, rule.value, rule.conditions),
                predicate=rule.predicate, value=rule.value,
                operations=rule.operations, conditions=rule.conditions,
            )
            order.append(key)
        else:
            merged[key] = PolicyRule(
                id=existing.id, predicate=existing.predicate, value=existing.value,
                operations=existing.operations | rule.operations, conditions=existing.conditions,
            )
    return [merged[key] for key in order]


def rule_to_dict(rule: PolicyRule) -> dict[str, Any]:
    """Serialize one ``PolicyRule`` into the on-disk shape. ``operations`` is sorted for a stable,
    diffable YAML rendering -- ``frozenset`` iteration order is not guaranteed."""
    return {
        "id": rule.id,
        "predicate": rule.predicate,
        "value": rule.value,
        "operations": sorted(rule.operations),
        "conditions": [[name, value] for name, value in rule.conditions],
    }


def rule_from_dict(data: Any) -> PolicyRule | None:
    """The read side of ``rule_to_dict``. Fails closed on anything malformed -- a hand-edited or
    corrupted entry is dropped, never raised, and never treated as a match."""
    if not isinstance(data, dict):
        return None
    rule_id, predicate = data.get("id"), data.get("predicate")
    operations = data.get("operations")
    conditions = data.get("conditions")
    if not isinstance(rule_id, str) or not rule_id:
        return None
    if not isinstance(predicate, str) or not predicate:
        return None
    if not isinstance(operations, list) or not operations or not all(isinstance(o, str) for o in operations):
        return None
    if conditions is None:
        conditions = []
    if not isinstance(conditions, list):
        return None
    parsed_conditions: list[tuple[str, Any]] = []
    for entry in conditions:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2 or not isinstance(entry[0], str):
            return None
        parsed_conditions.append((entry[0], entry[1]))
    return PolicyRule(
        id=rule_id, predicate=predicate, value=data.get("value"),
        operations=frozenset(operations), conditions=tuple(parsed_conditions),
    )


def rules_to_config(rules: list[PolicyRule]) -> dict[str, Any]:
    """The full ``auto_accept:`` section value for a rule list -- what ``policy.compat.
    migrate_to_policy_v2`` assigns onto ``cfg[AUTO_ACCEPT_CONFIG_KEY]``."""
    return {"version": SCHEMA_VERSION, "rules": [rule_to_dict(rule) for rule in rules]}


def compile_rules_from_config(cfg: dict[str, Any]) -> list[PolicyRule]:
    """Compile the on-disk v2 ``auto_accept:`` section (if present and well-formed) into
    ``PolicyRule``s -- the v2 counterpart of ``policy.compat.compile_rules`` reading v1's
    ``auto_accept_rules``/``auto_accept_grants``. A missing or malformed section compiles to no
    rules, never an error -- exactly ``compile_rule_entry``'s own fail-closed posture for one bad
    entry, applied to the section as a whole.
    """
    section = cfg.get(AUTO_ACCEPT_CONFIG_KEY) if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        return []
    raw_rules = section.get("rules")
    if not isinstance(raw_rules, list):
        return []
    compiled: list[PolicyRule] = []
    for raw in raw_rules:
        rule = rule_from_dict(raw)
        if rule is not None:
            compiled.append(rule)
    return compiled


def verb_families(rule: PolicyRule) -> frozenset[registry.VerbFamily]:
    """Every ``VerbFamily`` any operation this rule covers belongs to -- e.g. a rule whose
    ``operations`` includes ``sheets.delete_dimensions`` carries ``VerbFamily.DESTRUCTIVE``
    (see F4: today that operation is folded into a plain "Write" grant with no distinct signal)."""
    # registry.VERB_FAMILY is a complete literal over every Verb member (same assumption
    # registry.ToolRegistryEntry.verb_family's own `VERB_FAMILY[self.verb]` makes) -- a plain
    # index, not `.get()`, so an accidentally-incomplete future edit to that table fails loudly
    # here instead of silently dropping a verb from every family check.
    return frozenset(registry.VERB_FAMILY[verb] for operation in rule.operations
                      for verb in registry.operation_verbs(operation))


def is_destructive_or_send(rule: PolicyRule) -> bool:
    """Whether any operation this rule's expansion covers is destructive (``delete``) or a send
    (``send``/``draft``/``share``) -- the two verb families P4's migration banner calls out, since
    those are the ones a v1 config could have granted (via a broad grant capability like F4's
    sandbox-folder "Write") without the user ever having seen the word "delete" or "send" attached
    to what they approved."""
    return bool(verb_families(rule) & _DESTRUCTIVE_OR_SEND_FAMILIES)


def destructive_or_send_rules(cfg: dict[str, Any]) -> list[PolicyRule]:
    """Every currently-configured v2 rule (compiled from the on-disk ``auto_accept:`` section)
    whose expansion is destructive-or-send -- what the migration banner (``settings_controller.
    SettingsController.policy_v2_migration_notice_html``) lists."""
    return [rule for rule in compile_rules_from_config(cfg) if is_destructive_or_send(rule)]


__all__ = [
    "AUTO_ACCEPT_CONFIG_KEY",
    "MIGRATED_TO_POLICY_V2_MARKER",
    "SCHEMA_VERSION",
    "compile_rules_from_config",
    "destructive_or_send_rules",
    "is_destructive_or_send",
    "merge_rules",
    "rule_from_dict",
    "rule_id_for",
    "rule_to_dict",
    "rules_to_config",
    "verb_families",
]
