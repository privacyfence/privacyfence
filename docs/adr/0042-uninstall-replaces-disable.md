# ADR 0042: `uninstall [--purge]` replaces `disable`; uninstalling keeps the data, purging deletes it

## Status

Accepted — 2026-09-24. Decided by the maintainer on 2026-09-24 (option C of the product cleanup's
Linux phase). Implemented on Linux (`scripts/linux_privilege_separation.sh`, `debian/prerm`,
`debian/postrm`). macOS and Windows follow in the cleanup's next two phases with the same semantics;
until then their scripts still carry `disable`.

Records the per-platform mechanics that [ADR 0041](0041-only-the-current-install-layout-is-supported.md)
decision 3 left to this phase.

## Context

Every shipped install is privilege-separated ([ADR 0003](0003-separated-installs-only.md)): the
daemon runs as its own service account and keeps its data under a system root
(`/var/lib/privacyfence`, `/Library/Application Support/PrivacyFence`, `%ProgramData%\PrivacyFence`).
Each platform's separation script had a `disable` subcommand that undid this: it stopped and
removed the service, deleted the marker, moved the data directory back into the owner's home and
restored the per-user autostart it had replaced. The `.deb`'s `prerm` ran it on `remove`.

ADR 0041 removed the reasons for that. There are no earlier layouts to return to, so there is no
autostart entry to restore (the `.deb` no longer ships one), and its decision 3 (G3) says removing
the package keeps the data in the system root, purging deletes it, and nothing moves data back into
a home directory.

`disable` also never truly unwound separation on a packaged install. After it, the next start of
the packaged daemon runs `privilege_separation.enforce_separation()` (ADR 0003 decision 6), which
either separates the install again or refuses to serve. So `disable` was, in practice, a way to
stop PrivacyFence and move its secrets into a directory the agent can read.

Uninstalling still needs a step that the package manager alone does not do. The service unit and
the companion autostart entry are rendered by the separation script, so dpkg does not track them and
would leave them behind, pointing at a binary it has just deleted. macOS has no package manager at
all, so its uninstall is a command the user runs.

## Decision

1. **`disable` is removed and replaced by `uninstall [--purge]`** in all three separation scripts.
2. **`uninstall`** stops and unregisters the service, and removes the companion autostart entry.
   It leaves the data under the system root, the marker, and the service account and group in
   place, so a reinstall finds the same data owned by the same account.
3. **`uninstall --purge`** does that and also deletes the data directory (which holds the marker)
   and the service account and group.
4. **Nothing moves data into a home directory** (ADR 0041, G3), on any path.
5. **Linux wiring.** `debian/prerm remove` runs `uninstall`. `debian/postrm purge` deletes
   `/var/lib/privacyfence` and the `privacyfence` account and group itself. By the time dpkg runs
   `postrm`, it has deleted `/usr/sbin/privacyfence-privilege-separation` with the rest of the
   package, so the tool cannot be called there. A unit test holds the path and names in `postrm`
   to the script's own constants. `prerm upgrade` is unchanged: it stops the unit, and `postinst`
   starts it again.

## Alternatives considered

- **A: delete `disable` and have each package inline the stop step.** Rejected. macOS has no package
  manager and needs a command the user runs anyway, and the stop logic would be written three times,
  in three installer formats, next to a script that already has it.
- **B: keep the name `disable` for stop-and-leave.** Rejected. After it, the install is still
  separated and the service account still owns the data, so the name describes something that did
  not happen.
- **Keep the tool callable from `postrm`** by copying it somewhere dpkg does not track. Rejected: it
  would be a second, unowned copy of a root-run script, left for nothing to update or remove. The
  purge steps are three commands.

## Consequences

- `apt remove` followed by a reinstall keeps every connector token, rule and audit record.
  `apt purge` leaves nothing: no data, no account or group, no unit, no autostart entry.
- A packaged daemon whose separation was purged by hand, with the package still installed, refuses
  to serve on its next start (ADR 0003 decision 6). That is the intended state for "I removed
  PrivacyFence's data".
- The `.deb` ships no files under `/etc` and so has no conffiles.
- The macOS and Windows phases implement decisions 1–4 with their own wiring: a user-run
  `uninstall [--purge]` on macOS, and the Windows uninstaller.

## Verification

- `tests/unit/test_privilege_separation.py::TestLinuxUninstall`: `uninstall` stops the unit and keeps
  the data; `--purge` deletes the data, then the account, then the group; `postrm purge` uses the
  script's own path and names; neither maintainer script reaches into a home directory.
- `tests/integration/test_deb_packaged_lifecycle.py::test_deb_install_validate_scenario_remove_reinstall_purge_lifecycle`
  (run by `build.yml`'s `build-deb` job): install, remove keeps the data, reinstall serves it, purge
  leaves nothing.
- `tests/integration/test_linux_graphical_session_autostart.py::test_deb_purged_separation_autostarts_nothing_and_refuses_to_serve`.

## Related

- [ADR 0041](0041-only-the-current-install-layout-is-supported.md) — decision 3 (G3) is the rule this
  ADR implements.
- [ADR 0003](0003-separated-installs-only.md) — decision 6 is why the old `disable` never truly
  unwound separation.
