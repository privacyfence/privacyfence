# ADR 0033: One route layer per surface, with an auth adapter per mode, not a second module

## Status

Accepted — 2026-09-24. Implemented (PSC-2b, PSC-4b, PSC-5).

## Context

Through PSC-1, local and org mode each had their own route module for the approval surface
(`web/routes_approvals.py`, 814 lines; `web/routes_org_approvals.py`, 755 lines) and the settings
surface (`web/routes_settings.py`, 845 lines; `web/routes_org_settings.py`, 621 lines), running
parallel handlers over the same route paths. Everything below the route layer — the approval card
HTML, the list rows, the step-up ceremony, passkey enrollment (`web/routes_security.py`, already one
module mounted by both modes), and, since the policy v2 redesign, the policy writers themselves —
was already shared. What differed between the two modes was the auth model: which session cookie,
which CSRF/origin check, which principal-resolution function.

[Issue #579](https://github.com/privacyfence/privacyfence/issues/579) named the concrete cost of
leaving that duplication in the route layer rather than factoring it into an adapter: two settings
surfaces, not two approval surfaces, is where it actually bit. Through P8 of the policy v2 redesign,
`web/routes_org_settings.py` kept reading and writing v1's `auto_accept_rules`/`auto_accept_grants`
directly for three phases after local mode moved to v2 — "entirely unaffected by [local mode's]
rewrite", its own module docstring said, and [ADR 0004](0004-retire-the-v1-auto-accept-config-model.md)
records that deleting v1 out from under it at that point "would have been a straightforward
regression." A second instance landed in the same neighbourhood: `org_settings_scope.py`'s
allow-list of org-permitted actions had, at one point, run ahead of the routes that actually
consumed it. Neither drift was "two implementations, two reviews" — each was one reviewed
implementation and one that quietly fell behind it, on the surface that did not get a second look
as often.

## Decision

**Every route surface that both local and org mode expose is one module, parameterized on a small,
explicit auth adapter — never a second implementation of the same control.** Concretely:

- `web/routes_approvals.py`'s `_build_route_list()` (PSC-2b) takes `resolve_principal`,
  `check_csrf`, `check_origin`, `unauthenticated_page_response`/`unauthenticated_read_response`,
  `step_up_response`, `bridge_shim`, `render_list_page`, and `human_session_guard` as keyword
  arguments; `create_app()` (local) and `build_routes()` (org) each supply their own adapter
  functions and delegate to it. `web/routes_org_approvals.py` is deleted.
- `web/routes_settings.py`'s `build_routes()` (local) and `build_org_routes()` (org, PSC-4b) share
  one `_ALLOWED_ACTIONS`/`_SENSITIVE_ACTIONS` dispatch core, `_needs_step_up`/
  `_apply_step_up_gate`/`_record_settings_audit` (previously duplicated, now module-level helpers
  both call), and — since PSC-5 — one renderer, `settings_window_html.build_html(mode=,
  is_admin=)`. `web/routes_org_settings.py` and `web/org_settings_pages.py` are deleted.
- `web/org_settings_scope.py`'s `ACTION_SCOPES` (PSC-4b) inverts from "a filter bolted onto local
  mode's action list" into the primary declaration both route layers project from: which modes an
  action has a real route in, and whether it is `admin_only`. `_ALLOWED_ACTIONS` is now *projected*
  from it, not the other way around.
- The pattern is the one `web/routes_security.py` already established for passkey enrollment before
  this plan started (one route module, mounted by both modes, parameterized on `resolve_principal`
  plus the session functions) — PSC-2b and PSC-4b/PSC-5 extend it to the other two surfaces rather
  than inventing a second shape.

Genuinely mode-specific behavior stays mode-specific, in the adapter or in a small sibling module,
never duplicated into the shared handlers: `require_human_session` (local-only, no org analogue);
the org-only IdP step-up fallback (`web/routes_org_stepup.py`, mounted only by the org app); the
three local-only bespoke settings routes (org-config bundle upload, audit-log download, `quit_app`).

## Alternatives considered

- **Keep per-mode modules, sharing more helper functions between them.** This was the status quo
  through PSC-1, and it is what produced both drift instances in "Context" above: a helper function
  can be imported into both modules and then diverge anyway, because there is still a second call
  site for a change to miss, and nothing forces the two to be reviewed together. A single module
  with mode differences confined to an adapter's parameters makes a second, unreviewed copy
  structurally impossible rather than merely discouraged — the same reasoning
  [ADR 0004](0004-retire-the-v1-auto-accept-config-model.md) applied to auto-accept evaluators
  applies here to route handlers.
- **Merge the settings surfaces' rendering (one page) as well as their dispatch.** Rejected — see
  [ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md) and "What this decision
  does not cover" below; the target was one implementation behind a capability-filtered subset, not
  one information architecture.

## What this decision does not cover

- **`web/routes_connect.py` and `web/oauth_loopback.py` stay separate.** Org mode's per-service
  OAuth authorization page is a server-side redirect flow; local mode's is a `webbrowser.open()`
  plus a loopback listener. That is a different mechanism for reaching the same outcome, not a
  second coating over one control — the drift argument above is about two implementations of the
  *same* decision, and there is no shared decision here to duplicate.
- **Org mode gains no new capabilities.** `web/org_settings_scope.py`'s per-principal connector
  actions (`enable_connector`, `disable_connector`, `refresh_connectors`, `authenticate_connector`)
  stay `LOCAL_MODE`-only — meaningful per-principal in org mode in principle, but with no org route
  behind them, `is_action_permitted` denies them by construction (the B20 finding this table exists
  to prevent from recurring: an allow-list entry with no route is the bug). A merged dispatcher
  makes adding an org route for one of them mechanically easy; deciding whether org mode *should*
  expose connector management is a separate decision this phase does not make.
- **The settings surfaces are one implementation, not one information architecture.** Org mode kept
  its own admin-only nav split (`ORG_NAV_ITEMS`) and lost some local-only display richness in the
  Privacy Filter editor when rendering merged in PSC-5 — see
  [ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md)'s own Consequences section
  for the concrete UI difference this traded away; that ADR's scope decision (routing org's writes
  through the shared dispatcher) is narrower than and implements part of this one.

