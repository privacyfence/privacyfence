# Release Publishing & Download KPI — Phased Implementation Plan

Status: Phase 1 code is implemented (`cloudflare/downloads/`,
`.github/workflows/deploy-download-worker.yml`) and Phase 1.1 is done, but Phase 1's own exit
criteria are **not yet met**: every `deploy-download-worker.yml` run on `main` so far (5/5, most
recently 2026-09-13 13:09Z) fails in the `deploy` job's "Apply D1 migrations" step, before
`wrangler deploy` or the `/health` smoke test ever run. `verify` (typecheck/tests/dry-run bundle,
no credentials involved) passes every time; only the credentialed `deploy` job fails. There have
been **two different causes**, and they must be fixed in order:

1. **Runs 1–4 (through 2026-09-13 08:21Z): an unauthorized token.** The job received a non-empty
   token and Cloudflare rejected it — `7403`, `The given account is not valid or is not authorized
   to access this service`, against account `326657b4f70af041996d60fd6b8f83fa`'s D1 API. This
   contradicts Phase 0's "confirmed present" note below: the secret had a value, but that value was
   never authorized for this account's D1/Workers resources.
2. **Run 5 (2026-09-13 13:09Z, current): no token at all.** The secret rename that landed in
   `95993a1` (#349) updated the workflow to read `CF_DOWNLOADS_WORKER_API_TOKEN` /
   `CF_DOWNLOADS_WORKER_ACCOUNT_ID`, but the GitHub secrets were never re-created under those
   names, so the step now runs with both env vars empty (`In a non-interactive environment, it's
   necessary to set a CLOUDFLARE_API_TOKEN environment variable`). Re-creating the secrets with the
   *old* token value would simply return to cause 1.

**Fixing both is manual Cloudflare-dashboard/GitHub-settings work, not a code change**: create an
**account-owned API token** with the permissions listed under Prerequisites below, verify it against
the account's own API before storing it, put it in the `CF_DOWNLOADS_WORKER_API_TOKEN` secret
alongside `CF_DOWNLOADS_WORKER_ACCOUNT_ID`, and re-run `deploy-download-worker.yml`
(`workflow_dispatch`) to confirm a green run before treating Phase 1 as exited.

The same rename applies to the release-archive credentials (`CF_RELEASES_R2_*`, see the Prerequisites
section), which no workflow has exercised since — **the next tag push will fail on every R2 upload
step** unless those are re-created under their new names too, with `CF_RELEASES_R2_ENDPOINT` as a
repository *variable* rather than a secret. Phase 2 has not been started (`scripts/r2_release.py` still only has its original `channel`
and `upload` subcommands — no `finalize`/`verify`/`promote`, no manifest schema). Use this doc to
scope a single session/PR to one phase — e.g. "implement phase 2 of the release publishing plan"
refers to a phase heading below.

## Goal

Make `privacyfence.eu` the primary place users discover and download PrivacyFence releases,
while keeping release binaries in the existing private Cloudflare R2 bucket, supporting
stable/beta/rc/alpha channels, reliably counting real installer downloads, preserving GitHub
Releases as a secondary download source, avoiding IP/cookie/fingerprint tracking, publishing new
releases automatically from the existing tag-based CI, and making publication fail safely so an
incomplete release never becomes "latest".

The public KPI is **PrivacyFence installer downloads**, internally split by source
(Cloudflare vs. GitHub), version, channel, OS, architecture, and day/month. SBOMs, checksums,
org-admin scripts, metadata requests, source archives, and CI probes never count as downloads.

## Target architecture

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
                                                                    │                    └─ D1 download counters
                                                                    ▼
                                                              private R2 object
                                                                    ▲
                                                                    │
                                                            privacyfence.eu/download/
```

R2 stays private. The browser never receives R2 credentials or a direct R2 endpoint — the Worker
is the only public path to release artifacts.

## Prerequisites (Cloudflare-side, provisioned manually per the separate Cloudflare Setup Guide)

Each phase below states which of these it needs to already exist. As of writing, all of this is
done and confirmed before Phase 1 starts. Concrete resource identifiers, for direct use in
`wrangler.toml` and CI:

- **Cloudflare zone**: `privacyfence.eu`.
- **Account ID**: `326657b4f70af041996d60fd6b8f83fa`.
- **Download domain**: `downloads.privacyfence.eu` (Custom Domain attached to the
  `privacyfence-downloads` Worker below).
- **R2 bucket**: `privacyfence-releases` — confirmed private (Public Development URL disabled,
  no Custom Domain attached). Same bucket the existing `scripts/r2_release.py` upload pipeline
  writes to.
- **R2 binding name**: `RELEASES`.
- **D1 database**: `privacyfence-downloads` — created and **empty** (schema comes from this
  repo's migration, not the dashboard).
- **D1 database ID**: `1cb5c99c-6d34-4031-a5f6-2f3a406e4979` (goes in `wrangler.toml`'s
  `[[d1_databases]]` block).
- **D1 binding name**: `DB`.
- **Worker**: `privacyfence-downloads`, with both bindings above (`RELEASES` → the R2 bucket,
  `DB` → the D1 database); no Cloudflare Access in front of it; `*.workers.dev` left enabled for
  now (disabled only after Phase 3/production validation, together with `workers_dev = false` in
  the committed `wrangler.toml`).
- **Deploy credential — an account-owned API token, not a user one.** Cloudflare recommends
  account-owned tokens ("Account API Tokens") wherever a credential should not be associated with a
  particular user: they act as service principals, which is what a CI deploy key is. A user token
  carries whoever created it along with it, so it can lose access the moment that person's role
  changes — an outcome this repo cannot detect except as a red release build. Create it under
  **Manage Account → Account API Tokens** (Super Administrator role required), *not* My Profile →
  API Tokens, while in account `326657b4f70af041996d60fd6b8f83fa`; the account scope is then
  implicit in ownership. Permissions:

  | Scope   | Permission                | Needed for                                      |
  | ------- | ------------------------- | ----------------------------------------------- |
  | Account | D1 · Edit                 | `wrangler d1 migrations apply --remote`          |
  | Account | Workers Scripts · Edit    | `wrangler deploy`                                |
  | Account | Workers R2 Storage · Edit | resolving the `RELEASES` binding                 |
  | Zone    | Workers Routes · Edit     | the `downloads.privacyfence.eu` custom domain    |
  | Zone    | Zone · Read               | resolving that zone at deploy time               |

  Scope Zone Resources to `privacyfence.eu`, and leave the expiry empty — a dated token becomes a
  silent release outage. Account-owned tokens do carry zone permissions for zones in their own
  account, so both zone rows are available.

  **Verify before storing** (this is the step Phase 0 skipped — "present" is not "authorized"), and
  note the *account* verify path: an account-owned token is not entitled to call `/user/tokens/verify`,
  and `wrangler whoami` reports on the authenticated user, so neither is the test here.

  ```
  ACCT=326657b4f70af041996d60fd6b8f83fa
  curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/tokens/verify" \
    -H "Authorization: Bearer $CF_TOKEN"          # expect "status": "active"
  curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/d1/database" \
    -H "Authorization: Bearer $CF_TOKEN"          # expect privacyfence-downloads, NOT 7403
  ```

  The second call is the one that distinguishes a good token from the one that failed four times.

- **GitHub secrets**: `CF_DOWNLOADS_WORKER_API_TOKEN` (the account-owned token above) and
  `CF_DOWNLOADS_WORKER_ACCOUNT_ID` (`326657b4f70af041996d60fd6b8f83fa`). As of 2026-09-13 **neither
  exists under these names** — the rename in `95993a1` (#349) updated every reference in code but
  the secrets themselves were never re-created, so `deploy` now runs with both env vars empty; the
  older token stored under the pre-rename names was itself unauthorized. See the Status note at the
  top of this doc and Phase 0 below. Delete the stale `CLOUDFLARE_API_TOKEN` /
  `CLOUDFLARE_ACCOUNT_ID` secrets once the new ones are in place — nothing reads them any more.
  These are named and stored separately from, and additional to, the existing
  `CF_RELEASES_R2_ACCESS_KEY_ID` / `CF_RELEASES_R2_SECRET_ACCESS_KEY` / `CF_RELEASES_R2_ENDPOINT`
  used by the release-upload pipeline (`scripts/r2_release.py`) — do not conflate or remove those;
  they're a different Cloudflare credential (R2, not Workers/D1) for a different purpose (the
  private release archive, not the downloads Worker), which is also why the names no longer share
  a `CLOUDFLARE_`/`CF_R2_` split that made them easy to confuse — both now carry the `CF_` prefix
  plus which resource they're for (`RELEASES_R2` vs. `DOWNLOADS_WORKER`).

## Phase 0 — Preconditions check (no code)

Confirmed (2026-09-12): R2 bucket `privacyfence-releases` is still private, D1 database
`privacyfence-downloads` is empty, the `privacyfence-downloads` Worker's bindings are named
exactly `RELEASES`/`DB`, the `downloads.privacyfence.eu` custom domain is attached, and both
`CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` (since renamed to `CF_DOWNLOADS_WORKER_API_TOKEN`/
`CF_DOWNLOADS_WORKER_ACCOUNT_ID`, see Prerequisites above) are present in GitHub alongside the
existing `CF_R2_*` (since renamed to `CF_RELEASES_R2_*`) credentials. All identifiers needed for
`wrangler.toml` (account ID, D1 database ID, bucket/database/binding names) are recorded above. No
repo changes in this phase — it is implementation-ready; Phase 1 can start.

**Update (2026-09-13): "present" was confirmed, "authorized" was not, and Phase 1's actual CI runs
have since shown it isn't.** Runs 1–4 of `deploy-download-worker.yml` on `main` fail in `deploy`'s
"Apply D1 migrations" step with Cloudflare API error `7403` ("The given account is not valid or is
not authorized to access this service") against
`/accounts/326657b4f70af041996d60fd6b8f83fa/d1/database/1cb5c99c-6d34-4031-a5f6-2f3a406e4979/query`
— the secret had a value, but that value's permissions don't actually cover this account's D1 (and,
untested so far since the job never gets past migrations, possibly Workers) resources. Run 5, after
the `95993a1` rename, fails one step earlier still: both env vars are now empty, because the
secrets were never re-created under their new names. This phase's "implementation-ready" verdict
should have meant a successful `wrangler` call, not just a non-empty secret; re-verify by hand next
time before marking a Cloudflare-side precondition confirmed.

**Action needed (manual; nothing here is a code change):**

1. Create an **account-owned API token** (Manage Account → Account API Tokens) with the permission
   table under Prerequisites above, and verify it with the two `curl` calls recorded there *before*
   storing it anywhere. Do not reuse the old token value — it is the one that returned 7403, and a
   user token cannot be converted into an account-owned one.
2. Store it as `CF_DOWNLOADS_WORKER_API_TOKEN`, with `CF_DOWNLOADS_WORKER_ACCOUNT_ID` alongside it;
   delete the pre-rename `CLOUDFLARE_*` secrets.
3. Re-create the release-archive credentials under their new names too
   (`CF_RELEASES_R2_ACCESS_KEY_ID` / `CF_RELEASES_R2_SECRET_ACCESS_KEY` as secrets,
   `CF_RELEASES_R2_ENDPOINT` as a repository **variable**) — otherwise the next tag push fails on
   every R2 upload step, independently of anything the Worker does. Mint that key pair as an
   account-owned token too, for the same reason as step 1.
4. Re-run `deploy-download-worker.yml` via `workflow_dispatch` and confirm all three credentialed
   steps pass before Phase 1 is considered exited.

Note for step 4: `wrangler.toml` commits `workers_dev = false`, so the first successful deploy
disables the `*.workers.dev` URL, and the job's `/health` smoke test only ever probes
`downloads.privacyfence.eu`. The custom domain therefore has to be genuinely live — a successful
deploy with a misconfigured domain still fails the job, and Phase 1's exit criteria below should be
read as requiring the custom domain, not the `*.workers.dev` fallback they mention.

## Phase 1 — Worker infrastructure

Rollout doc's "Phase A". New tree only — `main`'s existing behavior doesn't change; the website
is not touched yet.

**Add:**

- `cloudflare/downloads/package.json`, `package-lock.json`, `wrangler.toml`. Binding names must
  match the dashboard exactly (`RELEASES` r2 binding, `DB` d1 binding). Commit
  `workers_dev = false` and `[[routes]] custom_domain = true` for `downloads.privacyfence.eu`
  from day one, even though the dashboard leaves `*.workers.dev` enabled during this phase — a
  later `wrangler deploy` must not be able to silently re-enable it.
- `cloudflare/downloads/migrations/0001_download_counts.sql`:

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

  No IP, user ID, cookie ID, fingerprint, email, Cloudflare Ray ID, or full User-Agent in any
  table, ever.

- `cloudflare/downloads/src/index.ts` implementing:
  - `GET /download/<channel>/<platform-arch>` and `GET /download/version/<version>/<platform-arch>`
    — resolve `releases/<channel>/latest.json` (or the version's own manifest for the
    version-pinned route) → manifest → matching artifact → stream from `env.RELEASES` directly,
    never redirect to a public URL.
  - `GET /api/releases/stable`, `/api/releases/beta`, `/api/releases/rc`, `GET /api/releases`,
    `GET /api/stats/downloads`, `GET /health`.
  - Counting semantics: increment `env.DB` only for `GET` requests that begin serving the
    artifact successfully — `Range: bytes=0-...` counts, `Range: bytes=<N>-` with `N>0` does not,
    `HEAD`/`OPTIONS`/404/metadata/stats requests never count. Record the counter
    asynchronously/best-effort — a D1 failure must never block or slow the byte stream.
  - `Content-Type`, `Content-Length`, `ETag`, `Content-Disposition: attachment`,
    `Accept-Ranges` set correctly; CORS (`Access-Control-Allow-Origin`) restricted to
    `https://privacyfence.eu` (and `https://www.privacyfence.eu` if used) and only on the
    `/api/*` routes — download routes need no browser CORS.
- `cloudflare/downloads/test/` covering, using local Miniflare/`vitest-pool-workers` R2+D1
  fixtures (never production infra):
  - stable/beta/rc/alpha channel resolution
  - valid installer download; missing installer → 404; missing manifest → safe failure
  - GET increments counter; HEAD does not
  - `Range: bytes=0-` increments; `Range: bytes=N-` does not
  - metadata API and stats API never increment
  - macOS/Windows/Linux artifact classification
  - D1 failure does not prevent download; R2 failure does not increment
  - Since no real manifest exists yet, use hand-written fixture `manifest.json`/`latest.json`
    files in the test harness only.
- `.github/workflows/deploy-download-worker.yml`: triggers on `cloudflare/downloads/**` changes
  to `main`. Steps: checkout, node setup, `npm ci`, unit tests, `wrangler` typecheck/dry-run,
  apply D1 migrations remotely, `wrangler deploy`, smoke-test `/health`. Uses only the
  `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` env vars wrangler's CLI itself requires (sourced
  from the `CF_DOWNLOADS_WORKER_API_TOKEN` / `CF_DOWNLOADS_WORKER_ACCOUNT_ID` GitHub secrets) — R2
  access is via the Worker binding, never embedded S3 credentials.

