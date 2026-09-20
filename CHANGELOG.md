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

### Fixed

- **The packaged-artifact smoke tests now exercise the install a user actually gets, so a release
  build can pass again.** Three consecutive pre-release tags (`v4.1.0b1`/`b2`/`b3`) were lost to
  these tests being behind the product on three separate counts, each one hidden until the one in
  front of it was fixed. They now stand in for the companion to mint a session PrivacyFence can
  attribute to a person (`MINT COMPANION`, the attested shape the self-approval review's Phase 2
  made a precondition for confirming an auto-accept rule), and they enroll a passkey through the
  real `/security` routes before approving anything — which is what a fresh DMG/`.pkg`/`.deb`
  install has demanded since `step_up.require_passkey` started defaulting on for packaged builds.
  The macOS browser scenario does both through a real Chromium with a virtual authenticator, so the
  registration and assertion ceremonies are now covered end to end against the packaged binary
  rather than only in unit tests. No product behaviour changes: the gates were right, the tests
  were asserting the behaviour that preceded them.
- **The Windows installer no longer depends on `Microsoft.PowerShell.Security` being loadable.**
  `privilege-separation.ps1` read ACLs with `Get-Acl`, and on a stock GitHub Actions
  `windows-latest` runner that module refuses to load inside the installer's own
  `powershell -File` invocation — failing the install outright, which is the correct response to a
  separation step that cannot verify its own work but not a correct thing for the step to be unable
  to do. Two previous attempts worked around the module load and were each overtaken by the next
  shape of the same failure. It now reads owners and access rules through the .NET methods on the
  object `Get-Item` already returns, which need no module import at all.
- The `.deb` lifecycle test validated the installed autostart entry by pointing
  `desktop-file-validate` at `privacyfence.desktop.disabled` — the name auto-separation leaves
  behind — which that tool rejects on the filename alone, before reading the contents. It now
  validates a correctly-named copy, so the check tests the file again rather than the rename.

### Changed

- Documented that upgrading PrivacyFence on macOS is just re-running `PrivacyFence.pkg` — the
  installer restarts the daemon on the new build itself, so there's no need to quit anything
  first. (`README.md`, `docs/TECHNICAL_REFERENCE.md`)

## [4.1.0] — 2026-09-20

### Security

- **Confirming an auto-accept rule an AI client asked for now needs the same proof as approving
  one of its writes.** PrivacyFence has two kinds of confirmation dialog and they had been treated
  as one. The kind that follows an **Always allow** click is a second step inside a decision whose
  own approval card already demanded a passkey and an attributable session, so asking again there
  would be a second tap for one decision. The kind an MCP client raises —
  `privacyfence_propose_policy_change`, and the deprecated
  `privacyfence_propose_auto_accept_rule_change` — has no card
  in front of it: the dialog *is* the whole gate, and what it creates is a rule deciding what gets
  approved without asking from then on. Both were exempt from the decide-time checks, because those
  are scoped to a result named `accept`. So on the strongest configuration PrivacyFence offers —
  privilege-separated, `step_up.enabled`, `require_passkey` on, a passkey enrolled — a local
  process holding an unattested session could ask for a rule over MCP and then confirm its own
  dialog, with no passkey and no human. Those dialogs are now marked as what they are, and
  confirming one takes exactly what changing the same setting from the Settings page already takes:
  a session PrivacyFence can attribute to a person, and a passkey wherever `require_passkey` is on
  — regardless of `step_up.scope`, since a rule is not a read or a write but the thing that decides
  which of those you get asked about at all. Org mode gets the passkey half on the same condition
  (it has no session provenance to check — every session there is an IdP authentication).
  **Cancelling is ungated**, for the same reason denying always has been. Found reviewing the
  policy v2 redesign's two new meta-tools against the rest of the self-approval work, before
  they ship together.
- **A packaged install now requires a passkey before it releases anything, out of the box.**
  `step_up.enabled` and `step_up.require_passkey` both default to on for the DMG/`.pkg`, the
  Windows installer and the `.deb` — the same builds ADR 0003 already makes privilege-separated or
  refuses to serve at all, so the credential store the passkey is checked against is out of the
  agent's reach on exactly the installs this switches on. ADR 0003 listed this default under *Out
  of scope* because decision 1 was only half the precondition; the other half is the enrollment
  gate below, without which defaulting this on would have advertised a guarantee a local process
  could defeat by enrolling a passkey of its own. An explicit `enabled`/`require_passkey` in
  `config/settings.yaml` still wins in both directions, and every install seeded from an older
  `settings.yaml.example` has both written out as `false` — so **this changes fresh installs, not
  existing ones on upgrade**. A source checkout, an editable install and `pipx install
  privacyfence` all still default off, for the reason they always have: nothing separates them, and
  a passkey checked against a store the agent can write is a checkbox a local process ticks for
  itself. Setting `require_passkey: true` on an unseparated install is still refused outright at
  startup, naming the fix.
- **The companion app walks you through your first passkey.** A fresh packaged install comes up
  requiring a passkey it does not have yet — a deliberately fail-closed state in which nothing is
  approved and every page says why, but not one anybody would find on their own. The companion now
  asks the daemon at each start whether that is the case and, if it is, opens `/security` with a
  session already minted. It re-offers at every start until something is enrolled and does nothing
  once one is; it stays quiet while privilege separation's per-user half is still pending, since
  until you have logged out and back in there is no page to open yet.
- **The one-time recovery code no longer travels in an HTTP response body on a packaged install.**
  That code removes every passkey enrolled for a principal when spent, which made it a
  credential-store reset token handed to whoever completed an enrollment — including a local
  process that had reached one. The daemon now mints it, has the companion put it in a dialog on
  your own desktop, and only *then* stores it: a code nobody could be shown is never stored either,
  so the install is left able to issue one rather than holding one that exists and cannot be
  produced. If the companion cannot be reached, the passkey is still enrolled and `/security` says
  why no code was issued. Because nothing keeps the plaintext, re-presenting a code means issuing a
  new one, and the companion is where that happens: **New Recovery Code…** on the macOS/Windows
  menu bar, a matching Applications-menu entry on Linux. The daemon asks you to confirm through the
  companion first — issuing one stops whatever you wrote down before from working — and no reply on
  the daemon's own control channel ever carries a code, in either direction, which matters because
  that channel is group-shared with the logged-in user on a separated install. Org mode is
  unchanged (no companion, and the session that reached `/security` is an IdP authentication), and
  so is any non-packaged local-mode install, which autostarts no companion.
- **`step_up.scope` now defaults to `writes_and_pii_reads` rather than `writes`**, in both
  deployment modes. The narrower default was chosen when the adversary in view was a human at the
  keyboard; ADR 0003 adopts a different one — an agent with code execution on the same machine —
  and against that adversary `writes` leaves every read releasable by a session alone, including
  one PrivacyFence itself flagged as carrying personal data. An install with `scope:` written out
  in `config/settings.yaml` or `org_config.json` keeps exactly what it set; only one that never
  expressed an opinion moves, and it moves one rung, not to the widest.
