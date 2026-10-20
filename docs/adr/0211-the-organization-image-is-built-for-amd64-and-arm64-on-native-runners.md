# ADR 0211: The organization image is built for amd64 and arm64 on native runners

## Status

Accepted — 2026-10-20. Implemented: `.github/workflows/build.yml` (`build-org-image`), `.github/workflows/publish-org-image.yml`.

## Context

[ADR 0044](0044-the-deb-declares-only-the-architectures-ci-builds.md) ships the `.deb` for amd64 only, because the PyInstaller bundle cannot be cross-built. Organization servers are often arm64, and the container image needs no per-architecture packaging.

## Decision

The image is built and tested for linux/amd64 and linux/arm64, each on a runner of its own architecture (`ubuntu-latest`, `ubuntu-24.04-arm`), and the two are combined into one multi-architecture manifest at publish time.

## Rejected alternatives

- **amd64 only, as for the `.deb` (ADR 0044)**: arm64 servers are common and the image needs no per-architecture packaging work.
- **QEMU emulation for arm64**: it is slower by an order of magnitude and tests the image under an emulator rather than on the architecture it ships for.

## Consequences

- Both architectures are tested as they run; a release needs an `ubuntu-24.04-arm` runner to be available.
- The manifest is combined from the two tested images, not rebuilt.
