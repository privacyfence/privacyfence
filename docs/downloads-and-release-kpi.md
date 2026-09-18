# Downloads and the installer-download KPI

How a PrivacyFence release reaches a user, and how installer downloads are counted. The runtime
code is the source of truth when behavior changes: `cloudflare/downloads/` (the Worker),
`scripts/r2_release.py` (publication), `scripts/release_stats.py` (the GitHub half of the KPI),
and `website/download/` (the page people actually use).

Release mechanics themselves — what a tag triggers, which channel publishes where — live in
[`CLAUDE.md`](../CLAUDE.md) § "Releasing"; this document covers the delivery and counting side.

## How a release reaches a user

```
git tag vX.Y.Z
      │
      ▼
GitHub Build & Release  ──► Build installers (macOS/Windows/Linux) ──► private Cloudflare R2
      │                                                                releases/<channel>/<version>/
      └──► GitHub Release (notes + stable binary mirror)                       │
                                                                               ▼
                                                                       release manifest
                                                                               │
                                                                               ▼
                                                                   channel "latest" pointer
                                                                               │
                                                                               ▼
                                                              downloads.privacyfence.eu (Worker)
                                                                  │                    │
                                                                  │                    └─ D1 counters
                                                                  ▼
                                                            private R2 object
                                                                  ▲
                                                                  │
                                                          privacyfence.eu/download/
```

**R2 stays private.** The browser never receives R2 credentials or a direct R2 endpoint — the
Worker is the only public path to a release artifact. That is what makes the download count a full
count rather than a sample: nothing can enumerate the bucket, hotlink an object, or fetch a release
without passing the route that counts it.

It does *not* restrict who may download a pre-release. Every channel the Worker knows is served
unauthenticated. See `CLAUDE.md` § "Who can download a pre-release" for that decision and the
two-step path to reversing it.

## The download Worker

`cloudflare/downloads/` — deployed to `downloads.privacyfence.eu` by
`.github/workflows/deploy-download-worker.yml` on any change to that tree on `main`.

Routes:

- `GET /download/<channel>/<platform-arch>` — resolves `releases/<channel>/latest.json` → that
  version's manifest → the matching artifact, and streams it from the `RELEASES` binding.
  Never a redirect to a public URL.
- `GET /download/version/<version>/<platform-arch>` — the same, pinned to one version's own
  manifest.
- `GET /api/releases`, `GET /api/releases/<channel>` — release metadata.
- `GET /api/stats/downloads` — the Cloudflare half of the KPI.
- `GET /health`.

A bare `/` returns 404 `{"error":"no such route"}`. That is correct rather than a defect: the
handler serves `/health`, `/api/*` and `/download/*` and falls through for everything else.

`Content-Type`, `Content-Length`, `ETag`, `Content-Disposition: attachment` and `Accept-Ranges`
are set on downloads. CORS (`Access-Control-Allow-Origin`) is restricted to the website's own
origin and applies only to `/api/*` — download routes need no browser CORS.

### Counting semantics

`env.DB` is incremented only for `GET` requests that begin serving an artifact successfully
(`isDownloadStart()` in `src/artifacts.ts`):

| Request | Counted? |
| ------- | -------- |
| `GET /download/...` → 200 | yes |
| `GET` with `Range: bytes=0-...` | yes |
| `GET` with `Range: bytes=<N>-`, `N > 0` (a resume) | **no** |
| `HEAD`, `OPTIONS` | no |
| 404s, metadata and stats requests | no |

The resume row is the one that matters most: a naive implementation counts every `206` and
inflates the KPI every time someone's connection drops. All of the above is verified against the
live Worker, not only under Miniflare.

The counter is written asynchronously and best-effort — a D1 failure must never block or slow the
byte stream, and an R2 failure must not increment.

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

No IP address, user ID, cookie ID, fingerprint, email, Cloudflare Ray ID or User-Agent is stored
anywhere, in this table or any other. The schema has no column for any of them, which is a stronger
guarantee than a policy.

Because the channel is recorded, deliberate verification traffic stays separable from real user
downloads forever — run production checks against `alpha`, not `stable`.

## The KPI