- **A sign-in session is no longer treated as proof that a human asked for it.** Three paths reach a
  local-mode `pf_session`, all by design (ADR 0002 decision 6): the companion's own **Open
  Approvals** item, a bootstrap link a human was handed, and a bare `MINT` on the control channel —
  which privilege separation *widens* to a group the agent is in. Nothing downstream recorded which,
  so all three produced the same object with the same authority, and a local process could release
  the write it had itself requested. Every session now carries a provenance: `human` for one minted
  through the companion (its menu item, confirmed by a call-back to the process the human clicked,
  or `privacyfence-app --print-sign-in-link`, confirmed by the companion's own dialog) and
  `unattested` for everything else. Releasing an approving decision and changing a sensitive setting
  require `human`; viewing and denying are unchanged, so an unattested link still shows what is
  pending and says plainly that it cannot approve it. Enforced on privilege-separated installs —
  since ADR 0003, every packaged one — where the companion this rests on is guaranteed to be
  installed and running; a non-packaged `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1` checkout has neither
  a companion nor an `authority/` boundary and is unchanged. See
  `docs/security-and-compliance.md`'s "A session is not a human", including what this deliberately
  does *not* claim about telling the companion apart from the agent.
- **Every sign-in link PrivacyFence issues is now audited, and the recent ones are shown on the
  Passkeys page.** Exactly one of the three ways to a session used to write an audit entry — the
  MCP sign-in-link tool, now removed — and the two silent ones were the two anything on this
  machine could use, so the log recorded the sanctioned path and not the reachable ones. Each mint
  (and each refused attested mint) is now recorded under its own `sign_in_code_minted` decision,
  naming which path asked and whether the resulting session can approve. `/security` lists the
  recent ones, so a link you did not ask for is visible rather than merely inferable.
- **PrivacyFence no longer writes a live sign-in link to disk.** Every startup used to leave the
  current `?bootstrap=` link in `~/.privacyfence/approvals_url` (and `settings_url`), refreshed on
  every restart, in a directory that is group-shared with your login account by design — so any
  program running as you, the AI client included, could read a working session out of it. Those
  files are no longer written, and any left by an older version are deleted the next time
  PrivacyFence starts. The not-authorized page points at the companion app and
  `privacyfence-app --print-sign-in-link` instead; the startup log line names those two rather than
  a link it was never able to print unredacted anyway.
- **Removed: `privacyfence_get_sign_in_link`.** This meta-tool minted a live sign-in link for
  PrivacyFence's own approval and settings UI and handed it to the calling AI client — the exact
  party the credential governs. It existed because a locked-out human had no other way in: the
  daemon is headless and the companion app was optional, "nothing installs or starts it
  automatically yet". ADR 0003 made the companion mandatory and autostarted on all three platforms,
  so that justification expired. `privacyfence_status` now answers an un-onboarded install with
  `next_step: "open_privacyfence_companion"` and no link to relay, and the not-authorized page
  leads with the companion rather than "ask Claude". A human whose companion menu is out of reach
  runs `privacyfence-app --print-sign-in-link` themselves (above). **If your MCP client's tool list
  is cached, it will drop this tool on its next refresh; nothing else calls it.**
- **New: `privacyfence-app --print-sign-in-link`, a way back into the web UI that never routes a
  credential through the agent.** Run it yourself, in your own terminal: PrivacyFence's companion
  app confirms it with you before the link it prints is allowed to approve anything. If nothing
  confirms it, it still prints a link and says what that link is — a view-only session — rather than
  leaving you with nothing when the reason you are locked out may well be that no companion is
  running. This is the break-glass path that replaces asking your AI client for a sign-in link.
- **Enrolling a passkey now needs proof of its own, in both deployment modes.** Removing your *last*
  enrolled credential has always demanded a fresh assertion with it, because letting a session alone
  un-enroll would silently turn a "mandatory" install back into an unenforced one. Adding one had
  exactly the same effect by the shorter route and asked for nothing beyond the session cookie: a
  local process holding a session (ADR 0002 decision 6 names three ways one is reachable by design)
  could enroll a credential it generated itself and then satisfy every step-up check with it,
  including on the strongest configuration PrivacyFence offers — privilege-separated,
  `step_up.enabled`, `step_up.require_passkey` on, and the human's own hardware passkey already
  enrolled. Approvals, and every sensitive settings change behind the same gate, were self-serve.
  Checking harder at verification time cannot close that: registration uses `none` attestation and
  the "user verified" flag is a bit the authenticator sets about itself, which a process that is not
  a browser sets to 1. So `/security` gates the *enrollment* instead. With a credential already on
  file, adding another needs a fresh assertion with one you have, through the same prompt-then-retry
  round trip removing your last one already uses — one extra tap for a human who has a passkey, and
  a prompt a session holding only a cookie cannot answer. With nothing on file there is nothing to
  assert with, so local mode asks the companion app to confirm with whoever is at the login session.
  Refusals are audited (`webauthn_enrollment_refused`), and a successful first enrollment is now
  named as such in its own audit summary. Three limits are stated plainly in
  [`docs/security-and-compliance.md`](docs/security-and-compliance.md) rather than left to be
  inferred: org mode's *first* enrollment has no companion to ask and rests on the IdP session that
  reached `/security` (which is why that document no longer claims a phished IdP session cannot
  satisfy step-up on its own); the companion confirmation raises the cost of forging a first
  enrollment but is not authentication of the companion, which shares an OS user with the agent and
  cannot be told apart from it; and on Linux the confirmation needs `zenity` or `kdialog` — neither
  is a PrivacyFence dependency, and a desktop with neither is told so by name.
- `step_up.scope` takes a third value, `writes_and_reads`, which requires a passkey assertion before
  releasing *any* approving decision — a write, a read PII detection flagged, and a read it did not.
  The two scopes that existed before (`writes` and `writes_and_pii_reads`) both leave
  an unflagged read releasable by a session on its own, which is the right trade only for an install
  that trusts `pii_detector.py` to have flagged everything worth a second factor; this value is for
  the installs that would rather not depend on that. It behaves identically in both deployment
  modes — one `StepUpConfig` and one `webauthn_stepup.is_step_up_required` serve both — and is
  configured the same way every other step-up setting already is: `config/settings.yaml`'s
  `step_up:` section in local mode, `scripts/build_org_bundle.py --step-up-scope writes_and_reads`
  (or the `step_up` section of `org_config.json` directly) in org mode. Denying still needs no
  step-up under any scope, and a read an auto-accept rule already covers never becomes an approval
  in the first place, so no scope asks for a passkey on one. `/security` now states which of the
  three is in force rather than assuming one of the first two.
- The Windows installer now separates the install itself (ADR 0003 decision 4). Setup runs
  `privilege-separation.ps1 enable` as a post-install step with its own elevated token, so a
  Windows install ends up running the daemon under the dedicated `NT SERVICE\PrivacyFence`
  account, with its policy, passkey store and audit key out of reach of the AI client it governs,
  without anybody having to find out that script exists and type it into an elevated PowerShell.
  Windows was the last shipped channel that produced an unseparated install by default. **If that
  step fails, the install fails** — a PrivacyFence that cannot separate itself would still show the
  same approval prompts, accept the same passkey enrollment and write the same audit log while
  meaning something weaker by all three, so it is not installed at all. `enable`'s existing
  refusals are unchanged, which puts a real cost on the table rather than hiding it: **somebody who
  cannot elevate on their own machine can no longer install PrivacyFence on Windows.** The
  non-elevated per-user install tier added in #407 (and preserved by ADR 0002 decision 5a) is
  withdrawn; it could never be separated, because a Windows service runs whatever its `binPath`
  names and an install directory the signed-in user can rewrite hands the agent a way to run its
  own code *as* the service account. Uninstalling is unaffected, and
  `privilege-separation.ps1 disable` still returns any install to the unseparated layout, data and
  autostart task included.
- The Windows installer's "Launch PrivacyFence now" checkbox on the Finish page is gone. On a
  separated install the daemon is a Windows service that `enable` has already started, and a second
  copy launched into the signed-in user's own session is refused outright by the startup identity
  check rather than merely redundant — so the checkbox's only possible outcome was an error
  dialog. The companion tray icon is started by its own scheduled task instead, and the Start Menu
  entries for the settings page and the companion are unchanged.
- Privilege separation no longer gives up when it cannot tell which human an install is for. ADR
  0003 decision 3 splits `enable` into a machine half — creating the service account, moving and
  re-owning the data directory, writing the marker, installing the service — and a per-user half,
  which is only the two steps that need a person: adding them to the `_privacyfence` /
  `privacyfence` / `PrivacyFenceUsers` group, and migrating whatever they had under
  `~/.privacyfence` (`%LOCALAPPDATA%\PrivacyFence`). The machine half now always runs and always
  fully separates the install, so an MDM push, an unattended `apt` upgrade, a root shell or a
  `.pkg` installed with nobody at the console produces a separated install rather than the
  unseparated one each of those used to fall back to. The per-user half is re-runnable on its own —
  `enable --for-user <name>` (`enable -ForUser <name>` on Windows) — and `status` reports the
  interim state as its own answer (`PENDING USER`) rather than as "not separated", because the
  install *is* separated; what is outstanding is one group membership.
- Installing the Debian/Ubuntu `.deb` now separates the install or fails, rather than separating it
  where it can. ADR 0003 decision 5: `debian/postinst` used to end its one separation step with a
  shell `|| true`, so anything that went wrong there left a package that reported itself installed
  and a PrivacyFence whose central claim — the agent cannot approve its own request — did not hold.
  That step is now two, with the two failure policies decision 3 made possible. The machine half
  (`enable --machine-only`) runs on every `configure`, unconditionally, and a failure of it fails
  the package install loudly, leaving dpkg with a half-configured package rather than a silently
  unseparated one. The per-user half keeps its `$SUDO_USER` gate and keeps the right to defer, so an
  unattended install — an MDM push, `unattended-upgrades`, a root shell — still succeeds *and* still
  ends up separated, with only the group membership pending for the companion app to close at the
  first real login session. Re-running the machine half also no longer clears the owner already
  recorded on an install whose per-user half is closed, which is what every upgrade does now.
- The companion app closes that pending half by itself. On start, an install that is separated but
  whose current user is not in its service group gets one elevated `enable --for-user` — macOS'
  own admin-password dialog, a UAC prompt on Windows, `pkexec` on Linux — after which it names the
  one thing no password can do, which is logging out and back in for the new group membership to
  reach the session. It prompts nobody on an unseparated install or on one that is already
  complete, and where there is no way to ask for a password at all (a Linux desktop with no polkit
  agent) it prints the single command to run instead of guessing. "Nobody was logged in at install
  time" therefore stops meaning "this install is unprotected forever".
- The macOS DMG is now the only macOS artifact, and it carries the `.pkg` rather than an app bundle
  to drag (ADR 0003 decision 2). `scripts/build_dmg.sh` now builds the `.pkg` itself and puts it,
  plus the `.mcpb`, on the disk image — nothing else: no `.app`, no `/Applications` symlink, so
  there is no way to install macOS PrivacyFence except through the `.pkg`'s own root-context install
  step. The standalone `.pkg` (`macos-arm64-pkg`) is retired; `macos-arm64` still resolves to the
  DMG, so the download-KPI series is unaffected. The `.pkg`'s conclusion screen also stops telling
  every reader to open the `.mcpb` "next to this installer", which used to be false for anyone who
  had downloaded the standalone `.pkg`.
- A packaged local-mode install that finds itself unseparated no longer serves anything (ADR 0003
  decision 6). This is the backstop for the installs the entries above don't cover — a
  pre-ADR-0003 DMG install upgrading in place, a restored backup, an install where `disable` was
  run and forgotten. On startup, a packaged build attempts this platform's provisioning (the same
  mechanism the install-time entries above describe) and, if it is still unseparated afterward,
  refuses outright — no `/mcp`, no approvals — naming the one command that fixes it. The one-shot
  marker that used to make a declined macOS admin-password prompt permanent is gone: a decline is
  asked again on the next start rather than respected forever, since under this ADR a decline is not
  a configuration, it is an unfinished install. `disable` keeps working — it is how you get your
  data back out from under the service account, which Windows' documented uninstall order needs —
  but stops being a way to keep a packaged daemon running; its own output says so. This refusal is
  unconditional on a packaged build, with no developer override: source checkouts and `pip`/`pipx`
  installs are not packaged builds and are not gated by it at all (ADR 0003 decision 7 — that is how
  org mode is deployed and how the project is developed).
