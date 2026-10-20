#!/usr/bin/env bash
# Build the organization-mode Docker image (deploy/docker/Dockerfile, docs/org-mode-docker.md) for
# one platform and load it into the local Docker daemon.
#
# What it stages, and why: the build context is a throwaway directory, build/org-image/, holding
# only the Dockerfile, docker-entrypoint.sh, healthcheck.py, requirements/runtime.lock.txt (as
# runtime.lock.txt) and the one privacyfence wheel from dist/. Building from the repository root
# instead would send the whole checkout -- .git, credentials files, build output -- to the Docker
# daemon, and let a stray second wheel into the image. This way the image is exactly the release
# wheel plus the hash-pinned runtime lock (ADR 0182), nothing else.
#
# Prerequisites:
#   python -m build --wheel --no-isolation    # exactly one dist/privacyfence-*.whl
#   Docker with buildx
#
# Optional environment, for a build behind a TLS-intercepting proxy (a Claude Code cloud container):
#   PIP_CERT_FILE   CA bundle handed to pip inside the build as the pip_cert build secret
#   HTTPS_PROXY     forwarded to the build, with --network host (the proxy listens on 127.0.0.1)
#
# Usage:
#   scripts/build_org_image.sh --platform linux/amd64|linux/arm64 --tag NAME:TAG [--version VERSION]
#
# Output: the image NAME:TAG in the local Docker daemon.
set -euo pipefail

USAGE="Usage: scripts/build_org_image.sh --platform linux/amd64|linux/arm64 --tag NAME:TAG [--version VERSION]"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PLATFORM=""
TAG=""
VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) [ $# -ge 2 ] || { echo "$USAGE" >&2; exit 2; }; PLATFORM="$2"; shift 2 ;;
    --tag)      [ $# -ge 2 ] || { echo "$USAGE" >&2; exit 2; }; TAG="$2"; shift 2 ;;
    --version)  [ $# -ge 2 ] || { echo "$USAGE" >&2; exit 2; }; VERSION="$2"; shift 2 ;;
    *) echo "$USAGE" >&2; exit 2 ;;
  esac
done
case "$PLATFORM" in
  linux/amd64|linux/arm64) ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ -n "$TAG" ] || { echo "$USAGE" >&2; exit 2; }

shopt -s nullglob
WHEELS=(dist/privacyfence-*.whl)
shopt -u nullglob
if [ "${#WHEELS[@]}" -ne 1 ]; then
  echo "expected exactly one dist/privacyfence-*.whl, found ${#WHEELS[@]} -- build it with: python -m build --wheel --no-isolation" >&2
  exit 1
fi
WHEEL="${WHEELS[0]}"

if [ -z "$VERSION" ]; then
  # privacyfence-<version>-py3-none-any.whl
  VERSION="$(basename "$WHEEL")"
  VERSION="${VERSION#privacyfence-}"
  VERSION="${VERSION%%-*}"
fi

CONTEXT="build/org-image"
rm -rf "$CONTEXT"
mkdir -p "$CONTEXT/dist"
cp deploy/docker/Dockerfile deploy/docker/docker-entrypoint.sh deploy/docker/healthcheck.py "$CONTEXT/"
cp requirements/runtime.lock.txt "$CONTEXT/runtime.lock.txt"
cp "$WHEEL" "$CONTEXT/dist/"

EXTRA=()
if [ -n "${PIP_CERT_FILE:-}" ]; then
  EXTRA+=(--secret "id=pip_cert,src=$PIP_CERT_FILE")
fi
if [ -n "${HTTPS_PROXY:-}" ]; then
  EXTRA+=(--network host --build-arg "HTTPS_PROXY=$HTTPS_PROXY" --build-arg "https_proxy=$HTTPS_PROXY")
fi

docker buildx build --platform "$PLATFORM" --provenance=false --sbom=false \
  --build-arg "PRIVACYFENCE_VERSION=$VERSION" --load -t "$TAG" ${EXTRA[@]+"${EXTRA[@]}"} "$CONTEXT"

echo "Built $TAG ($PLATFORM, privacyfence $VERSION)"
