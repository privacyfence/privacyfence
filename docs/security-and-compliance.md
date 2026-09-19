# Security and compliance

This document describes the security controls implemented by PrivacyFence. It is a technical control reference, not a certification statement.

## Security model

PrivacyFence mediates MCP access to connected third-party services. The daemon, policy engine, approval UI, connector credentials, organization configuration, and audit log are part of the trusted computing base.

The primary goals are:

- do not release protected provider data before the configured policy permits it;
- require explicit human approval for operations configured to require review/confirmation;
- keep connector credentials and user-scoped state out of MCP-visible content;
- keep principals isolated in org mode;
- fail closed on invalid security configuration;
- preserve enough audit evidence to reconstruct policy/approval decisions.

These goals describe what the controls are built to do. How far each one extends depends on the
deployment mode — see [Local-mode trust boundary](#local-mode-trust-boundary) for where the approval
and fail-closed goals stop in local mode, which is the default.

## Deployment model

PrivacyFence runs in one of two deployment modes, chosen by IT when the daemon is configured — not
something an individual user or the AI can switch. In **local mode** (the default), one instance
runs on one employee's own machine, with a single implicit principal authorized by a random secret
written to local state; there is no sign-in. In **org mode** (opt-in), IT operates a shared
instance, typically on Ubuntu (see [`org-mode-setup-guide.md`](org-mode-setup-guide.md)), and each
person authenticates via OIDC against the organization's own identity provider — a browser session
and an MCP client's OAuth 2.1 token issued for the same sign-in resolve to the same principal.

Neither mode runs on PrivacyFence-operated infrastructure: local mode runs entirely on the
employee's device, and org mode runs on a server the organization itself provisions and controls.
There is no multi-tenant service and no PrivacyFence API that connector traffic passes through —
every tool call reaches the underlying provider (Google, Slack, Salesforce, Atlassian, Telegram)
directly from that machine or server.

## Local-mode trust boundary

**In local mode the trust boundary is the operating-system user account.** Without privilege
separation, the daemon, its state, the browser session and the AI client all run as the same user
on the same machine, so a process running as that user can reach everything the approval UI depends
on. This section states plainly what that does and does not mean, because the goals listed above
are otherwise easy to read more broadly than they hold.

**On macOS, Linux and Windows you can move that boundary** — see [Privilege separation (macOS,
Linux and Windows)](#privilege-separation-macos-linux-and-windows) below, which is what
[#428](https://github.com/privacyfence/privacyfence/issues/428) Phase 4 builds. macOS and Linux move
it **by default** as of D1 (4.1); Windows remains opt-in, per that subsection. Everything in the
rest of this section describes the un-separated install — still what a Windows install is unless
`enable` is run by hand, still reachable on any platform via `... disable`, and still what a macOS
install that bypassed the `.pkg` inside the DMG is until its one admin-password prompt is answered
(the `.pkg` itself, #428 D2, answers this at install time instead — see that subsection) or a Linux
install is until `enable --auto` can resolve who owns it (see that subsection for when it can't); that subsection says
exactly which of these statements a separated install changes and which it leaves standing.

A local process running as the signed-in user can:

- connect to the control channel under the data directory's `authority` subdirectory ([#428](https://github.com/privacyfence/privacyfence/issues/428)
  Phase 1 split this, and `config/settings.yaml`, enrolled WebAuthn credentials, and the audit log,
  out of the rest of the data directory; Phase 2 replaced the persistent `web_token` file and its
  `POST /api/bootstrap` HTTP route with a Unix domain socket (macOS/Linux) or an ACL'd named pipe
  (Windows) — a *different interface* than a browser can reach, but still no security gain on its
  own, since it still sits at the same uid as everything else there) and mint a fresh bootstrap
  code — the not-authorized page prints that exact command, deliberately, for a locked-out human;
- exchange the code for a `pf_session` cookie by visiting `/approvals?bootstrap=<code>`;
- `POST /api/approvals/<id>/decide` and release a pending approval.

No browser is involved at any step. The CSRF double-submit and same-origin checks on that last
request are defenses against a hostile web page loaded in the user's browser: such a page cannot read
the session cookie's value to echo it back, and cannot forge an `Origin` header. Neither constrains a
local process, which holds the cookie and sets its own headers. The same distinction applies to every
other control on this path — the bootstrap code's short TTL, its single-use consumption, and the
session's idle and absolute expiry all limit how long a *leaked* credential stays useful, not who may
mint one.

This matters more here than it would in most single-user software, because the process most likely to
do it is the one PrivacyFence exists to govern: an MCP client with shell access on the same machine is
the normal local-mode install.

**What the approval gate does defend against in local mode:** an AI client acting through `/mcp`
alone; mistakes and unattended drift; a remote attacker without code execution on the machine; and a
hostile web page in the user's browser. Those are real, and they are what the gate does day to day.

**What it does not defend against in local mode:** a local process, running as the signed-in user,
acting deliberately. Treat the approval gate there as a workflow control with a strong audit trail,
not as a boundary against local code execution.

**Org mode does not share this**, for a structural reason rather than a difference in checks: the
daemon runs on a server the organization operates, so an AI client on an employee's device has no
loopback access to it, no control channel to reach and no bootstrap endpoint to call —
`privacyfence_get_sign_in_link` raises there outright. Authentication is IdP-backed, and where
configured, WebAuthn step-up binds a write approval to a fresh user-verified assertion.

**Closing this in local mode** takes two changes, both tracked: running the daemon under its own
account so its state is neither readable nor writable by processes running as the user
([#428](https://github.com/privacyfence/privacyfence/issues/428) — Phases 1 and 2, a state-layout
refactor and the control-channel interface itself, have landed; Phase 4's actual privilege
separation is what closes this, and has now shipped on all three desktop platforms — default-on for
macOS and Linux (D1, 4.1), opt-in for Windows — see below), and giving the human a way
into the web UI that does not route a credential through the AI client
([#427](https://github.com/privacyfence/privacyfence/issues/427) — the companion app, Phase 3).
Local-mode WebAuthn step-up ([#426](https://github.com/privacyfence/privacyfence/issues/426))
depends on both: a passkey enrolled in a credential store the agent can rewrite is not a control.
Phase 1 (config plus a `/security` enrollment page, mirroring org mode's) and Phase 2 (the
decide-time check itself) have both landed for local mode now that Phase 4 above has, since the
credential store the assertion is checked against is exactly the one Phase 4 makes service-owned.
With `step_up.enabled` set, local mode's own `/api/approvals/{id}/decide` demands a fresh WebAuthn
assertion before releasing an approving decision on a write (or on a read too, in a wider
`scope` -- see below) -- mirroring org mode's own gate, minus the IdP re-authentication fallback local mode has no
equivalent of. With `step_up.enabled` alone, and no passkey enrolled, there is no ceremony left to
demand and the decision goes through unguarded rather than deadlocking behind one nobody could
complete -- so with only `enabled` set, simply never enrolling a passkey dodges the check entirely.

**What step-up covers is `step_up.scope`, and it means the same thing in both modes** -- one
`StepUpConfig` (`step_up_config.py`) and one predicate (`webauthn_stepup.is_step_up_required`)
serve local and org mode alike; only where the value is configured differs
(`config/settings.yaml`'s `step_up:` section vs `org_config.json`'s, the latter written by
`scripts/build_org_bundle.py --step-up-scope`). Narrowest first:

| `scope` | write | read flagged by `pii_detector.py` | any other read |
| --- | --- | --- | --- |
| `writes` (default) | passkey | — | — |
| `writes_and_pii_reads` | passkey | passkey | — |
| `writes_and_reads` | passkey | passkey | passkey |

Denying never needs step-up under any scope (denying discloses nothing), and neither does a bare
confirm dialog, which is a second step inside a decision the card it belongs to already gated. A
read an auto-accept rule covers never becomes an approval in the first place, so no scope asks for
a passkey on one -- `writes_and_reads` widens what a *pending* approval costs to release, not what
gets gated. Pick it over `writes_and_pii_reads` when the install would rather not depend on PII
detection having flagged everything worth a second factor; the cost is a passkey prompt on every
read a rule doesn't already cover.

**`step_up.require_passkey` (Phase 3) is what makes it a guarantee rather than an opt-in check.**
With it on: an approving decision with nothing enrolled is hard-failed (`403`, naming `/security`)
rather than let through; the same is true for a sensitive subset of the local settings actions --
the rule-row, grant, policy and PII actions in `web/routes_settings.py`'s own `_SENSITIVE_ACTIONS`
-- so an agent that cannot forge an approval cannot route around the gate by adding an always-allow
rule or a broader grant either, since that action itself now demands the same fresh assertion; and
removing your last enrolled credential always needs one first, regardless of this flag, so a session
alone cannot un-enroll its way back to the unguarded state. If nothing is enrolled when the daemon
starts with `require_passkey` on, it still starts (refusing to boot would remove the one path,
`/security`, that fixes the misconfiguration) but shows a persistent banner on every page until a
passkey is added, and releases nothing in the meantime. Treat local-mode step-up as a real guarantee
once `require_passkey` is on and a passkey is enrolled; with `enabled` alone it stays what it always
was -- opt-in, evadable by simply not enrolling.

### Tamper-evidence and recovery for local-mode step-up (#426 Phase 4)

Four events on the credential-store/requirement lifecycle are written to the audit log, each on its
own `decision` value (see `audit_log.py`'s own field docstring for the exact strings): enrolling a
passkey, removing one, a recovery code being spent, and the `step_up.require_passkey` requirement
itself turning on or off. None of these prevent anything on their own — see the framing in [What #426
does and doesn't guarantee](#local-mode-trust-boundary) above: they are detection after the fact,
recording that a change happened rather than stopping one that shouldn't have — see [Local-mode
trust boundary](#local-mode-trust-boundary) above for what this feature does and does not guarantee
on its own. They matter for the same reason the rest of the audit log does: a reviewer (or a future
automated check) can reconstruct what changed and when, rather than trusting the current state of
`config/settings.yaml` and `webauthn_credentials.json` to be the whole story.

**Requirement changes were, through #426 Phase 4, only observable at daemon startup** — there was no
UI path to flip `step_up.require_passkey` at all, so a *change* could only be caught by comparing the
value a fresh startup loads against what the previous startup last saw, via `webauthn_stepup.
observe_step_up_requirement`. B9 of the 4.1.0 action plan added a one-directional Settings-page
control (the General page's Security card, `SettingsController.enable_step_up`) that turns this
requirement *on* — never off — the moment a passkey is already enrolled, and calls
`observe_step_up_requirement` itself right away rather than waiting for the next startup, so that
transition is audited immediately. Turning the requirement back *off* still has no UI path and remains
a `config/settings.yaml` edit plus a restart, deliberately: that asymmetry is what keeps the "treat this
install as compromised" banner below trustworthy — a disable it observes never came from a control in
the human's own browser. An install predating B9, or one where the requirement has never changed
either way, produces nothing extra to audit at startup. Restarting with the same value twice in a row
is silent, by design — only an actual transition is recorded.

**Turning the requirement off latches a persistent banner**, not just a one-time audit line — the
same `pf-shell-banner` the Phase 3 "nothing enrolled yet" notice uses, on both `/approvals` and
`/settings`, and a matching warning in the daemon's own log at every startup while it holds. It
survives further restarts on its own: a local process could otherwise flip the flag off, wait out a
restart, and flip it back on before a human ever reads the log, leaving only a single easy-to-miss
audit line as the record. The banner instead persists until a startup observes `require_passkey`
back on — at which point turning it on is itself audited too, and the banner clears.

**Recovery: what happens when the only enrolled authenticator is lost** (a new machine, a wiped
TPM). With no IdP, local mode has no remote reset — before [privilege separation](#privilege-separation-macos-linux-and-windows),
the honest answer was "edit `config/settings.yaml` from a shell and restart", which is the same door
this whole feature exists to close for an adversary, not merely to close for everyone else too. Once
that door needs the service account or an elevation prompt, a sanctioned way back in stops being
optional. `web/routes_security.py`'s enrollment flow (`register_verify`) issues a one-time recovery
code — a 16-character, human-typeable string in four groups — the moment a principal doesn't
currently have an unused one on file, most often their very first enrollment. It is shown to the
browser exactly once, in that same response, and never again: only a salted SHA-256 hash of it is
stored (`webauthn_stepup.py`'s own `generate_recovery_code`/`consume_recovery_code`), alongside the
credential file itself, under the same service-owned root a separated install protects. Trading the
code in at `POST /security/recover` needs no WebAuthn ceremony — deliberately, since producing one is
exactly what a locked-out human cannot do — only the still-valid session that got them to `/security`
in the first place, which local mode's ordinary sign-in path (a bootstrap link) still provides even
with `require_passkey` on, since step-up gates *decisions*, not sign-in itself. A successful trade-in
removes every credential enrolled for that principal, clearing the stuck state so a fresh passkey can
be enrolled immediately afterward, and is itself audited (`webauthn_recovery_code_used`) whether or
not the human goes on to enroll again. The code is single-use: spending it, correctly or not, never
grants a second attempt at the same code, and a fresh one is only issued at the next successful
enrollment.

### The approval binder's single assertion

`POST /api/approvals/batch/decide` lets a human release several pending approvals at once from
`/approvals` (see [`approval-list-ui-ux.md`](approval-list-ui-ux.md#the-approval-binder)) behind a
single WebAuthn ceremony, in both local and org mode, whenever `step_up` applies to anything
selected. `webauthn_stepup.batch_decision_fingerprint` plays the role for the batch that
`decision_fingerprint` plays for one decision: a SHA-256 over the canonicalized, sorted
`approval_id:result` pairs of the *entire* submitted set, deny items included. The server
recomputes it from the resubmitted items on completion and compares against the one it minted the
challenge for — it never trusts a client-supplied set — so an assertion obtained for one selection
cannot be replayed to authorize a larger set, a smaller one, or the same set with one result
flipped; like the single-decision challenge, it is single-use and short-lived.

**What one assertion establishes is the same as a per-item one, extended to a set:** the same
freshness and the same user-verification requirement `step_up.enabled` already demands per
decision, now bound to an exact, tamper-evident set of decisions rather than to one.
`step_up.require_passkey` fails the whole batch closed — the same `403` naming `/security`,
nothing applied — exactly as it already does for a single decision, because it is the same check,
run once instead of N times. An install that wants no single-assertion batching at all can set
`step_up.batch: per_item`, which refuses to admit a batch containing anything that needs step-up
and pushes those items back to their own card, one ceremony each.

**What it does not establish is per-item attention, and that is not new to the binder.** Per [ADR
0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) decision 6, step-up's own guarantee
was already asymmetric: *integrity* is the strong half — the agent cannot approve its own request —
and *confidentiality of the review screen* is the weaker half — an agent that reaches the web UI
can still read what is pending. The binder inherits that asymmetry rather than widening it:
batching changes how many times a human authorizes, never what they are shown before doing so.
Every batched row still carries only the metadata-only inline disclosure described in
[`approval-list-ui-ux.md`](approval-list-ui-ux.md#the-approval-binder), never the full card, and
the classes that would make one ceremony stand in for a materially different kind of decision — a
PII-forced confirmation, a `confirm`/`choice` dialog — are excluded from batching altogether
(`approvals.PendingApproval.is_batchable()`). A batch assertion authorizes exactly the set of rows
a human selected, with the disclosure the list already showed for each; it authorizes nothing wider
and is no substitute for opening a card when the human wants one.

### Privilege separation (macOS, Linux and Windows)

One script per platform runs the daemon under a dedicated account instead of yours. It creates that
account, moves the data directory to a system location owned by it, and inverts the startup wiring
so the daemon leaves your session and the companion app enters it:

| | macOS | Linux | Windows |
|---|---|---|---|
| Script | `scripts/macos_privilege_separation.sh` | `privacyfence-privilege-separation` (`.deb`), or `scripts/linux_privilege_separation.sh` | `privilege-separation.ps1`, installed next to the app (elevated PowerShell) |
| Account | `_privacyfence` | `privacyfence` | `NT SERVICE\PrivacyFence` (a virtual service account) |
| Data directory | `/Library/Application Support/PrivacyFence` | `/var/lib/privacyfence` | `%ProgramData%\PrivacyFence` |
| Daemon starts as | a LaunchDaemon | a system systemd unit (`privacyfence-daemon.service`) | a Windows service (`PrivacyFence`) |
| Companion starts as | a LaunchAgent (the menu-bar app) | an XDG autostart entry running `privacyfence-companion --serve` | a Scheduled Task (`PrivacyFenceCompanion`, the tray app) |
| Replaces | the login-session LaunchAgent | the `.deb`'s XDG autostart entry and the `--user` unit | the installer's own `PrivacyFence` Scheduled Task, disabled rather than deleted |

All three still ship the manual `enable`/`disable`/`status` subcommands above; the migration moves
live connector OAuth tokens, so take a backup first if running one by hand. `... disable` reverses
it on any platform. **macOS and Linux now turn this on by default as of #428 D1 (4.1)**, rather than
waiting out the originally-planned soak period: the `.deb`'s `postinst` runs `enable --auto` itself,
root already, on every install and every upgrade ([`debian/postinst`](../debian/postinst)); an
install that is just a copied app bundle has no equivalent package-manager hook, so the daemon's
own startup asks once, via the standard admin-password dialog, the first time it finds itself
unseparated (`privilege_separation.maybe_auto_enable_macos()`). #428 D2 (4.1) gives macOS a
root-context install-time hook of its own — `scripts/build_pkg.sh`'s signed `.pkg`, whose own
`installer/macos/pkg/postinstall` script runs `enable --auto` itself while the package install is
still running, the same shape as the `.deb`'s `postinst`. That `.pkg` is what the downloaded DMG
carries, so it is the ordinary macOS path now and the runtime dialog is the fallback. `--auto` (used by all three
triggers now, never by a human directly) is the same `enable`, made safe to run unattended: anywhere
it can't safely tell who owns the install or find the daemon's executables, it logs why and leaves
the install opt-in rather than guessing or failing a package install. **Windows stays opt-in** — D1
does not extend to it, on top of the install-tier and mandatory-companion requirements below, which
raise the bar for an unattended default beyond what the two POSIX platforms needed.

**Windows expresses the same layout in a different primitive, and adds one requirement the others
do not have.** There are no permission bits there, so the modes below are NTFS ACLs
(`icacls`), applied at enable time and re-checked on every daemon start. Two consequences are worth
stating rather than leaving to be discovered:

- **A service runs whatever its `binPath` names**, so the install location is part of the boundary.
  PrivacyFence installed under your own profile — the non-elevated, per-user path the installer
  offers ([#407](https://github.com/privacyfence/privacyfence/issues/407)) — would let a process
  running as you rewrite the daemon's own executable and have the service run it *as the service
  account*, which is worse than not separating at all. So `enable` refuses against a user-writable
  install and says why; privilege separation on Windows requires the per-machine install under
  `%ProgramFiles%`. That is the resolution of the open question [ADR
  0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) carried: two install tiers, with
  separation available only on the elevated one.
- **The companion is mandatory, not a convenience.** A Windows service runs in session 0 and cannot
  reach your desktop, so without the tray app there is no way for connector OAuth
  (Slack/Salesforce/Atlassian) to open a sign-in page at all — the case ADR 0002 decision 5 was
  written for, arriving where it was predicted. `enable` refuses to install the daemon half alone.

One thing has no POSIX counterpart at all: **ownership is part of the boundary**. An object's owner
on Windows can rewrite its ACL regardless of what that ACL says, so `enable` takes ownership of the
data directory (to `Administrators`) rather than letting the move out of `%LOCALAPPDATA%` leave it
with you — otherwise every permission above would be advisory against the one account it is meant
to exclude. `… disable` hands ownership back.

One thing is *tighter* on Windows than on POSIX: the shared handoff directory is readable by the
group, not writable. POSIX has to grant `rwx` there because the companion creates its own socket
file in it and `connect(2)` needs write permission on the node; both Windows control channels are
named pipes rather than files, so nothing in your session ever creates anything there.

The Linux companion is where the two differ in more than naming. It has no tray (ADR 0002 decision
4's dependency budget), so what autostarts is `--serve`: the companion's control channel alone, no
icon and no menu. That is not a convenience — a separated daemon has no desktop session, so
`webbrowser.open()` from it reaches nothing, and connector OAuth for Slack/Salesforce/Atlassian
would have no way to show you a sign-in page. The clickable Applications-menu entry (Open
Approvals, Open Settings, Quit) is unchanged and still one-shot.

**What it closes.** Five things stop being true for a process running as your own account:

- it can no longer edit `config/settings.yaml` — the always-allow rules, auto-accept grants and PII
  configuration that decide what it is allowed to do without asking;
- it can no longer write `webauthn_credentials.json`, which is what makes a local passkey
  ([#426](https://github.com/privacyfence/privacyfence/issues/426)) mean anything at all rather than
  a checkbox a local process ticks for itself;
- it can no longer read the audit log's HMAC key, which is the caveat [Audit integrity and
  forwarding](#audit-integrity-and-forwarding) carries;
- it can no longer read the connector credentials the daemon holds;
- it can no longer drive the companion's browser-opening channel (`OPEN <url>`) — that socket stays
  group-shared for the daemon's own use, but a connection is now refused unless it comes from the
  daemon's service-account uid, so sharing the group no longer lets it hand your browser an
  attacker-chosen http(s) URL under a connector-OAuth pretext (#428 B10).

Those four files live under `<system root>/authority`: mode `0700` owned by the service account on
macOS and Linux, and on Windows an ACL granting that account (plus `SYSTEM` and `Administrators`)
and nothing else. The root above it is `0711` — traversable so your session can reach the handoff
directory, never listable — which on Windows is an `icacls` grant of traverse-only to `Users`, with
the inheritance `%ProgramData%` hands out severed first.

**What it deliberately does not close.** Minting a session stays reachable from your own account.
The companion app and the AI client both run as you, and no permission bit can tell them apart —
`SO_PEERCRED`'s uid answers "which user", never "which program". Rather than build a process-identity
check that would be sound on macOS and weak on Linux, the design makes a session *insufficient*
instead of *uncallable*: this phase takes the human-authority files away, and #426's passkey then
makes possession of a session not enough to release an approval — but only once [privilege
separation](#privilege-separation-macos-linux-and-windows) is active, `step_up.enabled` and
`step_up.require_passkey` are both set, and a passkey is enrolled. None of those four is this
deployment's default, so on a default install the session is still sufficient on its own: the agent
can approve its own request. The consequence, stated rather than left to be discovered for an
install that turns all four on: **integrity is the strong guarantee — the agent cannot approve its
own request — while confidentiality of the review screen is the weaker one**, since an agent that
reaches the web UI can still read what is pending. The control channel's socket accordingly lives in
a group-shared `<system root>/handoff` directory, not under `authority`, along with the agent's own
`mcp_token` (which is the agent's credential and is meant to stay reachable). See
[ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) decision 6 for the full
reasoning.

**And it does not survive root.** `sudo` re-owns any file and reconfigures any LaunchDaemon or
systemd unit; a local Administrator on Windows takes ownership of any file and reconfigures any
service. What this defends against is an agent running with your *normal* privileges — the ordinary
case — and it makes escalation require an authentication prompt you see.

`sudo scripts/<platform>_privilege_separation.sh status` checks the layout on disk: the three
directory modes, that `authority` really is owned by the service account rather than still by you,
that your login session has picked up the group membership (it needs a logout/login after enabling),
and that the daemon's LaunchDaemon/systemd unit is loaded — on Linux it also checks that the old
autostart entry is gone and the companion's new one is in place, since either being wrong is a
working install that quietly loses a capability.
The daemon re-checks the same things at startup and logs them under `SEC-09`,
and refuses to start outright if it finds itself running as the wrong account — on a separated
install that would otherwise look like a silent policy reset rather than a failure, since it could
not read the real `settings.yaml` and would seed a default one.

## Authentication boundaries

### Local web UI

The local browser UI is not authenticated by a reusable token in the URL. The daemon uses a one-time bootstrap exchange to establish an HttpOnly session cookie. Browser requests are then authenticated from that session.

Mutating requests require the authenticated session, same-origin checks, and CSRF validation. Session/bootstrap secrets are not intended for logging or propagation into connector data.

### MCP-issued sign-in links

`privacyfence_get_sign_in_link` is a meta-tool, available over `/mcp` like every connector tool, that mints a fresh bootstrap link for this same local web UI (`/approvals` or `/settings`) and returns it to the calling MCP client. It is dispatched directly rather than through the gated-call path every connector tool uses — deliberately: the human approval that path would require lives behind the very UI a locked-out user is trying to reach, so gating this tool on that UI would be circular.

What bounds it instead: local mode only (it raises in org mode, which authenticates through IdP-backed OAuth rather than a bootstrap link, so it can never return a working credential there); the link it mints is the same single-use, short-lived bootstrap code every other sign-in path in this section uses, consumed by the first visit whether or not it succeeds; `page` is allowlisted to `approvals`/`settings`, never an arbitrary path; and the local web UI is bound to `localhost`, so the link is only useful from the same machine the MCP client and daemon are already both running on. Every call is written to the audit log under its own `sign_in_link_issued` decision, carrying the calling client's self-reported reason — the same disclosed-and-unverified posture every other tool's `reason` parameter has.

Net effect: an MCP client can obtain a working session for the human-facing approval/settings surface without a human first approving that specific request. The justification this paragraph used to give — that such a client already holds equivalent-or-greater access via every other tool this daemon exposes — holds for connector reads and writes, which are themselves gated. It understates one case: a session also reaches the approval UI, so it can *release* a gated call rather than merely request one, and that is the product's central control rather than one more tool. This is not a weakness introduced by this tool — see [Local-mode trust boundary](#local-mode-trust-boundary), where a process running as the user mints the same session through the control channel without it — but it should not be described as a neutral consequence of existing trust either. Like every tool over `/mcp` (meta-tools included), it is advertised with the same uniform read-only/non-destructive annotations regardless of this real effect — see [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#meta-tools) for why those are MCP UI hints, not a security boundary, and [issue #46](https://github.com/privacyfence/privacyfence/issues/46) for the broader question of whether that uniform advertisement should change.

**Revised, #426 Phase 4:** the paragraph above is still true of a session by itself, and stays true regardless of configuration — this tool has no `step_up` awareness of its own, and doesn't need any: minting a session was never the part step-up narrows. What changes is what that session is *sufficient for*, and only under two conditions together, neither of which is this deployment's default. With [privilege separation](#privilege-separation-macos-linux-and-windows) active (default-on for macOS/Linux as of #428 D1, opt-in for Windows) **and** `step_up.require_passkey` turned on in `config/settings.yaml` (opt-in everywhere — reachable from the Settings page once a passkey is enrolled, B9, or still by hand; see `step_up_config.py`'s own `LiveStepUpConfig` docstring), the credential store a step-up assertion is checked against is no longer writable by the same process minting the session, so that session alone can no longer release an approving decision on a gated write, nor change what a future write can reach through `_SENSITIVE_ACTIONS` (an always-allow rule, a grant, a relaxed default policy). It can still mint the session, still view what's pending, and still hold read access to the review screen — the confidentiality half [ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) decision 6 names as the weaker guarantee, left open on purpose. With either condition missing — no privilege separation, or `step_up.require_passkey` left off — the original paragraph's net effect stands unqualified: the session is enough on its own.

### Local MCP

The local `/mcp` endpoint uses the generated bearer token stored in the user's PrivacyFence state so local MCP clients/shims can authenticate independently from the browser session.

### Org mode

Org mode authenticates human users through the configured OIDC provider and applies PrivacyFence's org authorization/session model to MCP and web traffic. Principal identity is carried explicitly through request handling and user-scoped storage/connector resolution.

Where configured, WebAuthn step-up is used for sensitive org-mode approval actions. Credential enrollment and lookup are scoped to the authenticated principal. By default, step-up accepts either a passkey assertion or a fresh IdP re-authentication; `step_up.require_passkey` ([#406](https://github.com/privacyfence/privacyfence/issues/406)) closes the IdP-reauth path for organizations that want hardware-bound WebAuthn as a hard requirement — a compromised or phished IdP session can no longer satisfy step-up on its own, and a principal with no enrolled passkey is hard-failed toward enrollment rather than silently allowed through the weaker path.

## Authorization and principal isolation

Local mode has one principal for the daemon instance. Org mode supports multiple principals and maintains user-scoped state under principal-aware paths.

`ConnectorRegistry` creates/caches connector hosts per principal. Service authorization callbacks evict the affected principal's cached connector host so subsequent calls use the updated credentials.

Org approval routes filter/authorize by principal rather than exposing the local-mode all-pending-approvals view across users.

## Approval and policy enforcement

Tool calls pass through the common gate before connector execution where required by policy. A user decision is bound to the pending request; stale/already-resolved approvals are not reusable as fresh authorization.

Always-allow rules are explicit scoped policy objects, not global bypasses. Rule matching is documented in [`always-allow-rules-reference.md`](always-allow-rules-reference.md).

Policy denials and unattended-mode restrictions fail before protected connector results are released.

These are enforcement properties of the gate itself. In local mode they bind an AI client acting
through `/mcp`; they do not bind a local process that reaches the web UI directly — see
[Local-mode trust boundary](#local-mode-trust-boundary).

## PII and content privacy

Provider content can be inspected for PII before release. Organization policy can allow, redact, or block configured categories. Invalid policy values are rejected instead of falling back to permissive behavior.

Preview and scan paths are bounded to avoid unbounded processing of provider-controlled content. Structured file parsing uses dedicated extraction code and hardened XML parsing where applicable. See [`file-type-support.md`](file-type-support.md) and [`pii-detection-keywords.md`](pii-detection-keywords.md).

## Credential and secret handling

Connector OAuth/session credentials are stored in PrivacyFence state, not returned through MCP tools. File creation/update paths that contain credentials use the repository's secure file helpers and restrictive permissions where the operating system supports them.

The self-hosted live-provider test runner keeps its real QA connector credentials outside GitHub-hosted runners and outside committed repository content. See [`connector-live-check-setup.md`](connector-live-check-setup.md).

Each connector's OAuth client secret is shared across every user of a given deployment rather than issued per-user: it authenticates the PrivacyFence installation to the provider, not an individual end user. A leaked client secret should be rotated with the provider directly; PrivacyFence itself has no per-secret rotation schedule or automated rotation mechanism.

## Organization configuration trust

Org-mode configuration is validated before use, including the configured trust/signature model for organization bundles. Startup should fail when required trust/configuration fields are absent or invalid rather than silently switching to a weaker mode.

Operators should protect the org configuration/trust material as deployment configuration and control who can replace it.

## HTTP security controls

The embedded web application applies security headers and CSP. Inline script/style elements required by the generated UI use per-response/content nonces rather than broad unsafe-inline allowances.

Browser sessions use HttpOnly cookies and same-origin/CSRF checks for state-changing operations. Sensitive bootstrap/bearer material is not intended to be carried in persistent browser URLs.

Deploy org mode behind the configured HTTPS reverse proxy and preserve the Host/origin assumptions documented by the setup guide.

Org mode's OAuth dynamic client registration (DCR) endpoint bounds its own resource usage: it caps the total number of registrations it will hold at once, validates registration metadata against a size limit, and prunes stale/expired registrations on an age-and-count basis rather than retaining them indefinitely.

## Download staging

Org-mode files that cannot be returned inline can be staged as encrypted temporary content and served from an opaque short-lived download token. Staged-link lifetime and inline-size thresholds are configurable and validated.

See [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## Audit integrity and forwarding

PrivacyFence records gate/approval activity in its audit log, including principal information in org mode. Every entry is unconditionally chained to the one before it with a keyed hash (HMAC-SHA256) — this isn't an opt-in feature; `AuditLogger` computes it for every install, and `verify_chain()` (or `scripts/verify_audit_log.py`) detects a line inserted, edited, or removed after the fact.

The chain's signing key lives next to the `.jsonl` files it protects, at the same file permissions. That defends against accidental corruption and against a party who gains write access to the log files specifically (e.g. a bug in some other export/backup path) without also reading the key — it does **not** defend against a party who already has full read/write access to the audit directory, since that party can read the key alongside the log and recompute a consistent chain over a tampered file. The real defense against that threat is a copy that leaves this trust boundary entirely — see the forwarding paragraph below. In local mode that party includes any process running as the signed-in user (see [Local-mode trust boundary](#local-mode-trust-boundary)), so forwarding carries more of the weight there than the file permissions do — unless the install has separated privileges (default-on for a fresh macOS/Linux install as of #428 D1, opt-in on Windows; [privilege separation](#privilege-separation-macos-linux-and-windows)), which moves the audit directory and its key onto an account that user does not hold, and is exactly the change that lets the file permissions carry their own weight again.

Org deployments can use the implemented forwarding/export path for external retention/monitoring. Forwarding does not replace local operational decisions about retention, backup, and access control.

Treat audit data as sensitive: it can reveal which services/tools/resources were used even when protected content itself was not released.

## Dependencies and supply chain

Runtime/test/build dependencies are declared in `pyproject.toml`, with release/dependency audit workflows under `.github/workflows/` and lock/update tooling under `requirements/` and `scripts/`.

CI includes dependency auditing and static analysis in addition to the normal test suite. Ruff and Bandit are blocking in the test workflow. mypy runs twice: informationally over the whole tree, and blocking over the modules promoted by `[[tool.mypy.overrides]]` in `pyproject.toml` (`scripts/mypy_strict_modules.py`), which is how security-critical modules are held to strict typing once they are clean.

Each tagged release build generates a CycloneDX software bill of materials (SBOM) alongside the packaged artifacts.

Release artifacts use the platform signing/notarization paths described in [`platform-support.md`](platform-support.md).

## Operational security

Back up only the state your deployment needs and protect backups equivalently to the live credentials/configuration they contain. Restore procedures must preserve file ownership/permissions and should be tested on a non-production copy.

PrivacyFence uses a single-instance file lock via `portalocker`; one state directory should not be actively served by multiple daemon processes at once.

For centralized deployments, availability depends on the operator's service/reverse-proxy design. PrivacyFence itself is not a clustered shared-state service.

See [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md).

## Testing evidence

Current automated security evidence includes unit/integration tests, browser/CSP tests, coverage-floor enforcement, static analysis, Python compatibility checks, scheduled live-provider checks, and release/platform smoke coverage described in [`testing-policy.md`](testing-policy.md).

What is deliberately left to human judgment rather than automated, and why, is in [`testing-policy.md`](testing-policy.md)'s "What deliberately remains manual".

## Vendor risk criteria

PrivacyFence has no certified information security management framework (e.g. ISO 27001), no
Business Continuity Plan, and no contractual risk-response process or SLA. This is a structural
consequence of the deployment model above, not an oversight:

- **Certified information security framework:** none — there is nothing to certify, since there is
  no PrivacyFence-operated infrastructure (see Deployment model above). Such certifications attest
  to controls around *operated* infrastructure, which doesn't exist here.
- **Business continuity plan:** none — there is no PrivacyFence-operated service whose outage could
  disrupt a deployment. If the maintainer became unreachable, already-installed copies keep running
  exactly as before; the code being open source lets an organization audit, fork, or maintain a
  pinned version independently of the original maintainer.
- **Risk response process / SLA: None.** Reports are handled best-effort, not against a committed
  response time — see [`SECURITY.md`](../SECURITY.md) for how to report and what to expect.

None of this changes the technical risk profile described elsewhere in this document — no vendor
infrastructure in the data path, no new data processor, human-in-the-loop enforcement on sensitive
calls, and a local audit trail. Organizations evaluating PrivacyFence against a standard
vendor-risk questionnaire should treat the absence above as a risk-acceptance decision, not a
security gap: approve it through a risk-acceptance/exception process rather than a standard
vendor-security sign-off, pin deployments to a specific reviewed release rather than auto-updating,
and assign an internal owner to track new releases and patch or roll back if a report doesn't land
in time.

## Vulnerability reporting

Report suspected vulnerabilities to **info@privacyfence.eu**, or use GitHub's private vulnerability
reporting from this repository's Security tab, rather than a public issue. See
[`SECURITY.md`](../SECURITY.md) for the full disclosure process, what to include in a report, and
scope.

## Compliance positioning

PrivacyFence provides technical controls that can support an organization's privacy/security program, including approval gates, PII filtering, principal isolation, secure credential handling, audit logging, and controlled deployment configuration.

Whether a deployment satisfies a particular regulatory, contractual, or certification requirement depends on the organization's configuration, infrastructure, policies, identity provider, retention practices, operational procedures, and independent compliance assessment. The repository documentation should not be read as claiming certification by itself.
