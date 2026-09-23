# ADR 0008: one principal per OS user, identified by the kernel

## Status

Accepted; implemented in part (`local-mode-fixes-plan.md` Phase 3 — see Related). Amends [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md)
decision 6 (see "Relationship to ADR 0002 decision 6" below) and decision 2's companion menu.
Supersedes the local-mode-fixes plan's own Phase 2 §2.6 interim guard (the single-recorded-owner
refusal in `privilege_separation.owner_membership_pending()`/`other_account_owns_this_install()`,
and the shell scripts' `--allow-additional-user` flag) — that guard closed one leak ahead of this
redesign; this ADR is the redesign.

**This phase ships a narrower slice than the plan document describes**, and that narrowing is
recorded here rather than left implicit. See "What this phase deliberately does not do" below for
the full list and the reasoning behind each cut. The security property the plan's Problem 3
actually asks for — that a second OS user added to the service group no longer shares the first
user's Gmail/Drive/audit trail/passkeys/recovery code — is delivered in full. What is deferred is
everything downstream of "that second user now has an isolated, empty account": a way for them to
connect their own connectors, and a personal `/settings` page. Both are next steps, not gaps in
this phase's own guarantee, and the plan's own §3.1 says as much: *"A new user starts with the
packaged default policy and no connectors."*

## Context

ADR 0002 decision 1 drew the local-mode trust boundary at the OS user account and was explicit
that nothing built on top of it may claim a guarantee resting on telling two *processes* apart
while they share a uid. That is a statement about the daemon and the agent — the local-mode-fixes
plan's own Problem 3 diagnosis is about a different pair: two different *human* OS accounts on one
separated machine.

ADR 0003 makes a shared machine possible on purpose (decision 3: any account can be added to the
service group by an administrator), and the local-mode-fixes plan's own Phase 2 found that, once
added, a second account was not merely under-served but actively dangerous:

