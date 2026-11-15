# ADR 0289: The n8n node ships from this repository as an npm package and is published with provenance on stable tags

## Status

Accepted — 2026-11-14. Implemented.
Relates to [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md).

## Context

A delivered credential ([ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md)) is only
useful if a workflow can call PrivacyFence with it. n8n's MCP Client node leaves the endpoint, the tool list, `reason`
and the refusal texts to every workflow author, so a purpose-built community node was wanted. It has to be installable
from npm, show a provenance badge, and always match the PrivacyFence it talks to. Tracking issue: https://github.com/privacyfence/privacyfence/issues/1095.

## Decision

- The node lives in this repository, in `n8n/`, as its own npm package `n8n-nodes-privacyfence`, not part of `mcpb/shim/`.
  The package has no runtime dependencies and one peer dependency, `n8n-workflow`.
- The package is licensed **MIT** (`n8n/LICENSE`); the rest of the repository stays Apache-2.0. The version in the
  repository is `0.0.0-dev`; the real one is derived from the PrivacyFence tag.
- `.github/workflows/publish-n8n-node.yml` publishes it with `npm publish --provenance --access public`, only when
  `scripts/r2_release.py channel <version>` says `stable`, in the GitHub environment `npm`, after `build.yml` for the
  same commit has finished. Trusted publishing is used unless the repository secret `NPM_TOKEN` is set.
- Every stable PrivacyFence tag publishes a new package version, also when `n8n/` did not change.
- Its tests, lint and build run in the `n8n-node` job of `tests.yml`, and a contract test
  (`tests/integration/test_n8n_node_contract.py`) drives the built node against the real daemon.

## Alternatives considered

- **A separate repository.** The node's tests need the real daemon and its version must follow PrivacyFence's.
- **Publishing with the node CLI's release command.** It runs release-it (bump, commit, tag, push), which fights the
  tag-driven versions.
- **Keeping only the MCP Client node and documenting it.** It leaves the endpoint, the tool list, `reason`, the content
  arrays and the refusal text to every workflow author.
- **A separate "PrivacyFence Tools" sub-node for AI Agents.** Declined by the owner for this change; `usableAsTool`
  covers an agent calling one tool.

## Consequences

- One version number, one tag, one release for the server and the node; a patch release that changes nothing in `n8n/`
  still publishes a package version.
- npm provenance ties each package to this repository's workflow run.
- Pre-releases are not published to npm.

## Verification

- `.github/workflows/publish-n8n-node.yml`, `.github/workflows/tests.yml` (job `n8n-node`).
- `tests/unit/test_n8n_publish_workflow.py`, `tests/integration/test_n8n_node_contract.py`.

## Related

- Issue: [issue 1095](https://github.com/privacyfence/privacyfence/issues/1095); sources `n8n/package.json`, `n8n/LICENSE`, `.github/workflows/publish-n8n-node.yml`.
- [ADR 0278](0278-a-scoped-token-can-be-delivered-into-an-agent-runtime-n8n-first.md), ADRs 0290 to 0293.
