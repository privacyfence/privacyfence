# Architecture Decision Records

An **Architecture Decision Record (ADR)** is a short, numbered, permanent note of one significant
decision: the situation that forced it, what was decided, what was rejected and why, and what
follows from it. ADRs answer the question a standing reference doc cannot: *why is it this way,
and why didn't we just…?*

This directory is the only place that question gets answered. Plans come and go, reference docs
describe whatever the code does today, and neither keeps a rejected alternative around for long.

## Where each kind of document lives

| Kind | Answers | Home | Lifecycle |
|---|---|---|---|
| **Plan** | What are we about to do, and in what order? | A GitHub issue (preferred), or a `docs/*-plan.md` while its work is open | Temporary. Deleted when its work lands, **after** its decisions have been extracted here |
| **ADR** | Why is it this way, and what was rejected? | `docs/adr/NNNN-*.md` | Permanent. Never deleted; after acceptance only its Status changes |
| **Reference** | How does it work, or how do I do X, today? | `docs/*.md`, `CLAUDE.md`, code docstrings | Living. Edited in the same PR as the behavior. Links here for the *why* instead of retelling it |

## When a decision needs an ADR

Write one when a decision meets **any** of these:

- it is expensive or disruptive to reverse (a packaging format, a wire protocol, a data model);
- it sets or moves a trust or security boundary, or decides who holds a credential;
- it changes how PrivacyFence is built, signed, released or distributed;
- it rejects an alternative a reasonable newcomer would propose, for a reason that isn't obvious;
- it deliberately departs from a rule written elsewhere in this repo (e.g. "stdlib first");
- someone will plausibly ask "why didn't we just…?" in six months.

Implementation detail, naming, and task sequencing don't need one. If in doubt, write a short one:
a 40-line ADR is fine.

## Rules

1. **One decision per ADR.** A document that decides several things is a plan; split its decisions
   out (ADR 0002 and 0003 predate this rule and are left as they are).
2. **Frozen once accepted.** After `Accepted`, the body is not rewritten. The only permitted edits
   are the Status section, fixing a broken link, and typos. Implementation progress, rollout state
   and "phase N landed" notes belong in the tracking issue, not here.
3. **Change your mind with a new ADR.** A reversal or significant amendment is a new ADR that says
   `Supersedes NNNN` (or `Amends NNNN`); the old one's Status gains one line pointing forward.
   This holds even when a plan says "amend ADR NNNN in place": the plan predates the rule or is
   wrong about it. (ADRs 0026–0028 were extracted from exactly such in-place amendments.)
4. **Link only to things that last.** Issues, PRs, commit SHAs, source files, and other ADRs. Never
   link a plan document: it will be deleted. To cite a plan that is already gone, give the commit
   that deleted it and the path, e.g. `git show 96cd5af4^:docs/https-connector-refactor-plan.md`.
5. **Retiring a plan is gated on this directory.** The PR that deletes a plan either adds or amends
   the ADRs for every decision the plan contained, or states in its description that the plan
   contained none. This is part of the definition of done
   ([`coding-and-testing-guidelines.md` §2.7](../coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo)).
6. **Numbers are never reused.** Take the next free number when you open the PR; if a parallel
   branch takes it first, renumber yours before merging.
7. **Update the index below** in the same PR, and `docs/README.md` needs no change: it links here.

### Status values

| Status | Meaning |
|---|---|
| `Proposed` | Under discussion; decides nothing yet |
| `Accepted` | In force. Say whether it is implemented if that isn't obvious |
| `Rejected` | Considered and declined; kept so it isn't re-proposed from scratch |
| `Superseded by NNNN` | Replaced; kept for the reasoning trail |
| `Deprecated` | No longer relevant (the thing it governed is gone) with no replacement |

A decision recorded after the fact says so: `Accepted (recorded retroactively on YYYY-MM-DD;
decided around YYYY-MM-DD in <source>)`.

## Template

Copy this into `docs/adr/NNNN-short-kebab-title.md`. The title states the decision, not the topic.

```markdown
# ADR NNNN: <the decision, stated as a short sentence>

## Status

Accepted — YYYY-MM-DD. <One line on implementation state if not obvious.>
<Supersedes / Amends / Superseded by lines, one each, if any.>

## Context

What forced a decision: the problem, the constraints, the facts that mattered. Short.

## Decision

What was decided, stated so it can be checked against the code.

## Alternatives considered

- **<Alternative>** — why it was rejected.

## Consequences

What becomes easier, what becomes harder, what is now ruled out, what risk is accepted.

## Verification

Where the decision is enforced or observable: files, tests, workflows, guards.

## Related

Issues, PRs, commits, other ADRs. For a deleted source document: `git show <sha>^:<path>`.
```

## Index

