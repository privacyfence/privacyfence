# ADR 0266: Live checks run on a dedicated runner and test the built-in apps

## Status

Accepted — 2026-10-30. Not yet implemented: the live-check workflows still target the runner labelled
`privacyfence-test`, and the QA server's deploy files do not exist yet. They land in later changes; this
ADR records the decision they follow.
Amends [ADR 0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md): the runner host.
Amends [ADR 0181](0181-the-qa-runner-is-reachable-only-from-mains-two-qa-workflows.md): the runner group and label.
Relates to [ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md) and [ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md).

## Context

The live checks run on a self-hosted runner whose tokens were minted with the QA bundle's own OAuth
apps ([ADR 0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md),
[ADR 0181](0181-the-qa-runner-is-reachable-only-from-mains-two-qa-workflows.md)). The current host also
runs unrelated services. Built-in apps ([ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md))
are what most people will use, and they work only in organization mode through a public server, so
the old setup cannot test them at all. Local mode is removed in 8
([ADR 0261](0261-local-mode-is-deprecated-in-7-and-removed-in-8.md)), so a check that depends on it would
need rebuilding then.

## Decision

- **A new dedicated host, `runner.privacyfence.eu`** (a Hetzner Cloud server, Ubuntu LTS), replacing
  the old one rather than reworking it in place. It runs only PrivacyFence's runner and QA services.
  It is behind the Cloudflare proxy (Full strict, with an origin certificate), and its firewall admits
  TCP 443 from Cloudflare's ranges only. **Administration is over Tailscale only;** OpenSSH is
  disabled, and no token is ever moved by hand.
- **The GitHub runner** is registered as `runner-privacyfence-eu` with the label `privacyfence-live`
  in its own organization runner group `privacyfence-live`, set to selected workflows on `main`
  ([ADR 0181](0181-the-qa-runner-is-reachable-only-from-mains-two-qa-workflows.md) stays in force):
  `connector-live-check.yml`, `qa-record-fixture.yml` and the QA server's deploy workflow. It stays a
  non-ephemeral service.
- **Users and Docker.** The runner service runs as `pfrunner` (uid and gid 10001), which is not in the
  `docker` group (root-equivalent) and has one sudo rule: a root-owned deploy script that takes no
  arguments. The QA server and Peek run as containers of one compose project, and share exactly one
  directory with `pfrunner`: the QA server's data directory.
- **The checks run in organization mode as the principal `qa-live`** with the built-in apps, so 8.x
  needs no runner change. The runner's bundle is optional, for testing an organization's own app.
  The built-in credentials are written from CI secrets in a step's environment only, and the generated
  module is deleted after each run because the runner is not ephemeral.
- **A QA organization server on the same host,** built from `main` and redeployed on each relevant
  push, is where the maintainer connects the QA accounts in a browser, for every service. The runner
  copies the `qa-live` principal's tokens from its data directory and writes changed ones back. The
  old loopback flows over an SSH tunnel, with tokens copied by hand, are no longer used. **`/mcp` and
  the MCP OAuth endpoints are blocked at its proxy,** so no AI client can reach a public server
  holding live QA tokens, and nothing on the server uses or refreshes them: the runner is their only
  user, which matters for rotating refresh tokens and Telegram sessions.
- **A new connector lands as two changes:** first connect it on the QA server, then add its tools and
  record the live fixture.
- The weekly check also fetches the relay's health endpoint and reports it.
- Cloudflare's bot protection must not challenge `/.well-known/privacyfence-relay` or
  `/oauth/callback/*` on the host.

## Alternatives considered

- **Reworking the existing runner in place.** Rejected: it hosts unrelated services and has no public
  organization server.
- **Keeping the live checks on QA apps of their own.** Rejected: the apps people use would stay
  untested.
- **A GitHub-hosted runner with credentials as secrets.** Rejected by
  [ADR 0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md) for the QA accounts' tokens.
- **Opening SSH to the internet instead of Tailscale.** Rejected: the Cloudflare proxy does not carry
  SSH, and an exposed port is exposure.
- **Minting tokens with loopback CLI flows over an SSH tunnel and copying them by hand.** Rejected:
  that is the old runner's way, and every copy is a place to leak a token.
- **A QA server with `/mcp` open.** Rejected for the reasons above.
- **Publishing a `main`-branch image to GHCR for the QA server to pull.** Rejected: the image is built
  in the workflow, uploaded as an artifact and loaded on the host, so no registry tag carries
  credentials-bearing builds and no pull credential exists on the host.

## Consequences

- The QA server and the runner share one directory; each can change what the other reads. Both hold
  only QA accounts, and `/mcp` is closed so the server never uses the tokens.
- The cutover is one switch: the workflows move to the new label together. Until the QA accounts are
  connected on the new server the live check fails at its token copy; the fix is to finish connecting
  them, not to point the workflows back.
- Local-mode loopback sign-in and an organization's own apps are no longer live-tested, unless the
  optional bundle carries one; unit tests cover both.
- Maintainers administer the host over Tailscale and the QA Peek instance is public, protected by
  Peek's own sign-in, because its device sign-in runs in the maintainer's browser.

## Verification

Once implemented: the live-check workflow tests and `tests/unit/test_qa_fixture_recorder.py`,
`.github/workflows/connector-live-check.yml`, `.github/workflows/qa-record-fixture.yml`,
`docs/connector-qa.md`. Today: the runner group settings are made on GitHub.

## Related

ADR 0019, 0181, 0255, 0259, 0261.
