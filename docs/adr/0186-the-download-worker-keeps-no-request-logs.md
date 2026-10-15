# ADR 0186: the download Worker keeps no request logs

## Status

Accepted — 2026-10-15.

## Context

The privacy page at `/privacy/` says the download Worker adds one to a counter when someone downloads
an installer and that the counter is all it stores. Workers observability, left on, kept a log of
every request to the Worker in the Cloudflare account, which made that sentence untrue.

## Decision

`cloudflare/downloads/wrangler.toml` sets `[observability]` `enabled = false`, so the Worker keeps
no request logs.

## Alternatives considered

- **Disclose the logs and their retention on `/privacy/`.** Rejected by the maintainer: the aim is
  to keep the page's claim true, not to widen it.
- **Sample the logs with `head_sampling_rate`.** Rejected: it is still a second store of requests.

## Consequences

- There are no stored request logs to troubleshoot with.
- A live `wrangler tail` still shows the Worker's logs while it runs; nothing is kept afterwards.

## Verification

- `cloudflare/downloads/wrangler.toml` (`[observability]`).
- The text of `/privacy/` (`website/privacy/index.html`).

## Related

- [ADR 0024](0024-pre-releases-are-publicly-downloadable.md) — the download Worker.
