# ADR 0003: every shipped local-mode install is privilege-separated

## Status

Accepted; implemented. Decisions 2 and 3 landed in #547 (`4d28549`, `c8caba0`, `f2ad715`),
decisions 4 and 5 in #549/#550 (Windows installer, `.deb` postinst), and decisions 6 and 7 in the
PR that added this Status update. Two amendment notes below (under decision 2 and under decision
6) record where what shipped differs from what this ADR originally wrote — an ADR is a record, so
those are notes on the decision rather than edits to it.

Supersedes [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) decision 5a's answer
("two install tiers", the non-elevated Windows per-user path kept), and retires the macOS DMG and
the declinable first-start admin prompt (#428 D1) as *shipping* mechanisms. ADR 0002's decisions
1–6 are otherwise unchanged and are the reason this one exists: this ADR decides nothing new about
what the boundary is, only that a supported install is never on the wrong side of it.

## Context

#428 Phase 4 moved local mode's daemon onto its own OS account. #426 made a session insufficient to
release an approving decision, by checking a passkey against a credential store that account owns.
Together they are the product's central integrity claim, stated in ADR 0002 decision 6:

> **Integrity is the strong guarantee** — the agent cannot approve its own request.

That claim is true of a separated install and false of an unseparated one, and today which of the
two a person ends up with is decided by the artifact they downloaded and the dialogs they clicked:

| Install path | Separated? |
|---|---|
| macOS DMG (the primary distributable) | only if the human accepts a bare, unexplained admin-password dialog at first daemon start (`privilege_separation.maybe_auto_enable_macos()`). A decline is recorded in a marker file and never asked again — the install stays unseparated for its whole life, silently. |
| macOS `.pkg` | yes, provisioned by `installer/macos/pkg/postinstall` as root during the install — unless nobody is logged in at the console, where it logs why and "leaves the install opt-in". |
| Windows installer | **no.** Separation is opt-in and requires a hand-typed command in an elevated PowerShell that nothing in the install flow mentions. |
| Debian/Ubuntu `.deb` | yes, via `debian/postinst`'s `enable --auto` — unless `$SUDO_USER` doesn't resolve to a real account, where it logs why and leaves the install opt-in. The step ends with a shell `or-true`, so a failure of it never fails the package install. |
| pip / pipx wheel | **no.** There is no installer hook to run, by construction. |

Every one of those fallbacks was correct when it was written. They all share one premise — that
separation is a hardening step layered on top of a product that works without it — and #426 is what
made that premise false. The three defaults that follow from it are now wrong in the same way:

1. **An unseparated install is not a less-hardened PrivacyFence. It is a PrivacyFence whose main
   claim does not hold.** The agent runs as the user; the user owns `~/.privacyfence`; the
   human-authority files are in it. A local process can read the bootstrap credential, mint a
   session, and decide its own approval.
2. **Turning the passkey on there makes it worse, not better.** ADR 0002 decision 6 says exactly
   this — "a passkey checked against a credential store the agent can write is a checkbox a local
   process ticks for itself, which is worse than not having the feature, because it claims a
   guarantee" — and then nothing stops that configuration. `step_up.require_passkey` is reachable
   from the Settings page of an unseparated install, and enrolls into a store that install's agent
   can rewrite.
3. **The choice is offered to exactly the people least able to make it.** It is not presented as a
   security decision at all. It is presented as a file format (`.dmg` or `.pkg`), an installer that
   finishes successfully without mentioning the subject, and, on the one platform where the product
   does ask, an unexplained system password prompt that arrives detached from anything the person
   just did. Somebody who knows what a service account is will separate. Somebody who does not will
   take the default, which on macOS' primary distributable and on every Windows install is "no".

This is not a documentation gap. `docs/security-and-compliance.md` is scrupulous about it and has
been from the start — every relevant sentence there carries its own "default-on for macOS/Linux,
opt-in on Windows" hedge. The hedges are accurate. They describe a product that ships two
materially different security postures under one name and one version number, and chooses between
them by which button was clicked.

## Decision

### 1. Separation is a property of a supported install, not an option within one

**Every distribution channel PrivacyFence publishes for local mode produces a privilege-separated
install, or it is not published.** A build that cannot separate itself at install time is not
shipped, and a shipped install that finds itself unseparated does not serve.

That is the whole decision. Everything below is what it costs on each platform.

The rule this replaces is worth naming so it is not reintroduced by accident: *"make it available,
document the trade, let the operator choose."* That rule is right for a setting (which PII
detectors run, what `step_up.scope` covers, whether to forward the audit log). It is wrong for the
boundary the settings are enforced *on*, because the person choosing cannot evaluate the choice
from inside the product, and because the wrong answer does not produce a visibly weaker
PrivacyFence — it produces an identical-looking one that is lying.

