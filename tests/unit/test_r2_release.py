"""Tests for scripts/r2_release.py's version -> channel mapping.

This script deliberately doesn't import the ``privacyfence`` package (see its own module
docstring) and instead carries its own copy of the version-parsing regex, mirroring
src/privacyfence/update_checker.py's. Imported by file path (importlib) rather than as a package,
since scripts/ isn't part of the installed ``privacyfence`` distribution -- same pattern as
tests/unit/test_build_org_bundle.py.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "r2_release.py"
_spec = importlib.util.spec_from_file_location("r2_release", _SCRIPT_PATH)
r2_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r2_release)


class TestChannelForVersion:
    @pytest.mark.parametrize(
        ("version", "expected_channel"),
        [
            ("4.1.0", "stable"),
            ("v4.1.0", "stable"),  # tolerates a leading "v" the same way update_checker.py does
            ("4.2.0a1", "alpha"),
            ("4.2.0a13", "alpha"),
            ("4.2.0b1", "beta"),
            ("4.2.0rc1", "rc"),
        ],
    )
    def test_maps_version_to_channel(self, version, expected_channel):
        assert r2_release.channel_for_version(version) == expected_channel

    def test_rejects_dev_build(self):
        with pytest.raises(ValueError, match="between-tags dev build"):
            r2_release.channel_for_version("4.2.1.dev3+gabc1234")

    def test_rejects_unparseable_string(self):
        with pytest.raises(ValueError, match="doesn't look like a release version"):
            r2_release.channel_for_version("not-a-version")


class TestUploadRejectsDevBuild:
    def test_upload_raises_before_touching_r2(self, monkeypatch):
        # A dev-build version should fail fast on channel_for_version(), before upload() ever
        # tries to build an R2 client (which would otherwise require R2 credentials just to hit
        # this error path).
        def _fail_if_called():
            raise AssertionError("_r2_client() should not be called for a dev-build version")

        monkeypatch.setattr(r2_release, "_r2_client", _fail_if_called)

        with pytest.raises(ValueError, match="between-tags dev build"):
            r2_release.upload("4.2.1.dev3+gabc1234", ["setup.py"])


# --------------------------------------------------------------------------------------------
# Phase 2: manifest pipeline (finalize / verify / promote) and the upload immutability guard.
#
# Exercised against a fake S3 client rather than real R2: these tests are about this script's own
# ordering and failure decisions, and a test that needs credentials is a test nobody runs.
# --------------------------------------------------------------------------------------------
class _FakeClientError(Exception):
    """Stands in for botocore's ClientError -- r2_release only ever inspects `.response`."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Minimal in-memory S3. Records every mutating call in `operations`, in order, so tests can
    assert the publication ordering finalize() guarantees."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.operations: list[tuple[str, str]] = []

    def put(self, key: str, body: bytes, sha256: str | None = None) -> None:
        """Seed an object directly, bypassing the recorded operations."""
        metadata = {"sha256": sha256} if sha256 else {}
        self.objects[key] = {"Body": body, "Metadata": metadata}

    # -- boto3 surface used by r2_release ------------------------------------------------
    def head_object(self, Bucket, Key):  # noqa: N803 -- boto3's own parameter names
        if Key not in self.objects:
            raise _FakeClientError("404")
        entry = self.objects[Key]
        return {"ContentLength": len(entry["Body"]), "Metadata": entry["Metadata"]}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):  # noqa: N803
        self.operations.append(("upload_file", key))
        self.objects[key] = {
            "Body": Path(filename).read_bytes(),
            "Metadata": (ExtraArgs or {}).get("Metadata", {}),
        }

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.operations.append(("put_object", Key))
        self.objects[Key] = {"Body": Body, "Metadata": {}}

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _FakeClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):  # noqa: N803
        contents = [
            {"Key": key, "Size": len(entry["Body"])}
            for key, entry in sorted(self.objects.items())
            if key.startswith(Prefix)
        ]
        return [{"Contents": contents}]


@pytest.fixture
def fake_s3(monkeypatch):
    client = FakeS3()
    monkeypatch.setattr(r2_release, "_r2_client", lambda: client)
    monkeypatch.setenv("R2_BUCKET", "test-bucket")
    return client


def _seed_release(client, version="4.3.0", channel="stable", *, installers=("dmg", "exe", "deb")):
    """Puts a complete-or-partial set of uploaded release objects in the bucket."""
    prefix = f"releases/{channel}/{version}/"
    names = {
        "dmg": f"PrivacyFence-{version}.dmg",
        "pkg": f"PrivacyFence-{version}.pkg",
        "exe": f"PrivacyFence-{version}-setup.exe",
        "deb": f"privacyfence_{version}_amd64.deb",
    }
    for kind in installers:
        body = f"{kind} bytes".encode()
        client.put(prefix + names[kind], body, sha256=hashlib.sha256(body).hexdigest())
    # Non-installers that really do get uploaded to this same prefix by build.yml.
    for extra in ("sbom-python.cdx.json", "sbom-shim-npm.cdx.json", "build_org_bundle.py"):
        body = b"not an installer"
        client.put(prefix + extra, body, sha256=hashlib.sha256(body).hexdigest())
    return prefix


