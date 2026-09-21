# PrivacyFence documentation

This directory documents PrivacyFence as it works in the current source tree. Runtime code, build scripts, configuration examples, and CI workflows are the source of truth when behavior changes.

## Start here

- [`getting-started.md`](getting-started.md) — install PrivacyFence step by step on macOS, Windows, or Debian/Ubuntu, connect an MCP client, and finish first-run setup.
- [`migration-guide.md`](migration-guide.md) — upgrading an install from 4.0 or earlier: what the mandatory privilege-separation/step-up hardening needs beyond a plain package upgrade.
- [`../CHANGELOG.md`](../CHANGELOG.md) — release history. The history this directory deliberately doesn't carry (see "Documentation rules" below) lives there.
- [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md) — architecture, runtime, MCP transport, configuration, state, approvals, connectors, audit logging, and packaging.
- [`security-and-compliance.md`](security-and-compliance.md) — security boundaries, authentication, authorization, privacy controls, audit integrity, and deployment considerations.
- [`testing-policy.md`](testing-policy.md) — test layers, CI execution, live-provider checks, and what remains manual.
- [`downloads-and-release-kpi.md`](downloads-and-release-kpi.md) — how a release reaches a user and how installer downloads are counted (the `downloads.privacyfence.eu` Worker, release manifests, and the Cloudflare resources and credentials behind them).

## User and operator guides

- [`org-mode-setup-guide.md`](org-mode-setup-guide.md) — deploy and configure centralized org mode.
- [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md) — backup, restore, upgrades, restart behavior, availability, and operations.
- [`org-mode-download-delivery.md`](org-mode-download-delivery.md) — org-mode inline and staged file delivery.
- [`platform-support.md`](platform-support.md) — macOS, Windows, and Linux packaging/support matrix, including currently-known open items.
- [`dev-vs-live-setup.md`](dev-vs-live-setup.md) — isolate source-development and packaged installations.
- [`release-testing.md`](release-testing.md) — current release validation that still requires a human.

## Approval, policy, and data handling

- [`always-allow-rules-reference.md`](always-allow-rules-reference.md) — standing-rule behavior and supported rule shapes.
- [`approval-list-ui-ux.md`](approval-list-ui-ux.md) — current approval-list interaction model.
- [`approval-window-content-reference.md`](approval-window-content-reference.md) — current approval-card and confirmation content.
- [`claude-knowledge-boundary.md`](claude-knowledge-boundary.md) — what MCP clients can know before and after approval.
- [`file-type-support.md`](file-type-support.md) — attachment preview, extraction, and PII-scan support.
- [`pii-detection-keywords.md`](pii-detection-keywords.md) — PII detector categories, patterns, and language-specific keywords.

The primary runtime modules for this area are `src/privacyfence/gate.py`, `approvals.py`, `auto_accept.py`, `privacy_filter.py`, `pii_detector.py`, `text_extraction.py`, and `src/privacyfence/web/`.

## Connector setup

- [`google-cloud-setup.md`](google-cloud-setup.md)
- [`slack-setup.md`](slack-setup.md)
- [`salesforce-setup.md`](salesforce-setup.md)
- [`atlassian-setup.md`](atlassian-setup.md)
- [`telegram-setup.md`](telegram-setup.md)

Connector implementation lives under `src/privacyfence/connectors/`; daemon construction and per-principal connector lifecycle are in `daemon_main.py`, `connector_host.py`, and `connector_registry.py`.

## Development and QA

- [`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) — coding and test-writing expectations.
- [`testing-policy.md`](testing-policy.md) — what runs automatically and where.
- [`connector-live-check-setup.md`](connector-live-check-setup.md) — self-hosted live-provider runner setup.
- [`qa-environment-setup.md`](qa-environment-setup.md) — dedicated QA accounts and reusable seed data.
- [`connector-qa-testing.md`](connector-qa-testing.md) — extended connector/gate exploratory QA.
- [`release-testing.md`](release-testing.md) — manual release checks that automation cannot reliably judge.

CI and build behavior is defined in `.github/workflows/`, `pyproject.toml`, `tests/`, and `scripts/`.

## Architecture decisions and assets

- [`adr/0001-remove-macos-native-extra.md`](adr/0001-remove-macos-native-extra.md) — current decision that PrivacyFence has no AppKit/PyObjC runtime dependency.
- [`adr/0002-local-mode-trust-boundary-and-companion-app.md`](adr/0002-local-mode-trust-boundary-and-companion-app.md) — local mode's trust boundary is the OS user account, and the companion app that replaces the agent as the sign-in channel.
- [`adr/0003-separated-installs-only.md`](adr/0003-separated-installs-only.md) — every shipped local-mode install is privilege-separated, or it is not shipped and does not serve.
- [`adr/0004-retire-the-v1-auto-accept-config-model.md`](adr/0004-retire-the-v1-auto-accept-config-model.md) — one auto-accept model (scope + verb + condition) replaces `auto_accept_rules`/`auto_accept_grants`.
- [`adr/0005-moving-the-approval-decision-off-the-device.md`](adr/0005-moving-the-approval-decision-off-the-device.md) — **proposed, not accepted**: whether an approving decision should be rendered and answered somewhere the governed agent cannot execute code.
- [`adr/0006-attributing-a-request-to-the-ai-system-that-made-it.md`](adr/0006-attributing-a-request-to-the-ai-system-that-made-it.md) — which AI system made a request is resolved from the connection, recorded with the provenance that established it, and may inform a human without informing a decision.
- [`images/screenshots/README.md`](images/screenshots/README.md) — screenshot generation and maintenance.
- `images/` — diagrams and screenshots referenced by documentation.

## Documentation rules

Documentation in this directory is a standing reference, not a changelog. Describe what the current implementation does and the boundaries it currently has. Do not preserve completed implementation plans, migration narratives, phase names, or “before/after” history in standing docs. That history has a home: [`../CHANGELOG.md`](../CHANGELOG.md) at the repository root, where a change is recorded once, under the release that shipped it.

The exception is an active implementation plan, intentionally a live document while its tracked work remains open. There is no such plan today. Remove or convert each once its own work is complete rather than leaving it in `docs/`, keeping the parts still worth keeping: `automated-test-strategy-plan.md` was retired under exactly this rule once all fourteen of its phases had landed, with the core testing principle, the seven-layer taxonomy and the full list of what deliberately stays manual folded into [`testing-policy.md`](testing-policy.md) first; `release-publishing-kpi-plan.md` went the same way once its phases had shipped, folded into [`downloads-and-release-kpi.md`](downloads-and-release-kpi.md), with its one unimplemented phase moved to a tracking issue rather than left in `docs/` as a plan nobody was working. Standing, platform-specific open items that aren't phase-shaped implementation work belong in `platform-support.md`'s "Known open items" section instead of a dedicated plan doc.

When behavior changes, update the nearest standing reference in the same pull request. Prefer stable module, command, route, configuration-key, and workflow names over line numbers or historical pull-request identifiers.
