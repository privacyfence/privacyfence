"""The on-disk policy schema: the ``auto_accept:`` section of ``config/settings.yaml``, and the
loader that reads it back into the ``PolicyRule`` shape ``policy.engine.evaluate``/``preflight``
consume.

Schema:

.. code-block:: yaml

    auto_accept:
      version: 2
      rules:
        - id: r-3f9a1c2b8e
          predicate: approved_sandbox_folder
          value: ["1CdeF..."]
          operations: [drive.write_file, sheets.write_range]
          conditions: [[not_shared_drive, null]]

``id`` is content-derived (``rule_id_for``) rather than a random UUID: the same (predicate, value,
conditions) triple always mints the same id, regardless of which operations carry it, so a rule's
identity in the audit log and on the Settings Rules page doesn't churn on every restart.
``operations`` uses the operation-key vocabulary (``policy.engine.PolicyRule.operations``'s own
docstring explains why the key survives alongside verbs).

Every function here is pure and fails closed: a malformed ``auto_accept:`` section (wrong type, a
rule entry missing a required field, an ``operations``/``conditions`` value of the wrong shape)
never raises and never invents a match -- it's dropped. The one thing that *does* raise is a config
still carrying the pre-``auto_accept:`` sections (``reject_v1_sections``): those are refused
outright rather than silently ignored, because ignoring them would turn every rule the person wrote
into an approval popup with no hint why (ADR 0041) -- unless an earlier release already converted
them (``drop_converted_v1_sections``, ADR 0047). This module never reads or writes
``settings.yaml`` itself; ``daemon_main.load_config`` and ``auto_accept``'s writers do.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .engine import PolicyRule

AUTO_ACCEPT_CONFIG_KEY = "auto_accept"
SCHEMA_VERSION = 2

# Top-level ``settings.yaml`` keys of the policy format this schema replaced. Nothing reads them;
# ``reject_v1_sections`` refuses a config that still has one (ADR 0041).
V1_SECTION_KEYS: tuple[str, ...] = ("auto_accept_rules", "auto_accept_grants")


# Written at the top level of ``settings.yaml`` by the v1 -> v2 conversion an earlier release ran
# on startup, next to the ``auto_accept:`` section it produced. That conversion left the v1
# sections on disk, and nothing after it read them.
CONVERTED_V1_MARKER = "migrated_to_policy_v2"


def drop_converted_v1_sections(cfg: dict[str, Any]) -> list[str]:
    """Remove, in place, the v1 sections an earlier release already converted, and its marker.

    Returns the keys removed, empty when ``cfg`` carries no ``CONVERTED_V1_MARKER``. Those sections'
    rules are already in ``auto_accept:``, so removing them changes no decision; a v1 section with
    no marker was never converted and is left for ``reject_v1_sections`` to refuse (ADR 0047).
    """
    if not cfg.get(CONVERTED_V1_MARKER):
        return []
    removed = [key for key in (*V1_SECTION_KEYS, CONVERTED_V1_MARKER) if key in cfg]
    for key in removed:
        del cfg[key]
    return removed


class V1PolicyConfigError(ValueError):
    """A ``settings.yaml`` still carries a pre-``auto_accept:`` policy section. A ``ValueError`` so
    ``daemon_main.main`` reports it as a configuration error and exits, like any other bad config."""


def reject_v1_sections(cfg: dict[str, Any], source: str) -> None:
    """Raise ``V1PolicyConfigError`` naming every v1 section present in ``cfg``.

    Refused even when a section is empty: its presence means the file was written for a format
    this version does not read, and saying so is cheaper than a person discovering it one
    unexpected approval popup at a time.
    """
    present = [key for key in V1_SECTION_KEYS if key in cfg]
    if not present:
        return
    names = " and ".join(f"'{key}'" for key in present)
    raise V1PolicyConfigError(
        f"{source} contains {names}, an auto-accept format this version of PrivacyFence no "
        f"longer reads. Remove {'those sections' if len(present) > 1 else 'that section'} and "
        f"recreate the rules on the Settings > Auto-accept page (they are stored in the '{AUTO_ACCEPT_CONFIG_KEY}:' "
        "section). Rules are not converted automatically."
    )


def rule_id_for(predicate: str, value: Any, conditions: tuple[tuple[str, Any], ...]) -> str:
    """A stable id for a rule's *meaning* -- what it trusts and under what conditions -- never for
    which operations carry it. Two compiled rules that agree on ``(predicate, value, conditions)``
    always get the same id, which is what lets ``merge_rules`` below union their operations into one
    on-disk row without minting a fresh id for the merge."""
    key = repr((predicate, _sortable(value), conditions))
    digest = hashlib.sha256(key.encode()).hexdigest()[:10]
    return f"r-{digest}"


def _sortable(value: Any) -> Any:
    """``list`` values are compared/hashed order-independently -- a rule's id must not depend on the
    order its resource ids happen to be listed in."""
    if isinstance(value, list):
        return tuple(sorted((repr(v) for v in value)))
    return value


def rule_id_for_rule(rule: PolicyRule) -> str:
    """The canonical, content-derived id for an already-compiled ``PolicyRule``, recomputed from
    its own ``(predicate, value, conditions)`` rather than trusting ``rule.id`` -- which is what
    lets gate.py attribute a live decision to the identical row the Settings Rules page lists, even
    for a rule built in memory rather than read back from disk."""
    return rule_id_for(rule.predicate, rule.value, rule.conditions)


def merge_rules(rules: list[PolicyRule]) -> list[PolicyRule]:
    """Union rules that share a ``(predicate, value, conditions)`` key into one rule spanning every
    operation any of them covered, preserving first-seen order. This changes nothing about *whether*
    a call matches -- ``policy.engine.evaluate`` already treats ``rule.operations`` as an unordered
    set membership test -- it only makes the on-disk config read as one sentence per intent ("allow
    read, update and format on the folder") instead of one row per operation key.
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
    """The full ``auto_accept:`` section value for a rule list."""
    return {"version": SCHEMA_VERSION, "rules": [rule_to_dict(rule) for rule in rules]}


def compile_rules_from_config(cfg: dict[str, Any]) -> list[PolicyRule]:
    """Compile the on-disk ``auto_accept:`` section (if present and well-formed) into
    ``PolicyRule``s. A missing or malformed section compiles to no rules, never an error; a
    malformed entry is dropped on its own.
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


__all__ = [
    "AUTO_ACCEPT_CONFIG_KEY",
    "CONVERTED_V1_MARKER",
    "SCHEMA_VERSION",
    "V1PolicyConfigError",
    "V1_SECTION_KEYS",
    "compile_rules_from_config",
    "drop_converted_v1_sections",
    "merge_rules",
    "reject_v1_sections",
    "rule_from_dict",
    "rule_id_for",
    "rule_id_for_rule",
    "rule_to_dict",
    "rules_to_config",
]