class TestClassifyInstaller:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("PrivacyFence-4.3.0.dmg", ("macos-arm64", "macos", "arm64")),
            ("PrivacyFence-4.2.0b1.dmg", ("macos-arm64", "macos", "arm64")),
            ("PrivacyFence-4.3.0.pkg", ("macos-arm64-pkg", "macos", "arm64")),
            ("PrivacyFence-4.3.0-setup.exe", ("windows-x64", "windows", "x64")),
            ("privacyfence_4.3.0_amd64.deb", ("linux-x64", "linux", "x64")),
        ],
    )
    def test_installers_are_classified(self, filename, expected):
        assert r2_release.classify_installer(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "sbom-python.cdx.json",
            "sbom-shim-npm.cdx.json",
            "build_org_bundle.py",
            "sync_room_directory.py",
            "privacyfence-4.3.0.tar.gz",
            "privacyfence-4.3.0-py3-none-any.whl",
            "manifest.json",
        ],
    )
    def test_non_installers_are_excluded(self, filename):
        # These stay in R2 but must never enter the manifest: the Worker counts every artifact it
        # serves, so listing them here would silently inflate the installer-download KPI.
        assert r2_release.classify_installer(filename) is None


class TestUploadImmutability:
    def test_records_sha256_metadata(self, fake_s3, tmp_path):
        artifact = tmp_path / "PrivacyFence-4.3.0.dmg"
        artifact.write_bytes(b"installer bytes")

        r2_release.upload("4.3.0", [str(artifact)])

        key = "releases/stable/4.3.0/PrivacyFence-4.3.0.dmg"
        assert fake_s3.objects[key]["Metadata"]["sha256"] == hashlib.sha256(b"installer bytes").hexdigest()

    def test_reupload_of_identical_bytes_is_allowed(self, fake_s3, tmp_path):
        # A re-run of a partially-failed release job repeats uploads that already succeeded.
        artifact = tmp_path / "PrivacyFence-4.3.0.dmg"
        artifact.write_bytes(b"installer bytes")

        r2_release.upload("4.3.0", [str(artifact)])
        fake_s3.operations.clear()
        r2_release.upload("4.3.0", [str(artifact)])

        assert fake_s3.operations == [], "identical re-upload should be skipped, not re-sent"

    def test_different_bytes_under_same_key_hard_fails(self, fake_s3, tmp_path):
        artifact = tmp_path / "PrivacyFence-4.3.0.dmg"
        artifact.write_bytes(b"installer bytes")
        r2_release.upload("4.3.0", [str(artifact)])

        artifact.write_bytes(b"TAMPERED")
        with pytest.raises(SystemExit, match="refusing to overwrite"):
            r2_release.upload("4.3.0", [str(artifact)])


