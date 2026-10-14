"""One stored auto-accept rule per scope: the row model behind ``auto_accept:`` version 3 (ADR 0172).

A v2 section stores one ``PolicyRule`` per predicate, and one Settings scope compiles to up to four
predicates, so the same folder could appear as several rules. A version-3 section stores one
``ScopeRow`` per (scope, value). A row keeps the verbs and operation keys the person allowed, and
``compile_row`` turns it into the ``PolicyRule``s the engine evaluates (the engine does not change;
several compiled rules may share one row id, as ADR 0074 already allows). A rule the catalogue
cannot map to a scope stays a *custom* row: it carries its predicate and conditions, compiles to
exactly one rule, and keeps the id it was written with.

This module is pure. It reads and writes no files, touches no tokens and imports none of the daemon,
the auto-accept writers, the scoped-token store, the Settings controller or the web layer; it works
on rows and rules in memory. ``migrate_v2`` is the pure half of the upgrade (the token-aware half
lives in ``policy_upgrade``), and every function that changes rows returns a new list.
"""
from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import catalogue, describe, outdated, propose, registry, store
from .catalogue import PolicyExtraScope
from .engine import PolicyRule
from .propose import ProposableScope
from .registry import TOOL_REGISTRY, Verb

SCHEMA_VERSION = 3

_Conditions = tuple[tuple[str, Any], ...]
_Pairs = frozenset[tuple[str, str]]


@dataclass(frozen=True)
class ScopeRow:
    """One stored rule: a scope, its value, and the verbs and operation keys allowed on it.

    ``scope`` is a ``catalogue.scope_catalogue()`` id (a propose widening group or an
    ``EXTRA_SCOPES`` id); ``None`` marks a custom row, which carries its own ``predicate`` and
    ``conditions``. ``former_ids`` are the v2 rule ids merged into the row, sorted.
    """

    id: str
    scope: str | None
    value: Any
    verbs: frozenset[Verb]
    operations: frozenset[str]
    predicate: str = ""
    conditions: _Conditions = ()
    former_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class V2Migration:
    """The result of ``migrate_v2``: the rows plus what the token upgrade needs to re-point tokens."""

    rows: list[ScopeRow] = field(default_factory=list)
    id_map: dict[str, str] = field(default_factory=dict)
    members: dict[str, frozenset[str]] = field(default_factory=dict)
    member_pairs: dict[str, _Pairs] = field(default_factory=dict)
    personal_data_ids: frozenset[str] = frozenset()


def row_id_for(scope: str, value: Any) -> str:
    """The stable id of a scoped row. It hashes the scope and the value (order-independent), never
    the verbs, so adding or removing verbs keeps the id."""
    key = repr(("scope", scope, store._sortable(value)))
    return "r-" + hashlib.sha256(key.encode()).hexdigest()[:10]


def group_entries(scope: str | None) -> tuple[ProposableScope, ...] | None:
    """The ``PROPOSABLE_SCOPES`` entries of a widening group, or ``None`` for any other id."""
    return propose.SCOPES_BY_GROUP.get(scope) if scope is not None else None


def extra_scope(scope: str | None) -> PolicyExtraScope | None:
    """The ``EXTRA_SCOPES`` entry with this id, or ``None``."""
    return catalogue.EXTRA_SCOPES.get(scope) if scope is not None else None


def scope_verbs(scope: str | None) -> tuple[Verb, ...]:
    """The verbs a scope offers, in ``verb_sort_key`` order; empty for an unknown scope."""
    entries = group_entries(scope)
    extra = extra_scope(scope)
    if entries is not None:
        verbs: set[Verb] = {v for entry in entries for v in entry.verbs}
    elif extra is not None:
        verbs = set(extra.verbs)
    else:
        verbs = set()
    return tuple(sorted(verbs, key=propose.verb_sort_key))


