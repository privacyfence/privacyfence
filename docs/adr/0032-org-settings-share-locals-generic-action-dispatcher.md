# ADR 0032: Org mode's settings writes route through local mode's generic action dispatcher

## Status

Accepted — 2026-09-24. Implemented (PSC-5).

## Context

PSC-4b (#634–#637) merged local and org mode's settings *dispatch* into one module
(`web/routes_settings.py`), but org mode kept its own four bespoke, form-POSTing routes
(`/api/settings/rules/add`, `/api/settings/rules/remove`, `/api/settings/privacy/policy`,
`/api/settings/privacy/pii`) and its own plain-HTML-forms page renderer
(`web/org_settings_pages.py`) — deliberately: merging *rendering* was left to PSC-5.

PSC-5's brief is to give local and org mode one settings renderer
(`settings_window_html.build_html()`), capability-filtered per mode. That renderer is a
JS-driven single-page app: every interactive control (`renderAutoAccept`'s "Add rule", `renderGeneral`'s
PII toggle, `renderPrivacy`'s policy segmented controls) posts through one client-side bridge,
`window.webkit.messageHandlers.pf.postMessage({action, ...payload})`, which
`web/routes_settings.py`'s own `_settings_bridge_shim` turns into `fetch('/api/settings/' + action,
...)` for the browser. That bridge has no way to target four different URLs with four different
payload shapes for org's four actions — reusing the shared renderer's interactive controls for org
mode therefore requires org's writes to answer the same `POST /api/settings/{action}` path local
mode's own dispatcher does.

Separately, PSC-4a and PSC-4b both flagged (and left open) that org mode's settings pages have no
WebAuthn step-up ceremony UI: a 428/403 step-up refusal returned raw JSON instead of the passkey
prompt local mode's own settings page shows (`PF_WEBAUTHN_JS`/`_settings_bridge_shim`, injected by
local mode's `_render_settings_page`). Whether merging renderers would close that gap "naturally"
was explicitly left for this phase to decide.

## Decision

Org mode's four bespoke settings-write routes are replaced by one generic `POST
/api/settings/{action}`, restricted to `_ORG_ALLOWED_ACTIONS` — exactly the six actions
`org_settings_scope.ACTION_SCOPES` already declared permitted for `ORG_MODE`
(`add_policy_rule`, `remove_policy_rule`, `toggle_pii_detection`, `toggle_pii_category`,
`set_default_policy`, `set_category_policy`). It accepts the same JSON body shape local mode's
dispatcher does, reuses the same shared step-up/authorization/audit primitives
(`is_action_permitted`, `_apply_step_up_gate`, `_record_settings_audit`) org's bespoke routes
already used, and calls the same underlying mutators (`auto_accept.add_policy_v2_rules`/
`remove_policy_v2_rule`, `org_install_policy.apply_change`) those routes already called.

Both `GET /settings` and `GET /settings/privacy` now render through `settings_window_html.
build_html(state, mode="org", is_admin=principal.is_admin, ...)`, carrying the same
`PF_WEBAUTHN_JS`/`_settings_bridge_shim` injection local mode's page already had. This closes the
WebAuthn ceremony gap as a direct consequence of sharing the dispatch path, not as separate new
plumbing.

`toggle_pii_detection`/`toggle_pii_category` keep local mode's own "flip the current value"
semantics (an empty payload — what the shared toggle control actually posts) rather than
`org_install_policy.apply_change`'s previous explicit-`enabled`-field contract; the new dispatcher
computes the flip server-side before calling `apply_change`, so the mutator's own contract and
audit trail are unchanged, only the caller is.

## Alternatives considered

- **Keep org's four bespoke routes and forms, and only share the page's outer chrome/nav/copy.**
  Rejected: PSC-5's own brief calls for one renderer with capability-filtered *sections*, not a
  chrome-only merge, and it would leave the WebAuthn gap open, with no natural path to close it
  short of duplicating the ceremony UI a second time for a different request shape.
- **Give `build_html()` a second, forms-based rendering mode for org's writes, dispatch untouched.**
  Rejected: this is a materially larger JS change (every interactive template gaining a
  mode-branch between a bridge `data-action` and a plain `<form>`) for less benefit than reusing
  the dispatch path outright, and it still wouldn't close the WebAuthn gap, since org's writes
  would still not go through the bridge that carries the ceremony retry logic.
- **Also expose the other ~24 local-only actions (connector management, update checks, Telegram
  auth, etc.) to org mode.** Rejected: out of scope. `_ORG_ALLOWED_ACTIONS` is exactly
  `org_settings_scope.ACTION_SCOPES`'s existing `ORG_MODE` membership; nothing here widens what
  org mode can do, only how the six actions it could already do are dispatched and rendered.

## Consequences

- Org mode's admin-only Privacy Filter editor loses the "inherit vs. explicitly configured"
  distinction and "falling back to the org-mode block default" banner the former
  `web/org_settings_pages.py` page had (`_privacy_policy_view`'s own `falls_back_to_block_default`/
  `explicit_categories` fields, not carried into the shared renderer's simpler segmented-control
  model). This brings org mode down to local mode's own long-shipped Privacy Filter page's level of
  detail (`settings_controller._privacy_state`/`renderPrivacy` never had this distinction either),
  not a new limitation invented for org mode specifically — see this phase's own PR description
  for the follow-up.
- A future local-only action added to `_ALLOWED_ACTIONS` needs no org-mode decision at all unless
  someone also adds it to `ACTION_SCOPES`'s `ORG_MODE` set — at which point it is automatically
  both authorized *and* dispatchable for org mode through this one generic route, with no second
  route to remember to add.
- Local mode's own dispatcher (`settings_action` in `build_routes`) is untouched by this phase in
  either direction; a separate, orthogonal PR (`feature/audit-local-mode-settings`) independently
  resolved PSC-4b's own documented local/org audit-logging asymmetry by wiring
  `_record_settings_audit` into local mode's dispatcher too, landing on `main` while this phase was
  in flight. Both dispatchers now audit-log a settings write, the same shape, under their own
  principal (`LOCAL_PRINCIPAL` locally, the signed-in principal in org mode).

## Verification

- `web/routes_settings.py`'s `build_org_routes` (`_ORG_ALLOWED_ACTIONS`, `_apply_org_action`,
  `settings_action`) and `_wrap_org_settings`.
- `settings_window_html.py`'s `build_html(mode=, is_admin=)`/`_capabilities_for`.
- `tests/unit/web/test_routes_org_settings.py` (the generic dispatcher's authorization/CSRF/
  step-up/audit behavior) and `tests/unit/test_settings_window_html.py`'s `TestOrgCapabilities`
  (capability-filtered rendering).

## Related

PSC-4a (#635), PSC-4b (#637), #579 (org-mode settings step-up). Policy surface consolidation
plan, PSC-5 phase brief (branch `claude/579-580-adr006-plans`, not linked directly per this
directory's own rule 4 — see that plan's own commit history for its text before deletion).
