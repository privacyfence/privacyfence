# Security and compliance

For operators and security reviewers: what PrivacyFence protects, where each protection stops, and
how the pieces are laid out on disk. This is a technical control reference, not a certification
statement. The reasons behind each decision are in the linked ADRs.

## Deployment modes

| | Local mode (default) | Organization mode |
| --- | --- | --- |
| Where it runs | One person's own computer | A server the organization runs |
| Who signs in | Nobody: one implicit user per OS account | Each person, through the organization's OIDC identity provider |
| How AI clients connect | `/mcp` on `127.0.0.1`, plain HTTP ([ADR 0010](adr/0010-local-mode-serves-plain-http-on-localhost.md)) | `/mcp` behind the organization's HTTPS reverse proxy, with OAuth 2.1 ([ADR 0011](adr/0011-org-mode-runs-its-own-oauth-authorization-server.md)) |
| Privilege separation | Always, on a packaged install (see [Privilege separation](#privilege-separation)) | Not used: the AI client has no access to the server |

Claude Desktop and Claude Code work with either mode. claude.ai can reach PrivacyFence only through
an organization deployment, because local mode listens only on localhost.

There is no PrivacyFence-operated infrastructure in either mode. No hosted service sits in the
data path: every tool call goes from the machine or server running PrivacyFence straight to the
provider (Google, Slack, Salesforce, Atlassian, Telegram).

A **packaged install** is the macOS `.pkg` (on the DMG), the Windows installer, or the `.deb`. A
source checkout or a `pip`/`pipx install privacyfence` is not packaged. It is never
privilege-separated, and it is how organization mode is deployed.

## Trust boundary

The daemon, its policy, the approval UI, connector credentials, the organization configuration and
the audit log are the trusted computing base. Invalid security configuration stops the daemon
rather than weakening it.

### Local-mode trust boundary

On a packaged install the daemon runs under its own service account. The files that decide what
the AI client may do are out of reach of your own OS account, and so of any AI client running as
you (see [Privilege separation](#privilege-separation)). What stays reachable from your account, by
design, is starting a web session. The companion app and an AI client run as the same OS user, and
no permission bit can tell them apart ([ADR 0002](adr/0002-local-mode-trust-boundary-and-companion-app.md),
decision 6). So a session is made *insufficient* rather than unreachable. A session the AI client
starts for itself cannot approve anything ([Human and unattested sessions](#human-and-unattested-sessions)),
and releasing a gated call needs a passkey ([Passkey step-up](#passkey-step-up)).

The result on a packaged install:

- **Integrity is the strong guarantee.** An AI client with shell access cannot approve its own
  request, add an always-allow rule, change the PII policy, enroll its own passkey, or rewrite the
  audit log without it showing.
- **Confidentiality of the review screen is weaker.** A process running as you can open the
  approvals page and read what is pending. It cannot release it.
- **Root or a local Administrator is out of scope.** `sudo` re-owns any file and reconfigures any
  service. Separation makes escalation need an authentication prompt you see; it does not stop it.

On a non-packaged install (no separation) the AI client runs as the same account as the daemon. It
can read and write every file above, so the approval gate is a workflow control with an audit
trail, not a boundary against local code. It still defends against an AI client acting only
through `/mcp`, against mistakes, against remote attackers without code execution, and against a
hostile web page in your browser.

### Organization mode

The daemon runs on a server the AI client cannot reach except through `/mcp`. People sign in with
the organization's identity provider. A browser session and an MCP client's OAuth token from the
same sign-in resolve to the same person. Each person's connectors, approvals, staged downloads,
passkeys and audit log are kept apart by principal, and one person never receives another's.

## Human and unattested sessions

Every local-mode web session records how it was started:

| Session | Started by | Can view | Can approve |
| --- | --- | --- | --- |
| `human` | The companion's **Open Approvals** or **Open Settings** (the daemon calls the companion back to confirm the request came from it); clicking PrivacyFence itself (app icon, Start Menu, applications menu), after the companion's confirmation dialog; `privacyfence-app --print-sign-in-link`, after the companion's confirmation dialog | Yes | Yes |
| `unattested` | Anything else running as your OS user that asks the control channel for a sign-in code; `--print-sign-in-link` when no companion confirms it | Yes | No |

A `human` session is needed to approve a call (singly or in a batch), to confirm a rule the AI
client proposed, to use a recovery code, to upload an organization config bundle, and for every
sensitive settings change listed under [Passkey step-up](#passkey-step-up). Denying and cancelling
need nothing. An unattested session shows what is pending and says it cannot approve.

This is enforced on privilege-separated installs, where a companion is guaranteed to be installed
and running. No MCP tool can obtain a sign-in link
([ADR 0013](adr/0013-no-mcp-tool-mints-a-sign-in-credential.md)).

Every sign-in code the daemon issues, and every attested one it refuses, is written to the audit
log as `sign_in_code_minted`. The code's source and whether its session can approve are included.
The most recent ones are listed under **Recent sign-ins** on the `/security` page. If one appears
that you did not ask for, treat the install as compromised.

Sign-in codes are single-use and expire after 10 minutes. A session expires after 30 minutes idle
or 24 hours after it started, whichever comes first.

Limits: the companion signs you in by opening a URL carrying the code in your browser. A process
running as you can read that URL from the browser's command line and race the browser to use it.
The companion's own socket is also reachable by your OS user, so a process that binds it before the
companion starts can impersonate it. Both are visible (a missing companion breaks connector
sign-in), and neither can be done at a time of the AI client's choosing without you.

## Passkey step-up

Step-up asks for a fresh WebAuthn passkey assertion (Touch ID, Windows Hello, a security key, or
a phone over the browser's QR-code flow) before a decision is released. The `step_up` settings (in `config/settings.yaml` for local mode,
`org_config.json` for organization mode; see [Configuration reference](configuration-reference.md))
default to:

| Key | Packaged local install | Non-packaged local install | Organization mode |
| --- | --- | --- | --- |
| `enabled` | `true` | `false` | `false` |
| `require_passkey` | `true` | `false` (setting `true` is refused at startup) | `false` |
| `scope` | `writes_and_pii_reads` | `writes_and_pii_reads` | `writes_and_pii_reads` |
| `batch` | `single_assertion` | `single_assertion` | `single_assertion` |

An explicit value always wins. Organization mode sets these with `scripts/build_org_bundle.py`
(`--step-up-enabled`, `--step-up-require-passkey`, `--step-up-scope`).

`scope` decides which approving decisions need a passkey:

| `scope` | Write | Read flagged as personal data | Any other read |
| --- | --- | --- | --- |
| `writes` | Passkey | — | — |
| `writes_and_pii_reads` | Passkey | Passkey | — |
| `writes_and_reads` | Passkey | Passkey | Passkey |

A read an always-allow rule covers never becomes an approval, so no scope asks about it.

With `require_passkey` on, the following also need a passkey in **both modes**
([ADR 0034](adr/0034-sensitive-settings-writes-require-step-up-in-both-modes.md)), whatever `scope`
says:

- adding or removing an always-allow rule; changing the default or a category policy; turning PII
  detection or a PII category on or off; the calendar free/busy setting; re-enabling a connector
  that was switched off; turning step-up on;
- uploading an organization config bundle;
- in organization mode, pinning or unpinning an OAuth client on the **AI systems** page;
- confirming a rule an AI client proposed with `privacyfence_propose_policy_change` (see below).

Adding a passkey, and removing your last one, are gated whatever `require_passkey` says (see
[Enrolling a passkey](#enrolling-a-passkey)). Denying or cancelling never needs a passkey.

How the settings combine:

- **`require_passkey` on, nothing enrolled:** the daemon starts, every page shows a banner, and
  every approving decision `scope` covers and every sensitive change is refused (`403`, naming
  `/security`) until a passkey is added. On a packaged install the companion opens `/security` at
  its next start so you can enroll one.
- **`enabled` on, `require_passkey` off:** a passkey is asked for only if one is enrolled. With
  none enrolled, local mode lets decisions, batches included, go through. Organization mode accepts
  a fresh sign-in at the identity provider instead of a passkey for a single decision, and refuses
  a batch that needs step-up (`400`, nothing applied): approve each item from its card, or add a
  passkey ([ADR 0066](adr/0066-step-up-falls-back-by-mode-and-require-passkey-closes-the-fallback.md)).
- **Batch approval:** one assertion covers a selected set of approvals and is bound to that exact
  set and each item's result. It cannot be replayed for a larger, smaller or altered set.
  `batch: per_item` sends every item that needs step-up back to its own card. PII confirmations
  and choice dialogs are never batched.

Turning step-up on from the Settings page (the **Security** card) is immediate and audited
(`step_up_requirement_enabled`). There is no control that turns it off. That takes an edit to
`config/settings.yaml` and a restart. The change is audited (`step_up_requirement_disabled`), and a
banner stays on every page, with a warning in the daemon log, until it is turned back on.

### Passkeys for proposed rules

`privacyfence_propose_policy_change` lets an AI client ask for an always-allow rule. No approval
card is involved, so the confirmation dialog is the whole gate. Confirming it needs a `human`
session and, wherever `require_passkey` is on, a passkey, in both modes and regardless of `scope`.
The dialogs that follow an approval card you already answered (the PII confirmation, **Always
allow**) need neither again.

### Enrolling a passkey

Registration accepts any authenticator: built in, a roaming security key, or a phone. It requires
user verification (a PIN, fingerprint or face), and asks for no particular attachment, because
nothing in a passkey's signed data says how it is attached, so the server could not check it
([ADR 0055](adr/0055-step-up-passkey-enrollment-accepts-any-authenticator.md)).

Registration uses `none` attestation, so the server cannot prove a person or real authenticator
created a new passkey, and the user-verification flag is the authenticator's own claim. Enrollment
itself is therefore gated:

- **A passkey is already enrolled:** adding another needs an assertion with an existing one. Same
  in both modes.
- **Nothing enrolled yet (local mode):** the companion shows a system dialog asking you to confirm.
  No answer, a denial, or no companion running is a refusal. On Linux this dialog needs `zenity` or
  `kdialog`; with neither installed, the first enrollment is refused with a message naming them.
- **Nothing enrolled yet (organization mode):** there is no companion, so the first enrollment
  rests on the identity-provider sign-in. Enroll everyone before relying on `require_passkey`
  against a stolen session, and treat an unexpected `webauthn_credential_enrolled` entry as an
  incident.

Refused enrollments are audited as `webauthn_enrollment_refused`. A first enrollment's audit entry
says "First passkey enrolled".

## Recovery code

A recovery code lets you back in when every enrolled passkey is lost. It is 16 hexadecimal
characters in four groups (`A1B2-C3D4-E5F6-1789`). It is issued when you enroll a passkey and no
unused code is on file. Only a salted SHA-256 hash is stored, under `authority/`.

**Where it is shown.** On a packaged local install the companion shows it in a desktop dialog, and
the daemon stores it only after the dialog was shown. If the companion cannot be reached, the
passkey is still enrolled, no code is issued, and `/security` says why. In organization mode and on
a non-packaged install it is shown once in the browser.

**Getting a new one.** Nothing keeps the plain text, so a lost code is replaced, not shown again:
**New Recovery Code…** in the companion's menu (macOS menu bar, Windows tray), or the **New
PrivacyFence recovery code** action on the PrivacyFence entry in the Linux applications menu. The
companion asks you to confirm first, because a new code invalidates the old one.

**Using it.** On `/security`, choose **Use your recovery code**. The attempt:

- needs a `human` session on a privilege-separated install; an unattested one is refused;
- is rate-limited to 5 attempts per session and 20 across all sessions in any 15 minutes (the
  count resets when the daemon restarts);
- is audited, whatever the outcome: `webauthn_recovery_refused` with the reason (unattested
  session, too many attempts, wrong or used code) but never the code, or
  `webauthn_recovery_code_used` on success.

A wrong code consumes nothing: the stored code stays valid for the next attempt. A correct code is
marked used and removes every passkey enrolled for you, so you can enroll a new one straight away.

## Privilege separation

Every packaged install runs the daemon under a dedicated service account and keeps its data in a
system directory that account owns. The installer does this; a packaged daemon that finds itself
unseparated refuses to serve `/mcp` or approvals and names the command that fixes it
([ADR 0003](adr/0003-separated-installs-only.md)).

| | macOS | Linux (`.deb`) | Windows |
| --- | --- | --- | --- |
| Separation tool | `/Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh` | `/usr/sbin/privacyfence-privilege-separation` | `%ProgramFiles%\PrivacyFence\privilege-separation.ps1` |
| Service account | `_privacyfence` | `privacyfence` (system user) | `NT SERVICE\PrivacyFence` (virtual account) |
| Group your account joins | `_privacyfence` | `privacyfence` | `PrivacyFenceUsers` |
| System root | `/Library/Application Support/PrivacyFence` | `/var/lib/privacyfence` | `%ProgramData%\PrivacyFence` |
| Daemon runs as | LaunchDaemon `com.privacyfence.daemon` | systemd unit `privacyfence-daemon.service` | Windows service `PrivacyFence` |
| Companion runs as | LaunchAgent `com.privacyfence.companion` (menu-bar app) | `/etc/xdg/autostart/privacyfence-companion.desktop` (`privacyfence-companion --serve`, no tray) | Scheduled task `PrivacyFenceCompanion` (tray app) |
| Daemon program | Root-owned copy in `/Library/PrivacyFence/image` | `/opt/privacyfence` (owned by the package) | `%ProgramFiles%\PrivacyFence` (admin-only install) |

Each tool takes `enable [--for-user <name>]`, `status` and `uninstall [--purge]` (`-ForUser`,
`-Purge` on Windows). Run it with `sudo`, or from an elevated PowerShell with
`-ExecutionPolicy Bypass -File`. `uninstall` keeps the data; `--purge` also deletes the data, the
marker, and the service account and group
([ADR 0042](adr/0042-uninstall-replaces-disable.md)). `status` checks the modes and owners below,
your group membership (`PENDING USER` while nobody has been added to the group yet; a new
membership takes effect at your next login), and that the daemon and companion are registered. The daemon repeats the same checks at every start, and refuses to run as
the wrong account.

Layout under the system root (POSIX modes; Windows expresses the same with NTFS ACLs):

| Path | Holds | macOS / Linux | Windows |
| --- | --- | --- | --- |
| *(root)* | Everything below | service account, `0711`: anyone may traverse, nobody else may list | Owner `Administrators`; inheritance removed; service account, `SYSTEM`, `Administrators` full; `Users` traverse only |
| `privilege-separation.json` | The marker: platform, account names, recorded owner | `0644` | Adds `Users` read |
| `authority/` | `config/settings.yaml` (policy, rules, PII settings); `webauthn_credentials.json`; `webauthn_recovery_code.json`; `step_up_state.json`; `mcp_token`; `logs/audit/` (audit log and its `.audit_chain.key`) | service account, `0700` | Service account, `SYSTEM`, `Administrators` only |
| `credentials/` | Connector OAuth tokens and the Telegram session | service account, `0700` | As root |
| `org/`, `logs/`, `downloads/`, `uploads/`, caches | Organization config, daemon logs, staged files | service account, `0700` | As root |
| `users/os-<id>/` | Another OS account's own data, with its own `authority/` ([ADR 0008](adr/0008-one-principal-per-os-user.md)) | service account, `0700` | As root |
| `handoff/` | `mcp_url`, `web_base_url`; on macOS and Linux also `control.sock` and each account's `companion*.sock` | group, `3770` (setgid and sticky); files `0640`, control socket `0660` | Adds `PrivacyFenceUsers` read (both channels are named pipes, so nothing is created here) |

The recorded owner in the marker keeps the install's original data. Adding another account with
`enable --for-user` gives that account its own principal and never changes the owner
([ADR 0043](adr/0043-the-recorded-owner-is-never-rewritten.md)). One group member cannot take over
another's companion socket ([ADR 0027](adr/0027-a-group-member-cannot-take-over-another-members-companion-socket.md)).

What this takes away from a process running as you:

- editing `config/settings.yaml`: the always-allow rules, policy and PII settings;
- writing `webauthn_credentials.json` or the recovery-code file;
- reading the MCP token file or the audit log's key. An MCP client gets its token from the control
  channel, which identifies the calling OS account from the kernel; the `.mcpb` shim does this
  itself, and other clients use `privacyfence-app --print-mcp-token`;
- reading connector credentials;
- asking the companion to open an arbitrary URL. The companion accepts that only from the daemon's
  service account. From your own account it accepts only "show Approvals/Settings", which asks you
  first.

On a separated install, a tool that reads or writes a local file you named goes through the
`.mcpb` shim, which runs as you and acts only on a path that appears in that tool call's arguments
([ADR 0007](adr/0007-local-file-bridge.md)).

A non-packaged install is never separated. `PRIVACYFENCE_DEV_ALLOW_UNSEPARATED=1` exists for
development against a source checkout and must never be set in a real deployment.

## Audit log integrity

Each gate decision, approval and security event is written to a weekly JSON Lines file
(`YYYY-Www.jsonl`). Local mode's log is `authority/logs/audit/` under the data directory; other
principals have their own. Every entry is chained to the previous one with HMAC-SHA256, keyed by
`.audit_chain.key` in the same directory, on every install. Check a log from a source checkout with
its dependencies installed:

```
sudo python3 scripts/verify_audit_log.py "/var/lib/privacyfence/authority/logs/audit"
python3 scripts/verify_audit_log.py <dir> --week 2026-W38     # one ISO week
```

Exit status: `0` intact, `1` a week failed (or no files found), `2` directory missing.

The chain detects edits, insertions and deletions made without the key. Anyone who can read the
key can rebuild a consistent chain. On a separated install that means root or the service account.
On a non-packaged install it includes any process running as you. For evidence that survives that,
send a copy off the machine. Organization mode can forward every entry to syslog or an HTTP
endpoint (`audit_forwarding` in `org_config.json`); local mode has no forwarding.

Security events have their own `decision` values: `sign_in_code_minted`,
`webauthn_credential_enrolled`, `webauthn_credential_removed`, `webauthn_enrollment_refused`,
`webauthn_recovery_code_used`, `webauthn_recovery_refused`, `step_up_requirement_enabled` and
`step_up_requirement_disabled`. Treat the audit log as sensitive. It shows which services, tools and
resources were used even when no content was released.

## Which AI system the audit log names

Each gated call records the calling AI system in `agent_id`, `agent_name`, `agent_version` and
`agent_source`. The approval card, the approvals list and the Audit Log page show it as
**Verified**, **Not verified** ("Says it is …"), or **Unrecognised AI system** with the name sent.

| `agent_source` | Meaning | Shown as |
| --- | --- | --- |
| `oauth_client` | Organization mode: the call's access token belongs to an OAuth client an administrator pinned to an AI system on the **AI systems** page | Verified |
| `client_info` | The name the client sent (MCP `clientInfo`, or an organization-mode client's registered name), including one relabelled by a local `agent_overrides:` entry | Not verified, or Unrecognised if it matches no known AI system |
| `""` (empty) | No usable name | Unrecognised |

The only verified identity is an organization pin. A pin applies to one OAuth registration only:
it does not follow a client that registers again under the same name, and it lapses when the
registration expires. Every pin and unpin is audited
([ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)).
In local mode every AI client of one OS account holds the same MCP token, so nothing can tell them
apart. An `agent_overrides:` entry only relabels a name and never makes it verified
([ADR 0037](adr/0037-a-local-override-is-a-relabel-and-never-attests.md)).

No name, verified or not, changes an outcome: it selects no rule and releases nothing. The card
names the AI system and shows its logo only when it is verified; otherwise it says "the AI system"
([ADR 0036](adr/0036-card-copy-names-the-caller-through-one-placeholder.md)).

## What the AI sees before approval

Before a gated call is approved, the AI client has only what it already had: the tool schema, the
arguments it sent, and a status it can act on. PrivacyFence may fetch and scan provider data first
to build the card, detect personal data or check a rule. That data stays inside PrivacyFence.

- **Reads:** the result is held until the approval resolves. The card may show you a bounded
  preview. On deny or cancel the client gets nothing from it. Where policy requires redaction, the
  client gets the redacted version.
- **Writes:** approval decides whether the external change happens. Connector credentials and
  internal tokens never appear in a tool result.
- **Rules:** an always-allow rule changes only whether you are asked, inside its stored scope. It
  adds no tools and reveals no extra data.
- **Errors** returned over MCP carry no credentials, session or sign-in codes, secret paths, or
  unreleased content; details go to the daemon log.
- **Notifications** follow `notifications.detail`: `minimal` and `standard` (the default) carry no
  gated content; `detailed` adds the approval's summary line and can put it on a lock screen.

See [Approvals and policy](approvals-and-policy.md) for the approval list, cards and PII handling.

## Other controls

- **Web UI:** HttpOnly session cookie, CSRF token and same-origin checks on every change, a
  host allowlist, and a Content Security Policy with per-response nonces. Every custom route must be
  classified or the app refuses to start ([ADR 0014](adr/0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md)).
- **Organization OAuth server:** at most 2,000 registered clients, 8 KiB of metadata per
  registration, and unused registrations pruned after 180 days. Access tokens last 1 hour and
  refresh tokens at most 30 days.
- **Organization config:** validated at startup, which fails instead of weakening; the bundle's
  hash is audited and it can be signed ([ADR 0016](adr/0016-org-config-bundle-hash-log-and-signing.md)).
  Protect who can replace it.
- **Downloads in organization mode:** staged encrypted, served once through a short-lived link
  (default 300 seconds) ([ADR 0017](adr/0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md)).
- **Client secrets** belong to the deployment, not a person. Rotate a leaked one with the provider.
- **Supply chain:** each release build produces CycloneDX SBOMs, and installers are signed as
  described in [Platform support](platform-support.md).
- **Operations:** one daemon per data directory (a file lock enforces it); organization mode is not
  clustered. Back up and restore the data directory with its owners and permissions intact.

## What PrivacyFence does not claim

- **No certification, business-continuity plan or SLA.** There is no PrivacyFence-operated
  infrastructure to certify and no service whose outage affects you; installed copies keep running
  without the maintainer. Reports are handled best-effort. Treat this as a risk acceptance: approve
  PrivacyFence through an exception process, pin a reviewed release, and assign an internal owner to
  track releases ([ADR 0025](adr/0025-no-certified-security-framework.md)).
- **No protection against root or a local Administrator**, or against local code on a
  non-packaged install.
- **No confidentiality of the local review screen** against a process running as you.
- **No verified AI-system identity in local mode.**
- **No hard stop from the unattended-session flag.** It is advisory
  ([ADR 0015](adr/0015-unattended-session-flag-is-advisory-only.md)).
- **No compliance by itself.** Whether a deployment meets a regulatory or contractual requirement
  depends on its configuration, identity provider, retention and independent assessment.

To report a vulnerability, see [`SECURITY.md`](../SECURITY.md).
