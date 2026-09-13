# Release Publishing & Download KPI — Phased Implementation Plan

Status: **Phases 0, 1 and 1.1 are done.** `deploy-download-worker.yml` run 8
(`workflow_dispatch` on `main` at `54942a1`, 2026-09-13 14:43Z) is the first green one: "Apply D1
migrations", "Deploy Worker" and "Smoke-test /health" all passed, so the `download_counts` table
exists in the real D1 database, the Worker is deployed and bound to `downloads.privacyfence.eu`
(custom domain), and that host answers `/health` with 200. Phase 1's exit criteria are met.

Getting there took 5 failed runs and two unrelated credential faults, recorded here because the
lesson outlived them: **a stored secret is not a working credential, and CI is where that
difference shows up.**

1. **Runs 1–4 (through 2026-09-13 08:21Z): an unauthorized token.** The job received a non-empty
   token and Cloudflare rejected it — `7403`, `The given account is not valid or is not authorized
   to access this service`, against account `326657b4f70af041996d60fd6b8f83fa`'s D1 API. This
   contradicted Phase 0's "confirmed present" note: the secret had a value, but that value was
   never authorized for this account's D1/Workers resources.
2. **Run 5 (2026-09-13 13:09Z): no token at all.** The secret rename in `95993a1` (#349) updated the
   workflow to read `CF_DOWNLOADS_WORKER_API_TOKEN` / `CF_DOWNLOADS_WORKER_ACCOUNT_ID`, but the
   GitHub secrets were never re-created under those names, so the step ran with both env vars empty.

Both were fixed by hand, not in code: an **account-owned API token** (see Prerequisites below for
the token type, permission table and pre-flight verification) stored under the renamed secrets.

**The release-archive R2 credentials are in place and verified too** (2026-09-13), out-of-band
rather than by CI: `CF_RELEASES_R2_ACCESS_KEY_ID` / `CF_RELEASES_R2_SECRET_ACCESS_KEY` /
`CF_RELEASES_R2_ENDPOINT` were re-created under their renamed keys and checked directly against
`privacyfence-releases`. That out-of-band step was necessary because no workflow exercises them
until a tag push — `deploy-download-worker.yml` reaches R2 through the Worker's `RELEASES` binding,
never through S3 credentials, so run 8 said nothing about them, and the alternative was finding out
mid-release. Keep that in mind when either credential is next rotated: a green
`deploy-download-worker.yml` is not evidence about the R2 pair, and vice versa.

**Phase 2 is done, proven end-to-end by `v4.0.0a14`** (`build.yml` run 89, 2026-09-13 16:37Z). That
run's `finalize-release` job wrote `releases/alpha/4.0.0a14/manifest.json`, verified every artifact
it references against the size and SHA-256 recorded in R2, promoted `alpha/latest.json`, and then
its `HEAD`-only smoke test resolved all three installers through the live Worker:

```
ok: macos-arm64
ok: windows-x64
ok: linux-x64
```

So the whole chain now works in production: tag → build → R2 upload → manifest → verify →
`latest.json` → Worker resolution.

**The release before it is worth keeping on the record, because it proved the safety property
rather than the happy path.** `v4.0.0a13` (run 88) failed in `build-windows`, for a reason with
nothing to do with this plan: `2aa93d2` had added a hash-pinned
`pip install --require-hashes -r requirements/runtime.lock.txt` to the build jobs, and on Windows
`mcp` pulls `pywin32`, which the then-Linux-only lock did not contain. `finalize-release` was
skipped, exactly as designed — so `4.0.0a13`'s DMG, `.deb` and SBOMs sit in R2 to this day with
**no manifest and no `latest.json` pointing at them**. An incomplete release existed in the bucket
without ever becoming the one the Worker serves. That is the transactional guarantee working under
real conditions, and it is better evidence than any test. (The lock gap was fixed separately by
switching generation to `uv pip compile --universal`, which emits
`pywin32==312 ; sys_platform == 'win32'` with hashes — see `#358`.)

**Phase 3 is unblocked, and one of its bullets is already proven**: route resolution through
`latest.json` → manifest → R2 object works for all three platforms. What remains is precisely what
`HEAD` cannot exercise, because `HEAD` deliberately never counts — real `GET` streaming, the D1
counter incrementing once per download start, `Range: bytes=0-` counting while a `bytes=N-` resume
does not, and `/api/stats/downloads` reflecting reality. Phase 4 has landed but is likewise not yet
confirmed in production (see its own section). Use this doc to scope a single session/PR to one
phase — e.g. "implement phase 2 of the release publishing plan" refers to a phase heading below.

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
  `CF_DOWNLOADS_WORKER_ACCOUNT_ID` (`326657b4f70af041996d60fd6b8f83fa`). Both are in place and
  proven working as of run 8 (2026-09-13 14:43Z) — see the Status note at the top of this doc for
  the two faults that preceded that. If the pre-rename `CLOUDFLARE_API_TOKEN` /
  `CLOUDFLARE_ACCOUNT_ID` secrets are still present, delete them — nothing reads them any more, and
  the token behind them was the unauthorized one.
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

**Resolved (2026-09-13 14:43Z, run 8).** Both faults were fixed by hand, in this order, and the
re-run went green on all three credentialed steps:

1. An **account-owned API token** (Manage Account → Account API Tokens) created with the permission
   table under Prerequisites above, and verified against
   `/accounts/<id>/tokens/verify` and the account's D1 list endpoint *before* being stored. The old
   token was not reused — it was the one returning 7403, and a user token cannot be converted into
   an account-owned one.
2. Stored as `CF_DOWNLOADS_WORKER_API_TOKEN`, with `CF_DOWNLOADS_WORKER_ACCOUNT_ID` alongside it.
3. The release-archive credentials re-created under their new names at the same time
   (`CF_RELEASES_R2_ACCESS_KEY_ID` / `CF_RELEASES_R2_SECRET_ACCESS_KEY` as secrets,
   `CF_RELEASES_R2_ENDPOINT` as a repository **variable**), then verified out-of-band against the
   bucket — see the Status note at the top for why CI could not do it: no workflow exercises them
   before a tag push, because the Worker reaches R2 through its `RELEASES` binding rather than S3
   credentials.
4. `deploy-download-worker.yml` re-run via `workflow_dispatch`.

Worth keeping in mind for any future deploy: `wrangler.toml` commits `workers_dev = false`, so that
first successful deploy disabled the `*.workers.dev` URL, and the job's `/health` smoke test only
ever probes `downloads.privacyfence.eu`. The custom domain has to be genuinely live — a successful
deploy with a misconfigured domain still fails the job.

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
`https://downloads.privacyfence.eu/health` returns 200; all Worker unit tests pass in CI. **Met** —
run 8 (`workflow_dispatch` on `main` at `54942a1`, 2026-09-13 14:43Z), after the credential fixes
recorded in the Phase 0 update above. `verify` had been green throughout; `deploy` passed "Apply D1
migrations", "Deploy Worker" (bound to `downloads.privacyfence.eu` as a custom domain) and
"Smoke-test /health" for the first time on that run.

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

**Two decisions this phase settled**, both worth knowing before touching this code again:

- **The manifest lists installers only.** SBOMs, the org-config scripts and the sdist/wheel are
  uploaded to the same prefix but never enter `artifacts[]`. This is what keeps the KPI definition
  above true: `cloudflare/downloads/src/index.ts` counts *every* artifact it serves and
  `queryStats()` sums every row without filtering on `artifact_kind`, so anything listed in a
  manifest is, by construction, countable. Leaving non-installers out is therefore a correctness
  property, not a tidiness preference — the alternative was adding a kind filter to Phase 1's
  already-deployed stats query. It also means `finalize-release` needs nothing from
  `publish-pypi.yml`, whose sdist/wheel it could not have waited for anyway (`needs:` does not
  reach across workflow files).
- **SHA-256 is recorded as R2 object metadata at upload time, not computed at finalize time.**
  `finalize` runs in its own job on a clean runner, where none of the installers built by the
  other three jobs exist on disk; reading the digest back from `head_object()` is what makes both
  the manifest and the immutability guard one metadata call per object instead of a re-download.
  `sbom` is a hard dependency after all: SBOMs are not in the manifest, but a tag whose SBOM
  generation failed has not fully shipped and should not become `latest` either.

**Exit criteria:** unit tests green — **met** (`tests/unit/test_r2_release.py`, covering the
manifest shape against the Worker's own fixtures, the ordering guarantee, the immutability guard,
and every `verify` failure mode). A real tag push (or a manual `workflow_dispatch` rerun against
an already-tagged commit) produces `releases/<channel>/<version>/manifest.json` and updates
`<channel>/latest.json` in R2 — **met** by `v4.0.0a14` (run 89), including the `HEAD`-only smoke
test resolving all three installers through the live Worker; see the Status note at the top. A
deliberately-broken run (one installer missing) leaves `latest.json` unchanged — **met**, by test
rather than by hand.

## Phase 3 — Wire the Worker to real release data

Rollout doc's "Phase C". Validation only — a PR only if bugs surface.

- Point the already-deployed Worker (Phase 1) at whatever real stable/pre-release versions
  Phase 2 has published (no new tag needed — use the latest existing tag).
- Manually verify: macOS/Windows/Linux downloads stream correctly; D1 counters increment once
  per real download; `Range: bytes=0-` counts; `Range: bytes=N-` resume doesn't;
  `/api/stats/downloads` reflects reality.
- Fix anything found as a small patch to `cloudflare/downloads/src/index.ts`, with a regression
  test added to Phase 1's suite — don't let Phase 3 grow its own untested surface.

**Starting point (2026-09-13).** `alpha/latest.json` points at `4.0.0a14`, so this phase has real
data to work against without cutting a new tag. Two things are already confirmed in production and
need no re-checking:

- **Route resolution works for all three platforms** — run 89's `HEAD` smoke test resolved
  `/download/alpha/{macos-arm64,windows-x64,linux-x64}` through `latest.json` → manifest → R2.
- **`/health` returns `{"status":"ok"}`**, and the bare root `/` returns 404
  `{"error":"no such route"}`. The 404 is correct, not a defect: `index.ts`'s handler serves
  `/health`, `/api/*` and `/download/*` and falls through to `notFound("no such route")` for
  everything else. Whether `/` should instead redirect to `privacyfence.eu/download/` is a Phase 5
  product question, not a bug to fix here.

**Verified in production against `alpha`/`4.0.0a14` (2026-09-13).** Counters started at
`{"total":0}`, so every number below is unambiguously from this run:

| Request | Result | Counted? |
| ------- | ------ | -------- |
| `GET /download/alpha/linux-x64` | `200`, 47,028,460 B, `application/vnd.debian.binary-package` | yes |
| `HEAD /download/alpha/linux-x64` | `200` | no |
| `GET` with `Range: bytes=0-1023` | `206` | yes |
| `GET` with `Range: bytes=1024-` | `206` | **no** |
| `GET /api/releases/alpha` | `200` | no |

Final state: `{"total":2,"by_channel":{"alpha":2},"by_platform":[{"linux","x64",2}]}` — exactly the
two download starts, attributed to the right channel and platform. The `bytes=1024-` row is the one
that matters most: a naive implementation counts every `206` and inflates the KPI every time
someone's download drops and resumes. `isDownloadStart()` gets it right against the live Worker,
not just under Miniflare.

Running this against `alpha` rather than `stable` was deliberate — `download_counts` records the
channel, so this deliberate verification traffic stays separable from real user downloads forever.

Two definition-of-done bullets are structurally guaranteed rather than observable this way, and are
worth recording as such: `download_counts` has no IP, cookie, fingerprint or User-Agent column at
all (see `migrations/0001_download_counts.sql`), and the browser never reaches R2 directly, since
every byte above came through the Worker.

**Exit criteria:** all download/counting "definition of done" bullets (see bottom of this doc)
that depend on real data hold in production, with no website change yet. (The original wording
offered `*.workers.dev` as the access path; that URL no longer exists — the committed
`workers_dev = false` took effect on Phase 1's first successful deploy, so this phase is verified
against `downloads.privacyfence.eu`.)

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

**Implemented.** The filter+sum logic was extracted into `scripts/release_stats.py` rather than
left inline, with `tests/unit/test_release_stats.py` covering it (`is_installer_asset`/
`compute_stats` are pure functions, so no HTTP mocking is needed) — closing the coverage gap this
phase's own wording flagged as merely acceptable if left inline. It reuses
`classify_installer()` from Phase 2's `scripts/r2_release.py` rather than a second, separately
maintained filename filter: a GitHub Release for a stable tag is built from the same DMG/
`-setup.exe`/`.deb` files that script's manifest already classifies as installers, so one
definition serves both the Cloudflare and GitHub halves of the KPI. `pages.yml` now calls
`python3 scripts/release_stats.py --repo "$REPOSITORY" --output _site/release-stats.json` instead
of its old inline heredoc, and `website/stats.js` reads `github_installer_downloads` instead of the
old, ambiguous `downloads` field. **Not yet met**: this hasn't run in production yet (needs a push
to `main` or the next scheduled `pages.yml` run) to confirm the deployed `release-stats.json`
matches a manual count on the real release list.

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

