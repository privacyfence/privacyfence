"""The confirmation-dialog probe ``test_org_ubuntu_release_smoke.py`` drives.

The release smoke test needs one MCP call that always blocks on a real human confirmation, with no
connector, OAuth flow or third-party credential behind it, so the tool call -> approval card ->
decide route -> settings.yaml -> audit log contract can be exercised end to end against a daemon
started from an installed package.

The probe is ``privacyfence_propose_policy_change`` (``gate.propose_policy_change``). Its request
is ``{operation, group, verbs, value, reason}``, validated against
``policy.catalogue.scope_catalogue()``; the audit log records ``decision`` as
``policy_rule_changed_via_bridge_proposal`` when the proposal is confirmed.

``assert_probe_rule_on_disk`` reads the v3 ``auto_accept:`` section and checks the whole row (scope,
value, verbs and derived operations) rather than grepping for a string, so it fails if the write
path regresses.
"""
from __future__ import annotations

from typing import Any

import yaml

#: The meta-tool the smoke test drives for its confirmation-dialog round trip.
PROBE_TOOL = "privacyfence_propose_policy_change"

#: A ``policy.catalogue.scope_catalogue()`` group id, and the one verb of its own the probe asks
#: for. Gmail's sender-domain scope needs a value (a domain).
PROBE_GROUP = "gmail.sender_domain"
PROBE_VERBS = ["read"]

#: The operations ``(PROBE_GROUP, PROBE_VERBS)`` stores in its row.
PROBE_OPERATIONS = ("gmail.read_message", "gmail.read_thread")

#: ``gate.propose_policy_change``'s audit decision for a confirmed proposal.
AUDIT_DECISION_CHANGED = "policy_rule_changed_via_bridge_proposal"


def probe_arguments(*, value: list[str], reason: str) -> dict[str, Any]:
    """``PROBE_TOOL``'s arguments for adding the probe rule over ``value``.

    ``reason`` is the self-reported, logged justification every gated and meta tool takes.
    """
    return {
        "operation": "add",
        "group": PROBE_GROUP,
        "verbs": list(PROBE_VERBS),
        "value": list(value),
        "reason": reason,
    }


def expected_description(value: str) -> str:
    """The substring a confirmed response's ``description`` carries for a probe rule over ``value``."""
    return f"Gmail - sender domain {value}: allow read"


def probe_rule_on_disk(settings_text: str, *, value: list[str]) -> dict[str, Any] | None:
    """The probe's row in ``settings_text``'s v3 ``auto_accept:`` section, or ``None``.

    Never raises on a file it cannot make sense of: an unparseable, missing or wrong-shaped section
    is simply "no such rule".
    """
    try:
        config = yaml.safe_load(settings_text)
    except yaml.YAMLError:
        return None
    if not isinstance(config, dict):
        return None
    section = config.get("auto_accept")
    if not isinstance(section, dict) or section.get("version") != 3:
        return None
    rules = section.get("rules")
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("scope") == PROBE_GROUP and rule.get("value") == list(value):
            return rule
    return None


def assert_probe_rule_on_disk(settings_text: str, *, value: list[str]) -> None:
    """Assert a confirmed probe round trip over ``value`` persisted as a complete version-3 row."""
    rule = probe_rule_on_disk(settings_text, value=value)
    assert rule is not None, (
        f"no version-3 auto_accept row with scope {PROBE_GROUP!r} over {value!r} in "
        f"settings.yaml -- got: {settings_text!r}"
    )
    assert rule.get("verbs") == PROBE_VERBS, f"probe row has the wrong verbs: {rule!r}"
    operations = rule.get("operations")
    assert isinstance(operations, list) and set(PROBE_OPERATIONS) <= set(operations), (
        f"probe rule persisted without the operations {PROBE_VERBS!r} derives for "
        f"{PROBE_GROUP!r}: {rule!r}"
    )
