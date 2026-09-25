# Downloads, the release archive and the installer-download KPI

How a tagged release's files are stored, how they reach a user, and how installer downloads are
counted. The code is the source of truth: `scripts/r2_release.py` (publication to R2),
`cloudflare/downloads/` (the download Worker), `scripts/release_stats.py` (the GitHub half of the
KPI) and `website/download/` plus `website/stats.js` (the pages that show it).

What a tag triggers, the channel scheme and the release checklist are in
[`CLAUDE.md`](../CLAUDE.md) § "Releasing"; this document covers storage, delivery and counting.

## How a release reaches a user

```
git tag vX.Y.Z  ──►  build.yml / publish-pypi.yml
                          │
                          ├──► private Cloudflare R2: releases/<channel>/<version>/...
                          │         │
                          │         └─ finalize-release: manifest.json, then latest.json
                          │
                          └──► GitHub Release (stable: files attached; pre-release: no files)

privacyfence.eu/download/ ──► downloads.privacyfence.eu (Worker) ──► R2 object (RELEASES binding)
                                          │
                                          └─► D1 counters (DB binding)
```

The browser never receives R2 credentials or a bucket URL. Every installer download on
`privacyfence.eu/download/` streams through the Worker.

## Release archive (R2)

Every tag push, stable and pre-release alike, uploads that release's files to the Cloudflare R2
bucket **`privacyfence-releases`** (`DEFAULT_BUCKET` in `scripts/r2_release.py`; the workflows
also set `R2_BUCKET: privacyfence-releases`). R2 is the one archive that holds every file of every
release, whatever else is also published elsewhere.

### Layout

```
releases/
  <channel>/
    latest.json                  pointer: {"version": ..., "manifest": "releases/<channel>/<version>/manifest.json"}
    <version>/
      <every uploaded file, by basename>
      manifest.json              installers only (see below)
```

- `<version>` is the version `setuptools_scm` resolves from the tag, e.g. `4.1.0` or `4.2.0b1` —
  never the `v`-prefixed tag name.
- `<channel>` is derived from the version's PEP 440 suffix: none → `stable`, `a` → `alpha`,
  `b` → `beta`, `rc` → `rc` (`channel_for_version()`; the Worker's `src/channel.ts` and
  `website/download/download.js` carry hand-kept copies of the same regex). A between-tags dev
  version (`….dev<n>+g<sha>`) is rejected, so nothing can be uploaded for an untagged build.

### What uploads what

Every upload step below runs only on a tag ref (`if: startsWith(github.ref, 'refs/tags/')`), and
every job that resolves a version runs `r2_release.py check-tag` before publishing anything (see
[ADR 0022](adr/0022-one-release-tag-per-commit.md)).

| Workflow · job | Uploads to `releases/<channel>/<version>/` |
| -------------- | ------------------------------------------ |
| `build.yml` · `build` | `PrivacyFence-*.dmg`, `scripts/build_org_bundle.py`, `scripts/sync_room_directory.py` |
| `build.yml` · `build-windows` | `PrivacyFence-*-setup.exe` |
| `build.yml` · `build-deb` | `privacyfence_*.deb` |
| `build.yml` · `sbom` | `sbom-python.cdx.json`, `sbom-shim-npm.cdx.json` |
| `publish-pypi.yml` · `publish-r2` | the sdist and wheel (`dist/*`); runs after `build` and `wait_for_build` |
| `build.yml` · `finalize-release` | `manifest.json`, then `latest.json` (`r2_release.py finalize`) |

The macOS `.pkg` and `.mcpb` travel inside the DMG and are never uploaded on their own
(`scripts/build_dmg.sh`).

### Public distribution per channel