def _operations_for_verbs(scope: str | None, verbs: Iterable[Verb]) -> frozenset[str]:
    wanted = frozenset(verbs)
    entries = group_entries(scope)
    extra = extra_scope(scope)
    operations: set[str] = set()
    if entries is not None:
        for entry in entries:
            for verb in wanted & entry.verbs:
                operations |= propose.operations_for(entry, verb)
    elif extra is not None:
        for verb in wanted & frozenset(extra.verbs):
            operations |= catalogue.extra_operations_for(extra, verb)
    return frozenset(operations)


def implied_operations(scope: str | None, verbs: Iterable[Verb]) -> frozenset[str]:
    """Every operation key the scope's catalogue entries derive for ``verbs``."""
    return _operations_for_verbs(scope, verbs)


def _custom_verbs(operations: Iterable[str]) -> frozenset[Verb]:
    return frozenset(v for op in operations for v in registry.operation_verbs(op))


def compile_row(row: ScopeRow) -> list[PolicyRule]:
    """The ``PolicyRule``s the engine evaluates for a row: one for a custom row or an extra scope,
    one per catalogue entry with operations left for a propose group."""
    if row.scope is None:
        return [PolicyRule(
            id=row.id, predicate=row.predicate, value=row.value,
            operations=row.operations, conditions=row.conditions,
        )]
    entries = group_entries(row.scope)
    extra = extra_scope(row.scope)
    if entries is not None:
        rules: list[PolicyRule] = []
        for entry in entries:
            operations: frozenset[str] = frozenset()
            for verb in row.verbs & entry.verbs:
                operations |= row.operations & propose.operations_for(entry, verb)
            if operations:
                rules.append(PolicyRule(
                    id=row.id, predicate=entry.predicate, value=row.value, operations=operations,
                    conditions=(entry.condition,) if entry.condition else (),
                ))
        return rules
    if extra is not None:
        operations = row.operations & _operations_for_verbs(row.scope, row.verbs)
        if operations:
            return [PolicyRule(id=row.id, predicate=extra.predicate, value=row.value, operations=operations)]
    return []


def compile_rows(rows: Iterable[ScopeRow]) -> list[PolicyRule]:
    """``compile_row`` of every row, concatenated in row order."""
    return [rule for row in rows for rule in compile_row(row)]


def row_to_dict(row: ScopeRow) -> dict[str, Any]:
    """The on-disk shape of a row. Verbs, operations and ids are sorted for a stable YAML."""
    data: dict[str, Any] = {"id": row.id}
    if row.scope is None:
        data["predicate"] = row.predicate
    else:
        data["scope"] = row.scope
    data["value"] = row.value
    data["verbs"] = sorted(v.value for v in row.verbs)
    data["operations"] = sorted(row.operations)
    if row.scope is None:
        data["conditions"] = [[name, value] for name, value in row.conditions]
    if row.former_ids:
        data["former_ids"] = sorted(row.former_ids)
    return data


def _parse_verbs(raw: Any) -> frozenset[Verb]:
    verbs: set[Verb] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        try:
            verbs.add(Verb(item))
        except ValueError:
            alias = registry.LEGACY_VERB_ALIASES.get(item)
            if alias is not None:
                verbs.add(alias)
    return frozenset(verbs)