**Exit criteria:** `deploy-download-worker.yml` runs green on merge to `main`;
`https://downloads.privacyfence.eu/health` returns 200; all Worker unit tests pass in CI. **Not yet
met** — see the Status note at the top of this doc and the Phase 0 update above: `verify` (the
unit-test bullet) is green, but `deploy` has failed on every run so far — first on an unauthorized
Cloudflare API token, now on secrets that no longer exist under their renamed keys — before
`wrangler deploy` or the `/health` check ever run. Re-check this criterion once the account-owned
token and both secret families are in place.

(The `/health` bullet originally offered "or the still-enabled `*.workers.dev` URL while testing" as
an alternative. That is not actually available: the committed `workers_dev = false` takes effect on
the first successful deploy, and the workflow's smoke step probes the custom domain regardless.)

## Phase 1.1 — Bring the Worker tree under the repo-wide CI gates — done

Phase 1 added `cloudflare/downloads/` — this repo's second Node component — but the repo-wide
gates that should have picked it up still only knew about `mcpb/shim/`. Both gaps below were found
by a code-review pass after Phase 1 landed; they belonged to this plan rather than to that review,
since this tree is this plan's to own. Both are now closed.

**1. `dependency-audit.yml` didn't see this tree (R8).** Its `paths:` triggers named only
`mcpb/shim/package.json`/`package-lock.json`, and its `npm-audit` job was hard-scoped to
`working-directory: mcpb/shim` — so a Worker dependency change triggered nothing, and the weekly
scheduled run audited nothing here either. `.github/dependabot.yml` *does* cover the directory,
which is how the `sharp`/libheif CVEs behind the `overrides.sharp` pin surfaced: a Dependabot
alert, not this repo's own gate.

Copying the shim's policy verbatim wouldn't have fixed it. That policy audits `--omit=dev` on the
reasoning that dev dependencies "run in CI and on contributors' machines, never on an end user's."
This tree inverts that: `package.json` declares **zero runtime `dependencies`** — everything is
`devDependencies` (`wrangler`, `vitest`, `@cloudflare/vitest-pool-workers`, `typescript`,
`@cloudflare/workers-types`) — so `--omit=dev` here would audit an empty set and always pass. And
those dev dependencies are not build-time-only the way the shim's are: `wrangler` runs in a job
holding `CLOUDFLARE_API_TOKEN` and pushes code to a public production endpoint. A compromised
package in this dev tree has a materially larger blast radius than one in the shim's.

So this got its own severity decision rather than a copied one: the whole tree, dev dependencies
included, is now audited with plain `npm audit --audit-level=high` (no `--omit=dev`) in its own
`npm-audit-worker` job, blocking on high/critical — the deploy credential justifies treating dev
deps here as seriously as the shim treats its runtime ones. `dependency-audit.yml`'s own
severity-policy comment documents the reasoning at the point of use. `push`/`pull_request` now
both trigger on `cloudflare/downloads/package.json` and `package-lock.json`, same as the other two
manifests this workflow watches.

**2. ~~The Worker's tests are a post-merge gate, not a pre-merge one.~~ Done** — `3ae2544` (#345),
which shipped ahead of this note being updated: `deploy-download-worker.yml`'s `verify` job now
runs typecheck/`npm test`/dry-run bundle on `pull_request` too (same path filter as `push`), and
`deploy` (`needs: verify`) still only runs on `main`/dispatch, so a PR that breaks the Worker fails
its own checks instead of surfacing the break on `main` afterwards, and the Cloudflare credentials
`deploy` needs never reach a fork PR's run. Same fix the now-removed `automated-test-strategy-plan.md`
Phase 2.1 applied to `platform-windows` and Phase 8 to `org-mode-smoke`.

One thing that fix deliberately does *not* do, and the reason this item is worth reading rather
than just ticking: **`verify` reports, but it does not gate.** It carries the same `paths:` filter
as `push`, so it is intentionally still absent from `scripts/update_branch_protection.py`'s
`REQUIRED_STATUS_CHECKS` — that script's own comment (R3) explains why a `paths:`-filtered context
can't be a required check: it never reports at all on a PR that misses the filter, and a required
check that never reports blocks the merge indefinitely. So a red `verify` is visible on the PR but
will not stop it merging. Making it genuinely gating needs a job that always runs and
short-circuits when the paths don't match, so the context always reports; that is a separate,
deliberate change, not an oversight in the split above — and the same limitation applies to the
`npm-audit-worker` job item 1 just added: it's `paths:`-filtered too, so it reports but doesn't
gate either.

**Exit criteria:** a Worker dependency change triggers `dependency-audit.yml` and is audited under
a written, deliberate severity policy — met (item 1). The Worker's tests and typecheck run on
every PR that touches `cloudflare/downloads/**`, and `deploy-download-worker.yml` still runs them
before deploying, so a direct push to `main` cannot deploy an untested Worker — met (item 2).
Whether these per-PR checks should also *block* the merge — which needs an always-reporting job,
not a `paths:`-filtered one — is left as a deliberate open choice above rather than folded into
this phase's exit.

## Phase 2 — Release metadata pipeline

Rollout doc's "Phase B".

**Change:**

- `scripts/r2_release.py` — keep the existing `channel` command and upload mechanism untouched;
  add:
  - `upload --version VERSION FILE...` — auto-computes SHA-256; immutability guard: target
    object doesn't exist → upload; exists with the same checksum → allow (CI rerun); exists with
    a different checksum → **hard fail** (a published binary must never silently change).
  - `finalize --version VERSION` — resolves version/channel, confirms all expected installer
    objects are present in R2 with correct size/checksum, generates and uploads
    `releases/<channel>/<version>/manifest.json`, runs `verify`, and — **only if verification
    passes** — updates `releases/<channel>/latest.json` last. An incomplete release must be able
    to exist in R2 without ever becoming visible via `latest.json`.
  - `verify --version VERSION` — checks every object the manifest references actually exists,
    with matching size and SHA-256.
  - `promote --version VERSION` — writes/updates the channel's `latest.json` pointer, callable
    independently of `finalize` for manual re-promotion if ever needed.
  - Manifest schema (schema `1`):

    ```json
    {
      "schema": 1,
      "version": "4.3.0",
      "channel": "stable",
      "published_at": "2026-09-11T12:00:00Z",
      "artifacts": [
        {
          "id": "macos-arm64",
          "kind": "installer",
          "platform": "macos",
          "architecture": "arm64",
          "filename": "PrivacyFence-4.3.0.dmg",
          "key": "releases/stable/4.3.0/PrivacyFence-4.3.0.dmg",
          "size": 12345678,
          "sha256": "..."
        }
      ]
    }
    ```

  - `latest.json` pointer: `{"version": "4.3.0", "manifest": "releases/stable/4.3.0/manifest.json"}`.
- `tests/unit/test_r2_release.py` — extend for all new subcommands, including the immutability
  guard's failure path and the ordering guarantee (manifest/artifacts land before `latest.json`
  ever changes).