The public number is **PrivacyFence installer downloads**, summed from two halves:

- **Cloudflare** — `GET /api/stats/downloads`, as above.
- **GitHub** — `scripts/release_stats.py`, run by `.github/workflows/pages.yml`, which sums
  `download_count` across release assets that are installers only (`*.dmg`, `*-setup.exe`,
  `*.deb`) and emits it as `github_installer_downloads`. It reuses `classify_installer()` from
  `scripts/r2_release.py` rather than maintaining a second filename filter, so one definition
  serves both halves.

SBOMs, checksums, org-admin scripts, source archives, the sdist/wheel, metadata requests and CI
probes never count.

**Release CI must use `HEAD`, never `GET`, on public download routes**, so that checking the
system never inflates the number it is checking. `build.yml`'s `finalize-release` smoke test does
exactly this.

`website/stats.js` adds the two halves into one headline figure, with the Cloudflare half resolving
to `0` on failure — an outage understates the total rather than hiding the figure or blocking the
page. The figure stays hidden entirely below `>= 50` downloads or `>= 10` stars, so social proof
never advertises an empty launch.

## Publication is transactional

`scripts/r2_release.py` (`channel`, `upload`, `finalize`, `verify`, `promote`), called from
`build.yml`. `finalize` writes `manifest.json`, verifies every artifact it references is really in
R2 with the recorded size and SHA-256, and only then rewrites the channel's `latest.json`.

A release missing any mandatory installer fails with `latest.json` untouched, so an incomplete
release can sit in the bucket without ever becoming the one the Worker serves. This has held under
real conditions: a tag whose Windows build failed left its DMG, `.deb` and SBOMs in R2 with no
manifest and no pointer to them.

Four properties worth knowing before changing any of it:

- **The manifest lists installers only.** SBOMs, org-config scripts and the sdist/wheel upload to
  the same prefix but never enter `artifacts[]`. This is correctness, not tidiness: the Worker
  counts every artifact it serves and the stats query sums every row without filtering
  `artifact_kind`, so anything a manifest lists is countable by construction — and the KPI says
  those files never count.
- **SHA-256 is recorded as R2 object metadata at upload time**, not computed during finalize, which
  runs on a clean runner where none of the installers exist on disk. An ETag is no substitute:
  uploads go multipart above a threshold, and a multipart ETag is not the object's MD5.
- **Uploads are immutable.** Identical bytes under an existing key are skipped (so re-running a
  partly-failed release job is safe); different bytes hard-fail rather than silently replacing
  something people may already have downloaded.