def _parse_conditions(raw: Any) -> _Conditions | None:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return None
    parsed: list[tuple[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2 or not isinstance(entry[0], str):
            return None
        parsed.append((entry[0], entry[1]))
    return tuple(parsed)


def row_from_dict(data: Any) -> ScopeRow | None:
    """The read side of ``row_to_dict``. Fails closed (returns ``None``) for anything malformed: a
    missing id, empty operations, neither scope nor predicate, a scope outside the catalogue, or a
    scoped row left with no verbs. A custom row may have no verbs: its verbs are derived from its
    operations and are display-only."""
    if not isinstance(data, dict):
        return None
    row_id, operations = data.get("id"), data.get("operations")
    if not isinstance(row_id, str) or not row_id:
        return None
    if not isinstance(operations, list) or not operations or not all(isinstance(o, str) for o in operations):
        return None
    scope, predicate = data.get("scope"), data.get("predicate")
    former_raw = data.get("former_ids")
    former_ids = tuple(sorted(i for i in former_raw if isinstance(i, str))) if isinstance(former_raw, list) else ()
    if scope is not None:
        if not isinstance(scope, str) or (group_entries(scope) is None and extra_scope(scope) is None):
            return None
        verbs = _parse_verbs(data.get("verbs"))
        if not verbs:
            return None
        return ScopeRow(
            id=row_id, scope=scope, value=data.get("value"), verbs=verbs,
            operations=frozenset(operations), former_ids=former_ids,
        )
    if not isinstance(predicate, str) or not predicate:
        return None
    conditions = _parse_conditions(data.get("conditions"))
    if conditions is None:
        return None
    return ScopeRow(
        id=row_id, scope=None, value=data.get("value"), verbs=_custom_verbs(operations),
        operations=frozenset(operations), predicate=predicate, conditions=conditions, former_ids=former_ids,
    )


def rows_from_section(section: Any) -> list[ScopeRow]:
    """Every well-formed row of a version-3 ``auto_accept:`` section; a malformed entry is dropped."""
    raw_rows = section.get("rules") if isinstance(section, dict) else None
    if not isinstance(raw_rows, list):
        return []
    rows = (row_from_dict(raw) for raw in raw_rows)
    return [row for row in rows if row is not None]


def rows_to_config(rows: Iterable[ScopeRow]) -> dict[str, Any]:
    """The full ``auto_accept:`` section value for a row list."""
    return {"version": SCHEMA_VERSION, "rules": [row_to_dict(row) for row in rows]}


def rows_from_config(cfg: Any) -> list[ScopeRow]:
    """The rows of ``cfg``'s ``auto_accept:`` section, read-only. A v2 section (or one with a
    ``rules`` list and no version) is converted in memory; anything else reads as no rows."""
    section = cfg.get(store.AUTO_ACCEPT_CONFIG_KEY) if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        return []
    version = section.get("version")
    if version == SCHEMA_VERSION:
        return rows_from_section(section)
    if version == 2 or (version is None and isinstance(section.get("rules"), list)):
        return migrate_v2(section.get("rules")).rows
    return []


def scope_of_rule(rule: PolicyRule) -> tuple[str, Any] | None:
    """The ``(scope, value)`` a v2 rule belongs to, or ``None`` when it must stay a custom row."""
    candidates: set[str] = set()
    if len(rule.conditions) == 1:
        matching = [
            entry for entry in propose.PROPOSABLE_SCOPES
            if entry.predicate == rule.predicate and entry.condition == rule.conditions[0]
        ]
        if len(matching) == 1:
            candidates.add(matching[0].widening_group)
    elif not rule.conditions:
        connectors = {propose.connector_of_operation(op) for op in rule.operations}
        candidates.update(
            entry.widening_group for entry in propose.PROPOSABLE_SCOPES
            if entry.predicate == rule.predicate and entry.condition is None and entry.connector in connectors
        )
        candidates.update(
            scope_id for scope_id, extra in catalogue.EXTRA_SCOPES.items()
            if extra.predicate == rule.predicate and extra.connector in connectors
        )
    if len(candidates) != 1:
        return None
    (scope,) = candidates
    if not rule.operations <= implied_operations(scope, scope_verbs(scope)):
        return None
    return scope, rule.value


def _signature(rules: Iterable[PolicyRule]) -> dict[tuple[str, _Conditions], frozenset[str]]:
    """What a rule list decides, per (predicate, conditions): the union of its operations."""
    merged: dict[tuple[str, _Conditions], frozenset[str]] = {}
    for rule in rules:
        key = (rule.predicate, rule.conditions)
        merged[key] = merged.get(key, frozenset()) | rule.operations
    return merged


def _row_for_rules(scope: str, value: Any, rules: list[PolicyRule]) -> ScopeRow:
    """A candidate scoped row for rules that all map to ``(scope, value)``."""
    return ScopeRow(
        id=row_id_for(scope, value), scope=scope, value=value,
        verbs=frozenset(v for rule in rules for v in describe.rule_verbs(rule)),
        operations=frozenset(op for rule in rules for op in rule.operations),
    )


def _is_lossless(row: ScopeRow, rules: list[PolicyRule]) -> bool:
    return _signature(compile_row(row)) == _signature(rules)


def _custom_row(rule: PolicyRule, row_id: str) -> ScopeRow:
    return ScopeRow(
        id=row_id, scope=None, value=rule.value, verbs=_custom_verbs(rule.operations),
        operations=rule.operations, predicate=rule.predicate, conditions=rule.conditions,
    )


def _pairs(rules: Iterable[PolicyRule]) -> _Pairs:
    return frozenset((rule.predicate, op) for rule in rules for op in rule.operations)


def add_rules(rows: list[ScopeRow], rules: list[PolicyRule]) -> tuple[list[ScopeRow], list[str]]:
    """Merge compiled rules into rows, the one merge every writer uses.

    A rule that maps to a scope extends the row for that (scope, value), or starts one; any other
    rule becomes (or extends) a custom row. Returns the new rows and the ids of the rows touched,
    deduplicated, in order."""
    new_rows = list(rows)
    touched: list[str] = []
    for rule in rules:
        mapped = scope_of_rule(rule)
        # A rule whose own scope cannot reproduce it (an operation its predicate's entry does not
        # derive) would be silently narrowed by compile_row, so it stays custom.
        if mapped is not None and not _is_lossless(_row_for_rules(*mapped, [rule]), [rule]):
            mapped = None
        if mapped is not None:
            scope, value = mapped
            key = store._sortable(value)
            index = next(
                (i for i, row in enumerate(new_rows) if row.scope == scope and store._sortable(row.value) == key),
                None,
            )
            verbs = frozenset(describe.rule_verbs(rule))
            if index is None:
                new_rows.append(ScopeRow(
                    id=row_id_for(scope, value), scope=scope, value=value, verbs=verbs, operations=rule.operations,
                ))
                touched.append(new_rows[-1].id)
            else:
                row = new_rows[index]
                new_rows[index] = dataclasses.replace(
                    row, verbs=row.verbs | verbs, operations=row.operations | rule.operations,
                )
                touched.append(row.id)
            continue
        row_id = store.rule_id_for(rule.predicate, rule.value, rule.conditions)
        index = next((i for i, row in enumerate(new_rows) if row.scope is None and row.id == row_id), None)
        if index is None:
            new_rows.append(_custom_row(rule, row_id))
        else:
            operations = new_rows[index].operations | rule.operations
            new_rows[index] = dataclasses.replace(
                new_rows[index], operations=operations, verbs=_custom_verbs(operations),
            )
        touched.append(row_id)
    return new_rows, list(dict.fromkeys(touched))


def remove_row(rows: list[ScopeRow], row_id: str) -> list[ScopeRow]:
    """``rows`` without the row with this id."""
    return [row for row in rows if row.id != row_id]


def missing_operations(row: ScopeRow) -> frozenset[str]:
    """What the row's verbs imply today but its operations lack (ADR 0166)."""
    if row.scope is None:
        return outdated.missing_operations(compile_row(row)[0])
    return implied_operations(row.scope, row.verbs) - row.operations


def missing_tools(row: ScopeRow) -> tuple[str, ...]:
    """The tools of ``missing_operations`` whose verb the row has, sorted."""
    missing = missing_operations(row)
    return tuple(sorted(
        entry.tool for entry in TOOL_REGISTRY.values()
        if entry.operation in missing and entry.verb is not None and entry.verb in row.verbs
    ))


def fill_missing(rows: list[ScopeRow], row_id: str) -> list[ScopeRow]:
    """``rows`` with one row's missing operations added (the "Update rule" button)."""
    return [
        dataclasses.replace(row, operations=row.operations | missing_operations(row)) if row.id == row_id else row
        for row in rows
    ]


def set_verbs(rows: list[ScopeRow], row_id: str, verbs: Iterable[Verb]) -> list[ScopeRow] | None:
    """Change one scoped row's verbs, or return ``None`` to refuse (unknown or custom row, no
    verbs, or a verb outside the scope).

    A verb that stays keeps exactly the operations it had; a new verb gets all of its tools; a
    removed verb's operations go unless a kept verb also implies them."""
    wanted = frozenset(verbs)
    row = next((r for r in rows if r.id == row_id), None)
    if row is None or row.scope is None or not wanted or not wanted <= frozenset(scope_verbs(row.scope)):
        return None
    kept, added = row.verbs & wanted, wanted - row.verbs
    operations = (row.operations & implied_operations(row.scope, kept)) | implied_operations(row.scope, added)
    updated = dataclasses.replace(row, verbs=wanted, operations=operations)
    return [updated if r.id == row_id else r for r in rows]


def pairs_of(rows: Iterable[ScopeRow], row_id: str) -> _Pairs:
    """The ``(predicate, operation)`` pairs ``compile_row`` gives for a row today; empty if unknown."""
    row = next((r for r in rows if r.id == row_id), None)
    return _pairs(compile_row(row)) if row is not None else frozenset()


def migrate_v2(raw_rules: Any) -> V2Migration:
    """Convert a v2 rule list into rows without changing what any unscoped call can auto-accept.

    Rules that map to the same (scope, value) merge into one row, provided the row compiles back to
    exactly the operations and conditions those rules had, per predicate; otherwise each stays its
    own custom row under its old id, as does every rule no scope claims. A raw entry's
    ``allow_personal_data`` is read here, before parsing, because rows do not carry it."""
    rules: list[PolicyRule] = []
    personal_data_ids: set[str] = set()
    for raw in raw_rules if isinstance(raw_rules, list) else []:
        rule = store.rule_from_dict(raw)
        if rule is None:
            continue
        rules.append(rule)
        if raw.get("allow_personal_data") is True:
            personal_data_ids.add(rule.id)

    groups: dict[tuple[str, Any], list[PolicyRule]] = {}
    placement: list[tuple[str, Any] | PolicyRule] = []
    for rule in rules:
        mapped = scope_of_rule(rule)
        if mapped is None:
            placement.append(rule)
            continue
        key = (mapped[0], store._sortable(mapped[1]))
        if key not in groups:
            groups[key] = []
            placement.append(key)
        groups[key].append(rule)

    rows: list[ScopeRow] = []
    id_map: dict[str, str] = {}
    members: dict[str, frozenset[str]] = {}
    for item in placement:
        if isinstance(item, PolicyRule):
            rows.append(_custom_row(item, item.id))
            continue
        group = groups[item]
        candidate = _row_for_rules(item[0], group[0].value, group)
        if not _is_lossless(candidate, group):
            rows.extend(_custom_row(rule, rule.id) for rule in group)
            continue
        old_ids = frozenset(rule.id for rule in group)
        rows.append(dataclasses.replace(candidate, former_ids=tuple(sorted(old_ids))))
        members[candidate.id] = old_ids
        id_map.update({old: candidate.id for old in old_ids})

    member_pairs: dict[str, _Pairs] = {}
    for rule in rules:
        member_pairs[rule.id] = member_pairs.get(rule.id, frozenset()) | _pairs([rule])
    return V2Migration(
        rows=rows, id_map=id_map, members=members, member_pairs=member_pairs,
        personal_data_ids=frozenset(personal_data_ids),
    )


__all__ = [
    "SCHEMA_VERSION",
    "ScopeRow",
    "V2Migration",
    "add_rules",
    "compile_row",
    "compile_rows",
    "extra_scope",
    "fill_missing",
    "group_entries",
    "implied_operations",
    "migrate_v2",
    "missing_operations",
    "missing_tools",
    "pairs_of",
    "remove_row",
    "row_from_dict",
    "row_id_for",
    "row_to_dict",
    "rows_from_config",
    "rows_from_section",
    "rows_to_config",
    "scope_of_rule",
    "scope_verbs",
    "set_verbs",
]
