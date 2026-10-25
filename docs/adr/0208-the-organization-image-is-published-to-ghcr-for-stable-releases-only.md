# ADR 0208: The organization image is published to GHCR for stable releases only

## Status

Accepted — 2026-10-20. Implemented: `.github/workflows/publish-org-image.yml`, `docs/org-mode-docker.md`.

## Context

Organization mode ships as a container image (`ghcr.io/privacyfence/privacyfence-org`), and an image has to live in some registry. PyPI carries stable releases only, and the organization guide promises that an unattended upgrade never picks up a test build; a `latest` tag that could point at a release candidate would break that promise.

## Decision

The image is pushed to GitHub Container Registry only, authenticated with the workflow's own `GITHUB_TOKEN` (`packages: write`), so no registry secret exists. Only stable release tags publish: a pre-release tag ends the publish workflow green with nothing pushed.

## Rejected alternatives

- **Docker Hub, or both registries**: needs a third-party account and a long-lived token held as a repository secret, where GHCR needs neither.
- **Pre-release images**: an image `latest` that could be a release candidate breaks the promise that unattended upgrades only ever pick up stable releases.

## Consequences

- There is no registry credential to rotate or leak.
- The GHCR package must be switched to public once, by hand, after the first publish (see `docs/releasing.md`).
- Testing a pre-release image means building it from the tag's `build.yml` run, not pulling it.
