#!/usr/bin/env python3
"""Publish release files to the Cloudflare R2 release archive.

Every tagged release (stable and pre-release alike) uploads its files here, laid out as:

    releases/
      stable/<version>/...
      beta/<version>/...
      alpha/<version>/...
      rc/<version>/...

"<version>" is __version__ as setuptools_scm resolves it from the git tag (see this repo's
docs/releasing.md), e.g. "4.1.0" or "4.2.0b1" -- never the "v"-prefixed tag name
itself. The channel directory is derived from that version's PEP 440 pre-release suffix (a/b/rc,
same short spellings src/privacyfence/update_checker.py's _STAGE_RANK already uses to rank
update-check results); a version with no suffix is "stable".

A release (ADR 0306) builds the organization image, privacyfence-sync and the SBOM only, so what
lands here is the SBOM and the org admin tools (scripts/sync_room_directory.py,
scripts/sign_connector_config.py and the connector config signing page). The archive keeps these
uploads and nothing else: there is no manifest and no `latest` pointer. The bucket is left at
Cloudflare R2's default (private, no public bucket policy or custom domain).

Stdlib + boto3 only (`pip install boto3`) -- no PrivacyFence install required, matching the other
release-time scripts in this directory (scripts/sync_room_directory.py). This script deliberately
doesn't import the ``privacyfence`` package for the same reason: it carries its own copy of the
version-parsing regex, mirroring update_checker.py's rather than importing it, so a CI job can run
it (e.g. the SBOM job in build.yml) without first installing the full package.

Requires these environment variables (prefixed CF_RELEASES_ to keep them apart from other
Cloudflare credentials):
    CF_RELEASES_R2_ACCESS_KEY_ID, CF_RELEASES_R2_SECRET_ACCESS_KEY  -- R2 API token
    CF_RELEASES_R2_ENDPOINT                                         -- R2 S3-compatible endpoint URL
    R2_BUCKET                                                       -- defaults to "privacyfence-releases"

Examples:
    python3 scripts/r2_release.py channel 4.2.0b1
        -> prints "beta"

    python3 scripts/r2_release.py check-tag --tag v4.2.0b1 --version 4.2.0b1
        -> exits 0; exits 1 if the build resolved a version the tag doesn't name

    python3 scripts/r2_release.py upload --version 4.1.0 \\
        sbom-python.cdx.json scripts/sync_room_directory.py
        -> uploads each to releases/stable/4.1.0/<basename> in R2
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

# Mirrors src/privacyfence/update_checker.py's _VERSION_RE / _STAGE_RANK -- see this module's
# docstring for why this is a deliberate copy rather than an import. Unlike that module (which
# also needs to parse a leading "v" and a trailing ".dev<n>+local" dev-build segment, for comparing
# against a running dev build's own __version__), this script only ever sees a real resolved
# version string, so the "v" and ".dev" pieces are still matched here -- just to reject them below
# with a clear error, not to fall back to anything.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?(?:\+.*)?$")
_STAGE_TO_CHANNEL = {"a": "alpha", "b": "beta", "rc": "rc"}

# Tag names, for assert_tag_matches_version() below only -- deliberately more permissive than
# _VERSION_RE above, which parses versions setuptools_scm has already resolved and therefore
# already canonicalized. A tag is written by hand, and PEP 440 lets one pre-release be spelled
# several ways that all normalize to the same version ("v4.1.0-alpha1", "v4.1.0.a1" and "v4.1.0a1"
# all resolve to "4.1.0a1"; this repo has shipped both the "v4.1.0-a1" and "v4.1.0a5" spellings).
# A check that rejected a spelling setuptools_scm accepts would fail releases this guard has no
# business failing, so the group layout is kept identical to _VERSION_RE's and the stage word is
# mapped back to its short form below.
_TAG_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:[-_.]?(alpha|beta|preview|pre|rc|c|a|b)[-_.]?(\d+))?(?:[-_.]?dev(\d+))?(?:\+.*)?$",
    re.IGNORECASE,
)
_TAG_STAGE_ALIASES = {
    "alpha": "a",
    "a": "a",
    "beta": "b",
    "b": "b",
    "preview": "rc",
    "pre": "rc",
    "c": "rc",
    "rc": "rc",
}

DEFAULT_BUCKET = "privacyfence-releases"

# SHA-256 is recorded as S3 user metadata at upload time, so the immutability guard in `upload`
# needs one metadata call per object instead of re-downloading release files just to hash them.
# (The ETag is no substitute: boto3 switches to a multipart upload above its threshold, and a
# multipart ETag is not the object's MD5.)
_SHA256_METADATA_KEY = "sha256"


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


def _release_identity(value: str, pattern: re.Pattern[str]) -> tuple[int, int, int, str, int]:
    """Parses a release version (with _VERSION_RE) or a tag naming one (with _TAG_RE) into the
    identity every spelling of that release shares: (major, minor, patch, stage, stage number).
    Raises ValueError for anything that isn't a real tagged release, same as channel_for_version()
    -- most importantly a between-tags dev build, which was never `git tag`d."""
    match = pattern.match(value.strip())
    if not match:
        raise ValueError(f"{value!r} doesn't look like a release version (major.minor.patch[a|b|rc<n>])")
    if match.group(6) is not None:
        raise ValueError(
            f"{value!r} is a between-tags dev build, not a tagged release -- nothing to publish "
            "(run this against a commit that's actually tagged)"
        )
    stage = match.group(4)
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        _TAG_STAGE_ALIASES[stage.lower()] if stage else "",
        int(match.group(5) or 0),
    )


def assert_tag_matches_version(tag: str, version: str) -> None:
    """Raises ValueError unless `version` -- what the build resolved -- is the release that the
    pushed git tag `tag` names.

    Guards the one failure mode that otherwise stays invisible until an upload is already being
    attempted. setuptools_scm derives the version from `git describe`, which reports *a* tag on the
    commit being built, not specifically the tag whose push started the run: when several release
    tags point at one commit, describe picks one of them, and the build stamps every artifact with
    a version nobody asked to release. Retagging a commit for a second pre-release is enough to
    trigger it, and what it produces is a build trying to republish an already-published version
    under different bytes -- which upload()'s immutability guard correctly refuses, minutes later,
    once a full artifact set has been built and signed. The run that did this:
    https://github.com/privacyfence/privacyfence/actions/runs/35388772087

    Compares parsed identities rather than strings, so a tag spelled the long way ("v4.1.0-alpha1")
    still matches the version setuptools_scm resolves it to ("4.1.0a1") -- see _TAG_RE."""
    if _release_identity(tag, _TAG_RE) != _release_identity(version, _VERSION_RE):
        raise ValueError(
            f"tag {tag!r} does not name the version this build resolved ({version!r}).\n"
            "setuptools_scm derives the version from `git describe`, which reports one of the tags "
            "on the commit being built -- when a commit carries several release tags, that need "
            "not be the tag whose push started this run. Delete the tag that isn't wanted and "
            "re-run this workflow from the tag that is; a version already published stays "
            "published, since release artifacts are immutable."
        )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    channel_parser = subparsers.add_parser("channel", help="Print the release channel for a version string")
    channel_parser.add_argument("version")

    check_tag_parser = subparsers.add_parser(
        "check-tag", help="Fail unless the resolved version is the one the pushed tag names"
    )
    check_tag_parser.add_argument("--tag", required=True, help="Tag that triggered the run, e.g. v4.2.0b1")
    check_tag_parser.add_argument("--version", required=True, help="Version the build resolved, e.g. 4.2.0b1")

    upload_parser = subparsers.add_parser("upload", help="Upload files to the R2 release archive")
    upload_parser.add_argument("--version", required=True, help="Resolved release version, e.g. 4.2.0b1")
    upload_parser.add_argument("--bucket", default=None, help=f"R2 bucket (default: {DEFAULT_BUCKET})")
    upload_parser.add_argument("files", nargs="+", help="Local files to upload")

    args = parser.parse_args(argv)

    if args.command == "channel":
        try:
            print(channel_for_version(args.version))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "check-tag":
        try:
            assert_tag_matches_version(args.tag, args.version)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"tag {args.tag} names the resolved version {args.version}")
        return 0

    handlers = {
        "upload": lambda: upload(args.version, args.files, bucket=args.bucket),
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
