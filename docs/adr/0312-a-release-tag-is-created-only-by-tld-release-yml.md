# ADR 0312: a release tag is created only by `tld-release.yml`

## Status

Accepted — 2026-11-15. Implemented: `.github/workflows/tld-release.yml`, `scripts/release_check.py`,
`scripts/tag_release.py` (`--check-only`), `.tld/admin-policy.yaml`.
Supersedes [ADR 0185](0185-a-release-tag-is-created-only-by-release-yml.md).
Amends [ADR 0021](0021-release-tag-push-never-uses-github-token.md): the token is `TLD_RELEASE_TOKEN` in
environment `tld-release`, and the tag is an annotated one created through the REST API.
Relates to [ADR 0023](0023-changelog-is-the-only-source-of-release-notes.md) and
[ADR 0030](0030-preflight-dispatches-build-yml-before-tagging.md).
Amended by [ADR 0327](0327-stable-releases-go-through-tld-release-and-a-release-only-version-file.md).

## Context

After the move to the tld plugin, `release.yml` and `tld-release.yml` both existed. Each could create a `v*`
tag, each had its own token, environment and tag ruleset, and the release docs described only the first.
A tag starts `build.yml` and `publish-pypi.yml`, so a second route to it is a second place where the
publishing path can be got wrong or left unguarded.

## Decision

1. **One route.** `.github/workflows/tld-release.yml` is the only way to create a `v*` tag. It runs in the
   `tld-release` environment, which admits only `main`, and the owner approves every run.
2. **Rendered, not edited.** `tld ops repo-admin` renders the workflow from `.tld/admin-policy.yaml`. The
   policy's parameters are `tag_prefix: "v"`, `version_style: pep440`, `create_release: "false"` and
   `check_script: scripts/release_check.py`. The workflow file is not hand-edited beyond them.
3. **The check script.** `scripts/release_check.py` runs `r2_release.py channel`, `changelog_section.py`
   (stable versions only, with the body that will ship written to the run summary) and
   `tag_release.py --no-fetch --check-only`, which runs every check of `tag_release.py` and creates no tag.
4. **The tag.** On a real run the workflow creates an annotated tag through the REST API with
   `TLD_RELEASE_TOKEN`, an owner token stored as that environment's secret. It is never `GITHUB_TOKEN`, so
   the tag push starts the publishing workflows (ADR 0021).
5. **Who may tag.** The `tld-tags` ruleset lets no workflow token create a tag; it replaces the `release tags`
   ruleset and the `release` environment. A person with the admin role could still push a tag by hand; that is
   not the procedure.
6. **Green first.** The workflow refuses a commit while any check run on `main`'s tip is unfinished or red.

## Alternatives considered

- **Keep `release.yml` beside it** — two routes to one tag, two tokens, two rulesets.
- **Edit `tld-release.yml` beyond its parameters to keep the old post-tag poll** — every audit would report
  the drift; the check that `build.yml` started moves to `/cut-release` instead.
- **Check the `build.yml` pre-flight from the check script** — it would need a token and an unauthenticated
  API call, which is flaky; the pre-flight stays a procedure (ADR 0030).
- **A `release:` section in the profile and `/tld:release`** — it needs `version_files`, and the version here
  comes from `setuptools_scm` tags, not from a file.

## Consequences

- The first tag made this way is annotated where recent ones were lightweight. `setuptools_scm`,
  `r2_release.py check-tag` and `release_run_guard.py` are unaffected.
- The green check blocks on any red, queued or running check run on `main`'s tip, including a scheduled run
  and the self-hosted `deploy-qa-server` job when that runner is offline.
- The workflow does not check afterwards that `build.yml` started; whoever cuts confirms it. If nothing
  started, the owner deletes the tag and cuts again.
- ADR 0030's pre-flight remains a procedure, not a gate: the green check refuses while it runs or is red, but
  cannot notice a pre-flight that was never dispatched. ADR 0023's preview now comes from `release_check.py`.
- If `main` moves while a run waits for approval, `tag_release.py` refuses and the run is dispatched again.

## Verification

- `tests/unit/test_release_check.py` and `tests/unit/test_tag_release.py`.
- `/cut-release` (`.claude/commands/cut-release.md`) watches every real cut for the `build.yml` and
  `publish-pypi.yml` runs.

## Related

[ADR 0021](0021-release-tag-push-never-uses-github-token.md),
[ADR 0185](0185-a-release-tag-is-created-only-by-release-yml.md),
[`docs/releasing.md`](../releasing.md#cutting-a-release).
