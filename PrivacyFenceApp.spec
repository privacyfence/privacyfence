# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PrivacyFenceApp.app (the daemon)
#
# Produces:
#   dist/PrivacyFenceApp.app/
#     Contents/MacOS/PrivacyFenceApp       ← daemon (main app; headless background
#                                             process, reachable only over its own
#                                             embedded web approval/settings UI --
#                                             P10 retired the native menu bar/dialogs)
#     Contents/MacOS/privacyfence-app      ← symlink → PrivacyFenceApp (for daemon auto-start)
#     Contents/MacOS/PrivacyFenceCompanion ← menu-bar companion (ADR 0002)
#     Contents/MacOS/PrivacyFence          ← launcher: the bundle's CFBundleExecutable, i.e.
#                                             what a double-click in /Applications runs --
#                                             opens Approvals, starting the companion if
#                                             needed (ADR 0031). launchd starts the daemon
#                                             and the companion by their own explicit paths,
#                                             never through the bundle.
#
# Claude's MCP entry point is the daemon's own /mcp Streamable HTTP endpoint
# (web/server.py); the stdio<->/mcp shim Claude Desktop actually spawns is
# built separately — a small Node/TypeScript proxy, see mcpb/shim/ and
# scripts/build_mcpb.sh — and distributed as a one-click Claude Desktop
# extension (.mcpb) instead of living inside this app.
#
# Build:
#   pip install pyinstaller
#   pyinstaller PrivacyFenceApp.spec
#
# Notes:
#   - Run on the target architecture. For Apple Silicon: arch -arm64 pyinstaller ...
#   - Code-signing and notarization are handled by build_dmg.sh.

import os
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)
sys.path.insert(0, str(Path("scripts").resolve()))

# Shared with PrivacyFenceApp.linux.spec and PrivacyFenceApp.win.spec -- see that module's
# docstring for why the list itself lives there instead of being hand-copied into every spec.
from pyinstaller_common import DATAS, HIDDEN_IMPORTS

# Version comes from the git tag via setuptools_scm now, not a hardcoded
# string here (see this repo's CLAUDE.md "Releasing" section) -- read back
# through the *installed* privacyfence package's own metadata (build_dmg.sh
# and CI both `pip install -e .` before running PyInstaller), the same way
# src/privacyfence/__init__.py itself resolves __version__ at runtime.
VERSION = _pkg_version("privacyfence")

# Use .icns built by build_dmg.sh; fall back to PNG (will error on macOS, but
# lets you run pyinstaller directly for quick dev iteration on Linux/CI).
ICON = os.environ.get("PRIVACYFENCE_ICNS", "src/privacyfence/resources/icon_512.png")

# ── data files / hidden imports ───────────────────────────────────────────────
# Shared with every other platform's spec via scripts/pyinstaller_common.py (imported above).

datas = DATAS
hidden_imports = HIDDEN_IMPORTS

# ── daemon (main .app entry point) ────────────────────────────────────────────

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
    strip=False,
    upx=True,
    console=False,      # no terminal window
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

# ── companion (#428 Phase 3, ADR 0002) ────────────────────────────────────────
# A second entry point *inside this same .app bundle* -- Contents/MacOS/PrivacyFenceCompanion,
# alongside Contents/MacOS/PrivacyFenceApp -- not a second .app, second signature, or second
# notarization path (ADR 0002 decision 4). The extra hidden imports below are pystray's macOS
# (AppKit/NSStatusItem) backend and PyObjC's own dynamic-lookup surface, neither of which the
# daemon's own Analysis above needs or declares -- best-effort, since this spec can only run (and
# only be verified) on an actual macOS build host, never in this repo's own Linux-hosted CI.

companion_a = Analysis(
    ["src/_companion_entry.py"],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports + ["pystray._darwin", "PIL.Image", "objc", "Foundation", "AppKit"],
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
    strip=False,
    upx=True,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ── launcher (ADR 0031) ───────────────────────────────────────────────────────
# The bundle's main executable. Before ADR 0031 that was the daemon, so a
# double-click in /Applications started a second daemon as the logged-in user,
# which a separated install refuses (privilege_separation.check_runtime_identity)
# -- invisibly, with LSUIElement set and no console. This is companion.py's
# --launch instead, and needs exactly the companion's imports (pystray/AppKit
# included: with no companion running, it becomes the menu-bar companion).

launcher_a = Analysis(
    ["src/_launcher_entry.py"],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports + ["pystray._darwin", "PIL.Image", "objc", "Foundation", "AppKit"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

launcher_pyz = PYZ(launcher_a.pure)

launcher_exe = EXE(
    launcher_pyz,
    launcher_a.scripts,
    [],
    exclude_binaries=True,
    name="PrivacyFence",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

# ── bundle into .app ──────────────────────────────────────────────────────────

coll = COLLECT(
    daemon_exe,
    daemon_a.binaries,
    daemon_a.datas,
    companion_exe,
    companion_a.binaries,
    companion_a.datas,
    launcher_exe,
    launcher_a.binaries,
    launcher_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PrivacyFenceApp",
)

app = BUNDLE(
    coll,
    name="PrivacyFenceApp.app",
    icon=ICON,
    bundle_identifier="com.privacyfence.app",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "PrivacyFence",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": "1",
        # Explicit, not PyInstaller's default (the first EXE collected, the daemon): a
        # double-click opens Approvals through the companion (ADR 0031).
        "CFBundleExecutable": "PrivacyFence",
        "LSUIElement": True,          # no Dock icon; the companion's menu-bar item is the only UI
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        # Allow outbound network connections for OAuth + API calls
        "com.apple.security.network.client": True,
    },
)
