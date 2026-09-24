#!/usr/bin/env bash
# Build PrivacyFence.pkg -- the signed macOS installer package that *is* the
# macOS install. It provisions privilege separation (#428 D2) automatically at
# install time, instead of leaving that to the daemon's own admin-password
# runtime prompt (privilege_separation.maybe_auto_enable_macos(), #428 D1),
# which now only ever fires for an install that bypassed this package. A .pkg
# install already runs as root and already asks for an administrator password
# as part of the normal "Install PrivacyFence" step non-technical users already
# expect -- so the one unavoidable elevation macOS requires for this (creating
# a system account and a LaunchDaemon) happens there, once, with PrivacyFence's
# own explanatory text (installer/macos/pkg/resources/*.html), instead of an
# unexplained dialog appearing later at some unrelated moment. See ADR 0002 and
# this repo's CLAUDE.md.
#
# **This package is not released on its own.** scripts/build_dmg.sh calls this
# script and puts the resulting .pkg inside the DMG, next to PrivacyFence.mcpb;
# that DMG is the only macOS artifact that ships. The .pkg used to be a second,
# separately-downloadable artifact alongside a drag-install DMG, which is what
# made its conclusion screen's "open <mcpb>, next to this installer" line a lie
# for anyone who downloaded the .pkg by itself -- there was no .mcpb next to it.
# Carrying both in one DMG makes that sentence true and leaves exactly one macOS
# download to explain.
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
# runs PyInstaller itself, so it only ever runs after scripts/build_dmg.sh's
# own PyInstaller step (which is why that script calls this one rather than
# the other way round).
#
# Prerequisites (build machine only):
#   dist/PrivacyFenceApp.app already built -- scripts/build_dmg.sh does this
#   pip install -e .   # so VERSION below can read installed metadata
#
# Usage (normally: don't -- run scripts/build_dmg.sh, which calls this):
#   ./scripts/build_pkg.sh [--sign "Developer ID Installer: Your Name (TEAMID)"]
#
# Note the certificate *type*: pkg signing needs a "Developer ID Installer"
# identity, not the "Developer ID Application" one scripts/build_dmg.sh's
# own --sign signs the .app/.dmg with -- Apple issues them separately, and
# productsign (not codesign) is what actually consumes this one. That is also
# why the environment variable read below is SIGN_IDENTITY_INSTALLER and not
# SIGN_IDENTITY: this script runs as a child of scripts/build_dmg.sh, whose
# own SIGN_IDENTITY is in the environment and is the wrong certificate type.
# Signing with it would fail productsign outright at best, so it is never
# consulted here.
#
# Output: dist/PrivacyFence-<version>.pkg (consumed by scripts/build_dmg.sh)
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
COMPONENT_PLIST="${BUILD_DIR}/component.plist"
DIST_XML="${BUILD_DIR}/distribution.xml"
PKG_NAME="${PRODUCT_NAME}-${VERSION}.pkg"
PKG_PATH="dist/${PKG_NAME}"

[ -d "$BUNDLE" ] || {
  echo "error: ${BUNDLE} not found -- run scripts/build_dmg.sh, which builds it and then calls this script (this one never runs PyInstaller itself)" >&2
  exit 1
}

# --sign wins over the environment; SIGN_IDENTITY_INSTALLER is how
# scripts/build_dmg.sh (and build.yml's job-level env) passes it in. Never
# SIGN_IDENTITY -- see this script's own header comment on the certificate type.
SIGN_IDENTITY="${SIGN_IDENTITY_INSTALLER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --sign) SIGN_IDENTITY="${2:-}"; shift 2 ;;
    *) echo "error: unknown argument: $1" >&2; exit 2 ;;
  esac
done

# The installer's OS floor is the app's own: PrivacyFenceApp.spec's LSMinimumSystemVersion (the
# support matrix in docs/platform-support.md; tests/unit/test_minimum_os_versions.py keeps them in
# step). Read back from the built bundle rather than restated, so the .pkg refuses exactly the
# systems the app would refuse to launch on -- without it, Installer.app would install an app
# Launch Services then won't open.
MIN_MACOS=$(plutil -extract LSMinimumSystemVersion raw "${BUNDLE}/Contents/Info.plist")

# PrivacyFence ships for Apple silicon only (docs/platform-support.md's support matrix):
# PyInstaller builds for the architecture it runs on, and the release runner is arm64. The .pkg
# says so through hostArchitectures below, so Installer.app refuses an Intel Mac instead of
# installing an app it cannot run. Refuse to package a bundle built for anything else.
HOST_ARCH="arm64"
BUNDLE_ARCHS=$(lipo -archs "${BUNDLE}/Contents/MacOS/${APP_NAME}")
[ "$BUNDLE_ARCHS" = "$HOST_ARCH" ] || {
  echo "error: ${BUNDLE} is built for '${BUNDLE_ARCHS}', but the .pkg ships for ${HOST_ARCH} only -- build on Apple silicon" >&2
  exit 1
}

