# Migrating from 4.0 and earlier

This is for an install already running PrivacyFence 4.0.0 or earlier — a version from before
privilege separation became mandatory. If you're setting up PrivacyFence for the first time, use
[`getting-started.md`](getting-started.md) instead; it already reflects everything below.

**A plain package upgrade is not the whole migration.** Privilege separation itself is applied
automatically — it's no longer a setting, so there's nothing to opt into. But the step-up hardening
built on top of it (a mandatory passkey, the wider default scope, session provenance) reads its
configuration from a `config/settings.yaml` (or `org_config.json`) that your install already has on
disk, written by an older version of the example file. That file explicitly pins the *old* values,
and an upgrade never rewrites a config file that already exists — so those protections don't turn
themselves on just because you installed a newer package. Getting them means editing that file by
hand, once, as described below.

## What's changing, in short

- **Privilege separation — daemon runs under its own OS account, not yours — is now mandatory for
  every packaged install**, and applies unconditionally on upgrade. See
  [ADR 0003](adr/0003-separated-installs-only.md) and
  [`security-and-compliance.md`](security-and-compliance.md#privilege-separation-macos-linux-and-windows).
- **A packaged daemon that finds itself still unseparated after upgrading refuses to serve** — no
  `/mcp`, no approvals — until separation completes, rather than quietly running the old way.
- **A passkey requirement, and a wider `step_up.scope`, now default on — but only for a config file
  that never pinned a value of its own.** Yours almost certainly did, so this needs a manual edit if
  you want it (see [Turn the new hardening on](#turn-the-new-hardening-on-existing-configs-dont-move-by-themselves)).
- **Sessions now carry provenance.** Only a session minted through the companion app (`human`) can
  approve anything on a separated install; one the agent minted for itself (`unattested`) can view
  but never approve. See [`security-and-compliance.md`](security-and-compliance.md#a-session-is-not-a-human).
- **`privacyfence_get_sign_in_link` is removed.** The companion app is now the only way a human
  reaches PrivacyFence's web UI without routing a credential through the AI client it governs.
- **The one-time recovery code no longer travels in an HTTP response on a packaged install.** The
  companion shows it in a dialog on your own desktop instead.
- **The Windows non-elevated, per-user install tier is withdrawn.** If you're on it, this is a
  reinstall, not an upgrade — see the Windows section below.

## Before you upgrade

- **Back up your data directory** (`~/.privacyfence`, or `%LOCALAPPDATA%\PrivacyFence` on Windows)
  before running privilege separation for the first time, by hand or via a package upgrade. It
  migrates live connector OAuth tokens, WebAuthn credentials, `settings.yaml`, and the audit log
  into the new service-owned location; that migration is meant to be safe, but it's still a one-way
  move of the only copy of that state.
- **Know which install you're on.** A source checkout or `pip`/`pipx install privacyfence` is
  unaffected by the mandatory-separation refusal (it's how org mode is deployed and how the project
  is developed) — skip straight to [Turn the new hardening on](#turn-the-new-hardening-on-existing-configs-dont-move-by-themselves)
  for that path. Everything else below is about a packaged install: the macOS DMG/`.pkg`, the
  Windows installer, or the `.deb`.

## Per-platform steps

### macOS

If your existing install was a drag-to-`/Applications` copy of `PrivacyFenceApp.app` (the old DMG
workflow), that path is retired — the DMG no longer carries an app bundle to drag, only the `.pkg`
and the `.mcpb`. Download the new `PrivacyFence-<version>.dmg`, mount it, and run
**PrivacyFence.pkg** (don't just replace the old `.app` by hand): its `postinstall` script
provisions privilege separation itself, as root, during the install.

If for some reason you can't run the `.pkg` and end up starting the new daemon against your old,
unseparated `~/.privacyfence`, the daemon's own startup path attempts the same provisioning itself
(the admin-password prompt `getting-started.md` describes) and refuses to serve if that doesn't
succeed. You can also run it yourself ahead of time:

```bash
sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh enable
```

**Log out and back in once.** macOS only evaluates group membership at login, so until you do, your
session isn't in the `_privacyfence` group and can't reach the daemon — `... status` reports
`PENDING USER` in the meantime, not a failure. Check with:

```bash
sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh status
```

Your data directory has moved from `~/.privacyfence` to `/Library/Application Support/PrivacyFence`.
If you connected Claude Code to the old `/mcp` endpoint directly, re-run `claude mcp add` against
the new handoff directory's `mcp_url` and `privacyfence-app --print-mcp-token` (ADR 0008: `mcp_token`
is no longer a shared file under `handoff/` at all — see `getting-started.md`'s own
"Connect Claude Code (or another HTTP MCP client)" section for the exact command) — the old files
under `~/.privacyfence` are gone. The `.mcpb` shim for Claude Desktop finds the new location on its
own.

### Windows

**If you installed the older non-elevated, per-user tier** (the one that never asked for
administrator rights and could never be privilege-separated): there is no upgrade path from it.
Uninstall it and run the current installer, which requires administrator rights and installs once,
per-machine, under `%ProgramFiles%`. Someone who cannot elevate on this machine can no longer run
PrivacyFence — that's a deliberate consequence of retiring that tier (ADR 0003 decision 4), not a
bug in the new installer.

Otherwise, run the new installer normally. It now runs `privilege-separation.ps1 enable` itself as
an install step, using its own elevated token — **if that step fails, the whole install fails**
rather than leaving you with an install that looks done but isn't separated. There's nothing extra
to type into PowerShell yourself.

**Sign out and back in once**, so your session picks up the `PrivacyFenceUsers` group. Check with:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" status
```

Your data directory has moved from `%LOCALAPPDATA%\PrivacyFence` to `%ProgramData%\PrivacyFence`. If
you connected Claude Code directly to the old `/mcp` endpoint, re-run `claude mcp add` against the
new handoff directory's `mcp_url` and `privacyfence-app --print-mcp-token` (ADR 0008: `mcp_token` is
no longer a shared file under `handoff\` at all).

### Debian/Ubuntu

```bash
sudo apt install ./privacyfence_<version>_amd64.deb
```

`postinst` now runs the machine half of separation unconditionally on every install and upgrade, and
**a failure of that step now fails the package install** rather than silently leaving an unseparated
`.deb` behind. An unattended upgrade (no console user — an MDM push, `unattended-upgrades`) still
ends up fully separated; only the one per-person step — your group membership — is left pending,
which `status` reports as `PENDING USER`.

**Log out and back in once** to close that. Or close it by hand without waiting for a fresh login:

```bash
sudo privacyfence-privilege-separation enable --for-user "$USER"
sudo privacyfence-privilege-separation status
```

Your data directory has moved from `~/.privacyfence` to `/var/lib/privacyfence`. The package moves
your old autostart entries aside (`.disabled`) rather than leaving two daemons trying to start as
you; no action needed there. If you connected Claude Code directly, re-run `claude mcp add` against
the new `/var/lib/privacyfence/handoff/` files.

## Turn the new hardening on — existing configs don't move by themselves

Privilege separation is not configurable — you get it either way, automatically, per the steps
above. **The passkey requirement and the wider step-up scope are configuration, and your existing
`config/settings.yaml` already has an opinion.** Every `settings.yaml.example` before this release
wrote `step_up.enabled: false`, `step_up.require_passkey: false`, and `step_up.scope: writes` out
explicitly — and if you never touched that section, your `config/settings.yaml` almost certainly
carries those same three lines. An explicit value always wins over the new default, in both
directions, so those lines will keep meaning exactly what they said before, indefinitely, unless you
change them.

To actually get what a fresh install now gets by default:

1. Open `config/settings.yaml` and find the `step_up:` section.
2. Delete the `enabled: false`, `require_passkey: false`, and `scope: writes` lines (or set the
   first two to `true` and the third to `writes_and_pii_reads`/`writes_and_reads` explicitly).
3. Restart the daemon — `scope` and `require_passkey` changes need one; `enabled` alone can also be
   turned on later from the Settings page's Security card once a passkey is enrolled, but only after
   `require_passkey` itself is something you've decided on here.
4. **Enroll a passkey before, or immediately after, turning `require_passkey` on.** On a packaged
   install the companion app will walk you through this at its next start regardless — it checks
   whether step-up is required with nothing enrolled and opens `/security` for you if so — but doing
   it deliberately once you've made the edit avoids living in the "nothing is approved yet" banner
   state longer than necessary.
5. **Write down the recovery code the companion shows you**, in its own dialog, the first time you
   enroll. It's shown once; a later "New Recovery Code…" from the companion's menu issues a fresh one
   if you lose it, but can't re-show the original.

If you'd rather keep the old, narrower posture (no mandatory passkey, `writes`-only step-up scope)
for now, no action is needed — an upgrade alone won't change it under you.

### Org mode

Org mode has no config file that predates this release in the same sense — `org_config.json` is
rebuilt from `scripts/build_org_bundle.py` each time an IT admin changes it, so whether the wider
`step_up.scope` default reaches you depends on whether that bundle already passed
`--step-up-scope` explicitly:

- If an earlier bundle build passed `--step-up-scope writes` (or any value) on purpose, that value
  is written into `org_config.json` and stays exactly what it was — the server upgrade doesn't
  touch it.
- If `--step-up-scope` was never passed, `org_config.json` carries no `scope` key at all, and **the
  new default (`writes_and_pii_reads`) applies automatically the next time the server restarts on
  the upgraded version** — a real behavior change for that fleet, not something you opted into.
  Reads PrivacyFence's PII detector flags will start asking for a step-up assertion wherever
  `step_up.enabled` is already on. If you want to keep the narrower `writes` scope deliberately,
  rebuild and redistribute the bundle with `--step-up-scope writes`.

`require_passkey`/`enabled` behave the same way in org mode as the scope key: an explicit value in
`org_config.json` wins, an absent one takes the new default. Org mode was never covered by the
mandatory-separation change (its daemon already runs somewhere the governed AI client has no
access), so nothing there needs the per-platform steps above.

## After upgrading, on every platform

1. **Refresh your AI client's tool list**, or just let it happen on the next natural refresh.
   `privacyfence_get_sign_in_link` is gone; nothing else calls it, so a client with a cached tool
   list simply drops it. If you were in the habit of asking Claude for a sign-in link when locked
   out, use the companion app's **Open Approvals**/**Open Settings** instead, or
   `privacyfence-app --print-sign-in-link` (`privacyfence-app.exe`/`PrivacyFenceApp.exe` on Windows,
   redirected to a file — it has no console) as the break-glass path.
2. **Expect a confirmation dialog to ask for more than it used to**, if you already had
   `step_up.require_passkey` on before this upgrade. Confirming an auto-accept rule an AI client
   proposed over MCP (`privacyfence_propose_policy_change` and the older
   `privacyfence_propose_auto_accept_rule_change`) now needs the same passkey and attributable
   session that changing the same setting from the Settings page already needed — it previously
   needed neither. Denying/cancelling is unaffected.
3. **Verify the install is actually separated** with the platform's `status` command above — not
   just that it started. `PENDING USER` means one more login is outstanding; anything other than
   fully separated on a packaged build means it isn't serving `/mcp` or approvals at all yet.

## Rolling back

An older PrivacyFence build does not understand the post-separation data layout (the service
account's ownership, the `authority`/`handoff` split) or the new session-provenance and recovery-code
handling. Rolling back a packaged install therefore isn't "reinstall the old package": run
`... disable` first to move the data directory back out from under the service account, or restore
it from the backup you took before upgrading. See
[`org-mode-operational-readiness.md`](org-mode-operational-readiness.md#rollback) for the same
reasoning applied to a server deployment.

## Where to go next

- [Getting started](getting-started.md) — what a fresh install looks like today; useful as the
  target state you're migrating toward.
- [Security and compliance](security-and-compliance.md) — the trust boundary, session provenance,
  and what a passkey buys, in full.
- [ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) and
  [ADR 0003](adr/0003-separated-installs-only.md) — why each of these changes was made.
- [Platform support](platform-support.md) — the privilege-separation mechanics and layout for each
  platform.
