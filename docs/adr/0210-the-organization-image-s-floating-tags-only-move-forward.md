# ADR 0210: The organization image's floating tags only move forward

## Status

Accepted — 2026-10-20. Implemented: `scripts/org_image_tags.py`, `.github/workflows/publish-org-image.yml`.

## Context

Each stable release is pushed under its full version and under floating tags (`X.Y`, `X`, `latest`) that operators pin to for unattended upgrades. A stable patch release cut on an older line, such as a 6.4.x fix after 6.5.0, would otherwise drag those tags backwards.

## Decision

`scripts/org_image_tags.py` decides the tags from the existing GHCR versions: the full version is always pushed, and `X.Y`, `X` and `latest` move only when the new version is greater than the one they already point at.

## Rejected alternatives

- **Moving `X.Y`, `X` and `latest` unconditionally**: a patch on an older line would move `latest` backwards.

## Consequences

- `latest` is always the highest stable version, whatever order releases are cut in.
- The rule lives in a small script with unit tests rather than in workflow shell.
