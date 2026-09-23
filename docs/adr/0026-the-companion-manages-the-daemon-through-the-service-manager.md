# ADR 0026: the companion manages the daemon through the platform's service manager, one elevation prompt per action

## Status

Accepted — 2026-09-23; implemented in [#609](https://github.com/privacyfence/privacyfence/pull/609)
(`7b51a27b`). Amends [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) decision 2
(the companion's scope). #609 first recorded this as an in-place amendment inside ADR 0002; it was
moved here, unchanged in substance, when the ADR rules in [`README.md`](README.md) were adopted.

## Context

ADR 0002 decision 2 scoped the companion to "Open Approvals, Open Settings, and Quit". Two
problems reported against 4.1.5 on macOS showed that scope was too narrow:

- A packaged upgrade could leave the separated daemon's LaunchDaemon/systemd unit/Windows service
  **unloaded while the installer reported success**: a `launchctl bootout`→`bootstrap` race on
  macOS (errors 5 and 37), `--auto` swallowing a failing `bootstrap`, a Windows installer that
  overwrote a running service's files, and KeepAlive policies (`SuccessfulExit=false`,
  `Restart=on-failure`, SCM failure actions) that ignore the daemon's own clean exits.
- The companion **showed no daemon status and offered no way to act**. Its Quit is refused on
  separated installs (#428 B4, correctly: the control socket is group-shared with the agent), and
  the control channel had no status command. A stopped daemon had no visible symptom and no
  recovery path short of a terminal.

## Decision

The platform's own service manager does the managing; the companion is its user interface.

- **Status needs no privileges.** `daemon_status.probe()` asks the daemon first, over a new
  `STATUS` control-channel command — read-only, returning no tokens or paths, and therefore, unlike
  every other command on that channel, never gated on `allow_quit` or a passkey. When the daemon
  isn't answering (the stuck-after-upgrade case) it falls back to the service manager directly:
  `launchctl print`, `systemctl show`, `sc.exe query`. Result: `running` / `starting` / `stopped`
  / `failed` / `unresponsive` / `unknown`, polled every five seconds; the tray icon greys out when
  the daemon is not up.
- **Start, Restart and Stop each ask for a fresh administrator prompt.**
  `service_control.run_elevated()` runs the platform script's new `daemon start|stop|restart`
  subcommand through the platform-native elevation: `osascript … with administrator privileges`
  (macOS), `pkexec` with a new polkit policy (Linux), UAC (Windows) — the same three mechanisms and
  the same script-trust check (root-owned, not group/world-writable) ADR 0002 Phase 4 already uses
  for `enable --for-user`. A cancelled prompt reports as cancelled and is never retried.
- **Service Details…** shows the one-sentence reason behind the status symbol.
- **Linux**, which ADR 0002 decision 4 gives no tray, gets the same four actions as `.desktop`
  Actions (`--action=service-status|service-start|service-restart|service-stop`) plus a
  `notify-send` notification from the `--serve` process's background poll. The notification rule
  is the tray's: stopped or failed for more than fifteen seconds, and not within the first minute
  after the companion itself started.
- **The upgrade path checks for itself.** The platform scripts gain `daemon ensure-running`;
  `enable` and the macOS `postinstall` call it, the macOS bootout/bootstrap race is retried with
  backoff, and the Windows installer stops the service and companion before copying files
  (`PrepareToInstall`). The tray's Start button is the last line of recovery, not the only one.

ADR 0002 decisions 3 and 4 still hold. What the companion shows is a diagnostic sentence about the
daemon's *process*, built from `launchctl`/`systemctl`/`sc.exe` output and the daemon's own
version/pid — never approval content. There is no new dependency: `daemon_status.py` and
`service_control.py` use `subprocess` and the platform's own tools, and the grey icon is computed
at runtime with Pillow's `ImageOps`, already required by `pystray`.

## Alternatives considered

- **A standing grant (a sudoers rule, a polkit `allow_active=yes`, a setuid helper) so the
  companion can start/stop without a prompt.** Rejected: a prompt per action is the cost of never
  widening what an agent sharing the companion's own uid could reach (ADR 0002 decision 6).
- **Lifting #428 B4's refusal of the control channel's `QUIT`.** Rejected, and the refusal stays:
  the control socket is group-shared with the agent, so a quit reachable there is a quit the agent
  can send.
- **In-place amendment of ADR 0002** (what the source plan prescribed). Superseded by this ADR when
  the one-decision-per-ADR rule was adopted.

## Consequences

- A stopped or stuck daemon is visible within seconds and recoverable from the tray or the Linux
  desktop entry, by someone who can answer an administrator prompt.
- Every start/stop/restart costs the human an administrator password. That is deliberate.
- `STATUS` is the one control-channel command that needs no gate; anything added to it later must
  stay free of tokens, paths and approval content, or it needs the gate the other commands have.
- The Linux polkit policy (`installer/linux/eu.privacyfence.daemon-control.policy`) is new
  installed surface to keep in step with the scripts' subcommands.

## Verification

- `tests/unit/test_daemon_status.py`: control-channel answer first, service-manager fallback per
  platform, each status value.
- `tests/unit/test_service_control.py`: the elevation argv per platform, cancelled-prompt
  detection, the script-trust check.
- `tests/unit/web/test_control_channel.py`: `STATUS` answers without a passkey or `allow_quit`
  and returns no secrets.
- `tests/unit/test_companion.py`: status line, grey icon, the Start/Restart/Stop/Service Details
  actions and the notification rule.
- `resources/linux/privacyfence-companion.desktop`'s `Actions=` line; the platform scripts'
  `daemon` subcommands.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — decisions 2 (amended), 3, 4
  and 6.
- [ADR 0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md) — the socket
  takeover fix shipped in the same change.
- [ADR 0008](0008-one-principal-per-os-user.md) — supersedes the interim one-owner guard that also
  shipped in #609.
- Source plan: `local-mode-fixes-plan.md` §"Problem 2" and D3, never merged to `main`; read it
  with `git show 453ae02e:local-mode-fixes-plan.md`.
