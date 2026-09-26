# Deny with feedback — plan

**Status:** proposed, 2026-09-26. Nothing here is implemented. Tracks
[privacyfence/privacyfence#640](https://github.com/privacyfence/privacyfence/issues/640). This is
a plan document per [`adr/README.md`](adr/README.md): it is deleted when its work lands, after its
decisions have been extracted into ADRs 0082 and 0083 (below).

**Starts only after org-mode-mobile has shipped.** This plan is written against the code as it
stands on `feature/org-mode-mobile` (tip `ce901da5` when this was written), not against today's
`main`. That branch rewrites every surface this plan touches: the approval card's markup and CSS
(`approval_window_html.py`, `resources/approval_window/styles.css`), the approval list
(`approval_list_html.py`), the design system (`resources/design/`), and it adds web push
(`web_push.py`, `web/routes_push.py`). Starting earlier would mean building the deny-note UI twice,
or merging it into a rewrite. The gate is phase p1's step 0: `feature/org-mode-mobile`'s work must
be on `main` **and** inside a pushed release tag (`git tag --contains <its merge commit> --list
'v*'` is non-empty, either channel). If it is not, p1 stops as `blocked`. Holding back until the
release tag exists keeps the mobile release's notes and its release testing
(`docs/release-testing.md`'s manual phone checks) separate from this change.

**To run it:** `/implement <GitHub URL of this file>`, after the gate above holds. Merge
`origin/main` into the plan's branch first, so the orchestrator cuts `feature/deny-with-feedback`
from a base that already carries the mobile work.

## Goal

When a person denies an approval, they can optionally tell the agent **why, or what to do
instead**, in the same action. The agent receives that on both delivery paths: the synchronous
`GateDeniedError` and `privacyfence_await_approval`. A plain Deny still takes one tap. It now
returns a clearer default that discourages blind retries.

## What happens today (measured on `main` and `feature/org-mode-mobile`)

| Step | Where | What it carries |
|---|---|---|
| Card Deny | `approval_window_html._button_row_html` (`data-pf-action="deny"`), `_JS.post()` → `window.webkit.messageHandlers.pf.postMessage({action:'resolve', result})` | `result` only |
| Card → server | `routes_approvals._bridge_shim` / `_org_bridge_shim`: `fetch` POST of `Object.assign({}, payload, {csrf})` | Every key of the payload, so a new key passes through unchanged |
| Decide route | `routes_approvals` `decide` (`POST /api/approvals/{id}/decide`): JSON body, CSRF, origin, then `web_ui.resolve(id, result, choice, principal_id=)` | Other body keys are ignored |
| List row Deny, "Deny selected" | `approval_list_html` `denyOne()`/`denySelected()`: one per-id decide POST each (client-side fan-out; the batch endpoint is used only by "Approve selected") | `{result:'deny', csrf}` |
| Record | `PendingApproval.answer()` → `result`, `decided_via`, `batch_id`; `finalize()`; `LedgerHit` | No reason field |
| Sync delivery | `gate.gated_call` review branch and popup branch: `raise GateDeniedError("Request denied by user")` | Static text |
| To the client | `routes_mcp` → `safe_errors.public_message(exc)`, which trusts named `RuntimeError` subclasses verbatim after `redact_secrets()` → `is_error` tool result | Static text |
| Async delivery | `mcp_dispatch.await_approval` → `{approval_id: status}`, where status comes from `registry.await_status` and is `"denied"` | Status only |
| Audit | `gate._audit`, decision `"rejected"`, `AuditEntry` schema v5 | No reason field |

Corrections to the issue's "every approval surface" list:

- **There is no native popup.** `WebApprovalUI` is the only `ApprovalUI` implementation.
  `approval_window.py` and `approval_popup.py` are gone and survive only in comments. The macOS,
  Windows and Linux companion windows render the same web card.
- **`web/control_channel.py` carries no approval decisions.** It is the daemon/companion
  line protocol (MINT, ENROLLMENT, SHOW, …). Org mode decides over the same HTTP routes as local
  mode, scoped by principal, so nothing changes there.
- **Batch deny does not use the batch endpoint.** "Deny selected" fans out per-id decide POSTs.
- **Confirm dialogs** (`dialog_window_html.py`: the PII confirmation and the "Always allow" rule
  confirmation) post `cancel`, not `deny`. They are follow-up steps after the person chose Allow on
  a card, so their Cancel is not a reply to the agent's request. They are out of scope (see "Not
  in scope").

## Design

### The note and the intent

A denial carries two optional parts, both authored only by the human who decides the card:

- **`intent`**: one of a fixed vocabulary, picked from chips. It gives the agent a structured
  instruction even when nothing is typed.

  | Value | Chip label | Guidance text the agent receives |
  |---|---|---|
  | `stop` | Stop — don't retry | The user does not want this done. Do not retry this or a similar call; ask the user before doing anything further toward it. |
  | `wrong_target` | Wrong target | The user says the target is wrong (recipient, file, folder, record or account). Correct the target, then ask again. |
  | `rewrite` | Change the content | The user wants the content changed. Revise it, then ask again. |
  | `different_approach` | Try another way | The user wants a different tool or approach for this, not a retry of this call. |

- **`note`**: free text, up to **500 characters** after sanitization. The server sanitizes it
  with a new `deny_feedback.sanitize_note()`, modelled on
  `agent_identity.sanitize_client_string()`:
  - normalize to NFC;
  - turn `\r\n` and `\r` into `\n`;
  - drop every Unicode `Cc`/`Cf`/`Zl`/`Zp` character except `\n` (this removes bidi and
    zero-width characters too);
  - collapse runs of more than two `\n` into two;
  - strip surrounding whitespace.

  A note that is **longer than 500 characters after sanitization is rejected with 400**, not
  truncated. The textarea's `maxlength` stops a real user before that, so only a crafted client
  hits it, and silently cutting what someone wrote to an agent is worse than refusing. An empty
  result means no note.

Both live in one new module, `src/privacyfence/deny_feedback.py`:

- `INTENTS` (the table above);
- `MAX_NOTE_CHARS = 500`;
- `@dataclass(frozen=True) DenialFeedback(intent: str = "", note: str = "")`, with `is_empty`;
- `parse(payload) -> DenialFeedback`, which raises `ValueError` with a static message for an
  unknown intent, a non-string note or an over-long note;
- `sanitize_note(str) -> str`;
- `denial_message(feedback) -> str` and `await_entry(feedback) -> dict`.

The `stop` chip is not special server-side. It is guidance text like the others. PrivacyFence
does not start refusing the agent's later calls because of it: that would be a new policy
mechanism, and it does not belong in a feedback feature.

### What the agent receives

**Synchronous path.** `GateDeniedError` gets one constructor for the human-deny case,
`GateDeniedError.by_user(feedback)`, which builds its message with `deny_feedback.denial_message`.
Every `raise GateDeniedError("Request denied by user")` in the two `gated_call` branches is replaced
with it. The unattended and policy denials keep their own static text.

```
Request denied by user. Don't retry the same call; ask the user how to proceed.
```
With an intent and a note:
```
Request denied by user. The user chose "Wrong target": the user says the target is wrong
(recipient, file, folder, record or account). Correct the target, then ask again.
User's note (written by the person who denied this request; JSON string):
"Don't email the whole team. Just send it to Anna."
```

- The message is a single line in reality. Newlines are shown here only for layout.
- It always starts with `Request denied by user.`, the exact prefix two connector tests and any
  client-side matching already rely on.
- The note is inserted as `json.dumps(note, ensure_ascii=False)`, so quotes, backslashes and
  newlines in it are escaped. The note can never end the string early or appear as PrivacyFence's
  own text. This is the "clearly delimited field" the issue asks for.
- `public_message()` still runs `redact_secrets()` over the whole message, so a token someone
  pastes into a note is redacted on the way out.

**Async path.** `privacyfence_await_approval` keeps its `{approval_id: status}` contract exactly:
every value is still a bare status string. A denied approval that carries feedback also appears
under one reserved top-level key:

```json
{
  "3f2a…": "denied",
  "9b1c…": "approved",
  "denial_feedback": {
    "3f2a…": {"intent": "wrong_target", "note": "Don't email the whole team. Just send it to Anna.",
              "guidance": "The user says the target is wrong (…). Correct the target, then ask again."}
  }
}
```

- The key appears only when at least one denied id in the call has non-empty feedback.
- `intent`/`note` are `null` when absent.
- Approval ids are `uuid4().hex` (`approvals.register_or_coalesce`/`register_confirm`), so they can
  never collide with the reserved key. A unit test asserts this.
- The tool's description gains one sentence: *"A 'denied' approval may also have an entry under
  `denial_feedback`: that is the user's own instruction for what to do instead, so follow it rather
  than retrying."*
- The `denied` wording in the description changes to: *"a human said no -- re-issuing will not
  change that; don't retry, ask the user how to proceed unless denial_feedback says otherwise"*.

Why a reserved key instead of turning a denied id's value into an object: every existing consumer,
the tool description and `mcp_dispatch.await_approval`'s `any(s != "pending" …)` loop treat values
as strings, and models read that description literally. One additive key keeps every current reader
correct. ADR 0082 records this.

**When the agent re-issues after a denial.** `LedgerHit` gains `feedback: DenialFeedback`, so a
re-issued call inside the ledger TTL raises the same message the synchronous path would have. The
agent learns the reason whichever path it collects the decision on.

### Data model and plumbing

- `PendingApproval.deny_feedback: DenialFeedback`, default empty. It is set in `answer()` under the
  same first-answer-wins check as `result`, before `event.set()`, so the worker woken by the event
  always sees it. Only a `deny` result may carry it: `answer()` raises `ValueError` for
  non-empty feedback with any other result, and the route turns that into a 400 before it gets
  there.
- `PendingApprovalRegistry.answer(..., feedback=DenialFeedback())` passes it through. `WebApprovalUI.resolve(..., feedback=)` and
  `ApprovalUI.resolve`'s signature follow.
- `finalize()` leaves it as is. `consume_ledger()` copies it into `LedgerHit`.
  `_resolve_decision()` gains a sixth tuple element, `feedback`. The no-registry `interact(None)`
  branch and the `_drive_interaction` crash-to-deny branch return an empty one.
- New read method `registry.denial_feedback(approval_id, *, principal_id) -> DenialFeedback | None`,
  with the same principal scoping as `await_status`, so a foreign id reads as nothing.
- Coalesced waiters on one approval all receive the same feedback. That is correct: they are the
  same request.

### Routes

- `decide` reads optional `note` and `intent` keys:
  - Present with `result == "deny"`: `deny_feedback.parse` validates them, and a `ValueError`
    becomes 400 `{"status": "error", "error": "<static text>"}`.
  - Present with any other result: 400. The note must never ride along on an approval, where
    it would reach the agent together with data it just got permission for.
  - CSRF and origin checks run first, unchanged.
  - Deny still skips step-up: `_STEP_UP_RESULTS` is untouched. Tests prove that a noted deny
    on a write, and on an org with `require_passkey` and nothing enrolled, still succeeds without a
    challenge.
- `batch_decide` is unchanged. It rejects `note`/`intent` keys with 400, so nobody builds on an
  accidental pass-through. The UI does not send deny batches there (above).
- `routes_org_stepup` (the IdP fallback) accepts only approving results and is unchanged.

### Audit (ADR 0083)

The audit log records **that** feedback was given, not the text:

- `AuditEntry` schema v6 adds `deny_intent: str = ""` and `deny_note_chars: int = 0`. Follow the
  v5 precedent, `2abe1818`: the history comment, `CURRENT_SCHEMA_VERSION`, defaults so older rows
  still load, and `test_audit_log.py`. Also check the Excel export and `audit_forwarding.py` for any
  field list the new keys must join (v5 needed none).
- They are set on `"rejected"` rows, and on the `"expired"` row `pop_expired_ledger_events` writes
  for a deny nobody collected. That second case is the normal one when the agent learns of the
  deny through `await_approval` and never re-issues.
- **The note text is never written to the audit log, any log line, or the approvals SSE
  payload.** The audit log is kept for years, HMAC-chained so it cannot be edited later, and
  forwarded off-host to a SIEM (ADR 0071). A note is the user's conversational text to their own
  agent. It is not a record of what the system did, and it may hold personal data. Running
  `pii_detector` over it before storage was considered and rejected. Masking gives half a sentence
  of no forensic value. Storing it at all would give an org admin a new window into what employees
  say to their agents, and an audit log has no business opening one.

### UI

The design system from org-mode-mobile (ADRs 0078 and 0079) applies throughout:

- no `@media` width queries;
- no colour literals;
- 44px targets;
- `textarea.field` and `.button` from `resources/design/app.css`;
- the card's action hierarchy unchanged (the shared rules' tenth rule).

A chip component, if one is needed, goes into `app.css` with every state, and into the style guide
`scripts/render_ui_review.py` builds.

**Approval card** (`approval_window_html.py`, `resources/approval_window/styles.css`):

- **Deny stays one tap**, and so does Escape. Next to Deny in `.pf-btn-row-left`, add a
  link-styled control, *Deny with a note…*. It uses the same `.pf-btn-link` treatment as *Always
  allow*, so it never outranks Deny or Allow once. It carries `data-pf-note-open`, not
  `data-pf-action`, so `enableButtons()`'s gating does not treat it as a decision.
- Activating it expands a panel directly above the button row (`<section id="pf-deny-note"
  hidden>`), which contains:
  - a kicker: *Tell the agent why, or what to do instead (optional)*;
  - the four intent chips, as a `radiogroup`. Choosing one is optional, and it can be cleared;
  - a `textarea.field` with `maxlength="500"`, a live `0 / 500` counter (`aria-live="polite"`),
    and placeholder *e.g. Send it only to Anna, not the whole team.*;
  - help text: *Only the agent that made this request receives this note. It is not kept in the
    audit log.*;
  - *Cancel* (`button secondary`, closes the panel and keeps the text) and *Deny and send*
    (`button danger`, with `data-pf-action="deny"` plus `data-pf-note-submit`).
- `_JS.post()` adds `note` and `intent` to the resolve payload only when they are non-empty. The
  shims forward them unchanged. After a noted deny the list's toast reads *Denied. Your note will
  go to the agent.* (a new `_DENIED_WITH_NOTE_MESSAGE` next to `_DENIED_MESSAGE`).
- Keyboard:
  - Escape while the panel is open closes the panel and makes no decision. The next Escape
    denies as today, so the safe direction stays one keypress away and typed text is never
    thrown away by a reflex.
  - Enter and Space inside the textarea type, never decide.
  - Ctrl/Cmd+Enter in the textarea submits *Deny and send*. Deny is the safe direction, so a
    shortcut is acceptable there (it never can be for Allow once).
- The note is never rendered back into any page. The server never echoes it into HTML or SSE, so
  there is no XSS surface to guard.

**Approval list** (`approval_list_html.py`):

- The row's Deny stays one tap with no note. The list's premise is that denying needs no context
  (its module docstring). Someone who wants to explain opens the card with *Review*.
- Next to *Deny selected*, add *Deny selected with a note…*. It opens the same panel (the same
  chips, textarea, counter and help text), whose submit reads *Deny N and send*. The `denySelected`
  fan-out then sends `{result:'deny', note, intent, csrf}` on every per-id POST. The help text adds
  *The same note goes to every selected request.*
- Build the panel markup and its JS once, in a small shared module
  (`src/privacyfence/deny_note_html.py`: `panel_html(prefix, submit_label)` and `PANEL_JS`), so
  that both documents use one implementation. The card is a self-contained document, so it inlines
  both, and the list does too.

**Org mode and mobile specifically:**

- Web push fires only when an approval is created and carries a count (ADR 0081). It never sees a
  decision, so a note cannot reach Apple's, Google's or Mozilla's push services. p1 adds a test that
  pins `web_push`'s payload builder to its current keys, so a future change cannot quietly add
  decision data.
- On a phone the panel opens inside the card's own scroll region. The on-screen keyboard must not
  cover *Deny and send*. Check this in the phone harness at 393×852 and 320px, and on a real iPhone
  and a real Android device (manual list below).

## Not in scope

- **Confirm dialogs' Cancel** (PII confirmation, rule confirmation, `propose_policy_change`'s
  confirm). They keep their static `"Request denied by user"`, updated only to the new default
  text. They could get a note later through the same `deny_feedback` API.
