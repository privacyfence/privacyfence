"""The scope catalogue a surface offers for "add a rule".

The bridge's ``privacyfence_propose_policy_change`` and the Auto-accept Settings page share this one
definition rather than each deriving their own, because tables that must agree by hand are how the
old rule model drifted.

``settings_controller.py`` re-exports every name here under its old, private spelling
(``_PolicyExtraScope``, ``_POLICY_EXTRA_SCOPES``, ``_policy_scope_catalogue``, ...) so its own
callers and tests are unaffected; this module is the one definition both it and ``gate.py``'s
bridge writer call.

``EXTRA_SCOPES`` covers the three operation groups ``policy.propose.PROPOSABLE_SCOPES`` deliberately
does not offer to the reactive "Always allow" popup (see that module's own docstring): Apps Script's
tools, which had no predicate at all, and Gmail's two filter tools and Slack's group-chat tool,
which have no resource identity to scope to. The Settings page and the bridge tool configure them
the same deliberate way -- pick a connector and a verb, see "what this unblocks" before committing
-- rather than reactively, off one gated call's own popup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import describe, propose, registry, store
from .engine import PolicyRule
from .registry import Verb


@dataclass(frozen=True)
class PolicyExtraScope:
    """One of the three operation groups with no ``policy.propose.PROPOSABLE_SCOPES`` entry of
    its own. Keys are this catalogue's own ids, distinct from any ``policy.propose.SCOPES_BY_GROUP``
    key (that module already uses ``"gmail.anything"`` for its own, unrelated unconditional-drafting
    entry) -- see ``policy.scopes.NEW_SCOPE_SELECTORS``' own comment on why the predicates themselves
    (``gmail.anything``/``slack.anything``) are new names too, not a reuse of ``always_allow``.
    """

    predicate: str
    connector: str
    verbs: tuple[Verb, ...]
    label: str
    needs_value: bool
    value_hint: str


EXTRA_SCOPES: dict[str, PolicyExtraScope] = {
    "apps_script.project": PolicyExtraScope(
        predicate="apps_script.project", connector="apps_script",
        verbs=(Verb.READ, Verb.UPDATE),
        label="Apps Script — project", needs_value=True, value_hint="1A2b3C…script-id",
    ),
    "gmail.configure": PolicyExtraScope(
        predicate="gmail.anything", connector="gmail",
        verbs=(Verb.CONFIGURE,),
        label="Gmail — anything (unconditional)", needs_value=False, value_hint="",
    ),
    "slack.share_anything": PolicyExtraScope(
        predicate="slack.anything", connector="slack",
        verbs=(Verb.SHARE,),
        label="Slack — anything (unconditional)", needs_value=False, value_hint="",
    ),
}

# Example values for an "add a rule" form's value field, keyed by predicate, scoped down to the
# predicates ``policy.propose.PROPOSABLE_SCOPES`` actually offers. Condition-only predicates
# (age_threshold_days, time_window_days, ...) never appear: this catalogue writes scopes, not
# conditions -- see ``rules_for_catalogue_entry``'s own docstring.
VALUE_HINTS: dict[str, str] = {
    "trusted_sender_domain": "domain1.com, domain2.com",
    "approved_channel": "C0123456789, C9876543210",
    "approved_channel_all_results": "C0123456789, C9876543210",
    "approved_recipient": "U0123456789",
    "personal_calendar": "primary",
    "approved_object_types": "Account, Contact, Opportunity",
    "approved_report_ids": "00O000000000001",
    "approved_folder": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "approved_sandbox_folder": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "parent_folder_allowlist": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "move_within_approved_folders": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "label_name_allowlist": "Newsletters, Receipts",
    "approved_project_keys": "MYPROJ, OTHERPROJ",
    "approved_space_keys": "TEAM, DOCS",
    "approved_chats": "123456789, -100987654321",
    "approved_chats_all_results": "123456789, -100987654321",
    "approved_task_list": "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow",
}


def extra_operations_for(connector: str, verb: Verb) -> frozenset[str]:
    """Every operation key one of ``EXTRA_SCOPES``' verbs governs, for ``connector`` -- derived from
    the tool registry the same way ``policy.propose.operations_for`` derives a
    ``PROPOSABLE_SCOPES`` entry's own operations, since none of these three predicates is declared
    there (see this module's own docstring)."""
    return frozenset(
        entry.operation
        for entry in registry.TOOL_REGISTRY.values()
        if entry.operation is not None and entry.verb == verb
        and entry.operation.split(".", 1)[0] == connector
    )


def scope_catalogue() -> list[dict[str, Any]]:
    """Every scope an "add a rule" picker offers: one entry per ``policy.propose.SCOPES_BY_GROUP``
    widening group, plus ``EXTRA_SCOPES`` above. What ``rules_for_catalogue_entry`` below validates
    an incoming ``group`` id against, and what ``privacyfence_list_policy`` reports back as
    ``scope_groups`` so a model knows which verbs a given group can actually govern before calling
    ``privacyfence_propose_policy_change``."""
    entries: list[dict[str, Any]] = []
    for group_id, group_scopes in propose.SCOPES_BY_GROUP.items():
        first = group_scopes[0]
        verbs = sorted({v for scope in group_scopes for v in scope.verbs}, key=propose.verb_sort_key)
        scope_type = first.scope_type
        noun = first.hint or group_id
        noun = noun[:1].upper() + noun[1:] if noun else noun
        label = (
            f"{describe.connector_label(first.connector)} — {describe.scope_type_label(scope_type)}"
            if group_id == scope_type
            else f"{describe.connector_label(first.connector)} — {noun}"
        )
        entries.append({
            "id": group_id,
            "label": label,
            "connector": first.connector,
            "needs_value": propose.scope_needs_value(first),
            "value_hint": VALUE_HINTS.get(first.predicate, ""),
            "verbs": [v.value for v in verbs],
        })
    for group_id, extra in EXTRA_SCOPES.items():
        entries.append({
            "id": group_id,
            "label": extra.label,
            "connector": extra.connector,
            "needs_value": extra.needs_value,
            "value_hint": extra.value_hint,
            "verbs": [v.value for v in extra.verbs],
        })
    entries.sort(key=lambda e: (e["connector"], e["label"]))
    return entries


def parse_verbs(verbs: Any) -> list[Verb]:
    """Turn a plain list of verb strings (a form's checked checkboxes, or a bridge call's own
    ``verbs`` array) back into ``policy.registry.Verb`` members, dropping -- rather than erroring on
    -- anything that isn't a real verb name, the same fail-closed-and-quiet posture
    ``rules_for_catalogue_entry`` itself takes."""
    if not isinstance(verbs, list):
        return []
    parsed: list[Verb] = []
    for raw in verbs:
        try:
            parsed.append(Verb(raw))
        except ValueError:
            continue
    return parsed


def rules_for_catalogue_entry(group: str, value: list[str] | None, verbs: list[Verb]) -> list[PolicyRule]:
    """The ``PolicyRule``s an "add a rule" submission (``group``/``value``/``verbs``) compiles to,
    or ``[]`` for an unrecognized group, a group none of the requested verbs govern, or (for the one
    valued extra, ``apps_script.project``) a value-needing scope submitted with none.

    This is also the write-time validation that rejects a verb the scope type cannot govern: a
    caller asking for a verb ``group``
    doesn't list -- or one that derives no operation key for this group at all, which is what
    ``ProposableScope.excludes`` means -- gets no rules back, the same fail-closed-and-quiet
    response an unknown group already gets. ``gate.propose_policy_change`` turns that empty result
    into a clear ``ValueError`` before any popup is shown, rather than silently doing nothing the way
    a Settings form (which can't offer a checkbox for a verb the group doesn't list in the first
    place) safely can.
    """
    extra = EXTRA_SCOPES.get(group)
    if extra is not None:
        allowed_verbs = [v for v in verbs if v in extra.verbs]
        if not allowed_verbs or (extra.needs_value and not value):
            return []
        operations: frozenset[str] = frozenset().union(
            *(extra_operations_for(extra.connector, v) for v in allowed_verbs)
        )
        if not operations:
            return []
        return store.merge_rules([
            PolicyRule(id=extra.predicate, predicate=extra.predicate, value=value, operations=operations),
        ])
    if group not in propose.SCOPES_BY_GROUP:
        return []
    return propose.rules_for_scope_group(group, value, verbs)


__all__ = [
    "EXTRA_SCOPES",
    "VALUE_HINTS",
    "PolicyExtraScope",
    "extra_operations_for",
    "parse_verbs",
    "rules_for_catalogue_entry",
    "scope_catalogue",
]
