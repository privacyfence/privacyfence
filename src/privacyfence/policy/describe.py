"""Rendering a v2 rule into words -- P5 of the policy v2 redesign.

The redesign proposal's §09 gives ``policy/describe.py`` one job: rule -> sentence, rule -> covered
tool list, change -> confirmation text. It replaces ``auto_accept.describe_rule``,
``describe_rule_short`` and ``describe_rule_change``, which are three hand-written English tables
keyed by v1 rule name -- and are why the popup and Settings can describe the same intent
differently (F2). ``_RULE_DESCRIPTIONS``' templates are read-direction-only prose ("Jira issue reads
in project(s): ..."), so ``gate.py`` already has to avoid them on the write gate and fall back to
``describe_rule_change``'s bare ``operation_key`` spelling; neither can say how *wide* the rule it
is describing actually is.

Everything here is derived instead: the connector and scope type come from P2's selector registry,
the verbs from P1's registry (via ``policy.propose``'s catalogue, so a rule is only credited with
the verbs its own predicate governs), and the tool list from the registry's own rows. A rule is
rendered the same way wherever it is shown, and "what this actually unblocks" is a real list rather
than something the reader has to infer from an operation key.

``covered_tools`` is the answer to F2 in particular: it is what lets a surface show, before the
user agrees to anything, that one intent reaches one tool and another reaches thirteen.

Nothing outside ``tests/`` consumes this module yet -- P6's single Auto-accept page and the popup's
own confirmation dialog are the surfaces that will.
"""
from __future__ import annotations

from typing import Iterable

from . import propose, registry, scopes
from .engine import PolicyRule
from .propose import RuleProposal, Widening
from .registry import TOOL_REGISTRY, Verb


def connector_label(connector: str) -> str:
    """``apps_script`` -> ``Apps Script``. Derived rather than tabulated: every connector name in
    this repo is a lowercase, underscore-separated spelling of its own display name."""
    return connector.replace("_", " ").title()


def scope_type_label(scope_type: str) -> str:
    """``drive.folder`` -> ``folder``, ``gmail.sender_domain`` -> ``sender domain``. The scope type
    names were chosen to read this way (redesign proposal §04), so the noun a sentence needs is the
    name itself with its connector prefix dropped."""
    _connector, _, rest = scope_type.partition(".")
    return rest.replace("_", " ")


def _scope_type_of(rule: PolicyRule) -> str:
    """The v2 scope type a compiled rule's predicate lands under.

    ``label_name_allowlist`` genuinely serves two scope types (``gmail.label`` and
    ``contacts.label``) depending on which connector the rule's operations belong to, which is why
    ``ScopeSelector.scope_type`` is a tuple for it -- resolve that against the rule's own
    operations rather than picking one arbitrarily. An unrecognised predicate has no scope type at
    all; callers render it as the predicate name, never as something that looks configured.
    """
    selector = scopes.SCOPE_SELECTORS.get(rule.predicate) or scopes.NEW_SCOPE_SELECTORS.get(rule.predicate)
    if selector is None:
        return ""
    if not isinstance(selector.scope_type, tuple):
        return selector.scope_type
    connectors = {propose.connector_of_operation(operation) for operation in rule.operations}
    for scope_type in selector.scope_type:
        if scope_type.partition(".")[0] in connectors:
            return scope_type
    return selector.scope_type[0]


def _catalogue_verbs(predicate: str) -> frozenset[Verb]:
    """Every verb ``predicate`` can govern, per ``propose.PROPOSABLE_SCOPES``. Empty for a predicate
    no surface proposes (a hand-written v1 rule, or one of P2's scopes that no phase has made
    proposable yet) -- callers then fall back to crediting the rule with whatever verbs its
    operation keys carry, which is the best that can be said about it."""
    return frozenset(
        verb
        for entry in propose.PROPOSABLE_SCOPES
        if entry.predicate == predicate
        for verb in entry.verbs
    )


