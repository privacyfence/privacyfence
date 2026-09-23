# ADR 0024: pre-releases are publicly downloadable through the download Worker

## Status

Accepted (recorded retroactively on 2026-09-23; decided 2026-09-13 in `eb49fe6e`, "Decide the
pre-release access question, and give Phase 7 a real gate", merged in
[#363](https://github.com/privacyfence/privacyfence/pull/363)). Implemented; the decision
ratified behavior that was already live.

## Context

Every tag uploads its artifacts to the Cloudflare R2 bucket `privacyfence-releases`, left at R2's
default: private, no public bucket policy, no custom domain. Pre-release channels (`alpha`,
`beta`, `rc`) are otherwise kept away from public distribution: they never reach PyPI/TestPyPI,
and their GitHub Release entry carries no files.

CLAUDE.md used to say alpha/beta/rc "need restricted access", that the private bucket was the way
to guarantee it, and left open how an authorized tester would get a file out. Both claims had
already been overtaken by code:

- The download Worker (`cloudflare/downloads/src/index.ts`) serves `/download/<channel>/<artifact>`
  and `/download/version/<version>/<artifact>` unauthenticated for every channel it knows, and
  `/api/releases` lists the latest manifest of each. A private bucket behind a public Worker is a
  private bucket with a public door.
- The download page then advertised pre-releases on purpose. The release-publishing plan asked
  for a public "want to test the next version?" invitation, and `website/download/download.js`
  offers whichever pre-release channel has the newest build to every visitor.

The open question had been answered by accident, in the opposite direction from the one intended.

## Decision

**Anyone can download a pre-release, through the Worker.** A tester needs no credential, presigned
URL or Access policy: the link, or the "Want to test the next version?" section on
`privacyfence.eu/download/`, is enough.

What still holds: every pre-release download goes through the Worker, so it is counted, and R2
itself stays unreachable except through it. The bucket's privacy is kept for that reason, and not
as access control.

## Alternatives considered

- **Restrict pre-releases to authorized testers** (the previous written intent) — rejected. An
  open-source governance tool wants testers more than it wants gatekeeping. The channels are
  already separated from stable everywhere it matters: no PyPI, no GitHub Release assets, a
  distinct `latest.json` and distinct D1 counter rows. And making people work to obtain the
  software you want them to test tends to produce no testers rather than careful ones.
- **Rely on the private bucket for restriction** — not a real option: it gates nothing while the
  Worker serves every channel publicly.

## Consequences

- Pre-release builds are public software in practice, even though they are listed on no public
  index other than the project's own download page.
- The download KPI stays a full count for every channel, because nothing can enumerate the bucket,
  hotlink an object, or fetch a release without passing through the counting route.
- **To reverse this**, change the Worker, not the bucket, which is already private and gates
  nothing on its own:
  1. Gate `routeDownload`/`routeApi` on channel: serve `stable` publicly and require proof for the
     rest (a shared token header, or Cloudflare Access in front of the pre-release paths). The
     gate must also cover `/download/version/<version>/…`, which resolves a pre-release version to
     its channel inside `routeDownload`.
  2. Drop or gate the pre-release section in `website/download/download.js` (and its markup in
     `website/download/index.html`).

  Doing (1) without (2) leaves the website inviting people to a download that will refuse them.
  A reversal is a new ADR that supersedes this one.

## Verification

- `cloudflare/downloads/src/index.ts`: `routeDownload` and `routeApi` apply no authentication and
  accept any channel in `CHANNELS` (`cloudflare/downloads/src/channel.ts`: `stable`, `alpha`,
  `beta`, `rc`). Tests: `cloudflare/downloads/test/download.test.ts` serves and lists `alpha`,
  `beta` and `rc` releases.
- `website/download/download.js`: `PRERELEASE_CHANNELS` and `pickPreRelease` choose the newest
  published pre-release by version and link it through the Worker's `/download/<channel>/…`.
- `website/download/index.html`: the `prerelease-block` section, hidden until a pre-release
  exists.
- `scripts/r2_release.py` module docstring restates the decision.

## Related

- [#363](https://github.com/privacyfence/privacyfence/pull/363) (`eb49fe6e`): recorded the
  decision and the reversal steps in CLAUDE.md.
- The release-publishing plan (Phase 1 Worker, Phase 5 website) was deleted in `6d31c0eb`:
  `git show 6d31c0eb^:docs/release-publishing-kpi-plan.md`. Its standing replacement is
  `docs/downloads-and-release-kpi.md`.
- CLAUDE.md, "Cloudflare R2 release archive": which channels reach which public index. A
  pre-release still gets an asset-less GitHub Release entry marked prerelease, because
  `update_checker.py`'s beta channel reads that flag.