| # | Decision | Status |
|---|---|---|
| [0001](0001-remove-macos-native-extra.md) | No macOS-native (AppKit/PyObjC) runtime extra | Accepted; superseded in part by 0002 |
| [0002](0002-local-mode-trust-boundary-and-companion-app.md) | Local mode's trust boundary is the OS user account; a companion app replaces the agent as the sign-in channel | Accepted; superseded in part by 0003; amended by 0008, 0026, 0027, 0031 |
| [0003](0003-separated-installs-only.md) | Every shipped local-mode install is privilege-separated | Accepted |
| [0004](0004-retire-the-v1-auto-accept-config-model.md) | Retire the v1 auto-accept config model | Accepted |
| [0005](0005-moving-the-approval-decision-off-the-device.md) | Moving the approval decision off the device | Proposed |
| [0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) | Attribute a request to the AI system from the connection, with ranked provenance | Accepted; not implemented; amended by 0035 |
| [0007](0007-local-file-bridge.md) | Local file access crosses the privilege-separation boundary through the `.mcpb` shim | Accepted; extended by 0028 |
| [0008](0008-one-principal-per-os-user.md) | One principal per OS user, identified by the kernel | Accepted; implemented in part |
| [0009](0009-use-the-official-mcp-sdk.md) | Use the official `mcp` SDK for Streamable HTTP, not a hand-rolled transport | Accepted (retroactive) |
| [0010](0010-local-mode-serves-plain-http-on-localhost.md) | Local mode serves plain HTTP on `localhost`, not HTTPS with a self-signed certificate | Accepted (retroactive) |
| [0011](0011-org-mode-runs-its-own-oauth-authorization-server.md) | Org mode runs its own OAuth authorization server | Accepted (retroactive) |
| [0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md) | A `.mcpb` stdio shim connects Claude Desktop to local mode with zero hand-configuration | Accepted (retroactive) |
| [0013](0013-no-mcp-tool-mints-a-sign-in-credential.md) | No MCP tool may mint a sign-in credential | Accepted (retroactive) |
| [0014](0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md) | Every bespoke settings POST route is classified sensitive/exempt, or the app refuses to start | Accepted (retroactive) |
| [0015](0015-unattended-session-flag-is-advisory-only.md) | The self-declared unattended-session flag is advisory and never authorizes | Accepted (retroactive) |
| [0016](0016-org-config-bundle-hash-log-and-signing.md) | Org-config bundle integrity has two independent layers: a startup hash log and TOFU-pinned signing | Accepted (retroactive) |
| [0017](0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md) | Org-mode downloads: the approval gate is the privacy boundary, staging a bounded cost | Accepted (retroactive) |
| [0018](0018-linux-ships-a-self-contained-deb-built-with-pyinstaller.md) | Linux local mode ships a self-contained `.deb` built with PyInstaller | Accepted (retroactive) |
| [0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md) | Live-connector test credentials live only on a project-owned self-hosted runner | Accepted (retroactive) |
| [0020](0020-pypi-publishing-uses-oidc-trusted-publisher-only.md) | PyPI/TestPyPI publishing uses OIDC Trusted Publisher only | Accepted (retroactive) |
| [0021](0021-release-tag-push-never-uses-github-token.md) | The release tag is never pushed with `GITHUB_TOKEN` | Accepted (retroactive) |
| [0022](0022-one-release-tag-per-commit.md) | One release tag per commit; a stuck release is fixed forward | Accepted (retroactive) |
| [0023](0023-changelog-is-the-only-source-of-release-notes.md) | `CHANGELOG.md` is the only source of stable release notes, and rendering fails loudly | Accepted (retroactive) |
| [0024](0024-pre-releases-are-publicly-downloadable.md) | Pre-releases are publicly downloadable through the download Worker | Accepted (retroactive) |
| [0025](0025-no-certified-security-framework.md) | No certified security framework (ISO 27001/BCP/SLA): a risk-acceptance posture | Accepted (retroactive) |
| [0026](0026-the-companion-manages-the-daemon-through-the-service-manager.md) | The companion manages the daemon through the platform's service manager, one elevation prompt per action | Accepted |
| [0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md) | A service-group member cannot take over another member's companion socket | Accepted; amended by 0029 |
| [0028](0028-clients-without-the-shim-get-capability-urls.md) | Clients without the shim move files through single-use capability URLs | Accepted |
| [0029](0029-the-layout-step-never-re-owns-a-socket.md) | The installers' layout step never re-owns a socket | Accepted |
| [0030](0030-preflight-dispatches-build-yml-before-tagging.md) | Cutting a release first dispatches `build.yml` on the untagged commit | Accepted |
| [0031](0031-clicking-privacyfence-opens-approvals-through-the-companion.md) | Clicking PrivacyFence opens Approvals through the companion | Accepted |
| [0032](0032-org-settings-share-locals-generic-action-dispatcher.md) | Org mode's settings writes route through local mode's generic action dispatcher | Accepted |
| [0033](0033-one-route-layer-per-surface-with-an-auth-adapter-per-mode.md) | One route layer per surface (approvals, settings), with an auth adapter per mode, not a second module | Accepted |
| [0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md) | Sensitive settings writes require WebAuthn step-up in both local and org mode | Accepted |
| [0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md) | Agent attribution reads the handshake on every call; org mode attests only admin-pinned OAuth clients | Accepted; not implemented |
| [0036](0036-gmail-draft-signature-is-shown-but-not-write-scanned.md) | A Gmail draft's appended signature is shown in the approval popup but not write-scanned | Accepted |
