#!/usr/bin/env bash
# Build PrivacyFence.dmg — the macOS disk image, and the only macOS artifact
# that ships.
#
# It is a carrier, not a drag-install image: it holds PrivacyFence.pkg (built
# here by scripts/build_pkg.sh, see step 7) and PrivacyFence.mcpb, and nothing
# else — no PrivacyFenceApp.app, no /Applications drop link. The user flow is
# "mount the DMG, double-click PrivacyFence.pkg, then double-click
# PrivacyFence.mcpb", both steps in the same window.
#
# That replaces an earlier layout where the DMG carried the .app plus an
# /Applications symlink and the .pkg shipped as a *separate* download beside
# it. Two problems, both fixed by this: dragging the .app installed a copy
# that had to ask for an administrator password later, at some unrelated
# moment, to provision privilege separation (#428 D1's runtime prompt), where
# the .pkg does it during the install the user is already answering a password
# for (#428 D2); and the .pkg's own conclusion screen told the user to open
# the .mcpb "next to this installer", which was simply untrue for a standalone
# .pkg download — there was no .mcpb next to it. One download, one install
# path, and the sentence is now true.
#
# Prerequisites (needed only on your build machine, not end-user machines):
#   pip install -e .        # PrivacyFence itself, so VERSION below can read
#                            # its installed metadata (git-tag-derived, see
#                            # this repo's CLAUDE.md "Releasing" section)
#   pip install pyinstaller
#   brew install create-dmg
#   brew install librsvg   # optional, only if you add SVG assets
#   node + npx on PATH (used by scripts/build_mcpb.sh)
#
# Usage:
#   ./scripts/build_dmg.sh [--sign "Developer ID Application: Your Name (TEAMID)"]
#
# --sign takes the "Developer ID Application" identity, which signs the .app.
# The .pkg inside the DMG needs a "Developer ID Installer" identity instead --
# a different certificate type Apple issues separately -- so that one is passed
# through the environment as SIGN_IDENTITY_INSTALLER and handed to
# scripts/build_pkg.sh at step 7. Leaving it unset just builds an unsigned
# .pkg inside an otherwise signed DMG, same as it always did when the .pkg was
# built as its own CI step.
#
# Output: dist/PrivacyFence-<version>.dmg (carrying dist/PrivacyFence-<version>.pkg
# and dist/PrivacyFence-<version>.mcpb, both of which stay in dist/ too -- they
# are inputs to this image, not separately released artifacts)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Find Python / PyInstaller — prefer the project venv, then PATH.
if [ -x ".venv/bin/pyinstaller" ]; then
  PYTHON=".venv/bin/python"
  PYINSTALLER=".venv/bin/pyinstaller"
elif command -v pyinstaller &>/dev/null; then
  PYTHON="$(command -v python3)"
  PYINSTALLER="$(command -v pyinstaller)"
else
  echo "PyInstaller not found — installing into .venv…"
  .venv/bin/pip install --quiet pyinstaller
  PYTHON=".venv/bin/python"
  PYINSTALLER=".venv/bin/pyinstaller"
fi

# Version comes from the git tag via setuptools_scm now (this repo's CLAUDE.md
# "Releasing" section) -- read back through the installed package's own
# metadata, same as PrivacyFenceApp.spec and src/privacyfence/__init__.py.
# Fails clearly if PrivacyFence itself hasn't been `pip install -e .`d yet.
VERSION=$("$PYTHON" -c "from importlib.metadata import version; print(version('privacyfence'))")
APP_NAME="PrivacyFenceApp"       # .app bundle / executable name (daemon)
PRODUCT_NAME="PrivacyFence"      # public-facing DMG volume / installer name
BUNDLE="dist/${APP_NAME}.app"
DMG_NAME="${PRODUCT_NAME}-${VERSION}.dmg"
DMG_PATH="dist/${DMG_NAME}"

SIGN_IDENTITY="${SIGN_IDENTITY:-}"
for arg in "$@"; do
  case "$arg" in
    --sign) SIGN_IDENTITY="${2:-}"; shift 2 ;;
  esac
done

echo "=== Building ${PRODUCT_NAME} ${VERSION} ==="

# ── 1. Convert PNG icon to ICNS (must happen before PyInstaller) ─────────────
ICON_SRC="src/privacyfence/resources/icon_512.png"
ICON_DIR="build/privacyfence_icons.iconset"
ICNS_PATH="build/privacyfence.icns"

