# ADR 0176: A chain-less audit line is accepted only before the first chained line

## Status

Accepted — 2026-10-14. Implemented.
Amends ADR 0071. The verifier no longer accepts a chain-less line anywhere in a file.

## Context

ADR 0071 makes the audit log a keyed hash chain. `AuditLogger.verify_chain` skipped any line with an
empty `entry_hash` and restarted the chain after it, anywhere in the file, so a forged line with no
chain fields could be spliced between chained entries and the chain still verified. The script also
never compared the newest hash with `.audit_chain_state.json` or checked that one week's file
continues the previous week's, so a removed week or a cut-off tail went unreported.

## Decision

- **A chain-less line is accepted only while no chained line has been seen** in the file. Logs
  written before chaining existed sit at the top of a file and still verify. After the first chained
  line, a chain-less line fails verification at that line.
- **Genesis rule.** When chain-less lines precede the first chained line, that line's `prev_hash`
  must be `GENESIS_HASH`; otherwise verification fails at it. A chain starts at genesis exactly when
  no state file existed, which is when legacy lines precede it.
- **`scripts/verify_audit_log.py` checks continuity and the tail** unless `--week` is given. The
  first chained entry of each week must have a `prev_hash` equal to the previous week's last
  `entry_hash`. A week that starts at `GENESIS_HASH` is a legitimate restart (the state file was
  missing); it prints "starts a new chain segment" and does not fail. The newest entry's hash must
  equal `last_hash` in `.audit_chain_state.json`.
- **The tail check is limited.** The state file is not keyed, so someone who also edits it hides a
  cut-off tail; the docs say so.

## Alternatives considered

- **Treat every chain-less line as a break.** Rejected: logs written before chaining existed would
  fail verification forever.
- **Key the state file.** Not done; off-host forwarding (ADR 0071) is the answer to an attacker who
  can read the key.

## Consequences

- A forged chain-less line, a removed week and entries cut from the end are reported, except where
  the state file was edited too.
- A deliberate restart of the chain is reported as a note, not a failure.

## Verification

- `audit_log.AuditLogger.verify_chain`, `scripts/verify_audit_log.py`.
- `tests/unit/test_audit_log.py` and `tests/unit/test_verify_audit_log.py` (forged line after a
  chained entry, legacy top-of-file lines, genesis rule, removed week, cut tail, new chain segment).

## Related

- ADR [0071](0071-audit-log-integrity-is-a-keyed-hash-chain-plus-off-host-forwarding.md)