## Consequences

- A security fix to the approval or settings route layer (authorization, CSRF, step-up) lands in
  one file and applies to both modes automatically, the same property
  [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md)'s P10 decision already established
  for the native-vs-web approval UI split.
- A merged decide/dispatch route also means one bug reaches both modes at once. This is accepted
  risk, not a new one: everything below the route layer (the card HTML, `gate.py`, `approvals.py`,
  the step-up ceremony, and, since the policy v2 redesign, the policy writer both settings surfaces
  call) already carried this property before this phase: the route layer was the last place
  duplication stood in for a second implementation, not a second review.
- A future action added to local mode's settings dispatcher needs no separate org-mode decision at
  all unless someone also adds it to `ACTION_SCOPES`'s `ORG_MODE` membership — seen through in
  practice by [ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md).

## Verification

- `web/routes_approvals.py`'s `_build_route_list()`/`create_app()`/`build_routes()`; the absence of
  `web/routes_org_approvals.py`.
- `web/routes_settings.py`'s `build_routes()`/`build_org_routes()`/`_needs_step_up()`/
  `_apply_step_up_gate()`; the absence of `web/routes_org_settings.py` and
  `web/org_settings_pages.py`.
- `web/org_settings_scope.py`'s `ACTION_SCOPES`/`is_action_permitted()`, and its own test forcing
  every `_ALLOWED_ACTIONS` member to carry a classification.
- `tests/unit/web/test_routes_approvals.py` (extended in PSC-2b for the merged module; an org-mode
  request cannot read or decide another principal's approval).
- `tests/unit/web/test_routes_org_settings.py`, `tests/unit/test_settings_window_html.py`'s
  `TestOrgCapabilities`.

## Related

- [Issue #579](https://github.com/privacyfence/privacyfence/issues/579) — this decision's own
  tracking issue; closed by PSC-4a (#635), with the approval-surface merge (PSC-2b, #636) and the
  settings-surface merge (PSC-4b, #637; rendering, PSC-5, #641) landing alongside it.
- [ADR 0004](0004-retire-the-v1-auto-accept-config-model.md) — the earlier instance of the same
  "second implementation drifts" failure mode this decision generalizes past auto-accept
  evaluation to the route layer.
- [ADR 0008](0008-one-principal-per-os-user.md) — D5's `_PrincipalScopeMiddleware`/
  `current_principal()` plumbing, and that ADR's own Related section, which named this merge as
  what #579 still had to do once both modes already agreed on how to resolve a principal.
- [ADR 0032](0032-org-settings-share-locals-generic-action-dispatcher.md) — the narrower PSC-5 scope
  decision (routing org's settings writes onto the shared dispatcher, beyond pure rendering) that
  this ADR's settings-surface half rests on.
- Policy surface consolidation plan, phases PSC-1 through PSC-6 (branch
  `claude/579-580-adr006-plans`, not linked directly per this directory's own rule 4 — see that
  plan's own commit history for its text before deletion).
