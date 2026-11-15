# ADR 0300: The download page and the Worker are retired

## Status

Accepted — 2026-11-14. Supersedes [ADR 0024](0024-pre-releases-are-publicly-downloadable.md); amends [ADR 0277](0277-the-websites-primary-call-to-action-is-install.md). Implemented.

## Context

[ADR 0024](0024-pre-releases-are-publicly-downloadable.md) made pre-releases publicly downloadable through the download page and its Worker. With no installers built, the page, the releases page, the download counter and the Worker serve nothing.

## Decision

- The download page, the releases page, the download counter and the Cloudflare downloads Worker with its deploy workflow are removed.
- Old installers stay on their GitHub Releases.
- `/download/` becomes a short stub that links to `/install/` and to the GitHub Releases page, so old links do not return 404.
- The security headers do not allow the downloads host.
- The primary call to action of [ADR 0277](0277-the-websites-primary-call-to-action-is-install.md) already points at `/install/`; only the `/download/` redirect text is amended.

## Alternatives considered

- **Keeping the Worker and a legacy download page.** Rejected: the owner chose to retire them; a page of 7.x installers invites new desktop installs.
- **Deleting `/download/` outright.** Rejected: links in the wild would break.

## Consequences

- Download statistics end with the page.
- The release archive on R2 stays for what is still built.

## Verification

`tests/unit/test_build_site.py`.

## Related

ADR 0024, 0277, 0296, 0301.
