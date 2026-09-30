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
| **Reference** | How does it work, or how do I do X, today? | `docs/*.md`, `CONTRIBUTING.md`, code docstrings | Living. Edited in the same PR as the behavior. Links here for the *why* instead of retelling it |

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
| [0004](0004-retire-the-v1-auto-accept-config-model.md) | Retire the v1 auto-accept config model | Accepted; superseded in part by 0041 |
| [0005](0005-moving-the-approval-decision-off-the-device.md) | Moving the approval decision off the device | Proposed |
| [0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) | Attribute a request to the AI system from the connection, with ranked provenance | Accepted; implemented; amended by 0035, 0037 |
| [0007](0007-local-file-bridge.md) | Local file access crosses the privilege-separation boundary through the `.mcpb` shim | Accepted; extended by 0028 |
| [0008](0008-one-principal-per-os-user.md) | One principal per OS user, identified by the kernel | Accepted; implemented in part; amended by 0043, 0104 |
| [0009](0009-use-the-official-mcp-sdk.md) | Use the official `mcp` SDK for Streamable HTTP, not a hand-rolled transport | Accepted (retroactive) |
| [0010](0010-local-mode-serves-plain-http-on-localhost.md) | Local mode serves plain HTTP on `localhost`, not HTTPS with a self-signed certificate | Accepted (retroactive) |
| [0011](0011-org-mode-runs-its-own-oauth-authorization-server.md) | Org mode runs its own OAuth authorization server | Accepted (retroactive) |
| [0012](0012-mcpb-shim-connects-claude-desktop-to-local-mode.md) | A `.mcpb` stdio shim connects Claude Desktop to local mode with zero hand-configuration | Accepted (retroactive) |
| [0013](0013-no-mcp-tool-mints-a-sign-in-credential.md) | No MCP tool may mint a sign-in credential | Accepted (retroactive) |
| [0014](0014-every-bespoke-route-is-classified-or-the-app-refuses-to-start.md) | Every bespoke settings POST route is classified sensitive/exempt, or the app refuses to start | Accepted (retroactive) |
| [0015](0015-unattended-session-flag-is-advisory-only.md) | The self-declared unattended-session flag is advisory and never authorizes | Accepted (retroactive) |
| [0016](0016-org-config-bundle-hash-log-and-signing.md) | Org-config bundle integrity has two independent layers: a startup hash log and TOFU-pinned signing | Accepted (retroactive) |
| [0017](0017-org-mode-downloads-the-approval-gate-is-the-privacy-boundary.md) | Org-mode downloads: the approval gate is the privacy boundary, staging a bounded cost | Accepted (retroactive); amended by 0092 |
| [0018](0018-linux-ships-a-self-contained-deb-built-with-pyinstaller.md) | Linux local mode ships a self-contained `.deb` built with PyInstaller | Accepted (retroactive); amended by 0039, 0044 |
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
| [0035](0035-agent-attribution-reads-client-params-per-call-and-org-pins-are-admin-set.md) | Agent attribution reads the handshake on every call; org mode attests only admin-pinned OAuth clients | Accepted |
| [0036](0036-card-copy-names-the-caller-through-one-placeholder.md) | Card copy names the caller through one placeholder, filled from the verified name or "the AI system"; connectors never learn who is asking | Accepted (retroactive) |
| [0037](0037-a-local-override-is-a-relabel-and-never-attests.md) | A local `agent_overrides:` match is a relabel recorded as claimed on every install; local mode has no attested source | Accepted |
| [0038](0038-gmail-draft-signature-is-shown-but-not-write-scanned.md) | A Gmail draft's appended signature is shown in the approval popup but not write-scanned | Accepted |
| [0039](0039-installers-refuse-an-os-below-the-support-matrix.md) | Every installer refuses an OS below the support matrix's floor | Accepted |
| [0040](0040-telegram-app-credentials-ship-in-every-distribution.md) | Telegram app credentials ship in every distribution, including the PyPI sdist/wheel | Accepted |
| [0041](0041-only-the-current-install-layout-is-supported.md) | Only the current install layout is supported; there is no upgrade path from earlier layouts | Accepted; amended by 0047 |
| [0042](0042-uninstall-replaces-disable.md) | `uninstall [--purge]` replaces `disable`; uninstalling keeps the data, purging deletes it | Accepted |
| [0043](0043-the-recorded-owner-is-never-rewritten.md) | The marker's recorded owner is written once and never rewritten by adding another account | Accepted |
| [0044](0044-the-deb-declares-only-the-architectures-ci-builds.md) | The `.deb` declares only the architectures CI builds and tests | Accepted |
| [0045](0045-the-windows-installer-ends-its-own-processes-and-force-closes-the-rest.md) | The Windows installer ends its own processes, and RestartManager force-closes whatever is left | Accepted |
| [0046](0046-release-ci-pins-codesigntool-by-version-and-sha256.md) | Release CI pins eSigner CodeSignTool to one version and its SHA-256, not "latest" | Accepted |
| [0047](0047-settings-an-earlier-release-converted-are-cleaned-up-not-refused.md) | v1 policy sections an earlier release already converted are removed at startup, not refused | Accepted |
| [0048](0048-every-crawler-is-allowed-including-ai-training.md) | privacyfence.eu allows every crawler, AI search and AI training included | Accepted |
| [0049](0049-website-css-is-plain-modern-css-without-a-framework.md) | The website's CSS is plain modern CSS, with no framework | Accepted |
| [0050](0050-website-analytics-is-ga4-behind-consent.md) | Website analytics is GA4, loaded only after consent; Google is the only search tooling | Accepted |
| [0051](0051-privacyfence-eu-publishes-the-user-and-operator-docs-only.md) | privacyfence.eu publishes the user and operator docs only; links out of that set go to GitHub at the same tag | Accepted |
| [0052](0052-docs-are-built-with-zensical-from-the-latest-stable-tag.md) | The docs are built with Zensical, from the latest stable release tag | Accepted |
| [0053](0053-the-website-is-hosted-on-github-pages-behind-cloudflare.md) | The website is hosted on GitHub Pages, behind the Cloudflare proxy | Accepted |
| [0054](0054-the-windows-installer-refuses-anything-but-native-x64.md) | The Windows installer refuses anything but a native x64 Windows, Windows 11 on arm64 included | Accepted |
| [0055](0055-step-up-passkey-enrollment-accepts-any-authenticator.md) | Step-up passkey enrollment accepts any authenticator: built in, security key or phone; user verification stays required | Accepted |
| [0056](0056-code-carries-no-project-history.md) | Code carries no project history; a blocking test enforces it | Accepted |
| [0057](0057-stable-tags-are-gated-on-graphical-session-coverage.md) | Stable tags are gated on graphical-session coverage; pre-release tags only report it | Accepted (retroactive) |
| [0058](0058-nothing-runs-elevated-unless-only-an-administrator-can-rewrite-it.md) | Nothing runs elevated unless only an administrator can rewrite it | Accepted (retroactive) |
| [0059](0059-the-windows-daemon-runs-as-its-own-virtual-service-account.md) | The Windows daemon runs as its own virtual service account | Accepted (retroactive) |
| [0060](0060-the-system-root-override-is-ignored-on-a-real-install.md) | The system-root override is ignored on a real install | Accepted (retroactive) |
| [0061](0061-the-mcp-token-and-the-browser-session-are-audience-separated.md) | The MCP token and the browser session are audience-separated | Accepted (retroactive) |
| [0062](0062-only-a-companion-attested-session-may-approve.md) | Only a companion-attested session may approve | Accepted (retroactive) |
| [0063](0063-the-web-ui-csp-uses-per-response-nonces-not-unsafe-inline.md) | The web UI's CSP uses nonces, not `'unsafe-inline'` | Accepted (retroactive) |
| [0064](0064-browser-notifications-stay-on-the-machine.md) | Browser notifications stay on the machine | Accepted (retroactive); amended by 0081 |
| [0065](0065-approving-from-the-list-is-a-batch-bound-to-one-step-up-assertion.md) | Approving from the list is a batch bound to one step-up assertion | Accepted (retroactive) |
| [0066](0066-step-up-falls-back-by-mode-and-require-passkey-closes-the-fallback.md) | Step-up falls back by mode, and `require_passkey` closes the fallback | Accepted (retroactive) |
| [0067](0067-the-default-step-up-scope-is-writes-and-pii-reads.md) | The default step-up scope is writes and PII-flagged reads | Accepted (retroactive) |
| [0068](0068-step-up-is-turned-on-from-the-ui-and-off-only-by-a-config-edit.md) | Step-up is turned on from the UI and off only by a config edit | Accepted (retroactive) |
| [0069](0069-require-passkey-with-nothing-enrolled-starts-and-releases-nothing.md) | With `require_passkey` on and nothing enrolled, the daemon starts and releases nothing step-up covers | Accepted (retroactive) |
| [0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md) | Enabling a connector is a sensitive action, and disabling one is not | Accepted (retroactive) |
| [0071](0071-audit-log-integrity-is-a-keyed-hash-chain-plus-off-host-forwarding.md) | Audit-log integrity is a keyed hash chain on the host, plus off-host forwarding | Accepted (retroactive) |
| [0072](0072-org-mode-persists-only-sealed-refresh-tokens.md) | Org mode persists only refresh tokens, each sealed to its bearer | Accepted (retroactive) |
| [0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md) | An approved write is single-use; an approved read replays within the ledger TTL | Accepted (retroactive) |
| [0074](0074-auto-accept-rules-are-identified-by-a-content-derived-id.md) | Auto-accept rules are identified by a content-derived id, and decisions are attributed by it | Accepted (retroactive) |
| [0075](0075-apps-script-gets-no-run-tool.md) | The Apps Script connector has no tool that runs a script | Accepted (retroactive) |
| [0076](0076-every-connector-tool-is-advertised-read-only.md) | Every connector tool is advertised to MCP clients as read-only | Superseded by 0086 |
| [0077](0077-the-approval-popup-never-proposes-a-rule-for-the-extra-scope-operations.md) | The approval popup never proposes a rule for the extra-scope operations (Apps Script, Gmail filters, Slack group chats) | Accepted (retroactive) |
| [0078](0078-the-app-shares-the-websites-design-system.md) | The app shares the website's design system, instead of adopting a CSS framework | Accepted |
| [0079](0079-the-apps-visual-language-is-the-websites.md) | The app's visual language is the website's, with a dark mode derived from it | Accepted |
| [0080](0080-pdf-previews-are-rasterised-on-the-server.md) | PDF previews are rasterised on the server for narrow screens, instead of pdf.js or a new route | Accepted |
| [0081](0081-org-mode-sends-a-count-only-web-push.md) | Org mode sends a count-only, padded web push through the browser's push service, with an org-wide off switch; local mode sends none | Accepted |
| [0082](0082-org-mode-form-action-allows-self-and-sign-out-stays-on-the-app.md) | Org mode's `form-action` is `'self'`, and a sign-out lands on the app's own page; local mode keeps `'none'` | Accepted |
| [0083](0083-a-humans-deny-note-reaches-the-agent-as-delimited-sanitized-user-text.md) | A human's deny note reaches the agent as delimited, sanitized user text, on both delivery paths, unfiltered | Accepted |
| [0084](0084-the-audit-log-records-that-a-deny-had-feedback-never-the-text.md) | The audit log records that a deny had feedback (intent, note length), never the feedback text | Accepted |
| [0085](0085-ci-installs-ai-client-clis-from-a-committed-lockfile.md) | CI installs AI-client CLIs from a committed lockfile; only the weekly canary runs `@latest` | Accepted |
| [0086](0086-tool-annotations-are-truthful-by-default.md) | Tool annotations are truthful by default; an organization bundle or a local connection can ask for every tool read-only | Accepted; superseded in part by 0089 |
| [0087](0087-two-claude-desktop-extensions-ship-in-the-dmg-and-the-windows-installer.md) | Two Claude Desktop extensions ship in the DMG and the Windows installer | Superseded by 0089 |
| [0088](0088-local-mode-shows-every-requester-as-undetected.md) | Local mode shows every requester as "Undetected" | Accepted |
| [0089](0089-tool-annotations-are-always-truthful.md) | Tool annotations are always truthful; the all-read-only mode and the no-prompts extension are removed | Accepted |
| [0090](0090-a-reused-denial-says-it-is-an-earlier-decision.md) | A reused denial says it is an earlier decision | Accepted |
| [0091](0091-the-configured-step-up-scope-is-a-minimum-each-person-can-widen.md) | The configured step-up scope is a minimum each person can widen | Accepted |
| [0092](0092-the-inline-download-limit-caps-the-tool-result-at-100000-bytes.md) | The inline download limit caps the tool result, at 100,000 bytes by default | Accepted; amended by 0097 |
| [0093](0093-a-pending-approval-releases-nothing.md) | A pending approval releases nothing, and one request_id ties its audit trail together | Accepted |
| [0094](0094-claude-clients-are-matched-by-their-observed-names.md) | Claude clients are matched by the names they were seen sending, and an unrecognised DCR name yields to a recognised handshake name | Accepted |
| [0095](0095-gmail-signature-cid-images-are-copied-from-sent-mail.md) | A Gmail signature's `cid:` images are copied from the user's own sent mail | Accepted |
| [0096](0096-gmail-get-message-returns-html-only-when-asked.md) | `gmail_get_message` returns the HTML body only when asked, under the same `body` policy | Accepted |
| [0097](0097-a-download-link-needs-a-client-that-can-reach-the-server.md) | A download link needs a client that can reach the server; `drive_get_file_content` returns a document's text | Accepted |
| [0098](0098-each-ai-agent-has-its-own-setup-doc-and-website-page.md) | Each AI agent has its own setup doc and its own website page, counted as the `ai-agent` content group | Accepted (retroactive) |
| [0099](0099-an-ai-agents-website-page-lands-after-the-release-that-carries-its-doc.md) | An AI agent's website page lands after the stable release whose `/docs/` carries its setup doc | Accepted (retroactive) |
| [0100](0100-unverified-client-setup-instructions-never-merge-to-main.md) | Unverified AI-client setup instructions never merge to `main` | Accepted (retroactive) |
| [0101](0101-the-website-shows-no-third-party-logos.md) | The website shows no third-party logos; AI agents and connectors are named in text | Accepted (retroactive) |
| [0102](0102-an-upload-slot-is-consumed-after-the-gate-not-before.md) | An upload slot is consumed after the gate, not before it | Accepted |
| [0103](0103-chatgpt-is-matched-by-its-registered-name.md) | ChatGPT is matched by the name it registers with, and the `openai-mcp` guess is removed | Accepted |
| [0104](0104-the-companion-copies-the-ai-client-token.md) | The companion copies the AI client token to the clipboard, with no passkey; Settings reaches it only through the companion | Accepted |
| [0105](0105-a-google-read-is-retried-once-when-its-connection-drops.md) | A Google API read is retried once when its connection drops; PrivacyFence never retries a write | Accepted |
| [0106](0106-google-not-found-and-not-shared-errors-reach-the-agent.md) | A Google not-found or not-shared answer reaches the AI client as a fixed message naming the connected account; every other Google error stays generic | Accepted |
| [0107](0107-gemini-cli-and-antigravity-are-not-supported-clients.md) | Gemini CLI, Antigravity and the Gemini app are not supported AI clients; Gemini support means Gemini Enterprise | Accepted |
| [0108](0108-oauth-sign-in-popups-keep-their-opener-and-basic-clients-need-not-repeat-their-id.md) | An OAuth sign-in popup keeps its opener, and an HTTP Basic client need not repeat its client ID in the body | Accepted |
| [0109](0109-gemini-enterprise-is-matched-by-its-admin-registered-name.md) | Gemini Enterprise is matched by the name its admin registers it with | Accepted |
| [0110](0110-gemini-enterprise-is-registered-through-the-existing-register-endpoint.md) | Gemini Enterprise is registered through the existing `/register` endpoint; no pre-registration command | Accepted |
| [0111](0111-a-duplicate-release-run-cancels-itself.md) | A duplicate release run cancels itself | Accepted |
| [0112](0112-stable-releases-are-listed-on-the-mcp-registry-with-the-mcpb.md) | Stable releases are listed on the official MCP registry, with the `.mcpb` attached to the GitHub Release | Accepted |
| [0113](0113-process-docs-live-in-docs-not-in-claude-md.md) | Project process documentation lives in `docs/` and `CONTRIBUTING.md`, not in `CLAUDE.md` | Accepted |
| [0114](0114-the-glama-listing-runs-a-tool-catalog-not-a-hosted-privacyfence.md) | The Glama listing runs a credential-free tool catalog, not a hosted PrivacyFence | Accepted |
| [0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md) | Tool definitions carry their parameter, return and routing guidance in prose, and a test enforces it | Accepted |
| [0116](0116-list-tools-page-inside-the-client-and-keep-their-return-shape.md) | List and search tools page inside the client up to `max_results` and keep their bare-list return shape | Accepted |
| [0117](0117-atlassian-account-ids-resolve-through-a-lazy-shared-cache.md) | Atlassian account ids resolve through a lazy cache that Jira and Confluence share | Accepted |
| [0118](0118-an-approver-sees-atlassians-name-for-every-mentioned-or-assigned-account.md) | An approver sees Atlassian's name for every mentioned or assigned account | Accepted |
| [0119](0119-atlassian-find-users-is-auto-approved-and-returns-no-email.md) | `jira_find_users` and `confluence_find_users` are auto-approved and return no email | Accepted |
