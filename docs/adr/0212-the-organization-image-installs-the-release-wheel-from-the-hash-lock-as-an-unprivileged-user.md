# ADR 0212: The organization image installs the release wheel from the hash lock as an unprivileged user

## Status

Accepted — 2026-10-20. Implemented: `deploy/docker/Dockerfile`, `scripts/build_org_image.sh`.

## Context

The `.deb` ships a PyInstaller bundle. A container has no such need, and every release job's installs must come from a hash-pinned source ([ADR 0182](0182-every-tool-a-release-or-qa-runner-job-installs-comes-from-a-hash-pinned-lock.md)).

## Decision

The image installs the release wheel and the hash-pinned runtime lock into a virtual environment, which installs on both architectures, and runs the daemon as a dedicated unprivileged user that owns the data directory.

## Rejected alternatives

- **A PyInstaller bundle in the image, like the `.deb`**: it gains nothing in a container and PyInstaller cannot cross-build; the wheel plus the universal hash lock already installs on both architectures.
- **Running as root inside the container**: nothing in the daemon needs it, and the data directory checks expect a dedicated owner.

## Consequences

- The image contains exactly the wheel and the locked dependencies, so its contents are reproducible from the release.
- Operators mounting a data volume must make it writable by the image's user; `docs/org-mode-docker.md` says how.
