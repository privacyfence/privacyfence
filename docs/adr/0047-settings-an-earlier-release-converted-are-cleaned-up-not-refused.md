# ADR 0047: v1 policy sections an earlier release already converted are removed at startup, not refused

## Status

Accepted — 2026-09-25. Implemented.

Amends [ADR 0041](0041-only-the-current-install-layout-is-supported.md) decision 2 for one case:
a `settings.yaml` carrying the `migrated_to_policy_v2` marker.

## Context

ADR 0041 decision 2 refuses to start on a `settings.yaml` that still has `auto_accept_rules` or
`auto_accept_grants`, on the premise that no such file exists outside a tester's machine and that
the refusal is clear.

Neither held. Every stable release from 4.1 through 4.4 ran the v1 → v2 conversion on startup:
it wrote the `auto_accept:` section, set a top-level `migrated_to_policy_v2: true` marker, and
deliberately left the v1 sections on disk. The shipped `settings.yaml.example` seeded
`auto_accept_rules`, so every install that ever started one of those releases carries this
exact shape. Upgrading one to 4.5.0a3 on Windows produced a service that stopped within two
seconds of every start. The refusal was printed to stderr, which a Windows service does not have,
before the daemon had opened its log file, so the log, the Event Log and the companion app all
said nothing about why.

In that file the v1 sections are dead. Nothing has read them since the conversion ran: the
conversion itself stopped at the marker, and the v1 evaluator was already retired. Their rules
are the `auto_accept:` section's.

## Decision

When `settings.yaml` has a truthy `migrated_to_policy_v2` marker, `daemon_main.load_config`
removes `auto_accept_rules`, `auto_accept_grants` and the marker, writes the file back, and logs
one warning naming what it removed (`policy.store.drop_converted_v1_sections`). The daemon then
starts. If the rewrite fails, it starts anyway with the sections removed from memory, logs that,
and tries again on the next start.

A v1 section **without** the marker was never converted, so its rules are in no `auto_accept:`
section. That case is still refused exactly as ADR 0041 decision 2 says.

No rule is converted, re-read or compiled from a v1 section. This removes dead data and does not
bring back the conversion ADR 0041 removed.

## Alternatives considered

- **Keep refusing and fix only the invisible error.** The error is now visible too (the Event Log
  entry carries it). But the refusal would still stop every 4.1–4.4 install on its first 4.5
  start, telling people to recreate rules that are already in effect, and delete sections that
  change nothing. ADR 0041's reason for refusing (rules that would silently stop applying) does
  not apply when the rules already apply from `auto_accept:`.
- **Ignore the sections without rewriting the file.** The file would keep the sections forever,
  with a warning on every start. The Settings page's own writer would also carry them into every
  save.
- **Remove v1 sections whenever `auto_accept:` exists, marker or not.** A hand-edited file can have
  both without the conversion ever having run, and then the v1 rules were never carried over.
  The marker is the only evidence that the conversion did run.
- **Keep a backup of the removed sections.** The conversion already backed up the original file
  as `settings.yaml.bak` when it ran, and what is removed has changed no decision since.

## Consequences

- Upgrading from any 4.1–4.4 install to 4.5 needs no manual edit.
- `CONVERTED_V1_MARKER` is the one place the current code names the 4.1–4.4 marker. It is only
  ever deleted, never written.
- A startup refusal is recorded (`daemon_main.last_startup_error`) and the Windows service host
  puts it in its Event Log entry. This is not part of this decision, but it is what makes the
  remaining refusal case visible on Windows.

## Verification

- `tests/unit/policy/test_store.py::TestDropConvertedV1Sections`.
- `tests/unit/test_daemon_main.py::TestLoadConfig::test_v1_sections_an_earlier_release_converted_are_removed`
  and `::test_a_failed_rewrite_still_starts_with_the_sections_ignored`.
- `tests/unit/test_daemon_main.py::TestLoadConfig::test_v1_policy_section_is_refused_with_a_clear_error`:
  the unmarked case is still refused and the file left untouched.

## Related

- [ADR 0041](0041-only-the-current-install-layout-is-supported.md): amended by this ADR.
- [ADR 0004](0004-retire-the-v1-auto-accept-config-model.md): the conversion whose leftovers this
  removes.
- `eef54df9`: the commit that removed the conversion and made the leftovers fatal.