def rule_verbs(rule: PolicyRule) -> tuple[Verb, ...]:
    """The verbs a rule actually allows, narrowest family first.

    Intersecting the predicate's own verbs with each operation key's verbs is what keeps a rule on a
    double-verb key honest: ``approved_channel`` under ``slack.read_messages`` allows ``read``, not
    ``read`` *and* ``search``, even though the key carries both (F7) -- the all-results predicate is
    the one that allows the search.
    """
    governed = _catalogue_verbs(rule.predicate)
    verbs = {
        verb
        for operation in rule.operations
        for verb in registry.operation_verbs(operation)
        if not governed or verb in governed
    }
    return tuple(sorted(verbs, key=propose.verb_sort_key))


def covered_tools(rule: PolicyRule) -> tuple[str, ...]:
    """Every tool this rule can auto-accept -- the redesign proposal's "what this actually
    unblocks", and the concrete answer to "does this cover one operation or thirteen?" that neither
    surface can give today."""
    verbs = frozenset(rule_verbs(rule))
    return tuple(sorted(
        entry.tool
        for entry in TOOL_REGISTRY.values()
        if entry.operation in rule.operations and entry.verb is not None and entry.verb in verbs
    ))


def _value_phrase(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def rule_sentence(rule: PolicyRule) -> str:
    """One rule as the sentence it is: ``Drive - folder 1CdeF: allow read, update, format``.

    An unconditional scope says so rather than hiding behind a rule name (D4), and a rule's
    conditions are appended as the narrowing they are.
    """
    scope_type = _scope_type_of(rule)
    connectors = sorted({propose.connector_of_operation(operation) for operation in rule.operations})
    connector = connector_label(connectors[0]) if connectors else ""
    if scope_type:
        noun = scope_type_label(scope_type)
        subject = f"{noun} {_value_phrase(rule.value)}" if rule.value else noun
    else:
        subject = rule.predicate
    verbs = ", ".join(verb.value for verb in rule_verbs(rule)) or "nothing"
    sentence = f"{connector} - {subject}: allow {verbs}" if connector else f"{subject}: allow {verbs}"
    if rule.conditions:
        sentence += " - when " + ", ".join(name.replace("_", " ") for name, _value in rule.conditions)
    return sentence


def button_label(proposal: RuleProposal) -> str:
    """The short phrase an "Always allow" button carries, so a reviewer knows which standing rule
    they are about to create before clicking -- deliberately the scope's own category ("this
    folder"), not the instance value, so the button's width doesn't depend on how long one folder id
    happens to be. ``""`` for an unconditional scope, which has no category to name; the button then
    stays plain "Always allow", exactly as ``auto_accept.describe_rule_short`` does for
    ``always_allow`` today."""
    return proposal.scope.hint


def widening_label(widening: Widening) -> str:
    """One widening chip: ``also allow format (2 tools)``. The tool count is the point -- it is the
    width the user is being asked to agree to, stated rather than implied."""
    tools = sum(
        1
        for entry in TOOL_REGISTRY.values()
        if entry.operation in widening.operations and entry.verb == widening.verb
    )
    suffix = "1 tool" if tools == 1 else f"{tools} tools"
    return f"also allow {widening.verb.value} ({suffix})"


def confirmation_text(proposal: RuleProposal, widenings: Iterable[Widening] = ()) -> str:
    """What the confirmation dialog says for a proposal plus whichever widenings were taken.

    Built from the rules that will actually be written (``propose.rules_for_proposal``), not from a
    template keyed on the proposal -- so the text can never describe a narrower rule than the one
    being created, which is the failure mode F2 describes from the other direction.
    """
    rules = propose.rules_for_proposal(proposal, widenings)
    tools = sorted({tool for rule in rules for tool in covered_tools(rule)})
    lines = ["Always allow: " + "; ".join(rule_sentence(rule) for rule in rules)]
    lines.append(f"Covers {len(tools)} tool{'' if len(tools) == 1 else 's'}: " + ", ".join(tools))
    return "\n".join(lines)


__all__ = [
    "button_label",
    "confirmation_text",
    "connector_label",
    "covered_tools",
    "rule_sentence",
    "rule_verbs",
    "scope_type_label",
    "widening_label",
]
