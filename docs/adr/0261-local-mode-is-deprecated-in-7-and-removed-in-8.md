# ADR 0261: Local mode is deprecated in 7.x and removed in 8.x

## Status

Accepted — 2026-10-30. Implemented: `org_mode.LOCAL_MODE_DEPRECATION` and the notices that show it.
Relates to [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) and [ADR 0003](0003-separated-installs-only.md).

## Context

Built-in apps ([ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md)), the relay, the
Admin page and single-user mode all serve organization mode. Desktop (local) mode, with its
privilege-separated installs and companion app ([ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md),
[ADR 0003](0003-separated-installs-only.md)), cannot use built-in apps without loopback redirects that
Slack, Miro and Atlassian refuse to distribute. Keeping two products costs more with every feature.

## Decision

- Local mode is **deprecated in 7.x and removed in 8.x.** Removal is a separate decision made when 8.x
  is planned; this ADR fixes the direction and the notice.
- One text, `org_mode.LOCAL_MODE_DEPRECATION`, says that desktop mode is deprecated and will be
  removed in PrivacyFence 8, that the built-in sign-in works only in organization mode and the Docker
  image, and where to move to. It is shown by:
  - a warning in the daemon log at every local-mode start;
  - a `deprecation` field in `privacyfence_status` (null in organization mode) that the AI client is
    asked to relay to the person once;
  - a strip on every desktop page;
  - one desktop notification from the companion, once per operating-system user (a marker file in the
    companion's per-user state directory; failure to read or write it just shows the notice again).
- Desktop keeps working exactly as in 6.x, with no built-in apps.

## Alternatives considered

- **Dropping local mode in 7 without a deprecation release.** Rejected: people who installed the
  desktop package would lose their setup with no notice and no time to move.
- **Keeping local mode indefinitely.** Rejected: it doubles the surface every feature must work on
  and blocks the simplification of redirects, bundles and settings.

## Consequences

- 7.x carries code that exists only to be deleted in 8.x; the migration checkpoint
  ([ADR 0262](0262-7x-is-the-migration-checkpoint-for-8x.md)) is what makes that deletion safe.
- Desktop users get no built-in apps and are told why.

## Verification

`tests/unit/test_org_mode.py`, `tests/unit/test_companion.py`, `tests/unit/web/test_mcp_dispatch.py`.
Code: `src/privacyfence/org_mode.py` (`LOCAL_MODE_DEPRECATION`), `src/privacyfence/web_shell.py`,
`src/privacyfence/companion.py`.

## Related

ADR 0002, 0003, 0255, 0262.