**Implemented.** `website/download/index.html` + `download.js` build every card at runtime from
`GET /api/releases/<channel>` — filenames, sizes, checksums and which platforms exist all come from
the manifest, so a new build needs no website change. `stats.js` now sums the GitHub and Cloudflare
halves into one headline number, with the Cloudflare half resolving to 0 on failure so a Worker
outage understates the total rather than hiding the social proof or blocking anything.

`tests/integration/test_download_page.py` drives the real page in headless Chromium with the Worker
API stubbed at the network layer (`page.route`), which is what lets it assert the behaviours this
phase actually specifies rather than just that some markup exists — OS detection highlights exactly
one card while all three stay visible, buttons point at the Worker, the beta block stays hidden when
that channel 404s, stats vanish on a stats outage without touching the downloads, and a stable-
metadata failure falls back to GitHub Releases so the page is never a dead end. Stubbing rather than
calling the live Worker is deliberate: a test that hit production would need the network, would be
flaky on a deploy, and would inflate the very counter this plan exists to keep honest.

**Exit criteria:** `privacyfence.eu/download/` live and correct in production; homepage
unchanged; every download from that page increments Cloudflare's counters as in Phase 3.
**Not yet met** — the code has landed but Pages has not deployed it, so nothing has been clicked in
production. Confirm on the deployed page before trusting Phase 6's cutover.

