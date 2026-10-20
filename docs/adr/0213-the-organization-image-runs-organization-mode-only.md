# ADR 0213: The organization image runs organization mode only

## Status

Accepted — 2026-10-20. Implemented: `deploy/docker/docker-entrypoint.sh`, `deploy/docker/Dockerfile`.

## Context

The same wheel can start local mode, which listens on loopback for a desktop user. Inside a container that listener is unreachable from outside, so a container started without an organization bundle would look healthy to nobody while doing nothing useful.

## Decision

The entrypoint starts organization mode and refuses to start, with a message naming the missing bundle, when none is installed.

## Rejected alternatives

- **Starting local mode when no bundle is installed**: it would listen on the container's loopback, unreachable, and look healthy to nobody.

## Consequences

- A misconfigured deployment fails at start with an explicit message instead of running as a useless local instance.
- The image cannot be used as a local-mode container.
