# ADR 0214: The organization image stops with SIGINT

## Status

Accepted — 2026-10-20. Implemented: `deploy/docker/Dockerfile`, `deploy/docker/compose.yaml`.

## Context

A container's main process receives the stop signal as PID 1, and the daemon's clean shutdown is wired to SIGINT.

## Decision

The image sets `STOPSIGNAL SIGINT` and the entrypoint `exec`s the daemon so it is PID 1 and receives it directly. No init process is added.

## Rejected alternatives

- **`tini` or `docker run --init`**: it is an extra package, or a flag every operator must remember; the daemon already shuts down cleanly on SIGINT.

## Consequences

- `docker stop` and Compose shut the daemon down cleanly with no extra flag.
- Any child process the daemon starts is the daemon's responsibility to reap.
