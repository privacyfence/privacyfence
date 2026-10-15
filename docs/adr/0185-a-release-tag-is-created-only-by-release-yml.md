# ADR 0185: a release tag is created only by `release.yml`; a tag ruleset refuses `v*` tags from other accounts

## Status

Accepted — 2026-10-15. The documentation change is in the repository; the ruleset is a repository setting.
Amends [ADR 0021](0021-release-tag-push-never-uses-github-token.md).

## Context

[ADR 0021](0021-release-tag-push-never-uses-github-token.md) makes `release.yml` push the tag with
`RELEASE_TAG_TOKEN` so the tag starts `build.yml` and `publish-pypi.yml`. Nothing stopped any
account with write access from pushing a `v*` tag by hand, which starts the same publishing
workflows without the pre-flight build ([ADR 0030](0030-preflight-dispatches-build-yml-before-tagging.md)),
and `docs/releasing.md` described a hand push as an equal alternative.

## Decision

- `release.yml` is the only documented way to cut a release.
- A repository tag ruleset on `v*` refuses creating such a tag unless the pusher holds the
  repository **admin** role. The account that owns the fine-grained `RELEASE_TAG_TOKEN` must hold
  that role.

## Alternatives considered

- **Leave both paths documented.** Rejected: a hand-pushed tag skips the checks the workflow runs.
- **Require a GitHub App installation token and let only the App bypass.** Rejected for now: it
  changes how the token is issued; the admin-role bypass needs no new credential.

## Consequences

- A contributor with write access cannot create a release tag.
- Accepted: a repository administrator could still push a tag by hand. The docs say that is not the
  procedure; the ruleset limits who can, it does not remove the ability.
- The token's owner must keep the admin role, or releases stop.

## Verification

- [`docs/releasing.md`](../releasing.md) (the release procedure) and
  `.github/workflows/release.yml`.
- The `v*` tag ruleset in the repository settings.

## Related

- [ADR 0021](0021-release-tag-push-never-uses-github-token.md)
- [ADR 0022](0022-one-release-tag-per-commit.md)
