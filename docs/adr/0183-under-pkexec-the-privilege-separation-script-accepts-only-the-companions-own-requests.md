# ADR 0183: under pkexec the privilege-separation script accepts only the companion's own requests

## Status

Accepted — 2026-10-15. Implemented.

## Context

The Linux companion elevates `/usr/sbin/privacyfence-privilege-separation` with `pkexec`. The
polkit action matched the script's path alone, so once a person authenticated, the same
authorisation covered every argument the script understands, including `--daemon-exec`, which
names a program the service account will run, and `uninstall --purge`. Anything running as the
person (the agent's own account) could ask for those, and the password prompt only said "start,
stop or restart".

## Decision

1. **The argument allow-list is the guard.** When `PKEXEC_UID` is set, the script accepts exactly:
   `daemon start|stop|restart|ensure-running|status`, `enable --auto`, and
   `enable --for-user <name>` where `<name>` is the account that ran `pkexec`. Everything else
   exits with "not allowed via pkexec". These are the only argument lists the companion and the
   auto-enable path pass. Running the script as root without `pkexec` (the `.deb` postinst, `sudo`)
   is unchanged.
2. **Two polkit actions make the prompt honest.** `eu.privacyfence.daemon-control` (first argument
   `daemon`) says the password is for starting, stopping or restarting the background service;
   `eu.privacyfence.enable-for-user` (first argument `enable`) says it is for setting the service
   up for your account. They are selected by the `org.freedesktop.policykit.exec.argv1`
   annotation and both use `auth_admin_keep`.

## Alternatives considered

- **Rely on the polkit actions alone.** Rejected: the annotation selects a prompt, and polkit
  matches no other argument, so it cannot refuse `--daemon-exec` after `daemon`.
- **One action per argument list.** Rejected: more prompts and more policy for no extra guard; the
  allow-list already decides what runs.
- **Keep the single action.** Rejected: the person is asked for a password to authorise something
  the dialog does not describe.

## Consequences

- Elevated, the script can no longer be pointed at a different daemon program or removed by an
  argument list the companion never sends.
- The `.deb` needs a polkit that honours `exec.argv1` (0.113 and later; Ubuntu 24.04 ships 124).
- A new elevated request from the companion must be added to the allow-list and, if it is a new
  kind, to the policy file.

## Verification

- `scripts/linux_privilege_separation.sh` (`check_pkexec_arguments`),
  `installer/linux/eu.privacyfence.daemon-control.policy`.
- `tests/unit/test_privilege_separation.py`.

## Related

- [ADR 0026](0026-the-companion-manages-the-daemon-through-the-service-manager.md)
- [ADR 0058](0058-nothing-runs-elevated-unless-only-an-administrator-can-rewrite-it.md)