- **Not every installer is mandatory.** `scripts/r2_release.py`'s `_INSTALLERS` marks each
  recognized filename pattern required or optional; only the required ones (`REQUIRED_ARTIFACT_IDS`)
  gate `latest.json`. The macOS `.pkg` (#428 D2) is the one optional entry today — an additional,
  fully-automated-install option alongside the DMG, not a replacement for it — so a problem building
  or signing it can never stall the DMG/`.exe`/`.deb` from reaching "latest" the way a missing
  *mandatory* installer does. An optional installer still enters the manifest (and is downloadable
  and counted) whenever it is actually present.

`promote()` refuses to point a channel at a version with no manifest. `finalize` always writes the
manifest first, so this never fires on that path — but `promote` exists to be run by hand, and by
hand is exactly when a typo would otherwise leave every downloader on the channel resolving
`latest.json` to a 404 with the previous pointer already overwritten.

## The website

`website/download/` builds every card at runtime from `GET /api/releases/<channel>` — filenames,
sizes, checksums and which platforms exist all come from the manifest, so a new build needs no
website change. Browser OS detection only *highlights* the likely installer; it never hides the
others.

Three failure paths degrade independently: a stable-metadata failure falls back to GitHub Releases
so the page is never a dead end; a pre-release channel with nothing published renders nothing
rather than an empty invitation; a stats outage hides one line and touches nothing else.

The pre-release section follows whichever channel has a build, trying `rc`, then `beta`, then
`alpha`, and names the channel from the manifest. Hardcoding one channel is how that section once
stayed permanently hidden while a perfectly good build sat published one channel over.

**`pages.yml` builds `_site` from a hand-written list of files, not from the `website/`
directory.** A file not named there ships as a 404 no matter how many tests pass locally — that is
how `/download/` first shipped, while the homepage CTAs already pointed at it.
`tests/unit/test_website_download_cta.py::test_every_website_file_is_actually_deployed` now fails
if any file under `website/` is missing from that step. Add new website files to both.

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

The D1 schema comes from this repo's migration, never the dashboard. `workers_dev = false` and
`[[routes]] custom_domain = true` are committed in `wrangler.toml` so a later `wrangler deploy`
cannot silently re-enable a `*.workers.dev` URL.

## Credentials

**There are two unrelated Cloudflare credential sets. Do not conflate them** — they differ in
resource (Workers/D1 vs. R2), in purpose (deploying the Worker vs. publishing the release
archive), and in which workflow reads them. The names carry the distinction: `CF_` plus which
resource they are for.

| Secret / variable | Used by | For |
| ----------------- | ------- | --- |
| `CF_DOWNLOADS_WORKER_API_TOKEN` | `deploy-download-worker.yml` | `wrangler` deploy + D1 migrations |
| `CF_DOWNLOADS_WORKER_ACCOUNT_ID` | `deploy-download-worker.yml` | the account above |
| `CF_RELEASES_R2_ACCESS_KEY_ID` | `build.yml`, `publish-pypi.yml` | S3 uploads to the release archive |
| `CF_RELEASES_R2_SECRET_ACCESS_KEY` | `build.yml`, `publish-pypi.yml` | as above |
| `CF_RELEASES_R2_ENDPOINT` (a **variable**, not a secret) | `build.yml`, `publish-pypi.yml` | the bucket's S3 endpoint |

The Worker reaches R2 through its `RELEASES` binding, never through S3 credentials — so a green
`deploy-download-worker.yml` is no evidence at all about the R2 pair, and vice versa. Nothing
exercises the R2 credentials until a tag push, i.e. mid-release; verify them out of band after any
rotation rather than finding out then.

### Use account-owned API tokens

Create the Worker deploy token under **Manage Account → Account API Tokens** (Super Administrator
role required), *not* My Profile → API Tokens. Cloudflare recommends account-owned tokens wherever
a credential should not be associated with a particular user: they act as service principals, which
is what a CI deploy key is. A user token carries whoever created it, so it can stop working the
moment that person's role changes — an outcome this repo cannot detect except as a red release
build. The account scope is implicit in ownership. The same rule applies to the R2 token.

Permissions for the Worker deploy token:

| Scope | Permission | Needed for |
| ----- | ---------- | ---------- |
| Account | D1 · Edit | `wrangler d1 migrations apply --remote` |
| Account | Workers Scripts · Edit | `wrangler deploy` |
| Account | Workers R2 Storage · Edit | resolving the `RELEASES` binding |
| Zone | Workers Routes · Edit | the `downloads.privacyfence.eu` custom domain |
| Zone | Zone · Read | resolving that zone at deploy time |

Scope Zone Resources to `privacyfence.eu`, and **leave the expiry empty** — a dated token becomes a
silent release outage.

### Verify a token before storing it

**A stored secret is not a working credential, and CI is where that difference shows up.** The
Worker deploy went through five failed runs proving this: four carried a token Cloudflare rejected
with `7403` (`The given account is not valid or is not authorized to access this service`) though
the secret was present and non-empty, and one carried no token at all, because a rename landed in
the workflow while the GitHub secrets kept their old names.

Note the *account* verify path. An account-owned token is not entitled to call
`/user/tokens/verify`, and `wrangler whoami` reports on the authenticated user rather than the
token, so neither is a valid test here:

```
ACCT=326657b4f70af041996d60fd6b8f83fa
curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/tokens/verify" \
  -H "Authorization: Bearer $CF_TOKEN"
curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/d1/database" \
  -H "Authorization: Bearer $CF_TOKEN"
```

Expect `"status": "active"` from the first and the `privacyfence-downloads` database — not `7403` —
from the second. The second call is the one that distinguishes a good token from an unauthorized
one.
