"""Policy v2 vocabulary -- see the "Scope and Verbs" redesign proposal.

This package is being built up in independently-reviewable phases (P0-P9); each phase's exit
criterion is "no behaviour change" until the engine swap (P3) lands, and even then the change is
opt-in (`policy.engine: v2`, default `v1`) until a later phase flips the default. `gate.py` (P3,
shadow-mode evaluation, plus P6's own always-on v2-store check -- see `auto_accept.
_AutoAcceptState.policy_v2_store_rules`), `daemon_main.py` (P4, the one-time on-disk migration; P6,
hot-loading that same v2-store check), `settings_controller.py`/`web/routes_settings.py` (P4, the
post-migration Settings notice) are consumers outside this package and its tests.

P5 (`policy/propose.py`, `policy/describe.py`) landed the one writer both "Always allow" surfaces
will share -- the scope catalogue that replaces the five v1 suggestion tables, and the rendering
that lets a surface state a rule's width rather than leave it to be inferred. P6 wires the first of
those two surfaces up to it: `settings_controller.py`'s Auto-accept page reads and writes the v2
`auto_accept:` section directly (`policy/store.py`, `policy/propose.rules_for_scope_group`),
including the three F5 operation groups (Apps Script's tools, Gmail's filter tools, Slack's
group-chat tool) that had no v1 predicate to be reachable through at all -- see `policy/scopes.py`'s
`NEW_SCOPE_SELECTORS` and `settings_controller._POLICY_EXTRA_SCOPES`. `gate.py`'s own two
"Always allow" suggestion call sites (the approval popup) are the one surface P5's writer still
doesn't reach -- rewiring the popup's own widening-chip UI onto it remains a later phase's work.
"""
from __future__ import annotations
