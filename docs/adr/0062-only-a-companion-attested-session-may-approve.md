# ADR 0062: only a companion-attested session may approve

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-19 in `57eaa5a5`, "Stop
treating a local-mode session as proof that a human asked", merged as
[#556](https://github.com/privacyfence/privacyfence/pull/556), and extended to the organization
config upload in `151d6994`, merged as [#557](https://github.com/privacyfence/privacyfence/pull/557)).
Implemented. `docs/security-and-compliance.md`'s "Human and unattested sessions" section describes
today's behavior.

## Context

A local-mode `pf_session` can be reached three ways, all by design: the companion's Open
Approvals/Open Settings item, a sign-in link a human was handed, and a bare `MINT` on the control
channel. On a privilege-separated install that channel is widened from `0600` to `0660` so the
companion can reach it, which puts the agent in the same group. All three produced the same session
with the same authority, so an agent could mint itself a session and release the write it had
itself requested. ADR 0002 decision 6 had said a minted session should be *insufficient*, with the
passkey supplying the rest, but nothing downstream recorded how a session was established, and an
install with no passkey enrolled had no second question at all.

## Decision

**Every local-mode session carries a provenance, fixed at mint time, and only a `human` session may
approve or change what gets gated.**

- `web/session_auth.py`: `PROVENANCE_HUMAN = "human"`, `PROVENANCE_UNATTESTED = "unattested"`.
  `BootstrapStore.mint(provenance=...)` stamps the code and `_BootstrapMiddleware` passes it
  unchanged to the session the code exchanges for. Both default to `unattested`.
- `web/control_channel.py`: a bare `MINT` mints `unattested`. `MINT COMPANION <nonce>` mints `human`
  only after the daemon hands the nonce back over the companion's own channel (`CONFIRM MINT`) and
  the process that issued it (`issue_mint_nonce`, 30 s, single-use) recognizes it. `MINT CONSOLE`
  (`privacyfence-app --print-sign-in-link`) mints `human` only after the companion's dialog
  (`CONFIRM SIGNIN`). `SHOW /approvals|/settings`, sent by a second companion process (Linux's
  one-shot menu click, see `companion._open_path`), mints `human` only after a dialog
  (`_show_page`). Every shape, refused ones included, writes an audit entry (`_audit_mint`).
- Refused to an `unattested` session, with `403 {"error": "human_session_required"}`
  (`session_auth.human_session_required_json`): an approving decision (`accept`/`accept_all`,
  singly or batched) and creating an auto-accept rule (`web/routes_approvals.py`), every
  `_SENSITIVE_ACTIONS` settings action and the organization config upload (`web/routes_settings.py`),
  and trading in a recovery code (`web/routes_security.py`). Viewing and denying are not gated.
  The check runs before the step-up ceremony and does not depend on `step_up.require_passkey`.
- The gate is on exactly when the install is privilege-separated:
  `require_human_session = privilege_separation.is_enabled()` in `web/server.py`'s `build_app`.

## Alternatives considered

- **Make minting uncallable by the agent** (peer executable path or code-signature checks). Already
  rejected by ADR 0002 decision 6: three platforms would get three strengths of one guarantee.
- **Rely on the passkey alone.** It answers "is this the enrolled human", not "did a human ask for
  this session", and an install with no passkey enrolled asks nothing.
- **Enforce on unseparated installs too.** There is no companion guaranteed to attest through, and an
  agent there can rewrite the credential store directly, so the check would buy nothing. Same line
  `StepUpConfig.from_local_config()` draws for `require_passkey`.

## Consequences

- An agent-minted session can read what is pending but cannot release it; confidentiality of the
  review screen stays the weaker guarantee ADR 0002 decision 6 names.
- This is not authentication of the companion. It shares an OS user with the agent, and its
  address lives under `handoff_dir()`, which is not a security boundary, so a process that binds it
  first can answer for it. Forging a `human` session costs impersonating the companion, which
  breaks the connector OAuth flows sharing that address.
- With no companion running, the companion's fallback is an `unattested` link, and it says so.
- A click from Linux's applications menu, or `SHOW` in general, costs one Allow dialog.

## Verification

- `tests/unit/web/test_control_channel.py`: `TestAttestedMintCommands` (bare `MINT` is
  unattested; a confirmed nonce mints `human`; an unconfirmed or missing nonce mints nothing),
  `TestMintNonces` (single-use, expiring), `TestShowCommand` (a denied dialog mints nothing) and
  `TestEveryMintIsAudited`.
- `tests/unit/web/test_server.py`'s `TestHumanSessionWiring`: a separated install refuses an
  unattested approval, an unseparated one does not.
- `tests/control_channel_client.py`'s attested-mint helpers drive `MINT COMPANION` against a real
  channel.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) decision 6 — make minting
  insufficient, not uncallable.
- [ADR 0003](0003-separated-installs-only.md) — why every packaged install is separated and has a
  companion.
- [ADR 0013](0013-no-mcp-tool-mints-a-sign-in-credential.md) — no MCP tool mints a sign-in
  credential, relying on this provenance.
- [ADR 0031](0031-clicking-privacyfence-opens-approvals-through-the-companion.md) — who may send
  `SHOW`.
- [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md) — the step-up gate on
  the same `_SENSITIVE_ACTIONS`.
- [ADR 0061](0061-the-mcp-token-and-the-browser-session-are-audience-separated.md) — the MCP token
  never counts as a session at all.
