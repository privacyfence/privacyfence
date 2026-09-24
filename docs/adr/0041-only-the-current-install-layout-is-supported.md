# ADR 0041: only the current install layout is supported; there is no upgrade path from earlier layouts

## Status

Accepted — 2026-09-24. Decided by the maintainer on 2026-09-24 (decisions G1, G3 and H1 of the
product cleanup that followed that day's source-code audit of the documentation). Implemented for
the policy settings format; the file-location moves and the installers' upgrade and uninstall steps
are removed platform by platform in the cleanup's later phases, each citing this ADR.

Supersedes [ADR 0004](0004-retire-the-v1-auto-accept-config-model.md) in part: decisions 4 and 5
kept the one-time v1 → v2 settings conversion (`policy/compat.py`'s `migrate_to_policy_v2`) as the
last caller of the v1 compiler and of `policy/resource_registry.py`'s grant expansion. That
conversion is removed. ADR 0004's other decisions are unchanged.

## Context

PrivacyFence has no existing users. Every install that exists is a maintainer's or a tester's, and
each can be reinstalled from scratch.

The code nonetheless carried upgrade machinery for layouts no current release produces:

- a startup step that converted a `settings.yaml` in the v1 policy format (`auto_accept_rules`,
  `auto_accept_grants`) to the `auto_accept:` section, rewrote the file, kept a `.bak`, and raised
  a Settings notice about the converted rules; and before it, a rename of the retired
  `telegram.search_messages` operation key;
- moves of files from earlier locations into the current ones (`paths.py`), a fallback in the
  `.mcpb` shim to a shared MCP token file that separated installs no longer write, and a Windows
  install-path candidate kept only for an older per-user install;
- installer steps that stop or delete the autostart registrations of earlier layouts, and a
  `disable` path that moves the service's data back into the owner's home directory.

Each is code on a startup or install path, and several are on trust-boundary paths
(`privilege_separation.py`, the installers' post-install scripts). None of it runs on a fresh
install, so it is tested only by fixtures written for it, and every reader of those paths has to
work out that it is dead in practice. The shipped `settings.yaml.example` still seeded a v1
`auto_accept_rules:` section, so every fresh install went through the conversion on its first
start — the conversion was the only thing that made the shipped default work.

## Decision

1. **Only the current layout is supported (G1).** No code reads, converts or moves data from an
   earlier config format, file location, tool name or autostart registration. A future format
   change that happens after there are users needs its own migration decision; this ADR does not
   forbid one, it records that there is nothing to migrate from today.
2. **An earlier config format is refused, not ignored.** A `settings.yaml` that still has
   `auto_accept_rules` or `auto_accept_grants` — even an empty one — stops the daemon at startup
   with a configuration error naming the section and saying to recreate the rules on the Settings
   Auto-accept page (`policy.store.reject_v1_sections`, called from `daemon_main.load_config`, for
   the local principal and for every org principal). The file is not modified. The shipped
   `settings.yaml.example` is written in the current format.
3. **Uninstall keeps data; purge deletes it (G3).** Removing the package stops PrivacyFence and
   leaves its data in the system root. Purging (or the platform's documented equivalent) deletes
   it. Nothing moves data back into a home directory. The per-platform mechanics, including what
   `privacyfence-privilege-separation disable` becomes, are decided in the Linux phase and recorded
   there or in this ADR's successors.
4. **Anything a current fresh install uses stays (H1),** even if it reads like upgrade code. The
   test is whether a fresh install of the current release can reach it, not why it was written.
   For example, `privilege_separation.enforce_separation` separating a packaged install that is not
   yet separated stays unless a phase shows no fresh install can get there.

## Alternatives considered

- **Keep the migrations for a deprecation window.** A window protects existing users; there are
  none. It would keep the code, its tests and its trust-boundary surface for no one.
- **Ignore v1 sections silently.** The rules in them would stop applying and every matching call
  would raise an approval card with nothing to say why. Refusing with the section named costs one
  restart; ignoring costs an unknown number of unexplained prompts.
- **Keep the conversion but stop maintaining it.** That is ADR 0004's state. The conversion still
  rewrote `settings.yaml` on startup and depended on a v1 compiler that had to stay correct, so
  "unmaintained" would have meant "untrusted code on the startup path".
- **Delete data on package removal too.** A reinstall, or an `apt remove` meant as a stop, would
  lose every token, rule and audit record. Package managers already separate remove from purge.

## Consequences

- A tester with an older install gets a clear refusal and recreates their rules; a reinstall is
  the documented path for the rest of the old layout.
- `policy/compat.py`, `daemon_main._migrate_settings_to_policy_v2`,
  `auto_accept.migrate_telegram_search_operation_key`, the `migrated_to_policy_v2` marker, the
  Settings conversion notice, `settings_controller`'s v1 predicate tables and
  `policy/resource_registry.py`'s grant expansion are gone. `policy/conditions.py` no longer
  accepts v1 condition names (`ConditionSelector.replaces`); the old-name mapping lives only in the
  test-only v1 reference harness (`tests/unit/policy/_v1_reference.py`), which still proves each
  selector behaves as the predicate it replaced.
- Installer changes under this ADR are exercised only by `build.yml`'s packaged smoke tests and the
  graphical-session workflows, so an alpha that passes them gates the next stable release (H2).

## Verification

- `src/privacyfence/policy/compat.py` does not exist; nothing in `src/` defines or calls a
  `migrate_*` policy function.
- `tests/unit/policy/test_store.py::TestRejectV1Sections`.
- `tests/unit/test_daemon_main.py::TestLoadConfig` — the bootstrapped default is in the current
  format; a v1 file is refused and left untouched; `main()` reports it as a configuration error.
- `tests/unit/test_daemon_main.py::TestLoadPrincipalSettings::test_a_principals_v1_policy_section_is_refused_not_converted`.

## Related

- [ADR 0004](0004-retire-the-v1-auto-accept-config-model.md) — superseded in part (decisions 4–5).
- [ADR 0003](0003-separated-installs-only.md) — every shipped install is separated; decision 4 above
  keeps the code that makes a fresh one so.
- PR #662 (Apps Script in Settings), merged before this change because both edit
  `settings_controller.py`.
