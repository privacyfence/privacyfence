"""The confirmation-dialog probe the packaged-artifact smoke tests drive, in one place.

``test_macos_packaged_smoke.py``, ``test_windows_packaged_smoke.py``,
``test_deb_packaged_lifecycle.py`` and ``test_org_ubuntu_release_smoke.py`` all need the same
thing from the artifact they just built: one MCP call that *always* blocks on a real human
confirmation, with no connector, OAuth dance or third-party credential of any kind behind it, so
the tool call -> approval card -> decide route -> settings.yaml -> audit log contract can be
exercised end to end against a frozen daemon that offers no ``build_connectors`` monkeypatch seam.
Each module's own docstring explains why it makes that substitution; this module is the
substitution itself, so the four of them name one tool rather than four copies of one.

The probe is ``privacyfence_propose_policy_change`` (``gate.propose_policy_change``, P7 of the
policy v2 redesign). It was ``privacyfence_propose_auto_accept_rule_change`` -- gate.py's
deprecated v1-shaped alias -- until #580 came to delete that tool, which would have left the
release gate driving code that no longer exists. Both register their confirmation with
``sensitive=True`` (gate.py carries the same comment verbatim at both sites), so the dialog class,
the unattended-session refusal and the step-up posture a packaged install imposes on it are all
unchanged by the move; what changes is the request shape and, with it, what the audit log and
settings.yaml assertions on the other side have to be keyed on:

=========================  ==============================================  ==========================================
                           v1 (``propose_auto_accept_rule_change``)        v2 (``propose_policy_change``)
=========================  ==============================================  ==========================================
request                    ``{target, operation_key, rule_name, value}``   ``{operation, group, verbs, value}``
validated against          nothing before the popup                        ``policy.catalogue.scope_catalogue()``
audit ``connector``        ``"rule"`` (the ``target``)                     ``"policy"``
audit ``decision``         ``rule_changed_via_bridge_proposal``            ``policy_rule_changed_via_bridge_proposal``
=========================  ==============================================  ==========================================

``ACCEPTED_DESCRIPTION``/``rule sentence`` do *not* change: P9 already made the v1 alias report the
v2 rule's own ``policy.describe.rule_sentence`` rather than echoing the v1 ``rule_name``, so the
confirmed-response assertions carried over verbatim.

**On-disk shape is what to assert, not a rule name.** The probe's group compiles to a v2 rule whose
``predicate`` happens to still spell ``trusted_sender_domain`` -- v2 kept v1's predicate vocabulary
(``policy/store.py``'s own docstring) -- so a bare ``"trusted_sender_domain" in settings_text``
grep keeps passing across this move without proving anything about *which* writer produced it, or
that the write landed in the v2 ``auto_accept:`` section at all rather than in a stale v1
``auto_accept_rules:`` one. ``assert_probe_rule_on_disk`` below reads the section
``policy/store.py`` defines and checks the whole row -- schema version, predicate, value and the
operations the requested verb derives -- which is the assertion that actually fails if the write
path regresses.
"""
from __future__ import annotations

from typing import Any

import yaml

#: The meta-tool every packaged smoke test drives for its confirmation-dialog round trip.
PROBE_TOOL = "privacyfence_propose_policy_change"

#: A ``policy.catalogue.scope_catalogue()`` group id, and the one verb of its own the probe asks
#: for. Gmail's sender-domain scope needs a value (a domain), which is what gives each round trip
#: in a module -- allow and deny -- its own distinguishable rule.
PROBE_GROUP = "gmail.sender_domain"
PROBE_VERBS = ["read"]

#: What ``(PROBE_GROUP, PROBE_VERBS)`` compiles to through
#: ``policy.catalogue.rules_for_catalogue_entry``: v2 keeps v1's predicate vocabulary, and ``read``
#: on this group governs both Gmail read operations, not just the single ``gmail.read_message``
#: key the v1 probe used to name by hand.
PROBE_PREDICATE = "trusted_sender_domain"
PROBE_OPERATIONS = ("gmail.read_message", "gmail.read_thread")

#: ``gate.propose_policy_change``'s own audit vocabulary, for the log assertions on the far side of
#: a round trip. ``"rejected"`` is unchanged from v1 -- declining a dialog is declining a dialog.
AUDIT_CONNECTOR = "policy"
AUDIT_DECISION_CHANGED = "policy_rule_changed_via_bridge_proposal"
AUDIT_DECISION_REJECTED = "rejected"


def probe_arguments(*, value: list[str], reason: str) -> dict[str, Any]:
    """``PROBE_TOOL``'s arguments for adding the probe rule over ``value``.

    ``reason`` is the same self-reported, logged one-sentence justification every gated and meta
    tool takes; pass the calling module's own scenario name so an audit entry read back off disk
    says which smoke test wrote it.
    """
    return {
        "operation": "add",
        "group": PROBE_GROUP,
        "verbs": list(PROBE_VERBS),
        "value": list(value),
        "reason": reason,
    }


def expected_description(value: str) -> str:
    """The substring a confirmed response's ``description`` carries for a probe rule over
    ``value`` -- ``policy.describe.rule_sentence``'s own rendering, the same one the approval card
    a human just clicked showed them."""
    return f"Gmail - sender domain {value}: allow read"


def probe_rule_on_disk(settings_text: str, *, value: list[str]) -> dict[str, Any] | None:
    """The probe's own row in ``settings_text``'s v2 ``auto_accept:`` section, or ``None``.

    Reads the section ``policy/store.py`` defines (``version: 2`` plus a ``rules:`` list of
    ``rule_to_dict`` rows) rather than grepping, and never raises on a file it cannot make sense
    of: an unparseable, missing or wrong-shaped section is simply "no such rule", which is the
    failure the caller's own assertion should report.
    """
    try:
        config = yaml.safe_load(settings_text)
    except yaml.YAMLError:
        return None
    if not isinstance(config, dict):
        return None
    section = config.get("auto_accept")
    if not isinstance(section, dict) or section.get("version") != 2:
        return None
    rules = section.get("rules")
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("predicate") == PROBE_PREDICATE and rule.get("value") == list(value):
            return rule
    return None


def assert_probe_rule_on_disk(settings_text: str, *, value: list[str]) -> None:
    """Assert a confirmed probe round trip over ``value`` actually persisted, as a v2 rule.

    The claim is the whole row, not the presence of a string: the predicate and value the probe's
    group/value compile to, *and* every operation its verb derives. A writer that persisted a
    narrower rule than the dialog described -- or wrote into v1's ``auto_accept_rules:`` section
    instead, which shares the predicate vocabulary -- fails here rather than grepping green.
    """
    rule = probe_rule_on_disk(settings_text, value=value)
    assert rule is not None, (
        f"no v2 auto_accept rule with predicate {PROBE_PREDICATE!r} over {value!r} in "
        f"settings.yaml -- got: {settings_text!r}"
    )
    operations = rule.get("operations")
    assert isinstance(operations, list) and set(PROBE_OPERATIONS) <= set(operations), (
        f"probe rule persisted without the operations {PROBE_VERBS!r} derives for "
        f"{PROBE_GROUP!r}: {rule!r}"
    )
