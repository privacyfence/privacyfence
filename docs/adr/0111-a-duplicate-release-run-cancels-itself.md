# ADR 0111: A duplicate release run cancels itself

## Status

Accepted — 2026-09-29.

## Context

On 2026-09-29, `release.yml` pushed the `v5.2.0` tag once
([run 36579367367](https://github.com/privacyfence/privacyfence/actions/runs/36579367367)). GitHub
started each tag-triggered workflow twice, two seconds apart, for that one push:

- `build.yml` runs 36581010122 and 36581012661;
- `publish-pypi.yml` runs 36581010055 and 36581012784.

Every earlier release tag got exactly one run of each.

Two `build.yml` runs for one tag cannot both succeed. Each builds and signs its own artifacts, and
two signed builds never match byte for byte. `scripts/r2_release.py upload` refuses to overwrite an
R2 object with different content (ADR 0022). Each run therefore uploaded the half it reached first
and failed on the other half:

- run …0122 uploaded the DMG and the SBOMs, and failed on the `.deb` and the installer;
- run …2661 did the reverse.

Neither run had all four build jobs succeed, so `finalize-release` created no GitHub Release in
either. Both `publish-pypi.yml` runs failed at `wait_for_build`, so nothing reached PyPI. Re-running
cannot recover such a release, because a rebuild is re-signed and hits the same refusal. 5.2.0 was
superseded by 5.2.1 (CHANGELOG rule 4).

## Decision

1. **`build.yml` and `publish-pypi.yml` each start with a `dedupe` job, and every other job waits on
   it.** The job lists its own workflow's runs for the same commit and passes them to
   `scripts/release_run_guard.py`. A run is a duplicate when an older run, meaning a lower run ID,
   matches all of the following and has not completed:
   - same workflow, event (`push`) and ref;
   - same commit.

   A duplicate cancels its own run with `gh run cancel`. Of two doubled runs, exactly one decides it
   is the duplicate, whichever order their `dedupe` jobs run in.
2. **Only `push` runs are ever duplicates.** A `workflow_dispatch` is a person asking for a run: the
   `build.yml` pre-flight on `main`, or a manual re-run.
3. **The guard fails open.** If the runs can't be listed, the run goes ahead, as every run did
   before this ADR.
4. **`publish-pypi.yml`'s `wait_for_build` leaves cancelled `build.yml` runs out** when it picks the
   newest run for the commit. Without that, a newer cancelled duplicate would stand in for the run
   that is building.

## Alternatives considered

- **A `concurrency` group per tag, not cancelling in progress.** Rejected: the second run only
  queues. It then rebuilds and fails on R2 after the first finished. As the newest `build.yml` run
  for the commit, it would also be the one `wait_for_build` reads, so PyPI would be blocked.
- **A `concurrency` group per tag with `cancel-in-progress: true`.** Rejected: the newer run
  cancels the older one, so the run that survives depends on GitHub's scheduling order. A
  deliberate re-run would also cancel a run already in progress.
- **Letting R2's immutability guard be the only defence.** Rejected: it catches the collision, but
  only after a full build and signing pass, and it leaves the release unrecoverable under that
  version number.
- **Skipping the duplicate's jobs instead of cancelling the run.** Rejected: a run whose jobs are
  all skipped concludes `success`. `wait_for_build` could then take a duplicate that built nothing
  as proof that the commit built.
- **Treating a finished older run as making this one a duplicate too.** Rejected: deleting and
  re-pushing a tag after its first run failed (ADR 0021's recovery path) must start a run that goes
  ahead.

## Consequences

- A doubled tag push now builds and publishes once. The duplicate shows as a cancelled run in the
  Actions tab, with a warning naming the run it deferred to.
- Every release job starts after a short extra job, a few seconds on `ubuntu-latest`.
- The `dedupe` job holds `actions: write`, only to cancel its own run. No other job gains a
  permission.
- If GitHub's API can't be read, a doubled push fails as `v5.2.0` did, and the fix is the next
  version number.

## Verification

- `scripts/release_run_guard.py` (`is_duplicate()`). Tests: `tests/unit/test_release_run_guard.py`,
  including the `v5.2.0` pair of run IDs.
- `.github/workflows/build.yml`: the `dedupe` job, and `needs: dedupe` on `build`,
  `build-windows`, `build-deb` and `sbom`.
- `.github/workflows/publish-pypi.yml`: the `dedupe` job, `needs: dedupe` on `wait_for_build` and
  `build`, and the cancelled-run filter in `wait_for_build`.

## Related

- [ADR 0022](0022-one-release-tag-per-commit.md): the R2 immutability guard and fixing a stuck
  release forward.
- [ADR 0021](0021-release-tag-push-never-uses-github-token.md): the tag push and its recovery.
- [PR 818](https://github.com/privacyfence/privacyfence/pull/818): 5.2.1 supersedes 5.2.0.