class TestFinalize:
    def test_manifest_contains_only_installers(self, fake_s3):
        _seed_release(fake_s3)

        manifest = r2_release.finalize("4.3.0")

        assert [a["id"] for a in manifest["artifacts"]] == ["linux-x64", "macos-arm64", "windows-x64"]
        assert all(a["kind"] == "installer" for a in manifest["artifacts"])

    def test_manifest_matches_the_workers_fixture_shape(self, fake_s3):
        # Contract test across trees: cloudflare/downloads/src/manifest.ts parses what this
        # writes, and its fixtures are the shape it was built against. If these diverge, the
        # Worker breaks in production rather than here.
        fixture_path = Path(__file__).resolve().parents[2] / "cloudflare/downloads/test/fixtures/stable/manifest.json"
        fixture = json.loads(fixture_path.read_text())
        _seed_release(fake_s3)

        manifest = r2_release.finalize("4.3.0")

        assert manifest.keys() == fixture.keys()
        assert manifest["schema"] == fixture["schema"]
        assert manifest["artifacts"][0].keys() == fixture["artifacts"][0].keys()

    def test_writes_manifest_before_latest_pointer(self, fake_s3):
        # The transactional guarantee: latest.json moves last, after verification.
        _seed_release(fake_s3)

        r2_release.finalize("4.3.0")

        written = [key for op, key in fake_s3.operations if op == "put_object"]
        assert written == [
            "releases/stable/4.3.0/manifest.json",
            "releases/stable/latest.json",
        ]

    def test_missing_installer_leaves_latest_untouched(self, fake_s3):
        fake_s3.put("releases/stable/latest.json", b'{"version": "4.2.0"}')
        _seed_release(fake_s3, installers=("dmg", "exe"))  # no .deb

        with pytest.raises(SystemExit, match="missing mandatory installer"):
            r2_release.finalize("4.3.0")

        assert fake_s3.objects["releases/stable/latest.json"]["Body"] == b'{"version": "4.2.0"}'
        assert "releases/stable/4.3.0/manifest.json" not in fake_s3.objects

    def test_missing_pkg_does_not_block_latest(self, fake_s3):
        # #428 D2: unlike dmg/exe/deb, the .pkg is not in REQUIRED_ARTIFACT_IDS -- a release with
        # no .pkg at all (the default _seed_release set) must still finalize and reach "latest".
        _seed_release(fake_s3)  # dmg, exe, deb -- no pkg

        manifest = r2_release.finalize("4.3.0")

        assert {artifact["id"] for artifact in manifest["artifacts"]} == {"macos-arm64", "windows-x64", "linux-x64"}
        pointer = json.loads(fake_s3.objects["releases/stable/latest.json"]["Body"])
        assert pointer == {"version": "4.3.0", "manifest": "releases/stable/4.3.0/manifest.json"}

    def test_pkg_is_included_when_present_but_still_optional(self, fake_s3):
        _seed_release(fake_s3, installers=("dmg", "exe", "deb", "pkg"))

        manifest = r2_release.finalize("4.3.0")

        ids = {artifact["id"] for artifact in manifest["artifacts"]}
        assert ids == {"macos-arm64", "macos-arm64-pkg", "windows-x64", "linux-x64"}
        pkg_artifact = next(a for a in manifest["artifacts"] if a["id"] == "macos-arm64-pkg")
        assert pkg_artifact["filename"] == "PrivacyFence-4.3.0.pkg"
        assert pkg_artifact["platform"] == "macos"
        assert "macos-arm64-pkg" not in r2_release.REQUIRED_ARTIFACT_IDS

    def test_object_without_recorded_digest_is_refused(self, fake_s3):
        prefix = _seed_release(fake_s3)
        fake_s3.objects[prefix + "PrivacyFence-4.3.0.dmg"]["Metadata"] = {}

        with pytest.raises(SystemExit, match="no recorded sha256"):
            r2_release.finalize("4.3.0")

    def test_pre_release_finalizes_into_its_own_channel(self, fake_s3):
        _seed_release(fake_s3, version="4.4.0b1", channel="beta")

        manifest = r2_release.finalize("4.4.0b1")

        assert manifest["channel"] == "beta"
        assert [key for op, key in fake_s3.operations if op == "put_object"][-1] == "releases/beta/latest.json"


class TestVerify:
    def test_detects_size_mismatch(self, fake_s3):
        _seed_release(fake_s3)
        r2_release.finalize("4.3.0")
        fake_s3.objects["releases/stable/4.3.0/PrivacyFence-4.3.0.dmg"]["Body"] = b"short"

        with pytest.raises(SystemExit, match="size mismatch"):
            r2_release.verify("4.3.0")

    def test_detects_sha256_mismatch(self, fake_s3):
        _seed_release(fake_s3)
        r2_release.finalize("4.3.0")
        fake_s3.objects["releases/stable/4.3.0/PrivacyFence-4.3.0.dmg"]["Metadata"]["sha256"] = "0" * 64

        with pytest.raises(SystemExit, match="sha256 mismatch"):
            r2_release.verify("4.3.0")

    def test_detects_missing_object(self, fake_s3):
        _seed_release(fake_s3)
        r2_release.finalize("4.3.0")
        del fake_s3.objects["releases/stable/4.3.0/privacyfence_4.3.0_amd64.deb"]

        with pytest.raises(SystemExit, match="missing from storage"):
            r2_release.verify("4.3.0")

    def test_without_a_manifest_says_so(self, fake_s3):
        with pytest.raises(SystemExit, match="run `finalize` first"):
            r2_release.verify("4.3.0")


class TestPromote:
    def test_writes_pointer_at_the_manifest(self, fake_s3):
        fake_s3.put("releases/stable/4.3.0/manifest.json", b"{}")
        r2_release.promote("4.3.0")

        pointer = json.loads(fake_s3.objects["releases/stable/latest.json"]["Body"])
        assert pointer == {"version": "4.3.0", "manifest": "releases/stable/4.3.0/manifest.json"}

    def test_pointer_matches_the_workers_fixture_shape(self, fake_s3):
        fixture_path = Path(__file__).resolve().parents[2] / "cloudflare/downloads/test/fixtures/stable/latest.json"
        fixture = json.loads(fixture_path.read_text())
        fake_s3.put("releases/stable/4.3.0/manifest.json", b"{}")
        r2_release.promote("4.3.0")

        pointer = json.loads(fake_s3.objects["releases/stable/latest.json"]["Body"])
        assert pointer.keys() == fixture.keys()

    def test_refuses_to_promote_a_version_with_no_manifest(self, fake_s3):
        # Promoting a typo'd version would otherwise point the whole channel at a 404, with the
        # previous latest.json already overwritten and nothing to roll back to.
        fake_s3.put("releases/stable/latest.json", b'{"version": "4.2.0"}')

        with pytest.raises(SystemExit, match="refusing to promote"):
            r2_release.promote("4.3.0")

        assert fake_s3.objects["releases/stable/latest.json"]["Body"] == b'{"version": "4.2.0"}'