| Channel | R2 | GitHub Release | PyPI / TestPyPI |
| ------- | -- | -------------- | --------------- |
| `stable` | every file above | created by `finalize-release` with the DMG, `-setup.exe`, `.deb`, both SBOMs and both org-config scripts attached; body from `CHANGELOG.md` | sdist + wheel |
| `alpha` / `beta` / `rc` | every file above | created, marked prerelease, **no files attached** (`update_checker.py`'s beta channel reads that flag) | never |

A pre-release's DMG, installers, SBOMs, sdist and wheel are stored only in R2 and reachable only
through the Worker's download routes.

### What the private bucket does and does not buy

The bucket is left at R2's default: private, no public bucket policy, no Public Development URL,
no Custom Domain.

- **It does** make the Worker the only public path to any artifact. Nothing can enumerate the
  bucket, hotlink an object, or fetch a release without passing through the route that counts it,
  which is what makes the download count a full count rather than a sample.
- **It does not** restrict who can download a pre-release. The Worker serves
  `/download/<channel>/<artifact-id>` for every channel it knows, unauthenticated, and
  `/api/releases` lists them all. That is deliberate; see
  [ADR 0024](adr/0024-pre-releases-are-publicly-downloadable.md) for the decision and for how to
  reverse it (Worker routes and website together).

### Publication is transactional

`scripts/r2_release.py` subcommands (from its `argparse`):

| Subcommand | Arguments | Does |
| ---------- | --------- | ---- |
| `channel` | `<version>` | prints the channel; exit 1 for a non-release version |
| `check-tag` | `--tag`, `--version` | exit 1 unless the resolved version is the one the pushed tag names |
| `upload` | `--version`, `[--bucket]`, `files…` | uploads each file to `releases/<channel>/<version>/<basename>` |
| `finalize` | `--version`, `[--bucket]` | writes `manifest.json`, runs `verify`, then `promote` |
| `verify` | `--version`, `[--bucket]` | checks every manifest artifact exists with the recorded size and SHA-256 |
| `promote` | `--version`, `[--bucket]` | rewrites the channel's `latest.json` to point at that version |

`--bucket` defaults to `$R2_BUCKET`, else `privacyfence-releases`. `upload` publishes nothing by
itself; only `finalize` moves `latest.json`, and only after verification passes. A release
missing any mandatory installer fails `finalize` with `latest.json` untouched, so an incomplete
release can sit in the bucket without becoming the one the Worker serves. `finalize-release`
`needs: [build, build-windows, build-deb, sbom]`, so a failure in any of those leaves the previous
release as `latest`.

Properties to know before changing any of it:

- **The manifest lists installers only.** `_INSTALLERS` recognizes exactly three filename
  patterns — `PrivacyFence-*.dmg` → `macos-arm64`, `PrivacyFence-*-setup.exe` → `windows-x64`,
  `privacyfence_*_amd64.deb` → `linux-x64` — and all three (`REQUIRED_ARTIFACT_IDS`) are mandatory
  for `finalize`. SBOMs, org-config scripts, the sdist/wheel and any `.pkg` stay in R2 but never
  enter `artifacts[]` (`classify_installer()` returns `None`; the `.pkg` case is asserted in
  `tests/unit/test_r2_release.py`). This keeps the KPI correct by construction: the Worker counts
  every artifact it serves and the stats query sums every row without filtering `artifact_kind`.
- **Manifest schema is `1`** (`MANIFEST_SCHEMA`): `schema`, `version`, `channel`, `published_at`,
  and `artifacts[]` of `id`, `kind` (`installer`), `platform`, `architecture`, `filename`, `key`,
  `size`, `sha256`, sorted by `id`. The contract is `cloudflare/downloads/src/manifest.ts`; change
  both and the Worker's test fixtures together.
- **SHA-256 is recorded as R2 object metadata (`sha256`) at upload time**, not computed during
  `finalize`, which runs on a runner where none of the installers exist on disk. An ETag is no
  substitute: large uploads go multipart, and a multipart ETag is not the object's MD5.
- **Uploads are immutable.** Identical bytes under an existing key are skipped, so re-running a
  partly failed job is safe; different bytes hard-fail. A version that has published artifacts
  stays published — cut the next one.
- **`promote` refuses a version with no `manifest.json`.** `finalize` always writes it first, so
  this only matters when `promote` is run by hand, where a mistyped version would otherwise point
  a whole channel at a 404.

## The download Worker

`cloudflare/downloads/`, served at `downloads.privacyfence.eu`. It reaches R2 and D1 only through
its `RELEASES` and `DB` bindings (`wrangler.toml`) and holds no S3 credentials.

`.github/workflows/deploy-download-worker.yml` runs on changes to `cloudflare/downloads/**` or
itself:

- `verify` (push to `main`, pull requests, dispatch): `npm ci`, `npm run typecheck`, `npm test`
  (vitest under local Miniflare, no credentials), `npm run dry-run`.
- `deploy` (push to `main` or dispatch only, after `verify`):
  `wrangler d1 migrations apply DB --remote`, `wrangler deploy`, then a `HEAD /health` check that
  must return 200.

### Routes

| Route | Methods | Returns |
| ----- | ------- | ------- |
| `/download/<channel>/<artifact-id>` | `GET`, `HEAD` | resolves `releases/<channel>/latest.json` → manifest → artifact and streams it from R2 |
| `/download/version/<version>/<artifact-id>` | `GET`, `HEAD` | same, from that version's own manifest (older versions stay downloadable) |
| `/api/releases` | `GET` | `{"channels": {stable, alpha, beta, rc}}`, each the latest manifest or `null` |
| `/api/releases/<channel>` | `GET` | that channel's latest manifest; 404 if unknown or unpublished |
| `/api/stats/downloads` | `GET` | `{total, by_channel, by_platform}` from D1; 503 on a D1 error, never a fake zero |
| `/health` | `GET`, `HEAD` | `{"status":"ok"}`; touches neither binding |

`<channel>` is one of `stable`, `alpha`, `beta`, `rc`; `<artifact-id>` is a manifest `id`
(`macos-arm64`, `windows-x64`, `linux-x64`). Downloads are streamed, never redirected. Anything
else returns 404 JSON (`{"error": "no such route"}` for an unknown top-level path, including `/`);
a wrong method returns 405 with `Allow`. `/api/*` accepts `GET` and `OPTIONS` only.

Download responses carry `Content-Type` (by extension: `.dmg`, `.exe`, `.deb`, `.pkg`, else
`application/octet-stream`), `Content-Length`, `ETag`, `Accept-Ranges: bytes` and
`Content-Disposition: attachment; filename="…"`. A single `bytes=` range is honored with a 206 and
`Content-Range`; malformed or multi-range headers get the full object. Conditional requests go to
R2 (`onlyIf`), and a failed precondition returns 304.

CORS applies to `/api/*` only: `Access-Control-Allow-Origin` is reflected for
`https://privacyfence.eu` and `https://www.privacyfence.eu`, never for other origins and never on
`/download/*`.

### Counting semantics

A D1 counter is incremented only for a `GET` that R2 answers with a body and that is a download
start (`isDownloadStart()` in `src/artifacts.ts`):

| Request | Counted? |
| ------- | -------- |
| `GET /download/...` → 200 | yes |
| `GET` with `Range: bytes=0-…` | yes |
| `GET` with `Range: bytes=<N>-`, `N > 0` (a resume) | no |
| `GET` with a suffix range `Range: bytes=-<N>` | no |
| `GET` answered 304 (failed precondition) | no |
| `HEAD`, `OPTIONS` | no |
| 404s, `/api/*`, `/health` | no |

Not counting resumes is what keeps a dropped connection from inflating the KPI. These rules are
covered by `cloudflare/downloads/test/download.test.ts` and `artifacts.test.ts`.

The write goes through `ctx.waitUntil()` and is best-effort: a D1 failure is logged and swallowed
and never blocks the byte stream; an R2 miss returns 404 before any count.

### What is stored

`migrations/0001_download_counts.sql`:

```sql
CREATE TABLE download_counts (
    day           TEXT NOT NULL,
    channel       TEXT NOT NULL,
    version       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    architecture  TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, channel, version, platform, architecture, artifact_kind)
);
```

`day` is the UTC date. One row per key; a download increments it in place. No IP address, user
ID, cookie ID, fingerprint, email, Cloudflare Ray ID or User-Agent is stored anywhere — the schema
has no column for any of them.

## The KPI

The public figure is **installer downloads**, summed from two halves:

- **Cloudflare** — `GET /api/stats/downloads`'s `total`: every counted download across **all
  four channels**, alpha, beta and rc included. The query does not filter by channel;
  `by_channel` is there to separate them when reading the numbers, but the headline does not.
- **GitHub** — `scripts/release_stats.py`, run in `.github/workflows/pages.yml`'s build step,
  sums `download_count` over the assets of the newest 100 non-draft releases (one API page,
  `per_page=100`) that `classify_installer()` (imported from
  `scripts/r2_release.py`) recognizes as installers, and writes it to `release-stats.json` as
  `github_installer_downloads`, with `stars` and `latest_release`. Pre-release GitHub Releases
  carry no files, so in practice only stable contributes.

SBOMs, checksums, org-config scripts, source archives, the sdist/wheel, metadata requests and
`HEAD` probes never count.

**Checks against production use `HEAD`, never `GET`.** Because the headline total includes every
channel, a `GET` against `alpha` inflates the public number exactly as much as one against
`stable`. `build.yml`'s `finalize-release` smoke test (`HEAD` of all three artifact ids on the
release's channel) and `deploy-download-worker.yml`'s `/health` check both use `HEAD`.

### Who sees the numbers

- **`/api/stats/downloads` is public.** It has no authentication; anyone can read it with any HTTP
  client. CORS only decides which web origins' scripts may read it in a browser
  (`privacyfence.eu`, `www.privacyfence.eu`). It exposes aggregates only (`total`, per channel,
  per platform/architecture). `/api/stats/downloads` is the only stats route; any other
  `/api/stats/*` path is a 404.
- **Homepage** (`website/stats.js`): shows GitHub half + Cloudflare half as "release downloads",
  together with the star count, only once the sum is **≥ 50 or** stars are **≥ 10**; otherwise
  the block stays hidden. The Cloudflare half resolves to `0` on any failure, so an outage
  understates the total instead of hiding it; if `release-stats.json` fails, nothing is shown.
- **Download page** (`website/download/download.js`): shows the Cloudflare `total` alone as
  "N installers downloaded so far." whenever it is non-zero; any failure leaves the line hidden.

## The website

`website/download/` builds every card at runtime from `GET /api/releases/<channel>` — filenames,
sizes, SHA-256 and which platforms exist all come from the manifest, so a new build needs no
website change. Download buttons point at `downloads.privacyfence.eu/download/<channel>/<id>`.
An artifact id missing from the page's `PLATFORMS` map still renders under its raw id. Browser OS
detection only highlights the likely installer; it never hides the others.

**Pre-release section.** `newestPublishedPreRelease()` fetches `rc`, `beta` and `alpha` in
parallel (a failed or 404 fetch counts as unpublished), and `pickPreRelease()` returns the
manifest with the **highest version**, not the first channel that answers: compare
major.minor.patch, then stage (`rc` > `b` > `a`), then stage number. So a newer-cycle alpha
outranks a leftover rc from an older cycle. A manifest whose version does not parse is skipped.
The section names the channel from the manifest and stays hidden if nothing is published or the
winning manifest has no artifacts.

The failure paths are independent: a failed stable fetch shows a fallback linking to GitHub
Releases' latest release; an empty pre-release set renders nothing; a stats failure hides one
line. `tests/integration/test_download_page.py` exercises the page in headless Chromium with the
API stubbed.

**The site is built from a named list of files, not the `website/` directory.**
`scripts/build_site.py`'s manifest (`PAGES`, `STATIC`) is what ships; a file named in none of its
lists ships as a 404. `tests/unit/test_website_download_cta.py::test_every_website_file_is_actually_deployed`
fails if any file under `website/` is missing from them; add new website files to the manifest.
The build also pre-renders `/download/` from `/api/releases/stable` at deploy time, so the page
lists the current installers without JavaScript; `download.js` replaces those cards with the live
manifest, and keeps them (instead of showing the GitHub fallback) if the fetch fails.

## Cloudflare resources

Provisioned manually in the Cloudflare dashboard; everything else is reproducible from this
repository.

| Resource | Value |
| -------- | ----- |
| Zone | `privacyfence.eu` |
| Account ID | `326657b4f70af041996d60fd6b8f83fa` |
| Download domain | `downloads.privacyfence.eu` (Custom Domain on the Worker) |
| Worker | `privacyfence-downloads` |
| R2 bucket | `privacyfence-releases` — private (no Public Development URL, no Custom Domain) |
| R2 binding | `RELEASES` |
| D1 database | `privacyfence-downloads` |
| D1 database ID | `1cb5c99c-6d34-4031-a5f6-2f3a406e4979` |
| D1 binding | `DB` |

The D1 schema comes from this repo's migrations, never the dashboard. `workers_dev = false` and
`[[routes]] custom_domain = true` are committed in `wrangler.toml` so a `wrangler deploy` cannot
re-enable a `*.workers.dev` URL.

## Credentials

**There are two unrelated Cloudflare credential sets.** They differ in resource (Workers/D1 vs.
R2), purpose (deploying the Worker vs. publishing the release archive) and the workflows that read
them. Set them under repo **Settings → Secrets and variables → Actions**.

| Name | Kind | Used by | For |
| ---- | ---- | ------- | --- |
| `CF_DOWNLOADS_WORKER_API_TOKEN` | secret | `deploy-download-worker.yml` (as `CLOUDFLARE_API_TOKEN`) | `wrangler deploy` + D1 migrations |
| `CF_DOWNLOADS_WORKER_ACCOUNT_ID` | secret | `deploy-download-worker.yml` (as `CLOUDFLARE_ACCOUNT_ID`) | the account above |
| `CF_RELEASES_R2_ACCESS_KEY_ID` | secret | `build.yml`, `publish-pypi.yml` | S3 access to the release archive |
| `CF_RELEASES_R2_SECRET_ACCESS_KEY` | secret | `build.yml`, `publish-pypi.yml` | as above |
| `CF_RELEASES_R2_ENDPOINT` | **variable** (read as `vars.`) | `build.yml`, `publish-pypi.yml` | the bucket's S3-compatible endpoint URL |

`scripts/r2_release.py` exits with an error naming any of the three `CF_RELEASES_R2_*` values that
is missing (the `channel` and `check-tag` subcommands need none of them). The R2 pair is an R2 API
token scoped to the `privacyfence-releases` bucket.

The Worker reaches R2 through its binding, never through the R2 pair, so a green
`deploy-download-worker.yml` says nothing about the R2 credentials, and vice versa. Nothing
exercises the R2 credentials until a tag push; verify them out of band after any rotation.

### Use account-owned API tokens

Mint **both** tokens — the Worker deploy token and the R2 release-archive token — under
**Manage Account → Account API Tokens** (Super Administrator role required), not My Profile → API
Tokens. An account-owned token acts as a service principal; a user token stops working when that
user's access changes, which this repo would only notice as a failed deploy or a failed release.

Permissions for the Worker deploy token:

| Scope | Permission | Needed for |
| ----- | ---------- | ---------- |
| Account | D1 · Edit | `wrangler d1 migrations apply --remote` |
| Account | Workers Scripts · Edit | `wrangler deploy` |
| Account | Workers R2 Storage · Edit | resolving the `RELEASES` binding |
| Zone | Workers Routes · Edit | the `downloads.privacyfence.eu` custom domain |
| Zone | Zone · Read | resolving that zone at deploy time |

Scope Zone Resources to `privacyfence.eu`, and **leave the expiry empty** — a dated token becomes
a silent release outage.

### Verify a token before storing it

A stored, non-empty secret is not a working credential: Cloudflare rejects an unauthorized token
with error `7403` (`The given account is not valid or is not authorized to access this service`).
An account-owned token cannot call `/user/tokens/verify`, and `wrangler whoami` reports on the
user rather than the token, so use the account paths:

```
ACCT=326657b4f70af041996d60fd6b8f83fa
curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/tokens/verify" \
  -H "Authorization: Bearer $CF_TOKEN"
curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/d1/database" \
  -H "Authorization: Bearer $CF_TOKEN"
```

Expect `"status": "active"` from the first and the `privacyfence-downloads` database — not
`7403` — from the second. The second call is the one that distinguishes a good token from an
unauthorized one. When renaming a secret, rename it in GitHub and in the workflow together: a
workflow reading a name that does not exist gets an empty value.