- `step_up.require_passkey` now refuses to turn on for a local-mode install that isn't
  privilege-separated, whether set by hand in `config/settings.yaml` or through the Settings page's
  "turn on step-up" action (ADR 0003, "Why not gate the passkey instead"). A passkey checked against
  a credential store the same account can rewrite was already named, in ADR 0002 decision 6, as
  worse than not having the feature at all — this closes the one place that configuration was still
  reachable. On a packaged install this is unreachable in practice, since the startup refusal above
  already guarantees separation first; it exists for the source-checkout developer path, with the
  same `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1` escape hatch (a sibling of the existing
  `PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP`) as an explicit, logged opt-out for local development. A
  non-packaged, unseparated install running with that variable set says so — in the daemon's
  startup log and on `/security` — rather than silently claiming a protection it doesn't have.
- `docs/security-and-compliance.md`, `docs/platform-support.md`, and the Quick Start install
  instructions in `README.md` stop hedging "default-on for macOS/Linux, opt-in for Windows": every
  packaged install on all three platforms now separates itself, unconditionally, as part of
  installing. `pip`/`pipx install privacyfence` stops reading as an answer to "how do I install
  PrivacyFence on my laptop" — it's how org mode is deployed and how the project is developed, and
  it says so where it used to imply otherwise.
