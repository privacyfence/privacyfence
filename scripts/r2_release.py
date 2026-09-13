#!/usr/bin/env python3
"""Publish release artifacts to the Cloudflare R2 release archive.

Replaces (and was rehearsed by) the one-off ".github/workflows/r2-smoke-test.yml" workflow --
that workflow proved the GitHub Actions -> R2 credentials/endpoint plumbing works and has been
deleted now that this script exists.

Every tagged release (stable and pre-release alike) uploads its artifacts here, laid out as:

    releases/
      stable/<version>/...
      beta/<version>/...
      alpha/<version>/...
      rc/<version>/...

"<version>" is __version__ as setuptools_scm resolves it from the git tag (see this repo's
CLAUDE.md "Releasing" section), e.g. "4.1.0" or "4.2.0b1" -- never the "v"-prefixed tag name
itself. The channel directory is derived from that version's PEP 440 pre-release suffix (a/b/rc,
same short spellings src/privacyfence/update_checker.py's _STAGE_RANK already uses to rank
update-check results); a version with no suffix is "stable".

This bucket is where alpha/beta/rc artifacts live *instead of* going anywhere public: unlike
stable, which also reaches PyPI and a public GitHub Release, pre-release tags stop at R2 --
see .github/workflows/publish-pypi.yml and build.yml for exactly which channels reach which public
index. The bucket itself is left at Cloudflare R2's default (private, no public bucket policy or
custom domain) -- distributing an alpha/beta download link to testers is a separate, not-yet-
decided step, deliberately not automated by this script.

Stdlib + boto3 only (`pip install boto3`) -- no PrivacyFence install required, matching the other
release-time scripts in this directory (scripts/build_org_bundle.py, scripts/sync_room_directory.py).
This script deliberately doesn't import the ``privacyfence`` package for the same reason: it
carries its own copy of the version-parsing regex, mirroring update_checker.py's rather than
importing it, so a CI job can run it (e.g. the SBOM job in build.yml) without first installing the
full package.

Requires these environment variables (matching r2-smoke-test.yml's, before its deletion; renamed
from the original CF_R2_* to disambiguate from the download Worker's own CLOUDFLARE_* deploy
credentials -- see this repo's CLAUDE.md "Cloudflare R2 release archive" section):
    CF_RELEASES_R2_ACCESS_KEY_ID, CF_RELEASES_R2_SECRET_ACCESS_KEY  -- R2 API token
    CF_RELEASES_R2_ENDPOINT                                         -- R2 S3-compatible endpoint URL
    R2_BUCKET                                                       -- defaults to "privacyfence-releases"

Examples:
    python3 scripts/r2_release.py channel 4.2.0b1
        -> prints "beta"

    python3 scripts/r2_release.py upload --version 4.1.0 \\
        dist/PrivacyFence-4.1.0.dmg scripts/build_org_bundle.py scripts/sync_room_directory.py
        -> uploads each to releases/stable/4.1.0/<basename> in R2

    python3 scripts/r2_release.py finalize --version 4.1.0
        -> writes releases/stable/4.1.0/manifest.json, verifies it, then points
           releases/stable/latest.json at it -- in that order

    python3 scripts/r2_release.py verify --version 4.1.0
    python3 scripts/r2_release.py promote --version 4.1.0

Publication is transactional from a downloader's point of view. `upload` puts objects in the
bucket but publishes nothing; only `finalize` moves the channel's latest.json, and only after
confirming every artifact the manifest references is really there with the right size and digest.
An incomplete release can therefore exist in R2 without ever being the one the Worker serves.

Only installers (DMG / -setup.exe / .deb) enter the manifest -- see _INSTALLERS for why SBOMs,
the org-config scripts and the sdist/wheel are uploaded here but deliberately left out of it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Mirrors src/privacyfence/update_checker.py's _VERSION_RE / _STAGE_RANK -- see this module's
# docstring for why this is a deliberate copy rather than an import. Unlike that module (which
# also needs to parse a leading "v" and a trailing ".dev<n>+local" dev-build segment, for comparing
# against a running dev build's own __version__), this script only ever sees a real resolved
# version string, so the "v" and ".dev" pieces are still matched here -- just to reject them below
# with a clear error, not to fall back to anything.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?(?:\+.*)?$")
_STAGE_TO_CHANNEL = {"a": "alpha", "b": "beta", "rc": "rc"}

DEFAULT_BUCKET = "privacyfence-releases"

# Schema version of the manifest this script writes. Consumed by cloudflare/downloads/src/
# manifest.ts, whose `Manifest`/`ManifestArtifact` interfaces are the real contract -- that tree
# shipped first (Phase 1) against hand-written fixtures of this exact shape, so changing anything
# here means changing the Worker and its fixtures in the same PR.
MANIFEST_SCHEMA = 1

# SHA-256 is recorded as S3 user metadata at upload time rather than recomputed later, because
# `finalize` runs in its own job on a fresh runner: the DMG/installer/.deb were each built and
# uploaded by a *different* job (build, build-windows, build-deb), so none of those files exist
# on disk by the time the manifest is assembled. Reading the digest back from head_object() keeps
# both `finalize` and the immutability guard to one metadata call per object instead of
# re-downloading release binaries just to hash them. (The ETag is no substitute: boto3 switches to
# a multipart upload above its threshold, and a multipart ETag is not the object's MD5.)
_SHA256_METADATA_KEY = "sha256"

# Installer filename -> manifest identity. **Installers only**: SBOMs, the org-config scripts and
# the sdist/wheel are uploaded to the same prefix but deliberately never enter the manifest, so
# the Worker cannot serve them from /download/ and cannot count them. That keeps the KPI
# definition ("SBOMs, checksums, org-admin scripts, metadata requests, source archives ... never
# count as downloads") true by construction rather than by a filter someone has to remember to
# apply -- cloudflare/downloads/src/index.ts counts every artifact it serves, and queryStats() sums
# every row without filtering on artifact_kind.
#
# The `id` is the URL segment: /download/<channel>/<id> resolves against it via the Worker's
# findArtifact(). These three match the runners in build.yml that produce them (macos-latest is
# arm64, windows-latest x64, ubuntu-latest amd64) and the fixtures in
# cloudflare/downloads/test/fixtures/.
_INSTALLERS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r"^PrivacyFence-[^/]*\.dmg$"), "macos-arm64", "macos", "arm64"),
    (re.compile(r"^PrivacyFence-[^/]*-setup\.exe$"), "windows-x64", "windows", "x64"),
    (re.compile(r"^privacyfence_[^/]*_amd64\.deb$"), "linux-x64", "linux", "x64"),
)

# Every one of these must be present before a release may become "latest". A release missing any
# mandatory installer can still exist in R2 -- it just never gets a latest.json pointing at it.
REQUIRED_ARTIFACT_IDS = frozenset(spec[1] for spec in _INSTALLERS)


def channel_for_version(version: str) -> str:
    """Returns "stable", "beta", "alpha", or "rc" for a resolved version string like "4.1.0" or
    "4.2.0b1". Raises ValueError for anything that isn't a real tagged release -- most importantly
    a between-tags dev build (e.g. "4.2.1.dev3+gabc1234"), which was never `git tag`d and has
    nothing to publish."""
    match = _VERSION_RE.match(version.strip())
    if not match:
        raise ValueError(f"{version!r} doesn't look like a release version (major.minor.patch[a|b|rc<n>])")
    if match.group(6) is not None:
        raise ValueError(
            f"{version!r} is a between-tags dev build, not a tagged release -- nothing to publish "
            "(run this against a commit that's actually tagged)"
        )
    stage = match.group(4)
    return _STAGE_TO_CHANNEL[stage] if stage else "stable"


def _r2_client():
    import boto3

    missing = [
        name
        for name in ("CF_RELEASES_R2_ACCESS_KEY_ID", "CF_RELEASES_R2_SECRET_ACCESS_KEY", "CF_RELEASES_R2_ENDPOINT")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")

    return boto3.client(
        "s3",
        endpoint_url=os.environ["CF_RELEASES_R2_ENDPOINT"],
        aws_access_key_id=os.environ["CF_RELEASES_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_RELEASES_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 of a local file -- release artifacts are far too large to slurp."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_installer(filename: str) -> tuple[str, str, str] | None:
    """Maps an installer filename to its (id, platform, architecture), or None for anything
    that is not an installer -- SBOMs, org-config scripts, sdist/wheel. See _INSTALLERS."""
    for pattern, artifact_id, platform, architecture in _INSTALLERS:
        if pattern.match(filename):
            return artifact_id, platform, architecture
    return None


def _bucket_name(bucket: str | None) -> str:
    return bucket or os.environ.get("R2_BUCKET", DEFAULT_BUCKET)


def _release_prefix(channel: str, version: str) -> str:
    return f"releases/{channel}/{version}/"


def _head(s3, bucket: str, key: str) -> dict | None:
    """head_object(), with a missing object reported as None rather than an exception."""
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # botocore raises ClientError; importing it here isn't worth it
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def _stored_sha256(head: dict) -> str | None:
    """The digest `upload` recorded as user metadata, if this object carries one. Objects
    uploaded before that existed have none, which callers must report rather than paper over."""
    return (head.get("Metadata") or {}).get(_SHA256_METADATA_KEY)


def upload(version: str, files: list[str], *, bucket: str | None = None) -> None:
    channel = channel_for_version(version)
    bucket = _bucket_name(bucket)
    s3 = _r2_client()

    for file_arg in files:
        path = Path(file_arg)
        if not path.is_file():
            raise SystemExit(f"Not a file: {path}")

        key = f"{_release_prefix(channel, version)}{path.name}"
        digest = sha256_file(path)

        # Immutability guard: a published binary must never silently change under a version that
        # someone may already have downloaded. Re-uploading the identical bytes is fine and has to
        # be -- a re-run of a failed release job repeats every upload that already succeeded.
        existing = _head(s3, bucket, key)
        if existing is not None:
            previous = _stored_sha256(existing)
            if previous == digest:
                print(f"unchanged s3://{bucket}/{key} (sha256 {digest})")
                continue
            raise SystemExit(
                f"refusing to overwrite s3://{bucket}/{key} with different content:\n"
                f"  already published: sha256 {previous or '<no recorded digest>'}\n"
                f"  about to upload:   sha256 {digest}\n"
                "A released artifact is immutable. Cut a new version rather than replacing this one."
            )

        s3.upload_file(str(path), bucket, key, ExtraArgs={"Metadata": {_SHA256_METADATA_KEY: digest}})
        result = s3.head_object(Bucket=bucket, Key=key)
        print(f"uploaded s3://{bucket}/{key} ({result['ContentLength']} bytes, sha256 {digest})")


def _list_release_objects(s3, bucket: str, prefix: str) -> dict[str, dict]:
    """Every object under a release's prefix, keyed by filename (the part after the prefix)."""
    objects: dict[str, dict] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents", []):
            filename = entry["Key"][len(prefix) :]
            if filename and "/" not in filename:
                objects[filename] = entry
    return objects


