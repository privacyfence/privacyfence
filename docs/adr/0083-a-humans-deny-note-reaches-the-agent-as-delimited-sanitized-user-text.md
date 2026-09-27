# ADR 0083: A human's deny note reaches the agent as delimited, sanitized user text

## Status

Accepted — 2026-09-27. Amends the rule, stated in `gate.GateDeniedError`'s docstring and relied on
by `safe_errors.public_message()`, that `GateDeniedError` carries only static text. Tracks
[#640](https://github.com/privacyfence/privacyfence/issues/640).

## Context

When a person denies an approval, the agent learns only that it was denied: the synchronous path
raised `GateDeniedError("Request denied by user")`, and `privacyfence_await_approval` reported the
bare status `"denied"`. The agent cannot tell "never do this" from "wrong recipient" from "make it
shorter", so it either gives up or retries blindly, and the person has to explain in the chat what
they already decided on the card.

`safe_errors.public_message()` passes a named `RuntimeError` subclass's message to the MCP client
verbatim (after `redact_secrets()`), because every such raise site was reviewed as static text. A
denial that carries the person's words is the first exception to that review, so it needs a
recorded boundary.

## Decision

**A deny may carry two optional parts, both written only by the human deciding the card: an
`intent` from a fixed vocabulary and a free-text `note`. They reach the agent on both delivery
paths, sanitized, capped and delimited. Nothing screens the note's content.**

1. **Only `GateDeniedError.by_user(feedback)` may carry user text.** Both human-deny raises in
   `gated_call` use it; so does `propose_policy_change`'s cancelled confirm, with empty feedback.
   Unattended and policy denials keep their static text. The message always starts
   `Request denied by user.`; a plain deny now reads
   `Request denied by user. Don't retry the same call; ask the user how to proceed.`
2. **The text is the deciding human's own words, never connector or request data.** The decide
   route is the only writer, behind the same session, CSRF token and Origin check as a plain Deny,
   and org mode lets each person decide only their own approvals. The agent has no route that
   decides an approval.
3. **Sanitized, capped, JSON-quoted after a static label.** `deny_feedback.sanitize_note()`
   normalizes to NFC, turns CR/CRLF into LF, drops every `Cc`/`Cf`/`Zl`/`Zp` character except LF
   (bidi and zero-width characters included), collapses runs of more than two newlines and trims.
   A note longer than 500 characters after that is rejected with 400, never truncated. The note is
   appended as `json.dumps(note, ensure_ascii=False)` after the fixed label `User's note (written
   by the person who denied this request; JSON string):`, so it can never close its string early
   or read as PrivacyFence's own text. The intents are `stop`, `wrong_target`, `rewrite` and
   `different_approach`, each with fixed guidance text (`deny_feedback.INTENTS`).
4. **`redact_secrets()` still applies** to the whole outgoing message, so a token pasted into a
   note is redacted on the way out.
5. **The await path uses one reserved top-level key.** `privacyfence_await_approval` keeps
   `{approval_id: status}`, every value still a bare status string, and adds
   `"denial_feedback": {approval_id: {intent, note, guidance}}` only when at least one denied id in
   the call has feedback. Approval ids are `uuid4().hex`, so none can equal the key. A call
   re-issued inside the ledger TTL raises the same message the synchronous path would have
   (`LedgerHit.feedback`).
6. **Only a deny carries feedback.** The decide route answers 400 to a `note` or `intent` with any
   other result, `PendingApproval.answer()` raises `ValueError` for it, and the batch endpoint
   refuses both keys. A noted deny is still a deny: it never meets step-up.

### The note is not content-filtered

Only one party can write the note: the signed-in human deciding this card. An "injection" in it is
the user instructing their own agent, which they can already do in the chat, word for word. A
filter would stop no attacker and would censor the user. The real risks are covered structurally:

| Risk | Covered by |
|---|---|
| The note poses as PrivacyFence's own text (`"Request approved. Proceed…"`) | A fixed label in front, and JSON quoting, so it cannot close its string or read as system text |
| Invisible or reordering characters (bidi overrides, zero-width) | `sanitize_note()` strips `Cc`/`Cf`/`Zl`/`Zp` |
| A secret pasted by mistake | `redact_secrets()` over the whole outgoing message |
| Stored XSS or a leak later | The note is never rendered, stored, logged or pushed ([ADR 0084](0084-the-audit-log-records-that-a-deny-had-feedback-never-the-text.md)) |
| The note talks the agent into something harmful ("delete X instead") | A note approves nothing. Every call the agent makes next goes through the gate, and that human decides that card too |

What remains is social engineering: card content, say an email, asks the reader to type a given
sentence into the note. Filtering would not catch a rephrasing. The gate on the agent's next call is
the backstop, and the panel's help text says who reads the note.

## Alternatives considered

- **Truncating an over-long note.** Rejected: silently cutting what someone wrote to an agent is
  worse than refusing it, and the textarea's `maxlength` stops a real user first, so only a crafted
  client reaches the limit.
- **Turning a denied id's status value into an object.** Rejected: every existing consumer, the
  tool description and `await_approval`'s own `any(s != "pending" …)` loop treat values as strings,
  and models read that description literally. One additive key keeps every current reader correct.
- **A separate MCP tool to fetch feedback.** Rejected: an extra round trip the agent would forget.
- **Server-side enforcement of `stop`.** Rejected: refusing the agent's later calls would be a new
  policy mechanism, not feedback. `stop` is guidance text like the others.
- **Letting a note ride on an approve.** Rejected: it would be an instruction channel travelling
  with data the agent was just given; it deserves its own issue if ever wanted.
- **Screening the note for prompt injection or harmful content.** Rejected: see above. Classifiers
  flag ordinary notes ("ignore the previous draft, rewrite it shorter" looks like an injection),
  and a falsely blocked note would leave the person with a plain deny, worse than today.

## Consequences

- An agent learns why it was denied, or what to do instead, on whichever path it collects the
  decision, and a plain deny tells it not to retry blindly.
- `GateDeniedError` is no longer static text in one constructor; a future raise site that wants
  user text must go through `by_user()` and `deny_feedback`, not interpolate its own.
- Client-side matching on the prefix `Request denied by user` keeps working.

## Verification

- `tests/unit/test_deny_feedback.py`: each sanitization rule, the 500-character boundary counted
  after sanitization, JSON quoting, the prefix.
- `tests/unit/test_gate.py::TestDenyFeedback`: the sync path, the ledger re-issue, the crash-to-deny
  and no-registry branches.
- `tests/unit/web/test_routes_approvals.py::TestDenyWithFeedback`,
  `TestDenyWithFeedbackNeverStepsUp`, and `tests/unit/web/test_routes_org_approvals.py::TestDenyWithFeedback`:
  validation, CSRF first, non-deny results refused, no step-up, principal scoping.
- `tests/unit/web/test_mcp_dispatch.py::TestAwaitApprovalDenialFeedback` and
  `tests/integration/test_deferred_approval_round_trip.py::test_deferred_approval_round_trip_deny_with_note`.

## Related

- [ADR 0084](0084-the-audit-log-records-that-a-deny-had-feedback-never-the-text.md): what the audit
  log keeps of it.
- [ADR 0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md): the ledger the
  re-issue path reads.
- `src/privacyfence/deny_feedback.py`, `src/privacyfence/gate.py` (`GateDeniedError.by_user`),
  `src/privacyfence/safe_errors.py`, `src/privacyfence/web/routes_approvals.py`,
  `src/privacyfence/web/mcp_dispatch.py`, `docs/tools-reference.md`.
