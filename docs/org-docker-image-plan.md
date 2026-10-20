# Plan: a Docker image release for organization mode

## Goal

Today an organization deployment means installing `privacyfence` from PyPI into a virtual
environment on a Linux server and writing a hardened systemd unit and a reverse-proxy config by hand
(`docs/org-mode-setup-guide.md` sections 3, 7 and 8). After this plan, every **stable** release also
publishes a multi-architecture (linux/amd64 and linux/arm64) container image,
`ghcr.io/privacyfence/privacyfence-org`, built from that release's wheel and the hash-pinned runtime
lock, tested in CI before it is published, and carrying a GitHub build-provenance attestation. A new
operator page, `docs/org-mode-docker.md`, ships a tested Compose file that runs the daemon with Caddy
in front, and covers installing the bundle, upgrades, backups and verifying the image.

Decisions the maintainer made at planning time: GHCR only; stable releases only; amd64 and arm64;
ship a Compose file with Caddy.

No tracking issue exists.

## Current state

- **Org mode install path**: `docs/org-mode-setup-guide.md` §3 ("Service account and install") does
  `pip install privacyfence` into `/opt/privacyfence/venv`, runs it as `privacyfence-org` with data in
  `/var/lib/privacyfence-org/.privacyfence/` (`paths.data_dir()`, `src/privacyfence/paths.py:154`,
  `Path.home() / ".privacyfence"` for a site-packages install, `paths.py:82`). §8 gives the systemd
  unit, which `tests/integration/test_org_guide_configs.py` parses from the guide by heading name
  (`_section`, `_fenced_block`, lines 51–80).
- **Listener**: the bundle's `server` section sets `bind_host` (default `127.0.0.1`), `port` (default
  `8765`) and `trusted_proxies` (exact IPs, `src/privacyfence/org_mode.py:70-137`;
  `scripts/build_org_bundle.py:337-355` has `--server-bind-host`, `--server-port`,
  `--server-trusted-proxy IP`). Host allowlist = issuer hostname + `127.0.0.1` + `::1` + bind host.
- **Startup**: `daemon_main.main()` refuses to start on an unsigned, malformed or idp-less org
  bundle, printing `Fatal error: ...` (an unsigned one: `... has "mode": "org" but is not signed`,
  `daemon_main.py:511`; a key or serial mismatch: `... -- refusing to start`, `daemon_main.py:490-500`). It pins the bundle key and serial into the `org/`
  directory (`daemon_main.py:516`), so `org/` must be writable. With **no** bundle it starts in
  local mode.
- **Shutdown**: the daemon installs no SIGTERM handler; SIGINT raises `KeyboardInterrupt`, which
  `run_app` catches and shuts down on ("Interrupted; shutting down", `daemon_main.py:2160`).
- **Release pipeline** (`docs/releasing.md`): a `v*` tag push starts `build.yml` (macOS `build`,
  `build-windows`, `build-deb`, `sbom`, then `finalize-release` with
  `needs: [build, build-windows, build-deb, sbom]`), `publish-pypi.yml` (waits for `build.yml` to
  succeed, stable only) and `publish-mcp-registry.yml` (its own workflow with a `dedupe` job and a
  copied `wait_for_build` job "so a registry outage there would [not] hold PyPI back"). Upload and
  publish steps in `build.yml` are gated `if: startsWith(github.ref, 'refs/tags/')`, so a dispatched
  `build.yml` builds and tests everything and publishes nothing (the pre-flight).
- **Telegram credentials** ship in every distribution (ADR 0040): `scripts/telegram_credentials.py
  write` before building, `check-dist [--require] <files>` after (`publish-pypi.yml:229-236`).
- **Locks**: `requirements/runtime.lock.txt` is `uv pip compile --universal` with hashes, so it
  installs on linux/arm64 as well; ADR 0182 says every tool a release job installs comes from a
  hash-pinned lock. All `uses:` lines are pinned to a commit SHA with a `# vX.Y.Z` comment.
- **Tests**: `tests/integration/test_org_ubuntu_release_smoke.py` (marks `system`, `packaged`) runs
  the real daemon as a subprocess against a signed synthetic bundle and `tests/integration/mock_idp.py`
  (a loopback HTTP OIDC double; the daemon needs `PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP=1` to accept
  its `http://` issuer). `tests/conftest.py:55-79` and `:208-224` turn a `packaged` skip into a
  failure under `PRIVACYFENCE_REQUIRE_PACKAGED=1` when the skip reason matches
  `no dist/|built yet|not installed|not on PATH|...|only meaningful|...`.
- **No Docker anything** exists in the repository today.
- **Measured during planning** (a prototype of the Design below, built in a cloud session): image
  ~530 MB uncompressed; a fresh named volume inherits the image's `10001:10001 0700` data directory;
  the daemon starts in org mode under `--read-only --tmpfs /tmp --cap-drop ALL --security-opt
  no-new-privileges` with `bind_host: 0.0.0.0`; `docker stop` with the default SIGTERM hits the 10 s
  timeout and exits 137, while `STOPSIGNAL SIGINT` exits 0 in 0.4 s.

## Design

### 3.1 Names

