"""Release-workflow structural smoke test for the built ``.pkg`` installer
(``scripts/build_pkg.sh``), which is what the shipped DMG carries and what a
macOS user actually double-clicks -- see ``scripts/build_dmg.sh``'s own header.

Deliberately the *lightweight* half of this artifact's coverage: it never
installs anything and needs no root, so it runs inline in ``build.yml``'s
release-critical ``build`` job, right after ``scripts/build_dmg.sh`` (which
builds the ``.pkg`` itself, via ``scripts/build_pkg.sh``) --
mirroring how ``test_macos_packaged_smoke.py`` (TST-15) stays a fast, no-
``launchctl`` check in that same job while the real install-and-launchd
verification (this artifact's own analogue is
``test_macos_pkg_install.py``) lives in the separate, weekly-scheduled
``macos-graphical-session.yml`` instead -- see that module's own docstring
for why, and ``docs/testing-policy.md``'s Layer 6 row for the general
principle ("a release's critical path must not depend on the heaviest
tier").

What this proves, all without installing anything:

1. ``pkgutil --expand-full`` unpacks cleanly -- a malformed distribution
   package (bad ``productbuild --distribution`` XML, a component pkgbuild
   never actually produced) fails right here instead of only surfacing once
   a human -- or ``test_macos_pkg_install.py`` -- tries to install it.
2. ``installer/macos/pkg/postinstall`` is actually inside the built package,
   at the one path ``installer(8)`` will run it from, and executable --
   the entire mechanism this artifact exists for silently does nothing if
   this file goes missing from a future ``scripts/build_pkg.sh`` refactor.
3. ``PrivacyFenceApp.app``'s frozen daemon binary is actually inside the
   package's payload, not just the wrapper -- the same "PyInstaller quietly
   dropped something" failure mode ``test_macos_packaged_smoke.py``'s own
   module docstring describes, here aimed at the packaging step instead of
   the freeze step.
4. The embedded ``Distribution`` XML's version matches the version
   ``scripts/build_pkg.sh`` actually resolved -- a stale value here would
   have the installer's own UI, and ``pkgutil --pkg-info`` afterward, lie
   about what got installed.
5. Signature: same optional-and-skip-if-unsigned posture as
   ``test_macos_packaged_smoke.py``'s own ``test_packaged_app_signature_
   and_notarization`` -- a local dev build with no ``--sign`` is legitimate
   and this must not fail over it, but ``build.yml``'s real release job
   always signs, so this asserts the real chain there.

Skipped entirely unless running on real macOS with a just-built
``dist/PrivacyFence-*.pkg`` on disk.
"""
from __future__ import annotations

import platform
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import defusedxml.ElementTree as ET
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "dist"


def _built_pkgs() -> list[Path]:
    return sorted(DIST_DIR.glob("PrivacyFence-*.pkg")) if DIST_DIR.is_dir() else []


def _version_from_pkg_name(pkg_path: Path) -> str:
    # "PrivacyFence-<version>.pkg" -- same naming scripts/build_pkg.sh gives
    # scripts/build_dmg.sh's own "PrivacyFence-<version>.dmg".
    return pkg_path.stem.removeprefix("PrivacyFence-")


pytestmark = [
    pytest.mark.skipif(platform.system() != "Darwin", reason="only meaningful against a real .pkg -- pkgutil is macOS-only"),
    pytest.mark.skipif(
        not _built_pkgs(),
        reason=(
            "no dist/PrivacyFence-*.pkg built yet -- this is the release-workflow smoke test "
            "build.yml's `build` job runs after scripts/build_dmg.sh; run that script locally "
            "first (it builds the .pkg too) to exercise this test outside CI"
        ),
    ),
    pytest.mark.skipif(shutil.which("pkgutil") is None, reason="pkgutil not on PATH"),
    pytest.mark.packaged,
    pytest.mark.timeout(60),
]