echo "=== Building ${PRODUCT_NAME} ${VERSION} installer package (macOS ${MIN_MACOS}+, ${HOST_ARCH}) ==="

# ── 1. Stage a clean package root ─────────────────────────────────────────
# Not `--root dist` directly: dist/ also holds the .mcpb this same build
# produces (and, once scripts/build_dmg.sh wraps this .pkg up, the DMG
# itself), none of which belongs under /Applications. ditto (not
# cp -R) to preserve the .app bundle's extended attributes and, if it was
# signed, its code signature intact.
echo "→ Staging package root…"
rm -rf "$BUILD_DIR"
mkdir -p "$PKG_ROOT" "$SCRIPTS_DIR" "$RESOURCES_DIR"
ditto "$BUNDLE" "${PKG_ROOT}/${APP_NAME}.app"

cp -p installer/macos/pkg/postinstall "${SCRIPTS_DIR}/postinstall"
chmod +x "${SCRIPTS_DIR}/postinstall"

cp -p installer/macos/pkg/resources/welcome.html "${RESOURCES_DIR}/welcome.html"
# The conclusion screen tells the user to open the .mcpb sitting next to this
# installer, so the name baked in has to be the name the extension has *in the
# DMG* -- not dist/'s versioned PrivacyFence-<version>.mcpb. scripts/build_dmg.sh
# passes its own stable in-DMG name through MCPB_DMG_NAME; the default below
# duplicates it only so a standalone run of this script still renders something
# true (both names are "PrivacyFence.mcpb" -- change one, change the other).
MCPB_NAME="${MCPB_DMG_NAME:-${PRODUCT_NAME}.mcpb}"
sed -e "s|__MCPB_NAME__|${MCPB_NAME}|g" \
  installer/macos/pkg/resources/conclusion.html.tmpl > "${RESOURCES_DIR}/conclusion.html"

# ── 2. Build the component package ────────────────────────────────────────
# Default --ownership recommended (no flag needed) leaves the payload
# root:wheel -- see this script's own header comment for why that matters.
#
# --component-plist, with BundleIsRelocatable turned off, is not optional and
# not a nicety. pkgbuild marks every bundle in the payload relocatable by
# default, which tells installd: before installing, ask Launch Services where
# a bundle with this identifier already lives, and if it finds one, install
# over *that* copy and ignore --install-location entirely. For an app whose
# identifier the user has ever launched from anywhere else -- a Downloads
# folder, a still-mounted DMG, a build tree -- that silently redirects the
# whole install away from /Applications.
#
# This is not hypothetical here. It is what
# test_macos_pkg_install.py::test_pkg_install_enables_privilege_separation_
# with_no_manual_step had been failing on for weeks (#562, mis-diagnosed as a
# payload-visibility flake, which is why it looked intermittent -- it tracks
# whether Launch Services happens to have registered another copy yet):
#
#   installd: PackageKit: Applications/PrivacyFenceApp.app relocated to
#             Users/runner/work/.../dist/PrivacyFenceApp.app
#   ./postinstall: PrivacyFence postinstall: /Applications/PrivacyFenceApp.app
#             /Contents/Resources/scripts/macos_privilege_separation.sh not
#             found or not executable -- leaving privilege separation opt-in.
#
# `installer` reports "The install was successful" throughout, because from
# its point of view it was. The damage is the second line: installer/macos/pkg/
# postinstall resolves the separation script under the literal
# /Applications path, so a relocated install also quietly skips provisioning
# privilege separation -- and ADR 0003 decision 6 then refuses to serve the
# unseparated install it leaves behind. A user who once ran the app from their
# Downloads folder gets both halves of that.
echo "→ Analyzing package root (to turn off bundle relocation)…"
pkgbuild --analyze --root "$PKG_ROOT" "$COMPONENT_PLIST"
# One staged .app, one BundleIsRelocatable to clear (see step 1) -- if that
# ever stops being true, the index below is silently wrong for the rest, so
# fail rather than half-apply it.
bundle_count="$(/usr/libexec/PlistBuddy -c 'Print :' "$COMPONENT_PLIST" | grep -c 'BundleIsRelocatable' || true)"
if [ "$bundle_count" != "1" ]; then
  echo "error: expected exactly one bundle in ${PKG_ROOT}, found ${bundle_count} -- update the BundleIsRelocatable handling below" >&2
  exit 1
fi
/usr/libexec/PlistBuddy -c 'Set :0:BundleIsRelocatable false' "$COMPONENT_PLIST"

echo "→ Building component package…"
pkgbuild \
  --root "$PKG_ROOT" \
  --component-plist "$COMPONENT_PLIST" \
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
    <options customize="never" require-scripts="true" rootVolumeOnly="true" hostArchitectures="${HOST_ARCH}"/>
    <allowed-os-versions>
        <os-version min="${MIN_MACOS}"/>
    </allowed-os-versions>
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