- **Notes on approve.** An "Allow, but …" note would be an instruction channel riding along with
  released data, and it deserves its own issue.
- **Unattended and policy denials** are not human decisions, so they keep their text.
- **Showing the note to the user again** (in the list, in history, or in the audit viewer). It is
  deliberately not stored (ADR 0083).
- **Editing a note after denying.** The agent may already hold the denial.

## ADRs

The numbers are pre-assigned on the assumption that org-mode-mobile keeps 0078–0081. If `main` has
taken either number by the time the final PR opens, the orchestrator renumbers both in its last
merge of `main`.

- **ADR 0082: A human's deny note reaches the agent as delimited, sanitized user text.**
  - It amends the rule, stated in `GateDeniedError`'s docstring and relied on by
    `safe_errors.public_message()`, that `GateDeniedError` carries only static text.
  - Decided:
    - only `GateDeniedError.by_user()` may carry user text;
    - the text is the deciding human's own words, never connector or request data;
    - it is sanitized, capped at 500 characters and JSON-quoted after a static label;
    - `redact_secrets` still applies;
    - the await path uses the reserved `denial_feedback` key.
  - Rejected:
    - truncating over-long notes;
    - turning the status value into an object;
    - a separate MCP tool to fetch feedback (an extra round trip the agent would forget);
    - server-side enforcement of `stop`;
    - letting a note ride on an approve.