@pytest.fixture
def expanded_pkg() -> Path:
    pkg_path = _built_pkgs()[-1]
    dest = Path(tempfile.mkdtemp(prefix="pf-pkg-expand-")) / "expanded"
    result = subprocess.run(
        ["pkgutil", "--expand-full", str(pkg_path), str(dest)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"pkgutil --expand-full {pkg_path} failed:\n{result.stdout}{result.stderr}"
    return dest


def test_pkg_distribution_and_version(expanded_pkg):
    pkg_path = _built_pkgs()[-1]
    expected_version = _version_from_pkg_name(pkg_path)

    distribution = expanded_pkg / "Distribution"
    assert distribution.is_file(), f"no Distribution manifest in expanded {pkg_path.name}"
    xml = distribution.read_text(encoding="utf-8")
    assert "<title>PrivacyFence</title>" in xml, f"unexpected installer title:\n{xml}"
    assert f'version="{expected_version}"' in xml, (
        f"Distribution's pkg-ref version doesn't match the artifact's own filename "
        f"({expected_version}):\n{xml}"
    )
    # #428 D2: a per-user "just for me" install could never provision a
    # system account -- scripts/build_pkg.sh's own distribution.xml forces
    # the system domain instead of merely defaulting to it.
    assert 'enable_localSystem="true"' in xml
    assert 'enable_anywhere="false"' in xml
    assert 'enable_currentUserHome="false"' in xml


def test_pkg_contains_postinstall_and_app_payload(expanded_pkg):
    pkg_path = _built_pkgs()[-1]

    postinstall_candidates = list(expanded_pkg.glob("**/Scripts/postinstall"))
    assert postinstall_candidates, f"no Scripts/postinstall found anywhere under expanded {pkg_path.name}"
    for postinstall in postinstall_candidates:
        assert postinstall.stat().st_mode & 0o111, f"{postinstall} is not executable"
        contents = postinstall.read_text(encoding="utf-8")
        assert "macos_privilege_separation.sh" in contents, (
            f"{postinstall} doesn't call macos_privilege_separation.sh -- wrong script bundled?"
        )
        assert "enable --auto" in contents, f"{postinstall} doesn't auto-enable privilege separation: {contents}"

    app_bundles = list(expanded_pkg.glob("**/Payload/PrivacyFenceApp.app"))
    assert app_bundles, f"no PrivacyFenceApp.app found in any component's Payload under expanded {pkg_path.name}"
    app_bundle = app_bundles[0]
    daemon_binary = app_bundle / "Contents" / "MacOS" / "PrivacyFenceApp"
    assert daemon_binary.is_file(), f"{daemon_binary} missing from the package payload"

    # ADR 0031: a double-click in /Applications runs the launcher (open
    # Approvals through the companion), not the daemon, which launchd starts
    # by its own explicit path and which refuses to run as the clicking user.
    with open(app_bundle / "Contents" / "Info.plist", "rb") as f:
        info = plistlib.load(f)
    assert info["CFBundleExecutable"] == "PrivacyFence", info.get("CFBundleExecutable")
    launcher_binary = app_bundle / "Contents" / "MacOS" / "PrivacyFence"
    assert launcher_binary.is_file(), f"{launcher_binary} missing from the package payload"

    separation_script = app_bundle / "Contents" / "Resources" / "scripts" / "macos_privilege_separation.sh"
    assert separation_script.is_file(), (
        f"{separation_script} missing from the package payload -- the bundled postinstall script "
        f"calls exactly this path, at install time, as root"
    )


def test_pkg_payload_is_not_relocatable(expanded_pkg):
    """The package must install where it says it installs.

    ``pkgbuild`` marks every bundle in a payload relocatable unless told
    otherwise, which means installd asks Launch Services where a bundle with
    this identifier already lives and, if it finds one, installs over *that*
    copy instead of ``--install-location``. A user who has ever launched
    PrivacyFenceApp.app from a Downloads folder, a still-mounted DMG or a
    build tree therefore gets the whole install silently redirected there --
    with ``installer`` reporting success either way.

    The install landing in the wrong place is only half of it. The bundled
    ``postinstall`` resolves ``macos_privilege_separation.sh`` under the
    literal ``/Applications/PrivacyFenceApp.app`` path, so a relocated install
    also skips provisioning privilege separation entirely ("leaving privilege
    separation opt-in") -- and ADR 0003 decision 6 refuses to serve the
    unseparated install that leaves behind. Both halves shipped, and were
    mistaken for a flaky test for weeks (#562), because the symptom depends on
    whether Launch Services happens to know about another copy yet.

    ``scripts/build_pkg.sh`` turns this off via ``--component-plist`` with
    ``BundleIsRelocatable`` false. That does not make ``pkgbuild`` omit the
    ``<relocate>`` element itself -- at least not with every ``pkgbuild``
    build seen in CI, which still emits a self-closing ``<relocate/>`` as
    boilerplate regardless. What it does do, and what actually matters, is
    keep that element childless: a relocatable bundle shows up as a
    ``<bundle .../>`` *inside* ``<relocate>``, and that's what tells installd
    to go ask Launch Services. So this checks for an actual listed bundle,
    not for the tag's mere presence -- checking the tag itself flags a
    correctly-built package as broken the moment a newer Xcode/macOS
    ``pkgbuild`` starts including the empty placeholder.
    """
    pkg_path = _built_pkgs()[-1]
    package_infos = list(expanded_pkg.glob("**/PackageInfo"))
    assert package_infos, f"no PackageInfo found under expanded {pkg_path.name}"
    for package_info in package_infos:
        xml = package_info.read_text(encoding="utf-8")
        root = ET.fromstring(xml)
        relocate = root.find("relocate")
        relocatable_bundles = relocate.findall("bundle") if relocate is not None else []
        assert not relocatable_bundles, (
            f"{package_info} still lists relocatable bundles -- this package will install over "
            f"whatever copy of the bundle Launch Services already knows about rather than into "
            f"/Applications, and its postinstall will not find the app where it looks for it:\n{xml}"
        )


def test_pkg_signature(expanded_pkg):
    """Same optional-and-skip posture as ``test_macos_packaged_smoke.py``'s
    own ``test_packaged_app_signature_and_notarization`` -- see this
    module's own docstring §5."""
    pkg_path = _built_pkgs()[-1]
    check = subprocess.run(
        ["pkgutil", "--check-signature", str(pkg_path)],
        capture_output=True, text=True, timeout=30,
    )
    output = check.stdout + check.stderr
    if "Status: no signature" in output or check.returncode != 0:
        pytest.skip(
            f"{pkg_path.name} is unsigned for this build (SIGN_IDENTITY_INSTALLER unset) -- "
            f"build with SIGN_IDENTITY_INSTALLER='<Developer ID Installer identity>' set to "
            f"exercise this test: {output}"
        )
    assert "Developer ID Installer" in output, (
        f"expected a real Developer ID Installer signature, not some other identity type:\n{output}"
    )
    assert "Status: signed" in output, f"unexpected pkgutil --check-signature output:\n{output}"