- ADR 0003 (`docs/adr/0003-separated-installs-only.md`) decides that every local-mode install
  PrivacyFence ships is privilege-separated, and that a distribution channel which cannot separate
  itself at install time is not published. Today separation is effectively a user choice made by
  file format and dialog box — a macOS DMG separates only if a bare admin-password prompt at first
  daemon start is accepted, a Windows install never separates unless a command is typed by hand into
  an elevated PowerShell, and a pip install has no installer hook at all — while an unseparated
  install renders the same approval UI, accepts the same passkey enrollment and writes the same
  audit log, none of which mean what they say when the daemon and the AI client share a uid. The
  decision retires the macOS DMG in favor of the `.pkg` that already provisions separation as root
  during the ordinary install, makes the Windows installer separate the install itself (retiring the
  non-elevated per-user tier of issue #407, and superseding ADR 0002's decision 5a), stops the
  `.deb`'s postinst from falling back to an opt-in install, splits provisioning into a machine half
  that never needs to know who the human is and a re-runnable per-user half, and has a packaged
  daemon that still finds itself unseparated refuse to serve rather than serve a guarantee it cannot
  keep. Source checkouts and pip installs stay for development and org mode, behind an explicit
  `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED` opt-out that says what it is. No behavior changes with this
  entry — the ADR is the decision, and each platform's half lands in its own change.
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
  session is disabled in favor of a new one that starts the companion tray app there instead. It
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
- Issue #428 D2: a signed `PrivacyFence-<version>.pkg` installer (`scripts/build_pkg.sh`), built
  alongside the DMG in `build.yml`'s release job, is a second macOS distributable that answers the
  D1 entry above's own remaining gap — a DMG install has no root-context step to run `enable
  --auto` from, so D1 could only ask the daemon's own first start to pop an admin-password dialog,
  a real end-to-end path `docs/platform-support.md`'s "Known open items" still records as not yet
  manually verified against a release build. A `.pkg` install already runs as root and already asks
  for an administrator password as the ordinary "Install PrivacyFence" step, so its own
  `postinstall` script (`installer/macos/pkg/postinstall`) runs `macos_privilege_separation.sh
  enable --auto` there instead, resolving the human to provision it for from the logged-in console
  user (`stat -f '%Su' /dev/console`, since a package script has no `$SUDO_USER` the way `sudo`
  does) rather than waiting on a later, unexplained runtime prompt — and the installer's own
  welcome/conclusion pages (`installer/macos/pkg/resources/`) say what that means and how to
  reverse it, instead of a bare OS password dialog with no PrivacyFence-specific text at all. Never
  fails the package install over a privilege-separation hiccup — every failure path in the
  postinstall script logs and exits 0, same posture `enable --auto` already takes for itself. The
  DMG remains the primary distributable and is unaffected; the `.pkg` is an additional,
  fully-automated-install option, covered the same two-tier way the DMG already is
  (`test_macos_pkg_smoke.py`, structural, in `build.yml`'s release path; `test_macos_pkg_install.py`,
  a real `sudo installer -pkg ... -target /` with no separate `enable` call, in the weekly
  `macos-graphical-session.yml`). See issue #428.
- Issue #428 D2 follow-up (B1): `test_macos_pkg_install.py` — the real install above ran for the
  first time against a real `/Applications` path and found `enable` always refused to separate
  anything installed there. `require_trusted_image()` walks every ancestor directory up to `/`, and
  `/Applications` itself is `root:admin drwxrwxr-x` on every real Mac — group-writable by the same
  `admin` account the agent runs as on a typical single-user machine — so the walk always failed at
  `/Applications` itself, regardless of how the `.app` inside it was owned. This wasn't specific to
  the `.pkg`: the same `require_trusted_image()` call is what D1's own daemon-triggered runtime
  prompt and a human running `enable` by hand against a real drag-installed copy both go through
  too, so a real `/Applications` install could never actually have separated under any of the three
  paths — the gap `docs/platform-support.md`'s "Known open items" already flagged as unverified
  against a release build turned out to hide a real dead end, not just missing coverage.
  `macos_privilege_separation.sh`'s `enable` now copies the image — as root, immediately, while it
  already has the administrator authentication this command required to run at all — into a fresh
  root:wheel-owned `TRUSTED_IMAGE_DIR` (`/Library/PrivacyFence/image`) before trusting anything, and
  points the LaunchDaemon/LaunchAgent at the copy; `require_trusted_image()` now runs against that
  copy, not wherever `--app`/`--daemon-exec`/`--companion-exec` originally pointed.
  `test_macos_graphical_session_autostart.py` no longer needs its own pre-staging workaround
  (`_stage_as_root`) to get past this check — it now hands `enable` a plain, `/tmp`-extracted,
  user-owned copy directly, the real DMG-drag-install shape, and asserts the running daemon/companion
  actually execute from the staged copy. One real consequence: once separated, replacing
  `/Applications/PrivacyFenceApp.app` in place (a fresh DMG drag) no longer takes effect on its own —
  a separated install keeps running the staged copy until `enable` is run again, which is the correct
  cost of closing this rather than a regression to work around (auto-refreshing from an
  already-elevated process would mean trusting `/Applications` again, silently). See issue #428.
- Issue #428 D2 follow-up (download surface): the `.pkg` built above was uploading to R2 correctly
  but was invisible everywhere a person would actually go looking for it — `scripts/
  r2_release.py`'s `_INSTALLERS` (what `finalize` reads to decide a release's `manifest.json`, the
  one thing the download page, `/api/releases`, and the Worker's own `/download/<channel>/<id>`
  route all resolve through) recognized only the DMG/`.exe`/`.deb`, so a `.pkg` sat in the bucket
  with no id, no listing, and no route to it — the first alpha built with #428 D2 (`v4.1.0a3`)
  shipped exactly that way. `.pkg` is now a fourth recognized pattern there, given its own artifact
  id (`macos-arm64-pkg`) distinct from the DMG's `macos-arm64` — deliberately *not* added to
  `REQUIRED_ARTIFACT_IDS`, so a pkg-signing-cert gap or a pkg-specific smoke-test failure can never
  block the DMG/`.exe`/`.deb` from reaching "latest" the way a missing *mandatory* installer does;
  `finalize` already worked this way for every optional (non-manifest) upload, this just adds a
  manifest-visible middle tier between "counted and required" and "never counted at all".
  `website/download/download.js` needed no logic change to pick this up — its own "nothing about a
  release is hardcoded here" design (each manifest artifact renders its own card) already covered
  it, so this only adds `cloudflare/downloads/src/artifacts.ts`'s `.pkg` `Content-Type` mapping and
  a `PLATFORMS` display-name entry for the new id, both purely additive. `README`s aside, this
  release-side fix does not retroactively fix `v4.1.0a3`'s own manifest — a manifest is written
  once by `finalize` and the bucket's contents for that version are otherwise immutable — the `.pkg`
  becomes visible starting with the next tag `finalize` runs against with this fix in place. See
  issue #428.
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
- B20 of the 4.1.0 action plan: `web/org_settings_scope.py`'s `PER_PRINCIPAL_ACTIONS` allow-list
  named `add_rule_row`/`update_rule_row`, the grant equivalents, and the connector actions as
  permitted for any signed-in principal, but `web/routes_org_settings.py` only ever wired routes
  for removing a rule row and removing a grant row — the allow-list had run ahead of the routes.
  No route currently calls `is_action_permitted` with any of the unwired action names, so nothing
  was actually reachable, but a route added later in good faith could have trusted the allow-list's
  "yes" without noticing no implementation backed it. `PER_PRINCIPAL_ACTIONS` now holds exactly the
  two actions with a real route; the rest moved to a new `PER_PRINCIPAL_ACTIONS_UNROUTED` set that
  `is_action_permitted` denies until each one gets its own route and moves over.
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
- Approval binder, Phase 5: `docs/approval-list-ui-ux.md`'s "there is intentionally no Allow action
  on the list" — true up through Phase 2, superseded by Phase 3's batch approve — is replaced with
  the rule that actually holds, **no approval without disclosure**: what the binder discloses
  inline, which approval kinds it refuses to batch, and why. `docs/security-and-compliance.md`
  gains a new "The approval binder's single assertion" subsection stating what one WebAuthn
  assertion over a whole selected set establishes (the same freshness and user-verification
  `step_up.enabled` already requires per decision, now bound to an exact, tamper-evident set) and
  what it does not (per-item attention) — framed against [ADR
  0002](docs/adr/0002-local-mode-trust-boundary-and-companion-app.md) decision 6's own
  integrity-strong/confidentiality-weak asymmetry rather than as a new claim. No behavior changes
  with this entry.
- **The self-approval review's Phase 3: the settings surface outside the generic dispatcher is now
  gated too.** `POST /api/settings/org_config/upload` had its own route (a multipart upload, not a
  JSON action) and, for that reason alone, bypassed both the passkey step-up check and the
  human-session check every `_SENSITIVE_ACTIONS` entry already goes through — even though an
  uploaded organization config bundle can rewrite the PII policy, every auto-accept rule, and every
  connector's OAuth client config in one shot. It now needs the same fresh WebAuthn assertion
  (`step_up.require_passkey`) and the same human-attributable session (`require_human_session`) as
  any other sensitive settings change, bound to that exact upload's bytes so a completed ceremony
  can't be replayed to install a different file. Installing the *first* signed bundle an install has
  ever seen also pins its signing key going forward (trust-on-first-use) — this route now asks for
  an explicit confirmation before that one-way pin happens, rather than letting it happen silently as
  a side effect of an upload; `install_org_config_bytes` itself, and `daemon_main.load_org_config`'s
  own hand-edited-file path, still pin unconditionally, since placing a file on disk directly already
  needs access an HTTP request from an agent doesn't have.
- `toggle_connector` — classified non-sensitive on the reasoning that "an agent that already has
  connector access gains nothing new by flipping it" — only actually held for the disable direction:
  the same ungated action re-enabled a connector a human had deliberately switched off, which is
  exactly access the agent didn't already have. It's now two actions, `enable_connector` (sensitive,
  gated the same way as any other `_SENSITIVE_ACTIONS` entry) and `disable_connector` (not, same as
  before).
- `TestSensitiveActionsCoverAllAllowedActions`'s ratchet only ever covered the generic
  `POST /api/settings/{action}` dispatcher's own action names — exactly how `org_config_upload`
  bypassed it above by having a route of its own. A new `TestBespokeRoutesAreClassified` walks the
  actual `Route` objects `web/routes_settings.py`'s `build_routes()` returns and fails the moment a
  future bespoke POST route lands with no matching entry in `_BESPOKE_SENSITIVE_ROUTE_PATHS`/
  `_BESPOKE_EXEMPT_ROUTE_PATHS`, the same way the existing ratchet already fails on an unclassified
  `_ALLOWED_ACTIONS` entry.

### Added

- **A step-by-step install guide, one section per platform**
  (`docs/getting-started.md`). The README's Quick start already had the shape of each install, but
  it interleaves each step with the reasoning behind it — why privilege separation is mandatory,
  why PrivacyFence will not hand a sign-in link to the AI client it governs — which is the right
  thing for a README and the wrong thing to follow with an installer already open. The new guide is
  the same three installs written as instructions: what to download, what to double-click, what you
  should see after each step, when to log out and back in and why, how to install
  `PrivacyFence.mcpb` into Claude Desktop (including the Windows case where no `.mcpb` file
  association exists and the installer offers File Explorer instead), how to point Claude Code at
  the daemon's `/mcp` endpoint with the `mcp_url`/`mcp_token` handoff files on each platform, and
  the shared first-run steps — enrolling the passkey a packaged install approves nothing without,
  installing an organization configuration bundle, authenticating connectors, and answering a first
  approval. It ends with per-platform "is it working?" checks and a symptom/cause table for the
  failures that actually strand a new install: no tray icon, a client that sees no tools, a
  *Permission denied* on `mcp_token` that means nobody logged out and back in, and the Linux
  `zenity`/`kdialog` gap that refuses a first passkey enrollment. Linked from the README's Quick
  start and Documentation list, and from `docs/README.md`'s Start here.

- ADR 0005 (`docs/adr/0005-moving-the-approval-decision-off-the-device.md`) asks, and does not yet
  answer, whether local mode's approving decision should be rendered and answered on a device the
  agent does not run on. **Proposed, not accepted — no behavior changes with this entry.** It is
  written now because the same question had been deferred in one line three times (ADR 0002's *Out
  of scope*, ADR 0003's, and this release's self-approval hardening work), each time to "its own
  issue" that was never opened, and because the hardening that shipped in this release is what
  makes the question answerable: with the credential store behind a uid boundary, enrollment gated,
  the passkey on by default and sessions carrying a provenance, the same-machine gate is about as
  strong as a same-machine gate gets — which is the position from which its ceiling can be measured
  rather than guessed at. The ADR states that ceiling plainly (an adversary running as you can be
  present for any proof you give, including the render of the sentence your passkey signs), weighs
  a paired phone over push against a second device on the local network against a display-carrying
  hardware authenticator against doing nothing more, recommends the LAN option, and lists what
  would have to be true before any of it could be accepted. Nothing in the shipped hardening is
  wasted either way: an install with no second device keeps exactly what it has today.

- Policy v2 redesign, P3: a new `policy/engine.py`/`policy/compat.py` evaluator for auto-accept
  rules, built on the P1 tool registry and P2 scope/condition selectors, now runs alongside the
  existing `AutoAcceptEvaluator` on every gated call (`gate.py`, shadow mode). Nothing on disk
  changes, and nothing about what auto-accepts changes by default: the existing evaluator keeps
  deciding, and a disagreement between the two is logged once at `WARNING` (operation key, each
  side's matched rule, a redacted context fingerprint — never call content) rather than acted on.
  A new `policy.engine: v1 | v2` key in `config/settings.yaml` (default `v1`) is the switch for
  when the new evaluator becomes authoritative instead; flipping it back to `v1` is the documented
  rollback, no release needed.
- Policy v2 redesign, P4: `config/settings.yaml` gains an on-disk `auto_accept:` v2 rule format
  (`policy/store.py`). On first startup under this version, every existing `auto_accept_rules`/
  `auto_accept_grants` entry is compiled and merged into it once (`policy.compat.migrate_to_policy_v2`)
  — the original file is backed up to `settings.yaml.bak` first, and the migration is provably
  behavior-preserving: the v2 rules it writes are exactly what P3's shadow-mode compiler already
  produces from the same v1 config. `auto_accept_rules`/`auto_accept_grants` themselves are left on
  disk untouched and still fully readable/editable by hand — nothing about which engine decides
  changes here (`policy.engine` still governs that, per P3). If any migrated rule's expansion now
  names a destructive (`delete`) or send (`send`/`draft`/`share`) verb — e.g. a sandbox-folder
  "Write" grant, which already silently includes deleting spreadsheet rows/columns — the Settings
  page shows a one-time dismissible notice listing exactly which rules, so a user finds out in
  those terms rather than discovering it later.
- Policy v2 redesign, P6: the settings page's per-connector **Auto-accept Rules** sidebar (one page
  per connector, its own **Trusted `<Resource>`** grant sections, `rule_type`/`value` rows, and
  read-only "Governed by Drive" pointer pages for Sheets/Docs) is replaced by a single **Auto-accept**
  page — one filterable list across every connector, each rule rendered as a plain-language sentence
  with color-coded verb chips and an "Unblocks N tools" disclosure naming exactly which tools it
  covers before you decide to keep or remove it. Rules are added and removed straight against the
  on-disk v2 `auto_accept:` schema (`policy/store.py`, `policy/propose.py`) rather than
  `auto_accept_rules`/`auto_accept_grants` — the first surface this redesign actually writes through.
  This also makes three previously-ungovernable operation groups configurable for the first time —
  Apps Script's three tools (by script id, a new `apps_script.project` scope) and Gmail's filter
  tools/Slack's group-chat tool (two new honestly-unconditional scopes,
  `gmail.anything`/`slack.anything`) — none of which any surface, including a hand-edited
  `config/settings.yaml`, could configure auto-accept for before. `gate.py` now checks a rule written
  here unconditionally, regardless of `policy.engine`, since a rule using one of these three new
  scopes has no v1 equivalent to be shadowed against. Org mode's own, separate per-principal settings
  page (`web/routes_org_settings.py`) is unaffected — it still edits `auto_accept_rules`/
  `auto_accept_grants` directly and keeps working exactly as before.
- Policy v2 redesign, P7: the MCP bridge gets the same one-shape write path the Auto-accept Settings
  page (P6) already has. Two new tools, `privacyfence_list_policy`/`privacyfence_propose_policy_change`,
  read and write straight against the on-disk v2 `auto_accept:` section by scope (`group`, e.g.
  `drive.folder`) and verb, rather than choosing between a `target: "rule"` and a `target: "grant"`
  half of two older config sections; each rule carries a stable id a follow-up `update`/`remove`
  call can target. `privacyfence_check_policy` gains `matched_rule_id` alongside its existing
  verdict, checked against the same rules the real gated call would use, so a planning agent can say
  *why* a call will pass. A verb a named scope type cannot govern is rejected before any
  confirmation dialog is shown, closing the write-time half of what this redesign's F5 found:
  `apps_script.*`/`gmail.create_filter`/`gmail.update_filter`/`slack.create_group_chat` previously had
  no configurable rule *and* nothing stopping one from being written anyway.
  `privacyfence_list_auto_accept_rules`/`privacyfence_propose_auto_accept_rule_change` are kept,
  unchanged, as deprecated aliases for one minor release — an old-shape write still lands the same
  rule the new engine recognizes.
- Policy v2 redesign, P8: every audit log entry for an auto-accepted decision now carries a
  `rule_id` — the on-disk v2 rule's own stable, content-derived id — alongside the existing
  (and, per F9, possibly ambiguous) rule name, whenever the decision resolves to exactly one rule
  row rather than being guessed. The Auto-accept Settings page (P6) uses it to show each rule's
  own usage — "Matched Nx, last \<when\>" — and flags a rule that has never matched with a
  distinct badge next to its existing Remove link, so a rule list becomes something a person
  maintains rather than one that only ever grows. No behavior change to what auto-accepts: this is
  attribution and staleness reporting only.
- Policy v2 redesign, P9 (final phase): the two remaining v1 writers — the approval popup's own
  **Always allow** button and org mode's per-principal settings page (`web/routes_org_settings.py`,
  untouched by P6) — are ported onto the same v2 primitives P6/P7 already used
  (`policy/propose.py`, `policy/describe.py`, `auto_accept.add_policy_v2_rules`), then
  `AutoAcceptEvaluator`, `resource_grants.py`, `policy/compat.py`'s shadow-mode comparison, and the
  `policy.engine: v1 | v2` switch are deleted. `resource_grants.py`'s manifest survives as
  `policy/resource_registry.py`, with its resolver callbacks intact, for the three call sites that
  still need it (migration, audit-log/Settings name resolution, and the deprecated bridge aliases'
  grant-shaped input) — nothing evaluates a grant against a live call through it anymore, that
  moved to `policy/engine.py` back in P3. `docs/always-allow-rules-reference.md` is now generated
  from the scope/tool registry (`scripts/generate_always_allow_reference.py`) rather than
  hand-maintained, with a CI test failing on drift; `docs/TECHNICAL_REFERENCE.md`'s two auto-accept
  sections become one. Fixes a latent bug found while porting org mode: `daemon_main.py`'s org
  principal loader never ran P4's migration, only local mode's startup path did, so an org
  principal's hand-edited v1 config could go un-migrated indefinitely. See
  [ADR 0004](docs/adr/0004-retire-the-v1-auto-accept-config-model.md) for the full decision record,
  including why this phase's scope grew beyond its one-paragraph charter.
- Gmail draft bodies (`body_markdown` on all 6 draft tools) now support `# Heading 1`/`## Heading 2`
  syntax, rendered as Gmail's own "Large"/"Huge" font-size compose presets (not raw `<h1>`/`<h2>`
  tags, which render inconsistently across mail clients). See issue #414.
