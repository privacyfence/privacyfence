"""The scope catalogue a surface offers for "add a rule".

The bridge's ``privacyfence_propose_policy_change`` and the Auto-accept Settings page share this one
definition rather than each deriving their own, because tables that must agree by hand are how the
old rule model drifted.

``settings_controller.py`` re-exports every name here under its old, private spelling
(``_PolicyExtraScope``, ``_POLICY_EXTRA_SCOPES``, ``_policy_scope_catalogue``, ...) so its own
callers and tests are unaffected; this module is the one definition both it and ``gate.py``'s
bridge writer call.

``EXTRA_SCOPES`` covers the operation groups ``policy.propose.PROPOSABLE_SCOPES`` deliberately
does not offer to the reactive "Always allow" popup (see that module's own docstring): Apps Script's
tools, which had no predicate at all, Grist's seven operation keys (scoped by document), Peek's page tools, Miro's gated tools (scoped by board), and Gmail's two filter tools and
Slack's group-chat tool, which have no resource identity to scope to, and Slack's two read scopes for
mentions of the signed-in user and one-to-one DMs, and the scope for conversations the signed-in user
is a member of. The Settings page and the bridge tool configure them
the same deliberate way -- pick a connector and a verb, see "what this unblocks" before committing
-- rather than reactively, off one gated call's own popup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import describe, propose, registry, store, value_format
from .engine import PolicyRule
from .registry import Verb


@dataclass(frozen=True)
class PolicyExtraScope:
    """One of the operation groups with no ``policy.propose.PROPOSABLE_SCOPES`` entry of
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
    # Operation keys the registry derivation over-reaches for this scope, removed by
    # ``extra_operations_for``.
    excludes: frozenset[str] = frozenset()
    # Per verb, the predicate a rule for that verb uses when it is not ``predicate``. Read and
    # search share the ``slack.read_messages`` operation key and the engine matches on operation
    # keys, so the predicate is the only place a rule can say which of the two it allows.
    verb_predicates: tuple[tuple[Verb, str], ...] = ()
    # A value may be given but is not required (``needs_value`` stays False): Settings and the
    # bridge tool offer the field, a blank one means no value.
    value_optional: bool = False


def extra_predicate_for(extra: PolicyExtraScope, verb: Verb) -> str:
    """The predicate a rule for ``verb`` uses on ``extra``: its ``verb_predicates`` entry, or
    ``extra.predicate``."""
    return dict(extra.verb_predicates).get(verb, extra.predicate)


def extra_predicates(extra: PolicyExtraScope) -> frozenset[str]:
    """``extra.predicate`` and every predicate in ``extra.verb_predicates``."""
    return frozenset({extra.predicate, *(p for _, p in extra.verb_predicates)})


EXTRA_SCOPES: dict[str, PolicyExtraScope] = {
    "apps_script.project": PolicyExtraScope(
        predicate="apps_script.project", connector="apps_script",
        verbs=(Verb.READ, Verb.UPDATE),
        label="Apps Script — project", needs_value=True, value_hint="1A2b3C…script-id",
    ),
    "grist.document": PolicyExtraScope(
        predicate="grist.document", connector="grist",
        verbs=(Verb.READ, Verb.ADD, Verb.UPDATE, Verb.RESTRUCTURE),
        label="Grist — document", needs_value=True, value_hint="8CAN8gKdxY7z…document-id",
    ),
    "peek.page": PolicyExtraScope(
        predicate="peek.page", connector="peek",
        verbs=(Verb.READ, Verb.ADD),
        label="Peek — page", needs_value=True, value_hint="team-dashboard…page-slug",
    ),
    "miro.board": PolicyExtraScope(
        predicate="miro.board", connector="miro",
        verbs=(Verb.READ, Verb.ADD, Verb.UPDATE),
        label="Miro — board", needs_value=True, value_hint="uXjVOD6LSME=…board-id",
    ),
    "gmail.filters": PolicyExtraScope(
        predicate="gmail.anything", connector="gmail",
        verbs=(Verb.RESTRUCTURE,),
        label="Gmail — filters (unconditional)", needs_value=False, value_hint="",
    ),
    "slack.group_chats": PolicyExtraScope(
        predicate="slack.anything", connector="slack",
        verbs=(Verb.SEND,),
        label="Slack — group chats (unconditional)", needs_value=False, value_hint="",
        excludes=frozenset({"slack.send_message"}),
    ),
    "slack.mentions_of_me": PolicyExtraScope(
        predicate="slack.mentions_of_me", connector="slack",
        verbs=(Verb.READ, Verb.SEARCH),
        label="Slack — mentions of me", needs_value=False, value_hint="",
        verb_predicates=((Verb.SEARCH, "slack.mentions_of_me_all_results"),),
    ),
    "slack.direct_messages_to_me": PolicyExtraScope(
        predicate="slack.direct_messages_to_me", connector="slack",
        verbs=(Verb.READ,),
        label="Slack — direct messages to me", needs_value=False, value_hint="",
    ),
    "slack.my_conversations": PolicyExtraScope(
        predicate="slack.my_conversations", connector="slack",
        verbs=(Verb.READ, Verb.SEARCH),
        label="Slack — conversations I'm a member of", needs_value=False,
        value_hint="C0123456789, G0123456789 (optional: never covered)",
        verb_predicates=((Verb.SEARCH, "slack.my_conversations_all_results"),),
        value_optional=True,
    ),
    "gmail.query": PolicyExtraScope(
        predicate="gmail.query", connector="gmail",
        verbs=(Verb.READ, Verb.DOWNLOAD),
        label="Gmail — query", needs_value=True,
        value_hint='in:inbox -category:promotions -label:"Proposed SPAM"',
    ),
}