if [ ! -f "$ICNS_PATH" ]; then
  echo "→ Converting icon to .icns…"
  mkdir -p "$ICON_DIR"
  sips -z 16 16     "$ICON_SRC" --out "${ICON_DIR}/icon_16x16.png"     >/dev/null
  sips -z 32 32     "$ICON_SRC" --out "${ICON_DIR}/icon_16x16@2x.png"  >/dev/null
  sips -z 32 32     "$ICON_SRC" --out "${ICON_DIR}/icon_32x32.png"     >/dev/null
  sips -z 64 64     "$ICON_SRC" --out "${ICON_DIR}/icon_32x32@2x.png"  >/dev/null
  sips -z 128 128   "$ICON_SRC" --out "${ICON_DIR}/icon_128x128.png"   >/dev/null
  sips -z 256 256   "$ICON_SRC" --out "${ICON_DIR}/icon_128x128@2x.png" >/dev/null
  sips -z 256 256   "$ICON_SRC" --out "${ICON_DIR}/icon_256x256.png"   >/dev/null
  sips -z 512 512   "$ICON_SRC" --out "${ICON_DIR}/icon_256x256@2x.png" >/dev/null
  cp "$ICON_SRC"                      "${ICON_DIR}/icon_512x512.png"
  iconutil -c icns "$ICON_DIR" -o "$ICNS_PATH"
fi

# ── 2. Bake in the Telegram app credentials ───────────────────────────────────
# api_id/api_hash identify the PrivacyFence app to Telegram (not an
# organization — see docs/telegram-setup.md), and this repo is public, so
# they're never committed: CI supplies them as TELEGRAM_API_ID/TELEGRAM_API_HASH
# secrets, written here into a git-ignored module PyInstaller then bundles.
# Local builds without the env vars set just ship without Telegram support.
# scripts/telegram_credentials.py is the one generator every build shares.
"$PYTHON" scripts/telegram_credentials.py write

# ── 3. Build .app bundle ──────────────────────────────────────────────────────
# --clean forces a fresh module analysis every time: PyInstaller otherwise
# caches "module not found" results in build/PrivacyFenceApp/, so a rebuild
# after _telegram_credentials.py first appears (step 2, above) can silently
# keep using a stale analysis from before the file existed and ship without
# Telegram credentials baked in.
echo "→ Running PyInstaller…"
PRIVACYFENCE_ICNS="$ICNS_PATH" $PYINSTALLER --noconfirm --clean PrivacyFenceApp.spec

# ── 4. Create privacyfence-app symlink inside the bundle ─────────────────────────
# The LaunchAgent plist and mcpb/shim/'s own daemon auto-start (daemon.ts's
# DEFAULT_APP_PATH) use this name; the main exe is "PrivacyFenceApp".
MACOS_DIR="${BUNDLE}/Contents/MacOS"
if [ ! -e "${MACOS_DIR}/privacyfence-app" ]; then
  echo "→ Creating privacyfence-app symlink…"
  ln -s "PrivacyFenceApp" "${MACOS_DIR}/privacyfence-app"
fi

# Apply the .icns to the .app bundle
if command -v fileicon &>/dev/null; then
  fileicon set "$BUNDLE" "$ICNS_PATH" 2>/dev/null || true
fi

# ── 4b. Bundle the privilege-separation script + its launchd templates ───────
# #428 D1 (4.1): the daemon's own auto-enable trigger (privilege_separation.py
# maybe_auto_enable_macos()) shells out to this script, elevated. Until now
# nothing shipped it into the DMG at all -- opting in required a source
# checkout, which is also why the auto trigger couldn't exist before this.
# scripts/macos_privilege_separation.sh resolves its own REPO_ROOT as
# "$(dirname "${BASH_SOURCE[0]}")/.." and reads templates from
# "${REPO_ROOT}/installer/macos" -- copying both directories into
# Contents/Resources/ with that same scripts/ + installer/macos/ sibling
# layout means the script needs no packaged-vs-checkout branch (unlike the
# .deb's linux_privilege_separation.sh, which has one because the .deb
# installs the script somewhere else entirely, as
# /usr/sbin/privacyfence-privilege-separation).
echo "→ Bundling the privilege-separation script…"
RESOURCES="${BUNDLE}/Contents/Resources"
mkdir -p "${RESOURCES}/scripts" "${RESOURCES}/installer/macos"
cp -p scripts/macos_privilege_separation.sh "${RESOURCES}/scripts/macos_privilege_separation.sh"
chmod +x "${RESOURCES}/scripts/macos_privilege_separation.sh"
cp -p installer/macos/com.privacyfence.daemon.plist.tmpl "${RESOURCES}/installer/macos/"
cp -p installer/macos/com.privacyfence.companion.plist.tmpl "${RESOURCES}/installer/macos/"