- Calendar events can now be given a color. `calendar_create_event`/`calendar_update_event` accept
  a `color` parameter, and a new `calendar_set_event_color` tool changes just that field on an
  existing event, mirroring `calendar_set_event_visibility`. A new `calendar_list_colors` tool
  lists Calendar's fixed color palette (id, name e.g. "Tomato", hex background/foreground) so a
  color can be picked by name instead of a numeric id. See issue #414.
- Calendar recurring-event management. `calendar_create_event` accepts a new `recurrence` parameter
  (RRULE/EXDATE/RDATE/EXRULE lines, e.g. `"RRULE:FREQ=WEEKLY;COUNT=10"`) to create a recurring
  series. `calendar_update_event` and a new `calendar_delete_event` tool both accept a `scope`
  parameter (`"this"` default, `"following"`, or `"all"`) matching Google Calendar's own edit
  picker, plus `send_updates` for attendee notification control; the approval popup gets a new
  "Applies to" row showing which occurrences a scoped change covers. "This and following" splits
  the series by ending the old one with an RRULE `UNTIL` and (for updates) inserting a new series
  from that point — Google Calendar's own documented approach, there being no single API call for
  it. `calendar_delete_event` shares `calendar_create_event`/`calendar_update_event`'s auto-accept
  rule set (`i_am_organizer`, `no_external_attendees`, `personal_calendar`). `calendar_create_event`
  always sends an explicit time zone for a recurring event even when `start_time`/`end_time` already
  carry their own UTC offset — the Calendar API rejects a recurring event that omits one ("Missing
  time zone definition for start time"), caught by `qa_fixture_recorder.py --lifecycle` against the
  real API before this shipped. `calendar_delete_event`'s write card names its own effect, like
  every other write card: the event goes for everyone, cannot be restored from PrivacyFence, and
  its attendees may be told it was cancelled — the "Applies to" row above it already says how much
  of a recurring series that covers. See issue #415.
- Approval binder, Phase 1: `/approvals` now groups pending, batchable approvals by
  `(connector, operation)` with a per-group and page-level select-all, and **Deny selected**
  clears a whole group of unwanted requests in one action (client-side over the existing per-id
  decide endpoint — no new server-side batch path yet). A confirmation/selection dialog, or a card
  a PII match forces a second confirmation on, is never offered a checkbox — `PendingApproval.
  is_batchable()`/`blocked_reason()` classify every kind explicitly, with a coverage test that
  fails the moment a new kind isn't classified either way. Each row also gets an inline "Details"
  disclosure, fetched from a new read-only `GET /api/approvals/{id}/preview` fragment (the same
  metadata-only `preview` dict already stamped onto every approval at registration) — never an
  `<iframe>` onto the real card document, which would mean weakening the card's own
  `frame-ancestors 'none'` for cosmetics. Selection lives in the page's own JS state and survives
  the list's live SSE re-renders. Approving still opens the full card; there is still no bulk
  Allow.
- Approval binder, Phase 2: a new `POST /api/approvals/batch/decide` endpoint approves or denies a
  selected batch in one request (`{items: [{id, result}], csrf}`, `result` one of `accept`/`deny` —
  no `accept_all`, which still needs its own scoped rule-creation confirmation). Each item resolves
  independently and the response reports one outcome per item (`applied` / `already_decided` /
  `unknown` / `not_batchable`) at HTTP 200 — a partial outcome (a rule elsewhere already resolved
  one of the selected items) is normal, never silent. Every decision is still authorized against
  `current_principal()`, unchanged from the single-decide endpoint: another principal's id reads as
  `unknown`, never "exists but forbidden". Every applied decision is still audited individually,
  now additionally stamped `decided_via: "binder"` with a server-minted `batch_id`, so a reviewer
  can tell which audit entries a single binder submission released. No passkey step-up on this
  endpoint yet — that lands with the batch's own bound assertion in the next phase. The
  decide-time WebAuthn step-up sequence duplicated across `web/routes_approvals.py`, `web/
  routes_org_approvals.py` and `web/routes_settings.py` is now one shared helper (`web/
  step_up_decide.py`) all three call, behavior-preserving — the batch endpoint would otherwise have
  been a fourth copy.
- Approval binder, Phase 3: an **Approve selected** button on `/approvals` submits a batch
  approval, gated on one WebAuthn passkey assertion bound to the exact selected set
  (`webauthn_stepup.batch_decision_fingerprint`) rather than one prompt per item. Submitting with
  no assertion gets a `428` carrying a fresh challenge and a server-minted `batch_id`; resubmitting
  the identical items plus that `batch_id` and the completed assertion releases the whole batch in
  one request. The fingerprint binds the *entire* submitted set, deny items included — an assertion
  obtained for one selection can't be replayed to authorize a larger, smaller, or differently-decided
  one, and it's single-use, so replaying it after release also fails. Only an approving item that
  actually needs step-up (a write, or, in the wider scope, a PII-flagged read) triggers the
  ceremony at all — a deny-only batch never prompts. `step_up.require_passkey` fails the *whole*
  batch closed (a `403` naming `/security`, nothing applied) exactly as it already does for a
  single decision; with it off and nothing enrolled, the batch is let through unguarded, the same
  evadable behavior the single-decide endpoint already has. A new `step_up.batch` setting
  (`single_assertion`, the default, or `per_item`) lets an install refuse single-assertion batching
  entirely instead — with it set, a batch containing anything that needs step-up is rejected
  outright, nothing applied, and those items have to be decided one at a time from their own card.
  The submit button itself names the selected set's composition ("Approve 12 · 9 reads, 3 writes")
  so an unintended write can't hide inside a read-shaped batch. No IdP re-authentication fallback
  for the batch endpoint even in org mode, unlike its single-decision endpoint — this is a
  page-level ceremony, the same shape `web/routes_settings.py`'s sensitive actions already use, and
  that one has never offered an IdP link either.
- Approval binder, Phase 4: a gated call no longer stalls the previous three phases' batch UI empty.
  Previously, an agent issuing tool calls one at a time blocked the full 30-second hold window on
  the first call, relayed one link, and wasn't due to try a second call until a human had already
  decided the first — the one-at-a-time flow, with extra steps. Once a principal already has one
  unfinalized approval outstanding, a later gated call's own hold window now collapses to zero
  instead (`web.approvals.adaptive_hold`, on by default): it returns `approval_pending`
  immediately, so an agent can keep issuing independently-ready gated calls instead of stalling on
  each in turn. The `approval_pending` result also gains `pending_count` (this principal's own
  approvals outstanding, this one included) and `binder_url`; past one, its message points at
  `/approvals` instead of the one card's own link and asks for every outstanding id to be
  collected into a single `privacyfence_await_approval` call rather than relayed and awaited one
  at a time — `privacyfence_await_approval`'s own tool description now says the same thing.

### Changed

- The documentation sweep [ADR 0003](docs/adr/0003-separated-installs-only.md) decision 7 started
  is re-run now that the hedges it removed have been replaced by shipped guarantees rather than
  intended ones, and now that the policy v2 redesign has merged back. Corrected, against the
  source in each case: `platform-support.md` said installing the `.deb` "does not turn any of this
  on" and that a package install "never runs" privilege separation, which `debian/postinst` has
  done unconditionally, and fatally on failure, since decision 5; `README.md` called separation
  "on by default" on macOS and Windows where it is mandatory, said a declined macOS admin prompt
  "won't ask again" where decision 6 retired that marker and now asks at every start, and said
  `disable` was simply "reversible" without noting that a packaged install then refuses to serve;
  `TECHNICAL_REFERENCE.md` described the Windows layout as what happens when an install "has opted
  into" separation, and described the two deprecated MCP aliases as reading and writing the v1
  `auto_accept_rules`/`auto_accept_grants` sections, which nothing has done since P9 (only their
  *request* shape is v1 — both go to the v2 section now); `approval-list-ui-ux.md` gave the wrong
  reason for `/security` not being in local mode's nav (it is mounted there, just not in the nav);
  and `coding-and-testing-guidelines.md`, `connector-qa-testing.md` and
  `claude-knowledge-boundary.md` still listed resource grants as a thing a test or a review has to
  account for. `scripts/build_org_bundle.py --help` still advertised `writes` as the default
  step-up scope, the last place in the repo asserting the pre-4.2 default.
- `TECHNICAL_REFERENCE.md` regains the "Web surfaces (`/approvals`, `/settings`)" section, which
  documents `web.mcp.enabled`, `web.settings.enabled`/`allow_quit`, `GET /settings/connectors`, the
  shared shell and SSE channel, and the settings dispatcher's allowlist. It was a subsection of
  "Auto-accept grants" for historical reasons only, and went out with that section when P6 rewrote
  the auto-accept chapter — taking the only description of those config keys with it. Restored at
  the top level where it belongs, with the bespoke-route classification the self-approval review's
  Phase 3 added folded in.
- `docs/README.md`'s architecture-decision index, which stopped at ADR 0002, now lists ADR 0003,
  0004 and 0005.
- **The MCP server now runs on the official Python SDK's 2.x API** (`mcp>=2.2,<3.0`, up from
  `mcp>=1.28,<2.0`). A deliberate migration rather than a widened range: mcp 2.0 rewrote the
  low-level server surface `/mcp` is built on, so the old pin could not simply be raised.
  Handlers are registered as constructor arguments instead of decorators and receive a
  per-request context, the per-session `lifespan` this daemon keyed its unattended-session and
  deduplication state by is now entered once per session *manager*, and result/tool models
  renamed their fields to snake_case. `/mcp` behaves the same on the wire — the same tools, the
  same `initialize` instructions, the same `tools/list_changed` notification when connectors
  change, the same tool-level error result (rather than a protocol error) for a failed call,
  which 2.0 would otherwise have replaced with a generic "Error executing tool" message. Each
  Streamable HTTP session is now identified by the transport's own `Mcp-Session-Id` rather than
  an id minted by the server's lifespan, and its unattended-session state is released when the
  session ends, exactly as before. The organization-mode authorization server explicitly refuses
  SEP-990 identity assertions (the RFC 7523 `jwt-bearer` grant) that 2.x added to its provider
  interface: in org mode a human authenticates at the identity provider through PrivacyFence's
  own `/authorize`, which is what every downstream gate, audit entry and approval is scoped to.
  `requirements/*.lock.txt` are regenerated accordingly, including mcp 2.x's new transitive
  dependencies (`mcp-types`, `httpx2`, `httpcore2`, `truststore`), all hash-pinned. See
  issue #250.

