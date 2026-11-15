"""Tests for scripts/r2_release.py's version -> channel mapping.

This script deliberately doesn't import the ``privacyfence`` package (see its own module
docstring) and instead carries its own copy of the version-parsing regex, mirroring
src/privacyfence/update_checker.py's. Imported by file path (importlib) rather than as a package,
since scripts/ isn't part of the installed ``privacyfence`` distribution -- same pattern as
the other release-script tests.
"""

from __future__ import annotations

import hashlib
import importlib.util
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


class TestAssertTagMatchesVersion:
    """The guard that keeps a retagged commit from building someone else's version.

    setuptools_scm resolves __version__ from `git describe`, which reports *a* tag on the commit
    being built -- so a commit carrying both v4.1.0a6 and v4.1.0a7 can build as 4.1.0a6 on a
    v4.1.0a7 push, then fail at upload time against the immutability guard below.
    """

    @pytest.mark.parametrize(
        ("tag", "version"),
        [
            ("v4.1.0", "4.1.0"),
            ("4.1.0", "4.1.0"),  # tolerates a tag spelled without the "v", same as channel above
            ("v4.2.0b1", "4.2.0b1"),
            ("v4.2.0rc2", "4.2.0rc2"),
            # PEP 440 lets one pre-release be spelled several ways and setuptools_scm normalizes
            # them all, so tag and resolved version are routinely spelled differently while naming
            # one release. This check must not be stricter about that than setuptools_scm is.
            ("v4.1.0-a1", "4.1.0a1"),
            ("v4.1.0.rc2", "4.1.0rc2"),
            ("v4.1.0_b3", "4.1.0b3"),
            ("v4.1.0-alpha1", "4.1.0a1"),
            ("v4.1.0-beta2", "4.1.0b2"),
            ("v4.1.0-preview3", "4.1.0rc3"),
            ("v4.1.0C4", "4.1.0rc4"),
        ],
    )
    def test_accepts_matching_tag(self, tag, version):
        assert r2_release.assert_tag_matches_version(tag, version) is None

    @pytest.mark.parametrize(
        ("tag", "version"),
        [
            ("v4.1.0a7", "4.1.0a6"),  # the real one: two alpha tags on one commit
            ("v4.1.0a7", "4.1.0"),  # pre-release tag, stable version
            ("v4.1.0", "4.1.0a7"),  # and the reverse
            ("v4.2.0", "4.1.0"),
            ("v4.1.1", "4.1.0"),
            ("v4.2.0b1", "4.2.0a1"),  # same number, different stage
        ],
    )
    def test_rejects_mismatched_tag(self, tag, version):
        with pytest.raises(ValueError, match="does not name the version this build resolved"):
            r2_release.assert_tag_matches_version(tag, version)

    def test_rejects_dev_build_version(self):
        # A shallow clone (or a tag history that never arrived) resolves to fallback_version or a
        # dev version -- neither names a release, so this fails here rather than at upload.
        with pytest.raises(ValueError, match="between-tags dev build"):
            r2_release.assert_tag_matches_version("v4.1.0", "4.1.0.dev3+gabc1234")

    def test_rejects_unparseable_tag(self):
        with pytest.raises(ValueError, match="doesn't look like a release version"):
            r2_release.assert_tag_matches_version("not-a-tag", "4.1.0")


class TestCheckTagCommand:
    """The `check-tag` CLI surface build.yml and publish-pypi.yml's version step actually call."""

    def test_exits_zero_on_match(self, capsys):
        assert r2_release.main(["check-tag", "--tag", "v4.2.0b1", "--version", "4.2.0b1"]) == 0
        assert "4.2.0b1" in capsys.readouterr().out

    def test_exits_one_on_mismatch(self, capsys):
        assert r2_release.main(["check-tag", "--tag", "v4.1.0a7", "--version", "4.1.0a6"]) == 1
        # The message has to be enough to act on from a CI log alone -- both spellings, and why.
        stderr = capsys.readouterr().err
        assert "v4.1.0a7" in stderr
        assert "4.1.0a6" in stderr
        assert "git describe" in stderr


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
# Manifest pipeline (finalize / verify / promote) and the upload immutability guard.
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
    assert what `upload` sent."""

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


@pytest.fixture
def fake_s3(monkeypatch):
    client = FakeS3()
    monkeypatch.setattr(r2_release, "_r2_client", lambda: client)
    monkeypatch.setenv("R2_BUCKET", "test-bucket")
    return client


class TestUploadImmutability:
    def test_records_sha256_metadata(self, fake_s3, tmp_path):
        artifact = tmp_path / "sbom-python.cdx.json"
        artifact.write_bytes(b"sbom bytes")

        r2_release.upload("4.3.0", [str(artifact)])

        key = "releases/stable/4.3.0/sbom-python.cdx.json"
        assert fake_s3.objects[key]["Metadata"]["sha256"] == hashlib.sha256(b"sbom bytes").hexdigest()

    def test_reupload_of_identical_bytes_is_allowed(self, fake_s3, tmp_path):
        # A re-run of a partially-failed release job repeats uploads that already succeeded.
        artifact = tmp_path / "sbom-python.cdx.json"
        artifact.write_bytes(b"sbom bytes")

        r2_release.upload("4.3.0", [str(artifact)])
        fake_s3.operations.clear()
        r2_release.upload("4.3.0", [str(artifact)])

        assert fake_s3.operations == [], "identical re-upload should be skipped, not re-sent"

    def test_different_bytes_under_same_key_hard_fails(self, fake_s3, tmp_path):
        artifact = tmp_path / "sbom-python.cdx.json"
        artifact.write_bytes(b"sbom bytes")
        r2_release.upload("4.3.0", [str(artifact)])

        artifact.write_bytes(b"TAMPERED")
        with pytest.raises(SystemExit, match="refusing to overwrite"):
            r2_release.upload("4.3.0", [str(artifact)])
