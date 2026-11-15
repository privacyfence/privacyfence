# The release archive

How a tagged release's files are stored and how they reach a user. The code is the source of truth:
`scripts/r2_release.py` (publication to R2).

What a tag triggers, the channel scheme and the release checklist are in
[`releasing.md`](releasing.md); this document covers storage, delivery and the Cloudflare account.

## How a release reaches a user

A release is the GitHub Release (the SBOMs and the organization-bundle scripts), the organization
image on `ghcr.io` and `privacyfence-sync` on PyPI; every tag also archives its files in the private
Cloudflare R2 bucket described below. The download Worker that once served the archive over
`downloads.privacyfence.eu` is retired
([ADR 0301](adr/0301-releases-build-the-org-image-privacyfence-sync-and-the-sbom-only.md)).

## Release archive (R2)

Every tag push, stable and pre-release alike, uploads that release's files to the Cloudflare R2
bucket **`privacyfence-releases`** (`DEFAULT_BUCKET` in `scripts/r2_release.py`; the workflows
also set `R2_BUCKET: privacyfence-releases`). R2 is the one archive that holds every file of every
release, whatever else is also published elsewhere.

### Layout

```
releases/
  <channel>/
    <version>/
      <every uploaded file, by basename>
```

There is no manifest and no `latest` pointer: the archive only keeps what was uploaded.

- `<version>` is the version `setuptools_scm` resolves from the tag, e.g. `4.1.0` or `4.2.0b1` —
  never the `v`-prefixed tag name.
- `<channel>` is derived from the version's PEP 440 suffix: none → `stable`, `a` → `alpha`,
  `b` → `beta`, `rc` → `rc` (`channel_for_version()`). A between-tags dev version
  (`….dev<n>+g<sha>`) is rejected, so nothing can be uploaded for an untagged build.

### What uploads what

Every upload step below runs only on a tag ref (`if: startsWith(github.ref, 'refs/tags/')`), and
every job that resolves a version runs `r2_release.py check-tag` before publishing anything (see
[ADR 0022](adr/0022-one-release-tag-per-commit.md)).

| Workflow · job | Uploads to `releases/<channel>/<version>/` |
| -------------- | ------------------------------------------ |
| `build.yml` · `sbom` | `sbom-python.cdx.json` |
| `build.yml` · `finalize-release` | `scripts/sync_room_directory.py`, `scripts/sign_connector_config.py` and `privacyfence-connector-config-tool.html` |

### Public distribution per channel

| Channel | R2 | GitHub Release |
| ------- | -- | -------------- |
| `stable` | every file above | created by `finalize-release` with the SBOM and the org-config scripts attached; body from `CHANGELOG.md` |
| `alpha` / `beta` / `rc` | every file above | created, marked prerelease, **no files attached** |

A pre-release's files are stored only in R2.

### What the private bucket does and does not buy

The bucket is left at R2's default: private, no public bucket policy, no Public Development URL,
no Custom Domain. Nothing can enumerate the bucket or fetch an object without the credentials
below; the archive is a maintainer's store, not a public download path
([ADR 0300](adr/0300-the-download-page-and-worker-are-retired.md)).

### Publication

`scripts/r2_release.py` subcommands (from its `argparse`):

| Subcommand | Arguments | Does |
| ---------- | --------- | ---- |
| `channel` | `<version>` | prints the channel; exit 1 for a non-release version |
| `check-tag` | `--tag`, `--version` | exit 1 unless the resolved version is the one the pushed tag names |
| `upload` | `--version`, `[--bucket]`, `files…` | uploads each file to `releases/<channel>/<version>/<basename>` |

`--bucket` defaults to `$R2_BUCKET`, else `privacyfence-releases`.

- **SHA-256 is recorded as R2 object metadata (`sha256`) at upload time.** An ETag is no
  substitute: large uploads go multipart, and a multipart ETag is not the object's MD5.
- **Uploads are immutable.** Identical bytes under an existing key are skipped, so re-running a
  partly failed job is safe; different bytes hard-fail. A version that has published artifacts
  stays published — cut the next one.

## Cloudflare resources

Provisioned manually in the Cloudflare dashboard; everything else is reproducible from this
repository.

| Resource | Value |
| -------- | ----- |
| Zone | `privacyfence.eu` |
| Account ID | `326657b4f70af041996d60fd6b8f83fa` |
| R2 bucket | `privacyfence-releases` — private (no Public Development URL, no Custom Domain) |
| Relay Worker | `privacyfence-oauth-relay` ([`oauth-relay.md`](oauth-relay.md)) |
| Relay domain | `oauth.privacyfence.eu` (Custom Domain on the relay Worker) |
| Relay Durable Object class | `Registry` (SQLite-backed, binding `REGISTRY`) |

