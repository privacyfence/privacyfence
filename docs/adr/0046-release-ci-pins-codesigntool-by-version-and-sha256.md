# ADR 0046: Release CI pins CodeSignTool to one version and its SHA-256

## Status

Accepted — 2026-09-24. Implemented in `build.yml`'s `build-windows` job, step "Install eSigner
CodeSignTool".

## Context

`build-windows` signs the Windows installer with SSL.com's eSigner CodeSignTool, a Java CLI (with a
bundled JDK) downloaded from `SSLcom/CodeSignTool`'s GitHub Releases on every run. The step found
the asset by calling `api.github.com/repos/SSLcom/CodeSignTool/releases/latest` with no token.

Two problems with that:

- **Availability.** A hosted runner's anonymous API calls share GitHub's 60-requests-per-hour limit
  per IP address with every other job on that address. On 2026-09-24 the step failed with a 403
  before anything was built in 5 of about 25 `build.yml` dispatches (runs 36051986040, 36054880599,
  36054885700, 36056396419, 36056828851). A tag build would fail the same way, and
  `finalize-release` and `publish-pypi.yml`'s `wait_for_build` gate every other publish on this job.
- **Integrity.** Whatever SSLcom published last ran on the release runner with the eSigner username,
  password and TOTP secret in its environment, and produced the Authenticode signature users trust.
  Nothing checked what was downloaded, and nothing in this repository's history showed which
  version signed a given release.

## Decision

1. The step pins one CodeSignTool release (`CODESIGNTOOL_VERSION`) and the SHA-256 of its
   `CodeSignTool-<version>-windows.zip` asset (`CODESIGNTOOL_SHA256`), both as step-level `env:`.
2. It downloads that asset from its fixed `github.com/.../releases/download/...` URL, which is not
   an API call and draws on no API rate limit, with a bounded retry (4 attempts, 5/10/20 s backoff).
3. A hash mismatch fails the step before anything is extracted or signed. Only then does the step
   make an API call: an authenticated (`github.token`) lookup of the digest GitHub recorded for the
   asset, printed beside the pinned and downloaded hashes to diagnose the mismatch.
4. Updating CodeSignTool is a change to those two lines. The new hash is taken from the release
   page's asset list (or hashed by hand from the downloaded zip), not copied from a CI failure
   message.
5. The initial pin, v1.3.2 at `4afc32e8b7f79bbe1de7e4e7049aaad4e0f754357613b9bbec0e3052f06fd36b`,
   is trust-on-first-use. The asset was uploaded before GitHub started recording release-asset
   digests, so no independent hash was available, and this repository's cloud sessions cannot
   download from `github.com` release URLs. The hash is the one a hosted Windows runner computed
   over HTTPS in run
   [36061561100](https://github.com/privacyfence/privacyfence/actions/runs/36061561100), a
   deliberate placeholder-mismatch run. The unpinned step was already signing releases with
   whatever that URL served, so pinning does not vouch for these bytes after the fact. It
   guarantees that any change to them from now on fails the build.

## Alternatives considered

- **Keep `releases/latest`, authenticated with `github.token` and retried.** That fixes
  availability (1,000 requests/hour per repository instead of 60 per shared IP) and nothing else:
  the signing credentials would still run whatever was published last, unreviewed. Rejected
  because the integrity problem is the larger one.
- **Pin the version but skip the hash.** A release's assets can be replaced after publication, so a
  tag name alone doesn't identify the bytes. The hash is what makes the pin a pin.
- **Mirror the zip into R2 or this repository.** This adds a copy to keep in sync and doesn't
  avoid the hash check, which already detects a replaced upstream asset. Rejected as unnecessary.

## Consequences

- A new CodeSignTool release is not picked up until someone bumps the pin. If SSL.com changes the
  eSigner service in a way that needs a newer client, signing fails loudly and the fix is the
  two-line bump.
- If SSLcom deletes or replaces the pinned asset, the build fails, which is intended: the
  alternative is signing with bytes nobody reviewed.
- The step no longer needs `api.github.com` on the success path. The job's
  `permissions: contents: read` is enough for the diagnostic lookup of a public repository.

## Verification

- `.github/workflows/build.yml`, `build-windows`, "Install eSigner CodeSignTool": the pin, the
  retry and the hash check.
- The step's log line `CodeSignTool-<version>-windows.zip verified: sha256 <hash>`, and the
  "Build installer" step's CodeSignTool output, on every `build.yml` run with eSigner secrets.
- First verified in `build.yml` runs
  [36062143025](https://github.com/privacyfence/privacyfence/actions/runs/36062143025),
  [36062145619](https://github.com/privacyfence/privacyfence/actions/runs/36062145619) and
  [36062148281](https://github.com/privacyfence/privacyfence/actions/runs/36062148281) (hash
  verified, all four executables signed, every job green). The refusal path was verified in run
  [36061561100](https://github.com/privacyfence/privacyfence/actions/runs/36061561100).

## Related

- ADR [0021](0021-release-tag-push-never-uses-github-token.md) (why the release path is built to
  fail loudly rather than silently)
