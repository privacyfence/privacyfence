# ADR 0302: Linux is the only CI platform

## Status

Accepted — 2026-11-14. Relates to [ADR 0057](0057-stable-tags-are-gated-on-graphical-session-coverage.md). Implemented.

## Context

Windows and macOS legs existed to cover the installers and the platform-specific local mode. With both removed, those legs test code that is not shipped, at the highest runner cost, and they are required checks on the live ruleset.

## Decision

- `platform-windows`, `platform-macos`, `tests/platform` and the Windows coverage floor are removed.
- The shim steps and the Node requirement are removed from the main test job. The `npm ci` step of the AI-client suite stays.
- The required-check list in the branch-protection script drops the two platform checks. The owner applies the live ruleset before the change merges, because applying it earlier would drop Windows and macOS gating on `main` while 7.x still ships.
- The `platform` pytest marker is deleted; `packaged` stays, because the image and release tests use it.

## Alternatives considered

- **Keeping the legs as optional checks.** Rejected: they would exercise nothing PrivacyFence ships.
- **Keeping the Windows floor.** Rejected: its modules are deleted.

## Consequences

- Coverage floors of shrunken modules are re-measured.
- Between the change and the ruleset update, two checks would be required but never run; the owner closes that window first.

## Verification

`tests/unit/test_update_branch_protection.py`.

## Related

ADR 0057, 0296, 0301.
