"""The auto-accept policy: which gated calls may go through without asking.

A rule names a **scope** (which resources: a folder, a sender domain, a Slack channel kind), the
**operation keys** it covers, and optionally **conditions** that narrow it further. The package is
split by job:

* `registry.py` maps every tool to its operation key and verb; `resource_registry.py` holds the
  Settings resource grants.
* `scopes.py` and `conditions.py` hold one selector per predicate -- the code that decides whether a
  call's item is in scope, or whether a condition holds.
* `engine.py` is the only rule evaluator: the old one, and the `policy.engine` switch that once
  chose between them, are gone (ADR 0004).
* `store.py` reads and writes the on-disk `auto_accept:` section; `propose.py` is the one writer
  every "Always allow" surface uses to build rules; `describe.py` renders a rule as a sentence; and
  `catalogue.py` is the scope catalogue the Settings form and the MCP bridge validate a
  `group`/`verbs` submission against, which is where a verb the scope type cannot govern is rejected
  at write time.

Consumers outside this package are `gate.py`, which evaluates every gated call against the rules in
`auto_accept._AutoAcceptState.policy_v2_store_rules` and builds the popup's "Always allow" choices
from `propose.py`; `daemon_main.py`, which loads those rules from the `auto_accept:` section when it
loads a principal's settings; `settings_controller.py`/`web/routes_settings.py`, the Auto-accept
Settings page; and `web/mcp_dispatch.py`, the `privacyfence_check_policy`/`list_policy`/
`propose_policy_change` tools. Apps Script's tools, Gmail's filter tools and Slack's group-chat tool
are governable only through scopes that have no old predicate name -- see `scopes.NEW_SCOPE_SELECTORS`
and `catalogue.EXTRA_SCOPES`. The bridge tools that preceded `propose_policy_change` were removed; ADR
0004 records which and why.
"""
from __future__ import annotations
