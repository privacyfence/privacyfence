# PrivacyFence documentation

This directory documents PrivacyFence as it works in the current source tree. It has two parts:
the **user and operator docs**, which are published on `privacyfence.eu/docs/`, and the
**contributor docs**, which stay on GitHub. The source code, build scripts, configuration examples
and CI workflows are the source of truth; a doc that disagrees with them is a bug.

## User and operator docs

### Install and first steps

- [`getting-started.md`](getting-started.md) — what you need, which install fits, your first
  approval, and troubleshooting shared by every platform.
- [`install-macos.md`](install-macos.md), [`install-windows.md`](install-windows.md),
  [`install-linux.md`](install-linux.md) — install, connect Claude Desktop and Claude Code,
  troubleshoot, uninstall. One page per platform.
- [`platform-support.md`](platform-support.md) — supported OS versions and architectures, where
  PrivacyFence keeps its data, logs, and start/stop commands.
- [`connecting-a-service.md`](connecting-a-service.md) — connecting Gmail, Slack and the other
  services from Settings, and what each connector status means.

### Using PrivacyFence

- [`how-it-works.md`](how-it-works.md) — the daemon, the companion app, how AI clients connect,
  and the `privacyfence_*` tools every client sees.
- [`approvals-and-policy.md`](approvals-and-policy.md) — how requests are gated, approval cards,
  the PII check, "Always allow" and policy rules, the privacy filter, file previews and
  notifications.
- [`configuration-reference.md`](configuration-reference.md) — every `settings.yaml` key and every
  organization-bundle option, with its default.

### Organization deployment and security

- [`org-mode-setup-guide.md`](org-mode-setup-guide.md) — deploy PrivacyFence centrally for an
  organization, including claude.ai as a client.
- [`security-and-compliance.md`](security-and-compliance.md) — trust boundaries, step-up
  authentication, privilege separation, audit integrity, and what PrivacyFence does not claim.

### Connector setup

Registering the OAuth app (or bot) each service needs, for a local install or an organization
bundle:

- [`google-cloud-setup.md`](google-cloud-setup.md) — Gmail, Drive, Docs, Sheets, Calendar,
  Contacts, Tasks and Apps Script
- [`slack-setup.md`](slack-setup.md)
- [`salesforce-setup.md`](salesforce-setup.md)
- [`atlassian-setup.md`](atlassian-setup.md) — Jira and Confluence
- [`telegram-setup.md`](telegram-setup.md)

### Reference appendices

- [`tools-reference.md`](tools-reference.md) — every connector tool and how it is gated
  (generated).
- [`always-allow-rules-reference.md`](always-allow-rules-reference.md) — what "Always allow"
  proposes, tool by tool (generated).
- [`pii-detection-keywords.md`](pii-detection-keywords.md) — what the PII detector looks for.

Release history is in [`../CHANGELOG.md`](../CHANGELOG.md).

## Contributor docs

How PrivacyFence is built, tested and released. For people changing the code; not published on
privacyfence.eu.

- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — how to propose a change: issues, forks, pull
  requests, license.
- [`../CLAUDE.md`](../CLAUDE.md) — release mechanics and branch hygiene: cutting a tag, release
  notes, branch naming, `releases/*`, worktrees.
- [`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) — code and test
  conventions, adding a connector, and the definition of done for a pull request (§2.7).
- [`dev-vs-live-setup.md`](dev-vs-live-setup.md) — running PrivacyFence from source without
  clashing with a packaged install.
- [`testing-policy.md`](testing-policy.md) — the test layers, which workflow runs which layer, and
  where.
- [`release-testing.md`](release-testing.md) — the release gates and the manual checks every
  release still needs.
- [`packaging.md`](packaging.md) — how the DMG, `.pkg`, `.mcpb`, Windows installer and `.deb` are
  built, signed and installed.
- [`connector-qa.md`](connector-qa.md) — QA accounts and seed data, the self-hosted live-check
  runner, recorded fixtures, and exploratory connector QA.
- [`downloads-and-release-kpi.md`](downloads-and-release-kpi.md) — the R2 release archive, the
  `downloads.privacyfence.eu` Worker, and how downloads are counted.
- [`images/screenshots/README.md`](images/screenshots/README.md) — how the documentation
  screenshots are produced.
- [`ai-client-support-plan.md`](ai-client-support-plan.md) — the open plan for ChatGPT and Gemini
  support and the website's AI agents pages; deleted when its work lands.
- [`adr/README.md`](adr/README.md) — Architecture Decision Records: why things are the way they
  are, and what was rejected.

CI and build behavior is defined in `.github/workflows/`, `pyproject.toml`, `tests/` and `scripts/`.

## Documentation principles

Every doc in this directory except the ADRs follows these rules.

1. **Current behavior only.** A doc says what the code does today. History has exactly two
   homes: [`../CHANGELOG.md`](../CHANGELOG.md) (what changed, per release) and [`adr/`](adr/README.md)
   (why, and what was rejected). No phase names, no issue or finding IDs, no version qualifiers
   ("as of 4.2"), and no "used to", "no longer", "formerly" or "now" framing.
   `tests/unit/test_docs_no_history.py` checks the published and contributor docs for the common
   shapes.
2. **One topic, one home.** Each fact lives in one doc; other docs link to it.
3. **Written for the reader, not the implementer.** User and operator docs name what people see
   and type: menu items, commands, settings keys, paths. Function and class names belong in
   contributor docs and code.
4. **Checked against the code, and kept that way by tests** wherever a table can be generated or
   a claim asserted: [`tools-reference.md`](tools-reference.md) and
   [`always-allow-rules-reference.md`](always-allow-rules-reference.md) are generated, the
   configuration reference is checked against `settings.yaml.example`, the install pages against
   the support matrix, and every `docs/…md` path named anywhere in the repository must exist.
5. **Every limit and default stated** with its real value: sizes, timeouts, defaults that differ
   between the code and the seeded `settings.yaml`, supported OS versions and architectures.

ADRs are the one exception to rule 1: they are frozen records and are not rewritten (see
[`adr/README.md`](adr/README.md)).

When behavior changes, update the doc that owns it in the same pull request.
