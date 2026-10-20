# ADR 0209: The organization image is tested in build.yml and pushed by a workflow of its own

## Status

Accepted — 2026-10-20. Implemented: `.github/workflows/build.yml` (`build-org-image`), `.github/workflows/publish-org-image.yml`, `docs/releasing.md`.

## Context

`build.yml` already builds and tests every packaged artifact, and `publish-pypi.yml` waits for `build.yml` to succeed. The image has to be tested before it is pushed, and a registry outage must not be able to hold PyPI back ([ADR 0112](0112-stable-releases-are-listed-on-the-mcp-registry-with-the-mcpb.md) kept the MCP registry out of the PyPI path).

## Decision

`build-org-image` in `build.yml` builds the image for each architecture, runs the image test against it and uploads the tested image as a workflow artifact. `publish-org-image.yml` runs on the tag, waits for `build.yml`, downloads those artifacts, loads them, pushes and tags them. Nothing in `build.yml` pushes to a registry.

## Rejected alternatives

- **Pushing from `build.yml`**: `publish-pypi.yml` waits for `build.yml` to succeed, so a registry outage would block PyPI.
- **Rebuilding the image in the publish workflow**: it would publish an image no test ran against; the publish job pushes the bytes `build-org-image` tested (`docker save`, artifact, `docker load`).
- **Push-by-digest from the build jobs**: it leaks a pushed image before `finalize-release` has proven the rest of the release.

## Consequences

- `build-org-image` gates PyPI, the MCP registry, R2 `latest` and the GitHub Release on every tag, pre-releases included: `finalize-release` needs it, and the publish workflows wait for the whole `build.yml` run. A failing image test, or no `ubuntu-24.04-arm` runner, blocks all of them. That is the same all-or-nothing rule every other packaged artifact follows: a release whose organization image is broken is a broken release.
- A GHCR outage cannot hold back PyPI; only the push is affected, and it is rerun with `workflow_dispatch` from the tag.
- The tested images are kept as artifacts for 30 days, which is also how long a publish can be rerun.