def build_manifest(version: str, channel: str, artifacts: list[dict], *, published_at: str) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "channel": channel,
        "published_at": published_at,
        # Sorted by id so a re-run produces a byte-identical manifest rather than one that merely
        # means the same thing -- makes a diff between two finalize runs actually readable.
        "artifacts": sorted(artifacts, key=lambda artifact: artifact["id"]),
    }


def _load_manifest(s3, bucket: str, channel: str, version: str) -> dict:
    key = f"{_release_prefix(channel, version)}manifest.json"
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise SystemExit(f"no manifest at s3://{bucket}/{key} -- run `finalize` first") from exc
        raise
    return json.loads(body)


def verify(version: str, *, bucket: str | None = None) -> dict:
    """Checks every object the manifest references actually exists, with matching size and
    SHA-256. Returns the manifest so `finalize` can reuse it. Raises SystemExit on any mismatch."""
    channel = channel_for_version(version)
    bucket = _bucket_name(bucket)
    s3 = _r2_client()
    manifest = _load_manifest(s3, bucket, channel, version)

    problems: list[str] = []
    for artifact in manifest.get("artifacts", []):
        key = artifact["key"]
        head = _head(s3, bucket, key)
        if head is None:
            problems.append(f"{artifact['id']}: missing from storage ({key})")
            continue
        if head["ContentLength"] != artifact["size"]:
            problems.append(
                f"{artifact['id']}: size mismatch -- manifest {artifact['size']}, stored {head['ContentLength']}"
            )
        stored = _stored_sha256(head)
        if stored is None:
            problems.append(f"{artifact['id']}: object has no recorded sha256 -- re-upload it with this script")
        elif stored != artifact["sha256"]:
            problems.append(f"{artifact['id']}: sha256 mismatch -- manifest {artifact['sha256']}, stored {stored}")

    if problems:
        raise SystemExit("manifest verification failed:\n  " + "\n  ".join(problems))

    print(f"verified {len(manifest.get('artifacts', []))} artifact(s) for {version} ({channel})")
    return manifest


