# PrivacyFence documentation

This directory documents PrivacyFence as it works in the current source tree. It has two parts:
the **user and operator docs**, which are published on `privacyfence.eu/docs/`, and the
**contributor docs**, which stay on GitHub. The source code, build scripts, configuration examples
and CI workflows are the source of truth; a doc that disagrees with them is a bug.

## User and operator docs

### Install and first steps

- [`getting-started.md`](getting-started.md) — what you need, which deployment fits, your first
  approval, and troubleshooting.
- [`connecting-a-service.md`](connecting-a-service.md) — the Admin page, connecting Gmail, Slack and
  the other services, and what each connector status means.
- [`moving-to-organization-mode.md`](moving-to-organization-mode.md) — why 8 runs only in organization mode,
  the two ways forward, and what to do before upgrading to 8.x.

### Using PrivacyFence

- [`how-it-works.md`](how-it-works.md) — the server, how AI clients connect,
  and the `privacyfence_*` tools every client sees.
- [`approvals-and-policy.md`](approvals-and-policy.md) — how requests are gated, approval cards,
  the PII check, "Always allow" and policy rules, the privacy filter, file previews and
  notifications.
- [`configuration-reference.md`](configuration-reference.md) — every `settings.yaml` key and every
  organization-bundle option, with its default.
- [`privacyfence-sync.md`](privacyfence-sync.md) — run a scheduled data sync as a recipe, with no AI
  model involved: install, connecting, the rules it needs, and the recipe-writing plugin.

### Organization deployment and security

- [`org-mode-setup-guide.md`](org-mode-setup-guide.md) — deploy PrivacyFence centrally for an
  organization, including claude.ai as a client.
- [`org-mode-docker.md`](org-mode-docker.md) — run the organization deployment as containers: the
  published image, a tested Compose file with Caddy, upgrades, backups and verifying the image.
- [`upstream-mcp.md`](upstream-mcp.md) — put your own MCP server behind PrivacyFence: declare its tools
  and gates, map Workspace groups to audiences, keep membership current, and rotate the claim key.
- [`identity-claim.md`](identity-claim.md) — the signed claim PrivacyFence sends an upstream MCP server:
  its fields, how it is bound to one tool call and what an upstream must verify; read it when you write or
  review an upstream's verifier.
- [`n8n-node.md`](n8n-node.md) — the PrivacyFence node for n8n: install the community node, the
  credential, calling a tool from a workflow or an AI Agent, and the delivery switch.
- [`oidc-provider.md`](oidc-provider.md) — let Peek and Grist (or other apps you name) sign people in
  through PrivacyFence, with the identity PrivacyFence already has.
- [`security-and-compliance.md`](security-and-compliance.md) — trust boundaries, step-up
  authentication, audit integrity, and what PrivacyFence does not claim.
- [`oauth-relay.md`](oauth-relay.md) — the OAuth redirect relay at `oauth.privacyfence.eu`: what it
  forwards and stores, and how the maintainers run, check and revoke it.

### Connector setup

Registering your organization's own OAuth app (or bot) for a service, in place of the built-in one,
as a signed connector config or a bundle section on an organization server:

- [`google-cloud-setup.md`](google-cloud-setup.md) — Gmail, Drive, Docs, Sheets, Calendar,
  Contacts, Tasks and Apps Script
- [`slack-setup.md`](slack-setup.md)
- [`salesforce-setup.md`](salesforce-setup.md)
- [`atlassian-setup.md`](atlassian-setup.md) — Jira, Confluence and Jira Assets
- [`telegram-setup.md`](telegram-setup.md)
- [`grist-setup.md`](grist-setup.md)
- [`peek-setup.md`](peek-setup.md)
- [`miro-setup.md`](miro-setup.md)

### AI agent setup

Connecting each AI client to a single-user Docker install or an organization deployment:

- [`connect-claude-desktop.md`](connect-claude-desktop.md) — Claude Desktop, as a custom connector
- [`connect-claude-code.md`](connect-claude-code.md) — Claude Code, through OAuth sign-in
- [`connect-claude-ai.md`](connect-claude-ai.md) — claude.ai, through an organization deployment
- [`connect-chatgpt.md`](connect-chatgpt.md) — ChatGPT (Developer Mode), through an
  organization deployment
- [`connect-gemini-enterprise.md`](connect-gemini-enterprise.md) — Gemini Enterprise, through an
  organization deployment

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

- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — how to propose a change: issues, forks, branches, pull
  requests, license.
- [`releasing.md`](releasing.md) — versioning, cutting a tag, release notes, release gating,
  the organization image and the R2 release archive.
- [`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) — code and test
  conventions, adding a connector, and the definition of done for a pull request (§2.7).
- [`dev-vs-live-setup.md`](dev-vs-live-setup.md) — running PrivacyFence from source without
  clashing with a Docker install.
- [`testing-policy.md`](testing-policy.md) — the test layers, which workflow runs which layer, and
  where.
- [`release-testing.md`](release-testing.md) — the release gates and the manual checks every
  release still needs.
- [`connector-qa.md`](connector-qa.md) — QA accounts and seed data, the self-hosted live-check
  runner, recorded fixtures, and exploratory connector QA.
- [`ai-client-qa.md`](ai-client-qa.md) — the manual check of each real AI client: the test
  organization deployment, the per-client script, the evidence to record, and the results table.
- [`release-archive.md`](release-archive.md) — the R2 release archive, the Cloudflare resources and
  the credentials that publish to it.
- [`images/screenshots/README.md`](images/screenshots/README.md) — how the documentation
  screenshots are produced.
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
   configuration reference is checked against `settings.yaml.example`, and every `docs/…md` path named anywhere in the repository must exist.
5. **Every limit and default stated** with its real value: sizes, timeouts, defaults that differ
   between the code and the seeded `settings.yaml`, supported OS versions and architectures.

ADRs are the one exception to rule 1: they are frozen records and are not rewritten (see
[`adr/README.md`](adr/README.md)).

When behavior changes, update the doc that owns it in the same pull request.
