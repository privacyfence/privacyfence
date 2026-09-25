"""Policy v2 vocabulary -- see the "Scope and Verbs" redesign proposal.

`policy/engine.py` is the only rule evaluator: the old one, and the `policy.engine` switch that
once chose between them, are gone (ADR 0004). Its consumers outside this package are `gate.py`,
which evaluates every gated call against the rules in `auto_accept._AutoAcceptState.
policy_v2_store_rules`, `daemon_main.py`, which loads those rules from the `auto_accept:` section
when it loads a principal's settings, and `settings_controller.py`/`web/routes_settings.py`.

P5 (`policy/propose.py`, `policy/describe.py`) landed the one writer both "Always allow" surfaces
will share -- the scope catalogue that replaces the five v1 suggestion tables, and the rendering
that lets a surface state a rule's width rather than leave it to be inferred. P6 wires the first of
those two surfaces up to it: `settings_controller.py`'s Auto-accept page reads and writes the v2
`auto_accept:` section directly (`policy/store.py`, `policy/propose.rules_for_scope_group`),
including the three F5 operation groups (Apps Script's tools, Gmail's filter tools, Slack's
group-chat tool) that had no v1 predicate to be reachable through at all -- see `policy/scopes.py`'s
`NEW_SCOPE_SELECTORS` and `policy/catalogue.EXTRA_SCOPES`. `gate.py`'s own two "Always allow"
suggestion call sites (the approval popup) are the one surface P5's writer still doesn't reach --
rewiring the popup's own widening-chip UI onto it remains a later phase's work.

P7 gives the MCP bridge the same one-shape write path: `gate.propose_policy_change` (a bridge-facing
counterpart to `SettingsController.add_policy_rule`/`remove_policy_rule`, writing straight to
`policy/store.py`'s v2 section via new `auto_accept.add_policy_v2_rules`/`remove_policy_v2_rule`
helpers) and `gate.preflight_auto_accept` (a `matched_rule_id`-returning wrapper around
`AutoAcceptEvaluator.preflight_from_args`, checked against the v2 engine the same two-layer way
`gate._evaluate_auto_accept` already does for a real call). `policy/catalogue.py` -- the scope
catalogue P6 built for its own Settings form, promoted out of `settings_controller.py` so P7 doesn't
re-derive it a second time -- is what both `add_policy_rule` and `propose_policy_change` validate a
`group`/`verbs` submission against, which is also where "a verb the scope type cannot govern is
rejected at write time" (F5/P0·3) actually lives. The pre-P7 bridge tools were kept as deprecated
aliases for one minor release per ADR 0004 decision 3, then deleted in PSC-3 once 4.1.2 shipped --
see that ADR for which tools and why.
"""
from __future__ import annotations