| Thing | Value |
|---|---|
| Image | `ghcr.io/privacyfence/privacyfence-org` |
| Platforms | `linux/amd64`, `linux/arm64` |
| Tags per stable release `X.Y.Z` | `X.Y.Z` always; `X.Y`, `X` and `latest` only when no already-tagged stable release is higher in that scope (§3.5) |
| Per-arch intermediate tags | `X.Y.Z-amd64`, `X.Y.Z-arm64` (pushed by the publish workflow, then combined) |
| Image files | `deploy/docker/Dockerfile`, `deploy/docker/docker-entrypoint.sh`, `deploy/docker/healthcheck.py` |
| Deploy files | `deploy/docker/compose.yaml`, `deploy/docker/Caddyfile` |
| Build script | `scripts/build_org_image.sh` |
| Tag script | `scripts/org_image_tags.py` |
| Build job | `build.yml` → `build-org-image` (matrix, job name `build-org-image (amd64)` / `build-org-image (arm64)`) |
| Per-PR job | `tests.yml` → `org-docker-image` (amd64 only; **not** added to `REQUIRED_STATUS_CHECKS`) |
| Publish workflow | `.github/workflows/publish-org-image.yml` (`name: Publish org Docker image`) |
| Workflow artifacts | `org-image-amd64`, `org-image-arm64`, each holding `privacyfence-org-<arch>.tar.gz` (deliberately not matching `finalize-release`'s `PrivacyFence-*` download pattern) |
| Local image tag in CI | `privacyfence-org:ci-<arch>` |
| Container user | `privacyfence-org`, uid/gid `10001` |
| Data volume path | `/var/lib/privacyfence-org/.privacyfence` |
| Operator doc | `docs/org-mode-docker.md`, title `# Organization deployment with Docker` |
| Compose network | `pf`, subnet `172.30.87.0/24`; Caddy at `172.30.87.2`, daemon at `172.30.87.10` |

### 3.2 `deploy/docker/Dockerfile` (exact)

```dockerfile
# syntax=docker/dockerfile:1
# The organization-mode image (docs/org-mode-docker.md). Built only through scripts/build_org_image.sh,
# which stages this directory's files, requirements/runtime.lock.txt and exactly one privacyfence
# wheel into the build context.
FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ARG PRIVACYFENCE_VERSION
LABEL org.opencontainers.image.title="PrivacyFence (organization mode)" \
      org.opencontainers.image.description="PrivacyFence's central organization-mode daemon" \
      org.opencontainers.image.source="https://github.com/privacyfence/privacyfence" \
      org.opencontainers.image.url="https://privacyfence.eu" \
      org.opencontainers.image.documentation="https://github.com/privacyfence/privacyfence/blob/main/docs/org-mode-docker.md" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${PRIVACYFENCE_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/privacyfence/venv/bin:$PATH

COPY runtime.lock.txt /tmp/build/runtime.lock.txt
COPY dist/ /tmp/build/dist/
# pip_cert is optional: only a build behind a TLS-intercepting proxy passes one.
RUN --mount=type=secret,id=pip_cert,required=false \
    if [ -f /run/secrets/pip_cert ]; then export PIP_CERT=/run/secrets/pip_cert; fi \
 && python -m venv /opt/privacyfence/venv \
 && pip install --require-hashes -r /tmp/build/runtime.lock.txt \
 && pip install --no-deps /tmp/build/dist/privacyfence-*.whl \
 && rm -rf /tmp/build

COPY --chmod=0755 docker-entrypoint.sh /opt/privacyfence/docker-entrypoint.sh
COPY --chmod=0644 healthcheck.py /opt/privacyfence/healthcheck.py

RUN groupadd --system --gid 10001 privacyfence-org \
 && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/privacyfence-org --create-home \
      --shell /usr/sbin/nologin privacyfence-org \
 && install -d -m 700 -o 10001 -g 10001 /var/lib/privacyfence-org/.privacyfence \
      /var/lib/privacyfence-org/.privacyfence/org

USER 10001:10001
WORKDIR /var/lib/privacyfence-org
VOLUME ["/var/lib/privacyfence-org/.privacyfence"]
EXPOSE 8765
# The daemon shuts down cleanly on SIGINT (KeyboardInterrupt); as PID 1 it would ignore SIGTERM.
STOPSIGNAL SIGINT
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "/opt/privacyfence/healthcheck.py"]
ENTRYPOINT ["/opt/privacyfence/docker-entrypoint.sh"]
```

The base image digest is the `python:3.12-slim-trixie` index digest at planning time (2026-10-20);
Dependabot (`docker` ecosystem, §3.9) bumps it.

### 3.3 `deploy/docker/docker-entrypoint.sh` and `deploy/docker/healthcheck.py` (exact)

```sh
#!/bin/sh
# Organization mode only: without an installed bundle the daemon would start in local mode,
# listening on the container's own loopback where nothing can reach it.
set -eu
bundle="$HOME/.privacyfence/org/org_config.json"
if [ ! -f "$bundle" ]; then
    echo "No organization bundle at $bundle -- install one first (docs/org-mode-docker.md, \"Install the bundle\")." >&2
    exit 78
fi
exec privacyfence-app "$@"
```

```python
"""Docker HEALTHCHECK for the organization image.

The probe docs/org-mode-setup-guide.md's Monitoring section names, made from inside the container
to the port the installed bundle sets (``server.port``, default 8765). ``127.0.0.1`` is always in
the daemon's Host allowlist.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request
from pathlib import Path

BUNDLE = Path.home() / ".privacyfence" / "org" / "org_config.json"
PROBE_PATH = "/.well-known/oauth-authorization-server"


def main() -> int:
    try:
        server = json.loads(BUNDLE.read_text(encoding="utf-8")).get("server") or {}
    except (OSError, ValueError):
        return 1
    port = int(server.get("port", 8765))
    tls = server.get("tls") or {}
    if tls.get("cert_file"):
        # The certificate names the public hostname, not 127.0.0.1; this checks liveness only.
        url, context = f"https://127.0.0.1:{port}{PROBE_PATH}", ssl._create_unverified_context()
    else:
        url, context = f"http://127.0.0.1:{port}{PROBE_PATH}", None
    try:
        with urllib.request.urlopen(url, timeout=5, context=context) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

If `ruff check .` flags `S310` or `S323` here, add `# noqa: S310` / `# noqa: S323` on that line;
the reason is the comment already above it (a loopback liveness probe).

### 3.4 `scripts/build_org_image.sh`

```
Usage: scripts/build_org_image.sh --platform linux/amd64|linux/arm64 --tag NAME:TAG [--version VERSION]
```

Behaviour, in order (`set -euo pipefail`, header comment in the style of `scripts/build_deb.sh`):

1. Parse the three options; anything else, or a missing `--platform`/`--tag`, prints the usage line
   to stderr and exits 2. `--platform` must be exactly `linux/amd64` or `linux/arm64`.
2. Require exactly one `dist/privacyfence-*.whl` (the `privacyfence-sync` wheel is never in `dist/`;
   `publish-pypi.yml` builds it into `sync-dist/`). Zero or several: print
   `expected exactly one dist/privacyfence-*.whl, found N -- build it with: python -m build --wheel --no-isolation`
   and exit 1.
3. `VERSION` defaults to the wheel filename's version field (`privacyfence-<version>-py3-none-any.whl`).
4. Stage the context: `rm -rf build/org-image && mkdir -p build/org-image/dist`; copy
   `deploy/docker/Dockerfile`, `deploy/docker/docker-entrypoint.sh`, `deploy/docker/healthcheck.py`,
   `requirements/runtime.lock.txt` (as `runtime.lock.txt`) and the wheel (into `dist/`).
5. Build: `docker buildx build --platform "$PLATFORM" --provenance=false --sbom=false
   --build-arg PRIVACYFENCE_VERSION="$VERSION" --load -t "$TAG" "${EXTRA[@]}" build/org-image`, where
   `EXTRA` gets `--secret id=pip_cert,src="$PIP_CERT_FILE"` when `PIP_CERT_FILE` is set, and
   `--network host --build-arg HTTPS_PROXY="$HTTPS_PROXY" --build-arg https_proxy="$HTTPS_PROXY"`
   when `HTTPS_PROXY` is set. (Neither is set on GitHub-hosted runners; both are needed in a Claude
   Code cloud container, whose proxy is on `127.0.0.1`.)
6. Print `Built $TAG ($PLATFORM, privacyfence $VERSION)`.

### 3.5 `scripts/org_image_tags.py`

Stdlib only, importable by file path (the `tests/unit/test_tag_release.py` pattern), no import of
other scripts.

- `image_tags(version: str, known_tags: list[str]) -> list[str]`:
  - `version` must match `^(\d+)\.(\d+)\.(\d+)$`, else raise `ValueError("org images are published for stable versions only, got <version>")`.
  - `known` = every entry of `known_tags` matching `^v(\d+)\.(\d+)\.(\d+)$`, as int triples
    (pre-release and malformed tags are ignored; the version's own tag may or may not be present).
  - Result, in this order: `"X.Y.Z"`; `"X.Y"` unless some known `(X, Y, z)` has `z > Z`; `"X"`
    unless some known `(X, y, z)` has `(y, z) > (Y, Z)`; `"latest"` unless some known triple is
    `> (X, Y, Z)`.
- `main(argv)`: `org_image_tags.py VERSION` reads `git tag --list "v*"` (subprocess, `check=True`,
  `cwd` = the repo root, `Path(__file__).resolve().parents[1]`), prints one tag per line, exits 0;
  on `ValueError` prints `error: <message>` to stderr and exits 2.

### 3.6 Image test: `tests/integration/test_org_docker_image.py`

`pytestmark = [pytest.mark.system, pytest.mark.packaged, pytest.mark.timeout(900)]`. Module-level
skips (reason strings exact, each matches `tests/conftest.py`'s `_PACKAGED_SKIP_REASON` so
`PRIVACYFENCE_REQUIRE_PACKAGED=1` turns them into failures):

- not Linux: `"only meaningful on Linux (the test uses --network host)"`;
- `shutil.which("docker")` is None: `"docker not on PATH"`;
- `PRIVACYFENCE_ORG_IMAGE` unset/empty: `"org image not built yet -- set PRIVACYFENCE_ORG_IMAGE (see scripts/build_org_image.sh)"`.

Helpers (module-private): `_docker(*args, input=None, check=True, timeout=180)` (subprocess list,
text mode, captures output); `_signed_bundle(idp_issuer, port) -> dict` built with
`privacyfence.org_bundle_signing.generate_keypair`/`sign_bundle` from
`{"mode": "org", "server": {"issuer_url": "https://pf.example.internal", "bind_host": "0.0.0.0", "port": port, "trusted_proxies": ["127.0.0.1"]}, "idp": {"issuer": idp_issuer, "client_id": "privacyfence-docker", "client_secret": "docker-test-secret"}}`;
a `volume` fixture creating `pf-docker-test-<uuid4 hex[:8]>` and removing it (`docker volume rm -f`)
on teardown; `_install_bundle(volume, payload: bytes)` running
`docker run --rm -i -v <volume>:/var/lib/privacyfence-org/.privacyfence --entrypoint sh IMAGE -c 'umask 077 && cat > /var/lib/privacyfence-org/.privacyfence/org/org_config.json'`;
`_start(volume, name)` running `docker run -d --name <name> --network host --read-only --tmpfs /tmp
--cap-drop ALL --security-opt no-new-privileges -e PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP=1
--health-interval 2s --health-start-period 60s -v <volume>:/var/lib/privacyfence-org/.privacyfence IMAGE`,
with a finalizer `docker rm -f <name>`; `_wait_ready(port, name, timeout=90)` polling
`GET http://127.0.0.1:<port>/.well-known/oauth-authorization-server` with `Host: pf.example.internal`
until 200, failing with the container's `docker logs` if it exits or the timeout passes. A
module-scoped `MockIdp()` fixture (from `tests.integration.mock_idp`), shut down on teardown. Ports
from a `_free_port()` like the smoke test's.

Tests (names exact):

1. `test_runs_as_the_unprivileged_service_account` — `docker run --rm --entrypoint id IMAGE` stdout
   contains `uid=10001(privacyfence-org) gid=10001(privacyfence-org)`.
2. `test_a_fresh_volume_holds_a_private_data_directory` — with a fresh volume,
   `stat -c '%u:%g %a'` of the data directory and its `org/` both print `10001:10001 700`.
3. `test_image_metadata` — `docker image inspect IMAGE`: `Config.User == "10001:10001"`,
   `Config.StopSignal == "SIGINT"`, `Config.Healthcheck.Test[-1] == "/opt/privacyfence/healthcheck.py"`,
   label `org.opencontainers.image.source == "https://github.com/privacyfence/privacyfence"`, label
   `org.opencontainers.image.licenses == "Apache-2.0"`, label `org.opencontainers.image.version` non-empty.
4. `test_refuses_to_start_without_a_bundle` — fresh volume, `docker run --rm -v ... IMAGE` exits 78
   and stderr contains `No organization bundle at`.
5. `test_refuses_to_start_on_an_unsigned_bundle` — install the unsigned JSON of the same config;
   `docker run --rm --network host -e PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP=1 -v ... IMAGE` exits
   non-zero and its combined output contains `not signed` (case-insensitive).
6. `TestRunningContainer` (one container for the class, through a class-scoped autouse fixture, like
   `TestRunningOrgModeService` in the smoke test): `test_the_issuer_host_is_served` (well-known → 200),
   `test_an_unexpected_host_header_is_rejected` (`Host: evil.example` → 400),
   `test_login_redirects_to_the_identity_provider` (`GET /login`, no redirects → 302),
   `test_the_healthcheck_reports_healthy` (poll `docker inspect --format '{{.State.Health.Status}}'`
   until `healthy`, 90 s).
7. `test_docker_stop_shuts_down_cleanly` — own container; `docker stop -t 30` returns in under 15 s,
   `docker inspect --format '{{.State.ExitCode}}'` is `0`, `docker logs` contains
   `Interrupted; shutting down`.
8. `test_state_survives_a_new_container_on_the_same_volume` — start, wait ready, read
   `deployment_id` (`docker run --rm --entrypoint cat -v ... IMAGE /var/lib/privacyfence-org/.privacyfence/deployment_id`),
   stop and remove, start a second container on the same volume, wait ready, read it again: equal.

### 3.7 `build.yml` job `build-org-image` (phase p3)

```yaml
  build-org-image:
    needs: dedupe
    if: needs.dedupe.outputs.duplicate != 'true'
    name: build-org-image (${{ matrix.arch }})
    strategy:
      fail-fast: false
      matrix:
        include:
          - arch: amd64
            runner: ubuntu-24.04
          - arch: arm64
            runner: ubuntu-24.04-arm
    runs-on: ${{ matrix.runner }}
    permissions:
      contents: read
```

Steps, in order (copy actions and their SHA pins from `build-deb`): checkout with `fetch-depth: 0`;
`setup-python` `'3.12'`; "Install pinned runtime dependencies" and "Install Python dependencies"
(the exact two `build-deb` steps: runtime lock, dev lock, `pip install --no-deps --no-build-isolation -e .`);
"Determine version and release channel" (copy `build-deb`'s step verbatim, id `release`); "Write
Telegram app credentials" (`python3 scripts/telegram_credentials.py write`, env `TELEGRAM_API_ID`/
`TELEGRAM_API_HASH` from secrets); "Build wheel" (`python -m build --wheel --no-isolation`);
"Check Telegram app credentials are packaged" (`publish-pypi.yml`'s step, on `dist/*.whl` only);
"Build org image" (`bash scripts/build_org_image.sh --platform linux/${{ matrix.arch }} --tag
privacyfence-org:ci-${{ matrix.arch }} --version "${{ steps.release.outputs.version }}"`); "Run org
image test" (env `PRIVACYFENCE_ORG_IMAGE: privacyfence-org:ci-${{ matrix.arch }}`,
`PRIVACYFENCE_REQUIRE_PACKAGED: "1"`; `pytest tests/integration/test_org_docker_image.py -v`);
"Upload failure diagnostics" (`build-deb`'s step, artifact name
`build-org-image-${{ matrix.arch }}-failure-diagnostics`); "Save org image" (`mkdir -p org-image &&
docker save privacyfence-org:ci-${{ matrix.arch }} | gzip > org-image/privacyfence-org-${{ matrix.arch }}.tar.gz`);
"Upload org image as artifact" (name `org-image-${{ matrix.arch }}`, path `org-image/`,
`retention-days: 30`, with no `if:` (so it runs only when the test passed) and not tag-gated, so a
pre-flight's image can be tried by hand; 30 days is also how long a rerun of `publish-org-image.yml`
can still find it).

`finalize-release`'s `needs:` becomes `[build, build-windows, build-deb, sbom, build-org-image]`,
so a tag whose image fails its test gets no GitHub Release either. Nothing in `build.yml` pushes
to a registry.

**This widens release gating, deliberately.** `publish-pypi.yml` and `publish-mcp-registry.yml` wait
for the whole `build.yml` run to succeed, so a failing image test, or no `ubuntu-24.04-arm` runner,
now blocks PyPI, the MCP registry, R2's `latest` and the GitHub Release on every tag, pre-releases
included. That is the same all-or-nothing rule every other packaged artifact follows: a release
whose org image is broken is a broken release. Only the registry *push* is kept out of `build.yml`
(§3.8). ADR 0205 and `docs/releasing.md` say so.

`tests.yml` gets `org-docker-image` on `ubuntu-latest` (same triggers as the file's other jobs,
`timeout-minutes: 45`): checkout `fetch-depth: 0`; `setup-python` `'3.12'`; the same two install
steps as `build-deb` (runtime lock, dev lock, `pip install --no-deps --no-build-isolation -e .`);
`python -m build --wheel --no-isolation`; `bash scripts/build_org_image.sh --platform linux/amd64 --tag privacyfence-org:ci-amd64`;
the test with `PRIVACYFENCE_ORG_IMAGE=privacyfence-org:ci-amd64` and
`PRIVACYFENCE_REQUIRE_PACKAGED=1`; the failure-diagnostics upload (name
`org-docker-image-failure-diagnostics`). No Telegram step (PR builds have no secrets; the check is
only a warning off a stable tag anyway).

### 3.8 `.github/workflows/publish-org-image.yml` (phase p4)

Modelled on `publish-mcp-registry.yml`, and a workflow of its own for the same reason: `publish-pypi.yml`
waits for `build.yml` to succeed, so a GHCR outage inside `build.yml` would hold PyPI back.

- `on: push: tags: ['v*']` and `workflow_dispatch` (rerun from the tag; the header comment says so).
- Top-level `permissions: contents: read`. `env: IMAGE: ghcr.io/privacyfence/privacyfence-org`.
- `dedupe`: copy of `publish-mcp-registry.yml`'s, with the workflow file name changed to
  `publish-org-image.yml`.
- `wait_for_build`: copy of `publish-mcp-registry.yml`'s, plus `outputs: run_id:
  ${{ steps.wait.outputs.run_id }}` (give the step `id: wait`) and, on success, `echo
  "run_id=$(jq -r '.id' <<<"$run")" >> "$GITHUB_OUTPUT"` before `exit 0`; error text names
  "Publish org Docker image" instead of "Publish to MCP Registry".
- `publish` (`needs: wait_for_build`, `runs-on: ubuntu-latest`, permissions `contents: read`,
  `actions: read`, `packages: write`, `id-token: write`, `attestations: write`), steps:
  1. checkout `fetch-depth: 0`; `setup-python` `'3.13'`.
  2. "Determine version and release channel" (id `release`): copy `publish-mcp-registry.yml`'s.
  3. Every later step has `if: steps.release.outputs.channel == 'stable'`; a pre-release tag ends
     green after step 2, with a step-summary line `Pre-release <version>: no org image is published.`
     (its own step, `if: steps.release.outputs.channel != 'stable'`).
  4. "Download the tested images": `actions/download-artifact` (same SHA pin as `finalize-release`)
     with `pattern: org-image-*`, `path: org-image`, `merge-multiple: true`,
     `run-id: ${{ needs.wait_for_build.outputs.run_id }}`, `github-token: ${{ github.token }}`.
  5. "Log in to GHCR": env `GH_TOKEN: ${{ github.token }}`, `ACTOR: ${{ github.actor }}`;
     `echo "$GH_TOKEN" | docker login ghcr.io -u "$ACTOR" --password-stdin`.
  6. "Push and tag" (id `push`), env `VERSION: ${{ steps.release.outputs.version }}`:
     ```bash
     set -euo pipefail
     for arch in amd64 arm64; do
       gunzip -c "org-image/privacyfence-org-${arch}.tar.gz" | docker load
       docker tag "privacyfence-org:ci-${arch}" "${IMAGE}:${VERSION}-${arch}"
       docker push "${IMAGE}:${VERSION}-${arch}"
     done
     mapfile -t TAGS < <(python3 scripts/org_image_tags.py "$VERSION")
     ARGS=()
     for tag in "${TAGS[@]}"; do ARGS+=(-t "${IMAGE}:${tag}"); done
     docker buildx imagetools create "${ARGS[@]}" "${IMAGE}:${VERSION}-amd64" "${IMAGE}:${VERSION}-arm64"
     DIGEST="$(docker buildx imagetools inspect "${IMAGE}:${VERSION}" --format '{{json .Manifest}}' | jq -r .digest)"
     echo "digest=$DIGEST" >> "$GITHUB_OUTPUT"
     { echo "## ${IMAGE}:${VERSION}"; echo "Tags: ${TAGS[*]}"; echo "Digest: \`$DIGEST\`"; } >> "$GITHUB_STEP_SUMMARY"
     ```
  7. "Attest build provenance": `actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373 # v4.1.1`
     with `subject-name: ${{ env.IMAGE }}`, `subject-digest: ${{ steps.push.outputs.digest }}`,
     `push-to-registry: true`. Before pinning, confirm with
     `git ls-remote https://github.com/actions/attest-build-provenance refs/tags/v4.1.1` that the
     SHA still matches; if it does not, stop with `status=blocked`.

### 3.9 Dependabot

Add a sixth entry to `.github/dependabot.yml` (and change the header comment's "Five entries ... five
places" to six, naming `deploy/docker/Dockerfile`'s digest-pinned base image):

```yaml
  - package-ecosystem: "docker"
    directory: "/deploy/docker"
    schedule:
      interval: "weekly"
    # Digest bumps only: moving off Python 3.12 is a deliberate change, like the .deb's runner (ADR 0203).
    ignore:
      - dependency-name: "python"
        update-types: ["version-update:semver-major", "version-update:semver-minor"]
```

### 3.10 `deploy/docker/compose.yaml` (exact)

```yaml
# PrivacyFence organization mode with Caddy in front. docs/org-mode-docker.md explains every line.
name: privacyfence-org

services:
  privacyfence:
    image: ghcr.io/privacyfence/privacyfence-org:${PRIVACYFENCE_IMAGE_TAG:-latest}
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    stop_grace_period: 30s
    env_file:
      - path: privacyfence.env
        required: false
    volumes:
      - pf-data:/var/lib/privacyfence-org/.privacyfence
    networks:
      pf:
        ipv4_address: 172.30.87.10

  caddy:
    image: caddy:2
    restart: unless-stopped
    depends_on:
      - privacyfence
    environment:
      PF_HOSTNAME: ${PF_HOSTNAME:?set PF_HOSTNAME in .env}
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    networks:
      pf:
        ipv4_address: 172.30.87.2

networks:
  pf:
    ipam:
      config:
        - subnet: 172.30.87.0/24

volumes:
  pf-data:
  caddy-data:
  caddy-config:
```

`deploy/docker/Caddyfile` (exact):

```
{$PF_HOSTNAME} {
    reverse_proxy privacyfence:8765
}
```

The daemon publishes no port: only Caddy is reachable from outside. The bundle for this layout is
built with `--server-bind-host 0.0.0.0 --server-trusted-proxy 172.30.87.2` and the default port.

### 3.11 `docs/org-mode-docker.md`

Operator page in the style of `docs/org-mode-setup-guide.md` (plain, imperative, tables for facts).
Headings exact, in order:

- `# Organization deployment with Docker`, then an intro: what this replaces in the guide
  (sections 3, 7 and 8, and the Upgrade and Backup parts of section 14), and that sections 4–6
  and 9–13 apply unchanged (link each).
- `## The image`: a table with image name, tags (§3.1 rule in plain words: `latest` and `X`/`X.Y`
  follow the newest stable release; pin `X.Y` in production), platforms, user (uid 10001), data
  volume path, port 8765, stop signal SIGINT, health check, and "refuses to start without a bundle".
  Stable releases only: a pre-release has no image.
- `## Prerequisites`: Docker Engine 24 or newer with the Compose plugin 2.24 or newer; a public
  hostname with DNS and ports 80/443 reachable (Caddy obtains the certificate); everything else in
  the guide's [Prerequisites](org-mode-setup-guide.md#2-prerequisites) except Python.
- `## Files`, with `### Compose file` (the whole of `deploy/docker/compose.yaml` in one ` ```yaml `
  block, byte-identical), `### Caddyfile` (`deploy/docker/Caddyfile` in one plain ` ``` ` block,
  byte-identical), and `### .env` (`PF_HOSTNAME=pf.acme.example.com` and
  `PRIVACYFENCE_IMAGE_TAG=X.Y` in a ` ```ini ` block, plus where to get the two files:
  `https://raw.githubusercontent.com/privacyfence/privacyfence/vX.Y.Z/deploy/docker/compose.yaml`
  and `.../Caddyfile`, into `/opt/privacyfence-org`).
- `## Build the bundle`: as in the guide's [section 6](org-mode-setup-guide.md#6-signing-key-and-bundle),
  with `--server-bind-host 0.0.0.0 --server-trusted-proxy 172.30.87.2` in place of
  `--server-trusted-proxy 127.0.0.1`, and the default port. Say why: the daemon listens on the
  Compose network, which only Caddy can reach, and Caddy's fixed address is the one proxy it trusts.
- `## Install the bundle`: first install, while nothing runs:
  `docker compose run --rm --no-deps -T --entrypoint sh privacyfence -c 'umask 077 && cat > /var/lib/privacyfence-org/.privacyfence/org/org_config.json' < org_config.json`,
  run from `/opt/privacyfence-org`. Replacing it later, while the daemon runs:
  `docker compose exec -T privacyfence sh -c '<same>' < org_config.json`, then
  `docker compose restart privacyfence`. Say why `exec`: a `run` container would claim the daemon's
  fixed address 172.30.87.10 while the daemon holds it.
- `## Start`: `docker compose up -d`, `docker compose logs -f privacyfence`, the
  `Org mode active -- MCP-over-HTTP at https://...` line, then go on with the guide's
  [section 9](org-mode-setup-guide.md#9-first-sign-in-and-validation).
- `## Environment variables`: `privacyfence.env` next to `compose.yaml`, mode 0600, for
  `--audit-forwarding-http-bearer-token-env NAME` (`NAME=token`); never in the bundle.
- `## Upgrade`: change `PRIVACYFENCE_IMAGE_TAG`, `docker compose pull`, `docker compose up -d`.
  Same advice as the guide's [Upgrade](org-mode-setup-guide.md#upgrade): changelog first, matching
  `build_org_bundle.py`, backup first, and going back is safe only if the older version reads the data.
- `## Backup and restore`: stop the daemon (`docker compose stop privacyfence`), then
  `docker compose run --rm --no-deps -T --entrypoint tar privacyfence -C /var/lib/privacyfence-org/.privacyfence -czf - . > privacyfence-backup.tgz`,
  then `docker compose start privacyfence`. Restore into an empty volume with the same command and
  `-xzf - < privacyfence-backup.tgz`. Point at the guide's [Backup](org-mode-setup-guide.md#backup)
  for what the directory holds, and that the tarball holds every person's connector credentials.
- `## Administrative commands`: `docker compose exec privacyfence privacyfence-app --revoke-scoped-tokens PRINCIPAL_ID`
  (in the running container, for the same fixed-address reason).
- `## Verify the image`: `gh attestation verify oci://ghcr.io/privacyfence/privacyfence-org:X.Y.Z --owner privacyfence`,
  and what "Verification succeeded" proves (built by this repository's `build.yml`/`publish-org-image.yml`
  from the tagged commit).
- `## A bind-mounted data directory`: instead of the named volume, a host directory must be owned by
  `10001:10001` with mode `0700` (`sudo install -d -o 10001 -g 10001 -m 700 /srv/privacyfence-org`),
  or the daemon refuses to start.

### 3.12 Other docs

- `docs/org-mode-setup-guide.md`: in `## 1. Overview`, after the parts table, one paragraph:
  "To run the daemon and the reverse proxy as containers, follow
  [Organization deployment with Docker](org-mode-docker.md) instead of sections 3, 7 and 8; every
  other section applies unchanged." At the top of `## 3. Service account and install`, one line:
  "With the Docker image, skip this section and sections 7 and 8; see
  [Organization deployment with Docker](org-mode-docker.md)."
- `docs/README.md`, `### Organization deployment and security`: a bullet for `org-mode-docker.md`
  after the `org-mode-setup-guide.md` one (this also publishes it on the website, whose nav is read
  from this README).
- `docs/platform-support.md`: support-matrix row `| Linux org mode (Docker) | Docker Engine 24 (amd64, arm64) | ghcr.io/privacyfence/privacyfence-org | the container's privacyfence-org account (uid 10001) | [Organization deployment with Docker](org-mode-docker.md) |`;
  a sentence in `## Signing`: the org image carries a GitHub build-provenance attestation; a
  Data-locations row `| Docker org image | the volume at /var/lib/privacyfence-org/.privacyfence | ... |`
  with the same relative columns as the `pip`/`pipx` row.
- `docs/packaging.md`: a `What ships` row (`Linux org mode (amd64, arm64) | scripts/build_org_image.sh
  → deploy/docker/Dockerfile | ghcr.io/privacyfence/privacyfence-org:<version> | the release wheel and
  requirements/runtime.lock.txt in a venv on python:3.12-slim`), and a `## Organization Docker image`
  section before `## CI`: build script, staging, the SIGINT stop signal, the entrypoint's bundle check,
  the base-image digest and Dependabot.
- `docs/testing-policy.md`: a layer 6 row for `test_org_docker_image.py` (`build.yml`,
  `build-org-image`; also `tests.yml`, `org-docker-image`); a row for `org-docker-image` in the
  `tests.yml` job table (the one with the `org-mode-smoke` row, near line 63) and **not** in the
  Required checks paragraph (near line 91); the sentence on `PRIVACYFENCE_REQUIRE_PACKAGED=1` (near
  line 72, "the five packaged steps in `build.yml` ...") becomes six and names the `tests.yml` step too;
  and the "`build.yml` runs on a `v*` tag push ..." paragraph under the layer 6 table adds that
  `build-org-image` also runs `test_org_docker_image.py`.
- `docs/coding-and-testing-guidelines.md` §4 ("Packaging changes"): where it says packaged tests run
  only in `build.yml` and the graphical-session workflows, add `tests.yml`'s `org-docker-image`.
- `docs/releasing.md`: after the MCP-registry paragraph, a `**The organization Docker image ...**`
  paragraph: `publish-org-image.yml` on the tag, waits for `build.yml`, downloads the two images
  `build-org-image` tested, pushes, combines, applies the §3.5 tags, attests; stable only; rerun from
  the tag within 30 days (the artifacts' retention); a workflow of its own so GHCR cannot hold PyPI
  back, while `build-org-image` inside `build.yml` does gate PyPI and the GitHub Release (§3.7). And
  the first-release note: the GHCR package must be switched to public once. Also every place that
  lists `finalize-release`'s needs (`build, build-windows, build-deb, sbom`; three places today, near
  lines 64, 150 and 200) gains `build-org-image`.

### 3.13 Rejected alternatives (become the ADRs)

- **Docker Hub, or both registries**: needs a third-party account and a long-lived token secret;
  GHCR authenticates with the workflow's own `GITHUB_TOKEN` (`packages: write`), no secret.
- **Pre-release images**: PyPI carries stable only and the org guide promises unattended upgrades
  never pick up a test build; an image `latest` that could be a release candidate breaks that.
- **Pushing from `build.yml`**: `publish-pypi.yml` waits for `build.yml` to succeed, so a registry
  outage would block PyPI (the MCP-registry precedent, ADR 0112).
- **Rebuilding the image in the publish workflow**: would publish an image no test ran against. The
  publish job pushes the bytes `build-org-image` tested (`docker save` → artifact → `docker load`).
- **Push-by-digest from the build jobs**: leaks a pushed image before `finalize-release` has proven the
  rest of the release.
- **Floating tags moved unconditionally**: a stable patch on an older line would move `latest`
  backwards; `org_image_tags.py` moves `X.Y`/`X`/`latest` only forward.
- **A PyInstaller bundle in the image (like the `.deb`)**: no gain in a container, and PyInstaller
  cannot cross-build; the wheel plus the universal hash lock already installs on both architectures.
- **`tini` or `docker run --init`**: an extra package, or a flag every operator must remember; the
  daemon already shuts down cleanly on SIGINT.
- **Starting local mode when no bundle is installed**: it would listen on the container's loopback,
  unreachable, and look healthy to nobody; the entrypoint refuses instead.
- **cosign signatures**: another tool to install and pin (ADR 0182); GitHub artifact attestations are
  Sigstore-backed, need no tool in the job, and verify with `gh attestation verify`.
- **amd64 only, as for the `.deb` (ADR 0044)**: arm64 servers are common and the image needs no
  per-arch packaging work; `ubuntu-24.04-arm` builds and tests it natively.
- **QEMU emulation for arm64**: slower by an order of magnitude, and it tests the image under an
  emulator rather than on the architecture it ships for.
- **Running as root inside the container**: nothing in the daemon needs it, and the data directory
  checks expect a dedicated owner.

## ADRs

Next free numbers at planning time, one decision each (docs/adr/README.md rule 1). The
retirement phase re-checks and renumbers if taken. Each line: number, title, the rejected
alternatives from Design 3.13 it records.

- **0204** — The organization image is published to GHCR for stable releases only. Rejects: Docker Hub and both registries; pre-release images.
- **0205** — The organization image is tested in build.yml and pushed by a workflow of its own. Rejects: pushing from build.yml; rebuilding in the publish job; push-by-digest from the build jobs. Records that build-org-image now gates PyPI, the MCP registry and the GitHub Release on every tag.
- **0206** — The organization image's floating tags only move forward. Rejects: moving X.Y, X and latest unconditionally.
- **0207** — The organization image is built for amd64 and arm64 on native runners. Rejects: amd64 only as for the .deb (ADR 0044); QEMU emulation.
- **0208** — The organization image installs the release wheel from the hash lock as an unprivileged user. Rejects: a PyInstaller bundle as in the .deb; running as root.
- **0209** — The organization image runs organization mode only. Rejects: starting local mode when no bundle is installed.
- **0210** — The organization image stops with SIGINT. Rejects: tini or docker run --init.
- **0211** — The organization image is attested with GitHub build provenance, not cosign. Rejects: cosign signatures.

## Manual steps

One `manual_before` step: **mb1**, allow `actions/attest-build-provenance` in the repository's
Actions settings if they restrict actions to an allowlist. `publish-pypi.yml:90-98` records that an
unlisted action fails a whole workflow at startup with no jobs and no logs, and
`publish-org-image.yml` cannot be dispatched before a tag to find out. Every other phase builds and
tests the image itself (Docker runs in the cloud container; arm64 and the release jobs run on
GitHub's runners via a `build.yml` dispatch).

`manual_after` (step-by-step page: https://claude.ai/artifact/DkAVoNzy7qKd1aNB5bU1Sr):

1. **ma1** — run the pre-flight image on a real server with the Compose file, a real identity
   provider and a real hostname: sign in, connect a service, approve one request from an AI client,
   restart, check that state is kept.
2. **ma2** — at the first stable release after this merges: allow public packages in the org
   settings if needed, switch the new GHCR package to public, pull it signed out, and verify its
   attestation. (This cannot happen before merge: nothing is published until a stable tag.)

## Risks and open questions

- **`ubuntu-24.04-arm` availability.** It is free for public repositories. If the dispatched
  `build-org-image (arm64)` job never starts ("no runner matching"), stop with `status=blocked` and
  report it; do not switch to QEMU emulation.
- **Docker in the cloud container.** `dockerd` is installed but not running. Start it in the
  background (`dockerd > /tmp/dockerd.log 2>&1`, Bash `run_in_background`), wait for
  `docker info` to succeed, and build with `PIP_CERT_FILE=/root/.ccr/ca-bundle.crt` (the script
  adds the proxy flags because `HTTPS_PROXY` is set). If `docker info` still fails after 30 s, stop
  with `status=blocked`.
- **`docker save`/`docker load` across architectures.** Loading the arm64 tarball on the amd64
  publish runner only stores it; pushing it is fine. Nothing before a stable tag exercises it; ma2
  does at the first release. A failure there is fixed in a follow-up and publishes nothing wrong
  (the multi-arch tags are created only after both pushes succeed).
- **Healthcheck vs. a TLS-terminating daemon.** Covered by the `cert_file` branch in `healthcheck.py`;
  the Compose layout never uses it.
- **`env_file` with `required: false`** needs Compose 2.24+. If `docker compose config` in the cloud
  container or CI rejects the syntax, stop with `status=blocked` rather than dropping the hardening.
- **The bundle's trusted proxy is an exact IP.** If `uvicorn`'s `ProxyHeadersMiddleware` or the
  bundle validation rejects `172.30.87.2`, stop with `status=blocked`.
- **Coverage.** No `src/` code changes, so the coverage floor is untouched. If a phase finds it
  needs a `src/` change, stop with `status=blocked`: the plan assumed none.

## Implementation manifest

```yaml
plan_slug: org-docker-image
feature_branch: feature/org-docker-image
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/DkAVoNzy7qKd1aNB5bU1Sr
manual_steps_source: docs/org-docker-image-plan-manual-steps.html
manual_before:
  - id: mb1-allow-attest-action
    title: Allow actions/attest-build-provenance in the repository's Actions settings, if they use an allowlist
    why: p4's publish-org-image.yml uses it; an action outside the allowlist makes the whole workflow fail at startup with no logs (publish-pypi.yml:90-98), and it cannot be dispatched before a stable tag to notice
    done_when: Settings, Actions, General shows either "Allow all actions and reusable workflows", or the allowlist field contains actions/attest-build-provenance@*
manual_after:
  - id: ma1-real-server-compose
    title: Run the pre-flight image with the Compose file on a real server, real IdP and real hostname
    why: CI uses a mock IdP over plain HTTP on host networking; only a real deployment proves Caddy's certificate, forwarded headers, the trusted-proxy address and a real sign-in, connect and approval through the published layout
  - id: ma2-first-release-ghcr
    title: At the first stable release after merge, make the GHCR package public and verify a signed-out pull and its attestation
    why: a new GHCR package starts private, and nothing is pushed until a stable tag, so publishing and its attestation cannot be checked before merge
verify_after_merge:
  - python3 -m pytest tests/unit tests/integration -q -k "org_image or org_guide or org_docker"
final_checks:
  - docs/org-docker-image-plan.md and docs/org-docker-image-plan-manual-steps.html are deleted, and `git grep -n "org-docker-image-plan"` prints nothing
  - ADRs 0204 to 0211 (or their renumbered successors) exist with Status Accepted and are listed in docs/adr/README.md's index
  - CHANGELOG.md has the org-image entry under "## [Unreleased]" and no new version heading
  - A build.yml run dispatched on feature/org-docker-image has build-org-image (amd64) and build-org-image (arm64) green; its URL is in the PR description
phases:
  - id: p1-org-image
    title: Dockerfile, entrypoint, healthcheck, build script and the image test
    depends_on: []
    complexity: M
    touches:
      - deploy/docker/Dockerfile
      - deploy/docker/docker-entrypoint.sh
      - deploy/docker/healthcheck.py
      - scripts/build_org_image.sh
      - tests/integration/test_org_docker_image.py
      - .github/dependabot.yml
    brief: |
      Read first: CLAUDE.md, CONTRIBUTING.md, docs/releasing.md, docs/coding-and-testing-guidelines.md,
      docs/testing-policy.md, docs/adr/README.md, and this plan's Current state, Design 3.1-3.4, 3.6 and 3.9, and Risks and open questions.

      1. Create deploy/docker/Dockerfile, deploy/docker/docker-entrypoint.sh (mode 0755 in git:
         `git update-index --chmod=+x`) and deploy/docker/healthcheck.py with exactly the content of
         Design 3.2 and 3.3.
      2. Create scripts/build_org_image.sh per Design 3.4 (mode 0755 in git), with a header comment in
         the style of scripts/build_deb.sh's saying what it stages and why the context is staged
         (only the wheel, the lock and three files reach the daemon's image).
      3. Start Docker in this container: run `dockerd > /tmp/dockerd.log 2>&1` with Bash
         run_in_background, then poll `docker info` until it succeeds (stop with status=blocked
         after 30 s, per Risks).
      4. Build a wheel (`python -m build --wheel --no-isolation`; install `requirements/release.lock.txt`
         with `--require-hashes` first if `build` is missing) and the image:
         `PIP_CERT_FILE=/root/.ccr/ca-bundle.crt bash scripts/build_org_image.sh --platform linux/amd64 --tag privacyfence-org:ci-amd64`.
         If pip inside the build cannot reach PyPI even with the cert and proxy flags, stop with
         status=blocked and paste the error.
      5. Write tests/integration/test_org_docker_image.py per Design 3.6: the module docstring says
         what it covers and that build.yml's build-org-image and tests.yml's org-docker-image run it
         (both are added in phase p3), and copies nothing from the smoke module except the
         patterns named there (MockIdp from tests/integration/mock_idp.py, the free-port helper).
      6. Run it: `PRIVACYFENCE_ORG_IMAGE=privacyfence-org:ci-amd64 PRIVACYFENCE_REQUIRE_PACKAGED=1 python3 -m pytest tests/integration/test_org_docker_image.py -v`.
         If test 7 (clean stop) or test 2 (volume ownership) fails, the Design's measured facts are
         wrong for this base image: stop with status=blocked and report the output; do not change
         the stop signal or add an init process.
      7. Run `python3 -m pytest tests/integration/test_org_docker_image.py -q` with
         PRIVACYFENCE_ORG_IMAGE unset: the module is skipped. Run it with
         PRIVACYFENCE_REQUIRE_PACKAGED=1 and the variable unset: it fails (collection error naming
         PRIVACYFENCE_REQUIRE_PACKAGED).
      8. Add the Dependabot docker entry and update the header comment per Design 3.9.
      9. `ruff check .` (see Design 3.3 on noqa), then `python3 -m pytest tests/unit -q`.
      No CHANGELOG line and no docs in this phase (p3, p4, p5 and p6 own them).
    acceptance:
      - "`PRIVACYFENCE_ORG_IMAGE=privacyfence-org:ci-amd64 PRIVACYFENCE_REQUIRE_PACKAGED=1 python3 -m pytest tests/integration/test_org_docker_image.py -v` passes, all 11 tests (5 module tests, 4 in TestRunningContainer, tests 7 and 8)"
      - "`python3 -m pytest tests/integration/test_org_docker_image.py -q` without PRIVACYFENCE_ORG_IMAGE reports the module skipped"
      - "`bash scripts/build_org_image.sh --platform linux/riscv64 --tag x` exits 2 and prints the usage line"
      - "`docker image inspect privacyfence-org:ci-amd64 --format '{{.Config.StopSignal}} {{.Config.User}}'` prints `SIGINT 10001:10001`"
      - "`git ls-files -s deploy/docker/docker-entrypoint.sh scripts/build_org_image.sh` shows mode 100755 for both"
      - "`grep -n 'package-ecosystem: \"docker\"' .github/dependabot.yml` matches once"
      - "`ruff check .` and `python3 -m pytest tests/unit -q` pass"

  - id: p2-image-tags
    title: org_image_tags.py, which decides which floating tags a stable release moves
    depends_on: []
    complexity: S
    touches:
      - scripts/org_image_tags.py
      - tests/unit/test_org_image_tags.py
    brief: |
      Read first: CLAUDE.md, docs/coding-and-testing-guidelines.md, and this plan's Design 3.5.

      1. Create scripts/org_image_tags.py per Design 3.5: module docstring (what it decides, that
         publish-org-image.yml calls it, that floating tags only move forward), `image_tags()` and
         `main()`, `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`.
      2. Create tests/unit/test_org_image_tags.py, importing the script by file path the way
         tests/unit/test_tag_release.py imports tag_release (importlib.util.spec_from_file_location).
         Tests, each a fabricated `known_tags` list:
         - first release ever (`[]`, "1.0.0") -> ["1.0.0", "1.0", "1", "latest"];
         - newest release, its own tag present (["v6.4.0", "v6.5.0", "v6.5.1"], "6.5.1") -> all four;
         - patch on an older minor line (["v6.4.2", "v6.5.0"], "6.4.3") -> ["6.4.3", "6.4"];
         - patch on an older major line (["v5.9.0", "v6.0.0"], "5.9.1") -> ["5.9.1", "5.9", "5"];
         - pre-release and malformed tags are ignored (["v7.0.0a1", "v7.0.0rc2", "vX", "6.9.9"], "6.5.0") -> all four;
         - a pre-release version raises ValueError whose message contains "stable versions only"
           (parametrize "6.6.0a1", "6.6.0rc1", "6.6", "v6.6.0");
         - main() against a temporary git repo (`git init`, then
           `git -c user.name=t -c user.email=t@example.invalid commit --allow-empty -m init`, tags v1.0.0 and v1.1.0, run
           with the script's repo root monkeypatched to that directory — make the root a module
           constant `REPO_ROOT` so the test can patch it) prints "1.0.1\n1.0\n" for "1.0.1" and exits 0;
         - main(["6.6.0a1"]) exits 2 and prints "error: org images are published for stable versions only" to stderr.
      3. `ruff check .` and `python3 -m pytest tests/unit -q`.
    acceptance:
      - "`python3 -m pytest tests/unit/test_org_image_tags.py -v` passes, covering every case in the brief"
      - "`python3 scripts/org_image_tags.py 6.6.0a1; echo $?` prints the error line and 2"
      - "`ruff check .` and `python3 -m pytest tests/unit -q` pass"

  - id: p3-build-jobs
    title: build.yml's build-org-image matrix, finalize-release gating, and the per-PR tests.yml job
    depends_on: [p1-org-image]
    complexity: M
    touches:
      - .github/workflows/build.yml
      - .github/workflows/tests.yml
      - tests/unit/test_org_image_workflows.py
      - docs/testing-policy.md
      - docs/packaging.md
      - docs/coding-and-testing-guidelines.md
    brief: |
      Read first: CLAUDE.md, CONTRIBUTING.md, docs/releasing.md, docs/testing-policy.md,
      .claude/skills/steward/SKILL.md, the whole of .github/workflows/build.yml and tests.yml, and this
      plan's Design 3.1, 3.7 and 3.12 (testing-policy and packaging bullets).

      1. Add the `build-org-image` job to build.yml per Design 3.7, after `build-deb` and before
         `sbom`, with a comment block in the file's style (why a matrix on native runners, why the
         image is saved as an artifact and not pushed here, pointing at publish-org-image.yml and
         ADRs 0205 and 0207 by number).
      2. Change `finalize-release`'s `needs:` to `[build, build-windows, build-deb, sbom, build-org-image]`,
         and extend its existing comment that lists what it waits for.
      3. Add the `org-docker-image` job to tests.yml per Design 3.7, after `org-mode-smoke`, with a
         comment saying it is the per-PR leg of build.yml's build-org-image (amd64 only) and that it
         is deliberately not in scripts/update_branch_protection.py's REQUIRED_STATUS_CHECKS.
         Do not edit REQUIRED_STATUS_CHECKS.
      4. Create tests/unit/test_org_image_workflows.py (pyyaml, `pytestmark = pytest.mark.unit`).
         Note PyYAML parses the `on:` key as True. Tests for build.yml and tests.yml only (p4 adds
         tests for publish-org-image.yml to this same file later — leave a module docstring that
         says it checks the org-image jobs' wiring):
         - build-org-image's matrix is exactly {amd64: ubuntu-24.04, arm64: ubuntu-24.04-arm};
         - its permissions are exactly {contents: read} (no packages: write anywhere in build.yml);
         - its uploaded artifact name is `org-image-${{ matrix.arch }}` and does not match the
           glob `PrivacyFence-*` (fnmatch);
         - the step running test_org_docker_image.py sets PRIVACYFENCE_REQUIRE_PACKAGED "1";
         - finalize-release needs build-org-image;
         - no step in build.yml runs `docker push` or `docker login`;
         - tests.yml has an `org-docker-image` job whose steps run test_org_docker_image.py;
         - every `uses:` in build.yml and tests.yml is pinned to a 40-hex SHA.
      5. Docs: docs/testing-policy.md, docs/packaging.md and docs/coding-and-testing-guidelines.md §4 per Design 3.12 (only those three bullets).
      6. Local checks: `python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['.github/workflows/build.yml','.github/workflows/tests.yml']]"`,
         `ruff check .`, `python3 -m pytest tests/unit -q`, and
         `python3 -m pytest tests/unit/test_definition_of_done_drift.py -q`.
      7. Push the phase branch and dispatch build.yml on it (workflow_dispatch, no inputs; it is in
         ci.dispatchable and publishes nothing off a tag). Wait for `build-org-image (amd64)` and
         `build-org-image (arm64)`; both must be green, and the run's artifacts must include
         org-image-amd64 and org-image-arm64. Other jobs in that run are not this phase's
         acceptance, but report any that fail. If an org-image leg fails, read its log and fix the
         cause in this phase's files; if the cause is outside them (runner availability, a p1 file),
         stop with status=blocked and quote the log.
    acceptance:
      - "`python3 -m pytest tests/unit/test_org_image_workflows.py -v` passes"
      - "A build.yml run dispatched on this phase's branch has `build-org-image (amd64)` and `build-org-image (arm64)` green and artifacts org-image-amd64 and org-image-arm64 (run URL in the phase report)"
      - "`grep -n 'build-org-image' docs/testing-policy.md docs/packaging.md` matches in both files"
      - "`ruff check .` and `python3 -m pytest tests/unit -q` pass"

  - id: p4-publish-workflow
    title: publish-org-image.yml pushes the tested images to GHCR, tags and attests them
    depends_on: [p2-image-tags, p3-build-jobs]
    complexity: M
    touches:
      - .github/workflows/publish-org-image.yml
      - tests/unit/test_org_image_workflows.py
      - docs/releasing.md
    brief: |
      Read first: CLAUDE.md, docs/releasing.md, docs/adr/0112-stable-releases-are-listed-on-the-mcp-registry-with-the-mcpb.md,
      docs/adr/0182-every-tool-a-release-or-qa-runner-job-installs-comes-from-a-hash-pinned-lock.md,
      the whole of .github/workflows/publish-mcp-registry.yml, and this plan's Design 3.1, 3.5, 3.8
      and 3.12 (releasing bullet).

      1. Create .github/workflows/publish-org-image.yml per Design 3.8, with a header comment in
         publish-mcp-registry.yml's style: what it publishes, why a workflow of its own, why it
         downloads build.yml's tested images instead of building, stable only, floating tags via
         scripts/org_image_tags.py, the GHCR package must be made public once (docs/releasing.md),
         ADRs 0205, 0206 and 0211 by number.
      2. Verify the attest-build-provenance pin as Design 3.8 step 7 says. manual_before mb1 covers
         the Actions allowlist; do not add any other action that the repo's workflows do not already use.
      3. Extend tests/unit/test_org_image_workflows.py with publish-org-image.yml tests:
         - triggers are push tags ['v*'] and workflow_dispatch;
         - jobs dedupe -> wait_for_build -> publish via needs;
         - wait_for_build exposes output run_id;
         - only `publish` has packages: write, id-token: write and attestations: write; top-level
           permissions are exactly {contents: read};
         - the download step has `run-id: ${{ needs.wait_for_build.outputs.run_id }}` and pattern `org-image-*`;
         - every step after "Determine version and release channel" except the pre-release summary
           step is gated on `steps.release.outputs.channel == 'stable'`;
         - the push step's script calls `scripts/org_image_tags.py` and contains no `docker build`
           or `buildx build`;
         - every `uses:` is pinned to a 40-hex SHA.
      4. docs/releasing.md per Design 3.12.
      5. `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/publish-org-image.yml'))"`,
         `ruff check .`, `python3 -m pytest tests/unit -q`.
      The workflow cannot be exercised before a stable tag; ma2 verifies it at the first release.
      Do not dispatch it.
    acceptance:
      - "`python3 -m pytest tests/unit/test_org_image_workflows.py -v` passes, including the publish-org-image.yml tests"
      - "`grep -c 'org_image_tags.py' .github/workflows/publish-org-image.yml` is at least 1"
      - "`grep -n 'publish-org-image.yml' docs/releasing.md` matches"
      - "`ruff check .` and `python3 -m pytest tests/unit -q` pass"

  - id: p5-compose-docs
    title: Compose file, Caddyfile, the Docker operator page and its checks
    depends_on: [p1-org-image]
    complexity: M
    touches:
      - deploy/docker/compose.yaml
      - deploy/docker/Caddyfile
      - docs/org-mode-docker.md
      - docs/org-mode-setup-guide.md
      - docs/README.md
      - docs/platform-support.md
      - tests/integration/guide_sections.py
      - tests/integration/test_org_guide_configs.py
      - tests/integration/test_org_docker_deploy_files.py
    brief: |
      Read first: CLAUDE.md, docs/coding-and-testing-guidelines.md (§2.5 on reusing helpers, §5 on
      docs), the whole of docs/org-mode-setup-guide.md, tests/integration/test_org_guide_configs.py,
      and this plan's Design 3.1, 3.10, 3.11 and 3.12 (guide, README, platform-support bullets).

      1. Create deploy/docker/compose.yaml and deploy/docker/Caddyfile with exactly the content of
         Design 3.10.
      2. Move `_section` and `_fenced_block` out of tests/integration/test_org_guide_configs.py into
         a new tests/integration/guide_sections.py as `section(doc: Path, title: str) -> str` and
         `fenced_block(doc: Path, title: str, language: str) -> str` (same logic, the document now
         a parameter; `language=""` matches a plain fence). Make test_org_guide_configs.py use them;
         keep `_section(title)` and `_fenced_block(title, language)` in it as one-line wrappers
         bound to its GUIDE, so its tests pass without edits.
      3. Write docs/org-mode-docker.md per Design 3.11. The Compose file and Caddyfile blocks are
         byte-identical copies of the two files.
      4. Edit docs/org-mode-setup-guide.md, docs/README.md and docs/platform-support.md per Design
         3.12. Do not renumber the guide's sections.
      5. Create tests/integration/test_org_docker_deploy_files.py (`pytestmark = pytest.mark.integration`,
         module docstring in test_org_guide_configs.py's style), using guide_sections and pyyaml:
         - test_compose_file_matches_the_doc (```yaml block under "Compose file" == compose.yaml text);
         - test_caddyfile_matches_the_doc (plain block under "Caddyfile" == Caddyfile text);
         - test_the_daemon_publishes_no_port (services.privacyfence has no `ports`);
         - test_the_daemon_runs_hardened (read_only true, cap_drop == ["ALL"],
           "no-new-privileges:true" in security_opt, "/tmp" in tmpfs);
         - test_caddy_is_the_documented_trusted_proxy (services.caddy.networks.pf.ipv4_address ==
           "172.30.87.2", inside networks.pf.ipam.config[0].subnet via ipaddress, and
           "--server-trusted-proxy 172.30.87.2" appears in docs/org-mode-docker.md);
         - test_caddy_proxies_to_the_daemon_port (Caddyfile has `reverse_proxy privacyfence:8765`,
           and 8765 == privacyfence.web.server.DEFAULT_PORT);
         - test_the_data_volume_is_the_images_data_directory (compose mounts
           "pf-data:/var/lib/privacyfence-org/.privacyfence", and deploy/docker/Dockerfile's VOLUME
           line names the same path);
         - test_docker_compose_accepts_the_file: skip with "docker compose not on PATH" when
           `docker compose version` fails; else `docker compose -f deploy/docker/compose.yaml config --quiet`
           with PF_HOSTNAME=pf.example.com in the environment exits 0; and without PF_HOSTNAME it
           exits non-zero with "set PF_HOSTNAME in .env" in stderr.
      6. Start dockerd as in p1's brief step 3 if it is not running, so the compose test runs here
         rather than skips. If `docker compose config` rejects the `env_file` `required:` syntax,
         stop with status=blocked (Risks).
      7. Check the docs build that reads the README nav: `python3 -m pytest tests/unit/test_build_site.py -q`.
      8. `ruff check .`, `python3 -m pytest tests/unit -q`,
         `python3 -m pytest tests/integration/test_org_guide_configs.py tests/integration/test_org_docker_deploy_files.py -v -rs`
         (no test in the second file skipped).
    acceptance:
      - "`python3 -m pytest tests/integration/test_org_guide_configs.py tests/integration/test_org_docker_deploy_files.py -v -rs` passes with no skip in test_org_docker_deploy_files.py"
      - "`grep -n 'org-mode-docker.md' docs/README.md docs/org-mode-setup-guide.md docs/platform-support.md` matches in all three"
      - "`python3 -m pytest tests/unit/test_build_site.py -q` passes"
      - "`ruff check .` and `python3 -m pytest tests/unit -q` pass"

  - id: p6-retire
    title: ADRs, changelog, and delete the plan
    depends_on: [p4-publish-workflow, p5-compose-docs]
    complexity: S
    touches:
      - docs/adr/02*-the-organization-image*.md
      - docs/adr/README.md
      - CHANGELOG.md
      - docs/org-docker-image-plan.md
      - docs/org-docker-image-plan-manual-steps.html
    brief: |
      Read first: CONTRIBUTING.md ("Decisions, plans and ADRs"), docs/adr/README.md (rules, template,
      index), docs/adr/0203-the-deb-is-built-on-a-pinned-ubuntu-runner.md as the format to copy,
      and this plan's Design 3.13 and its ADRs section.

      1. `ls docs/adr` and take the next eight free numbers (0204-0211 unless a merged branch took
         them; if so, use the next free ones in the same order and use those numbers everywhere,
         including the comments p3 and p4 wrote in build.yml, tests.yml and publish-org-image.yml:
         `git grep -nE "ADRs? 02(0[4-9]|1[01])"`).
      2. Write the eight ADRs of the plan's ADRs section, titles exactly as listed there, Status
         `Accepted — <today>. Implemented: <files>`, each with Context, Decision, the rejected
         alternatives the list names (wording from Design 3.13), and Consequences. ADR 0205's
         Consequences state that build-org-image gates PyPI, the MCP registry, R2 latest and the
         GitHub Release on every tag (Design 3.7). Link files, workflows and other ADRs (0040, 0044,
         0112, 0182, 0203), never this plan. File names: the kebab-case of the title after the number.
      3. Add the eight rows to docs/adr/README.md's index.
      4. Add under CHANGELOG.md's `## [Unreleased]` an `### Added` subsection (create it above
         `### Fixed` if absent) with one entry in the file's style:
         "**Organization mode ships as a Docker image.** Each stable release now publishes
         `ghcr.io/privacyfence/privacyfence-org` for amd64 and arm64, with a Compose file that puts
         Caddy in front of it. See [Organization deployment with Docker](docs/org-mode-docker.md)."
         Never open a version heading.
      5. `git rm docs/org-docker-image-plan.md docs/org-docker-image-plan-manual-steps.html`, then
         `git grep -n "org-docker-image-plan"` must print nothing.
      6. `ruff check .`, `python3 -m pytest tests/unit -q` (includes the ADR index and changelog checks).
    acceptance:
      - "`ls docs/adr | grep -cE '^[0-9]{4}-the-organization-image'` is 8, and `grep -cE 'the-organization-image' docs/adr/README.md` is 8"
      - "`git grep -n 'org-docker-image-plan'` prints nothing"
      - "`sed -n '/^## \\[Unreleased\\]/,/^## \\[[0-9]/p' CHANGELOG.md | grep -c 'privacyfence-org'` prints 1 or more"
      - "`ruff check .` and `python3 -m pytest tests/unit -q` pass"
```