- `.github/workflows/build.yml` — add a `finalize-release` job,
  `needs: [build, build-windows, build-deb]` (decide whether `sbom` is also a hard dependency —
  default to yes, since SBOM generation is already part of every tagged build). This job must
  leave `latest.json` untouched if any mandatory installer is missing, making publication
  transactional from the user's perspective. This is additive — no existing `build.yml` job
  changes its own upload behavior.

**Exit criteria:** unit tests green; a real tag push (or a manual `workflow_dispatch` rerun
against an already-tagged commit) produces `releases/<channel>/<version>/manifest.json` and
updates `<channel>/latest.json` in R2 — verified by hand once. A deliberately-broken run (one
installer missing) leaves `latest.json` unchanged — verified by test, not just by hand.

## Phase 3 — Wire the Worker to real release data

Rollout doc's "Phase C". Validation only — a PR only if bugs surface.

- Point the already-deployed Worker (Phase 1) at whatever real stable/pre-release versions
  Phase 2 has published (no new tag needed — use the latest existing tag).
- Manually verify: macOS/Windows/Linux downloads stream correctly; D1 counters increment once
  per real download; `Range: bytes=0-` counts; `Range: bytes=N-` resume doesn't;
  `/api/stats/downloads` reflects reality.
- Fix anything found as a small patch to `cloudflare/downloads/src/index.ts`, with a regression
  test added to Phase 1's suite — don't let Phase 3 grow its own untested surface.

