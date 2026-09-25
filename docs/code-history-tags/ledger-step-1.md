# Ledger: step 1

## ADR candidates

1. **Stable tags are gated on graphical-session coverage; pre-releases only report it; nothing waits
   on a live run.** `scripts/check_graphical_session_coverage.py` (module docstring and
   `main()`), `tests/unit/test_check_graphical_session_coverage.py`. The reasoning lived in a closed
   issue's "options" list, which the code cited by number. Meets the bar: it is the release path, and
   it rejects two non-obvious alternatives (block `finalize-release` on a fresh run; gate every
   channel). ADR 0030 mentions that these workflows stay off the critical path, but not the
   stable-only gate or the one-re-run allowance.
2. **Nothing is run elevated unless only root/Administrators can rewrite it.** Before any admin
   prompt, the script must be root-owned and not group/world-writable (POSIX), writable by nothing
   outside SYSTEM/Administrators (Windows), and on macOS the bundle must also pass `codesign`
   verification. `src/privacyfence/privilege_separation.py`
   (`_posix_script_elevation_problem`, `_macos_auto_enable_script_problem`,
   `_windows_script_elevation_problem`, `maybe_auto_enable_macos`), and the matching classes in
   `tests/unit/test_privilege_separation.py`. Trust boundary: without it the auto-enable prompt is a
   local privilege-escalation path. No ADR records it.
3. **The Windows daemon runs as the virtual account `NT SERVICE\PrivacyFence`, not `LocalService`.**
   `src/privacyfence/privilege_separation.py` (the comment above `WINDOWS_SERVICE_NAME`). Trust
   boundary with a rejected alternative: `LocalService` is shared with every other service that
   uses it, so ACLs naming it would grant those services the authority files.
4. **`PRIVACYFENCE_SYSTEM_ROOT` is ignored once the platform's real root has a marker.**
   `src/privacyfence/privilege_separation.py` (comment above `SYSTEM_ROOT_ENV_VAR`,
   `_default_system_root`), `tests/unit/test_privilege_separation.py`
   (`test_refuses_the_override_when_the_real_root_has_a_marker`). Trust boundary: the companion
   and shim honour the variable from the user's session, so on a real install a redirect is an
   attack, not a test hatch.
5. *(Borderline.)* **The storage-permissions startup check warns in local mode and refuses to
   start in org mode.** `src/privacyfence/daemon_main.py` (`check_storage_permissions` and its
   caller), `tests/unit/test_daemon_main.py` (`TestCheckStoragePermissions`). The comment
   described it as the same "detectable vs. preventable" split ADR 0016 draws for the bundle hash
   log; worth deciding in step 7 whether it is a decision in its own right.

## Changed user-visible strings

- `daemon_main.py` startup log: `SEC-09: <problem>` → `Insecure storage permissions: <problem>`.
- `privilege_separation.py` wrong-account refusal: dropped `(#428 Phase 4)`.
- `privilege_separation.py` unseparated-install refusal: `(#428 Phase 4 / ADR 0003 decision 6)` →
  `(ADR 0003 decision 6)`.
- `privilege_separation.py` log: `privilege separation enabled automatically (#428 D1 / ADR 0003
  decision 6)` → `... (ADR 0003 decision 6)`.
- Windows service description (`windows_service.SERVICE_DESCRIPTION` and the `sc description` call
  in `scripts/windows_privilege_separation.ps1`): dropped `(issue #428 Phase 4)`.
- `scripts/check_graphical_session_coverage.py` warnings and the stable-channel error:
  `(privacyfence/privacyfence#374)` → `(see docs/testing-policy.md's layer 6)`.
- `scripts/linux_privilege_separation.sh` note: `auto-enabling privilege separation (#428 D1, 4.1)
  -- see debian/postinst` → `... (ADR 0003) -- see debian/postinst`.
- `scripts/macos_privilege_separation.sh`: the same note, `(#428 D1, 4.1)` → `(ADR 0003)`; the
  trusted-image refusal and the staging note lost `(B1)`.
- `installer/macos/pkg/postinstall` log: `(#428 D2, ADR 0003 decision 3)` → `(ADR 0003 decision 3)`.
- macOS installer welcome screen (`installer/macos/pkg/resources/welcome.html`): "ADR 0002 and
  issue #428" → "ADR 0002 and ADR 0003".
- `installer/linux/privacyfence-daemon.service.tmpl`: `Documentation=` points at ADR 0003 on GitHub
  instead of closed issue #428 (shown by `systemctl status`).

## Cross-slice edits needed

None. No test outside this slice asserts on any string above (`git grep` for each old string).

## Bugs noticed

None in behaviour. Comment drift noticed but left alone under rule 7:

- `tests/integration/test_macos_pkg_smoke.py`'s `test_pkg_signature` pointed at its module
  docstring's point 5; signing is point 6. Corrected while rewriting that `§5`.
- Several comments still call privilege separation "opt-in" (`privacyfence.service`,
  `com.privacyfence.app.plist`, which also links a "Privilege separation (macOS, opt-in)" doc
  section), which ADR 0003 no longer allows for a shipped install.
- `scripts/build_installer.ps1` step 4 says the Task Scheduler task looks for
  `privacyfence-app.exe`; the only task is the companion's, which runs
  `PrivacyFenceCompanion.exe`.

## Open issues kept as URLs

None. Every issue this slice cited is closed: #121, #151, #374, #396, #400, #407, #410, #411,
#426, #428, #562, #580, #598, #599, #679.
