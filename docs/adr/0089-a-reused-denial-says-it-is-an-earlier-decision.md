# ADR 0089: a reused denial says it is an earlier decision

## Status

Accepted — 2026-09-28, decided by the maintainer.
Amends [ADR 0083](0083-a-humans-deny-note-reaches-the-agent-as-delimited-sanitized-user-text.md)
for the text of a denial taken from the decision ledger. What the ledger replays, and for how long
([ADR 0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md)), is unchanged.

## Context

ADR 0073 lets a decided read replay from the decision ledger until `ledger_ttl` (5 minutes by
default), keyed on principal, connector, tool and canonical arguments. It replays denials as well as
approvals. In local mode every AI client is the same principal, so a new conversation that repeats
a read the user denied a couple of minutes earlier, such as `drive_download_file` on the same file,
is denied at once and no card is shown.

The agent received exactly the text of a fresh denial. The user saw a denial they had not just
given, and neither the user nor the agent could tell a remembered decision from a fault.

## Decision

- **A "deny" that `gate._resolve_decision` takes from the ledger (`consume_ledger`) carries an
  `EarlierDecision`**: how long ago the human decided it, and the ledger window
  (`LedgerHit.expires_at - decided_at`).
- **`deny_feedback.denial_message` puts one static sentence right after `DENIAL_PREFIX`**: the
  denial is not an error, the user was not asked again, the identical request was denied *N* ago,
  and PrivacyFence reuses a decision for identical requests for *M* after it is made. The rest of
  the message (default instruction, intent guidance, quoted note) is unchanged.
- **Only the ledger path adds it.** A denial the call collects while it waits (the hold window, or
  a coalesced wait on a live card) was decided for this request and keeps the ADR 0083 text. So does
  the registry-less path.
- The sentence is built only from numbers PrivacyFence computed. It carries no request or connector
  data, so `GateDeniedError` stays safe to pass to the client verbatim (`safe_errors.py`).

## Consequences

- The message still starts `Request denied by user.`, so client-side prefix matching keeps working.
- A deferred call re-issued to collect its own denial gets the sentence too, because it also comes
  from the ledger. The sentence is still true in that case: the user was not asked again for the
  re-issued call.
- `privacyfence_await_approval` is unchanged. It reports the status of an approval id the agent
  already holds, not a reused decision.

## Rejected

- **Stop replaying read denials.** This removes the surprise, but every retry of a denied read
  would prompt the user again. That is a policy change to ADR 0073, and it can still be made later
  on its own merits. Telling the agent does not rule it out.
- **Report the reuse only after the first collection (`ledger_collected`).** A decision the agent
  learned through `privacyfence_await_approval` is never marked collected. A new session's identical
  call would then get the plain text, which is the case this ADR exists for.
- **A separate error type or prefix for reused denials.** That breaks prefix matching on
  `Request denied by user.` for no benefit. The information fits in the message.

## Verification

- `src/privacyfence/deny_feedback.py` (`EarlierDecision`, `denial_message`),
  `src/privacyfence/gate.py` (`_resolve_decision`, `GateDeniedError.by_user`),
  `src/privacyfence/approvals.py` (`LedgerHit.expires_at`)
- Tests: `tests/unit/test_deny_feedback.py::TestDenialMessage`,
  `tests/unit/test_gate.py::TestDenyFeedback`