**Exit criteria:** all download/counting "definition of done" bullets (see bottom of this doc)
that depend on real data hold in production, still reachable only via `*.workers.dev` / with no
website change yet.

## Phase 4 — Fix the GitHub download KPI

Rollout doc's "Phase D".

**Change:**

- `.github/workflows/pages.yml` — filter the `download_count` sum to installer assets only
  (`*.dmg`, `*-setup.exe`, `*.deb`); explicitly exclude `*.spdx.json`, `*.cdx.json`, `*.py`,
  `*.whl`, `*.tar.gz`, checksum files, and any other release support files. Rename the emitted
  JSON field `downloads` → `github_installer_downloads` so its meaning is unambiguous.
- Consider extracting the filter+sum logic out of the workflow's inline Python into a small
  script under `scripts/` with a unit test, since `pages.yml`'s current inline Python has no test
  coverage today. If kept inline, note this as an accepted coverage gap rather than skip
  verifying it.

**Exit criteria:** the deployed site's `release-stats.json` shows `github_installer_downloads`
matching a manual count of installer-only assets on the current release list.

## Phase 5 — Website download page

Rollout doc's "Phase E". **Homepage CTAs are not touched in this phase** — `/download/` ships
and is validated in production standalone first.

**Add:**

- `website/download/index.html`, `website/download/download.js` — fetches
  `https://downloads.privacyfence.eu/api/releases/stable` (and beta) and builds download buttons
  dynamically; never hardcodes filenames. Browser OS detection may highlight the likely-correct
  installer but must never hide the others. Shows SHA-256/verification info and release notes;
  includes a "want to test the next version?" beta section using beta metadata.
