# ADR 0020: the release tag is never pushed with `GITHUB_TOKEN`

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-21 in `af193025`, "Add
release.yml and the three slash commands that drive P0's routing", merged in
[#601](https://github.com/privacyfence/privacyfence/pull/601)).

## Context

A release is a tag, not a commit: pushing a `v*` tag is what starts both `build.yml` and
`publish-pypi.yml` (`on: push: tags: ['v*']`), and everything a release produces — signed
installers, SBOMs, the sdist/wheel, the R2 upload, the GitHub Release — hangs off those two runs.

Until `release.yml` existed, the tag could only be pushed from a machine holding push rights to
`refs/tags/*`. A Claude Code on the web container can push branches but not tags, and
`scripts/tag_release.py`'s pre-tag checks only ran when someone remembered to run them from the
right laptop. Moving the push onto an Actions runner means the runner needs a credential that can
write a tag.

The obvious credential is the workflow's own `GITHUB_TOKEN`, and it is exactly the wrong one.
GitHub does not start a workflow run for an event raised by `GITHUB_TOKEN` (its recursion guard).
A tag pushed with it appears on the repository and looks like a successful release, but starts
neither `build.yml` nor `publish-pypi.yml`: no artifacts, no R2 upload, no GitHub Release, and no
error anywhere to notice. The failure is silent and total.

## Decision

The tag push in `.github/workflows/release.yml` authenticates with a dedicated secret,
`RELEASE_TAG_TOKEN`, which must not be `GITHUB_TOKEN`. It is a fine-grained PAT with
**Contents: write** on this repository, or a per-run GitHub App installation token.

The workflow enforces this in three places rather than trusting documentation:

1. The workflow-level `permissions:` is `contents: read`, so `GITHUB_TOKEN` cannot write the tag
   even by mistake.
2. A first step refuses to start when `RELEASE_TAG_TOKEN` is empty, before checkout, naming the
   `GITHUB_TOKEN` pitfall in its error.
3. After a real (non-dry-run) push, the job polls the REST API for a `build.yml` run on the tagged
   commit (12 attempts, 10 s apart) and fails loudly if none appears. This catches every other
   route into the silent state, including the secret later being set to the wrong kind of token.

## Alternatives considered

- **Use `GITHUB_TOKEN`** — rejected: the tag is created but no downstream workflow runs, and the
  run reports success. See Context.
- **Keep cutting releases only from a developer machine** — rejected: it cannot be done from a
  Claude Code on the web container, and it leaves `tag_release.py`'s checks optional in practice.
  A release should not depend on which machine someone is sitting at. The sources name no other
  alternative.

## Consequences

- The repository carries one long-lived write credential for release cutting. As with the R2
  release credentials, a token tied to a user account stops working when that user's access
  changes; CLAUDE.md prefers an account/App-owned credential for that reason.
- A misconfigured secret costs at most two minutes and a red run, not a phantom release. The
  poll's error tells the operator the tag exists but the release did not start, and to delete and
  re-push it with a real token.
- Using a GitHub App token instead of a PAT is permitted by the decision but not wired: the
  workflow reads only `secrets.RELEASE_TAG_TOKEN` and has no step that mints an installation
  token. Adopting it means adding such a step.
- The poll checks only `build.yml`. `publish-pypi.yml` is triggered by the same event and gates
  its own publishing on `build.yml`'s run succeeding, so a missing `build.yml` run already implies
  nothing publishes.

## Verification

- `.github/workflows/release.yml`: `permissions: contents: read`; step "Check the release token
  is configured"; `actions/checkout` with `token: ${{ secrets.RELEASE_TAG_TOKEN }}` (the push in
  `scripts/tag_release.py --push` inherits that auth); step "Verify the tag started build.yml".
- `.github/workflows/build.yml` and `publish-pypi.yml`: `on: push: tags: ['v*']`.
- CLAUDE.md, "Releasing": the reference description of the release workflow and the secret.

## Related

- [#601](https://github.com/privacyfence/privacyfence/pull/601) (`af193025`): introduced
  `release.yml`, the secret requirement and the post-push poll.
- [ADR 0021](0021-one-release-tag-per-commit.md): the tag rules `tag_release.py` enforces in the
  same workflow.
- [ADR 0022](0022-changelog-is-the-only-source-of-release-notes.md): the release-notes render
  `release.yml` runs before tagging a stable version.
- [ADR 0019](0019-pypi-publishing-uses-oidc-trusted-publisher-only.md): the other release
  credential decision.
