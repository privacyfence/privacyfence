# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PrivacyFenceApp (Linux onedir build) -- the Linux equivalent of
# PrivacyFenceApp.spec's macOS .app, packaged into a .deb by scripts/build_deb.sh (see ADR 0018
# for why the .deb is a self-contained PyInstaller bundle).
#
# Produces:
#   dist/PrivacyFenceApp/
#     PrivacyFenceApp        ← daemon (main entry point; headless background process,
#                                reachable only over its own embedded web approval/settings UI --
#                                there is no native menu bar or dialog UI)
#     privacyfence-app       ← symlink → PrivacyFenceApp (for daemon auto-start; the mcpb shim's
#                                findDaemonCmd() and the .deb's autostart entry both look for
#                                this name specifically)
#     <bundled libs>
#
# Same Analysis/PYZ/EXE/COLLECT structure as PrivacyFenceApp.spec, and the same datas/
# hidden_imports list (scripts/pyinstaller_common.py -- kept in one place so the two specs can't
# drift). What's deliberately *not* here, versus the macOS spec:
#   - No BUNDLE() step -- that's macOS .app bundling only; a Linux onedir output is the final
#     artifact as-is, no further PyInstaller-level wrapping.
#   - No .icns / entitlements / codesign arguments -- Linux's EXE() doesn't embed an exe icon the
#     way Windows/.app builds do; the existing PNG icons in src/privacyfence/resources/ are used
#     as-is by the app-menu entry (resources/linux/privacyfence-companion.desktop) instead, no conversion step needed (contrast Windows, which does need a generated .ico).
#
# Claude's MCP entry point is the daemon's own /mcp Streamable HTTP endpoint (web/server.py); the
# stdio<->/mcp shim Claude Desktop actually spawns is built separately -- see mcpb/shim/ and
# scripts/build_mcpb.sh -- and isn't part of this build.
#
# Build:
#   pip install pyinstaller
#   pyinstaller PrivacyFenceApp.linux.spec
#
# Notes:
#   - Run on the target architecture -- PyInstaller doesn't cross-compile, so an arm64 build
#     needs an arm64 build host (ADR 0044: the .deb declares only the architectures CI builds).
#   - `.deb` packaging (debian/ metadata, dpkg-deb, lintian) is handled by scripts/build_deb.sh,
#     not this spec -- this spec's only job is producing the onedir bundle.

import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)
sys.path.insert(0, str(Path("scripts").resolve()))

# Shared with PrivacyFenceApp.spec -- see scripts/pyinstaller_common.py's docstring for why the
# list itself lives there instead of being hand-copied into both specs.
from pyinstaller_common import DATAS, HIDDEN_IMPORTS

# Version comes from the git tag via setuptools_scm now, not a hardcoded string here (see this
# repo's CLAUDE.md "Releasing" section) -- read back through the *installed* privacyfence
# package's own metadata (scripts/build_deb.sh and CI both `pip install -e .` before running
# PyInstaller), same as PrivacyFenceApp.spec and src/privacyfence/__init__.py itself. Not used
# directly in this spec (no BUNDLE()/plist to stamp a version into, unlike macOS) -- read anyway
# so a missing `pip install -e .` fails loudly here too, before the Analysis step, rather than
# only inside scripts/build_deb.sh's own version-string handling.
VERSION = _pkg_version("privacyfence")

datas = DATAS
hidden_imports = HIDDEN_IMPORTS

# ── daemon (main entry point) ─────────────────────────────────────────────────

daemon_a = Analysis(
    ["src/_daemon_entry.py"],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

daemon_pyz = PYZ(daemon_a.pure)

daemon_exe = EXE(
    daemon_pyz,
    daemon_a.scripts,
    [],
    exclude_binaries=True,
    name="PrivacyFenceApp",
    debug=False,
    bootloader_ignore_signals=False,
    # Unlike PrivacyFenceApp.spec's strip=False (kept that way there to stay safe for macOS
    # codesigning), Linux binaries built here get stripped -- there's no signing step to worry
    # about, and lintian's `unstripped-binary-or-object` check (scripts/build_deb.sh's lint gate)
    # treats leaving debug symbols in as an error for a shipped .deb. The frozen app still runs
    # stripped: build.yml's packaged .deb lifecycle test starts it.
    strip=True,
    upx=True,
    console=False,      # no terminal window
    target_arch=None,
)

# ── companion (ADR 0002) ────────────────────────────────────────────────
# A second entry point of this same bundle, not a new build/signing path (ADR 0002 decision 4)
# -- built from the same DATAS/HIDDEN_IMPORTS as the daemon above (pystray/Pillow aren't in
# either list: Linux never imports them -- see companion.py's own module docstring -- so nothing
# platform-specific needs adding here the way the macOS spec's companion block does).

companion_a = Analysis(
    ["src/_companion_entry.py"],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

companion_pyz = PYZ(companion_a.pure)

companion_exe = EXE(
    companion_pyz,
    companion_a.scripts,
    [],
    exclude_binaries=True,
    name="PrivacyFenceCompanion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    console=False,
    target_arch=None,
)

# ── collect into onedir output ────────────────────────────────────────────────
# No BUNDLE() step here (macOS-only .app bundling) -- dist/PrivacyFenceApp/ *is* the shippable
# artifact; scripts/build_deb.sh stages it straight into the .deb under /opt/privacyfence.

coll = COLLECT(
    daemon_exe,
    daemon_a.binaries,
    daemon_a.datas,
    companion_exe,
    companion_a.binaries,
    companion_a.datas,
    strip=True,
    upx=True,
    upx_exclude=[],
    name="PrivacyFenceApp",
)
