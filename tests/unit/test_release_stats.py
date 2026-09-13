"""Tests for scripts/release_stats.py (release-publishing plan Phase 4).

Imported by file path (importlib), same pattern as tests/unit/test_r2_release.py -- scripts/ isn't
part of the installed ``privacyfence`` distribution. release_stats.py itself imports
``classify_installer`` from scripts/r2_release.py via a sys.path insert, so importing it here
requires nothing extra: exec_module() runs that same import, resolving against the file's own
directory exactly as it would when the workflow invokes `python3 scripts/release_stats.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release_stats.py"
_spec = importlib.util.spec_from_file_location("release_stats", _SCRIPT_PATH)
release_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_stats)


def _release(*, tag_name, assets, draft=False, prerelease=False):
    return {
        "tag_name": tag_name,
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
    }


def _asset(name, download_count):
    return {"name": name, "download_count": download_count}


class TestIsInstallerAsset:
    def test_dmg_is_an_installer(self):
        assert release_stats.is_installer_asset("PrivacyFence-4.1.0.dmg") is True

    def test_windows_setup_exe_is_an_installer(self):
        assert release_stats.is_installer_asset("PrivacyFence-4.1.0-setup.exe") is True

    def test_deb_is_an_installer(self):
        assert release_stats.is_installer_asset("privacyfence_4.1.0_amd64.deb") is True

    def test_sbom_files_are_excluded(self):
        assert release_stats.is_installer_asset("PrivacyFence-4.1.0.spdx.json") is False
        assert release_stats.is_installer_asset("PrivacyFence-4.1.0.cdx.json") is False

    def test_org_config_script_is_excluded(self):
        assert release_stats.is_installer_asset("build_org_bundle.py") is False

    def test_sdist_and_wheel_are_excluded(self):
        assert release_stats.is_installer_asset("privacyfence-4.1.0.tar.gz") is False
        assert release_stats.is_installer_asset("privacyfence-4.1.0-py3-none-any.whl") is False

    def test_checksum_file_is_excluded(self):
        assert release_stats.is_installer_asset("PrivacyFence-4.1.0.dmg.sha256") is False


class TestComputeStats:
    def test_sums_only_installer_downloads(self):
        repository = {"stargazers_count": 42}
        releases = [
            _release(
                tag_name="v4.1.0",
                assets=[
                    _asset("PrivacyFence-4.1.0.dmg", 10),
                    _asset("PrivacyFence-4.1.0-setup.exe", 7),
                    _asset("privacyfence_4.1.0_amd64.deb", 3),
                    _asset("PrivacyFence-4.1.0.spdx.json", 100),
                    _asset("PrivacyFence-4.1.0.cdx.json", 100),
                    _asset("privacyfence-4.1.0.tar.gz", 100),
                    _asset("privacyfence-4.1.0-py3-none-any.whl", 100),
                    _asset("build_org_bundle.py", 100),
                    _asset("sync_room_directory.py", 100),
                ],
            )
        ]

        stats = release_stats.compute_stats(repository, releases)

        assert stats["github_installer_downloads"] == 20
        assert stats["stars"] == 42
        assert stats["latest_release"] == "v4.1.0"

    def test_draft_releases_are_excluded(self):
        releases = [_release(tag_name="v4.2.0", assets=[_asset("PrivacyFence-4.2.0.dmg", 5)], draft=True)]

        stats = release_stats.compute_stats({"stargazers_count": 0}, releases)

        assert stats["github_installer_downloads"] == 0
        assert stats["latest_release"] is None

    def test_prerelease_counts_toward_downloads_but_not_latest(self):
        releases = [
            _release(tag_name="v4.2.0b1", assets=[_asset("PrivacyFence-4.2.0b1.dmg", 5)], prerelease=True),
            _release(tag_name="v4.1.0", assets=[_asset("PrivacyFence-4.1.0.dmg", 10)]),
        ]

        stats = release_stats.compute_stats({"stargazers_count": 0}, releases)

        # Pre-release downloads still count toward the installer-downloads KPI ...
        assert stats["github_installer_downloads"] == 15
        # ... but "latest_release" only ever names a real stable release, matching the "latest"
        # tag GitHub itself surfaces (and the order the GitHub API returns releases in: newest first).
        assert stats["latest_release"] == "v4.1.0"

    def test_no_published_releases_yields_no_latest(self):
        stats = release_stats.compute_stats({"stargazers_count": 5}, [])

        assert stats["github_installer_downloads"] == 0
        assert stats["stars"] == 5
        assert stats["latest_release"] is None

    def test_missing_download_count_defaults_to_zero(self):
        releases = [_release(tag_name="v4.1.0", assets=[{"name": "PrivacyFence-4.1.0.dmg"}])]

        stats = release_stats.compute_stats({"stargazers_count": 0}, releases)

        assert stats["github_installer_downloads"] == 0


class TestMain:
    def test_requires_a_token(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        exit_code = release_stats.main(["--repo", "privacyfence/privacyfence", "--output", str(tmp_path / "out.json")])

        assert exit_code == 2
        assert "GH_TOKEN" in capsys.readouterr().err

    def test_fetches_and_writes_output(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "test-token")
        output = tmp_path / "nested" / "release-stats.json"

        def fake_fetch_and_compute(repo, token):
            assert repo == "privacyfence/privacyfence"
            assert token == "test-token"
            return {"github_installer_downloads": 3, "stars": 1, "latest_release": "v4.1.0"}

        monkeypatch.setattr(release_stats, "fetch_and_compute", fake_fetch_and_compute)

        exit_code = release_stats.main(["--repo", "privacyfence/privacyfence", "--output", str(output)])

        assert exit_code == 0
        assert output.is_file()
        import json

        assert json.loads(output.read_text()) == {
            "github_installer_downloads": 3,
            "stars": 1,
            "latest_release": "v4.1.0",
        }
