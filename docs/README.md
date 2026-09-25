# PrivacyFence documentation

This directory documents PrivacyFence as it works in the current source tree. Runtime code, build scripts, configuration examples, and CI workflows are the source of truth when behavior changes.

## Start here

- [`getting-started.md`](getting-started.md) — install PrivacyFence step by step on macOS, Windows, or Debian/Ubuntu, connect an MCP client, and finish first-run setup.
- [`migration-guide.md`](migration-guide.md) — upgrading an install from 4.0 or earlier: what the mandatory privilege-separation/step-up hardening needs beyond a plain package upgrade.
- [`../CHANGELOG.md`](../CHANGELOG.md) — release history. The history this directory deliberately doesn't carry (see "Documentation rules" below) lives there.
- [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md) — architecture, runtime, MCP transport, configuration, state, approvals, connectors, audit logging, and packaging.
- [`security-and-compliance.md`](security-and-compliance.md) — security boundaries, authentication, authorization, privacy controls, audit integrity, and deployment considerations.

## User and operator guides

- [`org-mode-setup-guide.md`](org-mode-setup-guide.md) — deploy and configure centralized org mode.
- [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md) — backup, restore, upgrades, restart behavior, availability, and operations.
- [`org-mode-download-delivery.md`](org-mode-download-delivery.md) — org-mode inline and staged file delivery.
- [`platform-support.md`](platform-support.md) — macOS, Windows, and Linux packaging/support matrix, including currently-known open items.

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

## Contributor documentation (GitHub only)

These describe how PrivacyFence is built, tested and released. They are for people changing the
code, and are not published on privacyfence.eu.

- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — how to propose a change: issues, forks, pull requests, license.
- [`../CLAUDE.md`](../CLAUDE.md) — release mechanics and branch hygiene: cutting a tag, release notes, branch naming, `releases/*`, worktrees.
- [`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) — code and test conventions, adding a connector, and the definition of done for a pull request (§2.7).
- [`dev-vs-live-setup.md`](dev-vs-live-setup.md) — running PrivacyFence from source without clashing with a packaged install.
- [`testing-policy.md`](testing-policy.md) — the test layers, which workflow runs which layer, and where.
- [`release-testing.md`](release-testing.md) — the release gates and the manual checks every release still needs.
- [`packaging.md`](packaging.md) — how the DMG, `.pkg`, `.mcpb`, Windows installer and `.deb` are built, signed and installed.
- [`connector-qa.md`](connector-qa.md) — QA accounts and seed data, the self-hosted live-check runner, recorded fixtures, and exploratory connector QA.
- [`downloads-and-release-kpi.md`](downloads-and-release-kpi.md) — the R2 release archive, the `downloads.privacyfence.eu` Worker, and how downloads are counted.
- [`images/screenshots/README.md`](images/screenshots/README.md) — how the documentation screenshots are produced.

CI and build behavior is defined in `.github/workflows/`, `pyproject.toml`, `tests/` and `scripts/`.

## Architecture decisions and assets

- [`adr/README.md`](adr/README.md) — Architecture Decision Records: the index of every recorded decision, when a decision needs one, the rules for keeping them, and the template. Standing docs here say what the code does; ADRs say why, and what was rejected.
- `images/` — diagrams and screenshots referenced by documentation.

## Documentation rules

Documentation in this directory is a standing reference, not a changelog. Describe what the current implementation does and the boundaries it currently has. Do not preserve completed implementation plans, migration narratives, phase names, or “before/after” history in standing docs. (ADRs are the deliberate exception: they are records, not standing docs, and are never rewritten to match today.) That history has a home: [`../CHANGELOG.md`](../CHANGELOG.md) at the repository root, where a change is recorded once, under the release that shipped it.

The exception is an active implementation plan, intentionally a live document while its tracked work remains open. There is no such plan today. Remove or convert each once its own work is complete rather than leaving it in `docs/`, keeping the parts still worth keeping — and before deleting it, record each decision it made (anything hard to reverse, touching a trust boundary or the release path, or rejecting an alternative for a non-obvious reason) as an ADR under [`adr/`](adr/README.md). A standing reference keeps the outcome; only an ADR keeps the rejected alternatives. Earlier retirements: `automated-test-strategy-plan.md` was retired once all fourteen of its phases had landed, with the core testing principle, the seven-layer taxonomy and the full list of what deliberately stays manual folded into [`testing-policy.md`](testing-policy.md) first; `release-publishing-kpi-plan.md` went the same way once its phases had shipped, folded into [`downloads-and-release-kpi.md`](downloads-and-release-kpi.md), with its one unimplemented phase moved to a tracking issue rather than left in `docs/` as a plan nobody was working. Standing, platform-specific open items that aren't phase-shaped implementation work belong in `platform-support.md`'s "Known open items" section instead of a dedicated plan doc.

When behavior changes, update the nearest standing reference in the same pull request. Prefer stable module, command, route, configuration-key, and workflow names over line numbers or historical pull-request identifiers.
