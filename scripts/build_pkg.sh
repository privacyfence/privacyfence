#!/usr/bin/env bash
# Build PrivacyFence.pkg — a signed macOS installer package that provisions
# privilege separation (#428 D2) automatically at install time,
# instead of leaving that to the daemon's own admin-password runtime prompt
# (privilege_separation.maybe_auto_enable_macos()) the drag-install DMG path
# still depends on. A .pkg install already runs as root and already asks for
# an administrator password as part of the normal "Install PrivacyFence"
# step non-technical users already expect — so the one unavoidable
# elevation macOS requires for this (creating a system account and a
# LaunchDaemon) happens there, once, with PrivacyFence's own explanatory
# text (installer/macos/pkg/resources/*.html), instead of an unexplained
# dialog appearing later at some unrelated moment. See ADR 0002 and this
# repo's CLAUDE.md for the DMG's own account of why that runtime prompt
# exists at all. The DMG remains the primary distributable; this is an
# additional artifact for anyone who wants the fully-automated install.
#
# A pkg-installed .app lands root:wheel-owned by pkgbuild's own default
# ownership -- but /Applications itself is always root:admin, so that alone
# doesn't satisfy macos_privilege_separation.sh's B1 trusted-image check
# (require_trusted_image() walks every ancestor directory, /Applications
# included). `enable` -- run by this package's own postinstall script --
# closes that itself: it copies the image into a root:wheel-owned location
# of its own (TRUSTED_IMAGE_DIR) before trusting anything, regardless of
# where --app pointed. See that function's own comment for the full story
# (#428 D2's own B1 follow-up).
#
# Packages the *already-built* dist/PrivacyFenceApp.app -- this script never
# runs PyInstaller itself, so run scripts/build_dmg.sh first (its DMG and
# this script's .pkg then ship byte-identical app code from the same dist/).
#
# Prerequisites (build machine only):
#   dist/PrivacyFenceApp.app already built -- run scripts/build_dmg.sh first
#   pip install -e .   # so VERSION below can read installed metadata
#
# Usage:
#   ./scripts/build_pkg.sh [--sign "Developer ID Installer: Your Name (TEAMID)"]
#
# Note the certificate *type*: pkg signing needs a "Developer ID Installer"
# identity, not the "Developer ID Application" one scripts/build_dmg.sh's
# own --sign signs the .app/.dmg with -- Apple issues them separately, and
# productsign (not codesign) is what actually consumes this one.
#
# Output: dist/PrivacyFence-<version>.pkg
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

# Same git-tag-derived version as scripts/build_dmg.sh -- see this repo's
# CLAUDE.md "Releasing" section.
VERSION=$("$PYTHON" -c "from importlib.metadata import version; print(version('privacyfence'))")
APP_NAME="PrivacyFenceApp"
PRODUCT_NAME="PrivacyFence"
BUNDLE="dist/${APP_NAME}.app"
PKG_ID="com.privacyfence.installer"
BUILD_DIR="build/pkg"
PKG_ROOT="${BUILD_DIR}/root"
SCRIPTS_DIR="${BUILD_DIR}/scripts"
RESOURCES_DIR="${BUILD_DIR}/resources"
COMPONENT_PKG="${BUILD_DIR}/${PRODUCT_NAME}-${VERSION}-component.pkg"
DIST_XML="${BUILD_DIR}/distribution.xml"
PKG_NAME="${PRODUCT_NAME}-${VERSION}.pkg"
PKG_PATH="dist/${PKG_NAME}"

[ -d "$BUNDLE" ] || {
  echo "error: ${BUNDLE} not found -- run scripts/build_dmg.sh first (this script packages its output, it doesn't build it)" >&2
  exit 1
}

SIGN_IDENTITY="${SIGN_IDENTITY:-}"
for arg in "$@"; do
  case "$arg" in
    --sign) SIGN_IDENTITY="${2:-}"; shift 2 ;;
  esac
done

echo "=== Building ${PRODUCT_NAME} ${VERSION} installer package ==="

