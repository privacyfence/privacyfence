"""Tests for scripts/macos_sign_retry.sh's ``sign_with_timestamp_retry``.

The invariant that matters most: only a transient timestamp-server/network failure is retried. A
real signing failure (bad identity, entitlement, invalid binary) must fail on its first attempt
with the signing command's own exit status, so a broken build stays exactly as red and as fast as
it was before the retry existed. build.yml run 36060781689 is why the retry exists at all:
``codesign`` failed the macOS job with "The timestamp service is not available."

The helper is sourced into a real bash with a fake signing command that fails a scripted number of
times, so these tests exercise the same code path build_dmg.sh/build_pkg.sh run, minus macOS.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "macos_sign_retry.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="sources a bash helper used only by the macOS build scripts",
)

_TIMESTAMP_ERROR = (
    "dist/PrivacyFenceApp.app: The timestamp service is not available.\n"
    "In subcomponent: dist/PrivacyFenceApp.app/Contents/MacOS/PrivacyFenceCompanion"
)


def _run(tmp_path: Path, failures: list[tuple[int, str]], delays: str = "0 0 0"):
    """Run the helper around a fake signer that fails once per entry in ``failures``.

    Each entry is (exit status, stderr text) for one failing attempt; after they're used up the
    fake signer succeeds. Returns the completed process and how many times the signer ran.
    """
    counter = tmp_path / "attempts"
    counter.write_text("0")
    cases = "\n".join(
        f"    {i + 1}) printf '%s\\n' {_sh_quote(text)} >&2; exit {status} ;;"
        for i, (status, text) in enumerate(failures)
    )
    signer = tmp_path / "fake_sign.sh"
    signer.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(( $(cat "{counter}") + 1 )); echo "$n" > "{counter}"\n'
        'case "$n" in\n'
        f"{cases}\n"
        '    *) echo "signed $1" ;;\n'
        "esac\n"
    )
    signer.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        f'source "{_HELPER}"\n'
        f'sign_with_timestamp_retry dist/Thing.app "{signer}" dist/Thing.app\n'
        'echo "after-sign"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "SIGN_RETRY_DELAYS": delays},
        check=False,
    )
    return proc, int(counter.read_text())


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


class TestSucceedsWithoutRetry:
    def test_first_attempt_success_runs_once_and_replays_output(self, tmp_path):
        proc, attempts = _run(tmp_path, [])
        assert proc.returncode == 0
        assert attempts == 1
        assert "signed dist/Thing.app" in proc.stdout
        assert "after-sign" in proc.stdout
        assert "attempt" not in proc.stderr


class TestRetriesTransientFailures:
    def test_timestamp_outage_is_retried_until_it_clears(self, tmp_path):
        """The exact failure from build.yml run 36060781689, twice, then success."""
        proc, attempts = _run(tmp_path, [(1, _TIMESTAMP_ERROR), (1, _TIMESTAMP_ERROR)])
        assert proc.returncode == 0
        assert attempts == 3
        assert "after-sign" in proc.stdout

    def test_each_retry_logs_one_line_naming_file_attempt_and_error(self, tmp_path):
        proc, _ = _run(tmp_path, [(1, _TIMESTAMP_ERROR)])
        retry_lines = [line for line in proc.stderr.splitlines() if "retrying in" in line]
        assert len(retry_lines) == 1
        line = retry_lines[0]
        assert "dist/Thing.app" in line
        assert "attempt 1/4" in line
        assert "The timestamp service is not available." in line

    def test_failed_attempts_output_is_still_in_the_log(self, tmp_path):
        proc, _ = _run(tmp_path, [(1, _TIMESTAMP_ERROR)])
        assert "In subcomponent:" in proc.stdout

    @pytest.mark.parametrize(
        "text",
        [
            "The request timed out.",
            "The network connection was lost.",
            "A server with the specified hostname could not be found.",
            "The Internet connection appears to be offline.",
            "Could not connect to the server.",
        ],
    )
    def test_network_errors_under_the_timestamp_fetch_are_retried(self, tmp_path, text):
        proc, attempts = _run(tmp_path, [(1, text)])
        assert proc.returncode == 0
        assert attempts == 2

    def test_gives_up_after_the_last_delay_with_the_signers_exit_status(self, tmp_path):
        proc, attempts = _run(tmp_path, [(3, _TIMESTAMP_ERROR)] * 5)
        assert proc.returncode == 3
        assert attempts == 4
        assert "after-sign" not in proc.stdout
        assert len([line for line in proc.stderr.splitlines() if "retrying in" in line]) == 3

    def test_attempt_count_follows_the_configured_delays(self, tmp_path):
        proc, attempts = _run(tmp_path, [(1, _TIMESTAMP_ERROR)] * 5, delays="0")
        assert proc.returncode == 1
        assert attempts == 2


class TestNeverRetriesRealSigningFailures:
    @pytest.mark.parametrize(
        "text",
        [
            "Developer ID Application: Nobody (XXXXXXXXXX): no identity found",
            "dist/Thing.app: invalid entitlements blob",
            "dist/Thing.app: code object is not signed at all",
            "dist/Thing.app: errSecInternalComponent",
            "productsign: error: Could not find appropriate signing identity",
        ],
    )
    def test_fails_on_first_attempt_with_signers_exit_status(self, tmp_path, text):
        proc, attempts = _run(tmp_path, [(1, text)] * 4)
        assert proc.returncode == 1
        assert attempts == 1
        assert text in proc.stdout
        assert "retrying" not in proc.stderr
        assert "after-sign" not in proc.stdout


def test_build_scripts_route_every_signing_call_through_the_helper():
    """codesign/productsign must not be called bare again, or the next outage is red again."""
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    dmg = (scripts / "build_dmg.sh").read_text(encoding="utf-8")
    pkg = (scripts / "build_pkg.sh").read_text(encoding="utf-8")
    assert 'sign_with_timestamp_retry "$BUNDLE" \\\n    codesign ' in dmg
    assert 'sign_with_timestamp_retry "$PKG_PATH" productsign_fresh' in pkg
    for text in (dmg, pkg):
        signing_lines = [
            line.strip() for line in text.splitlines() if line.strip().startswith(("codesign ", "productsign "))
        ]
        assert len(signing_lines) == 1