- Static/browser tests: page renders; stable metadata loads; all available platforms appear; OS
  detection only highlights, never hides; buttons point at `downloads.privacyfence.eu`; beta
  section uses beta metadata; stats gracefully disappear if the stats API is unavailable.

**Change:**

- `website/stats.js` — fetch both `./release-stats.json` (now `github_installer_downloads` +
  `stars`) and `https://downloads.privacyfence.eu/api/stats/downloads`; compute
  `totalDownloads = githubInstallerDownloads + cloudflareInstallerDownloads`. Keep the existing
  "hide social proof until numbers are meaningful" behavior. A stats-API failure must never block
  the download page itself.
- `website/styles.css` — additions as needed for the new page.

**Exit criteria:** `privacyfence.eu/download/` live and correct in production; homepage
unchanged; every download from that page increments Cloudflare's counters as in Phase 3.

## Phase 6 — Cutover

Rollout doc's "Phase F". Smallest-diff, highest-visibility phase — ship alone so it's trivially
revertible.

**Change:**

- `website/index.html` — change the header Download button, hero "Download PrivacyFence" button,
  and footer "Download latest release" button from
  `github.com/privacyfence/privacyfence/releases/latest` to `https://privacyfence.eu/download/`.
  GitHub stays linked separately for source/docs.