- **ADR 0083: The audit log records that a deny had feedback, never the feedback text.**
  - Decided: schema v6 with `deny_intent` and `deny_note_chars`.
  - Rejected:
    - storing the text (retention, SIEM forwarding, admin visibility into employee-agent
      conversation);
    - storing it PII-masked (no forensic value).

## Phases

The phases run in order, with no parallel wave: p3 reuses the panel p2 builds, and p2's look is a
human gate.

```
p1-core      backend: model, sanitizer, messages, routes, await, audit v6, ADRs   (no UI)
p2-card      the note panel on the approval card, shared panel module             ── you sign off here
p3-list      "Deny selected with a note…" on the list
p4-retire    docs, screenshots, plan deleted
```

## Implementation manifest

```yaml
plan_slug: deny-with-feedback
feature_branch: feature/deny-with-feedback
tracking_issue: 640
max_parallel: 1
verify_after_merge:
  - python3 -m pytest tests/unit/test_gate.py tests/unit/test_approvals.py tests/unit/web/test_routes_approvals.py tests/unit/web/test_routes_org_approvals.py tests/unit/web/test_mcp_dispatch.py -q
  - python3 -m pytest tests/integration/test_browser_smoke.py -q
screenshots_after_merge: python3 scripts/render_ui_review.py --out test-results/ui-review
final_checks:
  - "grep -rn 'Request denied by user\"' src/privacyfence/gate.py finds no bare human-deny raise; every human deny goes through GateDeniedError.by_user"
  - no log call, audit field, SSE payload or web_push payload contains a deny note (grep for deny_feedback/.note usages and read each)
  - ADRs 0082 and 0083 (or their renumbered successors) exist, are Accepted and are listed in docs/adr/README.md
  - CHANGELOG.md has [Unreleased] entries and no version heading
  - docs/deny-with-feedback-plan.md is deleted and nothing links to it
manual:
  - "Real iPhone (Safari and the installed Home Screen app) and real Android Chrome, org mode: open a card, Deny with a note using the on-screen keyboard (Deny and send stays reachable), then Deny selected with a note on two requests; the agent's reply quotes the note"
  - "Local mode companion window on macOS and Windows: Escape closes the open panel, and a second Escape denies; Ctrl/Cmd+Enter sends"
  - "Claude Desktop through the mcpb shim: the sync denial text and await_approval's denial_feedback both arrive intact (the shim is schema-agnostic, so this is a check, not a change)"

phases:
  - id: p1-core
    title: Deny feedback end to end on the server, no UI
    depends_on: []
    brief: |
      0. Gate. Confirm feature/org-mode-mobile's work is on origin/main and inside a pushed release
         tag: find its merge into main (git log origin/main --merges --grep 'org-mode-mobile'), then
         `git tag --contains <sha> --list 'v*'` must be non-empty. If not, stop with
         PHASE-REPORT status=blocked and say so. Do not start on a pre-mobile base.
      Read the plan's "Design" section in full; it is the spec. Then:
      1. New src/privacyfence/deny_feedback.py: INTENTS (value -> chip label, guidance text, exactly
         as the plan's table), MAX_NOTE_CHARS = 500, DenialFeedback, sanitize_note, parse,
         denial_message, await_entry. Pure functions; mypy-strict (add a [[tool.mypy.overrides]]
         entry). Unit tests: each sanitization rule (NFC, CR/CRLF, Cc/Cf/Zl/Zp incl. bidi and
         zero-width, newline collapse, trim), exactly 500 accepted, 501 rejected (counted after
         sanitization), a note made only of stripped characters becomes empty, unknown intent
         rejected, non-string note rejected, JSON quoting of quotes/backslashes/newlines, message
         prefix is always "Request denied by user.".
      2. approvals.py: PendingApproval.deny_feedback set in answer() under first-answer-wins,
         ValueError for feedback on a non-deny result; registry.answer(feedback=);
         LedgerHit.feedback; consume_ledger copies it; registry.denial_feedback(id, principal_id=)
         with foreign-principal -> None. approval_ui.ApprovalUI.resolve and
         web_approval_ui.WebApprovalUI.resolve take feedback=.
      3. gate.py: GateDeniedError.by_user(feedback) classmethod; update the class docstring (the
         one exception to "static text", pointing to ADR 0082); _resolve_decision returns
         feedback as a sixth element (empty for interact(None) and the crash-to-deny branch);
         both human-deny raises in gated_call use by_user. propose_policy_change's cancelled
         confirm uses by_user(DenialFeedback()) so it gets the new default text. Unattended
         denials unchanged. Update safe_errors.py's module docstring to match.
         Fix the tests that assert the old exact string (grep 'Request denied by user'), and use
         startswith where the test only cares that the call was denied.
      4. mcp_dispatch.await_approval: add the reserved "denial_feedback" key only when a denied id
         has non-empty feedback; return type becomes dict[str, Any]. mcp_tools.AWAIT_APPROVAL_TOOL
         description: the two wording changes in the plan. Test that approval ids are uuid4 hex
         and can't equal "denial_feedback".
      5. web/routes_approvals.py decide: parse note/intent per the plan (400 on invalid, 400 on a
         non-deny result carrying them), pass feedback to web_ui.resolve. batch_decide: 400 if
         note or intent is present. Tests in test_routes_approvals.py and
         test_routes_org_approvals.py: pass-through to the agent in both modes, over-long,
         control characters, feedback on accept rejected, CSRF still enforced first, a noted deny
         on a write never steps up (local, and org with require_passkey and nothing enrolled),
         foreign principal, double submit keeps the first feedback.
      6. audit_log.py: schema v6, deny_intent and deny_note_chars, following 2abe1818's pattern
         (history comment, CURRENT_SCHEMA_VERSION, old-row defaults, test_audit_log.py; check the
         Excel export and audit_forwarding.py for field lists).
         gate._audit sets them on "rejected" rows; pop_expired_ledger_events sets them on the
         "expired" row for an uncollected deny. Test that no audit field, no log record (caplog)
         and no approvals SSE payload contains the note text.
      7. web_push: a test pinning the payload builder's keys, so decision data can never be added
         to it without a failing test.
      8. Integration: extend tests/integration/test_deferred_approval_round_trip.py with a
         deny-with-note round trip: the pending result, a POST of a deny with a note, then
         await_approval shows denial_feedback and the re-issued call raises the same message.
      9. Write ADRs 0082 and 0083 from the plan's "ADRs" section; add both to docs/adr/README.md.
         docs/tools-reference.md: document the denial text and denial_feedback.
         CHANGELOG [Unreleased]: one entry for the agent-visible behaviour (clearer default text,
         and feedback when given).
    acceptance:
      - gate step 0 checked and quoted in the report
      - a deny POSTed with note+intent reaches the agent on the sync path, the await path and a re-issue inside the ledger TTL (tests named in the report)
      - plain deny returns the new default text starting "Request denied by user."
      - note never in audit, logs, SSE or push (tests named)
      - deny with a note never triggers step-up (tests named)
      - ADRs 0082 and 0083 written and indexed

  - id: p2-card
    title: The note panel on the approval card
    depends_on: [p1-core]
    human_gate: true
    brief: |
      Read "UI" in the plan. The p1 routes already accept note/intent; this phase only adds the UI.
      1. New src/privacyfence/deny_note_html.py: panel_html(prefix, submit_label) and PANEL_JS,
         which handle the chips (a radiogroup that can be cleared), the counter, Cancel, and a
         getFeedback() that returns {note, intent} with empty values omitted. No inline styles and
         no colour literals; use app.css's field/button classes. If chips need a component, add
         it to resources/design/app.css with hover, focus-visible, checked, disabled and dark
         states and 44px targets, and add it to render_ui_review.py's style guide.
      2. approval_window_html.py: the "Deny with a note…" control next to Deny
         (.pf-btn-link, data-pf-note-open, starts aria-disabled like the other controls and is
         enabled by enableButtons), the panel above the button row, and _JS changes: post()
         includes feedback for data-pf-note-submit; the Escape, Enter/Space and Ctrl/Cmd+Enter
         behaviour exactly as the plan says. The action hierarchy is unchanged: assert in
         test_approval_window_html.py that Deny, Allow once and Always allow keep their classes
         and order, both for one candidate and for 2+ candidates.
      3. routes_approvals.py: _DENIED_WITH_NOTE_MESSAGE and the shims' toast choice (a deny whose
         payload has a note or intent). Both shims already forward extra keys; add a unit test
         that proves it for each.
      4. Browser tests in tests/integration/test_browser_smoke.py, local and org_server, desktop
         and the phone harness (393x852 and 320px): open the panel, choose a chip, type, and see
         the counter; Deny and send goes through, with the agent-side message asserted through the
         registry; Escape closes, then Escape denies; Enter in the textarea does not decide;
         Ctrl+Enter sends; with the textarea focused and the viewport shrunk to stand in for the
         keyboard, the submit button can be scrolled into view; no horizontal overflow; 44px
         targets; the WIDE and NARROW cards, and the card inline in a list row.
      5. docs/approvals-and-policy.md: the card section describes the note. CHANGELOG [Unreleased]
         entry for the card.
      6. Run render_ui_review.py and put before/after PNGs of a card with the panel closed and
         open (393px and 1280px, light and dark) in the report. That is what the human gate
         reviews.
    acceptance:
      - plain Deny and Escape unchanged (existing tests untouched and green)
      - action hierarchy assertions pass for 1 and 2+ candidates
      - phone-harness cases pass at 393 and 320 with no xfail
      - screenshots in the report

  - id: p3-list
    title: "Deny selected with a note…" on the approval list
    depends_on: [p2-card]
    brief: |
      Read "UI" → "Approval list" in the plan. Reuse deny_note_html from p2 unchanged; if it needs
      a change, make it backwards-compatible and say why in the report.
      1. approval_list_html.py: "Deny selected with a note…" next to "Deny selected", enabled and
         disabled by the same selection count; the panel with the submit label "Deny N and send"
         and the extra help line; denySelected() sends the feedback on every per-id POST. The row
         Deny does not change. Update the module docstring's action-order notes.
      2. Unit tests in test_approval_list_html.py; browser tests (local and org, desktop and phone):
         two selected requests denied with one note, and both agents' messages carry it; a 409
         for one id still counts as done and the other still gets the note; the row Deny still
         sends no note.
      3. docs/approvals-and-policy.md: the list section. CHANGELOG [Unreleased]: fold into p2's
         entry rather than adding a second one.
    acceptance:
      - batch deny with note covered in unit and browser tests, both modes
      - row Deny unchanged (existing tests untouched and green)

  - id: p4-retire
    title: Docs, screenshots, plan retired
    depends_on: [p3-list]
    brief: |
      1. Check that every final_checks item in the manifest holds, and fix any that does not.
      2. Regenerate the documentation screenshots under docs/images/screenshots/ with
         scripts/qa_readme_screenshots.py (fake clients, temporary data directory, synthetic data
         only; as in a7318069). The card now shows "Deny with a note…" in its button row.
      3. docs/security-and-compliance.md: one paragraph on what a deny note is, who can write it,
         where it goes, and that it is not stored, linking ADRs 0082 and 0083.
      4. Delete docs/deny-with-feedback-plan.md. The PR description states that the plan's
         decisions are extracted into ADRs 0082 and 0083 and that it made no others.
    acceptance:
      - final_checks all hold
      - plan file deleted; no dangling links (grep)
```