- **macOS ships one download, and it installs through the installer.** The DMG now carries
  `PrivacyFence.pkg` and `PrivacyFence.mcpb` side by side and nothing else — no
  `PrivacyFenceApp.app` to drag out, no `/Applications` symlink. Mount it, double-click the
  installer, then double-click the extension. The `.pkg` (#428 D2) is no longer published as a
  separate artifact at all: not on the GitHub Release, not in the R2 release archive, and not as a
  second macOS card on [privacyfence.eu/download](https://privacyfence.eu/download/) — releasing
  the DMG releases it.

  Two things this fixes. The installer's own conclusion screen has always told the user to open the
  `.mcpb` "next to this installer", which was untrue for anyone who downloaded the standalone
  `.pkg`: there was no `.mcpb` next to it. It is true now, and the wording says where to look.
  And dragging the app bundle out of the old DMG was a second install path that skipped the
  installer entirely, leaving privilege separation (#428 D1/D2) to the daemon's own admin-password
  prompt at some later, unrelated moment instead of the install the user was already answering a
  password for. That prompt still exists for an install that bypassed the installer — a bundle
  copied off another machine, a source run — but it is no longer where a download leads.

  Packaging mechanics: `scripts/build_dmg.sh` now calls `scripts/build_pkg.sh` itself (the `.pkg`
  has to exist before the image that carries it), so `build.yml` has one macOS build step rather
  than two, and passes the "Developer ID Installer" identity as `SIGN_IDENTITY_INSTALLER` rather
  than as a second, differently-scoped `SIGN_IDENTITY`. `scripts/r2_release.py` drops the
  `macos-arm64-pkg` artifact id along with its "optional installer" carve-out: all three installers
  it still knows (DMG, `-setup.exe`, `.deb`) are mandatory for a release to reach `latest`.

### Fixed

- **The Windows installer's privilege-separation step no longer depends on PowerShell module
  autoload working.** `scripts/windows_privilege_separation.ps1` calls `Get-Acl` throughout, and
  `installer/privacyfence.iss` runs it with `-NoProfile` (deliberately, so an administrator
  profile script can't change what runs) — which on some runner images leaves Windows PowerShell
  unable to autoload `Microsoft.PowerShell.Security` for it, failing the whole `enable` step with
  `CommandNotFoundException` before it ever reaches its own checks. The script now imports that
  module explicitly up front instead of relying on autoload. That explicit import turned out to
  have its own failure mode on a runner where autoload had quietly succeeded after all: a second
  `Import-Module` of a module whose type data is already registered throws `FormatXmlUpdateException`
  ("The member ... is already present") instead of doing nothing, which aborted `enable` exactly
  as hard as the missing-module case it was meant to fix. That specific duplicate-registration
  error is now tolerated — anything else Get-Acl's own unavailability would raise still stops the
  script.
- **Org mode's top navigation bar stays put across every page, not just `/approvals`.**
  `/connect`, `/security`, and `/settings` (and `/settings/privacy`) were each their own bare
  document, so following a Connect/Reconnect button, a passkey prompt, or a settings link off
  `/approvals` dropped the header/nav entirely, leaving only a plain centred link or two at the
  bottom of the page as a way back. All four now share `web_shell.wrap()`'s persistent
  header/nav (`web_shell.ORG_NAV_ITEMS`), and the old footer links back to Approvals/
  Connections/Passkeys/Settings are gone — the nav is the one way back now. Local mode's
  `/security` is unaffected: it has no web_shell-wrapped page of its own to be consistent with,
  so it keeps its small, unwrapped document and its own "Back to Connectors" footer link.
- **Approval cards and confirmation dialogs are readable on a phone.** Neither document declared a
  `<meta name="viewport">`, so iOS and Android laid it out in their default ~980px viewport and
  scaled the result down to fit: 13px body text rendered near 5px, Deny and Allow once were roughly
  36×13 device pixels side by side, and the `@media (max-width: 700px)` rules written to prevent
  exactly that never matched, because the viewport reported 980 regardless of the device. Both
  documents now declare a device-width viewport, and the phone-width rules they already carried are
  joined by the ones that layout needs to hold up: the heading wraps instead of overflowing
  horizontally at 25px, key/value rows stack rather than competing for one line, and Deny/Allow once
  become two equal 48px targets with always-allow a quiet link below them rather than a third
  control in the thumb zone. Nothing changes above the breakpoint, or in the native window, whose
  frame already sized itself to the document.
- **Detected PII is marked where it actually appears.** The card named the matched categories
  ("IBAN · National ID · Financial figures") and left the reviewer to find them by eye in a
  multi-message thread — the work the card exists to have already done. Matches are now highlighted
  in the preview pane itself, in the same tints as the category tags, so the tags read as a legend.
  Nothing extra is disclosed: the text is already the contents of the pane, the detector returns
  positions rather than matched substrings, marking is scoped to the categories the card already
  names, and a card with no PII section does no scanning at all.
- **Write cards say what approving them actually does.** A read card ends with "What will be
  provided to Claude"; a write card showed the payload and Claude's stated reason and nothing that
  named the consequence — the difference between approving a payload and approving an outcome, and
  it matters most where the payload looks harmless. "Add Gmail Label" and "Send Slack Message"
  present almost identically and only one of them is irreversible. Every write card now ends its
  action section with one plain sentence: "A label is added. Nothing is sent, moved or deleted." /
  "The message is posted and cannot be unsent." A new test fails the build if a tool reaches the
  write gate without one.
- **The card's section numbers are gone; the labels stay.** Which sections render varies by tool and
  direction, so the numbers only ever counted what happened to be on that one card — "03" was the
  PII gate on one and the disclosure list on the next, which is exactly what a reviewer seeing many
  of them cannot learn. Ordering is unchanged and still deliberate: the risk card renders before the
  disclosure list, never one scroll away from being missed.
- **Org mode's approvals page has a shell.** It was a bare document — design tokens, one body font
  rule, the list, and a centred footer of three links that nothing styled, so they rendered
  browser-default blue against a warm grey palette. No header, no brand, no nav, no favicon, and on
  a phone no navigation at all; local mode's identical list had all of it. Both modes now render
  through the same shell, with the nav set (Approvals / Connections / Passkeys / Settings) and the
  signed-in principal passed in per mode — org mode's whole authorization model is per-principal and
  the page never said whose queue was on screen. The shell also gained a link colour for ordinary
  `<a>` elements in page content, which nothing had styled before.
  Org mode deliberately renders **no** live indicator: its app mounts no `GET /api/state/stream`, so
  an indicator there would either claim a liveness that doesn't exist or sit permanently on a
  connection error. Tier-0/1 notifications, which ride the same stream, are off with it.
- **The approvals page explains a first run instead of claiming it is watching.** First run and
  steady state shared one empty state, written for steady state: "Nothing is waiting. / PrivacyFence
  is watching." On an install where no connector is authenticated that is misleading in both halves
  — nothing is waiting because nothing *can* wait, and nothing is being watched. That case now gets
  its own copy ("Nothing is governed yet."), explaining what PrivacyFence does and linking to
  `/settings/connectors`, reusing the wording of the settings page's own welcome banner. The
  steady-state copy is unchanged, and is what still shows whenever the answer can't be determined.
- **Connector icons no longer disappear after the first live update.** The server-rendered first
  paint drew each row's real brand icon, while the SSE re-render had no icon in its payload and
  always drew the letter-badge fallback — so within one poll interval every row silently degraded,
  on the page that most needs to look trustworthy. Each connector's icon is now a single CSS rule in
  the page's own stylesheet, which both render paths reach by class name, so a live-updated row
  draws exactly what the first paint did. Because the image data now appears once per *connector*
  rather than once per *row*, this also makes the page substantially smaller: a ten-row list over
  two connectors went from ~449KB to ~195KB. A connector with nothing pending when the page loaded
  has no rule and still falls back to the letter badge until the next full load.
- **An approvals row names what the request is about.** The row's title was `tool_name` and its
  second line was connector + the raw MCP tool id + age, so the row said `Read Gmail message` /
  `Gmail · gmail_get_thread · 2m ago` and never named the thread, document, event or contact being
  touched. `summary` — the field that does name it — was only a *fallback* title, and `tool_name` is
  always populated, so a normal row never reached it. The summary is now the title, the tool name
  moves to the meta line above it, and the raw tool id moves into the **Details** disclosure with
  the rest of the metadata preview. Rows with no summary (a bare confirm/choice dialog) still fall
  back to the tool name.
- **Read and write are visible on the row.** The card commits hard to the distinction — a pill in
  its header and a coloured rail down the window edge — while the row showed neither, though
  `gate_kind` was already in the row payload and already drove the **Approve selected** button's
  reads/writes count. Rows now carry the same pill, in the same token pairs as the card, and the
  page heading names the queue's composition ("4 approvals pending · 3 reads · 1 write"). That
  heading also now follows the live list: it sits outside the re-rendered region, so it previously
  kept whatever count the first paint had for as long as the page stayed open.
- **Approve selected is no longer styled as loudly as Review.** Both were filled
  `var(--color-accent)`, which made select-all-plus-one-click — the least-informed action available,
  taken off one-line summaries — as prominent as the control that opens disclosure. It is now an
  outline; Review keeps the fill. The composition label on it is unchanged, since that part is the
  guard rather than the problem.
- **The approvals list is usable on a phone.** `.pf-approval-actions` is `flex-shrink: 0` around
  three buttons while `.pf-approval-main` is `flex: 1; min-width: 0`, so the row's own `flex-wrap`
  never engaged — the text column shrank to roughly 25px at 393px instead, truncating the title
  after two or three characters and taking the connector/age line with it. Below 560px the row now
  stacks into identity on top and a full-width action strip underneath, row controls and selection
  checkboxes are a 44px target rather than ~30px and ~13px, and the toolbar's select-all and two
  batch actions stop competing for one line. **Deny** also moves to the far end of the action
  cluster, after **Review**, rather than sitting one 8px gap from it: denying resolves an approval
  outright and there is no undo path anywhere in the flow. That reorder is source order in both the
  server-rendered and the live-re-rendered row, so focus order and visual order still agree.
- **Quitting from the settings page no longer truncates its own response.** `/api/settings/quit_app`
  signalled the daemon's shutdown *before* returning, so the process could be torn down while its
  21-byte confirmation was still being written and the client saw `peer closed connection without
  sending complete message body` instead. Shutdown now runs as a background task, after the response
  body reaches the socket. This also removes an intermittent CI failure in
  `tests/system/test_local_mode_system.py`.
- **A refused companion-channel connection now actually receives its refusal.** On a
  privilege-separated install, `web/control_channel.py`'s peer check wrote `ERROR ...` and closed
  without reading the request — and closing a socket whose receive queue still holds unread data
  resets the connection, so the refused peer's own `send()` failed with `EPIPE` before it could read
  that line. `request_open_url()`'s caller saw a broken pipe rather than the diagnostic explaining
  why it was refused. The request is now drained before the close. This also removes an intermittent
  CI failure in `tests/unit/web/test_control_channel.py`.
- **`/security`'s "Back to connections" link no longer 404s in local mode.** It was hardcoded to
  `/connect`, org mode's own per-principal connector-authorization page (`web/routes_connect.py`) —
  a route local mode never mounts. `web/routes_security.py`'s `build_routes` now takes a `back_link`
  `(href, label)` pair (defaulting to `/connect`, org mode's existing behavior); local mode's own
  `web/server.py` wiring passes `/settings/connectors`, that mode's actual Connectors tab.

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
- `gate.py`'s dedicated popup executor (`_popup_executor`) is now sized against
  `approvals.DEFAULT_MAX_PENDING` (and, once the daemon starts, against
  `settings.yaml`'s own `web.approvals.max_pending` override, via the new
  `gate.configure_popup_executor()`) instead of a literal 8 workers. The "preparing this
  request" placeholder above covered the crash a worker-starved approval used to cause, but
  not the underlying stall: past the executor's worker count, a card's HTML was never built at
  all until an earlier one was decided, however many approvals the registry was otherwise
  willing to hold pending. The pool and the registry's own cap now stay tied together, so a
  future change to one can't silently reintroduce the gap between them.
- `approvals.PendingApproval` now carries the `preview` dict `gated_call()` passes to
  `show_popup()`/`show_read_popup()`, stamped at registration time rather than left for
  `build_card_html` to derive later on a `_popup_executor` worker. `preview` stays
  metadata-only by the same contract that already governs every card (`docs/coding-and-testing-
  guidelines.md` §1.5) — this only moves *when* it's known, not what it contains — and lets a
  future consumer (a read-only summary of what's pending) disclose without waiting on that
  worker at all. No behavior change on its own.
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
- The PyPI project page is no longer bare. `pyproject.toml` now declares `[project.urls]`
  (Homepage, Download, Documentation, Source, Changelog, Issues, Security) and `classifiers`, so
  the sidebar on `pypi.org/project/privacyfence/` links back to the site and repo and the project
  is classified (Development Status, License, Operating System, Intended Audience, Topic) rather
  than surfacing in no browse facet at all. README.md — which is the PyPI long description — had
  30 relative doc links and 4 relative screenshot `<img>`s that only resolve on GitHub; those are
  now absolute (`github.com/.../blob/main/...` for docs, `raw.githubusercontent.com/.../main/...`
  for images), and the three `../../releases` download pointers now point at
  `privacyfence.eu/download/`, the canonical download surface. See issue #370.
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
- B24 of the 4.1.0 action plan: `sudo scripts/linux_privilege_separation.sh enable` (and D1's
  auto-enable) actually stops the daemon's own XDG autostart entry from autostarting now.
  `stop_legacy_autostart()` only ever renamed `/etc/xdg/autostart/privacyfence.desktop` to
  `....desktop.disabled`, on the assumption that XDG autostart only reads `*.desktop` files —
  `systemd-xdg-autostart-generator` does not filter the autostart directories by filename, and
  turned the renamed file into a unit under `xdg-desktop-autostart.target` just the same, starting
  a second daemon in the logged-in user's own session at every login. It failed closed rather than
  doing damage (`check_runtime_identity` already refuses to run as the wrong account on a
  separated install), but the disable mechanism did not do what its own comment claimed, and
  `status`'s `STILL AUTOSTARTS` check — which only ever looked at the original, un-renamed path —
  reported no problem. The entry is now also marked `Hidden=true`, the key
  `systemd-xdg-autostart-generator` (and every other XDG-autostart reader) actually honors to skip
  a file without removing it; `disable` strips it back out when restoring the entry, and `status`
  now checks the renamed file for it too. An already-separated install upgrading past this fix
  self-heals the next time `enable --auto` runs (`debian/postinst`, on every install and upgrade),
  with no separate migration needed.
- B28 of the 4.1.0 action plan: the approval binder's `decided_via` and `batch_id` fields (Phase 2,
  schema v3) are now included in the audit log's XLSX export. The row builder wrote nineteen
  columns and neither field was among them, so a compliance reviewer working from the exported
  workbook had no way to tell which decisions were released together under one passkey assertion —
  that information existed only in the underlying JSONL, never in the artifact an auditor is
  actually handed. `batch_id` is routed through the same formula-injection guard (`_excel_literal`)
  already applied to the export's other free-text columns.
- B26 of the 4.1.0 action plan: `StepUpChallengeStore` now sweeps expired entries on every `put()`,
  closing an unbounded-growth path in the approval binder's batch step-up flow. Its key space was
  safe to leave unbounded only while it was `(principal_id, approval_id)` — `approval_id`s are
  themselves bounded by `max_pending` — but the binder's batch decide endpoint keys its own
  challenges on `f"batch:{batch_id}"`, and `batch_id` is read straight from the request body: a
  session holding at least one real pending approval could mint an unbounded number of live
  `StepUpChallengeStore` entries, each also costing a full `begin_assertion` call, just by resending
  a fresh `batch_id` and never completing the ceremony. Not a privilege escalation, but a
  slow, unbounded resource leak with a client-controlled multiplier.
- B27 of the 4.1.0 action plan: the approval binder's `batch_id` is documented (`audit_log.py`) as
  server-minted, but `POST /api/approvals/batch/decide` (and its org-mode counterpart) accepted any
  non-empty string the client sent verbatim and stamped it straight into the audit entry — the field
  the provenance guarantee rests on was, in the one case that mattered, whatever the caller chose. A
  caller could stamp unrelated decisions with the same `batch_id`, or replay one from a genuine
  passkey assertion, and make the audit trail read as though one human action authorized them; the
  WebAuthn authorization itself was unaffected (the fingerprint binds the decided set and is
  recomputed server-side), only the grouping claim in the record. A resubmitted `batch_id` is now
  kept only when the WebAuthn challenge-store lookup inside `verify_step_up()` proves it names a live
  challenge this server began; every other path — no step-up required for the batch, or the
  no-credential-enrolled/`require_passkey`-off fall-through — mints a fresh one instead of trusting
  the request body.
- Org mode's `/settings` page had no way to add an auto-accept rule at all — #400's first cut
  (`web/routes_org_settings.py`) only ever wired up viewing and *removing* a rule row, even though
  `auto_accept.add_auto_accept_rule()` and local mode's own desktop picker already supported it.
  A principal could see their configured rules and delete them, but the only way to add a new one
  was a hand-edited `settings.yaml`. A new "Add a rule" form on the page (one `<select>`, grouped
  by operation, listing every `(operation, rule type)` pair `RULES_BY_OPERATION` allows, plus a
  value field) posts to a new `POST /api/settings/rules/add` route, scoped to
  `current_principal()` exactly like the existing remove routes. `add_rule_row` moves from
  `org_settings_scope.PER_PRINCIPAL_ACTIONS_UNROUTED` to `PER_PRINCIPAL_ACTIONS` now that a route
  actually consumes it (see that module's docstring on why the two are kept apart).
- Org mode: `/connect` ("Connect your accounts") had no way back to the rest of the web UI —
  `/approvals`, `/security`, and `/settings` all link to it, and `/settings`/`/approvals` link back
  to each other, but `/connect` itself only offered a sign-out button. It now carries the same
  Approvals/Passkeys/Settings footer the approvals page uses, so a principal who lands there (from
  a first sign-in, or from `/security`'s own "Back to connections" link) isn't stuck without a way
  back short of editing the URL.
- **macOS: a failed daemon launch no longer kills the `.mcpb` shim itself.** `mcpb/shim/src/
  daemon.ts`'s `ensureDaemonRunning()` spawned `privacyfence-app` with no `error` listener on the
  child process; a spawn failure (`ENOENT` for a binary a Finder "upgrade" left missing or
  partially copied — see privacyfence/privacyfence#431 — or `EACCES`/`ENOEXEC` for a corrupted or
  quarantined one) emitted an unhandled `error` event, which Node rethrows as an uncaught exception
  and terminates the whole shim process immediately. That happened before `waitForConnectable`'s
  timeout, and before `waitForDaemonPatiently`'s never-give-up retry loop (built for exactly this
  kind of slow/flaky start) ever ran — so a companion app that failed to launch, whether right after
  an interrupted upgrade or after the daemon had previously crashed and left the install in a bad
  state, looked to Claude Desktop like the whole MCP connection dying with no diagnostic, rather
  than the "did not start in time" message the working retry path already produces for a merely
  slow one. The child's `error` event is now handled (logged, not rethrown), so a bad spawn falls
  through to the same timeout/retry machinery a slow one already takes.

## [4.0.0] — 2026-09-18

PrivacyFence 4.0 moves the entire user interface off macOS-native AppKit and onto a local web
server, ships on Windows and Debian/Ubuntu for the first time, and adds a centrally managed
organization mode. If you are on 3.x, read "Upgrading from 3.x" at the end of this entry before
installing — the menu bar icon you use today no longer exists.

Rolls up every `v4.0.0-alpha*` / `v4.0.0a*` / `v4.0.0b*` pre-release.

### Added

- **Windows support.** A signed Inno Setup installer (`PrivacyFence-<version>-setup.exe`) installs
  to `%ProgramFiles%\PrivacyFence\`, registers a Task Scheduler task so the daemon starts at
  login, and starts it immediately. A repeating time trigger on that task brings the daemon back
  after a crash. The installer also offers to open the bundled `.mcpb` at the end of setup
  (checked by default, alongside "Launch PrivacyFence now"), so Claude Desktop's install prompt
  appears automatically for most users instead of requiring them to locate the file in File
  Explorer first. See issue #407.
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
- Python 3.14 is now covered by CI. The `test-python-compat` job's matrix runs the core suite on
  3.11, 3.12 and 3.14 (3.13 is the full `test` job's own version), so the interpreter that is the
  default `python3` on current Ubuntu releases is proven rather than merely implied by
  `requires-python = ">=3.11"`.

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
- The README's "Install on Windows" steps now say where `PrivacyFence.mcpb` actually lands
  (`%ProgramFiles%\PrivacyFence\`, or `%LOCALAPPDATA%\Programs\PrivacyFence\` for a non-elevated,
  current-user-only install) and how to get there in File Explorer, instead of just saying to
  install it with no path given, for the case where the automatic Finish-page prompt above was
  declined. See issue #407.
- The Windows installer's "open the `.mcpb` after Finish" checkbox (issue #407, above) no longer
  attempts to `ShellExecute` the `.mcpb` when nothing on the machine is registered to open one —
  previously, that left Windows presenting its own "how do you want to open this file?" picker
  instead of anything PrivacyFence-specific. This isn't only a first-run/Claude-Desktop-not-
  installed-yet case: Claude Desktop's own installer doesn't always register the `.mcpb`
  association cleanly on Windows the first time, so the failure was also reported on a machine
  that already had Claude Desktop installed. The installer now checks the registry for a real,
  working `.mcpb` association before deciding what Finish does: if one exists, it opens the
  `.mcpb` exactly as before; otherwise the same checkbox opens File Explorer with the `.mcpb`
  pre-selected instead, so the user always lands somewhere they can act on (double-click once
  Claude Desktop is installed and associated, drag it onto Claude Desktop's Settings → Extensions
  page, or fix the association via Open With) rather than at a dead-end system dialog.
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

### Removed

- **The legacy Node stdio bridge (`bridge/`).** It is replaced by `PrivacyFence.mcpb`, whose shim
  is a thin stdio-to-Streamable-HTTP transport proxy: it carries no tool-schema knowledge of its
  own, so it never needs to be kept in step with the daemon's tool definitions the way the bridge
  did.
- The native macOS menu bar app and its AppKit approval/settings windows, superseded by the web UI
  above.

### Fixed

- `--atlassian-oauth` no longer fails when Atlassian's accessible-resources response splits a single
  site across entries; the callback URL uses the shared grant key.
- Drive API calls catch every exception, not just `HttpError`.
- `drive_download_file`, `gmail_download_attachment`, and `confluence_download_attachment` no
  longer fail with a bare, unhelpful "Tool call failed" when `destination_dir` can't actually be
  written to (a permissions error, or — as observed on macOS — the synthetic `/home` mount point,
  which rejects any direct `mkdir`/`open` under it with `[Errno 45] Operation not supported`). The
  underlying `os.makedirs`/`open` failure is now caught and re-raised as the connector's own
  `*ClientError` naming the path and asking for a different `destination_dir`, matching every other
  failure path these methods already had — previously the raw `OSError` skipped that wrapping
  entirely and fell through to the generic client-facing error message, leaving the calling agent
  with no way to tell what went wrong or that retrying with a different directory would help.

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
- `docs/security-and-compliance.md` now states the local-mode trust boundary explicitly: it is the
  operating-system user account, so a process running as the signed-in user — including an AI client
  with shell access, which is the normal local-mode install — can mint a session and release a
  pending approval without a browser. The CSRF, same-origin, TTL and expiry controls on that path are
  defenses against a hostile web page and against leaked credentials, not against local code
  execution, and the document previously left that easy to read more broadly than it holds. Nothing
  about the implementation changed; this corrects what is claimed for it, and names the work that
  closes the gap (issues #426, #427, #428). Org mode is unaffected — its daemon runs on a server the
  client has no loopback access to.

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

[Unreleased]: https://github.com/privacyfence/privacyfence/compare/v4.1.0...HEAD
[4.1.0]: https://github.com/privacyfence/privacyfence/compare/v4.0.0...v4.1.0
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