# Groups whose value is one expression, not a comma-separated list: commas belong to the value.
SINGLE_VALUE_GROUPS: frozenset[str] = frozenset({"gmail.query"})

# Ids retired when the nine verbs landed. `rules_for_catalogue_entry` resolves one before anything
# else, so older bridge calls keep working.
GROUP_ALIASES: dict[str, str] = {
    "gmail.configure": "gmail.filters",
    "slack.share_anything": "slack.group_chats",
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
    "approved_project_keys_all_results": "MYPROJ, OTHERPROJ",
    "approved_space_keys": "TEAM, DOCS",
    "approved_chats": "123456789, -100987654321",
    "approved_chats_all_results": "123456789, -100987654321",
    "approved_task_list": "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow",
}


def extra_operations_for(extra: PolicyExtraScope, verb: Verb) -> frozenset[str]:
    """Every operation key one of ``EXTRA_SCOPES``' verbs governs, for ``extra`` -- derived from
    the tool registry the same way ``policy.propose.operations_for`` derives a
    ``PROPOSABLE_SCOPES`` entry's own operations, since none of these predicates is declared
    there (see this module's own docstring), minus ``extra.excludes``."""
    return frozenset(
        entry.operation
        for entry in registry.TOOL_REGISTRY.values()
        if entry.operation is not None and entry.verb == verb
        and entry.operation.split(".", 1)[0] == extra.connector
    ) - extra.excludes


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
            "value_optional": False,
        })
    for group_id, extra in EXTRA_SCOPES.items():
        entries.append({
            "id": group_id,
            "label": extra.label,
            "connector": extra.connector,
            "needs_value": extra.needs_value,
            "value_hint": extra.value_hint,
            "verbs": [v.value for v in extra.verbs],
            "value_optional": extra.value_optional,
        })
    entries.sort(key=lambda e: (e["connector"], e["label"]))
    return entries


def parse_verbs(verbs: Any) -> list[Verb]:
    """Turn a plain list of verb strings (a form's checked checkboxes, or a bridge call's own
    ``verbs`` array) back into ``policy.registry.Verb`` members. A retired name
    (``registry.LEGACY_VERB_ALIASES``) is mapped to its current verb, duplicates are dropped keeping
    first-seen order, and anything that isn't a real verb name is dropped -- rather than erroring on
    -- the same fail-closed-and-quiet posture ``rules_for_catalogue_entry`` itself takes."""
    if not isinstance(verbs, list):
        return []
    parsed: list[Verb] = []
    for raw in verbs:
        resolved: Verb | None
        try:
            resolved = Verb(raw)
        except ValueError:
            resolved = registry.LEGACY_VERB_ALIASES.get(raw) if isinstance(raw, str) else None
        if resolved is not None and resolved not in parsed:
            parsed.append(resolved)
    return parsed


def rules_for_catalogue_entry(
    group: str, value: Any, verbs: list[Verb],
) -> list[PolicyRule]:
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

    ``group`` may be a retired id (``GROUP_ALIASES``), which is resolved first.
    """
    group = GROUP_ALIASES.get(group, group)
    return _compile_catalogue_rules(group, value, verbs)


def _compile_catalogue_rules(group: str, value: Any, verbs: list[Verb]) -> list[PolicyRule]:
    extra = EXTRA_SCOPES.get(group)
    if extra is None and group not in propose.SCOPES_BY_GROUP:
        return []
    predicates = (
        set(extra_predicates(extra)) if extra is not None
        else {scope.predicate for scope in propose.SCOPES_BY_GROUP[group]}
    )
    value = value_format.normalise_rule_value(predicates, value)
    if extra is not None:
        allowed_verbs = [v for v in verbs if v in extra.verbs]
        if not allowed_verbs or (extra.needs_value and not value):
            return []
        by_predicate: dict[str, list[Verb]] = {}
        for verb in extra.verbs:
            if verb in allowed_verbs:
                by_predicate.setdefault(extra_predicate_for(extra, verb), []).append(verb)
        built: list[PolicyRule] = []
        for predicate, predicate_verbs in by_predicate.items():
            operations: frozenset[str] = frozenset().union(
                *(extra_operations_for(extra, v) for v in predicate_verbs)
            )
            if operations:
                built.append(PolicyRule(id=predicate, predicate=predicate, value=value, operations=operations))
        return store.merge_rules(built) if built else []
    return propose.rules_for_scope_group(group, value, verbs)


__all__ = [
    "EXTRA_SCOPES",
    "GROUP_ALIASES",
    "VALUE_HINTS",
    "SINGLE_VALUE_GROUPS",
    "PolicyExtraScope",
    "extra_operations_for",
    "extra_predicate_for",
    "extra_predicates",
    "parse_verbs",
    "rules_for_catalogue_entry",
    "scope_catalogue",
]
