# ADR 0057: Stable tags are gated on graphical-session coverage; pre-release tags only report it

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-21 in
[#374](https://github.com/privacyfence/privacyfence/issues/374)). The report-only half landed in
`ae1260ab` ([#436](https://github.com/privacyfence/privacyfence/pull/436), 2026-09-16) and the
stable-only gate in `88603b64` ([#593](https://github.com/privacyfence/privacyfence/pull/593)).

## Context

`linux-graphical-session.yml`, `windows-graphical-session.yml` and `macos-graphical-session.yml`
are the only automated coverage for the daemon and companion starting themselves at login. They
run on packaging-related pushes to `main` and `releases/**`, weekly, and on dispatch. They never
run on a tag push and are not a `needs:` of `build.yml`'s `finalize-release`, because this tier is
slow and its runtime must not sit on a release's critical path (`docs/testing-policy.md`, layer 6).

That left a tag free to ship with autostart broken, as long as the last packaging-touching push
was green and nothing re-ran the three workflows since. #374 listed three options in increasing
cost: report the latest conclusion at release time; run the workflows on tags without a `needs:`
edge; or gate stable tags only, with a documented one-re-run allowance.

## Decision

`finalize-release` runs `scripts/check_graphical_session_coverage.py --channel <channel>` as an
ordinary step, after resolving the channel with `r2_release.py channel` and before it downloads
artifacts, attaches release assets, creates the pre-release entry or promotes R2's `latest.json`.

- The script resolves the release branch (`resolve_release_branch`: the `origin/releases/*`
  branch containing the commit, else `main`), reads each workflow's single latest *completed* run
  on it, and reports a gap when there is no run, the run's commit is not an ancestor of the
  released commit (`git merge-base --is-ancestor`), its conclusion is not `success`, or the API
  fetch failed (`evaluate`, `check_all`).
- Every gap is printed as a `::warning::` on every channel.
- Only `--channel stable` turns a gap into exit 1, which fails `finalize-release`, so the stable
  GitHub Release gets no assets, `latest.json` is not promoted, and `publish-pypi.yml`'s
  `wait_for_build` refuses to publish. Any other channel, or none, exits 0.
- Nothing waits on a live run: the script only reads runs that already completed.
- One re-run allowance: a red run judged a flake may have its own failed jobs re-run once, which
  updates that run in place; re-running `finalize-release` then reads the new conclusion. A second
  red run on the same commit is treated as real. This is a documented rule (the module docstring
  and the `::error::` text), not something the script counts or enforces.

## Alternatives considered

- **Block `finalize-release` on a fresh graphical-session run** (option 2 or 3 as a live wait, or a
  `needs:` edge). Rejected: it puts the slowest, most environment-sensitive tier on every release's
  critical path, the property layer 6 was built to avoid. Reading an existing run gives the same
  answer when coverage is current, and fails immediately instead of waiting when it is not.
- **Gate every channel.** Rejected: pre-release tags are where a flake is cheapest to absorb, and
  gating them would stall the beta channel over the tier most likely to flake. They still get the
  warning, so "nobody looked" is closed there too.
- **Report only (option 1 alone).** It was the first step (#436) but let a stable tag ship with
  red or stale autostart coverage, which #374 was about.

## Consequences

- A stable tag needs a green, reachable run of all three workflows. After a packaging change,
  dispatch any stale workflow on the commit being released before tagging.
- A failed gate happens after the build jobs uploaded to R2's `releases/stable/<version>/`, but
  before anything public: no release assets, no `latest.json` promotion, no PyPI.
- The re-run allowance relies on maintainer discipline; the script cannot tell a first red run
  from a second.

## Verification

- `tests/unit/test_check_graphical_session_coverage.py`:
  `test_fails_with_warnings_on_the_stable_channel`,
  `test_exits_zero_with_warnings_on_a_pre_release_channel`,
  `test_exits_zero_with_warnings_when_no_channel_given`, `test_run_not_an_ancestor_warns`,
  `test_fetch_error_is_reported_not_raised`, `test_queries_the_resolved_release_branch_not_main`.
- `.github/workflows/build.yml`, `finalize-release`'s "Check graphical-session/autostart coverage
  (gates stable tags only)" step, ahead of "Download build artifacts".

## Related

- [#374](https://github.com/privacyfence/privacyfence/issues/374) — the gap and its options.
- [ADR 0030](0030-preflight-dispatches-build-yml-before-tagging.md) — the other release-time gate
  on packaged coverage; it runs `build.yml` before tagging, and leaves this tier's scheduling alone.
- [ADR 0022](0022-one-release-tag-per-commit.md) — the same "fail before publishing" placement.
