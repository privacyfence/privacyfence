# ADR 0268: Pre-releases publish the organization image under their exact version

## Status

Accepted — 2026-10-30. Implemented: `scripts/org_image_tags.py`, `.github/workflows/publish-org-image.yml`.
Amends [ADR 0208](0208-the-organization-image-is-published-to-ghcr-for-stable-releases-only.md): pre-releases publish the image under their exact version tag only.

## Context

[ADR 0208](0208-the-organization-image-is-published-to-ghcr-for-stable-releases-only.md) published the
organization image for stable releases only, so a release candidate had to be tested as a hand-built
image. A release whose point is organization servers, built-in apps and a relay cannot be tested
meaningfully that way: the check that matters is installing the published image as an operator would.
The reason for 0208 stands: `latest`, `X.Y` and `X` must never point at a pre-release, because
unattended upgrades follow them.

## Decision

- A pre-release version (`X.Y.ZaN`, `bN` or `rcN`) publishes **exactly one tag, its own version**, and
  never moves `X.Y`, `X` or `latest`. Stable versions are unchanged, and the floating-tag comparison
  still ignores pre-release tags.
- The publish workflow runs for every release channel, with the same build-provenance attestation, and
  its summary says that no floating tag moved.
- The built-in-apps check (`scripts/builtin_apps.py check-dist --require`) runs on every release tag,
  pre-releases included, because an alpha image without the built-in apps cannot be tested.
- `docs/org-mode-docker.md` and `docs/releasing.md` say so.

## Alternatives considered

- **Keeping images stable-only and testing each release candidate as a hand-built image.** Rejected: it
  tests something other than what operators get.
- **A moving `next` or `alpha` tag.** Rejected: a tag that changes meaning is what unattended upgrades
  must not follow.
- **Publishing pre-release images to a separate package.** Rejected: a second package to authenticate,
  find and clean up, for no benefit over an exact tag.

## Consequences

- A pre-release can be run with `PRIVACYFENCE_IMAGE_TAG=<version>` and tested the regular way.
- Anyone pinning an exact pre-release tag stays on it; the floating tags are unaffected.

## Verification

`tests/unit/test_org_image_tags.py` (a pre-release gets exactly `[version]`, even when it is the
highest), `tests/unit/test_org_image_workflows.py`. Code: `scripts/org_image_tags.py`,
`.github/workflows/publish-org-image.yml`, `.github/workflows/build.yml`.

## Related

ADR 0024, 0208, 0255.
