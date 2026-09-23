# ADR 0019: PyPI and TestPyPI publishing uses OIDC Trusted Publisher only

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-04 in `451f76db`, which
added `.github/workflows/publish-pypi.yml` and the "Publishing to PyPI" section of `CLAUDE.md`).
Implemented and unchanged since then.

## Context

A release is a tag push (`CLAUDE.md`, "Releasing"). `publish-pypi.yml` listens for the same `v*`
tag as `build.yml`, builds the sdist and wheel, and uploads them, TestPyPI first and then PyPI.
The job needs some credential that both indexes will accept for an upload.

## Decision

Both index uploads authenticate only through PyPI's OIDC **Trusted Publisher** mechanism:

- `publish-testpypi` and `publish-pypi` use `pypa/gh-action-pypi-publish` with job-level
  `permissions: id-token: write` and nothing else. GitHub mints a short-lived OIDC token for the
  job, and PyPI/TestPyPI exchange it for a one-shot upload credential.
- No API token or password for either index is stored in the repository or its secrets. In the
  words of the workflow header, "There is no long-lived secret anywhere for this workflow to
  leak."
- Each job declares a GitHub Environment (`testpypi`, `pypi`) that matches the environment on its
  Trusted Publisher registration. `CLAUDE.md`: "Scoping the publisher to an environment means the
  minted OIDC token is only ever valid for that job, not any other job in this repo."
- Before the first publish, a maintainer registers the workflow as a (pending) Trusted Publisher
  on test.pypi.org and on pypi.org separately: project `privacyfence`, owner and repository
  `privacyfence`, workflow `publish-pypi.yml`, environment as above.

## Alternatives considered

- **A stored PyPI/TestPyPI API token as a repository secret** — this is the alternative the
  sources name, but only by contrast. `451f76db`'s commit message says the workflow uses Trusted
  Publisher "instead of a stored API token, so there's no long-lived secret for either index", and
  the workflow header and `pyproject.toml`'s `dev` extra repeat the contrast. No source weighs the
  token option further than that. The recorded reason is the absence of a long-lived secret.

## Consequences

- Nothing index-related can leak from the repository's secrets, because there is nothing there.
  Upload rights depend on the Trusted Publisher registrations kept on PyPI and TestPyPI, which live
  outside this repository and must be kept in step with it. Renaming the workflow file, the
  repository or an environment breaks publishing until the registrations are updated.
- Uploading is limited to this workflow running in GitHub Actions. A local rehearsal with `build`
  and `twine` (both in the `dev` extra) is for trying a release by hand; CI never uses `twine`.
- The Environments also give a place for a manual gate: a required reviewer on `pypi` turns the
  TestPyPI-before-PyPI ordering (`publish-pypi` `needs: publish-testpypi`) into an explicit
  approval step. `CLAUDE.md` describes this as optional; nothing in the repository enforces it.
- The rule covers the two package indexes only. The same workflow's `publish-r2` job still uses
  stored credentials (`CF_RELEASES_R2_ACCESS_KEY_ID` / `CF_RELEASES_R2_SECRET_ACCESS_KEY`), as
  does `build.yml`. R2 has no equivalent OIDC exchange in this repository.
- A `workflow_dispatch` rerun from an untagged commit produces a `+g<sha>` local version, which
  both indexes reject. Reruns must point at a tagged commit.

## Verification

- `.github/workflows/publish-pypi.yml`: the header comment; workflow-level `permissions: contents:
  read`; `publish-testpypi` and `publish-pypi` each with `environment:` and `permissions:
  id-token: write` only; `pypa/gh-action-pypi-publish` pinned by SHA, with no `password:` input.
- `CLAUDE.md`, "Publishing to PyPI": the mechanism and the one-time registration steps.
- `pyproject.toml`'s `dev` extra: the comment that CI "never needs twine" because it "uploads via
  pypa/gh-action-pypi-publish's OIDC trusted-publishing flow, not a stored token".
- A search of `.github/` for a PyPI token secret (`PYPI_API_TOKEN`, `TWINE_PASSWORD`, `__token__`)
  finds nothing.

## Related

- `451f76db`: adds `publish-pypi.yml` and the `CLAUDE.md` section.
- `5e4c0fe6`: adds the restrictive workflow-level `permissions: contents: read`, leaving the
  publish jobs with `id-token: write` only.
- `9ab65df8`: limits PyPI/TestPyPI to the stable channel and adds `publish-r2`.
