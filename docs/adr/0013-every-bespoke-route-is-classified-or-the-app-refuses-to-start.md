# ADR 0013: every bespoke settings POST route is classified, or the app refuses to start

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-19 in the self-approval
review's Phase 3, item 3.3: commit `151d6994` added the test and commit `4420453d` made
`build_routes()` enforce it). Implemented. The mechanism is described in
[`docs/TECHNICAL_REFERENCE.md`](../TECHNICAL_REFERENCE.md)'s settings-page section.

## Context

The `/settings` page mutates state through one generic dispatcher,
`POST /api/settings/{action}`. It is an explicit allowlist (`_ALLOWED_ACTIONS`), and every action
in it is classified in `_SENSITIVE_ACTIONS` or `_NON_SENSITIVE_ACTIONS`. A sensitive action goes
through `_needs_step_up` (passkey step-up) and `require_human_session` (an attributable session).
`TestSensitiveActionsCoverAllAllowedActions` fails if a new action lands without a classification.
`_NON_SENSITIVE_ACTIONS` is written out explicitly rather than derived as
`_ALLOWED_ACTIONS - _SENSITIVE_ACTIONS`, because a derived set would silently absorb an action
nobody classified.

Some requests do not fit "POST an action, get a snapshot back", so they got routes of their own
in `src/privacyfence/web/routes_settings.py`'s `build_routes()`. One of them,
`org_config_upload` (`POST /api/settings/org_config/upload`), is a multipart upload that can
rewrite the PII policy, every auto-accept rule and every connector's OAuth client config at once.
It was never checked by step-up or the human-session gate, because it was not a dispatcher action.
The classification ratchet covered action *names*, and this route was not one (finding F5 of the
review).

## Decision

Classification is required for routes as well as action names. Every bespoke POST route that
`build_routes()` constructs, meaning every POST route other than the generic
`/api/settings/{action}` dispatcher, must appear in exactly one of:

- `_BESPOKE_SENSITIVE_ROUTE_PATHS`: a route that has to apply the step-up and human-session gates
  by hand, inside its own route function. Today this is only the org-config upload.
- `_BESPOKE_EXEMPT_ROUTE_PATHS`: a dict mapping each path to a written reason why it needs no
  such gate. For example, `quit_app` has its own `confirmed=true` gate and does not change what
  gets gated.

`build_routes()` checks this every time the app is built, not only under pytest. An unclassified
bespoke POST route raises before the routes are returned, so the settings app, and with it the
daemon, never serves it.

## Alternatives considered

- **Rely on code review to spot a new bespoke route.** Rejected because this is how
  `org_config_upload` got through (TECHNICAL_REFERENCE.md: "which is how `org_config_upload`
  did").
- **Enforce it only with a test.** This was the first implementation (`151d6994`). It was extended
  the same day because the two sets were read only by the test, which a code-quality bot flagged
  as unused globals. The module comment argues that each check alone leaves a gap: the runtime
  check catches an unclassified route before it is served, and the test catches it at review/CI
  time.
- **Derive the exempt set as a complement.** Rejected for the same reason as for the action sets:
  a derived set silently absorbs unclassified entries. Every exemption carries its own reason
  instead.

## Consequences

- A new bespoke settings POST route cannot slip past the sensitive-action gates just by existing.
  Adding one means writing down whether it is sensitive, and why if it is not.
- Classifying a route does not gate it. A path in `_BESPOKE_SENSITIVE_ROUTE_PATHS` still has to
  call the gates itself, because multipart and JSON request shapes differ too much to share
  `settings_action`'s dispatch loop. The classification forces the decision. Whether the gate is
  actually applied is a separate check (see Verification).
- Scope is narrower than "every web route". The check covers POST routes built by
  `routes_settings.build_routes()` only. GET routes are listed in the exempt dict but skipped by
  the loop, and routes in other modules (approvals, MCP, downloads) are outside it.
- The runtime check is a Python `assert` (marked `# nosec B101`). Running the interpreter with
  `-O` would skip it. The comment beside it calls it "not a stripped-under-`-O` optimization", but
  that describes the intent, not how Python behaves. The pytest check is unaffected.

## Verification

- `src/privacyfence/web/routes_settings.py`: `_BESPOKE_SENSITIVE_ROUTE_PATHS`,
  `_BESPOKE_EXEMPT_ROUTE_PATHS`, and the classification loop at the end of `build_routes()`.
- `tests/unit/web/test_routes_settings.py::TestBespokeRoutesAreClassified`:
  `test_every_post_route_is_the_generic_dispatcher_sensitive_or_explicitly_exempt` walks the real
  `Route` objects and fails if it finds no bespoke route at all, so it cannot pass vacuously.
  `test_no_path_is_both_sensitive_and_exempt` checks that the two sets do not overlap.
- `TestSensitiveActionsCoverAllAllowedActions` in the same file is the action-name ratchet that
  this decision extends.
- The gates on the one sensitive path are tested separately, in `TestOrgConfigUploadStepUp` and
  `TestOrgConfigUploadHumanSession` in the same file.

## Related

- Commits `151d6994` ("Close the rest of the settings surface (self-approval review Phase 3)")
  and `4420453d` ("Make build_routes() enforce the bespoke-route classification, not just the
  test").
- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) decision 6 explains why the
  human-session and passkey gates are what make a sensitive action safe.
- [ADR 0012](0012-no-mcp-tool-mints-a-sign-in-credential.md) comes from the same self-approval
  review (Phase 2).