## Phase 6 — Cutover

Rollout doc's "Phase F". Smallest-diff, highest-visibility phase — ship alone so it's trivially
revertible.

**Change:**

- `website/index.html` — change the header Download button, hero "Download PrivacyFence" button,
  and footer "Download latest release" button from
  `github.com/privacyfence/privacyfence/releases/latest` to `https://privacyfence.eu/download/`.
  GitHub stays linked separately for source/docs.

**Implemented**, with one deviation and one addition worth knowing:

- The CTAs use a **relative** `href="download/"` rather than the absolute
  `https://privacyfence.eu/download/` this section originally specified. Same destination on the
  deployed site, but it also works on a Pages preview deploy and in local preview, and it matches
  the internal-link convention the page already uses for its anchors. The URL form was incidental
  to this phase's intent; pointing away from GitHub Releases was the point.
- `tests/unit/test_website_download_cta.py` guards the cutover statically. This phase's failure
  mode is asymmetric: revert the download page without the CTAs (or the reverse) and every
  "Download" button on the homepage points at nothing. That deserves an assertion which runs on
  every machine with no browser, so it is a plain unit test rather than an addition to Phase 5's
  Chromium suite, which skips wherever Chromium is missing. Verified to fail on a CTA reverted to
  GitHub Releases, and to pass once restored.

**This phase shipped without the production validation window the plan assumed.** Phase 5's own
exit criteria expect `/download/` confirmed live before the CTAs move; here both landed together at
the user's direction. The page's GitHub Releases fallback is what limits the downside — a broken
release API degrades the page rather than the CTA — but the homepage now depends on a page nobody
has clicked in production. Check it on the deployed site promptly; reverting this commit alone
restores the previous CTAs.

**Exit criteria:** all three primary CTAs point at the Worker-backed download page — **met**;
GitHub Releases still function as the documented secondary source — **met** (still linked from the
header, hero, connectors section, closing CTA and footer).

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
