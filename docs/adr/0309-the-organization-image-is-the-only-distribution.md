# ADR 0309: The organization image is the only distribution

## Status

Accepted — 2026-11-14. Amends [ADR 0020](0020-pypi-publishing-uses-oidc-trusted-publisher-only.md), [ADR 0040](0040-telegram-app-credentials-ship-in-every-distribution.md), [ADR 0114](0114-the-glama-listing-runs-a-tool-catalog-not-a-hosted-privacyfence.md), [ADR 0161](0161-privacyfence-sync-is-a-separate-plain-mcp-client.md), [ADR 0182](0182-every-tool-a-release-or-qa-runner-job-installs-comes-from-a-hash-pinned-lock.md), [ADR 0208](0208-the-organization-image-is-published-to-ghcr-for-stable-releases-only.md), [ADR 0209](0209-the-organization-image-is-tested-in-build-yml-and-pushed-by-a-workflow-of-its-own.md), [ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md); relates to [ADR 0295](0295-the-supported-python-floor-is-3-11-5.md). Implemented.

## Context

PrivacyFence 8 is installed only as the organization Docker image ([ADR 0301](0301-desktop-mode-and-its-installers-are-removed-in-8.md)). The `privacyfence` PyPI project, which served `pip install privacyfence` and the desktop packages, has no installer left to serve. `privacyfence-sync` is a client installed on a person's own computer and still needs PyPI.

## Decision

- The `privacyfence` PyPI project is not published again; releases up to 7.x stay. The wheel is built only inside the image build and the image test job. It is not published, not attached to a release and not uploaded to R2.
- `publish-pypi.yml` keeps its file name and publishes only `privacyfence-sync`: `dedupe`, `wait_for_build`, a `build` job for the sync package, `publish-testpypi-sync` and `publish-pypi-sync`, each stable-only with its environment (`testpypi`, `pypi`) unchanged. The trusted-publisher entries on pypi.org and test.pypi.org are bound to that file name.
- The lowest-version dependency audit, which exists because a pip install resolves from the declared ranges, is deleted. The supported Python floor ([ADR 0295](0295-the-supported-python-floor-is-3-11-5.md)) and its compatibility test stay.
- The project metadata that only fed the PyPI page (keywords, classifiers, project URLs) and the upload tool are removed. `setup.py`, the manifest, the version plugin and the release lock stay, because they build the image's wheel.
- The organization release smoke test stays as a required check with the installed-wheel daemon scenario, and loses only the systemd unit check.
- The Glama listing installs the catalog server from PyPI; its build spec lives on Glama and is changed there.

## Alternatives considered

- **Moving the sync jobs to a new workflow.** Rejected: PyPI's trusted-publisher entries are bound to the workflow file name, so a new file needs both registrations redone by hand.
- **Shipping the `privacyfence-sync` wheel as a GitHub release asset, or as a Docker image.** Rejected: the owner chose to keep PyPI; the client is installed on a person's own computer.
- **Deleting the installed-wheel smoke test with the pip path.** Rejected: it exercises the same daemon scenarios the image runs and is a required check; only its wording changes.

## Consequences

- Operators install the image or build it from the repository.
- Existing PyPI releases are not yanked.

## Verification

`tests/unit/test_publish_pypi_workflow.py` and `tests/unit/test_release_assets.py`.

## Related

ADR 0020, 0040, 0114, 0161, 0182, 0208, 0209, 0255, 0295, 0301, 0306.
