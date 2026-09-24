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

On a privilege-separated install with more than one OS account in its service group, this boundary
now applies **per account**: [ADR 0008](adr/0008-one-principal-per-os-user.md) gives each one its
own principal, so what follows in this section (a local process reaching everything the approval UI
depends on) is true of *that account's own* PrivacyFence state, never another account's on the same
machine. See [Authorization and principal isolation](#authorization-and-principal-isolation).

**On macOS, Linux and Windows every packaged install moves that boundary automatically** — see
[Privilege separation (macOS, Linux and Windows)](#privilege-separation-macos-linux-and-windows)
below, which is what [#428](https://github.com/privacyfence/privacyfence/issues/428) Phase 4 builds
and [ADR 0003](adr/0003-separated-installs-only.md) makes mandatory rather than opt-in on any of the
three. A packaged build that finds itself unseparated does not serve at all (ADR 0003 decision 6 —
no `/mcp`, no approvals), so the un-separated install the rest of this section describes is not
something any of the three platforms' installers ship: it is reachable only via `... disable`
on Windows or `... uninstall --purge` on macOS and Linux (documented and deliberate — see that
subsection), or from a non-packaged source/pip checkout run
with `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1` for local development (never a real deployment — see
that subsection and [ADR 0003](adr/0003-separated-installs-only.md) decision 7). That subsection
says exactly which of the statements below a separated install changes and which it leaves
standing.

A local process running as the signed-in user can:

- read the current sign-in link straight out of `handoff/approvals_url` — **no longer: that file
  is not written any more** (the self-approval plan's Phase 2). It was the second of the three paths to a session §02 of that review
  counts, and the only one that took no more than reading a file;
- connect to the control channel under the data directory's `authority` subdirectory ([#428](https://github.com/privacyfence/privacyfence/issues/428)
  Phase 1 split this, and `config/settings.yaml`, enrolled WebAuthn credentials, and the audit log,
  out of the rest of the data directory; Phase 2 replaced the persistent `web_token` file and its
  `POST /api/bootstrap` HTTP route with a Unix domain socket (macOS/Linux) or an ACL'd named pipe
  (Windows) — a *different interface* than a browser can reach, but still no security gain on its
  own, since it still sits at the same uid as everything else there) and mint a fresh bootstrap
  code — the not-authorized page prints that exact command, deliberately, for a locked-out human;
- exchange the code for a `pf_session` cookie by visiting `/approvals?bootstrap=<code>`;
- `POST /api/approvals/<id>/decide` and release a pending approval — **on a separated install this
  last step no longer follows from the first two**: a session minted by a bare `MINT` is
  `unattested`, and an approving decision requires one a human was asked for. See
  [A session is not a human](#a-session-is-not-a-human) below, which is the only part of this list
  privilege separation changes rather than merely relocates.

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
loopback access to it, no control channel to reach and no bootstrap endpoint to call — and, since
the self-approval plan's Phase 2 retired `privacyfence_get_sign_in_link`, no tool that would mint
one in either mode. Authentication is IdP-backed, and where
configured, WebAuthn step-up binds a write approval to a fresh user-verified assertion.

**Closing this in local mode** takes two changes, both tracked: running the daemon under its own
account so its state is neither readable nor writable by processes running as the user
([#428](https://github.com/privacyfence/privacyfence/issues/428) — Phases 1 and 2, a state-layout
refactor and the control-channel interface itself, have landed; Phase 4's actual privilege
separation is what closes this, and now ships mandatorily on all three desktop platforms —
[ADR 0003](adr/0003-separated-installs-only.md), see below), and giving the human a way
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
| `writes` | passkey | — | — |
| `writes_and_pii_reads` (default) | passkey | passkey | — |
| `writes_and_reads` | passkey | passkey | passkey |

Denying never needs step-up under any scope (denying discloses nothing), and neither does a bare
confirm dialog, which is a second step inside a decision the card it belongs to already gated. A
read an auto-accept rule covers never becomes an approval in the first place, so no scope asks for
a passkey on one -- `writes_and_reads` widens what a *pending* approval costs to release, not what
gets gated. Pick it over `writes_and_pii_reads` when the install would rather not depend on PII
detection having flagged everything worth a second factor; the cost is a passkey prompt on every
read a rule doesn't already cover.

The default was `writes` through 4.1 and is `writes_and_pii_reads` from 4.2, in both modes. The
narrower one was chosen when the adversary in view was a human at the keyboard; [ADR
0003](adr/0003-separated-installs-only.md) adopts a different one — an agent with code execution
on this machine — and against that adversary `writes` leaves every read releasable by a session
alone, including one PrivacyFence itself flagged as carrying personal data. Exfiltration is the
obvious thing such an agent wants. An install with `scope:` written out in `config/settings.yaml`
or `org_config.json` keeps exactly what it set; only one that never expressed an opinion moves.

**`step_up.require_passkey` (Phase 3) is what makes it a guarantee rather than an opt-in check.**
With it on: an approving decision with nothing enrolled is hard-failed (`403`, naming `/security`)
rather than let through; the same is true for a sensitive subset of the local settings actions --
the rule-row, grant, policy and PII actions in `web/routes_settings.py`'s own `_SENSITIVE_ACTIONS`,
plus organization config bundle uploads (`org_config_upload` has its own route rather than an
`_ALLOWED_ACTIONS` entry, but is gated the same way -- an uploaded bundle can rewrite the PII
policy, every auto-accept rule and every connector's OAuth client config in one shot, at least as
much "what gets gated" as any single `_SENSITIVE_ACTIONS` entry; see
`_BESPOKE_SENSITIVE_ROUTE_PATHS` in that module)
-- so an agent that cannot forge an approval cannot route around the gate by adding an always-allow
rule or a broader grant either, since that action itself now demands the same fresh assertion; and
the credential store's own two directions are gated regardless of this flag, so a session alone can
neither un-enroll its way back to the unguarded state (removing your last credential needs an
assertion with it) nor enroll its way past it (adding one needs an assertion with a credential
already on file, or — for the first — the companion's own confirmation; see
[Enrolling a passkey is itself gated](#enrolling-a-passkey-is-itself-gated)).
If nothing is enrolled when the daemon
starts with `require_passkey` on, it still starts (refusing to boot would remove the one path,
`/security`, that fixes the misconfiguration) but shows a persistent banner on every page until a
passkey is added, and releases nothing in the meantime. Treat local-mode step-up as a real guarantee
once `require_passkey` is on and a passkey is enrolled; with `enabled` alone it stays what it always
was -- opt-in, evadable by simply not enrolling.

**On a packaged install, `enabled` and `require_passkey` are both on by default** as of 4.2 (the
DMG/`.pkg`, the Windows installer, the `.deb` -- `paths.is_bundled()`, which is the same predicate
[ADR 0003](adr/0003-separated-installs-only.md)'s `enforce_separation()` is scoped to, so the
builds that default on are exactly the builds that are privilege-separated or refuse to serve).
A source checkout, an editable install and `pipx install privacyfence` all still default off:
nothing separates those, and a passkey checked against a credential store the agent can write is a
checkbox a local process ticks for itself ([ADR
0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) decision 6). An explicit value in
`config/settings.yaml` wins either way, in both directions, and an install seeded from a
`settings.yaml.example` older than 4.2 has both keys written out as `false` -- so this reaches
fresh installs rather than silently changing the posture of existing ones on upgrade.

A fresh packaged install therefore comes up requiring a passkey it does not have yet, which is the
banner-and-release-nothing state above. It is meant to last minutes: the companion app checks at
its own next start, and opens `/security` with a session already minted so the human can enroll
one. Nothing ships a configuration where `require_passkey` is on with nothing enrolled *and*
nothing is trying to fix it.

**Step-up asks whether this is the enrolled human; session provenance asks whether a human asked at
all**, and the second question is asked first, without waiting on any `step_up` setting — see
[A session is not a human](#a-session-is-not-a-human) directly below.

### A session is not a human

ADR 0002 decision 6 names three ways a local process reaches a `pf_session`, all of them by design:
the companion's own **Open Approvals** item, a bootstrap link a human was handed, and a bare `MINT`
on the control channel — which privilege separation *widens* from `0600` to `0660` so the companion
can reach it, putting the agent in the same group. The decision that made this acceptable was that
a minted session would be **insufficient** rather than uncallable, with the passkey supplying what
it lacked.

Nothing downstream recorded *which* of the three a session came from, so the three were
interchangeable: a session the human obtained by clicking Open Approvals and one the agent obtained
by minting its own were the same object with the same authority.

**Every local-mode session now carries a provenance** (`web/session_auth.py`):

| provenance | minted by | may view | may approve |
| --- | --- | --- | --- |
| `human` | the companion's own menu (`MINT COMPANION <nonce>`, confirmed by a call-back to the process the human clicked), or `privacyfence-app --print-sign-in-link` (`MINT CONSOLE`, confirmed by the companion's own dialog) | yes | yes |
| `unattested` | a bare `MINT` on the control channel — anything running as this OS user | yes | no |

`human` is required to release an *approving* decision (`accept`/`accept_all`, individually or in a
batch) and to take any `_SENSITIVE_ACTIONS` settings action, or upload an organization config
bundle (`org_config_upload`, gated the same way for the same reason -- see immediately above), or
confirm an auto-accept rule an MCP client asked for (see
[A confirmation dialog is not always a second step](#a-confirmation-dialog-is-not-always-a-second-step)).
Denying is not gated, under the same
reasoning step-up uses: denying discloses nothing. Viewing is not gated at all — a locked-out human
with only an unattested link can still see what is pending, and is told in so many words that this
link cannot approve it.

**This is enforced on privilege-separated installs only**, which since
[ADR 0003](adr/0003-separated-installs-only.md) is every packaged install (decision 6: an
unseparated packaged daemon refuses to serve) and which guarantees the companion is installed and
running (decisions 3–5). On a non-packaged source checkout run with
`PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1` there is no companion to mint through and no `authority/`
boundary either — an agent there rewrites the credential store directly, which is the case ADR 0002
decision 6 names — so the check is not applied and would buy nothing if it were. Same line
`StepUpConfig.from_local_config()` already draws when it refuses `require_passkey` on such an
install.

**Every mint is audited, and the recent ones are shown.** Each bootstrap code this daemon issues
— and each attested one it refuses — is recorded under its own `sign_in_code_minted` decision
(`audit_log.py`'s own vocabulary comment), naming which of the paths above asked and whether the
resulting session can approve. Before this, the only path that wrote an entry was the MCP
sign-in-link tool, i.e. the sanctioned one, while the two a local process could use silently wrote
nothing. `/security` lists the most recent of these under **Recent sign-ins**, because an audit
entry nobody reads is evidence after the fact: the point is that a link you did not ask for is
visible on the page you already visit to reason about what can approve here.

**A code in a browser's address bar is still a code on this machine.** The companion signs a human
in the only way a local web UI can: by opening `…?bootstrap=<code>` in their browser. On a
shared-uid machine a sibling process can read that URL out of the browser's own command line
(`/proc/<pid>/cmdline` on Linux, the equivalent elsewhere) for as long as it takes the browser to
redeem it, and the code is single-use, so whoever redeems first wins. What provenance changes is
that this is now a *race against a human click* rather than a file sitting in a group-shared
directory being refreshed on every restart — an attested session can no longer be obtained at a
time of the agent's choosing. It is not a boundary, and nothing about a credential passed through a
URL can be made into one; closing it properly means the companion handing the daemon a session the
browser never carries, which is not what any of this ships today.

The same reasoning is why the companion's `SHOW` command — the one an applications-menu click on
Linux uses to have the running companion do the opening — puts the confirmation dialog in front of
a human before it mints anything. That line arrives from another process running as the same OS
user, and the agent is indistinguishable from the menu click it exists for; without the dialog, any
local process could make the companion mint an approving session and open it in a browser at any
moment, with the above race for the code and no human anywhere in it.

**What this is not: authentication of the companion.** Companion and agent share an OS user, and
the companion's own channel lives under `handoff_dir()`, which `paths.py` states outright is
"deliberately *not* a security boundary" — so a local process running as that user can bind that
address before the companion does and answer the daemon's call-back itself. This is the identical
limit the [first-enrollment gate](#enrolling-a-passkey-is-itself-gated) accepts, for the identical
reason, and it is bounded the same way: on a separated install the companion is autostarted at
login, so winning that race means starting before it and staying there, which breaks the connector
OAuth flows that share the address and leaves the human with no companion where one is meant to be.
What provenance buys is that the two silent paths stop being interchangeable with the attended one,
and that forging the attended one costs impersonating a process whose absence is visible.

### A confirmation dialog is not always a second step

PrivacyFence raises a small Cancel/Confirm dialog in three places, and until 4.2 the decide endpoint
treated all three alike: exempt from both the passkey check and the provenance check, because those
are scoped to a decision *result* named `accept`/`accept_all`. The exemption had a reason, recorded
in `webauthn_stepup.is_step_up_required` -- "a confirm is a second step *inside* a decision the
caller's own card already gated, never a release of its own". That is true of the PII dialog and of
the one an **Always allow** click raises: both appear only after an approval card has already been
answered, and that card took both checks. Asking again would be a second passkey tap for one
decision.

It is not true of the third. `privacyfence_propose_policy_change` lets an MCP client ask for an
auto-accept rule directly. No card is shown, because nothing is being approved yet -- the dialog
*is* the gate, and
what it writes is a rule that decides what gets approved without asking from then on. That is the
same kind of change `_SENSITIVE_ACTIONS` names on the Settings page, reached by a different route
and, until this was closed, without either of that route's two checks.

Those dialogs are now marked at the point they are raised (`sensitive=True` through
`approvals.PendingApprovalRegistry.register_confirm`), and confirming one takes exactly what the
equivalent settings action takes:

- **an attributable session** -- `human` provenance, on the installs where provenance is enforced
  (see above). Org mode has no provenance to check: every session reaching that surface is an IdP
  authentication.
- **a passkey, wherever `require_passkey` is on** -- in both deployment modes, and independently of
  `step_up.scope`. A rule is not a read or a write; it is the thing that decides which of those you
  are asked about at all, which is why `web/routes_settings.py`'s own `_needs_step_up` never
  consults `scope` either.

**Cancelling is not gated**, for the same reason denying is not.

### Enrolling a passkey is itself gated

Both directions of the credential store are gated, and for one reason. Removing your *last* enrolled
credential requires a fresh assertion with it, because a session that could un-enroll on its own would
silently turn a "mandatory" install back into an unenforced one. **Adding** a credential has exactly
the same effect by the shorter route — a session that can enroll a credential it generated itself can
then satisfy every step-up check with it — so adding is gated too.

Checking harder at verification time cannot substitute for this. Registration uses `none` attestation,
so there is no signed claim about the authenticator's make or model; the user-verified flag
`require_user_verification=True` checks is a bit the authenticator sets about *itself*, which a real
platform authenticator sets after a biometric or a PIN and a process that is not one sets to 1,
because nothing signs the absence of a human; and `authenticator_attachment=platform` and
`exclude_credentials` are enforced by a cooperating browser and by nothing else. See
`webauthn_stepup.py`'s own "five things" list, which states each of these where a reader of that
module will find it.

So `web/routes_security.py`'s `register_options` gates the ceremony **before it starts**, in whichever
of two ways the credential store's own state allows:

- **A credential is already enrolled** — the gate is a fresh assertion with one of them, over a
  challenge bound to `enroll-credential|<principal>`, through the same `428`-then-retry round trip
  removing your last credential and releasing a gated write already use. A human who has a passkey
  needs one extra tap to add another; a session that has only a cookie gets a `428` it cannot answer.
  **Identical in both modes** — an org-mode IdP session gets no more latitude here than a local
  `pf_session`.
- **Nothing is enrolled yet** — there is nothing to assert with, so the gate is a confirmation from
  the [companion app](#privilege-separation-macos-linux-and-windows), which
  [ADR 0003](adr/0003-separated-installs-only.md) decisions 3–5 guarantee is installed and started on
  all three shipped platforms. The daemon asks over `web/control_channel.py`'s `CONFIRM ENROLL`, and
  the companion puts the system's own dialog in front of whoever is at the login session — the only
  PrivacyFence process that runs where a human can be asked at all. A refusal for any reason (denied,
  nobody answered, no companion running) is a refusal: failing this gate closed costs an enrollment,
  failing it open costs the guarantee.

`register_verify` does not re-run either gate — that would mean two prompts for one enrollment — but
it does refuse to complete a registration challenge that was not issued by an `options` call which
passed one, so a code path that skips the gate fails closed rather than quietly reopening the hole.

Refusals are audited as `webauthn_enrollment_refused`; a `428` asking for the assertion is not, since
it is an ordinary round trip in every legitimate second enrollment. A successful *first* enrollment
says so in its own summary ("First passkey enrolled: …"), because it is both the one no
already-enrolled credential could have gated and the one that decides what every later step-up check
is satisfied by — the entry to look for by eye.

**What the companion confirmation is, and is not.** Be exact about this, because the temptation is to
describe it as authentication and it is not: the companion and the agent run as the *same OS user*, so
no peer check, file permission or shared secret can distinguish them —
[ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) decision 6 says so outright, and
this gate does not repeal it. `handoff/` is group-shared with that user by design, so a determined
local process can bind the companion's own socket before (or instead of) the real companion and answer
`CONFIRM ENROLL` itself. What the gate buys is that **forging a first enrollment requires
impersonating a system component rather than calling an API**: the attempt is loud (it must take over
a socket the companion also wants, breaking the connector OAuth flows that share it), it is visible in
the audit trail either way, and it is not a side effect of merely holding a session — which is what
made the ungated version reachable by anything at all. Distinguishing a human's session from the
agent's is the change that would close it, and that is a different change from this one: the daemon
has to be able to tell which holder of a session is asking.

Limits, stated rather than implied:

- **Org mode's first enrollment is not gated**, because that mode has no companion. See
  [Org mode](#org-mode) below for what it rests on instead.
- **On Linux the confirmation needs `zenity` or `kdialog`.** Neither is a PrivacyFence dependency
  (ADR 0002 decision 4's dependency budget for the companion), so on a desktop with neither, a first
  enrollment is refused with a message naming them. Every PrivacyFence-supported Linux desktop ships
  one or the other; a stripped-down install may not.
- **A non-packaged build can bypass the first-enrollment gate** with
  `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1`, the same escape hatch ADR 0003 decision 7 gives the
  developer path and `step_up_config.py` honors for `require_passkey`. It is not consulted at all on a
  packaged build, which always has a companion.
- **A ceremony a human has already opened is theirs to lose.** The gate is on *starting* an
  enrollment, and the session is shared, so a local process watching for the moment a human answers
  the prompt can complete that one already-authorized ceremony with a credential of its own instead.
  It needs the human to be mid-enrollment to get anything, which makes it a narrow residual — but a
  residual, and closing it needs the same session-provenance change as the paragraph above.

### Tamper-evidence and recovery for local-mode step-up (#426 Phase 4)

Five events on the credential-store/requirement lifecycle are written to the audit log, each on its
own `decision` value (see `audit_log.py`'s own field docstring for the exact strings): enrolling a
passkey, removing one, an enrollment being *refused* by the gate above
(`webauthn_enrollment_refused`), a recovery code being spent, and the `step_up.require_passkey`
requirement itself turning on or off. None of these prevent anything on their own — see the framing in [What #426
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
currently have an unused one on file, most often their very first enrollment. Only a salted SHA-256
hash of it is stored (`webauthn_stepup.py`'s own `store_recovery_code`/`consume_recovery_code`),
alongside the credential file itself, under the same service-owned root a separated install
protects. Trading the
code in at `POST /security/recover` needs no WebAuthn ceremony — deliberately, since producing one is
exactly what a locked-out human cannot do — only the still-valid session that got them to `/security`
in the first place, which local mode's ordinary sign-in path (a bootstrap link) still provides even
with `require_passkey` on, since step-up gates *decisions*, not sign-in itself. On a
privilege-separated install that session must be one opened from the companion (the same
human-session check approving a decision makes); in org mode the IdP sign-in is that check. Attempts
are rate-limited — 5 per session and 20 across all sessions in any 15 minutes. A successful trade-in
removes every credential enrolled for that principal, clearing the stuck state so a fresh passkey can
be enrolled immediately afterward, and is itself audited (`webauthn_recovery_code_used`) whether or
not the human goes on to enroll again; every refused attempt is audited too
(`webauthn_recovery_refused`, with the reason but never the code). The code is single-use: spending it, correctly or not, never
grants a second attempt at the same code. A fresh one is issued at the next successful enrollment
that finds none on file — or, on a packaged install, whenever the companion is asked for one (see
below).

**Where the code is shown is not the same in both modes, as of 4.2.** In org mode it goes back to
the browser in `register_verify`'s own response, once, exactly as it always did: there is no
companion there, and the session that reached `/security` is an IdP authentication rather than a
locally minted cookie.

On a **packaged local-mode install** it never appears in that response at all. The daemon mints the
code, hands it to the companion over the companion's own channel (`SHOW RECOVERY`, see
`web/control_channel.py`), and the companion puts it in a dialog on the human's own desktop — and
only *then* does the daemon store it. The ordering is the point twice over: a credential-store
reset token stops being a value any local process holding a `pf_session` can read out of an HTTP
body, and a code nobody could be shown never becomes the one code on file (which would otherwise
leave the principal holding a recovery code that exists and cannot be produced, with no later
enrollment issuing another). If the companion cannot be reached, the passkey is still enrolled, no
code is issued, and `/security` says so with the companion's own reason.

Because nothing keeps the plaintext, **re-presenting a code means issuing a new one**, and the
companion is where that happens: "New Recovery Code…" on the macOS/Windows menu-bar item, or the
matching entry in the Linux applications menu. It asks the daemon, the daemon asks the human to
confirm through that same companion — issuing one invalidates whatever they wrote down before — and
then shows the new code. The reply on the daemon's own control channel carries no code in either
direction, which matters because that channel is `0660` group-shared with the logged-in user (and
therefore the agent) on a separated install: a local process that speaks it can, at most, put a
dialog on somebody's screen that they have to decline.

A non-packaged local-mode install (a source checkout, an editable install, `pipx install
privacyfence`) keeps the org-mode behavior — the code comes back in the response. Nothing
autostarts a companion for those ([ADR 0003](adr/0003-separated-installs-only.md) decisions 3–5 are
the packaged installers' half), so routing the code through one would mean never being able to
issue a recovery code there at all. Those installs also default `require_passkey` off, so there is
no recovery to be locked out of.

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
| Replaces | the login-session LaunchAgent | a pip/pipx install's `--user` unit | nothing: the installer registers no daemon task |

All three platforms ship `enable`/`uninstall [--purge]`/`status` ([ADR 0042](adr/0042-uninstall-replaces-disable.md);
Windows spells the flag `-Purge`): `uninstall` stops the service and keeps the data under the data
directory above, `--purge` deletes it and the service account (on Windows, the `PrivacyFenceUsers`
group; its virtual account goes with the service), and neither moves anything into a home
directory. On macOS, which has no package manager, `uninstall` is the uninstall: it also removes the
app the `.pkg` installed. On Windows the uninstaller runs it, adding `-Purge` only when its
**Delete PrivacyFence data** checkbox is ticked.
**Every packaged install on all three platforms now separates itself as part of installing**,
mandatorily rather than opt-in, per ADR 0003. The `.deb`'s `postinst` separates the install itself,
root already, on every install and every upgrade ([`debian/postinst`](../debian/postinst)). Since
ADR 0003 decision 5 the machine half of that — the system account, the data directory, the marker,
the systemd unit — is unconditional and carries no `|| true`, so an install that could not separate
itself fails the package install loudly rather than quietly becoming one that isn't; the per-user
half (the group membership) keeps its `$SUDO_USER` gate and keeps the right to defer, so an
unattended install with no session behind it still ends up separated, with that one re-runnable
step pending, which the companion closes the first time a real login session starts one (ADR 0003
decision 3). macOS's DMG carries a signed `.pkg` (`scripts/build_pkg.sh`) whose own
`installer/macos/pkg/postinstall` script runs `enable --auto` itself while the package install is
still running, the same shape as the `.deb`'s `postinst` — since [ADR 0003](adr/0003-separated-installs-only.md)
decision 2 the DMG holds nothing else to install from, so this `.pkg` step is the only way to get
macOS PrivacyFence onto a machine, not one path among several. An install that reached a running
state some other way (a copied app bundle, an in-place upgrade from before this ADR) has no
package-manager hook behind it, so the daemon's own startup asks, via the standard admin-password
dialog, the first time it finds itself unseparated (`privilege_separation.
maybe_auto_enable_macos()`) — asked again on every start it is declined, not once
(ADR 0003 decision 6 retired the one-shot marker #428 D1 wrote). `--auto` (used by the unattended
triggers, never by a human directly, and on Linux now only by the half that is allowed to defer) is
the same `enable`, made safe to run unattended: anywhere it can't safely tell who owns the install
or find the daemon's executables, it logs why and exits 0 rather than guessing or failing the step
it was called from. **Windows now separates too, as an installer step**: [ADR 0003](adr/0003-separated-installs-only.md)
decision 4 withdrew the non-elevated per-user install tier #407 added and ADR 0002 decision 5a
preserved, and `installer/privacyfence.iss` runs `privilege-separation.ps1 enable` itself with
Setup's own elevated token, right after the app is laid down — a failure of that step is an install
failure, not a silently-opt-in install.

**And a packaged build that ends up unseparated anyway does not serve.** ADR 0003 decision 6 is the
backstop for the installs the paragraph above doesn't cover — a pre-ADR-0003 install upgrading in
place, a restored backup, an install where `... uninstall --purge` was run and the program left
behind: on startup, a
packaged local-mode daemon attempts its platform's provisioning (the same mechanisms above, run
again) and, if it is still unseparated afterward, refuses outright — no `/mcp`, no approvals —
naming the one command that fixes it. Source checkouts and `pip`/`pipx` installs are not packaged
builds and are not gated this way (they are how org mode is deployed, and how the project is
developed); what they may not do instead is claim a guarantee they don't hold — see
`PRIVACYFENCE_DEV_ALLOW_UNSEPARATED` below.

**Windows expresses the same layout in a different primitive, and adds one requirement the others
do not have.** There are no permission bits there, so the modes below are NTFS ACLs
(`icacls`), applied at enable time and re-checked on every daemon start. Two consequences are worth
stating rather than leaving to be discovered:

- **A service runs whatever its `binPath` names**, so the install location is part of the boundary.
  A non-elevated, per-user install under your own profile would let a process running as you
  rewrite the daemon's own executable and have the service run it *as the service account*, which
  is worse than not separating at all — so `enable` refuses against a user-writable install and
  says why. [ADR 0003](adr/0003-separated-installs-only.md) decision 4 settled the open question
  [ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) carried by withdrawing that
  tier outright: the non-elevated per-user path [#407](https://github.com/privacyfence/privacyfence/issues/407)
  added is retired, `installer/privacyfence.iss` has required `PrivilegesRequired=admin` since
  [#410](https://github.com/privacyfence/privacyfence/issues/410) (originally for an unrelated
  reason — a non-elevated install could never register its own autostart task), and there is now
  one Windows install tier: an elevated, per-machine install under `%ProgramFiles%`, which the
  installer separates as part of installing.
- **The companion is mandatory, not a convenience.** A Windows service runs in session 0 and cannot
  reach your desktop, so without the tray app there is no way for connector OAuth
  (Slack/Salesforce/Atlassian) to open a sign-in page at all — the case ADR 0002 decision 5 was
  written for, arriving where it was predicted. `enable` refuses to install the daemon half alone.

One thing has no POSIX counterpart at all: **ownership is part of the boundary**. An object's owner
on Windows can rewrite its ACL regardless of what that ACL says, and any user may create a
directory under `%ProgramData%` — so `enable` takes ownership of the data directory (to
`Administrators`) rather than trusting whoever created it — otherwise every permission above would
be advisory against the one account it is meant to exclude.

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
`step_up.require_passkey` are both set, and a passkey is enrolled. **On a packaged install as of
4.2, all four are the default**: separation is mandatory or the daemon refuses to serve ([ADR
0003](adr/0003-separated-installs-only.md)), both flags default on, and the companion walks the
human through enrolling at its next start. Anywhere else — a source checkout, an editable install,
`pipx install privacyfence` — none of the four is a default.

**[Session provenance](#a-session-is-not-a-human) is what holds where they are not.** It asks a
different question than the passkey does, and does not wait on any `step_up` setting to ask it: a
session the agent minted for itself is `unattested` and cannot approve, enrolled passkey or not;
one minted through the companion is `human` and can. That is a *weaker* statement than the
passkey's — it rests on the companion being the process a human is in front of, not on a
cryptographic proof, and that section says exactly where the line is — but it costs nobody a
setting they have to find first. The two stack rather than substitute: a packaged install has both,
and an install with none of the four still cannot have an approval released by a session the agent
minted for itself. The consequence, stated rather than left to be
discovered, for an install that has all four: **integrity is the strong guarantee — the agent
cannot approve its own request — while confidentiality of the review screen is the weaker one**,
since an agent that reaches the web UI can still read what is pending. The control channel's socket accordingly lives in
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

**The developer path.** A non-packaged install (a source checkout, `pip`/`pipx install
privacyfence`) is not a packaged build and is not gated by the refusal above — it is how org mode is
deployed and how the project is developed, and [ADR 0003](adr/0003-separated-installs-only.md)
decision 7 leaves both untouched. What it may not do is claim protection it does not have:
`step_up_config.py`'s `from_local_config()` refuses `step_up.require_passkey: true` on a
non-packaged, unseparated local-mode install — the same "worse than not having the feature"
reasoning [ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md) decision 6 gives for a
passkey checked against a credential store the agent can rewrite — unless
`PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1` is set, the established house spelling (a sibling of
`PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP`). Set it only for local development against an unseparated
checkout, never for a real deployment.

## Authentication boundaries

### Local web UI

The local browser UI is not authenticated by a reusable token in the URL. The daemon uses a one-time bootstrap exchange to establish an HttpOnly session cookie. Browser requests are then authenticated from that session.

Mutating requests require the authenticated session, same-origin checks, and CSRF validation. Session/bootstrap secrets are not intended for logging or propagation into connector data.

The ordinary way a human reaches that exchange is the companion app's own **Open Approvals**/**Open Settings** items. `privacyfence-app --print-sign-in-link` is the break-glass alternative, for a session the companion's menu is not reachable from (an SSH login, a desktop whose applications menu nobody has open, a tray icon that failed to start): it prints one link, to stdout alone so it can be piped, and asks the companion to confirm with the human at the login session first — the command runs as the same OS user the agent does, so what makes the resulting session `human` (see [A session is not a human](#a-session-is-not-a-human)) is that a person clicked Allow, not that the request came from a terminal. If nothing confirms it, it prints an `unattested` link instead and says so: enough to see what is pending, not to release it.

### Sign-in links are no longer issued over MCP

`privacyfence_get_sign_in_link` was a meta-tool, available over `/mcp` like every connector tool,
that minted a fresh bootstrap link for the local web UI and returned it to the calling MCP client.
**It is retired** (the self-approval plan's Phase 2), and this section records why rather than
deleting the reasoning along with it.

It was never gated on a human approval, deliberately: the approval that would have required lives
behind the very UI a locked-out user is trying to reach, so gating it on that UI would have been
circular. What bounded it instead was local mode only, a single-use short-lived code, an allowlisted
`page`, a loopback-bound UI, and a `sign_in_link_issued` audit entry carrying the caller's
self-reported reason. Its net effect, which this document stated plainly, was that **an MCP client
could obtain a working session for the human-facing approval surface without a human approving that
specific request** — a session that can *release* a gated call rather than merely request one.

Two things changed that turned "documented cost" into "no longer worth paying":

- **Its justification expired.** The tool existed because a locked-out human had no other way in:
  the daemon is headless (P10 removed the menu bar) and the companion app was optional, "nothing
  installs or starts it automatically yet". [ADR 0003](adr/0003-separated-installs-only.md) makes
  the companion mandatory and autostarted on all three platforms (decisions 3–5), so that sentence
  is no longer true anywhere PrivacyFence ships.
- **A session stopped being one thing.** With [session provenance](#a-session-is-not-a-human), the
  link this tool minted would be `unattested` and could not approve anything — so the tool would be
  handing the agent a credential that no longer does what the tool's own description promised, while
  still granting read access to the review screen.

What replaces it: the companion's own **Open Approvals**/**Open Settings** items, and — for a human
whose companion menu is out of reach — `privacyfence-app --print-sign-in-link`, run by that human in
their own terminal (see [Local web UI](#local-web-ui)). `privacyfence_status` still tells a model
that an install is un-onboarded; its `next_step` is now `open_privacyfence_companion`, and it has no
link to hand over.

**What this does not close**, and it is the same sentence as before: a process running as the user
mints the same bootstrap code through the control channel without any tool's help — see
[Local-mode trust boundary](#local-mode-trust-boundary). Retiring the tool removes the *audited,
sanctioned* path, not the underlying reachability; what makes the remaining paths insufficient is
provenance and the passkey — which, on a packaged install as of 4.2, is on by default rather than
waiting to be turned on. Removing the tool does mean the one path that was
audited is gone — so every mint is audited now, whichever channel asked for it; see
[A session is not a human](#a-session-is-not-a-human).

### Local MCP

The local `/mcp` endpoint uses the generated bearer token stored in the user's PrivacyFence state so local MCP clients/shims can authenticate independently from the browser session.

### Org mode

Org mode authenticates human users through the configured OIDC provider and applies PrivacyFence's org authorization/session model to MCP and web traffic. Principal identity is carried explicitly through request handling and user-scoped storage/connector resolution.

Where configured, WebAuthn step-up is used for sensitive org-mode approval actions. Credential enrollment and lookup are scoped to the authenticated principal. By default, step-up accepts either a passkey assertion or a fresh IdP re-authentication; `step_up.require_passkey` ([#406](https://github.com/privacyfence/privacyfence/issues/406)) closes the IdP-reauth path for organizations that want hardware-bound WebAuthn as a hard requirement, and a principal with no enrolled passkey is hard-failed toward enrollment rather than silently allowed through the weaker path.

**What that flag buys against a stolen IdP session depends on whether the principal has enrolled yet**, and it is worth being precise rather than claiming a phished session can never satisfy step-up:

- **Once a passkey is enrolled**, a session alone satisfies nothing: an approving decision needs an assertion from an enrolled credential, and a session can neither produce one nor add a credential to assert with — adding one demands an assertion from a credential already on file, exactly as removing the last one does (see [Enrolling a passkey is itself gated](#enrolling-a-passkey-is-itself-gated); org mode is gated identically).
- **Before the principal's first enrollment**, the session is enough. A session that reaches `/security` with nothing enrolled can enroll, and a WebAuthn registration this product accepts carries no proof that a human or a genuine authenticator produced it (`none` attestation; the user-verified bit is a claim the authenticator makes about itself — see `webauthn_stepup.py`'s own "five things" list). Local mode gates that with a companion-issued confirmation; org mode has no companion, and the session that got there is at least an external authentication against the IdP rather than a locally minted cookie, so **the first enrollment rests on the IdP session** and `require_passkey` inherits whatever that session is worth.

The operational consequence, for an organization that wants the stronger reading: get every principal enrolled before treating `require_passkey` as a barrier against a stolen session, and treat an unexpected `webauthn_credential_enrolled` audit entry for a principal who had nothing on file as the event it is. Both `webauthn_credential_enrolled` and `webauthn_enrollment_refused` are audited in org mode on the same routes as in local mode.

## Authorization and principal isolation

Org mode supports multiple principals and maintains user-scoped state under principal-aware paths.

Local mode had exactly one principal through ADR 0007. [ADR 0008](adr/0008-one-principal-per-os-user.md)
gives a privilege-separated install one principal per OS account instead: the account this install
is provisioned for (its `owner_user`) keeps `LOCAL_PRINCIPAL`'s existing identity and data, and any
other OS account added to the service group gets its own `os-<uid>`/`os-<sid>` principal — its own
`/mcp` token (kernel-verified from the local control channel's own peer credentials, never a shared
file), its own `/approvals` and `/security`, its own audit log, and its own per-user companion-
channel address. An unseparated install (a dev checkout, or a pip/pipx install with privilege
separation never turned on) still has exactly one principal, unchanged. See that ADR's own "What
this phase deliberately does not do" for what a second principal cannot yet do (connect their own
services; use a personal `/settings` page).

`ConnectorRegistry` creates/caches connector hosts per principal. Service authorization callbacks evict the affected principal's cached connector host so subsequent calls use the updated credentials.

Org approval routes filter/authorize by principal; local mode's own `/approvals` and `/api/state/stream`
now do the same, once more than one principal can exist.

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

## Local file bridge

A privilege-separated local-mode install's daemon runs as its own OS account, with no standing access to the signed-in user's files — the same boundary [Local-mode trust boundary](#local-mode-trust-boundary) and [privilege separation](#privilege-separation-macos-linux-and-windows) describe for everything else. A tool that reads or writes a path the agent named (`drive_upload_file`'s `local_path`, `drive_download_file`'s `destination_dir`, and the equivalent Gmail/Confluence parameters) crosses that boundary through the `.mcpb` shim instead: the shim, which runs as the signed-in user, does the actual filesystem access on the daemon's behalf, over the same bearer-token-authenticated HTTP connection every tool call already uses.

This adds no capability the agent did not already have — the agent and the shim run as the same OS user, so anything the shim reads or writes on the daemon's behalf was always something that process could have reached directly. What it does add is a guard: the shim will only act on a path that appears, verbatim, somewhere in the arguments of the exact tool call it's currently servicing, so a compromised or buggy daemon cannot use the shim as a general file oracle for paths the agent never named. Every upload still passes the ordinary preview/PII-scan/approval gate before its bytes are ever used, and every download still reaches disk only after approval — the bridge changes *how* bytes cross the process boundary, not when a human is asked to approve anything.

Bytes in transit are staged the same way org-mode download staging is (above): per-principal, encrypted at rest, single-use, TTL-bound, with an identical no-oracle 404 for a missing/expired/wrong-principal/already-used token. See [ADR 0007](adr/0007-local-file-bridge.md) for the full wire protocol and the rejected alternatives (a shared transfer directory, granting the service account filesystem ACLs, doing file I/O in the companion) this design was chosen over.

An unseparated install (a dev checkout, or a pip/pipx install that never enabled privilege separation) is unaffected: the daemon is the same OS account as the user there, so every local-mode tool keeps reading and writing local paths directly, exactly as it did before ADR 0007.

## Audit integrity and forwarding

PrivacyFence records gate/approval activity in its audit log, including principal information in org mode. Every entry is unconditionally chained to the one before it with a keyed hash (HMAC-SHA256) — this isn't an opt-in feature; `AuditLogger` computes it for every install, and `verify_chain()` (or `scripts/verify_audit_log.py`) detects a line inserted, edited, or removed after the fact.

The chain's signing key lives next to the `.jsonl` files it protects, at the same file permissions. That defends against accidental corruption and against a party who gains write access to the log files specifically (e.g. a bug in some other export/backup path) without also reading the key — it does **not** defend against a party who already has full read/write access to the audit directory, since that party can read the key alongside the log and recompute a consistent chain over a tampered file. The real defense against that threat is a copy that leaves this trust boundary entirely — see the forwarding paragraph below. In local mode that party includes any process running as the signed-in user (see [Local-mode trust boundary](#local-mode-trust-boundary)), so forwarding carries more of the weight there than the file permissions do — unless the install has separated privileges (mandatory on a fresh packaged install as of [ADR 0003](adr/0003-separated-installs-only.md); [privilege separation](#privilege-separation-macos-linux-and-windows)), which moves the audit directory and its key onto an account that user does not hold, and is exactly the change that lets the file permissions carry their own weight again.

Org deployments can use the implemented forwarding/export path for external retention/monitoring. Forwarding does not replace local operational decisions about retention, backup, and access control.

Treat audit data as sensitive: it can reveal which services/tools/resources were used even when protected content itself was not released.

### Which AI system the audit log names, and how far to believe it

Each gated connector call records which AI system made it, and how that was learned (`agent_source`; the fields are in [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#which-ai-system-made-the-request)). The approval card, the approval list and the Audit Log page show the same thing in three forms: **Verified**, **Not verified** ("Says it is ChatGPT"), or **Unrecognised AI system** with the name the client sent.

**Local mode.** Every AI system one OS user runs holds that user's same MCP token ([ADR 0008](adr/0008-one-principal-per-os-user.md)), so nothing the token proves can tell them apart. The name an AI system gives is what it says about itself, and PrivacyFence records it as that: *Not verified*. Local mode has no verified source at all. An `agent_overrides:` entry in `settings.yaml` maps a name an AI system gives to the one it is, but only relabels it: the call stays *Not verified* on every install, privilege-separated or not. The file may be out of the AI system's reach, but the mapping is selected by the name the caller sends, and any process holding the shared token can send a mapped name ([ADR 0037](adr/0037-a-local-override-is-a-relabel-and-never-attests.md)). A verified local identity needs a credential per AI system, which does not exist yet.

**Organization mode.** An OAuth client's registered name is chosen by whatever registered it, so on its own it is *Not verified*. An administrator can pin a registration to an AI system on the admin-only *AI systems* settings page; pinning and unpinning need a passkey when `step_up.require_passkey` is on ([ADR 0034](adr/0034-sensitive-settings-writes-require-step-up-in-both-modes.md)), and every pin and unpin is audited. Only a call whose access token belongs to a pinned registration is *Verified* — the only verified identity PrivacyFence records in either mode. A pin names one registration: it never moves to another client, including one that registers again under the same name, and it stops applying once the registration expires ([ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md) decision 3).

**In both modes, an unverified name never changes an outcome.** It does not select a rule, auto-accept a call or release data; it is a reporting field. A verified one does not change an outcome today either — the attested tier is only the one a future rule would be allowed to key on ([ADR 0006](adr/0006-attributing-a-request-to-the-ai-system-that-made-it.md) decision 3). The vendor's logo appears only beside a verified identity, and the card's own wording names the AI system only when it is verified; otherwise it says "the AI system" ([ADR 0036](adr/0036-card-copy-names-the-caller-through-one-placeholder.md)).

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