The download Worker `privacyfence-downloads`, its `downloads.privacyfence.eu` domain and its D1
database are retired ([ADR 0300](adr/0300-the-download-page-and-worker-are-retired.md)); remove
them in the Cloudflare dashboard if they still exist. `workers_dev = false` and
`[[routes]] custom_domain = true` are committed in the relay's `wrangler.toml` so a
`wrangler deploy` cannot re-enable a `*.workers.dev` URL.

## Credentials

**There are two unrelated Cloudflare credential sets.** They differ in resource (the relay Worker
vs. R2), purpose (deploying the Worker vs. publishing the release archive) and the workflows that
read them. Set them under repo **Settings → Secrets and variables → Actions**. The OAuth relay
Worker has its own deploy credentials, kept as secrets of the `oauth-relay-worker` environment.

| Name | Kind | Used by | For |
| ---- | ---- | ------- | --- |
| `CF_OAUTH_RELAY_WORKER_API_TOKEN` | secret (environment `oauth-relay-worker`) | `deploy-oauth-relay-worker.yml` (as `CLOUDFLARE_API_TOKEN`) | `wrangler deploy` for the relay Worker |
| `CF_OAUTH_RELAY_WORKER_ACCOUNT_ID` | secret (environment `oauth-relay-worker`) | `deploy-oauth-relay-worker.yml` (as `CLOUDFLARE_ACCOUNT_ID`) | the account above |
| `CF_OAUTH_RELAY_REVOKE_TOKEN` | secret (environment `oauth-relay-worker`) | `deploy-oauth-relay-worker.yml` (as the Worker secret `REVOKE_TOKEN`) | the relay's maintainer token |
| `CF_OAUTH_RELAY_GMAIL_CLIENT_ID` | secret (environment `oauth-relay-worker`) | `deploy-oauth-relay-worker.yml` (as the Worker secret `GMAIL_CLIENT_ID`) | the relay's registration-notification mail |
| `CF_OAUTH_RELAY_GMAIL_CLIENT_SECRET` | secret (environment `oauth-relay-worker`) | `deploy-oauth-relay-worker.yml` (as the Worker secret `GMAIL_CLIENT_SECRET`) | as above |
| `CF_OAUTH_RELAY_GMAIL_REFRESH_TOKEN` | secret (environment `oauth-relay-worker`) | `deploy-oauth-relay-worker.yml` (as the Worker secret `GMAIL_REFRESH_TOKEN`) | as above |
| `CF_RELEASES_R2_ACCESS_KEY_ID` | secret | `build.yml` | S3 access to the release archive |
| `CF_RELEASES_R2_SECRET_ACCESS_KEY` | secret | `build.yml` | as above |
| `CF_RELEASES_R2_ENDPOINT` | **variable** (read as `vars.`) | `build.yml` | the bucket's S3-compatible endpoint URL |

`scripts/r2_release.py` exits with an error naming any of the three `CF_RELEASES_R2_*` values that
is missing (the `channel` and `check-tag` subcommands need none of them). The R2 pair is an R2 API
token scoped to the `privacyfence-releases` bucket.

A green `deploy-oauth-relay-worker.yml` says nothing about the R2 credentials, and vice versa.
Nothing exercises the R2 credentials until a tag push; verify them out of band after any rotation.

### Use account-owned API tokens

Mint **both** tokens — the relay Worker deploy token and the R2 release-archive token — under
**Manage Account → Account API Tokens** (Super Administrator role required), not My Profile → API
Tokens. An account-owned token acts as a service principal; a user token stops working when that
user's access changes, which this repo would only notice as a failed deploy or a failed release.

Permissions for the relay Worker deploy token:

| Scope | Permission | Needed for |
| ----- | ---------- | ---------- |
| Account | Workers Scripts · Edit | `wrangler deploy` |
| Zone | Workers Routes · Edit | the `oauth.privacyfence.eu` custom domain |
| Zone | Zone · Read | resolving that zone at deploy time |

Scope Zone Resources to `privacyfence.eu`, and **leave the expiry empty** — a dated token becomes
a silent release outage.

### Verify a token before storing it

A stored, non-empty secret is not a working credential: Cloudflare rejects an unauthorized token
with error `7403` (`The given account is not valid or is not authorized to access this service`).
An account-owned token cannot call `/user/tokens/verify`, and `wrangler whoami` reports on the
user rather than the token, so use the account path:

```
ACCT=326657b4f70af041996d60fd6b8f83fa
curl -s "https://api.cloudflare.com/client/v4/accounts/$ACCT/tokens/verify" \
  -H "Authorization: Bearer $CF_TOKEN"
```

Expect `"status": "active"`, not `7403`. When renaming a secret, rename it in GitHub and in the
workflow together: a workflow reading a name that does not exist gets an empty value.
