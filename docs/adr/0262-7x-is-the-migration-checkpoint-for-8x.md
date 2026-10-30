# ADR 0262: 7.x is the migration checkpoint for 8.x

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/data_format.py`.
Relates to [ADR 0041](0041-only-the-current-install-layout-is-supported.md), [ADR 0047](0047-settings-an-earlier-release-converted-are-cleaned-up-not-refused.md), [ADR 0171](0171-auto-accept-rules-use-nine-verbs.md), [ADR 0172](0172-one-stored-auto-accept-rule-per-scope.md), [ADR 0228](0228-an-extra-scope-can-give-each-verb-its-own-predicate.md) and [ADR 0236](0236-a-scoped-token-holds-its-own-rules.md).

## Context

8.x should delete the code that upgrades data from older releases: the version-1 policy clean-up
([ADR 0047](0047-settings-an-earlier-release-converted-are-cleaned-up-not-refused.md)), the version-2 to
version-3 rule conversion ([ADR 0172](0172-one-stored-auto-accept-rule-per-scope.md)), tokens that
only name rules ([ADR 0236](0236-a-scoped-token-holds-its-own-rules.md), with the limit-pair
successors of [ADR 0228](0228-an-extra-scope-can-give-each-verb-its-own-predicate.md)), the legacy verb
aliases ([ADR 0171](0171-auto-accept-rules-use-nine-verbs.md)) and the import of operational settings
from the bundle. That is safe only if every install ran a release that finished all of them, for every
person, and left proof. Until now nothing recorded that, and in organization mode the clean-up ran
only when a person first signed in, so someone who never signed in during 7.x would keep old data.

## Decision

- **Every start migrates everyone's data eagerly.** `data_format.migrate_everything` walks every
  principal that has data (the local principal and every `users/<id>/`), under that principal's
  scope: it cleans leftover version-1 sections, converts version-2 rules, converts tokens that only
  name rules, and rewrites retired verb names to their canonical ones, changing only the `verbs`
  value of a rule and keeping a one-time `.bak` beside the file. A principal's problem
  is collected, never raised.
- **The marker.** When nothing is left to convert and the admin config exists,
  `data_format.json` is written in the data directory (`{"format": 7, "written_by", "completed_at"}`),
  so it travels with the data and with backups. A problem is logged as "Not ready for PrivacyFence 8",
  the marker is not written, and the next start tries again.
- **7.x refuses data from a newer release.** `check_not_newer` refuses to start when the marker's
  `format` is newer than 7, before anything reads or rewrites data, and names the release to install.
- **What 8.x does with it** (to be built there): at start, if the data directory holds data and the
  marker is missing or older than 7, refuse with a message to install 7.x first and start it once; a
  directory with no data is a fresh install. 8.x may delete the pre-7 converters. Its installers may
  read the marker to stop an upgrade before the old release is removed, so the marker's path and its
  `format` key are a stable contract. 8.x must keep reading pre-7 shapes fail-closed rather than
  trusting the marker, and its installers must not follow symlinks when reading it.
- **The Admin page shows readiness** ("Ready for PrivacyFence 8: yes", or the number and first three
  problems).

## Alternatives considered

- **Keeping every converter forever.** Rejected: it is the code 8.x exists to delete.
- **A per-file version check in 8.** Rejected: the question 8.x needs answered is install-wide, and a
  per-file check cannot tell "never signed in" from "migrated".
- **Migrating lazily per principal, as before.** Rejected: it leaves exactly the people who never
  return as the reason 8.x cannot drop the converters.

## Consequences

- An install with data it cannot convert (for example a rule it cannot read) cannot move to 8.x until
  the data is fixed; nothing is silently skipped. The log, the Admin page and the upgrade notes name
  the problem.
- The marker is advisory, not a security boundary.
- Going back to the latest 6.x still works: the rewrites use formats 6.10 and newer read
  ([ADR 0263](0263-the-6-to-7-upgrade-is-reversible.md)).

## Verification

`tests/unit/test_data_format.py`, `tests/unit/test_scoped_tokens.py`. Code:
`src/privacyfence/data_format.py`, `src/privacyfence/policy_upgrade.py`, `src/privacyfence/daemon_main.py`.

## Related

ADR 0041, 0047, 0171, 0172, 0228, 0236, 0258, 0263.
