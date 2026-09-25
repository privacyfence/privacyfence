# Ledger: step 3

Slice: the approval window, the approvals list and binder, cards, and the approval routes (18 files,
216 guard hits, all removed). Bare `§1`/`§2`/`§3` references to the approval dialog's sections are
now the cards' names, which `approval_window_html.py`'s module docstring defines once: the **action
card** ("Action to perform" / "What Claude already knows"), the **reason card**, the **risk card**
and the **disclosure card** ("What will be provided to Claude"). `§1-§4` became "the left-column
cards".

Decisions already covered by an ADR are now cited by number: one approval implementation (ADR 0001,
`approval_ui.py`), step-up making a session insufficient (ADR 0002 decision 6,
`web/routes_approvals.py`), no MCP tool mints a sign-in credential (ADR 0013,
`tests/system/test_local_mode_system.py`), one route layer with an auth adapter per mode (ADR 0033,
`web/routes_approvals.py`).

## ADR candidates

1. **The approvals list never offers a per-row Allow; the only approve-from-list path is batch
   approval, gated on one WebAuthn assertion bound to the exact submitted set.** Row-level Deny
   needs no card and no step-up (denying leaks nothing); approving from a one-line summary is the
   habituation failure the card exists to prevent. A batch's `batch_id` is server-minted and a
   client-supplied one is never recorded unless a verified step-up assertion bound it. Rejected
   alternatives: an Allow button per row; one passkey ceremony per item in a batch; an `<iframe>`
   of the real card for the inline "Details" disclosure (cards ship `frame-ancestors 'none'`).
   Cited by: `src/privacyfence/approval_list_html.py` (module docstring, `_JS`),
   `src/privacyfence/web/routes_approvals.py` (`batch_decide`), `tests/unit/web/test_routes_approvals.py`
   (`TestBatchDecide`, `TestBatchStepUp`), `tests/unit/test_approval_list_html.py`. Meets the bar:
   trust boundary (what a human must see before data is released) and non-obvious rejected
   alternatives.
2. **Step-up fallback differs by mode.** Org mode offers an IdP re-auth link as a step-up fallback
   whenever `step_up.require_passkey` is off, so a decision is never released unguarded; local mode
   has no IdP, so with no passkey enrolled and `require_passkey` off, an approving decision goes
   through unguarded rather than deadlocking; `require_passkey` closes the fallback in both modes
   with a `403` naming `/security`. ADR 0034 covers the settings-write side of `require_passkey`
   but not the approval decide endpoint's fallback. Cited by: `src/privacyfence/web/routes_approvals.py`
   (`_local_step_up_response`, `_org_step_up_response`, `_org_bridge_shim`, module docstring),
   `tests/unit/web/test_routes_approvals.py` (`TestStepUpEvadableWithNoPasskeyEnrolled`,
   `TestRequirePasskeyHardFail`). Meets the bar: trust boundary.

## Changed user-visible strings

None. Every change is a comment, a docstring, or a comment inside the list page's inline `_JS`
(served to the browser, never displayed).

## Cross-slice edits needed

- `src/privacyfence/resources/sw.js:6` and `src/privacyfence/web_shell.py:155` say tier 2 push is
  "org-mode/P7+ work"; `tests/unit/web/test_routes_approvals.py` now says "tier 2, web push with
  VAPID, is not implemented". Those two lines belong to another slice.
- `tests/integration/test_browser_smoke.py:621,984` and `tests/unit/test_web_shell.py:151` cite
  `5deef1d8:docs/approval-list-ui-ux.md` and its `§3 point 4` / `§4.4`, the same deleted UX spec
  this slice's files no longer cite.
- **Guard gap, for step 7:** `test_code_no_history.py` misses three shapes this slice had, which
  I rewrote anyway: a plan item ID right after a docstring's opening quotes (`"""W8 ...`,
  `"""B23 of the 4.1.0 action plan`), because the plan-item lookbehind treats `"` as a string
  literal; a `§N` after a git-object path to a deleted doc (`5deef1d8:docs/approval-list-ui-ux.md §3`),
  because `_CITATION` accepts any `*.md`; and `PSC-2a`/`PSC-2b` (policy surface consolidation
  IDs), which no pattern covers. Worth a pattern or a `_CITATION` tightening before the pending
  lists go.

## Bugs noticed

- `src/privacyfence/approval_window_html.py`, `build_card_stack_html`: the `pinned_html` /
  `scrollable_html` comments ("always fully visible", "the only card that ever scrolls") and
  "Pinned, and placed *before* the disclosure card -- ... must never end up scrolled out of view"
  contradict the same function's docstring and its later comment, which say the whole left column
  is one shared scroll region, so those cards *can* scroll out of view. Comment-only; I kept the
  claims as they were apart from the section names.

## Open issues kept as URLs

None. Every issue this slice referenced is closed, so each reference was replaced by its reason:
#121, #141, #145, #151, #396, #406, #423, #426, #428, #576 (all closed, checked 2026-09-25).
