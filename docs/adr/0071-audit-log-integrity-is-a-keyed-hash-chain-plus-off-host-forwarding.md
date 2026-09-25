# ADR 0071: Audit-log integrity is a keyed hash chain on the host, plus off-host forwarding

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-10 in
`docs/security-remediation-plan.md` Phase 3 row 3.6, read it with
`git show ba1ec76e^:docs/security-remediation-plan.md`, and implemented in `52e9c43a`, merged in
[#270](https://github.com/privacyfence/privacyfence/pull/270)). Implemented.

## Context

The audit log is the record of every gate decision, approval and security event: weekly JSON Lines
files (`YYYY-Www.jsonl`) under each principal's `logs/audit/` directory. Before `52e9c43a` nothing
showed whether a line had been edited, inserted or removed after it was written.

Two kinds of tampering matter, and one mechanism cannot stop both. A party with only partial access
(a bug in an export or backup path, a process that can write the `.jsonl` files but not read the
rest of the directory) should be caught on the host. A fully privileged local party (root, the
service account on a separated install, or any process running as the user on a non-separated one)
can read whatever the daemon reads. On one host, nothing the daemon can verify stops that party from
rewriting the log and everything it is checked against.

## Decision

Integrity has two layers, and each is described in terms of what it actually stops:

1. **On the host, a keyed hash chain.** `AuditLogger` stamps every entry with `prev_hash` (the
   previous entry's `entry_hash`, or `GENESIS_HASH` at the start of a chain) and `entry_hash`, an
   HMAC-SHA256 over the entry's canonical fields (`_hash_canonical`). The key is 32 random bytes in
   `.audit_chain.key`, generated on first use by `_load_or_create_chain_key`, one per audit directory,
   stored next to the `.jsonl` files it protects with owner-only permissions, and never written into
   the log. The chain position survives restarts in `.audit_chain_state.json`.
   `AuditLogger.verify_chain()` and `scripts/verify_audit_log.py` (which reuses `AuditLogger` and
   reads the same key from the same directory) detect any edit, insertion, removal or reordering
   made by something that cannot read the key.
2. **Off the host, forwarding.** In organization mode, `org_config.json`'s `audit_forwarding`
   section (`org_mode.AuditForwardingConfig`, off by default) streams each entry to syslog or an
   HTTPS/JSON collector (`audit_forwarding.AuditForwarder`). This is the defence against a
   privileged local party: a copy that has already left the trust boundary cannot be rewritten
   from inside it. Forwarding is best-effort and off the decision path; the local `.jsonl` file stays
   the authoritative record and the chain is always on, whether forwarding is configured or not.

Local mode has no forwarding. There, the chain is the whole mechanism, and it is documented as
detection against anything that cannot read the key, nothing stronger.

## Alternatives considered

- **Hold the chain key outside the log directory** (a different directory, the OS keyring, an
  environment variable). The daemon has to read the key to append every entry, so it must sit
  wherever the daemon's own account can read it. The party the chain cannot stop, a local
  administrator or the daemon's account, can read it there just as easily. Moving the key relocates
  the secret without taking it away from that party, and it costs `verify_audit_log.py` and every
  per-principal directory a second location to find and keep in step. The chain claims only what it
  can deliver on one host, and forwarding covers the privileged tamperer.
- **An unkeyed hash chain (plain SHA-256).** Anyone who can write the file can recompute it, so it
  catches accidental corruption and nothing deliberate. The HMAC key costs one file.

## Consequences

- A verified chain proves that nothing without the key changed the log. It does not prove that root
  or the service account left the log untouched. `docs/security-and-compliance.md`'s "Audit log
  integrity" section says this to the reader, and `_load_or_create_chain_key`'s docstring says it in
  the code.
- An organization that needs evidence which survives a compromise of the daemon's host must turn on
  `audit_forwarding`. A local-mode user who needs that has no built-in option.
- A forwarded entry can be dropped (full queue, collector down) without affecting the local record.
  The forwarded stream is a copy, not the log of record.
- Losing `.audit_chain.key` makes the existing weeks unverifiable. A fresh key starts a new chain.

## Verification

- `tests/unit/test_audit_log.py`: `TestHashChain` (genesis, chaining, altered/reordered/malformed
  entries detected, key and state persisting across instances) and `TestChainKeyPersistenceFailures`.
- `tests/unit/test_verify_audit_log.py`: exit codes 0/1/2 for an intact chain, a tampered chain,
  and a missing key.
- `tests/unit/test_audit_forwarding.py`: the forwarder's transports and its drop-on-failure behavior.

## Related

- [ADR 0016](0016-org-config-bundle-hash-log-and-signing.md): the bundle-hash log writes into this
  audit trail, and its consequences note that forwarding is where startup hashes can be compared.
- [ADR 0025](0025-no-certified-security-framework.md): the local audit trail is part of the risk
  profile that ADR presents to adopting organizations. This ADR states what that trail can and cannot
  prove.
