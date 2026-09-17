# Changelog

<!--
HOW TO USE THIS FILE

1. `## [Unreleased]` is permanent. A feature branch adds its user-visible change under that
   heading and nothing else. Do NOT open a concrete `## [X.Y.Z]` heading on a feature branch:
   two branches in flight would both claim the same next version, which is the exact failure
   CLAUDE.md records at commit d929510 ("Revert version bump -- will release together with other
   pending CRs") from the era when versions were hand-bumped in two files. Only the PR that cuts
   a release turns `## [Unreleased]` into `## [X.Y.Z] -- YYYY-MM-DD`, adds a fresh empty
   `## [Unreleased]` above it, and updates the two link definitions at the bottom. If a section for
   that version already exists (4.0.0's was opened early), MERGE `[Unreleased]`'s entries into it
   and fix its date -- renaming the heading would create a second one, and
   scripts/changelog_section.py refuses to render a version that has two.

2. This file is NEVER a version source. setuptools_scm derives the version from the git tag and
   remains the only one -- see CLAUDE.md's "Releasing" section. Nothing may parse this file to
   determine a version, and no version string lives in the source tree. The dependency runs the
   other way: scripts/changelog_section.py reads a version *out* of this file to produce the
   GitHub Release body for that tag (see .github/workflows/build.yml).

3. Pre-release tags (`aN`/`bN`/`rcN`, and the older `-alphaN`/`-betaN` spellings) get no entry of
   their own. Their content is folded into the final version they led to, per Keep a Changelog.

4. Entries are ordered by version, NOT by date. The 3.4.x maintenance line and the 4.0 line ran
   in parallel, so 3.4.5-3.4.7 (2026-09-02/03) were cut after v4.0.0-alpha1..alpha4
   (2026-08-28/29). Sorting by date here would be actively misleading.
-->

All notable changes to PrivacyFence are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- `docs/security-and-compliance.md` now states the local-mode trust boundary explicitly: it is the
  operating-system user account, so a process running as the signed-in user — including an AI client
  with shell access, which is the normal local-mode install — can mint a session and release a
  pending approval without a browser. The CSRF, same-origin, TTL and expiry controls on that path are
  defenses against a hostile web page and against leaked credentials, not against local code
  execution, and the document previously left that easy to read more broadly than it holds. Nothing
  about the implementation changed; this corrects what is claimed for it, and names the work that
  closes the gap (issues #426, #427, #428). Org mode is unaffected — its daemon runs on a server the
  client has no loopback access to.
- ADR 0002 (`docs/adr/0002-local-mode-trust-boundary-and-companion-app.md`) records the architecture
  decision that follows from the statement above: local mode's trust boundary is the OS user
  account, and a minimal companion app (tray/menu-bar item — Open Approvals, Open Settings, Quit)
  returns as the channel that gets a human into the web UI without a sign-in credential traveling
  through the AI client. It fixes the companion app's dependency budget (a second entry point of the
  existing packaged binary; one platform-conditional tray dependency on macOS/Windows, none on
  Linux), keeps the web app as the only implementation of approvals and settings, and records why
  session minting is made *insufficient* (via the passkey in issue #426) rather than uncallable —
  two processes running as the same user cannot be told apart. Supersedes ADR 0001 in part. No
  behavior changes with this entry; it is the decision the implementation in issues #428 and #426
  will follow. See issue #427.
- Issue #428 Phase 1: local mode's human-authority state — the web-approval bootstrap secret
  (`web_token`), the privacy policy (`config/settings.yaml`), enrolled WebAuthn credentials, and the
  audit log plus its HMAC key — now lives under its own `authority` subdirectory, split out of
  `mcp_token` and the agent's own connector caches/credentials, which stay where they were. A pure
  refactor with no security gain yet — everything still runs as the same OS user until Phase 4 moves
  the daemon to its own account and re-owns this subtree to it — but it isolates that later,
  security-bearing state migration from everything that depends on the storage layout today. A
  pre-4.1 install's existing `settings.yaml`, WebAuthn credentials, `web_token`, and audit history
  are moved into the new location automatically on first startup under this version, so nothing is
  silently reset. See issue #428.
- Issue #428 Phase 2: minting a fresh bootstrap code on demand — once a previous session or link has
  already expired, without restarting the daemon — no longer goes through a persistent `web_token`
  file presented as a `POST /api/bootstrap` Bearer header over the same loopback HTTP port a browser
  uses. It now goes through a new control channel (`web/control_channel.py`): a Unix domain socket on
  macOS/Linux, an ACL'd named pipe on Windows — neither reachable by a browser's own loopback
  connection. `web_token` itself, and the `POST /api/bootstrap` route, are gone. Still no security
  gain alone — the channel is reachable by anything running as the same OS user, agent included —
  but it's the interface issue #428's Phase 3 (companion app) and Phase 4 (privilege separation) both
  need to exist first. The not-authorized page's on-demand recovery command changed to match (`nc -U`
  on macOS/Linux, PowerShell's `NamedPipeClientStream` on Windows — neither needs Python, matching
  the previous `curl`-based command's own no-extra-install posture). See issue #428.
- Issue #428 Phase 3 (ADR 0002): a companion app — `privacyfence-companion`, a second entry point of
  the same packaged application, not a new binary — gives a human a way into PrivacyFence's web UI
  that doesn't route a sign-in credential through the AI client. On macOS/Windows it's a persistent
  tray/menu-bar process (`pystray`, the one platform-conditional dependency ADR 0002 budgets for)
  offering Open Approvals, Open Settings, and Quit; on Linux — no tray, by design — the same three
  actions are a real (no longer `NoDisplay`) Applications-menu entry plus two Desktop Actions,
  invoking `privacyfence-companion --action=...` once and exiting. It mints its own sign-in links
  over the Phase 2 control channel and opens them in the default browser, and can ask the daemon to
  quit over a new `QUIT` command on that same channel (gated by the existing `allow_quit` setting).
  It also runs its own, opposite-direction channel that the daemon's connector OAuth flows
  (`oauth_loopback.py`) now try first before opening a browser themselves — falling straight back to
  today's direct `webbrowser.open()` when no companion is running, still the default until a human
  starts one. Nothing installs or autostarts the companion yet, and it changes no default behavior on
  its own — that inversion, and the privilege separation it exists to serve, is Phase 4. See issue
  #428.
- Issue #428 Phase 4, macOS: `scripts/macos_privilege_separation.sh enable` moves local mode's
  trust boundary off the logged-in user's account. It creates a dedicated `_privacyfence` system
  account, relocates the data directory from `~/.privacyfence` to
  `/Library/Application Support/PrivacyFence` owned by it, and inverts the startup wiring ADR 0002
  describes — the daemon becomes a LaunchDaemon with no login session, and the Phase 3 companion app
  becomes the LaunchAgent that autostarts in yours. Four things the AI client could previously do,
  it now cannot: edit the always-allow rules and PII policy in `config/settings.yaml`, write a
  forged credential into `webauthn_credentials.json` (which is what makes the local passkey in issue
  #426 mean anything), read the audit log's HMAC key, or read the daemon's connector credentials.
  Minting a sign-in session stays deliberately reachable — the companion and the agent run as the
  same user and no permission bit can tell them apart, so the design makes a session *insufficient*
  rather than uncallable (ADR 0002 decision 6). The agent's own `mcp_token` also stays reachable, in
  a group-shared `handoff` directory alongside the control-channel sockets; the MCPB shim and the
  not-authorized page both follow it there. **Opt-in, and staying opt-in for a full release**: the
  migration moves live connector OAuth tokens and `… disable` is the only way back. Root still
  defeats all of it. `… status` audits the on-disk result, and the daemon refuses to start if it
  finds itself running as the wrong account rather than silently seeding a default policy over the
  real one. Linux and Windows are unchanged — the same phase for each is still to come. See issue
  #428.
- Issue #428 Phase 4, Linux: `sudo scripts/linux_privilege_separation.sh enable` does for Linux
  what the macOS entry above does for macOS, in this platform's own idioms — a `privacyfence`
  system account (`useradd --system`, no underscore prefix, which means nothing here), the data
  directory relocated from `~/.privacyfence` to `/var/lib/privacyfence` (FHS 3.0 §5.8) owned by it,
  and the startup wiring inverted: a **system** systemd unit
  (`/etc/systemd/system/privacyfence-daemon.service`) runs the daemon with no desktop session,
  while both pre-existing ways it used to start in yours — the `.deb`'s XDG autostart entry and the
  repo's `--user` unit — are moved aside, since either would start a second daemon as you. It
  closes exactly the same four things, and the layout, modes, marker file and `handoff` directory
  are identical to macOS's; only the root and the account name differ. `privacyfence-companion`
  grows a `--serve` mode, which an XDG autostart entry runs in each desktop session: the
  companion's control channel alone, no tray and no new dependency. That one is not optional —
  a daemon with no desktop session cannot open a browser, so without it connector OAuth for Slack,
  Salesforce and Atlassian would have no way to show you a sign-in page. **Opt-in, and staying
  opt-in for a full release**, same as macOS: the migration moves live connector OAuth tokens, and
  `… disable` (which restores both startup paths it moved aside) is the only way back. Root still
  defeats all of it. See issue #428.
- Issue #428 Phase 4, Windows: `privilege-separation.ps1 enable`, run from an elevated PowerShell
  (the installer now puts it next to the application; a source checkout runs
  `scripts/windows_privilege_separation.ps1`), completes Phase 4 on the last platform — and it is
  the one where the mechanism genuinely differs rather than being differently spelled. The daemon
  becomes a **Windows service** running as the virtual account `NT SERVICE\PrivacyFence`
  (materialized by the Service Control Manager with the service, its own SID, no password for
  anyone to store), the data directory moves from `%LOCALAPPDATA%\PrivacyFence` to
  `%ProgramData%\PrivacyFence`, and the Scheduled Task that used to start the daemon in your
  session is disabled in favour of a new one that starts the companion tray app there instead. It
  closes the same four things — the agent can no longer edit the always-allow rules and PII policy,
  forge a WebAuthn credential, read the audit log's HMAC key, or read the daemon's connector
  credentials — and the marker file, the three directories and the `handoff` contents are identical
  to the other two platforms'.
  What is new is the permission model: Windows has no mode bits, so the layout is NTFS ACLs written
  with `icacls` and re-checked on every daemon start, with `/inheritance:r` first because
  `%ProgramData%` otherwise grants every account on the machine read access by inheritance, and
  `/setowner` because an owner can rewrite an ACL whatever it says — and moving the data directory
  out of `%LOCALAPPDATA%` would otherwise leave it owned by the account being excluded. The
  shared `handoff` directory ends up *tighter* than on POSIX — readable by the new
  `PrivacyFenceUsers` group, not writable, since both control channels are named pipes rather than
  socket files and nothing in your session needs to create anything there.
  Two Windows-only requirements are enforced rather than documented. **A per-machine install is
  required**: a service runs whatever its path names, so separating an install under your own
  profile would let the very client this contains rewrite the daemon's executable and have it run
  as the service account — `enable` reads the install directory's ACL and refuses, which settles
  issue #407's open question as two install tiers rather than dropping the non-elevated path. **And
  the companion is mandatory**, because a service runs in session 0 and cannot open a browser, so
  connector OAuth for Slack, Salesforce and Atlassian goes through it or not at all. **Opt-in, and
  staying opt-in for a full release**, same as the other two; the migration moves live connector
  OAuth tokens and `… disable` is the only way back — run it *before* uninstalling, since uninstall
  leaves `%ProgramData%\PrivacyFence` in place exactly as it leaves `%LOCALAPPDATA%\PrivacyFence`
  today. Administrator still defeats all of it. See issue #428.
- Issue #428 D1: privilege separation on macOS and Linux is now **default-on**, moved up from the
  original plan's 4.2 target rather than waiting the full release cycle the two entries above
  described. `enable`/`disable`/`status` are unchanged and `disable` remains how to opt back out;
  what's new is who runs `enable` and when. On Linux, `debian/postinst` runs
  `privacyfence-privilege-separation enable --auto` on every install and upgrade — it's already
  root at that point, which is exactly what provisioning the account and the system unit needs.
  On macOS, which has no equivalent package-manager hook (a DMG install runs nothing as root), the
  daemon's own startup asks once instead, via the standard admin-password dialog, the first time it
  finds itself unseparated (`privilege_separation.maybe_auto_enable_macos()`); `scripts/
  build_dmg.sh` now bundles `scripts/macos_privilege_separation.sh` and its launchd templates into
  the `.app` so that prompt has something to run — until now, opting in on a DMG install required a
  source checkout. `--auto`, new on both scripts, is what makes this safe to run unattended: it's
  the same `enable`, except anywhere that would otherwise fail on something only a human could
  resolve interactively (no resolvable account owner, no installed executables) it instead logs why
  and leaves the install opt-in, rather than failing a package install or nagging at every daemon
  start. A decline of the macOS prompt is respected and not asked again. This still ships ahead of
  the real-machine verification `docs/platform-support.md`'s "Known open items" describes — the
  automated contract coverage is unchanged, the manual pass against a release build is not done, and
  turning the default on makes running it sooner more important, not less. See issue #428.
- Issue #428 Phase 4, macOS: `scripts/macos_privilege_separation.sh enable` (and D1's auto-enable
  through it) now refuses to elevate a daemon or companion image that anyone but root or `wheel`
  could have rewritten, and `privilege_separation.audit_layout()` re-checks the same thing on every
  daemon start. ADR 0002 §5a asserted that `/Applications` was root-owned the way `/opt` is; it
  isn't — it's `root:admin drwxrwxr-x`, and a drag-installed `.app` is normally owned by the
  installing user, the same account the agent runs as — so nothing previously stopped that account
  from replacing the bundle's executable and getting code execution as the service account once
  privilege separation elevated to it, the identical escalation Windows already refused to permit.
  Corrected in ADR 0002 §5a itself.
- Issue #428 D1 follow-up: the macOS auto-enable prompt above ran whatever
  `scripts/macos_privilege_separation.sh` resolved to through an admin-password dialog without first
  checking what that was — on a packaged install the `.app`'s `Resources/` is as writable as anything
  else the logged-in user owns, and a source checkout never belongs to root at all, so an agent able
  to write either one could get its own script executed as root behind what looked like a routine
  permission prompt. `maybe_auto_enable_macos()` now refuses to elevate a script that is not
  root-owned and not group/world-writable, and, on a packaged install, whose `.app` bundle's
  signature doesn't verify (`codesign --verify --deep`) — anything else logs why and leaves the
  install opt-in, the same fallback `--auto` already takes for every other unresolvable case. A
  source checkout can never satisfy the ownership check, which is deliberate: this prompt now only
  ever runs a script the installer itself shipped.
- Issue #428 B4: the control channel's `QUIT` command is now refused unconditionally on a
  privilege-separated install, regardless of `allow_quit`. The control socket is `0660`
  group-shared after separation so the companion can still reach it, which puts the agent in the
  same group too; since a clean exit is exit 0, neither systemd's `Restart=on-failure` nor
  launchd's `SuccessfulExit: false` restarts a daemon stopped this way, so one `QUIT\n` on that
  socket was a standing, agent-reachable way to turn the privacy gate off entirely — exactly the
  capability privilege separation exists to take away from the agent. The error now names this
  platform's own service-manager command (`PlatformLayout.stop_command`, new alongside
  `start_command`/`status_command`) instead: a system service is the service manager's to stop,
  not this channel's. See issue #428.
- Org mode: a new `step_up.require_passkey` config flag (`--step-up-require-passkey` in
  `build_org_bundle.py`) closes the WebAuthn step-up gate's IdP-reauth fallback for organizations
  that want hardware-bound passkeys as a hard requirement before releasing a write approval.
  Previously, step-up accepted either a passkey assertion or a fresh IdP re-authentication
  unconditionally, even for an org that had enabled step-up specifically to defend against a
  compromised or phished IdP session — a principal with no enrolled passkey silently fell back to
  the weaker path. With `require_passkey` set, that fallback is gone entirely (the IdP step-up
  endpoint itself refuses, not just its link), and a principal with no enrolled passkey gets a
  hard failure pointing at `/security` to enroll one instead. Off by default. See issue #406.
- `web/server.py`'s module docstring no longer claims `/settings` stays unmounted in org mode
  because its CSRF model can't generalize to org mode's per-session cookie — `org_session.py`'s
  `check_csrf` already does that double-submit check, the same shape `session_auth.check_csrf`
  uses in local mode. The real, still-open gap is deciding which of `routes_settings.py`'s ~30
  actions are per-principal versus install-wide/admin-only and wiring `Principal.is_admin` into
  authorizing the latter, which the docstring now says instead. `docs/org-mode-setup-guide.md`
  gains a new §9 explaining where PII/privacy policy (install-wide, from the server's own
  `config/settings.yaml`, needs a daemon restart to change, and defaults to `block` for any group
  absent from that file — unlike local mode's `allow`) and auto-accept rules/grants (per-principal,
  under that user's own `users/<principal>/config/settings.yaml`) actually live today, since
  neither has a browser page of its own yet. See issue #400.
- Org mode now has a read-only `/settings` page, linked from `/approvals`'s footer: every
  signed-in principal can review and remove their own auto-accept rules and trusted-resource
  grants (never another principal's), and an admin (`Principal.is_admin`) additionally gets
  `/settings/privacy`, a read-only view of the effective install-wide PII/privacy policy that
  names which groups are explicitly configured versus silently relying on org mode's fail-safe
  `block` default. Removing a rule or grant goes through the same CSRF/origin checks as
  `/approvals` and is written to the audit log. Fixes a related bug found while building this:
  every org principal but whichever one a `local`-mode `run_app()` happened to initialize for
  privacy-filter purposes was silently falling through to an unconditional "allow" for every PII
  category, the opposite of org mode's intended fail-closed default — every org principal's
  privacy-filter state is now populated (from the real install-wide policy, not an unconfigured
  per-user file) the same way their auto-accept rules already were. Editing either surface from
  the browser remains out of scope for this first cut. See issue #400.
- Org mode's `/settings/privacy` is now editable by an admin, not only readable: each privacy
  group's default policy, each category's policy, the PII-detection master switch and its two
  individually-toggleable categories. This closes the question the read-only first cut above left
  open — whether the UI writes `settings.yaml` and demands a daemon restart, or the filter learns
  to reload. It reloads: the change is written to the server's own `settings.yaml` atomically and
  then applied to every principal in the running process, so it governs everyone's next request
  with no restart. Every change is written to the audit log under the admin who made it, and every
  audit entry recorded afterwards carries the new policy fingerprint
  (`AuditEntry.security_config_hash`) rather than the one loaded at startup. The write endpoints
  re-check `Principal.is_admin` themselves through `org_settings_scope.is_action_permitted` — the
  page being reachable is not what authorizes the write — on top of the same CSRF and same-origin
  checks `/approvals` uses. Fixes two bugs found while building it: the org `/settings` pages'
  inline stylesheet carried no CSP nonce, so both rendered unstyled under the app's own
  `style-src-elem` policy; and `pii_detector`'s per-principal state was never initialized from the
  install-wide `settings.yaml` for any org principal, so a category an admin had turned off
  install-wide stayed on for everyone (fail-closed, so nothing was let through that shouldn't have
  been, but not something an editable page could ship on top of). Install-wide log level and the
  Calendar free/busy toggle remain admin-only-in-principle but unwired — neither is privacy policy
  and each needs a reload path of its own. See issue #400.
- Issue #426 Phase 1: `StepUpConfig` (the WebAuthn step-up decision, previously org-mode-only) moves
  out of `org_mode.py` into a new `step_up_config.py`, and local mode gets its own entry point,
  `from_local_config`, reading a new `step_up:` section of `config/settings.yaml` (`enabled`,
  `scope`, `rp_id` — defaults to `localhost`, needing no TLS — `rp_name`, `require_passkey`).
  `web/routes_security.py`'s `/security` enrollment page and its `/api/security/webauthn/*` routes
  are now mode-agnostic — `build_routes` takes a principal/session resolver instead of an
  `OrgSessionStore` directly — and are mounted in local mode too, linked from the Settings page's
  General tab. This is enrollment only: nothing in local mode yet checks for or demands an
  assertion before releasing an approval (that's Phase 2), and `require_passkey` has no enforcement
  path there either (Phase 3) — an enrolled local-mode passkey today is inert, not a control
  already in effect. This phase was gated on issue #428's Phase 4 (privilege separation) having
  landed for the platform in question, since local mode's credential store is otherwise writable by
  the same agent the check is meant to defend against — see issue #426 for the full dependency
  reasoning, and the "Local-mode trust boundary" section of `docs/security-and-compliance.md` for
  what still doesn't hold until later phases land. See issue #426.
- Issue #426 Phase 2: local mode's `/api/approvals/{id}/decide` endpoint (`web/routes_approvals.py`)
  now demands a fresh WebAuthn assertion before releasing an approving decision (`accept`/
  `accept_all`) on a write, or on a PII-flagged read when `step_up.scope` is set to
  `writes_and_pii_reads` — ported from `web/routes_org_approvals.py`'s own decide-time gate, minus
  the IdP re-authentication fallback local mode has no equivalent of. A first attempt with no
  `webauthn_assertion` gets a `428` carrying fresh assertion options when a passkey is enrolled; a
  second attempt with a valid, decision-bound assertion completes the decision. `deny` never needs
  step-up. This is still opt-in machinery, not the guarantee issue #426 exists for: with no passkey
  enrolled, the `428` has no options to offer and the decision is let through unguarded rather than
  left permanently stuck — the one place step-up stays evadable at this phase, closed by Phase 3's
  `require_passkey` enforcement, not this one. See issue #426.
- Issue #426 Phase 3: `step_up.require_passkey` is now enforced in local mode, on both surfaces an
  agent could otherwise use to route around it. `web/routes_approvals.py`'s decide endpoint
  hard-fails (`403`, naming `/security`) instead of letting an approving decision through unguarded
  when nothing is enrolled — closing Phase 2's own deliberate gap. `web/routes_settings.py`'s
  sensitive settings actions (the rule-row, grant, policy and PII actions in its new
  `_SENSITIVE_ACTIONS`, out of the dispatcher's ~30) now demand the same fresh assertion before
  applying, so adding an always-allow rule or a broader grant can no longer substitute for a forged
  approval; a test asserts every allowlisted action is classified sensitive-or-not, failing when a
  future action lands in neither set. `web/routes_security.py` gates deleting your *last* enrolled
  credential behind a fresh assertion too, regardless of `require_passkey` — removing it is what
  would silently turn a mandatory install back into an unenforced one. A daemon started with
  `require_passkey` on and nothing enrolled still starts (refusing to boot would remove the only path
  to `/security` that fixes it) but logs a warning and shows a new persistent banner
  (`web_shell.wrap`'s `banner_html`) on every `/approvals`/`/settings` page until a passkey is added.
  See issue #426.
- Issue #426 Phase 4: tamper-evidence, recovery, and an honest write-up for local-mode step-up.
  Enrolling or removing a passkey, spending a recovery code, and `step_up.require_passkey` itself
  being turned on or off (at this phase, observable only at daemon startup, since there was no UI
  path to flip it at all yet — see `step_up_config.py`; B9 above adds one for turning it on) are all
  written to the audit log. Turning the requirement off latches a
  persistent banner on `/approvals`/`/settings` and a daemon-log warning that survives further
  restarts, not just a one-time audit line, until a later startup turns it back on. `web/
  routes_security.py`'s enrollment flow now issues a one-time recovery code — shown to the browser
  exactly once, stored only as a salted hash — the moment a principal doesn't have an unused one on
  file, and a new `POST /security/recover` endpoint trades a valid code for the removal of every
  credential enrolled for that principal, no WebAuthn ceremony required, so someone who loses their
  only authenticator (a new machine, a wiped TPM) has a sanctioned way back in instead of the
  shell-edit-and-restart door this feature exists to close. `docs/security-and-compliance.md` gets a
  new "Tamper-evidence and recovery" subsection and an honest revision of the MCP-issued-sign-in-link
  net-effect paragraph: with privilege separation active and `require_passkey` on, a session can no
  longer release a gated write or loosen policy on its own, though it can still be minted and still
  reaches the review screen. See issue #426.
- `docs/security-and-compliance.md`'s "What it deliberately does not close" paragraph named
  "integrity is the strong guarantee — the agent cannot approve its own request" as an unconditional
  property. #426 Phase 4 (above) revised the neighboring sign-in-link paragraph to say this holds
  only with privilege separation active, `step_up.enabled`, `step_up.require_passkey`, and a passkey
  enrolled all together — but left this sentence unrevised, so it still overstated the guarantee.
  It now names the same four preconditions and says plainly that on a default install, where none of
  them holds, a session alone is still sufficient to approve its own request. Nothing about the
  implementation changed; this corrects what is claimed for it.
- B9 of the 4.1.0 action plan: local mode's step-up requirement can now be turned on from the
  Settings page, not only by editing `config/settings.yaml` and restarting the daemon — #426 shipped
  the whole enforcement chain and then defaulted it off with no way to flip it back on short of a
  shell, which on a privilege-separated install means `sudo` and a text editor for the release's own
  headline security feature. Once a passkey is enrolled at `/security`, the General page's Security
  card gets a "Turn on" control (`enable_step_up`) that sets `step_up.enabled` and
  `step_up.require_passkey` together and takes effect immediately, with no daemon restart — the next
  write approval already demands the assertion. The action is refused, config untouched, unless a
  passkey is already enrolled, and is itself an audited, step-up-gated sensitive settings action once
  step-up is already on. One-directional by design: turning the requirement back off still has no UI
  path and remains a `config/settings.yaml` edit plus a restart, which is what keeps the existing
  "treat this install as compromised" banner meaningful — a disable it observes still can never have
  come from a browser control. See `step_up_config.py`'s `LiveStepUpConfig`.
- B10 of the 4.1.0 action plan: on a privilege-separated install, the companion app's own control
  channel (`OPEN <url>`, `web/control_channel.py`'s `CompanionChannelServer`) now refuses a
  connection unless it comes from the daemon's service-account uid. The socket is `0660`
  group-shared with the agent (same as the daemon's own MINT/QUIT channel), and before separation
  that sharing is exactly ADR 0002 decision 6's deliberate trade-off — companion, agent and daemon
  are all one uid, so no peer check could tell them apart. Separation changes that for this one
  channel: the daemon moves to a different account while the companion and the agent stay on the
  logged-in user's, so `SO_PEERCRED`/`LOCAL_PEERCRED`'s uid becomes meaningful here for the first
  time, and an agent sharing the group could previously send `OPEN` itself to drive the human's
  browser to an attacker-chosen http(s) URL. The daemon's own MINT/QUIT channel is unchanged — ADR
  0002 decision 6 still applies there. See `privilege_separation.service_account_uid()`.
- B11 of the 4.1.0 action plan: `PRIVACYFENCE_SYSTEM_ROOT` (`privilege_separation.py`'s
  test/development escape hatch for relocating a separated install's authority root) is now
  refused on a genuinely separated install instead of being honoured unconditionally. The
  daemon's own environment is controlled by launchd/systemd, but the companion app and the MCPB
  shim read this variable too, and *their* environment is whatever the signed-in user's session
  set — exactly the boundary privilege separation exists to hold. `system_root()` and the shim's
  `privilegeSeparationRoot()` now check the platform's real default root for an already-provisioned
  marker before trusting the override; once one exists there, a user-session process can no longer
  redirect itself onto a root it controls instead of the one the installer provisioned and locked
  down. The override still works exactly as before on the common case — a dev/CI machine, which
  has no real marker at that literal system root to begin with.
- B13 of the 4.1.0 action plan: the Slack/Salesforce/Atlassian OAuth loopback listener
  (`oauth_loopback.py`) no longer inherits `HTTPServer.allow_reuse_address`. On a privilege-separated
  install the agent is a different, less-trusted process than the daemon (ADR 0002) and could bind
  the fixed redirect port first; PKCE already stops it from completing the exchange, but leaving
  address reuse on meant the daemon's own bind() could still silently succeed over that squatted
  port on Windows, where `SO_REUSEADDR` on a *new* socket lets it steal a port another socket is
  actively listening on regardless of that socket's own options — leaving it undefined which of the
  two processes actually received the provider's callback. With reuse off, that bind() now always
  fails, which the existing actionable `OAuthLoopbackError` already reports.
- B23 of the 4.1.0 action plan: local mode's `/approvals` page now says, once, when step-up isn't
  actually protecting anything — B9 gave the requirement a browser-reachable on switch, but the
  default is still off and nothing said so. The two banners that already existed both fired on
  transitions or misconfigurations (`step_up_config.py`'s `local_enrollment_banner` once
  `require_passkey` is already in force and nothing is enrolled; `webauthn_stepup.py`'s
  `step_up_disabled_notice` once a disable transition has been latched), so a fresh install — or any
  install that has simply never turned this on — showed an approvals page that looked complete while
  an agent session could still approve its own writes, with no hint beyond the Security card in
  Settings. A new `StepUpConfig.off_notice()` fires exactly when step-up isn't genuinely required
  (`enabled and require_passkey` together), and `/approvals` renders it as a dismissible strip
  (`web_shell.wrap`'s new `dismissible_notice_html`) with a link to turn it on — advisory, not an
  alarm, so it stays dismissed in that browser once seen rather than nagging on every visit for as
  long as the install stays in its default state.

### Added

- Policy v2 redesign, P3: a new `policy/engine.py`/`policy/compat.py` evaluator for auto-accept
  rules, built on the P1 tool registry and P2 scope/condition selectors, now runs alongside the
  existing `AutoAcceptEvaluator` on every gated call (`gate.py`, shadow mode). Nothing on disk
  changes, and nothing about what auto-accepts changes by default: the existing evaluator keeps
  deciding, and a disagreement between the two is logged once at `WARNING` (operation key, each
  side's matched rule, a redacted context fingerprint — never call content) rather than acted on.
  A new `policy.engine: v1 | v2` key in `config/settings.yaml` (default `v1`) is the switch for
  when the new evaluator becomes authoritative instead; flipping it back to `v1` is the documented
  rollback, no release needed.

- A new `privacyfence_status` meta-tool: the one tool guaranteed to exist even on a fresh,
  un-onboarded install, so an empty or partial tool list reads as "not set up yet, here's how to
  fix that" instead of "PrivacyFence has nothing to do with this". Reports which connectors are
  authenticated (and, for the rest, whether they were never configured, never authenticated, or
  hit a real error). It never mints a sign-in credential itself: in local mode, when nothing is
  authenticated yet, it tells the model to offer the human a one-time sign-in link and only mint
  one (via `privacyfence_get_sign_in_link`) if they say yes — a bootstrap code is a live
  credential, and minting one because a model decided to check status rather than because a human
  asked is a wider grant than this tool is meant to be. See issue #396.
- The MCP server now returns `instructions` in its `initialize` response, telling the connecting
  client what PrivacyFence is and that an empty or partial tool list means its connectors aren't
  set up yet, not that PrivacyFence has nothing to do with the conversation — and when to call
  `privacyfence_status` to find out more. Previously the `initialize` result carried no
  instructions at all, so a fresh install had no way to explain its own silence. See issue #396.
- `privacyfence_status` and `privacyfence_get_sign_in_link` (which now also accepts `page:
  "connectors"`) hand back a link straight to Settings' Connectors section — a new `GET
  /settings/connectors` route — instead of landing an un-onboarded user on the General page with
  no indication of what to do next. That page also shows a short, dismissible welcome banner
  explaining what PrivacyFence does and the order of setup steps while no connector is
  authenticated yet. See issue #396.
- Authenticating, disabling, or refreshing a connector now pushes a real MCP `tools/list_changed`
  notification to every open Streamable HTTP session, so a client that already connected picks up
  the new tool list without needing to reconnect. See issue #396.
- The Windows installer now offers to open the bundled `.mcpb` at the end of setup (checked by
  default, alongside "Launch PrivacyFence now"), so Claude Desktop's install prompt appears
  automatically for most users instead of requiring them to locate the file in File Explorer
  first. See issue #407.
- Gmail draft bodies (`body_markdown` on all 6 draft tools) now support `# Heading 1`/`## Heading 2`
  syntax, rendered as Gmail's own "Large"/"Huge" font-size compose presets (not raw `<h1>`/`<h2>`
  tags, which render inconsistently across mail clients). See issue #414.
- Calendar events can now be given a color. `calendar_create_event`/`calendar_update_event` accept
  a `color` parameter, and a new `calendar_set_event_color` tool changes just that field on an
  existing event, mirroring `calendar_set_event_visibility`. A new `calendar_list_colors` tool
  lists Calendar's fixed color palette (id, name e.g. "Tomato", hex background/foreground) so a
  color can be picked by name instead of a numeric id. See issue #414.
- Python 3.14 is now covered by CI. The `test-python-compat` job's matrix runs the core suite on
  3.11, 3.12 and 3.14 (3.13 is the full `test` job's own version), so the interpreter that is the
  default `python3` on current Ubuntu releases is proven rather than merely implied by
  `requires-python = ">=3.11"`.

### Changed

- Every daemon log line now carries the running `privacyfence` version (e.g. `v4.0.1`, or the
  `setuptools_scm` dev form like `v4.0.1.dev3+gabc1234` between tags) right before the log level,
  so a log excerpt is self-describing without having to correlate it against when a build was
  installed. `setup_logging()` in `daemon_main.py` is the one place the format string lives, so
  every logger in the process picks this up.
- The README's Quick start steps for all three local-mode installers (DMG, Windows, `.deb`) now
  say "ask Claude to set up PrivacyFence" instead of "ask Claude for a sign-in link" — the latter
  named a specific tool (`privacyfence_get_sign_in_link`) a user had no way to know about unless
  they'd already read this far; the former matches what a fresh install's own `initialize`
  instructions already tell Claude to do on its own via `privacyfence_status`. The step also now
  says the link lands on Settings' Connectors page rather than Settings in general. See issue #396.
- A pending approval's `message` field, and `privacyfence_await_approval`'s own tool description,
  now explicitly tell the calling agent to relay the approval `url` to the user right away and
  either await or schedule a follow-up check, instead of leaving the agent to sit on a
  `approval_pending` result quietly. PrivacyFence itself was already returning the `url` and a
  poll tool (`privacyfence_await_approval`) alongside every pending approval — this only
  strengthens the in-band instructions an MCP client sees, since a daemon has no way to push a
  notification into a chat turn on its own.
- The README's "Install on Windows" steps now say where `PrivacyFence.mcpb` actually lands
  (`%ProgramFiles%\PrivacyFence\`, or `%LOCALAPPDATA%\Programs\PrivacyFence\` for a non-elevated,
  current-user-only install) and how to get there in File Explorer, instead of just saying to
  install it with no path given, for the case where the new automatic prompt above was declined.
  See issue #407.
- `privacyfence_get_sign_in_link`'s result text is now a single markdown link (naming the
  10-minute expiry in the link text itself, e.g. "Sign in to PrivacyFence — one-time link, expires
  in 10 minutes") instead of a raw `{"url": ...}` JSON blob, so a client that renders tool text as
  markdown shows something clickable instead of a link a human has to copy out by hand, and the
  expiry stays legible even if only the link text survives into a screenshot or shared transcript.
  `structuredContent` is unchanged.
- Windows installer/executable signing now goes through SSL.com's eSigner CodeSignTool instead of
  a locally imported Authenticode `.pfx`. CA/B Forum's June 2023 key-storage rules mean code-signing
  private keys can no longer be issued as an exportable `.pfx` at all — SSL.com holds this one in
  its eSigner cloud HSM — so `build.yml`'s `build-windows` job and `scripts/build_installer.ps1`'s
  `Invoke-Signing` helper now authenticate to eSigner per signing call (`ESIGNER_USERNAME`/
  `ESIGNER_PASSWORD`/`ESIGNER_CREDENTIAL_ID`/`ESIGNER_TOTP_SECRET`) rather than reading
  `WINDOWS_CERTIFICATE`/`WINDOWS_CERTIFICATE_PWD`. See `docs/platform-support.md`.

### Fixed

- An approval that resolved without a human clicking a button — its pending TTL lapsing
  (`pop_expired_events()`), or an auto-accept rule appearing while it was still waiting
  (`reevaluate_all()`) — no longer leaks the worker thread that was blocked showing its card.
  `approvals.PendingApproval.finalize()`/`pop_expired_events()` used to set only the
  approval-level `finalize_event`, never the UI-step `event` that `web_prompt.block_on_card`
  actually blocks on (only a human's decision, via `answer()`, ever set that one) — so the
  `gate.py` popup-executor worker driving that card's interaction never returned. Eight such
  approvals (the executor's worker count) and the daemon could no longer render any approval
  card at all, without a restart. Both paths now wake the UI step too; the interaction's own
  eventual `finalize()` call is a harmless no-op once the real outcome is already recorded.
- `GET /approvals/{id}` no longer 500s for a genuinely pending approval whose card hasn't been
  rendered yet. Card HTML is only built on `gate.py`'s dedicated popup executor (`build_card_html`
  runs from inside `show_popup`/`show_read_popup`, on that worker thread); once every worker is
  occupied showing an earlier card, a newly registered approval is listed and decidable but its
  `card.html` is still `""`, which crashed `_inject_shim`'s `html.index("</head>")`. The card page
  now serves a "preparing this request" placeholder that auto-refreshes instead, in both local
  mode (`web/routes_approvals.py`) and org mode (`web/routes_org_approvals.py`).
- Org mode: restarting the daemon no longer forces every connected MCP client through a full
  browser sign-in. The OAuth refresh tokens `/mcp` clients hold are now persisted across a
  restart, so the ordinary silent-refresh path survives one and a client re-authenticates with
  nobody present. Previously every token store was in-process only: a restart emptied them, the
  refresh path was unavailable along with everything else, and the client had to redo the whole
  `authorize → IdP redirect → sign-in → code exchange` round trip. For a human at a browser that
  was an annoyance; for a scheduled or background tool call it was a dead end, because there is
  nobody there to complete a redirect. Each record is sealed under a key derived from the refresh
  token itself rather than one the daemon keeps, so the file is inert without a token that was
  already valid — encrypting under a daemon-held key would have moved the secret rather than
  protected it. Access tokens (one hour, re-minted by the refresh) and browser sessions (a human
  is present by definition) are still deliberately in-memory only, and every revocation path —
  logout, `/revoke`, rotation, the 30-day chain cap — clears the persisted record too. See issue
  #402.
- A client holding the session id of a Streamable HTTP session that no longer exists — after a
  daemon restart, or an eviction — can now recover instead of being refused for the life of its
  own process. `/mcp` answered any request naming an unknown session with `404 Session not
  found`, which is correct by the spec and fatal in practice: neither official MCP client
  transport clears its stored session id on a 404, so it kept stamping the dead id on everything
  it sent, `initialize` included, and every one of those was refused on account of the id rather
  than judged on its own merits. An `initialize` that arrives carrying an unknown session id now
  opens a fresh session. Requests that genuinely need the session they name (a GET reopening an
  SSE stream, a DELETE, any non-`initialize` POST) still get today's 404. The bundled `.mcpb`
  shim retries the same frame once without the stale id, so a shim newer than the daemon it talks
  to recovers as well. See issue #402.
- The `/approvals` and `/settings` pages no longer get logged out from under a tab that's been
  open and actively watching (live SSE indicator, incoming approvals rendering) for longer than
  the 30-minute idle timeout. The session was only ever touched once, when the stream connected —
  watching it registered as zero activity — so the next click or refresh after 30 minutes returned
  401 even though the page still reported itself as live. The stream now refreshes its own session
  on every poll tick; an open connection is itself proof the tab is open, so the 24-hour absolute
  cap is the only cap left for a tab that's never closed. The live indicator also now reports
  "session expired" instead of a permanent, misleading "reconnecting…" once the browser gives up
  for good, and the expired-session page leads with "ask Claude for a new sign-in link" rather
  than burying it a paragraph down. See issue #423.
- Windows installs now store per-user state (config, credentials, the audit log) under
  `%LOCALAPPDATA%\PrivacyFence` instead of a literal `.privacyfence` folder dropped into
  `%USERPROFILE%`. A dot-prefixed name isn't a hiding convention Windows Explorer honors the way
  it is on POSIX, so it showed up as an ordinary, oddly-named folder sitting directly in the
  user's profile root; `%LOCALAPPDATA%` is the idiomatic per-machine "Known Folder" location
  (hidden by default, and the *Local* rather than *Roaming* one since this directory holds
  credentials and audit logs that shouldn't follow a roaming profile). No migration is provided —
  the Windows build has not had a stable release yet.
- On Windows, the daemon staying down after a reboot is no longer silent on either side. The
  installer now warns (instead of only logging) when it can't register the Task Scheduler
  autostart task, and the Claude Desktop shim's own fallback launch now checks the non-admin
  per-user install location (`%LOCALAPPDATA%\Programs\PrivacyFence\`) as well as
  `%ProgramFiles%\PrivacyFence\` — previously it only checked the latter, so a default (non-admin)
  install left both autostart *and* the shim's self-heal spawn unable to find the daemon, showing
  up as an MCP "unable to connect" with nothing in Task Manager.
- Org mode's approval page no longer shows the WebAuthn step-up helper's JavaScript source as
  literal visible text above the approval card. `_org_bridge_shim` concatenated it ahead of its
  own `<script>` tag instead of inside one, so the browser rendered the function bodies as page
  content and passkey step-up never actually ran.
- Authorizing a Google connector in org mode no longer fails with `Scope has changed from "..." to
  "..."`. The authorization request asked Google for incremental authorization
  (`include_granted_scopes`), so the token came back covering every scope that OAuth client already
  held for the user and the exchange rejected it — which broke the second Google connector always,
  and the first whenever the same client also served org-mode sign-in. The request no longer asks
  for it, and a granted scope wider than the requested one is accepted rather than refused; a
  grant *missing* a requested scope is still an error.
- Org mode now rejects an `org_config.json` whose `server.issuer_url` is not an absolute `http(s)`
  URL with a hostname, naming that key, instead of starting and then answering every request with
  `Invalid Host header`. Surrounding whitespace in the value is stripped rather than silently
  becoming part of the hostname the Host allowlist is built from.
- The org-mode startup log line now lists the `Host` header values the daemon accepts, so a reverse
  proxy forwarding a hostname the bundle doesn't name is diagnosable from `journalctl` alone.
- Org-mode sign-in no longer ends in an infinite redirect loop. The browser session cookie was
  `SameSite=Strict`, which a browser withholds on the landing request after the identity provider's
  redirect — so the post-login page saw no session and bounced back to `/login`, where the
  already-consented IdP sent the browser straight back. The cookie is now `SameSite=Lax`; CSRF
  protection is unchanged (the double-submit token and `Origin` check guard every mutation).
- Auto-accept rules and resource grants configured in an org-mode user's `settings.yaml` are now
  actually applied. Every principal's rule evaluator was left empty regardless of what that user's
  `settings.yaml` said, so each gated call went to a human approval even when a configured rule
  covered it, and unattended sessions could make no progress at all. `privacyfence_check_policy`
  reported `No auto-accept rule is configured for this operation` for operations that plainly had
  one, while `privacyfence_list_auto_accept_rules` — which reads the file from disk — kept listing
  it; the two meta-tools now agree. Local mode was never affected, and no call was ever
  auto-accepted that shouldn't have been: the failure was always toward asking a human.
- Org-mode clients are now offered their own connector tools over `/mcp`. The tool listing was
  built outside the signed-in principal's scope, so it enumerated the *local* principal's
  connectors; on an org server nobody authorizes services as `local`, so every connector was
  skipped and the advertised tool list collapsed to PrivacyFence's own meta-tools. Gmail, Drive,
  Slack and the rest were invisible to Claude in org mode — calls to them resolved correctly, but
  no client could discover the tools existed to make one. Local mode was never affected, and a
  principal is never shown tools backed by another principal's credentials.
- The PyPI project page is no longer bare. `pyproject.toml` now declares `[project.urls]`
  (Homepage, Download, Documentation, Source, Changelog, Issues, Security) and `classifiers`, so
  the sidebar on `pypi.org/project/privacyfence/` links back to the site and repo and the project
  is classified (Development Status, License, Operating System, Intended Audience, Topic) rather
  than surfacing in no browse facet at all. README.md — which is the PyPI long description — had
  30 relative doc links and 4 relative screenshot `<img>`s that only resolve on GitHub; those are
  now absolute (`github.com/.../blob/main/...` for docs, `raw.githubusercontent.com/.../main/...`
  for images), and the three `../../releases` download pointers now point at
  `privacyfence.eu/download/`, the canonical download surface. See issue #370.
- `drive_download_file`, `gmail_download_attachment`, and `confluence_download_attachment` no
  longer fail with a bare, unhelpful "Tool call failed" when `destination_dir` can't actually be
  written to (a permissions error, or — as observed on macOS — the synthetic `/home` mount point,
  which rejects any direct `mkdir`/`open` under it with `[Errno 45] Operation not supported`). The
  underlying `os.makedirs`/`open` failure is now caught and re-raised as the connector's own
  `*ClientError` naming the path and asking for a different `destination_dir`, matching every other
  failure path these methods already had — previously the raw `OSError` skipped that wrapping
  entirely and fell through to the generic client-facing error message, leaving the calling agent
  with no way to tell what went wrong or that retrying with a different directory would help.
- `apt remove` on a Linux install that privilege separation (auto-enabled by `postinst`, issue
  #428 D1) turned on no longer strands it. `debian/prerm` now runs
  `privacyfence-privilege-separation disable` on a real `remove` — before dpkg deletes the binary
  that command needs — so the system unit is stopped and removed and the migrated data, including
  live connector OAuth tokens and the audit log, moves back under `~/.privacyfence` instead of
  being left behind in a `0700` directory the user can no longer read, owned by an account whose
  only undo tool was just uninstalled. The operation is best-effort and never runs on a plain
  upgrade, which must leave a separated install's data and account in place — it just gets briefly
  stopped and restarted there too, see below.
- A separated install's daemon (`privacyfence-daemon.service`, a packaged PyInstaller onedir
  build running straight out of `/opt/privacyfence`) no longer risks crashing partway through a
  `.deb` upgrade. dpkg unpacks the new version's files over that same directory before `postinst`
  gets a chance to stop and restart the unit, so a shared library the still-running old process
  lazily loads could vanish out from under it mid-upgrade. `debian/prerm` now stops
  `privacyfence-daemon.service` first, on `upgrade`; `postinst`'s `enable --auto`, which already
  runs on every upgrade (issue #428 D1), starts it again once the new files are in place, so the
  daemon never ends up left down. A no-op, as before, on an unseparated install, which has no such
  unit.
- Issue #428 B8: `debian/postinst`'s header comment no longer claims installing the `.deb` never
  starts the daemon. That was true before D1 but not after: `enable --auto`, right below it, now
  starts `privacyfence-daemon.service` immediately (`systemctl enable --now`) whenever it can
  safely tell who owns the install — the comment now says so instead of asserting the opposite
  unconditionally. `test_deb_autostart_activates_daemon_via_real_login_session` had the same bug
  in test form: its "install must never start the daemon" assertion checked the daemon's pre-D1
  socket path under `~/.privacyfence`, which privilege separation moves out from under it, so the
  assertion could never fail regardless of what actually happened — fixed as part of splitting
  that test into separated/unseparated cases (issue #428 B7).
- Issue #428 B14: two admins saving install-wide privacy/PII policy from `/settings/privacy` at
  nearly the same moment no longer race to last-write-wins on `settings.yaml`.
  `org_install_policy.apply_change`'s read-modify-write-and-adopt sequence is now serialized by a
  module-level lock, so the second admin's save always starts from a `settings` that already
  reflects the first's rather than overwriting it as if it had never happened. `docs/
  org-mode-setup-guide.md` also no longer tells operators they can freely hand-edit `settings.yaml`
  between browser saves: `apply_change` rewrites the whole file from its own in-memory copy, so any
  hand edit made since the daemon last loaded the file — including comments — is silently discarded
  the next time an admin saves from the browser, restarted or not.
- Issue #428 B17: `linux-graphical-session.yml`'s path triggers never gained
  `scripts/linux_privilege_separation.sh` or `installer/linux/**`, the way `windows-graphical-
  session.yml` gained its own `.ps1` when B5c landed. A change to the Linux privilege-separation
  script or the unit templates it renders is exactly the kind of change most likely to break
  Linux autostart, and it now re-runs the only test that exercises it instead of waiting for the
  next `main` push that happens to touch something else on the existing path list, or the weekly
  schedule.

## [4.0.0] — 2026-09-14

PrivacyFence 4.0 moves the entire user interface off macOS-native AppKit and onto a local web
server, ships on Windows and Debian/Ubuntu for the first time, and adds a centrally managed
organization mode. If you are on 3.x, read "Upgrading from 3.x" at the end of this entry before
installing — the menu bar icon you use today no longer exists.

Rolls up every `v4.0.0-alpha*` / `v4.0.0a*` pre-release.

### Added

- **Windows support.** A signed Inno Setup installer (`PrivacyFence-<version>-setup.exe`) installs
  to `%ProgramFiles%\PrivacyFence\`, registers a Task Scheduler task so the daemon starts at
  login, and starts it immediately. A repeating time trigger on that task brings the daemon back
  after a crash.
- **Debian/Ubuntu support.** A `.deb` package (`sudo apt install ./privacyfence_<version>_amd64.deb`)
  installs to `/opt/privacyfence` and adds an XDG autostart entry, so the daemon starts at the next
  graphical login. Install, remove, purge, and upgrade are exercised by an automated lifecycle test
  on every release build.
- **Organization mode** — a centrally managed deployment where people sign in with your own
  identity provider over OIDC instead of authenticating connectors individually. Each principal
  gets isolated connector credentials, approvals, and audit trail; write approvals can require a
  WebAuthn step-up confirmation; and an application-level authorization allowlist sits on top of
  whatever the IdP already enforces. Organization configuration bundles are Ed25519-signed and
  verified before they are trusted.
- **MCP over Streamable HTTP** at `/mcp`. An MCP client now talks to the daemon over HTTP rather
  than through a local stdio process, which is what makes a shared, centrally hosted deployment
  possible at all.
- **`privacyfence_get_sign_in_link`** — a meta-tool that asks the daemon for a link to the
  approval/settings UI, so you can get into the UI by asking Claude for the link instead of hunting
  for a URL. See "Security" below for its deliberate lack of gating.
- **Approval notifications**, with a configurable detail level, and **deferred approvals** so
  several concurrent requests queue for review instead of blocking each other.
- **Settings on the web** — connector authentication, privacy filter, auto-accept rules, and
  organization configuration are all managed from the browser UI on every platform.
- **`pip install privacyfence`.** Stable releases now publish an sdist and wheel to PyPI (staged
  through TestPyPI first), authenticated with PyPI's Trusted Publisher OIDC rather than a stored
  API token.
- **CycloneDX SBOMs** — one for the Python runtime and one for the Node shim — generated and
  published with every release.
- **A download page and release archive.** `privacyfence.eu/download/` is served by a Cloudflare
  Worker in front of a private R2 bucket that holds every artifact of every release, stable and
  pre-release alike, and counts installer downloads as they are served. Pre-release channels are
  offered there too, so testers need no credential to get a build.
- **Organization-mode file delivery** — inline and staged download paths for attachments in a
  centrally hosted deployment, where the file cannot simply be written to the user's own disk.
- **Audit-log append integrity and centralized forwarding**, so a deployment can verify its log
  has not been rewritten and ship entries to a central collector.

### Changed

- **The macOS UI is the same web UI as every other platform now.** There is no menu bar icon any
  more, and no window to open: approvals and settings live in your browser, at the daemon's local
  URL. This is the single most disruptive change for a 3.x user. PrivacyFence no longer has an
  AppKit/PyObjC runtime dependency at all.
- **Versions come from git tags.** `setuptools_scm` derives the version from the tag at build time;
  there is no version string anywhere in the source tree and no version-bump commit. A shallow
  clone with no tag history resolves to a placeholder version rather than a real one.
- Attachment text extraction parses embedded XML with `defusedxml` instead of the standard
  library's `ElementTree` — see "Security" below.
- The project moved to the `privacyfence` GitHub organization, and the contact address is now
  `info@privacyfence.eu`.

### Removed

- **The legacy Node stdio bridge (`bridge/`).** It is replaced by `PrivacyFence.mcpb`, whose shim
  is a thin stdio-to-Streamable-HTTP transport proxy: it carries no tool-schema knowledge of its
  own, so it never needs to be kept in step with the daemon's tool definitions the way the bridge
  did.
- The native macOS menu bar app and its AppKit approval/settings windows, superseded by the web UI
  above.

### Fixed

- **The documented first-run sign-in path never worked.** The daemon has always logged the URL to
  its approval UI on startup, and the README told you to read it there — but every logger in the
  process runs through the secret-redacting formatter, which matched the word `bootstrap` and
  scrubbed the code out of that line before it reached a terminal or a file. Restarting produced
  an equally redacted line, and the menu bar fallback had already been removed with the rest of the
  native UI. The link is now written to `~/.privacyfence/settings_url`, rewritten fresh on every
  startup, and can also be fetched over MCP with `privacyfence_get_sign_in_link` (commit
  `2a984a1`).
- **The `.mcpb` shim failed silently in three ways**, each presenting to the user as "Claude cannot
  connect to PrivacyFence" while the daemon log showed nothing at all: it refused to start on any
  command-line flag it did not recognize, it never named what it was waiting on during a connection
  wait (commit `7a9c98d`), it dropped a request outright when a forward failed instead of answering
  it, and a rejected request could pin it to a session that was already dead.
- Windows autostart registered the task but the daemon then killed itself at startup over its own
  instance lock.
- `atomic_write_bytes` retries `os.replace` on the transient `PermissionError` Windows raises when
  another process still holds the destination open.
- Ciphertext orphaned by a daemon restart is swept rather than left behind.
- `--atlassian-oauth` no longer fails when Atlassian's accessible-resources response splits a single
  site across entries; the callback URL uses the shared grant key.
- Drive API calls catch every exception, not just `HttpError`.

### Security

- **`privacyfence_get_sign_in_link` is deliberately not gated.** It issues a single-use,
  short-lived, localhost-only bootstrap credential for the approval UI, in local mode only, and it
  does so without asking for approval first — because the approval would have to be granted in the
  very UI the user cannot reach. An MCP client that can call this tool already holds
  equivalent-or-greater access through every other tool the daemon exposes, so this is not a new
  trust boundary. Every issue is recorded in the audit log under its own `sign_in_link_issued`
  decision.
- **Known limitation:** every tool advertised over `/mcp` carries the same read-only,
  non-destructive annotations regardless of its real effect. Those are MCP client UI hints, not a
  security boundary — the gate in the daemon is the real authorization. Whether that uniform
  advertisement should change is tracked in
  [issue #46](https://github.com/privacyfence/privacyfence/issues/46).
- The `starlette` floor was raised to 1.3.1, covering five CVEs found live in the previously
  permitted range. Two of them mattered directly here: an unvalidated `Host` header or request path
  could shift `request.url`'s authority, which both session-auth origin checks compare against as
  defense-in-depth behind the CSRF double-submit token.
- Attachment XML (DOCX/PPTX parts) is parsed with `defusedxml`, and zip-member decompression is
  capped — this content is attacker-controlled and is parsed *before* anyone approves anything.
- A security remediation programme closed a numbered list of findings across the new web surface,
  including: URL-scheme validation in the Markdown renderers; identity-rule spoofing through
  substring matching; spreadsheet formula injection in the audit export; session-based
  authentication replacing a persistent token; OIDC discovery-document validation with enforced
  HTTPS; absolute lifetime caps on refresh tokens and sessions; sanitized exception messages at
  client and log boundaries; a per-principal approval cap against denial of service; atomic writes
  and restrictive directory permissions; nonce-based CSP with `object-src`/`frame-src` and
  replace-not-extend header handling; HSTS, `Permissions-Policy`, and COOP headers;
  standards-aware `Host`-header allowlist parsing; and dynamic-client-registration resource
  controls on the org-mode OAuth provider.
- Dependency lock files and a scheduled dependency audit now gate the build, for the Python
  runtime and for the download Worker's own tree.

### Upgrading from 3.x

- **There is no menu bar icon.** Nothing opens when you launch the app, by design. To reach
  approvals and settings, ask your MCP client for a sign-in link (`privacyfence_get_sign_in_link`)
  and open it, or open the URL in `~/.privacyfence/settings_url`. Do not look in
  `privacyfence.log` — it redacts the code in that link on purpose.
- **Reinstall the Claude Desktop extension.** The 3.x bridge is gone; install the `PrivacyFence.mcpb`
  from this release. On macOS the shim also starts the daemon for you, so there is no separate
  "open the app first" step.
- **Your configuration and audit log stay where they are** (`~/.privacyfence/`). There is no config
  migration to run.
- **Claude Desktop has no Linux build.** On Debian/Ubuntu, connect an MCP client that speaks
  Streamable HTTP to the daemon's `/mcp` endpoint instead of using the `.mcpb`.
- **Organization mode is opt-in.** A local install behaves as before unless you install an
  organization configuration bundle.

## [3.4.7] — 2026-09-03

### Changed

- `drive_get_file_content` reads a Google Doc through the Docs API's structured document instead of
  a flattened plain-text export, and returns Markdown in the same dialect the Docs write tools
  accept — so a document you read round-trips back into `drive_write_doc_content` or
  `drive_docs_edit_content`. Headings, bold, italic, strikethrough, underline, inline code, links,
  highlights (including nested combinations), horizontal rules, nested lists, and real GFM tables
  with column alignment are all preserved. Non-default highlight and text colors come back as
  separate `highlights`/`text_colors` lists, since Markdown has no syntax for an arbitrary color.

  Note for anyone scripting against these tools: `find_text` still matches the document's plain,
  unformatted text, not the Markdown this now returns.

## [3.4.6] — 2026-09-02

### Added

- `drive_sheets_get_values` can read formulas and cell formatting, not just displayed values:
  `value_render_option` chooses between the formatted value, the raw value, and the formula text,
  and `include_formatting` returns a per-cell grid of bold/italic/colors/number format/alignment/
  wrap alongside the values.

## [3.4.5] — 2026-09-02

### Added

- `drive_sheets_format_range` gained cell text wrap (`wrap_strategy`) and vertical alignment
  (`vertical_alignment`), matching the Sheets UI's Overflow/Clip/Wrap and Top/Middle/Bottom options.

### Fixed

- Nested inline Markdown in the Google Docs write tools (a highlight wrapping bold, for example)
  silently dropped the inner formatting and leaked literal `**`/`==` into the document.
- `---`, `***`, and `___` thematic breaks were inserted as literal text; they now render as a real
  divider.

## [3.4.4] — 2026-08-28

### Fixed

- Slack performance overhaul: search and the approval popups it feeds were unusably slow.

## [3.4.3] — 2026-08-27

### Fixed

- The credit-card PII pattern no longer matches pair-grouped digit runs, which were producing false
  positives on ordinary numbers.

## [3.4.2] — 2026-08-27

### Added

- PII-refinement trials are captured in the audit log, so a redaction decision can be reviewed after
  the fact.

## [3.4.1] — 2026-08-26

### Added

- Google Apps Script connector.
- Auto-accept Rules page: the rule-type field is a dropdown, grant rows offer right-click copy-ID,
  and the ID hint is tool-specific.

### Fixed

- Re-reading a file PrivacyFence itself just wrote, unchanged, no longer asks for PII confirmation a
  second time.

## [3.4.0] — 2026-08-06

Rolls up `v3.4.0-beta1` through `v3.4.0-beta3`.

### Added

- Weekly-cached Slack user/channel and Telegram chat directories, warmed in the background, with
  manual refresh tools — names resolve without a round trip on every approval.
- Multi-button "Always allow" offering each matching auto-accept candidate rather than a single
  take-it-or-leave-it rule.
- GFM tables in `edit_doc_content`, and support for escaped brackets in Markdown link text.

### Changed

- Approval and settings windows appear only once their WebKit content has loaded, instead of
  flashing empty first, and their buttons moved into that content.
- The remaining `osascript` confirmation and picker dialogs were ported onto the AppKit+WKWebView
  bridge.
- PDF text extraction uses `pypdf` instead of Quartz/PDFKit.

### Fixed

- Slack API rate limits are handled inside paginated directory calls rather than surfacing as an
  error.

## [3.3.1] — 2026-08-04

### Added

- `slack_search_messages` takes a `days` parameter for time-bounded searches.

## [3.3.0] — 2026-08-04

Rolls up `v3.3.0-beta1` and `v3.3.0-beta2`.

### Added

- Slack participant-based lookup in `slack_search_messages`, and Slack message permalink parsing.

### Changed

- The daemon and policy layer were decoupled from the native macOS UI — the first step toward the
  web UI that lands in 4.0.
- The native menu-bar settings were replaced by a webview settings window. The tray item is now
  "Settings…".

### Fixed

- `settings.yaml` corruption caused by a PyObjC string subclass reaching the bridge payload.

## [3.2.0] — 2026-08-03

Rolls up `v3.2.0-beta`.

### Added

- Rich-text (Markdown) email bodies in Gmail drafts.
- `slack_create_group_chat`, for starting a new group DM.

### Changed

- Attachment previews render extracted Markdown instead of a QuickLook thumbnail.
- Drive/Sheets/Docs operation lists were consolidated into single sources of truth.

### Fixed

- The preview pane was missing on `confluence_download_attachment` and on Gmail drafts with
  attachments.

## [3.1.1] — 2026-07-31

### Fixed

- Confluence attachment download: wrong endpoint, missing OAuth scopes, and a missing UI label.

## [3.1.0] — 2026-07-31

### Added

- `confluence_list_attachments` and `confluence_download_attachment`.
- `gmail_*_with_attachments` draft tools.

### Fixed

- Confluence page bodies are converted to plain text before the approval popup and the PII scan, so
  the review shows readable text rather than storage-format markup.

## [3.0.0] — 2026-07-30

### Added

- **Attachment and file preview pipeline** — binary previews travel through the gate, images render
  in the approval window, non-image files fall back to a thumbnail, and PII detection runs on
  attachment, upload, and download content rather than text alone.
- **Privacy Filter window** — set per-category allow/redact/block policy from the menu bar instead
  of hand-editing configuration.
- **Daily update check** against GitHub Releases, with a menu bar alert when a newer version exists.
- Individually toggleable IP-address and financial-figure PII detection.
- `slack_list_dms` and `slack_list_group_chats`, with participant filtering.
- Category-based redaction for Contacts, Tasks, and Confluence, and a Calendar free/busy visibility
  toggle.

### Changed

- **Approval window redesign** — card-stack rendering for the review and popup dialogs, with dark
  mode following the system appearance.
- "Allow for 5 min" folded into Allow, disclosed in a caption rather than occupying its own button.
- The meeting-room directory syncs through a separate, narrowly scoped Google Cloud project instead
  of requiring Workspace admin scope on the Calendar connector.

### Fixed

- `drive_download_file` is gated before content leaves the daemon, not after.
- A crash (`EXC_BAD_ACCESS`) after many approval popups had been shown and dismissed — closed
  windows were hidden rather than released.
- The Privacy Filter window's "Change…" picker did nothing.
- Google Docs table insertion used the wrong start index to find the table it had just created.
- Calendar's "set working location" created a duplicate event instead of updating the existing one.
- The audit log recorded a confirmed-but-no-op rule or grant removal as if it had changed something.
- The update-check alert opens `http(s)` URLs only, falling back to the releases page otherwise.

## [2.0.3] — 2026-07-29

### Fixed

- Gmail draft `To`/`Cc` headers are kept on one unfolded line; folding them was breaking replies in
  Apple Mail.

## [2.0.2] — 2026-07-21

### Fixed

- Real names are resolved for hand-authored auto-accept rule values, not only for grants.

## [2.0.1] — 2026-07-21

### Fixed

- Daemon startup crash when an `auto_accept_rules` operation key was null.

## [2.0.0] — 2026-07-21

### Added

- Connector-scoped auto-accept grants.
- Preflight policy checks and an unattended-session mode for scheduled Cowork tasks.
- Read and propose-write access to auto-accept rules and grants from the bridge.
- Real per-connector brand icons in the approval dialog, and a redesigned approval pane (risk
  spine, quieter "Claude says" copy, link-style buttons).
- Richer Markdown formatting in the Google Docs write tools.

### Changed

- **The MCP bridge was rewritten in Node.js**, so the bundled `.mcpb` no longer ships a Python
  runtime for the bridge process.
- **macOS builds are code-signed and notarized**, removing the Gatekeeper workarounds earlier
  releases needed.
- Menu bar redesign: a rules-manager window, status colors, and label fixes.
- `unattended_sessions.enabled` moved from `settings.yaml` to `org_config.json`.

### Fixed

- The bridge self-heals against a slow or not-yet-ready daemon instead of dying.
- Stale reads from the IPC dedup cache after a same-arguments write.
- Index drift when writing consecutive nested list items to Google Docs, and a table placeholder
  stripped by the Docs API.
- PII and content-flag badges rendered stacked at row 0.
- Two segfaults: closing the Auto-accept Rules window, and a menu rebuild racing an open status-bar
  dropdown.
- Calendar-visibility auto-accept rules, and operations missing from the rules-manager window.

## [1.0.0] — 2026-07-10

First release with a stable connector and policy interface.

### Changed

- The README was split into a product overview plus a separate Technical Reference.

### Fixed

- Duplicate metadata removed from approval popup details.
- HTML emails render as plain text in the review, rather than as markup.

## [0.7.0] — 2026-07-10

### Added

- "Accept for 5 min" — a session-temporary auto-accept for repeated writes to the same file.
- Calendar out-of-office and working-location events; Jira issue transitions and custom fields.
- An `approved_sandbox_folder` rule covering `sheets.rename_sheet` and `sheets.format_range`.

### Changed

- Approval content across Gmail, Drive, Salesforce, Slack, Tasks, Calendar, Telegram, and Jira is
  human-readable rather than raw JSON.
- The PII detection gate applies to the read direction only.

### Fixed

- Sheets auto-accept rules never appeared in the menu.
- A malformed all-day event in `calendar_set_working_location`.

## [0.6.0] — 2026-07-09

### Added

- **PII detection gate** — likely personal data (Hungarian, English, German) triggers an extra
  confirmation, and overrides a matching auto-accept rule rather than being skipped by it.
- Gmail filter list/create/update and label list/create, including nested labels.
- An `approved_task_list` auto-accept rule for Google Tasks writes.

### Changed

- PII detection is scoped to message content, not envelope metadata; email addresses and phone
  numbers alone are no longer flagged.
- `trusted_sender_domain` matches subdomains.

### Fixed

- Identical retried IPC calls are deduplicated, so one request no longer produces two approval
  popups.
- `gated_call` always leaves an audit entry.
- Confluence OAuth tokens refresh on 403 and 404, not only 401.
- A `calendar_get_event_details`/`calendar_update_event` crash from a bad `supportsAttachments`
  argument.

## [0.5.0] — 2026-07-07

Rolls up `v0.5.0` through `v0.5.6` (2026-07-07 – 2026-07-08).

### Added

- A security, privacy, and compliance statement aimed at IT, GDPR, and AI Act reviewers.
- Contact creation and label add/remove in the Contacts connector, with personal and Workspace
  directory contacts kept separate.
- A development-vs-installed setup guide and `dev_start.sh`, for running a source build alongside a
  released one.
- Coding and testing guidelines, plus a test-coverage overhaul across every connector, the IPC
  transport, the menu bar, and the OAuth loopback.

### Fixed

- SSL errors and crashes caused by Google API service objects and TLS state being shared across
  threads (Contacts, Gmail attachment fetches, and others).
- Confluence space resolution crashed on a 404 instead of reporting "not found".
- A `NoneType` crash in auto-accept rule lookup.

## [0.4.0] — 2026-07-03

Rolls up `v0.4.0` through `v0.4.11` (2026-07-03 – 2026-07-06).

### Changed

- **The project was renamed from Loopline to PrivacyFence.**
- Approval popups were replaced by a branded native AppKit window.
- The bridge is distributed as a one-click Claude Desktop extension (`.mcpb`) instead of being
  registered by hand.
- The configuration framework was redesigned: the setup wizard is gone, replaced by an organization
  config bundle and browser-based OAuth.
- All tools are advertised to the MCP client as read-only (see
  [issue #46](https://github.com/privacyfence/privacyfence/issues/46) for the consequences of that
  choice, still open in 4.0).

### Added

- Google Sheets support in the Drive connector.
- `drive_upload_file` for binary uploads, accepting either a local path or `content_base64`.
- `gmail_reply_draft` / `gmail_reply_all_draft` with real thread continuation.
- Auto-accept rules extended to write operations, grouped by connector in the menu.

### Fixed

- Atlassian OAuth migrated to granular scopes (Confluence), with Jira reverted to classic scopes;
  Confluence Cloud calls were missing the `/wiki` path segment.
- The Salesforce OAuth redirect URI now satisfies its HTTPS-callback exception.
- SSL certificate verification failed on machines other than the build machine.
- An IPC line-length limit broke large file reads.
- Jira and Confluence required re-authentication on every app restart.
- Audit log directory mismatch, and a missing Excel export.

## [0.3.1] — 2026-06-29

Rolls up `v0.3.1` through `v0.3.11`, all released on 2026-06-29.

### Added

- `drive_download_file` — streams a large Drive file to disk and returns the path, instead of
  capping content inline.
- `drive_write_doc_content` — writes Markdown to a Google Doc with real rich formatting through the
  Docs API.
- Google Meet links and meeting-room booking on `calendar_create_event` / `calendar_update_event`,
  plus `calendar_list_rooms`.
- `gmail_archive_message`, and an optional `mark_unread` on `slack_send_message`.
- A `day_of_week` field on Calendar event results, so the weekday is never computed from the
  timestamp and got wrong.

### Fixed

- The accept workflow raced itself: approval tools now use a fresh blocking socket per call, so a
  confirm or deny can no longer end in "IPC connection closed" with no way to retry.
- `drive_download_file` on large files hit both an MCP timeout and an `httplib2` SSL bug; it streams
  through an authorized `requests` session now.
- Slack history, thread, and search tools called dict `.get()` on dataclass objects.
- `create_event` no longer forces `timeZone=UTC` over an ISO string that already carries an offset.
- Slack `mark_unread` resolves a user ID to its DM channel, and reports which scope is missing.

## [0.2.0] — 2026-06-25

Rolls up `v0.2.0` and `v0.2.1`.

### Added

- MCP tool annotations (`readOnlyHint`, `destructiveHint`) on every tool, so Claude Code and Cowork
  stop graying out "Allow for all tasks".

## [0.1.0] — 2026-06-25

Initial development releases (`v0.1.0` – `v0.1.3`), published under the project's original name,
**Loopline**.

### Added

- A local approval gate in front of Gmail, Drive, Calendar, Contacts, Tasks, Slack, Telegram, Jira,
  Confluence, and Salesforce, driven from a macOS menu bar app with a setup wizard, and exposed to
  an MCP client through a stdio bridge.
- A Slack setup guide, and two-step-verification (2FA) handling for Telegram in the setup wizard.

### Changed

- Slack uses a single user token (`xoxp-`), with the bot token dropped entirely, so the AI sees
  exactly what you see and no bot is visible to anyone else.

[Unreleased]: https://github.com/privacyfence/privacyfence/compare/v4.0.0...HEAD
[4.0.0]: https://github.com/privacyfence/privacyfence/compare/v3.4.7...v4.0.0
[3.4.7]: https://github.com/privacyfence/privacyfence/compare/v3.4.6...v3.4.7
[3.4.6]: https://github.com/privacyfence/privacyfence/compare/v3.4.5...v3.4.6
[3.4.5]: https://github.com/privacyfence/privacyfence/compare/v3.4.4...v3.4.5
[3.4.4]: https://github.com/privacyfence/privacyfence/compare/v3.4.3...v3.4.4
[3.4.3]: https://github.com/privacyfence/privacyfence/compare/v3.4.2...v3.4.3
[3.4.2]: https://github.com/privacyfence/privacyfence/compare/v3.4.1...v3.4.2
[3.4.1]: https://github.com/privacyfence/privacyfence/compare/v3.4.0...v3.4.1
[3.4.0]: https://github.com/privacyfence/privacyfence/compare/v3.3.1...v3.4.0
[3.3.1]: https://github.com/privacyfence/privacyfence/compare/v3.3.0...v3.3.1
[3.3.0]: https://github.com/privacyfence/privacyfence/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/privacyfence/privacyfence/compare/v3.1.1...v3.2.0
[3.1.1]: https://github.com/privacyfence/privacyfence/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/privacyfence/privacyfence/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/privacyfence/privacyfence/compare/v2.0.3...v3.0.0
[2.0.3]: https://github.com/privacyfence/privacyfence/compare/v2.0.2...v2.0.3
[2.0.2]: https://github.com/privacyfence/privacyfence/compare/v2.0.1...v2.0.2
[2.0.1]: https://github.com/privacyfence/privacyfence/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/privacyfence/privacyfence/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/privacyfence/privacyfence/compare/v0.7.0...v1.0.0
[0.7.0]: https://github.com/privacyfence/privacyfence/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/privacyfence/privacyfence/compare/v0.5.6...v0.6.0
[0.5.0]: https://github.com/privacyfence/privacyfence/compare/v0.4.11...v0.5.6
[0.4.0]: https://github.com/privacyfence/privacyfence/compare/v0.3.11...v0.4.11
[0.3.1]: https://github.com/privacyfence/privacyfence/compare/v0.2.1...v0.3.11
[0.2.0]: https://github.com/privacyfence/privacyfence/compare/v0.1.3...v0.2.1
[0.1.0]: https://github.com/privacyfence/privacyfence/releases/tag/v0.1.3