# ── 1. Stage a clean package root ─────────────────────────────────────────
# Not `--root dist` directly: dist/ also holds the .dmg and .mcpb this same
# build produces, neither of which belongs under /Applications. ditto (not
# cp -R) to preserve the .app bundle's extended attributes and, if it was
# signed, its code signature intact.
echo "→ Staging package root…"
rm -rf "$BUILD_DIR"
mkdir -p "$PKG_ROOT" "$SCRIPTS_DIR" "$RESOURCES_DIR"
ditto "$BUNDLE" "${PKG_ROOT}/${APP_NAME}.app"

cp -p installer/macos/pkg/postinstall "${SCRIPTS_DIR}/postinstall"
chmod +x "${SCRIPTS_DIR}/postinstall"

cp -p installer/macos/pkg/resources/welcome.html "${RESOURCES_DIR}/welcome.html"
MCPB_NAME="${PRODUCT_NAME}-${VERSION}.mcpb"
sed -e "s|__MCPB_NAME__|${MCPB_NAME}|g" \
  installer/macos/pkg/resources/conclusion.html.tmpl > "${RESOURCES_DIR}/conclusion.html"

# ── 2. Build the component package ────────────────────────────────────────
# Default --ownership recommended (no flag needed) leaves the payload
# root:wheel -- see this script's own header comment for why that matters.
echo "→ Building component package…"
pkgbuild \
  --root "$PKG_ROOT" \
  --identifier "$PKG_ID" \
  --version "$VERSION" \
  --install-location "/Applications" \
  --scripts "$SCRIPTS_DIR" \
  "$COMPONENT_PKG"

# ── 3. Wrap in a product archive ──────────────────────────────────────────
# A plain component package (step 2) is installable as-is, but productbuild
# is what adds the welcome/conclusion pages (staged above) and forces a
# system-domain (root) install rather than letting Installer.app offer a
# "just for me" choice that could never provision a system account anyway.
echo "→ Building product installer…"
cat > "$DIST_XML" <<XML
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
    <title>${PRODUCT_NAME}</title>
    <organization>${PKG_ID}</organization>
    <domains enable_anywhere="false" enable_currentUserHome="false" enable_localSystem="true"/>
    <options customize="never" require-scripts="true" rootVolumeOnly="true"/>
    <welcome file="welcome.html" mime-type="text/html"/>
    <conclusion file="conclusion.html" mime-type="text/html"/>
    <choices-outline>
        <line choice="default">
            <line choice="${PKG_ID}"/>
        </line>
    </choices-outline>
    <choice id="default"/>
    <choice id="${PKG_ID}" visible="false">
        <pkg-ref id="${PKG_ID}"/>
    </choice>
    <pkg-ref id="${PKG_ID}" version="${VERSION}" onConclusion="none">$(basename "$COMPONENT_PKG")</pkg-ref>
</installer-gui-script>
XML

rm -f "$PKG_PATH"
productbuild \
  --distribution "$DIST_XML" \
  --resources "$RESOURCES_DIR" \
  --package-path "$BUILD_DIR" \
  "$PKG_PATH"

# ── 4. Optional signing ────────────────────────────────────────────────────
# productsign, not codesign -- see this script's own header comment on why
# the certificate type differs from scripts/build_dmg.sh's --sign.
if [ -n "$SIGN_IDENTITY" ]; then
  echo "→ Signing installer with: ${SIGN_IDENTITY}"
  UNSIGNED_PKG="${BUILD_DIR}/${PKG_NAME}-unsigned"
  mv "$PKG_PATH" "$UNSIGNED_PKG"
  productsign --sign "$SIGN_IDENTITY" "$UNSIGNED_PKG" "$PKG_PATH"
fi

# ── 5. Optional notarization ──────────────────────────────────────────────
# Same NOTARIZE_PROFILE (registered via `xcrun notarytool store-credentials`)
# scripts/build_dmg.sh's own step 8 uses -- a .pkg notarizes and staples the
# same way a .dmg does.
if [ -n "$SIGN_IDENTITY" ] && [ -n "${NOTARIZE_PROFILE:-}" ]; then
  echo "→ Submitting installer for notarization…"
  xcrun notarytool submit "$PKG_PATH" \
    --keychain-profile "$NOTARIZE_PROFILE" \
    --wait
  xcrun stapler staple "$PKG_PATH"
fi

echo ""
echo "✓ Done: ${PKG_PATH}"
echo "  Size: $(du -sh "${PKG_PATH}" | cut -f1)"