**Exit criteria:** all three primary CTAs point at the Worker-backed download page; GitHub
Releases still function as the documented secondary source.

## Phase 7 — Release history

Rollout doc's "Phase G" — explicitly deferrable; only start once Phases 1–6 have been live long
enough to trust the download counts.

**Add:**

- `website/releases/index.html`, `website/releases/releases.js`, backed by `GET /api/releases`.
  Shows each version, channel, release date, available platforms, and links to release notes and
  downloads.

## Cross-cutting notes (apply to every phase)

- No version-bump commit, ever — this plan is entirely tag-driven, consistent with this repo's
  existing `setuptools_scm`-based release mechanics (see this repo's `CLAUDE.md` §"Releasing").
- Release-CI smoke checks (Phase 2's `finalize-release`, and any later checks that hit public
  download routes) must use `HEAD`, never `GET`, so CI itself never inflates the KPI. Example:
  `HEAD /download/stable/macos-arm64`, `.../windows-x64`, `.../linux-x64` — each should return a
  successful response and the expected installer filename.
- PyPI/TestPyPI publishing (`publish-pypi.yml`) stays out of this critical path entirely — it
  continues independently and is not a dependency of `finalize-release`; PyPI download stats are
  not mixed into this KPI.
- Every PR from this plan follows the branch naming (`feature/<kebab-case>`) and Definition of
  Done in `docs/coding-and-testing-guidelines.md` §2.7.

## Definition of done (whole project)

- `privacyfence.eu/download/` shows the current stable release; Windows, macOS, and Linux
  installers are available when built for that release.
- The browser never receives R2 credentials or direct private-bucket access; R2 stays private.
- Every website installer download passes through `downloads.privacyfence.eu`.
- Successful download starts increment D1; resume requests do not inflate the counter.
- No IP addresses or persistent visitor identifiers are stored anywhere.
- GitHub download statistics count installers only, not every release asset.
- The site shows Cloudflare + GitHub installer downloads as one total KPI.
- Stable/beta/rc/alpha remain distinct throughout.
- A failed/incomplete build can never replace `latest.json`.
- Tagged releases publish manifests automatically.
- CI verifies public download routes without increasing the KPI.
- Existing GitHub Release links continue to function as a secondary download source.
- The whole download/statistics infrastructure is fully reproducible from this repository (no
  manual dashboard-only state left uncommitted, other than the one-time Cloudflare provisioning
  in the Prerequisites section).
- The `cloudflare/downloads/` tree is held to the same CI standard as the rest of the repo: its
  dependencies are audited under a written severity policy, and its tests gate a PR rather than
  only reporting after the merge (Phase 1.1).

## Minimum useful first milestone

Phases 1–6, roughly: D1 → Worker + private R2 binding → `/download/...` route → reliable
counting → `/api/stats/downloads` → release manifest + `latest.json` → `/download/` website page
→ switch homepage CTA. Phase 7 (release history) and any richer analytics/dashboard/charts can
wait indefinitely after that.
