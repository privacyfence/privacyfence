# Security and compliance

For operators and security reviewers: what PrivacyFence protects, where each protection stops, and
how the pieces are laid out on disk. This is a technical control reference, not a certification
statement. The reasons behind each decision are in the linked ADRs.

## Deployment modes

PrivacyFence runs in organization mode only. The Docker image runs in one of two shapes:

| | Organization server | Single-user mode (organization mode on localhost) |
| --- | --- | --- |
| Where it runs | A server the organization runs | One person's own computer, in Docker |
| Who signs in | Each person, through the organization's OIDC identity provider | One administrator: a setup code, then a passkey |
| How AI clients connect | `/mcp` behind the organization's HTTPS reverse proxy, with OAuth 2.1 ([ADR 0011](adr/0011-org-mode-runs-its-own-oauth-authorization-server.md)) | `/mcp` at `http://localhost`, with OAuth 2.1 and the person's consent |
| Separation from the AI client | The AI client has no access to the server except through `/mcp` | None beyond the container; see [Single-user mode](#single-user-mode) |
| Built-in OAuth apps | Yes, through the relay | Yes, through the relay |

Claude Desktop and Claude Code work with both. claude.ai can reach PrivacyFence only through an
organization server, because single-user mode listens only on localhost. A desktop install of
PrivacyFence 7 is not read by 8 ([moving to organization mode](moving-to-organization-mode.md)).

**Which PrivacyFence-operated services exist.** One, and only when a service uses a built-in app:
the **OAuth redirect relay** at `oauth.privacyfence.eu`, which forwards a provider's redirect to your
server. It is not in the path of any tool call: every tool call goes from the machine running
PrivacyFence straight to the provider (Google, Slack, Salesforce, Atlassian, Telegram, Grist or your
Peek server). An organization that uses its own apps for every provider needs the relay only for the
opt-in built-in Google sign-in. What it sees and what it can do are in
[Built-in apps, the admin config and the relay](#built-in-apps-the-admin-config-and-the-relay).

## Trust boundary

The server, its policy, the approval UI, connector credentials, the organization configuration and
the audit log are the trusted computing base. Invalid security configuration stops the daemon
rather than weakening it.

### Organization mode

The server runs where the AI client cannot reach it except through `/mcp`. People sign in with
the organization's identity provider. A browser session and an MCP client's OAuth token from the
same sign-in resolve to the same person. Each person's connectors, approvals, staged downloads,
passkeys and audit log are kept apart by principal, and one person never receives another's.

Installation-wide settings (which services are offered, sign-in restrictions, step-up, audit
forwarding and the others) are changed on the **Admin** page, by an administrator the identity
provider names. They are stored in the admin config described [below](#built-in-apps-the-admin-config-and-the-relay).

## Single-user mode

An organization-mode install without an identity provider is **single-user mode**: one
administrator, served only at `http://localhost` or `http://127.0.0.1`. It is how the Docker image
starts without any configuration. A server that ever had an identity provider, a pinned bundle key
or `org_server` recorded in its admin config is refused in single-user mode, so removing the
identity-provider settings cannot turn a production server into a setup-code server
([ADR 0260](adr/0260-organization-mode-can-start-from-the-environment-and-run-single-user-on-localhost.md)).

**Signing in.** The server logs a setup code (`Setup code: XXXX-XXXX`) at start, while no passkey is
enrolled. Entering it at `/login` signs you in as the administrator and leads to Passkeys. The code
works once, five wrong attempts discard it until the next start, and it **stops working for good once
a passkey is enrolled**; every later sign-in uses the passkey. Step-up is on and a passkey is
required by default, so approvals and every sensitive Admin action ask for the passkey even with a
valid session cookie.

**What it does not protect against.** Single-user mode is no protection against an agent that can run
`docker` on that computer. Whoever can run `docker` can read `docker logs` (and so the setup code),
read or replace the data volume, or open a page on another localhost port, and takes over the
install. The AI clients you connect run as you. Run the container under a separate operating-system
account (rootless Docker) or in a virtual machine, publish the port only on `127.0.0.1`, or use an
organization server. A lost passkey is recovered by stopping the container and deleting the passkey
credentials file in the volume; the next start prints a new code.

## Built-in apps, the admin config and the relay

**Built-in apps.** The organization image carries PrivacyFence's own OAuth apps
for Google, Slack, Salesforce, Atlassian and Miro. A server uses
one when a service is offered and no connector config replaces it. Their client secrets are public in
effect: anyone can read them from the image. A refresh token is bound to the app that issued it, so a built-in app's
public secret adds no protection to a stolen token; protect tokens as you would with your own app.
Secrets are rotated by adding the new one beside the old where the provider
allows two (Google and Salesforce do), releasing, and retiring the old one once the release is
adopted ([ADR 0255](adr/0255-built-in-oauth-apps-ship-in-every-distribution.md)). Do not mark the
built-in app as trusted or pre-approved in a provider's admin console: that trust would extend to
every PrivacyFence server and to anyone using the public app. To trust an app, register your own and
give it to PrivacyFence as a connector config.

**The relay.** A provider accepts only fixed redirect URLs for an app, so a built-in app redirects to
`https://oauth.privacyfence.eu/callback/<provider>`, and the relay forwards that browser redirect to
your server ([ADR 0259](adr/0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md)).

- *What it sees.* The query of each redirect (the one-time authorization code and your `state`) and
  the host name your server registered, its registration dates and, for the built-in Google sign-in,
  the verified sign-in domains. It writes no logs, stores no authorization code, and holds no client
  secret, access token or refresh token: it never exchanges a code. A localhost server needs no
  registration; its redirect goes straight back to your own browser. The
  [privacy statement](https://privacyfence.eu/privacy/#relay) and the
  [relay terms](https://privacyfence.eu/relay-terms/) are the legal text.
- *Domain verification.* The relay forwards only to a host that proved it controls its name: the
  server sends the relay a secret and serves its SHA-256 at `/.well-known/privacyfence-relay` (the
  route answers 404 unless a relayed service is in use), and renews daily. Only the holder of the
  secret can register a host or change its registration; the public hash alone does nothing. The
  relay refuses hosts under private suffixes such as `github.io` and `pages.dev`, where anyone can
  create a name. The built-in Google sign-in also needs each sign-in domain proven in DNS (a TXT
  record at `_privacyfence-relay.<domain>` whose value is a hash bound to the server's own host
  name, so it cannot be reused by another server), and forwards a code only for a verified domain.
  A verified domain is dropped only when its DNS record goes away.
- *Maintainer controls.* Each new registration emails the maintainers, and a reported host is
  revoked within one working day.
- *What a compromised relay could do, per provider.* It sees the code in transit and could send it
  elsewhere, or substitute its own. For **Google, Atlassian and Salesforce**, which PrivacyFence signs
  in with PKCE, a code without the server's verifier is useless, and a substituted code fails at the
  exchange. For **Slack and Miro**, which send no PKCE, a compromised relay could exchange a code
  before your server does, or sign a person in to an attacker's account; the Admin page says "Sign-in
  codes for this service pass through oauth.privacyfence.eu without PKCE. Use your own app to avoid
  this." on those rows. A compromised relay never gets a token that is already stored on your server,
  and none of this applies to a service that uses your organization's own app. A compromised deploy
  path could change the relay; the deploy environment requires reviewers other than the author.
- *Consent phishing.* Anyone who owns a public domain can run a server and show "PrivacyFence" on a
  provider's consent screen. Domain proof, the notification and revocation bound this, which is why
  the provider guides tell you not to trust the built-in app.

**The admin config.** Installation-wide settings live in `org/admin_config.json`, written only by the
daemon with owner-only permissions, and changed on the Admin page by an
administrator (the one administrator in single-user mode, an identity-provider administrator on a server). It has the trust of `settings.yaml`: whoever can write the install
directory can change it ([ADR 0257](adr/0257-operational-settings-live-in-a-daemon-written-admin-config.md)).
The addresses a Peek or Grist provisioning key is sent to come only from the server's environment (or
an upgraded server's locked address), so a hijacked Admin session cannot point the key at another
host; changing audit forwarding to another URL clears its bearer-token variable.

**Signed connector configs.** An organization's own app for Google, Slack, Salesforce, Atlassian, Miro
or Grist is a small text file signed with an Ed25519 key. The first upload pins the key only after an
administrator confirms its fingerprint, a different key is refused (rotation is deleting
`org/connector_config_signing_pubkey.txt` on the server), each provider's serial must exceed any
accepted before, also after the config is removed, and the file is verified again at every start
([ADR 0269](adr/0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md)).
Signing covers only app credentials (and the startup settings of a legacy bundle); operational
settings are not signed, they are the admin config
([ADR 0256](adr/0256-org-config-holds-only-startup-settings-and-app-credentials.md)).
Uploading or removing one always asks for a passkey, is audited without the secret, and makes everyone
who connected that service sign in again.

**Sign-in by verified email.** An administrator can be named by email
(`PRIVACYFENCE_ADMIN_EMAILS`), and sign-in can be limited to allowed email domains. Both trust
an address only when the identity provider reports it verified (`email_verified` exactly `true`),
and with Google the account's Workspace domain (`hd`) must match the address, so a personal account
registered with a work address cannot get in. A
provider that never sends `email_verified`, such as Microsoft Entra ID, needs
`PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL=true` for allowed domains to keep working; it never applies to
administrators named by email. See
[ADR 0265](adr/0265-admins-can-be-named-by-verified-email.md).

## Human and unattested sessions

A web session is created by signing in: with your passkey after the setup code in single-user mode,
or through the organization's identity provider on a server. An AI client has no way to obtain one:
no MCP tool mints a sign-in link or a sign-in credential
([ADR 0013](adr/0013-no-mcp-tool-mints-a-sign-in-credential.md)). A client can only call `/mcp`, so it
cannot approve its own request, and releasing a gated call needs a passkey wherever step-up requires
one ([Passkey step-up](#passkey-step-up)).

A session expires after 30 minutes idle or 24 hours after it started, whichever comes first.

## Passkey step-up

Step-up asks for a fresh WebAuthn passkey assertion (Touch ID, Windows Hello, a security key, or
a phone over the browser's QR-code flow) before a decision is released. The `step_up` settings live
in `org/admin_config.json` (see [Configuration reference](configuration-reference.md)) and default to:

| Key | Organization server | Single-user install |
| --- | --- | --- |
| `enabled` | `false` | `true` |
| `require_passkey` | `false` | `true` |
| `scope` | `writes_and_pii_reads` | `writes_and_pii_reads` |
| `batch` | `single_assertion` | `single_assertion` |

An explicit value always wins. An administrator makes step-up stricter on
**Admin → Step-up** (the page only offers stricter choices); relaxing it is an edit of
`org/admin_config.json` followed by a restart ([ADR 0068](adr/0068-step-up-is-turned-on-from-the-ui-and-off-only-by-a-config-edit.md)),
and `rp_id`, `rp_name` and `batch` have no control and are set the same way.

`scope` decides which approving decisions need a passkey:

| `scope` | Write | Read flagged as personal data | Any other read |
| --- | --- | --- | --- |
| `writes` | Passkey | — | — |
| `writes_and_pii_reads` | Passkey | Passkey | — |
| `writes_and_reads` | Passkey | Passkey | Passkey |

A read an always-allow rule covers never becomes an approval, so no scope asks about it.

With `require_passkey` on, the following also need a passkey on every deployment
([ADR 0034](adr/0034-sensitive-settings-writes-require-step-up-in-both-modes.md)), whatever `scope`
says:

- adding or removing an always-allow rule; changing the default or a category policy; turning PII
  detection or a PII category on or off; the calendar free/busy setting; re-enabling a connector
  that was switched off; turning step-up on;
- uploading an organization config bundle;
- every sensitive **Admin** action (offering a service, uploading or removing a connector config,
  changing a server address, the Salesforce login URL, sign-in restrictions, step-up, scoped tokens,
  audit forwarding, the privacy policy, uploading rooms or a bundle): these ask for a passkey
  **whatever the step-up configuration says**, in every mode, and a person with no passkey is told
  to add one on `/security` first ([ADR 0257](adr/0257-operational-settings-live-in-a-daemon-written-admin-config.md));
- confirming a rule an AI client proposed with `privacyfence_propose_policy_change` (see below).

Adding a passkey, and removing your last one, are gated whatever `require_passkey` says (see
[Enrolling a passkey](#enrolling-a-passkey)). Denying or cancelling never needs a passkey.

How the settings combine:

- **`require_passkey` on, nothing enrolled:** the daemon starts, every page shows a banner, and
  every approving decision `scope` covers and every sensitive change is refused (`403`, naming
  `/security`) until a passkey is added.
- **`enabled` on, `require_passkey` off:** a passkey is asked for only if one is enrolled. With
  none enrolled, a decision is accepted on a fresh sign-in at the identity provider instead of a passkey for a single decision, and a batch
  is refused that needs step-up (`400`, nothing applied): approve each item from its card, or add a
  passkey ([ADR 0066](adr/0066-step-up-falls-back-by-mode-and-require-passkey-closes-the-fallback.md)).
  That sign-in starts only from the card's own button, by a request the page checks, never from a
  link ([ADR 0179](adr/0179-an-idp-step-up-starts-only-from-the-card-by-post.md)).
- **Batch approval:** one assertion covers a selected set of approvals and is bound to that exact
  set and each item's result. It cannot be replayed for a larger, smaller or altered set.
  `batch: per_item` sends every item that needs step-up back to its own card. PII confirmations
  and choice dialogs are never batched.

Turning step-up on from the Settings page (the **Security** card) is immediate and audited
(`step_up_requirement_enabled`). There is no control that turns it off. That takes an edit of
`org/admin_config.json` and a restart. The change is audited (`step_up_requirement_disabled`), and a
banner stays on every page, with a warning in the server log, until it is turned back on.

### Passkeys for proposed rules

`privacyfence_propose_policy_change` lets an AI client ask for an always-allow rule. No approval
card is involved, so the confirmation dialog is the whole gate. Confirming it needs a signed-in session and, wherever `require_passkey` is on, a passkey, regardless of `scope`.
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
  everywhere.
- **Nothing enrolled yet:** the first enrollment rests on the sign-in: the setup code in
  single-user mode, the identity provider on a server. Enroll everyone before relying on
  `require_passkey` against a stolen session, and treat an unexpected
  `webauthn_credential_enrolled` entry as an incident.

Refused enrollments are audited as `webauthn_enrollment_refused`. A first enrollment's audit entry
says "First passkey enrolled".

## Recovery code

A recovery code lets you back in when every enrolled passkey is lost. It is 16 hexadecimal
characters in four groups (`A1B2-C3D4-E5F6-1789`). It is issued when you enroll a passkey and no
unused code is on file. Only a salted SHA-256 hash is stored, under `authority/`.

**Where it is shown.** Once, in the browser, when the passkey is enrolled. Nothing keeps the plain
text, so a lost code cannot be shown again.

**Using it.** On `/security`, choose **Use your recovery code**. The attempt:

- is rate-limited to 5 attempts per session and 20 across all sessions in any 15 minutes (the
  count resets when the daemon restarts);
- is audited, whatever the outcome: `webauthn_recovery_refused` with the reason (too many attempts, wrong or used code) but never the code, or
  `webauthn_recovery_code_used` on success.

A wrong code consumes nothing: the stored code stays valid for the next attempt. A correct code is
marked used and removes every passkey enrolled for you, so you can enroll a new one straight away.

## Scoped tokens

A scoped token is a bearer credential for `/mcp` that a person creates in Settings
for one client or job. It has a name, an expiry (90 days by default) and a scope.

- **What a scope limits.** The tools the client sees and may call, and the token's own rules. The
  rules are a copy kept on the token (added on the New token form or copied from an Auto-accept
  rule) and a scoped call never consults the person's Auto-accept rules
  ([ADR 0236](adr/0236-a-scoped-token-holds-its-own-rules.md)). A call that no rule of the token auto-accepts is denied at once (`denied_scoped_token` in
  the audit log). Nothing outside the scope can be reached.
- **Deny-only.** A scoped call never gets an approval card and never leaves a pending approval, and
  the temporary "Allow once" window does not apply to it. It cannot change rules, read the full
  rule list or wait for an approval.
- **Stored as a hash.** The secret is `pfst_` plus random characters; only its SHA-256 is kept, in
  `scoped_tokens.json` under `authority/`. A malformed file fails closed: the tokens stop working.
  It is accepted only on `/mcp`.
- **Attested agent.** The audit log records the call as the agent `scoped-token:<id>`, named after
  the token, with source `override`. It is attested because a person created the credential, and it
  outranks a redirect-URI attestation for that call
  ([ADR 0164](adr/0164-scoped-tokens-are-named-expiring-rule-and-tool-scoped-deny-only-credentials.md)).
  Renaming a token changes the name on later entries only; earlier entries keep the name it had.
- **Personal data in reads.** Two separate boxes on the New token form lift the personal-data scan for
  a token's covered calls: exports, downloads and page uploads, and Gmail and Slack message reads. The
  scan still runs and the audit log records what it found; every other call still stops for review, or
  is denied for a token, when personal data is found
  ([ADR 0173](adr/0173-personal-data-is-allowed-per-scoped-token-not-per-rule.md),
  [ADR 0219](adr/0219-a-scoped-token-can-let-email-and-slack-reads-return-personal-data.md)).
- **One token per routine.** Give each routine its own token with its own rules, so a rule for one
  routine never reaches another routine or your interactive clients
  ([ADR 0220](adr/0220-a-routines-rule-profile-is-a-scoped-token.md),
  [ADR 0236](adr/0236-a-scoped-token-holds-its-own-rules.md)).
- **Deleting a rule.** A token's rules cannot be edited. Deleting or changing an Auto-accept rule,
  in Settings or through `privacyfence_propose_policy_change`, does not change any token; revoke
  the token to take its access away ([ADR 0236](adr/0236-a-scoped-token-holds-its-own-rules.md)).
- **AI-proposed tokens.** A client can call `privacyfence_propose_scoped_token`; it creates nothing and
  returns only a review link ([ADR 0250](adr/0250-an-ai-client-can-propose-a-scoped-token-and-only-a-person-creates-it.md), [ADR 0251](adr/0251-a-token-proposal-is-attributed-to-the-client-that-made-it.md)).
  - The tool is not offered to a scoped token, and is refused in an unattended session.
  - A proposal is single use and lasts one hour. The review can only narrow it (rules, verbs, tools,
    days); the personal-data boxes start unticked and only the person can tick them.
  - Creating needs the token's name typed exactly. Passkey step-up, where in force, is bound to the
    request and the proposal, so it cannot be replayed for another.
  - The client never receives the token or the outcome. The review page names the proposing client and
    says that name is the client's own claim.
  - `from_session` builds the proposal from a ledger of the session's approved calls, kept in memory
    only and dropped when the session ends ([ADR 0252](adr/0252-each-mcp-session-keeps-its-approved-calls-in-memory-so-a-token-can-be-proposed-from-them.md)).
  - Every step is audited (`scoped_token_proposed`, `_accepted`, `_declined`, `_expired`).
- **Showing it.** The secret is shown once in the browser, and only after the administrator turns
  scoped tokens on (`scoped_tokens.enabled`) and sets the longest lifetime
  (`scoped_tokens.max_days`). Token proposals are a further opt-in,
  `scoped_tokens.allow_ai_proposals`, off by default and only effective when scoped tokens are on
  ([ADR 0253](adr/0253-ai-token-proposals-are-off-in-organization-mode-until-the-administrator-turns-them-on.md)).
- **Revoking.** A person revokes one token, or all, in Settings (with a passkey wherever the
  install requires one). An administrator revokes every token of one person with
  `privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID` (run it in the container: `docker compose exec privacyfence privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID`). A scoped token outlives an OAuth sign-in
  (which ends within 30 days), so offboarding a person includes running that command
  ([ADR 0165](adr/0165-org-mode-scoped-tokens-are-an-admin-opt-in-shown-once.md)).
  When Peek auto-provisioning is on, also remove the person from the identity provider and run
  `privacyfence-app --peek-release PRINCIPAL_ID`, which revokes the Peek token PrivacyFence minted for
  them and stops renewing it ([Peek setup](peek-setup.md#automatic-accounts)).
  When Grist auto-provisioning is on, offboarding also includes
  `privacyfence-app --grist-release PRINCIPAL_ID`, after removing the person from the identity
  provider. It deletes a key the provisioner created; a key the person made in Grist was adopted
  and stays in Grist, so delete it there or disable the account.
  A token delivered to n8n is deleted from n8n within 5 minutes of being revoked, so this command
  also removes those credentials ([below](#tokens-delivered-to-n8n)).

## Tokens delivered to n8n

An administrator can let an AI client ask for a scoped token to be delivered
into n8n ([ADR 0278](adr/0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md)). This is the
one case where a token's secret leaves PrivacyFence.

- **Who can read the token.** PrivacyFence stores it in n8n as a bearer credential, encrypted under n8n's key.
  Anyone who can edit workflows there can attach it to a node, including an HTTP Request node that points at an
  echo endpoint, and read it from the execution data. That includes the AI client that proposed it. Narrow rules
  and a short lifetime are the protection, and the review page says so before the person confirms.
- **The PrivacyFence credential holds the address.** With **Credential type in n8n** set to the PrivacyFence
  node, the credential holds the token and the PrivacyFence MCP address it is sent to. Whoever can edit that
  credential in n8n can point the token at a host of their own, which is the same reach as above: whoever can
  edit workflows. The node has no address override and never follows a redirect. The exposure is documented,
  not removed ([the node](n8n-node.md#what-the-credential-exposes)).
- **The workflow id is advisory.** `deliver_to.workflow_id` is what the client reports. It is shown on the review
  page and written to the audit log, and n8n does not tie the credential to it.
- **The n8n key never leaves the environment.** `PRIVACYFENCE_N8N_URL` and `PRIVACYFENCE_N8N_API_KEY` come only
  from the server's environment, so a hijacked Admin session cannot point the key at another host. The key is
  never logged, audited, stored by PrivacyFence or returned
  ([ADR 0279](adr/0279-the-n8n-address-and-api-key-come-only-from-the-server-environment.md)). On n8n Enterprise
  give it only `credential:create` and `credential:delete`; other editions cannot limit a key.
- **The environment can switch delivery on.** `PRIVACYFENCE_N8N_DELIVERY=true` also turns on scoped tokens and AI
  token proposals, and locks them (with the n8n switch) on Admin, so an administrator session cannot turn them off
  while it is set. Whoever controls the server's environment already controls the n8n key. Unsetting the variable
  hands both back to Admin ([ADR 0294](adr/0294-n8n-delivery-can-be-switched-on-from-the-server-environment.md)).
- **The status tool.** `privacyfence_runtime_credential_status` tells an AI client whether the person
  accepted, the credential's id, name and the MCP address, and when the token expires; a later revoke
  turns the answer into `not_delivered`, and an expiry into `expired`, carrying `token_expired_at` and a message asking the person to extend. It never returns the token, a token id or the n8n host, and answers only
  for the signed-in person's own proposals, so any AI client signed in as that person can read it ([ADR 0280](adr/0280-privacyfence-runtime-credential-status-reports-a-delivered-credential-to-its-proposer.md)).
- **Workflow data stays in n8n.** What a workflow reads through PrivacyFence is kept in n8n's execution history,
  under n8n's retention settings.
- **Fail fast.** A call no token rule covers is denied at once, so a workflow never waits for an approval.
- **Offboarding.** Revoking the token deletes the n8n credential within 5 minutes; an expired token's credential is kept until the person extends the token (Settings → Scoped tokens → Extend) or revokes it; a
  sweeper reconciles the records against the token store, so `privacyfence-app --revoke-scoped-tokens` is
  covered too ([ADR 0281](adr/0281-a-sweeper-deletes-the-credential-of-a-revoked-expired-or-undelivered-token.md)).
  If the n8n address is changed, the old credentials are not deleted: their tokens are revoked and the audit log
  says to delete them by hand.
- Every delivery, deletion and failure is audited (`runtime_credential_*`). Delivery starts only from an AI proposal ([ADR 0282](adr/0282-runtime-delivery-is-organization-mode-only-and-starts-from-an-ai-proposal.md)).

## Where the data lives

The server runs in its own container and keeps its data in the container's data volume
(`/var/lib/privacyfence-org/.privacyfence`), owned by the image's user (uid and gid `10001`). The
AI client reaches PrivacyFence only through `/mcp`, so it cannot read or write the files that decide
what it may do: the always-allow rules, policy and PII settings, the passkey and recovery-code
files, connector credentials, the scoped-token hashes and the audit log with its `.audit_chain.key`.
What the volume holds is listed in
[Organization deployment with Docker](org-mode-docker.md#what-stays-in-the-data-volume); the modes
and owners to keep on a bind-mounted data directory are in
[A bind-mounted data directory](org-mode-docker.md#a-bind-mounted-data-directory).

Anyone who can run `docker` on the host, or who can write to the volume, controls the install; that
is why single-user mode is no protection against an agent with a shell and the Docker socket
([Single-user mode](#single-user-mode)).

## Audit log integrity

Each gate decision, approval and security event is written to a weekly JSON Lines file
(`YYYY-Www.jsonl`). Each principal's log is `authority/logs/audit/` under that principal's data directory. Every entry is chained to the previous one with HMAC-SHA256, keyed by
`.audit_chain.key` in the same directory, on every install. Check a log from a source checkout with
its dependencies installed:

```
python3 scripts/verify_audit_log.py "<data directory>/authority/logs/audit"
python3 scripts/verify_audit_log.py <dir> --week 2026-W38     # one ISO week
```

Exit status: `0` intact, `1` a week failed (or no files found), `2` directory missing.

The script also checks that each week's first entry continues the previous week's last one, and that the newest entry matches `.audit_chain_state.json`, so a removed week or a cut-off tail is reported, unless the `.audit_chain_state.json` file was edited too (it is not keyed). Lines written before chaining existed are accepted only at the top of a file ([ADR 0176](adr/0176-a-chainless-audit-line-is-accepted-only-before-the-first-chained-line.md)).

The chain detects edits, insertions and deletions made without the key. Anyone who can read the
key can rebuild a consistent chain. On the server that means root on the host or anyone who can read the volume. For evidence that survives that,
send a copy off the machine. The server can forward every entry to syslog or an HTTP
endpoint (**Admin → Audit forwarding**, stored as `audit_forwarding` in `org/admin_config.json`).

Security events have their own `decision` values: `sign_in_code_minted`,
`webauthn_credential_enrolled`, `webauthn_credential_removed`, `webauthn_enrollment_refused`,
`webauthn_recovery_code_used`, `webauthn_recovery_refused`, `step_up_requirement_enabled` and
`step_up_requirement_disabled`. Changes to organization auto-accept rules have the `decision` values `org_rule_added`, `org_rule_removed` and `org_rule_verbs_changed`. Treat the audit log as sensitive. It shows which services, tools and
resources were used even when no content was released.

Every entry also has an `allowed_by` field that says who allowed the call: `person` (a click or an allow-all choice), `person_rule` (the person's own auto-accept rule), `org_rule` (an organization auto-accept rule) or `token_rule` (a scoped token's rule); it is empty when nothing allowed the call. Entries written before the field existed, and the tools that never ask, are classified when read (the latter as `no_approval_needed`), so the Excel export's **Allowed By** column is filled for old rows too. See [ADR 0334](adr/0334-the-audit-entry-names-who-allowed-a-call.md).

## Which AI system the audit log names

Each gated call records the calling AI system in `agent_id`, `agent_name`, `agent_version` and
`agent_source`. The approval card, the approvals list and the Audit Log page
show it as **Verified**, **Not verified** ("Says it is …"), or **Unrecognised AI system** with the
name sent.

| `agent_source` | Meaning | Shown as |
| --- | --- | --- |
| `oauth_client` | The call's access token belongs to an OAuth client that registered only with a vendor's own redirect URI (claude.ai, ChatGPT, Gemini Enterprise) | Verified |
| `client_info` | The name the client sent (MCP `clientInfo`, or a client's registered name), including one relabelled by an `agent_overrides:` entry | Not verified, or Unrecognised if it matches no known AI system |
| `""` (empty) | No usable name | Unrecognised |

The only verified identity is a redirect-URI attestation: the authorization code can only be
delivered to a redirect URI the client registered, so a registration whose every redirect URI is
one vendor's own callback got its tokens through that vendor. It needs no administrator step, and
it applies to one OAuth registration only. Claude Code and any client with a
loopback redirect stay Not verified, because any program on the client's computer can register one. A member, or a
program using their signed-in browser session, can still register under a vendor's redirect URI and
have their own calls shown as that vendor; it never reaches another member's calls
([AI systems](org-mode-setup-guide.md#11-ai-systems),
[ADR 0224](adr/0224-an-org-registration-is-attested-by-its-redirect-uris.md),
[ADR 0035](adr/0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md)).
A client that holds its own [scoped token](#scoped-tokens) has its calls
recorded as the attested agent `scoped-token:<id>`. An `agent_overrides:` entry only relabels the recorded name and never makes it verified
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
- **Notifications** are web push and carry the count only, encrypted and padded
  to a fixed size; the browser's push service (Apple, Google, Mozilla or Microsoft) sees when one
  was sent, and an administrator can turn them off on the Admin page
  ([ADR 0081](adr/0081-org-mode-sends-a-count-only-web-push.md)).

**A deny note** is the one thing a person can add to what the AI sees. Whoever denies a request can
pick an intent (for example "Wrong target") and type up to 500 characters of their own on why, or
what to do instead. Only the signed-in person deciding that approval can write it, behind the same
session, CSRF and origin checks as a plain Deny; the AI client has no way to decide an approval,
and nothing from the request or the connector data is ever put into the field. The note goes only
to the AI client that made the request, in its denial message and in `privacyfence_await_approval`,
with control and invisible characters stripped, secrets redacted and the text quoted after a fixed
label so it cannot pass for PrivacyFence's own words. It is not screened for content: any
"injection" in it is the user instructing their own agent, and a note approves nothing, so the
agent's next call still goes through the gate
([ADR 0083](adr/0083-a-humans-deny-note-reaches-the-agent-as-delimited-sanitized-user-text.md)).
It is not stored: the audit log records only the chosen intent and the note's length, and the text
never reaches a log line, the approvals stream or a push notification
([ADR 0084](adr/0084-the-audit-log-records-that-a-deny-had-feedback-never-the-text.md)).

See [Approvals and policy](approvals-and-policy.md) for the approval list, cards and PII handling.

## Other controls

- **Web UI:** HttpOnly session cookie, CSRF token and same-origin checks on every change, a
  host allowlist, and a Content Security Policy with per-response nonces. Every custom route must be
  classified or the app refuses to start ([ADR 0014](adr/0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md)).
- **Organization OAuth server:** at most 2,000 registered clients, 8 KiB of metadata per
  registration, and unused registrations pruned after 180 days. Access tokens last 1 hour and
  refresh tokens at most 30 days.
- **Organization config:** validated at startup, which fails instead of weakening. A server's legacy bundle has its hash audited and can be signed
  ([ADR 0016](adr/0016-org-config-bundle-hash-log-and-signing.md)). An organization server's own apps
  are signed connector configs, each verified again at every start. Protect who can replace them and
  the data directory, which holds the admin config.
- **Downloads:** staged encrypted, served once through a short-lived link
  (default 300 seconds) ([ADR 0017](adr/0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md)).
- **Client secrets** belong to the deployment, not a person. Rotate a leaked one with the provider.
- **Supply chain:** each release build produces CycloneDX SBOMs, and the organization image is verified as described in
  [Verify the image](org-mode-docker.md#verify-the-image). Release and QA-runner jobs install every build tool from hash-pinned locks
  ([ADR 0182](adr/0182-every-tool-a-release-or-qa-runner-job-installs-comes-from-a-hash-pinned-lock.md)).
- **Operations:** one server per data directory (a file lock enforces it); it is not
  clustered. Back up and restore the data directory with its owners and permissions intact.

## What PrivacyFence does not claim

- **No certification, business-continuity plan or SLA.** The only PrivacyFence-operated service is
  the optional redirect relay, offered as is under its [terms](https://privacyfence.eu/relay-terms/).
  If it is down, built-in apps cannot start a new sign-in until it is back; tool calls, connections
  that already exist and servers using their own apps are unaffected, and installed copies keep
  running without the maintainer. Reports are handled best-effort. Treat this as a risk acceptance: approve
  PrivacyFence through an exception process, pin a reviewed release, and assign an internal owner to
  track releases ([ADR 0025](adr/0025-no-certified-security-framework.md)).
- **No protection against root on the host**, or against an agent that can run `docker` on a
  single-user install ([Single-user mode](#single-user-mode)).
- **No hard stop from the unattended-session flag.** It is advisory
  ([ADR 0015](adr/0015-unattended-session-flag-is-advisory-only.md)).
- **No compliance by itself.** Whether a deployment meets a regulatory or contractual requirement
  depends on its configuration, identity provider, retention and independent assessment.

To report a vulnerability, see [`SECURITY.md`](../SECURITY.md).