### 2. macOS ships the `.pkg`. The DMG is withdrawn

A DMG runs nothing as root at install time. That is a property of the format, not of our code, and
no amount of work on our side changes it: the best a drag-install can ever do is what #428 D1
already does — ask later, from the daemon, with a system dialog and a decline path. D2 built the
artifact that does the job properly, and correctly shipped it as an addition. This makes it the
only macOS artifact.

- `scripts/build_dmg.sh` stops producing a DMG. What it actually does that matters — build, sign
  and notarize `dist/PrivacyFenceApp.app` — is what `scripts/build_pkg.sh` consumes, and stays;
  the script's name and its last steps are the part that goes.
- `build.yml`'s `build` job publishes `PrivacyFence-<version>.pkg` and no `.dmg`, to the GitHub
  Release, to R2, and into the download manifest.
- The `.pkg` takes over the `macos-arm64` artifact id. `macos-arm64-pkg` stays as an alias for one
  release cycle, then goes.
- `tests/integration/test_macos_packaged_smoke.py` (the DMG's mount-and-run smoke test) is replaced
  in the release-critical path by `test_macos_pkg_smoke.py`, which is already there, plus the parts
  of the DMG test that were about the `.app` rather than the disk image.

**Already-published DMGs stay where they are.** Every release already in R2 and on its GitHub
Release keeps its files and keeps working; the Worker keeps serving them from those releases' own
manifests. We stop producing new ones; we do not rewrite what we already shipped.

> **Amendment (2026-09-19, `f2ad715`):** what shipped is the inverse spelling of this decision's
> text, with the same effect. The DMG was **not** withdrawn and the `.pkg` did **not** take over
> the `macos-arm64` artifact id — instead `scripts/build_dmg.sh` now calls `scripts/build_pkg.sh`
> itself and builds the DMG as a *carrier*: it holds `PrivacyFence.pkg` and `PrivacyFence.mcpb` and
> nothing else, no app bundle and no `/Applications` symlink. The `.pkg` is no longer published on
> its own anywhere (`macos-arm64-pkg` was deleted outright, not kept as an alias for a cycle as
> planned below).
>
> This still satisfies what decision 2 actually needs: the object this decision objects to is the
> *drag-install path*, not the disk image as a format. With no `.app` on the image there is no way
> to install macOS PrivacyFence except through the `.pkg`'s own root-context install step, which is
> exactly what "the DMG is withdrawn" below was trying to buy. It also fixed something this ADR
> hadn't noticed: the `.pkg`'s conclusion screen used to tell the user to open the `.mcpb` "next to
> this installer", which was false for anyone who had downloaded the standalone `.pkg`.
>
> Two consequences of the original text are therefore moot rather than done: there is no download-
> KPI series break to record in `docs/downloads-and-release-kpi.md` (the `macos-arm64` id never
> changed hands), and there is no `macos-arm64-pkg` alias to retire on a later cycle (it never
> existed post-`f2ad715`). The rest of this decision's reasoning — one macOS artifact, no drag
> install, `test_macos_packaged_smoke.py` asserting the DMG's own layout with
> `test_macos_pkg_smoke.py` covering the `.pkg` inside it — holds as written.

### 3. Provisioning splits into a machine half and a per-user half

Two of the fallbacks in the table above — the `.pkg`'s "nobody is logged in at the console" and the
`.deb`'s "`$SUDO_USER` doesn't resolve" — exist for the same real reason: `enable` needs to know
*which human* this install is for, in order to add them to the `_privacyfence` / `privacyfence`
group that makes `handoff/` reachable from their session. At install time that is sometimes
genuinely unknowable (an MDM push, an unattended `apt` upgrade, a root shell).

The answer is not to fall back to an unseparated install. It is that **the machine half never
needed to know.** Creating the service account, moving the data directory, writing the marker and
installing the LaunchDaemon / systemd unit / service are all doable as root with no human in sight.
Only the group membership is per-person, and it is re-runnable.

So `enable` gains that split:

- the machine half always runs and always fully separates the install;
- the per-user half (`enable --for-user <name>`, or the equivalent) runs when the human is known —
  at install time when a console/`$SUDO_USER` account resolves, and otherwise from the companion
  the first time a real login session starts one.

"Nobody was logged in" then means "the group membership is pending", which the companion resolves
by existing, rather than "this install is unprotected forever", which is what it means today.

### 4. Windows has one install tier, and the installer separates it

ADR 0002 decision 5a answered #428's open question as "two install tiers; the per-user path stays,
exactly as it is, and cannot be separated." **That answer is withdrawn.** There is one tier: an
elevated, per-machine install under Program Files, which the installer separates as part of
installing.

Two things have changed since 5a was written, and they point the same way:

- `installer/privacyfence.iss` is already `PrivilegesRequired=admin` — not for this reason, but
  because a non-elevated install could never register its own autostart task (#410, and the long
  comment above that directive). The non-elevated tier 5a preserved has in practice not been the
  default install since that landed.
- #426 shipped. 5a's closing argument was that "privilege separation is opt-in — taking the product
  away from that user to make an *optional hardening step* universally available is the wrong trade
  in the wrong direction." The premise is what expired: it is not an optional hardening step any
  more.

Concretely: the installer runs `privilege-separation.ps1 enable` itself, elevated, as an install
step — not as a documented follow-up in a PowerShell window the user has to find. `enable`'s
existing refusals stay exactly as they are (a user-writable install directory is still refused, the
companion is still required); on a per-machine install under Program Files neither fires.

**This costs a real user and the cost is not hidden**: somebody who cannot elevate on their machine
at all can no longer install PrivacyFence. #407 added that path deliberately and this ADR takes it
away deliberately. What we would otherwise hand that person is a build whose central claim is
false, on the platform where nothing in the install flow would tell them so.

### 5. The `.deb`'s postinst stops being best-effort

`debian/postinst` currently ends its separation step with `|| true`, on the standard Debian
reasoning that a package's own postinst should not fail an install over something ancillary. Under
decision 1 it is not ancillary. The machine half of decision 3 runs unconditionally on `configure`,
and a failure of it fails the package install, loudly, the way a failed `postinst` is supposed to.
The per-user half keeps today's `$SUDO_USER` logic and keeps being allowed to defer.

An unattended upgrade with no session behind it therefore still succeeds and still ends up
separated — the case the `|| true` was protecting is exactly the case decision 3 makes work.

### 6. A packaged daemon that finds itself unseparated does not serve

Decisions 2–5 cover the installs we ship. This is the backstop for the installs that exist anyway:
a 4.x DMG install upgrading in place, a restored backup, a hand-copied `.app`, an install where
`disable` was run and forgotten.

On startup, a **packaged** local-mode daemon (frozen build, `paths.py`'s existing
`sys.frozen`/`_MEIPASS` test) that is not separated:

1. attempts its platform's provisioning — macOS' D1 prompt is kept, and Windows and Linux gain the
   equivalent invocation;
2. if it is still unseparated afterwards, **refuses to serve** — no `/mcp`, no approvals — and says
   why, in the one place a human will see it, naming the single command that fixes it.

This is the same fail-closed posture `check_runtime_identity()` already takes, for the same reason
it gives: the alternative failure is silent, and a silent failure here does not degrade the product
visibly, it invalidates a guarantee the UI is still making.

Two consequences of this that are decisions in their own right:

- **The one-shot decline marker goes.** `AUTO_ENABLE_ATTEMPTED_MARKER_NAME` exists to respect a
  decline by never asking again. Under this ADR a decline is not a configuration, it is an
  unfinished install, so the prompt returns on the next start.
- **`disable` stays, and stops being a way to run PrivacyFence.** It is how you get your data back
  out from under the service account — which is exactly what Windows' documented
  "`disable` before uninstalling" order needs — and after it the daemon will not start. Its output
  says so.

> **Amendment (2026-09-19):** two implementation choices this decision's text left implicit, made
> explicit here because a later reader will otherwise reasonably guess differently.
>
> First, `maybe_auto_enable_macos()` is no longer backgrounded on its own thread the way #428 D1
> shipped it. This gate has to *know* whether the attempt took before deciding to refuse, which a
> fire-and-forget thread cannot answer — so the attempt (macOS's `osascript` prompt included) now
> runs synchronously, on the daemon's own startup path, reusing the same 300-second timeout the
> threaded version already had. A packaged, unseparated install can therefore block on a password
> dialog for up to five minutes before either continuing or refusing; that is the cost of "attempt,
> then decide" being one sequence rather than two.
>
> Second, "a non-packaged one runs unseparated only with an explicit opt-out" (decision 7, below)
> is implemented as *not* a second daemon-startup refusal. Gating daemon startup itself on
> `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED` for every non-packaged run would also gate every org-mode
> deployment (the wheel/sdist is org mode's *only* deployment path — decision 7's own next
> paragraph), which decision 1's "Out of scope" explicitly leaves untouched. What the var actually
> gates is `step_up_config.py`'s `from_local_config()`: a non-packaged, unseparated local-mode
> install refuses `require_passkey: true` unless separated or this var is set — the concrete case
> the "Why not gate the passkey instead" rationale (below) says to keep regardless. This is the one
> place a non-packaged build could otherwise present a guarantee it does not hold, which is what
> decision 7's "say what they are" is actually about.

### 7. Source checkouts and pip installs stay, and say what they are

The wheel and the sdist are not withdrawn: they are how org mode is deployed, and org mode is
untouched by all of this (ADR 0002's "Out of scope" — its daemon already runs where the agent has
no access, and it has no bootstrap concept). They are also how the project is developed.

What changes is that neither may present itself as a protected local-mode install. Decision 6's
refusal is scoped to packaged builds; a non-packaged one runs unseparated only with an explicit
opt-out in the established house spelling — `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1`, a sibling of
`PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP` — and, when it does, says on `/security` and in the startup
log that this install's approvals are not protected against the client they govern.

`pip install privacyfence` therefore stops being an answer to "how do I install PrivacyFence on my
laptop". The download page and the README already point at the platform installers; the PyPI
description and the docs need to stop implying otherwise.

## Rationale

### Why withdraw artifacts rather than warn harder

A warning is the correct instrument when the reader can act on it and the product is honest either
way. Here the reader is being asked to evaluate a uid boundary from a dialog box, and the product
is not honest either way: an unseparated install renders the same approval UI, accepts the same
passkey enrollment, and writes the same audit log, while none of the three mean what they say.
Every warning we could add is a warning that the thing the user is looking at is not what it
appears to be — at which point shipping it is the defect, not the absence of a label on it.

The narrower version of this argument is the one that actually settles it: we already built the
artifact that does this right on macOS, and we already made the installer elevated on Windows for
an unrelated reason. What remains is not a feature. It is the removal of the worse option we kept
next to the better one out of habit.

### Why not "keep the DMG, make its prompt blocking"

Considered, because it preserves the drag-install. Rejected: it moves the one elevation macOS
requires to the worst possible moment — after the install "finished", from a background process,
with a system dialog that has no room to explain itself, at a point where the user's mental model
is that they are done. That is the exact failure D2 was written to fix. A blocking version of a
badly-placed prompt is a badly-placed prompt the user cannot escape.

### Why not gate the passkey instead, and leave the installs alone

i.e. refuse `step_up.require_passkey` unless separated, and let unseparated installs run without
it. This fixes claim (2) in the Context and leaves (1) and (3) untouched: the unseparated install
stops lying about the passkey specifically, and carries on being a governance product whose
governance a local process can overrule. It is also, in practice, a way of shipping the security
feature to the people who already knew to turn it on. Rejected on those grounds; the gate itself is
kept anyway as a consequence of decision 1 (it is unreachable once every install is separated, so
it costs nothing and catches the developer path).

### Why the machine/per-user split rather than "require a logged-in human"

Requiring a console user at install time would make decisions 4 and 5 fail on every MDM push and
every unattended upgrade — the deployments most likely to be running a fleet of these. The split in
decision 3 costs one re-runnable code path and removes the only technical reason either fallback
existed.

## Consequences

**macOS**

- `scripts/build_dmg.sh` becomes the `.app` build (name and final steps change); no DMG is produced.
- `build.yml`'s `build` job: no DMG upload to R2, the release, or the manifest; the `.pkg` takes the
  `macos-arm64` id.
- `test_macos_packaged_smoke.py` retires; `test_macos_pkg_smoke.py` is the release-critical gate,
  with `test_macos_pkg_install.py` continuing to carry the real-install coverage weekly.
- `maybe_auto_enable_macos()` keeps its prompt, loses its one-shot marker, and gains decision 6's
  refusal behind it.
- README's macOS section, `docs/platform-support.md`'s macOS section and support matrix, and the
  `.pkg` section's "the DMG remains the primary distributable" all change.

**Windows**

- `installer/privacyfence.iss` runs `privilege-separation.ps1 enable` as an install step, with the
  installer's existing elevated token; its failure is an install failure.
- `docs/platform-support.md`'s "Privilege separation (opt-in)" heading and the ADR 0002 §5a
  reference under it are rewritten; `windows_acl.image_problems()`'s per-user-install message and
  `privilege_separation.py`'s module docstring keep their explanation of *why* a user-writable
  install cannot be separated, and drop "which is why the per-user tier exists".
- `mcpb/shim/src/daemon.ts`'s `%LOCALAPPDATA%` fallback stays: it is what finds an already-existing
  pre-4.x install, which is precisely what decision 6 has to be able to start and refuse.

**Linux**

- `debian/postinst` loses `|| true` on the machine half.
- `linux_privilege_separation.sh`'s `--auto` keeps its `$SUDO_USER` logic for the per-user half only.

**Everywhere**

- `privilege_separation.py` grows decision 3's split and decision 6's gate; `daemon_main.main()`
  calls the latter.
- Every hedged sentence in `docs/security-and-compliance.md` ("default-on for macOS/Linux, opt-in
  for Windows", "with either condition missing…") collapses to an unconditional one for packaged
  installs, and gains the developer-path exception instead. The `/security` page states the same.
- The download page, the Worker's manifest ids, and the D1 download-counter rows all see the macOS
  artifact id change; `docs/downloads-and-release-kpi.md`'s continuity note covers the series break.
- **Existing unseparated installs upgrade into decision 6, not into a broken state.** The upgrade
  path on every platform runs the provisioning first and only refuses if that did not take; `enable`
  already migrates a live `~/.privacyfence` (connector OAuth tokens included), so the data follows
  the account.
- ADR 0002 decision 6's asymmetry is unchanged and still the honest statement of what this buys:
  integrity is the strong guarantee, confidentiality of the review screen is not. Making every
  install separated makes the strong half universal. It does not make the weak half strong.

## Out of scope

- **Whether `step_up.require_passkey` should default on.** Decision 1 removes the reason it cannot
  be trusted; it does not decide that it should be on by default, which is a separate trade (a lost
  authenticator on a local-mode install has no IdP-backed recovery — see
  `docs/security-and-compliance.md`). Worth its own issue now that the blocker is gone.
- **Org mode**, for ADR 0002's reasons, unchanged.
- **Non-x64/arm64 targets and other Linux packaging formats.** A future Flatpak/AppImage/Homebrew
  path is subject to decision 1 like anything else — it ships if it can separate itself — but
  nothing here commits to building one.
- **Moving the approval decision off the device.** Still the stronger long-term answer, still its
  own issue.

## Verification

- No release artifact list, workflow, manifest or download-page entry names a `.dmg` for a version
  tagged after this lands; `build.yml`'s macOS job produces exactly one installable artifact.
- A packaged build, started against an unseparated data directory on each of the three platforms,
  serves neither `/mcp` nor the approvals UI, and logs the platform's own remediation command —
  the natural home for this is each platform's existing packaged-artifact test
  (`pytest.mark.packaged`), which already installs and starts the real thing.
- `test_macos_pkg_install.py` and `test_deb_packaged_lifecycle.py` assert a separated install with
  no separate `enable` call, which they already do; the Windows installer test gains the equivalent
  assertion, which is the one that does not exist today.
- Decision 3 is verifiable without a human: the machine half provisions with no console user and no
  `$SUDO_USER`, and the resulting install is separated.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — the boundary this enforces; decision 5a superseded
- [#428](https://github.com/privacyfence/privacyfence/issues/428) — privilege separation (Phase 4 = the boundary, D1 = default-on, D2 = the `.pkg`)
- [#426](https://github.com/privacyfence/privacyfence/issues/426) — the local-mode passkey, and the reason "optional hardening" expired as a premise
- [#407](https://github.com/privacyfence/privacyfence/issues/407) — the Windows per-user install tier this retires
- [#410](https://github.com/privacyfence/privacyfence/issues/410) — why `PrivilegesRequired` is already `admin`
- `docs/platform-support.md`, `docs/security-and-compliance.md` — the shipped-behavior statements that change with this
