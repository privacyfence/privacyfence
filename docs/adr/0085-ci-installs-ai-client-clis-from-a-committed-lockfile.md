# ADR 0085: CI installs AI-client CLIs from a committed lockfile; only the weekly canary runs `@latest`

## Status

Accepted — 2026-09-27. Implemented for Claude Code; other clients are added to the same
`package.json`.

## Context

PrivacyFence's `/mcp` endpoint is only useful if the AI clients people actually run can connect to
it, and those clients change on their own schedule. Claude Code ships a new version most days. The
contract tests that existed before this decision (`test_mcp_daemon_contract.py`,
`test_shim_mcp_contract.py`) drive `/mcp` with the official Python `mcp` client or with our own
shim, never with a real vendor client, so a client-side change that broke the connection would
first be reported by a user.

A real client in CI needs its CLI on the runner. There are two ways to get it: install a fixed
version, or fetch whatever is newest at run time. They answer different questions. A fixed version
answers "did our change break a client?", which must be deterministic because it gates every PR. The
newest version answers "did the client's release break us?", which is not caused by the PR under
test and must not turn an unrelated PR red.

The CLI is third-party code executed on the runner, so how it is fetched is also a supply-chain
question.

## Decision

**`tests/integration/ai_clients/package.json` pins every AI-client CLI to an exact version, its
`package-lock.json` is committed, and CI installs them with `npm ci --prefix
tests/integration/ai_clients`. Only `.github/workflows/ai-client-canary.yml` runs a client at
`@latest`, through `npx --yes <package>@latest`.**

- The pinned CLIs run in `tests.yml`'s `test` job on every PR. The job names the installed binary in
  the test's override variable (`CLAUDE_CODE_BIN`), which makes the test fail, not skip, if it cannot
  run.
- The canary runs weekly and on dispatch, gates nothing, and on failure opens or updates one issue
  per client.
- A pin moves by a reviewed change to both files, by hand or through Dependabot's weekly
  `/tests/integration/ai_clients` entry, and the `test` job is what accepts it.
- The tests need no vendor account or credential. The harness removes every `ANTHROPIC_*`/`CLAUDE*`
  variable before starting a client, and no secret is added for them.

## Alternatives considered

- **`npx <package>@latest` on every PR.** Rejected: a client release would turn every open PR red
  at once for a cause none of them contains, and there would be no known-good version to compare
  against. It also runs unreviewed, freshly published code on every PR.
- **`npm install -g <package>@<version>` in the workflow.** Rejected: an exact version is pinned,
  but its dependency tree is not, and nothing checks the tarball's integrity against a reviewed
  value. `npm ci` checks every package against the committed lockfile's `integrity` hashes.
- **Pinning only, no canary.** Rejected: the pin would learn about a breaking client release only
  when someone bumped it, by which time users who auto-update would already have hit it.
- **One `package.json` per client.** Rejected: one install step and one Dependabot entry cover
  every client, and a client is added with one dependency line.

## Consequences

- A PR's T3 result depends only on the PR and the reviewed pin.
- A client release that breaks `/mcp` shows up within a week as a named issue, not as a red PR.
- The pinned version falls behind the client's newest release between bumps. The canary covers
  that gap.
- A client whose CLI needs a signed-in account even to list its MCP servers cannot be tested this
  way. Such a client is reported, not given a credential.

## Verification

- `tests/integration/ai_clients/package.json` and `package-lock.json`.
- `tests/integration/ai_client_harness.py` (credential stripping, binary resolution) and
  `tests/integration/test_claude_code_contract.py`.
- `tests.yml`'s `test` job: the "Install AI-client CLIs" step and `CLAUDE_CODE_BIN` on the pytest
  step.
- `.github/workflows/ai-client-canary.yml`; `.github/dependabot.yml`'s
  `/tests/integration/ai_clients` entry.
- `docs/testing-policy.md`, "AI-client contract tests (T3)".

## Related

- [ADR 0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md): the same rule that
  real credentials stay off GitHub-hosted runners, which the no-credential requirement here keeps.
- [ADR 0046](0046-release-ci-pins-codesigntool-by-version-and-sha256.md): the same "pin a
  third-party binary by version and hash" choice for release tooling.
