# ADR 0059: The Windows daemon runs as its own virtual service account

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-16 in
[#428](https://github.com/privacyfence/privacyfence/issues/428), `35e263f7`, merged in
[#465](https://github.com/privacyfence/privacyfence/pull/465)).

## Context

Privilege separation (ADR 0002, ADR 0003) runs the daemon under an account the logged-in human,
and therefore the AI agent, is not. The authority files (policy, passkeys, audit key) are then
protected by ACLs that name that account. macOS and Linux create a dedicated system account
(`_privacyfence`, `privacyfence`). Windows needed an equivalent. #428 proposed the built-in
`NT AUTHORITY\LocalService`.

`LocalService` is one account shared by every service on the machine configured to use it. An ACL
granting it access to PrivacyFence's authority directory grants the same access to every one of
those services, so any of them could read or rewrite the policy or the audit key.

## Decision

The Windows daemon runs as the virtual service account `NT SERVICE\PrivacyFence`
(`WINDOWS_SERVICE_ACCOUNT_NAME`, derived from `WINDOWS_SERVICE_NAME` in
`src/privacyfence/privilege_separation.py`).

- `scripts/windows_privilege_separation.ps1`'s `Install-DaemonService` creates the service with
  `sc.exe create PrivacyFence ... obj= "NT SERVICE\PrivacyFence"` and no `password=`. The Service
  Control Manager creates the account with the service, gives it its own SID, and grants it "log
  on as a service". Deleting the service removes the account.
- The account name is not configurable: Windows derives it from the service name, so the Python
  constant, `windows_service.py` (which imports `WINDOWS_SERVICE_NAME`) and the script's
  `$ServiceAccount = "NT SERVICE\$ServiceName"` are one fact, checked to agree by tests.
- A virtual account cannot hold secondary group memberships, so the service group
  (`PrivacyFenceUsers`, `WINDOWS_SERVICE_GROUP_NAME`) holds only the human, and every ACL and pipe
  DACL the installer writes names the account and the group separately.

## Alternatives considered

- **`NT AUTHORITY\LocalService`**, as #428 proposed. Rejected: shared with every other service that
  uses it, so an ACL naming it is not specific to PrivacyFence.
- **A regular local user account** (the POSIX approach). Rejected: it needs a password stored
  somewhere and rotated, and a separate grant of the logon-as-a-service right. A virtual account
  has no password to store or leak.
- **`LocalSystem`.** The sources do not record it being weighed. It would have the same sharing
  problem as `LocalService`, with full control of the machine on top.

## Consequences

- Only the daemon's process holds the SID the authority ACLs name.
- `%USERNAME%` inside the service is the machine account (`MACHINE$`), not the virtual account, so
  identity checks read the token instead (`windows_acl.current_account_name()`, which `current_user_name()` returns for
  `check_runtime_identity()`).
- The group/account split is Windows-only; code that treats the account as a member of its own
  group, as on POSIX, is wrong on Windows.

## Verification

- `tests/unit/test_privilege_separation.py`:
  `TestSystemRootOverride::test_the_windows_account_name_follows_the_service_name` and
  `TestWindowsInstallerContract::test_account_and_group_names_match`.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — the local-mode trust boundary.
- [ADR 0003](0003-separated-installs-only.md) — every packaged install is separated.
- [#428](https://github.com/privacyfence/privacyfence/issues/428) — privilege separation.
