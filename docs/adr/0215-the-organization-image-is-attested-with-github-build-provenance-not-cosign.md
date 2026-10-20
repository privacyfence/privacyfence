# ADR 0215: The organization image is attested with GitHub build provenance, not cosign

## Status

Accepted — 2026-10-20. Implemented: `.github/workflows/publish-org-image.yml`, `docs/org-mode-docker.md`.

## Context

A published image should be verifiable by the operators who run it. Every tool a release job installs has to come from a hash-pinned lock ([ADR 0182](0182-every-tool-a-release-or-qa-runner-job-installs-comes-from-a-hash-pinned-lock.md)).

## Decision

The publish job attaches GitHub artifact attestations (`actions/attest-build-provenance`) to the pushed image, and operators verify with `gh attestation verify`.

## Rejected alternatives

- **cosign signatures**: another tool to install and pin; GitHub artifact attestations are Sigstore-backed, need no tool in the job, and verify with `gh attestation verify`.

## Consequences

- No signing key or extra tool exists to manage.
- If the repository restricts allowed Actions, `actions/attest-build-provenance` must be allowed.
- Verification needs the `gh` CLI rather than `cosign`.