# ── 5. Optional code signing ──────────────────────────────────────────────────
if [ -n "$SIGN_IDENTITY" ]; then
  echo "→ Code-signing with: ${SIGN_IDENTITY}"
  codesign --deep --force --options runtime \
    --sign "$SIGN_IDENTITY" \
    --entitlements scripts/entitlements.plist \
    "$BUNDLE"
fi

# ── 6. Build the Claude Desktop extension (.mcpb) ─────────────────────────────
# One extension since P5 retired the bridge (decision D11): the /mcp shim
# (PrivacyFence.mcpb). Until P5 this step built a second, "Legacy Bridge"
# .mcpb alongside it as a rollback that needed no /mcp setup -- that lever
# isn't needed any more now that the bridge itself no longer exists.
echo "→ Building PrivacyFence's Claude Desktop extension…"
bash scripts/build_mcpb.sh
MCPB_SHIM_PATH="dist/${PRODUCT_NAME}-${VERSION}.mcpb"
MCPB_DMG_NAME="${PRODUCT_NAME}.mcpb"   # stable name inside the DMG (no version)

# ── 7. Build the installer package (.pkg) ─────────────────────────────────────
# The DMG's payload, not a sibling artifact -- see this script's own header.
# scripts/build_pkg.sh packages the .app built above (it never runs PyInstaller
# itself), and signs/notarizes with SIGN_IDENTITY_INSTALLER if it's set, which
# it reads from the environment rather than from a --sign argument here: this
# script's own SIGN_IDENTITY is the wrong certificate type for productsign and
# must not leak into it. MCPB_DMG_NAME tells its conclusion screen what the
# extension is actually called on the image it will be opened from.
echo "→ Building the installer package…"
MCPB_DMG_NAME="$MCPB_DMG_NAME" bash scripts/build_pkg.sh
PKG_PATH="dist/${PRODUCT_NAME}-${VERSION}.pkg"
PKG_DMG_NAME="${PRODUCT_NAME}.pkg"     # stable name inside the DMG (no version)

# ── 8. Package into DMG ───────────────────────────────────────────────────────
# Both files are staged into a source folder rather than passed as --add-file:
# create-dmg sizes the image from its source folder, so an *empty* source folder
# plus two --add-file arguments produces an image with no room to copy them into.
echo "→ Building DMG…"
rm -f "$DMG_PATH"
DMG_ROOT="build/dmg-root"
rm -rf "$DMG_ROOT"
mkdir -p "$DMG_ROOT"
cp -p "$PKG_PATH" "${DMG_ROOT}/${PKG_DMG_NAME}"
cp -p "$MCPB_SHIM_PATH" "${DMG_ROOT}/${MCPB_DMG_NAME}"

create-dmg \
  --volname "${PRODUCT_NAME}" \
  --volicon "$ICNS_PATH" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size 128 \
  --icon "${PKG_DMG_NAME}" 170 170 \
  --icon "${MCPB_DMG_NAME}" 430 170 \
  --no-internet-enable \
  "$DMG_PATH" \
  "$DMG_ROOT"

# ── 9. Optional notarization ──────────────────────────────────────────────────
# Set NOTARIZE_PROFILE to a name registered via `xcrun notarytool store-credentials`
# to submit the signed DMG to Apple and staple the resulting ticket. The .pkg
# inside it was already notarized and stapled on its own by step 7 -- stapling
# the image does not staple what it carries, and Gatekeeper checks the .pkg when
# the user double-clicks it out of the mounted image.
if [ -n "$SIGN_IDENTITY" ] && [ -n "${NOTARIZE_PROFILE:-}" ]; then
  echo "→ Submitting for notarization…"
  xcrun notarytool submit "$DMG_PATH" \
    --keychain-profile "$NOTARIZE_PROFILE" \
    --wait
  xcrun stapler staple "$DMG_PATH"
fi

echo ""
echo "✓ Done: ${DMG_PATH}"
echo "  Size: $(du -sh "${DMG_PATH}" | cut -f1)"
