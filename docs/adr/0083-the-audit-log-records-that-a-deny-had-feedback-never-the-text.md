# ADR 0083: The audit log records that a deny had feedback, never the feedback text

## Status

Accepted — 2026-09-27. Adds audit schema version 6. Companion to
[ADR 0082](0082-a-humans-deny-note-reaches-the-agent-as-delimited-sanitized-user-text.md).

## Context

ADR 0082 lets the person who denies an approval send the agent an intent and a free-text note. The
audit log is where every decision is recorded, so the question is what it keeps of that feedback.

The audit log is kept for years, is HMAC-chained so an entry cannot be edited later, and can be
forwarded off-host to a SIEM ([ADR 0071](0071-audit-log-integrity-is-a-keyed-hash-chain-plus-off-host-forwarding.md)).
In org mode an administrator can read it. A note is the user's conversational text to their own
agent. It is not a record of what the system did, and it may hold personal data.

## Decision

**`AuditEntry` schema v6 adds `deny_intent` (one of the intent values, or `""`) and
`deny_note_chars` (the sanitized note's length, or `0`). The note's text is never written to the
audit log, any log line, the approvals SSE payload or a web push.**

- Both fields are set on the `"rejected"` entry, and on the `"expired"` entry
  `pop_expired_ledger_events` produces for a deny no call ever collected. That second case is the
  normal one when the agent learns of the deny through `privacyfence_await_approval` and never
  re-issues.
- Every other entry, a plain deny and every entry written before v6 carry `""` / `0`. Older rows
  load with those defaults, and their hashes still verify, because each entry's hash covers only
  the keys it was written with.
- The Excel export appends two columns, `Deny Intent` and `Deny Note Length (chars)`, after the
  existing ones. `audit_forwarding.py` forwards the entry as a whole and has no field list.

This answers "did the person give a reason, and of what kind" for a reviewer, without making the
audit log a transcript.

## Alternatives considered

- **Storing the text.** Rejected: it would sit in a years-long, uneditable record, be forwarded to a
  SIEM, and give an org admin a new window into what employees say to their agents. An audit log
  has no business opening one.
- **Storing it PII-masked** (running `pii_detector` over it first). Rejected: masking leaves half a
  sentence of no forensic value, and still stores the rest.
- **Recording nothing.** Rejected: whether a deny came with guidance is a useful fact about how
  approvals are used, and the intent is a closed vocabulary that carries no personal data.

## Consequences

- The note cannot be shown back to anyone later, in the list, history or audit viewer. That is
  deliberate; showing it again would need somewhere to store it.
- A consumer of the audit log that reads by column index keeps working; one that validates a fixed
  key set must accept two new keys at `schema_version` 6.

## Verification

- `tests/unit/test_audit_log.py::TestDenyFeedbackFields`: schema version, defaults, v5 rows loading,
  a mixed v5/v6 chain verifying, tamper detection, the Excel columns.
- `tests/unit/test_gate.py::TestDenyFeedback`: the fields on `"rejected"` and `"expired"` rows, and
  the note absent from the audit file and from every log record.
- `tests/unit/web/test_routes_org_approvals.py::TestDenyWithFeedback::test_the_approvals_stream_never_carries_the_note`
  and `tests/unit/test_web_push.py::TestPayloadIsMinimal`: nothing in the SSE stream or a push.

## Related

- [ADR 0082](0082-a-humans-deny-note-reaches-the-agent-as-delimited-sanitized-user-text.md).
- [ADR 0071](0071-audit-log-integrity-is-a-keyed-hash-chain-plus-off-host-forwarding.md): the chain
  and forwarding.
- [ADR 0081](0081-org-mode-sends-a-count-only-web-push.md): why a push never sees a decision.
- `src/privacyfence/audit_log.py` (`CURRENT_SCHEMA_VERSION`), `src/privacyfence/gate.py` (`_audit`).
