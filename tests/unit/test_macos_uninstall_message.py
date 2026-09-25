"""The macOS ``uninstall`` success message, run under bash on any host.

On a ``.pkg`` install ``uninstall`` deletes the app bundle the script runs
from, so the purge hint cannot repeat ``$0``. The block that prints the hint
is cut out of the script and run on its own: the rest of ``uninstall`` needs
macOS and root, the message only needs bash.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "macos_privilege_separation.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _success_message_block() -> str:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index('  if [ "$PURGE" != "1" ]; then\n')
    end = script.index("    return 0\n  fi\n", start) + len("    return 0\n  fi\n")
    return script[start:end]


def _run(script_path: str) -> str:
    code = (
        'SYSTEM_ROOT="/Library/Application Support/PrivacyFence"\n'
        "SERVICE_ACCOUNT=_privacyfence\n"
        'DEFAULT_APP="/Applications/PrivacyFenceApp.app"\n'
        "PURGE=0\n"
        f"message() {{\n{_success_message_block()}}}\n"
        "message\n"
    )
    result = subprocess.run(["bash", "-c", code, script_path], capture_output=True, text=True, check=True)
    return result.stdout


def test_hint_repeats_the_script_when_it_still_exists(tmp_path):
    script = tmp_path / "macos_privilege_separation.sh"
    script.write_text("", encoding="utf-8")

    out = _run(str(script))

    assert f"sudo {script} uninstall --purge" in out
    assert "install PrivacyFence again" not in out


def test_hint_says_reinstall_first_when_the_bundle_took_the_script_with_it(tmp_path):
    gone = tmp_path / "PrivacyFenceApp.app" / "Contents" / "Resources" / "scripts" / "x.sh"

    out = _run(str(gone))

    assert str(gone) not in out
    assert "install PrivacyFence again, then run" in out
    assert (
        "sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/"
        "macos_privilege_separation.sh uninstall --purge"
    ) in out
