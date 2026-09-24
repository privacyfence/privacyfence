# ADR 0043: The marker's recorded owner is written once and never rewritten by adding another account

## Status

Accepted — 2026-09-24. Implemented in all three separation scripts' marker writers
(`scripts/linux_privilege_separation.sh` and `scripts/macos_privilege_separation.sh`'s
`write_marker`, `scripts/windows_privilege_separation.ps1`'s `Write-Marker`).

Amends [ADR 0008](0008-one-principal-per-os-user.md): it states the invariant that ADR's
principal mapping already relied on.

## Context

The privilege-separation marker's `owner_user` field names the install's owner.
`privilege_separation.owner_uid()`/`owner_sid()` read it, and `web/control_channel.py`'s
`principal_id_for_peer()` maps a control-channel peer that matches it to `LOCAL_PRINCIPAL`. That
principal holds the install's original data: connector tokens, policy and the audit log. Every
other account in the service group gets its own `os-<uid>`/`os-<sid>` principal (ADR 0008).

`enable --for-user <name>` is how a second account is added to the service group. On all three
platforms it ended by rewriting the marker, and each script's marker writer recorded the account
that run had just resolved. On Linux the writer kept a recorded owner only when the run had
resolved *no* account, which is the machine half's case, not `--for-user`'s. On macOS and Windows
it always wrote the resolved account, or `""` when there was none. So adding a second account
moved the owner's principal to that account, and the original owner was mapped to a new, empty
`os-<uid>` principal. The macOS and Windows machine halves could also clear a recorded owner when
they re-ran with nobody signed in.

Found during phase L4 of the product cleanup ("Found, not fixed" in privacyfence/privacyfence#672).

## Decision

1. Every marker writer keeps a non-empty `owner_user` it finds in an existing marker, whatever
   account the current run resolved. It records the resolved account only when the marker has no
   owner yet: a first `enable`, or the first `enable --for-user` on an install whose machine half
   ran with nobody to add.
2. Nothing else changes the owner. `uninstall --purge` (ADR 0042) removes it by removing the
   marker. Changing the owner of an existing install means purging it and enabling again.
3. The daemon does not add a second check of its own. It only reads `owner_user` and never writes
   it. Only the separation scripts, running as root or Administrator, write the marker. The one other
   account that can write it is the service account, which already holds every principal's data.
   Either could equally replace a daemon-side record of the owner, so a second copy would add no
   protection.

## Alternatives considered

- **Let `--for-user` replace the owner when asked (a `--new-owner` flag).** Rejected: nothing
  needs it, and it would give the owner's data to a different account without moving or wiping
  it. Purge-and-enable already covers a real change of owner.
- **Pin the owner in the daemon at first start and refuse a marker that disagrees.** Rejected for
  the reason in decision 3. It would also turn a hand-repaired marker into a startup failure.
- **Repair already-affected installs automatically.** Rejected: a marker naming a second account
  cannot be distinguished from one whose first account was that account. The changelog tells
  users that `status` shows the recorded owner.

## Consequences

- Adding accounts is now safe to repeat in any order. The first recorded owner keeps
  `LOCAL_PRINCIPAL`, and every other account gets its own principal, as ADR 0008 intended.
- A macOS or Windows `enable` with nobody signed in no longer clears the owner. That rule was
  already true on Linux (ADR 0003 decisions 3 and 5).
- An install whose marker a pre-fix `--for-user` already rewrote stays that way until a person
  corrects it.

## Verification

- `tests/unit/test_privilege_separation.py`'s `TestForUserKeepsTheRecordedOwner` runs each script's
  real `enable --for-user` and marker writer, under bash for the POSIX scripts and under PowerShell
  where one is on `PATH` (the `platform-windows` job). It reads the result back through
  `privilege_separation.separation()`: a second account leaves the owner unchanged, a first
  `--for-user` on a machine-only install records it, and a machine half with no owner keeps it.
- `tests/integration/test_deb_packaged_lifecycle.py`'s unattended-install test adds a real second
  account to the installed `.deb` with `enable --for-user` and asserts the recorded owner is
  unchanged.

## Related

- [ADR 0003](0003-separated-installs-only.md), [ADR 0008](0008-one-principal-per-os-user.md),
  [ADR 0042](0042-uninstall-replaces-disable.md).
- privacyfence/privacyfence#672 (where the bug was found).