def promote(version: str, *, bucket: str | None = None) -> None:
    """Points the channel's latest.json at this version. Separated from `finalize` so a release
    can be re-promoted by hand without rebuilding its manifest."""
    channel = channel_for_version(version)
    bucket = _bucket_name(bucket)
    s3 = _r2_client()

    # Refuse to point a whole channel at a manifest that isn't there. `finalize` always writes the
    # manifest first so this never fires on its path, but this command exists to be run by hand --
    # and by hand is exactly when a typo'd version would otherwise leave every downloader on this
    # channel resolving latest.json to a 404 with nothing to roll back to.
    manifest_key = f"{_release_prefix(channel, version)}manifest.json"
    if _head(s3, bucket, manifest_key) is None:
        raise SystemExit(
            f"refusing to promote {version} ({channel}): no manifest at s3://{bucket}/{manifest_key}.\n"
            "Run `finalize` for this version first."
        )

    key = f"releases/{channel}/latest.json"
    pointer = {"version": version, "manifest": f"{_release_prefix(channel, version)}manifest.json"}
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(pointer, indent=2).encode() + b"\n",
        ContentType="application/json",
    )
    print(f"promoted {channel} latest -> {version}")


def finalize(version: str, *, bucket: str | None = None, now: datetime | None = None) -> dict:
    """Assembles and publishes a release's manifest, then -- only if verification passes -- moves
    the channel's latest.json onto it.

    The ordering is the whole point: artifacts and manifest.json land first, verification runs
    against what is actually stored, and latest.json is written last. An incomplete or corrupt
    release can therefore exist in R2 without ever becoming visible to the Worker, which reaches
    every release through latest.json (or an explicit version).
    """
    channel = channel_for_version(version)
    bucket = _bucket_name(bucket)
    s3 = _r2_client()
    prefix = _release_prefix(channel, version)

    stored = _list_release_objects(s3, bucket, prefix)
    artifacts: list[dict] = []
    missing_digests: list[str] = []
    for filename, entry in sorted(stored.items()):
        identity = classify_installer(filename)
        if identity is None:
            continue  # not an installer -- stays in R2, never enters the manifest
        artifact_id, platform, architecture = identity
        head = _head(s3, bucket, entry["Key"])
        digest = _stored_sha256(head) if head else None
        if digest is None:
            missing_digests.append(filename)
            continue
        artifacts.append(
            {
                "id": artifact_id,
                "kind": "installer",
                "platform": platform,
                "architecture": architecture,
                "filename": filename,
                "key": entry["Key"],
                "size": entry["Size"],
                "sha256": digest,
            }
        )

    if missing_digests:
        raise SystemExit(
            "these objects have no recorded sha256 and were probably uploaded before this script "
            "recorded one -- re-upload them:\n  " + "\n  ".join(sorted(missing_digests))
        )

    found_ids = {artifact["id"] for artifact in artifacts}
    absent = REQUIRED_ARTIFACT_IDS - found_ids
    if absent:
        raise SystemExit(
            f"cannot finalize {version} ({channel}): missing mandatory installer(s) "
            f"{', '.join(sorted(absent))}.\n"
            f"latest.json is left untouched, so this incomplete release is not published."
        )

    published_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = build_manifest(version, channel, artifacts, published_at=published_at)
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}manifest.json",
        Body=json.dumps(manifest, indent=2).encode() + b"\n",
        ContentType="application/json",
    )
    print(f"wrote s3://{bucket}/{prefix}manifest.json ({len(artifacts)} artifacts)")

    verify(version, bucket=bucket)
    promote(version, bucket=bucket)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    channel_parser = subparsers.add_parser("channel", help="Print the release channel for a version string")
    channel_parser.add_argument("version")

    upload_parser = subparsers.add_parser("upload", help="Upload files to the R2 release archive")
    upload_parser.add_argument("--version", required=True, help="Resolved release version, e.g. 4.2.0b1")
    upload_parser.add_argument("--bucket", default=None, help=f"R2 bucket (default: {DEFAULT_BUCKET})")
    upload_parser.add_argument("files", nargs="+", help="Local files to upload")

    for name, help_text in (
        ("finalize", "Build and publish this release's manifest, then move the channel's latest.json"),
        ("verify", "Check every object the manifest references exists with matching size/sha256"),
        ("promote", "Point the channel's latest.json at this version (without rebuilding the manifest)"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--version", required=True, help="Resolved release version, e.g. 4.2.0b1")
        sub.add_argument("--bucket", default=None, help=f"R2 bucket (default: {DEFAULT_BUCKET})")

    args = parser.parse_args(argv)

    if args.command == "channel":
        try:
            print(channel_for_version(args.version))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    handlers = {
        "upload": lambda: upload(args.version, args.files, bucket=args.bucket),
        "finalize": lambda: finalize(args.version, bucket=args.bucket),
        "verify": lambda: verify(args.version, bucket=args.bucket),
        "promote": lambda: promote(args.version, bucket=args.bucket),
    }
    if args.command in handlers:
        try:
            handlers[args.command]()
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    return 1  # pragma: no cover -- unreachable, argparse enforces `required=True` above


if __name__ == "__main__":
    sys.exit(main())