- every `/mcp` caller presenting `handoff/mcp_token` became the single principal `"local"`
  (`web/mcp_auth.py`'s `StaticTokenVerifier`), so a second account's Claude Desktop reached the
  first account's Gmail/Drive/Slack through PrivacyFence, using policy and audit trail that were
  never theirs;
- the companion channel had one socket per machine, and the last companion to (re)bind it received
  every account's `OPEN`/`MINT COMPANION`/`SHOW RECOVERY` traffic — including recovery codes, which
  is how a second account could walk off with the first account's approval authority entirely;
- `enable --for-user`'s data migration could overwrite the first account's `settings.yaml`,
  credentials and audit log with the second account's own `~/.privacyfence`.

Phase 2 (`local-mode-fixes-plan.md` §2.6; the socket fix is ADR 0027, the rest shipped in #609) closed all
three with a *refusal*: `owner_membership_pending()` stopped offering the elevated join to anyone
but the marker's recorded `owner_user`, the sticky bit plus a uid check stopped socket takeover
outright, and `--for-user`'s migration refused to touch a non-owner's `~/.privacyfence`. That is a
correct, narrow fix for the leak that was live — and it is explicitly temporary. It does not give a
second account anything; it stops them from silently taking the first account's.

This ADR is the "Phase 3 will" the shell scripts and ADR 0002 both already point at: a second
account is no longer refused, because it is no longer dangerous to admit.

## Decision

### D1: the daemon learns *who* from the kernel, on the one channel that can answer

Local mode has exactly one place a process ever proves which OS account it runs as without being
asked to self-report: the control channel's peer credentials — `SO_PEERCRED` (Linux),
`LOCAL_PEERCRED` (macOS), the named-pipe client process's token (Windows). `web/control_channel.py`
already reads the first two, today only to answer one question ("is this connection the daemon's
own separated service account, for `_verify_companion_peer()`"). This ADR generalizes that read
into `peer_identity()`: a `(uid_or_sid, account_name)` pair for *any* connecting peer, backed by the
same primitives, plus a new one for Windows this codebase did not have before (`GetNamedPipeClient
ProcessId` → `OpenProcess` → `OpenProcessToken` → `GetTokenInformation(TokenUser)`, mirroring the
pattern `_current_user_security_attributes()` already uses for this process's *own* SID, aimed at
the peer's instead).

This is not the case ADR 0002 decision 6 forbids. Decision 6 is about the daemon's own MINT/QUIT
channel telling *the human who clicked the tray icon* from *the agent running curl*, while both
share one uid — a distinction no peer credential can make, because there is only one peer identity
to read. `peer_identity()` answers a different question: which of *several* uids is this. Nothing
here claims the companion-vs-agent guarantee decision 6 says a uid cannot provide; #428 B10 already
established that the same primitive *can* distinguish two different accounts (there, the daemon's
service account from everyone else) — this ADR is that reasoning applied to two human accounts
instead of one human account and one service account.

### D2: two identities, not one, per install

- The account named by the marker's `owner_user` keeps mapping to `LOCAL_PRINCIPAL` (`id="local"`).
  Every byte this install already has on disk stays exactly where it is; upgrading a single-user
  install changes nothing observable.
- Every other service-group member maps to `Principal(id=f"os-{uid}")` (POSIX) or
  `Principal(id=f"os-{sid}")` (Windows) — stable for the life of the account, opaque otherwise.
  `paths.user_dir()`/`authority_dir()`/`uploads_dir()`/`downloads_dir()` already resolve any
  non-local principal id to `data_dir()/users/<id>/...`; nothing there changes for this phase, which
  is what P6/P7/P8's earlier work buys back here for free (see `local-mode-fixes-plan.md`'s own
  "Already generic / no changes needed" accounting).
- Group membership keeps exactly its ADR 0003 meaning — "may use PrivacyFence on this machine" —
  and stops meaning "may share the owner's principal." `owner_membership_pending()` no longer
  refuses a non-owner account; every service-group member who has not yet completed the per-user
  half is "pending" in the same sense the owner always was, and `enable --for-user` (now
  unconditional, `--allow-additional-user` retired) is how they finish. `other_account_owns_this_
  install()` and the notice it drove are removed with it — there is no longer a second-class
  account for a notice to explain.

### D3: kernel-authenticated MCP tokens replace the one shared secret

`handoff/mcp_token` was a single value read from one file — anything that could read the file was
`"local"`. A separated install now issues one token **per principal**, minted over the control
channel rather than read from a shared file: `MINT MCP` returns the caller's own token (loaded from
`authority_dir(principal)/mcp_token` if one already exists, minted and persisted otherwise — the
same load-or-create posture `mcp_auth.load_or_create_mcp_token()` always had, just keyed by the
caller's peer identity instead of assumed); `ROTATE MCP` replaces it. `StaticTokenVerifier` becomes
`PerUserTokenVerifier`: a `{sha256(token): principal_id}` map, populated at startup from every
already-provisioned principal's own token file and grown by every `MINT`/`ROTATE`.
`AccessToken.client_id` stays the literal `"local"` — nothing about org mode's own token shape
changes — and now also carries `subject=principal_id`, which `principal_from_access_token` reads
before falling back to `LOCAL_PRINCIPAL`. `routes_mcp.py`'s dispatch was already principal-scoped
end to end for org mode's sake (P6/P7/P8); once the token itself carries a real subject, every
per-tool-call gate/audit/connector/staging path downstream is correct with no further change, which
is the whole reason this ADR does not also need to rewrite `routes_mcp.py`.

The `.mcpb` shim mints its own token the same way the companion already mints a bootstrap code:
`protocol.ts` gains `mintMcpToken()`, sent as `MINT MCP\n` over the same control-channel address
`companion.py` already speaks to. `index.ts` calls it instead of reading `mcp_token` off disk, and
falls back to the legacy file only when the control-channel mint itself fails (an unseparated
install predating this change, or a daemon too old to answer `MINT MCP` at all) — which is also why
the daemon only deletes a leftover `handoff/mcp_token` when privilege separation is enabled: an
unseparated single-user install has nothing to gain from losing a file an old client might still be
reading, and nothing to lose by keeping it, since there is exactly one principal there regardless.
`privacyfence-app --print-mcp-token` is the CLI's own equivalent, for a direct HTTP client that
has no shim to mint on its behalf.

### D4: the companion channel becomes per-user, not per-machine

`companion.sock` was one address for the whole machine; whichever companion (re)bound it last
received every account's `OPEN`/`CONFIRM`/`SHOW RECOVERY` traffic. Every non-owner principal now
gets its own address — `handoff/companion-<uid>.sock` (POSIX) — while the owner keeps the
unsuffixed `companion.sock` unchanged, so an existing single-user install's address does not move.
The daemon resolves which address to speak to from `current_principal()` at the point it needs to
reach a companion (an OAuth flow, a first-enrollment confirmation, a recovery code) — every one of
those call sites already runs inside that principal's own `principal_scope`, so this costs a lookup,
not new plumbing. `handoff/`'s sticky bit (Phase 2 §2.6) still does its job unmodified: it stops any
*other* group member from unlinking a socket they do not own, which matters exactly as much with
five legitimate companions as it did with one.

### D5: the browser session carries the principal it was minted for

`pf_session` had no principal dimension at all — `LocalSessionStore`'s own docstring said so
explicitly, contrasting it with org mode's `OrgSessionStore`. Every one of `MINT`/`MINT COMPANION`/
`MINT CONSOLE` now resolves the connecting peer's principal (D1) and threads it through
`BootstrapStore.mint()` into `LocalSessionStore.create()`, so the session a browser ends up holding
is tagged with the OS account that asked for it. `web/server.py`'s `_PrincipalScopeMiddleware` reads
that tag back instead of the previous hardcoded `_default_principal` (always `LOCAL_PRINCIPAL`), and
`routes_approvals.py`/`routes_settings.py` stop importing `LOCAL_PRINCIPAL` directly at each
decision/step-up call site in favor of `current_principal()` — the same principal the middleware
already entered for the whole request, which is the pattern every other per-principal registry in
this codebase already relies on (P6's own `PrincipalRegistry`).

`/approvals` and `/security` (passkey enrollment, step-up, recovery) work per-principal as a direct
consequence: `routes_security.py` already took a `resolve_principal` parameter for exactly this
seam (built for org mode, unused by local mode until now); `routes_approvals.py`'s pending-approval
list was already filterable by principal (`PendingApprovalRegistry`'s own `max_pending_per_
principal`, from the self-approval hardening plan). A request whose principal cannot see another
principal's pending approval gets a 404, not a 403 — the same "don't confirm existence" posture
this codebase already uses at every other principal boundary.

## What this phase deliberately does not do

**No personal `/settings` page for a non-owner principal.** Local mode's `SettingsController` is
one instance, built once at daemon startup, wrapping the owner's own `ConnectorHost` and config
file — turning that into a genuinely per-principal settings surface (its own connector-toggle
state, its own config file, its own admin UI) is a real second project, not a seam this phase can
reuse the way `routes_security.py`'s already did. A non-owner principal's requests to `/settings`
get a 404, exactly like a cross-principal approval lookup. Nothing about this narrows what the
plan's own §3.1 promises: *"a new user starts with the packaged default policy and no connectors"*
— there is, by design, nothing yet for a personal settings page to show them.

**No `/connect` surface for local mode.** `web/routes_connect.py` is already fully
principal-generic (every path it touches goes through `paths.user_dir(principal)`) and would be the
right template for a local-mode connector-authorization flow, but mounting one is new product
surface, not a fix to an existing leak, and the plan's own text above says a fresh principal
starting with no connectors is the intended shape of this phase, not a stopgap. A follow-up that
wants a second OS user to actually *use* a connector needs this; this phase only needs them not to
use the owner's.

**No real multi-user Linux integration test with provisioned OS accounts in this change.** The
plan's own `tests/integration/test_multi_user_isolation.py` (real `useradd` accounts, a live
separated daemon, two real shim/`MINT MCP` round trips) needs a Linux CI runner with root, which is
exactly what `.claude/skills/steward/SKILL.md` reserves for packaged/system-level tests dispatched
to GitHub Actions rather than run from an interactive session. The unit-level equivalents (peer
identity over a real `AF_UNIX` `socketpair()`, `PerUserTokenVerifier` isolation, session-principal
binding, per-uid companion socket refusal) are added directly; the full end-to-end version is
recorded as a follow-up for a runner-dispatched run rather than skipped silently.

**Windows peer identity is written, not exercised.** The `GetNamedPipeClientProcessId`/
`OpenProcessToken`/`GetTokenInformation` chain follows the same pattern this codebase already uses
for its own process's SID, but nothing in this repository can execute pywin32 code outside a
Windows CI runner — the same posture every other Windows-specific branch in `control_channel.py`
and `privilege_separation.py` already has, and the same reason those branches are `# pragma: no
cover` on the non-Windows suite today.

## Relationship to ADR 0002 decision 6

Decision 6's claim — a peer's uid cannot tell the human clicking the tray icon from the agent
running curl, because they share one uid — is unchanged and unaffected. Every mechanism this ADR
adds distinguishes *two different OS accounts* from each other; it grants no OS account a way to
prove it is the human rather than that account's own agent. A second OS user's agent is exactly as
capable of calling `MINT MCP`/`MINT`/`MINT COMPANION` as the owner's agent is of calling the
owner's own — decision 6's asymmetry (integrity is the strong guarantee; confidentiality of the
review screen is the weaker one) holds per-principal, unchanged in kind, just no longer shared
across accounts that have nothing to do with each other.

## Consequences

- A separated install can add a second (third, fourth, …) OS account to its service group and have
  that account's Claude Desktop see its own, empty, unconfigured PrivacyFence — never the owner's
  Gmail/Drive/audit trail/passkeys/recovery code.
- `handoff/mcp_token` is removed on startup of a separated daemon; an old `.mcpb` that only knows how
  to read that file gets the same "update the extension" failure mode ADR 0007 already established
  for a stale shim, not a silent wrong-principal token.
- The companion socket takeover [ADR 0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md) closed with a refusal is now closed by
  there being nothing to take over: every non-owner principal has its own address.
- `enable --for-user` for a second account migrates *that account's own* legacy data into its own
  `users/os-<uid>/`, never into the shared root — the data-merge hazard the interim guard prevented
  by refusing is now prevented by isolating instead.
- An install that already ran `--allow-additional-user` under the interim guard has a second OS
  account in the service group whose calls were, until this ships, resolving to `LOCAL_PRINCIPAL`
  exactly like the owner's own. Upgrading such an install does not retroactively separate any data
  that account already read or wrote as `LOCAL_PRINCIPAL` — only calls made after the upgrade get
  the new, isolated principal. This is stated here rather than left for someone to discover: an
  administrator who used that flag should treat the owner's pre-upgrade connector state as having
  been shared, and rotate anything sensitive if that matters to them.

## Verification

- `tests/unit/web/test_control_channel.py`: `peer_identity()` over a real `AF_UNIX`
  `socket.socketpair()`; `MINT MCP`/`ROTATE MCP` mint distinct, stable-per-peer tokens; the existing
  `MINT`/`MINT COMPANION`/`MINT CONSOLE` paths bind the resulting session to the peer's principal;
  a companion socket already owned by a different uid is refused (extends the existing
  `_existing_socket_owner_problem` coverage to the per-uid address).
- `tests/unit/web/test_mcp_auth.py`: `PerUserTokenVerifier` resolves distinct tokens to distinct
  principals and a subject of `"local"` to `LOCAL_PRINCIPAL`.
- `tests/unit/web/test_routes_approvals.py` (extended): principal A's pending approval is invisible
  (404) to principal B, and B's own decision on their own approval still works exactly as before for
  `LOCAL_PRINCIPAL`.
- `tests/unit/test_privilege_separation.py` (extended): `owner_membership_pending()` no longer
  refuses a non-owner account; `--allow-additional-user` is gone from the platform scripts' own
  argument parsing.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — decision 1 (the OS-user trust
  boundary this ADR narrows further), decision 6 (the guarantee this ADR does not touch), and decision 2, whose Phase 2
  amendment is now [ADR 0026](0026-the-companion-manages-the-daemon-through-the-service-manager.md)
  and [ADR 0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md). The
  interim one-owner guard this ADR supersedes shipped with them in
  [#609](https://github.com/privacyfence/privacyfence/pull/609).
- [ADR 0003](0003-separated-installs-only.md) — decision 3, which made a shared service group
  possible in the first place.
- [ADR 0007](0007-local-file-bridge.md) — the per-principal staging directories this phase's
  per-principal MCP tokens finally give a real second occupant.
- `local-mode-fixes-plan.md` (never merged to `main`; read it with
  `git show 453ae02e:local-mode-fixes-plan.md`) — Phase 3's own text, including
  the "packaged default policy and no connectors" framing this ADR's deferrals rest on.
- [Issue #579](https://github.com/privacyfence/privacyfence/issues/579) — builds directly on D5's
  `_PrincipalScopeMiddleware`/`current_principal()` plumbing. That work already unified principal
  resolution across local and org mode (both `web/routes_approvals.py` and
  `web/routes_org_approvals.py` read the same contextvar, fed by mode-specific resolvers registered
  in `web/server.py`), which is why #579's own "enabler" step — originally scoped as giving
  `session_auth.authenticated` a `Principal | None` return type — is already satisfied by a
  different mechanism than the one it named. What #579 still has to do is merge the *route* layer
  (the still-duplicated step-up/`sensitive_confirm` orchestration, and the settings dispatcher) now
  that both modes already agree on how to ask "who is this."
